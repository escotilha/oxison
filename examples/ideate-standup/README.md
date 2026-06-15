# Example — greenfield `oxison ideate` (from an idea, no repo)

Real, unedited output from running `oxison ideate` on a one-paragraph idea plus a
fetched website link — **no repository involved**. It shows what greenfield mode
produces: a synthesized understanding, a product spec, and a from-scratch roadmap.

## How it was generated

```bash
oxison ideate --brief-file brief.txt --url https://example.com --model claude-sonnet-4-6
```

- **Input:** the idea in [`brief.txt`](brief.txt) + one fetched URL (the brief became a `brief:idea` source; the page became a `web:` source)
- **Model:** `claude-sonnet-4-6`
- **Generated:** 2026-06-15 with oxison v0.1.0
- **Cost:** ~$0.28 across 3 read-only AI calls (comprehend + PRODUCT + plan)
- No repo existed and nothing was scaffolded or built — greenfield is plan-only.

## What's here

| File | What it is |
|---|---|
| [`brief.txt`](brief.txt) | The one-paragraph project idea — the only required input |
| [`COMPREHENSION.md`](COMPREHENSION.md) | The synthesized understanding of the proposed product |
| [`PRODUCT.md`](PRODUCT.md) | The product to build — vision, users, features, non-goals |
| [`ROADMAP.md`](ROADMAP.md) | A sequenced, from-scratch build plan (9 tasks, each with observable acceptance criteria) |

A run also emits `comprehension.json` + `roadmap.json` (the machine-readable
contracts that feed `oxison build`); they're omitted here for brevity.

> Regenerate anytime: `oxison ideate --brief-file brief.txt`.
