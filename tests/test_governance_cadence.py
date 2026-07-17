"""Blocked reconciliation and Limen cadence adapter tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ontologia.entity.identity import EntityType
from ontologia.governance.cadence_owner import main as owner_main
from ontologia.governance.cadence_predicate import main as predicate_main
from ontologia.governance.memory import content_digest
from ontologia.governance.reconcile import (
    build_reconcile_inputs,
    build_snapshot_evidence_index,
    export_lineage_graph,
    reconcile_governance_snapshot,
)
from ontologia.registry.store import RegistryStore
from tests.test_governance_memory import (
    BODY_HASH,
    GENERATED,
    RECONCILED,
    SNAPSHOT,
    SNAPSHOT_DIGEST,
    _intent_node,
    _pre_cadence_inputs,
)

RAW_HASH = "sha256:" + "b" * 64
BLOCKED_SOURCE_ID = "src_blocked_fixture"
BLOCKED_RAW_UNIT_ID = "raw_blocked_fixture"


def _blocked_input_values(lineage_graph: dict) -> dict:
    ready = _pre_cadence_inputs(
        lineage_graph,
        source_ids=("src_event_1",),
    )
    census = deepcopy(ready.source_census)
    census["raw_units"][0]["content_hash"] = RAW_HASH
    census["raw_units"].append(
        {
            "raw_unit_id": BLOCKED_RAW_UNIT_ID,
            "discovery_root_id": "root:fixture",
            "source_family": "fixture-unavailable",
            "source_instance": "expected-export",
            "format_adapter": "fixture.v1",
            "native_identifiers": {},
            "acquisition_status": "owner_blocked",
            "content_hash": None,
            "custody_pointer": "custody:expected-export",
            "evidence_references": ["custody:expected-export"],
        },
    )
    census["census_digest"] = content_digest(
        {key: value for key, value in census.items() if key != "census_digest"},
    )

    envelopes = [deepcopy(item) for item in ready.source_envelopes]
    envelopes[0]["raw_unit_content_hash"] = RAW_HASH
    envelopes[0]["redacted_projection_pointer"] = "source-envelope.v1.jsonl#src_event_1"
    events = [deepcopy(item) for item in ready.normalized_events]
    events[0]["raw_unit_content_hash"] = RAW_HASH
    events[0]["identity_basis"]["content_hash"] = BODY_HASH
    events[0]["source_envelope_reference"] = "source-envelope.v1.jsonl#src_event_1"

    parity = deepcopy(ready.normalization_parity_receipt)
    parity["input_census"]["census_digest"] = census["census_digest"]
    parity["input_census"]["raw_unit_ids"].append(BLOCKED_RAW_UNIT_ID)
    parity["input_census"]["raw_units"][0]["content_hash"] = RAW_HASH
    parity["input_census"]["raw_units"].append(
        {
            "raw_unit_id": BLOCKED_RAW_UNIT_ID,
            "content_hash": None,
        },
    )
    parity["promotions"][0]["raw_unit_content_hash"] = RAW_HASH
    parity["promotions"].append(
        {
            "raw_unit_id": BLOCKED_RAW_UNIT_ID,
            "raw_unit_content_hash": None,
            "disposition": {
                "type": "blocked",
                "owner_reference": "repo:fixture/owner",
                "failed_predicate": "official export exists",
                "next_action": "acquire the official read-only export",
                "evidence_references": ["custody:expected-export"],
            },
        },
    )
    parity["readiness"].update(
        {
            "unresolved_blockers": [BLOCKED_SOURCE_ID],
            "quarantines": ["src_residual_fixture"],
            "ready": False,
            "status": "blocked",
        },
    )
    parity["receipt_digest"] = content_digest(
        {key: value for key, value in parity.items() if key != "receipt_digest"},
    )

    coverage = deepcopy(ready.coverage_receipt)
    coverage["denominator"]["count"] = 2
    coverage["sources"][0]["source_id"] = "src_fixture_1"
    coverage["sources"].append(
        {
            "source_id": BLOCKED_SOURCE_ID,
            "status": "owner_blocked",
            "accessible": False,
            "owner_reference": "repo:fixture/owner",
            "failed_predicate": "official export exists",
            "next_action": "acquire the official read-only export",
            "evidence_references": ["custody:expected-export"],
        },
    )
    coverage["counts"]["owner_blocked"] = 1
    coverage["unresolved_blockers"] = [BLOCKED_SOURCE_ID]
    coverage["ready"] = False
    coverage["closure_status"] = "closed_with_owner_routed_debt"
    coverage["residual_owners"] = [
        {
            "source_id": BLOCKED_SOURCE_ID,
            "owner_reference": "repo:fixture/owner",
            "failed_predicate": "official export exists",
            "next_action": "acquire the official read-only export",
        },
    ]
    coverage["receipt_hash"] = content_digest(
        {key: value for key, value in coverage.items() if key != "receipt_hash"},
    )

    assertions = [deepcopy(item) for item in ready.assertion_evidence]
    assertions[0]["verification_state"] = "unverified"
    assertions[0].pop("freshness")
    return {
        "lineage_graph": deepcopy(ready.lineage_graph),
        "governance_testament": deepcopy(ready.governance_testament),
        "source_census": census,
        "source_envelopes": envelopes,
        "normalized_events": events,
        "assertion_evidence": assertions,
        "normalization_parity_receipt": parity,
        "coverage_receipt": coverage,
    }


def _build_blocked_inputs(lineage_graph: dict):
    values = _blocked_input_values(lineage_graph)
    return build_reconcile_inputs(
        snapshot_id=SNAPSHOT,
        snapshot_digest=SNAPSHOT_DIGEST,
        snapshot_at=RECONCILED,
        allow_blocked=True,
        **values,
    )


def _traceable_store(root: Path) -> tuple[RegistryStore, dict]:
    store = RegistryStore(root)
    store.load()
    repo = store.create_entity(
        EntityType.REPO,
        "governed-repository",
        created_by="registry",
        metadata={"owner": "owner:governed-repository"},
        timestamp_ms=101,
    )
    store.record_observation(
        "metric:governance-coverage",
        repo.uid,
        0.5,
        source="test",
        metadata={"evidence_references": ["source:src_event_1"]},
    )
    store.save()

    source = RegistryStore(root.parent / f"{root.name}-lineage-source")
    source.load()
    directive = _intent_node(
        "intent:blocked-governance",
        entity_id=repo.uid,
    )
    directive.metadata.update(
        {
            "ideal_form_id": "ideal:blocked-governance",
            "predicate_receipts": [
                {
                    "predicate_id": "predicate:official-export",
                    "receipt_reference": "receipt:parity-fixture",
                    "result": "blocked",
                },
            ],
        },
    )
    source.add_authority_node(directive)
    source.save()
    graph = export_lineage_graph(
        source,
        snapshot_id=SNAPSHOT,
        generated_at=GENERATED,
    )
    return store, graph


def _input_files(root: Path, values: dict) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = root / f"{name}.json"
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths[name] = path
    return paths


def _predecessor_receipt(path: Path) -> str:
    body = {
        "contract_name": "governance-stage-receipt.v1",
        "contract_version": 1,
        "stage": "classify",
        "status": "completed",
        "snapshot_id": SNAPSHOT,
        "snapshot_digest": SNAPSHOT_DIGEST,
    }
    digest = content_digest(body)
    path.write_text(
        json.dumps({**body, "receipt_digest": digest}),
        encoding="utf-8",
    )
    return digest


def _owner_args(
    paths: dict[str, Path],
    *,
    predecessor: Path,
    state_root: Path,
    output: Path,
    artifact: Path,
) -> list[str]:
    return [
        "--snapshot-digest",
        SNAPSHOT_DIGEST,
        "--lineage",
        str(paths["lineage_graph"]),
        "--governance-testament",
        str(paths["governance_testament"]),
        "--source-census",
        str(paths["source_census"]),
        "--source-envelopes",
        str(paths["source_envelopes"]),
        "--normalized-events",
        str(paths["normalized_events"]),
        "--assertion-evidence",
        str(paths["assertion_evidence"]),
        "--normalization-parity",
        str(paths["normalization_parity_receipt"]),
        "--coverage",
        str(paths["coverage_receipt"]),
        "--predecessor-receipt",
        str(predecessor),
        "--state-root",
        str(state_root),
        "--out",
        str(output),
        "--artifact-out",
        str(artifact),
    ]


def _predicate_args(owner_args: list[str]) -> list[str]:
    args = owner_args.copy()
    index = args.index("--artifact-out")
    args[index] = "--artifact"
    return args


def _environment(
    monkeypatch: pytest.MonkeyPatch,
    run_root: Path,
    *,
    predecessor_digest: str,
    metrics: Path,
    traversal: int,
    prior_receipt: Path | None = None,
    max_items: int = 100,
    predicate: bool = False,
) -> None:
    values = {
        "LIMEN_GOV_STAGE": "reconcile",
        "LIMEN_GOV_SNAPSHOT_ID": SNAPSHOT,
        "LIMEN_GOV_SNAPSHOT_AT": RECONCILED,
        "LIMEN_GOV_RUN_ROOT": str(run_root),
        "LIMEN_GOV_STAGE_ATTEMPT": "1",
        "LIMEN_GOV_TRAVERSAL": str(traversal),
        "LIMEN_GOV_PROOF_MODE": "1" if traversal >= 2 else "0",
        "LIMEN_GOV_STAGE_METRICS_OUT": str(metrics),
        "LIMEN_GOV_STAGE_RECEIPTS": str(
            run_root / "governance-stage-receipts.v1.json",
        ),
        "LIMEN_GOV_PREDECESSOR_RECEIPT_DIGEST": predecessor_digest,
        "LIMEN_GOV_PRIOR_STAGE_RECEIPT": str(prior_receipt or ""),
        "LIMEN_GOV_MAX_ITEMS": str(max_items),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    if predicate:
        monkeypatch.setenv("LIMEN_GOV_PREDICATE_MODE", "1")
    else:
        monkeypatch.delenv("LIMEN_GOV_PREDICATE_MODE", raising=False)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_allow_blocked_preserves_debt_and_real_evidence(tmp_path: Path) -> None:
    store, graph = _traceable_store(tmp_path / "state")
    values = _blocked_input_values(graph)
    with pytest.raises(ValueError, match="normalization parity receipt is not ready"):
        build_reconcile_inputs(
            snapshot_id=SNAPSHOT,
            snapshot_digest=SNAPSHOT_DIGEST,
            snapshot_at=RECONCILED,
            **values,
        )

    inputs = _build_blocked_inputs(graph)
    output = tmp_path / "output"
    result = reconcile_governance_snapshot(
        store,
        inputs,
        output_dir=output,
        allow_blocked=True,
    )

    readiness = result["node_self_image_set"]["readiness"]
    assert readiness["exact_all"] is True
    assert readiness["ready"] is False
    assert readiness["status"] == "closed_with_owner_routed_debt"
    assert BLOCKED_SOURCE_ID in readiness["unresolved_blockers"]
    assert "src_residual_fixture" in readiness["quarantines"]
    assert "assertion:fixture" in readiness["citation_debt"]
    image = result["node_self_image_set"]["self_images"][0]
    assert image["active_ideal_forms"][0]["implementation_state"] == "blocked"
    assert "receipt:parity-fixture" in image["evidence_references"]
    assert "receipt:coverage-fixture" in image["evidence_references"]
    assert result["receipt"]["ready"] is False


def test_allow_blocked_rejects_synthetic_assertion_evidence(
    tmp_path: Path,
) -> None:
    _store, graph = _traceable_store(tmp_path / "state")
    inputs = _build_blocked_inputs(graph)
    inputs.assertion_evidence[0]["evidence_references"][0]["reference"] = "source:synthetic"
    with pytest.raises(ValueError, match="evidence is unresolved"):
        build_snapshot_evidence_index(inputs, allow_blocked=True)


def test_allow_blocked_rejects_unresolved_lineage_source(
    tmp_path: Path,
) -> None:
    _store, graph = _traceable_store(tmp_path / "state")
    values = _blocked_input_values(graph)
    values["lineage_graph"]["nodes"][0]["source_envelope_id"] = "src_synthetic"
    with pytest.raises(ValueError, match="source envelope is unresolved"):
        build_reconcile_inputs(
            snapshot_id=SNAPSHOT,
            snapshot_digest=SNAPSHOT_DIGEST,
            snapshot_at=RECONCILED,
            allow_blocked=True,
            **values,
        )


def test_reconcile_cadence_resumes_proves_and_detects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    _store, graph = _traceable_store(state_root)
    values = _blocked_input_values(graph)
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    paths = _input_files(inputs_root, values)
    predecessor = tmp_path / "04-classify.json"
    predecessor_digest = _predecessor_receipt(predecessor)
    run_root = tmp_path / "run"
    output = run_root / "reconcile"
    artifact = run_root / "ontologia-reconcile-stage.v1.json"
    owner_args = _owner_args(
        paths,
        predecessor=predecessor,
        state_root=state_root,
        output=output,
        artifact=artifact,
    )

    first_metrics = run_root / "metrics" / "reconcile.json"
    _environment(
        monkeypatch,
        run_root,
        predecessor_digest=predecessor_digest,
        metrics=first_metrics,
        traversal=1,
    )
    assert owner_main(owner_args) == 0
    first = json.loads(first_metrics.read_text(encoding="utf-8"))
    assert first["child_receipts"][0]["status"] == "completed"
    stage = json.loads(artifact.read_text(encoding="utf-8"))
    assert stage["readiness"]["exact_all"] is True
    assert stage["readiness"]["ready"] is False

    before_predicate = _tree_bytes(state_root)
    _environment(
        monkeypatch,
        run_root,
        predecessor_digest=predecessor_digest,
        metrics=first_metrics,
        traversal=1,
        predicate=True,
    )
    assert predicate_main(_predicate_args(owner_args)) == 0
    assert _tree_bytes(state_root) == before_predicate

    resume_metrics = run_root / "metrics" / "reconcile-resume.json"
    _environment(
        monkeypatch,
        run_root,
        predecessor_digest=predecessor_digest,
        metrics=resume_metrics,
        traversal=1,
    )
    governed_before = {
        **_tree_bytes(state_root),
        **{
            f"output/{path.name}": path.read_bytes()
            for path in (*output.iterdir(), artifact)
            if path.is_file()
        },
    }
    assert owner_main(owner_args) == 0
    resumed = json.loads(resume_metrics.read_text(encoding="utf-8"))
    assert resumed["emitted_events"] == 0
    assert resumed["child_receipts"][0]["status"] == "skipped_completed"

    prior = run_root / "reconcile-prior-stage-receipt.json"
    prior.write_text(
        json.dumps(
            {
                "stage": "reconcile",
                "child_receipts": first["child_receipts"],
            },
        ),
        encoding="utf-8",
    )
    proof_metrics = run_root / "metrics" / "proof" / "reconcile.json"
    _environment(
        monkeypatch,
        run_root,
        predecessor_digest=predecessor_digest,
        metrics=proof_metrics,
        traversal=2,
        prior_receipt=prior,
    )
    assert owner_main(owner_args) == 0
    proof = json.loads(proof_metrics.read_text(encoding="utf-8"))
    assert proof["emitted_events"] == 0
    assert proof["child_receipts"][0]["status"] == "skipped_completed"
    assert proof["child_receipts"][0]["prior_receipt_digest"].startswith("sha256:")
    governed_after = {
        **_tree_bytes(state_root),
        **{
            f"output/{path.name}": path.read_bytes()
            for path in (*output.iterdir(), artifact)
            if path.is_file()
        },
    }
    assert governed_after == governed_before

    tampered = json.loads(artifact.read_text(encoding="utf-8"))
    tampered["readiness"]["unresolved_blockers"].append("synthetic:blocker")
    artifact.write_text(json.dumps(tampered), encoding="utf-8")
    _environment(
        monkeypatch,
        run_root,
        predecessor_digest=predecessor_digest,
        metrics=proof_metrics,
        traversal=2,
        prior_receipt=prior,
        predicate=True,
    )
    assert predicate_main(_predicate_args(owner_args)) == 2
