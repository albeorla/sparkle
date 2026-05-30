"""Behaviour tests for the Sparkle MCP layer (``src/sparkle/mcp_server.py``).

What this proves (not just registration):
  * ``build_app()`` returns a FastMCP named "sparkle" exposing the full surface:
    16 tools, 4 resources, 5 resource templates, 4 prompts.
  * A representative slice of tool *callables* drives a real debate against a
    temp graph end-to-end: add a claim -> attack it (branch/link) -> read the
    live referee signal -> rule. The SAME hard invariant that lives in
    ``ops.rule`` (you may not ratify a claim the adversary never attacked) holds
    THROUGH the tool layer: the refused ruling surfaces as a tool error, and the
    legal ruling settles to a ``ratified`` version.
  * A resource read returns sane, parseable content.

Zero-dependency contract: this file guards its import. The MCP SDK is a
``sparkle[mcp]`` extra, NOT part of the stdlib-pure core. If the SDK is absent,
the whole module is SKIPPED (``raise unittest.SkipTest``) so the base
``python3 -m unittest discover -s tests`` run stays green with nothing installed.
To actually exercise this layer, install the extra in a throwaway venv and run
the suite from there.

Conventions match ``tests/test_cli.py``: stdlib ``unittest``, each test isolates
its graph under a ``tempfile.TemporaryDirectory`` (no shared ``.sparkle`` state),
and handles are read off a ``"handle"`` field rather than ``split()[-1]``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

# --- zero-dependency guard -------------------------------------------------
# The base run has no third-party packages; degrade to a clean SKIP instead of
# erroring so `discover` stays green. Importing mcp_server pulls in the MCP SDK
# at its module top (`from mcp.server.fastmcp import ...`).
try:  # pragma: no cover - exercised only under the venv run
    from mcp.server.fastmcp import FastMCP  # noqa: F401

    from src.sparkle import mcp_server, ops
    from src.sparkle.graph import GraphStore
except ImportError as exc:  # pragma: no cover - the base zero-dep path
    raise unittest.SkipTest(
        f"sparkle[mcp] not installed ({exc}); "
        "run `pip install -e '.[mcp]'` in a venv to exercise the MCP layer"
    )


def _run(coro):
    """Drive one coroutine to completion on a fresh event loop (stdlib only)."""
    return asyncio.run(coro)


def _structured(call_result):
    """Pull the structured dict/list out of a FastMCP ``call_tool`` result.

    FastMCP versions differ: some return ``(content_blocks, structured)``;
    older ones return just ``[content_block, ...]``. Handle both so the test is
    a behaviour test, not a version test. Falls back to JSON-parsing the first
    text content block when no structured payload is provided.
    """
    if isinstance(call_result, tuple):
        content, structured = call_result
        if structured is not None:
            return structured
        return json.loads(content[0].text)
    # bare list of content blocks
    return json.loads(call_result[0].text)


def _resource_text(read_result) -> str:
    """Concatenate the text payload of a FastMCP ``read_resource`` result."""
    return "".join(item.content for item in read_result)


class McpServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "graph.json"
        self.store = GraphStore(self.store_path)
        self.store.init()
        # The server reads a process-wide store handle that run_server() sets;
        # inject our temp store so build_app()'s tools write into it. We save and
        # restore the module global so tests never leak a store between cases.
        self._saved_store = mcp_server._STORE
        mcp_server._STORE = self.store
        self.app = mcp_server.build_app()

    def tearDown(self) -> None:
        mcp_server._STORE = self._saved_store
        self.temp_dir.cleanup()

    # -- tool/resource/prompt invocation helpers ---------------------------

    def call_tool(self, name: str, args: dict) -> object:
        """Invoke a registered tool through the MCP layer and return its payload."""
        return _structured(_run(self.app.call_tool(name, args)))

    def read_resource(self, uri: str) -> str:
        return _resource_text(_run(self.app.read_resource(uri)))

    # ----------------------------------------------------------------------
    # Surface registration: the exact counts the MVP promises.
    # ----------------------------------------------------------------------

    def test_build_app_returns_named_fastmcp(self) -> None:
        self.assertIsInstance(self.app, FastMCP)
        self.assertEqual(self.app.name, "sparkle")

    def test_surface_counts_16_tools_4_resources_5_templates_4_prompts(self) -> None:
        tools = _run(self.app.list_tools())
        resources = _run(self.app.list_resources())
        templates = _run(self.app.list_resource_templates())
        prompts = _run(self.app.list_prompts())

        self.assertEqual(len(tools), 16, [t.name for t in tools])
        self.assertEqual(len(resources), 4, [str(r.uri) for r in resources])
        self.assertEqual(len(templates), 5, [t.uriTemplate for t in templates])
        self.assertEqual(len(prompts), 4, [p.name for p in prompts])

    def test_every_promised_tool_is_registered_by_name(self) -> None:
        names = {t.name for t in _run(self.app.list_tools())}
        expected = {
            "sparkle_add_node",
            "sparkle_link",
            "sparkle_branch",
            "sparkle_rule",
            "sparkle_harvest",
            "sparkle_frontier",
            "sparkle_node",
            "sparkle_lineage",
            "sparkle_subgraph",
            "sparkle_relations",
            "sparkle_templates",
            "sparkle_signal",
            "sparkle_run_summary",
            "sparkle_run_diff",
            "sparkle_ratify_region",
            "sparkle_rollback_run",
        }
        self.assertEqual(names, expected)

    def test_resources_prompts_templates_registered_by_name(self) -> None:
        res_uris = {str(r.uri) for r in _run(self.app.list_resources())}
        self.assertEqual(
            res_uris,
            {
                "sparkle://frontier",
                "sparkle://relations",
                "sparkle://templates",
                "sparkle://playbook",
            },
        )
        tmpl_uris = {t.uriTemplate for t in _run(self.app.list_resource_templates())}
        self.assertEqual(
            tmpl_uris,
            {
                "sparkle://node/{ref}",
                "sparkle://lineage/{ref}",
                "sparkle://subgraph/{ref}",
                "sparkle://run/{run_id}/summary",
                "sparkle://run/{run_id}/diff",
            },
        )
        prompt_names = {p.name for p in _run(self.app.list_prompts())}
        self.assertEqual(
            prompt_names, {"challenge", "investigate", "synthesize", "next_move"}
        )

    # ----------------------------------------------------------------------
    # The debate loop, driven THROUGH the tool layer (not ops directly).
    # propose -> attack -> signal -> rule, with the invariant intact.
    # ----------------------------------------------------------------------

    def test_invariant_holds_through_tool_layer_then_settles(self) -> None:
        """The full slice: add_node -> branch(attack) -> signal -> rule.

        The point is that the SAME guardrail in ops.rule holds when reached via
        the MCP tool: ratifying an unattacked claim is refused as a tool error;
        after a contradicts attack lands, ruling is legal and settles to a
        frozen ``ratified`` version.
        """
        run_id = "loop-1"

        # PROPOSE: write the claim under debate via the tool.
        added = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Background music improves coding focus",
                "content": "Lyric-free music reduces context-switch cost.",
                "run_id": run_id,
            },
        )
        self.assertTrue(added["created"])
        claim_handle = added["handle"]
        # Model-authored writes carry the provisional stamp and capped confidence.
        self.assertEqual(added["metadata"]["provisional"], True)
        self.assertEqual(added["metadata"]["run_id"], run_id)
        self.assertLessEqual(added["confidence"], 0.5)

        # REFUSED: ruling before any attack must be refused through the tool,
        # surfaced as a tool error that preserves the ops invariant message.
        with self.assertRaises(Exception) as ctx:
            self.call_tool(
                "sparkle_rule",
                {"claim_ref": claim_handle, "verdict": "accept", "run_id": run_id},
            )
        self.assertIn("never attacked", str(ctx.exception))

        # ATTACK: one structured debate move — an objection node linked
        # contradicts INTO the claim — via the branch tool.
        branched = self.call_tool(
            "sparkle_branch",
            {
                "from_ref": claim_handle,
                "template": "objection",
                "title": "Lyrics hijack the language network",
                "content": "Word-bearing music competes with reading code.",
                "run_id": run_id,
            },
        )
        self.assertTrue(branched["created"])
        self.assertEqual(branched["link"]["relation"], "contradicts")

        # SIGNAL: read the live referee state through the signal tool. The claim
        # is now challenged and ready to judge — a pure read, no write.
        signal = self.call_tool("sparkle_signal", {"ref": claim_handle})
        self.assertTrue(signal["challenged"])
        self.assertEqual(signal["contradicts"], 1)
        self.assertEqual(signal["supports"], 0)
        self.assertFalse(signal["has_decision"])

        # RULE: now legal. settle=True writes a superseding ratified version;
        # the original claim stays frozen-active.
        ruled = self.call_tool(
            "sparkle_rule",
            {
                "claim_ref": claim_handle,
                "verdict": "reject",
                "settle": True,
                "run_id": run_id,
            },
        )
        self.assertTrue(ruled["settled"])
        self.assertEqual(ruled["decision"]["node_type"], "decision")
        self.assertEqual(ruled["ratified_claim"]["status"], "ratified")
        self.assertNotEqual(ruled["ratified_claim"]["node_id"], added["node_id"])
        # Frozen-honest: the original claim node is untouched.
        self.assertEqual(self.store.get_node(added["node_id"])["status"], "active")

        # The signal now reflects the decision/ratified outcome.
        post = self.call_tool("sparkle_signal", {"ref": claim_handle})
        self.assertEqual(post["live_signal"], "ratified")

    def test_attack_via_low_level_link_tool_also_satisfies_invariant(self) -> None:
        """The other attack path: add an objection node + a contradicts link.

        Proves the invariant keys on the inbound edge, not on how it was made:
        the branch template and the manual link tool reach the same gate.
        """
        run_id = "loop-2"
        claim = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Pair programming raises code quality",
                "content": "Two reviewers catch defects earlier.",
                "run_id": run_id,
            },
        )
        objection = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "objection",
                "title": "Throughput halves",
                "content": "Two people on one task can cut output.",
                "run_id": run_id,
                "agent_role": "critic",
            },
        )
        linked = self.call_tool(
            "sparkle_link",
            {
                "from_ref": objection["handle"],
                "to_ref": claim["handle"],
                "relation": "contradicts",
                "run_id": run_id,
            },
        )
        self.assertEqual(linked["relation"], "contradicts")

        signal = self.call_tool("sparkle_signal", {"ref": claim["handle"]})
        self.assertTrue(signal["challenged"])

        # A decision-only ruling (settle=False) writes the decision but no
        # ratified version.
        ruled = self.call_tool(
            "sparkle_rule",
            {"claim_ref": claim["handle"], "verdict": "accept", "run_id": run_id},
        )
        self.assertFalse(ruled["settled"])
        self.assertNotIn("ratified_claim", ruled)
        self.assertEqual(ruled["decision"]["node_type"], "decision")

    def test_rule_tool_refuses_non_claim_target(self) -> None:
        """rule() targets a claim; ruling an objection is refused through the tool."""
        run_id = "loop-3"
        objection = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "objection",
                "title": "An objection, not a claim",
                "content": "Cannot be ruled on.",
                "run_id": run_id,
                "agent_role": "critic",
            },
        )
        with self.assertRaises(Exception) as ctx:
            self.call_tool(
                "sparkle_rule",
                {
                    "claim_ref": objection["handle"],
                    "verdict": "accept",
                    "run_id": run_id,
                },
            )
        self.assertIn("targets a claim", str(ctx.exception))

    def test_add_node_tool_blocks_model_authored_terminal_status(self) -> None:
        """The model-confidence/terminal-status guard fires through the tool.

        Tools pass model_authored=True, so ops.guard_authored_status refuses a
        model-written terminal status (e.g. 'ratified') unless it came from a
        settled ruling.
        """
        with self.assertRaises(Exception) as ctx:
            self.call_tool(
                "sparkle_add_node",
                {
                    "node_type": "claim",
                    "title": "Sneaky pre-ratified claim",
                    "content": "Trying to author ratified directly.",
                    "run_id": "loop-4",
                    "status": "ratified",
                },
            )
        self.assertIn("ratified", str(ctx.exception))

    def test_add_node_tool_dedups_identical_reproposal(self) -> None:
        """A re-proposal of the same idea collapses to the existing node id."""
        run_id = "loop-5"
        args = {
            "node_type": "claim",
            "title": "Dedup through the tool",
            "content": "Same idea proposed twice via MCP.",
            "run_id": run_id,
        }
        first = self.call_tool("sparkle_add_node", dict(args))
        self.assertTrue(first["created"])
        second = self.call_tool("sparkle_add_node", dict(args, agent_role="other"))
        self.assertFalse(second["created"])
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(len(self.store.list_nodes()), 1)

    def test_harvest_tool_produces_synthesis_linked_to_source(self) -> None:
        """The closing harvest move writes a synthesis --produced--> source node."""
        run_id = "loop-6"
        claim = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Frequent commits reduce merge pain",
                "content": "Small diffs are easier to integrate.",
                "run_id": run_id,
            },
        )
        synth = self.call_tool(
            "sparkle_harvest",
            {
                "from_ref": claim["handle"],
                "title": "Takeaway: commit small and often",
                "content": "Smaller integration steps keep the branch mergeable.",
                "run_id": run_id,
            },
        )
        self.assertEqual(synth["node_type"], "synthesis")
        self.assertEqual(synth["link"]["relation"], "produced")
        # Documented semantic: harvest writes status='active', not 'harvested'.
        self.assertEqual(synth["status"], "active")

    # ----------------------------------------------------------------------
    # Reads expose sane content (resource + tool mirror agree).
    # ----------------------------------------------------------------------

    def test_relations_resource_returns_sane_content(self) -> None:
        text = self.read_resource("sparkle://relations")
        data = json.loads(text)
        self.assertIsInstance(data, list)
        rels = {item["relation"] for item in data}
        # The debate-critical relations must be present in the legend.
        self.assertIn("contradicts", rels)
        self.assertIn("supports", rels)
        # The resource and its tool mirror carry the same legend. A tool that
        # returns a top-level JSON array gets its structured payload wrapped as
        # {"result": [...]} by the MCP SDK (structured output must be an object),
        # while the resource read returns the raw list; unwrap before comparing.
        tool_data = self.call_tool("sparkle_relations", {})
        if isinstance(tool_data, dict) and set(tool_data) == {"result"}:
            tool_data = tool_data["result"]
        self.assertEqual(tool_data, data)

    def test_templates_resource_lists_the_four_debate_moves(self) -> None:
        text = self.read_resource("sparkle://templates")
        data = json.loads(text)
        names = {item["name"] for item in data}
        self.assertEqual(names, {"application", "objection", "reframing", "support"})

    def test_node_resource_template_returns_node_with_neighbors(self) -> None:
        """The node/{ref} resource template resolves a ref and returns neighbors."""
        run_id = "loop-7"
        claim = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "A read target",
                "content": "Read me back via the node resource template.",
                "run_id": run_id,
            },
        )
        handle = claim["handle"]
        text = self.read_resource(f"sparkle://node/{handle}")
        data = json.loads(text)
        self.assertEqual(data["title"], "A read target")
        self.assertIn("inbound", data)
        self.assertIn("outbound", data)

    def test_frontier_resource_returns_paged_feed_shape(self) -> None:
        """The frontier resource returns the prioritized feed envelope."""
        run_id = "loop-8"
        self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "An unchallenged claim on the frontier",
                "content": "It should surface as needing attention.",
                "run_id": run_id,
            },
        )
        text = self.read_resource("sparkle://frontier")
        data = json.loads(text)
        for key in ("total", "returned", "counts_by_bucket", "entries"):
            self.assertIn(key, data)
        self.assertGreaterEqual(data["total"], 1)

    # ----------------------------------------------------------------------
    # Per-run review surface: summary / diff / ratify-region / rollback.
    # ----------------------------------------------------------------------

    def test_run_summary_and_diff_tools_report_the_run_region(self) -> None:
        run_id = "region-1"
        self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Region claim",
                "content": "Tagged to a run for review.",
                "run_id": run_id,
            },
        )
        summary = self.call_tool("sparkle_run_summary", {"run_id": run_id})
        self.assertEqual(summary["run_id"], run_id)
        self.assertEqual(summary["node_count"], 1)
        self.assertEqual(summary["nodes_by_type"].get("claim"), 1)

        diff = self.call_tool("sparkle_run_diff", {"run_id": run_id})
        self.assertEqual(len(diff["nodes"]), 1)
        self.assertEqual(diff["nodes"][0]["title"], "Region claim")

        # A different run id sees an empty region.
        empty = self.call_tool("sparkle_run_summary", {"run_id": "no-such-run"})
        self.assertEqual(empty["node_count"], 0)

    def test_branch_and_rule_writes_also_land_in_the_run_region(self) -> None:
        """Regression: ``sparkle_branch`` and ``sparkle_rule`` must forward run_id.

        Both wrappers take a *required* ``run_id`` but used to drop it on the
        floor, so the branched objection, the decision, the ratified
        supersession, and every edge between them fell OUTSIDE the run region --
        meaning ``run_summary`` / ``run_diff`` / ``rollback_run`` could not treat
        a debate as one reviewable, rollback-able unit. ``ops.add_branch`` and
        ``ops.rule`` already accept ``run_id``; the front-end just wasn't passing
        it. This proves the whole loop now lands in the region.
        """
        run_id = "region-branch-rule"
        claim = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Region claim under debate",
                "content": "Proposed, then attacked, then ruled -- all one run.",
                "run_id": run_id,
            },
        )
        self.call_tool(
            "sparkle_branch",
            {
                "from_ref": claim["handle"],
                "template": "objection",
                "title": "An attack on the claim",
                "content": "Here is what could make it false.",
                "run_id": run_id,
            },
        )
        self.call_tool(
            "sparkle_rule",
            {
                "claim_ref": claim["handle"],
                "verdict": "Holds despite the objection.",
                "settle": True,
                "run_id": run_id,
            },
        )

        summary = self.call_tool("sparkle_run_summary", {"run_id": run_id})
        # original claim + branched objection + decision + ratified supersession
        self.assertEqual(summary["node_count"], 4)
        self.assertEqual(summary["nodes_by_type"].get("claim"), 2)
        self.assertEqual(summary["nodes_by_type"].get("objection"), 1)
        self.assertEqual(summary["nodes_by_type"].get("decision"), 1)
        # branch contradicts + two evaluates edges + supersedes edge all stamped
        self.assertEqual(summary["edge_count"], 4)

    def test_ratify_region_clears_provisional_via_supersession(self) -> None:
        """Region sign-off supersedes each provisional node with the flag cleared.

        It does NOT set a terminal status (that path stays behind sparkle_rule),
        and it is model-honest: a NEW node carries the cleared stamp.
        """
        run_id = "region-2"
        added = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "Provisional draft",
                "content": "Written by the model, not yet accepted.",
                "run_id": run_id,
            },
        )
        self.assertTrue(added["metadata"]["provisional"])

        result = self.call_tool("sparkle_ratify_region", {"run_id": run_id})
        self.assertEqual(result["count"], 1)
        accepted = result["ratified"][0]
        self.assertFalse(accepted["metadata"]["provisional"])
        self.assertEqual(accepted["metadata"]["ratified_run"], run_id)
        # Status is not terminal — acceptance is a metadata flip, not a verdict.
        self.assertEqual(accepted["status"], "active")
        # Model-honest supersession: a new node carries the cleared stamp.
        self.assertNotEqual(accepted["node_id"], added["node_id"])

    def test_rollback_run_abandons_run_nodes_via_supersession(self) -> None:
        """Rollback supersedes each run node to status 'abandoned' (no delete)."""
        run_id = "region-3"
        added = self.call_tool(
            "sparkle_add_node",
            {
                "node_type": "claim",
                "title": "To be rolled back",
                "content": "This run will be abandoned.",
                "run_id": run_id,
            },
        )
        result = self.call_tool("sparkle_rollback_run", {"run_id": run_id})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rolled_back"][0]["status"], "abandoned")
        # The original node is untouched (frozen); a NEW abandoned node exists.
        self.assertEqual(self.store.get_node(added["node_id"])["status"], "active")
        statuses = {node["status"] for _, node in self.store.list_nodes()}
        self.assertIn("abandoned", statuses)

    # ----------------------------------------------------------------------
    # Prompts return their adversarial-playbook text with the ref embedded.
    # ----------------------------------------------------------------------

    def test_challenge_prompt_embeds_ref_and_vocabulary(self) -> None:
        result = _run(self.app.get_prompt("challenge", {"node_ref": "abc123"}))
        text = result.messages[0].content.text
        self.assertIn("abc123", text)
        self.assertIn("objection", text)
        # The shared vocabulary block is appended to every prompt.
        self.assertIn("Node types you may create", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
