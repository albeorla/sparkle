# Sparkle

Claim-graph research MVP with content-addressed provenance. Pure Python 3.11+, zero external dependencies in the core. An optional `sparkle[mcp]` extra pulls the MCP SDK for the agent-facing server; the core never depends on it.

## Project structure

- `src/sparkle/` — package
  - `models.py`, `graph.py`, `templates.py`, `bootstrap.py`, `presentation.py` — the stdlib-pure kernel
  - `ops.py` — the operations contract: every graph action as a function returning JSON-able data; the debate invariants, the referee/transition engine, the dedup gate, the alias sidecar, the frontier, and the advisory file lock all live here. The single surface every front-end calls.
  - `cli.py` — a thin formatter; each command makes one `ops.py` call and prints
  - `mcp_server.py` — MCP server, a thin FastMCP adapter over `ops.py` (only imported by the lazy `sparkle mcp` subcommand, so the core stays dependency-free)
- `tests/test_cli.py` — integration tests via unittest
- `docs/` — PRD and roadmap
- `demo/` — music-and-coding claim graph walkthrough with mermaid visualization
- `.sparkle/graph.json` — local graph data (not committed); `.sparkle/aliases.json` holds user-chosen names outside the hashed payload

## Key conventions

- **Immutable data model**: Node and Edge are frozen dataclasses with SHA256 content-addressed IDs. Editing is model-honest supersession (`revise`/`rule`), never in-place mutation.
- **The `ops.py` seam is the keystone**: the CLI and the MCP server never touch the store directly. Both bottom out in `ops.py` behind one `ValueError` boundary, so the debate rules are enforced once and the front-ends (CLI, MCP, future harness) stay interchangeable. Put any new invariant in `ops.py`, never in a front-end.
- **Core stays dependency-free**: stdlib only (dataclasses, json, pathlib, hashlib, argparse, collections, contextlib, fcntl). The MCP SDK loads only with the optional extra.
- **Validation at boundaries**: confidence must be [0.0, 1.0], node types and statuses validated via argparse choices; `ratified` is terminal and only a settled ruling may write it.
- **Single JSON store**: all reads/writes go through `GraphStore._read()` / `._write()`, wrapped by an advisory file lock (`ops.graph_lock`) for the "one front-end at a time per graph" contract.
- **Live signals, not stored confidence**: the frontier and status signals are computed each read from the inbound supports-minus-contradicts tally plus node type/status; the frozen stored confidence is never used as a bucket key and never implied to have changed.
- **No "@last" token**: whole-second timestamps have no tiebreaker, so always link by explicit short prefix or alias name.
- **Error handling**: `ValueError` for user-facing errors, caught in `cli.main()` and printed to stderr with exit code 2.

## Running

```bash
pip install -e .                                  # installs the `sparkle` console command
pip install -e '.[mcp]'                            # also pulls the MCP SDK for `sparkle mcp`
sparkle <command>                                 # console entry point (after install)

PYTHONPATH=src python3 -m sparkle <command>       # via __main__.py (no install)
python3 -m src.sparkle.cli <command>              # direct module

sparkle mcp                                        # run the MCP server over stdin/stdout (needs the extra)
claude mcp add sparkle -- sparkle mcp              # register the server with a client
```

Note: `python3 -m sparkle` needs `PYTHONPATH=src` because the package lives under `src/`.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

All tests must pass before committing. Tests use temp directories — no side effects.

## Docs

Four docs must stay in sync with code changes: `README.md`, `docs/prd.md`, `docs/roadmap.md`, and this file. Use `/doc-sync` after any feature or structural change. (The `demo/` walkthrough is refreshed separately and is out of scope for routine doc-sync unless explicitly asked.)

Documentation sources and what to check in each:

- `README.md`
  - "Where It's Going" table and "Intelligent Operation (MCP)" section match what shipped (interactive operator works; autonomous harness does not)
  - Graph model status list matches `NodeStatus` in `models.py` (including `ratified` as terminal)
  - Class diagram: fields and methods on Node, Edge, GraphStore, BranchTemplate match code
  - Sequence diagram: add-branch flow matches `cli.py` call sequence
  - State diagram: status values match `NodeStatus`, including the `ratified` terminal transition
  - Source layout: all files in `src/sparkle/` listed, including `ops.py` and `mcp_server.py`, with the `ops.py` seam called out
  - CLI Reference: all subcommands documented (including `relations`, `revise`, `import`, `mcp`) with the `--link-to`/`--relation`, `--as`, and `--ids-only` flags
  - Testing section: test count and coverage list match actual test methods
  - "Current Limits" and "Where It's Going": nothing listed that's already implemented
- `docs/prd.md`
  - "MVP status" matches what's built (ops.py seam, ratified status, MCP server/extra, interactive operator, revise/import/aliases/dedup)
  - "Next usability focus" only lists unbuilt work (autonomy, `--format json`, standalone introspection, undo, session logging)
- `docs/roadmap.md`
  - "Completed" includes all shipped work
  - "Next" leads with the autonomous harness (Phase 2) as gated/not-done; phase sections only list unbuilt items
- `CLAUDE.md` (this file)
  - Project structure, key conventions, and the running section reflect `ops.py`, `mcp_server.py`, and the MCP extra/command
