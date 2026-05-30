"""Sparkle MCP server — a thin FastMCP adapter over :mod:`sparkle.ops`.

This module is the second front-end on the seam (see
``discussions/sparkle-agent-architecture.md``). Like the CLI, it is a *thin
wrapper*: every tool and every read bottoms out in an ``ops.py`` function (or a
small composition of them), so the debate invariants, the referee/transition
engine, and the content-dedup gate are inherited for free through the single
``ValueError`` boundary. **No graph logic is reimplemented here.** A mutation
that ``ops.py`` would refuse (e.g. ruling a claim the adversary never attacked)
is refused here too, because the refusal lives in ``ops.rule``, not in this file.

What this layer adds on top of ``ops.py`` (and nothing more):

- **Provisional-write policy on model-authored writes.** Mutation tools stamp
  ``{"provisional": True, "run_id": ..., "agent_role": ...}`` into the node's
  metadata and pass ``model_authored=True`` so ``ops.py`` applies the confidence
  cap and refuses a model-authored terminal status. The dedup gate runs inside
  ``ops.add_node`` before any write, so a re-proposal a second later returns the
  existing node instead of forking the graph.
- **Run tagging + region review.** Each tool call carries a ``run_id`` (the
  client supplies one per session/loop). The run-region reads
  (:func:`sparkle_run_summary`, :func:`sparkle_run_diff`) and the region
  mutations (:func:`sparkle_ratify_region`, :func:`sparkle_rollback_run`) are
  pure compositions of ``ops`` reads/writes over that ``run_id`` metadata stamp.
- **Reads exposed as BOTH resources and identically-named tools**, so a client
  that lacks resource support degrades gracefully to tool calls. The frontier,
  node, lineage, and subgraph reads each appear twice.
- **Prompts** are the adversarial playbook as slash commands. They embed the
  node-type/relation vocabulary inline and call **no model themselves** — the
  host's model executes them.
- **A ``listChanged`` notification after every mutation**, so a client refreshes
  its resource list once the graph changed.

Dependency boundary: the MCP SDK is imported at module top. That is safe because
this module is imported *lazily* by ``cli.py`` (only on the ``sparkle mcp``
subcommand), so the zero-dependency core never pays for it. A missing
``sparkle[mcp]`` extra surfaces as the clear "pip install" message the CLI
prints on the same ``ValueError``-style boundary.

Phase scope: 0, 1a, 1b, 1c. The autonomous harness (Phase 2, ``sparkle[agents]``,
``harness.py``) is intentionally NOT built here — see the seam marker at the
bottom of this file. No model-calling code lives in this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import ops
from .graph import GraphStore
from .harness import JUDGE_AFFIRMING_VERDICT


# ---------------------------------------------------------------------------
# Graph path resolution + a process-wide store handle
# ---------------------------------------------------------------------------

# The store is resolved once in run_server() and read by every tool/resource.
# "One front-end at a time per graph" is the MVP concurrency contract; the
# advisory file lock inside ops.graph_lock() guards each read-modify-write.
_STORE: GraphStore | None = None


def _resolve_graph_path(explicit: str | Path | None = None) -> Path:
    """Resolve the graph store path for the server.

    Order (first hit wins):
      1. an explicit path passed in (the CLI hands ``args.store`` here),
      2. the ``SPARKLE_GRAPH`` environment variable,
      3. the client's project dir (``MCP_PROJECT_DIR`` / ``PWD``) + ``.sparkle/graph.json``,
      4. the current working directory + ``.sparkle/graph.json``.
    """
    if explicit is not None and str(explicit) not in ("", "."):
        # The CLI always passes its --store default (.sparkle/graph.json). Treat
        # that bare default as "not explicit" so the env var can still win.
        if str(explicit) != str(Path(".sparkle/graph.json")):
            return Path(explicit)
    env = os.environ.get("SPARKLE_GRAPH")
    if env:
        return Path(env)
    project_dir = os.environ.get("MCP_PROJECT_DIR") or os.environ.get("PWD")
    if project_dir:
        return Path(project_dir) / ".sparkle" / "graph.json"
    return Path.cwd() / ".sparkle" / "graph.json"


def _store() -> GraphStore:
    if _STORE is None:  # pragma: no cover - guarded by run_server()
        raise ValueError("MCP server store is not initialized; call run_server()")
    return _STORE


# ---------------------------------------------------------------------------
# Provisional-write metadata stamp (model-authored policy lives in ops.py)
# ---------------------------------------------------------------------------


def _provisional_metadata(
    run_id: str,
    agent_role: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stamp the model-authored provenance into a node's metadata.

    ``provisional`` marks the node as model-written and not yet human-ratified;
    ``run_id`` ties it to the session/loop so a region can be reviewed,
    ratified, or rolled back; ``agent_role`` records which playbook role wrote
    it. The confidence cap and the terminal-status guard are NOT applied here —
    they live in ``ops.py`` and fire because these tools pass
    ``model_authored=True``.
    """
    meta = {"provisional": True, "run_id": run_id, "agent_role": agent_role}
    if extra:
        meta.update(extra)
    return meta


# The node-type and relation vocabulary, rendered inline into every prompt so
# the host's model writes well-typed nodes without re-explaining the schema.
def _vocabulary_block() -> str:
    types = ", ".join(ops.VALID_NODE_TYPES)
    rels = "\n".join(
        f"  - {item['relation']}: {item['gloss']}" for item in ops.relations()
    )
    return (
        f"Node types you may create: {types}.\n"
        f"Edge relations (arrow reads from --relation--> to):\n{rels}\n"
        "Edge convention: a branch child points at its parent "
        "(from_id = the new node, to_id = the node it reacts to).\n"
        "Use explicit short id prefixes for every reference; the '@last' token "
        "is forbidden because whole-second timestamps have no tiebreaker."
    )


# ---------------------------------------------------------------------------
# Build the FastMCP app and register the whole surface
# ---------------------------------------------------------------------------


def build_app() -> FastMCP:
    """Construct the FastMCP app with every tool, resource, and prompt.

    Kept separate from :func:`run_server` so it can be built and introspected
    without opening a stdio transport (handy for tests and tool listing).
    """
    mcp = FastMCP("sparkle")

    async def _notify_changed(ctx: Context | None) -> None:
        """Emit a resource listChanged after a mutation, best-effort."""
        if ctx is None:
            return
        try:
            await ctx.session.send_resource_list_changed()
        except Exception:  # pragma: no cover - transport edge; never fail a write
            pass

    # -------------------------------------------------------------------
    # TOOLS (mutations) — thin wrappers; provisional stamp + model cap via ops
    # -------------------------------------------------------------------

    @mcp.tool()
    async def sparkle_add_node(
        node_type: str,
        title: str,
        content: str,
        run_id: str,
        agent_role: str = "proposer",
        confidence: float | None = 0.5,
        status: str = "active",
        citations: list[str] | None = None,
        tags: list[str] | None = None,
        link_to: str | None = None,
        relation: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a typed, model-authored node (optionally fused with one link).

        Passes ``model_authored=True`` so ops.py caps the confidence and refuses
        a terminal status from the model. The dedup gate inside ``ops.add_node``
        runs before any write: a re-proposal of the same idea returns the
        existing node with ``created=False`` instead of forking the graph. When
        ``link_to``/``relation`` are both given, the new node is the *source* of
        the edge. Use explicit short id prefixes for ``link_to`` (no '@last').
        """
        result = ops.add_node(
            _store(),
            node_type=node_type,
            title=title,
            content=content,
            citations=citations,
            author=agent_role,
            confidence=confidence,
            status=status,
            tags=tags,
            metadata=_provisional_metadata(run_id, agent_role),
            link_to=link_to,
            relation=relation,
            run_id=run_id,
            model_authored=True,
        )
        await _notify_changed(ctx)
        return result

    @mcp.tool()
    async def sparkle_link(
        from_ref: str,
        to_ref: str,
        relation: str,
        run_id: str,
        agent_role: str = "proposer",
        note: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Link two existing nodes by id-or-prefix (arrow: from --relation--> to).

        Thin wrapper over ``ops.add_edge``; both endpoints are resolved by the
        store's prefix resolver. The run/role stamp lands in the edge metadata so
        the link is rollback-able with the rest of the run.
        """
        result = ops.add_edge(
            _store(),
            from_ref=from_ref,
            to_ref=to_ref,
            relation=relation,
            note=note,
            metadata=_provisional_metadata(run_id, agent_role),
        )
        await _notify_changed(ctx)
        return result

    @mcp.tool()
    async def sparkle_branch(
        from_ref: str,
        template: str,
        title: str,
        run_id: str,
        content: str | None = None,
        agent_role: str = "critic",
        confidence: float = 0.5,
        citations: list[str] | None = None,
        tags: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run one of the four debate moves (support/object/reframe/apply).

        Thin wrapper over ``ops.add_branch`` with ``model_authored=True`` so the
        model-confidence cap applies. The branch child is created pointing at its
        parent following the existing convention; the templated relation is fixed
        by the template, never guessed. Run ``list-templates`` (or the templates
        resource) for the available moves.
        """
        result = ops.add_branch(
            _store(),
            from_ref=from_ref,
            template=template,
            title=title,
            content=content,
            citations=citations,
            author=agent_role,
            confidence=confidence,
            tags=tags,
            run_id=run_id,
            model_authored=True,
        )
        await _notify_changed(ctx)
        return result

    @mcp.tool()
    async def sparkle_rule(
        claim_ref: str,
        verdict: str,
        run_id: str,
        rationale: str = "",
        settle: bool = False,
        confidence: float | None = 0.8,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """The judge's ruling — carries the one hard debate invariant.

        Thin wrapper over ``ops.rule``. The invariant lives in ``ops.py``: you may
        NOT ratify a claim the adversary never attacked, so this raises a
        ``ValueError`` (surfaced as a tool error) if the claim has no inbound
        objection/contradicts edge — run ``/challenge`` first. On a legal ruling
        it writes a ``decision`` node; with ``settle=True`` it also writes a
        SUPERSEDING claim version carrying status ``ratified`` (never an in-place
        mutation — nodes are frozen).

        ``require_distinct_adversary=True``: the MCP server is an AGENT-driven
        front-end (autonomous operation), so it gets the same author-distinct
        floor as the autonomous harness — a claim only counts as challenged if an
        objection comes from a DIFFERENT author than the claim, so a single agent
        cannot ratify its own claim off a self-written strawman. (The trusted
        human ``sparkle rule`` CLI path leaves this off.)
        """
        result = ops.rule(
            _store(),
            claim_ref,
            verdict=verdict,
            rationale=rationale,
            settle=settle,
            author="judge",
            confidence=confidence,
            run_id=run_id,
            require_distinct_adversary=True,
            affirming_verdicts=frozenset({JUDGE_AFFIRMING_VERDICT}),
        )
        await _notify_changed(ctx)
        return result

    @mcp.tool()
    async def sparkle_harvest(
        from_ref: str,
        title: str,
        content: str,
        run_id: str,
        confidence: float | None = 0.5,
        citations: list[str] | None = None,
        tags: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """The closing 'synthesize/harvest' move: write a synthesis off a node.

        Composition of two ``ops`` writes and nothing more: it creates a
        model-authored ``synthesis`` node (dedup + confidence cap via
        ``ops.add_node``) and links it ``produced`` to the source node
        (``synthesis --produced--> source``), matching the synthesizer role in
        the built-in playbook. No graph logic is reimplemented — both writes
        bottom out in ``ops``.
        """
        result = ops.add_node(
            _store(),
            node_type="synthesis",
            title=title,
            content=content,
            citations=citations,
            author="synthesizer",
            confidence=confidence,
            status="active",
            tags=tags,
            metadata=_provisional_metadata(run_id, "synthesizer"),
            link_to=from_ref,
            relation="produced",
            run_id=run_id,
            model_authored=True,
        )
        await _notify_changed(ctx)
        return result

    # -------------------------------------------------------------------
    # READS as BOTH resources and identically-named tools (graceful degrade)
    # -------------------------------------------------------------------

    # --- frontier: the headline work feed ---

    def _frontier(top: int | None, cursor: int, bucket: str | None) -> dict[str, Any]:
        return ops.frontier(_store(), top=top, cursor=cursor, bucket=bucket)

    @mcp.resource("sparkle://frontier")
    def frontier_resource() -> dict[str, Any]:
        """Prioritized 'what needs adversarial attention next' feed (first page).

        Live read keyed on the inbound edge tally and node type/status, never on
        stored confidence. Use the ``sparkle_frontier`` tool for paging or to
        filter by bucket.
        """
        return _frontier(top=20, cursor=0, bucket=None)

    @mcp.tool()
    def sparkle_frontier(
        top: int | None = 20,
        cursor: int = 0,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        """Frontier feed as a tool, with paging (``top`` + ``cursor``) + bucket filter.

        Tool mirror of the ``sparkle://frontier`` resource so a client without
        resource support still gets the feed. Buckets: READY_TO_JUDGE, STALLED,
        UNCHALLENGED, THIN_EVIDENCE, OPEN_QUESTIONS. ``next_cursor`` is null when
        the list is exhausted.
        """
        return _frontier(top=top, cursor=cursor, bucket=bucket)

    # --- node ---

    @mcp.resource("sparkle://node/{ref}")
    def node_resource(ref: str) -> dict[str, Any]:
        """A node with its grouped inbound/outbound neighbors. ``ref`` is an
        id, a short prefix, or an alias name."""
        return ops.get_node(_store(), ops.resolve_alias(_store(), ref))

    @mcp.tool()
    def sparkle_node(ref: str) -> dict[str, Any]:
        """A node + neighbors as a tool (mirror of ``sparkle://node/{ref}``)."""
        return ops.get_node(_store(), ops.resolve_alias(_store(), ref))

    # --- lineage (the 'why' provenance chain) ---

    @mcp.resource("sparkle://lineage/{ref}")
    def lineage_resource(ref: str) -> list[dict[str, Any]]:
        """Inbound lineage walk for a node — its provenance chain."""
        return ops.lineage(_store(), ops.resolve_alias(_store(), ref))

    @mcp.tool()
    def sparkle_lineage(ref: str) -> list[dict[str, Any]]:
        """Lineage as a tool (mirror of ``sparkle://lineage/{ref}``)."""
        return ops.lineage(_store(), ops.resolve_alias(_store(), ref))

    # --- subgraph (connected component) ---

    @mcp.resource("sparkle://subgraph/{ref}")
    def subgraph_resource(ref: str) -> dict[str, Any]:
        """The connected component around a node (nodes + edges)."""
        return ops.subgraph(_store(), ops.resolve_alias(_store(), ref))

    @mcp.tool()
    def sparkle_subgraph(ref: str) -> dict[str, Any]:
        """Subgraph as a tool (mirror of ``sparkle://subgraph/{ref}``)."""
        return ops.subgraph(_store(), ops.resolve_alias(_store(), ref))

    # --- supporting reads (context the prompts and host model need) ---

    @mcp.resource("sparkle://relations")
    def relations_resource() -> list[dict[str, str]]:
        """The edge-relation legend with a direction gloss for each relation."""
        return ops.relations()

    @mcp.tool()
    def sparkle_relations() -> list[dict[str, str]]:
        """Relation legend as a tool (mirror of ``sparkle://relations``)."""
        return ops.relations()

    @mcp.resource("sparkle://templates")
    def templates_resource() -> list[dict[str, Any]]:
        """The structured branch templates (the four debate moves)."""
        return ops.list_templates()

    @mcp.tool()
    def sparkle_templates() -> list[dict[str, Any]]:
        """Branch templates as a tool (mirror of ``sparkle://templates``)."""
        return ops.list_templates()

    @mcp.resource("sparkle://playbook")
    def playbook_resource() -> dict[str, Any]:
        """The adversarial playbook (roles, phases, transition rules, stops).

        Data, not code: the same playbook the referee in ``ops.py`` interprets.
        A power user can drop an override beside the store; this returns whatever
        is active.
        """
        return ops.load_playbook(_store())

    @mcp.tool()
    def sparkle_signal(ref: str) -> dict[str, Any]:
        """Live display-only status signal for a claim (mirror of the referee).

        Reads the inbound edge tally through the playbook transition rules and
        reports a live signal alongside the stored status. It NEVER writes and
        NEVER implies the stored confidence changed.
        """
        return ops.referee_signal(_store(), ops.resolve_alias(_store(), ref))

    # -------------------------------------------------------------------
    # Per-run review surface: summary / diff / ratify-region / rollback
    # All pure compositions of ops reads/writes over the run_id metadata stamp.
    # -------------------------------------------------------------------

    def _run_nodes(run_id: str) -> list[dict[str, Any]]:
        """Every node this run wrote, oldest-first. Pure ops.list_nodes read."""
        out = []
        for node in ops.list_nodes(_store()):
            if node.get("metadata", {}).get("run_id") == run_id:
                out.append(node)
        return out

    def _run_edges(run_id: str) -> list[dict[str, Any]]:
        """Every edge this run wrote. Pure ops.list_edges read."""
        return [
            edge
            for edge in ops.list_edges(_store())
            if edge.get("metadata", {}).get("run_id") == run_id
        ]

    @mcp.resource("sparkle://run/{run_id}/summary")
    def run_summary_resource(run_id: str) -> dict[str, Any]:
        """Counts of what a run created (nodes by type, edges by relation)."""
        return _run_summary(run_id)

    def _run_summary(run_id: str) -> dict[str, Any]:
        nodes = _run_nodes(run_id)
        edges = _run_edges(run_id)
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for n in nodes:
            by_type[n["node_type"]] = by_type.get(n["node_type"], 0) + 1
            by_status[n["status"]] = by_status.get(n["status"], 0) + 1
        by_relation: dict[str, int] = {}
        for e in edges:
            by_relation[e["relation"]] = by_relation.get(e["relation"], 0) + 1
        return {
            "run_id": run_id,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes_by_type": by_type,
            "nodes_by_status": by_status,
            "edges_by_relation": by_relation,
        }

    @mcp.tool()
    def sparkle_run_summary(run_id: str) -> dict[str, Any]:
        """Run summary as a tool (mirror of ``sparkle://run/{run_id}/summary``)."""
        return _run_summary(run_id)

    @mcp.resource("sparkle://run/{run_id}/diff")
    def run_diff_resource(run_id: str) -> dict[str, Any]:
        """The full region a run created: its node views and edge views.

        The review surface for "here's everything this loop touched" before you
        ratify or roll it back. Pure read over the run_id metadata stamp.
        """
        return _run_diff(run_id)

    def _run_diff(run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "nodes": _run_nodes(run_id),
            "edges": _run_edges(run_id),
        }

    @mcp.tool()
    def sparkle_run_diff(run_id: str) -> dict[str, Any]:
        """Run diff as a tool (mirror of ``sparkle://run/{run_id}/diff``)."""
        return _run_diff(run_id)

    @mcp.resource("sparkle://run/{run_id}/manifest")
    def run_manifest_resource(run_id: str) -> dict[str, Any]:
        """The role -> model-family roster a run recorded (cross-family audit).

        Proves which model family backed each role and whether the locked
        critic-differs-from-proposer invariant held. Raises if the run wrote no
        manifest (a debate from before manifests existed, or an unknown id).
        """
        return ops.run_manifest(_store(), run_id)

    @mcp.tool()
    def sparkle_run_manifest(run_id: str) -> dict[str, Any]:
        """Run manifest as a tool (mirror of ``sparkle://run/{run_id}/manifest``)."""
        return ops.run_manifest(_store(), run_id)

    @mcp.tool()
    async def sparkle_ratify_region(
        run_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Human sign-off on a run's nodes: clear the provisional flag in place.

        Composition over ``ops.revise``: for each node this run authored, it
        writes a superseding version with ``provisional`` removed from metadata
        (frozen-model honest — never an in-place mutation). This is the "accept
        what the model drafted" half of the human-refereed loop. It does NOT set
        a terminal status; binding ratification of a claim still goes through
        ``sparkle_rule`` with ``settle=True`` so the challenge invariant holds.
        """
        store = _store()
        data = store.read()

        def _superseded_within_run(node_id: str) -> bool:
            # Skip an original already superseded by ANOTHER node from this run.
            # Two cases this covers: (1) its own CLEARED copy from a prior pass
            # (so repeated calls converge to count=0 instead of re-firing forever
            # on the frozen-provisional original), and (2) a within-run REVISION
            # (V1 superseded by V2 mid-run) — only the live tip needs clearing, so
            # we don't write a needless cleared copy of a dead earlier version.
            for edge in data["edges"].values():
                if edge["relation"] != "supersedes" or edge["to_id"] != node_id:
                    continue
                src_meta = data["nodes"].get(edge["from_id"], {}).get("metadata", {})
                if src_meta.get("run_id") == run_id:
                    return True
            return False

        ratified: list[dict[str, Any]] = []
        for node in _run_nodes(run_id):
            meta = dict(node.get("metadata", {}))
            if not meta.get("provisional"):
                continue
            if _superseded_within_run(node["node_id"]):
                continue
            meta["provisional"] = False
            meta["ratified_run"] = run_id
            # Supersede with a corrected version carrying the cleared stamp, and
            # re-home inbound edges onto the accepted version.
            ratified.append(_revise_metadata(store, node["node_id"], meta))
        await _notify_changed(ctx)
        return {"run_id": run_id, "ratified": ratified, "count": len(ratified)}

    @mcp.tool()
    async def sparkle_rollback_run(
        run_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Mark a run's nodes abandoned via supersession (never a hard delete).

        Composition over ``ops.revise``: the store has no delete, so rollback is
        model-honest — each node this run authored is superseded by a version
        with status ``abandoned`` (which drops it off the frontier). Edges
        remain as provenance of what was tried. Use ``sparkle_run_diff`` first to
        see exactly what will be rolled back.
        """
        store = _store()
        rolled: list[dict[str, Any]] = []
        for node in _run_nodes(run_id):
            # A node already in a terminal status is done: never re-abandon an
            # abandoned draft, and never revert a binding settled ruling — the
            # ratified version inherits the proposer's run_id, so rollback would
            # otherwise nuke a decision the judge made. Terminal means terminal.
            if node["status"] in ops.TERMINAL_STATUSES:
                continue
            # An original already superseded by an abandoned version is closed;
            # re-superseding it would fork another abandoned sibling and keep the
            # rollback from ever converging. Skip it so rollback is idempotent.
            if ops._superseded_by_terminal(store, node["node_id"]):
                continue
            new = ops.revise(
                store,
                node["node_id"],
                status="abandoned",
                rehome_edges=False,
            )
            rolled.append(new)
        await _notify_changed(ctx)
        return {"run_id": run_id, "rolled_back": rolled, "count": len(rolled)}

    # -------------------------------------------------------------------
    # PROMPTS — adversarial playbook as slash commands; call NO model here
    # -------------------------------------------------------------------

    @mcp.prompt()
    def challenge(node_ref: str) -> str:
        """Cast the host model as a critic attacking one claim."""
        return (
            "You are a CRITIC in a structured, human-refereed inquiry. Your job is to "
            f"find the strongest honest objection to the node `{node_ref}`.\n\n"
            "Steps:\n"
            f"1. Read the node and its neighbors via the `sparkle_node` tool (ref `{node_ref}`).\n"
            "2. Identify the single most damaging, non-strawman objection.\n"
            "3. Record it with `sparkle_branch` using template `objection` (writes an "
            f"`objection` node linked `contradicts` to `{node_ref}`).\n\n"
            "Write a real objection, not a weak one you intend to knock down — a weak "
            "challenge launders self-agreement. State the assumption it breaks.\n\n"
            f"{_vocabulary_block()}"
        )

    @mcp.prompt()
    def investigate(node_ref: str) -> str:
        """Cast the host model as an evidence-gatherer for a claim or question."""
        return (
            "You are an EVIDENCE-GATHERER in a structured inquiry. Find concrete "
            f"evidence bearing on the node `{node_ref}` (for or against).\n\n"
            "Steps:\n"
            f"1. Read the node and its neighbors via `sparkle_node` (ref `{node_ref}`).\n"
            "2. Use web search and fetch to find and VERIFY real sources — gather "
            "specific, citable evidence, not opinion and not figures recalled from "
            "memory. For each load-bearing figure, QUOTE the supporting line "
            "verbatim from the source you actually retrieved, so the number is "
            "grounded in source text, not merely asserted.\n"
            "3. Record each piece with `sparkle_branch` using template `support` for "
            "evidence that strengthens it (writes an `evidence` node linked "
            f"`supports` to `{node_ref}`), or template `objection` for evidence that "
            "weakens it. Put the ACTUAL retrieved URLs in `citations`. If you "
            "cannot verify a figure against a real source, mark it "
            "'(recalled, unverified)' rather than inventing a citation.\n\n"
            f"{_vocabulary_block()}"
        )

    @mcp.prompt()
    def synthesize(claim_ref: str) -> str:
        """Cast the host model as judge, then synthesizer, on a claim."""
        return (
            "You are first a JUDGE, then a SYNTHESIZER, in a structured inquiry.\n\n"
            f"1. Read the full region around the claim `{claim_ref}` via "
            "`sparkle_subgraph` and check its live signal via `sparkle_signal`.\n"
            "2. JUDGE: weigh the support against the objections. Treat every "
            "specific figure or named-source citation as UNVERIFIED recall UNLESS "
            "it carries a real retrieved-source URL — then credit it as "
            "source-backed (the gatherer retrieved it, not independently verified "
            "by you). Do NOT let an unverifiable specific be the deciding evidence. "
            "You may only rule with `sparkle_rule` if the claim has a recorded "
            "objection from a DIFFERENT author (the server refuses a self-strawman "
            "ratification). Pass `settle=true` ONLY if you uphold the claim as "
            "stated; a refuted/overstated verdict must use settle=false.\n"
            "3. SYNTHESIZE: capture the durable takeaway with `sparkle_harvest` "
            f"(writes a `synthesis` node linked `produced` to `{claim_ref}`). "
            "Present source-backed figures as such WITH their URLs; mark only "
            "no-source figures '(recalled, unverified)'; do NOT stamp the whole "
            "synthesis as unverified when retrieved sources exist.\n\n"
            "Pass a stable `run_id` to every mutation so the result can be reviewed "
            "or rolled back as one region.\n\n"
            f"{_vocabulary_block()}"
        )

    @mcp.prompt()
    def next_move() -> str:
        """Read the frontier, pick the highest-leverage item, run its prompt."""
        return (
            "You are the OPERATOR of a claim graph. Decide the single "
            "highest-leverage next move.\n\n"
            "Steps:\n"
            "1. Call the `sparkle_frontier` tool (or read `sparkle://frontier`).\n"
            "2. Take the top-ranked entry. Its `suggested_prompt` tells you which "
            "role to play: `/challenge` (attack an unchallenged claim), "
            "`/investigate` (gather evidence for a thin or open node), or "
            "`/synthesize` (judge a claim that has both support and objections).\n"
            "3. Run that prompt against the entry's `node_id`, supplying a stable "
            "`run_id` for the session.\n\n"
            "Priority order is already baked into the frontier ranking: judge-ready "
            "first, then stalled, then unchallenged, then thin-evidence, then open "
            "questions.\n\n"
            f"{_vocabulary_block()}"
        )

    return mcp


def _revise_metadata(
    store: GraphStore, node_id: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Supersede a node to update only its metadata stamp (model-honest).

    ``ops.revise`` does not expose a metadata field (it is editorial fields
    only), so the metadata-only flip used by region ratification goes through
    the same supersede machinery ops uses, via ``ops._supersede_node`` inside a
    graph lock. This is still a composition of ops — no store call here bypasses
    the ops layer's write path or its lock.
    """
    with ops.graph_lock(store):
        result = ops._supersede_node(
            store,
            old_id=store.resolve_id(node_id),
            changes={"metadata": metadata},
            rehome_inbound=True,
            note="region ratification",
        )
        view = ops._node_view(result["node_id"], store.get_node(result["node_id"]))
    return view


# ---------------------------------------------------------------------------
# Entry point — resolve graph path, run over stdio
# ---------------------------------------------------------------------------


def run_server(store_path: str | Path | None = None) -> None:
    """Resolve the graph path, build the app, and serve over stdio.

    Called by ``cli.main()`` on the ``sparkle mcp`` subcommand (which passes its
    ``--store`` value here). The graph path resolves env var > client project dir
    > cwd ``.sparkle/graph.json`` (see :func:`_resolve_graph_path`). The store is
    initialized so a first connection on a fresh project has a valid file.
    """
    global _STORE
    path = _resolve_graph_path(store_path)
    _STORE = GraphStore(path)
    _STORE.init()
    app = build_app()
    app.run(transport="stdio")


# ---------------------------------------------------------------------------
# Phase 2 seam (intentionally NOT implemented here)
# ---------------------------------------------------------------------------
# The autonomous harness (`sparkle[agents]` extra, `src/sparkle/harness.py`)
# will NOT route through this MCP layer. It will import `ops.py` directly and
# walk the playbook, filling each role's text via its own thinking backend and
# committing through the same `ops` functions these tools wrap — so it inherits
# the same invariants, dedup gate, and confidence cap by construction. No
# `sparkle_run_loop`, no model/LLM client, and no autonomy engine belongs in
# this file. This comment is the seam; that is all Phase 2 gets here for now.
