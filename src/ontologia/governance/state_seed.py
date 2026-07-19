"""Seed an exact, pre-import registry denominator for governance reconciliation.

The public lineage graph carries source-owned external identifiers.  Ontologia
owns durable ``ent_*`` identities, observations, and the exact registry
denominator used by self-image export.  This module bridges those surfaces
once, persists the crosswalk, and then accepts only byte-equivalent replay.

It deliberately does not import authority nodes or edges.  The bounded
``reconcile`` cadence stage remains the sole owner of that mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ontologia.entity.identity import EntityType, LifecycleStatus
from ontologia.governance.memory import content_digest
from ontologia.governance.reconcile import validate_lineage_graph
from ontologia.metrics.metric import (
    AggregationPolicy,
    MetricDefinition,
    MetricType,
)
from ontologia.registry.store import RegistryStore, open_store

SEED_CONTRACT = "governance-state-seed.v1"
CROSSWALK_CONTRACT = "governance-entity-crosswalk.v1"
SEED_RESULT_CONTRACT = "governance-state-seed-result.v1"
CREATED_BY = "ontologia.governance.seed-state"
OBSERVATION_METRIC_ID = "metric:governance-source-evidence-present"
OBSERVATION_SOURCE = "repo:organvm/organvm-ontologia"
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_ENTITY_UID_PATTERN = re.compile(r"^ent_[a-z]+_[0-9A-HJKMNP-TV-Z]{26}$")
_EVENT_ID_PATTERN = re.compile(r"^evt_[a-f0-9]{64}$")
_RAW_UNIT_ID_PATTERN = re.compile(r"^raw_[A-Za-z0-9_-]+$")
_SOURCE_ID_PATTERN = re.compile(r"^src_[A-Za-z0-9_-]+$")
_COVERAGE_STATUSES = (
    "acquired",
    "parsed",
    "quarantined",
    "inaccessible",
    "missing_expected",
    "owner_blocked",
)
_COVERAGE_ACCESSIBILITY = {
    "acquired": True,
    "parsed": True,
    "quarantined": True,
    "inaccessible": False,
    "missing_expected": False,
    "owner_blocked": False,
}
_DEBT_FIELDS = (
    "unresolved_blockers",
    "quarantines",
    "missing_requirements",
    "citation_debt",
    "incomplete_predicates",
)
_IDEAL_METADATA_KEYS = frozenset(
    {"active", "ideal_form_id", "predicate_receipts"},
)
_EXACT_STATE_FILES = frozenset(
    {
        "entities.json",
        "events.jsonl",
        "metrics.json",
        "names.jsonl",
        "observations.jsonl",
        "variables.json",
    },
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
        *_DEBT_FIELDS,
        "closure_status",
        "residual_owners",
        "receipt_hash",
    },
)
_PARITY_KEYS = frozenset(
    {
        "contract_name",
        "contract_version",
        "receipt_id",
        "snapshot_id",
        "snapshot_digest",
        "generated_at",
        "input_census",
        "output_events",
        "promotions",
        "readiness",
        "digest_algorithm",
        "receipt_digest",
    },
)


@dataclass(frozen=True)
class StateSeedResult:
    """Materialized seed artifacts and whether an exact state was reused."""

    resolved_lineage: dict[str, Any]
    crosswalk: dict[str, Any]
    replayed: bool

    def summary(self) -> dict[str, Any]:
        return {
            "contract_name": SEED_RESULT_CONTRACT,
            "contract_version": 1,
            "snapshot_id": self.crosswalk["snapshot_id"],
            "snapshot_at": self.crosswalk["snapshot_at"],
            "counts": deepcopy(self.crosswalk["counts"]),
            "crosswalk_digest": self.crosswalk["crosswalk_digest"],
            "resolved_lineage_digest": self.crosswalk["resolved_lineage_digest"],
            "replayed": self.replayed,
        }


def _required_text(value: Mapping[str, Any], field_name: str) -> str:
    field = value.get(field_name)
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"state seed requires nonempty {field_name}")
    return field


def _timestamp(value: str, *, label: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _digest_excluding(value: Mapping[str, Any], field_name: str) -> str:
    body = dict(value)
    body.pop(field_name, None)
    return content_digest(body)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} contains unsupported or missing fields")


def _digest(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"{label} digest is invalid")
    return value


def _unique_text_list(
    value: Any,
    *,
    label: str,
    pattern: re.Pattern[str] | None = None,
    nonempty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} must be a unique text list")
    if pattern is not None and not all(pattern.fullmatch(item) for item in value):
        raise ValueError(f"{label} contains an invalid identifier")
    return list(value)


def _readiness(
    value: Any,
    *,
    label: str,
    status_field: str = "status",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} readiness must be an object")
    exact_all = value.get("exact_all")
    ready = value.get("ready")
    status = value.get(status_field)
    if not isinstance(exact_all, bool) or not isinstance(ready, bool):
        raise ValueError(f"{label} readiness booleans are invalid")
    normalized: dict[str, Any] = {"exact_all": exact_all}
    for field_name in _DEBT_FIELDS:
        debt = value.get(field_name)
        if (
            not isinstance(debt, list)
            or len(debt) != len(set(map(str, debt)))
            or not all(isinstance(item, str) and item for item in debt)
        ):
            raise ValueError(f"{label} readiness {field_name} is invalid")
        normalized[field_name] = sorted(debt)
    computed_ready = exact_all and not any(normalized[field_name] for field_name in _DEBT_FIELDS)
    if ready is not computed_ready:
        raise ValueError(f"{label} readiness contradicts declared debt")
    if not exact_all:
        raise ValueError(f"{label} is not exact_all")
    if ready and status != "ready":
        raise ValueError(f"{label} ready status is invalid")
    if not ready and status not in {"blocked", "closed_with_owner_routed_debt"}:
        raise ValueError(f"{label} blocked status is invalid")
    return {**normalized, "ready": ready, "status": str(status)}


def _validate_coverage_receipt(
    value: Mapping[str, Any],
) -> tuple[set[str], dict[str, int], dict[str, Any]]:
    _exact_keys(value, _COVERAGE_KEYS, label="coverage receipt")
    denominator = value.get("denominator")
    if not isinstance(denominator, Mapping):
        raise ValueError("coverage denominator must be an object")
    _exact_keys(
        denominator,
        frozenset({"discovery_manifest_reference", "count", "manifest_hash"}),
        label="coverage denominator",
    )
    _required_text(denominator, "discovery_manifest_reference")
    _digest(denominator.get("manifest_hash"), label="coverage manifest")

    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("coverage sources must be a nonempty list")
    source_ids: list[str] = []
    expected_residuals: list[dict[str, str]] = []
    actual_counts: Counter[str] = Counter()
    base_source_keys = {
        "source_id",
        "status",
        "accessible",
        "evidence_references",
    }
    blocker_keys = {"owner_reference", "failed_predicate", "next_action"}
    for item in sources:
        if not isinstance(item, Mapping):
            raise ValueError("coverage source must be an object")
        status = item.get("status")
        if status not in _COVERAGE_STATUSES:
            raise ValueError("coverage source status is invalid")
        expected_keys = base_source_keys if status == "parsed" else base_source_keys | blocker_keys
        if set(item) != expected_keys:
            raise ValueError("coverage source contains unsupported or missing fields")
        source_id = _required_text(item, "source_id")
        if not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError("coverage source_id is invalid")
        if item.get("accessible") is not _COVERAGE_ACCESSIBILITY[str(status)]:
            raise ValueError(f"coverage source {source_id} accessibility contradicts status")
        _unique_text_list(
            item.get("evidence_references"),
            label=f"coverage source {source_id} evidence_references",
            nonempty=True,
        )
        source_ids.append(source_id)
        actual_counts[str(status)] += 1
        if status != "parsed":
            residual = {
                "source_id": source_id,
                **{
                    field_name: _required_text(item, field_name)
                    for field_name in sorted(blocker_keys)
                },
            }
            expected_residuals.append(residual)
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("coverage source denominator contains duplicate source IDs")
    denominator_count = denominator.get("count")
    if (
        not isinstance(denominator_count, int)
        or isinstance(denominator_count, bool)
        or denominator_count != len(source_ids)
    ):
        raise ValueError("coverage denominator count does not equal classified sources")
    if denominator.get("manifest_hash") != content_digest(sources):
        raise ValueError("coverage manifest hash does not bind the source denominator")

    counts = value.get("counts")
    if not isinstance(counts, Mapping) or set(counts) != set(_COVERAGE_STATUSES):
        raise ValueError("coverage counts are invalid")
    normalized_counts: dict[str, int] = {}
    for status in _COVERAGE_STATUSES:
        count = counts.get(status)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"coverage count for {status} is invalid")
        normalized_counts[status] = count
    if normalized_counts != {status: actual_counts.get(status, 0) for status in _COVERAGE_STATUSES}:
        raise ValueError("coverage counts do not equal source classifications")

    expected_debt = {
        "unresolved_blockers": sorted(
            source_id
            for source_id, item in zip(source_ids, sources, strict=True)
            if item["status"] in {"inaccessible", "owner_blocked"}
        ),
        "quarantines": sorted(
            source_id
            for source_id, item in zip(source_ids, sources, strict=True)
            if item["status"] == "quarantined"
        ),
        "missing_requirements": sorted(
            source_id
            for source_id, item in zip(source_ids, sources, strict=True)
            if item["status"] == "missing_expected"
        ),
        "incomplete_predicates": sorted(
            source_id
            for source_id, item in zip(source_ids, sources, strict=True)
            if item["status"] == "acquired"
        ),
    }
    normalized_debt = {
        field_name: sorted(
            _unique_text_list(value.get(field_name), label=f"coverage {field_name}"),
        )
        for field_name in _DEBT_FIELDS
    }
    for field_name, expected in expected_debt.items():
        if not set(expected) <= set(normalized_debt[field_name]):
            raise ValueError(f"coverage {field_name} omits source-classification debt")

    residuals = value.get("residual_owners")
    if not isinstance(residuals, list):
        raise ValueError("coverage residual_owners must be a list")
    normalized_residuals: list[dict[str, str]] = []
    residual_keys = frozenset(
        {"source_id", "owner_reference", "failed_predicate", "next_action"},
    )
    for item in residuals:
        if not isinstance(item, Mapping):
            raise ValueError("coverage residual owner must be an object")
        _exact_keys(item, residual_keys, label="coverage residual owner")
        source_id = _required_text(item, "source_id")
        if not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError("coverage residual owner source_id is invalid")
        normalized_residuals.append(
            {field_name: _required_text(item, field_name) for field_name in residual_keys},
        )

    def residual_sort_key(item: Mapping[str, str]) -> str:
        return item["source_id"]

    if sorted(normalized_residuals, key=residual_sort_key) != sorted(
        expected_residuals,
        key=residual_sort_key,
    ):
        raise ValueError("coverage residual owners do not exactly cover non-parsed sources")

    constitutional_scope = value.get("constitutional_scope")
    if not isinstance(constitutional_scope, Mapping):
        raise ValueError("coverage constitutional_scope must be an object")
    _exact_keys(
        constitutional_scope,
        frozenset(
            {
                "scope_reference",
                "exact_all",
                "blocked_scopes",
                "missing_requirements",
                "ready",
            },
        ),
        label="coverage constitutional_scope",
    )
    _required_text(constitutional_scope, "scope_reference")
    blocked_scopes = sorted(
        _unique_text_list(
            constitutional_scope.get("blocked_scopes"),
            label="coverage constitutional blocked_scopes",
        ),
    )
    scope_missing = sorted(
        _unique_text_list(
            constitutional_scope.get("missing_requirements"),
            label="coverage constitutional missing_requirements",
        ),
    )
    expected_blocked_scopes = sorted(
        source_id
        for source_id, item in zip(source_ids, sources, strict=True)
        if item["status"] != "parsed"
    )
    exact_all = value.get("exact_all")
    if exact_all is not True:
        raise ValueError("coverage receipt is not exact_all")
    if (
        constitutional_scope.get("exact_all") is not exact_all
        or blocked_scopes != expected_blocked_scopes
    ):
        raise ValueError("coverage constitutional_scope contradicts classified sources")
    if not set(scope_missing) <= set(normalized_debt["missing_requirements"]):
        raise ValueError("coverage constitutional_scope debt is not owner-routed globally")
    scope_ready = exact_all and not blocked_scopes and not scope_missing
    if constitutional_scope.get("ready") is not scope_ready:
        raise ValueError("coverage constitutional_scope ready is invalid")
    ready = value.get("ready")
    expected_ready = (
        exact_all
        and normalized_counts["parsed"] == len(source_ids)
        and not residuals
        and not any(normalized_debt[field_name] for field_name in _DEBT_FIELDS)
        and scope_ready
    )
    if ready is not expected_ready:
        raise ValueError("coverage ready contradicts classified sources")
    return (
        set(source_ids),
        normalized_counts,
        {
            "scope_reference": str(constitutional_scope["scope_reference"]),
            "exact_all": exact_all,
            "blocked_scopes": blocked_scopes,
            "missing_requirements": scope_missing,
            "ready": scope_ready,
        },
    )


def _validate_parity_receipt(
    value: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, int]]:
    _exact_keys(value, _PARITY_KEYS, label="normalization parity receipt")
    if value.get("digest_algorithm") != "sha256-rfc8785-excluding-self-digest-v1":
        raise ValueError("normalization parity digest_algorithm is invalid")
    input_census = value.get("input_census")
    if not isinstance(input_census, Mapping):
        raise ValueError("normalization parity input_census must be an object")
    _exact_keys(
        input_census,
        frozenset(
            {
                "census_id",
                "census_reference",
                "census_digest",
                "raw_unit_ids",
                "raw_units",
            },
        ),
        label="normalization parity input_census",
    )
    _required_text(input_census, "census_id")
    _required_text(input_census, "census_reference")
    _digest(input_census.get("census_digest"), label="normalization parity census")
    raw_unit_ids = _unique_text_list(
        input_census.get("raw_unit_ids"),
        label="normalization parity raw_unit_ids",
        pattern=_RAW_UNIT_ID_PATTERN,
        nonempty=True,
    )
    raw_units = input_census.get("raw_units")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("normalization parity raw_units must be a nonempty list")
    raw_content_by_id: dict[str, str | None] = {}
    for item in raw_units:
        if not isinstance(item, Mapping):
            raise ValueError("normalization parity raw unit must be an object")
        _exact_keys(
            item,
            frozenset({"raw_unit_id", "content_hash"}),
            label="normalization parity raw unit",
        )
        raw_unit_id = _required_text(item, "raw_unit_id")
        if not _RAW_UNIT_ID_PATTERN.fullmatch(raw_unit_id):
            raise ValueError("normalization parity raw_unit_id is invalid")
        if raw_unit_id in raw_content_by_id:
            raise ValueError("normalization parity raw unit denominator contains duplicates")
        raw_content_by_id[raw_unit_id] = _digest(
            item.get("content_hash"),
            label=f"normalization parity raw unit {raw_unit_id}",
            allow_none=True,
        )
    if set(raw_unit_ids) != set(raw_content_by_id):
        raise ValueError("normalization parity raw unit listings disagree")

    output_events = value.get("output_events")
    if not isinstance(output_events, Mapping):
        raise ValueError("normalization parity output_events must be an object")
    _exact_keys(
        output_events,
        frozenset({"event_set_reference", "event_set_digest", "event_ids"}),
        label="normalization parity output_events",
    )
    _required_text(output_events, "event_set_reference")
    _digest(output_events.get("event_set_digest"), label="normalization parity event set")
    output_event_ids = _unique_text_list(
        output_events.get("event_ids"),
        label="normalization parity event_ids",
        pattern=_EVENT_ID_PATTERN,
    )

    promotions = value.get("promotions")
    if not isinstance(promotions, list) or not promotions:
        raise ValueError("normalization parity promotions must be a nonempty list")
    promoted_raw_ids: list[str] = []
    promoted_event_ids: set[str] = set()
    disposition_counts: Counter[str] = Counter()
    disposition_raw_ids: dict[str, set[str]] = {
        "blocked": set(),
        "quarantined": set(),
        "ignored_transport_echo": set(),
        "unsupported": set(),
    }
    for item in promotions:
        if not isinstance(item, Mapping):
            raise ValueError("normalization parity promotion must be an object")
        has_events = "event_ids" in item
        has_disposition = "disposition" in item
        expected_keys = {"raw_unit_id", "raw_unit_content_hash"} | (
            {"event_ids"} if has_events else {"disposition"}
        )
        if has_events is has_disposition or set(item) != expected_keys:
            raise ValueError("normalization parity promotion has invalid classification")
        raw_unit_id = _required_text(item, "raw_unit_id")
        if raw_unit_id not in raw_content_by_id:
            raise ValueError("normalization parity promotion references an unknown raw unit")
        if (
            _digest(
                item.get("raw_unit_content_hash"),
                label=f"normalization parity promotion {raw_unit_id}",
                allow_none=True,
            )
            != raw_content_by_id[raw_unit_id]
        ):
            raise ValueError("normalization parity promotion content binding is stale")
        promoted_raw_ids.append(raw_unit_id)
        if has_events:
            event_ids = _unique_text_list(
                item.get("event_ids"),
                label=f"normalization parity promotion {raw_unit_id} event_ids",
                pattern=_EVENT_ID_PATTERN,
                nonempty=True,
            )
            promoted_event_ids.update(event_ids)
            disposition_counts["parsed"] += 1
            continue
        disposition = item.get("disposition")
        if not isinstance(disposition, Mapping):
            raise ValueError("normalization parity disposition must be an object")
        _exact_keys(
            disposition,
            frozenset(
                {
                    "type",
                    "owner_reference",
                    "failed_predicate",
                    "next_action",
                    "evidence_references",
                },
            ),
            label="normalization parity disposition",
        )
        disposition_type = disposition.get("type")
        if disposition_type not in disposition_raw_ids:
            raise ValueError("normalization parity disposition type is invalid")
        for field_name in ("owner_reference", "failed_predicate", "next_action"):
            _required_text(disposition, field_name)
        _unique_text_list(
            disposition.get("evidence_references"),
            label=f"normalization parity disposition {raw_unit_id} evidence_references",
            nonempty=True,
        )
        disposition_raw_ids[str(disposition_type)].add(raw_unit_id)
        disposition_counts[str(disposition_type)] += 1
    if len(promoted_raw_ids) != len(set(promoted_raw_ids)) or set(
        promoted_raw_ids,
    ) != set(raw_unit_ids):
        raise ValueError("normalization parity promotions do not classify every raw unit once")
    if promoted_event_ids != set(output_event_ids):
        raise ValueError("normalization parity promotions do not exactly cover output events")

    readiness = value.get("readiness")
    normalized_readiness = _readiness(
        readiness,
        label="normalization parity receipt",
    )
    blocker_dispositions = disposition_raw_ids["blocked"] | disposition_raw_ids["unsupported"]
    if not blocker_dispositions <= set(normalized_readiness["unresolved_blockers"]):
        raise ValueError("normalization parity blocker debt omits blocked dispositions")
    if not disposition_raw_ids["quarantined"] <= set(normalized_readiness["quarantines"]):
        raise ValueError("normalization parity quarantine debt omits quarantined dispositions")
    if not set(normalized_readiness["missing_requirements"]) <= set(
        normalized_readiness["unresolved_blockers"],
    ):
        raise ValueError("normalization parity missing requirements are not blocker-routed")
    return (
        set(raw_unit_ids),
        set(output_event_ids),
        {
            "parsed": disposition_counts["parsed"],
            "quarantined": disposition_counts["quarantined"],
            "blocked": disposition_counts["blocked"],
            "unclassified": (
                disposition_counts["unsupported"] + disposition_counts["ignored_transport_echo"]
            ),
        },
    )


def _validate_receipts(
    coverage: Any,
    normalization_parity: Any,
    *,
    snapshot_id: str,
    snapshot_at: str,
    lineage_source_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage receipt must be an object")
    if not isinstance(normalization_parity, Mapping):
        raise ValueError("normalization parity receipt must be an object")
    if (
        coverage.get("contract_name") != "coverage-receipt.v1"
        or coverage.get("contract_version") != 1
    ):
        raise ValueError("state seed requires coverage-receipt.v1")
    if (
        normalization_parity.get("contract_name") != "normalization-parity-receipt.v1"
        or normalization_parity.get("contract_version") != 1
    ):
        raise ValueError("state seed requires normalization-parity-receipt.v1")
    if coverage.get("snapshot_id") != snapshot_id:
        raise ValueError("coverage receipt snapshot_id mismatch")
    if normalization_parity.get("snapshot_id") != snapshot_id:
        raise ValueError("normalization parity receipt snapshot_id mismatch")
    snapshot_digest = _required_text(normalization_parity, "snapshot_digest")
    if not _DIGEST_PATTERN.fullmatch(snapshot_digest):
        raise ValueError("normalization parity snapshot_digest is invalid")
    if coverage.get("receipt_hash") != _digest_excluding(coverage, "receipt_hash"):
        raise ValueError("coverage receipt digest mismatch")
    if normalization_parity.get("receipt_digest") != _digest_excluding(
        normalization_parity,
        "receipt_digest",
    ):
        raise ValueError("normalization parity receipt digest mismatch")
    coverage_source_ids, _coverage_counts, constitutional_scope = _validate_coverage_receipt(
        coverage,
    )
    raw_unit_ids, event_ids, _parity_counts = _validate_parity_receipt(
        normalization_parity,
    )
    if lineage_source_ids != coverage_source_ids:
        raise ValueError(
            "lineage source-envelope denominator does not exactly match coverage "
            f"(missing={sorted(lineage_source_ids - coverage_source_ids)!r}, "
            f"extra={sorted(coverage_source_ids - lineage_source_ids)!r})",
        )

    coverage_generated_at = _required_text(coverage, "generated_at")
    parity_generated_at = _required_text(normalization_parity, "generated_at")
    if coverage_generated_at != parity_generated_at:
        raise ValueError("coverage and parity receipt timestamps do not match")
    if _timestamp(coverage_generated_at, label="receipt generated_at") < _timestamp(
        snapshot_at,
        label="snapshot_at",
    ):
        raise ValueError("receipt generation precedes snapshot_at")

    coverage_readiness = _readiness(
        {
            "exact_all": coverage.get("exact_all"),
            "ready": coverage.get("ready"),
            "closure_status": coverage.get("closure_status"),
            **{field_name: coverage.get(field_name) for field_name in _DEBT_FIELDS},
        },
        label="coverage receipt",
        status_field="closure_status",
    )
    parity_readiness = _readiness(
        normalization_parity.get("readiness"),
        label="normalization parity receipt",
    )
    coverage_id = _required_text(coverage, "receipt_id")
    parity_id = _required_text(normalization_parity, "receipt_id")
    if coverage_id == parity_id:
        raise ValueError("coverage and parity receipts must have distinct IDs")
    for field_name in _DEBT_FIELDS:
        coverage_debt = set(coverage_readiness[field_name])
        parity_reference = f"receipt:{parity_id}#/readiness/{field_name}"
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
    return (
        {
            "contract_name": "coverage-receipt.v1",
            "denominator_kind": "lineage_source_envelopes",
            "denominator_count": len(coverage_source_ids),
            "receipt_id": coverage_id,
            "receipt_digest": str(coverage["receipt_hash"]),
            "reference": f"receipt:{coverage_id}",
            "constitutional_scope_ready": constitutional_scope["ready"],
            "ready": coverage_readiness["ready"],
            "result": "pass" if coverage_readiness["ready"] else "blocked",
        },
        {
            "contract_name": "normalization-parity-receipt.v1",
            "denominator_kind": "normalization_raw_units",
            "denominator_count": len(raw_unit_ids),
            "output_event_count": len(event_ids),
            "receipt_id": parity_id,
            "receipt_digest": str(normalization_parity["receipt_digest"]),
            "snapshot_digest": snapshot_digest,
            "reference": f"receipt:{parity_id}",
            "ready": parity_readiness["ready"],
            "result": "pass" if parity_readiness["ready"] else "blocked",
        },
    )


def _validate_seed(
    value: Any,
    *,
    snapshot_id: str,
    snapshot_at: str,
    external_ids: set[str],
) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("governance state seed must be an object")
    if value.get("contract_name") != SEED_CONTRACT or value.get("contract_version") != 1:
        raise ValueError(f"state seed must use {SEED_CONTRACT}")
    if value.get("snapshot_id") != snapshot_id or value.get("snapshot_at") != snapshot_at:
        raise ValueError("state seed snapshot identity or timestamp mismatch")
    entries = value.get("entities")
    if not isinstance(entries, list) or not entries:
        raise ValueError("state seed entities must be a nonempty list")
    by_external_id: dict[str, dict[str, str]] = {}
    allowed_keys = {
        "display_name",
        "entity_type",
        "external_id",
        "owner_reference",
    }
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != allowed_keys:
            raise ValueError("state seed entity contains unsupported or missing fields")
        external_id = _required_text(item, "external_id")
        if external_id in by_external_id:
            raise ValueError(f"state seed duplicates external_id {external_id}")
        if _ENTITY_UID_PATTERN.fullmatch(external_id):
            raise ValueError("state seed external IDs cannot already be Ontologia UIDs")
        entity_type = _required_text(item, "entity_type")
        try:
            EntityType(entity_type)
        except ValueError as exc:
            raise ValueError(f"state seed entity type is invalid: {entity_type}") from exc
        by_external_id[external_id] = {
            "external_id": external_id,
            "entity_type": entity_type,
            "display_name": _required_text(item, "display_name"),
            "owner_reference": _required_text(item, "owner_reference"),
        }
    actual_ids = set(by_external_id)
    if actual_ids != external_ids:
        missing = sorted(external_ids - actual_ids)
        extra = sorted(actual_ids - external_ids)
        raise ValueError(
            f"state seed denominator mismatch (missing={missing!r}, extra={extra!r})",
        )
    return by_external_id


def _lineage_bindings(
    lineage_graph: Any,
    *,
    snapshot_id: str,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], set[str]]:
    graph = deepcopy(dict(validate_lineage_graph(lineage_graph, snapshot_id=snapshot_id)))
    by_external_id: dict[str, list[dict[str, Any]]] = {}
    lineage_source_ids: set[str] = set()
    for node in graph["nodes"]:
        metadata = node.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"lineage node {node.get('node_id')} lacks metadata")
        external_id = metadata.get("entity_id")
        if not isinstance(external_id, str) or not external_id:
            raise ValueError(f"lineage node {node.get('node_id')} lacks external entity_id")
        if _ENTITY_UID_PATTERN.fullmatch(external_id):
            raise ValueError("state seed requires source-owned external entity IDs")
        if set(metadata) & _IDEAL_METADATA_KEYS:
            raise ValueError(
                f"lineage node {node.get('node_id')} already contains ideal metadata",
            )
        by_external_id.setdefault(external_id, []).append(node)
        lineage_source_ids.add(str(node["source_envelope_id"]))
    for edge in graph["edges"]:
        for span in edge["evidence_spans"]:
            lineage_source_ids.add(str(span["source_envelope_id"]))
    if not by_external_id:
        raise ValueError("lineage external entity denominator is empty")
    return graph, by_external_id, lineage_source_ids


def _representative(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return min(
        nodes,
        key=lambda node: (
            _timestamp(
                str(node["occurred_at"]),
                label=f"lineage node {node['node_id']} occurred_at",
            ),
            str(node["node_id"]),
        ),
    )


def _stable_suffix(external_id: str) -> str:
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]


def _receipt_predicates(
    external_id: str,
    receipt_bindings: tuple[dict[str, Any], dict[str, Any]],
) -> list[dict[str, str]]:
    suffix = _stable_suffix(external_id)
    names = ("coverage", "normalization-parity")
    return [
        {
            "predicate_id": f"predicate:{name}:{suffix}",
            "receipt_reference": str(binding["reference"]),
            "result": str(binding["result"]),
        }
        for name, binding in zip(names, receipt_bindings, strict=True)
    ]


def _resolve_lineage(
    graph: dict[str, Any],
    nodes_by_external_id: Mapping[str, list[dict[str, Any]]],
    entity_ids: Mapping[str, str],
    receipt_bindings: tuple[dict[str, Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    representatives = {
        external_id: _representative(nodes) for external_id, nodes in nodes_by_external_id.items()
    }
    representative_metadata: dict[str, dict[str, str]] = {}
    for external_id, representative in representatives.items():
        suffix = _stable_suffix(external_id)
        representative_metadata[external_id] = {
            "representative_node_id": str(representative["node_id"]),
            "source_envelope_id": str(representative["source_envelope_id"]),
            "ideal_form_id": f"ideal:governance-snapshot-classification:{suffix}",
        }

    for node in graph["nodes"]:
        metadata = node["metadata"]
        external_id = str(metadata["entity_id"])
        metadata["entity_id"] = entity_ids[external_id]
        if node["node_id"] == representative_metadata[external_id]["representative_node_id"]:
            metadata.update(
                {
                    "active": True,
                    "ideal_form_id": representative_metadata[external_id]["ideal_form_id"],
                    "predicate_receipts": _receipt_predicates(
                        external_id,
                        receipt_bindings,
                    ),
                },
            )
    validate_lineage_graph(graph, snapshot_id=str(graph["frozen_snapshot_id"]))
    return graph, representative_metadata


def _metric_definition(snapshot_id: str) -> MetricDefinition:
    return MetricDefinition(
        metric_id=OBSERVATION_METRIC_ID,
        name="Governance source evidence present",
        metric_type=MetricType.GAUGE,
        unit="boolean",
        description=(
            "One when the registered entity is linked to immutable source "
            "evidence in the frozen governance snapshot."
        ),
        aggregation=AggregationPolicy.LATEST,
        metadata={"snapshot_id": snapshot_id},
    )


def _entity_metadata(
    *,
    external_id: str,
    owner_reference: str,
    snapshot_id: str,
) -> dict[str, str]:
    return {
        "governance_external_id": external_id,
        "governance_snapshot_id": snapshot_id,
        "owner": owner_reference,
    }


def _fresh_root(path: Path) -> bool:
    if path.is_symlink():
        raise ValueError("state root cannot be a symbolic link")
    if not path.exists():
        return True
    if not path.is_dir():
        raise ValueError("state root must be a directory")
    return not any(path.iterdir())


def _validate_output_locations(
    state_root: Path,
    resolved_lineage_out: Path,
    crosswalk_out: Path,
) -> None:
    resolved_state = state_root.resolve()
    outputs = (resolved_lineage_out.resolve(), crosswalk_out.resolve())
    if outputs[0] == outputs[1]:
        raise ValueError("resolved lineage and crosswalk outputs must differ")
    for output in outputs:
        if output == resolved_state or output.is_relative_to(resolved_state):
            raise ValueError("seed outputs must live outside the exact state root")


def _build_crosswalk(
    *,
    snapshot_id: str,
    snapshot_at: str,
    seed: Mapping[str, Any],
    lineage_input: Mapping[str, Any],
    resolved_lineage: Mapping[str, Any],
    seed_entries: Mapping[str, Mapping[str, str]],
    entity_ids: Mapping[str, str],
    representative_metadata: Mapping[str, Mapping[str, str]],
    receipt_bindings: tuple[dict[str, Any], dict[str, Any]],
    state_file_digests: Mapping[str, str],
) -> dict[str, Any]:
    entries = []
    for external_id in sorted(seed_entries):
        seed_entry = seed_entries[external_id]
        representative = representative_metadata[external_id]
        entries.append(
            {
                "external_id": external_id,
                "entity_id": entity_ids[external_id],
                "entity_type": seed_entry["entity_type"],
                "display_name": seed_entry["display_name"],
                "owner_reference": seed_entry["owner_reference"],
                "representative_node_id": representative["representative_node_id"],
                "source_envelope_id": representative["source_envelope_id"],
                "observation_metric_id": OBSERVATION_METRIC_ID,
                "ideal_form_id": representative["ideal_form_id"],
            },
        )
    body = {
        "contract_name": CROSSWALK_CONTRACT,
        "contract_version": 1,
        "crosswalk_id": f"ontologia-governance-state:{snapshot_id}",
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "seed_digest": content_digest(seed),
        "lineage_input_digest": content_digest(lineage_input),
        "resolved_lineage_digest": content_digest(resolved_lineage),
        "state_file_digests": dict(sorted(state_file_digests.items())),
        "receipt_bindings": [deepcopy(binding) for binding in receipt_bindings],
        "entries": entries,
        "counts": {
            "external_entities": len(entries),
            "registered_entities": len(entries),
            "resolved_lineage_nodes": len(resolved_lineage["nodes"]),
        },
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    return {**body, "crosswalk_digest": content_digest(body)}


def _validate_crosswalk(
    value: Any,
    *,
    expected_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("persisted entity crosswalk must be an object")
    if value.get("contract_name") != CROSSWALK_CONTRACT or value.get("contract_version") != 1:
        raise ValueError("persisted entity crosswalk contract mismatch")
    if value.get("crosswalk_digest") != _digest_excluding(value, "crosswalk_digest"):
        raise ValueError("persisted entity crosswalk digest mismatch")
    for field_name in (
        "snapshot_id",
        "snapshot_at",
        "seed_digest",
        "lineage_input_digest",
    ):
        if value.get(field_name) != expected_bindings[field_name]:
            raise ValueError(f"persisted entity crosswalk {field_name} is stale")
    if value.get("receipt_bindings") != expected_bindings["receipt_bindings"]:
        raise ValueError("persisted entity crosswalk receipt bindings are stale")
    state_file_digests = value.get("state_file_digests")
    if (
        not isinstance(state_file_digests, Mapping)
        or set(state_file_digests) != _EXACT_STATE_FILES
        or not all(
            isinstance(digest, str) and _DIGEST_PATTERN.fullmatch(digest)
            for digest in state_file_digests.values()
        )
    ):
        raise ValueError("persisted entity crosswalk state file digests are invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("persisted entity crosswalk entries are invalid")
    external_ids = [
        str(entry.get("external_id")) for entry in entries if isinstance(entry, Mapping)
    ]
    entity_ids = [str(entry.get("entity_id")) for entry in entries if isinstance(entry, Mapping)]
    if (
        len(external_ids) != len(entries)
        or external_ids != sorted(external_ids)
        or len(external_ids) != len(set(external_ids))
        or len(entity_ids) != len(set(entity_ids))
        or not all(_ENTITY_UID_PATTERN.fullmatch(uid) for uid in entity_ids)
    ):
        raise ValueError("persisted entity crosswalk denominator is invalid")
    return deepcopy(dict(value))


def _state_expected_files(store: RegistryStore) -> None:
    children = list(store.store_dir.iterdir())
    symbolic_links = sorted(path.name for path in children if path.is_symlink())
    files = {path.name for path in children if path.is_file() and not path.is_symlink()}
    other_entries = sorted(
        path.name for path in children if not path.is_file() and not path.is_symlink()
    )
    if files != _EXACT_STATE_FILES or symbolic_links or other_entries:
        raise ValueError(
            "state root contains unexpected filesystem entries "
            f"(files={sorted(files)!r}, symlinks={symbolic_links!r}, "
            f"other={other_entries!r})",
        )


def _state_file_digests(store: RegistryStore) -> dict[str, str]:
    _state_expected_files(store)
    return {
        file_name: f"sha256:{hashlib.sha256((store.store_dir / file_name).read_bytes()).hexdigest()}"
        for file_name in sorted(_EXACT_STATE_FILES)
    }


def _validate_exact_state(
    store: RegistryStore,
    *,
    crosswalk: Mapping[str, Any],
) -> None:
    if store.authority_graph.nodes() or store.authority_graph.edges():
        raise ValueError("state root authority graph must remain empty before reconcile")
    if store.edge_index.all_hierarchy_edges() or store.edge_index.all_relation_edges():
        raise ValueError("state root contains unexpected structural relations")
    if store.lineage_index.all_records():
        raise ValueError("state root contains unexpected legacy lineage")
    if store.variable_store.to_list():
        raise ValueError("state root contains unexpected variables")
    if store.quarantine_diagnostics:
        raise ValueError("state root contains quarantine diagnostics")

    entries = crosswalk["entries"]
    expected_by_uid = {str(entry["entity_id"]): entry for entry in entries}
    entities = {entity.uid: entity for entity in store.list_entities()}
    if set(entities) != set(expected_by_uid):
        raise ValueError("state root entity denominator differs from persisted crosswalk")
    snapshot_id = str(crosswalk["snapshot_id"])
    snapshot_at = str(crosswalk["snapshot_at"])
    for uid, expected in expected_by_uid.items():
        entity = entities[uid]
        if (
            entity.entity_type.value != expected["entity_type"]
            or entity.lifecycle_status is not LifecycleStatus.ACTIVE
            or entity.created_at != snapshot_at
            or entity.created_by != CREATED_BY
            or entity.metadata
            != _entity_metadata(
                external_id=str(expected["external_id"]),
                owner_reference=str(expected["owner_reference"]),
                snapshot_id=snapshot_id,
            )
        ):
            raise ValueError(f"state root entity {uid} differs from persisted crosswalk")
        names = store.name_history(uid)
        if (
            len(names) != 1
            or names[0].display_name != expected["display_name"]
            or names[0].valid_from != snapshot_at
            or names[0].valid_to is not None
            or names[0].is_primary is not True
            or names[0].source != CREATED_BY
        ):
            raise ValueError(f"state root name history for {uid} is not exact")

    metric = _metric_definition(snapshot_id)
    metrics = store.list_metrics()
    if len(metrics) != 1 or metrics[0].to_dict() != metric.to_dict():
        raise ValueError("state root governance observation metric is not exact")
    observations = store.observation_store.query()
    if len(observations) != len(entries):
        raise ValueError("state root observation denominator is not exact")
    observations_by_uid = {observation.entity_id: observation for observation in observations}
    if len(observations_by_uid) != len(observations):
        raise ValueError("state root contains duplicate entity observations")
    for uid, expected in expected_by_uid.items():
        observation = observations_by_uid.get(uid)
        evidence = [f"source:{expected['source_envelope_id']}"]
        if (
            observation is None
            or observation.metric_id != OBSERVATION_METRIC_ID
            or observation.value != 1.0
            or observation.timestamp != snapshot_at
            or observation.source != OBSERVATION_SOURCE
            or observation.metadata != {"evidence_references": evidence}
        ):
            raise ValueError(f"state root observation for {uid} is not snapshot-exact")

    events = store.events(limit=len(entries) + 1)
    if len(events) != len(entries):
        raise ValueError("state root seed event denominator is not exact")
    events_by_uid = {
        event.subject_entity: event
        for event in events
        if event.event_type == "entity.created" and event.subject_entity is not None
    }
    if set(events_by_uid) != set(expected_by_uid):
        raise ValueError("state root seed events are not exact")
    for uid, event in events_by_uid.items():
        expected = expected_by_uid[uid]
        if (
            event.source != CREATED_BY
            or event.changed_property is not None
            or event.previous_value is not None
            or event.new_value is not None
            or event.payload.get("entity_type") != expected["entity_type"]
            or event.payload.get("display_name") != expected["display_name"]
            or set(event.payload) != {"entity_type", "display_name"}
            or _timestamp(event.timestamp, label=f"state seed event {uid} timestamp")
            < _timestamp(snapshot_at, label="snapshot_at")
        ):
            raise ValueError(f"state root seed event for {uid} is stale")
    if store.quarantine_diagnostics:
        raise ValueError("state root contains quarantine diagnostics")
    actual_state_digests = _state_file_digests(store)
    if actual_state_digests != crosswalk.get("state_file_digests"):
        raise ValueError("state root bytes differ from persisted crosswalk")


def _render_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    rendered = _render_json(value)
    if path.is_file():
        if path.read_text(encoding="utf-8") != rendered:
            raise ValueError(f"existing seed output is stale: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def _load_existing_output(path: Path) -> Any:
    try:
        rendered = path.read_text(encoding="utf-8")
        value = json.loads(rendered)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load persisted seed output {path.name}") from exc
    if not isinstance(value, Mapping) or rendered != _render_json(value):
        raise ValueError(f"persisted seed output is not canonical: {path.name}")
    return value


def seed_governance_state(
    *,
    lineage_graph: Any,
    coverage_receipt: Any,
    normalization_parity_receipt: Any,
    seed: Any,
    snapshot_id: str,
    snapshot_at: str,
    state_root: Path,
    resolved_lineage_out: Path,
    crosswalk_out: Path,
    max_entities: int = 100_000,
) -> StateSeedResult:
    """Create or verify one exact pre-import registry denominator."""
    if max_entities <= 0:
        raise ValueError("max_entities must be positive")
    _timestamp(snapshot_at, label="snapshot_at")
    _validate_output_locations(state_root, resolved_lineage_out, crosswalk_out)
    graph, nodes_by_external_id, lineage_source_ids = _lineage_bindings(
        lineage_graph,
        snapshot_id=snapshot_id,
    )
    if (
        len(nodes_by_external_id) > max_entities
        or len(graph["nodes"]) > max_entities
        or len(graph["edges"]) > max_entities
    ):
        raise ValueError("state seed work denominator exceeds max_entities")
    seed_entries = _validate_seed(
        seed,
        snapshot_id=snapshot_id,
        snapshot_at=snapshot_at,
        external_ids=set(nodes_by_external_id),
    )
    receipt_bindings = _validate_receipts(
        coverage_receipt,
        normalization_parity_receipt,
        snapshot_id=snapshot_id,
        snapshot_at=snapshot_at,
        lineage_source_ids=lineage_source_ids,
    )
    expected_bindings = {
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "seed_digest": content_digest(seed),
        "lineage_input_digest": content_digest(lineage_graph),
        "receipt_bindings": [deepcopy(binding) for binding in receipt_bindings],
    }
    fresh = _fresh_root(state_root)
    output_presence = (resolved_lineage_out.exists(), crosswalk_out.exists())
    if fresh and any(output_presence):
        raise ValueError("fresh state root cannot reuse detached seed outputs")
    if not fresh and not all(output_presence):
        raise ValueError("existing state root lacks its persisted seed outputs")

    if not fresh:
        crosswalk = _validate_crosswalk(
            _load_existing_output(crosswalk_out),
            expected_bindings=expected_bindings,
        )
        entity_ids = {
            str(entry["external_id"]): str(entry["entity_id"]) for entry in crosswalk["entries"]
        }
        if set(entity_ids) != set(nodes_by_external_id):
            raise ValueError("persisted entity crosswalk external denominator is stale")
        resolved_lineage, representative_metadata = _resolve_lineage(
            graph,
            nodes_by_external_id,
            entity_ids,
            receipt_bindings,
        )
        store = open_store(state_root)
        _validate_exact_state(store, crosswalk=crosswalk)
        expected_crosswalk = _build_crosswalk(
            snapshot_id=snapshot_id,
            snapshot_at=snapshot_at,
            seed=seed,
            lineage_input=lineage_graph,
            resolved_lineage=resolved_lineage,
            seed_entries=seed_entries,
            entity_ids=entity_ids,
            representative_metadata=representative_metadata,
            receipt_bindings=receipt_bindings,
            state_file_digests=_state_file_digests(store),
        )
        if crosswalk != expected_crosswalk:
            raise ValueError("persisted entity crosswalk no longer matches owner inputs")
        if _load_existing_output(resolved_lineage_out) != resolved_lineage:
            raise ValueError("persisted resolved lineage is stale")
        return StateSeedResult(
            resolved_lineage=resolved_lineage,
            crosswalk=crosswalk,
            replayed=True,
        )

    store = open_store(state_root)
    snapshot_ms = int(_timestamp(snapshot_at, label="snapshot_at").timestamp() * 1000)
    entity_ids: dict[str, str] = {}
    for external_id in sorted(seed_entries):
        entry = seed_entries[external_id]
        entity = store.create_entity(
            EntityType(entry["entity_type"]),
            entry["display_name"],
            created_by=CREATED_BY,
            metadata=_entity_metadata(
                external_id=external_id,
                owner_reference=entry["owner_reference"],
                snapshot_id=snapshot_id,
            ),
            timestamp_ms=snapshot_ms,
            created_at=snapshot_at,
        )
        entity_ids[external_id] = entity.uid

    resolved_lineage, representative_metadata = _resolve_lineage(
        graph,
        nodes_by_external_id,
        entity_ids,
        receipt_bindings,
    )
    for external_id in sorted(seed_entries):
        store.record_observation(
            OBSERVATION_METRIC_ID,
            entity_ids[external_id],
            1.0,
            source=OBSERVATION_SOURCE,
            metadata={
                "evidence_references": [
                    f"source:{representative_metadata[external_id]['source_envelope_id']}",
                ],
            },
            timestamp=snapshot_at,
        )
    store.register_metric(_metric_definition(snapshot_id))
    store.save()
    state_file_digests = _state_file_digests(store)

    crosswalk = _build_crosswalk(
        snapshot_id=snapshot_id,
        snapshot_at=snapshot_at,
        seed=seed,
        lineage_input=lineage_graph,
        resolved_lineage=resolved_lineage,
        seed_entries=seed_entries,
        entity_ids=entity_ids,
        representative_metadata=representative_metadata,
        receipt_bindings=receipt_bindings,
        state_file_digests=state_file_digests,
    )
    _validate_exact_state(store, crosswalk=crosswalk)
    _atomic_write(resolved_lineage_out, resolved_lineage)
    _atomic_write(crosswalk_out, crosswalk)
    return StateSeedResult(
        resolved_lineage=resolved_lineage,
        crosswalk=crosswalk,
        replayed=False,
    )


__all__ = [
    "CROSSWALK_CONTRACT",
    "SEED_CONTRACT",
    "SEED_RESULT_CONTRACT",
    "StateSeedResult",
    "seed_governance_state",
]
