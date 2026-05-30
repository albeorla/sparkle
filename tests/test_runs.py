"""Run accounting, lineage/region integrity, locking, and error-path coverage.

This file pins the *run-region* surface (what a loop wrote, how to ratify or roll
it back), the single-writer file lock that keeps two writers from clobbering each
other, and the documented ``ValueError`` boundaries (corrupt graph, ambiguous
prefix, unknown node/relation/type).

Two layers, by design:

1. SDK-FREE layer (always runs in the zero-dependency base suite): the file lock,
   the documented error paths, and the run-accounting / rollback / region-ratify
   *mechanics*. Run accounting in the product lives in ``mcp_server.py`` (the
   ``run_id`` metadata stamp is applied only by the MCP tools), but every piece it
   is built from is pure ``ops``/``graph``. This layer reconstructs the exact
   filtering + rollback + metadata-flip the MCP layer composes, driving ``ops``
   directly, so the invariants are proven even when the MCP SDK is absent.
2. SDK-GATED layer (skipped gracefully when ``mcp`` is not installed): exercises
   the real ``build_app()`` run tools end-to-end so the actual closures
   (``sparkle_run_summary``/``_diff``/``ratify_region``/``rollback_run``) are
   covered when the extra is present.

Conventions match tests/test_cli.py: pure-stdlib ``unittest``, every test isolates
its graph under a ``tempfile.TemporaryDirectory`` (no shared ``.sparkle`` state),
and a handle is read off the ``Handle:`` line (never ``split()[-1]``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from src.sparkle import ops
from src.sparkle.graph import GraphStore
from src.sparkle.models import Edge, Node


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Local mirror of the MCP run-region logic (pure ops; no SDK needed).
#
# These three helpers reproduce EXACTLY what mcp_server.py composes over the
# run_id metadata stamp (mcp_server.py:457-496, 540-578). They let the base
# suite prove run accounting / rollback / region-ratify integrity without the
# MCP SDK installed. The SDK-gated TestCase below proves the real closures match.
# ---------------------------------------------------------------------------


def _provisional_meta(run_id: str, agent_role: str) -> dict:
    """Mirror of mcp_server._provisional_metadata (mcp_server.py:101-118)."""
    return {"provisional": True, "run_id": run_id, "agent_role": agent_role}


def _run_nodes(store: GraphStore, run_id: str) -> list[dict]:
    """Mirror of the MCP _run_nodes closure (mcp_server.py:457-463)."""
    return [
        node
        for node in ops.list_nodes(store)
        if node.get("metadata", {}).get("run_id") == run_id
    ]


def _run_edges(store: GraphStore, run_id: str) -> list[dict]:
    """Mirror of the MCP _run_edges closure (mcp_server.py:465-471)."""
    return [
        edge
        for edge in ops.list_edges(store)
        if edge.get("metadata", {}).get("run_id") == run_id
    ]


def _run_summary(store: GraphStore, run_id: str) -> dict:
    """Mirror of the MCP _run_summary closure (mcp_server.py:478-496)."""
    nodes = _run_nodes(store, run_id)
    edges = _run_edges(store, run_id)
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


def _run_diff(store: GraphStore, run_id: str) -> dict:
    """Mirror of the MCP _run_diff closure (mcp_server.py:512-517)."""
    return {
        "run_id": run_id,
        "nodes": _run_nodes(store, run_id),
        "edges": _run_edges(store, run_id),
    }


def _rollback_run(store: GraphStore, run_id: str) -> dict:
    """Mirror of the MCP sparkle_rollback_run closure (mcp_server.py:565-585).

    Supersede every node this run authored with a version status='abandoned'
    (no rehome). The store has no delete; rollback is model-honest supersession.
    Two skips keep it idempotent and prevent reverting a binding ruling:
    a node already in a terminal status is left alone (never re-abandon a draft,
    never revert a settled 'ratified' version that inherited the run_id), and an
    original already superseded by a terminal version is closed, so re-rolling
    back does not fork a second abandoned sibling.
    """
    rolled: list[dict] = []
    for node in _run_nodes(store, run_id):
        if node["status"] in ops.TERMINAL_STATUSES:
            continue
        if ops._superseded_by_terminal(store, node["node_id"]):
            continue
        rolled.append(
            ops.revise(store, node["node_id"], status="abandoned", rehome_edges=False)
        )
    return {"run_id": run_id, "rolled_back": rolled, "count": len(rolled)}


# ---------------------------------------------------------------------------
# Shared base case: isolated temp store per test, plus a tiny debate builder.
# ---------------------------------------------------------------------------


class RunTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_run_node(
        self,
        *,
        run_id: str,
        node_type: str = "claim",
        title: str,
        content: str,
        agent_role: str = "proposer",
        status: str = "active",
        link_to: str | None = None,
        relation: str | None = None,
        confidence: float | None = 0.5,
    ) -> dict:
        """Add a run-tagged, model-authored node the way the MCP tools do."""
        return ops.add_node(
            self.store,
            node_type=node_type,
            title=title,
            content=content,
            author=agent_role,
            confidence=confidence,
            status=status,
            metadata=_provisional_meta(run_id, agent_role),
            link_to=link_to,
            relation=relation,
            model_authored=True,
        )


# ===========================================================================
# 1. RUN ACCOUNTING: run_summary / run_diff report the right nodes/edges
# ===========================================================================


class RunAccountingTests(RunTestBase):
    def test_run_summary_counts_only_this_runs_nodes_and_edges(self) -> None:
        # Run A: a claim plus an objection branch attacking it (a node + an edge).
        claim = self.add_run_node(
            run_id="runA", title="A claim", content="under debate"
        )
        ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="An objection",
            content="this is wrong because",
            author="critic",
            model_authored=True,
        )
        # The objection branch's edge is NOT run-tagged (add_branch stamps no
        # run_id), so run accounting that filters on metadata.run_id sees the
        # claim only. We tag a SECOND run's work explicitly to prove isolation.
        ev = self.add_run_node(
            run_id="runB",
            node_type="evidence",
            title="Some evidence",
            content="supporting datum",
            agent_role="evidence_gatherer",
            link_to=claim["node_id"],
            relation="supports",
            confidence=0.4,
        )

        summary_a = _run_summary(self.store, "runA")
        summary_b = _run_summary(self.store, "runB")

        # Run A authored exactly one node (the claim) carrying run_id=runA.
        self.assertEqual(summary_a["node_count"], 1)
        self.assertEqual(summary_a["nodes_by_type"], {"claim": 1})
        self.assertEqual(summary_a["nodes_by_status"], {"active": 1})

        # Run B authored one evidence node and one supports edge (the fused link
        # carries no run_id of its own — add_node's link does not stamp metadata,
        # so the edge is NOT counted under run B). Pin that exact behavior.
        self.assertEqual(summary_b["node_count"], 1)
        self.assertEqual(summary_b["nodes_by_type"], {"evidence": 1})
        self.assertEqual(summary_b["edge_count"], 0)
        self.assertNotIn(ev["node_id"], [n["node_id"] for n in _run_nodes(self.store, "runA")])

    def test_run_summary_counts_run_tagged_edges_by_relation(self) -> None:
        # A run that writes an explicit, run-tagged edge (the way sparkle_link does).
        a = self.add_run_node(run_id="r", title="First", content="alpha")
        b = self.add_run_node(
            run_id="r", node_type="evidence", title="Second", content="beta"
        )
        ops.add_edge(
            self.store,
            from_ref=b["node_id"],
            to_ref=a["node_id"],
            relation="supports",
            metadata=_provisional_meta("r", "evidence_gatherer"),
        )

        summary = _run_summary(self.store, "r")
        self.assertEqual(summary["node_count"], 2)
        self.assertEqual(summary["edge_count"], 1)
        self.assertEqual(summary["edges_by_relation"], {"supports": 1})
        self.assertEqual(summary["nodes_by_type"], {"claim": 1, "evidence": 1})

    def test_run_diff_returns_node_and_edge_views_for_the_run(self) -> None:
        a = self.add_run_node(run_id="run1", title="Claim one", content="c1")
        b = self.add_run_node(
            run_id="run1", node_type="evidence", title="Ev one", content="e1"
        )
        ops.add_edge(
            self.store,
            from_ref=b["node_id"],
            to_ref=a["node_id"],
            relation="supports",
            metadata=_provisional_meta("run1", "evidence_gatherer"),
        )
        # An unrelated node in a different run must never leak into this diff.
        self.add_run_node(run_id="OTHER", title="Other claim", content="other")

        diff = _run_diff(self.store, "run1")
        node_ids = {n["node_id"] for n in diff["nodes"]}
        self.assertEqual(node_ids, {a["node_id"], b["node_id"]})
        self.assertEqual(len(diff["edges"]), 1)
        self.assertEqual(diff["edges"][0]["relation"], "supports")
        # Every node in the diff carries the run stamp.
        for n in diff["nodes"]:
            self.assertEqual(n["metadata"]["run_id"], "run1")

    def test_run_summary_empty_for_unknown_run(self) -> None:
        self.add_run_node(run_id="real", title="Claim", content="content")
        summary = _run_summary(self.store, "ghost")
        self.assertEqual(summary["node_count"], 0)
        self.assertEqual(summary["edge_count"], 0)
        self.assertEqual(summary["nodes_by_type"], {})
        self.assertEqual(_run_diff(self.store, "ghost")["nodes"], [])


# ===========================================================================
# 2. ROLLBACK: rollback_run reverts a run's effects (via supersession)
# ===========================================================================


class RollbackRunTests(RunTestBase):
    def test_rollback_supersedes_each_run_node_to_abandoned(self) -> None:
        c1 = self.add_run_node(run_id="loop", title="Claim X", content="x")
        c2 = self.add_run_node(
            run_id="loop", node_type="evidence", title="Ev Y", content="y"
        )
        # A node from a different run must be untouched by this rollback.
        keep = self.add_run_node(run_id="keepme", title="Keeper", content="z")

        result = _rollback_run(self.store, "loop")
        self.assertEqual(result["count"], 2)

        # The original frozen nodes still exist (no hard delete) but are now
        # superseded by abandoned versions, so the live frontier-facing status
        # for the run's content is 'abandoned'.
        new_statuses = sorted(n["status"] for n in result["rolled_back"])
        self.assertEqual(new_statuses, ["abandoned", "abandoned"])

        # The originals are frozen and unchanged.
        self.assertEqual(self.store.get_node(c1["node_id"])["status"], "active")
        self.assertEqual(self.store.get_node(c2["node_id"])["status"], "active")
        # Each rolled-back node points supersedes--> its original.
        for rolled in result["rolled_back"]:
            self.assertEqual(rolled["old_id"] in (c1["node_id"], c2["node_id"]), True)

        # The untouched run's node is still active.
        self.assertEqual(self.store.get_node(keep["node_id"])["status"], "active")

    def test_rollback_clears_the_rolled_back_claim_from_the_frontier(self) -> None:
        """A rolled-back run leaves the work feed entirely (defect now fixed).

        Rollback supersedes the claim with an ``abandoned`` version. The ORIGINAL
        claim is frozen and stays ``active``, but the frontier now follows the
        ``supersedes`` edge: a node a TERMINAL version superseded is treated as
        closed (ops._superseded_by_terminal), so the frozen original drops off
        the feed along with its abandoned copy. A human who rolls back a run sees
        that work disappear from "what needs attention next", as intended.

        Previously this pinned a DEFECT (the original lingered on UNCHALLENGED
        forever); the engine fix closes the superseded original on the frontier.
        """
        claim = self.add_run_node(run_id="loop", title="Lonely claim", content="solo")
        before = ops.frontier(self.store)
        self.assertIn("Lonely claim", {e["title"] for e in before["entries"]})

        _rollback_run(self.store, "loop")

        after = ops.frontier(self.store)
        entries_by_id = {e["node_id"]: e for e in after["entries"]}
        # The abandoned superseding copy is suppressed by its own terminal status.
        abandoned_ids = {
            n["node_id"] for n in ops.list_nodes(self.store) if n["status"] == "abandoned"
        }
        self.assertTrue(abandoned_ids, "rollback should have written an abandoned node")
        self.assertFalse(
            abandoned_ids & set(entries_by_id),
            "abandoned superseding version should not appear on the frontier",
        )
        # The ORIGINAL active claim is now closed too: superseded by a terminal
        # (abandoned) version, so it leaves the feed.
        self.assertNotIn(
            claim["node_id"],
            entries_by_id,
            "rollback should close the superseded original on the frontier",
        )
        # Nothing from this run is left needing attention.
        self.assertEqual(after["entries"], [])

    def test_rollback_is_idempotent_second_pass_is_a_noop(self) -> None:
        """Rollback converges: a second pass finds nothing left to roll back.

        The original is frozen ``active`` and the first rollback supersedes it
        with an ``abandoned`` version. The second pass skips it on two counts:
        the abandoned copy is terminal, and the original is already superseded by
        a terminal version (ops._superseded_by_terminal). So ``count`` drops to 0
        instead of re-firing forever. Previously this pinned a DEFECT (count
        stayed 1 on every call); the rollback skip-guard now makes it converge.
        """
        self.add_run_node(run_id="loop", title="Claim", content="once")
        first = _rollback_run(self.store, "loop")
        self.assertEqual(first["count"], 1)

        # Second rollback: original already superseded by an abandoned version,
        # so there is nothing left to roll back -> count 0.
        second = _rollback_run(self.store, "loop")
        self.assertEqual(
            second["count"],
            0,
            "rollback should be idempotent: nothing left to roll back",
        )
        # The store has exactly 2 nodes tagged for this run (active + abandoned);
        # repeated rollbacks do not fork new abandoned nodes (deterministic id).
        run_tagged = [
            n
            for n in ops.list_nodes(self.store)
            if n.get("metadata", {}).get("run_id") == "loop"
        ]
        self.assertEqual(len(run_tagged), 2)
        self.assertEqual(
            sorted(n["status"] for n in run_tagged), ["abandoned", "active"]
        )

    def test_rollback_empty_run_is_a_noop(self) -> None:
        self.add_run_node(run_id="present", title="C", content="here")
        result = _rollback_run(self.store, "absent")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["rolled_back"], [])


# ===========================================================================
# 3. REGION RATIFY: clearing the provisional flag in place (metadata flip)
# ===========================================================================


class RatifyRegionTests(RunTestBase):
    """Region ratification per spec: for each run node still flagged provisional,
    supersede with a version that has provisional cleared. Drives the same
    mcp_server._revise_metadata machinery (ops.graph_lock + ops._supersede_node).
    """

    def _ratify_region(self, run_id: str) -> dict:
        """Mirror of sparkle_ratify_region (mcp_server.py:525-550)."""
        ratified: list[dict] = []
        for node in _run_nodes(self.store, run_id):
            meta = dict(node.get("metadata", {}))
            if not meta.get("provisional"):
                continue
            meta["provisional"] = False
            meta["ratified_run"] = run_id
            with ops.graph_lock(self.store):
                result = ops._supersede_node(
                    self.store,
                    old_id=self.store.resolve_id(node["node_id"]),
                    changes={"metadata": meta},
                    rehome_inbound=True,
                    note="region ratification",
                )
                view = ops._node_view(
                    result["node_id"], self.store.get_node(result["node_id"])
                )
            ratified.append(view)
        return {"run_id": run_id, "ratified": ratified, "count": len(ratified)}

    def test_ratify_region_clears_provisional_flag_via_supersession(self) -> None:
        claim = self.add_run_node(run_id="sess", title="Draft claim", content="draft")
        self.assertTrue(self.store.get_node(claim["node_id"])["metadata"]["provisional"])

        result = self._ratify_region("sess")
        self.assertEqual(result["count"], 1)
        accepted = result["ratified"][0]

        # The accepted (superseding) version has provisional cleared and the
        # ratified_run stamp set; it is a NEW node, not the original mutated.
        self.assertFalse(accepted["metadata"]["provisional"])
        self.assertEqual(accepted["metadata"]["ratified_run"], "sess")
        self.assertNotEqual(accepted["node_id"], claim["node_id"])

        # The original is frozen: still provisional.
        self.assertTrue(self.store.get_node(claim["node_id"])["metadata"]["provisional"])

        # Status is NOT bumped to a terminal value by region ratify (binding
        # ratification of a claim goes through rule(settle=True)).
        self.assertEqual(accepted["status"], "active")

    def test_ratify_region_skips_the_accepted_copy_but_refires_on_original(self) -> None:
        """The accepted copy IS skipped (provisional cleared), but the frozen
        original stays provisional and run-tagged, so a second ratify re-fires
        on it. Same non-idempotency root as rollback: region ops supersede but
        never neutralize the original. Pins the current behavior; see findings.
        """
        self.add_run_node(run_id="sess", title="Claim", content="body")
        first = self._ratify_region("sess")
        self.assertEqual(first["count"], 1)

        run_nodes = _run_nodes(self.store, "sess")
        provisional_flags = sorted(
            n["metadata"].get("provisional") for n in run_nodes
        )
        # One accepted copy (False) + the frozen original (True).
        self.assertEqual(provisional_flags, [False, True])

        # The accepted copy is correctly skipped (provisional already cleared),
        # but the still-provisional original is re-superseded -> count 1, not 0.
        second = self._ratify_region("sess")
        self.assertEqual(
            second["count"],
            1,
            "region ratify re-fires on the still-provisional frozen original",
        )

    def test_ratify_region_rehomes_inbound_edges_onto_accepted_version(self) -> None:
        # A claim with an inbound supporting edge. After region ratify, the
        # support must re-home onto the accepted version so provenance survives.
        claim = self.add_run_node(run_id="sess", title="Target", content="t")
        ev = self.add_run_node(
            run_id="OTHER",  # different run so ratify only touches the claim
            node_type="evidence",
            title="Backing",
            content="b",
            agent_role="evidence_gatherer",
            link_to=claim["node_id"],
            relation="supports",
        )

        result = self._ratify_region("sess")
        accepted_id = result["ratified"][0]["node_id"]

        # The accepted claim version now has the supporting evidence inbound.
        accepted = ops.get_node(self.store, accepted_id)
        inbound_ids = {n["node_id"] for n in accepted["inbound"]}
        self.assertIn(ev["node_id"], inbound_ids)


# ===========================================================================
# 4. LINEAGE / REGION INTEGRITY: a ruling + ratified version surface together
# ===========================================================================


class LineageRegionIntegrityTests(RunTestBase):
    def test_settled_ruling_writes_ratified_version_and_decision_in_lineage(self) -> None:
        # Full region: propose -> attack -> rule(settle) -> the ratified version
        # and the decision both surface in the original claim's lineage (inbound
        # walk), proving the region is recoverable from the claim.
        claim = self.add_run_node(run_id="r", title="Debated claim", content="dc")
        ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Counterpoint",
            content="actually no",
            author="critic",
            model_authored=True,
        )
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            rationale="the objection fails",
            settle=True,
            author="judge",
        )
        self.assertTrue(ruling["settled"])
        self.assertIn("ratified_claim", ruling)
        self.assertEqual(ruling["ratified_claim"]["status"], "ratified")

        # lineage(claim) follows inbound edges, so the decision node (evaluates
        # --> claim) and the objection (contradicts --> claim) both appear.
        lineage_titles = {n["title"] for n in ops.lineage(self.store, claim["node_id"])}
        self.assertIn("Debated claim", lineage_titles)
        self.assertIn("Counterpoint", lineage_titles)
        self.assertTrue(
            any(t.startswith("Ruling:") for t in lineage_titles),
            f"decision node missing from lineage: {lineage_titles}",
        )

    def test_referee_signal_reports_ratified_after_settled_ruling(self) -> None:
        claim = self.add_run_node(run_id="r", title="Claim", content="x")
        ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Obj",
            content="no",
            author="critic",
            model_authored=True,
        )
        # Before ruling: challenged but no decision -> ready_to_judge / thin.
        before = ops.referee_signal(self.store, claim["node_id"])
        self.assertTrue(before["challenged"])
        self.assertFalse(before["has_decision"])

        ruling = ops.rule(
            self.store, claim["node_id"], verdict="accept", settle=True, author="judge"
        )
        ratified_id = ruling["ratified_claim"]["node_id"]
        # The ratified version has a decision evaluating it -> live signal ratified.
        sig = ops.referee_signal(self.store, ratified_id)
        self.assertEqual(sig["stored_status"], "ratified")
        self.assertEqual(sig["live_signal"], "ratified")


# ===========================================================================
# 5. SINGLE-WRITER FILE LOCK: prove it serializes concurrent writers
# ===========================================================================


# Worker script run as a separate OS process. Each process adds N distinct
# nodes (distinct content so the dedup gate never collapses them) to one shared
# store, each via ops.add_node (which takes ops.graph_lock for its whole
# read-modify-write). If the advisory lock genuinely serializes writers, every
# distinct node survives; without it, concurrent read-modify-write on the single
# JSON file would lose updates (a classic lost-update race).
_WORKER_SRC = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    repo_root = Path(sys.argv[1])
    sys.path.insert(0, str(repo_root))

    from src.sparkle import ops
    from src.sparkle.graph import GraphStore

    store_path = sys.argv[2]
    worker_id = sys.argv[3]
    count = int(sys.argv[4])

    store = GraphStore(store_path)
    for i in range(count):
        ops.add_node(
            store,
            node_type="claim",
            title=f"w{worker_id}-n{i}",
            content=f"content from worker {worker_id} item {i}",
            confidence=0.5,
        )
    """
)


@unittest.skipUnless(
    ops.fcntl is not None,
    "POSIX fcntl advisory lock unavailable on this platform; lock is a no-op",
)
class FileLockSerializationTests(RunTestBase):
    def test_lock_sidecar_is_created_on_a_locked_write(self) -> None:
        self.add_run_node(run_id="r", title="Claim", content="body")
        lock_path = self.store_path.with_name(self.store_path.name + ".lock")
        self.assertTrue(
            lock_path.exists(), "graph_lock should create the .lock sidecar next to the store"
        )

    def test_concurrent_subprocess_writers_lose_no_updates(self) -> None:
        """Spawn real OS processes hammering one store; assert no lost update.

        This is the true concurrency test: separate processes cannot share the
        in-process GIL, so only the OS-level fcntl lock can serialize their
        read-modify-write cycles. We assert the store ends with EXACTLY
        writers*per_writer distinct nodes and is valid JSON (no torn write).
        """
        writers = 4
        per_writer = 25
        worker_file = Path(self.temp_dir.name) / "worker.py"
        worker_file.write_text(_WORKER_SRC, encoding="utf-8")

        procs = []
        for w in range(writers):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(worker_file),
                        str(REPO_ROOT),
                        str(self.store_path),
                        str(w),
                        str(per_writer),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(
                p.returncode,
                0,
                f"worker failed (rc={p.returncode}):\nSTDOUT:{out.decode()}\nSTDERR:{err.decode()}",
            )

        # Store must be valid JSON (no torn/partial write under contention).
        data = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertIn("nodes", data)

        # Every distinct node must have survived -> no lost update.
        nodes = ops.list_nodes(self.store)
        self.assertEqual(
            len(nodes),
            writers * per_writer,
            "lost update under concurrency: expected "
            f"{writers * per_writer} nodes, found {len(nodes)}",
        )
        # And the titles cover the full w*per_writer cross-product exactly once.
        titles = sorted(n["title"] for n in nodes)
        expected = sorted(
            f"w{w}-n{i}" for w in range(writers) for i in range(per_writer)
        )
        self.assertEqual(titles, expected)

    def test_concurrent_threads_lose_no_updates(self) -> None:
        """In-process thread variant: many threads, one store, no lost update.

        Threads contend on the same process; the GIL plus the advisory lock's
        read-modify-write window still admit a lost-update race if the lock did
        not serialize the whole add_node cycle. Asserting every distinct node
        survives proves the locked write path holds within a process too.
        """
        threads_n = 6
        per_thread = 20
        errors: list[BaseException] = []

        def worker(tid: int) -> None:
            try:
                store = GraphStore(self.store_path)
                for i in range(per_thread):
                    ops.add_node(
                        store,
                        node_type="claim",
                        title=f"t{tid}-n{i}",
                        content=f"thread {tid} item {i}",
                        confidence=0.5,
                    )
            except BaseException as exc:  # noqa: BLE001 - record and assert later
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        self.assertEqual(errors, [], f"worker threads raised: {errors}")
        nodes = ops.list_nodes(self.store)
        self.assertEqual(
            len(nodes),
            threads_n * per_thread,
            "lost update across threads: expected "
            f"{threads_n * per_thread} nodes, found {len(nodes)}",
        )


# ===========================================================================
# 6. ERROR PATHS: corrupt graph, ambiguous prefix, unknown node/relation/type
# ===========================================================================


class ErrorPathTests(RunTestBase):
    def test_corrupt_graph_json_raises_value_error(self) -> None:
        # A graph.json that is not valid JSON must raise the documented error.
        self.store_path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Corrupt graph store at"):
            self.store.read()

    def test_valid_json_wrong_shape_raises_invalid_graph(self) -> None:
        # Valid JSON but missing the nodes/edges dicts -> 'Invalid graph store'.
        self.store_path.write_text(json.dumps({"nodes": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Invalid graph store at"):
            self.store.read()

    def test_valid_json_node_missing_required_field_raises_invalid_graph(self) -> None:
        bad = {"nodes": {"abc": {"node_type": "claim", "title": "t"}}, "edges": {}}
        self.store_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Invalid graph store at"):
            self.store.read()

    def test_ambiguous_prefix_raises_value_error(self) -> None:
        # Force two node ids that share a common prefix, then resolve that prefix.
        data = self.store.read()
        data["nodes"] = {
            "abc111": {
                "node_type": "claim",
                "title": "one",
                "content": "c1",
                "status": "active",
                "created_at": "2024-01-01T00:00:00+00:00",
            },
            "abc222": {
                "node_type": "claim",
                "title": "two",
                "content": "c2",
                "status": "active",
                "created_at": "2024-01-01T00:00:01+00:00",
            },
        }
        # Write directly (bypassing add_node) to control the ids precisely.
        self.store_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Ambiguous prefix ab"):
            self.store.resolve_id("ab")

    def test_unknown_prefix_raises_no_node_found(self) -> None:
        self.add_run_node(run_id="r", title="Claim", content="body")
        with self.assertRaisesRegex(ValueError, r"No node found for prefix"):
            self.store.resolve_id("ffffffffffff")

    def test_empty_prefix_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, r"node prefix cannot be empty"):
            self.store.resolve_id("   ")

    def test_unknown_node_type_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, r"unknown node_type"):
            ops.add_node(
                self.store,
                node_type="not_a_type",
                title="Bad",
                content="bad type",
            )

    def test_unknown_relation_raises_value_error(self) -> None:
        a = self.add_run_node(run_id="r", title="A", content="a")
        b = self.add_run_node(run_id="r", node_type="evidence", title="B", content="b")
        with self.assertRaisesRegex(ValueError, r"unknown edge relation"):
            ops.add_edge(
                self.store,
                from_ref=b["node_id"],
                to_ref=a["node_id"],
                relation="not_a_relation",
            )

    def test_unknown_branch_template_raises_value_error(self) -> None:
        claim = self.add_run_node(run_id="r", title="Claim", content="body")
        with self.assertRaisesRegex(ValueError, r"Unknown template"):
            ops.add_branch(
                self.store,
                from_ref=claim["node_id"],
                template="not_a_template",
                title="x",
                content="y",
            )

    def test_rule_on_unattacked_claim_refuses_with_value_error(self) -> None:
        # Run accounting / rollback only matters once a region exists; the hard
        # gate is that you cannot ratify a claim the adversary never attacked.
        claim = self.add_run_node(run_id="r", title="Unchallenged", content="solo")
        with self.assertRaisesRegex(ValueError, r"never attacked"):
            ops.rule(self.store, claim["node_id"], verdict="accept", settle=True)

    def test_rule_targeting_non_claim_raises_value_error(self) -> None:
        ev = self.add_run_node(
            run_id="r", node_type="evidence", title="Ev", content="datum"
        )
        with self.assertRaisesRegex(ValueError, r"rule\(\) targets a claim"):
            ops.rule(self.store, ev["node_id"], verdict="accept")


# ===========================================================================
# 6b. RUN MANIFEST: the per-run role -> model-family roster (cross-family audit)
# ===========================================================================


class RunManifestTests(RunTestBase):
    """The manifest is the persisted PROOF of which model family backed each
    role. It rides a top-level ``runs`` key inside the store, outside every
    hashed node/edge payload, so writing it must never perturb a node id.
    """

    _ROLES = {
        "proposer": {"family": "claude", "model": "claude-opus-4-8", "author": "proposer"},
        "critic": {"family": "codex", "model": "gpt-5.5", "author": "critic"},
    }

    def test_write_then_read_round_trips_the_roster(self) -> None:
        ops.write_run_manifest(
            self.store, "run-x", self._ROLES, cross_family_ok=True, label_with_model=False
        )
        got = ops.run_manifest(self.store, "run-x")
        self.assertEqual(got["run_id"], "run-x")
        self.assertEqual(got["roles"], self._ROLES)
        self.assertTrue(got["cross_family_ok"])
        self.assertFalse(got["label_with_model"])
        self.assertIn("started_at", got)

    def test_manifest_survives_node_writes_and_leaves_ids_untouched(self) -> None:
        # A node written BEFORE the manifest keeps its content-addressed id
        # after a manifest write, and the manifest is still readable after MORE
        # node writes land (the top-level 'runs' key rides read-modify-write).
        claim = self.add_run_node(run_id="run-y", title="Claim", content="body")
        before_id = claim["node_id"]

        ops.write_run_manifest(
            self.store, "run-y", self._ROLES, cross_family_ok=True, label_with_model=False
        )
        # The earlier node is untouched (same id resolves to the same node).
        self.assertEqual(self.store.get_node(before_id)["title"], "Claim")

        # Write another node AFTER the manifest; the manifest still reads back.
        self.add_run_node(
            run_id="run-y", node_type="evidence", title="Ev", content="datum"
        )
        self.assertEqual(self.store.get_node(before_id)["title"], "Claim")
        self.assertEqual(ops.run_manifest(self.store, "run-y")["roles"], self._ROLES)

    def test_manifest_survives_a_ruling_supersession_rewrite(self) -> None:
        # ops.rule(settle=True) supersedes the claim with a ratified version
        # (a new node plus rehomed edges) — the heaviest store-mutation path,
        # not a plain add_node. The manifest must survive that rewrite too.
        claim = self.add_run_node(run_id="run-z", title="Claim", content="debated")
        ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Obj",
            content="no",
            author="critic",
            model_authored=True,
        )
        ops.write_run_manifest(
            self.store, "run-z", self._ROLES, cross_family_ok=True, label_with_model=False
        )
        ruling = ops.rule(
            self.store, claim["node_id"], verdict="accept", settle=True, author="judge"
        )
        self.assertTrue(ruling["settled"])
        self.assertEqual(ops.run_manifest(self.store, "run-z")["roles"], self._ROLES)

    def test_run_manifest_unknown_run_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, r"no run manifest for run_id"):
            ops.run_manifest(self.store, "ghost")

    def test_write_manifest_rejects_empty_run_id(self) -> None:
        with self.assertRaisesRegex(ValueError, r"run_id cannot be empty"):
            ops.write_run_manifest(
                self.store, "  ", {}, cross_family_ok=True, label_with_model=False
            )


# ===========================================================================
# 7. SDK-GATED: the REAL MCP run closures, end-to-end (skips without the extra)
# ===========================================================================

try:  # The base zero-dependency run skips this whole class.
    import mcp  # noqa: F401

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on whether sparkle[mcp] is installed
    _MCP_AVAILABLE = False


@unittest.skipUnless(_MCP_AVAILABLE, "sparkle[mcp] extra not installed; MCP SDK absent")
class McpRunClosureTests(unittest.TestCase):
    """Drive the real build_app() run tools so the actual closures are covered.

    Only runs when the MCP SDK is importable. The SDK-free TestCases above
    already prove the same invariants against ops directly, so the base
    zero-dependency suite stays fully covered even when this class is skipped.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        from src.sparkle import mcp_server

        self.mcp_server = mcp_server
        mcp_server._STORE = GraphStore(self.store_path)
        mcp_server._STORE.init()
        self.app = mcp_server.build_app()

    def tearDown(self) -> None:
        self.mcp_server._STORE = None
        self.temp_dir.cleanup()

    def _tool(self, name: str):
        """Resolve a registered tool's underlying callable by name."""
        # FastMCP stores tools in a tool manager; the public fn is on the Tool.
        manager = self.app._tool_manager
        tool = manager.get_tool(name) if hasattr(manager, "get_tool") else None
        if tool is None:
            tools = manager.list_tools() if hasattr(manager, "list_tools") else []
            tool = next((t for t in tools if t.name == name), None)
        if tool is None:
            self.skipTest(f"tool {name} not introspectable on this MCP SDK version")
        return tool.fn

    def _call(self, name: str, **kwargs):
        import asyncio
        import inspect

        fn = self._tool(name)
        if inspect.iscoroutinefunction(fn):
            return asyncio.run(fn(**kwargs))
        return fn(**kwargs)

    def test_build_app_registers_the_run_tools(self) -> None:
        manager = self.app._tool_manager
        tools = manager.list_tools() if hasattr(manager, "list_tools") else []
        names = {t.name for t in tools}
        for required in (
            "sparkle_run_summary",
            "sparkle_run_diff",
            "sparkle_run_manifest",
            "sparkle_ratify_region",
            "sparkle_rollback_run",
        ):
            self.assertIn(required, names)

    def test_run_manifest_tool_reads_back_the_roster(self) -> None:
        # The engine writes manifests; here we write one through ops on the
        # server's store, then prove the real tool closure reads it back.
        ops.write_run_manifest(
            self.mcp_server._STORE,
            "mcpmanifest",
            {"proposer": {"family": "claude", "model": "claude-opus-4-8", "author": "proposer"},
             "critic": {"family": "codex", "model": "gpt-5.5", "author": "critic"}},
            cross_family_ok=True,
            label_with_model=False,
        )
        manifest = self._call("sparkle_run_manifest", run_id="mcpmanifest")
        self.assertEqual(manifest["run_id"], "mcpmanifest")
        self.assertTrue(manifest["cross_family_ok"])
        self.assertNotEqual(
            manifest["roles"]["proposer"]["family"],
            manifest["roles"]["critic"]["family"],
        )

    def test_run_summary_and_diff_report_the_runs_region(self) -> None:
        self._call(
            "sparkle_add_node",
            node_type="claim",
            title="MCP claim",
            content="written via the real tool",
            run_id="mcprun",
        )
        summary = self._call("sparkle_run_summary", run_id="mcprun")
        self.assertEqual(summary["node_count"], 1)
        self.assertEqual(summary["nodes_by_type"], {"claim": 1})

        diff = self._call("sparkle_run_diff", run_id="mcprun")
        self.assertEqual(len(diff["nodes"]), 1)
        self.assertEqual(diff["nodes"][0]["metadata"]["run_id"], "mcprun")

    def test_rollback_run_marks_region_abandoned(self) -> None:
        self._call(
            "sparkle_add_node",
            node_type="claim",
            title="Doomed",
            content="to be rolled back",
            run_id="doomrun",
        )
        result = self._call("sparkle_rollback_run", run_id="doomrun")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rolled_back"][0]["status"], "abandoned")

    def test_ratify_region_clears_provisional(self) -> None:
        self._call(
            "sparkle_add_node",
            node_type="claim",
            title="Draft",
            content="needs sign-off",
            run_id="ratrun",
        )
        result = self._call("sparkle_ratify_region", run_id="ratrun")
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["ratified"][0]["metadata"]["provisional"])
        self.assertEqual(result["ratified"][0]["metadata"]["ratified_run"], "ratrun")

    def test_rule_refusal_surfaces_through_the_tool(self) -> None:
        node = self._call(
            "sparkle_add_node",
            node_type="claim",
            title="Unattacked",
            content="no objection yet",
            run_id="r",
        )
        with self.assertRaisesRegex(ValueError, r"never attacked"):
            self._call(
                "sparkle_rule",
                claim_ref=node["node_id"],
                verdict="accept",
                settle=True,
                run_id="r",
            )

    def test_mcp_rule_refuses_a_self_strawman_ratification(self):
        # The agent-driven MCP path enforces the author-distinct floor (it passes
        # require_distinct_adversary=True, like the autonomous harness): a claim
        # objected to ONLY by its own author cannot be ruled/ratified, so a single
        # autonomous agent cannot self-strawman its way to a settled claim.
        claim = self._call(
            "sparkle_add_node",
            node_type="claim",
            title="Self-authored claim",
            content="the proposer's own claim",
            run_id="ssr",
            agent_role="proposer",
        )
        # Same-author objection: the proposer objecting to its OWN claim.
        self._call(
            "sparkle_branch",
            from_ref=claim["node_id"],
            template="objection",
            title="A strawman I wrote myself",
            content="a self-authored objection",
            run_id="ssr",
            agent_role="proposer",
        )
        with self.assertRaisesRegex(ValueError, r"own author"):
            self._call(
                "sparkle_rule",
                claim_ref=claim["node_id"],
                verdict="upheld",
                settle=True,
                run_id="ssr",
            )

    def test_mcp_rule_allows_a_cross_author_ruling(self):
        # The positive case: a genuine objection from a DIFFERENT author unlocks
        # the ruling, so the distinct-adversary floor does not block real debate.
        claim = self._call(
            "sparkle_add_node",
            node_type="claim",
            title="Cross-author claim",
            content="the proposer's claim",
            run_id="xa",
            agent_role="proposer",
        )
        self._call(
            "sparkle_branch",
            from_ref=claim["node_id"],
            template="objection",
            title="Critic objection",
            content="a real objection from a different author",
            run_id="xa",
            agent_role="critic",
        )
        result = self._call(
            "sparkle_rule",
            claim_ref=claim["node_id"],
            verdict="upheld",
            settle=True,
            run_id="xa",
        )
        self.assertIn("decision", result)


if __name__ == "__main__":
    unittest.main()
