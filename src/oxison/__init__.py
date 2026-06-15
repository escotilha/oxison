"""oxison — point it at a repo, get product docs back.

oxison clones nothing and mutates nothing in the target repo. It reads
a local repository, comprehends it by driving the Claude Code CLI as a
**read-only** subprocess, and writes product artifacts (PRODUCT,
MANUAL, STACK, and a roadmap-or-security follow-on) into its own
output directory.

The #1 invariant: the comprehension/planning workers are launched read-only
(``Read,Glob,Grep`` — no shell, no write tools, so they are structurally
incapable of mutating or executing) and oxison itself owns every file write,
exclusively into ``./oxison-output/``. (The Oxfaz *build* worker is the one
deliberate exception — it writes code in an isolated worktree; see its docs.)
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
