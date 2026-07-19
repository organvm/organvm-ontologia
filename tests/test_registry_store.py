"""Tests for the unified registry store."""

import json
from pathlib import Path

from ontologia.entity.identity import EntityType, LifecycleStatus
from ontologia.events import bus
from ontologia.metrics.metric import AggregationPolicy, MetricDefinition, MetricType
from ontologia.registry.store import RegistryStore, open_store


class TestStoreCreation:
    def test_empty_store(self, store: RegistryStore):
        assert store.entity_count == 0

    def test_create_entity(self, store: RegistryStore):
        entity = store.create_entity(
            EntityType.REPO,
            display_name="organvm-engine",
            created_by="test",
        )
        assert entity.uid.startswith("ent_repo_")
        assert store.entity_count == 1

    def test_create_entity_with_metadata(self, store: RegistryStore):
        entity = store.create_entity(
            EntityType.ORGAN,
            display_name="Meta",
            metadata={"registry_key": "META-ORGANVM"},
        )
        assert entity.metadata["registry_key"] == "META-ORGANVM"

    def test_get_entity(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="test-repo")
        found = store.get_entity(entity.uid)
        assert found is not None
        assert found.uid == entity.uid

    def test_get_entity_not_found(self, store: RegistryStore):
        assert store.get_entity("nonexistent") is None


class TestStoreNaming:
    def test_current_name(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="my-repo")
        name = store.current_name(entity.uid)
        assert name is not None
        assert name.display_name == "my-repo"
        assert name.is_primary

    def test_rename(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="old-name")
        new_record = store.rename_entity(entity.uid, "new-name")
        assert new_record is not None
        assert new_record.display_name == "new-name"
        # Current name should be the new one
        current = store.current_name(entity.uid)
        assert current is not None
        assert current.display_name == "new-name"

    def test_rename_preserves_history(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="alpha")
        store.rename_entity(entity.uid, "beta")
        history = store.name_history(entity.uid)
        assert len(history) == 2
        assert history[0].display_name == "alpha"
        assert history[1].display_name == "beta"

    def test_add_alias(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="main-name")
        alias = store.add_alias(entity.uid, "nickname")
        assert alias is not None
        assert not alias.is_primary
        # Primary should still be main-name
        assert store.current_name(entity.uid).display_name == "main-name"

    def test_rename_nonexistent(self, store: RegistryStore):
        assert store.rename_entity("fake_uid", "name") is None

    def test_alias_nonexistent(self, store: RegistryStore):
        assert store.add_alias("fake_uid", "alias") is None


class TestStoreLifecycle:
    def test_update_lifecycle(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="repo")
        assert store.update_lifecycle(entity.uid, LifecycleStatus.DEPRECATED)
        found = store.get_entity(entity.uid)
        assert found.lifecycle_status == LifecycleStatus.DEPRECATED

    def test_update_lifecycle_nonexistent(self, store: RegistryStore):
        assert not store.update_lifecycle("fake", LifecycleStatus.ARCHIVED)


class TestStorePersistence:
    def test_save_and_reload(self, store_dir: Path):
        store1 = RegistryStore(store_dir=store_dir)
        bus.set_events_path(store1.events_path)
        store1.load()
        e1 = store1.create_entity(EntityType.REPO, display_name="persisted-repo")
        store1.save()

        # Reload from disk
        store2 = RegistryStore(store_dir=store_dir)
        bus.set_events_path(store2.events_path)
        store2.load()
        assert store2.entity_count == 1
        found = store2.get_entity(e1.uid)
        assert found is not None
        assert found.entity_type == EntityType.REPO

    def test_names_persisted_inline(self, store_dir: Path):
        store1 = RegistryStore(store_dir=store_dir)
        bus.set_events_path(store1.events_path)
        store1.load()
        e = store1.create_entity(EntityType.REPO, display_name="repo-a")
        store1.rename_entity(e.uid, "repo-b")

        # Names should be in JSONL already (appended inline)
        assert store1.names_path.is_file()
        lines = [l for l in store1.names_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2  # initial name + rename

        # Reload and verify
        store2 = RegistryStore(store_dir=store_dir)
        bus.set_events_path(store2.events_path)
        store2.load()
        history = store2.name_history(e.uid)
        assert len(history) == 2

    def test_events_persisted(self, store_dir: Path):
        store = RegistryStore(store_dir=store_dir)
        bus.set_events_path(store.events_path)
        store.load()
        store.create_entity(EntityType.REPO, display_name="repo")
        events = store.events()
        assert len(events) >= 1
        assert events[0].event_type == bus.ENTITY_CREATED


class TestStoreQuery:
    def test_list_entities_by_type(self, store: RegistryStore):
        store.create_entity(EntityType.ORGAN, display_name="Meta")
        store.create_entity(EntityType.REPO, display_name="engine")
        store.create_entity(EntityType.REPO, display_name="dashboard")

        organs = store.list_entities(entity_type=EntityType.ORGAN)
        assert len(organs) == 1
        repos = store.list_entities(entity_type=EntityType.REPO)
        assert len(repos) == 2

    def test_list_entities_by_lifecycle(self, store: RegistryStore):
        e = store.create_entity(EntityType.REPO, display_name="old")
        store.update_lifecycle(e.uid, LifecycleStatus.ARCHIVED)
        store.create_entity(EntityType.REPO, display_name="new")

        active = store.list_entities(lifecycle_status=LifecycleStatus.ACTIVE)
        assert len(active) == 1
        archived = store.list_entities(lifecycle_status=LifecycleStatus.ARCHIVED)
        assert len(archived) == 1


class TestStoreResolver:
    def test_resolver_resolves_by_name(self, store: RegistryStore):
        store.create_entity(EntityType.REPO, display_name="organvm-engine")
        resolver = store.resolver()
        result = resolver.resolve("organvm-engine")
        assert result is not None
        assert result.matched_by == "primary_name"

    def test_resolver_resolves_by_uid(self, store: RegistryStore):
        entity = store.create_entity(EntityType.REPO, display_name="engine")
        resolver = store.resolver()
        result = resolver.resolve(entity.uid)
        assert result is not None
        assert result.matched_by == "uid"


class TestOpenStore:
    def test_open_store(self, tmp_path: Path):
        store = open_store(store_dir=tmp_path / "test-store")
        assert store.entity_count == 0
        store.create_entity(EntityType.REPO, display_name="test")
        assert store.entity_count == 1


def test_open_store_uses_default_directory(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setattr("ontologia.registry.store.Path.home", lambda: home)

    store = open_store()
    assert store.store_dir == home / ".organvm" / "ontologia"

    store.create_entity(EntityType.REPO, display_name="default-root")
    store.save()
    assert store.entities_path.is_file()


def test_save_names_rebuilds_name_history(store: RegistryStore):
    first = store.create_entity(EntityType.REPO, display_name="repo-alpha")
    second = store.create_entity(EntityType.REPO, display_name="repo-beta")
    store.rename_entity(first.uid, "repo-alpha-renamed")
    store.add_alias(first.uid, "repo-alpha-aka")

    store.save_names()
    lines = [line for line in store.names_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 4  # repo-alpha, rename, alias, repo-beta

    parsed = [json.loads(line) for line in lines]
    parsed_ids = {record["entity_id"] for record in parsed}
    assert parsed_ids == {first.uid, second.uid}

    reopened = RegistryStore(store_dir=store.store_dir)
    reopened.load()
    assert len(reopened.name_history(first.uid)) == 3
    assert reopened.current_name(first.uid).display_name == "repo-alpha-renamed"
    assert len(reopened.name_history(second.uid)) == 1


def test_list_metrics(store: RegistryStore):
    store.register_metric(
        MetricDefinition(
            metric_id="met_test_count",
            name="Test Count",
            metric_type=MetricType.GAUGE,
            aggregation=AggregationPolicy.SUM,
        ),
    )
    store.register_metric(
        MetricDefinition(
            metric_id="met_repo_size",
            name="Repo Size",
            metric_type=MetricType.COUNTER,
            aggregation=AggregationPolicy.MAX,
        ),
    )

    metric_ids = {metric.metric_id for metric in store.list_metrics()}
    assert metric_ids == {"met_test_count", "met_repo_size"}

    store.save()
    reopened = RegistryStore(store_dir=store.store_dir)
    reopened.load()
    reopened_ids = {metric.metric_id for metric in reopened.list_metrics()}
    assert reopened_ids == metric_ids


def test_load_ignores_corrupt_persistence_records(store_dir: Path):
    # Files contain a mix of valid and malformed rows.
    (store_dir / "entities.json").write_text("{}")
    (store_dir / "names.jsonl").write_text(
        json.dumps(
            {
                "entity_id": "ent_repo_alpha",
                "display_name": "Repo Alpha",
                "slug": "repo-alpha",
                "valid_from": "2026-01-01T00:00:00+00:00",
                "is_primary": True,
            },
        )
        + "\n"
        + "{invalid-name-record}\n",
    )
    (store_dir / "edges.jsonl").write_text(
        json.dumps(
            {
                "edge_type": "relation",
                "source_id": "ent_src",
                "target_id": "ent_tgt",
                "relation_type": "depends_on",
                "valid_from": "2026-01-01T00:00:00+00:00",
            },
        )
        + "\n"
        + "{invalid-edge-record}\n",
    )
    (store_dir / "lineage.jsonl").write_text(
        json.dumps(
            {
                "entity_id": "ent_repo_child",
                "related_id": "ent_repo_parent",
                "lineage_type": "derived_from",
                "recorded_at": "2026-01-01T00:00:00+00:00",
            },
        )
        + "\n"
        + "{invalid-lineage-record}\n",
    )
    (store_dir / "variables.json").write_text('{"vars": [')
    (store_dir / "metrics.json").write_text('{"metrics": [')
    (store_dir / "observations.jsonl").write_text(
        "not-json-line\n"
        + json.dumps(
            {
                "metric_id": "met_telemetry",
                "entity_id": "ent_repo_alpha",
                "value": 12.5,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "source": "system",
            },
        ),
    )

    store = RegistryStore(store_dir=store_dir)
    store.load()

    assert store.current_name("ent_repo_alpha") is not None
    assert len(store.edge_index.all_relation_edges()) == 1
    predecessors = store.lineage_index.predecessors("ent_repo_child")
    assert len(predecessors) == 1
    assert predecessors[0].related_id == "ent_repo_parent"
    assert store.observation_store.latest("met_telemetry", "ent_repo_alpha") is not None
    assert store.get_metric("met_telemetry") is None
