"""Timestamped observation store — append-only JSONL.

Every metric reading is recorded as an observation with the metric_id,
entity_id, timestamp, value, and source. Rolling computations are derived
at query time from this raw data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Observation:
    """A single metric reading at a point in time."""

    metric_id: str
    entity_id: str
    value: float
    timestamp: str = field(default_factory=_now_iso)
    source: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "metric_id": self.metric_id,
            "entity_id": self.entity_id,
            "value": self.value,
            "timestamp": self.timestamp,
            "source": self.source,
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        return cls(
            metric_id=data["metric_id"],
            entity_id=data["entity_id"],
            value=float(data["value"]),
            timestamp=data.get("timestamp", ""),
            source=data.get("source", "system"),
            metadata=data.get("metadata", {}),
        )


class ObservationStore:
    """JSONL-backed observation store with query capabilities."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._observations: list[Observation] = []

    @property
    def path(self) -> Path:
        return self._path

    def load(
        self,
        on_error: Callable[[int, str, Exception], None] | None = None,
    ) -> None:
        """Load existing observations from JSONL.

        ``on_error`` lets the owning registry preserve a hashed quarantine
        diagnostic while keeping the historical skip-and-continue behavior.
        """
        self._observations.clear()
        if not self._path.is_file():
            return
        for line_number, raw_line in enumerate(self._path.read_text().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                self._observations.append(Observation.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError) as error:
                if on_error is not None:
                    on_error(line_number, raw_line, error)
                continue

    def record(self, obs: Observation) -> None:
        """Record an observation (in-memory + append to file)."""
        self._observations.append(obs)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(obs.to_jsonl() + "\n")

    def observe(
        self,
        metric_id: str,
        entity_id: str,
        value: float,
        source: str = "system",
    ) -> Observation:
        """Convenience: create and record an observation."""
        obs = Observation(
            metric_id=metric_id,
            entity_id=entity_id,
            value=value,
            source=source,
        )
        self.record(obs)
        return obs

    def query(
        self,
        metric_id: str | None = None,
        entity_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> list[Observation]:
        """Query observations with optional filters."""
        results: list[Observation] = []
        for obs in self._observations:
            if metric_id and obs.metric_id != metric_id:
                continue
            if entity_id and obs.entity_id != entity_id:
                continue
            if since and obs.timestamp < since:
                continue
            if until and obs.timestamp > until:
                continue
            results.append(obs)

        if limit:
            results = results[-limit:]
        return results

    def latest(self, metric_id: str, entity_id: str) -> Observation | None:
        """Get the most recent observation for a metric+entity pair."""
        for obs in reversed(self._observations):
            if obs.metric_id == metric_id and obs.entity_id == entity_id:
                return obs
        return None

    def time_series(
        self,
        metric_id: str,
        entity_id: str,
        since: str | None = None,
    ) -> list[tuple[str, float]]:
        """Get (timestamp, value) pairs for a metric+entity."""
        return [
            (obs.timestamp, obs.value)
            for obs in self.query(metric_id=metric_id, entity_id=entity_id, since=since)
        ]

    @property
    def count(self) -> int:
        return len(self._observations)
