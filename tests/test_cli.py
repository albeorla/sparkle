from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from src.sparkle import ops, presentation
from src.sparkle.cli import main
from src.sparkle.graph import GraphStore
from src.sparkle.models import Edge, Node


class SparkleCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "graph.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["--store", str(self.store), *args])
        finally:
            sys.stdin = old_stdin
        return exit_code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def handle_of(output: str) -> str:
        """Read the 12-char handle off the 'Handle:' line of an add/echo output.

        resolve_id accepts prefixes, so the handle works anywhere an id does.
        """
        for line in output.splitlines():
            if line.startswith("Handle:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no Handle: line in output:\n{output}")

    def test_init_and_bootstrap_seed_example_graph(self) -> None:
        exit_code, output, err = self.run_cli("init")
        self.assertEqual(exit_code, 0)
        self.assertIn("Initialized graph store", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("bootstrap")
        self.assertEqual(exit_code, 0)
        self.assertIn("Seeded concept graph", output)
        self.assertIn("root_claim_id:", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("list-nodes")
        self.assertEqual(exit_code, 0)
        self.assertIn("claim", output)
        self.assertIn("evidence", output)
        self.assertIn("synthesis", output)
        self.assertEqual(err, "")

    def test_home_shows_empty_graph_guidance(self) -> None:
        self.run_cli("init")
        exit_code, output, err = self.run_cli("home")
        self.assertEqual(exit_code, 0)
        self.assertIn("SPARKLE HOME", output)
        self.assertIn("Graph is empty.", output)
        self.assertIn("bootstrap", output)
        self.assertEqual(err, "")

    def test_home_shows_counts_and_recent_nodes(self) -> None:
        self.run_cli("init")
        exit_code, _, err = self.run_cli("bootstrap")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("home")
        self.assertEqual(exit_code, 0)
        self.assertIn("SPARKLE HOME", output)
        self.assertIn("Nodes: 5", output)
        self.assertIn("Edges: 5", output)
        self.assertIn("By type", output)
        self.assertIn("By status", output)
        self.assertIn("Recent", output)
        self.assertIn("A claim-graph research tool can use Merkle-style provenance", output)
        self.assertIn("tree <node_id_prefix>", output)
        self.assertIn("why <node_id_prefix>", output)
        self.assertEqual(err, "")

    def test_list_nodes_supports_type_status_tag_query_and_limit_filters(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Promising claim",
            "--content",
            "Claim about provenance and branching.",
            "--status",
            "promising",
            "--tags",
            "origin",
            "research",
        )
        self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Origin evidence",
            "--content",
            "Evidence about provenance.",
            "--tags",
            "origin",
        )
        self.run_cli(
            "add-node",
            "--type",
            "question",
            "--title",
            "Open question",
            "--content",
            "A branch to revisit later.",
            "--status",
            "stalled",
        )

        exit_code, output, err = self.run_cli("list-nodes", "--type", "claim")
        self.assertEqual(exit_code, 0)
        self.assertIn("Promising claim", output)
        self.assertNotIn("Origin evidence", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("list-nodes", "--status", "stalled")
        self.assertEqual(exit_code, 0)
        self.assertIn("Open question", output)
        self.assertNotIn("Promising claim", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("list-nodes", "--tag", "origin")
        self.assertEqual(exit_code, 0)
        self.assertIn("Promising claim", output)
        self.assertIn("Origin evidence", output)
        self.assertNotIn("Open question", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("list-nodes", "--query", "branching")
        self.assertEqual(exit_code, 0)
        self.assertIn("Promising claim", output)
        self.assertNotIn("Origin evidence", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("list-nodes", "--limit", "1")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len([line for line in output.splitlines() if line.strip()]), 1)
        self.assertEqual(err, "")

    def test_add_node_add_edge_show_and_export(self) -> None:
        self.run_cli("init")

        exit_code, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Test claim",
            "--content",
            "A deterministic graph should preserve provenance.",
            "--status",
            "promising",
            "--confidence",
            "0.8",
        )
        self.assertEqual(exit_code, 0)
        claim_id = self.handle_of(claim_out)

        exit_code, evidence_out, _ = self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Test evidence",
            "--content",
            "The archived discussion explicitly argues for provenance and branching.",
            "--citations",
            "https://chatgpt.com/c/69b579e3-3cf8-8331-8d8d-185381cbbb01",
        )
        self.assertEqual(exit_code, 0)
        evidence_id = self.handle_of(evidence_out)

        exit_code, output, err = self.run_cli(
            "add-edge",
            "--from",
            evidence_id[:12],
            "--to",
            claim_id[:12],
            "--relation",
            "supports",
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Linked:", output)
        self.assertIn("--supports-->", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("show", claim_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("CLAIM  promising  0.80", output)
        self.assertIn("Test claim", output)
        self.assertIn("Incoming", output)
        self.assertIn("supports", output)
        self.assertIn("evidence", output)
        self.assertEqual(err, "")

        export_path = Path(self.temp_dir.name) / "exports" / "claim.md"
        exit_code, output, err = self.run_cli(
            "export",
            "--root",
            claim_id[:12],
            "--output",
            str(export_path),
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Exported markdown", output)
        self.assertEqual(err, "")
        rendered = export_path.read_text(encoding="utf-8")
        self.assertIn("# Test claim", rendered)
        self.assertIn("## Edges", rendered)

    def test_lineage_walks_inbound_graph(self) -> None:
        self.run_cli("init")

        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Root claim",
            "--content",
            "Root content",
        )
        claim_id = self.handle_of(claim_out)

        _, question_out, _ = self.run_cli(
            "add-node",
            "--type",
            "question",
            "--title",
            "Open question",
            "--content",
            "What would strengthen the claim?",
        )
        question_id = self.handle_of(question_out)

        _, synthesis_out, _ = self.run_cli(
            "add-node",
            "--type",
            "synthesis",
            "--title",
            "Working synthesis",
            "--content",
            "A tested graph is easier to trust.",
        )
        synthesis_id = self.handle_of(synthesis_out)

        self.run_cli(
            "add-edge",
            "--from",
            question_id[:12],
            "--to",
            claim_id[:12],
            "--relation",
            "refines",
        )
        self.run_cli(
            "add-edge",
            "--from",
            claim_id[:12],
            "--to",
            synthesis_id[:12],
            "--relation",
            "derived_from",
        )

        exit_code, output, err = self.run_cli("lineage", synthesis_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("Working synthesis", output)
        self.assertIn("Root claim", output)
        self.assertIn("Open question", output)
        self.assertEqual(err, "")

    def test_list_edges_and_export_stdout_after_bootstrap(self) -> None:
        self.run_cli("init")
        exit_code, bootstrap_out, err = self.run_cli("bootstrap")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")

        root_claim_id = next(
            line.split(": ", 1)[1]
            for line in bootstrap_out.splitlines()
            if line.startswith("root_claim_id:")
        )

        exit_code, output, err = self.run_cli("list-edges")
        self.assertEqual(exit_code, 0)
        self.assertIn("-[supports]->", output)
        self.assertIn("-[derived_from]->", output)
        self.assertEqual(err, "")

        exit_code, output, err = self.run_cli("export", "--root", root_claim_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("# A claim-graph research tool can use Merkle-style provenance", output)
        self.assertIn("## Nodes", output)
        self.assertEqual(err, "")

    def test_tree_renders_local_ascii_structure(self) -> None:
        self.run_cli("init")
        exit_code, bootstrap_out, err = self.run_cli("bootstrap")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")

        root_claim_id = next(
            line.split(": ", 1)[1]
            for line in bootstrap_out.splitlines()
            if line.startswith("root_claim_id:")
        )

        exit_code, output, err = self.run_cli("tree", root_claim_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("claim", output)
        self.assertIn("Incoming:", output)
        self.assertIn("Outgoing:", output)
        self.assertIn("supports", output)
        self.assertIn("derived_from", output)
        self.assertIn("├─", output)
        self.assertEqual(err, "")

    def test_why_renders_inbound_provenance_chain(self) -> None:
        self.run_cli("init")
        exit_code, bootstrap_out, err = self.run_cli("bootstrap")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")

        synthesis_id = next(
            line.split(": ", 1)[1]
            for line in bootstrap_out.splitlines()
            if line.startswith("synthesis_id:")
        )

        exit_code, output, err = self.run_cli("why", synthesis_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("synthesis", output)
        self.assertIn("<- derived_from", output)
        self.assertIn("<- supports", output)
        self.assertIn("<- refines", output)
        self.assertIn("<- contradicts", output)
        self.assertEqual(err, "")

    def test_empty_prefix_returns_nonzero_and_stderr(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "First claim",
            "--content",
            "First content",
        )
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Second claim",
            "--content",
            "Second content",
        )

        exit_code, output, err = self.run_cli("show", "")
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("node prefix cannot be empty", err)

    def test_add_edge_rejects_unknown_node_reference(self) -> None:
        self.run_cli("init")
        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Claim",
            "--content",
            "Claim content",
        )
        claim_id = self.handle_of(claim_out)

        exit_code, output, err = self.run_cli(
            "add-edge",
            "--from",
            "missing",
            "--to",
            claim_id[:12],
            "--relation",
            "supports",
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("No node found for prefix", err)

    def test_list_templates_shows_structured_branch_options(self) -> None:
        self.run_cli("init")
        exit_code, output, err = self.run_cli("list-templates")
        self.assertEqual(exit_code, 0)
        self.assertIn("support", output)
        self.assertIn("objection", output)
        self.assertIn("reframing", output)
        self.assertIn("application", output)
        self.assertEqual(err, "")

    def test_add_branch_creates_templated_node_and_edge(self) -> None:
        self.run_cli("init")
        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Merkle DAG research claim",
            "--content",
            "A structured claim graph can improve solo research.",
        )
        claim_id = self.handle_of(claim_out)

        exit_code, output, err = self.run_cli(
            "add-branch",
            "--from",
            claim_id[:12],
            "--template",
            "support",
            "--title",
            "Support with origin evidence",
            "--citations",
            "https://chatgpt.com/c/69b579e3-3cf8-8331-8d8d-185381cbbb01",
            "--tags",
            "origin",
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Added evidence: Support with origin evidence", output)
        self.assertIn("Linked:", output)
        self.assertIn("--supports-->", output)
        self.assertEqual(err, "")
        branch_id = self.handle_of(output)

        exit_code, output, err = self.run_cli("show", claim_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertIn("Incoming", output)
        self.assertIn("supports", output)
        self.assertIn("Support with origin evidence", output)
        self.assertEqual(err, "")

        exit_code, branch_output, err = self.run_cli("show", branch_id)
        self.assertEqual(exit_code, 0)
        self.assertIn("EVIDENCE  active  0.50", branch_output)
        self.assertIn("Tags: branch:support, template, origin", branch_output)
        self.assertIn("What evidence, source, or observation strengthens this claim?", branch_output)
        self.assertEqual(err, "")

    def test_add_branch_rejects_unknown_template(self) -> None:
        self.run_cli("init")
        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Base claim",
            "--content",
            "Base content",
        )
        claim_id = self.handle_of(claim_out)

        exit_code, output, err = self.run_cli(
            "add-branch",
            "--from",
            claim_id[:12],
            "--template",
            "invalid",
            "--title",
            "Broken branch",
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("Unknown template", err)

    def test_add_node_rejects_out_of_range_confidence(self) -> None:
        self.run_cli("init")
        exit_code, output, err = self.run_cli(
            "add-node",
            "--type", "claim",
            "--title", "Bad confidence",
            "--content", "Should fail",
            "--confidence", "1.5",
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("confidence", err)

    def test_unknown_prefix_returns_nonzero_and_stderr(self) -> None:
        self.run_cli("init")
        exit_code, output, err = self.run_cli("show", "missing")
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("No node found for prefix", err)

    def test_graph_kernel_supports_custom_types_relations_metadata_and_export(self) -> None:
        store = GraphStore(
            self.store,
            node_types={"claim", "metric"},
            edge_relations={"quantifies"},
        )
        claim_id = store.add_node(Node(node_type="claim", title="Claim", content="Content", metadata={"pack": "finance"}))
        metric_id = store.add_node(
            Node(
                node_type="metric",
                title="Sharpe delta",
                content="Delta was positive in fixture data.",
                confidence=None,
                tags=["finance"],
                metadata={"value": 0.12},
            )
        )
        edge_id = store.add_edge(
            Edge(
                from_id=metric_id,
                to_id=claim_id,
                relation="quantifies",
                metadata={"source": "fixture"},
            )
        )

        self.assertEqual(store.resolve_id(claim_id[:12]), claim_id)
        self.assertEqual(store.get_node(claim_id[:12])["metadata"]["pack"], "finance")
        self.assertEqual(store.list_nodes(node_type="metric", tag="finance", query="positive")[0][0], metric_id)
        self.assertEqual(store.list_edges()[0][0], edge_id)
        self.assertIn(metric_id, store.export_lineage(claim_id[:12])["nodes"])
        self.assertIn("<- quantifies", presentation.render_why(store, claim_id[:12]))

    def test_graph_kernel_rejects_unknown_custom_type_or_relation(self) -> None:
        store = GraphStore(self.store, node_types={"claim"}, edge_relations={"supports"})
        claim_id = store.add_node(Node(node_type="claim", title="Claim", content="Content"))
        other_id = store.add_node(Node(node_type="claim", title="Other", content="Content"))

        with self.assertRaisesRegex(ValueError, "unknown node_type"):
            store.add_node(Node(node_type="metric", title="Metric", content="Content"))
        with self.assertRaisesRegex(ValueError, "unknown edge relation"):
            store.add_edge(Edge(from_id=other_id, to_id=claim_id, relation="quantifies"))

    def test_none_confidence_renders_na_across_read_commands(self) -> None:
        store = GraphStore(self.store)
        node_id = store.add_node(
            Node(
                node_type="claim",
                title="No confidence claim",
                content="A claim with unknown confidence.",
                confidence=None,
            )
        )

        for command in ("list-nodes",):
            exit_code, output, err = self.run_cli(command)
            self.assertEqual(exit_code, 0)
            self.assertEqual(err, "")
            self.assertIn("n/a", output)

        for command in ("show", "lineage"):
            exit_code, output, err = self.run_cli(command, node_id[:12])
            self.assertEqual(exit_code, 0)
            self.assertEqual(err, "")
            self.assertIn("n/a", output)

    def test_export_skips_dangling_edge_endpoint(self) -> None:
        store = GraphStore(self.store)
        root_id = store.add_node(
            Node(node_type="claim", title="Root claim", content="Root content")
        )
        evidence_id = store.add_node(
            Node(node_type="evidence", title="Real evidence", content="Evidence content")
        )
        store.add_edge(Edge(from_id=evidence_id, to_id=root_id, relation="supports"))

        # Inject a dangling edge pointing at a node id that does not exist.
        missing_id = "0" * 64
        data = json.loads(self.store.read_text(encoding="utf-8"))
        dangling_edge = {
            "from_id": missing_id,
            "to_id": root_id,
            "relation": "supports",
            "note": "",
            "created_at": "2026-01-01T00:00:00+00:00",
            "metadata": {},
        }
        data["edges"]["dangling-edge-id"] = dangling_edge
        self.store.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

        exit_code, output, err = self.run_cli("export", "--root", root_id[:12])
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("# Root claim", output)
        self.assertIn("Real evidence", output)
        # The dangling endpoint id must not show up as a rendered node body.
        self.assertNotIn(missing_id, output)

    def test_invalid_store_missing_required_field_exits_two(self) -> None:
        store = GraphStore(self.store)
        node_id = store.add_node(
            Node(node_type="claim", title="Claim title", content="Claim content")
        )

        # Corrupt the store by removing a required node field.
        data = json.loads(self.store.read_text(encoding="utf-8"))
        del data["nodes"][node_id]["title"]
        self.store.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

        exit_code, output, err = self.run_cli("list-nodes")
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("Invalid graph store", err)

    def test_lineage_and_full_component_export_differ(self) -> None:
        store = GraphStore(self.store)
        root_id = store.add_node(
            Node(node_type="claim", title="Root claim", content="Root content")
        )
        supporter_id = store.add_node(
            Node(node_type="evidence", title="Supporting evidence", content="Supports the root")
        )
        downstream_id = store.add_node(
            Node(node_type="synthesis", title="Downstream synthesis", content="Built from the root")
        )

        # Supporter -> root (inbound to root); root -> downstream (outbound from root).
        store.add_edge(Edge(from_id=supporter_id, to_id=root_id, relation="supports"))
        store.add_edge(Edge(from_id=root_id, to_id=downstream_id, relation="derived_from"))

        lineage_nodes = store.export_lineage(root_id)["nodes"]
        self.assertIn(supporter_id, lineage_nodes)
        self.assertNotIn(downstream_id, lineage_nodes)

        rendered = presentation.export_markdown(store, root_id)
        self.assertIn("Supporting evidence", rendered)
        self.assertIn("Downstream synthesis", rendered)

    def test_list_nodes_limit_emits_expected_single_node(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Only limited claim",
            "--content",
            "Single emitted node content.",
        )

        exit_code, output, err = self.run_cli("list-nodes", "--limit", "1")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        emitted = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(len(emitted), 1)
        self.assertIn("Only limited claim", emitted[0])

    def test_content_addressing_is_idempotent(self) -> None:
        store = GraphStore(self.store)
        node = Node(
            node_type="claim",
            title="Idempotent claim",
            content="Same content addressed twice.",
            created_at="2026-01-01T00:00:00+00:00",
        )

        first_id = store.add_node(node)
        second_id = store.add_node(node)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(store.list_nodes()), 1)

        different = Node(
            node_type="claim",
            title="Different claim",
            content="Different content yields a different id.",
            created_at="2026-01-01T00:00:00+00:00",
        )
        different_id = store.add_node(different)
        self.assertNotEqual(first_id, different_id)

    # ------------------------------------------------------------------
    # Refactored CLI + new behavior (Phases 0, 1a-1c)
    # ------------------------------------------------------------------

    def test_add_node_accepts_ratified_terminal_status(self) -> None:
        """The new terminal status 'ratified' is a valid --status choice."""
        self.run_cli("init")
        exit_code, output, err = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Already settled claim",
            "--content",
            "A claim a settled ruling has ratified.",
            "--status",
            "ratified",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Added claim: Already settled claim", output)
        claim_id = self.handle_of(output)

        exit_code, show_out, err = self.run_cli("show", claim_id)
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("CLAIM  ratified", show_out)

    def test_add_node_fused_link_creates_node_and_edge_in_one_call(self) -> None:
        """add-node --link-to/--relation makes the new node the edge source."""
        self.run_cli("init")
        _, parent_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Parent claim",
            "--content",
            "Claim the new evidence will back.",
        )
        parent_id = self.handle_of(parent_out)

        exit_code, output, err = self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Fused evidence",
            "--content",
            "Evidence created and linked in one command.",
            "--link-to",
            parent_id[:12],
            "--relation",
            "supports",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Added evidence: Fused evidence", output)
        # The fused link echoes new node (source) --supports--> parent (target).
        self.assertIn(
            'Linked: evidence "Fused evidence" --supports--> claim "Parent claim"',
            output,
        )
        evidence_id = self.handle_of(output)
        self.assertNotEqual(evidence_id, parent_id)

        # The parent now has the new evidence as an inbound supports neighbor.
        _, show_out, _ = self.run_cli("show", parent_id[:12])
        self.assertIn("Incoming", show_out)
        self.assertIn("supports", show_out)
        self.assertIn("Fused evidence", show_out)

    def test_add_node_link_to_requires_relation(self) -> None:
        """--link-to without --relation is rejected at the ops boundary."""
        self.run_cli("init")
        _, parent_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Lonely claim",
            "--content",
            "Has nothing pointed at it.",
        )
        parent_id = self.handle_of(parent_out)

        exit_code, output, err = self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Orphan link",
            "--content",
            "Tried to link without naming a relation.",
            "--link-to",
            parent_id[:12],
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("--link-to and --relation must be given together", err)

    def test_alias_resolves_and_repoints_on_reuse(self) -> None:
        """--as registers a name; reusing it re-points to the new node."""
        self.run_cli("init")
        _, first_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "First topic claim",
            "--content",
            "The original claim behind the name.",
            "--as",
            "topic",
        )
        first_id = self.handle_of(first_out)

        # The alias resolves anywhere an id is accepted (here, show).
        exit_code, show_out, err = self.run_cli("show", "topic")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("First topic claim", show_out)

        # Reuse the same name on a new node: the alias must follow the new node
        # so a name survives supersession instead of pointing at the stale one.
        _, second_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Second topic claim",
            "--content",
            "A corrected claim reusing the name.",
            "--as",
            "topic",
        )
        second_id = self.handle_of(second_out)
        self.assertNotEqual(first_id, second_id)

        exit_code, show_out, err = self.run_cli("show", "topic")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Second topic claim", show_out)
        self.assertNotIn("First topic claim", show_out)

        # The alias sidecar lives beside the store, never in the hashed payload.
        aliases_path = self.store.with_name("aliases.json")
        self.assertTrue(aliases_path.exists())
        aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
        self.assertTrue(aliases["topic"].startswith(second_id))

    def test_rule_invariant_rejects_ruling_with_no_inbound_objection(self) -> None:
        """The judge's rule() move refuses to ratify an unchallenged claim.

        The invariant lives in ops.py (one ValueError boundary) so the CLI, MCP
        server, and any future harness all inherit it.
        """
        store = GraphStore(self.store)
        claim_id = store.add_node(
            Node(node_type="claim", title="Unchallenged claim", content="Nobody attacked it.")
        )

        with self.assertRaisesRegex(ValueError, "never attacked"):
            ops.rule(store, claim_id[:12], verdict="accept")

        # Add an objection (a contradicts edge) and the ruling is now legal.
        objection_id = store.add_node(
            Node(node_type="objection", title="A counterpoint", content="Here is the attack.")
        )
        store.add_edge(
            Edge(from_id=objection_id, to_id=claim_id, relation="contradicts")
        )

        result = ops.rule(store, claim_id[:12], verdict="reject", settle=True)
        # A ruling writes a decision node plus a superseding ratified claim
        # version — never an in-place mutation of the frozen claim.
        self.assertTrue(result["settled"])
        self.assertEqual(result["decision"]["node_type"], "decision")
        self.assertEqual(result["ratified_claim"]["status"], "ratified")
        self.assertNotEqual(result["ratified_claim"]["node_id"], claim_id)
        # The original claim is untouched (frozen).
        self.assertEqual(store.get_node(claim_id)["status"], "active")

    def test_dedup_fingerprint_returns_existing_id_on_identical_reproposal(self) -> None:
        """Re-proposing identical type+title+content collapses to the same node."""
        store = GraphStore(self.store)
        first = ops.add_node(
            store,
            node_type="claim",
            title="Dedup target",
            content="The exact same idea proposed twice.",
        )
        self.assertTrue(first["created"])

        # A different author/confidence/timestamp must NOT fork the graph: the
        # fingerprint deliberately excludes those fields.
        second = ops.add_node(
            store,
            node_type="claim",
            title="Dedup target",
            content="The exact same idea proposed twice.",
            author="someone-else",
            confidence=0.9,
        )
        self.assertFalse(second["created"])
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(len(store.list_nodes()), 1)

        # A genuinely different claim still creates a distinct node.
        third = ops.add_node(
            store,
            node_type="claim",
            title="Different target",
            content="A distinct idea.",
        )
        self.assertTrue(third["created"])
        self.assertEqual(len(store.list_nodes()), 2)

    def test_import_builds_graph_from_json_on_stdin(self) -> None:
        """import '-' reads a JSON document from stdin and builds the graph."""
        self.run_cli("init")
        document = {
            "nodes": [
                {
                    "ref": "c1",
                    "node_type": "claim",
                    "title": "Imported claim",
                    "content": "A claim brought in from JSON.",
                },
                {
                    "ref": "e1",
                    "node_type": "evidence",
                    "title": "Imported evidence",
                    "content": "Evidence brought in from JSON.",
                },
            ],
            "edges": [
                {"from": "e1", "to": "c1", "relation": "supports"},
            ],
        }
        exit_code, output, err = self.run_cli(
            "import", "-", stdin=json.dumps(document)
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Imported 2 node(s), 1 edge(s)", output)
        self.assertIn("Nicknames", output)
        self.assertIn("c1", output)
        self.assertIn("e1", output)

        # The graph really contains the imported nodes and the supports edge.
        store = GraphStore(self.store)
        titles = {node["title"] for _, node in store.list_nodes()}
        self.assertEqual(titles, {"Imported claim", "Imported evidence"})
        edges = store.list_edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0][1]["relation"], "supports")

    def test_import_rejects_invalid_json_from_stdin(self) -> None:
        self.run_cli("init")
        exit_code, output, err = self.run_cli("import", "-", stdin="{not valid json")
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("invalid JSON", err)

    def test_import_rejects_a_terminal_status(self) -> None:
        """The LLM/batch import on-ramp may not fabricate a ratified claim. A
        terminal status in imported JSON is refused on the same footing as a
        model-authored write, so an automated document cannot smuggle its own
        conclusions in as already-settled with no adversarial ruling."""
        self.run_cli("init")
        document = {
            "nodes": [
                {
                    "ref": "c1",
                    "node_type": "claim",
                    "title": "Pre-ratified claim",
                    "content": "A claim that tries to import itself as settled.",
                    "status": "ratified",
                }
            ],
            "edges": [],
        }
        exit_code, output, err = self.run_cli(
            "import", "-", stdin=json.dumps(document)
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("terminal status", err)
        # Nothing was written: the import was refused before any node landed.
        store = GraphStore(self.store)
        self.assertEqual(list(store.list_nodes()), [])

    def test_revise_supersedes_and_rehomes_inbound_edges(self) -> None:
        """revise writes a corrected copy and re-points inbound edges by default."""
        self.run_cli("init")
        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Claim to revise",
            "--content",
            "The original wording.",
        )
        claim_id = self.handle_of(claim_out)

        # Give the claim an inbound supporting edge to re-home.
        self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Backing evidence",
            "--content",
            "Supports the claim under revision.",
            "--link-to",
            claim_id[:12],
            "--relation",
            "supports",
        )

        exit_code, output, err = self.run_cli(
            "revise",
            claim_id[:12],
            "--title",
            "Claim revised",
            "--content",
            "The corrected wording.",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Re-homed 1 inbound edge(s)", output)
        new_id = self.handle_of(output)
        self.assertNotEqual(new_id, claim_id)

        store = GraphStore(self.store)
        # The new version carries an inbound supports edge (the re-homed one).
        new_neighbors = store.get_neighbor_details(new_id)
        self.assertIn(
            "supports", {item["relation"] for item in new_neighbors["inbound"]}
        )
        # The new version supersedes the old via an outbound supersedes edge.
        self.assertIn(
            "supersedes", {item["relation"] for item in new_neighbors["outbound"]}
        )
        # The original frozen node is untouched and still in the store.
        self.assertEqual(store.get_node(claim_id)["title"], "Claim to revise")

    def test_revise_can_leave_inbound_edges_on_superseded_version(self) -> None:
        self.run_cli("init")
        _, claim_out, _ = self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Keep edges claim",
            "--content",
            "Original.",
        )
        claim_id = self.handle_of(claim_out)
        self.run_cli(
            "add-node",
            "--type",
            "evidence",
            "--title",
            "Left-behind evidence",
            "--content",
            "Stays on the old version.",
            "--link-to",
            claim_id[:12],
            "--relation",
            "supports",
        )

        exit_code, output, err = self.run_cli(
            "revise", claim_id[:12], "--content", "Updated.", "--no-rehome-edges"
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertIn("Left inbound edges on the superseded version", output)
        new_id = self.handle_of(output)

        store = GraphStore(self.store)
        new_inbound = store.get_neighbor_details(new_id)["inbound"]
        self.assertEqual(
            [item["relation"] for item in new_inbound], []
        )

    def test_list_nodes_ids_only_prints_bare_handles(self) -> None:
        """--ids-only prints just the 12-char handles, one per line."""
        self.run_cli("init")
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "First scan claim",
            "--content",
            "Content one.",
        )
        self.run_cli(
            "add-node",
            "--type",
            "claim",
            "--title",
            "Second scan claim",
            "--content",
            "Content two.",
        )

        exit_code, output, err = self.run_cli("list-nodes", "--ids-only")
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(len(line), 12)
            self.assertTrue(all(ch in "0123456789abcdef" for ch in line))
        # No titles or summary columns leak into the bare-handle output.
        self.assertNotIn("First scan claim", output)
        self.assertNotIn("claim", output)


if __name__ == "__main__":
    unittest.main()
