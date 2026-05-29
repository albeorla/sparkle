"""CHANGE C: the run-review surface now captures the WHOLE autonomous loop.

Plain English: before this change, the final harvested takeaway and the link
that ties it back to the claim it summarized fell OUTSIDE the run's accounting.
``ops.add_node`` could stamp a run id onto the new node but never onto the edge
it fused in the same call, so a synthesis node's ``produced`` link was invisible
to a run review (run_summary / run_diff) and to a rollback. A reviewer looking at
"everything this autonomous run touched" would see a synthesis node hanging in
space with no recorded connection to the debate that produced it, and a rollback
of the run would leave that connecting edge dangling.

CHANGE C closes that hole. ``ops.add_node`` gained an optional ``run_id`` channel
(default ``None`` = byte-identical) that, like ``add_branch`` and ``rule`` already
do, stamps the run id onto BOTH the new node's metadata AND the fused link edge.
The harness synthesizer move now passes ``run_id=`` so the synthesis ``produced``
edge lands in the run region. This file proves three things end to end:

  1. THE CHANNEL ITSELF: add_node with a run_id stamps the node AND its fused link
     edge; default None leaves both blobs untouched (the byte-identical floor that
     keeps human-CLI writes out of any run region).
  2. THE WHOLE LOOP: a full autonomous-shaped debate (claim, objection, decision,
     ratified claim, synthesis node, plus every edge — contradicts, evaluates x2,
     supersedes, AND the synthesis ``produced`` edge) all carry one run id, so a
     run summary/diff sees the complete loop and a rollback reaches every member.
  3. THE ACCEPTED LIMIT: a deduped re-proposal of the SAME seed stays attributed
     to its FIRST run's id (frozen nodes are not re-stamped), documented and
     pinned so the boundary is explicit, not a silent surprise.

Conventions match tests/test_run_plumbing.py and tests/test_runs.py: pure-stdlib
``unittest``, each test isolates its graph under a ``tempfile.TemporaryDirectory``
(no shared ``.sparkle`` state), no network, no third-party packages, no real
claude/codex CLI invocation. The run-region helpers are a verbatim mirror of the
MCP run filter (a node/edge is in run R iff ``metadata.run_id == R``), driving
``ops`` directly so the plumbing is proven even when the MCP SDK is absent.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from src.sparkle import ops
from src.sparkle.graph import GraphStore


# ---------------------------------------------------------------------------
# Run-region filter, mirrored from tests/test_run_plumbing.py:56-117 (which in
# turn mirrors mcp_server.py): a node/edge belongs to run R iff its metadata's
# run_id == R. _node_view / _edge_view both surface metadata, so a run_id stamped
# by ops.add_node lands here exactly as it does for the product's run surface.
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


class RunCompletenessBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # --- The five autonomous-shaped moves, driven through ops the way the
    #     harness RoleAgent._dispatch does. Each carries the run_id, so the whole
    #     loop lands in one region. ---

    def propose(self, *, run_id: str, title: str, content: str) -> dict:
        """PROPOSE: a run-tagged, model-authored claim (harness.py:620-634)."""
        return ops.add_node(
            self.store,
            node_type="claim",
            title=title,
            content=content,
            author="proposer",
            metadata={"run_id": run_id, "agent_role": "proposer", "provisional": True},
            model_authored=True,
        )

    def attack(self, *, run_id: str, target: str, title: str, content: str) -> dict:
        """OBJECT: a run-tagged objection branch (harness.py:646-656)."""
        return ops.add_branch(
            self.store,
            from_ref=target,
            template="objection",
            title=title,
            content=content,
            author="critic",
            model_authored=True,
            run_id=run_id,
        )

    def harvest(self, *, run_id: str, target: str, title: str, content: str) -> dict:
        """HARVEST: the synthesizer move, including run_id= so the new add_node
        channel forwards the stamp to the fused 'produced' edge (harness.py:719-734).
        """
        return ops.add_node(
            self.store,
            node_type="synthesis",
            title=title,
            content=content,
            author="synthesizer",
            link_to=target,
            relation="produced",
            metadata={
                "run_id": run_id,
                "agent_role": "synthesizer",
                "provisional": True,
            },
            model_authored=True,
            run_id=run_id,
        )


# ===========================================================================
# 1. THE CHANNEL: ops.add_node's run_id stamps BOTH the node and its fused edge
# ===========================================================================


class AddNodeRunIdChannelTests(RunCompletenessBase):
    def test_run_id_stamps_the_node_metadata(self) -> None:
        """A linkless add_node with run_id stamps the new node's metadata.

        The proposer path stamps the node via an explicit metadata blob, but the
        run_id= kwarg must do the same on its own so any caller (e.g. a future
        linkless run write) lands in the region without hand-building metadata.
        """
        result = ops.add_node(
            self.store,
            node_type="claim",
            title="Standalone",
            content="body",
            author="proposer",
            run_id="chan",
        )
        self.assertEqual(result["metadata"]["run_id"], "chan")
        # Persisted, not just on the return view.
        self.assertEqual(
            self.store.get_node(result["node_id"])["metadata"]["run_id"], "chan"
        )

    def test_run_id_forwards_to_the_fused_link_edge(self) -> None:
        """The heart of CHANGE C: the fused 'produced' edge carries the run_id.

        Before this, add_node stamped only the node; the link edge it fused in the
        SAME call got no metadata, so the synthesis->claim connection was invisible
        to the run surface. Now both the synthesis node AND the produced edge are
        in the region.
        """
        claim = self.propose(run_id="chan", title="Claim", content="c")
        syn = ops.add_node(
            self.store,
            node_type="synthesis",
            title="Takeaway",
            content="the synthesis",
            author="synthesizer",
            link_to=claim["node_id"],
            relation="produced",
            run_id="chan",
        )
        # The synthesis NODE is stamped.
        self.assertEqual(self.store.get_node(syn["node_id"])["metadata"]["run_id"], "chan")
        # The fused produced EDGE is stamped too. The link confirmation view is a
        # spoken-word gloss with no metadata key, so we read the stored edge via
        # the run-region filter (the surface run_summary/run_diff actually use).
        self.assertEqual(syn["link"]["relation"], "produced")
        produced = [e for e in _run_edges(self.store, "chan") if e["relation"] == "produced"]
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["metadata"]["run_id"], "chan")
        # And it is the synthesis->claim edge (the new node is the source).
        self.assertEqual(produced[0]["from_id"], syn["node_id"])
        self.assertEqual(produced[0]["to_id"], claim["node_id"])

    def test_explicit_metadata_run_id_wins_over_the_kwarg_on_the_node(self) -> None:
        """An explicit metadata['run_id'] is preserved (setdefault), not clobbered.

        The harness synthesizer passes BOTH metadata={'run_id': r} and run_id=r,
        which must agree; the seam stamps the node via setdefault so a caller's
        explicit value wins. If the two disagree, the explicit metadata value is
        what lands on the node — proving the kwarg never silently overrides it.
        """
        claim = self.propose(run_id="kw", title="Claim", content="c")
        syn = ops.add_node(
            self.store,
            node_type="synthesis",
            title="T",
            content="t",
            author="synthesizer",
            link_to=claim["node_id"],
            relation="produced",
            metadata={"run_id": "explicit", "agent_role": "synthesizer"},
            run_id="kw",
        )
        node_meta = self.store.get_node(syn["node_id"])["metadata"]
        self.assertEqual(node_meta["run_id"], "explicit")
        self.assertEqual(node_meta["agent_role"], "synthesizer")
        # The fused edge takes the run_id= kwarg value (the edge stamp uses the
        # kwarg directly, not the node's metadata blob).
        produced = [
            e for e in ops.list_edges(self.store)
            if e["relation"] == "produced" and e["from_id"] == syn["node_id"]
        ][0]
        self.assertEqual(produced["metadata"]["run_id"], "kw")

    def test_default_none_leaves_node_and_edge_byte_identical(self) -> None:
        """Guardrail: NO run_id kwarg leaves both the node AND the fused edge clean.

        The human-CLI path passes no run_id. If a default stamp ever leaked onto
        the node or the link edge, this would flip and break run isolation (the
        same floor test_run_plumbing.py pins for add_branch/rule).
        """
        # A plain human-CLI synthesis-with-link write: no run_id anywhere.
        claim = ops.add_node(
            self.store, node_type="claim", title="Plain claim", content="no run"
        )
        syn = ops.add_node(
            self.store,
            node_type="synthesis",
            title="Plain synthesis",
            content="no run either",
            link_to=claim["node_id"],
            relation="produced",
        )
        self.assertNotIn("run_id", self.store.get_node(syn["node_id"])["metadata"])
        produced = [
            e for e in ops.list_edges(self.store)
            if e["relation"] == "produced" and e["from_id"] == syn["node_id"]
        ][0]
        self.assertNotIn("run_id", produced.get("metadata", {}))
        # Neither belongs to any run region.
        self.assertEqual(_run_nodes(self.store, "any"), [])
        self.assertEqual(_run_edges(self.store, "any"), [])

    def test_link_to_and_relation_must_still_come_as_a_pair_with_run_id(self) -> None:
        """The run_id channel does not relax the link_to/relation pairing rule.

        run_id is orthogonal to the fused link; supplying a relation without a
        target (or vice versa) still raises, so the new channel cannot be used to
        smuggle a half-specified edge.
        """
        with self.assertRaises(ValueError):
            ops.add_node(
                self.store,
                node_type="synthesis",
                title="Dangling",
                content="no target",
                relation="produced",
                run_id="r",
            )


# ===========================================================================
# 2. THE WHOLE LOOP: an autonomous-shaped debate's nodes AND edges all land in
#    the run region, and rollback reaches every member.
# ===========================================================================


class FullLoopInRunRegionTests(RunCompletenessBase):
    def _full_loop(self, run_id: str) -> dict:
        """propose -> object -> rule(settle) -> harvest, all under one run_id.

        Returns the four move handles so individual ids can be asserted. This is
        the exact shape the harness produces: a claim, a cross-author objection,
        a settled ruling (decision node + ratified claim version), and a synthesis
        that 'produced' off the claim.
        """
        claim = self.propose(run_id=run_id, title="Big claim", content="debated")
        objection = self.attack(
            run_id=run_id, target=claim["node_id"], title="Counter", content="actually no"
        )
        ruling = ops.rule(
            self.store,
            claim["node_id"],
            verdict="accept",
            rationale="objection rebutted",
            settle=True,
            author="judge",
            run_id=run_id,
            require_distinct_adversary=True,
        )
        synthesis = self.harvest(
            run_id=run_id,
            target=claim["node_id"],
            title="Takeaway",
            content="what we learned",
        )
        return {
            "claim": claim,
            "objection": objection,
            "ruling": ruling,
            "synthesis": synthesis,
        }

    def test_summary_counts_every_node_type_in_the_loop(self) -> None:
        """run_summary over the full loop counts claim, objection, decision, synthesis.

        This is the contract CHANGE C completes: before, the synthesis node could
        be tagged but its 'produced' edge was not, so the loop's edge accounting
        was short by one. Now the WHOLE loop — every node and every edge — is in
        the region under a single run id.
        """
        loop = self._full_loop("whole")
        summary = _run_summary(self.store, "whole")

        # Five nodes: original claim, objection, decision, ratified claim version,
        # and the synthesis node.
        self.assertEqual(summary["node_count"], 5)
        self.assertEqual(
            summary["nodes_by_type"],
            {"claim": 2, "objection": 1, "decision": 1, "synthesis": 1},
        )
        # Statuses: original claim + objection + decision active (3), ratified
        # claim version ratified (1), synthesis active (1).
        self.assertEqual(
            summary["nodes_by_status"], {"active": 4, "ratified": 1}
        )
        # Sanity: the four move handles are all in the region.
        run_ids = {n["node_id"] for n in _run_nodes(self.store, "whole")}
        self.assertIn(loop["claim"]["node_id"], run_ids)
        self.assertIn(loop["objection"]["node_id"], run_ids)
        self.assertIn(loop["ruling"]["decision"]["node_id"], run_ids)
        self.assertIn(loop["ruling"]["ratified_claim"]["node_id"], run_ids)
        self.assertIn(loop["synthesis"]["node_id"], run_ids)

    def test_summary_counts_every_edge_including_the_produced_edge(self) -> None:
        """run_summary counts contradicts, evaluates x2, supersedes, AND produced.

        The 'produced' edge is the one CHANGE C rescued from invisibility. Without
        the fix the edge count would be 4 (contradicts + 2 evaluates + supersedes)
        and the synthesis->claim connection would be missing from the region.
        """
        self._full_loop("edges")
        summary = _run_summary(self.store, "edges")

        # Five edges: the objection's contradicts, two evaluates (judge looked at
        # the original AND the ratified version), the ratified->original supersedes,
        # and the synthesis->claim produced edge.
        self.assertEqual(summary["edge_count"], 5)
        self.assertEqual(
            summary["edges_by_relation"],
            {"contradicts": 1, "evaluates": 2, "supersedes": 1, "produced": 1},
        )

    def test_diff_returns_the_exact_loop_membership_all_stamped(self) -> None:
        """run_diff returns the exact five-node set, and every node/edge is stamped.

        Guards against leakage in either direction: no extra node sneaks into the
        region, and no member is missing the run_id stamp.
        """
        loop = self._full_loop("d")
        diff = _run_diff(self.store, "d")
        node_ids = {n["node_id"] for n in diff["nodes"]}
        self.assertEqual(
            node_ids,
            {
                loop["claim"]["node_id"],
                loop["objection"]["node_id"],
                loop["ruling"]["decision"]["node_id"],
                loop["ruling"]["ratified_claim"]["node_id"],
                loop["synthesis"]["node_id"],
            },
        )
        for n in diff["nodes"]:
            self.assertEqual(n["metadata"]["run_id"], "d")
        for e in diff["edges"]:
            self.assertEqual(e["metadata"]["run_id"], "d")
        # The produced edge is genuinely present in the diff's edge set.
        self.assertTrue(any(e["relation"] == "produced" for e in diff["edges"]))

    def test_produced_edge_links_synthesis_to_the_claim_it_summarized(self) -> None:
        """The rescued edge actually connects the synthesis back to the claim.

        Capturing the edge is only useful if it carries the right lineage: the
        synthesis node is the source and the original claim is the target, so a
        reviewer can trace the takeaway to the debate that produced it.
        """
        loop = self._full_loop("link")
        produced = [
            e for e in _run_edges(self.store, "link") if e["relation"] == "produced"
        ]
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["from_id"], loop["synthesis"]["node_id"])
        self.assertEqual(produced[0]["to_id"], loop["claim"]["node_id"])

    def test_rollback_reaches_the_synthesis_and_its_produced_edge(self) -> None:
        """Rollback now reaches the synthesis node because it is in the region.

        Before CHANGE C, even if the synthesis node were tagged, the rollback walks
        the run's NODES — so the synthesis node is abandoned along with the still-
        active proposer/critic/synthesizer work, while the binding ruling stands.
        This proves the harvested takeaway is not orphaned after a run is undone.
        """
        loop = self._full_loop("undo")
        synthesis_id = loop["synthesis"]["node_id"]
        decision_id = loop["ruling"]["decision"]["node_id"]
        objection_id = loop["objection"]["node_id"]
        ratified_id = loop["ruling"]["ratified_claim"]["node_id"]
        claim_id = loop["claim"]["node_id"]

        result = _rollback_run(self.store, "undo")
        rolled_olds = {r["old_id"] for r in result["rolled_back"]}

        # The synthesis node is active and not superseded by a terminal -> it rolls
        # back. So do the still-active objection and decision.
        self.assertIn(synthesis_id, rolled_olds)
        self.assertIn(objection_id, rolled_olds)
        self.assertIn(decision_id, rolled_olds)
        # The ratified version is terminal -> skipped; the original claim is
        # already superseded by it -> skipped. The binding ruling stands.
        self.assertNotIn(ratified_id, rolled_olds)
        self.assertNotIn(claim_id, rolled_olds)
        self.assertEqual(
            self.store.get_node(ratified_id)["status"],
            "ratified",
            "rollback must never revert a settled ratification",
        )
        # Every rolled-back node is now superseded by an abandoned version, so the
        # synthesis takeaway is cleanly undone, not left dangling.
        self.assertEqual(
            sorted(r["status"] for r in result["rolled_back"]),
            ["abandoned", "abandoned", "abandoned"],
        )

    def test_rollback_over_the_full_loop_converges(self) -> None:
        """Re-rolling the whole loop is idempotent (no forked abandoned siblings)."""
        self._full_loop("conv")
        first = _rollback_run(self.store, "conv")
        # objection + decision + synthesis are the three still-active rollables.
        self.assertEqual(first["count"], 3)
        second = _rollback_run(self.store, "conv")
        self.assertEqual(
            second["count"], 0, "rollback over the loop must converge to a no-op"
        )

    def test_loop_without_synthesis_edge_stamp_would_lose_the_edge(self) -> None:
        """Regression contrast: the OLD (un-run_id'd) harvest leaves the edge out.

        Drives the synthesizer move WITHOUT the run_id= kwarg (the pre-CHANGE-C
        behavior, reproduced by passing only the node-metadata run_id). The
        synthesis NODE still lands in the region (its metadata carries run_id), but
        its 'produced' EDGE does NOT — exactly the hole CHANGE C closes. Pinning
        this makes the fix's value explicit: the kwarg is what rescues the edge.
        """
        claim = self.propose(run_id="old", title="Claim", content="c")
        self.attack(run_id="old", target=claim["node_id"], title="Obj", content="no")
        # Old-style harvest: node metadata carries run_id, but NO run_id= kwarg, so
        # the fused edge gets no stamp.
        syn = ops.add_node(
            self.store,
            node_type="synthesis",
            title="Takeaway",
            content="t",
            author="synthesizer",
            link_to=claim["node_id"],
            relation="produced",
            metadata={"run_id": "old", "agent_role": "synthesizer"},
            model_authored=True,
        )
        # The synthesis node IS in the region (metadata stamp).
        self.assertEqual(self.store.get_node(syn["node_id"])["metadata"]["run_id"], "old")
        self.assertIn(syn["node_id"], {n["node_id"] for n in _run_nodes(self.store, "old")})
        # But its produced edge is NOT (no kwarg = no edge stamp) — the loop's
        # synthesis link is invisible to the run surface, the bug CHANGE C fixes.
        produced_in_region = [
            e for e in _run_edges(self.store, "old") if e["relation"] == "produced"
        ]
        self.assertEqual(produced_in_region, [])
        # The edge exists in the graph, just untagged.
        all_produced = [
            e for e in ops.list_edges(self.store)
            if e["relation"] == "produced" and e["from_id"] == syn["node_id"]
        ]
        self.assertEqual(len(all_produced), 1)
        self.assertNotIn("run_id", all_produced[0].get("metadata", {}))


# ===========================================================================
# 3. THE ACCEPTED LIMIT: a deduped re-proposal of the SAME seed stays attributed
#    to its FIRST run (frozen nodes are not re-stamped). Documented and pinned.
# ===========================================================================


class DedupRepeatSeedAttributionLimitTests(RunCompletenessBase):
    """Pin the ONE residual gap the spec accepts (ops.add_node docstring:356-362).

    When run #2 re-proposes a byte-identical claim (same type/title/content), the
    exact-fingerprint dedup returns the EXISTING node from run #1, which keeps run
    #1's run_id. Nodes are frozen by contract, so the seam deliberately does NOT
    mutate the existing node to re-attribute it. The result: a re-proposed seed
    shows up in run #1's region, not run #2's, and run #2's summary does not count
    that claim. These tests make the boundary explicit rather than a silent
    surprise during a run review.
    """

    def test_deduped_reproposal_keeps_the_first_runs_attribution(self) -> None:
        """A re-proposal of the same seed returns run #1's node, with run #1's id."""
        first = self.propose(run_id="run1", title="Seed", content="identical body")
        self.assertTrue(first["created"])
        self.assertEqual(first["metadata"]["run_id"], "run1")

        # Run #2 re-proposes the IDENTICAL seed.
        second = self.propose(run_id="run2", title="Seed", content="identical body")
        # It is a dedup hit: the same node id comes back, NOT created.
        self.assertFalse(second["created"])
        self.assertEqual(second["node_id"], first["node_id"])
        # The accepted limit: the node still carries run #1's id, not run #2's.
        self.assertEqual(second["metadata"]["run_id"], "run1")
        self.assertEqual(
            self.store.get_node(first["node_id"])["metadata"]["run_id"], "run1"
        )

    def test_reproposed_seed_stays_in_run_one_region_not_run_two(self) -> None:
        """The deduped seed is counted by run #1's summary, never run #2's.

        This is the user-visible consequence of the accepted limit: a reviewer of
        run #2 will NOT see the re-proposed claim in run #2's region — it stayed
        attributed to run #1. Pinned so the boundary is documented behavior.
        """
        self.propose(run_id="run1", title="Seed", content="same")
        self.propose(run_id="run2", title="Seed", content="same")

        # Run #1's region still contains the seed claim.
        run1_nodes = _run_nodes(self.store, "run1")
        self.assertEqual(len(run1_nodes), 1)
        self.assertEqual(run1_nodes[0]["node_type"], "claim")
        # Run #2's region is EMPTY for that claim — the dedup hit was never
        # re-attributed, so run #2 sees no node for the seed it re-proposed.
        run2_nodes = _run_nodes(self.store, "run2")
        self.assertEqual(run2_nodes, [])
        self.assertEqual(_run_summary(self.store, "run2")["node_count"], 0)

    def test_frozen_node_is_not_mutated_to_paper_over_the_gap(self) -> None:
        """The seam never mutates the frozen node's content-address or run_id stamp.

        The content-addressed id and the first run's stamp are stable across the
        re-proposal: the dedup returns the SAME id and the same metadata blob, so
        no frozen-node mutation happened (the deliberate non-fix the spec accepts).
        """
        first = self.propose(run_id="run1", title="Seed", content="frozen body")
        first_id = first["node_id"]
        first_meta = dict(self.store.get_node(first_id)["metadata"])

        second = self.propose(run_id="run2", title="Seed", content="frozen body")

        # The dedup returns the SAME content-addressed id (no forked copy, no
        # remint), and the stored metadata blob is byte-identical to run #1's.
        self.assertEqual(second["node_id"], first_id, "content-addressed id must be stable")
        after_meta = self.store.get_node(first_id)["metadata"]
        self.assertEqual(
            after_meta, first_meta, "frozen node metadata must not be mutated"
        )
        # Only one node exists for the seed (dedup did not fork a second copy).
        seed_nodes = [
            n for n in ops.list_nodes(self.store)
            if n["title"] == "Seed" and n["node_type"] == "claim"
        ]
        self.assertEqual(len(seed_nodes), 1)


if __name__ == "__main__":
    unittest.main()
