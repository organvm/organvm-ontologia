"""Focused tests for authority memory, self-images, and quarantine receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    build_self_image_set,
    export_lineage_graph,
    import_lineage_graph,
    load_materialized_snapshot_bundle,
    reconcile_snapshot_bundle,
)
from ontologia.registry.store import RegistryStore

SNAPSHOT = "snapshot:2026-07-16-governance"
OBSERVED = "2026-07-16T12:00:00Z"
RECONCILED = "2099-07-16T12:00:00Z"
BODY_HASH = "sha256:" + "a" * 64


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
    store.add_hierarchy_edge(organ.uid, repo.uid)
    store.add_relation_edge(repo.uid, organ.uid, "produces_for")
    store.record_observation("metric:coverage", repo.uid, 0.75, source="test")

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

    image1 = store.node_self_image(
        repo.uid,
        constitutional_digest="sha256:" + "b" * 64,
        last_reconciled_at=RECONCILED,
    ).to_dict()
    image2 = store.node_self_image(
        repo.uid,
        constitutional_digest="sha256:" + "b" * 64,
        last_reconciled_at=RECONCILED,
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
                    "receipt_reference": "receipt:reconciled-repository",
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
    store.record_observation("metric:governance-coverage", repo.uid, 1.0, source="test")
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
    )
    imported_store.save()
    imported = import_lineage_graph(imported_store, graph, snapshot_id=SNAPSHOT)
    assert imported.ready
    assert imported.imported_node_ids == ("artifact:reconcile", "intent:reconcile")

    snapshot_bundle = {
        "contract_name": "governance-snapshot-bundle.v1",
        "contract_version": 1,
        "snapshot_id": SNAPSHOT,
        "snapshot_digest": "sha256:" + "c" * 64,
        "snapshot_at": RECONCILED,
        "constitutional_digest": "sha256:" + "b" * 64,
        "lineage_graph": graph,
    }
    output_dir = tmp_path / "output"
    first = reconcile_snapshot_bundle(imported_store, snapshot_bundle, output_dir=output_dir)
    before = {
        path.name: path.read_bytes()
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    }
    receipt_log_before = (
        imported_store.store_dir / "governance-reconciliations.jsonl"
    ).read_bytes()
    second = reconcile_snapshot_bundle(imported_store, snapshot_bundle, output_dir=output_dir)

    assert first == second
    assert first["receipt"]["ready"] is True
    image_set = first["node_self_image_set"]
    assert image_set["contract_name"] == "node-self-image-set.v1"
    assert image_set["registered_node_ids"] == [imported_repo.uid]
    assert [image["node_id"] for image in image_set["self_images"]] == [imported_repo.uid]
    assert image_set["readiness"]["exact_all"] is True
    assert {
        path.name: path.read_bytes()
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    } == before
    assert (
        imported_store.store_dir / "governance-reconciliations.jsonl"
    ).read_bytes() == receipt_log_before


def test_self_image_set_rejects_empty_registry(store: RegistryStore) -> None:
    with pytest.raises(ValueError, match="at least one registered node"):
        build_self_image_set(
            store,
            snapshot_id=SNAPSHOT,
            snapshot_digest="sha256:" + "c" * 64,
            reconciled_at=RECONCILED,
            constitutional_digest="sha256:" + "b" * 64,
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
