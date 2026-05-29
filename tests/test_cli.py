from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from src.sparkle import presentation
from src.sparkle.cli import main
from src.sparkle.graph import GraphStore
from src.sparkle.models import Edge, Node


class SparkleCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Path(self.temp_dir.name) / "graph.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["--store", str(self.store), *args])
        return exit_code, stdout.getvalue(), stderr.getvalue()

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
        claim_id = claim_out.strip().split()[-1]

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
        evidence_id = evidence_out.strip().split()[-1]

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
        self.assertIn("Added edge", output)
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
        claim_id = claim_out.strip().split()[-1]

        _, question_out, _ = self.run_cli(
            "add-node",
            "--type",
            "question",
            "--title",
            "Open question",
            "--content",
            "What would strengthen the claim?",
        )
        question_id = question_out.strip().split()[-1]

        _, synthesis_out, _ = self.run_cli(
            "add-node",
            "--type",
            "synthesis",
            "--title",
            "Working synthesis",
            "--content",
            "A tested graph is easier to trust.",
        )
        synthesis_id = synthesis_out.strip().split()[-1]

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
        claim_id = claim_out.strip().split()[-1]

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
        claim_id = claim_out.strip().split()[-1]

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
        self.assertIn("Added branch node", output)
        self.assertIn("Added branch edge", output)
        self.assertEqual(err, "")
        branch_id = next(
            line.split()[-1]
            for line in output.splitlines()
            if line.startswith("Added branch node ")
        )

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
        claim_id = claim_out.strip().split()[-1]

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


if __name__ == "__main__":
    unittest.main()
