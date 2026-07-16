"""Focused tests for authority memory, self-images, and quarantine receipts."""

from __future__ import annotations

import json

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
)
from ontologia.registry.store import RegistryStore

SNAPSHOT = "snapshot:2026-07-16-governance"
OBSERVED = "2026-07-16T12:00:00Z"
RECONCILED = "2099-07-16T12:00:00Z"
BODY_HASH = "sha256:" + "a" * 64


def _evidence(source_id: str = "source:event-1") -> EvidenceSpan:
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
        evidence=[_evidence("source:plan-1")],
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
            evidence=[_evidence(f"source:edge-{index}")],
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
            "implementation_state": "partial",
            "distance_to_ideal": 0.25,
            "predicate": "pytest -q",
            "receipt": "receipt:test",
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
            evidence=[_evidence("source:implementation")],
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
    assert image1["schema_id"] == "node-self-image.v1"
    assert image1["owner"] == "owner:ontologia"
    assert image1["memory_cursor"]["node_count"] == 2
    assert image1["event_cursor"]["event_count"] >= 1
    assert image1["relations"]["incoming"]
    assert image1["relations"]["outgoing"]
    assert image1["active_ideal_forms"][0]["ideal_form_id"] == "ideal:perpetual-memory"
    assert image1["evidence_refs"]

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
