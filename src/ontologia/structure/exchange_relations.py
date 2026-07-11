"""BIFRONS exchange relations — the temporal vocabulary of the portal loop.

``RelationEdge.relation_type`` is an arbitrary string, so the portal's relations
live here as a cohesive vocabulary rather than bloating the core ``RelationType``
enum. Every exchange relation carries, in its ``metadata``: the ``exchange_id``
that binds one star traversal end-to-end, a ``confidence``, and ``evidence``.

The loop these relations describe:

    star -> resonance -> internal proposal/adaptation -> upstream patch -> merge

Each edge is temporal (``valid_from`` / optional ``valid_to``) like every other
ontologia edge, so the full history of a star's journey is queryable at any time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ontologia.structure.edges import RelationEdge


class ExchangeRelation:
    """String constants for the BIFRONS portal relation vocabulary."""

    STARRED = "starred"
    UNSTARRED = "unstarred"
    TECHNICALLY_MIRRORS = "technically_mirrors"
    PARALLELS = "parallels"
    KIN_TO = "kin_to"
    RESONATES_WITH = "resonates_with"
    ABSORBED_INTO = "absorbed_into"
    INSPIRED_PROPOSAL = "inspired_proposal"
    ADAPTED_INTO = "adapted_into"
    CONTRIBUTED_TO = "contributed_to"
    PATCH_SUBMITTED_TO = "patch_submitted_to"
    PATCH_MERGED_INTO = "patch_merged_into"
    PATCH_DECLINED_BY = "patch_declined_by"
    SUPERSEDED_BY = "superseded_by"
    LICENSED_UNDER = "licensed_under"


# The three resonance lenses (mirror the engine's mirror lenses).
LENS_RELATIONS = {
    "technical": ExchangeRelation.TECHNICALLY_MIRRORS,
    "parallel": ExchangeRelation.PARALLELS,
    "kinship": ExchangeRelation.KIN_TO,
}

ALL_RELATIONS = frozenset(
    getattr(ExchangeRelation, n)
    for n in dir(ExchangeRelation)
    if not n.startswith("_")
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_exchange_relation(
    source_id: str,
    target_id: str,
    relation: str,
    *,
    exchange_id: str,
    confidence: float = 1.0,
    evidence: list[str] | None = None,
    valid_from: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RelationEdge:
    """Build a temporal RelationEdge tagged with the portal's exchange metadata."""
    metadata: dict[str, Any] = {
        "exchange_id": exchange_id,
        "confidence": confidence,
        "evidence": evidence or [],
    }
    if extra:
        metadata.update(extra)
    return RelationEdge(
        source_id=source_id,
        target_id=target_id,
        relation_type=relation,
        valid_from=valid_from or _now_iso(),
        metadata=metadata,
    )


def lens_relation(lens: str) -> str:
    """Map a resonance lens ('technical'|'parallel'|'kinship') to its relation."""
    return LENS_RELATIONS.get(lens, ExchangeRelation.RESONATES_WITH)
