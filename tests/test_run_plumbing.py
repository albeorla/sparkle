"""Run-region plumbing: the ATTACK and RULING moves now carry the run_id.

This file closes the Phase-1 *under-reporting* gap that ``tests/test_runs.py``
deliberately pinned as a defect. There, an objection written with
``ops.add_branch`` carried NO ``run_id`` (the comment at test_runs.py:197-224
spells it out: "the objection branch's edge is NOT run-tagged (add_branch stamps
no run_id)"), and ``ops.rule`` threaded no ``run_id`` either. So a full
autonomous loop — propose the claim, ATTACK it with an objection, then JUDGE it —
left the objection and the decision INVISIBLE to the run surface
(``run_summary`` / ``run_diff``). The run looked like it contained only the
proposer's own claim; the adversary's objection and the judge's verdict simply
were not in the region. That is exactly the "a model could ratify its own claim
and the report would never show it was challenged" hazard Phase 2 had to fix.

Phase 2 threads an optional ``run_id`` (and, for ``add_branch``, a ``metadata``
blob) through both moves and stamps it onto:

  * ATTACK  (``ops.add_branch``): the objection NODE and its ``contradicts`` EDGE.
  * RULING  (``ops.rule``):       the decision NODE, the ``evaluates`` EDGE to the
                                  original claim, the ratified claim NODE, the
                                  ``supersedes`` EDGE, and (when settled) the
                                  second ``evaluates`` EDGE to the ratified claim.

So a single run_id now captures the WHOLE debate, not just the proposer's claim.
These tests prove that end to end, then prove rollback over that run is coherent
(supersedes the run's still-active work, leaves frozen originals, converges).

Conventions match tests/test_cli.py and tests/test_runs.py: pure-stdlib
``unittest``, every test isolates its graph under a ``tempfile.TemporaryDirectory``
(no shared ``.sparkle`` state), no network, no third-party packages — this runs
in the zero-dependency base suite. The run-region helpers below are a verbatim
mirror of the MCP run filter (mcp_server.py / test_runs.py:62-131): a node/edge
belongs to a run iff ``metadata.run_id == run_id``. We drive ``ops`` directly so
the plumbing is proven even when the MCP SDK is absent.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from src.sparkle import ops
from src.sparkle.graph import GraphStore


# ---------------------------------------------------------------------------
# Run-region filter, mirrored verbatim from tests/test_runs.py:62-131 (which in
# turn mirrors mcp_server.py). A node/edge is "in run R" iff metadata.run_id == R.
# These read off _node_view / _edge_view, which both surface metadata, so a
# run_id stamped by ops.add_branch / ops.rule is visible here exactly as it is to
# the product's run_summary / run_diff / rollback_run.
# ---------------------------------------------------------------------------


def _run_nodes(store: GraphStore, run_id: str) -> list[dict]:
    return [
        node
        for node in ops.list_nodes(store)
        if node.get("metadata", {}).get("run_id") == run_id
    ]


def _run_edges(store: GraphStore, run_id: str) -> list[dict]:
    return [
        edge
        for edge in ops.list_edges(store)
        if edge.get("metadata", {}).get("run_id") == run_id
    ]


def _run_summary(store: GraphStore, run_id: str) -> dict:
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
    return {
        "run_id": run_id,
        "nodes": _run_nodes(store, run_id),
        "edges": _run_edges(store, run_id),
    }


def _rollback_run(store: GraphStore, run_id: str) -> dict:
    """Mirror of sparkle_rollback_run (test_runs.py:111-131 / mcp_server.py).

    Supersede every still-active run node with an ``abandoned`` version; skip
    nodes already terminal and originals already superseded by a terminal version
    so the pass is idempotent and never reverts a binding ruling.
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
# Shared base case: one isolated temp store per test, plus a tiny run-loop builder.
# ---------------------------------------------------------------------------


class RunPlumbingBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def propose(self, *, run_id: str, title: str, content: str, author: str = "proposer") -> dict:
        """The PROPOSE move: a run-tagged, model-authored claim (the way the loop does)."""
        return ops.add_node(
            self.store,
            node_type="claim",
            title=title,
            content=content,
            author=author,
            metadata={"run_id": run_id, "agent_role": "proposer", "provisional": True},
            model_authored=True,
        )

    def attack(
        self,
        *,
        run_id: str,
        target: str,
        title: str,
        content: str,
        author: str = "critic",
    ) -> dict:
        """The ATTACK move: a run-tagged objection branch (add_branch with run_id)."""
        return ops.add_branch(
            self.store,
            from_ref=target,
            template="objection",
            title=title,
            content=content,
            author=author,
            model_authored=True,
            run_id=run_id,
        )


# ===========================================================================
# 1. ATTACK: ops.add_branch threads run_id onto the objection NODE and EDGE
# ===========================================================================


class AttackCarriesRunIdTests(RunPlumbingBase):
    def test_objection_node_and_edge_both_carry_the_run_id(self) -> None:
        """The previously-deferred gap, directly: an attack now lands in the run.

        In Phase 1 the objection branch carried no run_id, so run accounting saw
        only the proposer's claim. Now ``add_branch(run_id=...)`` stamps BOTH the
        objection node's metadata AND its ``contradicts`` edge's metadata, so the
        adversary's attack is part of the run region.
        """
        claim = self.propose(run_id="r", title="Claim", content="under debate")
        attack = self.attack(
            run_id="r", target=claim["node_id"], title="Objection", content="wrong because"
        )

        # The objection NODE carries the run stamp.
        self.assertEqual(attack["metadata"]["run_id"], "r")
        self.assertEqual(attack["node_type"], "objection")
        stored = self.store.get_node(attack["node_id"])
        self.assertEqual(stored["metadata"]["run_id"], "r")

        # The templated contradicts EDGE carries the run stamp too. The link
        # confirmation view is a spoken-word gloss with no metadata key, so we
        # read the stored edge through the run-region filter (the surface the
        # product's run_summary/run_diff actually use).
        self.assertEqual(attack["link"]["relation"], "contradicts")
        run_edges = _run_edges(self.store, "r")
        contradicts = [e for e in run_edges if e["relation"] == "contradicts"]
        self.assertEqual(len(contradicts), 1)
        self.assertEqual(contradicts[0]["metadata"]["run_id"], "r")

        # And it is the run's objection->claim edge (child contradicts parent).
        self.assertEqual(attack["link"]["from_id"], attack["node_id"])
        self.assertEqual(attack["link"]["to_id"], claim["node_id"])
        self.assertEqual(contradicts[0]["from_id"], attack["node_id"])
        self.assertEqual(contradicts[0]["to_id"], claim["node_id"])

    def test_run_summary_after_propose_then_attack_reports_the_objection(self) -> None:
        """run_summary over the run now shows the claim AND the objection.

        This is the heart of the fix: before, summary would report only
        ``{"claim": 1}`` and ``edge_count == 0`` because the objection was
        untagged. Now the objection node and its contradicts edge are counted.
        """
        claim = self.propose(run_id="loop", title="Disputed", content="claim body")
        self.attack(
            run_id="loop", target=claim["node_id"], title="Counterpoint", content="no"
        )

        summary = _run_summary(self.store, "loop")
        # Two nodes in the run now: the proposer's claim + the critic's objection.
        self.assertEqual(summary["node_count"], 2)
        self.assertEqual(summary["nodes_by_type"], {"claim": 1, "objection": 1})
        # And the contradicts edge is in the region (it was invisible before).
        self.assertEqual(summary["edge_count"], 1)
        self.assertEqual(summary["edges_by_relation"], {"contradicts": 1})

    def test_add_branch_metadata_blob_merges_with_the_run_stamp(self) -> None:
        """A caller-supplied metadata blob is preserved alongside run_id.

        The autonomous engine stamps an ``agent_role`` (and ``provisional``) on
        the objection so the region carries provenance; run_id must not clobber it.
        """
        claim = self.propose(run_id="r", title="Claim", content="c")
        attack = ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Obj",
            content="no",
            author="critic",
            model_authored=True,
            run_id="r",
            metadata={"agent_role": "critic", "provisional": True},
        )
        meta = self.store.get_node(attack["node_id"])["metadata"]
        self.assertEqual(meta["run_id"], "r")
        self.assertEqual(meta["agent_role"], "critic")
        self.assertTrue(meta["provisional"])
        # The stored edge carries the same merged blob (read via the run filter,
        # since the link confirmation view has no metadata key).
        edge_meta = [e for e in _run_edges(self.store, "r") if e["relation"] == "contradicts"][0]["metadata"]
        self.assertEqual(edge_meta["run_id"], "r")
        self.assertEqual(edge_meta["agent_role"], "critic")

    def test_untagged_attack_stays_out_of_the_run_region(self) -> None:
        """Guardrail: omitting run_id must leave the objection un-tagged.

        This is the byte-identical default the existing test_runs.py pins. If a
        default stamp ever leaked in, this would flip and break run isolation.
        """
        claim = self.propose(run_id="r", title="Claim", content="c")
        attack = ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Untagged objection",
            content="no run here",
            author="critic",
            model_authored=True,
        )
        self.assertNotIn("run_id", self.store.get_node(attack["node_id"])["metadata"])
        # The objection's stored edge is not in any run region either.
        self.assertEqual(_run_edges(self.store, "r"), [])
        # The run sees only the proposer's claim; the objection is excluded.
        summary = _run_summary(self.store, "r")
        self.assertEqual(summary["node_count"], 1)
        self.assertEqual(summary["edge_count"], 0)


# ===========================================================================
# 2. RULING: ops.rule threads run_id onto the decision, evaluates edges,
#    ratified claim, and supersedes edge — the JUDGE move joins the run.
# ===========================================================================


class RulingCarriesRunIdTests(RunPlumbingBase):
    def test_unsettled_ruling_stamps_decision_node_and_evaluates_edge(self) -> None:
        """A non-binding verdict still lands in the run region.

        Even without settling, the decision NODE and its ``evaluates`` EDGE to the
        claim must carry the run_id so "the judge looked at this" is in the report.
        """
        claim = self.propose(run_id="r", title="Claim", content="c")
        self.attack(run_id="r", target=claim["node_id"], title="Obj", content="no")
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="needs_work",
            rationale="not yet",
            settle=False,
            author="judge",
            run_id="r",
        )
        self.assertFalse(ruling["settled"])
        decision = self.store.get_node(ruling["decision"]["node_id"])
        self.assertEqual(decision["metadata"]["run_id"], "r")
        self.assertEqual(ruling["decision_link"]["relation"], "evaluates")
        # The stored evaluates edge carries the run stamp (read via the run
        # filter; the link confirmation view has no metadata key).
        evaluates = [e for e in _run_edges(self.store, "r") if e["relation"] == "evaluates"]
        self.assertEqual(len(evaluates), 1)
        self.assertEqual(evaluates[0]["metadata"]["run_id"], "r")

    def test_settled_ruling_stamps_ratified_claim_and_supersedes_edge(self) -> None:
        """A binding ratification puts the WHOLE decision into the run region.

        The decision node, the evaluates edge to the original claim, the ratified
        claim version, the supersedes edge (ratified --supersedes--> original), and
        the second evaluates edge to the ratified version must all carry run_id.
        """
        claim = self.propose(run_id="r", title="Claim", content="c")
        self.attack(run_id="r", target=claim["node_id"], title="Obj", content="no")
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            rationale="the objection fails",
            settle=True,
            author="judge",
            run_id="r",
        )
        self.assertTrue(ruling["settled"])

        # Ratified claim version carries the run stamp and the ratified status.
        ratified = self.store.get_node(ruling["ratified_claim"]["node_id"])
        self.assertEqual(ratified["status"], "ratified")
        self.assertEqual(ratified["metadata"]["run_id"], "r")

        # The supersedes edge (ratified -> original) is in the run region.
        run_edges = _run_edges(self.store, "r")
        supersedes = [e for e in run_edges if e["relation"] == "supersedes"]
        self.assertEqual(len(supersedes), 1)
        self.assertEqual(supersedes[0]["from_id"], ruling["ratified_claim"]["node_id"])
        self.assertEqual(supersedes[0]["to_id"], claim["node_id"])

    def test_full_loop_run_summary_reports_claim_objection_and_decision(self) -> None:
        """END TO END: propose -> ATTACK -> RULE(settle) all share one run_id.

        This is the contract the whole phase exists for. Before Phase 2, a run
        summary over a settled debate showed only the proposer's claim. Now it
        shows the proposer's claim, the critic's objection, the judge's decision,
        and the ratified claim version — the entire loop, under a single run_id.
        """
        claim = self.propose(run_id="loop", title="Big claim", content="debated")
        self.attack(
            run_id="loop", target=claim["node_id"], title="Counter", content="actually no"
        )
        ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            rationale="objection rebutted",
            settle=True,
            author="judge",
            run_id="loop",
        )

        summary = _run_summary(self.store, "loop")
        # Four nodes: original claim, objection, decision, ratified claim version.
        self.assertEqual(summary["node_count"], 4)
        self.assertEqual(
            summary["nodes_by_type"],
            {"claim": 2, "objection": 1, "decision": 1},
        )
        # Three active nodes (original claim, objection, decision) and one
        # ratified (the superseding claim version the ruling wrote).
        self.assertEqual(summary["nodes_by_status"], {"active": 3, "ratified": 1})

        # Edges: the contradicts objection, two evaluates (orig + ratified), and
        # the supersedes link. All four carry the run_id.
        self.assertEqual(summary["edge_count"], 4)
        self.assertEqual(
            summary["edges_by_relation"],
            {"contradicts": 1, "evaluates": 2, "supersedes": 1},
        )

    def test_full_loop_run_diff_returns_every_member_of_the_region(self) -> None:
        """run_diff over the loop returns the exact node set, all run-stamped."""
        claim = self.propose(run_id="d", title="Claim", content="body")
        attack = self.attack(
            run_id="d", target=claim["node_id"], title="Obj", content="no"
        )
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            settle=True,
            author="judge",
            run_id="d",
        )

        diff = _run_diff(self.store, "d")
        node_ids = {n["node_id"] for n in diff["nodes"]}
        self.assertEqual(
            node_ids,
            {
                claim["node_id"],
                attack["node_id"],
                ruling["decision"]["node_id"],
                ruling["ratified_claim"]["node_id"],
            },
        )
        # Every node and edge in the diff is genuinely stamped (no leakage).
        for n in diff["nodes"]:
            self.assertEqual(n["metadata"]["run_id"], "d")
        for e in diff["edges"]:
            self.assertEqual(e["metadata"]["run_id"], "d")

    def test_unstamped_ruling_writes_no_run_id_of_its_own(self) -> None:
        """Guardrail: omitting run_id on rule() adds no stamp from the ruling.

        The human CLI path passes no run_id. To isolate the ruling's own stamping
        behavior we use an UN-tagged claim and objection (no inherited run_id), so
        the only way a run_id could appear on the decision or ratified version is
        if rule() stamped it. It must not: the decision node, the ratified claim
        version, and every edge the ruling wrote stay free of any run_id.
        """
        # Un-tagged claim + objection: a plain human-CLI debate, no run region.
        claim = ops.add_node(
            self.store, node_type="claim", title="Plain claim", content="no run"
        )
        ops.add_branch(
            self.store,
            from_ref=claim["node_id"],
            template="objection",
            title="Plain objection",
            content="disagree",
            author="critic",
        )
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            settle=True,
            author="judge",
        )
        # The decision and ratified claim carry NO run_id (the ruling added none,
        # and the claim it superseded had none to inherit).
        self.assertNotIn(
            "run_id", self.store.get_node(ruling["decision"]["node_id"])["metadata"]
        )
        self.assertNotIn(
            "run_id",
            self.store.get_node(ruling["ratified_claim"]["node_id"])["metadata"],
        )
        # No edge carries a run_id either: the ruling's evaluates/supersedes edges
        # are all un-tagged, so they belong to no run region.
        self.assertTrue(
            all(not e.get("metadata", {}).get("run_id") for e in ops.list_edges(self.store))
        )


# ===========================================================================
# 3. ROLLBACK over the run is coherent now that the whole loop is in the region
# ===========================================================================


class RollbackOverRunTests(RunPlumbingBase):
    def test_rollback_of_unjudged_loop_abandons_claim_and_objection(self) -> None:
        """Rolling back a propose+attack run supersedes BOTH with abandoned copies.

        Because the objection now carries the run_id, rollback reaches it too —
        in Phase 1 the untagged objection would have survived a rollback, leaving
        an orphaned attack in the graph. Now the whole attack region rolls back.
        """
        claim = self.propose(run_id="loop", title="Claim", content="x")
        attack = self.attack(
            run_id="loop", target=claim["node_id"], title="Obj", content="no"
        )

        result = _rollback_run(self.store, "loop")
        # Both the claim and the objection are still-active run nodes -> both roll.
        self.assertEqual(result["count"], 2)
        rolled_olds = {r["old_id"] for r in result["rolled_back"]}
        self.assertEqual(rolled_olds, {claim["node_id"], attack["node_id"]})
        self.assertEqual(
            sorted(r["status"] for r in result["rolled_back"]),
            ["abandoned", "abandoned"],
        )
        # Frozen originals are untouched.
        self.assertEqual(self.store.get_node(claim["node_id"])["status"], "active")
        self.assertEqual(self.store.get_node(attack["node_id"])["status"], "active")

    def test_rollback_skips_the_ratified_claim_so_it_never_reverts_a_ruling(self) -> None:
        """A settled run is rollback-coherent: rollback never un-settles a verdict.

        After rule(settle=True) the run region holds the original claim, the
        objection, the decision, and the ratified (terminal) claim version. The
        decision and ratified version are terminal-or-superseding, so rollback's
        skip-guards leave the binding ruling intact while still abandoning the
        still-active proposer/critic work. This proves the run_id stamp does not
        let rollback corrupt a finished debate.
        """
        claim = self.propose(run_id="done", title="Claim", content="c")
        attack = self.attack(
            run_id="done", target=claim["node_id"], title="Obj", content="no"
        )
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            settle=True,
            author="judge",
            run_id="done",
        )
        ratified_id = ruling["ratified_claim"]["node_id"]
        decision_id = ruling["decision"]["node_id"]

        result = _rollback_run(self.store, "done")

        rolled_olds = {r["old_id"] for r in result["rolled_back"]}
        # The ratified claim is terminal -> skipped. The original claim is already
        # superseded by that terminal version -> skipped. So neither the original
        # nor the ratified claim is reverted; the ruling stands.
        self.assertNotIn(ratified_id, rolled_olds)
        self.assertNotIn(claim["node_id"], rolled_olds)
        self.assertEqual(
            self.store.get_node(ratified_id)["status"],
            "ratified",
            "rollback must never revert a settled ratification",
        )
        # The ratified version is still terminal and in the region.
        ratified_run_nodes = {
            n["node_id"]: n["status"] for n in _run_nodes(self.store, "done")
        }
        self.assertEqual(ratified_run_nodes[ratified_id], "ratified")
        # The decision node is active (not terminal) and not yet superseded, so it
        # IS rolled back along with the still-active objection.
        self.assertIn(decision_id, rolled_olds)
        self.assertIn(attack["node_id"], rolled_olds)

    def test_rollback_over_loop_is_idempotent(self) -> None:
        """Re-rolling the loop converges to count 0 (no forked abandoned siblings)."""
        claim = self.propose(run_id="loop", title="Claim", content="x")
        self.attack(run_id="loop", target=claim["node_id"], title="Obj", content="no")

        first = _rollback_run(self.store, "loop")
        self.assertEqual(first["count"], 2)
        second = _rollback_run(self.store, "loop")
        self.assertEqual(
            second["count"], 0, "rollback over the run must converge to a no-op"
        )

    def test_rollback_only_touches_the_named_run(self) -> None:
        """A second run's objection must survive a rollback of the first run.

        Two debates share a graph but distinct run_ids. Rolling back run A must
        leave run B's claim AND objection active — proving the run_id stamp on the
        attack scopes rollback correctly, not graph-wide.
        """
        claim_a = self.propose(run_id="A", title="Claim A", content="a")
        attack_a = self.attack(
            run_id="A", target=claim_a["node_id"], title="Obj A", content="no"
        )
        claim_b = self.propose(run_id="B", title="Claim B", content="b")
        attack_b = self.attack(
            run_id="B", target=claim_b["node_id"], title="Obj B", content="no"
        )

        result = _rollback_run(self.store, "A")
        self.assertEqual(result["count"], 2)
        rolled_olds = {r["old_id"] for r in result["rolled_back"]}
        self.assertEqual(rolled_olds, {claim_a["node_id"], attack_a["node_id"]})

        # Run B is wholly untouched: its claim and objection are still active.
        self.assertEqual(self.store.get_node(claim_b["node_id"])["status"], "active")
        self.assertEqual(self.store.get_node(attack_b["node_id"])["status"], "active")
        # And run B's summary is unchanged: claim + objection still present.
        summary_b = _run_summary(self.store, "B")
        self.assertEqual(summary_b["nodes_by_type"], {"claim": 1, "objection": 1})


if __name__ == "__main__":
    unittest.main()
