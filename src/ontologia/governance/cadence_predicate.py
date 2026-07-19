"""Independently validate Ontologia's reconcile cadence artifacts read-only."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from ontologia.governance.cadence_contract import (
    OWNER_REFERENCE,
    CadenceContractError,
    ReconcileInputPaths,
    bounded_tree_size,
    load_reconcile_inputs,
    validate_predecessor_receipt,
    verify_stage_artifact,
)
from ontologia.registry.store import open_store


def _resolved(value: str) -> Path:
    return Path(value).expanduser().resolve()


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


def run(args: argparse.Namespace) -> None:
    if os.environ.get("LIMEN_GOV_PREDICATE_MODE") != "1":
        raise CadenceContractError(
            "predicate requires LIMEN_GOV_PREDICATE_MODE=1",
        )
    if os.environ.get("LIMEN_GOV_STAGE") != "reconcile":
        raise CadenceContractError("predicate stage must be reconcile")
    snapshot_id = os.environ.get("LIMEN_GOV_SNAPSHOT_ID", "").strip()
    snapshot_at = os.environ.get("LIMEN_GOV_SNAPSHOT_AT", "").strip()
    predecessor_digest = os.environ.get(
        "LIMEN_GOV_PREDECESSOR_RECEIPT_DIGEST",
        "",
    ).strip()
    try:
        max_items = int(os.environ.get("LIMEN_GOV_MAX_ITEMS", ""))
    except ValueError as exc:
        raise CadenceContractError(
            "LIMEN_GOV_MAX_ITEMS must be a positive integer",
        ) from exc
    if not snapshot_id or not snapshot_at or max_items <= 0:
        raise CadenceContractError("predicate snapshot environment is incomplete")
    input_paths = _input_paths(args)
    inputs = load_reconcile_inputs(
        input_paths,
        snapshot_id=snapshot_id,
        snapshot_digest=args.snapshot_digest,
        snapshot_at=snapshot_at,
        max_input_bytes=args.max_input_bytes,
        allow_blocked=True,
    )
    validate_predecessor_receipt(
        args.predecessor_receipt,
        expected_digest=predecessor_digest,
        snapshot_id=inputs.snapshot_id,
        snapshot_digest=inputs.snapshot_digest,
    )
    bounded_tree_size(args.state_root, max_bytes=args.max_state_bytes)
    verify_stage_artifact(
        args.artifact,
        store=open_store(args.state_root),
        inputs=inputs,
        input_paths=input_paths,
        output_dir=args.out,
        predecessor_receipt_digest=predecessor_digest,
        max_items=max_items,
        owner_reference=args.owner_reference,
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
    parser.add_argument("--artifact", type=_resolved, required=True)
    parser.add_argument("--max-input-bytes", type=int, default=536_870_912)
    parser.add_argument("--max-state-bytes", type=int, default=536_870_912)
    parser.add_argument("--owner-reference", default=OWNER_REFERENCE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except (CadenceContractError, OSError, ValueError) as exc:
        print(f"ontologia reconcile cadence predicate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
