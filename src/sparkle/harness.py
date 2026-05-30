"""Sparkle autonomous adversarial harness — the Phase 2 engine + role agents.

This module is the autonomous loop the spec calls for: a proposer states a
claim, a critic from a *different model FAMILY* (Claude vs GPT) attacks it,
evidence gets gathered, a judge rules with the cross-family gate ON, and a
synthesizer harvests. Every graph mutation is driven through :mod:`sparkle.ops`,
so the engine inherits all of the debate invariants and the Phase-2 integrity
floor (the self-loop ban + the distinct-adversary check) by construction, and
layers a stronger CROSS-FAMILY gate on top: the adversary must be a genuinely
different backend family, not just a different author string.

Hard boundaries this module respects:

- It imports :mod:`sparkle.ops` and stdlib only. It NEVER imports ``anthropic``
  or ``openai`` and NEVER imports :mod:`sparkle.thinker` at module top. The real
  model backends are injected as duck-typed :class:`Thinker` objects; the live
  CLI-backed wrappers (:class:`sparkle.thinker.ClaudeCliThinker` /
  :class:`sparkle.thinker.CodexCliThinker`) are imported lazily only inside
  :func:`run_cli_loop`.
- The engine is *pure with respect to the model*: it takes injected per-role
  thinkers, so the whole loop runs against a deterministic stub with no network
  and no CLI invocation. The live cross-family debate (Claude via ``claude -p``,
  GPT via ``codex exec``) is a manual human acceptance step, not something this
  engine validates.

The contract a role agent enforces: a thinker returns free text; the agent
extracts a single JSON move object, validates it against that role's allowed
moves and required args, and only then dispatches exactly one ``ops.*`` call
with the role's author identity and the current ``run_id``. An unknown or
malformed move is rejected and recorded, never dispatched raw. A ``ValueError``
from ops (e.g. the judge refusing a self-strawman ratification) is caught and
recorded as a refusal — the invariant fired, which is correct.

Pure stdlib. Zero third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from . import ops
from .graph import GraphStore


# ---------------------------------------------------------------------------
# The thinker contract (the engine owns the interface it depends on)
# ---------------------------------------------------------------------------


@runtime_checkable
class Thinker(Protocol):
    """The minimal duck-typed contract the engine needs from a model backend.

    One method. The engine calls ``think(role=..., system=..., prompt=...,
    context=...)`` and gets back a single text blob; the role agent parses it.
    The engine never assumes JSON-mode, tool-calling, or streaming.

    ``role`` is one of the playbook role names
    (``proposer``/``critic``/``evidence_gatherer``/``judge``/``synthesizer``) so
    a real thinker can map role -> backend (the critic on a *different* model
    FAMILY than the proposer). The stub thinker keys its scripted responses on
    ``role``.

    Because this is a structural ``Protocol``, the real CLI-backed thinkers
    (:class:`sparkle.thinker.ClaudeCliThinker` /
    :class:`sparkle.thinker.CodexCliThinker`) and the test stub are
    interchangeable with zero shared base class and zero import of any model SDK
    by this module.
    """

    def think(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str: ...


# Optional, duck-typed hook a thinker MAY expose so the engine can enforce a
# token/cost ceiling. A thinker that does not implement it simply contributes
# zero tokens (the stub reports zero), so the ceiling never trips by accident.
def _thinker_tokens(thinker: Any) -> int:
    """Best-effort token count for the last/total call of a thinker.

    Reads ``thinker.tokens_used`` if present (an int), else 0. Kept duck-typed
    so the stub and the real CLI wrappers both work without a shared base class.
    The CLI thinkers expose ``tokens_used`` (the name the engine reads), so the
    token ceiling is actually wired — not a latent dead budget.
    """
    value = getattr(thinker, "tokens_used", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Config — pure stdlib (dataclasses + os), reads env, holds NO secret
# ---------------------------------------------------------------------------


# Backend family ids. Real adversarial diversity comes from DIFFERENT FAMILIES
# (Claude vs GPT), not different Claude model tiers — so every Claude-side role
# runs Opus 4.8 and the genuine adversary is the codex (GPT) critic.
CLAUDE_FAMILY = "claude"
CODEX_FAMILY = "codex"

# Locked default per-role backend family. The adversary (critic) MUST be a
# different family than the proposer; the judge MAY share the proposer family
# (Albert chose judge=claude). evidence_gatherer's 'oppose' writes the same
# contradicts edge the cross-family gate counts, so it is held to the adversary
# bar too (cross-family vs the proposer).
_DEFAULT_BACKENDS: dict[str, str] = {
    "proposer": CLAUDE_FAMILY,
    "critic": CODEX_FAMILY,
    "judge": CLAUDE_FAMILY,
    "evidence_gatherer": CLAUDE_FAMILY,
    "synthesizer": CLAUDE_FAMILY,
}

# Locked default per-role model id. Opus 4.8 on EVERY claude-side role; the
# codex critic runs GPT-5.5.
_DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
_DEFAULT_CODEX_MODEL = "gpt-5.5"


def _role_backend_default(role: str) -> str:
    """Per-role backend family from env (SPARKLE_<ROLE>_BACKEND) or the locked map."""
    env = os.environ.get(f"SPARKLE_{role.upper()}_BACKEND")
    return env if env else _DEFAULT_BACKENDS[role]


def _role_model_default(role: str) -> str:
    """Per-role model id from env (SPARKLE_<ROLE>_MODEL) or the family default.

    The default model follows the role's *resolved* backend family: a codex
    role defaults to GPT-5.5, a claude role to Opus 4.8.
    """
    env = os.environ.get(f"SPARKLE_{role.upper()}_MODEL")
    if env:
        return env
    backend = _role_backend_default(role)
    return _DEFAULT_CODEX_MODEL if backend == CODEX_FAMILY else _DEFAULT_CLAUDE_MODEL


@dataclass
class HarnessConfig:
    """Engine configuration with the locked defaults and the locked invariant.

    Each role is wired to a backend FAMILY (``claude`` or ``codex``) and a model
    id, both from env with sane defaults. The one hard, testable contract: the
    critic's family must differ from the proposer's family, so the adversary is
    a genuinely different model family (Claude vs GPT), not the proposer
    rephrasing itself on the same family. This dataclass holds no API key and
    imports no model SDK — it loads in the zero-dependency base test suite.

    Per-role model ids are still carried (the factory reads ``model_for_role``),
    but they are NOT what the gate counts — the gate counts the backend FAMILY.
    """

    # --- Per-role backend family (the gate counts THIS) -------------------
    proposer_backend: str = field(
        default_factory=lambda: _role_backend_default("proposer")
    )
    critic_backend: str = field(
        default_factory=lambda: _role_backend_default("critic")
    )
    judge_backend: str = field(
        default_factory=lambda: _role_backend_default("judge")
    )
    evidence_backend: str = field(
        default_factory=lambda: _role_backend_default("evidence_gatherer")
    )
    synthesizer_backend: str = field(
        default_factory=lambda: _role_backend_default("synthesizer")
    )

    # --- Per-role model id (the factory passes THIS to the CLI thinker) ---
    proposer_model: str = field(
        default_factory=lambda: _role_model_default("proposer")
    )
    critic_model: str = field(
        default_factory=lambda: _role_model_default("critic")
    )
    judge_model: str = field(
        default_factory=lambda: _role_model_default("judge")
    )
    evidence_model: str = field(
        default_factory=lambda: _role_model_default("evidence_gatherer")
    )
    synthesizer_model: str = field(
        default_factory=lambda: _role_model_default("synthesizer")
    )

    # Optional reasoning-effort override for the codex (GPT) backend. None =
    # leave the user's codex config alone. Wired into CodexCliThinker by the
    # factory.
    reasoning_effort: str | None = field(
        default_factory=lambda: os.environ.get("SPARKLE_CODEX_REASONING_EFFORT")
        or None
    )

    # Hard runaway backstop, always on. Counts every attempted move across all
    # phases; the engine aborts the whole run if it hits this, even mid-phase.
    max_total_moves: int = field(
        default_factory=lambda: int(os.environ.get("SPARKLE_MAX_TOTAL_MOVES", "24"))
    )
    # Optional cost ceiling. None = off. Counts every thinker.think() call.
    max_thinker_calls: int | None = field(
        default_factory=lambda: (
            int(os.environ["SPARKLE_MAX_THINKER_CALLS"])
            if os.environ.get("SPARKLE_MAX_THINKER_CALLS")
            else None
        )
    )
    # Optional token budget. None = off. Needs a thinker exposing token usage.
    token_budget: int | None = field(
        default_factory=lambda: (
            int(os.environ["SPARKLE_TOKEN_BUDGET"])
            if os.environ.get("SPARKLE_TOKEN_BUDGET")
            else None
        )
    )
    # Optional per-phase round override (critique/gather). None = use playbook.
    rounds_override: int | None = None
    # When set, author identities are suffixed with the model id, e.g.
    # 'critic:gpt-5.5'. Default off — the bare role name is enough for the
    # cross-family gate since the family is recovered from the role, not the
    # model id baked into the author string.
    label_with_model: bool = False

    def __post_init__(self) -> None:
        # Locked invariant: the adversary (critic) MUST be a different backend
        # FAMILY than the proposer (Claude vs GPT). Different model TIERS on the
        # same family is NOT adversarial diversity, so the gate counts the
        # family. The locked default (proposer=claude, critic=codex) satisfies
        # this.
        #
        # NOTE on the evidence gatherer: the locked default backend map puts the
        # gatherer on the SAME family as the proposer (both claude), so we do
        # NOT add an evidence!=proposer config invariant — it would make the
        # locked-default HarnessConfig() un-constructable. The cross-family GATE
        # at ratification time (see _cross_family_objection) is what protects the
        # decision: a same-family evidence 'oppose' writes a contradicts edge but
        # does NOT count toward the gate, so it cannot ratify a same-family
        # proposer's claim. The gate, not a config invariant, carries this floor.
        if self.critic_backend == self.proposer_backend:
            raise ValueError(
                "critic backend family must differ from proposer backend family "
                "so the adversary is a genuinely different model family (Claude "
                "vs GPT), not the proposer rephrasing itself on the same family; "
                "set SPARKLE_CRITIC_BACKEND to a different family"
            )

    def model_for_role(self, role: str) -> str:
        """The model id a real thinker should use for ``role``."""
        return {
            "proposer": self.proposer_model,
            "critic": self.critic_model,
            "evidence_gatherer": self.evidence_model,
            "judge": self.judge_model,
            "synthesizer": self.synthesizer_model,
        }.get(role, self.proposer_model)

    def backend_for_role(self, role: str) -> str:
        """The backend FAMILY id (``claude`` / ``codex``) a role runs on.

        This is what the factory dispatches on and what the cross-family gate
        counts. An unknown role defaults to the claude family.
        """
        return {
            "proposer": self.proposer_backend,
            "critic": self.critic_backend,
            "evidence_gatherer": self.evidence_backend,
            "judge": self.judge_backend,
            "synthesizer": self.synthesizer_backend,
        }.get(role, CLAUDE_FAMILY)

    def family_for_author(self, author: str) -> str:
        """Recover the backend FAMILY behind an author identity written by a role.

        Authors are bare role names by default (``proposer``/``critic``/...), or
        ``role:model`` when :attr:`label_with_model` is set (the role prefix is
        what carries the family). An author that maps to no known role (e.g. a
        human ``local`` write) is treated as its own distinct family — a human
        objection is genuinely a different adversary — so it returns the author
        string itself. This lets the engine enforce the locked *family*-
        distinctness of the adversary, not just author-string distinctness, when
        the judge rules.
        """
        role = author.rsplit(":", 1)[0] if (self.label_with_model and ":" in author) else author
        known_roles = {
            "proposer",
            "critic",
            "evidence_gatherer",
            "judge",
            "synthesizer",
        }
        if role in known_roles:
            return self.backend_for_role(role)
        return author

    def author_for_role(self, role: str) -> str:
        """The distinct author identity a role writes with.

        Bare role name by default (sufficient because the cross-family gate
        recovers the family from the role name via :meth:`family_for_author`).
        Optionally suffixed with the model id.
        """
        if self.label_with_model:
            return f"{role}:{self.model_for_role(role)}"
        return role


# ---------------------------------------------------------------------------
# Per-role system prompts + the JSON move contract handed to the thinker
# ---------------------------------------------------------------------------


def _vocabulary_block() -> str:
    """Node-type + relation legend, rendered into every role system prompt."""
    types = ", ".join(ops.VALID_NODE_TYPES)
    rels = "\n".join(
        f"  - {item['relation']}: {item['gloss']}" for item in ops.relations()
    )
    return (
        f"Node types in the graph: {types}.\n"
        f"Edge relations (arrow reads from --relation--> to):\n{rels}\n"
        "A branch child points at its parent (from = the new node, "
        "to = the node it reacts to)."
    )


# Each role's allowed move names and the system instruction that tells the
# thinker the exact JSON shape to emit. The role agent extracts the first JSON
# object from the returned text, validates it, and dispatches one ops.* call.
ROLE_SYSTEM: dict[str, str] = {
    "proposer": (
        "You are the PROPOSER in an adversarial claim-graph debate. State ONE "
        "sharp, falsifiable claim about the seed question. Return a single JSON "
        "object and nothing else:\n"
        '  {"move":"propose","title":"<short claim title>",'
        '"content":"<the claim, stated to be attacked>",'
        '"citations":[],"tags":[]}\n'
        "Or stop the loop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "critic": (
        "You are the CRITIC, running on a DIFFERENT model family than the "
        "proposer (Claude vs GPT). "
        "Attack the target claim with the strongest genuine objection — not a "
        "strawman. Return a single JSON object and nothing else:\n"
        '  {"move":"object","target":"<claim handle>",'
        '"title":"<short objection title>",'
        '"content":"<what weakens or falsifies the claim>","citations":[]}\n'
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "evidence_gatherer": (
        "You are the EVIDENCE GATHERER. The critic already supplies the "
        "opposition, so your job is to find the STRONGEST genuine evidence that "
        "SUPPORTS or verifies the target claim. Record opposing evidence only "
        "when the honest evidence truly cuts against the claim. Return a single "
        "JSON object and nothing else:\n"
        '  {"move":"support","target":"<claim handle>","title":"<short>",'
        '"content":"<supporting evidence>","citations":[]}\n'
        "  or, only if the evidence genuinely undercuts the claim,\n"
        '  {"move":"oppose","target":"<claim handle>","title":"<short>",'
        '"content":"<opposing evidence>","citations":[]}\n'
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "judge": (
        "You are the JUDGE. Rule on the target claim only after a genuine "
        "objection from a DIFFERENT author exists. Weigh the support against "
        "the objections, then decide. Return a single JSON object and nothing "
        "else:\n"
        '  {"move":"rule","target":"<claim handle>",'
        '"verdict":"<upheld | refuted | overstated>","rationale":"<why>",'
        '"settle":<true|false>}\n'
        "settle MUST be true ONLY if you UPHOLD the claim AS STATED. If the "
        "claim is refuted, overstated, only partly true, or unproven, settle "
        "MUST be false -- you still record the ruling, you just do not ratify "
        "it. NEVER ratify (settle=true) a claim your own verdict rejects.\n"
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "synthesizer": (
        "You are the SYNTHESIZER. Harvest the settled debate into one synthesis "
        "node. Return a single JSON object and nothing else:\n"
        '  {"move":"harvest","target":"<claim handle>",'
        '"title":"<short synthesis title>","content":"<the takeaway>"}\n'
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
}

# The moves each role is allowed to emit. Anything else is rejected as unknown.
ROLE_MOVES: dict[str, set[str]] = {
    "proposer": {"propose", "stop", "done"},
    "critic": {"object", "stop", "done"},
    "evidence_gatherer": {"support", "oppose", "stop", "done"},
    "judge": {"rule", "stop", "done"},
    "synthesizer": {"harvest", "stop", "done"},
}


# ---------------------------------------------------------------------------
# Move parsing + validation (the agent never dispatches raw thinker text)
# ---------------------------------------------------------------------------


def _extract_move(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a thinker's free text.

    Tolerates a fenced ```json block, leading prose, or trailing prose. Returns
    the parsed dict, or ``None`` when no parseable JSON object is present (the
    caller treats that as a 'malformed' outcome and skips, never crashing).
    """
    if not isinstance(text, str):
        return None
    # Prefer a fenced block if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    # Then the first balanced-looking {...} span.
    brace = _first_json_object(text)
    if brace is not None:
        candidates.append(brace)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _first_json_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` substring, or None.

    A small, dependency-free scanner: tracks brace depth and respects string
    literals so a ``}`` inside a JSON string does not close the object early.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _nonempty(value: Any) -> bool:
    """A required arg is present when it is a non-empty string."""
    return isinstance(value, str) and value.strip() != ""


# The judge's system prompt fixes a closed verdict vocabulary
# (upheld | refuted | overstated) and the settle contract: "settle MUST be true
# ONLY if you UPHOLD the claim AS STATED ... NEVER ratify a claim your own
# verdict rejects." The seam (ops.rule) treats verdict as free text on purpose
# (the human CLI rules with words like "accept"/"needs work"), so the coupling
# cannot live there. The autonomous judge is the one path with a fixed
# vocabulary, so the engine enforces it here: a ruling ratifies ONLY on an
# affirming verdict. Any other verdict still records the ruling, it just does
# not stamp the terminal `ratified` status — so a non-compliant judge turn
# cannot permanently mark a claim it rejected as settled-in-its-favor.
JUDGE_AFFIRMING_VERDICT = "upheld"


def _verdict_permits_settle(verdict: Any) -> bool:
    """True only when the judge's verdict is the affirming token ('upheld').

    Conservative by design: a ratification is permanent and the highest-trust
    state in the graph, so anything that is not an unambiguous 'upheld'
    (refuted, overstated, partly true, or any off-vocabulary string) does not
    settle.
    """
    return isinstance(verdict, str) and verdict.strip().lower() == JUDGE_AFFIRMING_VERDICT


# ---------------------------------------------------------------------------
# Role agent — turns thinker text into ONE validated move + one ops.* call
# ---------------------------------------------------------------------------

# Outcome vocabulary recorded per move for the engine's summary.
#   written  — an ops.* mutation succeeded
#   dedup    — ops.add_node returned an existing node (created=False)
#   malformed— no parseable JSON move in the thinker text
#   unknown  — 'move' not in this role's allowed set
#   rejected — a required arg was missing/empty
#   refused  — ops raised ValueError (an invariant fired, correctly)
#   stop     — the thinker asked to end the loop
#   error    — the thinker backend itself failed (timeout, non-zero CLI exit,
#              malformed/empty answer); recorded so one flaky call does not crash
#              the run


@dataclass
class MoveResult:
    """One role agent step's outcome, recorded for the engine summary."""

    role: str
    move: str | None
    outcome: str
    node_id: str | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "move": self.move,
            "outcome": self.outcome,
        }
        if self.node_id is not None:
            out["node_id"] = self.node_id
        if self.message:
            out["message"] = self.message
        return out


def _cross_family_objection(
    store: GraphStore, claim_id: str, claim_author: str, config: HarnessConfig
) -> bool:
    """True when an inbound ``contradicts`` objection comes from a DIFFERENT FAMILY.

    The seam's ``require_distinct_adversary`` gate (in :func:`ops.rule`) enforces a
    different *author string*. That is necessary but not sufficient for the
    locked Phase-2 decision: the adversary must run on a genuinely DIFFERENT
    backend FAMILY than the proposer (Claude vs GPT), so the attack is not the
    proposer's own reasoning rephrased on the same family. This engine-side check
    closes the gap where a role with a distinct author (e.g. the evidence
    gatherer's ``oppose``) happens to run on the same family as the proposer. The
    engine's judge requires at least one objection whose author maps to a family
    different from the claim author's family.

    CHANGE A — a REHOMED contradicts edge does NOT count. When a claim is revised,
    its inbound objections are copied onto the new version and tagged
    ``rehomed=True`` for lineage/display. A rewritten claim must earn a FRESH
    cross-author + cross-family objection before it can be ratified, so a stale
    copied-over objection is skipped here (mirroring the seam's author-distinct
    floor, which skips the same edges).
    """
    claim_family = config.family_for_author(claim_author)
    data = store.read()
    for edge in data["edges"].values():
        if edge["relation"] != "contradicts" or edge["to_id"] != claim_id:
            continue
        # CHANGE A: a rehomed objection is not a fresh adversary — skip it.
        if edge.get("metadata", {}).get("rehomed"):
            continue
        src = data["nodes"].get(edge["from_id"])
        if src is None:
            continue
        if config.family_for_author(src.get("author", "local")) != claim_family:
            return True
    return False


class RoleAgent:
    """A single playbook role: render prompt -> think -> validate -> dispatch.

    The agent fixes its author identity per role (so the distinct-adversary gate
    in :func:`ops.rule` sees a real opponent) and stamps the current ``run_id``
    into every ops call (so the whole loop is visible to the run surface).
    """

    def __init__(self, role: str, config: HarnessConfig, run_id: str) -> None:
        if role not in ROLE_MOVES:
            raise ValueError(f"unknown playbook role: {role!r}")
        self.role = role
        self.config = config
        self.run_id = run_id
        self.author = config.author_for_role(role)

    def act(
        self,
        store: GraphStore,
        thinker: Thinker,
        *,
        target_id: str | None,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> MoveResult:
        """Run one role step. Returns a :class:`MoveResult`; never raises on a
        bad model output or an ops invariant — those are recorded as outcomes.
        """
        system = ROLE_SYSTEM.get(self.role, "")
        try:
            text = thinker.think(
                role=self.role, system=system, prompt=prompt, context=context
            )
        except Exception as exc:  # noqa: BLE001 - thinker is a Protocol; stay backend-agnostic
            # The live CLI thinkers raise RuntimeError on every real failure
            # (timeout, non-zero exit, malformed JSON, empty answer). Catching a
            # broad Exception keeps the engine decoupled from the concrete
            # backend's exception type: one flaky model call is recorded as a
            # single failed move so the loop continues to the next phase instead
            # of crashing the whole run with a raw traceback.
            return MoveResult(self.role, None, "error", message=str(exc))
        move = _extract_move(text)
        if move is None:
            return MoveResult(self.role, None, "malformed", message="no JSON move")

        name = move.get("move")
        if not isinstance(name, str) or name not in ROLE_MOVES[self.role]:
            return MoveResult(
                self.role, name if isinstance(name, str) else None, "unknown",
                message=f"move not allowed for role {self.role}",
            )

        if name in ("stop", "done"):
            reason = move.get("reason", "")
            return MoveResult(
                self.role, name, "stop",
                message=str(reason) if reason else "",
            )

        # A move that references a target fills it from the engine's current
        # claim pointer when the thinker omitted or stubbed it. The stub only
        # has to name the move type and text; the live claim id is injected here.
        if target_id is not None and not _nonempty(move.get("target")):
            move["target"] = target_id

        return self._dispatch(store, name, move)

    def _dispatch(
        self, store: GraphStore, name: str, move: dict[str, Any]
    ) -> MoveResult:
        """Validate required args, then call exactly one mapped ops.* function.

        A missing/empty required arg is 'rejected'. A ``ValueError`` from ops is
        caught and recorded as 'refused' — the invariant fired, which is the
        correct behavior, not a crash.
        """
        try:
            if name == "propose":
                if not (_nonempty(move.get("title")) and _nonempty(move.get("content"))):
                    return MoveResult(self.role, name, "rejected", message="title+content required")
                result = ops.add_node(
                    store,
                    node_type="claim",
                    title=move["title"],
                    content=move["content"],
                    citations=move.get("citations") or None,
                    author=self.author,
                    tags=move.get("tags") or None,
                    metadata={
                        "run_id": self.run_id,
                        "agent_role": "proposer",
                        "provisional": True,
                    },
                    model_authored=True,
                )
                outcome = "written" if result.get("created") else "dedup"
                return MoveResult(self.role, name, outcome, node_id=result["node_id"])

            if name in ("object", "support", "oppose"):
                if not (
                    _nonempty(move.get("target"))
                    and _nonempty(move.get("title"))
                    and _nonempty(move.get("content"))
                ):
                    return MoveResult(self.role, name, "rejected", message="target+title+content required")
                template = "objection" if name in ("object", "oppose") else "support"
                result = ops.add_branch(
                    store,
                    from_ref=move["target"],
                    template=template,
                    title=move["title"],
                    content=move["content"],
                    citations=move.get("citations") or None,
                    author=self.author,
                    model_authored=True,
                    run_id=self.run_id,
                )
                return MoveResult(self.role, name, "written", node_id=result["node_id"])

            if name == "rule":
                if not (
                    _nonempty(move.get("target"))
                    and _nonempty(move.get("verdict"))
                ):
                    return MoveResult(self.role, name, "rejected", message="target+verdict required")
                # Engine-side cross-FAMILY gate (the locked Phase-2 decision):
                # the seam's require_distinct_adversary only checks the author
                # STRING differs. Before we let the judge rule, also require that
                # at least one objection came from a genuinely DIFFERENT backend
                # FAMILY than the claim's author (Claude vs GPT) — otherwise a
                # role with a distinct author but the proposer's family (e.g. the
                # evidence gatherer's 'oppose') could ratify a self-attacked
                # claim. A rehomed objection (copied onto a revised claim) does
                # not count (CHANGE A), so a rewritten claim must earn a fresh
                # cross-family objection. Refuse here so the loop does not settle,
                # exactly as an ops refusal would.
                target_node = ops.get_node(store, move["target"])
                claim_author = target_node.get("author", "local")
                if not _cross_family_objection(
                    store, target_node["node_id"], claim_author, self.config
                ):
                    return MoveResult(
                        self.role,
                        name,
                        "refused",
                        message=(
                            "no objection came from a backend FAMILY different "
                            "than the claim's; the adversary must be a different "
                            "model family (Claude vs GPT), not just a different "
                            "author"
                        ),
                    )
                # Couple settle to the verdict (the judge prompt's contract):
                # only an affirming 'upheld' verdict may ratify. A judge that
                # asks to settle while its verdict rejects the claim has its
                # settle dropped — the ruling is still recorded, but the claim is
                # not stamped 'ratified'. This is the engine's enforcement of
                # "NEVER ratify a claim your own verdict rejects".
                requested_settle = bool(move.get("settle", False))
                settle = requested_settle and _verdict_permits_settle(move["verdict"])
                result = ops.rule(
                    store,
                    move["target"],
                    verdict=move["verdict"],
                    rationale=move.get("rationale", ""),
                    settle=settle,
                    author=self.author,
                    confidence=0.8,
                    run_id=self.run_id,
                    require_distinct_adversary=True,
                )
                node_id = result["decision"]["node_id"]
                if requested_settle and not settle:
                    return MoveResult(
                        self.role, name, "written", node_id=node_id,
                        message=(
                            f"verdict {move['verdict']!r} is not "
                            f"'{JUDGE_AFFIRMING_VERDICT}'; ruling recorded but the "
                            "claim was NOT ratified (a judge may not settle a "
                            "claim its own verdict rejects)"
                        ),
                    )
                return MoveResult(self.role, name, "written", node_id=node_id)

            if name == "harvest":
                if not (
                    _nonempty(move.get("target"))
                    and _nonempty(move.get("title"))
                    and _nonempty(move.get("content"))
                ):
                    return MoveResult(self.role, name, "rejected", message="target+title+content required")
                # CHANGE C: pass run_id= so the new add_node channel forwards the
                # run stamp to BOTH the synthesis node metadata AND the fused
                # 'produced' edge. Without this the synthesis->claim edge was
                # invisible to run_summary/run_diff/rollback. metadata also
                # carries run_id for the node (an explicit metadata['run_id']
                # wins over the kwarg in the seam, so the two agree here).
                result = ops.add_node(
                    store,
                    node_type="synthesis",
                    title=move["title"],
                    content=move["content"],
                    author=self.author,
                    link_to=move["target"],
                    relation="produced",
                    metadata={
                        "run_id": self.run_id,
                        "agent_role": "synthesizer",
                        "provisional": True,
                    },
                    model_authored=True,
                    run_id=self.run_id,
                )
                outcome = "written" if result.get("created") else "dedup"
                return MoveResult(self.role, name, outcome, node_id=result["node_id"])

        except ValueError as exc:
            # An ops invariant fired (unchallenged claim, self-strawman, etc.).
            # That is correct behavior: record it, do not crash the loop.
            return MoveResult(self.role, name, "refused", message=str(exc))

        # Defensive: a known move with no handler (cannot happen given the maps).
        return MoveResult(self.role, name, "unknown", message="no dispatch path")


# ---------------------------------------------------------------------------
# Pure run-region summary (mirrors mcp_server._run_summary, no mcp import)
# ---------------------------------------------------------------------------


def run_region_summary(store: GraphStore, run_id: str) -> dict[str, Any]:
    """Counts of what a run wrote, filtered by the ``run_id`` metadata stamp.

    Pure composition of ``ops.list_nodes`` / ``ops.list_edges`` reads over the
    run stamp — the same logic the MCP server exposes, reimplemented here so the
    engine never imports the MCP layer.
    """
    nodes = [
        n
        for n in ops.list_nodes(store)
        if n.get("metadata", {}).get("run_id") == run_id
    ]
    edges = [
        e
        for e in ops.list_edges(store)
        if e.get("metadata", {}).get("run_id") == run_id
    ]
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


# ---------------------------------------------------------------------------
# Prompt rendering — what each role sees about the live claim + its neighbors
# ---------------------------------------------------------------------------


def _render_prompt(
    store: GraphStore, role: str, seed: str, claim_id: str | None
) -> tuple[str, dict[str, Any]]:
    """Build the role's prompt text + a context snapshot of the live subgraph.

    The proposer sees only the seed question. Every later role sees the real
    claim, its neighbors, and the live referee signal (the genuine tally), so
    the critic attacks the actual claim and the judge sees the actual support /
    objection counts — not a hallucinated state.
    """
    vocab = _vocabulary_block()
    if role == "proposer" or claim_id is None:
        prompt = (
            f"Seed question / topic to debate:\n{seed}\n\n{vocab}\n\n"
            "Emit your move now."
        )
        return prompt, {"seed": seed}

    node = ops.get_node(store, claim_id)
    signal = ops.referee_signal(store, claim_id)
    neighbor_lines = []
    for item in node.get("inbound", []):
        neighbor_lines.append(
            f"  [{item['handle']}] {item['node_type']} --{item['relation']}--> "
            f"this :: {item['title']} (author={item.get('author', 'local')})"
        )
    neighbors_text = "\n".join(neighbor_lines) or "  (no objections or evidence yet)"
    prompt = (
        f"Seed question / topic:\n{seed}\n\n"
        f"Target claim [{node['handle']}]: {node['title']}\n"
        f"{node['content']}\n\n"
        f"Live signal: {signal['live_signal']} "
        f"(supports={signal['supports']}, contradicts={signal['contradicts']}, "
        f"net={signal['net']}, challenged={signal['challenged']}).\n"
        f"Existing reactions to this claim:\n{neighbors_text}\n\n"
        f"{vocab}\n\nEmit your move now."
    )
    context = {
        "seed": seed,
        "claim_id": claim_id,
        "claim_handle": node["handle"],
        "signal": signal,
        "subgraph": ops.subgraph(store, claim_id),
    }
    return prompt, context


# ---------------------------------------------------------------------------
# The engine — pure w.r.t. the model, drives the playbook via ops.*
# ---------------------------------------------------------------------------


class AutonomousEngine:
    """Drives the adversarial playbook loop entirely through ``ops.*``.

    Construct with a :class:`GraphStore`, a :class:`HarnessConfig`, and either a
    single :class:`Thinker` (used for every role) or a ``dict[str, Thinker]``
    mapping role -> thinker. The engine imports ``ops`` and the ``Thinker``
    protocol only; it never imports ``anthropic`` or ``openai``.

    Stop conditions, all enforced:
      - per-phase ``max_iterations`` from the playbook (never exceeded),
      - a HARD ``max_total_moves`` backstop (aborts mid-run on a runaway thinker),
      - an explicit ``{"move":"stop"|"done"}`` from any role,
      - frontier-empty (the current claim dropped off and nothing is actionable),
      - judge ruled+settled (the natural terminal -> synthesize, then stop),
      - an optional ``max_thinker_calls`` / ``token_budget`` cost ceiling.
    """

    def __init__(
        self,
        store: GraphStore,
        thinkers: Thinker | dict[str, Thinker],
        config: HarnessConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config or HarnessConfig()
        self._thinkers = thinkers

    def _thinker_for(self, role: str) -> Thinker:
        if isinstance(self._thinkers, dict):
            try:
                return self._thinkers[role]
            except KeyError as exc:  # pragma: no cover - misconfiguration
                raise ValueError(f"no thinker provided for role {role!r}") from exc
        return self._thinkers

    def run(self, seed_question: str, *, run_id: str | None = None) -> dict[str, Any]:
        """Run the full propose -> critique -> gather -> judge -> synthesize loop.

        Returns ``{run_id, claim_id, status, moves_made, thinker_calls, moves,
        final_signal, summary}`` where ``summary`` is the run-region counts
        (nodes by type/status, edges by relation) filtered on this run's stamp.
        """
        if not (isinstance(seed_question, str) and seed_question.strip()):
            raise ValueError("seed question cannot be empty")

        run_id = run_id or f"run-{uuid4().hex[:12]}"

        # Persist the CONFIGURED role -> family/model roster for this run so an
        # auditor can see, after the fact, that the critic was set up on a
        # different model family than the proposer (that config map is otherwise
        # gone at process exit). This records CONFIG, not a runtime probe of
        # which model answered each call — but run_cli_loop builds the thinkers
        # from this same config, so the shipped path is faithful. The BINDING
        # cross-family check is the judge-time gate (see ops.rule), not this
        # record. Built from config alone; touches no node/edge payload.
        cfg = self.config
        roles_manifest = {
            role: {
                "family": cfg.backend_for_role(role),
                "model": cfg.model_for_role(role),
                "author": cfg.author_for_role(role),
            }
            for role in ("proposer", "critic", "evidence_gatherer", "judge", "synthesizer")
        }
        manifest = ops.write_run_manifest(
            self.store,
            run_id,
            roles_manifest,
            cross_family_ok=cfg.critic_backend != cfg.proposer_backend,
            label_with_model=cfg.label_with_model,
        )

        playbook = ops.load_playbook(self.store)
        phases = playbook.get("phases", [])

        moves: list[dict[str, Any]] = []
        thinker_calls = 0
        moves_made = 0
        claim_id: str | None = None
        status = "completed"
        judged = False

        def budget_exceeded() -> bool:
            cfg = self.config
            # `>=` (not `>`): the pre-call check runs BEFORE the increment, so
            # `thinker_calls` is the number of calls already made. Stopping when
            # it has REACHED the max spends exactly `max_thinker_calls` metered
            # CLI calls, not max+1. Mirrors the `>=` on max_total_moves.
            if cfg.max_thinker_calls is not None and thinker_calls >= cfg.max_thinker_calls:
                return True
            if cfg.token_budget is not None:
                total = 0
                seen: set[int] = set()
                roles = (
                    self._thinkers.values()
                    if isinstance(self._thinkers, dict)
                    else [self._thinkers]
                )
                for t in roles:
                    if id(t) in seen:
                        continue
                    seen.add(id(t))
                    total += _thinker_tokens(t)
                if total > cfg.token_budget:
                    return True
            return False

        # --- Phase walk -----------------------------------------------------
        # The proposer phase seeds the root claim; every later phase acts on the
        # current claim pointer. Each phase honors its own max_iterations; the
        # hard cap and the cost ceiling are checked on every attempted move.
        loop_done = False
        for phase in phases:
            if loop_done:
                break
            role = phase.get("role")
            if role not in ROLE_MOVES:
                continue  # a playbook phase referencing an unknown role is skipped
            agent = RoleAgent(role, self.config, run_id)

            iterations = int(phase.get("max_iterations", 1))
            if role in ("critic", "evidence_gatherer") and self.config.rounds_override is not None:
                iterations = self.config.rounds_override

            # Non-proposer phases need a claim to act on; if the proposer never
            # produced one, there is nothing left to do.
            if role != "proposer" and claim_id is None:
                status = "frontier-empty"
                loop_done = True
                break

            for _ in range(max(0, iterations)):
                # HARD cap: count this attempt; abort the whole run if over.
                if len(moves) >= self.config.max_total_moves:
                    status = "hard-cap-reached"
                    loop_done = True
                    break
                if budget_exceeded():
                    status = "cost-ceiling-reached"
                    loop_done = True
                    break

                prompt, context = _render_prompt(
                    self.store, role, seed_question, claim_id
                )
                thinker = self._thinker_for(role)
                thinker_calls += 1
                result = agent.act(
                    self.store,
                    thinker,
                    target_id=claim_id,
                    prompt=prompt,
                    context=context,
                )
                moves.append(result.as_dict())
                # One increment per move (not per "round"): the engine is a
                # phase walk, not a round loop, so this counts moves made. Kept
                # distinct from thinker_calls for clarity even though they move
                # together today (every attempt appends exactly one move).
                moves_made += 1

                # A done/stop move is role-scoped, not a global kill switch.
                # The critic and evidence gatherer run BEFORE the judge, and
                # "nothing further to add" is their common, correct end-state
                # (the gatherer's own prompt says the critic supplies the
                # opposition). For those mid-pipeline roles, done means "this
                # role is finished" — advance to the next phase so the judge
                # still rules. Only a terminal role (or the proposer, which has
                # produced no claim to judge) ends the whole run.
                if result.outcome == "stop":
                    if role in ("critic", "evidence_gatherer"):
                        break  # role done; the phase walk advances to the judge
                    status = "model-stopped"
                    loop_done = True
                    break

                # The proposer's claim becomes the engine's current target.
                if role == "proposer" and result.outcome in ("written", "dedup"):
                    claim_id = result.node_id

                # A settled ruling is the natural terminal.
                if (
                    role == "judge"
                    and result.outcome == "written"
                    and result.move == "rule"
                ):
                    judged = True

                # After each mutation, check whether the current claim is still
                # actionable. If it left the frontier (judged/ratified) and
                # nothing else is actionable, stop early.
                if claim_id is not None and self._claim_off_frontier(claim_id):
                    if judged:
                        # Keep going only into the synthesize phase; break the
                        # inner loop so the phase walk advances.
                        break
                    if self._frontier_empty():
                        status = "frontier-empty"
                        loop_done = True
                        break

                # Token ceiling can also be tripped by token usage post-call.
                if budget_exceeded():
                    status = "cost-ceiling-reached"
                    loop_done = True
                    break

        if status == "completed" and judged:
            status = "judged"

        final_signal = (
            ops.referee_signal(self.store, claim_id) if claim_id is not None else None
        )
        return {
            "run_id": run_id,
            "claim_id": claim_id,
            "status": status,
            "moves_made": moves_made,
            "thinker_calls": thinker_calls,
            "moves": moves,
            "final_signal": final_signal,
            "summary": run_region_summary(self.store, run_id),
            "manifest": manifest,
        }

    # --- frontier helpers ---------------------------------------------------

    def _claim_off_frontier(self, claim_id: str) -> bool:
        """True when the current claim no longer appears on the work frontier.

        A judged/ratified claim drops off the frontier per ops, so this is how
        the engine learns the debate moved on.
        """
        feed = ops.frontier(self.store)
        return all(entry["node_id"] != claim_id for entry in feed["entries"])

    def _frontier_empty(self) -> bool:
        """True when nothing on the whole graph is actionable."""
        return ops.frontier(self.store)["total"] == 0


# ---------------------------------------------------------------------------
# CLI entry — lazily builds the REAL thinkers (the only path that needs the key)
# ---------------------------------------------------------------------------


def run_cli_loop(
    *,
    store: GraphStore,
    seed: str,
    run_id: str | None = None,
    rounds: int | None = None,
    max_moves: int | None = None,
) -> dict[str, Any]:
    """Build the live per-role CLI thinkers, run the engine, print a summary.

    This is the only place that constructs the real model backends. Importing
    :mod:`sparkle.thinker` is pure stdlib (no ``anthropic``, no ``openai``), so
    the import ALWAYS succeeds — there is no pip extra to miss. The real failure
    is a missing CLI at run time: the live thinkers shell out to ``claude`` /
    ``codex``. Before building them we check that every backend family the config
    actually uses has its CLI on PATH, and raise a clear :class:`ValueError`
    (caught by cli.py -> stderr + exit 2) telling the user to install + log in to
    the claude and codex CLIs, NOT to pip-install anything.

    Returns the engine's run dict (also printed in human form using the
    ``Handle:`` line convention so the output is greppable).
    """
    import shutil  # noqa: PLC0415 - stdlib, local to keep module-top imports lean

    config = HarnessConfig(rounds_override=rounds)
    if max_moves is not None:
        config.max_total_moves = max_moves

    # Runtime CLI-presence check (replaces the old pip-extra ImportError path).
    # Only the families this config actually uses need to be present.
    roles = ("proposer", "critic", "judge", "evidence_gatherer", "synthesizer")
    used_families = {config.backend_for_role(role) for role in roles}
    cli_for_family = {CLAUDE_FAMILY: "claude", CODEX_FAMILY: "codex"}
    missing = sorted(
        cli_for_family[fam]
        for fam in used_families
        if fam in cli_for_family and shutil.which(cli_for_family[fam]) is None
    )
    if missing:
        raise ValueError(
            "the adversarial loop runs on the local CLIs, not a Python SDK; "
            f"required CLI(s) not found on PATH: {', '.join(missing)}. Install "
            "and log in to the claude and codex CLIs (claude rides your Claude "
            "Max login, codex rides your Codex/ChatGPT login) — no API key or "
            "pip extra is needed"
        )

    # Pure-stdlib import — the factory builds subprocess-backed CLI thinkers and
    # never imports a model SDK. The missing-binary case is handled above.
    from .thinker import build_role_thinkers  # noqa: PLC0415

    thinkers = build_role_thinkers(config)
    engine = AutonomousEngine(store, thinkers, config)
    result = engine.run(seed, run_id=run_id)

    _print_summary(result)
    return result


def _print_summary(result: dict[str, Any]) -> None:
    """Human-readable run summary using the project's ``Handle:`` convention."""
    print(f"Run: {result['run_id']}")
    print(f"Status: {result['status']}")
    print(f"Moves: {result['moves_made']}  Thinker calls: {result['thinker_calls']}")
    if result.get("claim_id"):
        print(f"Handle: {result['claim_id'][:12]}")
    signal = result.get("final_signal")
    if signal:
        print(
            f"Final signal: {signal['live_signal']} "
            f"(supports={signal['supports']}, contradicts={signal['contradicts']}, "
            f"net={signal['net']})"
        )
    summary = result["summary"]
    print(
        f"Wrote {summary['node_count']} nodes, {summary['edge_count']} edges "
        f"in this run."
    )
    if summary["nodes_by_type"]:
        types = ", ".join(
            f"{count} {ntype}" for ntype, count in sorted(summary["nodes_by_type"].items())
        )
        print(f"Nodes by type: {types}")
    for move in result["moves"]:
        node_part = f" -> {move['node_id'][:12]}" if move.get("node_id") else ""
        msg = f" ({move['message']})" if move.get("message") else ""
        print(f"  {move['role']}/{move.get('move')}: {move['outcome']}{node_part}{msg}")
