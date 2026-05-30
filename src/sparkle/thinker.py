"""Sparkle thinkers — the ONLY module that shells out to a model CLI backend.

This is the live-path edge of the Phase 2 autonomous harness. The autonomous
engine (:mod:`sparkle.harness`) is pure with respect to the model: it drives the
debate by calling a *thinker* — any object with a single ``think(...)`` method
(the ``Thinker`` protocol declared in ``harness.py``). This module supplies the
real thinkers, which shell out to local command-line tools, plus a deterministic
stub thinker for network-free, key-free runs.

Why CLIs instead of an SDK / API key:

- ``claude -p`` rides Albert's Claude Max login; ``codex exec`` rides the
  Codex/ChatGPT login. Both bill against the existing subscription, not a
  per-token Console API key. So there is NO ``anthropic`` / ``openai`` pip
  package here and NO ``ANTHROPIC_API_KEY``: the real thinkers run the
  already-installed ``claude`` / ``codex`` binaries via :mod:`subprocess`.
- Real adversarial diversity comes from DIFFERENT MODEL FAMILIES (Claude vs
  GPT), not different Claude tiers, so every Claude-side role runs Opus 4.8 and
  the critic runs GPT-5.5 through the codex CLI.

Hard dependency boundary (the whole point of the module):

- ``import sparkle.thinker`` MUST succeed with ZERO third-party packages
  installed. Everything imported at module top is stdlib (subprocess, json,
  shutil, tempfile, os).
- This module names no SDK. The engine, the seam (``ops.py``), the CLI core, and
  the MCP server stay zero-dependency.

The test seam: each real thinker takes an INJECTABLE ``runner`` (a callable
wrapping :func:`subprocess.run`) defaulting to the real one, so tests can drive
argv construction and output parsing WITHOUT spawning a real CLI process. No
live CLI call is made at import time, at construction time, or by the factory —
a call only happens when something invokes ``think`` with the default runner.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable

# Locked per-role defaults. Adversarial diversity is cross-FAMILY (Claude vs
# GPT), so every Claude-side role runs Opus 4.8 and the codex critic runs
# GPT-5.5. These mirror the HarnessConfig defaults; the config is the source of
# truth and the factory reads from it.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_CODEX_MODEL = "gpt-5.5"

# Backend family ids the factory dispatches on. These are the literal strings a
# HarnessConfig returns from ``backend_for_role`` / ``family_for_author``.
FAMILY_CLAUDE = "claude"
FAMILY_CODEX = "codex"

# CLI binary names looked up on PATH (used for the actionable missing-CLI hint).
CLAUDE_BIN = "claude"
CODEX_BIN = "codex"

# The locked-down adversarial claude thinker runs with NO tools at all. The
# debate roles only ever return text, so the lockdown is deny-ALL, not a
# deny-list: `--tools ""` disables every built-in tool (verified against Claude
# Code 2.1.157 — the init event then reports an empty tools array). A deny-list
# was tried first and was unsafe: blocking 7 named tools still left ~21 reachable
# (Read/Glob/Grep the disk, Cron/RemoteTrigger schedule work, TaskCreate spawns
# an un-sandboxed subagent), and it named the wrong subagent tool ("Task" instead
# of "TaskCreate"). `--tools ""` removes tools from the AVAILABLE set, so it is
# stronger than permission-gating: no settings allow-rule can re-open a tool that
# does not exist. One tool (LSP) survives `--tools ""`, so it is denied
# explicitly here to reach a true zero-tool session.
CLAUDE_RESIDUAL_DENY = ("LSP",)

# The ONE exception to deny-all: the evidence-gatherer role may be given real web
# access so it can VERIFY sources instead of reciting figures from memory (a
# tool-denied model fabricates precise citations with false confidence — a
# verified failure mode). Web access takes BOTH flags, doing different jobs
# (verified live against Claude Code 2.1.157): `--tools WebSearch WebFetch`
# restricts the AVAILABLE built-in set to exactly these two (plus the LSP
# survivor) so nothing dangerous (Bash/Write/Task/ToolSearch/MCP) is even
# reachable, and `--allowedTools WebSearch WebFetch` PRE-APPROVES them so they
# run headlessly (otherwise `--permission-mode default` blocks them at the gate).
CLAUDE_WEB_TOOLS = ("WebSearch", "WebFetch")

# Per-call subprocess timeout (seconds). codex in particular runs at high
# reasoning effort and is token-heavy, so the default is generous; both thinkers
# expose it as a constructor knob.
DEFAULT_TIMEOUT_SECONDS = 600


# A runner wraps subprocess.run so tests can inject a fake. It takes the argv
# list plus a kwargs dict (cwd / timeout / input / capture_output / text /
# check) and returns something shaped like a CompletedProcess (returncode,
# stdout, stderr).
Runner = Callable[[list[str], "dict[str, Any]"], "subprocess.CompletedProcess[str]"]


def _default_runner(
    argv: list[str], kwargs: dict[str, Any]
) -> "subprocess.CompletedProcess[str]":
    """The real runner: a thin wrapper over :func:`subprocess.run`.

    Kept tiny and side-effect-free apart from the spawn so the injected fake
    runner in the tests is a drop-in replacement (same signature, same return
    shape). ``check=False`` because each thinker inspects the return code itself
    to raise a clear, family-tagged error.
    """
    return subprocess.run(argv, **kwargs)  # noqa: S603 - argv is fully constructed by us


class ClaudeCliThinker:
    """A live thinker backed by the ``claude -p`` command-line tool (Claude Max).

    Structurally satisfies the ``Thinker`` protocol declared in
    :mod:`sparkle.harness` (a single ``think`` method) without inheriting from
    it — the engine duck-types, so no shared base class and no import of the
    harness is required here.

    The thinker is locked down for adversarial use: it disables ALL built-in
    tools (deny-all, not a deny-list — see :data:`CLAUDE_RESIDUAL_DENY` and
    :meth:`_build_argv`), forces a non-auto-approve permission mode, loads no MCP
    servers, and loads only user-level settings (no project/local allow-rules),
    and it runs in a fresh, isolated working directory (a throwaway tempdir,
    removed after the call). With zero tools available the model can reason and
    answer but cannot act on the machine at all. The ONE exception is
    ``web_search=True`` (the evidence-gatherer role), which adds web search/fetch
    and ONLY those (see :data:`CLAUDE_WEB_TOOLS`) so the role can verify sources
    rather than recite them from memory; nothing else becomes reachable. The
    one-shot ``-p`` mode has no separate system channel, so the engine's system
    prompt is folded into the prompt text. No process is spawned until
    :meth:`think` is called.

    :param model: the Claude model id passed to ``--model`` (defaults to Opus
        4.8, the locked Claude-side model for every role).
    :param runner: an injected callable wrapping :func:`subprocess.run` (the test
        seam); defaults to the real runner.
    :param timeout: per-call subprocess timeout in seconds.
    """

    family = FAMILY_CLAUDE

    def __init__(
        self,
        model: str = DEFAULT_CLAUDE_MODEL,
        *,
        runner: Runner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        web_search: bool = False,
    ) -> None:
        if not model:
            raise ValueError("ClaudeCliThinker requires a non-empty model id")
        self.model = model
        self.timeout = timeout
        # When True, this role gets real web search/fetch (and ONLY those) so it
        # can verify sources instead of reciting them from memory. The factory
        # sets it for the evidence gatherer; every other role stays deny-all.
        self.web_search = bool(web_search)
        self._runner: Runner = runner if runner is not None else _default_runner
        # Token usage accumulated across think() calls so the engine's
        # token-budget ceiling (harness._thinker_tokens reads `tokens_used`) can
        # observe real spend. Starts at zero; updated from each result element's
        # usage block when present.
        self.tokens_used = 0

    def _build_argv(self, *, system: str, prompt: str) -> list[str]:
        """Construct the exact ``claude`` argv, including the tool lockdown.

        Tool lockdown (verified against Claude Code 2.1.157): ``--tools ""``
        disables ALL built-in tools, so the model has nothing to act with;
        ``--disallowedTools LSP`` removes the one tool that survives ``--tools ""``
        for a true zero-tool session; ``--permission-mode default`` keeps the
        user's ``dontAsk`` auto-approve from running anything headless;
        ``--strict-mcp-config`` (no ``--mcp-config``) loads NO MCP servers; and
        ``--setting-sources user`` loads only user-level settings so a project or
        local allow-rule cannot re-open a tool for the adversarial run. The model
        can reason and answer but cannot act on the machine. Split out so the test
        suite can assert the argv shape without spawning the CLI.

        When :attr:`web_search` is set (the evidence gatherer), the deny-all
        ``--tools ""`` is replaced by ``--tools WebSearch WebFetch`` (availability
        restricted to exactly the web pair plus the LSP survivor) AND
        ``--allowedTools WebSearch WebFetch`` (pre-approval so they run under
        ``--permission-mode default``). Nothing dangerous becomes reachable; the
        role gains real source verification. ``--disallowedTools LSP`` stays last
        (variadic), with the variadic ``--tools``/``--allowedTools`` lists each
        terminated by the next flag.
        """
        folded = _fold_system(system, prompt)
        argv = [
            CLAUDE_BIN,
            "-p",
            folded,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--permission-mode",
            "default",
            "--strict-mcp-config",
            "--setting-sources",
            "user",
        ]
        if self.web_search:
            argv += [
                "--tools",
                *CLAUDE_WEB_TOOLS,
                "--allowedTools",
                *CLAUDE_WEB_TOOLS,
            ]
        else:
            argv += ["--tools", ""]
        argv += ["--disallowedTools", *CLAUDE_RESIDUAL_DENY]
        return argv

    def think(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Run one ``claude -p`` turn in an isolated dir and return its text.

        ``role`` is informational (this thinker is bound to one model via the
        factory, which builds a separate instance per role). ``context`` is the
        engine's frontier snapshot; the engine bakes whatever the model needs
        into ``system``/``prompt``, so it is accepted and ignored here.

        :raises RuntimeError: on timeout, non-zero exit, malformed JSON, a
            result element flagged ``is_error``, or an empty answer. A crash is
            never returned as a valid answer — the engine records it as a failed
            move.
        """
        _ = role, context  # accepted for protocol parity; not used by this thinker

        argv = self._build_argv(system=system, prompt=prompt)
        workdir = tempfile.mkdtemp(prefix="sparkle-claude-")
        try:
            proc = _run(
                self._runner,
                argv,
                cwd=workdir,
                timeout=self.timeout,
                family=FAMILY_CLAUDE,
            )
            _check_returncode(proc, family=FAMILY_CLAUDE)
            return self._parse(proc.stdout)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _parse(self, stdout: str | None) -> str:
        """Parse the claude JSON event array and return the result text.

        stdout is a single JSON array of events; the answer lives in the element
        whose ``type`` is ``"result"``. If that element is flagged
        ``is_error`` the run failed and we raise. Token usage is accumulated from
        the result element's ``usage`` block when present.
        """
        if not stdout or not stdout.strip():
            raise RuntimeError("claude CLI returned empty output")
        try:
            events = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude CLI returned malformed JSON: {exc}"
            ) from exc

        result_el = _find_result_element(events)
        if result_el is None:
            raise RuntimeError(
                "claude CLI output had no result element (no event with "
                'type == "result")'
            )
        if result_el.get("is_error"):
            raise RuntimeError(
                "claude CLI reported an error result: "
                f"{result_el.get('result') or result_el}"
            )

        answer = result_el.get("result")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("claude CLI result element carried no answer text")

        self.tokens_used += _claude_usage_tokens(result_el.get("usage"))
        return answer


class CodexCliThinker:
    """A live thinker backed by the ``codex exec`` command-line tool (GPT-5.5).

    Structurally satisfies the ``Thinker`` protocol. Locked down for adversarial
    use: it runs ``codex`` with the read-only sandbox in a fresh, isolated
    working directory (``-C <tempdir>``) so it cannot mutate anything, and reads
    the final answer from the ``--output-last-message`` file written inside that
    tempdir (stdout is ignored for the answer). The whole tempdir is removed
    after the call. No process is spawned until :meth:`think` is called.

    :param model: the model id passed to ``-m`` (defaults to GPT-5.5).
    :param runner: an injected callable wrapping :func:`subprocess.run` (the test
        seam); defaults to the real runner.
    :param timeout: per-call subprocess timeout in seconds.
    :param reasoning_effort: optional override appended as
        ``-c model_reasoning_effort=<value>``. Default ``None`` leaves the user's
        codex config in charge (codex already runs at high effort by default).
    """

    family = FAMILY_CODEX

    def __init__(
        self,
        model: str = DEFAULT_CODEX_MODEL,
        *,
        runner: Runner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        reasoning_effort: str | None = None,
    ) -> None:
        if not model:
            raise ValueError("CodexCliThinker requires a non-empty model id")
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self._runner: Runner = runner if runner is not None else _default_runner
        # Mirrors ClaudeCliThinker: the engine reads `tokens_used` for its budget
        # ceiling. The codex CLI does not surface per-call token usage on the
        # answer file, so this stays at zero unless a future codex flag exposes
        # it; the ceiling simply never trips on codex spend.
        self.tokens_used = 0

    def _build_argv(self, *, system: str, prompt: str, workdir: str, answer_file: str) -> list[str]:
        """Construct the exact ``codex exec`` argv.

        Isolated cwd via ``-C <workdir>``; read-only sandbox; the final answer is
        written to ``answer_file`` (inside the same tempdir) via
        ``--output-last-message``. ``--skip-git-repo-check`` because the throwaway
        tempdir is not a git repo. Split out so tests can assert the argv shape
        (model, sandbox, isolated cwd, output file, and any reasoning-effort
        override) without spawning the CLI.
        """
        folded = _fold_system(system, prompt)
        argv = [
            CODEX_BIN,
            "exec",
            folded,
            "-m",
            self.model,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            workdir,
            "--output-last-message",
            answer_file,
        ]
        if self.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
        return argv

    def think(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Run one ``codex exec`` turn in an isolated dir and return its text.

        ``role`` and ``context`` are accepted for protocol parity and ignored
        (the factory binds one instance per role; the engine bakes context into
        the prompt).

        :raises RuntimeError: on timeout, non-zero exit, or an empty/missing
            answer file. A crash is never returned as a valid answer.
        """
        _ = role, context  # accepted for protocol parity; not used by this thinker

        workdir = tempfile.mkdtemp(prefix="sparkle-codex-")
        answer_file = os.path.join(workdir, "last-message.txt")
        argv = self._build_argv(
            system=system, prompt=prompt, workdir=workdir, answer_file=answer_file
        )
        try:
            proc = _run(
                self._runner,
                argv,
                cwd=workdir,
                timeout=self.timeout,
                family=FAMILY_CODEX,
            )
            _check_returncode(proc, family=FAMILY_CODEX)
            return self._read_answer(answer_file)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _read_answer(self, answer_file: str) -> str:
        """Read the final-message file codex wrote; raise if it is missing/empty."""
        try:
            with open(answer_file, "r", encoding="utf-8") as handle:
                answer = handle.read()
        except OSError as exc:
            raise RuntimeError(
                "codex CLI wrote no answer file "
                f"({os.path.basename(answer_file)} missing): {exc}"
            ) from exc
        if not answer or not answer.strip():
            raise RuntimeError("codex CLI answer file was empty")
        return answer


class ScriptedThinker:
    """A deterministic, network-free stub thinker for key-free / test runs.

    Spawns no process and needs no CLI. It returns canned text keyed by ``role``
    (falling back to a constant default), so non-test callers have a thinker that
    drives the engine end-to-end with zero dependencies. The harness test suite
    ships its own richer stub; this one exists so importing :mod:`sparkle.thinker`
    always yields a usable, side-effect-free thinker.

    :param responses: optional mapping of role name -> canned answer text.
    :param default: text returned for any role not in ``responses``.
    """

    family = "stub"

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        default: str = "{}",
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        # Present so the engine's duck-typed token reader finds it; a stub spends
        # no tokens, so the budget ceiling never trips on it.
        self.tokens_used = 0

    def think(
        self,
        *,
        role: str,
        system: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        _ = system, prompt, context  # accepted for protocol parity; not used
        return self.responses.get(role, self.default)


# Backwards-friendly alias: the harness test suite and other callers may refer
# to a "stub thinker". Both names point at the same deterministic class.
StubThinker = ScriptedThinker


# ---------------------------------------------------------------------------
# Per-role factory — builds CLI thinkers from the config's role->backend map
# ---------------------------------------------------------------------------


_ROLES = ("proposer", "critic", "judge", "evidence_gatherer", "synthesizer")


def build_role_thinkers(config: Any) -> dict[str, Any]:
    """Build one CLI thinker per debate role from the config's backend map.

    For each of the five roles the engine drives, this reads the backend FAMILY
    (``config.backend_for_role(role)`` -> ``"claude"`` | ``"codex"``) and the
    model id (``config.model_for_role(role)``) and constructs the matching CLI
    thinker. Each role gets its OWN instance even when two roles share a backend
    and model, so each keeps a separate token tally and a distinct author
    identity downstream in the engine.

    No SDK import, no api-key param, no key check. The missing-binary check is a
    runtime concern surfaced by :func:`missing_clis` (which the CLI ``run``
    command calls to print an actionable hint) — the factory itself raises only
    on an unknown backend family.

    :param config: a :class:`~sparkle.harness.HarnessConfig` exposing
        ``backend_for_role(role)``, ``model_for_role(role)`` and (optionally) a
        ``reasoning_effort`` attribute for the codex critic.
    :returns: a dict mapping each role name to its CLI thinker.
    :raises ValueError: if a role maps to an unknown backend family.
    """
    reasoning_effort = getattr(config, "reasoning_effort", None) or None
    thinkers: dict[str, Any] = {}
    for role in _ROLES:
        family = config.backend_for_role(role)
        model = config.model_for_role(role)
        if family == FAMILY_CLAUDE:
            # The evidence gatherer gets real web search so it can verify sources
            # instead of reciting them from memory; every other Claude role stays
            # deny-all. Off-switch: config.evidence_web_search = False.
            web = role == "evidence_gatherer" and getattr(
                config, "evidence_web_search", True
            )
            thinkers[role] = ClaudeCliThinker(model, web_search=web)
        elif family == FAMILY_CODEX:
            thinkers[role] = CodexCliThinker(model, reasoning_effort=reasoning_effort)
        else:
            raise ValueError(
                f"unknown backend family {family!r} for role {role!r}; "
                f'expected "{FAMILY_CLAUDE}" or "{FAMILY_CODEX}"'
            )
    return thinkers


def missing_clis() -> list[str]:
    """Return the CLI binaries the live loop needs that are NOT on PATH.

    Pure :func:`shutil.which` lookups — spawns nothing. The CLI ``run`` command
    calls this to turn a missing binary into an actionable "install + log in to
    the claude and codex CLIs" hint instead of a raw stack trace at run time.
    Returns an empty list when both binaries are present.
    """
    missing: list[str] = []
    for binary in (CLAUDE_BIN, CODEX_BIN):
        if shutil.which(binary) is None:
            missing.append(binary)
    return missing


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fold_system(system: str, prompt: str) -> str:
    """Fold the system prompt into the one-shot prompt text.

    Neither ``claude -p`` nor ``codex exec`` has a separate system channel, so
    the engine's system instruction is prepended to the user prompt with a blank
    line between them. An empty system is dropped so the prompt is unchanged.
    """
    if system:
        return f"{system}\n\n{prompt}"
    return prompt


def _run(
    runner: Runner,
    argv: list[str],
    *,
    cwd: str,
    timeout: int,
    family: str,
) -> "subprocess.CompletedProcess[str]":
    """Invoke the runner with the standard kwargs and map a timeout to a clear error."""
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "cwd": cwd,
        "check": False,
    }
    try:
        return runner(argv, kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{family} CLI timed out after {timeout}s"
        ) from exc


def _check_returncode(
    proc: "subprocess.CompletedProcess[str]", *, family: str
) -> None:
    """Raise a clear, family-tagged error on a non-zero exit, with a stderr tail."""
    if getattr(proc, "returncode", 0) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        tail = stderr[-500:] if stderr else "(no stderr)"
        raise RuntimeError(
            f"{family} CLI exited with code {proc.returncode}: {tail}"
        )


def _find_result_element(events: Any) -> dict[str, Any] | None:
    """Find the ``type == "result"`` element in the claude JSON event array."""
    if not isinstance(events, list):
        return None
    for el in events:
        if isinstance(el, dict) and el.get("type") == "result":
            return el
    return None


def _claude_usage_tokens(usage: Any) -> int:
    """Sum input + output tokens from a claude result element's usage block.

    Defensive: the usage block is optional and its exact shape can vary, so a
    missing or malformed block contributes zero rather than raising.
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key, 0)
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


__all__ = [
    "Runner",
    "ClaudeCliThinker",
    "CodexCliThinker",
    "ScriptedThinker",
    "StubThinker",
    "build_role_thinkers",
    "missing_clis",
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_CODEX_MODEL",
    "FAMILY_CLAUDE",
    "FAMILY_CODEX",
    "DEFAULT_TIMEOUT_SECONDS",
]
