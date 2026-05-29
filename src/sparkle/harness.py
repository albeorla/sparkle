"""Sparkle autonomous adversarial harness — the Phase 2 engine + role agents.

This module is the autonomous loop the spec calls for: a proposer states a
claim, a *different model* attacks it as the critic, evidence gets gathered, a
judge rules with the distinct-adversary gate ON, and a synthesizer harvests.
Every graph mutation is driven through :mod:`sparkle.ops`, so the engine
inherits all of the debate invariants and the Phase-2 integrity floor (the
self-loop ban + the distinct-adversary check) by construction.

Hard boundaries this module respects:

- It imports :mod:`sparkle.ops` and stdlib only. It NEVER imports ``anthropic``
  and NEVER imports :mod:`sparkle.thinker` at module top. The real model backend
  is injected as a duck-typed :class:`Thinker`; the live wrapper
  (:class:`sparkle.thinker.AnthropicThinker`) is imported lazily only inside
  :func:`run_cli_loop`.
- The engine is *pure with respect to the model*: it takes injected per-role
  thinkers, so the whole loop runs against a deterministic stub with no network
  and no API key. The live three-model debate is a manual human acceptance step,
  not something this engine validates.

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
    a real thinker can map role -> model id (the critic on a *different* model
    than the proposer). The stub thinker keys its scripted responses on ``role``.

    Because this is a structural ``Protocol``, the real
    :class:`sparkle.thinker.AnthropicThinker` and the test stub are
    interchangeable with zero shared base class and zero import of ``anthropic``
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
    so the stub and the real SDK wrapper both work without a shared base class.
    """
    value = getattr(thinker, "tokens_used", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Config — pure stdlib (dataclasses + os), reads env, holds NO secret
# ---------------------------------------------------------------------------


@dataclass
class HarnessConfig:
    """Engine configuration with the locked defaults and the locked invariant.

    Per-role model ids come from env with sane defaults. The one hard,
    testable contract: the critic model must differ from the proposer model so
    the adversary is a genuinely different model, not the proposer rephrasing
    itself. This dataclass holds no API key and imports no ``anthropic`` — it
    loads in the zero-dependency base test suite.
    """

    proposer_model: str = field(
        default_factory=lambda: os.environ.get(
            "SPARKLE_PROPOSER_MODEL", "claude-opus-4-8"
        )
    )
    critic_model: str = field(
        default_factory=lambda: os.environ.get(
            "SPARKLE_CRITIC_MODEL", "claude-sonnet-4-6"
        )
    )
    judge_model: str = field(
        default_factory=lambda: os.environ.get(
            "SPARKLE_JUDGE_MODEL", "claude-opus-4-8"
        )
    )
    # evidence_gatherer / synthesizer default to the critic / judge model but
    # are not part of the locked invariant.
    evidence_model: str = field(
        default_factory=lambda: os.environ.get(
            "SPARKLE_EVIDENCE_MODEL",
            os.environ.get("SPARKLE_CRITIC_MODEL", "claude-sonnet-4-6"),
        )
    )
    synthesizer_model: str = field(
        default_factory=lambda: os.environ.get(
            "SPARKLE_SYNTHESIZER_MODEL",
            os.environ.get("SPARKLE_JUDGE_MODEL", "claude-opus-4-8"),
        )
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
    # 'critic:claude-sonnet-4-6'. Default off — the bare role name is enough for
    # the distinct-adversary gate since proposer != critic.
    label_with_model: bool = False

    def __post_init__(self) -> None:
        if self.critic_model == self.proposer_model:
            raise ValueError(
                "critic model must differ from proposer model so the adversary "
                "is a genuinely different model, not the proposer rephrasing "
                "itself; set SPARKLE_CRITIC_MODEL to a different model"
            )
        # The evidence gatherer's 'oppose' move writes the SAME contradicts edge
        # the distinct-adversary gate counts, so if it ran on the proposer's own
        # model a single model could write both the claim and the only objection
        # that ratifies it. The attack must come from a genuinely different model,
        # so the gatherer is held to the same bar as the critic. (The judge is NOT
        # constrained here: it writes the ruling, not the objection, and the
        # locked defaults intentionally share a model between proposer and judge.)
        if self.evidence_model == self.proposer_model:
            raise ValueError(
                "evidence model must differ from proposer model: the evidence "
                "gatherer's 'oppose' move records the contradicts objection the "
                "judge counts, so running it on the proposer's own model would let "
                "one model both make and attack the claim; set "
                "SPARKLE_EVIDENCE_MODEL to a model different from the proposer"
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

    def model_for_author(self, author: str) -> str:
        """Recover the model id behind an author identity written by a role.

        Authors are bare role names by default (``proposer``/``critic``/...), or
        ``role:model`` when :attr:`label_with_model` is set. An author that maps
        to no known role (e.g. a human ``local`` write) is treated as its own
        distinct model — a human objection is genuinely a different adversary —
        so it returns the author string itself. This lets the engine enforce the
        locked *model*-distinctness of the adversary, not just author-string
        distinctness, when the judge rules.
        """
        if self.label_with_model and ":" in author:
            return author.rsplit(":", 1)[1]
        known_roles = {
            "proposer",
            "critic",
            "evidence_gatherer",
            "judge",
            "synthesizer",
        }
        if author in known_roles:
            return self.model_for_role(author)
        return author

    def author_for_role(self, role: str) -> str:
        """The distinct author identity a role writes with.

        Bare role name by default (sufficient for the distinct-adversary gate
        because proposer != critic). Optionally suffixed with the model id.
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
        "You are the CRITIC, running on a DIFFERENT model than the proposer. "
        "Attack the target claim with the strongest genuine objection — not a "
        "strawman. Return a single JSON object and nothing else:\n"
        '  {"move":"object","target":"<claim handle>",'
        '"title":"<short objection title>",'
        '"content":"<what weakens or falsifies the claim>","citations":[]}\n'
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "evidence_gatherer": (
        "You are the EVIDENCE GATHERER. Attach evidence that supports or "
        "opposes the target claim. Return a single JSON object and nothing "
        "else:\n"
        '  {"move":"support","target":"<claim handle>","title":"<short>",'
        '"content":"<supporting evidence>","citations":[]}\n'
        "  or\n"
        '  {"move":"oppose","target":"<claim handle>","title":"<short>",'
        '"content":"<opposing evidence>","citations":[]}\n'
        "Or stop with: {\"move\":\"done\",\"reason\":\"...\"}."
    ),
    "judge": (
        "You are the JUDGE. Rule on the target claim only after a genuine "
        "objection from a DIFFERENT author exists. Return a single JSON object "
        "and nothing else:\n"
        '  {"move":"rule","target":"<claim handle>",'
        '"verdict":"<your verdict>","rationale":"<why>","settle":true}\n'
        "Set settle=true only to ratify. "
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


def _model_distinct_objection(
    store: GraphStore, claim_id: str, claim_author: str, config: HarnessConfig
) -> bool:
    """True when an inbound ``contradicts`` objection comes from a DIFFERENT model.

    The seam's ``require_distinct_adversary`` gate (in :func:`ops.rule`) enforces a
    different *author string*. That is necessary but not sufficient for the
    locked Phase-2 decision: the adversary must run on a genuinely DIFFERENT MODEL
    than the proposer, so the attack is not the proposer's own reasoning
    rephrased. This engine-side check closes the gap where a role with a distinct
    author (e.g. the evidence gatherer's ``oppose``) happens to run on the same
    model as the proposer. The engine's judge requires at least one objection
    whose author maps to a model different from the claim author's model.
    """
    claim_model = config.model_for_author(claim_author)
    data = store.read()
    for edge in data["edges"].values():
        if edge["relation"] != "contradicts" or edge["to_id"] != claim_id:
            continue
        src = data["nodes"].get(edge["from_id"])
        if src is None:
            continue
        if config.model_for_author(src.get("author", "local")) != claim_model:
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
        text = thinker.think(
            role=self.role, system=system, prompt=prompt, context=context
        )
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
                # Engine-side model-distinctness gate (the locked Phase-2
                # decision): the seam's require_distinct_adversary only checks the
                # author STRING differs. Before we let the judge rule, also require
                # that at least one objection came from a genuinely DIFFERENT
                # MODEL than the claim's author — otherwise a role with a distinct
                # author but the proposer's model (e.g. the evidence gatherer's
                # 'oppose') could ratify a self-attacked claim. Refuse here so the
                # loop does not settle, exactly as an ops refusal would.
                target_node = ops.get_node(store, move["target"])
                claim_author = target_node.get("author", "local")
                if not _model_distinct_objection(
                    store, target_node["node_id"], claim_author, self.config
                ):
                    return MoveResult(
                        self.role,
                        name,
                        "refused",
                        message=(
                            "no objection came from a model different than the "
                            "claim's; the adversary must run on a different model "
                            "than the proposer, not just under a different author"
                        ),
                    )
                result = ops.rule(
                    store,
                    move["target"],
                    verdict=move["verdict"],
                    rationale=move.get("rationale", ""),
                    settle=bool(move.get("settle", False)),
                    author=self.author,
                    confidence=0.8,
                    run_id=self.run_id,
                    require_distinct_adversary=True,
                )
                node_id = result["decision"]["node_id"]
                return MoveResult(self.role, name, "written", node_id=node_id)

            if name == "harvest":
                if not (
                    _nonempty(move.get("target"))
                    and _nonempty(move.get("title"))
                    and _nonempty(move.get("content"))
                ):
                    return MoveResult(self.role, name, "rejected", message="target+title+content required")
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
    protocol only; it never imports ``anthropic``.

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

        Returns ``{run_id, claim_id, status, rounds_run, thinker_calls, moves,
        final_signal, summary}`` where ``summary`` is the run-region counts
        (nodes by type/status, edges by relation) filtered on this run's stamp.
        """
        if not (isinstance(seed_question, str) and seed_question.strip()):
            raise ValueError("seed question cannot be empty")

        run_id = run_id or f"run-{uuid4().hex[:12]}"
        playbook = ops.load_playbook(self.store)
        phases = playbook.get("phases", [])

        moves: list[dict[str, Any]] = []
        thinker_calls = 0
        rounds_run = 0
        claim_id: str | None = None
        status = "completed"
        judged = False

        def budget_exceeded() -> bool:
            cfg = self.config
            if cfg.max_thinker_calls is not None and thinker_calls > cfg.max_thinker_calls:
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
                rounds_run += 1

                # An explicit done/stop move ends the loop immediately.
                if result.outcome == "stop":
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
            "rounds_run": rounds_run,
            "thinker_calls": thinker_calls,
            "moves": moves,
            "final_signal": final_signal,
            "summary": run_region_summary(self.store, run_id),
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
    """Build the live per-role thinkers, run the engine, print a human summary.

    This is the only place that constructs the real model backend, and it does
    so lazily: importing :mod:`sparkle.thinker` (which lazily imports
    ``anthropic``) happens HERE, not at module top, so ``from .harness import
    run_cli_loop`` never transitively requires the ``agents`` extra. If the
    extra is missing, the import raises :class:`ImportError`, which the CLI
    catches to print the ``pip install 'sparkle[agents]'`` hint.

    Returns the engine's run dict (also printed in human form using the
    ``Handle:`` line convention so the output is greppable).
    """
    # Lazy import — keeps the zero-dependency import path clean. A missing
    # extra surfaces as ImportError, caught by cli.py.
    from .thinker import build_role_thinkers  # noqa: PLC0415

    config = HarnessConfig(rounds_override=rounds)
    if max_moves is not None:
        config.max_total_moves = max_moves

    thinkers = build_role_thinkers(config)
    engine = AutonomousEngine(store, thinkers, config)
    result = engine.run(seed, run_id=run_id)

    _print_summary(result)
    return result


def _print_summary(result: dict[str, Any]) -> None:
    """Human-readable run summary using the project's ``Handle:`` convention."""
    print(f"Run: {result['run_id']}")
    print(f"Status: {result['status']}")
    print(f"Rounds: {result['rounds_run']}  Thinker calls: {result['thinker_calls']}")
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
