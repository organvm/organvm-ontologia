"""Enhanced event bus — append-only JSONL with entity-scoped change tracking.

Extends the pulse event pattern with subject_entity, changed_property,
previous_value, and new_value fields. This makes every event a complete
audit record: who changed, what changed, from what, to what.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Event type constants (superset of pulse event types)
# ---------------------------------------------------------------------------

# Entity lifecycle
ENTITY_CREATED = "entity.created"
ENTITY_DEPRECATED = "entity.deprecated"
ENTITY_ARCHIVED = "entity.archived"

# Naming
NAME_ADDED = "name.added"
NAME_RETIRED = "name.retired"
NAME_PRIMARY_CHANGED = "name.primary_changed"

# Mutations
ENTITY_RENAMED = "entity.renamed"
ENTITY_RELOCATED = "entity.relocated"
ENTITY_RECLASSIFIED = "entity.reclassified"
ENTITY_MERGED = "entity.merged"
ENTITY_SPLIT = "entity.split"

# Registry
BOOTSTRAP_COMPLETED = "bootstrap.completed"
STORE_LOADED = "store.loaded"
STORE_SAVED = "store.saved"

ALL_ONTOLOGIA_TYPES: list[str] = [
    ENTITY_CREATED,
    ENTITY_DEPRECATED,
    ENTITY_ARCHIVED,
    NAME_ADDED,
    NAME_RETIRED,
    NAME_PRIMARY_CHANGED,
    ENTITY_RENAMED,
    ENTITY_RELOCATED,
    ENTITY_RECLASSIFIED,
    ENTITY_MERGED,
    ENTITY_SPLIT,
    BOOTSTRAP_COMPLETED,
    STORE_LOADED,
    STORE_SAVED,
]


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class OntologiaEvent:
    """A system event with entity-scoped change tracking."""

    event_type: str
    source: str
    subject_entity: str | None = None
    changed_property: str | None = None
    previous_value: Any = None
    new_value: Any = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_type": self.event_type,
            "source": self.source,
            "timestamp": self.timestamp,
        }
        if self.subject_entity is not None:
            d["subject_entity"] = self.subject_entity
        if self.changed_property is not None:
            d["changed_property"] = self.changed_property
        if self.previous_value is not None:
            d["previous_value"] = self.previous_value
        if self.new_value is not None:
            d["new_value"] = self.new_value
        if self.payload:
            d["payload"] = self.payload
        return d

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OntologiaEvent:
        return cls(
            event_type=data.get("event_type", ""),
            source=data.get("source", ""),
            subject_entity=data.get("subject_entity"),
            changed_property=data.get("changed_property"),
            previous_value=data.get("previous_value"),
            new_value=data.get("new_value"),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

# In-memory subscriber registry
_subscribers: dict[str, list[Callable[[OntologiaEvent], None]]] = {}


def subscribe(event_type: str, handler: Callable[[OntologiaEvent], None]) -> None:
    """Register a handler for a specific event type.

    Handlers are called synchronously when an event of the given type is emitted.
    Use "*" to subscribe to all event types.
    """
    _subscribers.setdefault(event_type, []).append(handler)


def unsubscribe(event_type: str, handler: Callable[[OntologiaEvent], None]) -> None:
    """Remove a handler for a specific event type."""
    handlers = _subscribers.get(event_type, [])
    if handler in handlers:
        handlers.remove(handler)


def clear_subscribers() -> None:
    """Remove all subscribers. Primarily for testing."""
    _subscribers.clear()


def _notify(event: OntologiaEvent) -> None:
    """Notify all matching subscribers of an event."""
    # Type-specific subscribers
    for handler in _subscribers.get(event.event_type, []):
        handler(event)
    # Wildcard subscribers
    for handler in _subscribers.get("*", []):
        handler(event)


# ---------------------------------------------------------------------------
# Emit + Replay (file-backed)
# ---------------------------------------------------------------------------

def _default_events_path() -> Path:
    return Path.home() / ".organvm" / "ontologia" / "events.jsonl"


# Module-level override for testing
_events_path_override: Path | None = None


def set_events_path(path: Path | None) -> None:
    """Override the events file path. Pass None to reset to default."""
    global _events_path_override  # noqa: PLW0603
    _events_path_override = path


def _events_path() -> Path:
    if _events_path_override is not None:
        return _events_path_override
    return _default_events_path()


def emit(
    event_type: str,
    source: str,
    subject_entity: str | None = None,
    changed_property: str | None = None,
    previous_value: Any = None,
    new_value: Any = None,
    payload: dict[str, Any] | None = None,
) -> OntologiaEvent:
    """Create, persist, and broadcast an event.

    Args:
        event_type: Type string (use constants from this module).
        source: What component emitted this event.
        subject_entity: UID of the entity this event concerns.
        changed_property: Which property changed (if applicable).
        previous_value: Value before the change.
        new_value: Value after the change.
        payload: Additional event data.

    Returns:
        The emitted event.
    """
    event = OntologiaEvent(
        event_type=event_type,
        source=source,
        subject_entity=subject_entity,
        changed_property=changed_property,
        previous_value=previous_value,
        new_value=new_value,
        payload=payload or {},
    )
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(event.to_jsonl() + "\n")

    _notify(event)
    return event


def replay(
    since: str | None = None,
    event_type: str | None = None,
    subject_entity: str | None = None,
    limit: int = 500,
    path: Path | None = None,
    on_error: Callable[[int, str, Exception], None] | None = None,
) -> list[OntologiaEvent]:
    """Read events from the JSONL log with optional filters.

    Args:
        since: ISO timestamp — only return events after this time.
        event_type: Filter by event type.
        subject_entity: Filter by subject entity UID.
        limit: Maximum events to return (from the tail).
        path: Override the events file path.

    Returns:
        Matching events, most recent last.
    """
    file_path = path or _events_path()
    if not file_path.is_file():
        return []

    events: list[OntologiaEvent] = []
    for line_number, raw_line in enumerate(file_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            if on_error is not None:
                on_error(line_number, raw_line, error)
            continue

        if event_type and data.get("event_type") != event_type:
            continue
        if since and data.get("timestamp", "") <= since:
            continue
        if subject_entity and data.get("subject_entity") != subject_entity:
            continue

        events.append(OntologiaEvent.from_dict(data))

    return events[-limit:]


def recent(n: int = 20) -> list[OntologiaEvent]:
    """Return the last n events from the log."""
    return replay(limit=n)
