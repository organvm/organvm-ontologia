from pathlib import Path

from ontologia.entity.identity import EntityType
from ontologia.query import search_entities
from ontologia.registry.store import open_store


def test_search_entities_empty_query(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.create_entity(EntityType.ORGAN, "Apple")
    results = search_entities("", store)
    assert results == []


def test_search_entities_sorting(tmp_path: Path) -> None:
    store = open_store(tmp_path)

    e1 = store.create_entity(EntityType.ORGAN, "Apple Corporation")
    store.create_entity(EntityType.ORGAN, "Banana Inc")
    e3 = store.create_entity(EntityType.ORGAN, "Pineapple Ltd")
    e4 = store.create_entity(EntityType.ORGAN, "Apple")

    # Historical name
    store.rename_entity(e1.uid, "Apple Corp")

    # Search for "apple"
    results = search_entities("apple", store)

    assert len(results) == 3
    # 1. Exact match
    assert results[0].uid == e4.uid
    # 2. Prefix match
    assert results[1].uid == e1.uid
    # 3. Substring match
    assert results[2].uid == e3.uid


def test_search_entities_historical_match(tmp_path: Path) -> None:
    store = open_store(tmp_path)

    # Entity gets a completely different name but we should still find it by old name
    e = store.create_entity(EntityType.ORGAN, "OldName")
    store.rename_entity(e.uid, "NewName")

    results = search_entities("oldname", store)
    assert len(results) == 1
    assert results[0].uid == e.uid

    results_new = search_entities("newname", store)
    assert len(results_new) == 1
    assert results_new[0].uid == e.uid


def test_search_entities_no_match(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.create_entity(EntityType.ORGAN, "Apple")

    results = search_entities("orange", store)
    assert len(results) == 0
