"""CLI-backed thinker coverage — argv construction and output parsing, no live calls.

Plain English: this proves the two real model backends build the exact command
lines we verified by hand and read their answers back correctly, WITHOUT ever
launching the real ``claude`` or ``codex`` tools. The Claude backend rides
Albert's Claude Max login via ``claude -p`` and the codex backend rides the
Codex/ChatGPT login via ``codex exec``; both could cost money and touch the repo
if invoked for real, so every test here injects a FAKE runner — a plain Python
callable that records the command line it was handed and returns canned output
instead of spawning a process. The stub thinker and the per-role factory are
exercised the same key-free way.

What we lock down:

- The Claude backend's command line names Opus 4.8 (``--model claude-opus-4-8``),
  asks for JSON output (``--output-format json``), and locks the model down
  (``--permission-mode default`` so an inherited ``dontAsk`` cannot auto-run a
  tool, ``--strict-mcp-config`` so no MCP servers load, and ``--disallowedTools``
  denying every execute/mutate/network tool) and runs in a throwaway working
  directory so it cannot read this repo; it parses the JSON event array stdout,
  pulls the answer out of the ``result`` element, accumulates token usage, and
  raises a clear error on an error-flagged result, a non-zero exit, a timeout, or
  malformed/empty output.
- The codex backend's command line is ``codex exec`` with the model
  (``-m gpt-5.5``), the read-only sandbox, the skip-git-repo-check flag, an
  isolated working dir (``-C <tempdir>``) and an answer file
  (``--output-last-message <file>``); it reads the final answer from that file
  (not stdout) and errors cleanly when the file is missing/empty or the process
  fails.
- Both backends pass a per-call subprocess timeout through to the runner.

This file is part of the ZERO-DEPENDENCY BASE SUITE: it imports NO third-party
package, spawns NO process, and makes NO live CLI call. Conventions match
tests/test_harness.py and tests/test_cli.py: pure-stdlib unittest, isolated temp
state, no side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from src.sparkle import thinker
from src.sparkle.thinker import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    ClaudeCliThinker,
    CodexCliThinker,
    ScriptedThinker,
    StubThinker,
    build_role_thinkers,
    missing_clis,
)


# ---------------------------------------------------------------------------
# Fake runners — drop-in replacements for the real subprocess.run wrapper.
# Each has the runner signature ``(argv, kwargs) -> CompletedProcess`` so it is
# a true seam: no process is ever spawned. They record what they were handed so
# the argv shape and the standard kwargs (cwd / timeout / capture / text /
# check) can be asserted.
# ---------------------------------------------------------------------------


class RecordingRunner:
    """A fake runner that records the last argv + kwargs and returns canned output.

    ``stdout`` / ``returncode`` / ``stderr`` are the canned process result the
    Claude backend will parse. Stores every call so a test can assert the runner
    was (or was not) invoked and inspect the exact command line.
    """

    def __init__(self, *, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, kwargs):  # noqa: ANN001
        self.calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )

    @property
    def last_argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1][1]


class CodexFileRunner:
    """A fake runner that mimics codex by WRITING the answer file the CLI would.

    The codex backend reads its answer from the ``--output-last-message`` file,
    not from stdout, so an honest fake must create that file. This runner finds
    the file path in the argv (the token after ``--output-last-message``) and,
    when ``write_answer`` is set, writes it; when ``write_answer is None`` it
    leaves the file absent so the missing-file error path can be exercised.
    """

    def __init__(self, *, write_answer: str | None = "", returncode: int = 0, stderr: str = "") -> None:
        self.write_answer = write_answer
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, kwargs):  # noqa: ANN001
        self.calls.append((list(argv), dict(kwargs)))
        if self.write_answer is not None and "--output-last-message" in argv:
            path = argv[argv.index("--output-last-message") + 1]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.write_answer)
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout="", stderr=self.stderr
        )

    @property
    def last_argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1][1]


def _timeout_runner(argv, kwargs):  # noqa: ANN001
    """A fake runner that always raises the subprocess timeout the real run would."""
    raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))


def _claude_result_payload(text: str, *, is_error: bool = False, usage: dict | None = None) -> str:
    """Render the single-JSON-array stdout the ``claude -p`` CLI emits.

    The array carries a couple of non-result events plus the one ``result``
    element the backend looks for, mirroring the real event stream shape.
    """
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": text}},
        {"type": "result", "is_error": is_error, "result": text},
    ]
    if usage is not None:
        events[-1]["usage"] = usage
    return json.dumps(events)


def _arg_after(argv: list[str], flag: str) -> str:
    """Return the token immediately following ``flag`` in ``argv``."""
    return argv[argv.index(flag) + 1]


# ---------------------------------------------------------------------------
# Claude backend — argv construction
# ---------------------------------------------------------------------------


class ClaudeArgvTests(unittest.TestCase):
    """The ``claude -p`` command line shape: model, JSON output, tool lockdown,
    isolated cwd, folded system prompt, and the per-call timeout."""

    def test_argv_names_opus_and_json_output(self):
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="claim it")

        argv = runner.last_argv
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[1], "-p")
        # Locked Claude-side model is Opus 4.8 on every role.
        self.assertEqual(_arg_after(argv, "--model"), DEFAULT_CLAUDE_MODEL)
        self.assertEqual(DEFAULT_CLAUDE_MODEL, "claude-opus-4-8")
        # JSON output so stdout is the parseable event array.
        self.assertEqual(_arg_after(argv, "--output-format"), "json")

    def test_argv_locks_down_permissions_mcp_and_tools(self):
        # The model may reason and answer but is structurally prevented from
        # acting: it runs with NO tools at all (deny-ALL via `--tools ""`, not a
        # deny-list), permission mode forced to 'default' (so the user's
        # 'dontAsk' auto-approve cannot run anything headless), NO MCP servers,
        # and only user-level settings (so a project/local allow-rule cannot
        # re-open a tool). A wrong/missing flag here is a manual-acceptance risk
        # (tests never spawn the real CLI), so we pin it explicitly.
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

        argv = runner.last_argv
        self.assertEqual(_arg_after(argv, "--permission-mode"), "default")
        self.assertIn("--strict-mcp-config", argv)
        # Deny-ALL: `--tools ""` disables every built-in tool (verified live
        # against Claude Code 2.1.157 to yield an empty tools array).
        self.assertEqual(_arg_after(argv, "--tools"), "")
        # Only user-level settings load — no project/local allow-rules.
        self.assertEqual(_arg_after(argv, "--setting-sources"), "user")
        # LSP is the one tool that survives `--tools ""`, so it is denied
        # explicitly to reach a true zero-tool session.
        self.assertIn("--disallowedTools", argv)
        self.assertIn("LSP", argv)
        # The old deny-list form (and the even older empty --allowedTools form)
        # must be gone: no execute/mutate tool names are listed, and no
        # --allowedTools.
        self.assertNotIn("--allowedTools", argv)
        for tool in ("Bash", "Write", "Edit", "WebFetch", "Task"):
            self.assertNotIn(tool, argv)
        # --disallowedTools is variadic, so it must be the final flag (it
        # consumes every trailing token).
        self.assertEqual(argv.index("--disallowedTools"), len(argv) - 2)

    def test_argv_honors_a_custom_model(self):
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker("claude-some-other", runner=runner).think(
            role="judge", system="", prompt="p"
        )
        self.assertEqual(_arg_after(runner.last_argv, "--model"), "claude-some-other")

    def test_runs_in_isolated_tempdir_cwd(self):
        # The CLI runs in a throwaway working directory (not this repo), and that
        # directory is cleaned up after the call.
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

        cwd = runner.last_kwargs["cwd"]
        self.assertIsInstance(cwd, str)
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(os.getcwd()))
        # Cleaned in the finally block — it must not survive the call.
        self.assertFalse(os.path.exists(cwd))

    def test_system_prompt_is_folded_into_the_prompt(self):
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner).think(
            role="proposer", system="SYS RULES", prompt="USER ASK"
        )
        # -p one-shot has no separate system channel; system is prepended.
        folded = runner.last_argv[2]
        self.assertIn("SYS RULES", folded)
        self.assertIn("USER ASK", folded)
        self.assertLess(folded.index("SYS RULES"), folded.index("USER ASK"))

    def test_empty_system_leaves_prompt_unchanged(self):
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="just this")
        self.assertEqual(runner.last_argv[2], "just this")

    def test_passes_per_call_timeout_to_runner(self):
        runner = RecordingRunner(stdout=_claude_result_payload("hi"))
        ClaudeCliThinker(runner=runner, timeout=42).think(role="proposer", system="", prompt="p")
        self.assertEqual(runner.last_kwargs["timeout"], 42)
        # Standard non-interactive capture kwargs the parser relies on.
        self.assertTrue(runner.last_kwargs["capture_output"])
        self.assertTrue(runner.last_kwargs["text"])
        self.assertFalse(runner.last_kwargs["check"])


# ---------------------------------------------------------------------------
# Claude backend — stdout parsing
# ---------------------------------------------------------------------------


class ClaudeParseTests(unittest.TestCase):
    """Parsing the JSON event array: pull the result text, accumulate usage, and
    raise on every failure shape (error result, non-zero exit, timeout, empty,
    malformed)."""

    def test_returns_result_element_text(self):
        runner = RecordingRunner(stdout=_claude_result_payload("the answer"))
        out = ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")
        self.assertEqual(out, "the answer")

    def test_accumulates_token_usage(self):
        runner = RecordingRunner(
            stdout=_claude_result_payload("a", usage={"input_tokens": 10, "output_tokens": 7})
        )
        t = ClaudeCliThinker(runner=runner)
        self.assertEqual(t.tokens_used, 0)
        t.think(role="proposer", system="", prompt="p")
        self.assertEqual(t.tokens_used, 17)
        # Token attr is the exact name the engine's budget reader uses
        # (harness._thinker_tokens reads `tokens_used`); a wrong name = dead budget.
        self.assertTrue(hasattr(t, "tokens_used"))

    def test_raises_on_is_error_result(self):
        runner = RecordingRunner(stdout=_claude_result_payload("boom", is_error=True))
        with self.assertRaises(RuntimeError):
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

    def test_raises_on_non_zero_exit(self):
        runner = RecordingRunner(stdout="", returncode=1, stderr="kaboom on PATH")
        with self.assertRaises(RuntimeError) as ctx:
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")
        # The stderr tail is surfaced so the failed move is debuggable.
        self.assertIn("kaboom on PATH", str(ctx.exception))

    def test_raises_on_empty_stdout(self):
        runner = RecordingRunner(stdout="   ", returncode=0)
        with self.assertRaises(RuntimeError):
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

    def test_raises_on_malformed_json(self):
        runner = RecordingRunner(stdout="not json at all {", returncode=0)
        with self.assertRaises(RuntimeError):
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

    def test_raises_when_no_result_element(self):
        # A well-formed array with no result element is still a failed move.
        runner = RecordingRunner(stdout=json.dumps([{"type": "system"}]), returncode=0)
        with self.assertRaises(RuntimeError):
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")

    def test_raises_on_timeout(self):
        with self.assertRaises(RuntimeError) as ctx:
            ClaudeCliThinker(runner=_timeout_runner, timeout=5).think(
                role="proposer", system="", prompt="p"
            )
        self.assertIn("timed out", str(ctx.exception))
        self.assertIn("claude", str(ctx.exception))

    def test_cleans_tempdir_even_on_failure(self):
        # The isolated working dir must not leak when the run fails.
        captured = {}

        def runner(argv, kwargs):  # noqa: ANN001
            captured["cwd"] = kwargs["cwd"]
            return subprocess.CompletedProcess(args=argv, returncode=2, stdout="", stderr="x")

        with self.assertRaises(RuntimeError):
            ClaudeCliThinker(runner=runner).think(role="proposer", system="", prompt="p")
        self.assertFalse(os.path.exists(captured["cwd"]))


# ---------------------------------------------------------------------------
# Codex backend — argv construction
# ---------------------------------------------------------------------------


class CodexArgvTests(unittest.TestCase):
    """The ``codex exec`` command line shape: subcommand, model, read-only
    sandbox, skip-git-repo-check, isolated cwd, output-file, optional reasoning
    effort, folded system, and the per-call timeout."""

    def test_argv_is_codex_exec_with_model(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner).think(role="critic", system="", prompt="object")

        argv = runner.last_argv
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1], "exec")
        self.assertEqual(_arg_after(argv, "-m"), DEFAULT_CODEX_MODEL)
        self.assertEqual(DEFAULT_CODEX_MODEL, "gpt-5.5")

    def test_argv_has_readonly_sandbox_and_skip_git_check(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")

        argv = runner.last_argv
        self.assertEqual(_arg_after(argv, "--sandbox"), "read-only")
        self.assertIn("--skip-git-repo-check", argv)

    def test_argv_uses_isolated_cwd_and_output_file_in_that_dir(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")

        argv = runner.last_argv
        workdir = _arg_after(argv, "-C")
        answer_file = _arg_after(argv, "--output-last-message")
        # Isolated working dir, not this repo, passed both as -C and as the
        # subprocess cwd; the answer file lives INSIDE that dir.
        self.assertNotEqual(os.path.realpath(workdir), os.path.realpath(os.getcwd()))
        self.assertEqual(os.path.realpath(runner.last_kwargs["cwd"]), os.path.realpath(workdir))
        self.assertEqual(
            os.path.realpath(os.path.dirname(answer_file)), os.path.realpath(workdir)
        )
        # Cleaned after the call — neither dir nor file may survive.
        self.assertFalse(os.path.exists(workdir))
        self.assertFalse(os.path.exists(answer_file))

    def test_no_reasoning_effort_by_default(self):
        # Default None leaves the user's codex config in charge — no -c override.
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")
        self.assertNotIn("model_reasoning_effort=", " ".join(runner.last_argv))

    def test_reasoning_effort_override_is_appended(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner, reasoning_effort="high").think(
            role="critic", system="", prompt="p"
        )
        argv = runner.last_argv
        self.assertIn("-c", argv)
        self.assertEqual(_arg_after(argv, "-c"), "model_reasoning_effort=high")

    def test_argv_honors_custom_model(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker("gpt-other", runner=runner).think(role="critic", system="", prompt="p")
        self.assertEqual(_arg_after(runner.last_argv, "-m"), "gpt-other")

    def test_system_prompt_is_folded_into_the_prompt(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner).think(role="critic", system="SYS", prompt="ASK")
        folded = runner.last_argv[2]
        self.assertIn("SYS", folded)
        self.assertIn("ASK", folded)
        self.assertLess(folded.index("SYS"), folded.index("ASK"))

    def test_passes_per_call_timeout_to_runner(self):
        runner = CodexFileRunner(write_answer="ok")
        CodexCliThinker(runner=runner, timeout=33).think(role="critic", system="", prompt="p")
        self.assertEqual(runner.last_kwargs["timeout"], 33)


# ---------------------------------------------------------------------------
# Codex backend — answer-file reading + failure paths
# ---------------------------------------------------------------------------


class CodexReadTests(unittest.TestCase):
    """Reading the final answer from the ``--output-last-message`` file (NOT
    stdout), and erroring cleanly on a failed run, a missing file, or an empty
    file."""

    def test_reads_answer_from_output_file(self):
        runner = CodexFileRunner(write_answer="the final answer")
        out = CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")
        self.assertEqual(out, "the final answer")

    def test_ignores_stdout_for_the_answer(self):
        # Even if stdout carried noise, the answer comes from the file.
        class NoisyRunner(CodexFileRunner):
            def __call__(self, argv, kwargs):  # noqa: ANN001
                self.calls.append((list(argv), dict(kwargs)))
                path = argv[argv.index("--output-last-message") + 1]
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("FILE ANSWER")
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="STDOUT NOISE", stderr=""
                )

        out = CodexCliThinker(runner=NoisyRunner()).think(role="critic", system="", prompt="p")
        self.assertEqual(out, "FILE ANSWER")

    def test_raises_on_non_zero_exit(self):
        runner = CodexFileRunner(write_answer="x", returncode=1, stderr="codex blew up")
        with self.assertRaises(RuntimeError) as ctx:
            CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")
        self.assertIn("codex blew up", str(ctx.exception))

    def test_raises_when_answer_file_missing(self):
        # write_answer=None -> the fake leaves no file, as a failed codex run would.
        runner = CodexFileRunner(write_answer=None, returncode=0)
        with self.assertRaises(RuntimeError):
            CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")

    def test_raises_on_empty_answer_file(self):
        runner = CodexFileRunner(write_answer="   ", returncode=0)
        with self.assertRaises(RuntimeError):
            CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")

    def test_raises_on_timeout(self):
        with self.assertRaises(RuntimeError) as ctx:
            CodexCliThinker(runner=_timeout_runner, timeout=7).think(
                role="critic", system="", prompt="p"
            )
        self.assertIn("timed out", str(ctx.exception))
        self.assertIn("codex", str(ctx.exception))

    def test_cleans_tempdir_even_on_failure(self):
        captured = {}

        def runner(argv, kwargs):  # noqa: ANN001
            captured["cwd"] = kwargs["cwd"]
            return subprocess.CompletedProcess(args=argv, returncode=3, stdout="", stderr="x")

        with self.assertRaises(RuntimeError):
            CodexCliThinker(runner=runner).think(role="critic", system="", prompt="p")
        self.assertFalse(os.path.exists(captured["cwd"]))


# ---------------------------------------------------------------------------
# Stub thinker — deterministic, network-free, no process
# ---------------------------------------------------------------------------


class StubThinkerTests(unittest.TestCase):
    """The bundled key-free stub: scripted-by-role text, zero tokens, no spawn."""

    def test_returns_scripted_text_by_role(self):
        stub = ScriptedThinker({"proposer": "P-TEXT", "critic": "C-TEXT"})
        self.assertEqual(stub.think(role="proposer", system="", prompt="x"), "P-TEXT")
        self.assertEqual(stub.think(role="critic", system="", prompt="x"), "C-TEXT")

    def test_falls_back_to_default_for_unscripted_role(self):
        stub = ScriptedThinker({"proposer": "P"}, default="DEF")
        self.assertEqual(stub.think(role="judge", system="", prompt="x"), "DEF")

    def test_reports_zero_tokens(self):
        self.assertEqual(ScriptedThinker().tokens_used, 0)

    def test_stub_alias_is_scripted_thinker(self):
        self.assertIs(StubThinker, ScriptedThinker)


# ---------------------------------------------------------------------------
# Factory — builds one CLI thinker per role from the config's backend map
# ---------------------------------------------------------------------------


class _FakeConfig:
    """A minimal stand-in for HarnessConfig exposing only what the factory reads.

    Avoids importing the full harness config so this file stays focused on the
    thinker surface; the real HarnessConfig wiring is covered in
    tests/test_family_gate.py.
    """

    def __init__(self, backends: dict[str, str], models: dict[str, str], reasoning_effort=None):
        self._backends = backends
        self._models = models
        self.reasoning_effort = reasoning_effort

    def backend_for_role(self, role: str) -> str:
        return self._backends[role]

    def model_for_role(self, role: str) -> str:
        return self._models[role]


_LOCKED_BACKENDS = {
    "proposer": "claude",
    "critic": "codex",
    "judge": "claude",
    "evidence_gatherer": "claude",
    "synthesizer": "claude",
}
_LOCKED_MODELS = {
    "proposer": "claude-opus-4-8",
    "critic": "gpt-5.5",
    "judge": "claude-opus-4-8",
    "evidence_gatherer": "claude-opus-4-8",
    "synthesizer": "claude-opus-4-8",
}


class FactoryTests(unittest.TestCase):
    """The per-role factory maps the locked backend map to the right CLI thinker
    classes, gives each role its own instance, and threads the codex reasoning
    effort through; it rejects an unknown family. No CLI is spawned (no think())."""

    def test_builds_claude_for_claude_roles_and_codex_for_critic(self):
        cfg = _FakeConfig(_LOCKED_BACKENDS, _LOCKED_MODELS)
        thinkers = build_role_thinkers(cfg)

        self.assertIsInstance(thinkers["proposer"], ClaudeCliThinker)
        self.assertIsInstance(thinkers["judge"], ClaudeCliThinker)
        self.assertIsInstance(thinkers["evidence_gatherer"], ClaudeCliThinker)
        self.assertIsInstance(thinkers["synthesizer"], ClaudeCliThinker)
        # The adversary is the cross-family critic.
        self.assertIsInstance(thinkers["critic"], CodexCliThinker)

    def test_each_role_carries_its_configured_model(self):
        cfg = _FakeConfig(_LOCKED_BACKENDS, _LOCKED_MODELS)
        thinkers = build_role_thinkers(cfg)
        self.assertEqual(thinkers["proposer"].model, "claude-opus-4-8")
        self.assertEqual(thinkers["critic"].model, "gpt-5.5")

    def test_each_role_gets_its_own_instance(self):
        # Two claude roles share a backend + model but must be distinct objects so
        # each keeps a separate token tally and a distinct author downstream.
        cfg = _FakeConfig(_LOCKED_BACKENDS, _LOCKED_MODELS)
        thinkers = build_role_thinkers(cfg)
        self.assertIsNot(thinkers["proposer"], thinkers["judge"])
        self.assertIsNot(thinkers["proposer"], thinkers["synthesizer"])

    def test_threads_reasoning_effort_to_codex_critic(self):
        cfg = _FakeConfig(_LOCKED_BACKENDS, _LOCKED_MODELS, reasoning_effort="high")
        thinkers = build_role_thinkers(cfg)
        self.assertEqual(thinkers["critic"].reasoning_effort, "high")

    def test_rejects_unknown_backend_family(self):
        backends = dict(_LOCKED_BACKENDS)
        backends["proposer"] = "gemini"
        cfg = _FakeConfig(backends, _LOCKED_MODELS)
        with self.assertRaises(ValueError):
            build_role_thinkers(cfg)


# ---------------------------------------------------------------------------
# Module hygiene — no third-party imports, no live calls, helper purity
# ---------------------------------------------------------------------------


class ModuleHygieneTests(unittest.TestCase):
    """The whole point of the CLI rebuild: no SDK dependency, no spawn at import
    or construction, and the PATH helper spawns nothing."""

    def test_imports_no_third_party_sdk(self):
        # Importing the thinker module must not pull in anthropic or openai.
        self.assertNotIn("anthropic", sys.modules)
        self.assertNotIn("openai", sys.modules)

    def test_constructing_thinkers_spawns_nothing(self):
        # No runner is invoked until think() is called; construction is inert.
        called = {"n": 0}

        def tripwire(argv, kwargs):  # noqa: ANN001
            called["n"] += 1
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="[]", stderr="")

        ClaudeCliThinker(runner=tripwire)
        CodexCliThinker(runner=tripwire)
        self.assertEqual(called["n"], 0)

    def test_missing_clis_spawns_nothing_and_returns_a_list(self):
        # Pure shutil.which lookups; result depends on the host PATH but the shape
        # is always a list whose entries are a subset of {claude, codex}.
        result = missing_clis()
        self.assertIsInstance(result, list)
        self.assertTrue(set(result).issubset({"claude", "codex"}))


if __name__ == "__main__":
    unittest.main()
