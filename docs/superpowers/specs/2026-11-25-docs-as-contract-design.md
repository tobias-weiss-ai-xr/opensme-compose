# Design: docs-as-contract guard

## Context

`docs/README.md` declares “Keep the index current” and the README has its
own `## Documentation` table. Both are hand-maintained, so new operator docs
silently disappear from the index (9 of 29 top-level docs were unindexed on
2026-11-25). The Roadmap lists “docs-as-contract” as a backlog idea; this
change turns it into an enforced Layer-0 invariant.

## Goals

- Every top-level `docs/*.md` is reachable from at least one of the two
  indexes (`docs/README.md`, README `## Documentation` section).
- Every `docs/*.md` link in those sections resolves to a file that exists.
- A future edit that removes an index row fails the static gate.

## Non-goals

- Indexing documents inside subdirectories (`docs/perf/`, `docs/audits/`,
  `docs/superpowers/`). Those are curated by their own parents or are
  working notes; recursing would force plans and reports into the operator
  index.
- Rewriting prose or moving files.

## Architecture

Single Python Layer-0 check `tests/00-static/check_docs_index.py`, following
the `check_cloud_bridge.py` pattern: `Result` from `tests/conftest.py`,
`ROOT` derived from `__file__` (so the mutation harness's shadow repo works),
plain `re`/`pathlib`, no new dependencies.

1. Discover coverage targets: `sorted(Path("docs").glob("*.md"))` minus
   `docs/README.md`.
2. Extract link targets from all of `docs/README.md` and only the README
   `## Documentation` section (up to the next `## ` heading).
3. Normalize relative links to repo-relative `docs/...md` paths; ignore
   anchors and external URLs.
4. Check A (coverage): each discovered doc appears in the union of link
   targets; one `ok`/`fail` per doc.
5. Check B (link validity): each normalized link target exists; one
   `ok`/`fail` per link.
6. Mutation entry in `check_mutations.py` flips one indexed link target;
   the check must go red in the shadow repo.

## Files

- Create: `tests/00-static/check_docs_index.py`
- Modify: `tests/run.py` (register in Layer 0),
  `tests/00-static/check_mutations.py` (mutation + coverage),
  `docs/VALIDATION.md` (inventory line + mutation count),
  `docs/README.md` (add the 9 currently unindexed docs),
  `docs/ROADMAP.md` (close the docs-as-contract backlog item),
  `docs/superpowers/plans/2026-11-25-docs-as-contract.md` (execution record).

## Testing

- `python3 tests/00-static/check_docs_index.py` — green after index updates.
- `python3 tests/00-static/check_mutations.py` — new mutation caught.
- `python3 tests/run.py --static` — full gate green (31 → 32 suites).

## Decisions

- “At least one index” rather than “both indexes”: the README table is a
  curated operator shortlist; forcing every historical report into it would
  hurt readability. `docs/README.md` is the normative full index.
- Anchors (`file.md#section`) count as a link to that document.
- Top-level only: subdirectories keep their own index conventions.
