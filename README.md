# ✨ Sparkle

A research tool that makes claims, evidence, and reasoning traceable — for humans and AI agents.

Instead of scattering research across notes, bookmarks, and chat threads, Sparkle stores it as a typed graph: claims link to evidence, objections, questions, and syntheses. Every conclusion traces back to the path that produced it. Nothing gets lost.

**[See it in action: Does music help you code?](demo/)**

## Why This Exists

Most research tools are good at capture and bad at accountability. Ask yourself:

- What supports this claim? What weakens it?
- What alternative paths did I explore?
- Why did I abandon that line of thinking?
- How exactly did I arrive at this conclusion?

If you can't answer those from your notes, you have a capture tool, not a research tool. Sparkle is the graph layer that makes these questions answerable — by you, by a collaborator reviewing your work, or by an AI agent conducting research on your behalf.

## Where It's Going

The CLI and the MCP server both work today. An AI agent running in Claude Code or Claude Desktop can now operate the graph natively — reading a "what needs attention next" feed, attacking weak claims, gathering evidence, and recording rulings — with the host's own model supplying all the thinking. No server-side model, no API key. See "Intelligent operation" below.

What's left:

| Phase | What | Why |
|-------|------|-----|
| **Autonomous harness** | Hands-off "Sparkle this claim for five rounds and come back" | Needs the server to get model inference on its own (a key or a sampling-capable client) |
| **Real citations** | Structured sources, excerpts, DOI/URL, BibTeX interop | Flat strings aren't verifiable sources |
| **Deeper introspection** | `gaps`, `tensions`, `stale`, `orphans` as first-class commands | Tell an agent what to work on next without reading the whole graph |

The big remaining call is the **autonomous harness**: today the model has to take turns with you (interactive). Running unattended needs the server to do its own thinking, which forces a decision about whether to add an LLM dependency to a tool whose whole identity is "pure Python, zero dependencies."

See [`docs/roadmap.md`](docs/roadmap.md) for the full plan.

## Quick Start

```bash
git clone <repo> && cd sparkle

# Optional: install the `sparkle` console command, then drop the PYTHONPATH prefix
pip install -e .   # afterwards you can run `sparkle init`, `sparkle home`, etc.

# Initialize a graph store
PYTHONPATH=src python3 -m sparkle init

# Seed with an example graph
PYTHONPATH=src python3 -m sparkle bootstrap

# See the dashboard
PYTHONPATH=src python3 -m sparkle home

# Explore
PYTHONPATH=src python3 -m sparkle tree <node_id_prefix>
PYTHONPATH=src python3 -m sparkle show <node_id_prefix>
PYTHONPATH=src python3 -m sparkle why <node_id_prefix>
```

Or explore the pre-built demo graph:

```bash
PYTHONPATH=src python3 -m sparkle --store demo/.sparkle/graph.json home
PYTHONPATH=src python3 -m sparkle --store demo/.sparkle/graph.json tree e036ff896cea
```

## Intelligent Operation (MCP)

Sparkle ships an MCP server so an AI agent can operate the graph natively — no shelling out, no ASCII parsing. Today this works **interactively** on Claude Code and Claude Desktop: you run a slash-command prompt, the host's own model reads a "where is the debate weak right now" feed, reasons about it, and calls mutation tools to write objections, evidence, and rulings back into the graph. The server supplies structure, targets, and guardrails; the host model supplies all the thinking. No server-side model and no API key.

```bash
pip install 'sparkle[mcp]'                 # adds the MCP SDK; the core stays dependency-free
claude mcp add sparkle -- sparkle mcp       # register the server with a client
```

The `sparkle mcp` command runs the server over stdin/stdout. It is a lazy seam: every other command works without the extra installed, and a missing extra fails with a clear `pip install 'sparkle[mcp]'` message.

What the server exposes:

- **A work frontier** (`sparkle://frontier`, also a tool) — a prioritized "what needs adversarial attention next" feed computed purely from the stored graph: unchallenged claims, open questions, stalled threads, thin-evidence claims, and claims ready to judge. Live, non-persisted aggregates (a supports-minus-contradicts tally plus node type/status), never the frozen stored confidence.
- **Mutation tools** that enforce debate rules the raw CLI doesn't — for example, the judge's "rule" move refuses to ratify a claim the adversary never attacked.
- **Adversarial prompts** as slash commands: `/challenge` (cast the model as critic), `/investigate` (evidence-gatherer), `/synthesize` (judge then synthesizer), `/next` (read the frontier and run the highest-leverage move).

The guardrails live in the shared operations layer (`ops.py`), not in the MCP wrapper, so the CLI, the MCP server, and any future harness all inherit the same rules through the same error boundary. Running **fully autonomously** (no human turn-taking) is not built yet — see the roadmap.

## How It Works

### The graph model

Research is a graph of typed nodes connected by typed edges:

- **Nodes**: `claim`, `evidence`, `question`, `objection`, `inference`, `decision`, `synthesis`
- **Edges**: `supports`, `contradicts`, `refines`, `derived_from`, `evaluates`, `produced`, `supersedes` (the CLI validates against this set; the Python kernel accepts custom relation sets)
- **Status**: `active`, `stalled`, `weakly_supported`, `promising`, `abandoned`, `harvested`, `ratified` (terminal — a judge's binding ruling; only a settled ruling can write it)
- **IDs**: SHA256 content hashes — same content always produces the same ID, tamper-evident by construction

### Branch templates

Recurring research moves have templates so you don't have to remember node types and relations:

| Template | Creates | Relation | Use when |
|----------|---------|----------|----------|
| `support` | evidence | supports | Adding evidence for a claim |
| `objection` | objection | contradicts | Challenging a claim |
| `reframing` | question | refines | Asking a better question |
| `application` | claim | derived_from | Drawing a practical conclusion |

```bash
PYTHONPATH=src python3 -m sparkle add-branch \
  --from <claim_id> --template support \
  --title "Primary source evidence" \
  --citations "https://example.com/paper"
```

### Storage

All state lives in a single human-readable JSON file (`.sparkle/graph.json`). Nodes and edges are keyed by content hash. Identical payloads collapse to the same ID. The store is immutable in spirit — provenance remains stable as the graph grows.

## CLI Reference

| Command | What it does |
|---------|-------------|
| `init` | Create an empty graph store |
| `bootstrap` | Seed with example nodes from the concept conversation |
| `home` | Dashboard with counts, recent nodes, next actions |
| `add-node` | Create a node with type, title, content, confidence (validated to 0.0-1.0), status, tags; optionally fuse a link in one call (`--link-to <prefix> --relation <rel>`) and register a name (`--as <name>`) |
| `add-edge` | Link two nodes with a relation (validated against the known relation set); endpoints accept ids, short prefixes, or alias names |
| `add-branch` | Templated node + edge creation for common research moves |
| `revise` | Supersede a node with a corrected version and re-home its inbound edges — model-honest editing that never mutates the frozen original (`--no-rehome-edges` to leave edges on the old version) |
| `import` | Build a graph fragment from a JSON document (file path or `-` for stdin); prints a nickname-to-id map. The LLM/batch on-ramp |
| `list-nodes` | List nodes through a single unified filter (type, status, tag, query, limit); `--ids-only` prints bare handles for fast scanning before linking |
| `list-edges` | List all edges |
| `list-templates` | Show available branch templates |
| `relations` | Show each edge relation with a plain-English direction gloss |
| `show` | Inspect a node with all its incoming and outgoing edges |
| `tree` | ASCII tree of a node's immediate neighborhood |
| `why` | Trace inbound provenance chain |
| `lineage` | Walk all inbound ancestors (BFS) |
| `export` | Export a subgraph rooted at a node to markdown |
| `mcp` | Run the MCP server over stdin/stdout (requires the `sparkle[mcp]` extra) |

All commands accept `--store <path>` to use a non-default graph file.

After every add, Sparkle echoes a short 12-character handle on its own `Handle:` line (so you stop copying 64-character hashes) and a spoken-word link confirmation (`Linked: evidence "X" --supports--> claim "Y"`) so you catch a backwards edge at a glance. Use explicit short prefixes or alias names when linking; there is deliberately no "last node" token, because whole-second timestamps have no tiebreaker and would silently attach an edge to the wrong node.

## Architecture

### Source layout

```
src/sparkle/
  __init__.py       # package marker, re-exports the reusable graph kernel
  models.py         # Node, Edge — frozen dataclasses with content-addressed IDs
  graph.py          # GraphStore — JSON-backed storage, traversal, subgraph, lineage export
  ops.py            # the operations contract — every action as a function returning dicts;
                    #   debate invariants, referee/transition engine, dedup gate, alias sidecar,
                    #   frontier, file lock. The single surface every front-end calls.
  cli.py            # CLI — a thin formatter; each command makes one ops.py call and prints
  mcp_server.py     # MCP server — a thin FastMCP adapter over ops.py (only loaded with the extra)
  presentation.py   # render_tree, render_why, export_markdown — read-only rendering over a store
  templates.py      # BranchTemplate — opinionated inquiry workflows
  bootstrap.py      # seeds example graph from concept conversation
  __main__.py       # python -m sparkle entrypoint
tests/
  test_cli.py       # 35 integration tests via unittest
demo/
  README.md         # demo overview with mermaid graph
  WALKTHROUGH.md    # conversational walkthrough of building a claim graph
  exported-research.md  # sparkle export output
  .sparkle/graph.json   # the demo graph (17 nodes, 19 edges)
docs/
  prd.md            # product requirements
  roadmap.md        # what's built, what's next
```

The `ops.py` seam is the structural keystone: the CLI never touches the store directly, and neither does the MCP server. Both bottom out in the same functions behind one `ValueError` boundary, so the debate rules are enforced in exactly one place and the three front-ends (CLI, MCP, future harness) stay interchangeable. The optional `sparkle[mcp]` extra only pulls the MCP SDK; the core stays pure stdlib with zero external dependencies.

### Diagrams

<details>
<summary>Class model</summary>

```mermaid
classDiagram
    class Node {
      +node_type
      +title
      +content
      +citations[]
      +author
      +created_at
      +confidence
      +status
      +tags[]
      +metadata
      +to_payload()
      +compute_id()
    }

    class Edge {
      +from_id
      +to_id
      +relation
      +note
      +created_at
      +metadata
      +to_payload()
      +compute_id()
    }

    class GraphStore {
      +path
      +node_types
      +edge_relations
      +init()
      +read()
      +add_node(node)
      +add_edge(edge)
      +list_nodes()
      +list_edges()
      +resolve_id(prefix)
      +get_node(node_id)
      +get_neighbor_details(node_id)
      +lineage(root_id)
      +subgraph(root_id)
      +export_lineage(root_id)
    }

    class presentation {
      +render_tree(store, root_id)
      +render_why(store, root_id)
      +export_markdown(store, root_id, output)
    }

    class BranchTemplate {
      +name
      +node_type
      +relation
      +default_status
      +description
      +prompt_prefix
      +edge_note
    }

    GraphStore --> Node : stores
    GraphStore --> Edge : stores
    BranchTemplate --> Node : configures
    presentation --> GraphStore : reads
```
</details>

<details>
<summary>Add-branch sequence</summary>

```mermaid
sequenceDiagram
    actor User
    participant CLI as sparkle.cli
    participant Store as GraphStore
    participant Tpl as BranchTemplate
    participant JSON as graph.json

    User->>CLI: add-branch --from <claim> --template support --title ...
    CLI->>Store: resolve_id(from_prefix)
    Store->>JSON: read nodes
    JSON-->>Store: matching node id
    Store-->>CLI: parent node id
    CLI->>Store: get_node(parent_id)
    Store->>JSON: read parent node
    JSON-->>Store: parent node payload
    Store-->>CLI: parent node
    CLI->>Tpl: build_branch_node(parent_title, template, title, ...)
    Tpl-->>CLI: branch Node + template metadata
    CLI->>Store: add_node(branch_node)
    Store->>JSON: write node by content hash
    JSON-->>Store: persisted
    Store-->>CLI: branch_id
    CLI->>Store: add_edge(branch_id -> parent_id, template.relation)
    Store->>JSON: write edge by content hash
    JSON-->>Store: persisted
    Store-->>CLI: edge_id
    CLI-->>User: branch node id + branch edge id
```
</details>

<details>
<summary>Node status model</summary>

```mermaid
stateDiagram-v2
    [*] --> active
    active --> promising
    active --> stalled
    active --> weakly_supported
    weakly_supported --> promising
    weakly_supported --> abandoned
    stalled --> active
    stalled --> abandoned
    promising --> harvested
    promising --> active
    harvested --> active
    active --> abandoned
    abandoned --> active
    active --> ratified : settled ruling (rule --settle)
    ratified --> [*]
```

`ratified` is a terminal status reached only through a settled judge's ruling, never set freehand. Because nodes are frozen, ratification is realized model-honestly: the ruling writes a decision node and a *superseding* claim version carrying `status=ratified` and a `supersedes` edge to the original — never an in-place mutation. `harvested` and `abandoned` are likewise terminal in spirit; a model-authored write may not set any of the three directly.
</details>

## Testing

```bash
python3 -m unittest discover -s tests -v
```

35 tests covering: init, bootstrap, node/edge CRUD, branch templates, show/tree/why rendering, filtered listing (type/status/tag/query/limit), `list-nodes --ids-only`, home dashboard, lineage, markdown export, content-addressing idempotency, custom node-type/relation graph kernels with metadata, lineage-only vs full-component export, `n/a` confidence rendering, dangling-edge export safety, corrupt-store handling, error handling (invalid lookups, ambiguous prefixes, unknown relations, out-of-range confidence), and the new operations layer: the `ratified` terminal status, the fused create-and-link path (`--link-to`/`--relation`, including the paired-or-error guard), alias registration and re-pointing on reuse (`--as`), the ruling invariant that refuses to ratify an unchallenged claim, the content-fingerprint dedup gate returning the existing id on identical re-proposal, JSON import from stdin (plus invalid-JSON rejection), and `revise` superseding a node with and without re-homing inbound edges.

## Current Limits

- No autonomous operation — the model takes turns with you (interactive). Running unattended needs the server to do its own thinking, which is the open zero-dependency decision (see the roadmap).
- Single-context adversary — when one model plays proposer, critic, and judge in sequence, the referee can guarantee an objection-shaped node exists but not that it is independent or non-strawman. Today this is honestly "structured, human-refereed inquiry with model-assisted drafting," not autonomous adversarial research.
- One front-end at a time per graph — an advisory file lock guards each read-modify-write, but there is no multi-writer guarantee beyond that.
- Citations are flat strings — no structured source metadata
- No first-class introspection commands — the frontier covers "what needs attention" via MCP, but `gaps`/`tensions`/`stale`/`orphans` are not yet standalone CLI commands
- No UI — terminal only
- Semantic dedup is out of scope — exact-fingerprint re-proposals auto-dedup, but near-duplicates are not detected (true fuzzy matching would force a model dependency)
