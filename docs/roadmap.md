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
- JSON `import` on-ramp (file or stdin) that builds a graph fragment two-pass and returns a nickname-to-id map, with optional per-node timestamps for deterministic re-import. As the LLM/batch on-ramp it is untrusted by default: every imported status is routed through the terminal-status guard, so automated or model-generated input can't fabricate a pre-`ratified` (or pre-`harvested`/`abandoned`) claim; a `trusted=True` opt-in exists only to round-trip a graph this tool itself exported
- Content-fingerprint dedup gate over normalized type+title+content (deliberately excluding timestamp/author/confidence) so an identical re-proposal collapses to the existing node instead of forking the graph; near-duplicates are never auto-merged
- MCP server (`sparkle[mcp]` optional extra, lazy `sparkle mcp` subcommand over stdin/stdout): a thin FastMCP adapter over `ops.py` exposing a work frontier (resource + tool mirror), debate-rule-enforcing mutation tools, run tagging with region review/rollback, and the `/challenge` `/investigate` `/synthesize` `/next` adversarial prompts — the complete interactive intelligent operator, working on Claude Code/Desktop with no server-side model or API key
- Live, non-persisted frontier and status signals keyed on the inbound supports-minus-contradicts tally plus node type/status, never on the frozen stored confidence
- Advisory file lock around each read-modify-write for the "one front-end at a time per graph" concurrency contract

### Phase 2: Autonomous adversarial harness (shipped, runs live)

`sparkle run "<seed>"` now runs the full adversarial debate live end-to-end, validated across three real debates on 2026-05-29: a balanced debate (support plus a genuine GPT objection) completes, the judge correctly declines to ratify an overstated claim, and the live signal honestly separates a `judged` claim (a ruling exists but did not settle it) from a `ratified` one (actually settled). By design, ratification is not gated on net-positive support: given a cross-family objection, the judge may issue an `upheld` verdict and settle even on tied evidence (net=0), because the judge weighs the debate rather than counting votes — the judge's reasoning is sovereign, this is a deliberate design choice, not a missing floor. The floor that IS structural runs the other way: a judge may not ratify a claim its own verdict rejected, since settle is honored only for an `upheld` verdict. Only single-round runs are validated live so far; multi-round (`--rounds` > 1) is available but unvalidated.

The zero-dependency-vs-key question is settled in favor of the command-line tools: sampling (the protocol's way for a server to borrow the host's model) is not available on the mainstream clients, so instead of bringing in a paid API SDK, the harness reaches the AI models by shelling out to the `claude` and `codex` command-line tools the user already has installed and logged in. `claude -p` rides the Claude Max subscription and `codex exec` rides the Codex/ChatGPT subscription, so there is no API key and no model SDK at all — `thinker.py` is pure standard-library Python (subprocess/json/shutil/tempfile/os), and the stdlib core, the CLI core, the MCP server, and the autonomous engine all stay dependency-free.

- Autonomous engine (`sparkle run <seed>` subcommand, no pip extra): walks the same propose -> critique -> gather -> judge -> synthesize playbook the interactive loop uses, with no human in the turn order. The model backend is injected as a duck-typed "thinker", so the engine is pure with respect to the model and imports no SDK; every graph mutation is routed through the same `ops` functions and the same referee, stamped with a `run_id` so the whole run — claim, objection, decision, ratified version, synthesis, and all of their edges — is visible to the run-summary/diff/rollback surface. `run` is a lazy seam; because the engine and thinker layer are pure stdlib the import always succeeds, so the only runtime failure is a missing `claude` / `codex` binary, which produces a clear "install and log in to the claude and codex CLIs" hint (not a pip hint)
- Cost and control knobs on `sparkle run`: `--rounds N` overrides the per-phase critique/gather iteration count (the playbook defaults to 3 each; only single-round runs are validated live), `--max-moves N` is a hard runaway cap on total moves, `--run-id <id>` tags the whole run as one reviewable/rollback-able region, and `SPARKLE_CODEX_REASONING_EFFORT=low|medium|high|xhigh` tunes the GPT critic's depth (codex defaults to a token-heavy `xhigh`). The Claude side of each call is locked down so it can answer but not act: the lockdown is deny-all, disabling every built-in tool (`--tools ""`, plus an explicit deny for the one tool that survives), `--permission-mode default`, only user-level settings loaded so a project/local allow-rule can't re-open a tool (`--setting-sources user`), no MCP servers (`--strict-mcp-config`), run in an isolated throwaway tempdir. The single exception is the evidence gatherer, granted exactly the web search/fetch pair (and nothing dangerous beyond it) so it can verify sources
- Live signal that reads the verdict honestly: a claim a judge has ruled on but not settled reads `judged`; only a claim that was actually settled (stored status `ratified`, or superseded by a ratified version) reads `ratified`
- Honest move count in the run summary: the loop is a single phase walk, so the summary reports `Moves:` (the `moves_made` per-move tally), not a misleading `Rounds:` label an unattended operator could read as several adversarial exchanges; the `--rounds` flag (per-phase critique/gather caps) is unchanged
- Saved-graph cross-family audit: each run persists the role-to-family/model roster it was configured with under a top-level `runs` key in the store (outside every hashed node/edge payload, so no content-addressed id changes), readable via `sparkle run-manifest <run_id>` (CLI), the `sparkle_run_manifest` MCP tool, the `sparkle://run/{run_id}/manifest` resource, and a `Cross-family:` line in `sparkle run` output. Honest scope: it records the *configured* roster (which family each role was set up to use), not a runtime probe of which model answered each call — faithful in the shipped CLI path because the run builds its thinkers from that same config; the *binding* per-ratification cross-family check stays the judge-time gate, recorded separately as decision nodes and `contradicts` edges
- Cross-family adversaries via command-line tools: Claude (Opus 4.8, through `claude -p`) plays proposer, judge, evidence gatherer, and synthesizer; OpenAI's GPT-5.5 (through `codex exec`) plays the critic. Each role is a separate AI call with its own context and a distinct author identity, run in a throwaway empty directory with no tools at all (the Claude side is deny-all) so the model can answer but can't touch the repo. Real adversarial diversity comes from different model families (Claude vs GPT), not different tiers of one model, so every Claude-side role uses Opus 4.8 and the genuine opponent is the GPT critic. Each role's family and model id are env-overridable (e.g. `SPARKLE_CRITIC_BACKEND`/`SPARKLE_CRITIC_MODEL`), the codex reasoning effort is tunable (`SPARKLE_CODEX_REASONING_EFFORT`), and one locked, config-checked invariant holds: the critic must be a different family than the proposer
- Cross-family integrity gate layered on the shared author floor: the operations layer's `require_distinct_adversary` gate (off for the human CLI, on for the harness) only counts an objection whose author differs from the claim's own author; the engine layers a stronger gate on top that requires at least one objection from a different model family than the claim's author (Claude vs GPT), so a same-family role with a distinct name can't ratify a self-attacked claim. A self-loop `contradicts` edge (a claim objecting to itself) is banned at the edge-write boundary
- Rewrite-re-challenge rule: when a claim is revised, its inbound objections are copied onto the new version for lineage/display but tagged as carried-over, and both the author floor and the cross-family gate skip carried-over objections — so a rewritten claim must earn a fresh cross-family objection before the judge can ratify it
- Stop conditions: per-phase round caps from the playbook, a hard total-move backstop for a runaway thinker, a role-scoped `done`/`stop` move (from a mid-pipeline role like the critic or evidence gatherer it means "this role is finished" and the walk advances to the judge; only a terminal role or the proposer ends the whole run), a frontier-empty stop, the natural "judge ruled" terminal, and an optional thinker-call / token-budget cost ceiling that spends exactly `max_thinker_calls` model calls (not max+1)
- Run reliability under a flaky model call: a model-CLI failure mid-run (timeout, non-zero exit, empty/garbled answer) is recorded as one failed move (an `error` outcome) so the loop continues to the next phase instead of crashing the whole run with a raw traceback
- Structural verdict-rejection floor on the autonomous path: the judge's verdict vocabulary is fixed (`upheld` | `refuted` | `overstated`), and the engine honors a settle-and-ratify request only for an `upheld` verdict — any other verdict records the ruling but never stamps the terminal `ratified` status, so a judge turn can't permanently mark a claim its own verdict rejected as settled-in-its-favor (enforced in code, not just by the prompt; the free-text human CLI / MCP seam is unchanged). Distinct from the still-absent zero-evidence floor
- Real web search for the evidence role plus a recall-honesty mandate on the rest: the evidence gatherer (and only it) runs with real web search/fetch so it verifies sources and cites the URLs it actually retrieved instead of reciting figures from memory (granted exactly the web pair — `--tools WebSearch WebFetch` restricts what is reachable, `--allowedTools WebSearch WebFetch` pre-approves them for headless runs — with nothing dangerous reachable; off-switch `config.evidence_web_search=False` degrades it gracefully to the honesty floor). Every still-tool-denied role (proposer, critic, judge, synthesizer) must tag any recalled figure/statistic/study/citation as `(recalled, unverified)` with no false precision, and the judge must discount unverifiable specifics rather than let an unchecked number decide a ruling. This closes a verified failure mode where tool-denied roles asserted fabricated statistics with confidence and a made-up figure became the load-bearing evidence in a verdict (verified live: a solar-power question that had fabricated cost figures now returns real fetched URLs from `iea.org`/`irena.org`, a source-checked quote, and an honest data caveat). Source-checking is partial, not total — those four roles still reason from memory, so it is a source-checked-evidence debate with recalled claims flagged, not a full fact-checker
- MCP self-strawman hardening: because the MCP server is an agent-driven front-end (autonomous operation), its `sparkle_rule` move now passes `require_distinct_adversary=True` — so a single agent driving Sparkle over MCP cannot ratify its own claim off a self-written objection (a claim only counts as challenged if an objection comes from a different author than the claim). This matches the autonomous harness; the trusted human `sparkle rule` CLI path stays permissive (the floor defaults off). Proven through the real `build_app()` tool closures: a same-author objection is refused, a cross-author one is allowed
- Engine test coverage with a deterministic stub thinker and an injected fake command-runner (no network, no real CLI process, runs in the base suite): the full propose -> object -> rule loop, the cross-family refusal vs success, the rewrite-re-challenge rule, the self-loop ban, run-id plumbing through `add-branch` / `rule` / `add-node` (proposal and synthesis nodes *and* their edges land in the run region), the hard move cap, the `done` move, the exact `claude` / `codex` argv shape and output parsing, and the critic-vs-proposer cross-family config invariant. The live cross-family debate itself runs end-to-end and is validated by hand (three real debates on 2026-05-29); it is token-heavy on the GPT side and not exercised by the suite, so the live-binary tests are skipped and no test invokes the real CLIs

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
