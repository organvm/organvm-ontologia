"""Unified registry store — manages all persistent state.

Coordinates JSON files (current state) and JSONL files (append-only logs)
in a single directory. All mutations go through the store so that events
are emitted and indexes are kept in sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ontologia.entity.identity import (
    EntityIdentity,
    EntityType,
    LifecycleStatus,
    create_entity,
)
from ontologia.entity.lineage import LineageIndex, LineageRecord, LineageType
from ontologia.entity.naming import NameIndex, NameRecord, add_name
from ontologia.entity.resolver import EntityResolver
from ontologia.events import bus
from ontologia.governance.memory import (
    AuthorityEdge,
    AuthorityGraphIndex,
    AuthorityNode,
    NodeSelfImage,
    QuarantineDiagnostic,
    canonical_json,
    content_digest,
    evidence_refs,
)
from ontologia.metrics.metric import MetricDefinition
from ontologia.metrics.observations import Observation, ObservationStore
from ontologia.structure.edges import EdgeIndex, HierarchyEdge, RelationEdge, _now_iso
from ontologia.variables.resolution import VariableStore
from ontologia.variables.variable import Scope, Variable


def _default_store_dir() -> Path:
    return Path.home() / ".organvm" / "ontologia"


@dataclass
class RegistryStore:
    """Unified store for entities, names, edges, and events.

    File layout in store_dir:
    - entities.json   — current entity state {uid: entity_dict}
    - names.jsonl     — append-only name history
    - edges.jsonl     — append-only edge log (hierarchy + relation)
    - events.jsonl    — append-only event log (managed by events.bus)
    """

    store_dir: Path
    _entities: dict[str, EntityIdentity] = field(default_factory=dict)
    _name_index: NameIndex = field(default_factory=NameIndex)
    _edge_index: EdgeIndex = field(default_factory=EdgeIndex)
    _lineage_index: LineageIndex = field(default_factory=LineageIndex)
    _authority_graph: AuthorityGraphIndex = field(default_factory=AuthorityGraphIndex)
    _quarantine: dict[str, QuarantineDiagnostic] = field(default_factory=dict)
    _variable_store: VariableStore = field(default_factory=VariableStore)
    _observation_store: ObservationStore | None = field(default=None)
    _metrics: dict[str, MetricDefinition] = field(default_factory=dict)
    _dirty: bool = False

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def entities_path(self) -> Path:
        return self.store_dir / "entities.json"

    @property
    def names_path(self) -> Path:
        return self.store_dir / "names.jsonl"

    @property
    def edges_path(self) -> Path:
        return self.store_dir / "edges.jsonl"

    @property
    def events_path(self) -> Path:
        return self.store_dir / "events.jsonl"

    @property
    def lineage_path(self) -> Path:
        return self.store_dir / "lineage.jsonl"

    @property
    def variables_path(self) -> Path:
        return self.store_dir / "variables.json"

    @property
    def observations_path(self) -> Path:
        return self.store_dir / "observations.jsonl"

    @property
    def metrics_path(self) -> Path:
        return self.store_dir / "metrics.json"

    @property
    def authority_nodes_path(self) -> Path:
        return self.store_dir / "governance-nodes.jsonl"

    @property
    def authority_edges_path(self) -> Path:
        return self.store_dir / "governance-edges.jsonl"

    @property
    def quarantine_path(self) -> Path:
        return self.store_dir / "quarantine.jsonl"

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load entities from JSON and names from JSONL."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._load_quarantine()

        # Load entities
        self._entities.clear()
        if self.entities_path.is_file():
            raw = self.entities_path.read_text()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("entities.json must contain an object")
                for uid, edict in data.items():
                    try:
                        self._entities[uid] = EntityIdentity.from_dict(edict)
                    except (KeyError, TypeError, ValueError) as error:
                        self._record_quarantine(
                            f"{self.entities_path.name}:{uid}",
                            json.dumps(edict, sort_keys=True),
                            error,
                        )
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                self._record_quarantine(self.entities_path.name, raw, error)

        # Load names
        self._name_index = NameIndex()
        if self.names_path.is_file():
            for line_number, raw_line in enumerate(
                self.names_path.read_text().splitlines(),
                start=1,
            ):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = NameRecord.from_dict(json.loads(line))
                    self._name_index.add(record)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    self._record_quarantine(
                        self.names_path.name,
                        raw_line,
                        error,
                        line_number,
                    )
                    continue

        # Load edges
        self._edge_index = EdgeIndex()
        if self.edges_path.is_file():
            for line_number, raw_line in enumerate(
                self.edges_path.read_text().splitlines(),
                start=1,
            ):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    edge_type = data.get("edge_type", "")
                    if edge_type == "hierarchy":
                        self._edge_index.add_hierarchy(HierarchyEdge.from_dict(data))
                    elif edge_type == "relation":
                        self._edge_index.add_relation(RelationEdge.from_dict(data))
                    else:
                        raise ValueError(f"unknown edge_type: {edge_type}")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    self._record_quarantine(
                        self.edges_path.name,
                        raw_line,
                        error,
                        line_number,
                    )
                    continue

        # Load lineage
        self._lineage_index = LineageIndex()
        if self.lineage_path.is_file():
            for line_number, raw_line in enumerate(
                self.lineage_path.read_text().splitlines(),
                start=1,
            ):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    self._lineage_index.add(LineageRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    self._record_quarantine(
                        self.lineage_path.name,
                        raw_line,
                        error,
                        line_number,
                    )
                    continue

        # Load the authority-qualified graph after the legacy lineage index.
        # The two formats intentionally coexist: legacy callers keep their
        # four lineage types while governance memory gets richer semantics.
        self._authority_graph = AuthorityGraphIndex()
        self._load_authority_nodes()
        self._load_authority_edges()

        # Load variables
        self._variable_store = VariableStore()
        if self.variables_path.is_file():
            raw = self.variables_path.read_text()
            try:
                data = json.loads(raw)
                self._variable_store = VariableStore.from_list(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self._record_quarantine(self.variables_path.name, raw, error)

        # Load metrics definitions
        self._metrics = {}
        if self.metrics_path.is_file():
            raw = self.metrics_path.read_text()
            try:
                data = json.loads(raw)
                for mid, mdict in data.items():
                    self._metrics[mid] = MetricDefinition.from_dict(mdict)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self._record_quarantine(self.metrics_path.name, raw, error)

        # Load observation store
        self._observation_store = ObservationStore(self.observations_path)
        self._observation_store.load(
            lambda line_number, raw_line, error: self._record_quarantine(
                self.observations_path.name,
                raw_line,
                error,
                line_number,
            ),
        )

        # Point the event bus at our events file
        bus.set_events_path(self.events_path)

        self._dirty = False

    def save(self) -> None:
        """Persist entities, variables, and metrics to JSON.

        Names, edges, lineage, and observations are always appended inline.
        """
        self.store_dir.mkdir(parents=True, exist_ok=True)

        # Write entities
        data = {uid: entity.to_dict() for uid, entity in self._entities.items()}
        self.entities_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
        )

        # Write variables
        var_data = self._variable_store.to_list()
        self.variables_path.write_text(
            json.dumps(var_data, indent=2) + "\n",
        )

        # Write metrics definitions
        met_data = {mid: m.to_dict() for mid, m in self._metrics.items()}
        self.metrics_path.write_text(
            json.dumps(met_data, indent=2, sort_keys=True) + "\n",
        )

        self._dirty = False

    def save_names(self) -> None:
        """Rewrite the full names JSONL from the in-memory index.

        Normally names are appended one-at-a-time via _append_name().
        This is a recovery/migration tool that rebuilds the file.
        """
        self.store_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for entity_id in sorted(self._name_index._by_entity):
            for record in self._name_index._by_entity[entity_id]:
                lines.append(record.to_jsonl())
        self.names_path.write_text("\n".join(lines) + "\n" if lines else "")

    # ------------------------------------------------------------------
    # Entity operations
    # ------------------------------------------------------------------

    def create_entity(
        self,
        entity_type: EntityType,
        display_name: str,
        created_by: str = "system",
        metadata: dict[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> EntityIdentity:
        """Create a new entity with identity and initial name.

        Args:
            entity_type: What kind of entity.
            display_name: Initial display name.
            created_by: Creator identifier.
            metadata: Optional metadata dict.
            timestamp_ms: Optional deterministic timestamp for UID.

        Returns:
            The new EntityIdentity.
        """
        entity = create_entity(
            entity_type=entity_type,
            created_by=created_by,
            metadata=metadata,
            timestamp_ms=timestamp_ms,
        )
        self._entities[entity.uid] = entity
        self._dirty = True

        # Create initial name record
        name_record = add_name(
            self._name_index,
            entity.uid,
            display_name,
            is_primary=True,
            source=created_by,
        )
        self._append_name(name_record)

        # Emit event
        bus.emit(
            bus.ENTITY_CREATED,
            source=created_by,
            subject_entity=entity.uid,
            payload={
                "entity_type": entity_type.value,
                "display_name": display_name,
            },
        )

        return entity

    def get_entity(self, uid: str) -> EntityIdentity | None:
        """Get an entity by UID."""
        return self._entities.get(uid)

    def update_lifecycle(
        self,
        uid: str,
        new_status: LifecycleStatus,
        source: str = "system",
    ) -> bool:
        """Update an entity's lifecycle status.

        Returns True if updated, False if entity not found.
        """
        entity = self._entities.get(uid)
        if not entity:
            return False

        old_status = entity.lifecycle_status
        entity.lifecycle_status = new_status
        self._dirty = True

        bus.emit(
            bus.ENTITY_DEPRECATED if new_status == LifecycleStatus.DEPRECATED
            else bus.ENTITY_ARCHIVED if new_status == LifecycleStatus.ARCHIVED
            else "entity.lifecycle_changed",
            source=source,
            subject_entity=uid,
            changed_property="lifecycle_status",
            previous_value=old_status.value,
            new_value=new_status.value,
        )
        return True

    def rename_entity(
        self,
        uid: str,
        new_name: str,
        source: str = "system",
    ) -> NameRecord | None:
        """Rename an entity — retires old primary name, adds new one.

        Returns the new NameRecord, or None if entity not found.
        """
        entity = self._entities.get(uid)
        if not entity:
            return None

        old_name = self._name_index.current_name(uid)
        old_display = old_name.display_name if old_name else None

        record = add_name(
            self._name_index,
            uid,
            new_name,
            is_primary=True,
            source=source,
        )
        self._append_name(record)

        bus.emit(
            bus.ENTITY_RENAMED,
            source=source,
            subject_entity=uid,
            changed_property="display_name",
            previous_value=old_display,
            new_value=new_name,
        )
        return record

    # ------------------------------------------------------------------
    # Name operations
    # ------------------------------------------------------------------

    def add_alias(
        self,
        uid: str,
        alias_name: str,
        source: str = "system",
    ) -> NameRecord | None:
        """Add a non-primary alias to an entity."""
        if uid not in self._entities:
            return None

        record = add_name(
            self._name_index,
            uid,
            alias_name,
            is_primary=False,
            source=source,
        )
        self._append_name(record)

        bus.emit(
            bus.NAME_ADDED,
            source=source,
            subject_entity=uid,
            new_value=alias_name,
        )
        return record

    def current_name(self, uid: str, at: str | None = None) -> NameRecord | None:
        """Get the current primary name for an entity."""
        return self._name_index.current_name(uid, at=at)

    def name_history(self, uid: str) -> list[NameRecord]:
        """Get full name history for an entity."""
        return self._name_index.all_names(uid)

    # ------------------------------------------------------------------
    # Resolver
    # ------------------------------------------------------------------

    def resolver(self) -> EntityResolver:
        """Build an EntityResolver from current state."""
        return EntityResolver(dict(self._entities), self._name_index)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    def list_entities(
        self,
        entity_type: EntityType | None = None,
        lifecycle_status: LifecycleStatus | None = None,
    ) -> list[EntityIdentity]:
        """List entities with optional filters."""
        results: list[EntityIdentity] = []
        for entity in self._entities.values():
            if entity_type and entity.entity_type != entity_type:
                continue
            if lifecycle_status and entity.lifecycle_status != lifecycle_status:
                continue
            results.append(entity)
        return results

    def events(
        self,
        since: str | None = None,
        event_type: str | None = None,
        subject_entity: str | None = None,
        limit: int = 500,
    ) -> list[bus.OntologiaEvent]:
        """Query the event log."""
        return bus.replay(
            since=since,
            event_type=event_type,
            subject_entity=subject_entity,
            limit=limit,
            path=self.events_path,
            on_error=lambda line_number, raw_line, error: self._record_quarantine(
                self.events_path.name,
                raw_line,
                error,
                line_number,
            ),
        )

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    @property
    def edge_index(self) -> EdgeIndex:
        """The in-memory edge index (hierarchy + relation edges)."""
        return self._edge_index

    def add_hierarchy_edge(
        self,
        parent_id: str,
        child_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> HierarchyEdge:
        """Create and persist a hierarchy edge (parent→child)."""
        edge = HierarchyEdge(
            parent_id=parent_id,
            child_id=child_id,
            valid_from=_now_iso(),
            metadata=metadata or {},
        )
        self._edge_index.add_hierarchy(edge)
        self._append_edge(edge, "hierarchy")
        return edge

    def add_relation_edge(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> RelationEdge:
        """Create and persist a relation edge (source→target)."""
        edge = RelationEdge(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            valid_from=_now_iso(),
            metadata=metadata or {},
        )
        self._edge_index.add_relation(edge)
        self._append_edge(edge, "relation")
        return edge

    def save_edges(self) -> None:
        """Rewrite the full edges JSONL from the in-memory EdgeIndex.

        Recovery/migration tool — analogous to save_names().
        """
        self.store_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for edge in self._edge_index.all_hierarchy_edges():
            d = edge.to_dict()
            d["edge_type"] = "hierarchy"
            lines.append(json.dumps(d, separators=(",", ":")))
        for edge in self._edge_index.all_relation_edges():
            d = edge.to_dict()
            d["edge_type"] = "relation"
            lines.append(json.dumps(d, separators=(",", ":")))
        self.edges_path.write_text("\n".join(lines) + "\n" if lines else "")

    def _append_edge(self, edge: HierarchyEdge | RelationEdge, edge_type: str) -> None:
        """Append a single edge record to the JSONL file."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        d = edge.to_dict()
        d["edge_type"] = edge_type
        with self.edges_path.open("a") as f:
            f.write(json.dumps(d, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------
    # Variable operations
    # ------------------------------------------------------------------

    @property
    def variable_store(self) -> VariableStore:
        return self._variable_store

    def set_variable(self, var: Variable) -> tuple[bool, str]:
        """Set a variable in the store. Returns (success, error_message)."""
        ok, msg = self._variable_store.set(var)
        if ok:
            self._dirty = True
            bus.emit(
                "variable.set",
                source="system",
                subject_entity=var.entity_id or "",
                changed_property=var.key,
                new_value=str(var.value),
                payload={"scope": var.scope.value, "mutability": var.mutability.value},
            )
        return ok, msg

    def resolve_variable(
        self,
        key: str,
        scope: Scope = Scope.GLOBAL,
        entity_chain: list[str | None] | None = None,
        default: Any = None,
    ) -> Any:
        """Resolve a variable through the inheritance chain. Returns the value."""

        result = self._variable_store.resolve(key, scope, entity_chain, default)
        return result.value

    # ------------------------------------------------------------------
    # Lineage operations
    # ------------------------------------------------------------------

    @property
    def lineage_index(self) -> LineageIndex:
        return self._lineage_index

    def add_lineage(
        self,
        entity_id: str,
        related_id: str,
        lineage_type: LineageType,
        metadata: dict[str, Any] | None = None,
    ) -> LineageRecord:
        """Record a lineage relationship and persist to JSONL."""
        record = LineageRecord(
            entity_id=entity_id,
            related_id=related_id,
            lineage_type=lineage_type,
            metadata=metadata or {},
        )
        self._lineage_index.add(record)
        self._append_lineage(record)

        bus.emit(
            "lineage.recorded",
            source="system",
            subject_entity=entity_id,
            payload={
                "related_id": related_id,
                "lineage_type": lineage_type.value,
            },
        )
        return record

    def _append_lineage(self, record: LineageRecord) -> None:
        """Append a single lineage record to JSONL."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self.lineage_path.open("a") as f:
            f.write(json.dumps(record.to_dict(), separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------
    # Authority-qualified governance memory
    # ------------------------------------------------------------------

    @property
    def authority_graph(self) -> AuthorityGraphIndex:
        """The dual-lane, evidence-backed governance memory graph."""
        return self._authority_graph

    def add_authority_node(self, node: AuthorityNode) -> AuthorityNode:
        """Persist an authority node idempotently."""
        if not self._authority_graph.add_node(node):
            return node
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self.authority_nodes_path.open("a") as file:
            file.write(json.dumps(node.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
        bus.emit(
            "governance.node_recorded",
            source="ontologia.governance",
            subject_entity=node.entity_id,
            payload={
                "node_id": node.node_id,
                "lane": node.lane.value,
                "authority_class": node.authority_class.value,
                "body_hash": node.body_hash,
            },
        )
        return node

    def add_authority_edge(self, edge: AuthorityEdge) -> AuthorityEdge:
        """Persist a reviewed authority edge idempotently."""
        if not self._authority_graph.add_edge(edge):
            return edge
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self.authority_edges_path.open("a") as file:
            file.write(json.dumps(edge.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
        bus.emit(
            "governance.edge_recorded",
            source="ontologia.governance",
            payload={
                "edge_id": edge.edge_id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "edge_type": edge.edge_type.value,
                "review_state": edge.review_state.value,
            },
        )
        return edge

    @property
    def quarantine_diagnostics(self) -> list[QuarantineDiagnostic]:
        """Return hashed diagnostics without exposing malformed source bodies."""
        return [self._quarantine[key] for key in sorted(self._quarantine)]

    def node_self_image(
        self,
        entity_id: str,
        *,
        constitutional_digest: str,
        last_reconciled_at: str,
    ) -> NodeSelfImage:
        """Project one deterministic self-image from registry-owned evidence."""
        entity = self._entities.get(entity_id)
        if entity is None:
            raise KeyError(f"unknown entity: {entity_id}")
        if not constitutional_digest or not last_reconciled_at:
            raise ValueError("self-image requires constitutional digest and reconciliation time")

        parent = self._edge_index.parent(entity_id, at=last_reconciled_at)
        incoming: list[dict[str, Any]] = []
        outgoing: list[dict[str, Any]] = []
        if parent is not None:
            incoming.append({"kind": "hierarchy", **parent.to_dict()})
        outgoing.extend(
            {"kind": "hierarchy", **edge.to_dict()}
            for edge in self._edge_index.children(entity_id, at=last_reconciled_at)
        )
        incoming.extend(
            {"kind": "relation", **edge.to_dict()}
            for edge in self._edge_index.incoming_relations(entity_id, at=last_reconciled_at)
        )
        outgoing.extend(
            {"kind": "relation", **edge.to_dict()}
            for edge in self._edge_index.outgoing_relations(entity_id, at=last_reconciled_at)
        )

        linked_nodes = self._authority_graph.nodes_for_entity(entity_id)
        linked_node_ids = {node.node_id for node in linked_nodes}
        for edge in self._authority_graph.edges():
            if edge.target_node_id in linked_node_ids:
                incoming.append({"kind": "governance", **edge.to_dict()})
            if edge.source_node_id in linked_node_ids:
                outgoing.append({"kind": "governance", **edge.to_dict()})

        incoming.sort(key=canonical_json)
        outgoing.sort(key=canonical_json)
        relations = {"incoming": incoming, "outgoing": outgoing}

        latest_node = linked_nodes[-1] if linked_nodes else None
        memory_cursor = {
            "node_count": len(linked_nodes),
            "latest_node_id": latest_node.node_id if latest_node else None,
            "latest_observed_at": latest_node.observed_at if latest_node else None,
        }

        entity_events = self.events(subject_entity=entity_id, limit=100_000)
        latest_event = entity_events[-1] if entity_events else None
        event_cursor = {
            "event_count": len(entity_events),
            "latest_event_type": latest_event.event_type if latest_event else None,
            "latest_timestamp": latest_event.timestamp if latest_event else None,
        }

        latest_observations: dict[str, Observation] = {}
        for observation in self.observation_store.query(entity_id=entity_id):
            current = latest_observations.get(observation.metric_id)
            if current is None or (observation.timestamp, observation.source) > (
                current.timestamp,
                current.source,
            ):
                latest_observations[observation.metric_id] = observation
        observations = [
            latest_observations[metric_id].to_dict()
            for metric_id in sorted(latest_observations)
        ]

        ideals: list[dict[str, Any]] = []
        for node in linked_nodes:
            ideal_form_id = node.metadata.get("ideal_form_id")
            if ideal_form_id is None or node.metadata.get("active", True) is False:
                continue
            ideals.append(
                {
                    "ideal_form_id": ideal_form_id,
                    "source_node_id": node.node_id,
                    "implementation_state": node.metadata.get("implementation_state", "unknown"),
                    "distance_to_ideal": node.metadata.get("distance_to_ideal"),
                    "predicate": node.metadata.get("predicate"),
                    "receipt": node.metadata.get("receipt"),
                },
            )
        ideals.sort(key=canonical_json)

        current_name = self.current_name(entity_id, at=last_reconciled_at)
        state = {
            "lifecycle_status": entity.lifecycle_status.value,
            "display_name": current_name.display_name if current_name else None,
            "metadata": entity.metadata,
        }
        owner = str(entity.metadata.get("owner", entity.created_by))

        return NodeSelfImage(
            entity_id=entity_id,
            owner=owner,
            identity=entity.to_dict(),
            relations=relations,
            memory_cursor=memory_cursor,
            event_cursor=event_cursor,
            observations=observations,
            state=state,
            constitutional_digest=constitutional_digest,
            topology_digest=content_digest(relations),
            active_ideal_forms=ideals,
            last_reconciled_at=last_reconciled_at,
            evidence_refs=evidence_refs(linked_nodes),
        )

    def trace_state_value(self, entity_id: str, field_name: str) -> dict[str, Any]:
        """Trace a current entity state value through events and source evidence."""
        entity = self._entities.get(entity_id)
        if entity is None:
            raise KeyError(f"unknown entity: {entity_id}")
        if field_name == "lifecycle_status":
            value: Any = entity.lifecycle_status.value
        elif field_name == "display_name":
            name = self.current_name(entity_id)
            value = name.display_name if name else None
        elif field_name.startswith("metadata."):
            value = entity.metadata.get(field_name.removeprefix("metadata."))
        else:
            raise ValueError(f"unsupported state field: {field_name}")

        events = [
            event.to_dict()
            for event in self.events(subject_entity=entity_id, limit=100_000)
            if event.changed_property == field_name
            or event.event_type == bus.ENTITY_CREATED
            or (field_name == "display_name" and event.event_type == bus.ENTITY_RENAMED)
        ]
        trace = {
            "entity_id": entity_id,
            "field": field_name,
            "value": value,
            "events": events,
            "evidence_refs": evidence_refs(self._authority_graph.nodes_for_entity(entity_id)),
        }
        return {**trace, "trace_digest": content_digest(trace)}

    # ------------------------------------------------------------------
    # Metric + Observation operations
    # ------------------------------------------------------------------

    @property
    def observation_store(self) -> ObservationStore:
        if self._observation_store is None:
            self._observation_store = ObservationStore(self.observations_path)
            self._observation_store.load()
        return self._observation_store

    def register_metric(self, metric: MetricDefinition) -> None:
        """Register a metric definition."""
        self._metrics[metric.metric_id] = metric
        self._dirty = True

    def get_metric(self, metric_id: str) -> MetricDefinition | None:
        """Look up a metric definition."""
        return self._metrics.get(metric_id)

    def list_metrics(self) -> list[MetricDefinition]:
        """List all registered metric definitions."""
        return list(self._metrics.values())

    def record_observation(
        self,
        metric_id: str,
        entity_id: str,
        value: float,
        source: str = "system",
    ) -> Observation:
        """Record a metric observation (persisted immediately to JSONL)."""
        return self.observation_store.observe(metric_id, entity_id, value, source)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_authority_nodes(self) -> None:
        if not self.authority_nodes_path.is_file():
            return
        for line_number, raw_line in enumerate(
            self.authority_nodes_path.read_text().splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                self._authority_graph.add_node(AuthorityNode.from_dict(json.loads(raw_line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self._record_quarantine(
                    self.authority_nodes_path.name,
                    raw_line,
                    error,
                    line_number,
                )

    def _load_authority_edges(self) -> None:
        if not self.authority_edges_path.is_file():
            return
        for line_number, raw_line in enumerate(
            self.authority_edges_path.read_text().splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                self._authority_graph.add_edge(AuthorityEdge.from_dict(json.loads(raw_line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self._record_quarantine(
                    self.authority_edges_path.name,
                    raw_line,
                    error,
                    line_number,
                )

    def _load_quarantine(self) -> None:
        self._quarantine = {}
        if not self.quarantine_path.is_file():
            return
        for raw_line in self.quarantine_path.read_text().splitlines():
            if not raw_line.strip():
                continue
            try:
                diagnostic = QuarantineDiagnostic.from_dict(json.loads(raw_line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # A corrupt diagnostic cannot safely diagnose itself. Keep
                # loading other records without exposing the raw body.
                continue
            self._quarantine[diagnostic.diagnostic_id] = diagnostic

    def _record_quarantine(
        self,
        source_path: str,
        raw_record: str,
        error: Exception,
        line_number: int | None = None,
    ) -> None:
        diagnostic = QuarantineDiagnostic.from_failure(
            source_path=source_path,
            raw_record=raw_record,
            error=error,
            line_number=line_number,
        )
        if diagnostic.diagnostic_id in self._quarantine:
            return
        self._quarantine[diagnostic.diagnostic_id] = diagnostic
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self.quarantine_path.open("a") as file:
            file.write(
                json.dumps(diagnostic.to_dict(), sort_keys=True, separators=(",", ":")) + "\n",
            )

    def _append_name(self, record: NameRecord) -> None:
        """Append a single name record to the JSONL file."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        with self.names_path.open("a") as f:
            f.write(record.to_jsonl() + "\n")


def open_store(store_dir: Path | None = None) -> RegistryStore:
    """Open (or create) a registry store and load its state.

    Args:
        store_dir: Directory for store files. Defaults to ~/.organvm/ontologia/.

    Returns:
        A loaded RegistryStore ready for use.
    """
    path = store_dir or _default_store_dir()
    store = RegistryStore(store_dir=path)
    store.load()
    return store
