export const meta = {
  name: 'sparkle-build-intelligent-operator',
  description: 'Build Sparkle Phases 0-1c (ops.py seam + ergonomics + MCP intelligent operator), then adversarially verify the design corrections hold',
  phases: [
    { title: 'Data model', detail: 'add the ratified terminal status to NodeStatus' },
    { title: 'Core seam', detail: 'ops.py: full contract + invariants + referee + dedup + frontier + playbook (pure stdlib)' },
    { title: 'CLI + packaging', detail: 'cli.py -> thin formatter + ergonomics + lazy mcp subcommand; pyproject extras' },
    { title: 'Tests', detail: 'repair Handle-line parses; cover ops, ergonomics, ratified, invariants' },
    { title: 'MCP layer', detail: 'mcp_server.py: tools + resources + prompts + safety rails over ops.py' },
    { title: 'Docs', detail: 'sync README/prd/roadmap/CLAUDE for the new commands + MCP server' },
    { title: 'Verify', detail: 'run tests, compile all modules, lazy-import path, self-installed MCP smoke test' },
    { title: 'Adversarial check', detail: 'skeptics confirm the hard-won design corrections actually hold in the written code' },
  ],
}

const ROOT = '/Users/aorlando/dev/ideas/sparkle'
const DESIGN = `${ROOT}/discussions/sparkle-agent-architecture.md`

// Locked decisions + the exact contracts every builder must share, so parallel agents agree.
const SPEC = `
SHARED BUILD SPEC (every agent must obey this; the full rationale is in ${DESIGN} — read it first).

LOCKED DECISIONS:
- Add a new terminal status "ratified" to NodeStatus (a judge's binding ruling lands here).
- The debate INVARIANTS and the referee/transition engine live in ops.py (NOT the MCP tool
  layer), so the CLI, the MCP server, and any future harness all inherit them through the
  same ValueError boundary. This is non-negotiable — it is what keeps front-ends interchangeable.
- Status/strength signals in the frontier are LIVE, non-persisted aggregates computed on each
  read (supports-minus-contradicts tally + node type/status). NEVER imply the stored confidence
  (frozen at its set value) changed. Do not key any frontier bucket on stored confidence alone.
- "Ratified" is realized the model-honest way: ruling writes a decision node plus, when the
  judge settles, a SUPERSEDING claim version carrying status=ratified — never an in-place
  mutation (nodes are frozen). Presence of an inbound accepted decision is what "judged" means
  to the frontier. (This realization is the recommended default; Albert may override.)
- Dedup fingerprint = sha256 over normalized (node_type + title.strip().lower() +
  content.strip().lower()), DELIBERATELY excluding created_at/author/confidence, so a
  re-proposal a second later returns {created:false, existing_id} instead of forking the graph.
  Exact-fingerprint matches auto-dedup; near-duplicates are flagged, never auto-merged.
- Concurrency: declare "one front-end at a time per graph" for this MVP. Add a simple advisory
  file lock around the read-modify-write in the store's write path (cheap, stdlib fcntl on
  posix; degrade gracefully where unavailable). No multi-writer guarantee beyond that.
- "@last" is FORBIDDEN. Timestamps are whole-second with no tiebreaker, so a "last node" token
  silently attaches edges to the wrong node. Use explicit short id prefixes (resolve_id already
  accepts them) everywhere instead.
- Aliases (the --as feature) live ONLY in .sparkle/aliases.json beside the store, never in any
  hashed payload. Re-point a name to the new node when reused so names survive supersession.

CLI OUTPUT CONTRACT (cli.py and tests/test_cli.py must match this exactly):
- add-node prints two lines: "Added {node_type}: {title}" then "Handle: {node_id[:12]}".
  If --link-to/--relation were given, also: "Linked: {from_type} \\"{from_title}\\" --{relation}--> {to_type} \\"{to_title}\\"".
- add-edge prints: "Linked: {from_type} \\"{from_title}\\" --{relation}--> {to_type} \\"{to_title}\\"" then "Handle: {edge_id[:12]}".
- add-branch keeps its two-line shape but the id lines become "Handle: {id[:12]}".
- Tests must read the 12-char handle off the "Handle:" line and use it as a prefix (resolve_id
  accepts prefixes) — do NOT parse the last whitespace token of the whole output.

SCOPE: Phases 0, 1a, 1b, 1c only. Do NOT build Phase 2 (the autonomous harness / sparkle[agents]
extra / sparkle_run_loop). Leave a clearly-marked seam for it, nothing more.
`

// ---------------------------------------------------------------------------
// Phase: Data model — add the ratified terminal status (everything references it)
// ---------------------------------------------------------------------------
phase('Data model')
await agent(
  `Add a new terminal status "ratified" to Sparkle's NodeStatus.\n\n${SPEC}\n\n` +
  `Edit ONLY ${ROOT}/src/sparkle/models.py. Add "ratified" to the NodeStatus Literal (after ` +
  `"harvested" or wherever it reads cleanly). Do not touch anything else in the file. ` +
  `This value means "a judge ruled and the claim is binding." Keep the frozen-dataclass model intact.`,
  { label: 'model:ratified-status', phase: 'Data model' }
)

// ---------------------------------------------------------------------------
// Phase: Core seam — ops.py is the entire stdlib-pure contract. One owner.
// ---------------------------------------------------------------------------
phase('Core seam')
await agent(
  `Create ${ROOT}/src/sparkle/ops.py — the single stdlib-pure contract every front-end calls. ` +
  `Read ${DESIGN} (full architecture) and ${ROOT}/src/sparkle/cli.py, graph.py, models.py, ` +
  `templates.py, presentation.py first so you lift behavior faithfully.\n\n${SPEC}\n\n` +
  `ops.py MUST contain (all returning plain JSON-able dicts, raising ValueError for user errors, ` +
  `NEVER printing, NO argparse, NO mcp, NO model/LLM):\n` +
  `1. A 1:1 lift of every action cli.main() performs today: init, bootstrap, home (as data), ` +
  `   add_node, add_edge (resolves prefixes first), add_branch (wraps build_branch_node + two ` +
  `   store writes), get_node, list_nodes (with filters), list_edges, neighbors, lineage, ` +
  `   subgraph, export (returns markdown string).\n` +
  `2. INVARIANTS as functions: rule(store, claim_ref, ...) raises ValueError if the claim has no ` +
  `   inbound objection/contradicts edge ("you may not ratify a claim the adversary never ` +
  `   attacked; run a challenge first"), then writes a decision node and, on settle, a ` +
  `   superseding claim version with status=ratified; a confidence cap helper for model-authored ` +
  `   nodes (default 0.5); a guard that refuses model-authored terminal statuses unless settled.\n` +
  `3. The referee / transition-rule engine: given a claim's inbound edge tally, compute the ` +
  `   live (display-only) status signal per the playbook transition rules. Pure, non-persisted.\n` +
  `4. The content-fingerprint dedup helper (excludes created_at, per SPEC).\n` +
  `5. frontier(store): compute the work-frontier buckets (UNCHALLENGED, OPEN_QUESTIONS, STALLED, ` +
  `   THIN_EVIDENCE, READY_TO_JUDGE) over the store, keyed on edge tally + node type/status, ` +
  `   NOT on stored confidence. Each entry: {node_id, node_type, title, status, why_listed, ` +
  `   suggested_prompt}. Support top-N + cursor paging.\n` +
  `6. A built-in adversarial playbook (roles/phases/transition-rules/stop-conditions as a dict ` +
  `   the referee reads) and a loader that can read an override from .sparkle/playbooks/.\n` +
  `7. An alias sidecar helper over .sparkle/aliases.json (read/write/resolve), per SPEC.\n` +
  `Keep it pure stdlib. This file is the frozen surface; everything else calls it.`,
  { label: 'core:ops.py', phase: 'Core seam' }
)

// ---------------------------------------------------------------------------
// Phase: CLI + packaging — distinct files, safe in parallel
// ---------------------------------------------------------------------------
phase('CLI + packaging')
await parallel([
  () => agent(
    `Refactor ${ROOT}/src/sparkle/cli.py into a thin formatter over ops.py, and add the ergonomics ` +
    `wins. Read ${DESIGN} (the "human ergonomics layer" section), the existing cli.py, and the new ` +
    `ops.py first.\n\n${SPEC}\n\n` +
    `Do: (a) replace every inline graph mutation/query with one call into ops.py; keep argparse + ` +
    `print() + the existing ValueError->stderr+exit-2 and SystemExit handling. The pure refactor ` +
    `must not change behavior. (b) Add ergonomics: the Handle-line output contract above; ` +
    `optional --link-to <prefix> --relation on add-node (paired-or-error; creates the edge via ` +
    `ops, NO @last); --as <name> to register an alias; a "relations" subcommand printing each ` +
    `relation with a plain-English direction gloss; the spoken-word "Linked: ..." echo after ` +
    `links; --ids-only on list-nodes; a "revise <ref>" subcommand (supersede + re-home edges via ` +
    `ops, surfacing the re-home-all-vs-choose decision to the user); an "import" subcommand ` +
    `reading JSON from a file or "-" (two-pass build via ops, printing a nickname->id map); make ` +
    `home/hints print the real program name (parser.prog), not the hard-coded python3 -m form. ` +
    `(c) Add a "mcp" subcommand whose handler does a LAZY "from .mcp_server import run_server" and ` +
    `on ImportError prints "pip install 'sparkle[mcp]'" to stderr and returns 2. Own cli.py only.`,
    { label: 'cli:thin-formatter+ergonomics', phase: 'CLI + packaging' }
  ),
  () => agent(
    `Edit ${ROOT}/pyproject.toml ONLY. Read it first.\n\n${SPEC}\n\n` +
    `Add [project.optional-dependencies] with an "mcp" extra pulling the MCP Python SDK ` +
    `(mcp>=1.12,<2) and a placeholder "agents" extra (commented or empty list) reserved for ` +
    `Phase 2 — do not add an LLM client now. Keep the base [project] dependency list EMPTY ` +
    `(zero-dependency core is the identity). Confirm package discovery still finds src/sparkle ` +
    `and the console entry point is unchanged. Do not touch any other file.`,
    { label: 'pkg:pyproject-extras', phase: 'CLI + packaging' }
  ),
])

// ---------------------------------------------------------------------------
// Phase: Tests — after cli.py is final so the agent reads the real output
// ---------------------------------------------------------------------------
phase('Tests')
await agent(
  `Update ${ROOT}/tests/test_cli.py for the refactored CLI and new behavior. Read the FINAL ` +
  `cli.py and ops.py and ${DESIGN} first.\n\n${SPEC}\n\n` +
  `Do: (a) repair every node-id extraction site to read the 12-char handle off the "Handle:" ` +
  `line and use it as a prefix (the old split()[-1] on full output is now wrong — there are ` +
  `roughly nine such sites plus the add-branch line parse). (b) Add tests covering: ratified ` +
  `status accepted; the fused add-node --link-to/--relation path; --as alias resolution and ` +
  `re-point-on-reuse; the rule() invariant rejecting a ruling with no inbound objection; the ` +
  `dedup fingerprint returning the existing id on identical re-proposal; JSON import building a ` +
  `graph from stdin; revise superseding + re-homing edges; --ids-only output. All tests use temp ` +
  `dirs, no side effects. The FULL suite must pass — that is the proof the seam holds. Own tests only.`,
  { label: 'tests:repair+cover', phase: 'Tests' }
)

// ---------------------------------------------------------------------------
// Phase: MCP layer — new file, depends on ops.py + the lazy subcommand
// ---------------------------------------------------------------------------
phase('MCP layer')
await agent(
  `Create ${ROOT}/src/sparkle/mcp_server.py — a thin FastMCP adapter over ops.py. Read ${DESIGN} ` +
  `(tool/resource/prompt surface) and ops.py first.\n\n${SPEC}\n\n` +
  `Expose, each a thin wrapper that calls ops.py and never reimplements graph logic:\n` +
  `- TOOLS (mutations): sparkle_add_node, sparkle_link, sparkle_branch, sparkle_rule (calls the ` +
  `  ops.py invariant), sparkle_harvest. Stamp {provisional:true, run_id, agent_role} into ` +
  `  metadata and apply the confidence cap on model-authored nodes via ops helpers. Run the dedup ` +
  `  gate before writing.\n` +
  `- READS as BOTH resources and identically-named tools (so clients lacking resource support ` +
  `  degrade): node, lineage, subgraph, and the frontier (with paging + a tool mirror). Plus ` +
  `  per-run diff/summary and ratify_region/rollback_run over the run_id metadata.\n` +
  `- PROMPTS: /challenge (critic on one node), /investigate (evidence-gatherer), /synthesize ` +
  `  (judge then synthesizer), /next (read frontier, pick highest-leverage, run its prompt). Each ` +
  `  embeds the node-type/relation vocabulary inline. Prompts call NO model themselves.\n` +
  `- A run_server() entry that resolves the graph path (SPARKLE_GRAPH env > client project dir > ` +
  `  cwd/.sparkle/graph.json), runs over stdio, and emits a listChanged notification after each ` +
  `  mutation. Import the mcp SDK at module top (this module is only imported lazily by the CLI, ` +
  `  so the core stays dependency-free). Do NOT build sparkle_run_loop or any model-calling code.`,
  { label: 'mcp:server', phase: 'MCP layer' }
)

// ---------------------------------------------------------------------------
// Phase: Docs — sync the four doc files (the CLI demo is intentionally left alone)
// ---------------------------------------------------------------------------
phase('Docs')
await agent(
  `Sync the docs to everything built in this run. Read ${DESIGN} and the project doc-sync rules ` +
  `in ${ROOT}/CLAUDE.md.\n\n${SPEC}\n\n` +
  `Update README.md, docs/prd.md, docs/roadmap.md, and CLAUDE.md: document the new CLI commands ` +
  `(relations, revise, import, --link-to, --as, --ids-only), the ratified status, the ops.py ` +
  `seam, the sparkle[mcp] extra and "sparkle mcp" command, and the interactive intelligent-` +
  `operator capability. Move shipped items into Completed; mark Phase 2 (autonomous harness) as ` +
  `Next, not done. Keep diagrams/field lists accurate to the new code. Own only these four doc ` +
  `files. Do NOT touch anything under demo/ — the CLI demo is intentionally not refreshed in this run.`,
  { label: 'docs:sync', phase: 'Docs' }
)

// ---------------------------------------------------------------------------
// Phase: Verify — empirical green check
// ---------------------------------------------------------------------------
phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    testsPass: { type: 'boolean' },
    testSummary: { type: 'string', description: 'exact tail of the unittest run' },
    compileClean: { type: 'boolean', description: 'py_compile across all modules incl mcp_server.py' },
    lazyImportOk: { type: 'boolean', description: 'sparkle mcp without the extra prints the install hint and exits 2' },
    mcpSmoke: { type: 'string', description: 'result of the self-installed MCP import/registration smoke test, or why it was skipped' },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['testsPass', 'testSummary', 'compileClean', 'lazyImportOk', 'mcpSmoke', 'problems'],
}
const verify = await agent(
  `Empirically verify the build at ${ROOT}. Run: (1) the full suite ` +
  `\`python3 -m unittest discover -s tests\` and capture the tail; (2) ` +
  `\`python3 -m py_compile\` on every module under src/sparkle/ INCLUDING mcp_server.py (syntax ` +
  `check without importing the mcp SDK); (3) confirm that running the "mcp" subcommand WITHOUT ` +
  `the mcp extra installed prints the "pip install 'sparkle[mcp]'" hint to stderr and exits 2 ` +
  `(the lazy-import error path); (4) MCP SMOKE TEST — YOU install the extra yourself, never ask ` +
  `the user: run \`pip install -e '.[mcp]'\` (use a venv or --user if needed; pass ` +
  `dangerouslyDisableSandbox if the install needs network), then import sparkle.mcp_server and ` +
  `confirm the server object is constructed and its tools/resources/prompts register without ` +
  `error. If the install genuinely cannot run in this environment, record that in mcpSmoke as ` +
  `"skipped: <reason>" — that is acceptable, NOT a failure. AFTER the smoke test, clean up: ` +
  `\`pip uninstall\` is optional but DELETE any build/ and *.egg-info/ artifacts you created so ` +
  `the tree is left clean. Report booleans + the exact test tail + the smoke result + any ` +
  `problems. Do not fix code; just report.`,
  { label: 'verify:empirical', phase: 'Verify', schema: VERIFY_SCHEMA }
)

// ---------------------------------------------------------------------------
// Phase: Adversarial check — skeptics confirm the design corrections survived
// ---------------------------------------------------------------------------
phase('Adversarial check')
const CHECK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    target: { type: 'string' },
    holds: { type: 'boolean' },
    evidence: { type: 'string', description: 'file:line proof read from the actual code' },
    violation: { type: 'string', description: 'if it does not hold, exactly where and how' },
  },
  required: ['target', 'holds', 'evidence', 'violation'],
}
const CHECKS = [
  `SEAM: the debate invariants and the referee live in ops.py, and BOTH cli.py and ` +
  `mcp_server.py reach them through ops.py — neither reimplements graph logic. Verify by reading code.`,
  `DEDUP: the fingerprint hashes normalized node_type+title+content and EXCLUDES created_at, ` +
  `so an identical re-proposal returns the existing id rather than forking the graph.`,
  `NO @last + RATIFIED: there is no "@last" token anywhere; "ratified" is in NodeStatus and is ` +
  `reachable as a real status (not just referenced), and rule() refuses a ruling with no inbound objection.`,
  `CORE PURITY: importing sparkle.cli / sparkle.ops pulls in NO third-party package; the mcp SDK ` +
  `is imported only inside mcp_server.py, which the CLI imports lazily. The base install stays zero-dependency.`,
  `OUTPUT CONTRACT: add-node prints the "Handle:" line as specified and the tests parse THAT line, ` +
  `not the last token of the whole output; the full suite passes.`,
]
const checks = await parallel(CHECKS.map((c, i) =>
  () => agent(
    `You are a skeptical reviewer. Verify ONE claim about the freshly built Sparkle code at ${ROOT} ` +
    `by READING THE ACTUAL CODE (cite file:line). Default to holds=false unless the code proves it.\n\n` +
    `CLAIM TO VERIFY:\n${c}\n\nReport whether it holds, the file:line evidence, and any violation.`,
    { label: `check:${i + 1}`, phase: 'Adversarial check', schema: CHECK_SCHEMA }
  )
))

return {
  verify,
  adversarialChecks: checks.filter(Boolean),
  scopeNote: 'Phases 0-1c built. Phase 2 (autonomous harness) intentionally NOT built — gated on the zero-dependency-vs-own-key decision.',
}
