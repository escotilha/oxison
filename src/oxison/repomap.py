"""Deterministic, read-only repository map — no AI, no writes.

Before spending a single token, oxison builds a structured ``RepoMap``
of the target: language histogram, dependency manifests, entry-point
heuristics, a pruned directory tree, and external-service hints. This
map is the AI workers' starting context (so they don't re-discover the
basics) and it grounds the STACK doc (deps/versions come from here, not
from the model's imagination).

Design (mirrors oxi-core's ``compute_probe`` philosophy):
- **Read-only.** Walks and reads files; never writes.
- **Best-effort.** A missing or unparseable manifest degrades to an
  empty entry, never an exception.
- **Bounded.** Skips vendor/build dirs; caps walk breadth so a huge
  monorepo can't hang the map step.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Directories never worth walking — vendored deps, build output, VCS.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".next",
        ".nuxt",
        "target",
        ".idea",
        ".vscode",
        ".terraform",
        "coverage",
        "htmlcov",
    }
)

# Extension -> human language name (for the histogram).
_LANG_BY_EXT: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C",
    ".h": "C/C++ header",
    ".cpp": "C++",
    ".cc": "C++",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".md": "Markdown",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
}

# Manifest filenames -> ecosystem label.
_MANIFEST_FILES: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java-maven",
    "build.gradle": "java-gradle",
    "Gemfile": "ruby",
    "composer.json": "php",
    "*.csproj": "dotnet",
}

# Common entry-point filenames.
_ENTRY_HINTS: frozenset[str] = frozenset(
    {"main.py", "manage.py", "app.py", "__main__.py", "index.js", "index.ts", "main.go", "main.rs"}
)

# Max files to count before stopping the histogram walk (safety bound).
_MAX_FILES_SCANNED = 50_000


@dataclass
class LangStat:
    files: int = 0
    loc: int = 0


@dataclass
class ManifestInfo:
    path: str
    ecosystem: str
    dependencies: list[str] = field(default_factory=list)


@dataclass
class RepoMap:
    root: str
    languages: dict[str, LangStat]
    manifests: list[ManifestInfo]
    entry_points: list[str]
    tree: list[str]
    services: list[str]
    total_files: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "root": self.root,
                "languages": {k: asdict(v) for k, v in self.languages.items()},
                "manifests": [asdict(m) for m in self.manifests],
                "entry_points": self.entry_points,
                "tree": self.tree,
                "services": self.services,
                "total_files": self.total_files,
            },
            indent=2,
            sort_keys=True,
        )

    def to_context(self) -> str:
        """Compact human/AI-readable summary for prompts."""
        langs = ", ".join(
            f"{name} ({s.files} files, {s.loc} LOC)"
            for name, s in sorted(self.languages.items(), key=lambda kv: -kv[1].loc)[:8]
        )
        deps = []
        for m in self.manifests:
            if m.dependencies:
                deps.append(f"  {m.path} ({m.ecosystem}): {', '.join(m.dependencies[:25])}")
        lines = [
            f"Repository: {self.root}",
            f"Total files (scanned): {self.total_files}",
            f"Languages: {langs or '(none detected)'}",
            f"Entry points: {', '.join(self.entry_points) or '(none detected)'}",
            f"Services/infra hints: {', '.join(self.services) or '(none detected)'}",
            "Dependency manifests:",
            *(deps or ["  (none detected)"]),
            "Top-level structure:",
            *(f"  {line}" for line in self.tree[:40]),
        ]
        return "\n".join(lines)


def _count_loc(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield files under root, skipping vendor/build dirs."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def _deps_from_pyproject(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    deps: list[str] = list(data.get("project", {}).get("dependencies", []))
    return [_dep_name(d) for d in deps]


def _deps_from_cargo(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return list(data.get("dependencies", {}).keys())


def _deps_from_package_json(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for key in ("dependencies", "devDependencies"):
        out.extend(data.get(key, {}).keys())
    return out


def _deps_from_requirements(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith(("#", "-")):
            out.append(_dep_name(line))
    return out


def _dep_name(spec: str) -> str:
    """Strip version constraints/extras from a dependency spec."""
    for sep in ("==", ">=", "<=", "~=", ">", "<", "[", " ", ";"):
        idx = spec.find(sep)
        if idx > 0:
            spec = spec[:idx]
    return spec.strip()


def _collect_manifest(path: Path, ecosystem: str, root: Path) -> ManifestInfo:
    rel = str(path.relative_to(root))
    name = path.name
    if name == "pyproject.toml":
        deps = _deps_from_pyproject(path)
    elif name == "Cargo.toml":
        deps = _deps_from_cargo(path)
    elif name == "package.json":
        deps = _deps_from_package_json(path)
    elif name == "requirements.txt":
        deps = _deps_from_requirements(path)
    else:
        deps = []
    return ManifestInfo(path=rel, ecosystem=ecosystem, dependencies=deps)


def _detect_services(root: Path) -> list[str]:
    """Best-effort external-service / infra hints."""
    services: list[str] = []
    if (root / "Dockerfile").exists():
        services.append("Docker")
    for compose in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        if (root / compose).exists():
            services.append("docker-compose")
            break
    if (root / "k8s").is_dir() or (root / "kubernetes").is_dir():
        services.append("Kubernetes")
    if any((root / f).exists() for f in (".github/workflows", ".gitlab-ci.yml")):
        services.append("CI/CD")
    env_example = root / ".env.example"
    if env_example.exists():
        try:
            keys = [
                ln.split("=", 1)[0].strip()
                for ln in env_example.read_text(encoding="utf-8").splitlines()
                if "=" in ln and not ln.strip().startswith("#")
            ]
            if keys:
                services.append(f"env keys: {', '.join(keys[:12])}")
        except OSError:
            pass
    return services


def _top_level_tree(root: Path) -> list[str]:
    """One-line-per-entry summary of the top two levels."""
    out: list[str] = []
    try:
        top = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return out
    for entry in top:
        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            out.append(f"{entry.name}/")
        else:
            out.append(entry.name)
    return out


def build_repo_map(target: Path) -> RepoMap:
    """Build a deterministic, read-only map of the target repo."""
    root = target.resolve()
    languages: dict[str, LangStat] = {}
    manifests: list[ManifestInfo] = []
    entry_points: list[str] = []
    total = 0
    seen_manifest_paths: set[str] = set()

    csproj_seen = False
    for file in _iter_files(root):
        total += 1
        if total > _MAX_FILES_SCANNED:
            break
        ext = file.suffix.lower()
        lang = _LANG_BY_EXT.get(ext)
        if lang:
            stat = languages.setdefault(lang, LangStat())
            stat.files += 1
            stat.loc += _count_loc(file)
        name = file.name
        if name in _MANIFEST_FILES and name not in seen_manifest_paths:
            manifests.append(_collect_manifest(file, _MANIFEST_FILES[name], root))
            seen_manifest_paths.add(str(file))
        elif ext == ".csproj" and not csproj_seen:
            manifests.append(ManifestInfo(path=str(file.relative_to(root)), ecosystem="dotnet"))
            csproj_seen = True
        if name in _ENTRY_HINTS:
            entry_points.append(str(file.relative_to(root)))

    # Top-level scripts from pyproject as entry points, too.
    for m in manifests:
        if m.path == "pyproject.toml":
            try:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                scripts = data.get("project", {}).get("scripts", {})
                entry_points.extend(f"script:{k}" for k in scripts)
            except (OSError, tomllib.TOMLDecodeError):
                pass

    return RepoMap(
        root=str(root),
        languages=languages,
        manifests=manifests,
        entry_points=sorted(set(entry_points)),
        tree=_top_level_tree(root),
        services=_detect_services(root),
        total_files=total,
    )


def estimate_tokens(repo_map: RepoMap) -> int:
    """Rough token estimate for the repo's comprehensible surface.

    Heuristic: total LOC across detected languages × ~10 chars/line ÷ 4
    chars/token, plus the map context itself. Deliberately conservative
    (over-estimates) so the chunker errs toward map-reduce.
    """
    total_loc = sum(s.loc for s in repo_map.languages.values())
    code_tokens = (total_loc * 10) // 4
    map_tokens = len(repo_map.to_context()) // 4
    return int(code_tokens + map_tokens)


def top_level_dirs(target: Path) -> list[str]:
    """Walkable top-level directories (for slicing in map-reduce)."""
    root = target.resolve()
    dirs: list[str] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                dirs.append(entry.name)
    except OSError:
        pass
    return dirs


__all__ = [
    "LangStat",
    "ManifestInfo",
    "RepoMap",
    "build_repo_map",
    "estimate_tokens",
    "top_level_dirs",
]
