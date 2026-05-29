"""Sparkle operations contract — the single stdlib-pure surface every front-end calls.

This module is the *seam* of the whole design (see
``discussions/sparkle-agent-architecture.md``). The CLI, the MCP server, and any
future autonomous harness all bottom out here, so the debate rules and the
content-dedup gate are enforced in exactly one place, behind one ``ValueError``
boundary.

Contract for every public function:

- Takes a :class:`~sparkle.graph.GraphStore` plus primitive arguments.
- Returns a plain JSON-able dict (or list/str) — never a dataclass, never a print.
- Raises :class:`ValueError` for user-facing errors. Nothing else is caught here.

Hard rules baked in (the "locked decisions"):

- The debate invariants and the referee/transition engine live HERE, not in any
  front-end. Front-ends are thin wrappers and inherit the rules for free.
- Status/strength signals are LIVE display-only aggregates (supports-minus-
  contradicts tally + node type/status). The stored ``confidence`` of a frozen
  node is never mutated and is never used as a frontier bucket key on its own.
- "Ratified" is realized model-honestly: a ruling writes a decision node and,
  when the judge settles, a *superseding* claim version with status=``ratified``.
  Nodes are frozen; nothing is mutated in place.
- The dedup fingerprint hashes normalized ``node_type + title + content`` and
  DELIBERATELY excludes ``created_at``/``author``/``confidence`` so a re-proposal
  a second later returns ``{created: False}`` instead of forking the graph.
- "@last" is forbidden. Whole-second timestamps have no tiebreaker, so a
  "last node" token silently attaches edges to the wrong node. Use explicit
  short id prefixes (``resolve_id`` already accepts them).
- Aliases live ONLY in ``.sparkle/aliases.json`` beside the store, never in any
  hashed payload.

Concurrency contract for this MVP: one front-end at a time per graph. A best-
effort advisory file lock (:func:`graph_lock`) wraps each read-modify-write so a
human in one client and a loop in another do not silently clobber an edge. It is
advisory only — no multi-writer guarantee beyond it.

Pure stdlib. No argparse, no MCP, no model/LLM dependency anywhere in this file.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .bootstrap import seed_concept_graph
from .graph import GraphStore
from .models import DEFAULT_EDGE_RELATIONS, Edge, Node, NodeStatus, NodeType
from .templates import BRANCH_TEMPLATES, build_branch_node
from . import presentation

try:  # POSIX advisory locking; absent on Windows.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore


# ---------------------------------------------------------------------------
# Vocabulary (re-exported so front-ends do not re-derive it from the model)
# ---------------------------------------------------------------------------

from typing import get_args

VALID_NODE_TYPES: list[str] = list(get_args(NodeType))
VALID_NODE_STATUSES: list[str] = list(get_args(NodeStatus))
VALID_EDGE_RELATIONS: list[str] = sorted(DEFAULT_EDGE_RELATIONS)

# Statuses that mean "this thread is closed." A model may not author these
# directly; only a settled ruling (via :func:`rule`) may write ``ratified``.
TERMINAL_STATUSES: frozenset[str] = frozenset({"ratified", "harvested", "abandoned"})

# Confidence ceiling for nodes a model authored without human sign-off.
DEFAULT_MODEL_CONFIDENCE_CAP: float = 0.5

# Plain-English direction gloss for each relation. ``from`` is always the
# source node id, ``to`` the target — the arrow reads "from --relation--> to".
RELATION_GLOSS: dict[str, str] = {
    "supports": "evidence/claim that strengthens the target claim",
    "contradicts": "objection/evidence that weakens or attacks the target claim",
    "refines": "reframes, narrows, or sharpens the target",
    "derived_from": "this node is a downstream implication of the target",
    "evaluates": "a judging/decision node assessing the target",
    "produced": "the target produced this node (e.g. a synthesis)",
    "supersedes": "this node replaces the target with a corrected version",
}


# ---------------------------------------------------------------------------
# Advisory file lock around read-modify-write (concurrency contract)
# ---------------------------------------------------------------------------


@contextmanager
def graph_lock(store: GraphStore) -> Iterator[None]:
    """Best-effort advisory lock around a read-modify-write on ``store``.

    Uses a ``.lock`` sidecar next to the store path with POSIX ``fcntl`` when
    available; degrades to a no-op where it is not. This is the MVP "one
    front-end at a time per graph" guarantee — advisory only.
    """
    if fcntl is None:
        yield
        return
    lock_path = store.path.with_name(store.path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Serialization helpers — keep everything JSON-able
# ---------------------------------------------------------------------------


def _handle(node_id: str) -> str:
    return node_id[:12]


def _node_view(node_id: str, node: dict[str, Any]) -> dict[str, Any]:
    """A flat, JSON-able view of a stored node plus its short handle."""
    return {
        "node_id": node_id,
        "handle": _handle(node_id),
        "node_type": node["node_type"],
        "title": node["title"],
        "content": node["content"],
        "status": node["status"],
        "confidence": node.get("confidence"),
        "citations": list(node.get("citations", [])),
        "author": node.get("author", "local"),
        "tags": list(node.get("tags", [])),
        "created_at": node.get("created_at"),
        "metadata": dict(node.get("metadata", {})),
    }


def _edge_view(edge_id: str, edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "handle": _handle(edge_id),
        "from_id": edge["from_id"],
        "to_id": edge["to_id"],
        "relation": edge["relation"],
        "note": edge.get("note", ""),
        "created_at": edge.get("created_at"),
        "metadata": dict(edge.get("metadata", {})),
    }


# ---------------------------------------------------------------------------
# Content-fingerprint dedup (excludes created_at/author/confidence, per SPEC)
# ---------------------------------------------------------------------------


def content_fingerprint(node_type: str, title: str, content: str) -> str:
    """SHA256 over normalized ``node_type + title + content``.

    Deliberately excludes ``created_at``, ``author``, and ``confidence`` so a
    re-proposal of the same idea a second later collapses to the same
    fingerprint instead of forking the graph with a near-duplicate node.
    """
    normalized = "\x1f".join(
        [
            node_type.strip().lower(),
            title.strip().lower(),
            content.strip().lower(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fingerprint_index(data: dict[str, Any]) -> dict[str, str]:
    """Map ``content_fingerprint -> node_id`` for every stored node.

    Earlier-created nodes win a tie (stable, deterministic dedup target).
    """
    index: dict[str, str] = {}
    items = sorted(
        data["nodes"].items(),
        key=lambda item: (item[1].get("created_at", ""), item[0]),
    )
    for node_id, node in items:
        fp = content_fingerprint(node["node_type"], node["title"], node["content"])
        index.setdefault(fp, node_id)
    return index


def find_duplicate(
    store: GraphStore, *, node_type: str, title: str, content: str
) -> dict[str, Any]:
    """Look up whether an exact-fingerprint duplicate already exists.

    Returns ``{"duplicate": bool, "existing_id": str | None, "fingerprint": str}``.
    Exact-fingerprint matches are auto-deduped by :func:`add_node`; semantic
    near-duplicates are out of scope (true fuzzy dedup needs embeddings, which
    would force a model dependency) and are never auto-merged.
    """
    fp = content_fingerprint(node_type, title, content)
    data = store.read()
    existing = _fingerprint_index(data).get(fp)
    return {
        "duplicate": existing is not None,
        "existing_id": existing,
        "fingerprint": fp,
    }


# ---------------------------------------------------------------------------
# Provisional-write helpers (confidence cap + terminal-status guard)
# ---------------------------------------------------------------------------


def cap_confidence(
    confidence: float | None, *, cap: float = DEFAULT_MODEL_CONFIDENCE_CAP
) -> float | None:
    """Clamp a model-authored node's confidence to ``cap`` (default 0.5).

    A human can set any confidence; a model should not be able to assert high
    certainty on its own. ``None`` (unknown) passes through unchanged.
    """
    if confidence is None:
        return None
    return min(confidence, cap)


def guard_authored_status(status: str, *, settled: bool = False) -> str:
    """Refuse a model-authored terminal status unless the ruling settled.

    Front-ends marking a write as model-authored route the status here. A
    terminal status (``ratified``/``harvested``/``abandoned``) is only allowed
    when ``settled`` is True — i.e. it came out of :func:`rule` after a real
    judging step, not freehand from the model.
    """
    if status in TERMINAL_STATUSES and not settled:
        raise ValueError(
            f"a model may not author terminal status {status!r} directly; "
            "it must come from a settled ruling (run rule())"
        )
    return status


# ---------------------------------------------------------------------------
# 1:1 lift of every CLI action — returns data, never prints
# ---------------------------------------------------------------------------


def init(store: GraphStore) -> dict[str, Any]:
    """Initialize the local graph store (idempotent)."""
    store.init()
    return {"store": str(store.path), "initialized": True}


def bootstrap(store: GraphStore) -> dict[str, Any]:
    """Seed the store from the original concept conversation."""
    with graph_lock(store):
        ids = seed_concept_graph(store)
    return {"seeded": True, "ids": ids}


def home(store: GraphStore) -> dict[str, Any]:
    """Dashboard data for the current graph (counts + recent nodes)."""
    nodes = store.list_nodes()
    edges = store.list_edges()
    type_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for _, node in nodes:
        type_counts[node["node_type"]] = type_counts.get(node["node_type"], 0) + 1
        status_counts[node["status"]] = status_counts.get(node["status"], 0) + 1
    recent = [_node_view(node_id, node) for node_id, node in nodes[-5:]]
    return {
        "store": str(store.path),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "empty": not nodes,
        "type_counts": type_counts,
        "status_counts": status_counts,
        "recent": recent,
    }


def list_templates() -> list[dict[str, Any]]:
    """List structured branch templates as data."""
    out = []
    for name in sorted(BRANCH_TEMPLATES):
        t = BRANCH_TEMPLATES[name]
        out.append(
            {
                "name": t.name,
                "node_type": t.node_type,
                "relation": t.relation,
                "default_status": t.default_status,
                "description": t.description,
                "prompt_prefix": t.prompt_prefix,
            }
        )
    return out


def relations() -> list[dict[str, str]]:
    """Relation legend with a plain-English direction gloss for each relation."""
    return [
        {"relation": rel, "gloss": RELATION_GLOSS.get(rel, "")}
        for rel in VALID_EDGE_RELATIONS
    ]


def add_node(
    store: GraphStore,
    *,
    node_type: str,
    title: str,
    content: str,
    citations: list[str] | None = None,
    author: str = "local",
    confidence: float | None = 0.5,
    status: str = "active",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    link_to: str | None = None,
    relation: str | None = None,
    model_authored: bool = False,
    settled: bool = False,
) -> dict[str, Any]:
    """Add a typed node; optionally fuse a link to an existing node in one call.

    Returns the created node view plus ``created`` (False when an exact-
    fingerprint duplicate already existed — the existing node is returned
    instead of forking the graph). When ``link_to``/``relation`` are both given,
    also creates the edge and includes a ``link`` view.

    Front-ends mark a write ``model_authored=True`` to apply the confidence cap
    and the terminal-status guard. The new node is the *source* of the optional
    link (``from_id=new node``); the caller chooses the relation, so the fused
    link never guesses direction.
    """
    if (link_to is None) != (relation is None):
        raise ValueError(
            "--link-to and --relation must be given together (or neither)"
        )

    if model_authored:
        confidence = cap_confidence(confidence)
        status = guard_authored_status(status, settled=settled)

    citations = list(citations or [])
    tags = list(tags or [])
    metadata = dict(metadata or {})

    with graph_lock(store):
        dup = find_duplicate(
            store, node_type=node_type, title=title, content=content
        )
        if dup["duplicate"]:
            node_id = dup["existing_id"]
            node = store.get_node(node_id)
            created = False
        else:
            node = Node(
                node_type=node_type,
                title=title,
                content=content,
                citations=citations,
                author=author,
                confidence=confidence,
                status=status,
                tags=tags,
                metadata=metadata,
            )
            node_id = store.add_node(node)
            node = store.get_node(node_id)
            created = True

        result: dict[str, Any] = {
            "created": created,
            "fingerprint": dup["fingerprint"],
            **_node_view(node_id, node),
        }

        if link_to is not None and relation is not None:
            link = _add_edge_locked(
                store,
                from_ref=node_id,
                to_ref=link_to,
                relation=relation,
                resolve_from=False,
            )
            result["link"] = link
    return result


def _add_edge_locked(
    store: GraphStore,
    *,
    from_ref: str,
    to_ref: str,
    relation: str,
    note: str = "",
    metadata: dict[str, Any] | None = None,
    resolve_from: bool = True,
) -> dict[str, Any]:
    """Resolve prefixes, write an edge, return a confirmation view.

    Must be called inside :func:`graph_lock`. The confirmation carries the
    spoken-word link gloss so a front-end can echo
    ``from_type "from_title" --relation--> to_type "to_title"`` and catch
    backwards edges.
    """
    from_id = store.resolve_id(from_ref) if resolve_from else from_ref
    to_id = store.resolve_id(to_ref)
    edge = Edge(
        from_id=from_id,
        to_id=to_id,
        relation=relation,
        note=note,
        metadata=dict(metadata or {}),
    )
    edge_id = store.add_edge(edge)
    from_node = store.get_node(from_id)
    to_node = store.get_node(to_id)
    return {
        "edge_id": edge_id,
        "handle": _handle(edge_id),
        "relation": relation,
        "from_id": from_id,
        "to_id": to_id,
        "from_type": from_node["node_type"],
        "from_title": from_node["title"],
        "to_type": to_node["node_type"],
        "to_title": to_node["title"],
        "note": note,
    }


def add_edge(
    store: GraphStore,
    *,
    from_ref: str,
    to_ref: str,
    relation: str,
    note: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Link two nodes by id-or-prefix. Resolves both prefixes first."""
    with graph_lock(store):
        return _add_edge_locked(
            store,
            from_ref=from_ref,
            to_ref=to_ref,
            relation=relation,
            note=note,
            metadata=metadata,
        )


def add_branch(
    store: GraphStore,
    *,
    from_ref: str,
    template: str,
    title: str,
    content: str | None = None,
    citations: list[str] | None = None,
    author: str = "local",
    confidence: float = 0.5,
    tags: list[str] | None = None,
    model_authored: bool = False,
) -> dict[str, Any]:
    """Create a structured inquiry branch (one of the four debate moves).

    Wraps :func:`~sparkle.templates.build_branch_node` plus the two store writes
    (node, then the templated edge child --> parent), matching the existing CLI
    convention. Returns both the branch node view and the link view.
    """
    if model_authored:
        confidence = cap_confidence(confidence) or 0.0
    with graph_lock(store):
        parent_id = store.resolve_id(from_ref)
        parent = store.get_node(parent_id)
        branch_node, tmpl = build_branch_node(
            parent_title=parent["title"],
            template_name=template,
            title=title,
            content=content,
            citations=list(citations or []),
            author=author,
            confidence=confidence,
            extra_tags=list(tags or []),
        )
        branch_id = store.add_node(branch_node)
        link = _add_edge_locked(
            store,
            from_ref=branch_id,
            to_ref=parent_id,
            relation=tmpl.relation,
            note=tmpl.edge_note,
            resolve_from=False,
        )
        node = store.get_node(branch_id)
        return {
            "created": True,
            "template": tmpl.name,
            **_node_view(branch_id, node),
            "link": link,
        }


def get_node(store: GraphStore, ref: str) -> dict[str, Any]:
    """Resolve a node and return it with grouped inbound/outbound neighbors."""
    node_id = store.resolve_id(ref)
    node = store.get_node(node_id)
    neighbors = store.get_neighbor_details(node_id)
    view = _node_view(node_id, node)
    view["inbound"] = [
        {
            "edge_id": item["edge_id"],
            "relation": item["relation"],
            "note": item.get("note", ""),
            **_node_view(item["node_id"], item["node"]),
        }
        for item in neighbors["inbound"]
    ]
    view["outbound"] = [
        {
            "edge_id": item["edge_id"],
            "relation": item["relation"],
            "note": item.get("note", ""),
            **_node_view(item["node_id"], item["node"]),
        }
        for item in neighbors["outbound"]
    ]
    return view


def list_nodes(
    store: GraphStore,
    *,
    node_type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List nodes with optional filters, oldest-first (created_at order)."""
    nodes = store.list_nodes(
        node_type=node_type, status=status, tag=tag, query=query
    )
    if limit is not None:
        nodes = nodes[:limit]
    return [_node_view(node_id, node) for node_id, node in nodes]


def list_edges(store: GraphStore) -> list[dict[str, Any]]:
    """List all edges, created_at order."""
    return [_edge_view(edge_id, edge) for edge_id, edge in store.list_edges()]


def neighbors(store: GraphStore, ref: str) -> dict[str, list[dict[str, Any]]]:
    """Grouped inbound/outbound neighbor views for a node."""
    node_id = store.resolve_id(ref)
    details = store.get_neighbor_details(node_id)
    return {
        "node_id": node_id,
        "inbound": [
            {
                "edge_id": item["edge_id"],
                "relation": item["relation"],
                "note": item.get("note", ""),
                **_node_view(item["node_id"], item["node"]),
            }
            for item in details["inbound"]
        ],
        "outbound": [
            {
                "edge_id": item["edge_id"],
                "relation": item["relation"],
                "note": item.get("note", ""),
                **_node_view(item["node_id"], item["node"]),
            }
            for item in details["outbound"]
        ],
    }


def lineage(store: GraphStore, ref: str) -> list[dict[str, Any]]:
    """Inbound lineage walk for a node (the 'why' provenance chain)."""
    return [
        _node_view(node_id, node) for node_id, node in store.lineage(ref)
    ]


def subgraph(store: GraphStore, ref: str) -> dict[str, Any]:
    """The connected component around a node, as node + edge views."""
    nodes, edges = store.subgraph(ref)
    return {
        "root_id": store.resolve_id(ref),
        "nodes": [_node_view(node_id, node) for node_id, node in nodes],
        "edges": [_edge_view(edge["edge_id"], edge) for edge in edges],
    }


def export(store: GraphStore, ref: str) -> str:
    """Export the connected component around a node to a markdown string.

    Returns the rendered markdown; writing it to a file is the front-end's job.
    """
    return presentation.export_markdown(store, ref, None)


# ---------------------------------------------------------------------------
# 3. The referee / transition-rule engine (pure, non-persisted, display-only)
# ---------------------------------------------------------------------------


def edge_tally(store: GraphStore, ref: str) -> dict[str, Any]:
    """Live inbound-edge tally for a claim. Never touches stored confidence.

    Counts inbound edges by relation and flags whether an accepted decision
    (a node with an inbound ``evaluates`` edge, the judge's ruling) points at
    this claim. This is the raw signal the referee interprets.
    """
    node_id = store.resolve_id(ref)
    data = store.read()
    counts: dict[str, int] = {}
    has_decision = False
    for edge in data["edges"].values():
        if edge["to_id"] != node_id:
            continue
        counts[edge["relation"]] = counts.get(edge["relation"], 0) + 1
        src = data["nodes"].get(edge["from_id"])
        if (
            src is not None
            and edge["relation"] == "evaluates"
            and src["node_type"] == "decision"
        ):
            has_decision = True
    supports = counts.get("supports", 0)
    contradicts = counts.get("contradicts", 0)
    return {
        "node_id": node_id,
        "counts": counts,
        "supports": supports,
        "contradicts": contradicts,
        "net": supports - contradicts,
        "challenged": contradicts > 0,
        "has_decision": has_decision,
    }


def referee_signal(store: GraphStore, ref: str) -> dict[str, Any]:
    """Compute the LIVE display-only status signal for a claim.

    Pure and non-persisted: this reads the inbound edge tally and the node's
    own stored type/status through the playbook's transition rules, and reports
    a signal. It NEVER writes and NEVER implies the stored ``confidence``
    changed. The stored ``status`` is returned alongside so a front-end can show
    "stored X / live signal Y".
    """
    node_id = store.resolve_id(ref)
    node = store.get_node(node_id)
    tally = edge_tally(store, node_id)
    rules = _load_transition_rules(store)
    signal = _apply_transition_rules(rules, node, tally)
    return {
        "node_id": node_id,
        "handle": _handle(node_id),
        "node_type": node["node_type"],
        "title": node["title"],
        "stored_status": node["status"],
        "supports": tally["supports"],
        "contradicts": tally["contradicts"],
        "net": tally["net"],
        "challenged": tally["challenged"],
        "has_decision": tally["has_decision"],
        "live_signal": signal["signal"],
        "why": signal["why"],
    }


def _apply_transition_rules(
    rules: list[dict[str, Any]], node: dict[str, Any], tally: dict[str, Any]
) -> dict[str, str]:
    """Walk the playbook transition rules in order; first match wins.

    Each rule is data: ``{"when": <predicate-spec>, "signal": str, "why": str}``.
    Predicate keys understood: ``node_type``, ``stored_status``, ``has_decision``,
    and numeric comparisons over ``supports``/``contradicts``/``net``
    (``supports_min``, ``contradicts_min``, ``net_min``, ``net_max``,
    ``net_eq``, ``supports_eq``, ``contradicts_eq``).
    """
    facts = {
        "node_type": node["node_type"],
        "stored_status": node["status"],
        "supports": tally["supports"],
        "contradicts": tally["contradicts"],
        "net": tally["net"],
        "has_decision": tally["has_decision"],
    }
    for rule_def in rules:
        if _rule_matches(rule_def.get("when", {}), facts):
            return {"signal": rule_def["signal"], "why": rule_def.get("why", "")}
    return {"signal": "active", "why": "no transition rule matched"}


def _rule_matches(spec: dict[str, Any], facts: dict[str, Any]) -> bool:
    for key, expected in spec.items():
        if key == "node_type":
            if facts["node_type"] != expected:
                return False
        elif key == "stored_status":
            if facts["stored_status"] != expected:
                return False
        elif key == "has_decision":
            if facts["has_decision"] != expected:
                return False
        elif key == "supports_min":
            if facts["supports"] < expected:
                return False
        elif key == "supports_eq":
            if facts["supports"] != expected:
                return False
        elif key == "contradicts_min":
            if facts["contradicts"] < expected:
                return False
        elif key == "contradicts_eq":
            if facts["contradicts"] != expected:
                return False
        elif key == "net_min":
            if facts["net"] < expected:
                return False
        elif key == "net_max":
            if facts["net"] > expected:
                return False
        elif key == "net_eq":
            if facts["net"] != expected:
                return False
        else:  # unknown predicate key -> never matches (fail closed)
            return False
    return True


# ---------------------------------------------------------------------------
# 6. The built-in adversarial playbook + override loader
# ---------------------------------------------------------------------------

BUILTIN_PLAYBOOK: dict[str, Any] = {
    "name": "default-adversarial",
    "version": 1,
    "roles": {
        "proposer": {"node_type": "claim", "relation": None},
        "critic": {"node_type": "objection", "relation": "contradicts"},
        "evidence_gatherer": {"node_type": "evidence", "relation": "supports"},
        "judge": {"node_type": "decision", "relation": "evaluates"},
        "synthesizer": {"node_type": "synthesis", "relation": "produced"},
    },
    "phases": [
        {"name": "propose", "role": "proposer", "max_iterations": 1},
        {"name": "critique", "role": "critic", "max_iterations": 3},
        {"name": "gather", "role": "evidence_gatherer", "max_iterations": 3},
        {"name": "judge", "role": "judge", "max_iterations": 1},
        {"name": "synthesize", "role": "synthesizer", "max_iterations": 1},
    ],
    # Ordered; first match wins. Keyed on the live edge tally + node type/status,
    # NEVER on stored confidence (frozen nodes have no rollup).
    "transition_rules": [
        {
            "when": {"node_type": "claim", "has_decision": True},
            "signal": "ratified",
            "why": "a judge's decision evaluates this claim",
        },
        {
            "when": {"node_type": "claim", "stored_status": "ratified"},
            "signal": "ratified",
            "why": "stored terminal status from a settled ruling",
        },
        {
            "when": {"node_type": "claim", "stored_status": "stalled"},
            "signal": "stalled",
            "why": "explicitly parked; needs a new angle or a decision",
        },
        {
            "when": {"node_type": "claim", "supports_eq": 0, "contradicts_eq": 0},
            "signal": "unchallenged",
            "why": "no support and no objection recorded yet",
        },
        {
            "when": {"node_type": "claim", "supports_min": 1, "contradicts_min": 1},
            "signal": "ready_to_judge",
            "why": "both support and objection exist but no ruling yet",
        },
        {
            "when": {"node_type": "claim", "contradicts_min": 1, "net_max": -1},
            "signal": "weakly_supported",
            "why": "more objections than support",
        },
        {
            "when": {"node_type": "claim", "supports_min": 1, "contradicts_eq": 0},
            "signal": "supported",
            "why": "supported and never challenged",
        },
    ],
    "stop_conditions": [
        "judge ruled (an accepted decision evaluates the claim)",
        "critique rounds exhausted with no new objection",
        "stalled twice in a row -> abandoned",
    ],
}


def load_playbook(store: GraphStore, name: str | None = None) -> dict[str, Any]:
    """Load the adversarial playbook, preferring an override from disk.

    Looks in ``<store-dir>/playbooks/<name>.json`` (default name
    ``default-adversarial``). Falls back to :data:`BUILTIN_PLAYBOOK` when no
    override file exists. A malformed override raises ``ValueError``.
    """
    target = name or BUILTIN_PLAYBOOK["name"]
    playbook_path = store.path.parent / "playbooks" / f"{target}.json"
    if playbook_path.exists():
        try:
            loaded = json.loads(playbook_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt playbook at {playbook_path}: {exc}") from exc
        if not isinstance(loaded, dict) or "transition_rules" not in loaded:
            raise ValueError(
                f"Invalid playbook at {playbook_path}: missing 'transition_rules'"
            )
        return loaded
    return BUILTIN_PLAYBOOK


def _load_transition_rules(store: GraphStore) -> list[dict[str, Any]]:
    return list(load_playbook(store).get("transition_rules", []))


# ---------------------------------------------------------------------------
# 2. Invariants — the judge's "rule" move (lives here, not in any front-end)
# ---------------------------------------------------------------------------


def rule(
    store: GraphStore,
    claim_ref: str,
    *,
    verdict: str,
    rationale: str = "",
    settle: bool = False,
    author: str = "local",
    confidence: float | None = 0.8,
) -> dict[str, Any]:
    """Record a judge's ruling on a claim — the one move with a hard invariant.

    INVARIANT: you may not ratify a claim the adversary never attacked. If the
    claim has no inbound ``objection``/``contradicts`` edge, this raises
    ``ValueError`` telling the caller to run a challenge first. This is the
    single most important guardrail and it lives here so the CLI, the MCP
    server, and any future harness all inherit it through the same boundary.

    On a legal ruling it writes a ``decision`` node and links it to the claim
    with an ``evaluates`` edge (judge convention). When ``settle=True`` it also
    writes a SUPERSEDING claim version carrying ``status="ratified"`` (never an
    in-place mutation; nodes are frozen) and links new --supersedes--> old.

    Returns the decision view and, when settled, the superseding claim view.
    """
    with graph_lock(store):
        claim_id = store.resolve_id(claim_ref)
        claim = store.get_node(claim_id)
        if claim["node_type"] != "claim":
            raise ValueError(
                f"rule() targets a claim; {claim_id[:12]} is a {claim['node_type']}"
            )

        tally = edge_tally(store, claim_id)
        if not tally["challenged"]:
            raise ValueError(
                "you may not ratify a claim the adversary never attacked; "
                "run a challenge first (add an objection/contradicts edge)"
            )

        decision = Node(
            node_type="decision",
            title=f"Ruling: {verdict}",
            content=rationale or verdict,
            author=author,
            confidence=confidence,
            status="active",
            tags=["ruling"],
            metadata={"verdict": verdict, "claim_id": claim_id},
        )
        decision_id = store.add_node(decision)
        decision_link = _add_edge_locked(
            store,
            from_ref=decision_id,
            to_ref=claim_id,
            relation="evaluates",
            note="judge ruling",
            resolve_from=False,
        )

        result: dict[str, Any] = {
            "settled": bool(settle),
            "decision": {**_node_view(decision_id, store.get_node(decision_id))},
            "decision_link": decision_link,
        }

        if settle:
            ratified = _supersede_node(
                store,
                old_id=claim_id,
                changes={
                    "status": "ratified",
                    "metadata": {**claim.get("metadata", {}), "ratified_by": decision_id},
                },
                rehome_inbound=False,
                note="ratified ruling",
            )
            # Point the decision at the ratified version too, so the frontier's
            # "judged" check sees a decision on the live (ratified) node.
            _add_edge_locked(
                store,
                from_ref=decision_id,
                to_ref=ratified["node_id"],
                relation="evaluates",
                note="judge ruling (ratified version)",
                resolve_from=False,
            )
            result["ratified_claim"] = _node_view(
                ratified["node_id"], store.get_node(ratified["node_id"])
            )
        return result


def _supersede_node(
    store: GraphStore,
    *,
    old_id: str,
    changes: dict[str, Any],
    rehome_inbound: bool,
    note: str = "",
) -> dict[str, Any]:
    """Write a corrected copy of a node and link new --supersedes--> old.

    Frozen-model honest editing: never mutates ``old_id``. ``changes`` are
    applied on top of the old node's fields to build the new immutable node.
    When ``rehome_inbound`` is True, every inbound edge of the old node is
    re-created pointing at the new node (its outbound edges already follow via
    lineage through the supersedes link). Must run inside :func:`graph_lock`.

    Returns ``{"node_id", "supersedes_edge", "rehomed": [...]}``.
    """
    old = store.get_node(old_id)
    base = {
        k: v
        for k, v in old.items()
        if k in {"node_type", "title", "content", "citations", "author", "confidence", "status", "tags", "metadata"}
    }
    base.update(changes)
    new_node = Node(
        node_type=base["node_type"],
        title=base["title"],
        content=base["content"],
        citations=list(base.get("citations", [])),
        author=base.get("author", "local"),
        confidence=base.get("confidence"),
        status=base.get("status", "active"),
        tags=list(base.get("tags", [])),
        metadata=dict(base.get("metadata", {})),
    )
    new_id = store.add_node(new_node)
    supersedes_edge = _add_edge_locked(
        store,
        from_ref=new_id,
        to_ref=old_id,
        relation="supersedes",
        note=note,
        resolve_from=False,
    )
    rehomed: list[dict[str, Any]] = []
    if rehome_inbound:
        details = store.get_neighbor_details(old_id)
        for item in details["inbound"]:
            if item["node_id"] == new_id:
                continue  # don't rehome the supersedes edge onto itself
            rehomed.append(
                _add_edge_locked(
                    store,
                    from_ref=item["node_id"],
                    to_ref=new_id,
                    relation=item["relation"],
                    note=item.get("note", ""),
                    resolve_from=False,
                )
            )
    return {
        "node_id": new_id,
        "supersedes_edge": supersedes_edge,
        "rehomed": rehomed,
    }


def revise(
    store: GraphStore,
    ref: str,
    *,
    title: str | None = None,
    content: str | None = None,
    confidence: float | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    rehome_edges: bool = True,
) -> dict[str, Any]:
    """Supersede a node with a corrected version, re-homing its inbound edges.

    Model-honest editing that never mutates the original frozen node. Only the
    provided fields change; the rest carry over. ``rehome_edges`` controls
    whether inbound edges are re-pointed at the new node (the re-home-all vs.
    leave-in-place product choice the front-end surfaces to the user).
    """
    changes: dict[str, Any] = {}
    if title is not None:
        changes["title"] = title
    if content is not None:
        changes["content"] = content
    if confidence is not None:
        changes["confidence"] = confidence
    if status is not None:
        changes["status"] = status
    if tags is not None:
        changes["tags"] = list(tags)
    if not changes:
        raise ValueError("revise() needs at least one field to change")

    with graph_lock(store):
        old_id = store.resolve_id(ref)
        result = _supersede_node(
            store,
            old_id=old_id,
            changes=changes,
            rehome_inbound=rehome_edges,
            note="revision",
        )
        return {
            "old_id": old_id,
            "rehomed_edges": rehome_edges,
            **_node_view(result["node_id"], store.get_node(result["node_id"])),
            "supersedes_edge": result["supersedes_edge"],
            "rehomed": result["rehomed"],
        }


# ---------------------------------------------------------------------------
# 5. frontier(store) — the work-frontier buckets (live tally, not confidence)
# ---------------------------------------------------------------------------

# Bucket names, fixed so front-ends and the MCP layer agree.
FRONTIER_BUCKETS = (
    "UNCHALLENGED",
    "OPEN_QUESTIONS",
    "STALLED",
    "THIN_EVIDENCE",
    "READY_TO_JUDGE",
)


def _frontier_entries(store: GraphStore) -> list[dict[str, Any]]:
    """Compute every frontier entry, keyed on edge tally + node type/status.

    Never keys a bucket on stored confidence alone (frozen confidence never
    rolls up). A judged claim (an accepted decision evaluates it, or it is
    stored ``ratified``) is closed and never appears on the frontier.
    """
    data = store.read()
    # Build an inbound tally per node in one pass.
    supports: dict[str, int] = {}
    contradicts: dict[str, int] = {}
    answered: dict[str, int] = {}  # questions that have an inbound answer
    decided: set[str] = set()
    for edge in data["edges"].values():
        to_id = edge["to_id"]
        rel = edge["relation"]
        if to_id not in data["nodes"]:
            continue
        if rel == "supports":
            supports[to_id] = supports.get(to_id, 0) + 1
        elif rel == "contradicts":
            contradicts[to_id] = contradicts.get(to_id, 0) + 1
        if rel in ("supports", "refines", "produced", "derived_from"):
            answered[to_id] = answered.get(to_id, 0) + 1
        src = data["nodes"].get(edge["from_id"])
        if rel == "evaluates" and src is not None and src["node_type"] == "decision":
            decided.add(to_id)

    entries: list[dict[str, Any]] = []
    nodes = sorted(
        data["nodes"].items(),
        key=lambda item: (item[1].get("created_at", ""), item[0]),
    )
    for node_id, node in nodes:
        node_type = node["node_type"]
        status = node["status"]
        sup = supports.get(node_id, 0)
        con = contradicts.get(node_id, 0)

        # A judged/closed claim is done — never on the frontier.
        if node_id in decided or status in ("ratified", "harvested", "abandoned"):
            continue

        bucket: str | None = None
        why = ""
        prompt = ""

        if node_type == "claim":
            if sup >= 1 and con >= 1:
                bucket = "READY_TO_JUDGE"
                why = "has both support and objection but no ruling yet"
                prompt = "/synthesize"
            elif con == 0 and status not in ("stalled",):
                bucket = "UNCHALLENGED"
                why = "no objection has been raised against this claim"
                prompt = "/challenge"
            elif sup == 0 and con >= 1:
                bucket = "THIN_EVIDENCE"
                why = "challenged but no supporting evidence gathered yet"
                prompt = "/investigate"
        elif node_type == "question":
            if answered.get(node_id, 0) == 0:
                bucket = "OPEN_QUESTIONS"
                why = "no answer, evidence, or refinement attached yet"
                prompt = "/investigate"

        # Stalled threads (any type explicitly parked) surface for unsticking.
        if bucket is None and status == "stalled":
            bucket = "STALLED"
            why = "explicitly stalled; needs a new angle or a decision"
            prompt = "/next"

        if bucket is None:
            continue

        entries.append(
            {
                "node_id": node_id,
                "node_type": node_type,
                "title": node["title"],
                "status": status,
                "why_listed": why,
                "suggested_prompt": prompt,
                "_bucket": bucket,
                # live, display-only aggregates
                "supports": sup,
                "contradicts": con,
                "net": sup - con,
            }
        )
    return entries


# Stable priority order: judge-ready first, then unstick, then attack/answer.
_BUCKET_PRIORITY = {
    "READY_TO_JUDGE": 0,
    "STALLED": 1,
    "UNCHALLENGED": 2,
    "THIN_EVIDENCE": 3,
    "OPEN_QUESTIONS": 4,
}


def frontier(
    store: GraphStore,
    *,
    top: int | None = None,
    cursor: int = 0,
    bucket: str | None = None,
) -> dict[str, Any]:
    """The prioritized 'what needs adversarial attention next' feed.

    Pure read: no model, no writes. Buckets (see :data:`FRONTIER_BUCKETS`) are
    keyed on the live inbound edge tally and node type/status, never on stored
    confidence. Supports top-N + cursor paging: pass ``top`` to cap the page and
    ``cursor`` to offset into the ranked list; the returned ``next_cursor`` is
    ``None`` when the list is exhausted.

    Each entry: ``{node_id, node_type, title, status, why_listed,
    suggested_prompt}`` plus live ``supports``/``contradicts``/``net`` aggregates
    and its ``bucket``.
    """
    if bucket is not None and bucket not in FRONTIER_BUCKETS:
        raise ValueError(
            f"unknown frontier bucket: {bucket}; valid: {', '.join(FRONTIER_BUCKETS)}"
        )
    if cursor < 0:
        raise ValueError("cursor must be >= 0")

    entries = _frontier_entries(store)
    if bucket is not None:
        entries = [e for e in entries if e["_bucket"] == bucket]

    entries.sort(
        key=lambda e: (_BUCKET_PRIORITY.get(e["_bucket"], 99), e["title"])
    )

    total = len(entries)
    window = entries[cursor:]
    if top is not None:
        if top < 0:
            raise ValueError("top must be >= 0")
        window = window[:top]

    next_cursor: int | None = cursor + len(window)
    if next_cursor is not None and next_cursor >= total:
        next_cursor = None

    # Re-key "_bucket" -> "bucket" for the public view.
    public = []
    for e in window:
        item = {k: v for k, v in e.items() if k != "_bucket"}
        item["bucket"] = e["_bucket"]
        public.append(item)

    by_bucket: dict[str, int] = {}
    for e in entries:
        by_bucket[e["_bucket"]] = by_bucket.get(e["_bucket"], 0) + 1

    return {
        "total": total,
        "returned": len(public),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "counts_by_bucket": by_bucket,
        "entries": public,
    }


# ---------------------------------------------------------------------------
# 7. Alias sidecar over .sparkle/aliases.json (never in any hashed payload)
# ---------------------------------------------------------------------------


def _aliases_path(store: GraphStore) -> Path:
    return store.path.with_name("aliases.json")


def read_aliases(store: GraphStore) -> dict[str, str]:
    """Read the ``name -> node_id`` alias map sitting beside the store."""
    path = _aliases_path(store)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt alias file at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid alias file at {path}")
    return {str(k): str(v) for k, v in data.items()}


def set_alias(store: GraphStore, name: str, ref: str) -> dict[str, Any]:
    """Point an alias ``name`` at a node (re-pointing if it already exists).

    Re-pointing on reuse is intentional: when a name is reused after a node is
    superseded, the name follows to the new node so it survives supersession.
    The alias lives only in the sidecar file, never in the hashed node payload.
    """
    if not name or not name.strip():
        raise ValueError("alias name cannot be empty")
    name = name.strip()
    with graph_lock(store):
        node_id = store.resolve_id(ref)
        aliases = read_aliases(store)
        repointed = name in aliases and aliases[name] != node_id
        aliases[name] = node_id
        path = _aliases_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(aliases, indent=2, sort_keys=True), encoding="utf-8"
        )
        import os

        os.replace(tmp, path)
    return {
        "name": name,
        "node_id": node_id,
        "handle": _handle(node_id),
        "repointed": repointed,
    }


def resolve_alias(store: GraphStore, ref: str) -> str:
    """Resolve a ref that may be an alias name OR a node id/prefix to a node id.

    Aliases are tried first (exact name match in the sidecar); otherwise the ref
    falls through to the store's prefix resolver. Lets a front-end accept a
    human-chosen name anywhere an id is accepted.
    """
    aliases = read_aliases(store)
    if ref in aliases:
        # Validate the alias still points at a live node.
        return store.resolve_id(aliases[ref])
    return store.resolve_id(ref)


# ---------------------------------------------------------------------------
# 6 (cont). JSON import — the LLM/batch on-ramp (two-pass build via ops)
# ---------------------------------------------------------------------------


def import_graph(store: GraphStore, document: dict[str, Any]) -> dict[str, Any]:
    """Two-pass build of a graph fragment from a JSON document.

    The document shape::

        {
          "nodes": [
            {"ref": "c1", "node_type": "claim", "title": "...", "content": "...",
             "confidence": 0.7, "status": "active", "tags": [...],
             "citations": [...], "created_at": "..."}   # created_at optional
          ],
          "edges": [
            {"from": "c1", "to": "c2", "relation": "supports", "note": "..."}
          ]
        }

    Pass 1 creates nodes (running the dedup gate, so an existing node is reused
    rather than forked) and builds a ``ref -> node_id`` nickname map. Pass 2
    creates edges, resolving each endpoint through that map first, then falling
    back to a node id/prefix. Returns ``{"nicknames": {...}, "created_nodes":
    int, "created_edges": int}``.

    An optional ``created_at`` per node enables deterministic re-import (the
    timestamp is part of the node id).
    """
    if not isinstance(document, dict):
        raise ValueError("import document must be a JSON object")
    raw_nodes = document.get("nodes", [])
    raw_edges = document.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("import document 'nodes' and 'edges' must be arrays")

    nicknames: dict[str, str] = {}
    created_nodes = 0
    created_edges = 0

    with graph_lock(store):
        for spec in raw_nodes:
            if not isinstance(spec, dict):
                raise ValueError("each node entry must be an object")
            for required in ("node_type", "title", "content"):
                if required not in spec:
                    raise ValueError(
                        f"node entry missing required field: {required!r}"
                    )
            node_type = spec["node_type"]
            title = spec["title"]
            content = spec["content"]
            dup = find_duplicate(
                store, node_type=node_type, title=title, content=content
            )
            if dup["duplicate"]:
                node_id = dup["existing_id"]
            else:
                kwargs: dict[str, Any] = {
                    "node_type": node_type,
                    "title": title,
                    "content": content,
                    "citations": list(spec.get("citations", [])),
                    "author": spec.get("author", "local"),
                    "confidence": spec.get("confidence", 0.5),
                    "status": spec.get("status", "active"),
                    "tags": list(spec.get("tags", [])),
                    "metadata": dict(spec.get("metadata", {})),
                }
                if "created_at" in spec:
                    kwargs["created_at"] = spec["created_at"]
                node = Node(**kwargs)
                node_id = store.add_node(node)
                created_nodes += 1
            ref = spec.get("ref")
            if ref is not None:
                nicknames[str(ref)] = node_id

        for spec in raw_edges:
            if not isinstance(spec, dict):
                raise ValueError("each edge entry must be an object")
            for required in ("from", "to", "relation"):
                if required not in spec:
                    raise ValueError(
                        f"edge entry missing required field: {required!r}"
                    )
            from_ref = str(spec["from"])
            to_ref = str(spec["to"])
            from_id = nicknames.get(from_ref) or store.resolve_id(from_ref)
            to_id = nicknames.get(to_ref) or store.resolve_id(to_ref)
            _add_edge_locked(
                store,
                from_ref=from_id,
                to_ref=to_id,
                relation=spec["relation"],
                note=spec.get("note", ""),
                resolve_from=False,
            )
            created_edges += 1

    return {
        "nicknames": nicknames,
        "created_nodes": created_nodes,
        "created_edges": created_edges,
    }


# ---------------------------------------------------------------------------
# Phase 2 seam (intentionally NOT implemented here)
# ---------------------------------------------------------------------------
# The autonomous harness (`sparkle[agents]` extra, `src/sparkle/harness.py`)
# will call THESE functions directly — not through the CLI or MCP — and walk the
# playbook above filling each role's text via its own thinking backend, while
# committing through `add_branch`/`rule`/`add_node` and reading state through
# `frontier`/`referee_signal`. Because the invariants and the referee live here,
# the harness inherits every guardrail by construction. No harness code, no
# model/LLM import, and no `sparkle_run_loop` belongs in this file. This comment
# is the seam; that is all Phase 2 gets for now.
