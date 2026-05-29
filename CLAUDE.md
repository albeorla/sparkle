# Sparkle

Claim-graph research MVP with content-addressed provenance. Pure Python 3.11+, zero external dependencies.

## Project structure

- `src/sparkle/` — core package (models, graph store, CLI, templates, bootstrap)
- `tests/test_cli.py` — integration tests via unittest
- `docs/` — PRD and roadmap
- `demo/` — music-and-coding claim graph walkthrough with mermaid visualization
- `.sparkle/graph.json` — local graph data (not committed)

## Key conventions

- **Immutable data model**: Node and Edge are frozen dataclasses with SHA256 content-addressed IDs
- **No external dependencies**: stdlib only (dataclasses, json, pathlib, hashlib, argparse, collections)
- **Validation at boundaries**: confidence must be [0.0, 1.0], node types and statuses validated via argparse choices
- **Single JSON store**: all reads/writes go through `GraphStore._read()` / `._write()`
- **Error handling**: `ValueError` for user-facing errors, caught in `cli.main()` and printed to stderr with exit code 2

## Running

```bash
pip install -e .                                  # installs the `sparkle` console command
sparkle <command>                                 # console entry point (after install)

PYTHONPATH=src python3 -m sparkle <command>       # via __main__.py (no install)
python3 -m src.sparkle.cli <command>              # direct module
```

Note: `python3 -m sparkle` needs `PYTHONPATH=src` because the package lives under `src/`.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

All tests must pass before committing. Tests use temp directories — no side effects.

## Docs

Three docs must stay in sync with code changes. Use `/doc-sync` after any feature or structural change.

Documentation sources and what to check in each:

- `README.md`
  - "What It Does" list matches implemented features
  - Class diagram: fields and methods on Node, Edge, GraphStore, BranchTemplate match code
  - Sequence diagram: add-branch flow matches `cli.py` call sequence
  - State diagram: status values match `NodeStatus` in `models.py`
  - Source layout: all files in `src/sparkle/` listed
  - CLI section: all subcommands documented with correct examples
  - Testing section: coverage list matches actual test methods
  - "Current Limits" and "Next": nothing listed that's already implemented
- `docs/prd.md`
  - "MVP status" matches what's built
  - "Next usability focus" only lists unbuilt work
- `docs/roadmap.md`
  - "Completed" includes all shipped work
  - Phase sections only list unbuilt items
