from __future__ import annotations

from pathlib import Path

from .graph import GraphStore


def render_tree(store: GraphStore, root_id_or_prefix: str) -> str:
    root_id = store.resolve_id(root_id_or_prefix)
    root = store.get_node(root_id)
    lines = [f"{root['node_type']} {root_id[:12]}  {root['title']}"]

    neighbors = store.get_neighbor_details(root_id)
    inbound = neighbors["inbound"]
    outbound = neighbors["outbound"]

    def append_branch(title: str, items: list[dict]) -> None:
        if not items:
            return
        lines.append(f"{title}:")
        for index, item in enumerate(items):
            connector = "└─" if index == len(items) - 1 else "├─"
            lines.append(
                f"{connector} {item['relation']:<12} {item['node']['node_type']:<10} "
                f"{item['node_id'][:12]}  {item['node']['title']}"
            )

    append_branch("Incoming", inbound)
    append_branch("Outgoing", outbound)
    return "\n".join(lines) + "\n"


def render_why(store: GraphStore, root_id_or_prefix: str) -> str:
    root_id = store.resolve_id(root_id_or_prefix)
    root = store.get_node(root_id)
    lines = [f"{root['node_type']} {root_id[:12]}  {root['title']}"]
    visited: set[str] = set()

    def walk(node_id: str, prefix: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        inbound = store.get_neighbor_details(node_id)["inbound"]
        for item in inbound:
            lines.append(
                f"{prefix}<- {item['relation']:<12} {item['node']['node_type']:<10} "
                f"{item['node_id'][:12]}  {item['node']['title']}"
            )
            walk(item["node_id"], prefix + "   ")

    walk(root_id, "")
    return "\n".join(lines) + "\n"


def export_markdown(store: GraphStore, root_id_or_prefix: str, output: Path | None = None) -> str:
    root_id = store.resolve_id(root_id_or_prefix)
    nodes, edges = store.subgraph(root_id)
    root = store.get_node(root_id)

    c = root["confidence"]
    root_conf = f"{c:.2f}" if c is not None else "n/a"
    lines = [
        f"# {root['title']}",
        "",
        f"- Root node: `{root_id}`",
        f"- Type: `{root['node_type']}`",
        f"- Status: `{root['status']}`",
        f"- Confidence: `{root_conf}`",
        "",
        "## Root claim",
        "",
        root["content"],
        "",
        "## Nodes",
        "",
    ]

    for node_id, node in nodes:
        if node_id == root_id:
            continue
        c = node["confidence"]
        conf = f"{c:.2f}" if c is not None else "n/a"
        lines.extend(
            [
                f"### {node['title']}",
                "",
                f"- ID: `{node_id}`",
                f"- Type: `{node['node_type']}`",
                f"- Status: `{node['status']}`",
                f"- Confidence: `{conf}`",
            ]
        )
        if node["citations"]:
            lines.append(f"- Citations: {', '.join(node['citations'])}")
        lines.extend(["", node["content"], ""])

    lines.extend(["## Edges", ""])
    for edge in edges:
        lines.append(
            f"- `{edge['from_id'][:12]}` -[{edge['relation']}]-> `{edge['to_id'][:12]}`"
            + (f" ({edge['note']})" if edge["note"] else "")
        )

    rendered = "\n".join(lines) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return rendered
