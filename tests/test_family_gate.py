"""Cross-FAMILY adversary gate + rewrite re-challenge — stub-driven, no live CLI.

Plain English: this proves the autonomous debate only lets the judge ratify a
claim when a *genuinely different model family* attacked it (Claude vs GPT), not
just a different author name. A Claude proposal poked at only by other Claude
roles must NOT settle; it takes the GPT critic (a different family) to earn a
ruling. The config refuses to even start if the adversary (critic) is wired to
the same family as the proposer. And after a claim is rewritten, the old
objection that gets copied onto the new version no longer counts — the rewrite
must draw a brand-new cross-family attack before it can be ratified (CHANGE A).

This file is part of the ZERO-DEPENDENCY BASE SUITE. It drives the engine with a
deterministic scripted stub thinker, so it imports NO ``anthropic`` / ``openai``,
spawns NO ``claude``/``codex`` CLI, and makes NO network call. The live
three-family debate is a manual human acceptance step, never run here.

Each test isolates its graph under a tempfile.TemporaryDirectory (no shared
.sparkle state), matching tests/test_harness.py conventions.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.sparkle import harness, ops
from src.sparkle.graph import GraphStore
from src.sparkle.harness import (
    HarnessConfig,
    RoleAgent,
    _cross_family_objection,
)


# ---------------------------------------------------------------------------
# Minimal scripted stub thinker (structurally satisfies harness.Thinker).
# No anthropic, no network, no key. Reports zero tokens. We only need the judge
# role here, so the stub is tiny: it returns whatever JSON move text it was
# handed for the role being consulted.
# ---------------------------------------------------------------------------


class StubThinker:
    """A scripted model backend keyed by role; returns a fixed JSON move text."""

    def __init__(self, script: dict[str, str]) -> None:
        self.script = script
        self.tokens_used = 0

    def think(self, *, role, system, prompt, context=None):  # noqa: ANN001
        source = self.script.get(role)
        if source is None:
            return json.dumps({"move": "done", "reason": f"no script for {role}"})
        return source


def _move(name: str, **kwargs) -> str:
    """Render a move dict as the JSON-string blob a thinker would return."""
    return json.dumps({"move": name, **kwargs})


class FamilyGateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)
        # Locked-default config: proposer/judge/evidence/synth = claude family,
        # critic = codex family. Env-independent because all five backends and
        # models are set explicitly, so an ambient SPARKLE_*_BACKEND does not
        # leak into the test.
        self.config = HarnessConfig(
            proposer_backend="claude",
            critic_backend="codex",
            judge_backend="claude",
            evidence_backend="claude",
            synthesizer_backend="claude",
            proposer_model="model-proposer",
            critic_model="model-critic",
            judge_model="model-judge",
            evidence_model="model-evidence",
            synthesizer_model="model-synth",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -- helpers ------------------------------------------------------------

    def _proposer_claim(self) -> str:
        """A claim authored by the proposer (claude family)."""
        claim = ops.add_node(
            self.store,
            node_type="claim",
            title="Instrumental music boosts coding focus",
            content="A claim authored by the proposer to be attacked.",
            author=self.config.author_for_role("proposer"),
            model_authored=True,
        )
        return claim["node_id"]

    def _objection(self, claim_id: str, *, author: str, title: str) -> dict:
        """Attach an objection (a contradicts edge) authored by ``author``."""
        return ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="objection",
            title=title,
            content=f"{author} attacks the claim: {title}.",
            author=author,
            model_authored=True,
        )

    def _run_judge(self, claim_id: str) -> harness.MoveResult:
        """Run only the judge role agent against ``claim_id`` and return the
        recorded MoveResult (refused or written; never raises).

        The verdict is the documented affirming token ('upheld') so the
        dispatch's verdict-settle coupling honors settle — these tests probe the
        cross-FAMILY gate, not the verdict vocabulary.
        """
        thinker = StubThinker(
            {"judge": _move("rule", verdict="upheld", settle=True)}
        )
        agent = RoleAgent("judge", self.config, run_id="run-fam")
        prompt, context = harness._render_prompt(
            self.store, "judge", "Does music help coding?", claim_id
        )
        return agent.act(
            self.store, thinker, target_id=claim_id, prompt=prompt, context=context
        )

    def _ratified_versions(self) -> list[dict]:
        return [
            n
            for n in self.store.read()["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]

    # -- (1) Judge REFUSES without a cross-FAMILY objection -----------------

    def test_judge_refuses_when_only_objection_is_same_family(self) -> None:
        """A claude proposer's claim attacked ONLY by another claude role
        (a DIFFERENT author but the SAME backend family) does not ratify: the
        engine's judge refuses because no objection came from a different model
        family (Claude vs GPT)."""
        claim_id = self._proposer_claim()
        # evidence_gatherer is a DIFFERENT author than the proposer, but maps to
        # the SAME family (claude). Its 'oppose'/objection writes a contradicts
        # edge that clears the seam's author-distinct floor but NOT the engine's
        # cross-family gate.
        objection = self._objection(
            claim_id, author="evidence_gatherer", title="Same-family attack"
        )

        # Sanity: a contradicts edge exists and is author-distinct (the seam's
        # floor passes), so the only thing standing between this and a ruling is
        # the cross-family gate.
        self.assertEqual(objection["link"]["relation"], "contradicts")
        self.assertTrue(
            ops._distinct_adversary_objection(self.store, claim_id, "proposer"),
            "the author-distinct floor is satisfied (distinct author)",
        )
        # The cross-family helper itself says no (same family).
        self.assertFalse(
            _cross_family_objection(self.store, claim_id, "proposer", self.config)
        )

        result = self._run_judge(claim_id)

        self.assertEqual(result.outcome, "refused")
        self.assertIn("FAMILY", result.message)
        self.assertIn("Claude vs GPT", result.message)
        # No ratified version was written — the claim is not settled.
        self.assertEqual(self._ratified_versions(), [])

    def test_judge_ratifies_with_a_cross_family_objection(self) -> None:
        """The mirror: when the codex (GPT) critic — a genuinely DIFFERENT model
        family than the claude proposer — records the objection, the engine's
        judge ratifies."""
        claim_id = self._proposer_claim()
        self._objection(claim_id, author="critic", title="Cross-family attack")

        # The cross-family helper agrees: critic (codex) differs from the claim's
        # claude family.
        self.assertTrue(
            _cross_family_objection(self.store, claim_id, "proposer", self.config)
        )

        result = self._run_judge(claim_id)

        self.assertEqual(result.outcome, "written")
        ratified = self._ratified_versions()
        self.assertEqual(len(ratified), 1, "the claim was ratified")

    def test_same_family_plus_cross_family_objection_does_ratify(self) -> None:
        """A same-family objection alone does not ratify, but adding ONE genuine
        cross-family objection alongside it is enough — the gate needs AT LEAST
        ONE different-family attack, not that every objection be cross-family."""
        claim_id = self._proposer_claim()
        # Same-family objection first (does not satisfy the gate on its own).
        self._objection(
            claim_id, author="evidence_gatherer", title="Same-family attack"
        )
        self.assertFalse(
            _cross_family_objection(self.store, claim_id, "proposer", self.config),
            "same-family objection alone does not clear the gate",
        )

        # Now add the cross-family (codex critic) objection.
        self._objection(claim_id, author="critic", title="Cross-family attack")
        self.assertTrue(
            _cross_family_objection(self.store, claim_id, "proposer", self.config),
            "one cross-family objection clears the gate even alongside a "
            "same-family one",
        )

        result = self._run_judge(claim_id)
        self.assertEqual(result.outcome, "written")
        self.assertEqual(len(self._ratified_versions()), 1)

    def test_human_objection_counts_as_cross_family(self) -> None:
        """A human ('local') objection against a model claim is treated as its
        own distinct family, so it satisfies the cross-family gate — a human
        adversary is genuinely different from the claude proposer."""
        claim_id = self._proposer_claim()
        # Default author for a hand-written objection is 'local' (a human), which
        # family_for_author maps to itself (not a known role), so it is a
        # distinct family from the proposer's claude family.
        self.assertEqual(self.config.family_for_author("local"), "local")
        self._objection(claim_id, author="local", title="Human attack")

        self.assertTrue(
            _cross_family_objection(self.store, claim_id, "proposer", self.config)
        )
        result = self._run_judge(claim_id)
        self.assertEqual(result.outcome, "written")
        self.assertEqual(len(self._ratified_versions()), 1)

    # -- (2) HarnessConfig invariant: critic family != proposer family ------

    def test_config_rejects_same_family_critic_and_proposer(self) -> None:
        """The locked contract: the adversary (critic) MUST run on a different
        backend family than the proposer, so the config refuses to construct when
        both are wired to the same family (e.g. both claude)."""
        with self.assertRaisesRegex(ValueError, "critic backend family must differ"):
            HarnessConfig(
                proposer_backend="claude",
                critic_backend="claude",
                judge_backend="claude",
            )

    def test_config_rejects_same_family_critic_from_env(self) -> None:
        """The same invariant fires when the env override forces the critic onto
        the proposer's family."""
        import os

        saved = {
            k: os.environ.get(k)
            for k in ("SPARKLE_PROPOSER_BACKEND", "SPARKLE_CRITIC_BACKEND")
        }
        os.environ["SPARKLE_PROPOSER_BACKEND"] = "codex"
        os.environ["SPARKLE_CRITIC_BACKEND"] = "codex"
        try:
            with self.assertRaisesRegex(
                ValueError, "critic backend family must differ"
            ):
                HarnessConfig()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_default_config_has_cross_family_adversary(self) -> None:
        """With no env overrides the locked defaults already satisfy the
        invariant: proposer=claude, critic=codex (a genuinely different family)
        and the judge MAY share the proposer's family (judge=claude)."""
        import os

        saved = {
            k: os.environ.pop(k, None)
            for k in (
                "SPARKLE_PROPOSER_BACKEND",
                "SPARKLE_CRITIC_BACKEND",
                "SPARKLE_JUDGE_BACKEND",
                "SPARKLE_EVIDENCE_GATHERER_BACKEND",
                "SPARKLE_SYNTHESIZER_BACKEND",
            )
        }
        try:
            config = HarnessConfig()
            self.assertEqual(config.backend_for_role("proposer"), "claude")
            self.assertEqual(config.backend_for_role("critic"), "codex")
            self.assertNotEqual(
                config.backend_for_role("critic"),
                config.backend_for_role("proposer"),
            )
            # The judge is allowed to share the proposer's family.
            self.assertEqual(
                config.backend_for_role("judge"),
                config.backend_for_role("proposer"),
            )
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    # -- (3) CHANGE A: a rewritten claim must be re-challenged --------------

    def test_rewrite_with_only_rehomed_objection_is_not_ratifiable(self) -> None:
        """CHANGE A. A claim earns a real cross-family objection, then is
        rewritten (revise rehomes the old objection onto the new version tagged
        rehomed=True). The rehomed copy no longer counts, so the rewritten claim
        is NOT ratifiable until a FRESH cross-family objection lands."""
        claim_id = self._proposer_claim()
        # The original claim earns a genuine cross-family (codex critic) attack.
        self._objection(claim_id, author="critic", title="Original cross-family attack")
        self.assertTrue(
            _cross_family_objection(self.store, claim_id, "proposer", self.config),
            "the original claim has a fresh cross-family objection",
        )

        # Rewrite the claim. revise() supersedes it with a new version and
        # rehomes (copies) the inbound objection edge, tagged rehomed=True.
        revised = ops.revise(
            self.store,
            claim_id,
            content="A sharpened restatement of the same claim.",
            rehome_edges=True,
        )
        new_id = revised["node_id"]
        self.assertNotEqual(new_id, claim_id)

        # The rehomed objection edge IS present (kept for lineage/display) and is
        # tagged rehomed=True.
        data = self.store.read()
        rehomed_into_new = [
            e
            for e in data["edges"].values()
            if e["relation"] == "contradicts"
            and e["to_id"] == new_id
            and e.get("metadata", {}).get("rehomed")
        ]
        self.assertEqual(
            len(rehomed_into_new), 1, "the old objection was rehomed onto the rewrite"
        )

        # ...but the rehomed copy does NOT count toward the cross-family gate, so
        # the rewritten claim is not yet ratifiable.
        self.assertFalse(
            _cross_family_objection(self.store, new_id, "proposer", self.config),
            "a rehomed objection is not a fresh adversary",
        )
        # And the seam's author-distinct floor also skips it.
        self.assertFalse(
            ops._distinct_adversary_objection(self.store, new_id, "proposer"),
            "the seam's floor also skips the rehomed objection",
        )

        # The judge agent refuses to rule the rewritten claim.
        result = self._run_judge(new_id)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("FAMILY", result.message)
        self.assertEqual(
            self._ratified_versions(), [], "the rewrite was not ratified"
        )

        # Now add a FRESH cross-family objection to the rewritten claim.
        self._objection(new_id, author="critic", title="Fresh cross-family attack")
        self.assertTrue(
            _cross_family_objection(self.store, new_id, "proposer", self.config),
            "the rewrite earns a fresh cross-family objection",
        )

        # The judge can now ratify the rewritten claim.
        result = self._run_judge(new_id)
        self.assertEqual(result.outcome, "written")
        ratified = self._ratified_versions()
        self.assertEqual(len(ratified), 1, "the rewrite is ratified after a fresh attack")

    def test_cross_family_helper_skips_rehomed_edges_directly(self) -> None:
        """Unit-level CHANGE A: the cross-family helper skips any contradicts edge
        tagged rehomed=True, so a copied-over objection is invisible to the gate
        even though the edge still exists in the graph."""
        claim_id = self._proposer_claim()
        # Hand-build a rehomed cross-family objection by stamping the edge
        # directly, mirroring what _supersede_node does on a revise.
        objection_node = ops.add_node(
            self.store,
            node_type="objection",
            title="A stale copied-over attack",
            content="Originally a critic attack, now rehomed onto a rewrite.",
            author="critic",
            model_authored=True,
        )
        ops.add_edge(
            self.store,
            from_ref=objection_node["node_id"],
            to_ref=claim_id,
            relation="contradicts",
            metadata={"rehomed": True},
        )

        # The critic is cross-family, but because the only edge is rehomed the
        # gate returns False.
        self.assertFalse(
            _cross_family_objection(self.store, claim_id, "proposer", self.config),
            "a rehomed cross-family objection does not clear the gate",
        )

        # A non-rehomed edge from the same kind of source DOES clear it, proving
        # the skip is what makes the difference (not the source family).
        fresh = ops.add_node(
            self.store,
            node_type="objection",
            title="A fresh attack",
            content="A genuinely new critic objection.",
            author="critic",
            model_authored=True,
        )
        ops.add_edge(
            self.store,
            from_ref=fresh["node_id"],
            to_ref=claim_id,
            relation="contradicts",
        )
        self.assertTrue(
            _cross_family_objection(self.store, claim_id, "proposer", self.config),
            "a fresh (non-rehomed) cross-family objection clears the gate",
        )


if __name__ == "__main__":
    unittest.main()
