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
- Shared operations layer (`ops.py`): every graph action is a function returning JSON-able data and raising `ValueError` for user errors; the CLI is a thin formatter over it, so the debate invariants, the referee/transition engine, and the dedup gate live in exactly one place and the front-ends stay interchangeable
- `ratified` terminal status for a judge's binding ruling, realized model-honestly as a decision node plus a superseding claim version (never an in-place mutation; nodes are frozen)
- Judge's ruling move with its hard invariant: a claim the adversary never attacked cannot be ratified
- Ergonomic capture wins: a 12-character handle echoed on its own line after every add, fused create-and-link in one command (`--link-to`/`--relation`, with a paired-or-error guard, new node as the edge source), user-chosen alias names in a sidecar file (`--as`, re-pointed on reuse so names survive supersession), a `relations` legend with direction glosses, spoken-word link confirmations to catch backwards edges, and `list-nodes --ids-only`
- `revise` command: supersede a node with a corrected version and re-home its inbound edges (or leave them with `--no-rehome-edges`), model-honest editing that never mutates the frozen original
- JSON `import` on-ramp (file or stdin) that builds a graph fragment two-pass and returns a nickname-to-id map, with optional per-node timestamps for deterministic re-import
- Content-fingerprint dedup gate over normalized type+title+content (deliberately excluding timestamp/author/confidence) so an identical re-proposal collapses to the existing node instead of forking the graph; near-duplicates are never auto-merged
- MCP server (`sparkle[mcp]` optional extra, lazy `sparkle mcp` subcommand over stdin/stdout): a thin FastMCP adapter over `ops.py` exposing a work frontier (resource + tool mirror), debate-rule-enforcing mutation tools, run tagging with region review/rollback, and the `/challenge` `/investigate` `/synthesize` `/next` adversarial prompts — the complete interactive intelligent operator, working on Claude Code/Desktop with no server-side model or API key
- Live, non-persisted frontier and status signals keyed on the inbound supports-minus-contradicts tally plus node type/status, never on the frozen stored confidence
- Advisory file lock around each read-modify-write for the "one front-end at a time per graph" concurrency contract

### Phase 2: Autonomous adversarial harness

The zero-dependency-vs-key question is settled in favor of the command-line tools: sampling (the protocol's way for a server to borrow the host's model) is not available on the mainstream clients, so instead of bringing in a paid API SDK, the harness reaches the AI models by shelling out to the `claude` and `codex` command-line tools the user already has installed and logged in. `claude -p` rides the Claude Max subscription and `codex exec` rides the Codex/ChatGPT subscription, so there is no API key and no model SDK at all — `thinker.py` is pure standard-library Python (subprocess/json/shutil/tempfile/os), and the stdlib core, the CLI core, the MCP server, and the autonomous engine all stay dependency-free.

- Autonomous engine (`sparkle run <seed>` subcommand, no pip extra): walks the same propose -> critique -> gather -> judge -> synthesize playbook the interactive loop uses, with no human in the turn order. The model backend is injected as a duck-typed "thinker", so the engine is pure with respect to the model and imports no SDK; every graph mutation is routed through the same `ops` functions and the same referee, stamped with a `run_id` so the whole run — claim, objection, decision, ratified version, synthesis, and all of their edges — is visible to the run-summary/diff/rollback surface. `run` is a lazy seam; because the engine and thinker layer are pure stdlib the import always succeeds, so the only runtime failure is a missing `claude` / `codex` binary, which produces a clear "install and log in to the claude and codex CLIs" hint (not a pip hint)
- Cross-family adversaries via command-line tools: Claude (Opus 4.8, through `claude -p`) plays proposer, judge, evidence gatherer, and synthesizer; OpenAI's GPT-5.5 (through `codex exec`) plays the critic. Each role is a separate AI call with its own context and a distinct author identity, run in a throwaway empty directory with no tools granted so the model can answer but can't touch the repo. Real adversarial diversity comes from different model families (Claude vs GPT), not different tiers of one model, so every Claude-side role uses Opus 4.8 and the genuine opponent is the GPT critic. Each role's family and model id are env-overridable (e.g. `SPARKLE_CRITIC_BACKEND`/`SPARKLE_CRITIC_MODEL`), the codex reasoning effort is tunable (`SPARKLE_CODEX_REASONING_EFFORT`), and one locked, config-checked invariant holds: the critic must be a different family than the proposer
- Cross-family integrity gate layered on the shared author floor: the operations layer's `require_distinct_adversary` gate (off for the human CLI, on for the harness) only counts an objection whose author differs from the claim's own author; the engine layers a stronger gate on top that requires at least one objection from a different model family than the claim's author (Claude vs GPT), so a same-family role with a distinct name can't ratify a self-attacked claim. A self-loop `contradicts` edge (a claim objecting to itself) is banned at the edge-write boundary
- Rewrite-re-challenge rule: when a claim is revised, its inbound objections are copied onto the new version for lineage/display but tagged as carried-over, and both the author floor and the cross-family gate skip carried-over objections — so a rewritten claim must earn a fresh cross-family objection before the judge can ratify it
- Stop conditions: per-phase round caps from the playbook, a hard total-move backstop for a runaway thinker, an explicit `done`/`stop` move from any role, a frontier-empty stop, the natural "judge ruled" terminal, and an optional thinker-call / token-budget cost ceiling
- Engine test coverage with a deterministic stub thinker and an injected fake command-runner (no network, no real CLI process, runs in the base suite): the full propose -> object -> rule loop, the cross-family refusal vs success, the rewrite-re-challenge rule, the self-loop ban, run-id plumbing through `add-branch` / `rule` / `add-node` (proposal and synthesis nodes *and* their edges land in the run region), the hard move cap, the `done` move, the exact `claude` / `codex` argv shape and output parsing, and the critic-vs-proposer cross-family config invariant. The live cross-family debate is token-heavy on the GPT side and is a manual human acceptance step, not validated by the test suite; the live-binary tests are skipped and no test invokes the real CLIs

## Next

### Phase 2b: Polish the agent and human surface

- **Structured JSON output** — `--format json` flag on all CLI commands, for machine-readable output without going through MCP.
- **Query language** — combine type, status, confidence range, tag, edge relation, and content search in a single query.
- **Standalone introspection commands** — `gaps` (claims with no evidence), `tensions` (claims with contradicting evidence and no synthesis), `stale` (nodes untouched for N days), `orphans` (disconnected nodes). The MCP frontier already covers "what needs adversarial attention next"; these would expose the same kind of signal as first-class CLI commands.
- **`undo`** — back out the last N operations without superseding node by node.
- **Session logging** — record every mutation with timestamp and actor (human vs agent name), beyond the per-run metadata stamp the MCP layer already writes.

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
