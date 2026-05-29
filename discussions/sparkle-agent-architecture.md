# Sparkle as an intelligently-operable claim graph — architecture & decision record

- **Status:** Design only. No code written. Produced 2026-05-29.
- **Why this exists:** Albert asked whether Sparkle should grow an MCP server and/or an
  agent harness, and pushed on the real question: "the system is great but it needs to be
  operated intelligently — how do we do that JUST with an MCP server?" This records the
  answer, the architecture, the issues an adversarial review caught, and the decisions
  Albert still owes before anyone writes code.
- **Connects to:** the Sparkle codebase (`src/sparkle/`), the code-review fixes already
  merged to `main` (commit `59c205a`), and the CLI-ergonomics findings from the
  "what would make this more useful" pass (summarized at the end).
- **How this was produced:** two multi-agent passes (a usefulness diagnosis and an
  architecture design), each grounded in the real code and pressure-tested by skeptics.
  Claims below were checked against the source; one skeptic claim was found wrong and
  is corrected inline (see "Edge direction").

---

## Verdict: can you operate Sparkle intelligently with JUST an MCP server?

**Interactively, yes — today, on Claude Code and Claude Desktop.** You run a slash-command
prompt (e.g. `/challenge`), the host's own model reads a "where is the debate weak right now"
feed, reasons about it, and calls mutation tools to write objections, evidence, and rulings
back into the graph. The server supplies structure, targets, and guardrails; the host model
supplies all the thinking. No server-side model, no API key, no new network dependency. This
is built entirely on the three MCP primitives (tools + resources + prompts) over a local
stdin/stdout connection — all confirmed working on the target clients.

**Fully autonomously (no human turn-taking), no — not with just an MCP server on the
mainstream clients.** "Sparkle this claim for five rounds and come back" needs the server to
get model inference on its own. The protocol's mechanism for a server to borrow the host's
model (called *sampling*) is **not supported in Claude Code or Claude Desktop** as of this
writing (tracked in an open GitHub issue, no ship date). Autonomy therefore has exactly two
honest paths, and both are product decisions, not code details:

1. The server brings its **own LLM API key** and runs the agent roles itself. This is the only
   reliable autonomous path on the Anthropic clients today — but it puts the first secret,
   network call, and cost into a project whose whole identity is "pure Python, zero
   dependencies." Albert must decide if that trade is acceptable.
2. On a third-party client that genuinely implements sampling, delegate the thinking to the
   host. The exact set of such clients is **unverified**, so the design probes for the
   capability at startup and never assumes it.

If neither backend is wired up, the autonomous tool must not fake it — it returns "autonomy
unavailable on this client, use the interactive loop instead" and points at the prompts.

**Bottom line:** intelligent operation with just an MCP server is real and shippable *now* in
interactive mode. The standalone harness is not a prerequisite for intelligent operation — it
is an optimization for hands-off autonomy, and it is the one piece that forces the
zero-dependency decision.

---

## Core architecture: the seam is the whole design

Everything hinges on one move: **put a thin, stdlib-pure "graph operations" layer underneath
everything, and make it the single contract every front-end calls.**

- **New module (`src/sparkle/ops.py`) = the contract.** Every graph action becomes a plain
  Python function that takes a `GraphStore` plus primitive arguments, returns plain
  JSON-able dicts, and raises `ValueError` for user errors. These are a 1:1 lift of what
  `cli.main()` does today. They return *data*, never print. No argparse, no MCP, no model.
- **The CLI becomes a thin formatter.** Each command keeps its argument parsing and its
  `print()` calls, but the actual work becomes one call into `ops.py`. Zero behavior change —
  the existing test suite (`tests/test_cli.py`) passing unchanged is the proof the seam holds.
- **The MCP server is a thin adapter** over `ops.py`. So is the optional future harness.
  Because all three front-ends bottom out in the same functions with the same error
  boundary, they are genuinely interchangeable and the substrate never gets reworked when
  the harness arrives.

### The one correction that makes "interchangeable" actually true (skeptic catch)

The naive version puts the debate guardrails (no ruling without a challenge, cap
model-authored confidence, refuse model-written terminal statuses, dedup) in the **MCP tool
layer**. That is wrong, and a reviewer caught it: the Phase-2 autonomous harness is specced
to call `ops.py` **directly, not through MCP**. If the guardrails live in the MCP layer, the
harness bypasses every one of them — and you'd have to reimplement or relocate them, which is
exactly the rework the seam was supposed to prevent.

**Fix, baked into the plan below:** the debate invariants and the referee/scorekeeper that
interprets them live **in `ops.py` from the start** (e.g. an `ops.rule()` that raises
`ValueError` if the target claim has no inbound objection). The MCP tools become genuinely
thin wrappers; the harness calling `ops.py` directly is then safe by construction. This is the
single most important structural decision in the document.

---

## The three MCP primitives, and the concrete surface

Most people think "MCP server = a bag of tools." It is three things, and the intelligence
lives in using all three:

- **Resources** tell the model *where the work is.* The headline one is the **work frontier**
  (`sparkle://frontier`): a prioritized "what needs adversarial attention next" feed computed
  purely from the stored graph — no model, no writes. Buckets: claims nobody has challenged,
  open questions with no answer, stalled threads, thin-evidence claims, and claims ready to
  judge (have both support and contradiction but no ruling yet). Plus per-node, per-lineage,
  and per-subgraph context pulls, and a per-run "here's the region this run created" review
  surface.
- **Tools** are the *only* way to mutate the graph, and they enforce debate rules the raw CLI
  does not: create a typed node, link two nodes, run a one-call "branch" move (the four debate
  moves — support, object, reframe, apply), the judge's "rule" move (refuses if the claim was
  never challenged), the "harvest/synthesize" closing move, plus the frontier mirrored as a
  tool (so clients without resource support still get the feed), and region ratify/rollback.
- **Prompts** are the *adversarial playbook as slash commands*: `/challenge` (cast the model
  as critic on one node), `/investigate` (cast it as evidence-gatherer), `/synthesize` (cast
  it as judge then synthesizer), `/next` (read the frontier, pick the highest-leverage item,
  run its suggested prompt). Each prompt embeds the node-type and relation vocabulary inline
  so the model writes well-typed nodes without you re-explaining the schema. The prompts call
  no model themselves — the host's model executes them.

Every read is exposed as **both** a resource and a tool, so a client lacking resource support
degrades gracefully to tool calls.

---

## The adversarial loop is a data file, not Python

Treat the debate as a **playbook** the server reads (a JSON resource), not behavior baked into
code. The playbook names four things:

- **Roles** — each role names the node type it may create and the relation it must attach
  (proposer → claim; critic → objection via `contradicts`; evidence-gatherer → evidence via
  `supports`; judge → decision; synthesizer → synthesis via `produced`).
- **Phases** — an ordered list (propose, critique, gather, judge, synthesize) with a
  per-phase iteration cap.
- **Transition rules** expressed as data over a claim's inbound edge tally (e.g. "more
  contradictions than supports → weakly supported"; "both sides strong and roughly equal →
  stalled").
- **Stop conditions** (judge ruled; critique rounds exhausted with no new objection; stalled
  twice → abandoned).

The payoff: the **same playbook runs on two engines**. The interactive engine (default, works
everywhere today) is the host's model reading the playbook and calling the move-tools in
order. The autonomous engine (optional, capability-gated) walks the identical playbook but
fills each role's text via the server's own thinking backend, committing through the identical
move-tools and the identical referee. Same config, same guardrails, same output — the only
difference is who supplies the thinking. Ship one built-in playbook; keep the format open so a
power user can drop in a two-critic red-team variant with no code change.

---

## Module layout and packaging (the stdlib-pure boundary stays intact)

- **Core (zero new dependencies, identity untouched):** `models.py`, `graph.py`,
  `templates.py`, `bootstrap.py`, `presentation.py` unchanged. **New `ops.py`** holds the
  contract, the referee/transition engine, and the content-fingerprint dedup helper — all
  pure stdlib. `cli.py` refactored to a thin formatter over `ops.py`.
- **`sparkle[mcp]` optional extra:** pulls the MCP SDK; `src/sparkle/mcp_server.py` registers
  a thin tool/resource/prompt over each `ops.py` function. Resolves the graph path at startup
  (env var, then the client's project dir, then current dir). Runs over stdin/stdout.
- **`sparkle[agents]` optional extra (Phase 2):** pulls an LLM SDK; `src/sparkle/harness.py`
  holds its own model client/key, reads graph state via `ops.py`, and executes moves via the
  same `ops.py` functions — it does not go through MCP and does not reimplement graph logic.
- **One console entry point.** Add an `mcp` sub-command (later an `agents` sub-command) to the
  existing parser, with a lazy import so every other command still works without the extra
  installed; a missing extra fails with a clear "pip install sparkle[mcp]" message on the same
  `ValueError`-style boundary. Wired into a client via `claude mcp add sparkle -- sparkle mcp`.

The model-inference dependency lives only in `[agents]` (and optionally inside specific
opt-in "smart" tools) — never in core, never assumed from the host.

---

## Phased build plan

- **Phase 0 — Carve the seam (+ ergonomics track).** Create `ops.py` (every action as a
  function returning dicts, raising `ValueError`); refactor `cli.py` to a thin formatter.
  **Net user-visible change: none** for the refactor itself — the existing tests passing
  unchanged is the proof. Put the invariants and referee in `ops.py` here. Then, riding on the
  same `ops.py`, land the small human-ergonomics wins (handle echo, fuse create+link, sidecar
  names, relation legend, cold-start polish) and the shared `import`/fused-create primitive.
  Pure stdlib, no new dependency, no MCP.
- **Phase 1a — MCP substrate.** The `sparkle[mcp]` extra; mutation tools and read
  tools/resources; the `mcp` sub-command and lazy import; stdin/stdout wiring. All primitives
  confirmed safe on the target clients.
- **Phase 1b — Work frontier + adversarial prompts.** The frontier resource and its tool
  mirror; the `/challenge`, `/investigate`, `/synthesize`, `/next` prompts; the playbook
  resource and the referee. **This is the complete interactive intelligent operator — works
  fully on Claude Code/Desktop with no model on the server.**
- **Phase 1c — Safety rails.** Content-fingerprint dedup gate, provisional-write policy
  (metadata stamp + confidence cap + refuse model-authored terminal statuses), run tagging,
  and the region-review/rollback surface. Makes the substrate safe to point a model at.
- **Phase 2 — Autonomous harness (gated, optional).** The `sparkle[agents]` extra; the
  autonomous engine + a session governor (round/write/cost caps; convergence/oscillation/
  deadlock stops) behind a swappable thinking backend; capability-probed at startup, degrades
  to the interactive loop when no backend is wired. **Requires the zero-dependency decision
  resolved first.**

---

## The hard problems the review caught (these reshaped the design)

1. **"Plus sampling" was an over-claim.** Sampling is unsupported on the mainstream clients,
   so the honest thesis is "interactive intelligent operation, yes; autonomous, only with a
   server key or a sampling-capable client." Reflected in the verdict above.
2. **Single-context adversary — the deepest catch.** If the same model, in the same context
   window, plays proposer then critic then judge in sequence, it is not an adversary — it is
   one mind arguing with its own recent output, which is the exact sycophancy/anchoring
   failure the adversarial structure is meant to defeat. The referee can guarantee an
   objection-shaped node *exists*, but not that it is good, independent, or non-strawman; a
   single-context model can satisfy "no ruling without challenge" by writing a weak objection
   and ruling against it — laundering self-agreement as a survived challenge. **Fixes:**
   (a) force role isolation — invoke the critic in a context that does not contain the
   evidence pass; and/or (b) make the human the adversary the tool actually has — the model
   *drafts* candidate objections, the human accepts/edits/rejects before they are written.
   Until isolation is enforced, describe this honestly as "structured, human-refereed inquiry
   with model-assisted drafting," not "autonomous adversarial research."
3. **No terminal "decided/ratified" status.** The six node statuses (active, stalled,
   weakly_supported, promising, abandoned, harvested) have no value meaning "the judge ruled
   and it is binding." The whole loop converges on a decision node, but the claim it judged
   has nowhere to record the verdict. **Decide before building:** add a seventh terminal
   status (e.g. `decided`/`ratified`), or explicitly define closure as "a decision node plus a
   live display signal" and document it.
4. **Invariants must live in `ops.py`, not the MCP layer** — see the seam correction above.
   This is the catch that keeps Phase 2 additive instead of a redesign.
5. **Edge direction — skeptic was wrong here, corrected.** A reviewer claimed `lineage(claim)`
   would miss a ruling. It does not: branch children (evidence, objections, and a ruling made
   the same way) are created pointing child → parent (`from_id=child`, `to_id=claim`), and the
   lineage/why walk follows inbound edges, so they are surfaced. The real, smaller care: keep
   the judge's "rule" move consistent with the existing branch convention, and verify the
   frontier's "ready to judge" query when it is written.
6. **Confidence is immutable; there is no rollup.** A node is frozen and its stored confidence
   never changes, so a frontier bucket keyed on "stored confidence < 0.4" can essentially
   never fire. Key the frontier on the edge tally and node type/status instead, and treat any
   supports-minus-contradicts number as a **display-only live aggregate** computed on each
   read — never imply the stored value changed.
7. **No write lock.** The store does read-modify-write on one JSON file with an atomic replace
   per write, but no lock across a read-then-write sequence. A human in Claude Code and an
   autonomous loop on the same graph can interleave and silently lose an edge. **Declare "one
   front-end at a time per graph" for the MVP; add a file lock before Phase 2.**
8. **Content-addressing folds the timestamp into the node ID.** So the existing collision
   check can never catch a re-proposal made a second later — an eager model silently forks the
   graph with near-duplicate claims. The dedup gate must fingerprint over normalized
   type+title+content (deliberately excluding the timestamp). Semantic near-duplicates remain
   a judgment call (true semantic dedup needs embeddings, which forces a model dependency).

---

## Open decisions Albert owes before any code

1. **Zero-dependency identity vs. autonomous mode.** Keep autonomy sampling-only (effectively
   off on the Anthropic clients until the protocol gap closes, preserving purity), or accept
   an isolated optional LLM dependency behind `sparkle[agents]` for the autonomous path. This
   is the biggest call and gates all of Phase 2.
2. **Where a status change lives.** Live, non-persisted display aggregate (recommended, no
   schema change), or a superseding node written on every flip (audit-pure but doubles a claim
   each time and bloats the graph).
3. **The real dedup key.** Exact normalized title+content match (safe to auto-merge), or route
   near-duplicates to a human review queue (recommended). Auto-merging fuzzy matches risks
   silently destroying a distinct claim.
4. **A terminal "decided" status** — add one, or define `harvested` as binding and document it
   (catch #3).
5. **Single intended write path.** MCP-only, or push the invariants down into the store so the
   CLI and MCP cannot diverge on what counts as a legal move (catch #4 leans toward `ops.py`).
6. **Concurrency contract** — "one writer per graph" for the MVP, or a file lock now
   (catch #7).
7. **Where per-run state lives** between tool calls — in-memory per process (simplest), with
   the run id stamped on each written node as the durable backstop (recommended).

---

## The human ergonomics layer (from the usefulness pass)

This is not a footnote — it is the other half of the design. The separate "what would make
this more useful" pass diagnosed the heavy-handedness precisely: **the tool puts its
64-character internal storage hash in your hand as the thing you copy, and makes every claim
and every link its own command.** Capturing one argument you already hold in your head — a
claim, a source that backs it, a counter-claim — costs seven commands and two manual hex
copy-pastes. The immutable, content-addressed model is sound and stays; what is missing is a
human-friendly surface on top of the raw machinery.

The decisive overlap: the two highest-value ergonomics fixes — **fuse create-and-link into one
command** and **import a whole argument from JSON** — are the *same primitive* as the MCP
`assert_claim`-shaped tools and the `ops.py` contract. Build `ops.py` once and it serves the
lighter CLI and the MCP server from the same code. So the ergonomics track is not a parallel
project; it is `ops.py` plus thin CLI surface, sharing Phase 0.

The ranked fixes below are the **post-review (reshaped) versions** — each survived an
adversarial pass that caught real bugs in the naive proposals:

1. **Echo a short handle + next step after every add** (small). After an add, show the title
   and a 12-character handle so you stop copying 64-char hashes. *Reshaped:* put the handle on
   its own `Handle:` line — nine tests parse the last token of the output as the full hash and
   would break otherwise — and do **not** auto-suggest an "attach evidence" command, because
   the edge direction would be backwards (the new node is the *source* of a `supports` edge,
   not the target). Match the real invocation form, not a hard-coded `sparkle ...`.
2. **Fuse create + link** (small): `--link-to <prefix> --relation` on add-node, collapsing the
   claim → evidence → objection sequence from seven commands to three with zero hash copies.
   *Reshaped:* **drop the proposed `@last` token.** Timestamps are whole-second only with no
   tiebreaker, so two nodes added in the same second make "last" silently attach to the wrong
   claim — a wrong edge that looks right, in a store with no delete. Use explicit short
   prefixes (the resolver already accepts them).
3. **User-chosen names via a sidecar file** (small): `--as remote-claim`, then refer to it by
   name anywhere. *Reshaped:* keep only user-chosen names (stored in `.sparkle/aliases.json`,
   outside the hashed payload, so IDs and portability are untouched); **drop** the auto-`n1/n2`
   counter (mutable state that desyncs on merge/bootstrap/supersede) and the duplicate `last`
   pointer. Re-point a name to the new node when it is reused, so names survive supersession.
4. **Relation legend + spoken-word link confirmation** (small): a `relations` command
   explaining each relation's direction, and "Linked: evidence X --supports--> claim Y" after
   each link so you catch backwards edges. *Reshaped:* drop the "list valid relations on
   error" part — argparse already does it; only the direction gloss and the echo are new.
5. **A `revise` command** (medium): supersede a node with a corrected one and re-home its
   edges — model-honest editing that never mutates. Open product decision: auto-rehome all
   edges vs. ask.
6. **Import an argument from JSON on stdin** (medium): the LLM/batch on-ramp — paste a JSON
   document of claims and links and it builds the graph, printing a nickname → id map. Expose
   an optional timestamp in the payload for deterministic re-import (since the timestamp is in
   the ID).
7. **Cold-start polish** (small): print the real `sparkle` command in hints (not the dev-only
   `python3 -m ...` form), and add `--ids-only` to list-nodes for fast scanning before linking.

Items 1–4 and 7 are small presentation/CLI wins that ride on Phase 0's `ops.py` refactor and
can ship in the same pass. Items 2 and 6 are the shared primitive with the MCP layer. None
conflict with the architecture above.

---

## Where to resume

- **Phase 0 is safe to start anytime** — it is a pure refactor with no product decisions
  except putting the invariants in `ops.py` (catch #4).
- **Phases 1a–1c** need only the status question (decision #4) and the write-path/concurrency
  declarations (decisions #5, #6) settled.
- **Phase 2 is gated** on the zero-dependency-vs-key decision (decision #1).
- Recommended first conversation with Albert: decisions #1 and #4, because they shape
  everything downstream.
</content>
</invoke>
