"""Phase 2 engine coverage — the autonomous adversarial loop, stub-driven.

Plain English: this proves the self-driving debate works end to end without any
network, API key, or the ``anthropic`` package. A deterministic scripted
"thinker" plays each role (proposer, critic, judge, ...), and we assert that a
full round produces a coherent graph (a claim, a *different-author* objection,
and a judge's ruling), that the judge's ratification genuinely required the
cross-author challenge (a strawman written under the proposer's own identity
would NOT ratify), that the loop stops on its safety conditions (per-phase round
caps, the hard runaway backstop, and an explicit done move), and that every
graph mutation flowed through ``ops`` so the whole loop is visible to the
run-region surface (the run summary captures the claim + objection + decision).

This file is part of the ZERO-DEPENDENCY BASE SUITE. It uses a STUB thinker, so
it imports NO ``anthropic`` and makes NO live call. The live three-model debate
is a manual human acceptance step, never validated here.

Conventions match tests/test_cli.py and tests/test_debate_loop.py: pure-stdlib
unittest, each test isolates its graph under a tempfile.TemporaryDirectory (no
shared .sparkle state), no side effects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from src.sparkle import harness, ops
from src.sparkle.graph import GraphStore
from src.sparkle.harness import (
    AutonomousEngine,
    HarnessConfig,
    RoleAgent,
    run_region_summary,
)


# ---------------------------------------------------------------------------
# Deterministic scripted thinker — satisfies harness.Thinker structurally.
# No anthropic, no network, no key. Reports zero tokens so the cost ceiling
# never trips unless a test sets a tiny call ceiling on purpose.
# ---------------------------------------------------------------------------


class StubThinker:
    """A scripted model backend keyed by role.

    Constructed with ``{role: move-source}``. A move-source is either a JSON
    string (the exact move text the engine should receive every time the role
    is called) or a callable ``(call_index, context) -> str`` so a role can
    return different moves on successive calls (e.g. object on the first
    critique round, then stop). The role agent injects the live target id when
    the script omits ``target``, so a script only names the move TYPE and text.

    Reports ``tokens_used = 0`` so the engine's optional cost ceiling stays off
    by default. Records a per-role call count so tests can assert how often each
    role was consulted.
    """

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.calls: list[str] = []
        self.calls_by_role: dict[str, int] = {}
        self.tokens_used = 0

    def think(self, *, role, system, prompt, context=None):  # noqa: ANN001
        self.calls.append(role)
        index = self.calls_by_role.get(role, 0)
        self.calls_by_role[role] = index + 1
        source = self.script.get(role)
        if source is None:
            # No script for this role -> emit a done move so the loop is finite.
            return json.dumps({"move": "done", "reason": f"no script for {role}"})
        if callable(source):
            return source(index, context or {})
        return source


def _move(name: str, **kwargs) -> str:
    """Render a move dict as the JSON-string blob a thinker would return."""
    return json.dumps({"move": name, **kwargs})


# A canonical happy-path script: propose, object exactly once, gather nothing,
# judge settles, synthesizer harvests. The critic objects on its FIRST call and
# then emits a deliberately non-actionable blob on later critique rounds. A non-
# JSON blob is recorded as 'malformed' (no node written, no second objection,
# and crucially NOT a stop) so the critique phase runs out its rounds and the
# loop proceeds to the judge. A 'done' here would stop the WHOLE loop before the
# judge ever rules, which is correct engine behavior but not the happy path.
def _happy_script() -> dict[str, object]:
    def critic(index, _context):
        if index == 0:
            return _move(
                "object",
                title="Lyrics pull attention away",
                content="Songs with words steal working memory from the code.",
            )
        return "Objection already lodged; nothing further."

    def gatherer(index, _context):
        return "No evidence to add this round."

    return {
        "proposer": _move(
            "propose",
            title="Music boosts coding focus",
            content="Instrumental music improves focus while coding.",
        ),
        "critic": critic,
        "evidence_gatherer": gatherer,
        "judge": _move(
            "rule",
            verdict="upheld",
            rationale=(
                "the lyrics objection targets songs with words; the claim is "
                "about INSTRUMENTAL music, so the objection does not defeat it"
            ),
            settle=True,
        ),
        "synthesizer": _move(
            "harvest",
            title="Settled: instrumental music helps coding focus",
            content="Instrumental music improves focus; the lyrics caveat is separate.",
        ),
    }


class HarnessEngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _config(self, **overrides) -> HarnessConfig:
        """A HarnessConfig with explicit per-role models, env-independent.

        Set proposer/critic/judge models explicitly so the test does not depend
        on the ambient SPARKLE_*_MODEL env. The backend families are left at
        their locked defaults (proposer=claude, critic=codex, judge=claude), so
        the cross-FAMILY invariant — critic family != proposer family — already
        holds; the model strings are free to overlap because the gate counts the
        family, not the model tier.
        """
        kwargs = dict(
            proposer_model="model-proposer",
            critic_model="model-critic",
            judge_model="model-judge",
            evidence_model="model-evidence",
            synthesizer_model="model-synth",
        )
        kwargs.update(overrides)
        return HarnessConfig(**kwargs)

    # -- Verifier dispatch: a 'verify' move writes a tally-neutral check ----

    def test_verify_move_writes_a_tally_neutral_verification_node(self) -> None:
        """A verifier 'verify' move attaches a verification node to the claim via
        an 'evaluates' edge: run-region tagged and judge-visible, but tally-neutral
        (it does not shift the support/contradict counts or trip the 'judged'
        signal). The full-round scripts never script a 'verify' move, so this is
        the one test that exercises the dispatch branch directly."""
        claim = ops.add_node(
            self.store,
            node_type="claim",
            title="Claim with a cited figure",
            content="A figure attributed to a retrieved source.",
            author="proposer",
            model_authored=True,
        )
        claim_id = claim["node_id"]
        # A support node carrying a citation the verifier would re-fetch.
        ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="support",
            title="Supporting figure",
            content="Figure X, per the cited source.",
            citations=["https://example.org/study"],
            author="evidence_gatherer",
            model_authored=True,
        )

        # The script omits 'target'; the agent injects the live target id.
        thinker = StubThinker(
            {
                "verifier": _move(
                    "verify",
                    title="Citation check",
                    content=(
                        "The cited URL resolves but is a commentary, not the "
                        "study itself; the figure is mis-bound."
                    ),
                    citations=["https://example.org/study"],
                )
            }
        )
        agent = RoleAgent("verifier", self._config(), run_id="run-verify")
        prompt, context = harness._render_prompt(
            self.store, "verifier", "seed", claim_id
        )
        outcome = agent.act(
            self.store, thinker, target_id=claim_id, prompt=prompt, context=context
        )
        self.assertEqual(outcome.outcome, "written")

        data = self.store._read()
        # Stored nodes are keyed BY id, so the id is the dict key.
        verifs = [
            (nid, n)
            for nid, n in data["nodes"].items()
            if n["node_type"] == "verification"
        ]
        self.assertEqual(len(verifs), 1)
        vid, vnode = verifs[0]
        # The verification node is the SOURCE of an 'evaluates' edge into the claim.
        evaluates = [
            e
            for e in data["edges"].values()
            if e["from_id"] == vid
            and e["to_id"] == claim_id
            and e["relation"] == "evaluates"
        ]
        self.assertEqual(len(evaluates), 1)
        # Run-region: the node carries the run id.
        self.assertEqual(vnode["metadata"].get("run_id"), "run-verify")
        # Tally-neutral: support count is untouched and no ruling is implied.
        tally = ops.edge_tally(self.store, claim_id)
        self.assertFalse(tally["has_decision"])
        self.assertEqual(tally["supports"], 1)
        self.assertEqual(tally["contradicts"], 0)

    # -- (1) Full scripted round produces a coherent, run-tagged graph ------

    def test_full_round_produces_claim_distinct_objection_and_ruling(self) -> None:
        """A whole round yields a claim, a DIFFERENT-author objection, and a
        ruling, and every mutation is captured under the run id."""
        thinker = StubThinker(_happy_script())
        engine = AutonomousEngine(self.store, thinker, self._config())

        result = engine.run("Does music help coding?", run_id="run-happy")

        # The proposer's claim is the engine's tracked target.
        self.assertIsNotNone(result["claim_id"])
        claim_id = result["claim_id"]
        claim = self.store.get_node(claim_id)
        self.assertEqual(claim["node_type"], "claim")
        self.assertEqual(claim["author"], "proposer")

        # The judge settled, so the run reaches the 'judged' terminal status.
        self.assertEqual(result["status"], "judged")

        # The run reports moves honestly: the counter is `moves_made` (one per
        # move), NOT a "rounds" label that an unattended operator could misread
        # as multiple adversarial exchanges. The old misleading `rounds_run`
        # key is gone, and the count equals the number of moves recorded.
        self.assertNotIn("rounds_run", result)
        self.assertEqual(result["moves_made"], len(result["moves"]))

        # The objection is a real attack from a DIFFERENT author (the critic),
        # wired objection --contradicts--> claim.
        data = self.store.read()
        contradicts = [
            e
            for e in data["edges"].values()
            if e["relation"] == "contradicts" and e["to_id"] == claim_id
        ]
        self.assertEqual(len(contradicts), 1, "exactly one objection into the claim")
        objection = data["nodes"][contradicts[0]["from_id"]]
        self.assertEqual(objection["node_type"], "objection")
        self.assertEqual(objection["author"], "critic")
        self.assertNotEqual(
            objection["author"], claim["author"],
            "the objection must come from a different author than the claim",
        )

        # A ruling exists: a decision node evaluates the claim.
        decisions = [
            n for n in data["nodes"].values() if n["node_type"] == "decision"
        ]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["author"], "judge")

        # The settled ruling wrote a NEW ratified claim version (frozen original
        # stays active).
        ratified = [
            n
            for n in data["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]
        self.assertEqual(len(ratified), 1)
        self.assertEqual(self.store.get_node(claim_id)["status"], "active")

        # The synthesizer harvested a synthesis node.
        synth = [n for n in data["nodes"].values() if n["node_type"] == "synthesis"]
        self.assertEqual(len(synth), 1)

        # Per-move outcomes record the coherent sequence.
        outcomes = [(m["role"], m["move"], m["outcome"]) for m in result["moves"]]
        self.assertIn(("proposer", "propose", "written"), outcomes)
        self.assertIn(("critic", "object", "written"), outcomes)
        self.assertIn(("judge", "rule", "written"), outcomes)
        self.assertIn(("synthesizer", "harvest", "written"), outcomes)

    def test_run_summary_captures_whole_loop_via_run_id(self) -> None:
        """Every mutation went through ops with the run id stamped, so the
        run-region summary sees the claim, the objection, and the decision."""
        thinker = StubThinker(_happy_script())
        engine = AutonomousEngine(self.store, thinker, self._config())
        result = engine.run("Does music help coding?", run_id="run-region")

        summary = run_region_summary(self.store, "run-region")
        self.assertEqual(summary["run_id"], "run-region")

        # The run captured the proposer's claim, the critic's objection node,
        # the judge's decision, the ratified claim version, and the synthesis.
        by_type = summary["nodes_by_type"]
        self.assertGreaterEqual(by_type.get("claim", 0), 1)
        self.assertEqual(by_type.get("objection", 0), 1)
        self.assertEqual(by_type.get("decision", 0), 1)
        self.assertEqual(by_type.get("synthesis", 0), 1)

        # The objection's contradicts edge and the judge's evaluates edge are
        # tagged with the run id (the run plumbing threaded through add_branch
        # and rule), so the run region sees them.
        by_relation = summary["edges_by_relation"]
        self.assertEqual(by_relation.get("contradicts", 0), 1)
        self.assertGreaterEqual(by_relation.get("evaluates", 0), 1)

        # The engine's own summary matches the standalone recompute.
        self.assertEqual(result["summary"], summary)

        # Sanity: a different run id sees nothing from this run.
        empty = run_region_summary(self.store, "run-nope")
        self.assertEqual(empty["node_count"], 0)
        self.assertEqual(empty["edge_count"], 0)

    def test_run_persists_a_cross_family_manifest(self) -> None:
        """The run records WHICH model family backed each role, so the cross-
        family guarantee is provable from the saved graph after the process
        exits — not just trusted from the in-memory config."""
        thinker = StubThinker(_happy_script())
        engine = AutonomousEngine(self.store, thinker, self._config())
        result = engine.run("Does music help coding?", run_id="run-manifest")

        # Surfaced in the run result AND persisted for after-the-fact audit.
        self.assertEqual(result["manifest"]["run_id"], "run-manifest")
        manifest = ops.run_manifest(self.store, "run-manifest")
        self.assertEqual(manifest, result["manifest"])

        # All six roles are rostered with family + model + author (the citation
        # verifier joined the roster between gather and judge).
        roles = manifest["roles"]
        self.assertEqual(
            set(roles),
            {
                "proposer",
                "critic",
                "evidence_gatherer",
                "verifier",
                "judge",
                "synthesizer",
            },
        )
        # The cross-family invariant held and is recorded: the critic ran a
        # DIFFERENT model family than the proposer (codex vs claude).
        self.assertTrue(manifest["cross_family_ok"])
        self.assertNotEqual(roles["proposer"]["family"], roles["critic"]["family"])
        self.assertEqual(roles["critic"]["family"], "codex")
        self.assertEqual(roles["proposer"]["family"], "claude")
        # The recorded model strings match the config the run actually used.
        self.assertEqual(roles["critic"]["model"], "model-critic")

    def test_manifest_records_configured_families_not_the_thinker_that_ran(self) -> None:
        """Honest limitation, pinned: the manifest reflects the CONFIGURED
        role->family roster, not a runtime probe of which thinker answered.

        Here a SINGLE stub backend runs every role, yet the manifest records the
        config's families (critic=codex). This is faithful in production because
        run_cli_loop builds the real per-role thinkers from this same config; the
        decoupling is only reachable via the test/injection seam. If this ever
        flips to the live thinker's family, update the 'records config' docstrings
        on ops.write_run_manifest and the harness manifest comment.
        """
        thinker = StubThinker(_happy_script())
        engine = AutonomousEngine(self.store, thinker, self._config())
        engine.run("Does music help coding?", run_id="run-cfg")

        manifest = ops.run_manifest(self.store, "run-cfg")
        self.assertEqual(manifest["roles"]["critic"]["family"], "codex")
        self.assertEqual(manifest["roles"]["proposer"]["family"], "claude")

    def test_judge_prompt_surfaces_evidence_content_and_retrieved_sources(self) -> None:
        """The judge must SEE each inbound node's CONTENT and its retrieved-source
        URLs, not just the title — so it can credit web-verified evidence instead
        of discounting it as unverified recall. Regression for a real-run gap
        where the judge said 'no retrieved source URL' about a cited evidence node
        because the prompt only carried titles.
        """
        claim = ops.add_node(
            self.store, node_type="claim", title="Some claim",
            content="the claim body", author="proposer",
        )
        ops.add_branch(
            self.store, from_ref=claim["node_id"], template="objection",
            title="An objection", content="why it might be wrong",
            author="critic", model_authored=True,
        )
        ops.add_node(
            self.store, node_type="evidence", title="Backing study",
            content="A 2022 RCT found X.", author="evidence_gatherer",
            citations=["https://example.org/study"],
            link_to=claim["node_id"], relation="supports", model_authored=True,
        )

        prompt, _ = harness._render_prompt(self.store, "judge", "seed?", claim["node_id"])
        # The retrieved URL and the evidence/objection CONTENT are all visible.
        self.assertIn("https://example.org/study", prompt)
        self.assertIn("retrieved sources:", prompt)
        self.assertIn("A 2022 RCT found X.", prompt)
        self.assertIn("why it might be wrong", prompt)

    def test_synthesizer_is_source_aware_not_blind_recall(self) -> None:
        """The synthesizer SEES the evidence gatherer's retrieved URLs (via
        _render_prompt), so it must credit source-backed figures rather than stamp
        everything '(recalled, unverified)'. Regression for a real-run gap where
        the synthesis called web-sourced figures unverified, contradicting the
        judge's 'real sources' ruling.
        """
        synth = harness.ROLE_SYSTEM["synthesizer"]
        self.assertIn("source-backed", synth)
        self.assertIn("retrieved source", synth.lower())
        # The blind 'no way to look anything up' clause is wrong for the
        # synthesizer's context and must not be appended to it.
        self.assertNotIn("You have NO way to look anything up", synth)
        # The judge keeps its credit-URL-backed carve-out.
        self.assertIn("UNLESS it carries a real source URL", harness.ROLE_SYSTEM["judge"])

    def test_synthesizer_answer_is_standalone_not_process_narration(self) -> None:
        """The synthesizer's final answer must read as a standalone, decision-first
        result, not a recap of the debate machinery. Blind judges dinged the prior
        wording for leaking process language ('the judge upheld', 'the objection
        survives'). The prompt must (a) keep the 'harvest' move contract intact so
        RoleAgent dispatch still works, and (b) carry NO debate-process phrasing.
        """
        synth = harness.ROLE_SYSTEM["synthesizer"]
        # (a) The JSON move contract is unchanged: still the 'harvest' move with
        # target/title/content, and 'harvest' is still an allowed move.
        self.assertIn('"move":"harvest"', synth)
        self.assertIn('"target"', synth)
        self.assertIn('"title"', synth)
        self.assertIn('"content"', synth)
        self.assertIn("harvest", harness.ROLE_MOVES["synthesizer"])
        # (b) No debate-process language leaks into the produced answer's
        # instructions.
        low = synth.lower()
        for banned in (
            "settled debate",
            "the judge",
            "the critic",
            "the objection",
            "the debate showed",
        ):
            self.assertNotIn(banned, low)

    # -- (2) The judge's ratification REQUIRED the cross-author challenge ---

    def test_self_strawman_objection_does_not_ratify(self) -> None:
        """When the only objection is written under the PROPOSER's own identity,
        the engine's judge (which runs with the distinct-adversary gate ON) is
        refused — a self-written strawman cannot ratify a claim."""
        # label_with_model=False by default, so the critic author is the bare
        # role 'critic'. To simulate a single model playing both sides, point
        # the critic role at a thinker whose author identity collides with the
        # proposer. We do that by handing the engine a per-role thinker dict and
        # using a config that forces the critic to write under 'proposer'.
        #
        # The clean lever the engine exposes is the author identity per role.
        # We cannot rename the critic's author from the script, so instead we
        # drive the engine normally but assert the floor through a STRAWMAN run
        # where the objection author == claim author by routing the objection
        # through a proposer-authored move. The proposer role only emits claims,
        # so the realistic way to produce a same-author objection is to write it
        # via ops with the proposer author and then run only the judge.
        proposer_author = "proposer"

        # PROPOSE via the engine's proposer agent so authorship is the engine's.
        claim = ops.add_node(
            self.store,
            node_type="claim",
            title="Self-served claim",
            content="A claim that will try to ratify off its own strawman.",
            author=proposer_author,
            metadata={"run_id": "run-straw", "agent_role": "proposer"},
            model_authored=True,
        )
        claim_id = claim["node_id"]

        # SAME-AUTHOR objection (the strawman): author == the claim's author.
        ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="objection",
            title="A self-written strawman",
            content="An objection the proposer wrote against itself.",
            author=proposer_author,
            model_authored=True,
            run_id="run-straw",
        )

        # The author-blind gate sees an objection (challenged True)...
        tally = ops.edge_tally(self.store, claim_id)
        self.assertTrue(tally["challenged"])

        # ...but the distinct-adversary gate (what the engine's judge passes)
        # REFUSES because the only objection shares the claim's author.
        with self.assertRaisesRegex(ValueError, "self-written objection cannot ratify"):
            ops.rule(
                self.store,
                claim_id,
                verdict="accept",
                settle=True,
                author="judge",
                require_distinct_adversary=True,
                run_id="run-straw",
            )

        # The claim was NOT ratified; no superseding version exists.
        ratified = [
            n
            for n in self.store.read()["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]
        self.assertEqual(ratified, [])

    def test_judge_agent_records_refusal_on_self_strawman(self) -> None:
        """The judge ROLE AGENT refuses a self-strawman and records it as
        'refused' (the invariant fired) rather than crashing the loop.

        A same-author objection is also a same-FAMILY objection (the proposer
        author maps to the proposer's backend family), so the engine's judge
        refuses it at its stronger CROSS-FAMILY gate before ever reaching
        ops.rule. The seam's author-distinctness refusal is exercised directly
        against ops in test_self_strawman_objection_does_not_ratify; here we
        assert the agent surfaces a refusal outcome (no crash, no ratification)
        when the only objection comes from the proposer's own model family.
        """
        # Build a claim + a same-author objection by hand, then run only the
        # judge agent against it.
        claim = ops.add_node(
            self.store,
            node_type="claim",
            title="Same-author claim",
            content="Will be objected to by its own author.",
            author="proposer",
            model_authored=True,
        )
        claim_id = claim["node_id"]
        # The objection here is authored 'proposer' to match the claim, so it
        # maps to the proposer's backend FAMILY: not a genuinely different
        # adversary (Claude vs GPT).
        ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="objection",
            title="Self objection",
            content="Proposer arguing against itself.",
            author="proposer",
            model_authored=True,
        )

        thinker = StubThinker(
            {"judge": _move("rule", verdict="accept", settle=True)}
        )
        agent = RoleAgent("judge", self._config(), run_id="run-judge")
        prompt, context = harness._render_prompt(
            self.store, "judge", "seed", claim_id
        )
        outcome = agent.act(
            self.store, thinker, target_id=claim_id, prompt=prompt, context=context
        )
        self.assertEqual(outcome.outcome, "refused")
        self.assertIn(
            "no objection came from a backend FAMILY different", outcome.message
        )
        self.assertIn("Claude vs GPT", outcome.message)
        # The strawman was NOT ratified: no superseding ratified claim version.
        ratified = [
            n
            for n in self.store.read()["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]
        self.assertEqual(ratified, [])

    def test_cross_author_objection_lets_the_judge_ratify(self) -> None:
        """The mirror of the strawman test: a DIFFERENT-author objection
        satisfies the distinct-adversary gate and the judge ratifies."""
        claim = ops.add_node(
            self.store,
            node_type="claim",
            title="A real claim",
            content="A claim attacked by a genuinely different author.",
            author="proposer",
            model_authored=True,
        )
        claim_id = claim["node_id"]
        ops.add_branch(
            self.store,
            from_ref=claim_id,
            template="objection",
            title="A real objection",
            content="The critic attacks the claim.",
            author="critic",
            model_authored=True,
        )
        ruling = ops.rule(
            self.store,
            claim_id,
            verdict="accept",
            settle=True,
            author="judge",
            require_distinct_adversary=True,
        )
        self.assertTrue(ruling["settled"])
        self.assertEqual(ruling["ratified_claim"]["status"], "ratified")

    def test_judge_refuted_verdict_records_ruling_but_does_not_ratify(self) -> None:
        """A judge that asks to settle while its verdict REJECTS the claim has
        its settle dropped: the ruling is still recorded, but no ratified
        version is written. Enforces 'never ratify a claim your own verdict
        rejects' on the autonomous path, where the verdict vocabulary is fixed.
        """
        script = {
            "proposer": _move(
                "propose", title="A claim", content="A claim to be judged."
            ),
            # Cross-family objection (critic=codex vs proposer=claude) so the
            # judge is allowed to rule.
            "critic": _move(
                "object", title="A real attack", content="The critic attacks it."
            ),
            "judge": _move(
                "rule",
                verdict="refuted",
                rationale="the objection holds; the claim does not survive",
                settle=True,
            ),
        }
        thinker = StubThinker(script)
        engine = AutonomousEngine(self.store, thinker, self._config())

        result = engine.run("seed", run_id="run-refuted")

        data = self.store.read()
        # The ruling WAS recorded (a decision node evaluates the claim)...
        decisions = [
            n for n in data["nodes"].values() if n["node_type"] == "decision"
        ]
        self.assertEqual(len(decisions), 1)
        # ...but the refuted claim was NOT ratified: no superseding version.
        ratified = [
            n
            for n in data["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]
        self.assertEqual(ratified, [])
        # The dropped-settle is surfaced on the rule move for the run summary.
        rule_moves = [m for m in result["moves"] if m.get("move") == "rule"]
        self.assertEqual(len(rule_moves), 1)
        self.assertEqual(rule_moves[0]["outcome"], "written")
        self.assertIn("not 'upheld'", rule_moves[0]["message"])

    def test_thinker_backend_failure_is_recorded_not_crashed(self) -> None:
        """A model-CLI failure (the live thinkers raise RuntimeError) is caught
        and recorded as a single 'error' move; the loop continues instead of
        crashing the whole run with a raw traceback."""

        class FlakyThinker(StubThinker):
            def think(self, *, role, system, prompt, context=None):  # noqa: ANN001
                if role == "critic":
                    raise RuntimeError("simulated CLI timeout on the critic")
                return super().think(
                    role=role, system=system, prompt=prompt, context=context
                )

        thinker = FlakyThinker(_happy_script())
        engine = AutonomousEngine(self.store, thinker, self._config())

        # The whole point: this call must NOT raise.
        result = engine.run("Does music help coding?", run_id="run-flaky")

        # The critic's backend failure was recorded as an 'error' outcome...
        self.assertTrue(
            any(
                m["role"] == "critic" and m["outcome"] == "error"
                for m in result["moves"]
            ),
            "the critic's RuntimeError should be recorded as an 'error' move",
        )
        # ...and the loop continued: the proposer's claim was still produced
        # before the failure, so the run ended gracefully rather than crashing.
        self.assertIsNotNone(result["claim_id"])

    def test_max_thinker_calls_spends_exactly_that_many_calls(self) -> None:
        """The cost ceiling spends EXACTLY ``max_thinker_calls`` model calls, not
        N+1 (the pre-call check uses ``>=`` against the count already made)."""

        class CountingThinker:
            family = "stub"

            def __init__(self) -> None:
                self.calls = 0
                self.tokens_used = 0

            def think(self, *, role, system, prompt, context=None):  # noqa: ANN001
                self.calls += 1
                if role == "proposer":
                    # A real claim so the loop proceeds into later phases and
                    # actually has the chance to spend more calls.
                    return _move(
                        "propose",
                        title="Budget claim",
                        content="A claim to burn budget rounds on.",
                    )
                # Malformed (not a 'done') so the critic phase keeps consulting
                # the thinker until the budget trips.
                return "no actionable move here"

        thinker = CountingThinker()
        config = self._config(max_thinker_calls=3)
        engine = AutonomousEngine(self.store, thinker, config)

        result = engine.run("seed", run_id="run-budget")

        self.assertEqual(thinker.calls, 3)
        self.assertEqual(result["status"], "cost-ceiling-reached")

    # -- (3) Stop conditions: hard cap, done move, frontier-empty -----------

    def test_hard_cap_aborts_a_runaway_thinker(self) -> None:
        """A critic that NEVER objects and NEVER stops would loop forever; the
        hard ``max_total_moves`` backstop aborts the whole run."""
        # The proposer proposes once; the critic emits a malformed blob every
        # call (never a real move, never a stop). Without a hard cap the engine
        # would burn through every critique/gather round and could spin. The
        # cap is set tiny so we prove it fires.
        def malformed(_index, _context):
            return "I refuse to emit JSON. No move here."

        script = {
            "proposer": _move(
                "propose", title="T", content="A claim to never resolve."
            ),
            "critic": malformed,
            "evidence_gatherer": malformed,
            "judge": malformed,
            "synthesizer": malformed,
        }
        thinker = StubThinker(script)
        config = self._config(max_total_moves=3)
        engine = AutonomousEngine(self.store, thinker, config)

        result = engine.run("runaway seed", run_id="run-cap")

        self.assertEqual(result["status"], "hard-cap-reached")
        # The engine never dispatched more moves than the hard cap.
        self.assertLessEqual(len(result["moves"]), 3)
        # Nothing settled: no decision, no ratified claim.
        data = self.store.read()
        self.assertEqual(
            [n for n in data["nodes"].values() if n["node_type"] == "decision"], []
        )

    def test_mid_pipeline_done_advances_so_the_judge_still_rules(self) -> None:
        """A done/stop from a MID-PIPELINE role (critic, evidence gatherer) means
        'this role is finished' and advances to the next phase — it does NOT
        abort the whole run. Regression for the footgun where the gatherer's
        common 'nothing to add' done killed the debate before the judge ruled.
        """
        # The critic objects once (cross-family: critic=codex vs proposer=claude),
        # then the evidence gatherer says done. The judge must STILL get to rule.
        def critic(index, _context):
            if index == 0:
                return _move(
                    "object",
                    title="Lyrics distract",
                    content="Songs with words steal attention.",
                )
            return "Objection already lodged; nothing further."

        script = {
            "proposer": _move(
                "propose",
                title="Music helps coding",
                content="Instrumental music improves focus while coding.",
            ),
            "critic": critic,
            "evidence_gatherer": _move("done", reason="no further evidence to add"),
            "judge": _move(
                "rule",
                verdict="upheld",
                rationale="the lyrics objection is about songs with words, not "
                "instrumental music",
                settle=True,
            ),
            "synthesizer": _move(
                "harvest",
                title="Settled: instrumental music helps focus",
                content="Instrumental music improves coding focus.",
            ),
        }
        thinker = StubThinker(script)
        engine = AutonomousEngine(self.store, thinker, self._config())

        result = engine.run("Does music help coding?", run_id="run-mid-done")

        # The gatherer's done did NOT abort: the judge ran and the run reached
        # the 'judged' terminal status.
        self.assertEqual(result["status"], "judged")
        data = self.store.read()
        decisions = [
            n for n in data["nodes"].values() if n["node_type"] == "decision"
        ]
        self.assertEqual(
            len(decisions), 1, "the judge ruled despite the gatherer's done"
        )
        # The upheld ruling ratified the claim.
        ratified = [
            n
            for n in data["nodes"].values()
            if n["node_type"] == "claim" and n["status"] == "ratified"
        ]
        self.assertEqual(len(ratified), 1)

    def test_proposer_done_yields_frontier_empty(self) -> None:
        """If the proposer never produces a claim (emits done), later phases
        have nothing to act on and the engine stops."""
        script = {"proposer": _move("done", reason="no claim to make")}
        thinker = StubThinker(script)
        engine = AutonomousEngine(self.store, thinker, self._config())

        result = engine.run("empty seed", run_id="run-empty")

        # The proposer's done is itself a stop, so the loop ends immediately.
        self.assertEqual(result["status"], "model-stopped")
        self.assertIsNone(result["claim_id"])
        data = self.store.read()
        self.assertEqual(data["nodes"], {})

    def test_per_phase_round_cap_is_never_exceeded(self) -> None:
        """The critic phase honors the playbook's max_iterations: a critic that
        keeps objecting is consulted at most that many times for its phase."""
        # The critic objects every call (distinct titles so each is a new node).
        def critic(index, _context):
            return _move(
                "object",
                title=f"Objection {index}",
                content=f"Attack number {index}.",
            )

        # The judge stops so the loop does not settle; we only care about how
        # many times the critic role was consulted.
        script = {
            "proposer": _move("propose", title="T", content="A claim."),
            "critic": critic,
            "evidence_gatherer": _move("done", reason="skip"),
            "judge": _move("done", reason="skip"),
        }
        thinker = StubThinker(script)
        engine = AutonomousEngine(self.store, thinker, self._config())
        engine.run("rounds seed", run_id="run-rounds")

        # The built-in playbook caps critique at 3 iterations.
        playbook = ops.load_playbook(self.store)
        critique_cap = next(
            p["max_iterations"] for p in playbook["phases"] if p["role"] == "critic"
        )
        self.assertEqual(critique_cap, 3)
        self.assertLessEqual(thinker.calls_by_role.get("critic", 0), critique_cap)

    # -- (4) Malformed / unknown moves are rejected, never dispatched raw ---

    def test_malformed_and_unknown_moves_are_recorded_not_crashed(self) -> None:
        """A malformed blob is 'malformed'; an out-of-role move is 'unknown'.
        Neither crashes the loop nor writes a node."""
        # Proposer emits valid claim. Critic emits a move that is not in its
        # allowed set ('rule' belongs to the judge), then done.
        def critic(index, _context):
            if index == 0:
                return _move("rule", target="x", verdict="accept")
            return _move("done", reason="give up")

        script = {
            "proposer": _move("propose", title="T", content="C."),
            "critic": critic,
        }
        thinker = StubThinker(script)
        engine = AutonomousEngine(self.store, thinker, self._config())
        result = engine.run("seed", run_id="run-bad")

        outcomes = [m["outcome"] for m in result["moves"]]
        self.assertIn("unknown", outcomes)
        # No objection node was written from the rejected critic move.
        data = self.store.read()
        self.assertEqual(
            [n for n in data["nodes"].values() if n["node_type"] == "objection"], []
        )

    # -- (5) HarnessConfig invariant: critic FAMILY != proposer FAMILY ------
    #
    # The locked rebuild moved the adversarial-diversity contract from model
    # tiers to backend FAMILIES: the adversary must run on a genuinely different
    # model family (Claude via the claude CLI vs GPT via the codex CLI), not the
    # proposer rephrasing itself on the same family. The invariant the config
    # enforces is therefore "critic backend family != proposer backend family",
    # which these tests assert (the old "critic model must differ" check is gone
    # because two roles on the same family — e.g. judge and proposer both claude
    # — legitimately share a model).

    def test_config_rejects_same_family_critic_and_proposer(self) -> None:
        """The locked contract: the critic must run on a DIFFERENT backend family
        than the proposer, so the adversary is not the proposer rephrasing itself
        on the same family (Claude vs GPT)."""
        with self.assertRaisesRegex(ValueError, "critic backend family must differ"):
            HarnessConfig(
                proposer_backend="claude",
                critic_backend="claude",
                judge_backend="claude",
            )

    def test_config_rejects_same_family_from_env(self) -> None:
        """The same invariant fires when both env overrides force the proposer
        and critic onto the same family."""
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
        """With no env overrides the locked defaults already satisfy the gate:
        proposer=claude family, critic=codex family (a genuinely different
        family), and the judge MAY share the proposer's family (judge=claude)."""
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
            self.assertEqual(config.author_for_role("critic"), "critic")
            self.assertEqual(config.author_for_role("proposer"), "proposer")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    # -- (6) The engine never imports anthropic ----------------------------

    def test_harness_module_does_not_import_anthropic(self) -> None:
        """The engine is pure w.r.t. the model: importing harness must not pull
        in the anthropic SDK (the live wrapper is imported lazily in
        run_cli_loop only)."""
        import sys

        self.assertNotIn("anthropic", sys.modules)
        # The module exposes the engine surface without the SDK present.
        self.assertTrue(hasattr(harness, "AutonomousEngine"))
        self.assertTrue(hasattr(harness, "Thinker"))


if __name__ == "__main__":
    unittest.main()
