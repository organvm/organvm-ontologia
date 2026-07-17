# Governance cadence reconcile owner

Ontologia owns the `reconcile` stage between normalized authority
classification and testament distillation. The stage consumes one frozen
snapshot through the direct, acyclic reconciliation interface. It never reads
an Atlas or final cadence bundle.

Before the first traversal, seed or verify the exact registered-entity
denominator with `ontologia governance seed-state`. The seed-state contract,
external-ID crosswalk, and the prohibition on pre-importing authority nodes are
documented in [`governance-state-seed.md`](governance-state-seed.md).

The ordinary direct CLI remains strict:

```bash
ontologia governance reconcile \
  --lineage lineage-graph.v1.json \
  --snapshot-id "$LIMEN_GOV_SNAPSHOT_ID" \
  --snapshot-digest "$SNAPSHOT_DIGEST" \
  --snapshot-at "$LIMEN_GOV_SNAPSHOT_AT" \
  --governance-testament governance-testament.v1.json \
  --source-census source-census.v1.json \
  --source-envelopes source-envelope.v1.json \
  --normalized-events normalized-events.v1.json \
  --assertion-evidence assertion-evidence.v1.json \
  --normalization-parity normalization-parity-receipt.v1.json \
  --coverage coverage-receipt.v1.json \
  --state-root state \
  --out output
```

Only the explicit cadence owner enables blocked materialization. It accepts
`exact_all` custody and parity receipts with owner-routed blockers,
quarantines, or unverified assertions, preserves that debt on the self-image
set and reconciliation receipt, and keeps `ready: false`. Missing
classification, synthetic evidence, unresolved reviewed lineage, missing
observations, missing ideal predicates, or a non-exact self-image denominator
still fail.

```bash
ontologia-governance-cadence-reconcile \
  --snapshot-digest "$SNAPSHOT_DIGEST" \
  --lineage lineage-graph.v1.json \
  --governance-testament governance-testament.v1.json \
  --source-census source-census.v1.json \
  --source-envelopes source-envelope.v1.jsonl \
  --normalized-events normalized-events.v1.jsonl \
  --assertion-evidence assertion-evidence.v1.json \
  --normalization-parity normalization-parity-receipt.v1.json \
  --coverage coverage-receipt.v1.json \
  --predecessor-receipt 04-classify.governance-stage-receipt.v1.json \
  --state-root state \
  --out reconcile \
  --artifact-out ontologia-governance-reconcile-stage.v1.json
```

Limen supplies `LIMEN_GOV_STAGE=reconcile`, the snapshot identity and time,
traversal, proof flag, predecessor digest, metrics path, stage receipt paths,
and the finite `LIMEN_GOV_MAX_ITEMS` bound. The first traversal runs real
registry reconciliation. An ordinary retry validates the persisted state and
exact output bytes before returning `skipped_completed`.

The proof traversal copies the persisted registry into temporary custody,
reruns the same owner operation there, and requires byte-identical lineage,
self-images, reconciliation receipt, and cadence artifact. It does not write
the governed outputs and emits zero events. The separate read-only predicate
reconstructs the expected documents from persisted owner state and rejects
input, predecessor, state, output, readiness, digest, or artifact tampering:

```bash
LIMEN_GOV_PREDICATE_MODE=1 \
  ontologia-governance-cadence-predicate \
  ...same inputs... \
  --artifact ontologia-governance-reconcile-stage.v1.json
```
