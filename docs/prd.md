# PRD

## Product

Sparkle is a solo research tool for building inspectable claims with provenance.

## Problem

Current notes workflows make it easy to collect fragments and hard to answer:

- what supports a claim
- what weakens a claim
- what alternative paths were explored
- why a path was abandoned or harvested
- how a final output traces back to evidence

## Product goal

Help a solo researcher build a claim graph where evidence, objections, questions, decisions, and syntheses remain reusable and traceable.

## User

A solo researcher, builder, or writer exploring an idea with branching lines of inquiry — or an AI agent conducting structured research on their behalf.

## Core principles

- Claims are the center of the workflow.
- Provenance is a first-class feature, not an afterthought.
- Abandoned work is preserved as context, not discarded as noise.
- The graph supports thinking; exported narratives support communication.

## MVP requirements

### Functional

- Create typed nodes: `claim`, `evidence`, `question`, `objection`, `inference`, `decision`, `synthesis`.
- Store nodes with stable content-derived IDs.
- Add typed edges between nodes.
- Create common branch types from templates such as `support`, `objection`, `reframing`, and `application`.
- View a node together with inbound and outbound links.
- Trace ancestry or lineage from a chosen node.
- Mark node status with values like `active`, `stalled`, `promising`, `abandoned`, `harvested`.
- Export a selected subgraph into a markdown narrative.
- Bootstrap a starter graph from the original concept conversation.

### Non-functional

- Local-first.
- Human-readable storage.
- Small enough to inspect manually.
- No heavy dependencies.
- Covered by automated local tests for the core CLI workflows.
- Usable and legible from the terminal without requiring a graphical UI.

## Success criteria for MVP

- A new user can initialize the store and add a claim in under a minute.
- A user can attach supporting and opposing nodes without editing raw JSON.
- A user can inspect where a synthesis came from.
- A user can export a research path into markdown.
- The repository includes repeatable tests for initialization, bootstrap, graph editing, lineage, export, and invalid lookup behavior.

## MVP status

Implemented in this repository:

- Python CLI for local claim-graph workflows, runnable via `python -m sparkle` or an installable `sparkle` console command
- deterministic content-addressed IDs for nodes and edges, with re-adds of identical content treated as no-ops and clashing IDs rejected
- structured branch templates for common inquiry moves
- original concept conversation referenced as provenance for the seeded example graph
- concept bootstrap flow that seeds a starter graph
- input validation for node type, status, confidence range, and edge relation, with confidence and relation checked at argument parse time
- crash-safe atomic writes plus defensive handling of corrupt stores, missing required fields, and dangling edges
- short prefix resolution and short ID display across terminal views
- terminal-native tree views, provenance chains, and home dashboard, with rendering kept in a dedicated presentation module
- filtered node listing by type, status, tag, query, and limit, plus an ids-only mode for fast scanning before linking
- a shared operations layer (`ops.py`) that every front-end calls — the CLI is a thin formatter over it, so the debate rules, the referee, and the dedup gate are enforced in exactly one place behind a single error boundary
- the `ratified` terminal status for a judge's binding ruling, realized model-honestly as a decision node plus a superseding claim version (never an in-place mutation, since nodes are frozen)
- the judge's ruling move with its hard invariant: a claim the adversary never attacked cannot be ratified
- ergonomic capture: a 12-character handle echoed after every add, a fused create-and-link in one command, user-chosen alias names stored in a sidecar file (re-pointed on reuse so they survive supersession), a relation legend with direction glosses, and spoken-word link confirmations to catch backwards edges
- model-honest editing: a `revise` command that supersedes a node with a corrected version and re-homes its inbound edges, never mutating the frozen original
- a JSON import on-ramp (file or stdin) that builds a graph fragment and returns a nickname-to-id map, with a content-fingerprint dedup gate so an identical re-proposal collapses to the existing node instead of forking the graph. Because this is the LLM/batch on-ramp, it is treated as untrusted by default: every imported status is routed through the terminal-status guard, so automated or model-generated input can't fabricate a pre-`ratified` (or pre-`harvested`/`abandoned`) claim; a `trusted=True` opt-in exists only to round-trip a graph this tool itself exported
- an MCP server (`sparkle[mcp]` extra, `sparkle mcp` command) exposing a work frontier, debate-rule-enforcing mutation tools, and adversarial slash-command prompts — so an AI agent in Claude Code or Claude Desktop can operate the graph interactively with the host's own model, no server-side model or API key
- an autonomous adversarial harness (`sparkle run` command, no pip extra) that drives the propose -> critique -> gather -> judge -> synthesize playbook with no human in the turn order: Claude (Opus 4.8) proposes and judges while OpenAI's GPT-5.5 attacks, debating a seed question unattended, with every mutation routed through `ops.py` so the loop inherits all the debate invariants. This runs live end-to-end today, validated across three real debates on 2026-05-29: a balanced debate (support plus a genuine objection) completes, the judge correctly declines to ratify an overstated claim, and the run is cost-controlled by `--rounds`, `--max-moves`, `--run-id`, and the `SPARKLE_CODEX_REASONING_EFFORT` env var (only single-round runs are validated live so far; multi-round is available but unvalidated)
- the harness reaches the AI models through command-line tools, not an API: it shells out to `claude -p` (on the Claude Max login) and `codex exec` (on the Codex/ChatGPT login) via subprocess, so there is no API key and no `anthropic` / `openai` package — the engine and the model-backend layer are both pure standard-library Python, and each AI call runs in a throwaway empty directory with no tools granted
- a cross-family integrity gate: before the judge may ratify a claim, the engine requires at least one objection from a genuinely different model family than the claim's author (Claude vs GPT), which is stronger than the shared "different author" floor in the operations layer (also turned on by the harness). A self-loop `contradicts` edge is banned at the edge-write boundary. Both floors are inherited by every front-end
- a rewrite-re-challenge rule: when a claim is revised, its old objections are copied onto the new version for lineage but tagged as carried-over and excluded from both the author floor and the cross-family gate, so a rewritten claim must earn a fresh cross-family objection before it can be ratified
- a structural verdict-rejection floor on the autonomous path: the judge's verdict vocabulary is fixed (`upheld` | `refuted` | `overstated`), and the engine honors a settle-and-ratify request ONLY for an `upheld` verdict — any other verdict still records the ruling but never stamps the terminal `ratified` status, so a judge turn can't permanently mark a claim its own verdict rejected as settled-in-its-favor. This is now enforced in code, not just by the prompt (the free-text human CLI / MCP seam, where verdicts are plain words, is unchanged). It is distinct from the still-absent zero-evidence floor noted below
- real source verification for the evidence role plus a recall-honesty mandate on the rest: the evidence gatherer (and only it) runs with real web search and fetch, so it verifies sources and cites the URLs it actually retrieved instead of reciting figures from memory — granted exactly the web pair (`--tools WebSearch WebFetch` to restrict what is reachable, `--allowedTools WebSearch WebFetch` to pre-approve them for headless runs), with nothing dangerous reachable, and an off-switch (`config.evidence_web_search=False`) that degrades it gracefully to the honesty floor below. Every still-tool-denied role (proposer, critic, judge, synthesizer) must tag any recalled figure, statistic, study name, or citation as `(recalled, unverified)` with no false precision, and the judge must discount unverifiable specifics rather than let an unchecked number decide a ruling. This closes a verified failure mode where tool-denied roles asserted fabricated statistics with full confidence and a made-up figure became the load-bearing evidence in a verdict (verified live: a solar-power question that had fabricated cost figures now returns an evidence node with real fetched URLs from `iea.org`/`irena.org`, a source-checked quote, and an honest data caveat). Honest scope: source-checking is partial, not total — those four roles still reason from memory, so this is a source-checked-evidence debate with recalled claims flagged, not a full fact-checker of every statement
- the agent-driven MCP path now reaches parity with the autonomous harness on the debate-integrity floors, so an agent driving Sparkle over MCP is held to the same bar as the hands-off loop:
  - **author-distinct floor**: the MCP rule move passes `require_distinct_adversary=True`, so a single agent cannot ratify its own claim off a self-written objection — a claim only counts as challenged if an objection comes from a different author than the claim
  - **verdict-rejection floor**: the MCP rule move opts the shared seam into the harness's affirming verdict word (`ops.rule` gains an opt-in `affirming_verdicts` frozenset, default `None`), so a `settle=True` ruling with a non-affirming verdict is refused before any write — an agent over MCP can no longer stamp the terminal `ratified` status on a claim its own verdict rejected. Previously this coupling lived only in the harness, so via MCP an agent could rule `refuted` with settle requested and ratify anyway
  - **run-region completeness**: `sparkle_add_node` and `sparkle_harvest` now pass `run_id=` to `ops.add_node`, so their fused edges (an evidence `supports` edge, the harvest `produced` edge) land in the run region and are visible to run-diff / rollback, matching the harness — previously those edges leaked out of the region
  - the trusted human `sparkle rule` CLI path stays permissive on all of these (the floors default off, since a human is trusted to challenge their own claim and to use free-text verdict words like "accept" / "needs work")
  - accepted boundary (documented, not a bug): the MCP distinct-adversary floor is author-string level, because the MCP operator pattern is one agent playing every role and the caller picks the `agent_role` string, so a single agent could relabel itself to fake an adversary's name. Genuine adversary independence — a truly different model family attacking the claim — is the cross-family CLI leg (`sparkle run`), not the MCP path
- run reliability under a flaky model call: a model-CLI failure mid-run (timeout, non-zero exit, empty/garbled answer) is recorded as one failed move (an `error` outcome) so the loop continues to the next phase instead of crashing the whole run; and a done/stop from a mid-pipeline role (the critic or evidence gatherer, which run before the judge) advances to the next phase instead of aborting the debate before the judge rules
- real adversarial diversity from different model families (Claude vs GPT) rather than different tiers of one model: every Claude-side role runs Opus 4.8 and the genuine adversary is the GPT critic, with each role's family and model id env-overridable and one locked, config-checked invariant — the critic must be a different family than the proposer
- an advisory file lock around each read-modify-write for the "one front-end at a time per graph" concurrency contract
- a live signal that distinguishes a `judged` claim (a judge has ruled on it but did not settle it) from a `ratified` one (actually settled: stored status `ratified`, or superseded by a ratified version), so a run's verdict reads honestly
- an honest move count in the run summary: the loop is a single phase walk, so the summary reports `Moves:` (the per-move tally via the `moves_made` counter), not a misleading `Rounds:` label an unattended operator could read as several adversarial exchanges (the `--rounds` flag, which caps per-phase critique/gather iterations, is unchanged)
- a saved-graph cross-family audit (`sparkle run-manifest <run_id>` CLI, `sparkle_run_manifest` MCP tool, `sparkle://run/{run_id}/manifest` resource, plus a `Cross-family:` line in `sparkle run` output): each run persists the role-to-family/model roster it was configured with under a top-level `runs` key in the store, outside every hashed node/edge payload so no content-addressed id changes. Honest scope: it records the *configured* roster (which family each role was set up to use), not a runtime probe of which model answered each call — faithful in the shipped CLI path because the run builds its thinkers from that same config. The *binding* per-ratification cross-family check stays the judge-time gate, recorded separately in the graph as decision nodes and `contradicts` edges; the manifest is the setup roster, not that gate's result
- the Claude side of a run is locked down so it can answer but not act: the lockdown is deny-all, disabling every built-in tool (`--tools ""`, plus an explicit deny for the one tool that survives), forcing `--permission-mode default`, loading only user-level settings so a project/local allow-rule can't re-open a tool (`--setting-sources user`), loading no MCP servers (`--strict-mcp-config`), and running in an isolated throwaway tempdir — with zero tools available the model can reason and answer but cannot act on the machine at all. The single exception is the evidence gatherer, which is granted exactly the web search/fetch pair (and nothing dangerous beyond it) so it can verify sources
- local test coverage around the full CLI command set, the operations layer, and the autonomous engine (proven with a deterministic stub thinker and a fake command-runner that pins the exact `claude` / `codex` arguments and parsing — no network, no real CLI process). The MCP integrity floors are proven both in-process (through the real `build_app()` tool closures) and through an opt-in live end-to-end proof (`test_mcp_liveproof.py`) that spawns a fresh `sparkle mcp` server as a subprocess, connects as a real MCP client over stdio, and asserts every floor fires through the actual transport + server + ops seam (pre-challenge ratify refused, self-strawman refused, the verdict-rejection floor, the honest `judged`-vs-`ratified` signal, a clean cross-author ratify, and the evidence/harvest edges landing in the run region); it is opt-in via `SPARKLE_LIVE_MCP=1` plus the `sparkle[mcp]` extra and skipped in the fast suite. The live cross-family debate runs end-to-end and is validated by hand (three real debates on 2026-05-29); it is token-heavy and not exercised by the suite, so the live-binary tests are skipped

## Out of scope for MVP

- graphical interface
- team collaboration
- real-time sync
- web app
- automated paper ingestion
- ranking, search, or recommendation systems

## Next usability focus

Both interactive agent operation (the MCP server) and hands-off autonomy (the `sparkle run` harness) now work live. The autonomy decision is settled: the harness reaches the AI models through the `claude` and `codex` command-line tools (Claude proposes and judges, GPT attacks) rather than a paid API, so there is no API key, no extra Python package, and the whole codebase stays zero-dependency. By design, ratification is not gated on net-positive support: once a cross-family objection exists and the judge issues an `upheld` verdict that asks to settle, the judge may settle even on tied evidence (net=0), because the judge weighs the debate rather than counting votes. This is a deliberate design choice — the judge's reasoning is sovereign — not a missing floor, and it is distinct from the floor that IS structural on the autonomous path: a judge may not ratify a claim its own verdict rejected, since settle is honored only for an `upheld` verdict. The next priorities are machine-readable CLI output and richer introspection, plus the longer-term citation and workflow tracks.

### For agents and humans

- `--format json` on all commands for machine-readable output without going through MCP
- Standalone introspection commands (`gaps`, `tensions`, `stale`, `orphans`) — the MCP frontier covers "what needs attention," but these are not yet first-class CLI commands
- `undo` for backing out wrong turns without superseding node by node
- Session logging with actor attribution

### For real research

- Structured citations with author, title, year, URL, DOI
- Excerpt storage tied to specific evidence nodes
- Sources as first-class graph nodes
- Guided investigation workflows and confidence propagation
- Export templates beyond raw markdown (memo, lit review, argument map)
