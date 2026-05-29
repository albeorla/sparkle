"""Phase 2 integrity-floor coverage: the adversary must be real.

Phase 1 proved one weakness: the "was this claim challenged?" gate only counted
that an inbound contradicts edge EXISTS, so a single model could ratify its own
claim off a self-written strawman. Phase 2 pins two floor invariants inside the
ops seam (src/sparkle/ops.py) so every front-end inherits them:

  1. A claim cannot be its own objection: a self-loop contradicts edge
     (from_id == to_id) is rejected at the edge-write boundary.
  2. The judge's ratify move can demand a genuinely DIFFERENT attacker via the
     opt-in require_distinct_adversary flag on rule(). When True, a same-author
     objection does NOT satisfy the challenge (rule refuses with an actionable
     message) while a different-author objection DOES. When left at the default
     (False), the existing human-trust behavior is byte-identical.

Pure-stdlib unittest. Each test isolates its graph under a TemporaryDirectory so
there are no side effects. We drive ops.* directly (no CLI) since these floor
invariants live in ops, not in any front-end. Exact error messages are asserted
where the spec defines them.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from src.sparkle import ops
from src.sparkle.graph import GraphStore
from src.sparkle.models import Edge, Node


# The exact messages the seam raises (asserted verbatim where the spec pins them).
SELF_LOOP_MSG = (
    "a contradicts edge cannot point at itself; a claim cannot be its own objection"
)
NEVER_ATTACKED_MSG = (
    "you may not ratify a claim the adversary never attacked; "
    "run a challenge first (add an objection/contradicts edge)"
)


class IdentityRuleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -- helpers -----------------------------------------------------------

    def _claim(self, title: str = "A claim", content: str = "Under debate.", **kw) -> str:
        """Add a claim node, returning its id. ``author`` may be passed via kw."""
        return self.store.add_node(
            Node(node_type="claim", title=title, content=content, **kw)
        )

    def _objection_node(self, title: str, author: str = "local") -> str:
        """An objection node with an explicit author (the would-be attacker)."""
        return self.store.add_node(
            Node(
                node_type="objection",
                title=title,
                content="Here is the attack.",
                author=author,
            )
        )

    def _attack(self, claim_id: str, *, author: str = "local", title: str = "Objection") -> str:
        """Wire an objection node --contradicts--> claim, authored by ``author``."""
        obj = self._objection_node(title, author=author)
        self.store.add_edge(Edge(from_id=obj, to_id=claim_id, relation="contradicts"))
        return obj

    def _distinct_adversary_refusal_msg(self, claim_id: str, claim_author: str) -> str:
        """The exact message rule() raises when only a self-authored objection exists.

        Mirrors the f-string in ops.rule() so the assertion is byte-exact rather
        than a loose substring match.
        """
        return (
            f"claim {claim_id[:12]} was only objected to by its own author "
            f"({claim_author!r}); a self-written objection cannot ratify it. "
            "A different author (a distinct critic) must record the contradicts "
            "objection before this claim can be ruled"
        )

    # -- 1. self-loop contradicts is rejected ------------------------------

    def test_self_loop_contradicts_edge_is_rejected_via_add_edge(self) -> None:
        """A claim cannot be its own objection: from_id == to_id on a contradicts
        edge raises the exact floor message at the public add_edge boundary."""
        claim = self._claim("Self-attacker", "Tries to object to itself.")
        with self.assertRaises(ValueError) as cm:
            ops.add_edge(
                self.store,
                from_ref=claim,
                to_ref=claim,
                relation="contradicts",
            )
        self.assertEqual(str(cm.exception), SELF_LOOP_MSG)

    def test_self_loop_contradicts_rejected_even_via_id_prefix(self) -> None:
        """The ban resolves prefixes first, so a prefix-vs-full-id self-loop is
        still caught (both refs resolve to the same node)."""
        claim = self._claim("Prefix self-attacker", "Same node via prefix.")
        with self.assertRaises(ValueError) as cm:
            ops.add_edge(
                self.store,
                from_ref=claim[:12],  # prefix resolves to the same node
                to_ref=claim,
                relation="contradicts",
            )
        self.assertEqual(str(cm.exception), SELF_LOOP_MSG)

    def test_self_loop_contradicts_blocked_through_fused_add_node_link(self) -> None:
        """The ban sits at the single edge-write chokepoint, so even add_node's
        fused self-link with relation 'contradicts' is impossible. (add_node's
        fused link makes the NEW node the source; pointing it at itself is the
        path a model loop might take, so it must be blocked here too.)"""
        # A fused contradicts link from the new node to itself: the new node is
        # the link source AND the link target -> a self-loop contradicts.
        first = self._claim("Target", "Exists so we can self-link the new node.")
        # add_node's fused link sets from_id=new node; to point at itself we
        # link_to the new node's own id. We cannot know the id before creation,
        # so instead create a node, then add the self-edge through add_edge —
        # the chokepoint is shared, so this asserts the same guard fires.
        new_id = ops.add_node(
            self.store,
            node_type="objection",
            title="Would-be self objector",
            content="Body.",
        )["node_id"]
        with self.assertRaises(ValueError) as cm:
            ops.add_edge(
                self.store,
                from_ref=new_id,
                to_ref=new_id,
                relation="contradicts",
            )
        self.assertEqual(str(cm.exception), SELF_LOOP_MSG)
        # Sanity: 'first' is unused as an edge endpoint; no edge was written.
        self.assertEqual(self.store.list_edges(), [])

    def test_self_loop_allowed_for_non_contradicts_relations(self) -> None:
        """The ban is contradicts-specific. A self-loop with a different relation
        is NOT blocked by this floor (the floor only forbids self-objection)."""
        claim = self._claim("Self refiner", "Refines itself, oddly.")
        # 'refines' self-loop is not the floor's concern; it must not raise the
        # self-loop contradicts message.
        result = ops.add_edge(
            self.store,
            from_ref=claim,
            to_ref=claim,
            relation="refines",
        )
        self.assertEqual(result["relation"], "refines")
        self.assertEqual(result["from_id"], result["to_id"])

    def test_distinct_contradicts_edge_between_two_nodes_is_allowed(self) -> None:
        """A normal attack (objection --contradicts--> claim, distinct nodes) is
        unaffected by the self-loop ban."""
        claim = self._claim()
        obj = self._objection_node("Real objection")
        result = ops.add_edge(
            self.store,
            from_ref=obj,
            to_ref=claim,
            relation="contradicts",
        )
        self.assertEqual(result["relation"], "contradicts")
        self.assertNotEqual(result["from_id"], result["to_id"])

    # -- 2a. default (False): existing human behavior is UNCHANGED ----------

    def test_default_rule_unaffected_an_objection_satisfies_the_challenge(self) -> None:
        """With the default (require_distinct_adversary=False), the existing
        author-blind behavior holds: any inbound contradicts unlocks a ruling,
        even when the objection shares the claim's author. This pins that the new
        flag did not change the human-trust path."""
        claim = self._claim(author="local")
        self._attack(claim, author="local")  # same author as the claim
        result = ops.rule(self.store, claim, verdict="accept", rationale="It holds.")
        self.assertFalse(result["settled"])
        self.assertEqual(result["decision"]["node_type"], "decision")
        self.assertEqual(result["decision"]["title"], "Ruling: accept")

    def test_default_rule_still_refuses_an_unchallenged_claim(self) -> None:
        """The base never-attacked guard is preserved under the default: a claim
        with no inbound contradicts raises the exact never-attacked message."""
        claim = self._claim("Unchallenged", "Nobody attacked it.")
        with self.assertRaises(ValueError) as cm:
            ops.rule(self.store, claim, verdict="accept")
        self.assertEqual(str(cm.exception), NEVER_ATTACKED_MSG)

    # -- 2b. require_distinct_adversary=True: same-author objection REFUSED --

    def test_distinct_adversary_refuses_a_self_authored_objection(self) -> None:
        """With require_distinct_adversary=True, an objection authored by the
        SAME author as the claim does NOT count as a challenge: rule() refuses
        with the exact self-objection message."""
        claim = self._claim(author="proposer")
        self._attack(claim, author="proposer")  # self-written strawman
        with self.assertRaises(ValueError) as cm:
            ops.rule(
                self.store,
                claim,
                verdict="accept",
                require_distinct_adversary=True,
            )
        self.assertEqual(
            str(cm.exception),
            self._distinct_adversary_refusal_msg(claim, "proposer"),
        )

    def test_distinct_adversary_refusal_uses_default_local_author(self) -> None:
        """When neither claim nor objection sets an author, both default to
        'local'; the refusal still fires and names 'local' in the message."""
        claim = self._claim()  # author defaults to 'local'
        self._attack(claim)  # objection author defaults to 'local'
        with self.assertRaises(ValueError) as cm:
            ops.rule(
                self.store,
                claim,
                verdict="accept",
                require_distinct_adversary=True,
            )
        self.assertEqual(
            str(cm.exception),
            self._distinct_adversary_refusal_msg(claim, "local"),
        )

    def test_distinct_adversary_refuses_before_any_decision_is_written(self) -> None:
        """A refused distinct-adversary ruling must not leave a decision node or
        a ratified version behind — the invariant fires before any write."""
        claim = self._claim(author="proposer")
        self._attack(claim, author="proposer")
        with self.assertRaises(ValueError):
            ops.rule(
                self.store,
                claim,
                verdict="accept",
                settle=True,
                require_distinct_adversary=True,
            )
        # No decision node was created, and the claim is untouched.
        decisions = [
            n for _, n in self.store.list_nodes() if n["node_type"] == "decision"
        ]
        self.assertEqual(decisions, [])
        self.assertEqual(self.store.get_node(claim)["status"], "active")

    # -- 2c. require_distinct_adversary=True: different-author objection OK --

    def test_distinct_adversary_accepts_a_different_author_objection(self) -> None:
        """With require_distinct_adversary=True, an objection authored by a
        DIFFERENT author (a real critic) satisfies the challenge and rule()
        proceeds to write a decision."""
        claim = self._claim(author="proposer")
        self._attack(claim, author="critic")  # genuinely different attacker
        result = ops.rule(
            self.store,
            claim,
            verdict="accept",
            rationale="A real critic attacked it.",
            require_distinct_adversary=True,
        )
        self.assertFalse(result["settled"])
        self.assertEqual(result["decision"]["node_type"], "decision")
        self.assertEqual(result["decision"]["title"], "Ruling: accept")
        self.assertEqual(result["decision_link"]["relation"], "evaluates")
        self.assertEqual(result["decision_link"]["to_id"], claim)

    def test_distinct_adversary_settle_with_real_critic_ratifies(self) -> None:
        """A distinct-critic objection unlocks a full settle: the ratified claim
        version is written under require_distinct_adversary=True."""
        claim = self._claim(author="proposer")
        self._attack(claim, author="critic")
        result = ops.rule(
            self.store,
            claim,
            verdict="reject",
            settle=True,
            require_distinct_adversary=True,
        )
        self.assertTrue(result["settled"])
        self.assertEqual(result["ratified_claim"]["status"], "ratified")
        self.assertNotEqual(result["ratified_claim"]["node_id"], claim)

    def test_distinct_adversary_objection_via_add_branch_critic_author(self) -> None:
        """The autonomous-engine path uses add_branch(template='objection',
        author='critic') to record the attack. That contradicts edge from a
        distinct critic satisfies the distinct-adversary gate end-to-end, proving
        the floor works through the real attack move, not just hand-wired edges."""
        claim = self._claim(author="proposer", title="Model claim", content="Body.")
        ops.add_branch(
            self.store,
            from_ref=claim,
            template="objection",
            title="Critic objection",
            content="The critic attacks.",
            author="critic",
        )
        result = ops.rule(
            self.store,
            claim,
            verdict="accept",
            require_distinct_adversary=True,
        )
        self.assertEqual(result["decision"]["node_type"], "decision")

    def test_distinct_adversary_refuses_when_branch_objection_self_authored(self) -> None:
        """The mirror: an add_branch objection authored by the proposer itself is
        a self-strawman and is REFUSED under require_distinct_adversary=True."""
        claim = self._claim(author="proposer", title="Model claim", content="Body.")
        ops.add_branch(
            self.store,
            from_ref=claim,
            template="objection",
            title="Self objection",
            content="The proposer attacks itself.",
            author="proposer",
        )
        with self.assertRaises(ValueError) as cm:
            ops.rule(
                self.store,
                claim,
                verdict="accept",
                require_distinct_adversary=True,
            )
        self.assertEqual(
            str(cm.exception),
            self._distinct_adversary_refusal_msg(claim, "proposer"),
        )

    def test_distinct_adversary_mixed_objections_one_distinct_is_enough(self) -> None:
        """If a self-authored objection AND a distinct-critic objection both
        exist, the distinct one satisfies the gate (it only takes one real
        adversary)."""
        claim = self._claim(author="proposer")
        self._attack(claim, author="proposer", title="Self strawman")
        self._attack(claim, author="critic", title="Real attack")
        result = ops.rule(
            self.store,
            claim,
            verdict="accept",
            require_distinct_adversary=True,
        )
        self.assertEqual(result["decision"]["node_type"], "decision")

    def test_distinct_adversary_true_still_requires_some_objection(self) -> None:
        """require_distinct_adversary does not bypass the base challenge gate: an
        unchallenged claim still raises the never-attacked message first, not the
        distinct-adversary one."""
        claim = self._claim(author="proposer", title="Unchallenged", content="None.")
        with self.assertRaises(ValueError) as cm:
            ops.rule(
                self.store,
                claim,
                verdict="accept",
                require_distinct_adversary=True,
            )
        self.assertEqual(str(cm.exception), NEVER_ATTACKED_MSG)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
