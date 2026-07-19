"""Read-only contracts for Ontologia's governance reconcile cadence stage."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ontologia.governance.memory import content_digest
from ontologia.governance.reconcile import (
    ReconcileInputs,
    build_reconcile_inputs,
    build_self_image_set,
    build_snapshot_evidence_index,
    export_lineage_graph,
)
from ontologia.registry.store import RegistryStore

OWNER_REFERENCE = "repo:organvm/organvm-ontologia"
STAGE_CONTRACT = "ontologia-governance-reconcile-stage.v1"
OUTPUT_NAMES = (
    "lineage-graph.json",
    "node-self-image-set.json",
    "reconciliation-receipt.json",
)
READINESS_DEBT_FIELDS = (
    "unresolved_blockers",
    "quarantines",
    "missing_requirements",
    "citation_debt",
    "incomplete_predicates",
)
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class CadenceContractError(RuntimeError):
    """Owner artifacts cannot prove an exact bounded reconcile stage."""


@dataclass(frozen=True)
class ReconcileInputPaths:
    lineage_graph: Path
    governance_testament: Path
    source_census: Path
    source_envelopes: Path
    normalized_events: Path
    assertion_evidence: Path
    normalization_parity: Path
    coverage: Path

    def ordered(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("lineage_graph", self.lineage_graph),
            ("governance_testament", self.governance_testament),
            ("source_census", self.source_census),
            ("source_envelopes", self.source_envelopes),
            ("normalized_events", self.normalized_events),
            ("assertion_evidence", self.assertion_evidence),
            ("normalization_parity_receipt", self.normalization_parity),
            ("coverage", self.coverage),
        )


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def load_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    value = load_value(path, max_bytes=max_bytes)
    if not isinstance(value, dict):
        raise CadenceContractError(f"{path.name} must contain a JSON object")
    return value


def load_sequence(
    path: Path,
    *,
    max_bytes: int,
    allow_object: bool = False,
) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        if path.stat().st_size > max_bytes:
            raise CadenceContractError(f"{path.name} exceeds the input byte limit")
        rows: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise CadenceContractError(
                            f"{path.name}:{line_number} must contain an object",
                        )
                    rows.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CadenceContractError(
                f"cannot read valid JSONL from {path.name}",
            ) from exc
        if not rows:
            raise CadenceContractError(f"{path.name} must be nonempty")
        return rows
    value = load_value(path, max_bytes=max_bytes)
    if allow_object and isinstance(value, dict):
        return [value]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, dict) for item in value)
    ):
        raise CadenceContractError(
            f"{path.name} must contain a nonempty JSON object array",
        )
    return value


def load_value(path: Path, *, max_bytes: int) -> Any:
    if max_bytes <= 0:
        raise CadenceContractError("max input bytes must be positive")
    try:
        if path.stat().st_size > max_bytes:
            raise CadenceContractError(f"{path.name} exceeds the input byte limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except CadenceContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CadenceContractError(f"cannot read valid JSON from {path.name}") from exc


def digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise CadenceContractError(f"cannot hash {path.name}") from exc
    return f"sha256:{digest.hexdigest()}", size


def bounded_tree_size(root: Path, *, max_bytes: int) -> int:
    if max_bytes <= 0 or not root.is_dir():
        raise CadenceContractError(
            "Ontologia state root and byte limit must be valid",
        )
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise CadenceContractError(
                    "Ontologia state root cannot contain symbolic links",
                )
            if path.is_file():
                total += path.stat().st_size
                if total > max_bytes:
                    raise CadenceContractError(
                        "Ontologia state exceeds the finite byte limit",
                    )
    except OSError as exc:
        raise CadenceContractError("cannot inspect Ontologia state bytes") from exc
    return total


def artifact_digest(value: Mapping[str, Any]) -> str:
    return content_digest(
        {key: item for key, item in value.items() if key != "artifact_digest"},
    )


def load_reconcile_inputs(
    paths: ReconcileInputPaths,
    *,
    snapshot_id: str,
    snapshot_digest: str,
    snapshot_at: str,
    max_input_bytes: int,
    allow_blocked: bool = True,
) -> ReconcileInputs:
    try:
        total_input_bytes = sum(path.stat().st_size for _name, path in paths.ordered())
    except OSError as exc:
        raise CadenceContractError("cannot inspect reconcile input bytes") from exc
    if total_input_bytes > max_input_bytes:
        raise CadenceContractError("reconcile inputs exceed the aggregate byte limit")
    try:
        return build_reconcile_inputs(
            snapshot_id=snapshot_id,
            snapshot_digest=snapshot_digest,
            snapshot_at=snapshot_at,
            lineage_graph=load_object(
                paths.lineage_graph,
                max_bytes=max_input_bytes,
            ),
            governance_testament=load_object(
                paths.governance_testament,
                max_bytes=max_input_bytes,
            ),
            source_census=load_object(
                paths.source_census,
                max_bytes=max_input_bytes,
            ),
            source_envelopes=load_sequence(
                paths.source_envelopes,
                max_bytes=max_input_bytes,
            ),
            normalized_events=load_sequence(
                paths.normalized_events,
                max_bytes=max_input_bytes,
            ),
            assertion_evidence=load_sequence(
                paths.assertion_evidence,
                max_bytes=max_input_bytes,
                allow_object=True,
            ),
            normalization_parity_receipt=load_object(
                paths.normalization_parity,
                max_bytes=max_input_bytes,
            ),
            coverage_receipt=load_object(
                paths.coverage,
                max_bytes=max_input_bytes,
            ),
            allow_blocked=allow_blocked,
        )
    except (OSError, ValueError) as exc:
        raise CadenceContractError(str(exc)) from exc


def validate_predecessor_receipt(
    path: Path,
    *,
    expected_digest: str,
    snapshot_id: str,
    snapshot_digest: str,
) -> dict[str, Any]:
    if not SHA256.fullmatch(expected_digest):
        raise CadenceContractError(
            "reconcile requires a valid predecessor receipt digest",
        )
    receipt = load_object(path, max_bytes=16_777_216)
    payload = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if (
        receipt.get("contract_name") != "governance-stage-receipt.v1"
        or receipt.get("stage") != "classify"
        or receipt.get("status") != "completed"
        or receipt.get("snapshot_id") != snapshot_id
        or receipt.get("snapshot_digest") != snapshot_digest
        or receipt.get("receipt_digest") != expected_digest
        or receipt.get("receipt_digest") != content_digest(payload)
    ):
        raise CadenceContractError(
            "classify predecessor receipt does not match the cadence binding",
        )
    return receipt


def _validate_readiness(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CadenceContractError("reconcile readiness must be an object")
    exact_all = value.get("exact_all")
    ready = value.get("ready")
    status = value.get("status")
    if not isinstance(exact_all, bool) or not isinstance(ready, bool):
        raise CadenceContractError("reconcile readiness booleans are invalid")
    readiness: dict[str, Any] = {"exact_all": exact_all}
    for field_name in READINESS_DEBT_FIELDS:
        debt = value.get(field_name)
        if (
            not isinstance(debt, list)
            or len(debt) != len(set(map(str, debt)))
            or not all(isinstance(item, str) and item for item in debt)
        ):
            raise CadenceContractError(
                f"reconcile readiness {field_name} is invalid",
            )
        readiness[field_name] = sorted(debt)
    computed_ready = exact_all and not any(
        readiness[field_name] for field_name in READINESS_DEBT_FIELDS
    )
    if ready is not computed_ready:
        raise CadenceContractError("reconcile readiness contradicts its debt")
    if ready and status != "ready":
        raise CadenceContractError("ready reconciliation has a non-ready status")
    if not ready and status not in {
        "blocked",
        "closed_with_owner_routed_debt",
    }:
        raise CadenceContractError("blocked reconciliation has an invalid status")
    readiness.update({"ready": ready, "status": status})
    return readiness


def expected_owner_documents(
    store: RegistryStore,
    inputs: ReconcileInputs,
) -> dict[str, dict[str, Any]]:
    try:
        evidence_index = build_snapshot_evidence_index(
            inputs,
            allow_blocked=True,
        )
        lineage_graph = export_lineage_graph(
            store,
            snapshot_id=inputs.snapshot_id,
            generated_at=inputs.generated_at,
            graph_id=str(inputs.lineage_graph["graph_id"]),
        )
        self_image_set = build_self_image_set(
            store,
            evidence_index=evidence_index,
            reconciled_at=inputs.generated_at,
            constitutional_digest=content_digest(inputs.governance_testament),
            readiness=inputs.readiness,
            allow_blocked=True,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise CadenceContractError(str(exc)) from exc
    receipt = {
        "contract_name": "governance-reconciliation-receipt.v1",
        "contract_version": 1,
        "receipt_id": f"ontologia-reconcile:{inputs.snapshot_id}",
        "snapshot_id": inputs.snapshot_id,
        "snapshot_digest": inputs.snapshot_digest,
        "snapshot_at": inputs.snapshot_at,
        "generated_at": inputs.generated_at,
        "input_digest": inputs.input_digest,
        "evidence_index_digest": evidence_index.digest,
        "lineage_digest": content_digest(lineage_graph),
        "self_image_set_digest": self_image_set["set_digest"],
        "counts": {
            "registered_nodes": len(self_image_set["registered_node_ids"]),
            "self_images": len(self_image_set["self_images"]),
            "lineage_nodes": len(lineage_graph["nodes"]),
            "lineage_edges": len(lineage_graph["edges"]),
            "unresolved": 0,
        },
        "exact_one": self_image_set["readiness"]["exact_all"],
        "unresolved": [],
        "readiness": dict(self_image_set["readiness"]),
        "ready": bool(self_image_set["readiness"]["ready"]),
    }
    return {
        "lineage-graph.json": lineage_graph,
        "node-self-image-set.json": self_image_set,
        "reconciliation-receipt.json": receipt,
    }


def _observations(
    entries: tuple[tuple[str, Path], ...],
) -> list[dict[str, Any]]:
    observations = []
    for artifact_id, path in entries:
        observed_digest, size = digest_file(path)
        observations.append(
            {
                "artifact_id": artifact_id,
                "reference": path.name,
                "digest": observed_digest,
                "byte_count": size,
            },
        )
    return observations


def build_stage_artifact(
    *,
    store: RegistryStore,
    inputs: ReconcileInputs,
    input_paths: ReconcileInputPaths,
    output_dir: Path,
    predecessor_receipt_digest: str,
    max_items: int,
    owner_reference: str = OWNER_REFERENCE,
) -> dict[str, Any]:
    if max_items <= 0:
        raise CadenceContractError("max_items must be positive")
    expected = expected_owner_documents(store, inputs)
    for name, expected_value in expected.items():
        observed = load_object(output_dir / name, max_bytes=1 << 31)
        if observed != expected_value:
            raise CadenceContractError(
                f"{name} differs from persisted owner-native reconciliation state",
            )
    work_item_count = sum(
        (
            len(inputs.source_census["raw_units"]),
            len(inputs.source_envelopes),
            len(inputs.normalized_events),
            len(inputs.assertion_evidence),
            len(inputs.lineage_graph["nodes"]),
            len(inputs.lineage_graph["edges"]),
            len(store.list_entities()),
        ),
    )
    if work_item_count <= 0 or work_item_count > max_items:
        raise CadenceContractError(
            "reconcile work denominator is empty or exceeds LIMEN_GOV_MAX_ITEMS",
        )
    input_observations = _observations(input_paths.ordered())
    output_observations = _observations(
        tuple((name.removesuffix(".json"), output_dir / name) for name in OUTPUT_NAMES),
    )
    readiness = _validate_readiness(
        expected["node-self-image-set.json"]["readiness"],
    )
    child_input_digest = content_digest(
        {
            "reconcile_input_digest": inputs.input_digest,
            "predecessor_receipt_digest": predecessor_receipt_digest,
            "input_artifacts": input_observations,
        },
    )
    child_output_digest = content_digest(output_observations)
    child = {
        "child_id": f"ontologia-reconcile:{inputs.snapshot_id}",
        "status": "completed",
        "input_digest": child_input_digest,
        "output_digest": child_output_digest,
    }
    body = {
        "contract_name": STAGE_CONTRACT,
        "contract_version": 1,
        "artifact_id": f"ontologia-reconcile-stage:{inputs.snapshot_id}",
        "snapshot_id": inputs.snapshot_id,
        "snapshot_digest": inputs.snapshot_digest,
        "snapshot_at": inputs.snapshot_at,
        "owner_reference": owner_reference,
        "predecessor_receipt_digest": predecessor_receipt_digest,
        "reconcile_input_digest": inputs.input_digest,
        "input_artifacts": input_observations,
        "output_artifacts": output_observations,
        "work_item_count": work_item_count,
        "child_receipts": [child],
        "readiness": readiness,
        "digest_algorithm": "sha256-rfc8785-excluding-artifact-digest-v1",
    }
    return {**body, "artifact_digest": content_digest(body)}


def verify_stage_artifact(
    artifact_path: Path,
    *,
    store: RegistryStore,
    inputs: ReconcileInputs,
    input_paths: ReconcileInputPaths,
    output_dir: Path,
    predecessor_receipt_digest: str,
    max_items: int,
    owner_reference: str = OWNER_REFERENCE,
) -> dict[str, Any]:
    observed = load_object(artifact_path, max_bytes=16_777_216)
    if observed.get("artifact_digest") != artifact_digest(observed):
        raise CadenceContractError("reconcile stage artifact digest mismatch")
    expected = build_stage_artifact(
        store=store,
        inputs=inputs,
        input_paths=input_paths,
        output_dir=output_dir,
        predecessor_receipt_digest=predecessor_receipt_digest,
        max_items=max_items,
        owner_reference=owner_reference,
    )
    if observed != expected:
        raise CadenceContractError(
            "reconcile stage artifact differs from owner-native evidence",
        )
    return observed
