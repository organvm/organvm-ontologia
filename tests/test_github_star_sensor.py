"""BIFRONS sensors — star/unstar + upstream drift detection and normalization."""

from __future__ import annotations

import sqlite3

from ontologia.sensing.external_repo_sensor import ExternalRepoSensor
from ontologia.sensing.github_star_sensor import GitHubStarSensor
from ontologia.sensing.interfaces import ChangeType, Sensor
from ontologia.sensing.normalization import normalize_batch


def _build_portal(path, *, star_events=(), snapshots=()):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE star_event (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "node_id TEXT, full_name TEXT, event TEXT, at TEXT, exchange_id TEXT)",
    )
    conn.execute(
        "CREATE TABLE repo_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "node_id TEXT, full_name TEXT, ref TEXT, snapshot_at TEXT, pushed_at TEXT)",
    )
    for ev in star_events:
        conn.execute(
            "INSERT INTO star_event(node_id, full_name, event, at, exchange_id) "
            "VALUES(?,?,?,?,?)", ev,
        )
    for sn in snapshots:
        conn.execute(
            "INSERT INTO repo_snapshot(node_id, full_name, ref, snapshot_at, pushed_at) "
            "VALUES(?,?,?,?,?)", sn,
        )
    conn.commit()
    conn.close()
    return path


def test_star_sensor_conforms_to_protocol(tmp_path):
    db = _build_portal(tmp_path / "p.db")
    sensor = GitHubStarSensor(db)
    assert isinstance(sensor, Sensor)  # runtime_checkable Protocol
    assert sensor.name == "github_star"


def test_star_sensor_unavailable_without_db(tmp_path):
    sensor = GitHubStarSensor(tmp_path / "missing.db")
    assert sensor.is_available() is False
    assert sensor.scan() == []


def test_star_sensor_emits_star_and_unstar(tmp_path):
    db = _build_portal(tmp_path / "p.db", star_events=[
        ("R_1", "a/one", "star", "2026-07-01T00:00:00Z", "ex_1"),
        ("R_2", "b/two", "star", "2026-07-02T00:00:00Z", "ex_2"),
        ("R_2", "b/two", "unstar", "2026-07-03T00:00:00Z", ""),
    ])
    sensor = GitHubStarSensor(db)
    assert sensor.is_available()
    signals = sensor.scan()
    assert len(signals) == 3
    types = [s.signal_type for s in signals]
    assert types == ["github_star", "github_star", "github_unstar"]
    assert signals[0].entity_id == "R_1"
    assert signals[0].details["exchange_id"] == "ex_1"


def test_star_signals_normalize_to_relation_changes(tmp_path):
    db = _build_portal(tmp_path / "p.db", star_events=[
        ("R_1", "a/one", "star", "2026-07-01T00:00:00Z", "ex_1"),
        ("R_2", "b/two", "unstar", "2026-07-03T00:00:00Z", ""),
    ])
    changes = normalize_batch(GitHubStarSensor(db).scan())
    assert len(changes) == 2
    assert all(c.change_type == ChangeType.RELATION for c in changes)
    assert changes[0].entity_id == "R_1"


def test_star_sensor_since_id_is_incremental(tmp_path):
    db = _build_portal(tmp_path / "p.db", star_events=[
        ("R_1", "a/one", "star", "t1", "ex_1"),
        ("R_2", "b/two", "star", "t2", "ex_2"),
        ("R_3", "c/three", "star", "t3", "ex_3"),
    ])
    later = GitHubStarSensor(db, since_id=2).scan()
    assert [s.entity_id for s in later] == ["R_3"]


def test_external_repo_sensor_detects_push_drift(tmp_path):
    db = _build_portal(tmp_path / "p.db", snapshots=[
        ("R_1", "a/one", "ref1", "2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z"),
        ("R_1", "a/one", "ref2", "2026-07-05T00:00:00Z", "2026-07-04T00:00:00Z"),
        ("R_2", "b/two", "refx", "2026-07-01T00:00:00Z", "2026-06-01T00:00:00Z"),
    ])
    sensor = ExternalRepoSensor(db)
    assert sensor.is_available()
    signals = sensor.scan()
    # Only R_1 advanced its pushed_at between snapshots.
    assert len(signals) == 1
    assert signals[0].entity_id == "R_1"
    assert signals[0].signal_type == "external_repo_changed"
    assert signals[0].details["previous_value"] == "2026-06-01T00:00:00Z"
