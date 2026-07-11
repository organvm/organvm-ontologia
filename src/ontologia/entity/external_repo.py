"""External repository entities for the BIFRONS portal.

A starred repository is represented as a first-class ontologia entity so it can
participate in the temporal graph (relations, lineage, sensing) exactly like an
internal repo. It stays ``entity_type = repo`` — external-ness is metadata, not
a new primitive — so every existing temporal query and traversal applies.

BIFRONS (Janus, two-faced) is the star<->contribution portal; distinct from
IANVA (the MCP doorway).
"""

from __future__ import annotations

from typing import Any

from ontologia.entity.identity import EntityIdentity, EntityType, create_entity

# metadata.ownership discriminates external repos from internal ORGANVM repos.
OWNERSHIP_EXTERNAL = "external"
OWNERSHIP_INTERNAL = "internal"
SOURCE_PLATFORM = "github"


def create_external_repo_entity(
    full_name: str,
    github_node_id: str,
    *,
    url: str = "",
    owner_type: str = "",
    visibility: str = "public",
    first_starred_at: str = "",
    currently_starred: bool = True,
    created_by: str = "bifrons",
    extra: dict[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> EntityIdentity:
    """Create an external-repo entity (``entity_type = repo``, external-owned)."""
    metadata: dict[str, Any] = {
        "ownership": OWNERSHIP_EXTERNAL,
        "source_platform": SOURCE_PLATFORM,
        "full_name": full_name,
        "github_node_id": github_node_id,
        "url": url,
        "owner_type": owner_type,
        "visibility": visibility,
        "first_starred_at": first_starred_at,
        "currently_starred": currently_starred,
    }
    if extra:
        metadata.update(extra)
    return create_entity(
        EntityType.REPO,
        created_by=created_by,
        metadata=metadata,
        timestamp_ms=timestamp_ms,
    )


def is_external_repo(entity: EntityIdentity) -> bool:
    """True if the entity is an external (non-ORGANVM) repository."""
    return (
        entity.entity_type == EntityType.REPO
        and entity.metadata.get("ownership") == OWNERSHIP_EXTERNAL
    )


def external_full_name(entity: EntityIdentity) -> str:
    """The ``owner/name`` of an external repo entity (empty if not one)."""
    if not is_external_repo(entity):
        return ""
    return entity.metadata.get("full_name", "")
