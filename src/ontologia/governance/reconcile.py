"""Public governance import, reconciliation, and self-image set adapters.

The registry owns durable identity and reviewed relationships.  These adapters
translate the public lineage contract into that substrate and export one
deterministic self-image for every registered entity.  Source bodies remain in
their custody owner; only immutable references and hashes are persisted here.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ontologia.governance.memory import (
    AuthorityClass,
    AuthorityEdge,
    AuthorityLane,
    AuthorityNode,
    EvidenceSpan,
    ReviewedEdgeType,
    ReviewState,
    canonical_json,
    content_digest,
)
from ontologia.registry.store import RegistryStore

LINEAGE_CONTRACT = "lineage-graph.v1"
SELF_IMAGE_SET_CONTRACT = "node-self-image-set.v1"
RECONCILIATION_RECEIPT_CONTRACT = "governance-reconciliation-receipt.v1"
SNAPSHOT_BUNDLE_CONTRACT = "governance-snapshot-bundle.v1"
_SNAPSHOT_ARTIFACT_FIELDS = ("lineage_graph", "governance_testament")
_PUBLIC_METADATA_KEYS = {
    "active",
    "authority_event_references",
    "entity_id",
    "ideal_form_id",
    "native_id",
    "parent_node_id",
    "predicate_receipts",
    "source_family",
    "zoom_level",
}
_SOURCE_ID_PATTERN = re.compile(r"^src_[A-Za-z0-9_-]+$")
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_LANES = {"operator_intent", "artifact"}
_AUTHORITY_CLASSES = {
    "operator_intent",
    "artifact",
    "transport_echo",
    "system_metadata",
    "unknown",
}
_NODE_TYPES = {
    "ask",
    "correction",
    "constraint",
    "acceptance_criterion",
    "human_gate",
    "plan",
    "brainstorm",
    "specification",
    "document",
    "commit",
    "issue",
    "pull_request",
    "implementation",
    "receipt",
    "source_event",
    "ideal_form",
}
_ARTIFACT_NODE_TYPES = {
    "plan",
    "brainstorm",
    "specification",
    "document",
    "commit",
    "issue",
    "pull_request",
    "implementation",
    "receipt",
}
_NODE_KEYS = {
    "node_id",
    "lane",
    "node_type",
    "source_envelope_id",
    "occurred_at",
    "authority_class",
    "summary",
    "content_hash",
    "review_state",
    "metadata",
}
_EDGE_KEYS = {
    "edge_id",
    "from_node",
    "to_node",
    "edge_type",
    "evidence_spans",
    "confidence",
    "review_state",
    "reviewer_reference",
}
_EDGE_SPAN_KEYS = {
    "source_envelope_id",
    "reference",
    "body_hash",
    "start_offset",
    "end_offset",
}

_NODE_TYPE_TO_CLASS: dict[tuple[str, str], AuthorityClass] = {
    ("operator_intent", "ask"): AuthorityClass.OPERATOR_ASK,
    ("operator_intent", "correction"): AuthorityClass.OPERATOR_CORRECTION,
    ("operator_intent", "constraint"): AuthorityClass.OPERATOR_CONSTRAINT,
    (
        "operator_intent",
        "acceptance_criterion",
    ): AuthorityClass.OPERATOR_ACCEPTANCE_CRITERION,
    ("operator_intent", "human_gate"): AuthorityClass.OPERATOR_HUMAN_GATE,
    ("operator_intent", "source_event"): AuthorityClass.OPERATOR_DIRECTIVE,
    ("artifact", "plan"): AuthorityClass.ASSISTANT_PLAN,
    ("artifact", "brainstorm"): AuthorityClass.BRAINSTORM,
    ("artifact", "specification"): AuthorityClass.SPECIFICATION,
    ("artifact", "implementation"): AuthorityClass.IMPLEMENTATION,
    ("artifact", "receipt"): AuthorityClass.RECEIPT,
    ("artifact", "source_event"): AuthorityClass.SOURCE_DOCUMENT,
}

_CLASS_TO_NODE_TYPE: dict[AuthorityClass, str] = {
    AuthorityClass.OPERATOR_ASK: "ask",
    AuthorityClass.OPERATOR_CORRECTION: "correction",
    AuthorityClass.OPERATOR_CONSTRAINT: "constraint",
    AuthorityClass.OPERATOR_ACCEPTANCE_CRITERION: "acceptance_criterion",
    AuthorityClass.OPERATOR_HUMAN_GATE: "human_gate",
    AuthorityClass.OPERATOR_ADOPTION: "source_event",
    AuthorityClass.OPERATOR_DIRECTIVE: "source_event",
    AuthorityClass.ASSISTANT_RESPONSE: "document",
    AuthorityClass.ASSISTANT_PLAN: "plan",
    AuthorityClass.BRAINSTORM: "brainstorm",
    AuthorityClass.SPECIFICATION: "specification",
    AuthorityClass.IMPLEMENTATION: "implementation",
    AuthorityClass.RECEIPT: "receipt",
    AuthorityClass.TOOL_ECHO: "source_event",
    AuthorityClass.CONTINUATION_SUMMARY: "document",
    AuthorityClass.TRANSPORT_ECHO: "source_event",
    AuthorityClass.MEMORY_SUMMARY: "document",
    AuthorityClass.SOURCE_DOCUMENT: "document",
}


def _required_text(value: Mapping[str, Any], field_name: str) -> str:
    field = value.get(field_name)
    if not isinstance(field, str) or not field:
        raise ValueError(f"missing_{field_name}")
    return field


def _required_list(value: Mapping[str, Any], field_name: str) -> list[Any]:
    field = value.get(field_name)
    if not isinstance(field, list):
        raise ValueError(f"invalid_{field_name}")
    return field


def _require_contract(value: Any, contract_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{contract_name} must be an object")
    if value.get("contract_name") != contract_name or value.get("contract_version") != 1:
        raise ValueError(f"expected {contract_name} contract version 1")
    return value


def _load_json(path: Path, max_bytes: int) -> Any:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"governance artifact exceeds max bytes: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_local_reference(reference: str, base_dir: Path) -> tuple[Path, str]:
    file_reference, separator, fragment = reference.partition("#")
    if not file_reference:
        raise ValueError("artifact reference cannot point back into the reference bundle")
    parsed = urlsplit(file_reference)
    if parsed.scheme and parsed.scheme != "file":
        raise ValueError(f"artifact reference requires an external resolver: {parsed.scheme}")
    path = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(file_reference)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(), fragment if separator else ""


def _json_pointer(value: Any, fragment: str) -> Any:
    if not fragment:
        return value
    if not fragment.startswith("/"):
        raise ValueError("artifact JSON pointer must begin with '/'")
    current = value
    for raw_token in fragment.removeprefix("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, Mapping):
            current = current[token]
        else:
            raise ValueError("artifact JSON pointer traverses a scalar")
    return current


def load_materialized_snapshot_bundle(
    path: Path,
    *,
    max_input_bytes: int = 16_777_216,
    max_artifact_bytes: int = 16_777_216,
) -> dict[str, Any]:
    """Resolve local snapshot artifact references only after exact digest checks."""
    raw = _load_json(path, max_input_bytes)
    snapshot = _require_contract(raw, SNAPSHOT_BUNDLE_CONTRACT)
    snapshot_id = _required_text(snapshot, "snapshot_id")
    materialized = deepcopy(dict(snapshot))
    references: dict[str, dict[str, Any]] = {}
    for field_name in _SNAPSHOT_ARTIFACT_FIELDS:
        value = snapshot.get(field_name)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ValueError(f"snapshot field {field_name} must be an object")
        if value.get("contract_version") == 1:
            continue
        contract_name = _required_text(value, "contract_name")
        reference = _required_text(value, "reference")
        expected_digest = _required_text(value, "digest")
        if value.get("snapshot_id") != snapshot_id:
            raise ValueError(f"{field_name} reference snapshot does not match bundle")
        artifact_path, fragment = _resolve_local_reference(reference, path.parent)
        artifact = _json_pointer(
            _load_json(artifact_path, max_artifact_bytes),
            fragment,
        )
        if not isinstance(artifact, Mapping):
            raise ValueError(f"{field_name} artifact must be an object")
        if artifact.get("contract_name") != contract_name:
            raise ValueError(f"{field_name} artifact contract mismatch")
        if content_digest(artifact) != expected_digest:
            raise ValueError(f"{field_name} artifact digest mismatch")
        references[field_name] = deepcopy(dict(value))
        materialized[field_name] = deepcopy(dict(artifact))
    materialized["_artifact_references"] = references
    materialized["_snapshot_bundle_digest"] = content_digest(snapshot)
    return materialized


def validate_lineage_graph(value: Any, *, snapshot_id: str | None = None) -> Mapping[str, Any]:
    """Validate the public lineage header and strict reconciliation semantics."""
    graph = _require_contract(value, LINEAGE_CONTRACT)
    for field_name in ("graph_id", "generated_at", "frozen_snapshot_id"):
        _required_text(graph, field_name)
    if snapshot_id is not None and graph["frozen_snapshot_id"] != snapshot_id:
        raise ValueError("lineage graph frozen_snapshot_id does not match snapshot")
    nodes = _required_list(graph, "nodes")
    edges = _required_list(graph, "edges")
    if not nodes:
        raise ValueError("lineage graph nodes must be non-empty")

    node_ids: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ValueError("lineage node must be an object")
        if set(node) - _NODE_KEYS:
            raise ValueError("lineage node contains unsupported public fields")
        node_ids.append(_required_text(node, "node_id"))
        for field_name in (
            "lane",
            "node_type",
            "source_envelope_id",
            "occurred_at",
            "authority_class",
            "summary",
            "content_hash",
            "review_state",
        ):
            _required_text(node, field_name)
        if not _SOURCE_ID_PATTERN.fullmatch(str(node["source_envelope_id"])):
            raise ValueError("lineage node source_envelope_id is not schema-valid")
        if not _DIGEST_PATTERN.fullmatch(str(node["content_hash"])):
            raise ValueError("lineage node content_hash is not schema-valid")
        lane = str(node["lane"])
        node_type = str(node["node_type"])
        authority_class = str(node["authority_class"])
        if lane not in _LANES or node_type not in _NODE_TYPES:
            raise ValueError("lineage node lane or node_type is not schema-valid")
        if authority_class not in _AUTHORITY_CLASSES:
            raise ValueError("lineage node authority_class is not schema-valid")
        if lane == "operator_intent" and authority_class != "operator_intent":
            raise ValueError("operator lineage node lacks operator authority")
        if node_type in _ARTIFACT_NODE_TYPES and lane != "artifact":
            raise ValueError("artifact lineage node is in the operator lane")
        if node["review_state"] not in {state.value for state in ReviewState}:
            raise ValueError("lineage node review_state is not schema-valid")
        if "metadata" in node and not isinstance(node["metadata"], Mapping):
            raise ValueError("lineage node metadata must be an object")
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("lineage node_id values must be unique")

    known = set(node_ids)
    edge_ids: list[str] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ValueError("lineage edge must be an object")
        if set(edge) - _EDGE_KEYS:
            raise ValueError("lineage edge contains unsupported public fields")
        edge_ids.append(_required_text(edge, "edge_id"))
        source = _required_text(edge, "from_node")
        target = _required_text(edge, "to_node")
        if source not in known or target not in known:
            raise ValueError("lineage edge endpoint is not registered")
        edge_type = _required_text(edge, "edge_type")
        review_state = _required_text(edge, "review_state")
        if edge_type not in {item.value for item in ReviewedEdgeType}:
            raise ValueError("lineage edge type is not schema-valid")
        if review_state not in {state.value for state in ReviewState}:
            raise ValueError("lineage edge review_state is not schema-valid")
        confidence = edge.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("lineage edge confidence is not schema-valid")
        evidence_spans = _required_list(edge, "evidence_spans")
        if not evidence_spans:
            raise ValueError("lineage edge evidence_spans must be non-empty")
        for span in evidence_spans:
            if not isinstance(span, Mapping):
                raise ValueError("lineage edge evidence span must be an object")
            if set(span) - _EDGE_SPAN_KEYS:
                raise ValueError("lineage edge evidence span has unsupported fields")
            source_id = _required_text(span, "source_envelope_id")
            if not _SOURCE_ID_PATTERN.fullmatch(source_id):
                raise ValueError("lineage edge source_envelope_id is not schema-valid")
            _required_text(span, "reference")
            body_hash = _required_text(span, "body_hash")
            if not _DIGEST_PATTERN.fullmatch(body_hash):
                raise ValueError("lineage edge body_hash is not schema-valid")
            for offset_name in ("start_offset", "end_offset"):
                offset = span.get(offset_name)
                if offset is not None and (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                ):
                    raise ValueError("lineage edge offset is not schema-valid")
    if len(edge_ids) != len(set(edge_ids)):
        raise ValueError("lineage edge_id values must be unique")
    return graph


def _authority_class(node: Mapping[str, Any]) -> AuthorityClass:
    declared = str(node["authority_class"])
    try:
        return AuthorityClass(declared)
    except ValueError:
        lane = str(node["lane"])
        node_type = str(node["node_type"])
        if declared == "transport_echo":
            return AuthorityClass.TRANSPORT_ECHO
        if declared in {"system_metadata", "unknown"} and lane == "artifact":
            return AuthorityClass.SOURCE_DOCUMENT
        if declared == "artifact" and node_type == "document":
            return AuthorityClass.SOURCE_DOCUMENT
        if declared == "operator_intent" and (lane, node_type) not in _NODE_TYPE_TO_CLASS:
            return AuthorityClass.OPERATOR_DIRECTIVE
        if declared == "artifact" and (lane, node_type) not in _NODE_TYPE_TO_CLASS:
            return AuthorityClass.SOURCE_DOCUMENT
        try:
            return _NODE_TYPE_TO_CLASS[(lane, node_type)]
        except KeyError as error:
            raise ValueError("unsupported authority class and node type") from error


def _evidence_spans(
    value: Mapping[str, Any],
    *,
    snapshot_id: str,
    fallback_source_id: str,
    fallback_hash: str,
) -> list[EvidenceSpan]:
    raw_spans = value.get("evidence_spans", [])
    if not isinstance(raw_spans, list):
        raise ValueError("invalid_evidence_spans")
    if not raw_spans:
        return [
            EvidenceSpan(
                source_id=fallback_source_id,
                body_hash=fallback_hash,
                snapshot_id=snapshot_id,
            ),
        ]
    spans: list[EvidenceSpan] = []
    for raw in raw_spans:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid_evidence_span")
        source_id = str(raw.get("source_envelope_id") or raw.get("source_id") or "")
        body_hash = str(raw.get("body_hash") or fallback_hash)
        if not source_id or not body_hash:
            raise ValueError("evidence span requires source envelope and body hash")
        spans.append(
            EvidenceSpan(
                source_id=source_id,
                body_hash=body_hash,
                snapshot_id=snapshot_id,
                start_offset=raw.get("start_offset"),
                end_offset=raw.get("end_offset"),
                independence_group=raw.get("independence_group"),
            ),
        )
    return spans


@dataclass(frozen=True)
class ImportResult:
    """Deterministic classification of every public lineage unit."""

    imported_node_ids: tuple[str, ...]
    imported_edge_ids: tuple[str, ...]
    unresolved: tuple[dict[str, str], ...]

    @property
    def ready(self) -> bool:
        return not self.unresolved


def import_lineage_graph(
    store: RegistryStore,
    value: Any,
    *,
    snapshot_id: str | None = None,
) -> ImportResult:
    """Import reviewed public lineage into the append-only registry substrate."""
    graph = validate_lineage_graph(value, snapshot_id=snapshot_id)
    frozen_snapshot_id = str(graph["frozen_snapshot_id"])
    imported_nodes: list[str] = []
    imported_edges: list[str] = []
    unresolved: list[dict[str, str]] = []

    for raw_node in graph["nodes"]:
        node = dict(raw_node)
        review_state = str(node["review_state"])
        if review_state != ReviewState.REVIEWED.value:
            unresolved.append(
                {
                    "unit_type": "node",
                    "unit_id": str(node["node_id"]),
                    "reason": "node_not_reviewed",
                },
            )
            continue
        lane = AuthorityLane(str(node["lane"]))
        source_metadata = dict(node.get("metadata", {}))
        metadata = {
            key: deepcopy(source_metadata[key])
            for key in sorted(_PUBLIC_METADATA_KEYS)
            if key in source_metadata
        }
        metadata.update(
            {
                "node_type": str(node["node_type"]),
                "source_envelope_id": str(node["source_envelope_id"]),
                "declared_authority_class": str(node["authority_class"]),
                "summary": str(node["summary"]),
                "review_state": review_state,
            },
        )
        authority_node = AuthorityNode(
            node_id=str(node["node_id"]),
            lane=lane,
            authority_class=_authority_class(node),
            source_family=str(metadata.get("source_family", "governance-snapshot")),
            source_instance=str(node["source_envelope_id"]),
            native_id=str(metadata.get("native_id", node["node_id"])),
            observed_at=str(node["occurred_at"]),
            body_hash=str(node["content_hash"]),
            evidence=_evidence_spans(
                node,
                snapshot_id=frozen_snapshot_id,
                fallback_source_id=str(node["source_envelope_id"]),
                fallback_hash=str(node["content_hash"]),
            ),
            entity_id=metadata.get("entity_id"),
            parent_id=metadata.get("parent_node_id"),
            zoom_level=str(metadata.get("zoom_level", "atom")),
            metadata=metadata,
        )
        store.add_authority_node(authority_node)
        imported_nodes.append(authority_node.node_id)

    available_nodes = {node.node_id for node in store.authority_graph.nodes()}
    node_times = {str(node["node_id"]): str(node["occurred_at"]) for node in graph["nodes"]}
    for raw_edge in graph["edges"]:
        edge = dict(raw_edge)
        edge_id = str(edge["edge_id"])
        if str(edge["review_state"]) != ReviewState.REVIEWED.value:
            unresolved.append(
                {"unit_type": "edge", "unit_id": edge_id, "reason": "edge_not_reviewed"},
            )
            continue
        source_id = str(edge["from_node"])
        target_id = str(edge["to_node"])
        if source_id not in available_nodes or target_id not in available_nodes:
            unresolved.append(
                {
                    "unit_type": "edge",
                    "unit_id": edge_id,
                    "reason": "reviewed_endpoint_not_imported",
                },
            )
            continue
        spans = _evidence_spans(
            edge,
            snapshot_id=frozen_snapshot_id,
            fallback_source_id=f"lineage-edge:{edge_id}",
            fallback_hash=content_digest(edge),
        )
        authority_edge = AuthorityEdge(
            edge_id=edge_id,
            source_node_id=source_id,
            target_node_id=target_id,
            edge_type=ReviewedEdgeType(str(edge["edge_type"]).lower()),
            recorded_at=str(edge.get("recorded_at") or node_times[source_id]),
            evidence=spans,
            confidence=float(edge["confidence"]),
            review_state=ReviewState.REVIEWED,
            reviewer=str(edge.get("reviewer_reference") or edge.get("reviewer") or ""),
            metadata={},
        )
        store.add_authority_edge(authority_edge)
        imported_edges.append(authority_edge.edge_id)

    return ImportResult(
        imported_node_ids=tuple(sorted(imported_nodes)),
        imported_edge_ids=tuple(sorted(imported_edges)),
        unresolved=tuple(sorted(unresolved, key=canonical_json)),
    )


def export_lineage_graph(
    store: RegistryStore,
    *,
    snapshot_id: str,
    generated_at: str,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """Export the persisted reviewed graph using the public lineage contract."""
    nodes: list[dict[str, Any]] = []
    for node in store.authority_graph.nodes():
        metadata = dict(node.metadata)
        source_envelope_id = str(
            metadata.get("source_envelope_id")
            or (node.evidence[0].source_id if node.evidence else node.source_instance),
        )
        node_type = str(metadata.get("node_type") or _CLASS_TO_NODE_TYPE[node.authority_class])
        declared_authority_class = metadata.get("declared_authority_class")
        if declared_authority_class in _AUTHORITY_CLASSES:
            authority_class = str(declared_authority_class)
        elif node.authority_class in {
            AuthorityClass.TRANSPORT_ECHO,
            AuthorityClass.TOOL_ECHO,
        }:
            authority_class = "transport_echo"
        else:
            authority_class = node.lane.value
        public_metadata = {
            **{
                key: deepcopy(metadata[key])
                for key in sorted(_PUBLIC_METADATA_KEYS)
                if key in metadata
            },
            "zoom_level": node.zoom_level,
            "source_family": node.source_family,
            "native_id": node.native_id,
        }
        if node.entity_id is not None:
            public_metadata["entity_id"] = node.entity_id
        if node.parent_id is not None:
            public_metadata["parent_node_id"] = node.parent_id
        nodes.append(
            {
                "node_id": node.node_id,
                "lane": node.lane.value,
                "node_type": node_type,
                "source_envelope_id": source_envelope_id,
                "occurred_at": node.observed_at,
                "authority_class": authority_class,
                "summary": str(metadata.get("summary", f"Reviewed governance node {node.node_id}.")),
                "content_hash": node.body_hash,
                "review_state": str(metadata.get("review_state", ReviewState.REVIEWED.value)),
                "metadata": public_metadata,
            },
        )

    edges = []
    for edge in store.authority_graph.edges():
        public_edge = {
            "edge_id": edge.edge_id,
            "from_node": edge.source_node_id,
            "to_node": edge.target_node_id,
            "edge_type": edge.edge_type.value,
            "evidence_spans": [
                {
                    "source_envelope_id": span.source_id,
                    "reference": f"source-envelope:{span.source_id}",
                    "body_hash": span.body_hash,
                }
                for span in edge.evidence
            ],
            "confidence": edge.confidence,
            "review_state": edge.review_state.value,
        }
        if edge.reviewer:
            public_edge["reviewer_reference"] = edge.reviewer
        edges.append(public_edge)
    graph = {
        "contract_name": LINEAGE_CONTRACT,
        "contract_version": 1,
        "graph_id": graph_id or f"lineage:{snapshot_id}",
        "generated_at": generated_at,
        "frozen_snapshot_id": snapshot_id,
        "nodes": nodes,
        "edges": edges,
    }
    validate_lineage_graph(graph, snapshot_id=snapshot_id)
    return graph


def build_self_image_set(
    store: RegistryStore,
    *,
    snapshot_id: str,
    snapshot_digest: str,
    reconciled_at: str,
    constitutional_digest: str,
) -> dict[str, Any]:
    """Export exactly one deterministic self-image for every registered entity."""
    registered_node_ids = sorted(entity.uid for entity in store.list_entities())
    if not registered_node_ids:
        raise ValueError("node self-image set requires at least one registered node")
    images = [
        store.node_self_image(
            node_id,
            constitutional_digest=constitutional_digest,
            last_reconciled_at=reconciled_at,
        ).to_dict()
        for node_id in registered_node_ids
    ]
    for image in images:
        if not image["observations"]:
            raise ValueError(f"registered node {image['node_id']} has no traceable observations")
        if not image["active_ideal_forms"]:
            raise ValueError(f"registered node {image['node_id']} has no active ideal forms")
    image_node_ids = [str(image["node_id"]) for image in images]
    exact_one = (
        len(image_node_ids) == len(set(image_node_ids))
        and sorted(image_node_ids) == registered_node_ids
    )
    if not exact_one:
        raise ValueError("self-image coverage is not exact-one")
    registry_projection = [
        entity.to_dict()
        for entity in sorted(store.list_entities(), key=lambda entity: entity.uid)
    ]
    body = {
        "contract_name": SELF_IMAGE_SET_CONTRACT,
        "contract_version": 1,
        "set_id": f"self-images:{snapshot_id}",
        "snapshot_id": snapshot_id,
        "snapshot_digest": snapshot_digest,
        "registry_reference": "registry:ontologia",
        "registry_digest": content_digest(registry_projection),
        "registered_node_ids": registered_node_ids,
        "self_images": images,
        "counts": {
            "registered": len(registered_node_ids),
            "exported": len(images),
        },
        "readiness": {
            "exact_all": True,
            "unresolved_blockers": [],
            "quarantines": [],
            "missing_requirements": [],
            "citation_debt": [],
            "incomplete_predicates": [],
            "ready": True,
            "status": "ready",
        },
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    return {**body, "set_digest": content_digest(body)}


def _append_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    rendered = canonical_json(receipt)
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if rendered in existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(rendered + "\n")


def _write_if_changed(path: Path, value: Any) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def reconcile_snapshot_bundle(
    store: RegistryStore,
    bundle: Any,
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Reconcile one frozen bundle and persist a typed, idempotent receipt."""
    snapshot = _require_contract(bundle, SNAPSHOT_BUNDLE_CONTRACT)
    snapshot_id = _required_text(snapshot, "snapshot_id")
    snapshot_at = _required_text(snapshot, "snapshot_at")
    lineage = snapshot.get("lineage_graph")
    if lineage is None and isinstance(snapshot.get("contracts"), Mapping):
        lineage = snapshot["contracts"].get("lineage_graph")
    lineage_graph = validate_lineage_graph(lineage, snapshot_id=snapshot_id)
    result = import_lineage_graph(store, lineage_graph, snapshot_id=snapshot_id)

    constitutional_digest = snapshot.get("constitutional_digest")
    if not isinstance(constitutional_digest, str) or not constitutional_digest:
        testament = snapshot.get("governance_testament")
        if testament is None and isinstance(snapshot.get("contracts"), Mapping):
            testament = snapshot["contracts"].get("governance_testament")
        if testament is None:
            raise ValueError("snapshot bundle requires constitutional evidence")
        constitutional_digest = content_digest(testament)

    exported_lineage = export_lineage_graph(
        store,
        snapshot_id=snapshot_id,
        generated_at=snapshot_at,
        graph_id=str(lineage_graph["graph_id"]),
    )
    self_image_set = build_self_image_set(
        store,
        snapshot_id=snapshot_id,
        snapshot_digest=str(snapshot.get("snapshot_digest") or content_digest(snapshot)),
        reconciled_at=snapshot_at,
        constitutional_digest=constitutional_digest,
    )
    receipt = {
        "contract_name": RECONCILIATION_RECEIPT_CONTRACT,
        "contract_version": 1,
        "receipt_id": f"ontologia-reconcile:{snapshot_id}",
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "input_digest": str(
            snapshot.get("_snapshot_bundle_digest") or content_digest(snapshot),
        ),
        "lineage_digest": content_digest(exported_lineage),
        "self_image_set_digest": self_image_set["set_digest"],
        "counts": {
            "registered_nodes": len(self_image_set["registered_node_ids"]),
            "self_images": len(self_image_set["self_images"]),
            "lineage_nodes": len(exported_lineage["nodes"]),
            "lineage_edges": len(exported_lineage["edges"]),
            "unresolved": len(result.unresolved),
        },
        "exact_one": self_image_set["readiness"]["exact_all"],
        "unresolved": list(result.unresolved),
        "ready": result.ready and bool(self_image_set["readiness"]["ready"]),
    }
    if not receipt["ready"]:
        raise ValueError("governance reconciliation has unresolved reviewed-lineage debt")

    _write_if_changed(output_dir / "lineage-graph.json", exported_lineage)
    _write_if_changed(output_dir / "node-self-image-set.json", self_image_set)
    _write_if_changed(output_dir / "reconciliation-receipt.json", receipt)
    _append_receipt(store.store_dir / "governance-reconciliations.jsonl", receipt)
    return {
        "lineage_graph": exported_lineage,
        "node_self_image_set": self_image_set,
        "receipt": receipt,
    }
