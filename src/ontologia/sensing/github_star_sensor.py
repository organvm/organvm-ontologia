"""GitHubStarSensor — emit star/unstar events from the BIFRONS portal store.

Reads the ``star_event`` table (written by alchemia's star sync) and emits one
RawSignal per event. Star and unstar become RELATION changes downstream (a star
is the ``starred`` relation coming into existence / being retired).

Read-only: this sensor never writes to the portal store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ontologia.sensing._bifrons import open_readonly, portal_db_path, table_exists
from ontologia.sensing.interfaces import RawSignal

SIGNAL_STAR = "github_star"
SIGNAL_UNSTAR = "github_unstar"


class GitHubStarSensor:
    """Detect star/unstar events recorded by the BIFRONS portal."""

    def __init__(self, db_path=None, *, since_id: int = 0, limit: int = 1000) -> None:
        self._db_path = db_path or portal_db_path()
        self._since_id = since_id
        self._limit = limit

    @property
    def name(self) -> str:
        return "github_star"

    def is_available(self) -> bool:
        conn = open_readonly(self._db_path)
        if conn is None:
            return False
        try:
            return table_exists(conn, "star_event")
        finally:
            conn.close()

    def scan(self) -> list[RawSignal]:
        conn = open_readonly(self._db_path)
        if conn is None:
            return []
        try:
            if not table_exists(conn, "star_event"):
                return []
            rows = conn.execute(
                "SELECT id, node_id, full_name, event, at, exchange_id "
                "FROM star_event WHERE id > ? ORDER BY id LIMIT ?",
                (self._since_id, self._limit),
            ).fetchall()
        finally:
            conn.close()

        now = datetime.now(timezone.utc).isoformat()
        signals: list[RawSignal] = []
        for row in rows:
            signals.append(RawSignal(
                sensor_name=self.name,
                signal_type=SIGNAL_UNSTAR if row["event"] == "unstar" else SIGNAL_STAR,
                entity_id=row["node_id"],
                details={
                    "full_name": row["full_name"],
                    "event": row["event"],
                    "value": row["full_name"],
                    "at": row["at"],
                    "exchange_id": row["exchange_id"],
                    "event_id": row["id"],
                },
                timestamp=row["at"] or now,
            ))
        return signals
