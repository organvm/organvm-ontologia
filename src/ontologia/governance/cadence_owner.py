"""Execute Ontologia's bounded governance reconcile cadence stage."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ontologia.governance.cadence_contract import (
    OUTPUT_NAMES,
    OWNER_REFERENCE,
    CadenceContractError,
    ReconcileInputPaths,
    bounded_tree_size,
    build_stage_artifact,
    canonical_bytes,
    load_object,
    load_reconcile_inputs,
    validate_predecessor_receipt,
    verify_stage_artifact,
)
from ontologia.governance.memory import content_digest
from ontologia.governance.reconcile import reconcile_governance_snapshot
from ontologia.registry.store import RegistryStore, open_store


def _resolved(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _write_if_changed(path: Path, content: bytes) -> bool:
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return True


def _runtime() -> dict[str, Any]:
    if os.environ.get("LIMEN_GOV_STAGE") != "reconcile":
        raise CadenceContractError("LIMEN_GOV_STAGE must be 'reconcile'")
    snapshot_id = os.environ.get("LIMEN_GOV_SNAPSHOT_ID", "").strip()
    snapshot_at = os.environ.get("LIMEN_GOV_SNAPSHOT_AT", "").strip()
    metrics_path = os.environ.get("LIMEN_GOV_STAGE_METRICS_OUT", "").strip()
    predecessor_digest = os.environ.get(
        "LIMEN_GOV_PREDECESSOR_RECEIPT_DIGEST",
        "",
    ).strip()
    stage_receipts = os.environ.get("LIMEN_GOV_STAGE_RECEIPTS", "").strip()
    run_root = os.environ.get("LIMEN_GOV_RUN_ROOT", "").strip()
    prior_stage_receipt = os.environ.get(
        "LIMEN_GOV_PRIOR_STAGE_RECEIPT",
        "",
    ).strip()
    try:
        max_items = int(os.environ.get("LIMEN_GOV_MAX_ITEMS", ""))
        traversal = int(os.environ.get("LIMEN_GOV_TRAVERSAL", ""))
        attempt = int(os.environ.get("LIMEN_GOV_STAGE_ATTEMPT", ""))
    except ValueError as exc:
        raise CadenceContractError(
            "cadence traversal, attempt, and max_items must be integers",
        ) from exc
    if (
        not snapshot_id
        or not snapshot_at
        or not metrics_path
        or not stage_receipts
        or not run_root
        or max_items <= 0
        or traversal <= 0
        or attempt <= 0
    ):
        raise CadenceContractError("reconcile cadence environment is incomplete")
    proof_mode = os.environ.get("LIMEN_GOV_PROOF_MODE") == "1"
    if proof_mode is not (traversal >= 2):
        raise CadenceContractError(
            "LIMEN_GOV_PROOF_MODE contradicts the cadence traversal",
        )
    if proof_mode and not prior_stage_receipt:
        raise CadenceContractError(
            "proof traversal requires LIMEN_GOV_PRIOR_STAGE_RECEIPT",
        )
    return {
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "metrics_path": Path(metrics_path),
        "predecessor_digest": predecessor_digest,
        "stage_receipts": Path(stage_receipts),
        "run_root": Path(run_root),
        "prior_stage_receipt": Path(prior_stage_receipt),
        "max_items": max_items,
        "proof_mode": proof_mode,
    }


def _input_paths(args: argparse.Namespace) -> ReconcileInputPaths:
    return ReconcileInputPaths(
        lineage_graph=args.lineage,
        governance_testament=args.governance_testament,
        source_census=args.source_census,
        source_envelopes=args.source_envelopes,
        normalized_events=args.normalized_events,
        assertion_evidence=args.assertion_evidence,
        normalization_parity=args.normalization_parity,
        coverage=args.coverage,
    )


def _child_digest(child: Mapping[str, Any]) -> str:
    return content_digest(
        {
            "child_id": child["child_id"],
            "status": child["status"],
            "input_digest": child["input_digest"],
            "output_digest": child["output_digest"],
        },
    )


def _proof_child(
    completed: Mapping[str, Any],
    prior_receipt_path: Path,
) -> dict[str, Any]:
    prior_receipt = load_object(prior_receipt_path, max_bytes=16_777_216)
    children = prior_receipt.get("child_receipts")
    if (
        prior_receipt.get("stage") != "reconcile"
        or not isinstance(children, list)
        or len(children) != 1
        or not isinstance(children[0], Mapping)
    ):
        raise CadenceContractError(
            "proof traversal requires the prior reconcile stage receipt",
        )
    prior = children[0]
    if any(
        prior.get(field_name) != completed.get(field_name)
        for field_name in ("child_id", "input_digest", "output_digest")
    ):
        raise CadenceContractError(
            "proof child differs from the prior reconcile stage receipt",
        )
    return {
        **completed,
        "status": "skipped_completed",
        "prior_receipt_digest": _child_digest(prior),
    }


def _resume_child(completed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **completed,
        "status": "skipped_completed",
        "prior_receipt_digest": _child_digest(completed),
    }


def _emit_metrics(
    path: Path,
    child: Mapping[str, Any],
    *,
    emitted_events: int,
) -> None:
    payload = {
        "resume_token": None,
        "completed_child_ids": [child["child_id"]],
        "pending_child_ids": [],
        "child_receipts": [dict(child)],
        "emitted_events": emitted_events,
    }
    _write_if_changed(path, canonical_bytes(payload))


def _assert_same_outputs(expected_dir: Path, observed_dir: Path) -> None:
    for name in OUTPUT_NAMES:
        try:
            if (expected_dir / name).read_bytes() != (observed_dir / name).read_bytes():
                raise CadenceContractError(
                    f"proof reconciliation changed {name}",
                )
        except OSError as exc:
            raise CadenceContractError(
                f"proof cannot compare governed output {name}",
            ) from exc


def _copy_state_root(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
) -> None:
    bounded_tree_size(source, max_bytes=max_bytes)
    try:
        shutil.copytree(source, destination)
    except OSError as exc:
        raise CadenceContractError(
            "cannot create temporary proof custody for Ontologia state",
        ) from exc


def _execute_reconcile(
    *,
    state_root: Path,
    output_dir: Path,
    inputs: Any,
) -> tuple[RegistryStore, int]:
    store = open_store(state_root)
    before_nodes = len(store.authority_graph.nodes())
    before_edges = len(store.authority_graph.edges())
    try:
        reconcile_governance_snapshot(
            store,
            inputs,
            output_dir=output_dir,
            allow_blocked=True,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise CadenceContractError(str(exc)) from exc
    emitted_events = (
        len(store.authority_graph.nodes())
        - before_nodes
        + len(store.authority_graph.edges())
        - before_edges
    )
    return store, emitted_events


def run(args: argparse.Namespace) -> None:
    runtime = _runtime()
    input_paths = _input_paths(args)
    inputs = load_reconcile_inputs(
        input_paths,
        snapshot_id=runtime["snapshot_id"],
        snapshot_digest=args.snapshot_digest,
        snapshot_at=runtime["snapshot_at"],
        max_input_bytes=args.max_input_bytes,
        allow_blocked=True,
    )
    validate_predecessor_receipt(
        args.predecessor_receipt,
        expected_digest=runtime["predecessor_digest"],
        snapshot_id=inputs.snapshot_id,
        snapshot_digest=inputs.snapshot_digest,
    )
    bounded_tree_size(args.state_root, max_bytes=args.max_state_bytes)

    if runtime["proof_mode"]:
        governed_store = open_store(args.state_root)
        governed_artifact = verify_stage_artifact(
            args.artifact_out,
            store=governed_store,
            inputs=inputs,
            input_paths=input_paths,
            output_dir=args.out,
            predecessor_receipt_digest=runtime["predecessor_digest"],
            max_items=runtime["max_items"],
            owner_reference=args.owner_reference,
        )
        with tempfile.TemporaryDirectory(
            prefix="ontologia-governance-reconcile-proof-",
        ) as temporary:
            proof_root = Path(temporary)
            proof_state = proof_root / "state"
            proof_output = proof_root / "output"
            _copy_state_root(
                args.state_root,
                proof_state,
                max_bytes=args.max_state_bytes,
            )
            proof_store, _emitted = _execute_reconcile(
                state_root=proof_state,
                output_dir=proof_output,
                inputs=inputs,
            )
            proof_artifact = build_stage_artifact(
                store=proof_store,
                inputs=inputs,
                input_paths=input_paths,
                output_dir=proof_output,
                predecessor_receipt_digest=runtime["predecessor_digest"],
                max_items=runtime["max_items"],
                owner_reference=args.owner_reference,
            )
            _assert_same_outputs(proof_output, args.out)
            if canonical_bytes(proof_artifact) != args.artifact_out.read_bytes():
                raise CadenceContractError(
                    "proof reconciliation changed the cadence artifact",
                )
        child = _proof_child(
            governed_artifact["child_receipts"][0],
            runtime["prior_stage_receipt"],
        )
        _emit_metrics(runtime["metrics_path"], child, emitted_events=0)
        return

    store = open_store(args.state_root)
    resumed = False
    try:
        artifact = verify_stage_artifact(
            args.artifact_out,
            store=store,
            inputs=inputs,
            input_paths=input_paths,
            output_dir=args.out,
            predecessor_receipt_digest=runtime["predecessor_digest"],
            max_items=runtime["max_items"],
            owner_reference=args.owner_reference,
        )
        resumed = True
        emitted_events = 0
    except CadenceContractError:
        store, emitted_events = _execute_reconcile(
            state_root=args.state_root,
            output_dir=args.out,
            inputs=inputs,
        )
        artifact = build_stage_artifact(
            store=store,
            inputs=inputs,
            input_paths=input_paths,
            output_dir=args.out,
            predecessor_receipt_digest=runtime["predecessor_digest"],
            max_items=runtime["max_items"],
            owner_reference=args.owner_reference,
        )
        _write_if_changed(args.artifact_out, canonical_bytes(artifact))
    completed = artifact["child_receipts"][0]
    child = _resume_child(completed) if resumed else dict(completed)
    _emit_metrics(
        runtime["metrics_path"],
        child,
        emitted_events=0 if resumed else emitted_events,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-digest", required=True)
    parser.add_argument("--lineage", type=_resolved, required=True)
    parser.add_argument("--governance-testament", type=_resolved, required=True)
    parser.add_argument("--source-census", type=_resolved, required=True)
    parser.add_argument("--source-envelopes", type=_resolved, required=True)
    parser.add_argument("--normalized-events", type=_resolved, required=True)
    parser.add_argument("--assertion-evidence", type=_resolved, required=True)
    parser.add_argument("--normalization-parity", type=_resolved, required=True)
    parser.add_argument("--coverage", type=_resolved, required=True)
    parser.add_argument("--predecessor-receipt", type=_resolved, required=True)
    parser.add_argument("--state-root", type=_resolved, required=True)
    parser.add_argument("--out", type=_resolved, required=True)
    parser.add_argument("--artifact-out", type=_resolved, required=True)
    parser.add_argument("--max-input-bytes", type=int, default=536_870_912)
    parser.add_argument("--max-state-bytes", type=int, default=536_870_912)
    parser.add_argument("--owner-reference", default=OWNER_REFERENCE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except (CadenceContractError, OSError, ValueError) as exc:
        print(f"ontologia reconcile cadence owner: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
