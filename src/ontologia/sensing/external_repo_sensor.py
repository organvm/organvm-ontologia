"""ExternalRepoSensor — detect upstream state changes on absorbed repos.

Reads the ``repo_snapshot`` table (written by alchemia when a dossier is built)
and emits a RawSignal when a repository's upstream state advances between two
snapshots (e.g. a new push / release moved ``pushed_at`` forward). These become
STATE changes downstream.

Read-only: this sensor never writes to the portal store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ontologia.sensing._bifrons import open_readonly, portal_db_path, table_exists
from ontologia.sensing.interfaces import RawSignal

SIGNAL_EXTERNAL_CHANGED = "external_repo_changed"


class ExternalRepoSensor:
    """Detect upstream drift on absorbed external repositories."""

    def __init__(self, db_path=None, *, limit: int = 1000) -> None:
        self._db_path = db_path or portal_db_path()
        self._limit = limit

    @property
    def name(self) -> str:
        return "external_repo"

    def is_available(self) -> bool:
        conn = open_readonly(self._db_path)
        if conn is None:
            return False
        try:
            return table_exists(conn, "repo_snapshot")
        finally:
            conn.close()

    def scan(self) -> list[RawSignal]:
        conn = open_readonly(self._db_path)
        if conn is None:
            return []
        try:
            if not table_exists(conn, "repo_snapshot"):
                return []
            rows = conn.execute(
                "SELECT node_id, full_name, ref, pushed_at, snapshot_at "
                "FROM repo_snapshot ORDER BY node_id, id",
            ).fetchall()
        finally:
            conn.close()

        # Group snapshots per repo and compare consecutive pushed_at values.
        latest: dict[str, dict] = {}
        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            node = row["node_id"]
            prev = latest.get(node)
            if prev is not None and row["pushed_at"] and row["pushed_at"] != prev["pushed_at"]:
                signals.append(RawSignal(
                    sensor_name=self.name,
                    signal_type=SIGNAL_EXTERNAL_CHANGED,
                    entity_id=node,
                    details={
                        "full_name": row["full_name"],
                        "previous_value": prev["pushed_at"],
                        "value": row["pushed_at"],
                        "ref": row["ref"],
                    },
                    timestamp=row["snapshot_at"] or now,
                ))
            latest[node] = {"pushed_at": row["pushed_at"]}
        return signals[: self._limit]
