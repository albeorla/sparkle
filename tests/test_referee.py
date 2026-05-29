"""Referee + ratification-guard coverage for Sparkle.

Pins the live referee read (edge_tally + the transition-rule engine that maps a
claim's inbound edges to a display-only signal), the single hard ratification
invariant in ops.rule(), the model-authored terminal-status guard, and the
content-fingerprint dedup that keeps a re-proposal from forking the graph.

Pure-stdlib unittest. Each test isolates its graph under a TemporaryDirectory so
there are no side effects and no shared .sparkle state. We drive ops.* functions
directly (no CLI) since the referee/guard/dedup invariants live in ops, not in
any front-end.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from src.sparkle import ops
from src.sparkle.graph import GraphStore
from src.sparkle.models import Edge, Node


class RefereeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -- helpers -----------------------------------------------------------

    def _claim(self, title: str = "A claim", content: str = "Under debate.", **kw) -> str:
        return self.store.add_node(
            Node(node_type="claim", title=title, content=content, **kw)
        )

    def _node(self, node_type: str, title: str, content: str) -> str:
        return self.store.add_node(
            Node(node_type=node_type, title=title, content=content)
        )

    def _attack(self, claim_id: str, title: str = "Objection") -> str:
        """Wire an objection node --contradicts--> claim (an attack)."""
        obj = self._node("objection", title, "Here is the attack.")
        self.store.add_edge(Edge(from_id=obj, to_id=claim_id, relation="contradicts"))
        return obj

    def _support(self, claim_id: str, title: str = "Evidence") -> str:
        ev = self._node("evidence", title, "Here is support.")
        self.store.add_edge(Edge(from_id=ev, to_id=claim_id, relation="supports"))
        return ev


# ---------------------------------------------------------------------------
# edge_tally: counts by relation, net, challenged, has_decision, direction
# ---------------------------------------------------------------------------


class EdgeTallyTests(RefereeTestCase):
    def test_empty_claim_tally_is_zeroed_and_unchallenged(self) -> None:
        claim = self._claim()
        t = ops.edge_tally(self.store, claim)
        self.assertEqual(t["node_id"], claim)
        self.assertEqual(t["counts"], {})
        self.assertEqual(t["supports"], 0)
        self.assertEqual(t["contradicts"], 0)
        self.assertEqual(t["net"], 0)
        self.assertFalse(t["challenged"])
        self.assertFalse(t["has_decision"])

    def test_counts_group_inbound_edges_by_relation(self) -> None:
        claim = self._claim()
        self._support(claim, "Ev one")
        self._support(claim, "Ev two")
        self._attack(claim, "Obj one")
        # A refines edge in too, to prove every relation is counted by key.
        refiner = self._node("inference", "Sharper version", "Narrows the claim.")
        self.store.add_edge(Edge(from_id=refiner, to_id=claim, relation="refines"))

        t = ops.edge_tally(self.store, claim)
        self.assertEqual(t["counts"], {"supports": 2, "contradicts": 1, "refines": 1})
        self.assertEqual(t["supports"], 2)
        self.assertEqual(t["contradicts"], 1)
        self.assertEqual(t["net"], 1)  # 2 supports - 1 contradicts

    def test_challenged_flips_true_at_first_inbound_contradicts(self) -> None:
        claim = self._claim()
        # Support alone never challenges.
        self._support(claim)
        self.assertFalse(ops.edge_tally(self.store, claim)["challenged"])
        # First contradicts edge flips it.
        self._attack(claim)
        t = ops.edge_tally(self.store, claim)
        self.assertTrue(t["challenged"])
        self.assertEqual(t["contradicts"], 1)

    def test_challenged_counts_contradicts_only_not_other_relations(self) -> None:
        """An objection node linked with the wrong relation does NOT challenge."""
        claim = self._claim()
        obj = self._node("objection", "Mislinked objection", "Wrong relation used.")
        # Link the objection with 'refines', not 'contradicts'.
        self.store.add_edge(Edge(from_id=obj, to_id=claim, relation="refines"))
        t = ops.edge_tally(self.store, claim)
        self.assertEqual(t["contradicts"], 0)
        self.assertFalse(t["challenged"])

    def test_outbound_edges_do_not_count_toward_inbound_tally(self) -> None:
        """edge_tally is inbound-only: the claim's own outbound edge is ignored."""
        claim = self._claim()
        other = self._claim("Another claim", "A second claim.")
        # claim --contradicts--> other (outbound from claim's perspective).
        self.store.add_edge(Edge(from_id=claim, to_id=other, relation="contradicts"))
        t = ops.edge_tally(self.store, claim)
        self.assertEqual(t["contradicts"], 0)
        self.assertFalse(t["challenged"])
        # But 'other' sees that contradicts as inbound.
        self.assertTrue(ops.edge_tally(self.store, other)["challenged"])

    def test_has_decision_only_for_inbound_evaluates_from_a_decision_node(self) -> None:
        claim = self._claim()
        # An evaluates edge from a NON-decision node must NOT set has_decision.
        ev = self._node("evidence", "Not a ruling", "Evaluates but not a decision.")
        self.store.add_edge(Edge(from_id=ev, to_id=claim, relation="evaluates"))
        self.assertFalse(ops.edge_tally(self.store, claim)["has_decision"])

        # A decision node with a non-evaluates relation also must NOT set it.
        dec = self._node("decision", "Ruling", "A real ruling.")
        self.store.add_edge(Edge(from_id=dec, to_id=claim, relation="supports"))
        self.assertFalse(ops.edge_tally(self.store, claim)["has_decision"])

        # Only a decision node via an evaluates edge sets has_decision.
        dec2 = self._node("decision", "Ruling 2", "The judging node.")
        self.store.add_edge(Edge(from_id=dec2, to_id=claim, relation="evaluates"))
        self.assertTrue(ops.edge_tally(self.store, claim)["has_decision"])


# ---------------------------------------------------------------------------
# Transition-rule engine: drive each lifecycle edge from the BUILTIN_PLAYBOOK
# map; assert each rule fires when its condition holds and the right signal
# wins. ops.referee_signal is the public reader; we assert live_signal + why.
# ---------------------------------------------------------------------------


class TransitionRuleTests(RefereeTestCase):
    def signal(self, ref: str) -> dict:
        return ops.referee_signal(self.store, ref)

    def test_rule_unchallenged_fires_on_a_bare_claim(self) -> None:
        """{claim, supports_eq:0, contradicts_eq:0} -> 'unchallenged'."""
        claim = self._claim()
        s = self.signal(claim)
        self.assertEqual(s["live_signal"], "unchallenged")
        self.assertEqual(s["why"], "no support and no objection recorded yet")
        self.assertEqual(s["stored_status"], "active")

    def test_rule_unchallenged_does_not_fire_once_an_edge_exists(self) -> None:
        """The 0/0 condition no longer holds after one support edge."""
        claim = self._claim()
        self._support(claim)
        self.assertNotEqual(self.signal(claim)["live_signal"], "unchallenged")

    def test_rule_supported_fires_when_support_and_no_contradicts(self) -> None:
        """{claim, supports_min:1, contradicts_eq:0} -> 'supported'."""
        claim = self._claim()
        self._support(claim)
        s = self.signal(claim)
        self.assertEqual(s["live_signal"], "supported")
        self.assertEqual(s["why"], "supported and never challenged")

    def test_rule_supported_does_not_fire_once_challenged(self) -> None:
        """A single contradicts edge breaks the contradicts_eq:0 condition."""
        claim = self._claim()
        self._support(claim)
        self._attack(claim)
        self.assertNotEqual(self.signal(claim)["live_signal"], "supported")

    def test_rule_ready_to_judge_fires_with_both_support_and_objection(self) -> None:
        """{claim, supports_min:1, contradicts_min:1} -> 'ready_to_judge'.

        Net is non-negative (1 support, 1 contradicts -> net 0), so the
        ready_to_judge rule wins ahead of the weakly_supported rule that needs
        net_max:-1.
        """
        claim = self._claim()
        self._support(claim)
        self._attack(claim)
        s = self.signal(claim)
        self.assertEqual(s["live_signal"], "ready_to_judge")
        self.assertEqual(s["why"], "both support and objection exist but no ruling yet")
        self.assertEqual(s["net"], 0)

    def test_rule_weakly_supported_fires_when_objections_outnumber_support(self) -> None:
        """{claim, contradicts_min:1, net_max:-1} -> 'weakly_supported'.

        One support, two objections -> net -1, so ready_to_judge does NOT win
        here? It WOULD (supports_min:1, contradicts_min:1 hold) — and it is
        ordered FIRST. So to actually reach weakly_supported we need support 0
        with objections, where ready_to_judge's supports_min:1 fails.
        """
        claim = self._claim()
        self._attack(claim, "Obj A")
        self._attack(claim, "Obj B")
        s = self.signal(claim)
        # 0 supports, 2 contradicts -> net -2. ready_to_judge needs supports>=1
        # (fails); weakly_supported needs contradicts>=1 and net<=-1 (holds).
        self.assertEqual(s["net"], -2)
        self.assertEqual(s["live_signal"], "weakly_supported")
        self.assertEqual(s["why"], "more objections than support")

    def test_ready_to_judge_wins_over_weakly_supported_when_both_hold(self) -> None:
        """First-match-wins: with 1 support + 2 objections both rules' guards
        hold, and ready_to_judge is ordered earlier, so it wins."""
        claim = self._claim()
        self._support(claim)
        self._attack(claim, "Obj A")
        self._attack(claim, "Obj B")
        s = self.signal(claim)
        self.assertEqual(s["net"], -1)  # weakly_supported's net_max:-1 also holds
        self.assertEqual(s["live_signal"], "ready_to_judge")

    def test_rule_stalled_fires_on_stored_stalled_status(self) -> None:
        """{claim, stored_status:'stalled'} -> 'stalled' (author-set only)."""
        claim = self._claim(status="stalled")
        s = self.signal(claim)
        self.assertEqual(s["stored_status"], "stalled")
        self.assertEqual(s["live_signal"], "stalled")
        self.assertEqual(s["why"], "explicitly parked; needs a new angle or a decision")

    def test_rule_ratified_fires_on_stored_ratified_status(self) -> None:
        """{claim, stored_status:'ratified'} -> 'ratified'."""
        claim = self._claim(status="ratified")
        s = self.signal(claim)
        self.assertEqual(s["stored_status"], "ratified")
        self.assertEqual(s["live_signal"], "ratified")
        self.assertEqual(s["why"], "stored terminal status from a settled ruling")

    def test_rule_ratified_fires_on_inbound_decision_even_when_status_active(self) -> None:
        """{claim, has_decision:True} is ordered first -> 'ratified' regardless
        of stored status. A decision-node evaluates edge wins over everything."""
        claim = self._claim()  # status active
        dec = self._node("decision", "Ruling", "Judged.")
        self.store.add_edge(Edge(from_id=dec, to_id=claim, relation="evaluates"))
        s = self.signal(claim)
        self.assertEqual(s["stored_status"], "active")
        self.assertTrue(s["has_decision"])
        self.assertEqual(s["live_signal"], "ratified")
        self.assertEqual(s["why"], "a judge's decision evaluates this claim")

    def test_non_claim_node_falls_through_to_default_active_signal(self) -> None:
        """Every transition rule keys node_type:'claim'; a non-claim node never
        matches and falls through to the default 'active'/'no rule matched'."""
        evidence = self._node("evidence", "Some evidence", "Just evidence.")
        s = self.signal(evidence)
        self.assertEqual(s["live_signal"], "active")
        self.assertEqual(s["why"], "no transition rule matched")

    def test_rule_engine_fails_closed_on_unknown_predicate_key(self) -> None:
        """_rule_matches returns False for any unrecognized predicate key, so a
        rule carrying an unknown key never fires (fail closed)."""
        # 0/0 claim facts: only the 'unchallenged' rule should normally match.
        claim = self._claim()
        facts = {
            "node_type": "claim",
            "stored_status": "active",
            "supports": 0,
            "contradicts": 0,
            "net": 0,
            "has_decision": False,
        }
        # A spec with a bogus key must not match even though node_type lines up.
        self.assertFalse(
            ops._rule_matches({"node_type": "claim", "totally_made_up": 7}, facts)
        )
        # A spec with only known keys that hold does match.
        self.assertTrue(
            ops._rule_matches({"node_type": "claim", "supports_eq": 0}, facts)
        )

    def test_apply_transition_rules_first_match_wins_in_order(self) -> None:
        """_apply_transition_rules walks rules in order and returns the first
        match. Two rules both matching -> the earlier one's signal is returned."""
        node = {"node_type": "claim", "status": "active"}
        tally = {"supports": 1, "contradicts": 0, "net": 1, "has_decision": False}
        rules = [
            {"when": {"node_type": "claim"}, "signal": "first", "why": "earliest"},
            {"when": {"node_type": "claim"}, "signal": "second", "why": "later"},
        ]
        out = ops._apply_transition_rules(rules, node, tally)
        self.assertEqual(out["signal"], "first")
        self.assertEqual(out["why"], "earliest")

    def test_apply_transition_rules_default_when_nothing_matches(self) -> None:
        """No rule matches -> default signal 'active' / 'no transition rule matched'."""
        node = {"node_type": "claim", "status": "active"}
        tally = {"supports": 0, "contradicts": 0, "net": 0, "has_decision": False}
        out = ops._apply_transition_rules(
            [{"when": {"node_type": "evidence"}, "signal": "x", "why": "y"}],
            node,
            tally,
        )
        self.assertEqual(out["signal"], "active")
        self.assertEqual(out["why"], "no transition rule matched")


# ---------------------------------------------------------------------------
# rule(): the ratification guard. Refuses when never attacked; succeeds once
# challenged; type guard fires first; settle=True writes a ratified version.
# ---------------------------------------------------------------------------


class RuleInvariantTests(RefereeTestCase):
    NEVER_ATTACKED_MSG = (
        "you may not ratify a claim the adversary never attacked; "
        "run a challenge first (add an objection/contradicts edge)"
    )

    def test_rule_raises_never_attacked_when_no_inbound_contradicts(self) -> None:
        """No inbound contradicts -> exact 'never attacked' ValueError."""
        claim = self._claim("Unchallenged", "Nobody attacked it.")
        with self.assertRaises(ValueError) as cm:
            ops.rule(self.store, claim, verdict="accept")
        self.assertEqual(str(cm.exception), self.NEVER_ATTACKED_MSG)

    def test_support_alone_does_not_satisfy_the_challenge_requirement(self) -> None:
        """A supports edge is not an attack; rule() still refuses."""
        claim = self._claim()
        self._support(claim)
        with self.assertRaisesRegex(ValueError, "never attacked"):
            ops.rule(self.store, claim, verdict="accept")

    def test_rule_succeeds_once_challenged_and_writes_decision(self) -> None:
        """A single inbound contradicts edge unlocks a legal ruling."""
        claim = self._claim()
        self._attack(claim)
        result = ops.rule(self.store, claim, verdict="accept", rationale="It holds.")
        self.assertFalse(result["settled"])
        self.assertEqual(result["decision"]["node_type"], "decision")
        self.assertEqual(result["decision"]["title"], "Ruling: accept")
        self.assertEqual(result["decision"]["content"], "It holds.")
        self.assertEqual(result["decision"]["status"], "active")
        # The decision links to the claim with an evaluates edge.
        self.assertEqual(result["decision_link"]["relation"], "evaluates")
        self.assertEqual(result["decision_link"]["to_id"], claim)
        # settle=False writes NO ratified version.
        self.assertNotIn("ratified_claim", result)
        # The original claim is untouched (frozen, still active).
        self.assertEqual(self.store.get_node(claim)["status"], "active")

    def test_rule_settle_writes_a_superseding_ratified_claim_version(self) -> None:
        """settle=True writes a NEW claim version with status='ratified' and
        links new --supersedes--> old; the frozen original stays 'active'."""
        claim = self._claim()
        self._attack(claim)
        result = ops.rule(self.store, claim, verdict="reject", settle=True)
        self.assertTrue(result["settled"])
        self.assertEqual(result["ratified_claim"]["status"], "ratified")
        self.assertNotEqual(result["ratified_claim"]["node_id"], claim)
        # Original claim never mutated in place.
        self.assertEqual(self.store.get_node(claim)["status"], "active")
        # A supersedes edge exists new -> old.
        relations = {
            (e["from_id"], e["to_id"], e["relation"])
            for _, e in self.store.list_edges()
        }
        self.assertIn(
            (result["ratified_claim"]["node_id"], claim, "supersedes"), relations
        )

    def test_rule_settle_points_decision_at_ratified_version_too(self) -> None:
        """The judge's decision also evaluates the ratified version, so the
        ratified node reads has_decision=True (it counts as judged/closed)."""
        claim = self._claim()
        self._attack(claim)
        result = ops.rule(self.store, claim, verdict="accept", settle=True)
        ratified_id = result["ratified_claim"]["node_id"]
        self.assertTrue(ops.edge_tally(self.store, ratified_id)["has_decision"])
        self.assertEqual(
            ops.referee_signal(self.store, ratified_id)["live_signal"], "ratified"
        )

    def test_rule_targets_a_claim_guard_fires_before_challenge_check(self) -> None:
        """rule() on a non-claim node raises the target-type ValueError, and it
        is checked BEFORE the challenged check (so a non-claim fails type-first,
        even with no inbound contradicts)."""
        evidence = self._node("evidence", "Some evidence", "Not a claim.")
        with self.assertRaises(ValueError) as cm:
            ops.rule(self.store, evidence, verdict="accept")
        msg = str(cm.exception)
        self.assertIn("rule() targets a claim", msg)
        self.assertIn("is a evidence", msg)
        # Make sure it is the type guard, not the never-attacked guard.
        self.assertNotIn("never attacked", msg)


# ---------------------------------------------------------------------------
# guard_authored_status + the model_authored=True path through add_node.
# A model may not author a terminal status ('ratified'/'harvested'/'abandoned')
# unless the write came from a settled ruling.
# ---------------------------------------------------------------------------


class AuthoredStatusGuardTests(RefereeTestCase):
    def test_guard_blocks_model_authored_ratified_unless_settled(self) -> None:
        with self.assertRaises(ValueError) as cm:
            ops.guard_authored_status("ratified", settled=False)
        msg = str(cm.exception)
        self.assertIn("a model may not author terminal status", msg)
        self.assertIn("'ratified'", msg)
        self.assertIn("settled ruling", msg)

    def test_guard_allows_ratified_when_settled(self) -> None:
        self.assertEqual(ops.guard_authored_status("ratified", settled=True), "ratified")

    def test_guard_blocks_every_terminal_status_for_a_model(self) -> None:
        for terminal in ("ratified", "harvested", "abandoned"):
            with self.subTest(status=terminal):
                with self.assertRaisesRegex(
                    ValueError, "a model may not author terminal status"
                ):
                    ops.guard_authored_status(terminal, settled=False)

    def test_guard_passes_through_non_terminal_statuses(self) -> None:
        for ok in ("active", "stalled", "weakly_supported", "promising"):
            with self.subTest(status=ok):
                self.assertEqual(ops.guard_authored_status(ok, settled=False), ok)

    def test_add_node_blocks_a_model_authored_ratified_write(self) -> None:
        """The guard is wired into add_node when model_authored=True."""
        with self.assertRaisesRegex(
            ValueError, "a model may not author terminal status"
        ):
            ops.add_node(
                self.store,
                node_type="claim",
                title="Sneaky ratify",
                content="A model trying to declare victory.",
                status="ratified",
                model_authored=True,
            )
        # Nothing was written.
        self.assertEqual(len(self.store.list_nodes()), 0)

    def test_add_node_allows_human_authored_ratified_write(self) -> None:
        """A human (model_authored=False) is not subject to the guard."""
        result = ops.add_node(
            self.store,
            node_type="claim",
            title="Human ratify",
            content="A human marking a claim ratified.",
            status="ratified",
            model_authored=False,
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "ratified")

    def test_model_authored_confidence_is_capped_at_half(self) -> None:
        """The model_authored path also clamps confidence to the 0.5 cap, so a
        high model-asserted certainty cannot slip into the graph."""
        result = ops.add_node(
            self.store,
            node_type="claim",
            title="Overconfident model claim",
            content="The model is very sure.",
            confidence=0.99,
            model_authored=True,
        )
        self.assertEqual(result["confidence"], 0.5)


# ---------------------------------------------------------------------------
# Dedup fingerprint: excludes created_at/author/confidence. Same type+title+
# content -> same id; differing content/type/title -> different id.
# ---------------------------------------------------------------------------


class DedupFingerprintTests(RefereeTestCase):
    def test_fingerprint_excludes_created_at_author_confidence(self) -> None:
        """The fingerprint is a pure function of type+title+content; nothing
        else feeds it, so a re-proposal at a different time/author/confidence
        produces the identical fingerprint."""
        fp1 = ops.content_fingerprint("claim", "Title", "Body content.")
        fp2 = ops.content_fingerprint("claim", "Title", "Body content.")
        self.assertEqual(fp1, fp2)

    def test_same_type_title_content_dedups_to_existing_id(self) -> None:
        """add_node returns the existing id (created=False) on a fingerprint dup
        even when author/confidence differ, instead of forking the graph."""
        first = ops.add_node(
            self.store,
            node_type="claim",
            title="Dedup target",
            content="The same idea twice.",
            author="alice",
            confidence=0.4,
        )
        self.assertTrue(first["created"])
        second = ops.add_node(
            self.store,
            node_type="claim",
            title="Dedup target",
            content="The same idea twice.",
            author="bob",  # different author
            confidence=0.9,  # different confidence
        )
        self.assertFalse(second["created"])
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        self.assertEqual(len(self.store.list_nodes()), 1)

    def test_differing_content_produces_a_different_id(self) -> None:
        same_title = "Shared title"
        a = ops.add_node(
            self.store, node_type="claim", title=same_title, content="Content A."
        )
        b = ops.add_node(
            self.store, node_type="claim", title=same_title, content="Content B."
        )
        self.assertTrue(a["created"])
        self.assertTrue(b["created"])
        self.assertNotEqual(a["node_id"], b["node_id"])
        self.assertNotEqual(a["fingerprint"], b["fingerprint"])
        self.assertEqual(len(self.store.list_nodes()), 2)

    def test_node_type_participates_in_the_fingerprint(self) -> None:
        """Same title+content but a different node_type is a different node."""
        fp_claim = ops.content_fingerprint("claim", "Same", "Same body.")
        fp_evidence = ops.content_fingerprint("evidence", "Same", "Same body.")
        self.assertNotEqual(fp_claim, fp_evidence)

        claim = ops.add_node(
            self.store, node_type="claim", title="Same", content="Same body."
        )
        evidence = ops.add_node(
            self.store, node_type="evidence", title="Same", content="Same body."
        )
        self.assertTrue(claim["created"])
        self.assertTrue(evidence["created"])
        self.assertNotEqual(claim["node_id"], evidence["node_id"])
        self.assertEqual(len(self.store.list_nodes()), 2)

    def test_title_participates_in_the_fingerprint(self) -> None:
        fp_a = ops.content_fingerprint("claim", "Title A", "Body.")
        fp_b = ops.content_fingerprint("claim", "Title B", "Body.")
        self.assertNotEqual(fp_a, fp_b)

    def test_fingerprint_normalizes_case_and_whitespace(self) -> None:
        """Normalization (strip + lower) means cosmetic case/whitespace
        differences collapse to the same fingerprint and dedup."""
        fp_a = ops.content_fingerprint("claim", "  Title  ", "Body Content")
        fp_b = ops.content_fingerprint("CLAIM", "title", "body content")
        self.assertEqual(fp_a, fp_b)

        first = ops.add_node(
            self.store, node_type="claim", title="Title", content="Body Content"
        )
        second = ops.add_node(
            self.store,
            node_type="claim",
            title="  TITLE  ",
            content="body content",
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(len(self.store.list_nodes()), 1)

    def test_find_duplicate_reports_existing_id_and_fingerprint(self) -> None:
        created = ops.add_node(
            self.store, node_type="claim", title="Find me", content="Body."
        )
        dup = ops.find_duplicate(
            self.store, node_type="claim", title="Find me", content="Body."
        )
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["existing_id"], created["node_id"])
        self.assertEqual(
            dup["fingerprint"], ops.content_fingerprint("claim", "Find me", "Body.")
        )

        miss = ops.find_duplicate(
            self.store, node_type="claim", title="Find me", content="Different body."
        )
        self.assertFalse(miss["duplicate"])
        self.assertIsNone(miss["existing_id"])


if __name__ == "__main__":
    unittest.main()
