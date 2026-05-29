"""Sparkle thinkers — the ONLY module that talks to the Anthropic model backend.

This is the live-path edge of the Phase 2 autonomous harness. The autonomous
engine (:mod:`sparkle.harness`) is pure with respect to the model: it drives the
debate by calling a *thinker* — any object with a single ``think(...)`` method
(the ``Thinker`` protocol declared in ``harness.py``). This module supplies the
real thinker that wraps the official Anthropic Python SDK; a deterministic stub
thinker lives in the test suite for network-free, key-free runs.

Hard dependency boundary (this is the whole point of the module):

- ``import sparkle.thinker`` MUST succeed with ZERO third-party packages
  installed. Nothing at module top imports ``anthropic``.
- ``anthropic`` is imported **lazily**, inside :meth:`AnthropicThinker.__init__`
  (and only there). Constructing or using the real thinker is what needs the
  ``sparkle[agents]`` extra and a key — merely importing this module does not.
- This is the *only* file in the package that ever names ``anthropic``. The
  engine, the seam (``ops.py``), the CLI core, and the MCP server stay
  zero-dependency.

What the real thinker does:

- Reads the key from the ``ANTHROPIC_API_KEY`` environment variable (the SDK's
  own default, made explicit here so a missing key fails with a clear message
  before any network call is attempted).
- Calls the Messages API (``client.messages.create``) with a system prompt, a
  single user turn, and a model id, and returns the concatenated text of the
  response's text blocks as a plain string.

What the per-role factory does:

- Builds one real thinker per debate role (proposer / critic / judge, plus
  evidence-gatherer and synthesizer) from the per-role model ids in the
  environment, enforcing the locked invariant that the **critic model differs
  from the proposer model** so the adversary is a genuinely different model and
  not the proposer rephrasing itself.
- Raises a single, actionable :class:`ImportError` if the SDK is missing and a
  :class:`RuntimeError` if the key is missing, so the CLI ``run`` command can
  print the ``pip install 'sparkle[agents]'`` hint (or the key hint) instead of
  surfacing a raw stack trace.

No live API call is made at import time, at construction time, or by the factory
— a call only happens when something invokes :meth:`AnthropicThinker.think`.
"""

from __future__ import annotations

import os
from typing import Any

# Locked default per-role models (see the Phase 2 spec). The invariant the
# config must preserve is critic_model != proposer_model: the critic must run on
# a different Claude model so its attack is not the proposer's own reasoning
# rephrased. evidence_gatherer defaults to the critic model and synthesizer to
# the judge model; both are env-overridable but only critic != proposer is
# locked.
DEFAULT_PROPOSER_MODEL = "claude-opus-4-8"
DEFAULT_CRITIC_MODEL = "claude-sonnet-4-6"
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# Env var names for the per-role model overrides (shared vocabulary with
# harness.HarnessConfig so the two stay in lockstep).
PROPOSER_MODEL_ENV = "SPARKLE_PROPOSER_MODEL"
CRITIC_MODEL_ENV = "SPARKLE_CRITIC_MODEL"
JUDGE_MODEL_ENV = "SPARKLE_JUDGE_MODEL"
EVIDENCE_MODEL_ENV = "SPARKLE_EVIDENCE_MODEL"
SYNTHESIZER_MODEL_ENV = "SPARKLE_SYNTHESIZER_MODEL"

# The SDK reads this; we read it too so a missing key is a clear error.
API_KEY_ENV = "ANTHROPIC_API_KEY"

# The pip hint the CLI surfaces on ImportError. Kept here so the message is
# defined once next to the lazy import that can raise it.
INSTALL_HINT = "pip install 'sparkle[agents]'"

# Conservative default cap on generated tokens per move. The engine's role
# prompts ask for a single small JSON object, so this is plenty; it is exposed
# as a constructor knob for callers who want to widen it.
DEFAULT_MAX_TOKENS = 1024


class AnthropicThinker:
    """A live thinker backed by the official Anthropic Python SDK Messages API.

    Structurally satisfies the ``Thinker`` protocol declared in
    :mod:`sparkle.harness` (a single ``think`` method) without inheriting from
    it — the engine duck-types, so no shared base class and no import of the
    harness is required here.

    The ``anthropic`` package is imported lazily inside :meth:`__init__`, so
    importing this module never requires the ``sparkle[agents]`` extra. The
    client is created once and reused across ``think`` calls. No request is sent
    until :meth:`think` is called.

    :param model: the Claude model id this thinker calls (e.g.
        ``"claude-opus-4-8"``).
    :param api_key: optional explicit key; defaults to the
        ``ANTHROPIC_API_KEY`` environment variable.
    :param max_tokens: cap on generated tokens per :meth:`think` call.
    :param client: optional pre-built client (mainly for testing the call
        shape without the SDK installed); when given, the lazy ``anthropic``
        import is skipped entirely.
    :raises ImportError: if ``anthropic`` is not installed and no ``client`` was
        supplied. The message includes the ``pip install 'sparkle[agents]'``
        hint.
    :raises RuntimeError: if no key is available (neither ``api_key`` nor the
        ``ANTHROPIC_API_KEY`` environment variable).
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ValueError("AnthropicThinker requires a non-empty model id")

        self.model = model
        self.max_tokens = max_tokens
        # Token usage accumulated across think() calls so an engine cost ceiling
        # can read it. Starts at zero; updated from each response's usage block.
        self.input_tokens = 0
        self.output_tokens = 0

        if client is not None:
            # Injected client (test seam): trust the caller; do not touch the
            # SDK or the environment so this path needs neither the package nor
            # a key.
            self._client = client
            return

        # Resolve the key BEFORE the network is ever touched so a missing key is
        # a clear, actionable error rather than a deep SDK auth failure.
        resolved_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        if not resolved_key:
            raise RuntimeError(
                "no Anthropic API key found; set the "
                f"{API_KEY_ENV} environment variable to run the live "
                "adversarial loop (the autonomous engine can also be driven by "
                "a stub thinker with no key for testing)"
            )

        # LAZY import — the entire reason this module exists. Importing
        # sparkle.thinker must work with zero third-party packages; only
        # constructing the real thinker pulls in the SDK.
        try:
            from anthropic import Anthropic  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - exercised only off-suite
            raise ImportError(
                "the Anthropic SDK is required to run the live adversarial loop; "
                f"install the agents extra with: {INSTALL_HINT}"
            ) from exc

        self._client = Anthropic(api_key=resolved_key)

    def think(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Send one system+user turn to the model and return its text.

        Satisfies the engine's ``Thinker`` contract: a single text blob comes
        back; the calling role agent is responsible for parsing a structured
        move out of it. The engine never assumes JSON-mode, tool-calling, or
        streaming — this method does a plain single-shot Messages call.

        ``role`` is informational here (this thinker is already bound to one
        model via :attr:`model`); the per-role model selection happens in the
        factory, which builds a separate thinker per role. ``context`` is the
        engine's frontier/subgraph snapshot; it is not sent verbatim — the
        engine bakes whatever the model needs into ``system``/``prompt`` — so it
        is accepted and ignored here.

        :returns: the concatenated text of all text blocks in the response. An
            empty string if the response carried no text blocks.
        """
        _ = role, context  # accepted for protocol parity; not used by this thinker

        message = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        # Accumulate usage if the SDK reported it, so an engine cost ceiling can
        # observe real token spend. Guarded because a stub/fake client may omit
        # it.
        usage = getattr(message, "usage", None)
        if usage is not None:
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

        return _extract_text(message)

    @property
    def total_tokens(self) -> int:
        """Total input+output tokens seen across all :meth:`think` calls."""
        return self.input_tokens + self.output_tokens


def _extract_text(message: Any) -> str:
    """Join the text of every text block in a Messages API response.

    The SDK returns ``message.content`` as a list of content blocks; text blocks
    have ``type == "text"`` and a ``.text`` attribute. Tool-use or other block
    types are skipped. Returns ``""`` if there is no text. Defensive against a
    response object that exposes ``content`` as dicts rather than typed blocks.
    """
    content = getattr(message, "content", None)
    if content is None:
        return ""

    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)

    return "".join(parts)


def _role_model_ids() -> dict[str, str]:
    """Resolve the per-role model ids from the environment, with locked defaults.

    The only LOCKED constraint is critic_model != proposer_model; that is
    enforced by :func:`build_role_thinkers`, not here, so this helper stays a
    pure read.
    """
    proposer = os.environ.get(PROPOSER_MODEL_ENV, DEFAULT_PROPOSER_MODEL)
    critic = os.environ.get(CRITIC_MODEL_ENV, DEFAULT_CRITIC_MODEL)
    judge = os.environ.get(JUDGE_MODEL_ENV, DEFAULT_JUDGE_MODEL)
    # evidence_gatherer defaults to the critic model, synthesizer to the judge
    # model (per the spec); both are independently env-overridable.
    evidence = os.environ.get(EVIDENCE_MODEL_ENV, critic)
    synthesizer = os.environ.get(SYNTHESIZER_MODEL_ENV, judge)
    return {
        "proposer": proposer,
        "critic": critic,
        "judge": judge,
        "evidence_gatherer": evidence,
        "synthesizer": synthesizer,
    }


def build_role_thinkers(
    config: Any | None = None,
    *,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, AnthropicThinker]:
    """Build one live thinker per debate role from the per-role model ids.

    This is the live-path factory the CLI ``run`` command uses. When a
    :class:`~sparkle.harness.HarnessConfig` is passed, the per-role model ids and
    the locked adversary invariants come from it (one source of truth shared with
    the engine, so the factory and the engine can never drift). When no config is
    given, the model ids are read from the environment with the locked defaults,
    and the critic-vs-proposer invariant is enforced here as a fallback.

    :param config: an optional object exposing ``model_for_role(role)`` (the
        engine's :class:`HarnessConfig`). When given, its ``__post_init__``
        already enforced critic != proposer and evidence != proposer, so the
        factory trusts those and just reads each role's model id from it.
    :returns: a dict mapping each role name (``proposer``, ``critic``,
        ``judge``, ``evidence_gatherer``, ``synthesizer``) to its
        :class:`AnthropicThinker`. Roles that share a model id still get
        separate thinker instances so each keeps its own token tally and a
        distinct author identity downstream.
    :raises ValueError: if the resolved critic model equals the proposer model
        (the adversary would not be a genuinely different model). Only checked
        here when no ``config`` was supplied; the config enforces it itself.
    :raises ImportError: if the Anthropic SDK is not installed (message carries
        the ``pip install 'sparkle[agents]'`` hint).
    :raises RuntimeError: if no key is available in ``ANTHROPIC_API_KEY``.
    """
    if config is not None:
        roles = ("proposer", "critic", "judge", "evidence_gatherer", "synthesizer")
        models = {role: config.model_for_role(role) for role in roles}
    else:
        models = _role_model_ids()
        if models["critic"] == models["proposer"]:
            raise ValueError(
                "critic model must differ from proposer model so the adversary "
                "is a genuinely different model, not the proposer rephrasing "
                f"itself; set {CRITIC_MODEL_ENV} to a model different from "
                f"{PROPOSER_MODEL_ENV} (both are currently {models['proposer']!r})"
            )

    thinkers: dict[str, AnthropicThinker] = {}
    for role, model_id in models.items():
        # Each role gets its own thinker instance even when two roles share a
        # model id: separate contexts, separate token tallies, and a distinct
        # author identity per role downstream in the engine. The first
        # construction performs the lazy SDK import and the key check; if those
        # fail, the error propagates to the CLI on the first role.
        thinkers[role] = AnthropicThinker(
            model_id, api_key=api_key, max_tokens=max_tokens
        )

    return thinkers


__all__ = [
    "AnthropicThinker",
    "build_role_thinkers",
    "DEFAULT_PROPOSER_MODEL",
    "DEFAULT_CRITIC_MODEL",
    "DEFAULT_JUDGE_MODEL",
    "INSTALL_HINT",
]
