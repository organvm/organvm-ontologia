"""Entity identity — immutable UID-based identification for all system objects.

Every entity in the system (organ, repo, module, document, session) gets a
permanent identity that survives renames, relocations, merges, and splits.
The UID is assigned once at creation and never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ontologia._ulid import generate_ulid


class EntityType(str, Enum):
    """Known entity types in the system.

    Each leaf type derives from a SPEC-000 primitive through the SPEC-001
    stratified taxonomy (ONT-001 through ONT-028). The derivation chain
    is: SPEC-000 Primitive -> SPEC-001 Category Path -> Leaf EntityType.

    See: specs/SPEC-001-ENTITY-MAPPING.md for the full derivation table.
    """

    # Independent Continuants — SPEC-000 primitive: Entity
    ORGAN = "organ"       # ONT-004: Entity > Continuant > IndependentContinuant
    REPO = "repo"         # ONT-005: Entity > Continuant > IndependentContinuant
    MODULE = "module"     # ONT-006: Entity > Continuant > IndependentContinuant

    # Generically Dependent Continuant — SPEC-000 primitive: Entity
    DOCUMENT = "document"  # ONT-011: Entity > Continuant > GenericallyDependentContinuant

    # Occurrent Process — SPEC-000 primitive: Event
    SESSION = "session"   # ONT-015: Entity > Occurrent > Process

    # Specifically Dependent Continuants — SPEC-000 primitive: Value
    VARIABLE = "variable"  # ONT-008: Entity > Continuant > SpecificallyDependentContinuant
    METRIC = "metric"      # ONT-009: Entity > Continuant > SpecificallyDependentContinuant


class LifecycleStatus(str, Enum):
    """Entity lifecycle states."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    MERGED = "merged"
    SPLIT = "split"
    ARCHIVED = "archived"


# Type prefix for UID generation
_TYPE_PREFIXES: dict[EntityType, str] = {
    EntityType.ORGAN: "organ",
    EntityType.REPO: "repo",
    EntityType.MODULE: "mod",
    EntityType.DOCUMENT: "doc",
    EntityType.SESSION: "sess",
    EntityType.VARIABLE: "var",
    EntityType.METRIC: "met",
}


def generate_entity_uid(
    entity_type: EntityType,
    timestamp_ms: int | None = None,
) -> str:
    """Generate a prefixed ULID for an entity.

    Format: ent_{type_prefix}_{ulid}
    Example: ent_repo_01JARQ5XB3ABCDEFGHJKMNPQRS

    Args:
        entity_type: The type of entity being created.
        timestamp_ms: Optional explicit timestamp for deterministic generation.

    Returns:
        Prefixed ULID string.
    """
    prefix = _TYPE_PREFIXES[entity_type]
    ulid = generate_ulid(timestamp_ms=timestamp_ms)
    return f"ent_{prefix}_{ulid}"


@dataclass
class EntityIdentity:
    """Immutable identity record for a system entity.

    The uid is permanent — all other fields describe the entity's nature
    and lifecycle but the uid itself never changes.
    """

    uid: str
    entity_type: EntityType
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    created_by: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "uid": self.uid,
            "entity_type": self.entity_type.value,
            "lifecycle_status": self.lifecycle_status.value,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityIdentity:
        """Deserialize from a dict."""
        return cls(
            uid=data["uid"],
            entity_type=EntityType(data["entity_type"]),
            lifecycle_status=LifecycleStatus(data.get("lifecycle_status", "active")),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", "system"),
            metadata=data.get("metadata", {}),
        )


def create_entity(
    entity_type: EntityType,
    created_by: str = "system",
    metadata: dict[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> EntityIdentity:
    """Create a new entity with a fresh UID.

    Args:
        entity_type: What kind of entity this is.
        created_by: Who or what created this entity.
        metadata: Optional metadata to attach.
        timestamp_ms: Optional timestamp for deterministic UID generation.

    Returns:
        A new EntityIdentity with a unique UID.
    """
    uid = generate_entity_uid(entity_type, timestamp_ms=timestamp_ms)
    return EntityIdentity(
        uid=uid,
        entity_type=entity_type,
        created_by=created_by,
        metadata=metadata or {},
    )
