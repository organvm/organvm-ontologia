# Governance state seed owner

Ontologia owns the registered-entity denominator used by governance
self-images. Source owners may identify the same organ, repository, document,
session, or artifact with provider-native strings, but those strings are not
Ontologia identities. The bounded `seed-state` command creates the one durable
crosswalk to `ent_*` UIDs before the reconcile cadence imports reviewed
authority nodes.

The seed is private owner configuration. Its schema lives at
`schemas/governance-state-seed.v1.schema.json`; the persisted crosswalk uses
`schemas/governance-entity-crosswalk.v1.schema.json`. Neither artifact contains
source bodies.

## Command

```bash
ontologia governance seed-state \
  --lineage "$CORPVS_LINEAGE" \
  --coverage "$CCE_COVERAGE_RECEIPT" \
  --normalization-parity "$CCE_PARITY_RECEIPT" \
  --seed "$PRIVATE/governance-state-seed.v1.json" \
  --snapshot-id "$LIMEN_GOV_SNAPSHOT_ID" \
  --snapshot-at "$LIMEN_GOV_SNAPSHOT_AT" \
  --state-root "$PRIVATE/ontologia-state" \
  --resolved-lineage-out "$PRIVATE/lineage-graph.resolved.v1.json" \
  --crosswalk-out "$PRIVATE/governance-entity-crosswalk.v1.json" \
  --max-entities 100000
```

The input byte limit defaults to 16 MiB per document. The entity, lineage-node,
and lineage-edge denominators must each fit the finite `--max-entities` bound.
The limit is only a resource guard; the actual denominator is always derived
from the current lineage graph.

## Seed contract

The seed declares exactly one entry for every distinct external
`nodes[].metadata.entity_id` in the lineage graph:

```json
{
  "contract_name": "governance-state-seed.v1",
  "contract_version": 1,
  "snapshot_id": "governance-native-20260716",
  "snapshot_at": "2026-07-16T20:00:00Z",
  "entities": [
    {
      "external_id": "session:chatgpt:6819132d-7848-8003-9d77-37c8f9105280",
      "entity_type": "session",
      "display_name": "ChatGPT session 6819132d",
      "owner_reference": "repo:organvm/session-meta"
    }
  ]
}
```

Missing, extra, duplicate, already-resolved, or unsupported entity declarations
fail before the state root is opened. Provider or source additions therefore
change configuration rather than a hard-coded catalog or count.

The CCE inputs are full final receipts, not header stubs. The command verifies
their canonical digests, schemas' closed field sets, dynamic denominators,
classification counts, residual-owner routing, complete raw-unit promotion
crosswalk, event coverage, readiness debt, common snapshot and generation
timestamp, and pairwise classification counts. Every source envelope used by
the lineage (including edge evidence) must exist in the coverage denominator.

## Exact mutation boundary

On a fresh root the command:

1. creates one active registry entity and primary name per external ID at the
   exact snapshot timestamp;
2. records one `metric:governance-source-evidence-present` observation per
   entity, bound to the deterministic representative node's source envelope;
3. registers that metric and persists the entity state;
4. rewrites only each lineage node's `metadata.entity_id`, plus
   `active`, `ideal_form_id`, and `predicate_receipts` on the earliest
   `(occurred_at, node_id)` representative for each entity;
5. binds every ideal to both final CCE receipts and derives each predicate
   result as `pass` only when its receipt is ready, otherwise `blocked`;
6. emits the resolved lineage and a digested external-ID crosswalk outside the
   exact state root.

It does **not** import authority nodes or edges. Do not run
`ontologia governance import` against the governed state before the cadence.
Traversal one of `ontologia-governance-cadence-reconcile` owns that import;
traversal two must observe zero new nodes or edges.

On replay, the command creates nothing. It requires:

- the same snapshot, seed, original lineage digest, and exact CCE receipt pair;
- the persisted crosswalk and resolved lineage;
- exactly the crosswalk's entity, name, metric, observation, and seed-event
  denominators;
- canonical output serialization and every exact state file's persisted byte
  digest, including creation-event timestamps;
- no authority graph, structural relations, legacy lineage, variables,
  quarantine records, or unowned state files.

Any mismatch fails closed. The generated UID suffixes are intentionally
assigned only on the first run; deterministic reuse comes from preserving the
state root and its crosswalk together in durable private custody.
