"""Public governance import, reconciliation, and self-image set adapters.

The registry owns durable identity and reviewed relationships.  These adapters
translate the public lineage contract into that substrate and export one
deterministic self-image for every registered entity.  Source bodies remain in
their custody owner; only immutable references and hashes are persisted here.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ontologia.entity.identity import EntityType, LifecycleStatus
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
_SNAPSHOT_ARTIFACT_FIELDS = (
    "source_census",
    "lineage_graph",
    "governance_testament",
    "coverage",
    "ideal_form_register",
    "normalization_parity_receipt",
)
_FINAL_BUNDLE_REQUIRED_FIELDS = (
    "bundle_id",
    "generated_at",
    "source_census",
    "normalized_events",
    "source_envelopes",
    "assertion_evidence",
    "lineage_graph",
    "governance_testament",
    "coverage",
    "ideal_form_register",
    "node_self_image_set",
    "iceberg_atlas",
    "normalization_parity_receipt",
    "governance_stage_receipts",
    "governance_cadence_receipts",
    "post_proof_idempotence",
    "governance_atlas_receipt",
    "readiness",
    "bundle_digest",
)
_READINESS_DEBT_FIELDS = (
    "unresolved_blockers",
    "quarantines",
    "missing_requirements",
    "citation_debt",
    "incomplete_predicates",
)
_COVERAGE_STATUSES = (
    "acquired",
    "parsed",
    "quarantined",
    "inaccessible",
    "missing_expected",
    "owner_blocked",
)
_COVERAGE_KEYS = frozenset(
    {
        "contract_name",
        "contract_version",
        "receipt_id",
        "snapshot_id",
        "generated_at",
        "denominator",
        "sources",
        "counts",
        "constitutional_scope",
        "exact_all",
        "ready",
        *_READINESS_DEBT_FIELDS,
        "closure_status",
        "residual_owners",
        "receipt_hash",
    },
)
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
_ENTITY_UID_PATTERN = re.compile(r"^ent_[a-z]+_[0-9A-HJKMNP-TV-Z]{26}$")
_PUBLIC_REGISTRY_NODE_KEYS = {"uid", "entity_type", "lifecycle_status"}
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


def _required_timestamp(value: Mapping[str, Any], field_name: str) -> tuple[str, datetime]:
    text = _required_text(value, field_name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"invalid_{field_name}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid_{field_name}")
    return text, parsed


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
    declared_bundle_digest = snapshot.get("bundle_digest")
    if (
        declared_bundle_digest is not None
        and declared_bundle_digest != _digest_excluding(snapshot, "bundle_digest")
    ):
        raise ValueError("final governance snapshot bundle digest mismatch")
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


@dataclass(frozen=True)
class ReconcileInputs:
    """Exact pre-cadence owner artifacts required by registry reconciliation."""

    snapshot_id: str
    snapshot_digest: str
    snapshot_at: str
    generated_at: str
    lineage_graph: Mapping[str, Any]
    governance_testament: Mapping[str, Any]
    source_census: Mapping[str, Any]
    source_envelopes: tuple[Mapping[str, Any], ...]
    normalized_events: tuple[Mapping[str, Any], ...]
    assertion_evidence: tuple[Mapping[str, Any], ...]
    normalization_parity_receipt: Mapping[str, Any]
    coverage_receipt: Mapping[str, Any]
    readiness: Mapping[str, Any]
    input_digest: str


@dataclass(frozen=True)
class SnapshotEvidenceIndex:
    """Snapshot-bound references that may appear in public self-images."""

    snapshot_id: str
    snapshot_digest: str
    snapshot_at: str
    generated_at: str
    input_digest: str
    source_references: tuple[str, ...]
    event_references: tuple[str, ...]
    assertion_references: tuple[str, ...]
    predicate_receipt_references: tuple[str, ...]

    @property
    def allowed_references(self) -> frozenset[str]:
        return frozenset(
            (
                *self.source_references,
                *self.event_references,
                *self.assertion_references,
                *self.predicate_receipt_references,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_at": self.snapshot_at,
            "generated_at": self.generated_at,
            "input_digest": self.input_digest,
            "source_references": list(self.source_references),
            "event_references": list(self.event_references),
            "assertion_references": list(self.assertion_references),
            "predicate_receipt_references": list(
                self.predicate_receipt_references,
            ),
        }

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())


def _reference_resolves(reference: Any, identifiers: set[str] | frozenset[str]) -> bool:
    return str(reference or "") in identifiers


def _bound_reference_aliases(prefix: str, identifier: str) -> set[str]:
    return {identifier, f"{prefix}:{identifier}"}


def _resolve_bound_value(reference: Any, bindings: Mapping[str, str]) -> str | None:
    return bindings.get(str(reference or ""))


def _digest_excluding(value: Mapping[str, Any], digest_field: str) -> str:
    body = dict(value)
    body.pop(digest_field, None)
    return content_digest(body)


def _normalize_readiness(
    value: Any,
    *,
    label: str,
    status_field: str = "status",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} readiness is invalid")
    exact_all = value.get("exact_all")
    ready = value.get("ready")
    status = value.get(status_field)
    if not isinstance(exact_all, bool) or not isinstance(ready, bool):
        raise ValueError(f"{label} readiness is invalid")
    readiness: dict[str, Any] = {"exact_all": exact_all}
    for field_name in _READINESS_DEBT_FIELDS:
        debt = value.get(field_name)
        if (
            not isinstance(debt, list)
            or len(debt) != len(set(map(str, debt)))
            or not all(isinstance(item, str) and item for item in debt)
        ):
            raise ValueError(f"{label} readiness {field_name} is invalid")
        readiness[field_name] = sorted(debt)
    computed_ready = exact_all and not any(
        readiness[field_name] for field_name in _READINESS_DEBT_FIELDS
    )
    if ready is not computed_ready:
        raise ValueError(f"{label} readiness contradicts its declared debt")
    if status not in {
        "incomplete",
        "blocked",
        "ready",
        "closed_with_owner_routed_debt",
    }:
        raise ValueError(f"{label} readiness status is invalid")
    if (ready and status != "ready") or (not ready and status == "ready"):
        raise ValueError(f"{label} readiness status contradicts ready")
    readiness.update(
        {
            "ready": ready,
            "status": str(status),
        },
    )
    return readiness


def _combined_readiness(
    parity: Mapping[str, Any],
    coverage: Mapping[str, Any],
    assertions: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    parity_readiness = _normalize_readiness(
        parity.get("readiness"),
        label="normalization parity receipt",
    )
    coverage_readiness = _normalize_readiness(
        {
            "exact_all": coverage.get("exact_all"),
            "ready": coverage.get("ready"),
            "status": coverage.get("closure_status"),
            **{
                field_name: coverage.get(field_name)
                for field_name in _READINESS_DEBT_FIELDS
            },
        },
        label="coverage receipt",
    )
    result: dict[str, Any] = {
        "exact_all": bool(
            parity_readiness["exact_all"] and coverage_readiness["exact_all"],
        ),
    }
    for field_name in _READINESS_DEBT_FIELDS:
        result[field_name] = sorted(
            {
                *parity_readiness[field_name],
                *coverage_readiness[field_name],
            },
        )
    for assertion in assertions:
        assertion_id = _required_text(assertion, "assertion_id")
        if assertion.get("verification_state") != "verified":
            assertion_reference = (
                assertion_id
                if assertion_id.startswith("assertion:")
                else f"assertion:{assertion_id}"
            )
            result["citation_debt"] = sorted(
                {*result["citation_debt"], assertion_reference},
            )
            result["incomplete_predicates"] = sorted(
                {
                    *result["incomplete_predicates"],
                    f"predicate:verify-assertion:{assertion_id}",
                },
            )
    result["ready"] = bool(
        result["exact_all"]
        and not any(result[field_name] for field_name in _READINESS_DEBT_FIELDS),
    )
    result["status"] = (
        "ready"
        if result["ready"]
        else "closed_with_owner_routed_debt"
        if result["exact_all"]
        else "incomplete"
    )
    return result


def _validate_lineage_coverage(
    coverage: Mapping[str, Any],
    *,
    expected_source_ids: set[str],
) -> None:
    """Validate CORPVS lineage-source coverage independently of raw-unit parity."""
    if set(coverage) != _COVERAGE_KEYS:
        raise ValueError("coverage receipt contains unsupported or missing fields")
    sources = _required_list(coverage, "sources")
    if not sources:
        raise ValueError("coverage receipt source denominator is empty")
    source_ids: list[str] = []
    counts: Counter[str] = Counter()
    expected_residuals: dict[str, dict[str, str]] = {}
    source_debt: dict[str, set[str]] = {field_name: set() for field_name in _READINESS_DEBT_FIELDS}
    base_keys = {"source_id", "status", "accessible", "evidence_references"}
    owner_keys = {"owner_reference", "failed_predicate", "next_action"}
    accessibility = {
        "acquired": True,
        "parsed": True,
        "quarantined": True,
        "inaccessible": False,
        "missing_expected": False,
        "owner_blocked": False,
    }
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("coverage receipt source denominator is invalid")
        status = source.get("status")
        if status not in _COVERAGE_STATUSES:
            raise ValueError("coverage receipt source status is invalid")
        expected_keys = base_keys if status == "parsed" else base_keys | owner_keys
        if set(source) != expected_keys:
            raise ValueError("coverage receipt source has unsupported or missing fields")
        source_id = _required_text(source, "source_id")
        if not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError("coverage receipt source ID is invalid")
        evidence = source.get("evidence_references")
        if (
            not isinstance(evidence, list)
            or not evidence
            or len(evidence) != len(set(map(str, evidence)))
            or not all(isinstance(reference, str) and reference for reference in evidence)
        ):
            raise ValueError("coverage receipt source evidence is invalid")
        if source.get("accessible") is not accessibility[str(status)]:
            raise ValueError("coverage receipt source accessibility contradicts status")
        source_ids.append(source_id)
        counts[str(status)] += 1
        if status != "parsed":
            expected_residuals[source_id] = {
                field_name: _required_text(source, field_name) for field_name in owner_keys
            }
        if status in {"inaccessible", "owner_blocked"}:
            source_debt["unresolved_blockers"].add(source_id)
        elif status == "quarantined":
            source_debt["quarantines"].add(source_id)
        elif status == "missing_expected":
            source_debt["missing_requirements"].add(source_id)
        elif status == "acquired":
            source_debt["incomplete_predicates"].add(source_id)
    if len(source_ids) != len(set(source_ids)) or set(source_ids) != expected_source_ids:
        raise ValueError("coverage receipt does not exactly cover lineage source envelopes")

    denominator = coverage.get("denominator")
    if (
        not isinstance(denominator, Mapping)
        or set(denominator) != {"discovery_manifest_reference", "count", "manifest_hash"}
        or not isinstance(denominator.get("discovery_manifest_reference"), str)
        or not denominator.get("discovery_manifest_reference")
        or denominator.get("count") != len(sources)
        or denominator.get("manifest_hash") != content_digest(sources)
    ):
        raise ValueError("coverage receipt source denominator mismatch")
    declared_counts = coverage.get("counts")
    expected_counts = {status: counts.get(status, 0) for status in _COVERAGE_STATUSES}
    if (
        not isinstance(declared_counts, Mapping)
        or set(declared_counts) != set(_COVERAGE_STATUSES)
        or dict(declared_counts) != expected_counts
    ):
        raise ValueError("coverage receipt classification counts mismatch")

    residuals = coverage.get("residual_owners")
    if not isinstance(residuals, list):
        raise ValueError("coverage receipt residual owners are invalid")
    actual_residuals: dict[str, dict[str, str]] = {}
    for residual in residuals:
        if not isinstance(residual, Mapping) or set(residual) != {"source_id", *owner_keys}:
            raise ValueError("coverage receipt residual owners are invalid")
        source_id = _required_text(residual, "source_id")
        if source_id in actual_residuals:
            raise ValueError("coverage receipt residual owners are duplicated")
        actual_residuals[source_id] = {
            field_name: _required_text(residual, field_name) for field_name in owner_keys
        }
    if actual_residuals != expected_residuals:
        raise ValueError("coverage receipt residual owners are not exact")

    normalized_readiness = _normalize_readiness(
        {
            "exact_all": coverage.get("exact_all"),
            "ready": coverage.get("ready"),
            "status": coverage.get("closure_status"),
            **{field_name: coverage.get(field_name) for field_name in _READINESS_DEBT_FIELDS},
        },
        label="coverage receipt",
    )
    if normalized_readiness["exact_all"] is not True:
        raise ValueError("coverage receipt is not exact_all")
    for field_name, expected in source_debt.items():
        if not expected <= set(normalized_readiness[field_name]):
            raise ValueError(f"coverage receipt omits {field_name} source debt")

    scope = coverage.get("constitutional_scope")
    if (
        not isinstance(scope, Mapping)
        or set(scope)
        != {
            "scope_reference",
            "exact_all",
            "blocked_scopes",
            "missing_requirements",
            "ready",
        }
        or not isinstance(scope.get("scope_reference"), str)
        or not scope.get("scope_reference")
    ):
        raise ValueError("coverage constitutional_scope is invalid")
    blocked_scopes = scope.get("blocked_scopes")
    missing_scope_requirements = scope.get("missing_requirements")
    if (
        not isinstance(blocked_scopes, list)
        or len(blocked_scopes) != len(set(map(str, blocked_scopes)))
        or not all(isinstance(item, str) and item for item in blocked_scopes)
        or not isinstance(missing_scope_requirements, list)
        or len(missing_scope_requirements) != len(set(map(str, missing_scope_requirements)))
        or not all(isinstance(item, str) and item for item in missing_scope_requirements)
    ):
        raise ValueError("coverage constitutional_scope debt is invalid")
    expected_blocked_scopes = sorted(expected_residuals)
    scope_ready = (
        normalized_readiness["exact_all"]
        and not expected_blocked_scopes
        and not missing_scope_requirements
    )
    if (
        scope.get("exact_all") is not normalized_readiness["exact_all"]
        or sorted(blocked_scopes) != expected_blocked_scopes
        or scope.get("ready") is not scope_ready
        or not set(missing_scope_requirements) <= set(normalized_readiness["missing_requirements"])
    ):
        raise ValueError("coverage constitutional_scope contradicts source coverage")
    expected_ready = (
        normalized_readiness["exact_all"]
        and expected_counts["parsed"] == len(sources)
        and not expected_residuals
        and not any(normalized_readiness[field_name] for field_name in _READINESS_DEBT_FIELDS)
        and scope_ready
    )
    if normalized_readiness["ready"] is not expected_ready:
        raise ValueError("coverage receipt ready contradicts its owner predicates")


def _validate_receipt_pair_debt(
    *,
    coverage_readiness: Mapping[str, Any],
    parity_readiness: Mapping[str, Any],
    parity_receipt_id: str,
) -> None:
    """Require CORPVS to bind debt from the exact CCE parity receipt."""
    for field_name in _READINESS_DEBT_FIELDS:
        coverage_debt = set(coverage_readiness[field_name])
        parity_reference = f"receipt:{parity_receipt_id}#/readiness/{field_name}"
        stale_parity_references = {
            reference
            for reference in coverage_debt
            if reference.startswith("receipt:normalization-parity-")
            and reference.endswith(f"#/readiness/{field_name}")
            and reference != parity_reference
        }
        if stale_parity_references:
            raise ValueError(f"coverage {field_name} binds a stale parity receipt")
        if bool(parity_readiness[field_name]) != (parity_reference in coverage_debt):
            raise ValueError(
                f"coverage {field_name} does not bind parity readiness debt",
            )


def _validate_testament_constitutional_scope(
    testament: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> None:
    """Prevent ratification claims from outrunning constitutional coverage."""
    status = _required_text(testament, "status")
    if status not in {"candidate", "ratified"}:
        raise ValueError("governance testament status is invalid")
    if status == "candidate":
        return
    scope = coverage["constitutional_scope"]
    ratification = testament.get("ratification")
    ratified_scope = (
        ratification.get("constitutional_coverage") if isinstance(ratification, Mapping) else None
    )
    if (
        scope.get("exact_all") is not True
        or scope.get("ready") is not True
        or scope.get("blocked_scopes")
        or scope.get("missing_requirements")
        or ratified_scope != scope
    ):
        raise ValueError(
            "ratified testament requires identical ready constitutional coverage",
        )


def _source_reference_aliases(
    envelope: Mapping[str, Any],
    *,
    parity_receipt_id: str,
) -> set[str]:
    source_id = _required_text(envelope, "source_id")
    aliases = {
        *_bound_reference_aliases("source", source_id),
        f"source-envelope:{source_id}",
        f"source-envelope.v1.jsonl#{source_id}",
        (
            f"receipt:{parity_receipt_id}:"
            f"source-envelope.v1.jsonl#{source_id}"
        ),
    }
    projection_pointer = envelope.get("redacted_projection_pointer")
    if isinstance(projection_pointer, str) and projection_pointer:
        aliases.add(projection_pointer)
    return aliases


def build_reconcile_inputs(
    *,
    snapshot_id: str,
    snapshot_digest: str,
    snapshot_at: str,
    lineage_graph: Any,
    governance_testament: Any,
    source_census: Any,
    source_envelopes: Any,
    normalized_events: Any,
    assertion_evidence: Any,
    normalization_parity_receipt: Any,
    coverage_receipt: Any,
    generated_at: str | None = None,
    allow_blocked: bool = False,
) -> ReconcileInputs:
    """Validate and bind the acyclic pre-cadence reconciliation interface."""
    if not _DIGEST_PATTERN.fullmatch(snapshot_digest):
        raise ValueError("snapshot_digest is not schema-valid")
    snapshot_time_text, snapshot_time = _required_timestamp(
        {"snapshot_at": snapshot_at},
        "snapshot_at",
    )
    census = _require_contract(source_census, "source-census.v1")
    parity = _require_contract(
        normalization_parity_receipt,
        "normalization-parity-receipt.v1",
    )
    coverage = _require_contract(coverage_receipt, "coverage-receipt.v1")
    graph = validate_lineage_graph(lineage_graph, snapshot_id=snapshot_id)
    testament = _require_contract(governance_testament, "governance-testament.v1")

    if (
        census.get("snapshot_id") != snapshot_id
        or census.get("snapshot_digest") != snapshot_digest
        or census.get("snapshot_at") != snapshot_at
    ):
        raise ValueError("source census snapshot binding mismatch")
    if census.get("census_digest") != _digest_excluding(census, "census_digest"):
        raise ValueError("source census digest mismatch")
    if (
        parity.get("snapshot_id") != snapshot_id
        or parity.get("snapshot_digest") != snapshot_digest
    ):
        raise ValueError("normalization parity snapshot binding mismatch")
    if parity.get("receipt_digest") != _digest_excluding(parity, "receipt_digest"):
        raise ValueError("normalization parity digest mismatch")
    parity_readiness = _normalize_readiness(
        parity.get("readiness"),
        label="normalization parity receipt",
    )
    if parity_readiness["exact_all"] is not True or (
        not allow_blocked and parity_readiness["ready"] is not True
    ):
        raise ValueError("normalization parity receipt is not ready")
    if coverage.get("snapshot_id") != snapshot_id:
        raise ValueError("coverage receipt snapshot binding mismatch")
    if coverage.get("receipt_hash") != _digest_excluding(coverage, "receipt_hash"):
        raise ValueError("coverage receipt digest mismatch")
    residual_owners = coverage.get("residual_owners")
    if not isinstance(residual_owners, list) or not all(
        isinstance(owner, Mapping)
        and all(
            isinstance(owner.get(field_name), str) and owner.get(field_name)
            for field_name in (
                "owner_reference",
                "failed_predicate",
                "next_action",
            )
        )
        for owner in residual_owners
    ):
        raise ValueError("coverage receipt residual owners are invalid")
    coverage_readiness = _normalize_readiness(
        {
            "exact_all": coverage.get("exact_all"),
            "ready": coverage.get("ready"),
            "status": coverage.get("closure_status"),
            **{
                field_name: coverage.get(field_name)
                for field_name in _READINESS_DEBT_FIELDS
            },
        },
        label="coverage receipt",
    )
    if coverage_readiness["exact_all"] is not True or (
        not allow_blocked and coverage_readiness["ready"] is not True
    ):
        raise ValueError("coverage receipt is not ready")

    census_raw_units = _required_list(census, "raw_units")
    census_hashes: dict[str, str | None] = {}
    for item in census_raw_units:
        if not isinstance(item, Mapping):
            raise ValueError("source census raw units are invalid or duplicated")
        raw_unit_id = _required_text(item, "raw_unit_id")
        if raw_unit_id in census_hashes:
            raise ValueError("source census raw units are invalid or duplicated")
        content_hash = item.get("content_hash")
        acquisition_status = _required_text(item, "acquisition_status")
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or not _DIGEST_PATTERN.fullmatch(content_hash)
        ):
            raise ValueError("source census raw units are invalid or duplicated")
        if acquisition_status == "acquired" and content_hash is None:
            raise ValueError("acquired source census raw unit lacks content hash")
        if not allow_blocked and content_hash is None:
            raise ValueError("source census raw units are invalid or duplicated")
        census_hashes[raw_unit_id] = content_hash
    if not census_hashes:
        raise ValueError("source census raw units are invalid or duplicated")

    if not isinstance(source_envelopes, list) or not source_envelopes:
        raise ValueError("source envelopes must be a non-empty list")
    envelopes: list[Mapping[str, Any]] = []
    envelope_ids: set[str] = set()
    envelope_raw_unit_ids: set[str] = set()
    for raw_envelope in source_envelopes:
        envelope = _require_contract(raw_envelope, "source-envelope.v1")
        source_id = _required_text(envelope, "source_id")
        raw_unit_id = _required_text(envelope, "raw_unit_id")
        custody = envelope.get("custody_snapshot")
        if (
            not isinstance(custody, Mapping)
            or custody.get("snapshot_id") != snapshot_id
            or custody.get("snapshot_hash") != snapshot_digest
            or custody.get("immutable") is not True
        ):
            raise ValueError(f"source envelope {source_id} custody binding mismatch")
        raw_unit_content_hash = envelope.get("raw_unit_content_hash")
        if (
            not isinstance(raw_unit_content_hash, str)
            or not _DIGEST_PATTERN.fullmatch(raw_unit_content_hash)
            or census_hashes.get(raw_unit_id) != raw_unit_content_hash
        ):
            raise ValueError(f"source envelope {source_id} raw content binding mismatch")
        body_hash = _required_text(envelope, "body_hash")
        if not _DIGEST_PATTERN.fullmatch(body_hash):
            raise ValueError(f"source envelope {source_id} body hash mismatch")
        if source_id in envelope_ids:
            raise ValueError("source envelope IDs must be unique")
        envelope_ids.add(source_id)
        envelope_raw_unit_ids.add(raw_unit_id)
        envelopes.append(envelope)
    acquired_raw_unit_ids = {
        raw_unit_id
        for raw_unit_id, content_hash in census_hashes.items()
        if content_hash is not None
    }
    if envelope_raw_unit_ids != acquired_raw_unit_ids:
        raise ValueError("source envelopes do not cover every acquired raw unit")
    parity_receipt_id = _required_text(parity, "receipt_id")
    envelope_body_hashes = {
        _required_text(envelope, "source_id"): _required_text(envelope, "body_hash")
        for envelope in envelopes
    }
    envelope_references = {
        reference
        for envelope in envelopes
        for reference in _source_reference_aliases(
            envelope,
            parity_receipt_id=parity_receipt_id,
        )
    }
    for node in graph["nodes"]:
        source_id = _required_text(node, "source_envelope_id")
        if envelope_body_hashes.get(source_id) != node.get("content_hash"):
            raise ValueError(
                f"lineage node {node.get('node_id')} source envelope is unresolved",
            )
    for edge in graph["edges"]:
        for span in edge["evidence_spans"]:
            source_id = _required_text(span, "source_envelope_id")
            if envelope_body_hashes.get(source_id) != span.get("body_hash"):
                raise ValueError(
                    f"lineage edge {edge.get('edge_id')} source envelope is unresolved",
                )

    if not isinstance(normalized_events, list) or not normalized_events:
        raise ValueError("normalized events must be a non-empty list")
    events: list[Mapping[str, Any]] = []
    event_ids: set[str] = set()
    for raw_event in normalized_events:
        event = _require_contract(raw_event, "normalized-event.v1")
        if (
            event.get("snapshot_id") != snapshot_id
            or event.get("snapshot_digest") != snapshot_digest
        ):
            raise ValueError("normalized event snapshot binding mismatch")
        event_id = _required_text(event, "event_id")
        raw_unit_id = _required_text(event, "raw_unit_id")
        content_hash = event.get("raw_unit_content_hash")
        if content_hash is None and isinstance(event.get("identity_basis"), Mapping):
            content_hash = event["identity_basis"].get("content_hash")
        if (
            not isinstance(content_hash, str)
            or not _DIGEST_PATTERN.fullmatch(content_hash)
            or census_hashes.get(raw_unit_id) != content_hash
        ):
            raise ValueError(f"normalized event {event_id} raw content binding mismatch")
        if not _reference_resolves(
            event.get("source_envelope_reference"),
            envelope_references,
        ):
            raise ValueError(f"normalized event {event_id} source envelope is unresolved")
        if event_id in event_ids:
            raise ValueError("normalized event IDs must be unique")
        event_ids.add(event_id)
        events.append(event)

    parity_input = parity.get("input_census")
    parity_output = parity.get("output_events")
    if (
        not isinstance(parity_input, Mapping)
        or parity_input.get("census_digest") != census.get("census_digest")
        or set(map(str, parity_input.get("raw_unit_ids", []))) != set(census_hashes)
    ):
        raise ValueError("normalization parity census crosswalk mismatch")
    if (
        not isinstance(parity_output, Mapping)
        or set(map(str, parity_output.get("event_ids", []))) != event_ids
    ):
        raise ValueError("normalization parity event crosswalk mismatch")
    promotions = parity.get("promotions")
    if not isinstance(promotions, list):
        raise ValueError("normalization parity promotion crosswalk mismatch")
    promotion_by_raw_unit: dict[str, Mapping[str, Any]] = {}
    promoted_event_ids: set[str] = set()
    disposition_raw_ids: dict[str, set[str]] = {
        "blocked": set(),
        "quarantined": set(),
        "ignored_transport_echo": set(),
        "unsupported": set(),
    }
    for promotion in promotions:
        if not isinstance(promotion, Mapping):
            raise ValueError("normalization parity promotion crosswalk mismatch")
        has_events = "event_ids" in promotion
        has_disposition = "disposition" in promotion
        expected_promotion_keys = {
            "raw_unit_id",
            "raw_unit_content_hash",
            "event_ids" if has_events else "disposition",
        }
        if has_events is has_disposition or set(promotion) != expected_promotion_keys:
            raise ValueError("normalization parity promotion crosswalk mismatch")
        raw_unit_id = _required_text(promotion, "raw_unit_id")
        if raw_unit_id in promotion_by_raw_unit:
            raise ValueError("normalization parity promotion crosswalk mismatch")
        if promotion.get("raw_unit_content_hash") != census_hashes.get(raw_unit_id):
            raise ValueError("normalization parity promotion content hash mismatch")
        promotion_events = promotion.get("event_ids")
        disposition = promotion.get("disposition")
        if isinstance(promotion_events, list) and promotion_events:
            if disposition is not None or not all(
                isinstance(event_id, str) and event_id in event_ids
                for event_id in promotion_events
            ):
                raise ValueError("normalization parity promotion crosswalk mismatch")
            if promoted_event_ids & set(promotion_events):
                raise ValueError("normalization parity event is promoted more than once")
            promoted_event_ids.update(promotion_events)
        elif not allow_blocked or not isinstance(disposition, Mapping):
            raise ValueError("normalization parity promotion crosswalk mismatch")
        else:
            disposition_type = disposition.get("type")
            if disposition_type not in disposition_raw_ids or set(disposition) != {
                "type",
                "owner_reference",
                "failed_predicate",
                "next_action",
                "evidence_references",
            }:
                raise ValueError("normalization parity disposition is invalid")
            for field_name in ("owner_reference", "failed_predicate", "next_action"):
                _required_text(disposition, field_name)
            evidence_references = disposition.get("evidence_references")
            if (
                not isinstance(evidence_references, list)
                or not evidence_references
                or len(evidence_references) != len(set(map(str, evidence_references)))
                or not all(
                    isinstance(reference, str) and reference for reference in evidence_references
                )
            ):
                raise ValueError("normalization parity disposition is invalid")
            disposition_raw_ids[str(disposition_type)].add(raw_unit_id)
        promotion_by_raw_unit[raw_unit_id] = promotion
    if set(promotion_by_raw_unit) != set(census_hashes) or promoted_event_ids != event_ids:
        raise ValueError("normalization parity promotion crosswalk mismatch")
    if not (disposition_raw_ids["blocked"] | disposition_raw_ids["unsupported"]) <= set(
        parity_readiness["unresolved_blockers"],
    ):
        raise ValueError("normalization parity blocker debt omits dispositions")
    if not disposition_raw_ids["quarantined"] <= set(
        parity_readiness["quarantines"],
    ):
        raise ValueError("normalization parity quarantine debt omits dispositions")
    lineage_source_ids = {_required_text(node, "source_envelope_id") for node in graph["nodes"]} | {
        _required_text(span, "source_envelope_id")
        for edge in graph["edges"]
        for span in edge["evidence_spans"]
    }
    if not lineage_source_ids <= envelope_ids:
        raise ValueError(
            "source envelopes do not cover the lineage-source denominator",
        )
    _validate_lineage_coverage(
        coverage,
        expected_source_ids=lineage_source_ids,
    )
    _validate_receipt_pair_debt(
        coverage_readiness=coverage_readiness,
        parity_readiness=parity_readiness,
        parity_receipt_id=parity_receipt_id,
    )
    _validate_testament_constitutional_scope(testament, coverage)

    if not isinstance(assertion_evidence, list) or not assertion_evidence:
        raise ValueError("assertion evidence must be a non-empty list")
    assertions = tuple(
        _require_contract(record, "assertion-evidence.v1")
        for record in assertion_evidence
    )
    readiness = _combined_readiness(parity, coverage, assertions)
    if not allow_blocked and readiness["ready"] is not True:
        raise ValueError("reconciliation assertion evidence is not ready")
    generated_candidates = [
        _required_timestamp(graph, "generated_at"),
        _required_timestamp(parity, "generated_at"),
        _required_timestamp(coverage, "generated_at"),
    ]
    if generated_at is not None:
        generated_candidates.append(
            _required_timestamp({"generated_at": generated_at}, "generated_at"),
        )
    generated_time_text, generated_time = max(
        generated_candidates,
        key=lambda item: item[1],
    )
    if generated_time < snapshot_time:
        raise ValueError("reconciliation generation window precedes frozen snapshot")

    digest_projection = {
        "snapshot_id": snapshot_id,
        "snapshot_digest": snapshot_digest,
        "snapshot_at": snapshot_at,
        "generated_at": generated_time_text,
        "lineage_graph": content_digest(graph),
        "governance_testament": content_digest(testament),
        "source_census": content_digest(census),
        "source_envelopes": content_digest(source_envelopes),
        "normalized_events": content_digest(normalized_events),
        "assertion_evidence": content_digest(assertion_evidence),
        "normalization_parity_receipt": content_digest(parity),
        "coverage_receipt": content_digest(coverage),
    }
    exact_input_digest = content_digest(digest_projection)
    return ReconcileInputs(
        snapshot_id=snapshot_id,
        snapshot_digest=snapshot_digest,
        snapshot_at=snapshot_time_text,
        generated_at=generated_time_text,
        lineage_graph=graph,
        governance_testament=testament,
        source_census=census,
        source_envelopes=tuple(envelopes),
        normalized_events=tuple(events),
        assertion_evidence=assertions,
        normalization_parity_receipt=parity,
        coverage_receipt=coverage,
        readiness=readiness,
        input_digest=exact_input_digest,
    )


def build_snapshot_evidence_index(
    inputs: ReconcileInputs,
    *,
    allow_blocked: bool = False,
) -> SnapshotEvidenceIndex:
    """Derive the only references allowed in a traceable self-image set."""
    source_references: set[str] = set()
    evidence_digests: dict[str, str] = {}
    parity_receipt_id = _required_text(
        inputs.normalization_parity_receipt,
        "receipt_id",
    )
    for envelope in inputs.source_envelopes:
        aliases = _source_reference_aliases(
            envelope,
            parity_receipt_id=parity_receipt_id,
        )
        source_references.update(aliases)
        body_hash = _required_text(envelope, "body_hash")
        evidence_digests.update({alias: body_hash for alias in aliases})

    event_references: set[str] = set()
    for event in inputs.normalized_events:
        event_id = _required_text(event, "event_id")
        aliases = _bound_reference_aliases("event", event_id)
        event_references.update(aliases)
        identity_basis = event.get("identity_basis")
        event_hash = (
            identity_basis.get("content_hash")
            if isinstance(identity_basis, Mapping)
            else event.get("raw_unit_content_hash")
        )
        if not isinstance(event_hash, str):
            raise ValueError(f"normalized event {event_id} lacks a content hash")
        evidence_digests.update({alias: event_hash for alias in aliases})

    receipt_references: set[str] = set()
    for receipt, digest_field in (
        (inputs.normalization_parity_receipt, "receipt_digest"),
        (inputs.coverage_receipt, "receipt_hash"),
    ):
        receipt_id = _required_text(receipt, "receipt_id")
        if receipt.get(digest_field) != _digest_excluding(receipt, digest_field):
            raise ValueError(f"predicate receipt {receipt_id} digest mismatch")
        aliases = _bound_reference_aliases("receipt", receipt_id)
        if aliases & receipt_references:
            raise ValueError(f"predicate receipt {receipt_id} is duplicated")
        receipt_references.update(aliases)
        receipt_digest = _required_text(receipt, digest_field)
        evidence_digests.update({alias: receipt_digest for alias in aliases})

    evidence_ids = source_references | event_references | receipt_references
    _, snapshot_time = _required_timestamp(
        {"snapshot_at": inputs.snapshot_at},
        "snapshot_at",
    )
    _, generated_time = _required_timestamp(
        {"generated_at": inputs.generated_at},
        "generated_at",
    )
    assertion_references: set[str] = set()
    for assertion in inputs.assertion_evidence:
        assertion_id = _required_text(assertion, "assertion_id")
        verification_state = assertion.get("verification_state")
        if verification_state not in {"verified", "unverified"}:
            raise ValueError(f"assertion {assertion_id} verification state is invalid")
        if not allow_blocked and verification_state != "verified":
            raise ValueError(f"assertion {assertion_id} is not verified")
        freshness = assertion.get("freshness")
        if verification_state == "verified":
            if not isinstance(freshness, Mapping) or freshness.get("status") != "fresh":
                raise ValueError(f"assertion {assertion_id} is stale")
            _, verified_time = _required_timestamp(freshness, "verified_at")
            if verified_time < snapshot_time or verified_time > generated_time:
                raise ValueError(
                    f"assertion {assertion_id} verification is outside the snapshot window",
                )
        evidence = _required_list(assertion, "evidence_references")
        if not evidence:
            raise ValueError(f"assertion {assertion_id} has no evidence")
        for item in evidence:
            expected_digest = (
                _resolve_bound_value(item.get("reference"), evidence_digests)
                if isinstance(item, Mapping)
                else None
            )
            if (
                not isinstance(item, Mapping)
                or not _reference_resolves(item.get("reference"), evidence_ids)
                or item.get("body_hash") != expected_digest
            ):
                raise ValueError(f"assertion {assertion_id} evidence is unresolved")
        aliases = _bound_reference_aliases("assertion", assertion_id)
        if aliases & assertion_references:
            raise ValueError(f"assertion {assertion_id} is duplicated")
        assertion_references.update(aliases)

    return SnapshotEvidenceIndex(
        snapshot_id=inputs.snapshot_id,
        snapshot_digest=inputs.snapshot_digest,
        snapshot_at=inputs.snapshot_at,
        generated_at=inputs.generated_at,
        input_digest=inputs.input_digest,
        source_references=tuple(sorted(source_references)),
        event_references=tuple(sorted(event_references)),
        assertion_references=tuple(sorted(assertion_references)),
        predicate_receipt_references=tuple(sorted(receipt_references)),
    )


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


def validate_self_image_set(
    value: Any,
    *,
    evidence_index: SnapshotEvidenceIndex,
    allow_blocked: bool = False,
) -> Mapping[str, Any]:
    """Validate denominator, snapshot window, and every public evidence edge."""
    image_set = _require_contract(value, SELF_IMAGE_SET_CONTRACT)
    for field_name in (
        "set_id",
        "snapshot_id",
        "snapshot_digest",
        "registry_reference",
        "registry_digest",
        "set_digest",
    ):
        _required_text(image_set, field_name)
    if (
        image_set["snapshot_id"] != evidence_index.snapshot_id
        or image_set["snapshot_digest"] != evidence_index.snapshot_digest
    ):
        raise ValueError("node self-image set snapshot binding mismatch")
    if image_set["registry_reference"] != "#/registry_projection":
        raise ValueError(
            "registry_reference must resolve to the embedded registry_projection",
        )

    registry_projection = _required_list(image_set, "registry_projection")
    if not registry_projection:
        raise ValueError("registry_projection must be non-empty")
    projection_node_ids: list[str] = []
    for node in registry_projection:
        if not isinstance(node, Mapping):
            raise ValueError("registry_projection node must be an object")
        if set(node) != _PUBLIC_REGISTRY_NODE_KEYS:
            raise ValueError(
                "registry_projection nodes must contain only public identity fields",
            )
        uid = _required_text(node, "uid")
        entity_type = _required_text(node, "entity_type")
        lifecycle_status = _required_text(node, "lifecycle_status")
        if not _ENTITY_UID_PATTERN.fullmatch(uid):
            raise ValueError("registry_projection uid is not schema-valid")
        if entity_type not in {item.value for item in EntityType}:
            raise ValueError("registry_projection entity_type is not schema-valid")
        if lifecycle_status not in {item.value for item in LifecycleStatus}:
            raise ValueError("registry_projection lifecycle_status is not schema-valid")
        projection_node_ids.append(uid)
    if len(projection_node_ids) != len(set(projection_node_ids)):
        raise ValueError("registry_projection uid values must be unique")
    if projection_node_ids != sorted(projection_node_ids):
        raise ValueError("registry_projection must be ordered by ascending uid")
    if content_digest(registry_projection) != image_set["registry_digest"]:
        raise ValueError("registry_digest does not bind the registry_projection")

    registered_node_ids = _required_list(image_set, "registered_node_ids")
    if registered_node_ids != projection_node_ids:
        raise ValueError(
            "registered_node_ids must derive exactly from registry_projection",
        )
    images = _required_list(image_set, "self_images")
    image_node_ids = [
        _required_text(image, "node_id") for image in images if isinstance(image, Mapping)
    ]
    if len(image_node_ids) != len(images):
        raise ValueError("self_images must contain only objects")
    if image_node_ids != registered_node_ids:
        raise ValueError(
            "self_images must derive exactly and in order from registered_node_ids",
        )

    _, snapshot_time = _required_timestamp(
        {"snapshot_at": evidence_index.snapshot_at},
        "snapshot_at",
    )
    _, generated_time = _required_timestamp(
        {"generated_at": evidence_index.generated_at},
        "generated_at",
    )
    if generated_time < snapshot_time:
        raise ValueError("self-image snapshot generation window is invalid")
    allowed_references = evidence_index.allowed_references

    def require_evidence(references: Any, location: str) -> None:
        if (
            not isinstance(references, list)
            or not references
            or not all(
                _reference_resolves(reference, allowed_references)
                for reference in references
            )
        ):
            raise ValueError(f"{location} has unresolved snapshot evidence")

    for image in images:
        if not isinstance(image, Mapping):
            raise ValueError("self_images must contain only objects")
        _, reconciled_time = _required_timestamp(image, "reconciled_at")
        if reconciled_time < snapshot_time or reconciled_time > generated_time:
            raise ValueError("self-image reconciled_at is outside the snapshot window")
        require_evidence(image.get("evidence_references"), "self-image")
        relations = image.get("relations")
        if not isinstance(relations, Mapping):
            raise ValueError("self-image relations must be an object")
        for direction in ("incoming", "outgoing"):
            related = relations.get(direction)
            if not isinstance(related, list):
                raise ValueError(f"self-image {direction} relations must be a list")
            for relation in related:
                if not isinstance(relation, Mapping):
                    raise ValueError("self-image relation must be an object")
                require_evidence(
                    relation.get("evidence_references"),
                    "self-image relation",
                )
        observations = image.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValueError("self-image observations must be non-empty")
        for observation in observations:
            if not isinstance(observation, Mapping):
                raise ValueError("self-image observation must be an object")
            require_evidence(
                observation.get("evidence_references"),
                "self-image observation",
            )
        active_ideal_forms = image.get("active_ideal_forms")
        if not isinstance(active_ideal_forms, list) or not active_ideal_forms:
            raise ValueError("self-image active ideals must be non-empty")
        for ideal in active_ideal_forms:
            if not isinstance(ideal, Mapping):
                raise ValueError("self-image active ideal must be an object")
            require_evidence(
                ideal.get("evidence_references"),
                "self-image active ideal",
            )

    counts = image_set.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("self-image counts must be an object")
    if counts.get("registered") != len(registry_projection):
        raise ValueError("counts.registered does not match registry_projection")
    if counts.get("exported") != len(images):
        raise ValueError("counts.exported does not match self_images")

    readiness = _normalize_readiness(
        image_set.get("readiness"),
        label="node self-image set",
    )
    if readiness["exact_all"] is not True or (
        not allow_blocked and readiness["ready"] is not True
    ):
        raise ValueError("node self-image set is not ready")
    if not readiness["ready"]:
        if readiness["status"] not in {
            "blocked",
            "closed_with_owner_routed_debt",
        }:
            raise ValueError("node self-image set blocked status is invalid")
        receipt_references = set(evidence_index.predicate_receipt_references)
        for image in images:
            if not isinstance(image, Mapping) or not (
                set(map(str, image.get("evidence_references", [])))
                & receipt_references
            ):
                raise ValueError(
                    "blocked self-image lacks a real owner receipt reference",
                )

    body = dict(image_set)
    actual_set_digest = body.pop("set_digest")
    if content_digest(body) != actual_set_digest:
        raise ValueError("set_digest does not bind the self-image set")
    return image_set


def build_self_image_set(
    store: RegistryStore,
    *,
    evidence_index: SnapshotEvidenceIndex,
    reconciled_at: str,
    constitutional_digest: str,
    readiness: Mapping[str, Any] | None = None,
    allow_blocked: bool = False,
) -> dict[str, Any]:
    """Export exactly one deterministic self-image for every registered entity."""
    entities = sorted(store.list_entities(), key=lambda entity: entity.uid)
    registry_projection = [
        {
            "uid": entity.uid,
            "entity_type": entity.entity_type.value,
            "lifecycle_status": entity.lifecycle_status.value,
        }
        for entity in entities
    ]
    registered_node_ids = [str(node["uid"]) for node in registry_projection]
    if not registered_node_ids:
        raise ValueError("node self-image set requires at least one registered node")
    images = [
        store.node_self_image(
            node_id,
            constitutional_digest=constitutional_digest,
            last_reconciled_at=reconciled_at,
            allowed_evidence_references=evidence_index.allowed_references,
        ).to_dict()
        for node_id in registered_node_ids
    ]
    normalized_readiness = _normalize_readiness(
        readiness
        or {
            "exact_all": True,
            **{field_name: [] for field_name in _READINESS_DEBT_FIELDS},
            "ready": True,
            "status": "ready",
        },
        label="node self-image set",
    )
    if not normalized_readiness["ready"]:
        if not allow_blocked:
            raise ValueError("node self-image set is not ready")
        receipt_references = sorted(
            reference
            for reference in evidence_index.predicate_receipt_references
            if reference.startswith("receipt:")
        )
        if not receipt_references:
            raise ValueError("blocked self-image set lacks owner receipt evidence")
        for image in images:
            image["evidence_references"] = sorted(
                {
                    *map(str, image["evidence_references"]),
                    *receipt_references,
                },
            )
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
    body = {
        "contract_name": SELF_IMAGE_SET_CONTRACT,
        "contract_version": 1,
        "set_id": f"self-images:{evidence_index.snapshot_id}",
        "snapshot_id": evidence_index.snapshot_id,
        "snapshot_digest": evidence_index.snapshot_digest,
        "registry_reference": "#/registry_projection",
        "registry_projection": registry_projection,
        "registry_digest": content_digest(registry_projection),
        "registered_node_ids": registered_node_ids,
        "self_images": images,
        "counts": {
            "registered": len(registered_node_ids),
            "exported": len(images),
        },
        "readiness": normalized_readiness,
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    result = {**body, "set_digest": content_digest(body)}
    validate_self_image_set(
        result,
        evidence_index=evidence_index,
        allow_blocked=allow_blocked,
    )
    return result


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


def reconcile_governance_snapshot(
    store: RegistryStore,
    inputs: ReconcileInputs,
    *,
    output_dir: Path,
    allow_blocked: bool = False,
) -> dict[str, Any]:
    """Reconcile exact pre-cadence owner artifacts without a final-bundle cycle."""
    evidence_index = build_snapshot_evidence_index(
        inputs,
        allow_blocked=allow_blocked,
    )
    lineage_graph = validate_lineage_graph(
        inputs.lineage_graph,
        snapshot_id=inputs.snapshot_id,
    )
    result = import_lineage_graph(
        store,
        lineage_graph,
        snapshot_id=inputs.snapshot_id,
    )
    if result.unresolved:
        raise ValueError("governance reconciliation has unresolved reviewed-lineage debt")
    constitutional_digest = content_digest(inputs.governance_testament)

    exported_lineage = export_lineage_graph(
        store,
        snapshot_id=inputs.snapshot_id,
        generated_at=inputs.generated_at,
        graph_id=str(lineage_graph["graph_id"]),
    )
    self_image_set = build_self_image_set(
        store,
        evidence_index=evidence_index,
        reconciled_at=inputs.generated_at,
        constitutional_digest=constitutional_digest,
        readiness=inputs.readiness,
        allow_blocked=allow_blocked,
    )
    receipt = {
        "contract_name": RECONCILIATION_RECEIPT_CONTRACT,
        "contract_version": 1,
        "receipt_id": f"ontologia-reconcile:{inputs.snapshot_id}",
        "snapshot_id": inputs.snapshot_id,
        "snapshot_digest": inputs.snapshot_digest,
        "snapshot_at": inputs.snapshot_at,
        "generated_at": inputs.generated_at,
        "input_digest": inputs.input_digest,
        "evidence_index_digest": evidence_index.digest,
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
        "readiness": deepcopy(self_image_set["readiness"]),
        "ready": result.ready and bool(self_image_set["readiness"]["ready"]),
    }
    if not receipt["ready"] and not allow_blocked:
        raise ValueError("governance reconciliation has unresolved reviewed-lineage debt")

    store.save()
    _write_if_changed(output_dir / "lineage-graph.json", exported_lineage)
    _write_if_changed(output_dir / "node-self-image-set.json", self_image_set)
    _write_if_changed(output_dir / "reconciliation-receipt.json", receipt)
    _append_receipt(store.store_dir / "governance-reconciliations.jsonl", receipt)
    return {
        "lineage_graph": exported_lineage,
        "node_self_image_set": self_image_set,
        "receipt": receipt,
    }


def reconcile_snapshot_bundle(
    store: RegistryStore,
    bundle: Any,
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate a final bundle and delegate through the acyclic owner interface."""
    snapshot = _require_contract(bundle, SNAPSHOT_BUNDLE_CONTRACT)
    missing_fields = [
        field_name
        for field_name in _FINAL_BUNDLE_REQUIRED_FIELDS
        if snapshot.get(field_name) is None
    ]
    if missing_fields:
        raise ValueError(
            "final governance snapshot bundle is incomplete: "
            + ", ".join(missing_fields),
        )
    readiness = snapshot.get("readiness")
    if (
        not isinstance(readiness, Mapping)
        or readiness.get("exact_all") is not True
        or readiness.get("ready") is not True
        or readiness.get("status") != "ready"
        or any(
            not isinstance(readiness.get(field_name), list)
            or readiness.get(field_name)
            for field_name in _READINESS_DEBT_FIELDS
        )
    ):
        raise ValueError("final governance snapshot bundle is not ready")
    if (
        "_snapshot_bundle_digest" not in snapshot
        and snapshot.get("bundle_digest")
        != _digest_excluding(snapshot, "bundle_digest")
    ):
        raise ValueError("final governance snapshot bundle digest mismatch")
    raw_contracts = snapshot.get("contracts")
    contracts: Mapping[str, Any] = (
        raw_contracts if isinstance(raw_contracts, Mapping) else {}
    )

    def snapshot_value(field_name: str) -> Any:
        value = snapshot.get(field_name)
        return value if value is not None else contracts.get(field_name)

    inputs = build_reconcile_inputs(
        snapshot_id=_required_text(snapshot, "snapshot_id"),
        snapshot_digest=_required_text(snapshot, "snapshot_digest"),
        snapshot_at=_required_text(snapshot, "snapshot_at"),
        generated_at=_required_text(snapshot, "generated_at"),
        lineage_graph=snapshot_value("lineage_graph"),
        governance_testament=snapshot_value("governance_testament"),
        source_census=snapshot_value("source_census"),
        source_envelopes=snapshot_value("source_envelopes"),
        normalized_events=snapshot_value("normalized_events"),
        assertion_evidence=snapshot_value("assertion_evidence"),
        normalization_parity_receipt=snapshot_value(
            "normalization_parity_receipt",
        ),
        coverage_receipt=snapshot_value("coverage"),
    )
    return reconcile_governance_snapshot(store, inputs, output_dir=output_dir)
