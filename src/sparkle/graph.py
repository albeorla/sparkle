from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
from typing import Any

from .models import DEFAULT_EDGE_RELATIONS, DEFAULT_NODE_TYPES, Edge, Node


class GraphStore:
    def __init__(
        self,
        path: str | Path,
        *,
        node_types: set[str] | None = None,
        edge_relations: set[str] | None = None,
    ) -> None:
        self.node_types = node_types or DEFAULT_NODE_TYPES
        self.edge_relations = edge_relations or DEFAULT_EDGE_RELATIONS
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"nodes": {}, "edges": {}})

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt graph store at {self.path}: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict) or not isinstance(data.get("edges"), dict):
            raise ValueError(f"Invalid graph store at {self.path}")
        for nid, n in data["nodes"].items():
            if not all(key in n for key in ("node_type", "title", "content")):
                raise ValueError(f"Invalid graph store at {self.path}: node {nid} missing required fields")
        for eid, e in data["edges"].items():
            if not all(key in e for key in ("from_id", "to_id", "relation")):
                raise ValueError(f"Invalid graph store at {self.path}: edge {eid} missing required fields")
        return data

    def _write(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def init(self) -> None:
        if not self.path.exists():
            self._write({"nodes": {}, "edges": {}})

    def add_node(self, node: Node) -> str:
        self._validate_node(node)
        data = self._read()
        node_id = node.compute_id()
        existing = data["nodes"].get(node_id)
        if existing is not None and existing != node.to_payload():
            raise ValueError(f"node id collision: {node_id} already exists with different content")
        data["nodes"][node_id] = node.to_payload()
        self._write(data)
        return node_id

    def add_edge(self, edge: Edge) -> str:
        self._validate_edge(edge)
        data = self._read()
        if edge.from_id not in data["nodes"]:
            raise ValueError(f"Unknown from_id: {edge.from_id}")
        if edge.to_id not in data["nodes"]:
            raise ValueError(f"Unknown to_id: {edge.to_id}")
        edge_id = edge.compute_id()
        existing = data["edges"].get(edge_id)
        if existing is not None and existing != edge.to_payload():
            raise ValueError(f"edge id collision: {edge_id} already exists with different content")
        data["edges"][edge_id] = edge.to_payload()
        self._write(data)
        return edge_id

    def read(self) -> dict[str, Any]:
        return self._read()

    def list_nodes(
        self,
        *,
        node_type: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        query: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        data = self._read()
        nodes = sorted(data["nodes"].items(), key=lambda item: item[1].get("created_at", ""))
        output = []
        for node_id, node in nodes:
            if node_type and node.get("node_type") != node_type:
                continue
            if status and node.get("status") != status:
                continue
            if tag and tag not in node.get("tags", []):
                continue
            if query:
                haystack = " ".join(
                    [
                        str(node.get("title", "")),
                        str(node.get("content", "")),
                        " ".join(node.get("tags", [])),
                        " ".join(node.get("citations", [])),
                    ]
                ).lower()
                if query.lower() not in haystack:
                    continue
            output.append((node_id, node))
        return output

    def list_edges(self) -> list[tuple[str, dict[str, Any]]]:
        data = self._read()
        return sorted(data["edges"].items(), key=lambda item: item[1].get("created_at", ""))

    def resolve_id(self, prefix: str) -> str:
        if not prefix or not prefix.strip():
            raise ValueError("node prefix cannot be empty")
        data = self._read()
        if prefix in data["nodes"]:
            return prefix
        matches = [node_id for node_id in data["nodes"] if node_id.startswith(prefix)]
        if not matches:
            raise ValueError(f"No node found for prefix: {prefix}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous prefix {prefix}: {', '.join(matches[:5])}")
        return matches[0]

    def get_node(self, node_id_or_prefix: str) -> dict[str, Any]:
        node_id = self.resolve_id(node_id_or_prefix)
        data = self._read()
        try:
            return data["nodes"][node_id]
        except KeyError as exc:
            raise ValueError(f"Unknown node: {node_id}") from exc

    def _collect_neighbors(self, data: dict[str, Any], node_id: str) -> dict[str, list[dict[str, Any]]]:
        inbound = []
        outbound = []
        for edge_id, edge in data["edges"].items():
            if edge["to_id"] == node_id:
                if edge["from_id"] not in data["nodes"]:
                    continue
                related_node = data["nodes"][edge["from_id"]]
                inbound.append({"edge_id": edge_id, "node_id": edge["from_id"], "node": related_node, **edge})
            if edge["from_id"] == node_id:
                if edge["to_id"] not in data["nodes"]:
                    continue
                related_node = data["nodes"][edge["to_id"]]
                outbound.append({"edge_id": edge_id, "node_id": edge["to_id"], "node": related_node, **edge})
        inbound.sort(key=lambda item: (item["relation"], item["node"]["title"]))
        outbound.sort(key=lambda item: (item["relation"], item["node"]["title"]))
        return {"inbound": inbound, "outbound": outbound}

    def get_neighbor_details(self, node_id_or_prefix: str) -> dict[str, list[dict[str, Any]]]:
        node_id = self.resolve_id(node_id_or_prefix)
        data = self._read()
        return self._collect_neighbors(data, node_id)

    def lineage(self, root_id_or_prefix: str) -> list[tuple[str, dict[str, Any]]]:
        root_id = self.resolve_id(root_id_or_prefix)
        data = self._read()
        visited: set[str] = set()
        order: list[tuple[str, dict]] = []
        queue = deque([root_id])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            if current not in data["nodes"]:
                continue
            order.append((current, data["nodes"][current]))
            for edge in data["edges"].values():
                if edge["to_id"] == current:
                    queue.append(edge["from_id"])
        return order

    def subgraph(self, root_id_or_prefix: str) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
        root_id = self.resolve_id(root_id_or_prefix)
        data = self._read()
        visited: set[str] = set()
        queue = deque([root_id])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in data["edges"].values():
                if edge["from_id"] == current or edge["to_id"] == current:
                    queue.append(edge["from_id"])
                    queue.append(edge["to_id"])

        nodes = [(node_id, data["nodes"][node_id]) for node_id in visited if node_id in data["nodes"]]
        nodes.sort(key=lambda item: item[1]["created_at"])
        edges = [
            {"edge_id": edge_id, **edge}
            for edge_id, edge in data["edges"].items()
            if edge["from_id"] in visited and edge["to_id"] in visited
        ]
        edges.sort(key=lambda item: item["created_at"])
        return nodes, edges

    def export_lineage(self, node_id_or_prefix: str) -> dict[str, Any]:
        root_id = self.resolve_id(node_id_or_prefix)
        lineage_ids = {node_id for node_id, _ in self.lineage(root_id)}
        data = self._read()
        return {
            "root_id": root_id,
            "nodes": {node_id: data["nodes"][node_id] for node_id in sorted(lineage_ids)},
            "edges": {
                edge_id: edge
                for edge_id, edge in data["edges"].items()
                if edge["from_id"] in lineage_ids and edge["to_id"] in lineage_ids
            },
        }

    def _validate_node(self, node: Node) -> None:
        if node.node_type not in self.node_types:
            raise ValueError(f"unknown node_type: {node.node_type}")

    def _validate_edge(self, edge: Edge) -> None:
        if edge.relation not in self.edge_relations:
            raise ValueError(f"unknown edge relation: {edge.relation}")
