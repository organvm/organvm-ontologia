"""Command-line interface for the Ontologia registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from ontologia.governance.reconcile import (
    export_lineage_graph,
    import_lineage_graph,
    load_materialized_snapshot_bundle,
    reconcile_snapshot_bundle,
)
from ontologia.registry.store import open_store


def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    reconcile.add_argument("--snapshot-bundle", required=True)
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
    args = build_parser().parse_args(argv)
    if args.command != "governance":
        return 2
    store = open_store(Path(args.state_root))
    if args.governance_command == "reconcile":
        result = reconcile_snapshot_bundle(
            store,
            load_materialized_snapshot_bundle(
                Path(args.snapshot_bundle),
                max_input_bytes=args.max_input_bytes,
                max_artifact_bytes=args.max_artifact_bytes,
            ),
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
