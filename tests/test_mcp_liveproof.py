"""Live, end-to-end MCP integrity proof over the REAL stdio transport.

This is the strongest form of the "an agent can autonomously drive Sparkle
through MCP and the seam enforces every debate-integrity invariant" claim: it
spawns a FRESH ``sparkle mcp`` server as a subprocess, connects to it as a real
MCP client over stdio, and drives a full debate while asserting each invariant
fires through the actual transport + server + ops seam (not an in-process
shortcut). It complements the deterministic in-process closure tests in
``test_runs.py`` (McpRunClosureTests) and ``test_mcp_server.py``.

OPT-IN: it spawns a subprocess and is slower than a unit test, so it is skipped
unless ``SPARKLE_LIVE_MCP=1`` is set AND the MCP client SDK is importable. Run it
with the venv that has the ``sparkle[mcp]`` extra:

    SPARKLE_LIVE_MCP=1 .venv/bin/python -m unittest tests.test_mcp_liveproof -v

The ``sparkle`` server binary is resolved next to the running interpreter, so
running under ``.venv/bin/python`` uses the venv's ``sparkle`` (which has the
extra). The server runs against a throwaway temp store via ``SPARKLE_GRAPH``.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest

_LIVE = os.environ.get("SPARKLE_LIVE_MCP") == "1"

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_CLIENT = True
except ImportError:  # pragma: no cover - depends on the mcp extra
    _MCP_CLIENT = False


def _text(result) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return " ".join(parts)


def _is_error(result) -> bool:
    return bool(getattr(result, "isError", False))


def _handle(text: str):
    m = re.search(r'"(?:node_id|handle)"\s*:\s*"([0-9a-f]{12,64})"', text)
    return m.group(1) if m else None


@unittest.skipUnless(_LIVE, "set SPARKLE_LIVE_MCP=1 to run the live stdio MCP proof")
@unittest.skipUnless(_MCP_CLIENT, "mcp client SDK not importable (needs the sparkle[mcp] extra)")
class LiveMcpProofTests(unittest.TestCase):
    """Drive a fresh sparkle MCP server over real stdio and assert every floor."""

    def test_every_integrity_invariant_holds_live_through_mcp(self) -> None:
        outcome = asyncio.run(self._run())
        for name, passed, detail in outcome:
            self.assertTrue(passed, f"{name} FAILED: {detail}")

    async def _call(self, session, name, **args):
        try:
            res = await session.call_tool(name, args)
            return (not _is_error(res)), _text(res)
        except Exception as exc:  # noqa: BLE001 - MCP client may raise on tool error
            return False, f"{type(exc).__name__}: {exc}"

    async def _run(self):
        store = os.path.join(tempfile.mkdtemp(prefix="sparkle_liveproof_"), "graph.json")
        sparkle_bin = os.path.join(os.path.dirname(sys.executable), "sparkle")
        params = StdioServerParameters(
            command=sparkle_bin,
            args=["mcp"],
            env={**os.environ, "SPARKLE_GRAPH": store},
        )
        RUN = "liveproof"
        checks = []

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                checks.append(("server connected + tools listed", bool(tools), f"{len(tools)} tools"))

                # 1. cannot ratify an unchallenged claim
                _, t = await self._call(session, "sparkle_add_node", node_type="claim",
                                        title="A", content="unchallenged", run_id=RUN, agent_role="proposer")
                a = _handle(t)
                ok, msg = await self._call(session, "sparkle_rule", claim_ref=a,
                                           verdict="upheld", settle=True, run_id=RUN)
                checks.append(("pre-challenge ratify refused", (not ok) and "never attacked" in msg, msg[:140]))

                # 2. self-strawman (same-author objection) cannot ratify
                _, t = await self._call(session, "sparkle_add_node", node_type="claim",
                                        title="B", content="self-strawman target", run_id=RUN, agent_role="proposer")
                b = _handle(t)
                await self._call(session, "sparkle_branch", from_ref=b, template="objection",
                                 title="mine", content="self objection", run_id=RUN, agent_role="proposer")
                ok, msg = await self._call(session, "sparkle_rule", claim_ref=b,
                                           verdict="upheld", settle=True, run_id=RUN)
                checks.append(("self-strawman ratify refused", (not ok) and "own author" in msg, msg[:140]))

                # 3. verdict-rejection floor: cross-author but non-affirming verdict cannot ratify
                _, t = await self._call(session, "sparkle_add_node", node_type="claim",
                                        title="C", content="rejected-verdict target", run_id=RUN, agent_role="proposer")
                c = _handle(t)
                await self._call(session, "sparkle_branch", from_ref=c, template="objection",
                                 title="critic", content="real objection", run_id=RUN, agent_role="critic")
                ok, msg = await self._call(session, "sparkle_rule", claim_ref=c,
                                           verdict="refuted", settle=True, run_id=RUN)
                checks.append(("verdict-rejection floor: refuted+settle refused", (not ok) and "does not affirm" in msg, msg[:140]))

                # 4. cross-author + settle=False -> honest 'judged', not 'ratified'
                _, t = await self._call(session, "sparkle_add_node", node_type="claim",
                                        title="D", content="judged target", run_id=RUN, agent_role="proposer")
                d = _handle(t)
                await self._call(session, "sparkle_branch", from_ref=d, template="objection",
                                 title="critic", content="real objection", run_id=RUN, agent_role="critic")
                ok, _ = await self._call(session, "sparkle_rule", claim_ref=d,
                                         verdict="overstated", settle=False, run_id=RUN)
                _, sig = await self._call(session, "sparkle_signal", ref=d)
                honest = "judged" in sig and '"live_signal":"ratified"' not in sig.replace(" ", "")
                checks.append(("ruling-not-settled reads 'judged' not 'ratified'", ok and honest, sig[:160]))

                # 5. full legit debate with run-region completeness: cross-author, evidence,
                #    upheld+settle, harvest -> the contradicts/supports/evaluates/produced edges
                #    all land in the run region (the edge-stamping fixes).
                _, t = await self._call(session, "sparkle_add_node", node_type="claim",
                                        title="E", content="ratify target", run_id=RUN, agent_role="proposer")
                e = _handle(t)
                await self._call(session, "sparkle_branch", from_ref=e, template="objection",
                                 title="critic", content="real objection", run_id=RUN, agent_role="critic")
                await self._call(session, "sparkle_add_node", node_type="evidence",
                                 title="ev", content="supporting datum", run_id=RUN, agent_role="evidence_gatherer",
                                 link_to=e, relation="supports")
                ok5, msg5 = await self._call(session, "sparkle_rule", claim_ref=e,
                                             verdict="upheld", settle=True, run_id=RUN)
                checks.append(("cross-author + upheld settles to ratified", ok5 and "decision" in msg5, msg5[:120]))
                await self._call(session, "sparkle_harvest", from_ref=e,
                                 title="synth", content="the takeaway", run_id=RUN)
                _, diff = await self._call(session, "sparkle_run_diff", run_id=RUN)
                rels = set(re.findall(r'"relation"\s*:\s*"([a-z_]+)"', diff))
                # supports (evidence) and produced (harvest) are the edges the fixes added to the region
                checks.append(("run region holds the evidence + harvest edges",
                               {"supports", "produced", "contradicts"}.issubset(rels),
                               f"edge relations in region: {sorted(rels)}"))

        return checks


if __name__ == "__main__":
    unittest.main()
