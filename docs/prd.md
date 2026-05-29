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
- filtered node listing by type, status, tag, query, and limit
- local test coverage around the current CLI command set

## Out of scope for MVP

- graphical interface
- team collaboration
- real-time sync
- web app
- automated paper ingestion
- ranking, search, or recommendation systems

## Next usability focus

Two priorities before anything else: reduce friction for humans mid-research, and make the tool agent-native.

### For humans

- `update-node` for editing mutable metadata (status, confidence, tags) without recreating nodes
- `merge` / `supersede` for converging syntheses
- `undo` for backing out wrong turns

### For agents

- MCP server so agents can operate the graph without shelling out
- `--format json` on all commands for machine-readable output
- `batch` mode for bulk operations via stdin
- Graph introspection commands (`gaps`, `tensions`, `stale`, `orphans`) that tell an agent what to work on next
- Session logging with actor attribution

### For real research

- Structured citations with author, title, year, URL, DOI
- Excerpt storage tied to specific evidence nodes
- Sources as first-class graph nodes
- Guided investigation workflows and confidence propagation
- Export templates beyond raw markdown (memo, lit review, argument map)
