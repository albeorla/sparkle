from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

from typing import get_args

from . import presentation
from .bootstrap import seed_concept_graph
from .graph import GraphStore
from .models import DEFAULT_EDGE_RELATIONS, Edge, Node, NodeStatus, NodeType
from .templates import BRANCH_TEMPLATES, build_branch_node

VALID_NODE_TYPES = list(get_args(NodeType))
VALID_NODE_STATUSES = list(get_args(NodeStatus))
VALID_EDGE_RELATIONS = sorted(DEFAULT_EDGE_RELATIONS)


def confidence_arg(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: '{raw}'")
    if not (0.0 <= value <= 1.0):
        raise argparse.ArgumentTypeError(f"confidence must be between 0.0 and 1.0, got {value}")
    return value


DEFAULT_STORE = Path(".sparkle/graph.json")
TYPE_SYMBOLS = {
    "claim": "C",
    "evidence": "+",
    "question": "?",
    "objection": "-",
    "inference": "~",
    "decision": "!",
    "synthesis": ">",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sparkle claim-graph MVP")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="Path to graph store JSON file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the local graph store")
    subparsers.add_parser("home", help="Show a dashboard for the current graph")
    list_nodes = subparsers.add_parser("list-nodes", help="List nodes in the graph")
    list_nodes.add_argument("--type", dest="filter_type", choices=VALID_NODE_TYPES)
    list_nodes.add_argument("--status", choices=VALID_NODE_STATUSES)
    list_nodes.add_argument("--tag")
    list_nodes.add_argument("--query")
    list_nodes.add_argument("--limit", type=int)
    subparsers.add_parser("list-edges", help="List edges in the graph")
    subparsers.add_parser("bootstrap", help="Seed the store from the original concept conversation")
    subparsers.add_parser("list-templates", help="List structured branch templates")

    add_node = subparsers.add_parser("add-node", help="Add a typed node")
    add_node.add_argument("--type", required=True, dest="node_type", choices=VALID_NODE_TYPES)
    add_node.add_argument("--title", required=True)
    add_node.add_argument("--content", required=True)
    add_node.add_argument("--citations", nargs="*", default=[])
    add_node.add_argument("--author", default="local")
    add_node.add_argument("--confidence", type=confidence_arg, default=0.5)
    add_node.add_argument("--status", default="active", choices=VALID_NODE_STATUSES)
    add_node.add_argument("--tags", nargs="*", default=[])

    add_edge = subparsers.add_parser("add-edge", help="Link two nodes")
    add_edge.add_argument("--from", required=True, dest="from_id")
    add_edge.add_argument("--to", required=True, dest="to_id")
    add_edge.add_argument("--relation", required=True, choices=VALID_EDGE_RELATIONS)
    add_edge.add_argument("--note", default="")

    add_branch = subparsers.add_parser("add-branch", help="Create a structured inquiry branch from an existing node")
    add_branch.add_argument("--from", required=True, dest="from_id")
    add_branch.add_argument("--template", required=True)
    add_branch.add_argument("--title", required=True)
    add_branch.add_argument("--content")
    add_branch.add_argument("--citations", nargs="*", default=[])
    add_branch.add_argument("--author", default="local")
    add_branch.add_argument("--confidence", type=confidence_arg, default=0.5)
    add_branch.add_argument("--tags", nargs="*", default=[])

    show = subparsers.add_parser("show", help="Show a node with inbound and outbound edges")
    show.add_argument("node_id")

    tree = subparsers.add_parser("tree", help="Render a local ASCII tree for a node")
    tree.add_argument("node_id")

    why = subparsers.add_parser("why", help="Render inbound provenance for a node")
    why.add_argument("node_id")

    lineage = subparsers.add_parser("lineage", help="Trace inbound lineage for a node")
    lineage.add_argument("node_id")

    export = subparsers.add_parser("export", help="Export a subgraph to markdown")
    export.add_argument("--root", required=True, dest="root_id")
    export.add_argument("--output", type=Path)

    return parser


def print_node_summary(node_id: str, node: dict) -> None:
    c = node["confidence"]
    conf = f"{c:.2f}" if c is not None else "n/a"
    print(
        f"{node_id[:12]}  {node['node_type']:<10}  {node['status']:<16}  "
        f"{conf}  {node['title']}"
    )


def format_related_node(node_id: str, node: dict) -> str:
    symbol = TYPE_SYMBOLS.get(node["node_type"], "*")
    return f"{symbol} {node['node_type']:<10} {node_id[:12]}  {node['title']}"


def print_relation_groups(title: str, items: list[dict]) -> None:
    if not items:
        return
    print(title)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["relation"]].append(item)

    for relation in sorted(grouped):
        print(f"  {relation}")
        for item in grouped[relation]:
            print(f"    {format_related_node(item['node_id'], item['node'])}")


def print_kv_counts(title: str, counts: dict[str, int]) -> None:
    if not counts:
        return
    print(title)
    for key in sorted(counts):
        print(f"  {key:<18} {counts[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    store = GraphStore(args.store)

    try:
        if args.command == "init":
            print(f"Initialized graph store at {args.store}")
            return 0

        if args.command == "bootstrap":
            ids = seed_concept_graph(store)
            print("Seeded concept graph from original concept conversation")
            for label, node_id in ids.items():
                print(f"{label}: {node_id}")
            return 0

        if args.command == "home":
            nodes = store.list_nodes()
            edges = store.list_edges()
            type_counts: dict[str, int] = defaultdict(int)
            status_counts: dict[str, int] = defaultdict(int)
            for _, node in nodes:
                type_counts[node["node_type"]] += 1
                status_counts[node["status"]] += 1

            print("SPARKLE HOME")
            print(f"Store: {args.store}")
            print(f"Nodes: {len(nodes)}")
            print(f"Edges: {len(edges)}")
            print("")

            if not nodes:
                print("Graph is empty.")
                print("")
                print("Next:")
                print("  python3 -m src.sparkle.cli bootstrap")
                print("  python3 -m src.sparkle.cli add-node --type claim --title \"...\" --content \"...\"")
                return 0

            print_kv_counts("By type", type_counts)
            print("")
            print_kv_counts("By status", status_counts)
            print("")
            print("Recent")
            for node_id, node in nodes[-5:]:
                print(f"  {node_id[:12]}  {node['node_type']:<10} {node['status']:<16} {node['title']}")
            print("")
            print("Next")
            print("  python3 -m src.sparkle.cli list-nodes --status active")
            print("  python3 -m src.sparkle.cli tree <node_id_prefix>")
            print("  python3 -m src.sparkle.cli why <node_id_prefix>")
            print("  python3 -m src.sparkle.cli add-branch --from <node_id_prefix> --template support --title \"...\"")
            return 0

        if args.command == "list-templates":
            for name in sorted(BRANCH_TEMPLATES):
                template = BRANCH_TEMPLATES[name]
                print(
                    f"{template.name:<12} {template.node_type:<10} "
                    f"{template.relation:<12} {template.description}"
                )
            return 0

        if args.command == "add-node":
            node = Node(
                node_type=args.node_type,
                title=args.title,
                content=args.content,
                citations=args.citations,
                author=args.author,
                confidence=args.confidence,
                status=args.status,
                tags=args.tags,
            )
            node_id = store.add_node(node)
            print(f"Added node {node_id}")
            return 0

        if args.command == "add-edge":
            from_id = store.resolve_id(args.from_id)
            to_id = store.resolve_id(args.to_id)
            edge = Edge(from_id=from_id, to_id=to_id, relation=args.relation, note=args.note)
            edge_id = store.add_edge(edge)
            print(f"Added edge {edge_id}")
            return 0

        if args.command == "add-branch":
            from_id = store.resolve_id(args.from_id)
            parent_node = store.get_node(from_id)
            branch_node, template = build_branch_node(
                parent_title=parent_node["title"],
                template_name=args.template,
                title=args.title,
                content=args.content,
                citations=args.citations,
                author=args.author,
                confidence=args.confidence,
                extra_tags=args.tags,
            )
            branch_id = store.add_node(branch_node)
            edge_id = store.add_edge(
                Edge(
                    from_id=branch_id,
                    to_id=from_id,
                    relation=template.relation,
                    note=template.edge_note,
                )
            )
            print(f"Added branch node {branch_id}")
            print(f"Added branch edge {edge_id}")
            return 0

        if args.command == "list-nodes":
            nodes = store.list_nodes(
                node_type=args.filter_type,
                status=args.status,
                tag=args.tag,
                query=args.query,
            )
            if args.limit is not None:
                nodes = nodes[: args.limit]
            if not nodes:
                print("No nodes found")
                return 0
            for node_id, node in nodes:
                print_node_summary(node_id, node)
            return 0

        if args.command == "list-edges":
            edges = store.list_edges()
            if not edges:
                print("No edges found")
                return 0
            for edge_id, edge in edges:
                print(
                    f"{edge_id[:12]}  {edge['from_id'][:12]} -[{edge['relation']}]-> "
                    f"{edge['to_id'][:12]}"
                )
            return 0

        if args.command == "show":
            node_id = store.resolve_id(args.node_id)
            node = store.get_node(node_id)
            neighbors = store.get_neighbor_details(node_id)

            c = node["confidence"]
            conf = f"{c:.2f}" if c is not None else "n/a"
            print(f"{node['node_type'].upper()}  {node['status']}  {conf}")
            print(node["title"])
            print(f"ID: {node_id}")
            if node["tags"]:
                print(f"Tags: {', '.join(node['tags'])}")
            if node["citations"]:
                print(f"Citations: {', '.join(node['citations'])}")
            print("")
            print(node["content"])
            print("")
            print_relation_groups("Incoming", neighbors["inbound"])
            print_relation_groups("Outgoing", neighbors["outbound"])
            return 0

        if args.command == "tree":
            node_id = store.resolve_id(args.node_id)
            print(presentation.render_tree(store, node_id), end="")
            return 0

        if args.command == "why":
            node_id = store.resolve_id(args.node_id)
            print(presentation.render_why(store, node_id), end="")
            return 0

        if args.command == "lineage":
            node_id = store.resolve_id(args.node_id)
            for ancestor_id, ancestor in store.lineage(node_id):
                print_node_summary(ancestor_id, ancestor)
            return 0

        if args.command == "export":
            root_id = store.resolve_id(args.root_id)
            rendered = presentation.export_markdown(store, root_id, args.output)
            if args.output:
                print(f"Exported markdown to {args.output}")
            else:
                print(rendered)
            return 0

    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
