"""Tests for the exact pre-import governance registry seed owner."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ontologia.cli import main as cli_main
from ontologia.governance.memory import (
    AuthorityClass,
    AuthorityLane,
    AuthorityNode,
    EvidenceSpan,
    content_digest,
)
from ontologia.governance.state_seed import (
    CROSSWALK_CONTRACT,
    OBSERVATION_METRIC_ID,
    SEED_CONTRACT,
    seed_governance_state,
)
from ontologia.registry.store import open_store

SNAPSHOT = "snapshot:governance-seed-fixture"
SNAPSHOT_AT = "2026-07-16T20:00:00Z"
GENERATED_AT = "2026-07-16T20:05:00Z"
SNAPSHOT_DIGEST = "sha256:" + "d" * 64


def _node(
    node_id: str,
    *,
    external_id: str,
    source_id: str,
    occurred_at: str,
    lane: str,
    node_type: str,
    body_character: str,
) -> dict:
    return {
        "node_id": node_id,
        "lane": lane,
        "node_type": node_type,
        "source_envelope_id": source_id,
        "occurred_at": occurred_at,
        "authority_class": "operator_intent" if lane == "operator_intent" else "artifact",
        "summary": f"Reviewed source for {external_id}.",
        "content_hash": "sha256:" + body_character * 64,
        "review_state": "reviewed",
        "metadata": {
            "entity_id": external_id,
            "zoom_level": "session" if lane == "operator_intent" else "document",
        },
    }


def _lineage() -> dict:
    return {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": f"lineage:{SNAPSHOT}",
        "generated_at": GENERATED_AT,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            _node(
                "intent:session-later",
                external_id="session:alpha",
                source_id="src_session_later",
                occurred_at="2026-07-16T19:02:00Z",
                lane="operator_intent",
                node_type="source_event",
                body_character="a",
            ),
            _node(
                "intent:session-origin",
                external_id="session:alpha",
                source_id="src_session_origin",
                occurred_at="2026-07-16T19:01:00Z",
                lane="operator_intent",
                node_type="source_event",
                body_character="b",
            ),
            _node(
                "artifact:repository",
                external_id="repository:beta",
                source_id="src_repository",
                occurred_at="2026-07-16T19:03:00Z",
                lane="artifact",
                node_type="specification",
                body_character="c",
            ),
            _node(
                "artifact:document",
                external_id="document:gamma",
                source_id="src_document",
                occurred_at="2026-07-16T19:04:00Z",
                lane="artifact",
                node_type="document",
                body_character="e",
            ),
        ],
        "edges": [
            {
                "edge_id": "edge:session-refines-repository",
                "from_node": "intent:session-origin",
                "to_node": "artifact:repository",
                "edge_type": "refines",
                "evidence_spans": [
                    {
                        "source_envelope_id": "src_session_origin",
                        "reference": "source:src_session_origin",
                        "body_hash": "sha256:" + "b" * 64,
                    },
                ],
                "confidence": 1.0,
                "review_state": "reviewed",
                "reviewer_reference": "reviewer:operator",
            },
        ],
    }


def _readiness(*, ready: bool) -> dict:
    return {
        "exact_all": True,
        "ready": ready,
        "status": "ready" if ready else "blocked",
        "unresolved_blockers": [] if ready else ["raw_missing_export"],
        "quarantines": [],
        "missing_requirements": [],
        "citation_debt": [],
        "incomplete_predicates": [],
    }


def _receipts(*, ready: bool = False) -> tuple[dict, dict]:
    source_units = [
        ("src_session_later", "raw_session_later", "a"),
        ("src_session_origin", "raw_session_origin", "b"),
        ("src_repository", "raw_repository", "c"),
        ("src_document", "raw_document", "d"),
    ]
    event_by_raw_unit = {
        raw_unit_id: f"evt_{character * 64}" for _source_id, raw_unit_id, character in source_units
    }
    raw_units = [
        {
            "raw_unit_id": raw_unit_id,
            "content_hash": f"sha256:{character * 64}",
        }
        for _source_id, raw_unit_id, character in source_units
    ]
    promotions = []
    for _source_id, raw_unit_id, character in source_units:
        promotion = {
            "raw_unit_id": raw_unit_id,
            "raw_unit_content_hash": f"sha256:{character * 64}",
        }
        if ready or raw_unit_id != "raw_document":
            promotion["event_ids"] = [event_by_raw_unit[raw_unit_id]]
        else:
            promotion["disposition"] = {
                "type": "blocked",
                "owner_reference": "owner:document-export",
                "failed_predicate": "official document export is in custody",
                "next_action": "Acquire the read-only export and rerun.",
                "evidence_references": ["receipt:blocker-document-export"],
            }
        promotions.append(promotion)
    parity_readiness = _readiness(ready=ready)
    if not ready:
        parity_readiness["unresolved_blockers"] = ["raw_document"]
    parity_body = {
        "contract_name": "normalization-parity-receipt.v1",
        "contract_version": 1,
        "receipt_id": "normalization-parity-fixture",
        "snapshot_id": SNAPSHOT,
        "snapshot_digest": SNAPSHOT_DIGEST,
        "generated_at": GENERATED_AT,
        "input_census": {
            "census_id": "census:fixture",
            "census_reference": "source-census.v1.json",
            "census_digest": "sha256:" + "e" * 64,
            "raw_unit_ids": [item["raw_unit_id"] for item in raw_units],
            "raw_units": raw_units,
        },
        "output_events": {
            "event_set_reference": "normalized-events.v1.jsonl",
            "event_set_digest": "sha256:" + "f" * 64,
            "event_ids": sorted(
                event_by_raw_unit[raw_unit_id]
                for _source_id, raw_unit_id, _character in source_units
                if ready or raw_unit_id != "raw_document"
            ),
        },
        "promotions": promotions,
        "readiness": parity_readiness,
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    parity = {
        **parity_body,
        "receipt_digest": content_digest(parity_body),
    }
    coverage_readiness = _readiness(ready=ready)
    if not ready:
        coverage_readiness["unresolved_blockers"] = [
            "receipt:normalization-parity-fixture#/readiness/unresolved_blockers",
            "src_document",
        ]
    sources = []
    for source_id, _raw_unit_id, _character in source_units:
        source = {
            "source_id": source_id,
            "status": "parsed" if ready or source_id != "src_document" else "owner_blocked",
            "accessible": ready or source_id != "src_document",
            "evidence_references": [f"receipt:source:{source_id}"],
        }
        if source["status"] != "parsed":
            source.update(
                {
                    "owner_reference": "owner:document-export",
                    "failed_predicate": "official document export is in custody",
                    "next_action": "Acquire the read-only export and rerun.",
                },
            )
        sources.append(source)
    residual_owners = (
        [
            {
                "source_id": "src_document",
                "owner_reference": "owner:document-export",
                "failed_predicate": "official document export is in custody",
                "next_action": "Acquire the read-only export and rerun.",
            },
        ]
        if not ready
        else []
    )
    coverage_body = {
        "contract_name": "coverage-receipt.v1",
        "contract_version": 1,
        "receipt_id": "coverage:fixture",
        "snapshot_id": SNAPSHOT,
        "generated_at": GENERATED_AT,
        "denominator": {
            "discovery_manifest_reference": "manifest:fixture",
            "count": len(sources),
            "manifest_hash": content_digest(sources),
        },
        "sources": sources,
        "counts": {
            "acquired": 0,
            "parsed": len(sources) if ready else len(sources) - 1,
            "quarantined": 0,
            "inaccessible": 0,
            "missing_expected": 0,
            "owner_blocked": 0 if ready else 1,
        },
        "constitutional_scope": {
            "scope_reference": "scope:fixture-lineage-sources",
            "exact_all": True,
            "blocked_scopes": [] if ready else ["src_document"],
            "missing_requirements": [],
            "ready": ready,
        },
        "exact_all": coverage_readiness["exact_all"],
        "ready": coverage_readiness["ready"],
        "closure_status": ("ready" if ready else "closed_with_owner_routed_debt"),
        **{
            field_name: coverage_readiness[field_name]
            for field_name in (
                "unresolved_blockers",
                "quarantines",
                "missing_requirements",
                "citation_debt",
                "incomplete_predicates",
            )
        },
        "residual_owners": residual_owners,
    }
    coverage = {
        **coverage_body,
        "receipt_hash": content_digest(coverage_body),
    }
    return coverage, parity


def _lineage_with_31_sources() -> dict:
    lineage = _lineage()
    for index in range(4, 31):
        lineage["nodes"].append(
            _node(
                f"artifact:extra-{index:02d}",
                external_id="repository:beta",
                source_id=f"src_extra_{index:02d}",
                occurred_at=f"2026-07-16T19:10:{index:02d}Z",
                lane="artifact",
                node_type="document",
                body_character="0123456789abcdef"[index % 16],
            ),
        )
    return lineage


def _independent_denominator_receipts() -> tuple[dict, dict]:
    parity_id = "normalization-parity-independent-fixture"
    raw_units = [
        {
            "raw_unit_id": f"raw_unit_{index:02d}",
            "content_hash": f"sha256:{index + 1:064x}",
        }
        for index in range(17)
    ]
    event_ids_by_raw_unit = {raw_unit["raw_unit_id"]: [] for raw_unit in raw_units}
    for index in range(31):
        raw_unit_id = raw_units[index % len(raw_units)]["raw_unit_id"]
        event_ids_by_raw_unit[raw_unit_id].append(f"evt_{index + 101:064x}")
    promotions = [
        {
            "raw_unit_id": raw_unit["raw_unit_id"],
            "raw_unit_content_hash": raw_unit["content_hash"],
            "event_ids": event_ids_by_raw_unit[raw_unit["raw_unit_id"]],
        }
        for raw_unit in raw_units
    ]
    parity_body = {
        "contract_name": "normalization-parity-receipt.v1",
        "contract_version": 1,
        "receipt_id": parity_id,
        "snapshot_id": SNAPSHOT,
        "snapshot_digest": SNAPSHOT_DIGEST,
        "generated_at": GENERATED_AT,
        "input_census": {
            "census_id": "census:independent-fixture",
            "census_reference": "source-census.v1.json",
            "census_digest": "sha256:" + "9" * 64,
            "raw_unit_ids": [raw_unit["raw_unit_id"] for raw_unit in raw_units],
            "raw_units": raw_units,
        },
        "output_events": {
            "event_set_reference": "normalized-events.v1.jsonl",
            "event_set_digest": "sha256:" + "8" * 64,
            "event_ids": sorted(
                event_id for event_ids in event_ids_by_raw_unit.values() for event_id in event_ids
            ),
        },
        "promotions": promotions,
        "readiness": {
            "exact_all": True,
            "unresolved_blockers": ["blocker:official-browser-export"],
            "quarantines": ["quarantine:adapter-diagnostic"],
            "missing_requirements": [],
            "citation_debt": [],
            "incomplete_predicates": [],
            "ready": False,
            "status": "blocked",
        },
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    parity = {
        **parity_body,
        "receipt_digest": content_digest(parity_body),
    }

    lineage = _lineage_with_31_sources()
    source_ids = sorted(
        {node["source_envelope_id"] for node in lineage["nodes"]}
        | {
            span["source_envelope_id"]
            for edge in lineage["edges"]
            for span in edge["evidence_spans"]
        },
    )
    sources = [
        {
            "source_id": source_id,
            "status": "parsed",
            "accessible": True,
            "evidence_references": [f"source-envelope:{source_id}"],
        }
        for source_id in source_ids
    ]
    coverage_body = {
        "contract_name": "coverage-receipt.v1",
        "contract_version": 1,
        "receipt_id": "coverage:independent-fixture",
        "snapshot_id": SNAPSHOT,
        "generated_at": GENERATED_AT,
        "denominator": {
            "discovery_manifest_reference": "coverage-receipt.v1.json#/sources",
            "count": len(sources),
            "manifest_hash": content_digest(sources),
        },
        "sources": sources,
        "counts": {
            "acquired": 0,
            "parsed": len(sources),
            "quarantined": 0,
            "inaccessible": 0,
            "missing_expected": 0,
            "owner_blocked": 0,
        },
        "constitutional_scope": {
            "scope_reference": "coverage-receipt.v1.json#/sources",
            "exact_all": True,
            "blocked_scopes": [],
            "missing_requirements": [],
            "ready": True,
        },
        "exact_all": True,
        "ready": False,
        "unresolved_blockers": [
            "assertion:governance-candidate",
            f"receipt:{parity_id}#/readiness/unresolved_blockers",
        ],
        "quarantines": [f"receipt:{parity_id}#/readiness/quarantines"],
        "missing_requirements": ["ratified constitutional record"],
        "citation_debt": ["assertion:governance-candidate"],
        "incomplete_predicates": ["IF-GOV-001"],
        "closure_status": "blocked",
        "residual_owners": [],
    }
    coverage = {
        **coverage_body,
        "receipt_hash": content_digest(coverage_body),
    }
    return coverage, parity


def _seed() -> dict:
    return {
        "contract_name": SEED_CONTRACT,
        "contract_version": 1,
        "snapshot_id": SNAPSHOT,
        "snapshot_at": SNAPSHOT_AT,
        "entities": [
            {
                "external_id": "session:alpha",
                "entity_type": "session",
                "display_name": "Session alpha",
                "owner_reference": "repo:organvm/session-meta",
            },
            {
                "external_id": "repository:beta",
                "entity_type": "repo",
                "display_name": "Repository beta",
                "owner_reference": "repo:organvm/organvm-corpvs-testamentvm",
            },
            {
                "external_id": "document:gamma",
                "entity_type": "document",
                "display_name": "Document gamma",
                "owner_reference": "repo:organvm/organvm-corpvs-testamentvm",
            },
        ],
    }


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "state",
        tmp_path / "resolved-lineage.json",
        tmp_path / "entity-crosswalk.json",
    )


def _seed_state(
    tmp_path: Path,
    *,
    lineage: dict | None = None,
    seed: dict | None = None,
    ready: bool = False,
):
    coverage, parity = _receipts(ready=ready)
    state_root, resolved, crosswalk = _paths(tmp_path)
    result = seed_governance_state(
        lineage_graph=lineage or _lineage(),
        coverage_receipt=coverage,
        normalization_parity_receipt=parity,
        seed=seed or _seed(),
        snapshot_id=SNAPSHOT,
        snapshot_at=SNAPSHOT_AT,
        state_root=state_root,
        resolved_lineage_out=resolved,
        crosswalk_out=crosswalk,
    )
    return result, state_root, resolved, crosswalk


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_seed_state_materializes_dynamic_denominator_and_exact_replay(
    tmp_path: Path,
) -> None:
    result, state_root, resolved_path, crosswalk_path = _seed_state(tmp_path)
    assert result.replayed is False
    assert result.crosswalk["contract_name"] == CROSSWALK_CONTRACT
    assert result.crosswalk["counts"] == {
        "external_entities": 3,
        "registered_entities": 3,
        "resolved_lineage_nodes": 4,
    }
    assert result.crosswalk["crosswalk_digest"] == content_digest(
        {key: value for key, value in result.crosswalk.items() if key != "crosswalk_digest"},
    )

    mapping = {entry["external_id"]: entry["entity_id"] for entry in result.crosswalk["entries"]}
    assert set(mapping) == {
        "document:gamma",
        "repository:beta",
        "session:alpha",
    }
    assert all(uid.startswith("ent_") for uid in mapping.values())
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    assert {node["metadata"]["entity_id"] for node in resolved["nodes"]} == set(mapping.values())

    ideal_nodes = [node for node in resolved["nodes"] if node["metadata"].get("ideal_form_id")]
    assert len(ideal_nodes) == 3
    session_ideal = next(
        node for node in ideal_nodes if node["metadata"]["entity_id"] == mapping["session:alpha"]
    )
    assert session_ideal["node_id"] == "intent:session-origin"
    assert [item["result"] for item in session_ideal["metadata"]["predicate_receipts"]] == [
        "blocked",
        "blocked",
    ]

    store = open_store(state_root)
    assert store.entity_count == 3
    assert store.authority_graph.nodes() == []
    assert store.authority_graph.edges() == []
    observations = store.observation_store.query()
    assert len(observations) == 3
    assert all(observation.timestamp == SNAPSHOT_AT for observation in observations)
    assert all(observation.metric_id == OBSERVATION_METRIC_ID for observation in observations)
    assert all(observation.metadata["evidence_references"] for observation in observations)

    before = _tree_bytes(tmp_path)
    replay = seed_governance_state(
        lineage_graph=_lineage(),
        coverage_receipt=_receipts()[0],
        normalization_parity_receipt=_receipts()[1],
        seed=_seed(),
        snapshot_id=SNAPSHOT,
        snapshot_at=SNAPSHOT_AT,
        state_root=state_root,
        resolved_lineage_out=resolved_path,
        crosswalk_out=crosswalk_path,
    )
    assert replay.replayed is True
    assert replay.crosswalk == result.crosswalk
    assert _tree_bytes(tmp_path) == before


def test_seed_state_derives_pass_from_both_ready_receipts(tmp_path: Path) -> None:
    result, _state, _resolved, _crosswalk = _seed_state(tmp_path, ready=True)
    ideal_nodes = [
        node for node in result.resolved_lineage["nodes"] if node["metadata"].get("ideal_form_id")
    ]
    assert ideal_nodes
    assert {
        receipt["result"]
        for node in ideal_nodes
        for receipt in node["metadata"]["predicate_receipts"]
    } == {"pass"}


def test_seed_state_keeps_31_lineage_sources_distinct_from_17_raw_units(
    tmp_path: Path,
) -> None:
    coverage, parity = _independent_denominator_receipts()
    state_root, resolved, crosswalk = _paths(tmp_path)
    result = seed_governance_state(
        lineage_graph=_lineage_with_31_sources(),
        coverage_receipt=coverage,
        normalization_parity_receipt=parity,
        seed=_seed(),
        snapshot_id=SNAPSHOT,
        snapshot_at=SNAPSHOT_AT,
        state_root=state_root,
        resolved_lineage_out=resolved,
        crosswalk_out=crosswalk,
    )
    coverage_binding, parity_binding = result.crosswalk["receipt_bindings"]
    assert coverage_binding == {
        "contract_name": "coverage-receipt.v1",
        "denominator_kind": "lineage_source_envelopes",
        "denominator_count": 31,
        "receipt_id": "coverage:independent-fixture",
        "receipt_digest": coverage["receipt_hash"],
        "reference": "receipt:coverage:independent-fixture",
        "constitutional_scope_ready": True,
        "ready": False,
        "result": "blocked",
    }
    assert parity_binding == {
        "contract_name": "normalization-parity-receipt.v1",
        "denominator_kind": "normalization_raw_units",
        "denominator_count": 17,
        "output_event_count": 31,
        "receipt_id": "normalization-parity-independent-fixture",
        "receipt_digest": parity["receipt_digest"],
        "snapshot_digest": SNAPSHOT_DIGEST,
        "reference": "receipt:normalization-parity-independent-fixture",
        "ready": False,
        "result": "blocked",
    }
    assert result.crosswalk["counts"]["resolved_lineage_nodes"] == 31
    assert max(len(promotion["event_ids"]) for promotion in parity["promotions"]) == 2
    assert coverage["constitutional_scope"]["ready"] is True
    assert result.crosswalk["receipt_bindings"][0]["ready"] is False

    before = _tree_bytes(tmp_path)
    replay = seed_governance_state(
        lineage_graph=_lineage_with_31_sources(),
        coverage_receipt=coverage,
        normalization_parity_receipt=parity,
        seed=_seed(),
        snapshot_id=SNAPSHOT,
        snapshot_at=SNAPSHOT_AT,
        state_root=state_root,
        resolved_lineage_out=resolved,
        crosswalk_out=crosswalk,
    )
    assert replay.replayed is True
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize(
    ("status", "accessible", "debt_field"),
    [
        ("acquired", True, "incomplete_predicates"),
        ("quarantined", True, "quarantines"),
        ("inaccessible", False, "unresolved_blockers"),
        ("missing_expected", False, "missing_requirements"),
        ("owner_blocked", False, "unresolved_blockers"),
    ],
)
def test_seed_state_routes_each_nonparsed_status_without_denominator_conflation(
    tmp_path: Path,
    status: str,
    accessible: bool,
    debt_field: str,
) -> None:
    coverage, parity = _receipts(ready=True)
    source = coverage["sources"][-1]
    source.update(
        {
            "status": status,
            "accessible": accessible,
            "owner_reference": "owner:coverage",
            "failed_predicate": "source envelope is parsed",
            "next_action": "Resolve the source classification and rerun.",
        },
    )
    coverage["counts"]["parsed"] = 3
    coverage["counts"][status] = 1
    for field_name in (
        "unresolved_blockers",
        "quarantines",
        "missing_requirements",
        "citation_debt",
        "incomplete_predicates",
    ):
        coverage[field_name] = ["src_document"] if field_name == debt_field else []
    coverage["constitutional_scope"].update(
        {
            "blocked_scopes": ["src_document"],
            "ready": False,
        },
    )
    coverage["ready"] = False
    coverage["closure_status"] = "closed_with_owner_routed_debt"
    coverage["residual_owners"] = [
        {
            "source_id": "src_document",
            "owner_reference": "owner:coverage",
            "failed_predicate": "source envelope is parsed",
            "next_action": "Resolve the source classification and rerun.",
        },
    ]
    coverage["denominator"]["manifest_hash"] = content_digest(
        coverage["sources"],
    )
    coverage["receipt_hash"] = content_digest(
        {key: value for key, value in coverage.items() if key != "receipt_hash"},
    )
    state_root, resolved, crosswalk = _paths(tmp_path)

    result = seed_governance_state(
        lineage_graph=_lineage(),
        coverage_receipt=coverage,
        normalization_parity_receipt=parity,
        seed=_seed(),
        snapshot_id=SNAPSHOT,
        snapshot_at=SNAPSHOT_AT,
        state_root=state_root,
        resolved_lineage_out=resolved,
        crosswalk_out=crosswalk,
    )

    binding = result.crosswalk["receipt_bindings"][0]
    assert binding["denominator_count"] == 4
    assert binding["constitutional_scope_ready"] is False
    assert binding["ready"] is False


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_seed_state_rejects_nonexact_seed_denominator(
    tmp_path: Path,
    mutation: str,
) -> None:
    seed = _seed()
    if mutation == "missing":
        seed["entities"].pop()
    elif mutation == "extra":
        seed["entities"].append(
            {
                "external_id": "document:extra",
                "entity_type": "document",
                "display_name": "Extra",
                "owner_reference": "owner:extra",
            },
        )
    else:
        seed["entities"].append(deepcopy(seed["entities"][0]))
    with pytest.raises(ValueError, match="denominator mismatch|duplicates external_id"):
        _seed_state(tmp_path, seed=seed)
    assert not (tmp_path / "state").exists()


def test_seed_state_rejects_nonempty_authority_graph(tmp_path: Path) -> None:
    result, state_root, resolved, crosswalk = _seed_state(tmp_path)
    store = open_store(state_root)
    first = result.crosswalk["entries"][0]
    store.add_authority_node(
        AuthorityNode(
            node_id="artifact:premature-import",
            lane=AuthorityLane.ARTIFACT,
            authority_class=AuthorityClass.SOURCE_DOCUMENT,
            source_family="fixture",
            source_instance=str(first["source_envelope_id"]),
            native_id="native:premature",
            observed_at=SNAPSHOT_AT,
            body_hash="sha256:" + "f" * 64,
            evidence=[
                EvidenceSpan(
                    source_id=str(first["source_envelope_id"]),
                    body_hash="sha256:" + "f" * 64,
                    snapshot_id=SNAPSHOT,
                ),
            ],
            entity_id=str(first["entity_id"]),
        ),
    )
    with pytest.raises(ValueError, match="authority graph must remain empty"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=_receipts()[0],
            normalization_parity_receipt=_receipts()[1],
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=state_root,
            resolved_lineage_out=resolved,
            crosswalk_out=crosswalk,
        )


def test_seed_state_rejects_extra_entity_and_stale_mapping(tmp_path: Path) -> None:
    _result, state_root, resolved, crosswalk = _seed_state(tmp_path)
    store = open_store(state_root)
    store.create_entity(
        entity_type=store.list_entities()[0].entity_type,
        display_name="Unexpected",
        created_by="test",
    )
    store.save()
    with pytest.raises(ValueError, match="entity denominator"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=_receipts()[0],
            normalization_parity_receipt=_receipts()[1],
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=state_root,
            resolved_lineage_out=resolved,
            crosswalk_out=crosswalk,
        )

    payload = json.loads(crosswalk.read_text(encoding="utf-8"))
    payload["entries"][0]["display_name"] = "Tampered"
    crosswalk.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="crosswalk digest mismatch"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=_receipts()[0],
            normalization_parity_receipt=_receipts()[1],
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=state_root,
            resolved_lineage_out=resolved,
            crosswalk_out=crosswalk,
        )


def test_seed_state_rejects_unbound_receipt_and_timestamp_mismatch(
    tmp_path: Path,
) -> None:
    coverage, parity = _receipts()
    parity["snapshot_id"] = "snapshot:other"
    with pytest.raises(ValueError, match="snapshot_id mismatch"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=coverage,
            normalization_parity_receipt=parity,
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=tmp_path / "state-a",
            resolved_lineage_out=tmp_path / "resolved-a.json",
            crosswalk_out=tmp_path / "crosswalk-a.json",
        )

    seed = _seed()
    seed["snapshot_at"] = "2026-07-16T19:59:59Z"
    with pytest.raises(ValueError, match="timestamp mismatch"):
        _seed_state(tmp_path / "timestamp", seed=seed)


def test_seed_state_rejects_incomplete_receipts_and_uncovered_lineage(
    tmp_path: Path,
) -> None:
    coverage, parity = _receipts()
    del coverage["denominator"]
    coverage["receipt_hash"] = content_digest(
        {key: value for key, value in coverage.items() if key != "receipt_hash"},
    )
    with pytest.raises(ValueError, match="coverage receipt contains unsupported or missing"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=coverage,
            normalization_parity_receipt=parity,
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=tmp_path / "missing-receipt-state",
            resolved_lineage_out=tmp_path / "missing-receipt-lineage.json",
            crosswalk_out=tmp_path / "missing-receipt-crosswalk.json",
        )

    lineage = _lineage()
    lineage["nodes"][0]["source_envelope_id"] = "src_fabricated"
    with pytest.raises(ValueError, match="does not exactly match coverage"):
        _seed_state(tmp_path / "uncovered-lineage", lineage=lineage)


def test_seed_state_rejects_malformed_representative_timestamp(tmp_path: Path) -> None:
    lineage = _lineage()
    lineage["nodes"][1]["occurred_at"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="must be an ISO-8601 timestamp"):
        _seed_state(tmp_path, lineage=lineage)


def test_seed_state_rejects_noncanonical_output_and_state_byte_drift(
    tmp_path: Path,
) -> None:
    _result, state_root, resolved, crosswalk = _seed_state(tmp_path / "output")
    payload = json.loads(crosswalk.read_text(encoding="utf-8"))
    crosswalk.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="persisted seed output is not canonical"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=_receipts()[0],
            normalization_parity_receipt=_receipts()[1],
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=state_root,
            resolved_lineage_out=resolved,
            crosswalk_out=crosswalk,
        )

    _result, state_root, resolved, crosswalk = _seed_state(tmp_path / "state")
    events_path = state_root / "events.jsonl"
    events = [
        json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line
    ]
    events[0]["timestamp"] = "1900-01-01T00:00:00Z"
    events_path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="state root seed event .* is stale"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=_receipts()[0],
            normalization_parity_receipt=_receipts()[1],
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=state_root,
            resolved_lineage_out=resolved,
            crosswalk_out=crosswalk,
        )


def test_seed_state_rejects_detached_outputs_and_maximum(tmp_path: Path) -> None:
    (_state, resolved, _crosswalk) = _paths(tmp_path)
    resolved.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="detached seed outputs"):
        _seed_state(tmp_path)

    coverage, parity = _receipts()
    with pytest.raises(ValueError, match="exceeds max_entities"):
        seed_governance_state(
            lineage_graph=_lineage(),
            coverage_receipt=coverage,
            normalization_parity_receipt=parity,
            seed=_seed(),
            snapshot_id=SNAPSHOT,
            snapshot_at=SNAPSHOT_AT,
            state_root=tmp_path / "bounded-state",
            resolved_lineage_out=tmp_path / "bounded-resolved.json",
            crosswalk_out=tmp_path / "bounded-crosswalk.json",
            max_entities=2,
        )


def test_seed_state_cli_and_contract_schemas(tmp_path: Path, capsys) -> None:
    coverage, parity = _receipts()
    values = {
        "lineage": _lineage(),
        "coverage": coverage,
        "parity": parity,
        "seed": _seed(),
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[name] = path
    state_root, resolved, crosswalk = _paths(tmp_path)
    assert (
        cli_main(
            [
                "governance",
                "seed-state",
                "--lineage",
                str(paths["lineage"]),
                "--coverage",
                str(paths["coverage"]),
                "--normalization-parity",
                str(paths["parity"]),
                "--seed",
                str(paths["seed"]),
                "--snapshot-id",
                SNAPSHOT,
                "--snapshot-at",
                SNAPSHOT_AT,
                "--state-root",
                str(state_root),
                "--resolved-lineage-out",
                str(resolved),
                "--crosswalk-out",
                str(crosswalk),
            ],
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["contract_name"] == "governance-state-seed-result.v1"
    assert summary["counts"]["registered_entities"] == 3

    schema_root = Path(__file__).parents[1] / "schemas"
    seed_schema = json.loads(
        (schema_root / "governance-state-seed.v1.schema.json").read_text(),
    )
    crosswalk_schema = json.loads(
        (schema_root / "governance-entity-crosswalk.v1.schema.json").read_text(),
    )
    assert seed_schema["properties"]["contract_name"]["const"] == SEED_CONTRACT
    assert crosswalk_schema["properties"]["contract_name"]["const"] == CROSSWALK_CONTRACT
    receipt_bindings = crosswalk_schema["properties"]["receipt_bindings"]
    assert receipt_bindings["prefixItems"] == [
        {"$ref": "#/$defs/coverageReceiptBinding"},
        {"$ref": "#/$defs/parityReceiptBinding"},
    ]
    assert (
        crosswalk_schema["$defs"]["parityReceiptBinding"]["required"].count(
            "snapshot_digest",
        )
        == 1
    )
