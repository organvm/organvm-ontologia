"""Command-line interface for the Ontologia registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from ontologia.governance.reconcile import (
    build_reconcile_inputs,
    export_lineage_graph,
    import_lineage_graph,
    load_materialized_snapshot_bundle,
    reconcile_governance_snapshot,
    reconcile_snapshot_bundle,
)
from ontologia.registry.store import open_store


def _load_json(path: str, max_bytes: int = 16_777_216) -> object:
    artifact_path = Path(path)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if artifact_path.stat().st_size > max_bytes:
        raise ValueError(f"governance artifact exceeds max bytes: {artifact_path.name}")
    return json.loads(artifact_path.read_text(encoding="utf-8"))


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ontologia")
    commands = parser.add_subparsers(dest="command", required=True)
    governance = commands.add_parser(
        "governance",
        help="Import, reconcile, and export reviewed governance memory",
    )
    governance_commands = governance.add_subparsers(dest="governance_command", required=True)

    reconcile = governance_commands.add_parser(
        "reconcile",
        help="Reconcile one frozen snapshot and export exact-one self-images",
    )
    reconcile_source = reconcile.add_mutually_exclusive_group(required=True)
    reconcile_source.add_argument("--snapshot-bundle")
    reconcile_source.add_argument("--lineage")
    reconcile.add_argument("--snapshot-id")
    reconcile.add_argument("--snapshot-digest")
    reconcile.add_argument("--snapshot-at")
    reconcile.add_argument("--governance-testament")
    reconcile.add_argument("--source-census")
    reconcile.add_argument("--source-envelopes")
    reconcile.add_argument("--normalized-events")
    reconcile.add_argument("--assertion-evidence")
    reconcile.add_argument("--normalization-parity")
    reconcile.add_argument("--coverage")
    reconcile.add_argument("--state-root", required=True)
    reconcile.add_argument("--out", required=True)
    reconcile.add_argument("--max-input-bytes", type=int, default=16_777_216)
    reconcile.add_argument("--max-artifact-bytes", type=int, default=16_777_216)

    import_command = governance_commands.add_parser(
        "import",
        help="Import a public lineage-graph.v1 document",
    )
    import_command.add_argument("--lineage", required=True)
    import_command.add_argument("--snapshot-id", required=True)
    import_command.add_argument("--state-root", required=True)

    export_command = governance_commands.add_parser(
        "export",
        help="Export the persisted public lineage-graph.v1 document",
    )
    export_command.add_argument("--snapshot-id", required=True)
    export_command.add_argument("--snapshot-at", required=True)
    export_command.add_argument("--state-root", required=True)
    export_command.add_argument("--out", required=True)
    export_command.add_argument("--graph-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "governance":
        return 2
    store = open_store(Path(args.state_root))
    if args.governance_command == "reconcile":
        if args.snapshot_bundle:
            result = reconcile_snapshot_bundle(
                store,
                load_materialized_snapshot_bundle(
                    Path(args.snapshot_bundle),
                    max_input_bytes=args.max_input_bytes,
                    max_artifact_bytes=args.max_artifact_bytes,
                ),
                output_dir=Path(args.out),
            )
        else:
            direct_fields = (
                "snapshot_id",
                "snapshot_digest",
                "snapshot_at",
                "governance_testament",
                "source_census",
                "source_envelopes",
                "normalized_events",
                "assertion_evidence",
                "normalization_parity",
                "coverage",
            )
            missing = [
                field_name
                for field_name in direct_fields
                if not getattr(args, field_name)
            ]
            if missing:
                parser.error(
                    "direct reconcile requires: "
                    + ", ".join(f"--{name.replace('_', '-')}" for name in missing),
                )
            inputs = build_reconcile_inputs(
                snapshot_id=args.snapshot_id,
                snapshot_digest=args.snapshot_digest,
                snapshot_at=args.snapshot_at,
                lineage_graph=_load_json(args.lineage, args.max_input_bytes),
                governance_testament=_load_json(
                    args.governance_testament,
                    args.max_input_bytes,
                ),
                source_census=_load_json(args.source_census, args.max_input_bytes),
                source_envelopes=_load_json(
                    args.source_envelopes,
                    args.max_input_bytes,
                ),
                normalized_events=_load_json(
                    args.normalized_events,
                    args.max_input_bytes,
                ),
                assertion_evidence=_load_json(
                    args.assertion_evidence,
                    args.max_input_bytes,
                ),
                normalization_parity_receipt=_load_json(
                    args.normalization_parity,
                    args.max_input_bytes,
                ),
                coverage_receipt=_load_json(
                    args.coverage,
                    args.max_input_bytes,
                ),
            )
            result = reconcile_governance_snapshot(
                store,
                inputs,
                output_dir=Path(args.out),
            )
        _print_json(result["receipt"])
        return 0
    if args.governance_command == "import":
        result = import_lineage_graph(
            store,
            _load_json(args.lineage),
            snapshot_id=args.snapshot_id,
        )
        payload = {
            "imported_node_ids": list(result.imported_node_ids),
            "imported_edge_ids": list(result.imported_edge_ids),
            "unresolved": list(result.unresolved),
            "ready": result.ready,
        }
        _print_json(payload)
        return 0 if result.ready else 1
    if args.governance_command == "export":
        graph = export_lineage_graph(
            store,
            snapshot_id=args.snapshot_id,
            generated_at=args.snapshot_at,
            graph_id=args.graph_id,
        )
        output = Path(args.out)
        rendered = json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        _print_json(
            {
                "contract_name": graph["contract_name"],
                "graph_id": graph["graph_id"],
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
            },
        )
        return 0
    return 2


__all__ = ["build_parser", "main"]
