"""Focused tests for authority memory, self-images, and quarantine receipts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ontologia.cli import main as cli_main
from ontologia.entity.identity import EntityType, LifecycleStatus
from ontologia.entity.lineage import LineageType
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
from ontologia.governance.reconcile import (
    build_reconcile_inputs,
    build_self_image_set,
    build_snapshot_evidence_index,
    export_lineage_graph,
    import_lineage_graph,
    load_materialized_snapshot_bundle,
    reconcile_governance_snapshot,
    reconcile_snapshot_bundle,
    validate_self_image_set,
)
from ontologia.registry.store import RegistryStore

SNAPSHOT = "snapshot:2026-07-16-governance"
OBSERVED = "2026-07-16T12:00:00Z"
RECONCILED = "2099-07-16T12:00:00Z"
GENERATED = "2099-07-16T12:05:00Z"
BODY_HASH = "sha256:" + "a" * 64
SNAPSHOT_DIGEST = "sha256:" + "c" * 64


def test_governance_canonical_json_is_rfc8785() -> None:
    assert canonical_json({"\U0001f600": 1, "\ue000": 2}) == '{"😀":1,"\ue000":2}'
    assert canonical_json(1.0) == "1"
    with pytest.raises(ValueError, match="safe integer domain"):
        canonical_json(2**60)


def _evidence(source_id: str = "src_event_1") -> EvidenceSpan:
    return EvidenceSpan(
        source_id=source_id,
        body_hash=BODY_HASH,
        snapshot_id=SNAPSHOT,
        start_offset=0,
        end_offset=24,
        independence_group="operator-session",
    )


def _intent_node(node_id: str, entity_id: str | None = None) -> AuthorityNode:
    return AuthorityNode(
        node_id=node_id,
        lane=AuthorityLane.OPERATOR_INTENT,
        authority_class=AuthorityClass.OPERATOR_DIRECTIVE,
        source_family="operator-session",
        source_instance="session-1",
        native_id=f"native:{node_id}",
        observed_at=OBSERVED,
        body_hash=BODY_HASH,
        evidence=[_evidence()],
        entity_id=entity_id,
    )


def _artifact_node(node_id: str, entity_id: str | None = None) -> AuthorityNode:
    return AuthorityNode(
        node_id=node_id,
        lane=AuthorityLane.ARTIFACT,
        authority_class=AuthorityClass.ASSISTANT_PLAN,
        source_family="plan-file",
        source_instance="repo-1",
        native_id=f"native:{node_id}",
        observed_at="2026-07-16T12:01:00Z",
        body_hash=BODY_HASH,
        evidence=[_evidence("src_plan_1")],
        entity_id=entity_id,
    )


def _pre_cadence_inputs(
    lineage_graph: dict,
    *,
    source_ids: tuple[str, ...] = ("src_event_1", "src_implementation"),
):
    raw_units = [
        {
            "raw_unit_id": f"raw_fixture_{index}",
            "discovery_root_id": "root:fixture",
            "source_family": "fixture",
            "source_instance": source_id,
            "format_adapter": "fixture.v1",
            "native_identifiers": {"event_id": f"native:{index}"},
            "acquisition_status": "acquired",
            "content_hash": BODY_HASH,
            "custody_pointer": f"private-cas://{source_id}",
            "evidence_references": [f"source:{source_id}"],
        }
        for index, source_id in enumerate(source_ids, start=1)
    ]
    census_body = {
        "contract_name": "source-census.v1",
        "contract_version": 1,
        "census_id": "census:fixture",
        "snapshot_id": SNAPSHOT,
        "snapshot_at": RECONCILED,
        "snapshot_digest": SNAPSHOT_DIGEST,
        "manifest_reference": "manifest:fixture",
        "manifest_digest": "sha256:" + "d" * 64,
        "discovery_roots": [
            {
                "root_id": "root:fixture",
                "root_kind": "custody_manifest",
                "runtime_reference": "runtime:fixture",
                "config_reference": "config:fixture",
            },
        ],
        "seed_expectations": [],
        "raw_units": raw_units,
        "digest_algorithm": "sha256-rfc8785-excluding-self-digest-v1",
    }
    census = {
        **census_body,
        "census_digest": content_digest(census_body),
    }
    envelopes = [
        {
            "contract_name": "source-envelope.v1",
            "contract_version": 1,
            "source_id": source_id,
            "source_family": "fixture",
            "source_instance": source_id,
            "format_adapter": "fixture.v1",
            "raw_unit_id": raw_unit["raw_unit_id"],
            "raw_unit_content_hash": BODY_HASH,
            "custody_snapshot": {
                "snapshot_id": SNAPSHOT,
                "snapshot_hash": SNAPSHOT_DIGEST,
                "immutable": True,
            },
            "native_identifiers": {"event_id": f"native:{index}"},
            "role": "operator" if index == 1 else "assistant",
            "authority_class": "operator_intent" if index == 1 else "artifact",
            "body_hash": BODY_HASH,
        }
        for index, (source_id, raw_unit) in enumerate(
            zip(source_ids, raw_units, strict=True),
            start=1,
        )
    ]
    events = [
        {
            "contract_name": "normalized-event.v1",
            "contract_version": 1,
            "event_id": "evt_" + str(index) * 64,
            "snapshot_id": SNAPSHOT,
            "snapshot_digest": SNAPSHOT_DIGEST,
            "raw_unit_id": raw_unit["raw_unit_id"],
            "raw_unit_content_hash": BODY_HASH,
            "identity_basis": {"content_hash": BODY_HASH},
            "source_envelope_reference": f"source:{source_id}",
        }
        for index, (source_id, raw_unit) in enumerate(
            zip(source_ids, raw_units, strict=True),
            start=1,
        )
    ]
    parity_body = {
        "contract_name": "normalization-parity-receipt.v1",
        "contract_version": 1,
        "receipt_id": "parity-fixture",
        "snapshot_id": SNAPSHOT,
        "snapshot_digest": SNAPSHOT_DIGEST,
        "generated_at": GENERATED,
        "input_census": {
            "census_id": census["census_id"],
            "census_reference": "census:fixture",
            "census_digest": census["census_digest"],
            "raw_unit_ids": [item["raw_unit_id"] for item in raw_units],
            "raw_units": [
                {
                    "raw_unit_id": item["raw_unit_id"],
                    "content_hash": item["content_hash"],
                }
                for item in raw_units
            ],
        },
        "output_events": {
            "event_set_reference": "events:fixture",
            "event_set_digest": content_digest(events),
            "event_ids": [item["event_id"] for item in events],
        },
        "promotions": [
            {
                "raw_unit_id": raw_unit["raw_unit_id"],
                "raw_unit_content_hash": BODY_HASH,
                "event_ids": [event["event_id"]],
            }
            for raw_unit, event in zip(raw_units, events, strict=True)
        ],
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
    parity = {
        **parity_body,
        "receipt_digest": content_digest(parity_body),
    }
    coverage_body = {
        "contract_name": "coverage-receipt.v1",
        "contract_version": 1,
        "receipt_id": "coverage-fixture",
        "snapshot_id": SNAPSHOT,
        "generated_at": GENERATED,
        "denominator": {
            "discovery_manifest_reference": "manifest:fixture",
            "count": len(envelopes),
            "manifest_hash": "sha256:" + "d" * 64,
        },
        "sources": [
            {
                "source_id": item["source_id"],
                "status": "parsed",
                "accessible": True,
                "evidence_references": [f"source:{item['source_id']}"],
            }
            for item in envelopes
        ],
        "counts": {
            "acquired": 0,
            "parsed": len(envelopes),
            "quarantined": 0,
            "inaccessible": 0,
            "missing_expected": 0,
            "owner_blocked": 0,
        },
        "exact_all": True,
        "ready": True,
        "unresolved_blockers": [],
        "quarantines": [],
        "missing_requirements": [],
        "citation_debt": [],
        "incomplete_predicates": [],
        "closure_status": "ready",
        "residual_owners": [],
    }
    coverage = {
        **coverage_body,
        "receipt_hash": content_digest(coverage_body),
    }
    assertions = [
        {
            "contract_name": "assertion-evidence.v1",
            "contract_version": 1,
            "assertion_id": "assertion:fixture",
            "assertion_class": "current_state",
            "statement": "The frozen fixture sources were normalized.",
            "verification_state": "verified",
            "freshness": {
                "verified_at": RECONCILED,
                "max_age_seconds": 3600,
                "status": "fresh",
            },
            "evidence_references": [
                {
                    "evidence_id": "evidence:fixture",
                    "independence_group": "fixture",
                    "evidence_type": "primary_source",
                    "reference": f"source:{source_ids[0]}",
                    "body_hash": BODY_HASH,
                },
            ],
        },
    ]
    testament = {
        "contract_name": "governance-testament.v1",
        "contract_version": 1,
        "testament_id": "testament:fixture",
        "status": "candidate",
    }
    return build_reconcile_inputs(
        snapshot_id=SNAPSHOT,
        snapshot_digest=SNAPSHOT_DIGEST,
        snapshot_at=RECONCILED,
        lineage_graph=lineage_graph,
        governance_testament=testament,
        source_census=census,
        source_envelopes=envelopes,
        normalized_events=events,
        assertion_evidence=assertions,
        normalization_parity_receipt=parity,
        coverage_receipt=coverage,
    )


def test_identical_text_preserves_authority_separation(store: RegistryStore) -> None:
    intent = _intent_node("intent:prime-directive")
    plan = _artifact_node("artifact:generated-plan")

    store.add_authority_node(intent)
    store.add_authority_node(plan)

    assert intent.body_hash == plan.body_hash
    assert [node.node_id for node in store.authority_graph.nodes(AuthorityLane.OPERATOR_INTENT)] == [
        intent.node_id,
    ]
    assert [node.node_id for node in store.authority_graph.nodes(AuthorityLane.ARTIFACT)] == [
        plan.node_id,
    ]

    with pytest.raises(ValueError, match="incompatible with lane"):
        AuthorityNode(
            node_id="invalid:promotion",
            lane=AuthorityLane.OPERATOR_INTENT,
            authority_class=AuthorityClass.ASSISTANT_PLAN,
            source_family="plan-file",
            source_instance="repo-1",
            native_id="plan:bad",
            observed_at=OBSERVED,
            body_hash=BODY_HASH,
        )

    reloaded = RegistryStore(store.store_dir)
    reloaded.load()
    assert reloaded.authority_graph.get_node(intent.node_id).lane == AuthorityLane.OPERATOR_INTENT
    assert reloaded.authority_graph.get_node(plan.node_id).lane == AuthorityLane.ARTIFACT


def test_full_reviewed_edge_vocabulary_round_trips(store: RegistryStore) -> None:
    intent = _intent_node("intent:source")
    artifact = _artifact_node("artifact:target")
    store.add_authority_node(intent)
    store.add_authority_node(artifact)

    for index, edge_type in enumerate(ReviewedEdgeType):
        edge = AuthorityEdge(
            source_node_id=artifact.node_id,
            target_node_id=intent.node_id,
            edge_type=edge_type,
            recorded_at=f"2026-07-16T12:{index:02d}:00Z",
            evidence=[_evidence(f"src_edge_{index}")],
            confidence=0.9,
            review_state=ReviewState.REVIEWED,
            reviewer="reviewer:operator",
        )
        store.add_authority_edge(edge)

    # Idempotent replay does not duplicate a persisted edge.
    first = store.authority_graph.edges()[0]
    before = store.authority_edges_path.read_text()
    store.add_authority_edge(first)
    assert store.authority_edges_path.read_text() == before

    reloaded = RegistryStore(store.store_dir)
    reloaded.load()
    assert {edge.edge_type for edge in reloaded.authority_graph.edges()} == set(ReviewedEdgeType)
    assert all(edge.review_state == ReviewState.REVIEWED for edge in reloaded.authority_graph.edges())
    assert all(edge.evidence for edge in reloaded.authority_graph.edges())


def test_self_image_is_deterministic_and_traceable(store: RegistryStore) -> None:
    organ = store.create_entity(
        EntityType.ORGAN,
        "Meta",
        created_by="registry",
        metadata={"owner": "owner:meta"},
        timestamp_ms=1,
    )
    repo = store.create_entity(
        EntityType.REPO,
        "organvm-ontologia",
        created_by="registry",
        metadata={"owner": "owner:ontologia"},
        timestamp_ms=2,
    )
    relation_metadata = {"evidence_references": ["source:src_event_1"]}
    store.add_hierarchy_edge(organ.uid, repo.uid, metadata=relation_metadata)
    store.add_relation_edge(
        repo.uid,
        organ.uid,
        "produces_for",
        metadata=relation_metadata,
    )
    store.record_observation(
        "metric:coverage",
        repo.uid,
        0.75,
        source="test",
        metadata={"evidence_references": ["source:src_event_1"]},
    )

    directive = _intent_node("intent:ideal", entity_id=repo.uid)
    directive.metadata.update(
        {
            "ideal_form_id": "ideal:perpetual-memory",
            "predicate_receipts": [
                {
                    "predicate_id": "predicate:unit",
                    "receipt_reference": "receipt:test:unit",
                    "result": "pass",
                },
                {
                    "predicate_id": "predicate:integration",
                    "receipt_reference": "receipt:test:integration",
                    "result": "fail",
                },
            ],
        },
    )
    plan = _artifact_node("artifact:implementation", entity_id=repo.uid)
    store.add_authority_node(directive)
    store.add_authority_node(plan)
    store.add_authority_edge(
        AuthorityEdge(
            source_node_id=plan.node_id,
            target_node_id=directive.node_id,
            edge_type=ReviewedEdgeType.IMPLEMENTS,
            recorded_at="2026-07-16T12:02:00Z",
            evidence=[_evidence("src_implementation")],
            confidence=1.0,
            review_state=ReviewState.REVIEWED,
            reviewer="reviewer:operator",
        ),
    )
    store.update_lifecycle(repo.uid, LifecycleStatus.DEPRECATED, source="test")
    allowed_evidence = frozenset(
        {
            "src_event_1",
            "source:src_event_1",
            "src_plan_1",
            "src_implementation",
            "receipt:test:unit",
            "receipt:test:integration",
        },
    )

    image1 = store.node_self_image(
        repo.uid,
        constitutional_digest="sha256:" + "b" * 64,
        last_reconciled_at=RECONCILED,
        allowed_evidence_references=allowed_evidence,
    ).to_dict()
    image2 = store.node_self_image(
        repo.uid,
        constitutional_digest="sha256:" + "b" * 64,
        last_reconciled_at=RECONCILED,
        allowed_evidence_references=allowed_evidence,
    ).to_dict()

    assert json.dumps(image1, sort_keys=True) == json.dumps(image2, sort_keys=True)
    assert image1["contract_name"] == "node-self-image.v1"
    assert image1["contract_version"] == 1
    assert image1["node_id"] == repo.uid
    assert image1["node_type"] == "repository"
    assert image1["owner_reference"] == "owner:ontologia"
    assert image1["cursors"]["memory"] == "memory:artifact:implementation"
    assert image1["cursors"]["event"].startswith("event:sha256:")
    assert set(image1["digests"]) == {"constitutional", "topology"}
    assert image1["relations"]["incoming"]
    assert image1["relations"]["outgoing"]
    assert image1["active_ideal_forms"][0]["form_id"] == "ideal:perpetual-memory"
    assert image1["active_ideal_forms"][0]["implementation_state"] == "partial"
    assert image1["active_ideal_forms"][0]["distance_to_ideal"] == 0.5
    assert image1["evidence_references"]
    assert "schema_id" not in image1
    assert "identity" not in image1
    assert all(
        set(relation) == {"relation_type", "target_node_id", "evidence_references"}
        for direction in image1["relations"].values()
        for relation in direction
    )

    trace = store.trace_state_value(repo.uid, "lifecycle_status")
    assert trace["value"] == "deprecated"
    assert any(event.get("changed_property") == "lifecycle_status" for event in trace["events"])
    assert trace["evidence_refs"]
    assert trace["trace_digest"].startswith("sha256:")


def test_malformed_records_emit_hashed_idempotent_quarantine(store: RegistryStore) -> None:
    secret_marker = "PRIVATE-BODY-MUST-NOT-LEAK"
    store.names_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.edges_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.lineage_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.observations_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.authority_nodes_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.authority_edges_path.write_text(f"{{not-json {secret_marker}}}\n")
    store.events_path.write_text(f"{{not-json {secret_marker}}}\n")

    store.load()
    store.events()  # event JSONL is quarantined when it is consumed
    diagnostics = store.quarantine_diagnostics
    assert len(diagnostics) == 7
    assert all(item.record_hash.startswith("sha256:") for item in diagnostics)
    assert all(item.diagnostic_id.startswith("sha256:") for item in diagnostics)
    assert secret_marker not in store.quarantine_path.read_text()

    before = store.quarantine_path.read_text()
    store.load()
    store.events()
    assert store.quarantine_path.read_text() == before


def test_legacy_lineage_types_remain_compatible(store: RegistryStore) -> None:
    for index, lineage_type in enumerate(LineageType):
        store.add_lineage(
            entity_id=f"entity:{index}",
            related_id=f"related:{index}",
            lineage_type=lineage_type,
        )

    reloaded = RegistryStore(store.store_dir)
    reloaded.load()
    assert {record.lineage_type for record in reloaded.lineage_index.all_records()} == set(
        LineageType,
    )


def test_public_reconciliation_exports_exact_one_fixed_point(
    store: RegistryStore,
    tmp_path,
) -> None:
    repo = store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=11,
    )
    node = _intent_node("intent:reconcile", entity_id=repo.uid)
    node.metadata.update(
        {
            "node_type": "source_event",
            "source_envelope_id": "src_event_1",
            "summary": "Reviewed operator directive.",
            "review_state": "reviewed",
            "zoom_level": "repository",
            "ideal_form_id": "ideal:reconciled-repository",
            "predicate_receipts": [
                {
                    "predicate_id": "predicate:reconciled-repository",
                    "receipt_reference": "receipt:parity-fixture",
                    "result": "pass",
                },
            ],
        },
    )
    store.add_authority_node(node)
    implementation = _artifact_node("artifact:reconcile", entity_id=repo.uid)
    implementation.metadata.update(
        {
            "node_type": "implementation",
            "source_envelope_id": "src_implementation",
            "summary": "Reviewed implementation artifact.",
            "review_state": "reviewed",
            "zoom_level": "repository",
        },
    )
    store.add_authority_node(implementation)
    store.add_authority_edge(
        AuthorityEdge(
            source_node_id=implementation.node_id,
            target_node_id=node.node_id,
            edge_type=ReviewedEdgeType.IMPLEMENTS,
            recorded_at="2026-07-16T12:02:00Z",
            evidence=[_evidence("src_implementation")],
            confidence=1.0,
            review_state=ReviewState.REVIEWED,
            reviewer="reviewer:operator",
        ),
    )
    store.record_observation(
        "metric:governance-coverage",
        repo.uid,
        1.0,
        source="test",
        metadata={"evidence_references": ["source:src_event_1"]},
    )
    store.save()
    graph = export_lineage_graph(
        store,
        snapshot_id=SNAPSHOT,
        generated_at=RECONCILED,
    )

    imported_store = RegistryStore(tmp_path / "imported")
    imported_store.load()
    imported_repo = imported_store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=11,
    )
    graph["nodes"][0]["metadata"]["entity_id"] = imported_repo.uid
    imported_store.record_observation(
        "metric:governance-coverage",
        imported_repo.uid,
        1.0,
        source="test",
        metadata={"evidence_references": ["source:src_event_1"]},
    )
    imported_store.save()
    imported = import_lineage_graph(imported_store, graph, snapshot_id=SNAPSHOT)
    assert imported.ready
    assert imported.imported_node_ids == ("artifact:reconcile", "intent:reconcile")

    inputs = _pre_cadence_inputs(graph)
    evidence_index = build_snapshot_evidence_index(inputs)
    output_dir = tmp_path / "output"
    first = reconcile_governance_snapshot(imported_store, inputs, output_dir=output_dir)
    before = {
        path.name: path.read_bytes()
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    }
    receipt_log_before = (
        imported_store.store_dir / "governance-reconciliations.jsonl"
    ).read_bytes()
    second = reconcile_governance_snapshot(imported_store, inputs, output_dir=output_dir)

    assert first == second
    assert first["receipt"]["ready"] is True
    image_set = first["node_self_image_set"]
    assert image_set["contract_name"] == "node-self-image-set.v1"
    assert image_set["registry_reference"] == "#/registry_projection"
    assert image_set["registry_projection"] == [
        {
            "uid": imported_repo.uid,
            "entity_type": "repo",
            "lifecycle_status": "active",
        },
    ]
    assert image_set["registry_digest"] == content_digest(
        image_set["registry_projection"],
    )
    assert image_set["registered_node_ids"] == [imported_repo.uid]
    assert [image["node_id"] for image in image_set["self_images"]] == [imported_repo.uid]
    assert image_set["readiness"]["exact_all"] is True
    assert (
        validate_self_image_set(
            image_set,
            evidence_index=evidence_index,
        )
        == image_set
    )
    assert first["receipt"]["evidence_index_digest"] == evidence_index.digest
    assert {
        path.name: path.read_bytes()
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    } == before
    assert (
        imported_store.store_dir / "governance-reconciliations.jsonl"
    ).read_bytes() == receipt_log_before

    direct_inputs = tmp_path / "direct-inputs"
    direct_inputs.mkdir()
    artifact_values = {
        "lineage": inputs.lineage_graph,
        "governance-testament": inputs.governance_testament,
        "source-census": inputs.source_census,
        "source-envelopes": list(inputs.source_envelopes),
        "normalized-events": list(inputs.normalized_events),
        "assertion-evidence": list(inputs.assertion_evidence),
        "normalization-parity": inputs.normalization_parity_receipt,
        "coverage": inputs.coverage_receipt,
    }
    artifact_paths = {}
    for artifact_name, artifact_value in artifact_values.items():
        artifact_path = direct_inputs / f"{artifact_name}.json"
        artifact_path.write_text(
            json.dumps(artifact_value),
            encoding="utf-8",
        )
        artifact_paths[artifact_name] = artifact_path
    cli_output = tmp_path / "cli-output"
    assert (
        cli_main(
            [
                "governance",
                "reconcile",
                "--lineage",
                str(artifact_paths["lineage"]),
                "--snapshot-id",
                inputs.snapshot_id,
                "--snapshot-digest",
                inputs.snapshot_digest,
                "--snapshot-at",
                inputs.snapshot_at,
                "--governance-testament",
                str(artifact_paths["governance-testament"]),
                "--source-census",
                str(artifact_paths["source-census"]),
                "--source-envelopes",
                str(artifact_paths["source-envelopes"]),
                "--normalized-events",
                str(artifact_paths["normalized-events"]),
                "--assertion-evidence",
                str(artifact_paths["assertion-evidence"]),
                "--normalization-parity",
                str(artifact_paths["normalization-parity"]),
                "--coverage",
                str(artifact_paths["coverage"]),
                "--state-root",
                str(imported_store.store_dir),
                "--out",
                str(cli_output),
            ],
        )
        == 0
    )
    assert json.loads(
        (cli_output / "reconciliation-receipt.json").read_text(encoding="utf-8"),
    ) == first["receipt"]


def test_self_image_set_rejects_self_declared_or_tampered_denominators(
    tmp_path: Path,
) -> None:
    store = RegistryStore(tmp_path / "registry")
    store.load()
    repo = store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={
            "owner": "owner:governed-repository",
            "custody_path": "/private/must-not-export",
        },
        timestamp_ms=31,
    )
    store.record_observation(
        "metric:governance-coverage",
        repo.uid,
        1.0,
        source="test",
        metadata={"evidence_references": ["source:src_event_1"]},
    )
    directive = _intent_node("intent:registry-denominator", entity_id=repo.uid)
    directive.metadata.update(
        {
            "ideal_form_id": "ideal:registry-denominator",
            "predicate_receipts": [
                {
                    "predicate_id": "predicate:registry-denominator",
                    "receipt_reference": "receipt:parity-fixture",
                    "result": "pass",
                },
            ],
        },
    )
    store.add_authority_node(directive)
    graph = export_lineage_graph(
        store,
        snapshot_id=SNAPSHOT,
        generated_at=GENERATED,
    )
    evidence_index = build_snapshot_evidence_index(
        _pre_cadence_inputs(
            graph,
            source_ids=("src_event_1",),
        ),
    )
    image_set = build_self_image_set(
        store,
        evidence_index=evidence_index,
        reconciled_at=GENERATED,
        constitutional_digest="sha256:" + "b" * 64,
    )

    projection = image_set["registry_projection"]
    assert projection == [
        {
            "uid": repo.uid,
            "entity_type": "repo",
            "lifecycle_status": "active",
        },
    ]
    assert "private" not in json.dumps(projection)

    tampered_projection = deepcopy(image_set)
    tampered_projection["registry_projection"][0]["lifecycle_status"] = "archived"
    with pytest.raises(ValueError, match="registry_digest"):
        validate_self_image_set(
            tampered_projection,
            evidence_index=evidence_index,
        )

    self_declared = deepcopy(image_set)
    replacement_id = repo.uid[:-1] + ("A" if repo.uid[-1] != "A" else "B")
    self_declared["registered_node_ids"] = [replacement_id]
    self_declared["self_images"][0]["node_id"] = replacement_id
    with pytest.raises(ValueError, match="derive exactly"):
        validate_self_image_set(
            self_declared,
            evidence_index=evidence_index,
        )

    for location, mutate in (
        (
            "self-image",
            lambda value: value["self_images"][0].update(
                {"evidence_references": ["entity:synthetic"]},
            ),
        ),
        (
            "observation",
            lambda value: value["self_images"][0]["observations"][0].update(
                {"evidence_references": ["observation:synthetic"]},
            ),
        ),
        (
            "active ideal",
            lambda value: value["self_images"][0]["active_ideal_forms"][0].update(
                {"evidence_references": ["receipt:unbound"]},
            ),
        ),
    ):
        unresolved = deepcopy(image_set)
        mutate(unresolved)
        with pytest.raises(ValueError, match=location):
            validate_self_image_set(
                unresolved,
                evidence_index=evidence_index,
            )

    unresolved_relation = deepcopy(image_set)
    unresolved_relation["self_images"][0]["relations"]["outgoing"] = [
        {
            "relation_type": "contains",
            "target_node_id": repo.uid,
            "evidence_references": ["registry-edge:synthetic"],
        },
    ]
    with pytest.raises(ValueError, match="relation"):
        validate_self_image_set(
            unresolved_relation,
            evidence_index=evidence_index,
        )

    future = deepcopy(image_set)
    future["self_images"][0]["reconciled_at"] = "2099-07-16T12:05:01Z"
    with pytest.raises(ValueError, match="outside the snapshot window"):
        validate_self_image_set(
            future,
            evidence_index=evidence_index,
        )


def test_self_image_set_rejects_empty_registry(store: RegistryStore) -> None:
    graph = {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": "lineage:empty-registry",
        "generated_at": GENERATED,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            {
                "node_id": "intent:empty-registry",
                "lane": "operator_intent",
                "node_type": "source_event",
                "source_envelope_id": "src_event_1",
                "occurred_at": OBSERVED,
                "authority_class": "operator_intent",
                "summary": "Empty registry evidence fixture.",
                "content_hash": BODY_HASH,
                "review_state": "reviewed",
            },
        ],
        "edges": [],
    }
    evidence_index = build_snapshot_evidence_index(
        _pre_cadence_inputs(graph, source_ids=("src_event_1",)),
    )
    with pytest.raises(ValueError, match="at least one registered node"):
        build_self_image_set(
            store,
            evidence_index=evidence_index,
            reconciled_at=GENERATED,
            constitutional_digest="sha256:" + "b" * 64,
        )


def test_reconcile_rejects_synthetic_observation_fallback_without_outputs(
    tmp_path: Path,
) -> None:
    source_store = RegistryStore(tmp_path / "source")
    source_store.load()
    repo = source_store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=41,
    )
    directive = _intent_node("intent:no-observation-fallback", entity_id=repo.uid)
    directive.metadata.update(
        {
            "ideal_form_id": "ideal:no-observation-fallback",
            "predicate_receipts": [
                {
                    "predicate_id": "predicate:no-observation-fallback",
                    "receipt_reference": "receipt:parity-fixture",
                    "result": "pass",
                },
            ],
        },
    )
    source_store.add_authority_node(directive)
    graph = export_lineage_graph(
        source_store,
        snapshot_id=SNAPSHOT,
        generated_at=GENERATED,
    )

    store = RegistryStore(tmp_path / "registry")
    store.load()
    reconciled_repo = store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=41,
    )
    graph["nodes"][0]["metadata"]["entity_id"] = reconciled_repo.uid
    store.record_observation(
        "metric:governance-coverage",
        reconciled_repo.uid,
        1.0,
        source="test",
    )
    inputs = _pre_cadence_inputs(graph, source_ids=("src_event_1",))
    output_dir = tmp_path / "output"

    with pytest.raises(ValueError, match="observation.*snapshot-bound"):
        reconcile_governance_snapshot(store, inputs, output_dir=output_dir)
    assert not output_dir.exists()


def test_pre_cadence_interface_rejects_cross_snapshot_custody() -> None:
    graph = {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": "lineage:cross-snapshot",
        "generated_at": GENERATED,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            {
                "node_id": "intent:cross-snapshot",
                "lane": "operator_intent",
                "node_type": "source_event",
                "source_envelope_id": "src_event_1",
                "occurred_at": OBSERVED,
                "authority_class": "operator_intent",
                "summary": "Cross-snapshot evidence fixture.",
                "content_hash": BODY_HASH,
                "review_state": "reviewed",
            },
        ],
        "edges": [],
    }
    inputs = _pre_cadence_inputs(graph, source_ids=("src_event_1",))
    envelopes = [deepcopy(item) for item in inputs.source_envelopes]
    envelopes[0]["custody_snapshot"]["snapshot_hash"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="custody binding mismatch"):
        build_reconcile_inputs(
            snapshot_id=inputs.snapshot_id,
            snapshot_digest=inputs.snapshot_digest,
            snapshot_at=inputs.snapshot_at,
            lineage_graph=inputs.lineage_graph,
            governance_testament=inputs.governance_testament,
            source_census=inputs.source_census,
            source_envelopes=envelopes,
            normalized_events=list(inputs.normalized_events),
            assertion_evidence=list(inputs.assertion_evidence),
            normalization_parity_receipt=inputs.normalization_parity_receipt,
            coverage_receipt=inputs.coverage_receipt,
        )


def test_pre_cadence_interface_rejects_false_ready_owner_receipt() -> None:
    graph = {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": "lineage:false-ready",
        "generated_at": GENERATED,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            {
                "node_id": "intent:false-ready",
                "lane": "operator_intent",
                "node_type": "source_event",
                "source_envelope_id": "src_event_1",
                "occurred_at": OBSERVED,
                "authority_class": "operator_intent",
                "summary": "False-ready coverage receipt fixture.",
                "content_hash": BODY_HASH,
                "review_state": "reviewed",
            },
        ],
        "edges": [],
    }
    inputs = _pre_cadence_inputs(graph, source_ids=("src_event_1",))
    coverage = deepcopy(inputs.coverage_receipt)
    coverage["ready"] = False
    coverage["closure_status"] = "closed_with_owner_routed_debt"
    coverage["unresolved_blockers"] = ["blocker:fixture"]
    coverage_body = dict(coverage)
    coverage_body.pop("receipt_hash")
    coverage["receipt_hash"] = content_digest(coverage_body)

    with pytest.raises(ValueError, match="coverage receipt is not ready"):
        build_reconcile_inputs(
            snapshot_id=inputs.snapshot_id,
            snapshot_digest=inputs.snapshot_digest,
            snapshot_at=inputs.snapshot_at,
            lineage_graph=inputs.lineage_graph,
            governance_testament=inputs.governance_testament,
            source_census=inputs.source_census,
            source_envelopes=list(inputs.source_envelopes),
            normalized_events=list(inputs.normalized_events),
            assertion_evidence=list(inputs.assertion_evidence),
            normalization_parity_receipt=inputs.normalization_parity_receipt,
            coverage_receipt=coverage,
        )


@pytest.mark.parametrize(
    ("reference", "body_hash", "freshness_status", "message"),
    [
        ("source:src_event_1", "sha256:" + "0" * 64, "fresh", "unresolved"),
        ("fabricated:src_event_1", BODY_HASH, "fresh", "unresolved"),
        ("source:src_event_1", BODY_HASH, "stale", "stale"),
    ],
)
def test_snapshot_evidence_index_rejects_unbound_assertion_evidence(
    reference: str,
    body_hash: str,
    freshness_status: str,
    message: str,
) -> None:
    graph = {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": "lineage:assertion-body-hash",
        "generated_at": GENERATED,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            {
                "node_id": "intent:assertion-body-hash",
                "lane": "operator_intent",
                "node_type": "source_event",
                "source_envelope_id": "src_event_1",
                "occurred_at": OBSERVED,
                "authority_class": "operator_intent",
                "summary": "Assertion hash binding fixture.",
                "content_hash": BODY_HASH,
                "review_state": "reviewed",
            },
        ],
        "edges": [],
    }
    inputs = _pre_cadence_inputs(graph, source_ids=("src_event_1",))
    assertions = [deepcopy(item) for item in inputs.assertion_evidence]
    assertions[0]["evidence_references"][0]["reference"] = reference
    assertions[0]["evidence_references"][0]["body_hash"] = body_hash
    assertions[0]["freshness"]["status"] = freshness_status
    tampered = build_reconcile_inputs(
        snapshot_id=inputs.snapshot_id,
        snapshot_digest=inputs.snapshot_digest,
        snapshot_at=inputs.snapshot_at,
        lineage_graph=inputs.lineage_graph,
        governance_testament=inputs.governance_testament,
        source_census=inputs.source_census,
        source_envelopes=list(inputs.source_envelopes),
        normalized_events=list(inputs.normalized_events),
        assertion_evidence=assertions,
        normalization_parity_receipt=inputs.normalization_parity_receipt,
        coverage_receipt=inputs.coverage_receipt,
    )

    with pytest.raises(ValueError, match=message):
        build_snapshot_evidence_index(tampered)


def test_final_bundle_path_does_not_accept_pre_cadence_partial_input(
    store: RegistryStore,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="final governance snapshot bundle is incomplete"):
        reconcile_snapshot_bundle(
            store,
            {
                "contract_name": "governance-snapshot-bundle.v1",
                "contract_version": 1,
                "snapshot_id": SNAPSHOT,
                "snapshot_digest": SNAPSHOT_DIGEST,
                "snapshot_at": RECONCILED,
            },
            output_dir=tmp_path / "output",
        )


def test_snapshot_references_materialize_only_at_exact_digest(tmp_path: Path) -> None:
    store = RegistryStore(tmp_path / "source")
    store.load()
    repo = store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=21,
    )
    node = _intent_node("intent:materialize", entity_id=repo.uid)
    node.metadata.update(
        {
            "node_type": "source_event",
            "source_envelope_id": "src_event_1",
            "summary": "Reviewed operator directive.",
            "review_state": "reviewed",
            "zoom_level": "repository",
            "private_body": "MUST-NOT-EXPORT",
        },
    )
    store.add_authority_node(node)
    graph = export_lineage_graph(
        store,
        snapshot_id=SNAPSHOT,
        generated_at=RECONCILED,
    )
    assert "private_body" not in json.dumps(graph)
    testament = {
        "contract_name": "governance-testament.v1",
        "contract_version": 1,
        "testament_id": "fixture",
        "status": "candidate",
    }
    graph_path = tmp_path / "lineage.json"
    testament_path = tmp_path / "testament.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    testament_path.write_text(json.dumps(testament), encoding="utf-8")
    bundle = {
        "contract_name": "governance-snapshot-bundle.v1",
        "contract_version": 1,
        "snapshot_id": SNAPSHOT,
        "snapshot_at": RECONCILED,
        "snapshot_digest": "sha256:" + "c" * 64,
        "lineage_graph": {
            "contract_name": "lineage-graph.v1",
            "artifact_id": graph["graph_id"],
            "reference": graph_path.name,
            "snapshot_id": SNAPSHOT,
            "digest": content_digest(graph),
        },
        "governance_testament": {
            "contract_name": "governance-testament.v1",
            "artifact_id": "fixture",
            "reference": testament_path.name,
            "snapshot_id": SNAPSHOT,
            "digest": content_digest(testament),
        },
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    materialized = load_materialized_snapshot_bundle(bundle_path)
    assert materialized["lineage_graph"] == graph
    assert materialized["governance_testament"] == testament

    bundle["lineage_graph"]["digest"] = "sha256:" + "0" * 64
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="lineage_graph artifact digest mismatch"):
        load_materialized_snapshot_bundle(bundle_path)


def test_unknown_artifact_authority_round_trips_without_promotion(
    store: RegistryStore,
) -> None:
    graph = {
        "contract_name": "lineage-graph.v1",
        "contract_version": 1,
        "graph_id": "lineage:unknown-artifact",
        "generated_at": RECONCILED,
        "frozen_snapshot_id": SNAPSHOT,
        "nodes": [
            {
                "node_id": "artifact:unknown",
                "lane": "artifact",
                "node_type": "source_event",
                "source_envelope_id": "src_unknown_artifact",
                "occurred_at": OBSERVED,
                "authority_class": "unknown",
                "summary": "Unclassified artifact source.",
                "content_hash": BODY_HASH,
                "review_state": "reviewed",
                "metadata": {"zoom_level": "atom"},
            },
        ],
        "edges": [],
    }
    result = import_lineage_graph(store, graph, snapshot_id=SNAPSHOT)
    assert result.ready
    exported = export_lineage_graph(
        store,
        snapshot_id=SNAPSHOT,
        generated_at=RECONCILED,
    )
    assert exported["nodes"][0]["lane"] == "artifact"
    assert exported["nodes"][0]["authority_class"] == "unknown"
