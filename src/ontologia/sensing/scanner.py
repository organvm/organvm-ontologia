"""Scanner orchestrator -- run all sensors and emit to bus."""

from __future__ import annotations

from pathlib import Path

from ontologia.sensing.interfaces import NormalizedChange, RawSignal
from ontologia.sensing.normalization import normalize_batch


def _default_sensors(workspace: Path) -> list:
    """Create all available sensors for the given workspace."""
    from ontologia.sensing.ci_sensor import CISensor
    from ontologia.sensing.external_repo_sensor import ExternalRepoSensor
    from ontologia.sensing.filesystem_sensor import FilesystemSensor
    from ontologia.sensing.git_sensor import GitSensor
    from ontologia.sensing.github_star_sensor import GitHubStarSensor
    from ontologia.sensing.registry_sensor import RegistrySensor
    from ontologia.sensing.session_sensor import SessionSensor

    return [
        RegistrySensor(workspace),
        GitSensor(workspace),
        CISensor(workspace),
        SessionSensor(),
        FilesystemSensor(workspace),
        # BIFRONS portal sensors — no-ops (is_available False) until stars sync.
        GitHubStarSensor(),
        ExternalRepoSensor(),
    ]


def scan_all(workspace: Path, sensors: list | None = None) -> list[NormalizedChange]:
    """Run all available sensors and return normalized changes.

    Args:
        workspace: Root workspace directory (e.g. ~/Workspace).
        sensors: Optional explicit list of sensor instances. When None,
            all default sensors are created automatically.

    Returns:
        Normalized changes from every sensor that produced signals.
    """
    if sensors is None:
        sensors = _default_sensors(workspace)

    all_signals: list[RawSignal] = []
    for sensor in sensors:
        if sensor.is_available():
            try:
                signals = sensor.scan()
                all_signals.extend(signals)
            except Exception:
                # Individual sensor failures must not break the whole scan.
                continue

    return normalize_batch(all_signals)


def scan_and_emit(workspace: Path, sensors: list | None = None) -> int:
    """Scan all sensors and emit events to the ontologia bus.

    Returns the number of events emitted.
    """
    changes = scan_all(workspace, sensors)

    count = 0
    try:
        from ontologia.events.bus import emit

        for change in changes:
            emit(
                event_type=f"sensor.{change.change_type.value}",
                source=f"sensor:{change.source_sensor}",
                subject_entity=change.entity_id,
                changed_property=change.property_name,
                previous_value=change.previous_value,
                new_value=change.new_value,
            )
            count += 1
    except ImportError:
        pass

    return count
