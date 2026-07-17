"""Authority-qualified governance memory and deterministic self-images.

The structural registry remains the owner of entity identity, topology, and
events.  This module adds the schema-shaped records needed to keep operator
intent distinct from generated artifacts without replacing those substrates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

import rfc8785


def canonical_json(value: Any) -> str:
    """Return RFC 8785 canonical JSON used by all governed digests."""
    return rfc8785.dumps(value).decode("utf-8")


def content_digest(value: Any) -> str:
    """Hash a JSON-compatible value with an explicit algorithm prefix."""
    payload = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class AuthorityLane(str, Enum):
    """The two authority lanes in the governance corpus."""

    OPERATOR_INTENT = "operator_intent"
    ARTIFACT = "artifact"


class AuthorityClass(str, Enum):
    """Role-aware authority classes; text equality never changes the class."""

    OPERATOR_ASK = "operator_ask"
    OPERATOR_CORRECTION = "operator_correction"
    OPERATOR_CONSTRAINT = "operator_constraint"
    OPERATOR_ACCEPTANCE_CRITERION = "operator_acceptance_criterion"
    OPERATOR_HUMAN_GATE = "operator_human_gate"
    OPERATOR_ADOPTION = "operator_adoption"
    OPERATOR_DIRECTIVE = "operator_directive"

    ASSISTANT_RESPONSE = "assistant_response"
    ASSISTANT_PLAN = "assistant_plan"
    BRAINSTORM = "brainstorm"
    SPECIFICATION = "specification"
    IMPLEMENTATION = "implementation"
    RECEIPT = "receipt"
    TOOL_ECHO = "tool_echo"
    CONTINUATION_SUMMARY = "continuation_summary"
    TRANSPORT_ECHO = "transport_echo"
    MEMORY_SUMMARY = "memory_summary"
    SOURCE_DOCUMENT = "source_document"


_INTENT_CLASSES = {
    AuthorityClass.OPERATOR_ASK,
    AuthorityClass.OPERATOR_CORRECTION,
    AuthorityClass.OPERATOR_CONSTRAINT,
    AuthorityClass.OPERATOR_ACCEPTANCE_CRITERION,
    AuthorityClass.OPERATOR_HUMAN_GATE,
    AuthorityClass.OPERATOR_ADOPTION,
    AuthorityClass.OPERATOR_DIRECTIVE,
}


class ReviewedEdgeType(str, Enum):
    """Complete reviewed lineage vocabulary for intent and artifact graphs."""

    EXACT_DUPLICATE = "exact_duplicate"
    TRANSPORT_ECHO = "transport_echo"
    QUOTES = "quotes"
    REFERENCES = "references"
    REFINES = "refines"
    CORRECTS = "corrects"
    SUPERSEDES = "supersedes"
    SPLITS = "splits"
    MERGES = "merges"
    CONTRADICTS = "contradicts"
    IMPLEMENTS = "implements"
    ADOPTS = "adopts"


class ReviewState(str, Enum):
    """Human/machine review state for a proposed lineage assertion."""

    UNREVIEWED = "unreviewed"
    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class EvidenceSpan:
    """Immutable evidence pointer; source bodies remain in their custody owner."""

    source_id: str
    body_hash: str
    snapshot_id: str
    start_offset: int | None = None
    end_offset: int | None = None
    independence_group: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.body_hash or not self.snapshot_id:
            raise ValueError("evidence requires source_id, body_hash, and snapshot_id")
        if self.start_offset is not None and self.start_offset < 0:
            raise ValueError("start_offset cannot be negative")
        if self.end_offset is not None:
            if self.end_offset < 0:
                raise ValueError("end_offset cannot be negative")
            if self.start_offset is not None and self.end_offset < self.start_offset:
                raise ValueError("end_offset cannot precede start_offset")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_id": self.source_id,
            "body_hash": self.body_hash,
            "snapshot_id": self.snapshot_id,
        }
        if self.start_offset is not None:
            result["start_offset"] = self.start_offset
        if self.end_offset is not None:
            result["end_offset"] = self.end_offset
        if self.independence_group is not None:
            result["independence_group"] = self.independence_group
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceSpan:
        return cls(
            source_id=data["source_id"],
            body_hash=data["body_hash"],
            snapshot_id=data["snapshot_id"],
            start_offset=data.get("start_offset"),
            end_offset=data.get("end_offset"),
            independence_group=data.get("independence_group"),
        )


@dataclass
class AuthorityNode:
    """One source atom on exactly one authority lane."""

    node_id: str
    lane: AuthorityLane
    authority_class: AuthorityClass
    source_family: str
    source_instance: str
    native_id: str
    observed_at: str
    body_hash: str
    evidence: list[EvidenceSpan] = field(default_factory=list)
    entity_id: str | None = None
    parent_id: str | None = None
    zoom_level: str = "atom"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not self.node_id
            or not self.source_family
            or not self.source_instance
            or not self.native_id
            or not self.observed_at
            or not self.body_hash
        ):
            raise ValueError("authority node requires stable identity, native time, and body hash")
        is_intent = self.authority_class in _INTENT_CLASSES
        if is_intent != (self.lane == AuthorityLane.OPERATOR_INTENT):
            raise ValueError(
                f"authority class {self.authority_class.value} is incompatible with lane "
                f"{self.lane.value}",
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_id": "lineage-graph.v1/node",
            "node_id": self.node_id,
            "lane": self.lane.value,
            "authority_class": self.authority_class.value,
            "source_family": self.source_family,
            "source_instance": self.source_instance,
            "native_id": self.native_id,
            "observed_at": self.observed_at,
            "body_hash": self.body_hash,
            "zoom_level": self.zoom_level,
            "evidence": [span.to_dict() for span in self.evidence],
            "metadata": self.metadata,
        }
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        if self.parent_id is not None:
            result["parent_id"] = self.parent_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthorityNode:
        return cls(
            node_id=data["node_id"],
            lane=AuthorityLane(data["lane"]),
            authority_class=AuthorityClass(data["authority_class"]),
            source_family=data["source_family"],
            source_instance=data["source_instance"],
            native_id=data["native_id"],
            observed_at=data["observed_at"],
            body_hash=data["body_hash"],
            evidence=[EvidenceSpan.from_dict(item) for item in data.get("evidence", [])],
            entity_id=data.get("entity_id"),
            parent_id=data.get("parent_id"),
            zoom_level=data.get("zoom_level", "atom"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class AuthorityEdge:
    """Reviewed, evidence-backed relationship between governance nodes."""

    source_node_id: str
    target_node_id: str
    edge_type: ReviewedEdgeType
    recorded_at: str
    evidence: list[EvidenceSpan]
    confidence: float
    review_state: ReviewState
    reviewer: str | None = None
    edge_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_node_id or not self.target_node_id or not self.recorded_at:
            raise ValueError("authority edge requires source, target, and native recorded_at")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if self.review_state == ReviewState.REVIEWED and not self.reviewer:
            raise ValueError("reviewed edges require a reviewer")
        if self.review_state == ReviewState.REVIEWED and not self.evidence:
            raise ValueError("reviewed edges require evidence")
        if not self.edge_id:
            identity = {
                "source_node_id": self.source_node_id,
                "target_node_id": self.target_node_id,
                "edge_type": self.edge_type.value,
                "recorded_at": self.recorded_at,
                "evidence": [span.to_dict() for span in self.evidence],
                "confidence": self.confidence,
                "review_state": self.review_state.value,
                "reviewer": self.reviewer,
                "metadata": self.metadata,
            }
            self.edge_id = content_digest(identity)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_id": "lineage-graph.v1/edge",
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "edge_type": self.edge_type.value,
            "recorded_at": self.recorded_at,
            "evidence": [span.to_dict() for span in self.evidence],
            "confidence": self.confidence,
            "review_state": self.review_state.value,
            "metadata": self.metadata,
        }
        if self.reviewer is not None:
            result["reviewer"] = self.reviewer
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthorityEdge:
        return cls(
            edge_id=data.get("edge_id", ""),
            source_node_id=data["source_node_id"],
            target_node_id=data["target_node_id"],
            edge_type=ReviewedEdgeType(data["edge_type"]),
            recorded_at=data["recorded_at"],
            evidence=[EvidenceSpan.from_dict(item) for item in data.get("evidence", [])],
            confidence=float(data["confidence"]),
            review_state=ReviewState(data["review_state"]),
            reviewer=data.get("reviewer"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class AuthorityGraphIndex:
    """In-memory dual-lane graph with deterministic iteration."""

    _nodes: dict[str, AuthorityNode] = field(default_factory=dict)
    _edges: dict[str, AuthorityEdge] = field(default_factory=dict)

    def add_node(self, node: AuthorityNode) -> bool:
        current = self._nodes.get(node.node_id)
        if current is not None:
            if current.to_dict() != node.to_dict():
                raise ValueError(f"conflicting authority node: {node.node_id}")
            return False
        self._nodes[node.node_id] = node
        return True

    def add_edge(self, edge: AuthorityEdge) -> bool:
        if edge.source_node_id not in self._nodes or edge.target_node_id not in self._nodes:
            raise ValueError("authority edge endpoints must exist before the edge is added")
        current = self._edges.get(edge.edge_id)
        if current is not None:
            if current.to_dict() != edge.to_dict():
                raise ValueError(f"conflicting authority edge: {edge.edge_id}")
            return False
        self._edges[edge.edge_id] = edge
        return True

    def get_node(self, node_id: str) -> AuthorityNode | None:
        return self._nodes.get(node_id)

    def nodes(self, lane: AuthorityLane | None = None) -> list[AuthorityNode]:
        nodes = self._nodes.values()
        if lane is not None:
            nodes = (node for node in nodes if node.lane == lane)
        return sorted(nodes, key=lambda node: (node.observed_at, node.node_id))

    def edges(self) -> list[AuthorityEdge]:
        return sorted(
            self._edges.values(),
            key=lambda edge: (edge.recorded_at, edge.edge_type.value, edge.edge_id),
        )

    def incoming(self, node_id: str) -> list[AuthorityEdge]:
        return [edge for edge in self.edges() if edge.target_node_id == node_id]

    def outgoing(self, node_id: str) -> list[AuthorityEdge]:
        return [edge for edge in self.edges() if edge.source_node_id == node_id]

    def nodes_for_entity(self, entity_id: str) -> list[AuthorityNode]:
        return [node for node in self.nodes() if node.entity_id == entity_id]


@dataclass(frozen=True)
class QuarantineDiagnostic:
    """Non-sensitive diagnostic for malformed source material."""

    diagnostic_id: str
    source_path: str
    record_hash: str
    error_type: str
    line_number: int | None = None

    @classmethod
    def from_failure(
        cls,
        source_path: str,
        raw_record: str,
        error: Exception,
        line_number: int | None = None,
    ) -> QuarantineDiagnostic:
        record_hash = f"sha256:{hashlib.sha256(raw_record.encode('utf-8')).hexdigest()}"
        identity = {
            "source_path": source_path,
            "record_hash": record_hash,
            "error_type": type(error).__name__,
            "line_number": line_number,
        }
        return cls(
            diagnostic_id=content_digest(identity),
            source_path=source_path,
            record_hash=record_hash,
            error_type=type(error).__name__,
            line_number=line_number,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_id": "coverage-receipt.v1/quarantine",
            "diagnostic_id": self.diagnostic_id,
            "source_path": self.source_path,
            "record_hash": self.record_hash,
            "error_type": self.error_type,
        }
        if self.line_number is not None:
            result["line_number"] = self.line_number
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuarantineDiagnostic:
        return cls(
            diagnostic_id=data["diagnostic_id"],
            source_path=data["source_path"],
            record_hash=data["record_hash"],
            error_type=data["error_type"],
            line_number=data.get("line_number"),
        )


@dataclass(frozen=True)
class NodeSelfImage:
    """Public ``node-self-image.v1`` projection for a registered entity."""

    node_id: str
    node_type: str
    owner_reference: str
    relations: dict[str, list[dict[str, Any]]]
    cursors: dict[str, str | None]
    digests: dict[str, str]
    observations: list[dict[str, Any]]
    active_ideal_forms: list[dict[str, Any]]
    reconciled_at: str
    evidence_references: list[str]
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contract_name": "node-self-image.v1",
            "contract_version": 1,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "owner_reference": self.owner_reference,
            "relations": self.relations,
            "cursors": self.cursors,
            "digests": self.digests,
            "observations": self.observations,
            "active_ideal_forms": self.active_ideal_forms,
            "reconciled_at": self.reconciled_at,
            "evidence_references": self.evidence_references,
        }
        if self.display_name is not None:
            body["display_name"] = self.display_name
        return body


def evidence_refs(nodes: Iterable[AuthorityNode]) -> list[dict[str, Any]]:
    """Flatten and deterministically deduplicate node evidence references."""
    refs: dict[str, dict[str, Any]] = {}
    for node in nodes:
        for span in node.evidence:
            item = {"node_id": node.node_id, **span.to_dict()}
            refs[content_digest(item)] = item
    return [refs[key] for key in sorted(refs)]
