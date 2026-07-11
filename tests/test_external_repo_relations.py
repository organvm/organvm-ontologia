"""BIFRONS external-repo entities + exchange relations."""

from __future__ import annotations

from ontologia.entity.external_repo import (
    OWNERSHIP_EXTERNAL,
    create_external_repo_entity,
    external_full_name,
    is_external_repo,
)
from ontologia.entity.identity import EntityType, create_entity
from ontologia.structure.exchange_relations import (
    ExchangeRelation,
    lens_relation,
    make_exchange_relation,
)


def test_external_repo_entity_is_repo_typed():
    ent = create_external_repo_entity(
        "astral-sh/ruff", "R_kg1", url="https://github.com/astral-sh/ruff",
        owner_type="Organization", first_starred_at="2026-06-01T00:00:00Z",
    )
    # External-ness is metadata, not a new primitive.
    assert ent.entity_type == EntityType.REPO
    assert ent.metadata["ownership"] == OWNERSHIP_EXTERNAL
    assert ent.metadata["full_name"] == "astral-sh/ruff"
    assert ent.metadata["github_node_id"] == "R_kg1"
    assert ent.metadata["currently_starred"] is True
    assert ent.uid.startswith("ent_repo_")


def test_is_external_repo_discriminates():
    external = create_external_repo_entity("a/b", "R_1")
    internal = create_entity(EntityType.REPO, metadata={"ownership": "internal"})
    assert is_external_repo(external) is True
    assert is_external_repo(internal) is False
    assert external_full_name(external) == "a/b"
    assert external_full_name(internal) == ""


def test_make_exchange_relation_carries_metadata():
    edge = make_exchange_relation(
        "ent_repo_X", "ent_repo_Y", ExchangeRelation.RESONATES_WITH,
        exchange_id="ex_42", confidence=0.83, evidence=["shared domain"],
    )
    assert edge.relation_type == "resonates_with"
    assert edge.metadata["exchange_id"] == "ex_42"
    assert edge.metadata["confidence"] == 0.83
    assert edge.metadata["evidence"] == ["shared domain"]
    # Temporal by construction.
    assert edge.valid_from
    assert edge.is_active()


def test_lens_relation_mapping():
    assert lens_relation("technical") == ExchangeRelation.TECHNICALLY_MIRRORS
    assert lens_relation("parallel") == ExchangeRelation.PARALLELS
    assert lens_relation("kinship") == ExchangeRelation.KIN_TO
    # Unknown lens falls back to the generic resonance relation.
    assert lens_relation("mystery") == ExchangeRelation.RESONATES_WITH


def test_relation_vocabulary_is_complete():
    # The portal's full relation loop is present.
    for rel in ("starred", "resonates_with", "absorbed_into", "contributed_to",
                "patch_merged_into", "patch_declined_by", "licensed_under"):
        assert rel in {
            getattr(ExchangeRelation, n)
            for n in dir(ExchangeRelation) if not n.startswith("_")
        }
