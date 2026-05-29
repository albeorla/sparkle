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
- an autonomous adversarial harness (`sparkle[agents]` extra, `sparkle run` command) that drives the propose -> critique -> gather -> judge -> synthesize playbook with no human in the turn order: a proposer, a different-model critic, and a judge debate a seed question unattended, with every mutation routed through `ops.py` so the loop inherits all the debate invariants
- a distinct-adversary integrity floor in the shared operations layer: the judge's ratification gate can require that an objection came from a different author than the claim's own author (a self-written objection can no longer ratify a claim), and a self-loop `contradicts` edge is banned at the edge-write boundary — both inherited by every front-end, turned on by the harness
- per-role model isolation: the proposer, critic, and judge are separate model calls with separate contexts and distinct author identities, with a locked, config-enforced invariant that the critic model differs from the proposer model so the adversary is a genuinely different model rather than the proposer rephrasing itself
- an advisory file lock around each read-modify-write for the "one front-end at a time per graph" concurrency contract
- local test coverage around the full CLI command set, the operations layer, and the autonomous engine (the engine is proven with a deterministic stub thinker — no network, no key; the live three-model debate is a manual acceptance step)

## Out of scope for MVP

- graphical interface
- team collaboration
- real-time sync
- web app
- automated paper ingestion
- ranking, search, or recommendation systems

## Next usability focus

Both interactive agent operation (the MCP server) and hands-off autonomy (the `sparkle run` harness) now work. The autonomy decision is settled: the model backend lives behind the optional `sparkle[agents]` extra and is imported lazily, so the core stays zero-dependency. The next priorities are machine-readable CLI output and richer introspection, plus the longer-term citation and workflow tracks.

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
