from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Literal, get_args

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

# The valid stored statuses, derived from NodeStatus so there is one source of
# truth. Used as the write-gate whitelist (GraphStore._validate_node) so no front
# end can persist an off-vocabulary status word.
DEFAULT_NODE_STATUSES: frozenset[str] = frozenset(get_args(NodeStatus))


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
        if self.confidence is not None:
            # Type guard BEFORE the range comparison: a non-numeric confidence
            # (e.g. a hostile "high") would make `0.0 <= confidence` raise an
            # uncaught TypeError; fail closed with the documented ValueError.
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise ValueError(
                    f"confidence must be a number between 0.0 and 1.0, got {self.confidence!r}"
                )
            if not (0.0 <= self.confidence <= 1.0):
                raise ValueError(
                    f"confidence must be between 0.0 and 1.0, got {self.confidence}"
                )

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
