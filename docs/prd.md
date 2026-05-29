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
- a JSON import on-ramp (file or stdin) that builds a graph fragment and returns a nickname-to-id map, with a content-fingerprint dedup gate so an identical re-proposal collapses to the existing node instead of forking the graph
- an MCP server (`sparkle[mcp]` extra, `sparkle mcp` command) exposing a work frontier, debate-rule-enforcing mutation tools, and adversarial slash-command prompts — so an AI agent in Claude Code or Claude Desktop can operate the graph interactively with the host's own model, no server-side model or API key
- an autonomous adversarial harness (`sparkle run` command, no pip extra) that drives the propose -> critique -> gather -> judge -> synthesize playbook with no human in the turn order: Claude (Opus 4.8) proposes and judges while OpenAI's GPT-5.5 attacks, debating a seed question unattended, with every mutation routed through `ops.py` so the loop inherits all the debate invariants
- the harness reaches the AI models through command-line tools, not an API: it shells out to `claude -p` (on the Claude Max login) and `codex exec` (on the Codex/ChatGPT login) via subprocess, so there is no API key and no `anthropic` / `openai` package — the engine and the model-backend layer are both pure standard-library Python, and each AI call runs in a throwaway empty directory with no tools granted
- a cross-family integrity gate: before the judge may ratify a claim, the engine requires at least one objection from a genuinely different model family than the claim's author (Claude vs GPT), which is stronger than the shared "different author" floor in the operations layer (also turned on by the harness). A self-loop `contradicts` edge is banned at the edge-write boundary. Both floors are inherited by every front-end
- a rewrite-re-challenge rule: when a claim is revised, its old objections are copied onto the new version for lineage but tagged as carried-over and excluded from both the author floor and the cross-family gate, so a rewritten claim must earn a fresh cross-family objection before it can be ratified
- real adversarial diversity from different model families (Claude vs GPT) rather than different tiers of one model: every Claude-side role runs Opus 4.8 and the genuine adversary is the GPT critic, with each role's family and model id env-overridable and one locked, config-checked invariant — the critic must be a different family than the proposer
- an advisory file lock around each read-modify-write for the "one front-end at a time per graph" concurrency contract
- local test coverage around the full CLI command set, the operations layer, and the autonomous engine (proven with a deterministic stub thinker and a fake command-runner that pins the exact `claude` / `codex` arguments and parsing — no network, no real CLI process; the live cross-family debate is a token-heavy manual acceptance step and the live-binary tests are skipped)

## Out of scope for MVP

- graphical interface
- team collaboration
- real-time sync
- web app
- automated paper ingestion
- ranking, search, or recommendation systems

## Next usability focus

Both interactive agent operation (the MCP server) and hands-off autonomy (the `sparkle run` harness) now work. The autonomy decision is settled: the harness reaches the AI models through the `claude` and `codex` command-line tools (Claude proposes and judges, GPT attacks) rather than a paid API, so there is no API key, no extra Python package, and the whole codebase stays zero-dependency. The next priorities are machine-readable CLI output and richer introspection, plus the longer-term citation and workflow tracks.

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
