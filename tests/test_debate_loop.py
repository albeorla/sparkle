"""End-to-end proof of Sparkle's adversarial debate loop.

The loop under test, in plain English: someone proposes a claim, an adversary
attacks it with an objection (a 'contradicts' edge into the claim), the referee
reports the live state, the judge rules (refusing to ratify anything that was
never attacked), a settled ruling writes a brand-new 'ratified' version of the
claim instead of mutating the frozen original, the provenance walk surfaces the
ruling and the objection, and a synthesis node captures the settled takeaway.

These tests drive ``ops.*`` directly because that is where the debate invariants
live (the CLI and the MCP server are thin pass-throughs). One test exercises the
loop through the CLI to confirm the public command surface behaves the same.

Conventions match tests/test_cli.py: pure-stdlib unittest, each test isolates
its graph under a tempfile.TemporaryDirectory (no shared .sparkle state), and the
CLI 'Handle:' line is parsed with handle_of (never split()[-1]).
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest

from src.sparkle import ops
from src.sparkle.cli import main
from src.sparkle.graph import GraphStore
from src.sparkle.models import Node


class DebateLoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -- CLI plumbing (mirrors tests/test_cli.py) ---------------------------

    def run_cli(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["--store", str(self.store_path), *args])
        finally:
            sys.stdin = old_stdin
        return exit_code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def handle_of(output: str) -> str:
        """Read the 12-char handle off the 'Handle:' line of an add output."""
        for line in output.splitlines():
            if line.startswith("Handle:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no Handle: line in output:\n{output}")

    # -- helpers ------------------------------------------------------------

    def _propose(self, title: str, content: str) -> str:
        result = ops.add_node(
            self.store, node_type="claim", title=title, content=content
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["node_type"], "claim")
        self.assertEqual(result["status"], "active")
        return result["node_id"]

    def _attack(self, claim_id: str, title: str, content: str) -> dict:
        """Adversary move: an objection node + a 'contradicts' edge into claim.

        Uses the 'objection' branch template, which is the single structured
        debate move (it fixes node_type='objection', relation='contradicts',
        and writes the edge child --contradicts--> parent).
        """
        return ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="objection",
            title=title,
            content=content,
        )

    # ----------------------------------------------------------------------
    # (a)-(f): the full loop, propose -> attack -> signal -> rule -> harvest
    # ----------------------------------------------------------------------

    def test_full_loop_propose_attack_signal_rule_harvest(self) -> None:
        # (a) PROPOSE a claim. A fresh claim has no edges, so the referee reads
        # it as 'unchallenged'.
        claim_id = self._propose(
            "Music boosts coding focus",
            "Listening to instrumental music improves focus while coding.",
        )
        pre = ops.referee_signal(self.store, claim_id)
        self.assertEqual(pre["stored_status"], "active")
        self.assertEqual(pre["live_signal"], "unchallenged")
        self.assertFalse(pre["challenged"])
        self.assertEqual(pre["contradicts"], 0)
        self.assertEqual(pre["supports"], 0)

        # rule() must REFUSE an unchallenged claim (the single hard invariant).
        with self.assertRaisesRegex(ValueError, "never attacked"):
            ops.rule(self.store, claim_id, verdict="accept")

        # (b) ATTACK: an objection node + a 'contradicts' edge INTO the claim.
        attack = self._attack(
            claim_id,
            "Lyrics pull attention away",
            "Songs with words steal working-memory from the code.",
        )
        self.assertEqual(attack["template"], "objection")
        self.assertEqual(attack["node_type"], "objection")
        self.assertEqual(attack["link"]["relation"], "contradicts")
        # Branch edge direction: child(objection) --contradicts--> parent(claim).
        self.assertEqual(attack["link"]["from_id"], attack["node_id"])
        self.assertEqual(attack["link"]["to_id"], claim_id)
        objection_id = attack["node_id"]

        # (c) SIGNAL reflects the attacked state. The raw tally flips 'challenged'
        # on the first inbound contradicts; the live signal is 'weakly_supported'
        # (an objection with no offsetting support, net <= -1).
        tally = ops.edge_tally(self.store, claim_id)
        self.assertEqual(tally["contradicts"], 1)
        self.assertEqual(tally["supports"], 0)
        self.assertEqual(tally["net"], -1)
        self.assertTrue(tally["challenged"])
        self.assertFalse(tally["has_decision"])

        post = ops.referee_signal(self.store, claim_id)
        self.assertTrue(post["challenged"])
        self.assertEqual(post["contradicts"], 1)
        self.assertEqual(post["live_signal"], "weakly_supported")
        self.assertEqual(post["stored_status"], "active")  # never mutated in place

        # (d) RULE with settle=True writes a superseding node, status 'ratified'.
        ruling = ops.rule(
            self.store,
            claim_id,
            verdict="reject",
            rationale="the lyrics objection holds",
            settle=True,
        )
        self.assertTrue(ruling["settled"])
        self.assertEqual(ruling["decision"]["node_type"], "decision")
        self.assertEqual(ruling["decision_link"]["relation"], "evaluates")
        ratified = ruling["ratified_claim"]
        self.assertEqual(ratified["status"], "ratified")
        self.assertEqual(ratified["node_type"], "claim")
        # The superseding version is a NEW node; the original stays frozen+active.
        self.assertNotEqual(ratified["node_id"], claim_id)
        self.assertEqual(self.store.get_node(claim_id)["status"], "active")
        ratified_id = ratified["node_id"]
        decision_id = ruling["decision"]["node_id"]

        # The decision evaluates BOTH the original and the ratified version, so
        # the referee reports 'ratified' for each (has_decision drives it).
        sig_old = ops.referee_signal(self.store, claim_id)
        self.assertTrue(sig_old["has_decision"])
        self.assertEqual(sig_old["live_signal"], "ratified")
        sig_ratified = ops.referee_signal(self.store, ratified_id)
        self.assertTrue(sig_ratified["has_decision"])
        self.assertEqual(sig_ratified["stored_status"], "ratified")
        self.assertEqual(sig_ratified["live_signal"], "ratified")

        # (e) LINEAGE(claim) surfaces the ruling, the objection, AND the ratified
        # version. lineage follows INBOUND edges (edge.to_id == current ->
        # enqueue edge.from_id): the objection and decision point INTO the claim,
        # and the ratified version points INTO the old claim via 'supersedes'
        # (new --supersedes--> old), so all three are reachable from the claim.
        lineage_ids = {n["node_id"] for n in ops.lineage(self.store, claim_id)}
        self.assertIn(claim_id, lineage_ids)
        self.assertIn(objection_id, lineage_ids)
        self.assertIn(decision_id, lineage_ids)
        self.assertIn(ratified_id, lineage_ids)

        # (f) HARVEST captures the settled result: a model-authored synthesis
        # node linked produced INTO the ratified claim (synthesis --produced-->
        # claim), exactly as the MCP harvest tool composes it on top of ops.
        harvest = ops.add_node(
            self.store,
            node_type="synthesis",
            title="Settled: skip lyrics while coding",
            content="Ruling rejected the broad claim; lyrics specifically hurt focus.",
            author="synthesizer",
            confidence=0.9,
            status="active",
            tags=["harvest"],
            metadata={"run_id": "loop-run"},
            link_to=ratified_id,
            relation="produced",
            model_authored=True,
        )
        self.assertTrue(harvest["created"])
        self.assertEqual(harvest["node_type"], "synthesis")
        # Model-authored writes clamp confidence to the 0.5 cap.
        self.assertEqual(harvest["confidence"], 0.5)
        self.assertEqual(harvest["link"]["relation"], "produced")
        self.assertEqual(harvest["link"]["from_id"], harvest["node_id"])
        self.assertEqual(harvest["link"]["to_id"], ratified_id)

        # The synthesis surfaces in the ratified claim's provenance walk.
        ratified_lineage = {
            n["node_id"] for n in ops.lineage(self.store, ratified_id)
        }
        self.assertIn(harvest["node_id"], ratified_lineage)
        self.assertIn(decision_id, ratified_lineage)

        # A judged/ratified claim drops off the frontier; the synthesis is not a
        # claim and never lands there either.
        frontier_ids = {e["node_id"] for e in ops.frontier(self.store)["entries"]}
        self.assertNotIn(ratified_id, frontier_ids)
        self.assertNotIn(claim_id, frontier_ids)
        self.assertNotIn(harvest["node_id"], frontier_ids)

    # ----------------------------------------------------------------------
    # (g) A second identical proposal dedupes to the same id even though
    #     created_at differs (the fingerprint deliberately excludes it).
    # ----------------------------------------------------------------------

    def test_identical_reproposal_dedupes_despite_different_created_at(self) -> None:
        # Pre-seed a node with a DELIBERATELY old created_at, written straight
        # through the store. Its node id (a SHA over the full payload, which
        # INCLUDES created_at) is therefore different from what add_node would
        # mint today. A naive id-equality dedup would fork a near-duplicate.
        seeded = Node(
            node_type="claim",
            title="Dedup by fingerprint, not timestamp",
            content="The same idea proposed at two different moments.",
            created_at="2020-01-01T00:00:00+00:00",
            author="early-bird",
            confidence=0.2,
        )
        seeded_id = self.store.add_node(seeded)
        self.assertEqual(
            self.store.get_node(seeded_id)["created_at"],
            "2020-01-01T00:00:00+00:00",
        )

        # find_duplicate keys ONLY on normalized type+title+content, so it points
        # at the earlier-timestamped node regardless of when we ask.
        fp = ops.content_fingerprint(
            "claim",
            "Dedup by fingerprint, not timestamp",
            "The same idea proposed at two different moments.",
        )
        dup = ops.find_duplicate(
            self.store,
            node_type="claim",
            title="Dedup by fingerprint, not timestamp",
            content="The same idea proposed at two different moments.",
        )
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["existing_id"], seeded_id)
        self.assertEqual(dup["fingerprint"], fp)

        # Re-propose the SAME idea now (today's created_at, different author and
        # confidence). add_node returns the EXISTING seeded id and does not fork.
        again = ops.add_node(
            self.store,
            node_type="claim",
            title="Dedup by fingerprint, not timestamp",
            content="The same idea proposed at two different moments.",
            author="late-comer",
            confidence=0.9,
        )
        self.assertFalse(again["created"])
        self.assertEqual(again["node_id"], seeded_id)
        self.assertEqual(again["fingerprint"], fp)
        self.assertEqual(len(self.store.list_nodes()), 1)

        # Fingerprint normalization: case-folding and whitespace-stripping the
        # type/title/content yields the same fingerprint.
        self.assertEqual(
            ops.content_fingerprint(
                "  CLAIM ",
                "Dedup By Fingerprint, Not Timestamp",
                "  the same idea proposed at TWO different moments. ",
            ),
            fp,
        )
        # node_type participates: same title+content, different type -> distinct.
        self.assertNotEqual(
            ops.content_fingerprint(
                "evidence",
                "Dedup by fingerprint, not timestamp",
                "The same idea proposed at two different moments.",
            ),
            fp,
        )

    # ----------------------------------------------------------------------
    # (h) A multi-round debate: refine after objection, re-attack, then settle.
    # ----------------------------------------------------------------------

    def test_multi_round_refine_reattack_then_settle(self) -> None:
        # Round 1: propose + first objection.
        claim_v1 = self._propose(
            "Pair programming raises code quality",
            "Two developers at one keyboard catch more defects.",
        )
        attack1 = self._attack(
            claim_v1,
            "Pairing halves throughput",
            "Two people working one task ships less per hour.",
        )
        objection1_id = attack1["node_id"]
        round1 = ops.referee_signal(self.store, claim_v1)
        self.assertTrue(round1["challenged"])
        self.assertEqual(round1["contradicts"], 1)

        # REFINE: the proposer narrows the claim into a stronger v2. revise()
        # writes a superseding node (never mutates v1) and, by default, rehomes
        # v1's inbound edges onto v2 — so the round-1 objection carries forward
        # and v2 is born already-challenged.
        refine = ops.revise(
            self.store,
            claim_v1,
            content="Two developers at one keyboard catch more defects on critical-path code.",
            status="active",
        )
        claim_v2 = refine["node_id"]
        self.assertNotEqual(claim_v2, claim_v1)
        self.assertEqual(refine["old_id"], claim_v1)
        self.assertEqual(self.store.get_node(claim_v1)["status"], "active")  # frozen
        self.assertEqual(len(refine["rehomed"]), 1)  # the round-1 contradicts edge
        carried = ops.edge_tally(self.store, claim_v2)
        self.assertEqual(carried["contradicts"], 1)
        self.assertTrue(carried["challenged"])

        # RE-ATTACK round 2: a fresh objection contradicting v2.
        attack2 = self._attack(
            claim_v2,
            "No data on critical-path",
            "Narrowing to critical-path code lacks supporting evidence.",
        )
        objection2_id = attack2["node_id"]
        self.assertEqual(attack2["link"]["to_id"], claim_v2)
        round2 = ops.edge_tally(self.store, claim_v2)
        self.assertEqual(round2["contradicts"], 2)  # round-1 carried + round-2 new
        self.assertTrue(round2["challenged"])
        self.assertEqual(
            ops.referee_signal(self.store, claim_v2)["live_signal"],
            "weakly_supported",
        )

        # SETTLE on v2. A ruling on the refined claim is legal (it is attacked)
        # and writes the ratified version off v2.
        ruling = ops.rule(
            self.store,
            claim_v2,
            verdict="accept the narrowed claim",
            rationale="critical-path framing survives both objections",
            settle=True,
        )
        self.assertTrue(ruling["settled"])
        ratified = ruling["ratified_claim"]
        self.assertEqual(ratified["status"], "ratified")
        self.assertNotEqual(ratified["node_id"], claim_v2)
        ratified_id = ratified["node_id"]

        decision_id = ruling["decision"]["node_id"]

        # Provenance walks follow INBOUND edges only. The full debate history
        # (both claim versions, both rounds of objection, and the decision) is
        # reachable from the original claim under debate, because every move
        # points INTO a claim version and the versions chain via 'supersedes'
        # (v2 --supersedes--> v1, ratified --supersedes--> v2).
        history = {n["node_id"] for n in ops.lineage(self.store, claim_v1)}
        self.assertIn(claim_v1, history)
        self.assertIn(claim_v2, history)
        self.assertIn(ratified_id, history)
        self.assertIn(objection1_id, history)
        self.assertIn(objection2_id, history)
        self.assertIn(decision_id, history)

        # The ratified node itself is the head of the chain: nothing supersedes
        # it, and its only inbound edge is the judge's decision, so its own
        # lineage is just itself + that decision (it points OUT to v2, and
        # lineage never follows outbound edges).
        ratified_lineage = {
            n["node_id"] for n in ops.lineage(self.store, ratified_id)
        }
        self.assertIn(ratified_id, ratified_lineage)
        self.assertIn(decision_id, ratified_lineage)

        # The settled claim (both versions) leaves the frontier.
        frontier_ids = {e["node_id"] for e in ops.frontier(self.store)["entries"]}
        self.assertNotIn(ratified_id, frontier_ids)
        self.assertNotIn(claim_v2, frontier_ids)

    # ----------------------------------------------------------------------
    # rule(settle=False): the judge records a decision WITHOUT ratifying.
    # ----------------------------------------------------------------------

    def test_rule_without_settle_records_decision_but_no_ratified_version(self) -> None:
        claim_id = self._propose(
            "Standups are worth the interruption",
            "A daily 10-minute sync prevents larger coordination failures.",
        )
        self._attack(
            claim_id,
            "Async updates cover the same ground",
            "A written thread conveys the same status without breaking flow.",
        )

        ruling = ops.rule(
            self.store, claim_id, verdict="needs more evidence", settle=False
        )
        self.assertFalse(ruling["settled"])
        self.assertNotIn("ratified_claim", ruling)
        self.assertEqual(ruling["decision"]["node_type"], "decision")
        self.assertEqual(ruling["decision_link"]["relation"], "evaluates")
        # Decision points decision --evaluates--> claim.
        self.assertEqual(
            ruling["decision_link"]["from_id"], ruling["decision"]["node_id"]
        )
        self.assertEqual(ruling["decision_link"]["to_id"], claim_id)

        # No superseding version was written: still exactly one claim, and its
        # stored status is unchanged at 'active' (nothing was ratified).
        claims = [n for _, n in self.store.list_nodes() if n["node_type"] == "claim"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(self.store.get_node(claim_id)["status"], "active")

        # has_decision is now True, which the live referee reports as 'ratified'
        # (the 'a judge's decision evaluates this claim' rule fires first), even
        # though the stored status stayed 'active'. This documents the gap
        # between the live signal and the stored status for a decision-only rule.
        sig = ops.referee_signal(self.store, claim_id)
        self.assertTrue(sig["has_decision"])
        self.assertEqual(sig["stored_status"], "active")
        self.assertEqual(sig["live_signal"], "ratified")

    # ----------------------------------------------------------------------
    # rule() refuses a non-claim target before it ever checks for an attack.
    # ----------------------------------------------------------------------

    def test_rule_refuses_non_claim_target(self) -> None:
        evidence = ops.add_node(
            self.store,
            node_type="evidence",
            title="A measured benchmark",
            content="Throughput numbers from a controlled run.",
        )
        with self.assertRaisesRegex(ValueError, "rule\\(\\) targets a claim"):
            ops.rule(self.store, evidence["node_id"], verdict="accept", settle=True)

    # ----------------------------------------------------------------------
    # The propose + attack moves driven through the CLI, then the judging step
    # through ops.rule (the CLI has no 'rule' command — the judge move is
    # reachable only via ops and the MCP server). Proves the CLI's add-node /
    # add-branch writes land in the same store the ops invariant reads from.
    # Handle-line parsing per the test convention (never split()[-1]).
    # ----------------------------------------------------------------------

    def test_loop_propose_and_attack_via_cli_then_rule_via_ops(self) -> None:
        self.run_cli("init")

        # PROPOSE via CLI; read the claim handle off the 'Handle:' line.
        code, out, err = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Tests pay for themselves",
            "--content",
            "Automated tests catch regressions cheaper than manual QA.",
        )
        self.assertEqual(code, 0, msg=err)
        self.assertIn("Added claim: Tests pay for themselves", out)
        claim_handle = self.handle_of(out)

        # Judging an unchallenged claim is refused at the ops boundary that the
        # CLI-written store feeds into.
        with self.assertRaisesRegex(ValueError, "never attacked"):
            ops.rule(self.store, claim_handle, verdict="accept", settle=True)

        # ATTACK via the add-branch objection command (--from takes the handle).
        code, branch_out, err = self.run_cli(
            "add-branch",
            "--from",
            claim_handle,
            "--template",
            "objection",
            "--title",
            "Tests rot when nobody maintains them",
            "--content",
            "Stale tests cost more than they catch.",
        )
        self.assertEqual(code, 0, msg=err)
        self.assertIn("Added objection: Tests rot when nobody maintains them", branch_out)
        objection_handle = self.handle_of(branch_out)

        # The CLI-written objection really contradicts the claim in the store.
        tally = ops.edge_tally(self.store, claim_handle)
        self.assertEqual(tally["contradicts"], 1)
        self.assertTrue(tally["challenged"])

        # RULE with settle now succeeds and writes a ratified version.
        ruling = ops.rule(self.store, claim_handle, verdict="accept", settle=True)
        self.assertTrue(ruling["settled"])
        self.assertEqual(ruling["ratified_claim"]["status"], "ratified")

        # Exactly one claim is ratified in the store; the original stays active.
        claim_statuses = sorted(
            n["status"]
            for _, n in self.store.list_nodes()
            if n["node_type"] == "claim"
        )
        self.assertEqual(claim_statuses, ["active", "ratified"])

        # The claim's provenance walk surfaces the objection and the decision.
        lineage_types = {n["node_type"] for n in ops.lineage(self.store, claim_handle)}
        self.assertIn("objection", lineage_types)
        self.assertIn("decision", lineage_types)
        # Sanity: the objection handle resolves into the same lineage.
        lineage_ids = {n["node_id"] for n in ops.lineage(self.store, claim_handle)}
        self.assertIn(self.store.resolve_id(objection_handle), lineage_ids)


if __name__ == "__main__":
    unittest.main()
