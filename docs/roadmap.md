# Roadmap

## Completed

- Local Python CLI for a claim-graph research store
- Content-addressed node IDs using deterministic hashing
- Typed nodes and typed edges
- Structured branch templates for support, objection, reframing, and application flows
- Node status and confidence metadata
- Input validation for node type, status, and confidence range (0.0-1.0)
- CLI argument validation via argparse choices, including confidence range-checked at parse time and edge relations restricted to the known set
- Empty or whitespace-only node prefixes rejected with a clear error
- Content collision protection: re-adding identical content is a no-op, but a clashing ID with different content is rejected
- Crash-safe writes that swap in the new graph file atomically so a failed write cannot corrupt the store
- Claim-card style node inspection
- Local ASCII tree rendering
- Provenance-focused why view
- Filtered node listing by status, type, tag, query, and limit
- Home dashboard for graph summary and next actions
- Lineage inspection
- Inbound-lineage export for a selected node
- Markdown export for a selected subgraph
- Bootstrap flow seeded from the original concept conversation
- Defensive error handling for corrupt stores, missing required fields, and missing node references, with dangling edges skipped during traversal and export
- Single-read traversal with a shared neighbor-collection helper for tree and provenance rendering
- Read-only rendering split into its own presentation module, separate from the graph store
- Short prefix resolution and 12-character ID display across CLI views
- `python -m sparkle` entrypoint plus an installable `sparkle` console command (`pip install -e .`)
- Automated tests for the current CLI workflows
- Repository docs that anchor the concept, scope, and next steps
- Demo walkthrough: music-and-coding claim graph with mermaid visualization

## Next

### Phase 1: Make the CLI actually usable mid-research

Right now you have to context-switch out of your research flow to operate the tool. The graph should be something you work *inside*, not something you stop to update.

- **`update-node`** — edit status, confidence, content, or tags on an existing node without recreating it. Immutable IDs stay stable; mutable metadata (status, confidence, tags) becomes editable. This is the single biggest friction point.
- **`merge` / `supersede`** — mark one node as superseding another, carrying forward its edges. Real research converges; right now there's no way to express "this synthesis replaced that one."
- **`batch` mode** — accept a sequence of commands from stdin or a file. An agent building a graph shouldn't need 20 separate subprocess calls. JSON-lines in, structured results out.
- **`undo`** — soft-delete the last N operations. Research is messy; you need to be able to back out a wrong turn without surgery on the JSON file.

### Phase 2: Agent-native interface

The CLI works for humans in a terminal. Agents need something better. This is where Sparkle becomes a tool that AI agents can use during real research, not just a thing humans poke at.

- **MCP server** — expose the graph as an MCP tool server. An agent running in Claude Code, Cursor, or any MCP client can `add-node`, `query`, `export` without shelling out. This is the highest-leverage thing on the roadmap.
- **Structured JSON output** — `--format json` flag on all commands. Agents can't parse ASCII trees. Every command should return machine-readable output when asked.
- **Query language** — find nodes by combining type, status, confidence range, tag, edge relation, and content search in a single query. "Show me all active objections to nodes tagged 'core-thesis' with confidence > 0.6" should be one command, not a pipeline.
- **Graph introspection commands** — `gaps` (claims with no evidence), `tensions` (claims with contradicting evidence and no synthesis), `stale` (nodes untouched for N days), `orphans` (disconnected nodes). These tell an agent *what to work on next*.
- **Session logging** — record every mutation with timestamp and actor (human vs agent name). When an agent builds 30 nodes, you need to know what it did and why, after the fact.

### Phase 3: Real citation and source management

The current `--citations` field is a flat string list. Real research needs sources you can verify, quote, and trace back to.

- **Structured citations** — author, title, year, URL, DOI, access date as first-class fields. Not just a string you hope is parseable.
- **Excerpt storage** — attach the specific quote or data point from a source that supports a node. "This paper supports my claim" is useless; "page 4, paragraph 2 says X" is useful.
- **Source nodes** — sources as first-class nodes in the graph, not metadata on evidence nodes. Multiple evidence nodes can reference the same source. You can ask "what did this paper contribute to my research?"
- **URL fetch + snapshot** — given a URL, fetch the content, store a snapshot, extract title/author. Sources rot; snapshots don't.
- **BibTeX / RIS import/export** — interop with existing reference managers. Nobody wants to re-enter their Zotero library.

### Phase 4: Research workflows that keep you honest

Individual commands are building blocks. Workflows are what make the tool opinionated about *good* research practice.

- **Guided investigation** — `sparkle investigate <claim>` walks you through: find evidence, find objections, identify reframing questions, draft inferences, attempt synthesis. Not a wizard — a checklist that knows what's missing.
- **Devil's advocate mode** — given a claim and its supporting evidence, prompt for (or generate, if agent-driven) the strongest objections. The tool should actively resist confirmation bias.
- **Confidence propagation** — when evidence is weakened or an objection is added, downstream inferences and syntheses should flag as "confidence may be stale." Not auto-update — just flag for review.
- **Review triggers** — configurable rules: "if a synthesis has more than 2 unaddressed objections on its ancestors, flag it." "If a claim has only self-report evidence, flag it." Make the graph self-auditing.
- **Export templates** — memo, literature review, argument map, executive summary. Different audiences need the same graph in different shapes. The graph is the source of truth; exports are views.

## Later

### Phase 5: Multi-graph and collaboration

- Shared repositories or sync between researchers
- Multi-author provenance and attribution
- Fork and merge across independent investigations of the same question
- Cross-graph references ("my evidence node cites your synthesis")
- Conflict-aware merge when two researchers edit the same subgraph

### Phase 6: Interface

- TUI for keyboard-driven claim-card navigation
- Web UI with interactive graph visualization and path highlighting
- Branch triage dashboard for active, stalled, abandoned, and harvested work
- Side-by-side view: graph structure on the left, node content on the right
