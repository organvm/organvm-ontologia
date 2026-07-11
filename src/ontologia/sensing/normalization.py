"""Normalization — convert raw sensor signals into normalized changes."""

from __future__ import annotations

from ontologia.sensing.interfaces import ChangeType, NormalizedChange, RawSignal

# Mapping from signal types to change types
_SIGNAL_TYPE_MAP: dict[str, ChangeType] = {
    "file_modified": ChangeType.STATE,
    "file_created": ChangeType.STATE,
    "file_deleted": ChangeType.STATE,
    "git_commit": ChangeType.STATE,
    "git_branch_created": ChangeType.HIERARCHY,
    "git_branch_deleted": ChangeType.HIERARCHY,
    "registry_updated": ChangeType.STATE,
    "edge_added": ChangeType.RELATION,
    "edge_removed": ChangeType.RELATION,
    "promotion_changed": ChangeType.STATE,
    "content_drift": ChangeType.SEMANTIC,
    "metric_spike": ChangeType.ANOMALY,
    # BIFRONS portal sensors (star intake + upstream drift)
    "github_star": ChangeType.RELATION,
    "github_unstar": ChangeType.RELATION,
    "external_repo_changed": ChangeType.STATE,
}


def normalize_signal(signal: RawSignal) -> NormalizedChange | None:
    """Convert a raw sensor signal into a normalized change.

    Returns None if the signal type is unrecognized or the signal
    lacks required fields.
    """
    if not signal.entity_id:
        return None

    change_type = _SIGNAL_TYPE_MAP.get(signal.signal_type)
    if change_type is None:
        return None

    return NormalizedChange(
        change_type=change_type,
        entity_id=signal.entity_id,
        property_name=signal.signal_type,
        new_value=signal.details.get("value"),
        previous_value=signal.details.get("previous_value"),
        confidence=signal.confidence,
        source_sensor=signal.sensor_name,
        timestamp=signal.timestamp,
    )


def normalize_batch(signals: list[RawSignal]) -> list[NormalizedChange]:
    """Normalize a batch of raw signals, filtering out unrecognized ones."""
    results: list[NormalizedChange] = []
    for signal in signals:
        normalized = normalize_signal(signal)
        if normalized is not None:
            results.append(normalized)
    return results
