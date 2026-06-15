# Example output — oxison documenting itself

This directory is **real, unedited output** from running oxison on its own
repository (dogfooding). It shows what the tool actually produces.

## How it was generated

```bash
oxison run . --model claude-sonnet-4-6
```

- **Target:** the oxison repo itself (117 files, ~35.8k estimated tokens → single-pass comprehension)
- **Model:** `claude-sonnet-4-6`
- **Generated:** 2026-06-15 with oxison v0.1.0
- **Cost:** ~$2.47 across 5 read-only AI calls (comprehend + PRODUCT + MANUAL + STACK + the branch doc), as reported by oxison itself
- The target repo was left **byte-for-byte unchanged** — oxison only ever writes into its own output directory.

## What's here

| File | What it is |
|---|---|
| [`PRODUCT.md`](PRODUCT.md) | What oxison is, who it's for, core features, mental model |
| [`MANUAL.md`](MANUAL.md) | Prerequisites, install, configuration, usage, workflows |
| [`STACK.md`](STACK.md) | Languages, dependencies, runtime, infra — grounded in the manifests |
| [`SECURITY-NOTES.md`](SECURITY-NOTES.md) | The branch doc: a read-only security-surface scan (oxison emits this when the repo has no roadmap) |
| [`COMPREHENSION.md`](COMPREHENSION.md) | The intermediate whole-repo understanding the docs are built from |

A run also emits the structured `comprehension.json` and `repomap.json`
artifacts (the machine-readable contract for downstream tooling); they're
omitted here only because they embed the absolute path of the machine that
generated them.

> These docs are a snapshot — regenerate anytime with `oxison run .` to see the
> current state of the codebase.
