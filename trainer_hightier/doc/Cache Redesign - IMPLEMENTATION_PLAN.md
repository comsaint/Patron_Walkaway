# trainer_hightier - Cache Redesign Implementation Plan

本文件屬於 **Implementation Plan 層**，承接 `Cache Redesign - SSOT.md`，定義 cache redesign 如何落地到 `trainer_hightier` training pipeline。  
本文件不包含 ticket 級工作拆解；具體任務、owner、執行順序與 DoD 應在後續 working plan 承接。

## 0) Alignment

- SSOT：`trainer_hightier/doc/Cache Redesign - SSOT.md`
- 目標：讓 source 更新、ADT quantile 變化與 feature registry 頻繁調整時，訓練管線只重算必要部分。
- 非目標：不重定義 walkaway label、模型策略、serving runtime 或 row-level historical ADT。

## 1) Target Architecture

### 1.1 Layer Overview

| Layer | Artifact | Grain | Shared / Run-local | Primary Miss Drivers |
|-------|----------|-------|--------------------|----------------------|
| L0 Source Manifest | source file manifest | table + file + partition | shared | file SHA / schema / hash algorithm |
| L1 Cleaned Source | normalized session / bet | table + partition | shared | source partition changed / cleaning semantic version |
| L2 ADT Rank Universe | latest/global ADT rank | canonical + player projection | shared | profile / mapping / slow coverage anchor |
| L3 Selected Entity Set | training entity set | quantile + scope + month | run-scoped or shared by policy hash | quantile / training scope / source partition |
| L4 Labels | walkaway label shard | canonical shard + month | shared by entity/source policy | bet sequence change / mapping / label semantic version |
| L5 Feature Primitive | supplier family + window | supplier + window + month | shared | entity delta / source dependency window / supplier semantic version |
| L6 Assembly | model training parquet | registry + entity set | run-local | registry selection / assembly semantic version |

The redesign moves expensive suppliers to consume a shared entity set and keeps registry churn out of source and primitive caches.

### 1.2 Cache Root Layout

MVP uses sidecar JSON manifests and stable cache roots:

```text
trainer_hightier/artifacts/cache/
  source_manifest_v2/
  cleaned_source_v2/
  universe_v1/
  entity_set_v1/
  labels_v1/
  feature_primitives_v1/
  assembly_v1/
  reports/
```

Run-local outputs continue to live under existing training/model artifact directories. Shared cache paths must not include a full cleaned-artifact token that changes whenever any source file changes.

## 2) Source Manifest Layer

### 2.1 Manifest Contents

Each source parquet file gets a content-addressed record:

```json
{
  "schema_version": 2,
  "hash_algorithm": "sha256_file_bytes_v1",
  "table": "t_bet",
  "relative_path": "t_bet/partition_202606/part_000001.parquet",
  "partition_yyyymm": "202606",
  "size_bytes": 123,
  "num_rows": 456,
  "row_group_count": 3,
  "schema_sha256": "...",
  "file_sha256": "...",
  "mtime_ns_diagnostic": 0
}
```

Correctness uses `file_sha256`, not `mtime_ns_diagnostic`.

### 2.2 Diff Flow

1. Scan current source folder.
2. Build current per-file manifest.
3. Load previous per-file manifest.
4. Diff by table + relative path + file SHA:
   - added
   - removed
   - modified
   - unchanged
5. Map changed files to source partitions.
6. Emit `source_change_set.json` for downstream invalidation.

If a file cannot be mapped to a partition month, fail fast unless explicitly marked as non-training source.

### 2.3 Performance Strategy

MVP computes full file SHA-256. Later optimization may add footer fast fingerprint, but it must not replace correctness unless explicitly approved by SSOT update.

## 3) Cleaned Source Layer

### 3.1 Bet / Session Cleaning Boundary

Cleaning cache keys include:

- source partition manifest fingerprint
- cleaning code semantic fingerprint
- L0 data scope
- registry / contract version needed for L0 cleaning
- output schema fingerprint

Cleaning cache keys exclude:

- ADT quantile
- selected rated universe
- feature registry baseline list
- model run id

### 3.2 Replacing Current Cleaned Bet Segment

The current ADT-segmented cleaned bet cache is replaced by entity set selection.

Target behavior:

- Cleaned bet represents normalized source records after L0 cleaning and data scope.
- Rated / ADT filtering happens in L2/L3 universe and entity layers.
- Downstream Step 3 / Step 3.5 reads entity set rather than relying on cleaned bet already being ADT-filtered.

Migration should remove long-term dependency on `adt_filter_quantile` in bet preprocess cache semantics.

## 4) ADT Universe Layer

### 4.1 ADT Rank Table

Build latest/global rank table at canonical level:

```text
universe_v1/adt_rank_latest/profile=<sha>/mapping=<sha>/slow_anchor=<date>/data.parquet
```

Required fields:

- `canonical_id`
- `player_id`
- `adt`
- `adt_rank`
- `adt_percentile`
- `has_slow_window_coverage`
- `profile_snapshot_sha256`
- `mapping_sha256`
- `slow_anchor_required`

The table may contain multiple `player_id` rows per `canonical_id`, but ranking is canonical-level.

### 4.2 Quantile Selection

Selected universe is derived by filter:

```sql
adt_percentile >= :quantile
AND has_slow_window_coverage
```

Quantile selection writes a small manifest with:

- quantile
- rank table fingerprint
- selected canonical count
- selected player count

Quantile changes do not invalidate L0 source or cleaned source caches.

## 5) Entity Set Layer

### 5.1 Purpose

Entity set becomes the shared input to:

- Feast retrieval / group cache
- short-term PIT materialization
- mid-term daily snapshot
- label join / assembly
- Step 4 split input preparation

### 5.2 Entity Set Contents

At minimum:

- `bet_id`
- `canonical_id`
- `player_id`
- `payout_complete_dtm`
- `prediction_visible_ts_cf`
- `gaming_day_event`
- `source_partition_yyyymm`
- `entity_month_yyyymm`
- `selected_quantile`
- `universe_fingerprint`
- `training_scope_fingerprint`

### 5.3 Delta Fill For Quantile Decrease

When quantile decreases:

1. Diff previous selected universe vs new selected universe.
2. Identify added canonical / player ids.
3. Build added entity rows by month.
4. Compute feature primitives only for added entity rows.
5. Merge base + delta during assembly.

MVP may store delta parquet files and compact periodically:

```text
entity_set_v1/q=0.9500/month=202606/base.parquet
entity_set_v1/q=0.9500/month=202606/delta/run=<run_id>.parquet
```

## 6) Label Layer

### 6.1 Label Dependency

Walkaway labels depend on canonical-level bet ordering. A changed bet can affect:

- itself
- adjacent same-canonical bets
- preceding bets whose horizon contains the changed gap start
- month-boundary labels near changed rows

### 6.2 MVP Invalidation

For month-grain invalidation:

```text
affected_label_months = previous_month(dirty_month)
                      + dirty_month
                      + next_month(dirty_month)
```

Label cache grain should be:

```text
labels_v1/month=YYYYMM/canonical_shard=<N>/data.parquet
```

Canonical sharding keeps delta fill and partial recompute feasible.

### 6.3 Future Precision Upgrade

After MVP, replace month safety window with time-range invalidation:

```text
[dirty_min_payout - ALERT_HORIZON_MIN - WALKAWAY_GAP_MIN buffer,
 dirty_max_payout + WALKAWAY_GAP_MIN buffer]
```

## 7) Feature Primitive Layer

### 7.1 Granularity

Feature primitive cache is grouped by supplier family + window:

- short-term PIT: `short_term:w1h`, `short_term:w6h`, etc.
- mid-term: `mid_term:w30d`
- slow: `slow:monthly_180d`
- Feast group: `feast:<service>/<group_id>`

Exact model baseline feature list is not part of primitive cache identity.

### 7.2 Manifest Contents

Each primitive manifest records:

- supplier family
- window
- supplier semantic version
- entity set fingerprint
- source dependency fingerprints
- source dependency window
- output schema fingerprint
- output row count
- key uniqueness status
- delta/base relationship

### 7.3 Dependency Windows

MVP dependency windows:

| Supplier | Dirty Source Expansion |
|----------|------------------------|
| short-term PIT | dirty month + neighbor month |
| mid-term 30d | dirty month + lookback-overlap months |
| slow 180d | anchors whose 180d source window overlaps dirty month |
| labels | previous/current/next month |
| assembly | entity set + registry only |

Exact expansion helpers should live in one cache dependency module, not be scattered across supplier implementations.

## 8) Assembly Layer

Assembly consumes:

- entity set
- labels
- feature primitives
- feature registry snapshot

Assembly cache key includes:

- registry SHA
- entity set fingerprint
- labels fingerprint
- primitive artifact fingerprints
- assembly semantic version

Registry add/remove should usually miss only this layer unless it introduces or changes a primitive.

## 9) Atomicity And Manifest Policy

All shared cache writers follow:

1. Write data to staging path.
2. Validate row count, schema, uniqueness, and output stat.
3. Atomic rename data artifact.
4. Write manifest to staging path.
5. Atomic rename manifest.
6. Update run-level cache report.

Readers must treat data without a compatible manifest as cache miss or corrupt, depending on layer safety:

- source identity uncertain: fail-fast
- derived artifact missing manifest: recompute
- manifest incompatible: miss and recompute

## 10) Observability

Each run writes:

```text
trainer_hightier/artifacts/cache/reports/cache_report_<run_id>.json
```

Report fields:

- run id
- layer
- artifact kind
- partition / month
- hit state: hit / miss / partial-hit / recompute / fail-fast
- reason code
- changed files
- affected months
- reused row count
- recomputed row count
- elapsed seconds
- output path
- manifest path

Trainer `run_report.json` should include a compact summary:

- source changed files count
- affected source months
- feature primitive hit ratio
- entity delta row count
- total cache reuse seconds saved estimate, if available

## 11) Validation Strategy

### 11.1 Unit-Level

- Source manifest detects unchanged files after folder overwrite.
- Source manifest detects one modified historical file.
- Partition diff maps changed files to expected months.
- Quantile decrease emits added canonical / player / entity delta.
- Feature registry remove causes assembly miss only.
- Feature registry add existing-derived feature causes assembly miss only.
- New primitive causes supplier-family miss.
- Label invalidation includes previous/current/next month.

### 11.2 Integration-Level

- Re-run with identical source bytes but new `mtime`: source cache hit.
- Modify one old parquet file: only affected partitions and windows recompute.
- Quantile increase: no source / primitive full recompute for existing rows.
- Quantile decrease: only added entity rows trigger feature delta fill.
- Add/remove baseline feature: source and primitive caches remain hit when primitive exists.

### 11.3 Safety-Level

- Corrupt manifest fails fast for source identity.
- Interrupted staging write is ignored by readers.
- Manifest schema version bump invalidates only intended layer.

## 12) Rollout Phases

### Phase 1 - Source Identity Foundation

Deliverables:

- content-addressed source manifest v2
- source diff report
- per-partition changed month output
- cache report skeleton

Milestone:

- Folder overwrite with identical bytes does not trigger source-level miss.

### Phase 2 - Cleaned Source And Entity Set Boundary

Deliverables:

- cleaned source cache without ADT quantile
- ADT rank table cache
- selected universe cache
- entity set cache
- direct replacement of cleaned bet segment path for downstream Step 3 / Step 3.5

Milestone:

- Quantile changes do not rerun L0 cleaning.

### Phase 3 - Labels And Feature Primitive Cache

Deliverables:

- label cache with canonical shard + month grain
- supplier family + window primitive cache
- dependency window invalidation helpers
- delta fill path for quantile decrease

Milestone:

- Feature add/remove mostly misses only assembly.

### Phase 4 - Assembly And Observability

Deliverables:

- registry-driven assembly cache
- full `cache_report.json`
- run report summary
- sample validation hooks

Milestone:

- Cache hit/miss reasons are explainable from artifacts without reading logs.

### Phase 5 - Cleanup And Migration Hardening

Deliverables:

- remove old ADT-segmented cleaned bet cache dependency
- retention policy
- cache compaction path
- documentation / runbook update

Milestone:

- Old cache path is no longer required for normal training runs.

## 13) Risks And Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Full SHA is slow on large source folders | Adds minutes before run | Accept for correctness MVP; add footer shortcut later if measured bottleneck |
| Delta fill creates many small files | Slow reads / disk clutter | Periodic compaction and retention policy |
| Mapping changes have broad blast radius | Many downstream misses | Source cleaning remains reusable; universe/entity/features invalidate explicitly |
| Primitive grouping too coarse | More recompute than necessary | Start supplier family + window; split further only with evidence |
| Primitive grouping too fine | Too many manifests / joins | Avoid per-feature cache unless necessary |
| Cache report too verbose | Hard to consume | Store full JSON plus compact run report summary |

## 14) Acceptance Criteria

- Identical content with overwritten source folder yields source hit.
- One historical file modification yields partial invalidation, not full source miss.
- ADT quantile change does not invalidate cleaned source.
- Quantile decrease fills only added entity rows for feature primitives.
- Feature registry add/remove of existing primitives does not rerun suppliers.
- Label invalidation includes canonical sequence boundary risk.
- Cache corrupt / source uncertain paths fail fast.
- All shared cache writes are atomic.
