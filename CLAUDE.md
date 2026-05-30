# Sparkle

Claim-graph research MVP with content-addressed provenance. Pure Python 3.11+, zero external dependencies in the core. An optional `sparkle[mcp]` extra pulls the MCP SDK for the agent-facing server; the core never depends on it.

## Project structure

- `src/sparkle/` — package
  - `models.py`, `graph.py`, `templates.py`, `bootstrap.py`, `presentation.py` — the stdlib-pure kernel
  - `ops.py` — the operations contract: every graph action as a function returning JSON-able data; the debate invariants, the referee/transition engine, the dedup gate, the alias sidecar, the frontier, the run manifest (the per-run configured role-to-family/model roster under the top-level `runs` key, written/read via `write_run_manifest`/`run_manifest`), and the advisory file lock all live here. The single surface every front-end calls.
  - `cli.py` — a thin formatter; each command makes one `ops.py` call and prints
  - `mcp_server.py` — MCP server, a thin FastMCP adapter over `ops.py` (only imported by the lazy `sparkle mcp` subcommand, so the core stays dependency-free)
- `tests/test_cli.py` — integration tests via unittest
- `docs/` — PRD and roadmap
- `demo/` — music-and-coding claim graph walkthrough with mermaid visualization
- `.sparkle/graph.json` — local graph data (not committed); `.sparkle/aliases.json` holds user-chosen names outside the hashed payload

## Key conventions

- **Immutable data model**: Node and Edge are frozen dataclasses with SHA256 content-addressed IDs. Editing is model-honest supersession (`revise`/`rule`), never in-place mutation.
- **The `ops.py` seam is the keystone**: the CLI and the MCP server never touch the store directly. Both bottom out in `ops.py` behind one `ValueError` boundary, so the debate rules are enforced once and the front-ends (CLI, MCP, future harness) stay interchangeable. Put any new invariant in `ops.py`, never in a front-end.
- **Author-distinct floor is a per-caller choice at the seam**: `ops.rule` takes `require_distinct_adversary` (default `False`). The two agent-driven front-ends turn it on — the autonomous harness and the MCP server's `sparkle_rule` both pass `True`, so a single agent cannot ratify its own claim off a self-written objection (a claim only counts as challenged when an objection comes from a different author). The trusted human `sparkle rule` CLI path leaves it off (a human is trusted to challenge their own claim honestly).
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
  - "Where It's Going", "Intelligent Operation (MCP)", and "Autonomous Operation (`sparkle run`)" match what shipped (interactive operator works; the autonomous harness now runs live end-to-end, validated by hand on 2026-05-29, not gated/not-built). The autonomous section documents that the evidence-gatherer role (and only it) has real web search/fetch to verify sources and cite retrieved URLs, while every other role carries the recall-honesty mandate (tag any recalled figure `(recalled, unverified)`, judge discounts unverifiable specifics); the MCP section documents that the agent-driven rule move enforces the author-distinct floor (a single agent can't self-strawman to ratify), while the trusted human CLI rule stays permissive
  - Graph model status list matches `NodeStatus` in `models.py` (including `ratified` as terminal); the live `judged` signal (a ruling exists but did not settle the claim) is documented as distinct from `ratified` (actually settled)
  - Class diagram: fields and methods on Node, Edge, GraphStore, BranchTemplate match code
  - Sequence diagram: add-branch flow matches `cli.py` call sequence
  - State diagram: status values match `NodeStatus`, including the `ratified` terminal transition
  - Source layout: all files in `src/sparkle/` listed, including `ops.py` and `mcp_server.py`, with the `ops.py` seam called out
  - CLI Reference: all subcommands documented (including `relations`, `revise`, `import`, `mcp`, `run`, and `run-manifest`) with the `--link-to`/`--relation`, `--as`, and `--ids-only` flags, plus `run`'s `--rounds`/`--max-moves`/`--run-id` and the `SPARKLE_CODEX_REASONING_EFFORT` env knob; `run` output is documented as showing a `Moves:` count (not `Rounds:`) and a `Cross-family:` line, and `run-manifest <run_id>` reads back the configured role-to-family/model roster
  - Testing section: test count and coverage list match actual test methods (the live cross-family debate is validated by hand, not by the suite), including the run-manifest round-trip / id-stability tests and the engine reporting `moves_made` (never `rounds_run`)
  - "Current Limits" and "Where It's Going": nothing listed that's already implemented; the live debate runs but is token-heavy. Keep two distinct points straight and do not conflate them: the verdict-rejection floor (a judge may not ratify a claim its own verdict rejected) is STRUCTURAL on the autonomous path (settle is honored only for an `upheld` verdict), while net=0 ratification is INTENTIONAL by design — once a cross-family objection exists and the judge upholds with settle requested, the judge MAY settle on tied evidence because it weighs the debate rather than counting votes; frame this as "the judge's reasoning is sovereign; ratification is not gated on net-positive support," NOT as a missing floor (note that a 0-support AND 0-objection claim still cannot ratify, because the cross-family gate needs a challenge first). The cross-family setup is auditable from the saved graph via `sparkle run-manifest`, with the honest caveat that the manifest records the CONFIGURED roster, not a runtime probe of which model answered each call. The autonomous-side Claude lockdown is documented as deny-all (`--tools ""` plus an explicit deny for the one surviving tool, plus `--setting-sources user`, `--permission-mode default`, `--strict-mcp-config`, throwaway tempdir), NOT the old 7-tool deny-list, with the single exception that the evidence gatherer is granted exactly the web search/fetch pair (`--tools WebSearch WebFetch` + `--allowedTools WebSearch WebFetch`) so it can verify sources; a mid-run model-CLI failure is documented as one `error` move that keeps the loop going (not a crash). The old "no external evidence / recalled-citation" limit is reframed as PARTIALLY closed: the evidence role verifies against the real web while the proposer/critic/judge/synthesizer still reason from memory but must flag recalled specifics and the judge discounts them — frame it as a source-checked-evidence debate with recalled claims flagged, NOT a full fact-checker
- `docs/prd.md`
  - "MVP status" matches what's built (ops.py seam, ratified status, MCP server/extra, interactive operator, revise/import/aliases/dedup, the working autonomous loop including its cost knobs and the `judged` signal, the honest `Moves:` count, and the saved-graph cross-family audit via the `runs` manifest key — `sparkle run-manifest` / `sparkle_run_manifest` / the `sparkle://run/{run_id}/manifest` resource — described as the CONFIGURED roster, not a runtime model probe), plus the evidence-role web search + recall-honesty mandate (evidence verifies real sources; other roles flag recalled specifics, judge discounts them; partial not full fact-checking) and the agent-driven MCP rule author-distinct enforcement (a single agent can't self-strawman; human CLI rule stays permissive)
  - "Next usability focus" only lists unbuilt work (`--format json`, standalone introspection, undo, session logging); autonomy is built, so it must not appear here as future work. Net=0 ratification is framed as an INTENTIONAL design choice (the judge weighs, it does not count votes; ratification is not gated on net-positive support), NOT a missing floor, and kept distinct from the structural verdict-rejection floor. The MVP-status list documents the untrusted JSON import (terminal-status guard, `trusted=True` opt-in only for export round-trips) and the deny-all Claude lockdown
- `docs/roadmap.md`
  - "Completed" includes all shipped work, including the Phase 2 autonomous harness as shipped and running live, the honest `Moves:` count (not `Rounds:`), the saved-graph cross-family audit (the `runs` manifest key via `sparkle run-manifest` / `sparkle_run_manifest` / the manifest resource), described as the configured roster rather than a runtime model probe, the evidence-role web search + recall-honesty mandate (partial source-checking, recalled specifics flagged and discounted), and the MCP self-strawman hardening (the agent-driven rule move enforces the author-distinct floor; human CLI rule stays permissive)
  - "Next" leads with the still-unbuilt polish track (Phase 2b: `--format json`, query language, introspection, undo, session logging); it must not list the autonomous harness as gated/not-done, and net=0 ratification (intentional by design) must not appear as unbuilt/next work
- `CLAUDE.md` (this file)
  - Project structure, key conventions, and the running section reflect `ops.py`, `mcp_server.py`, and the MCP extra/command
