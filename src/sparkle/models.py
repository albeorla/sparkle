from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal

DEFAULT_NODE_TYPES = {
    "claim",
    "evidence",
    "question",
    "objection",
    "inference",
    "decision",
    "synthesis",
}

DEFAULT_EDGE_RELATIONS = {
    "supports",
    "contradicts",
    "refines",
    "derived_from",
    "evaluates",
    "produced",
    "supersedes",
}

NodeType = Literal["claim", "evidence", "question", "objection", "inference", "decision", "synthesis"]

NodeStatus = Literal[
    "active",
    "stalled",
    "weakly_supported",
    "promising",
    "abandoned",
    "harvested",
    "ratified",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Node:
    node_type: str
    title: str
    content: str
    citations: list[str] = field(default_factory=list)
    author: str = "local"
    created_at: str = field(default_factory=utc_now_iso)
    confidence: float | None = 0.5
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def compute_id(self) -> str:
        canonical = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    relation: str
    note: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def compute_id(self) -> str:
        canonical = json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
