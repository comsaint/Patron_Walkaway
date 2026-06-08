# trainer_hightier - Cache Redesign Working Plan (Phase 3)

本文件屬於 **Working / execution plan** 層，承接：

- [Cache Redesign - SSOT.md](./Cache%20Redesign%20-%20SSOT.md)
- [Cache Redesign - IMPLEMENTATION_PLAN.md](./Cache%20Redesign%20-%20IMPLEMENTATION_PLAN.md)
- [Cache Redesign - WORKING_PLAN Phase 2.md](./Cache%20Redesign%20-%20WORKING_PLAN%20Phase%202.md)（Phase 2 已完成）

本文件只拆 **Phase 3：Labels And Feature Primitive Cache**。Phase 4+ 僅保留摘要與 non-goals。

## 0) Phase 3 Objective

在 Phase 2 entity set 穩定後：

1. **Labels cache**（L4）：canonical shard + month grain；dirty month 用 **prev/current/next** 安全窗失效（SSOT §4.4）。
2. **Feature primitive cache**（L5）：按 **supplier family + window** 分組；registry exact feature list 不進 primitive key。
3. **Dependency window helpers**：集中展開 source dirty month → labels / short PIT / mid / slow 受影響月份。
4. **Quantile delta fill**：quantile 降低時只補新增 entity rows 的 primitive（不重算既有 rows）。

**Milestone（Implementation Plan §12）：** feature registry add/remove 多數只 miss assembly（Phase 4），不重跑 supplier。

## 1) Decisions Locked (Phase 3)

| ID | 決策 | 備註 |
|----|------|------|
| P3-D-001 | Label MVP invalidation = `prev(dirty) + dirty + next(dirty)`。 | 對齊 SSOT；精準 time-range 留 Phase 3+ 升級項。 |
| P3-D-002 | Label cache grain = `month × canonical_shard`。 | `labels_v1/month=YYYYMM/canonical_shard=<N>/data.parquet` |
| P3-D-003 | Short PIT primitive identity = `short_term:<window>` + entity set fp + source deps。 | 移除/替換 `columns_fingerprint` 作為 primary miss 驅動（改 schema fp 僅驗證）。 |
| P3-D-004 | Short PIT source invalidation 改用 `l1_recompute_months`（source manifest v2），退役 `partition_inventory_fingerprint`。 | 與 Phase 2 收斂。 |
| P3-D-005 | Primitive manifest 必含 `entity_set_fingerprint` + `source_dependency_window`。 | Universe / quantile 變透過 entity set fp 傳遞。 |
| P3-D-006 | Quantile 降低：diff selected universe → `added_entity_rows` → delta primitive parquet。 | Base primitive 保留；assembly 合併 base+delta（Phase 4）。 |
| P3-D-007 | Phase 3 不改 walkaway label **business definition**。 | 只加 cache 邊界與 manifest。 |
| P3-D-008 | Dependency expansion 集中在 `cache_invalidation_v1`（擴充），不散落各 supplier。 | Implementation Plan §7.3 |

## 2) Work Breakdown

| ID | Task | Primary files | DoD |
|----|------|---------------|-----|
| P3-WP-1 | Dependency window helpers | 擴充 `cache_invalidation_v1.py` | `label_invalid_months(dirty)`、`short_pit_invalid_months(dirty, neighbor=1)`、`mid_term_invalid_months(dirty, lookback=32)`；單元測試 |
| P3-WP-2 | Labels cache v1 | 新模組 `trainer_hightier/utils/labels_cache_v1.py`；擴充 `walkaway_labels.py` | 按月+shard materialize；manifest 含 entity set fp、label semantic version、`WALKAWAY_GAP_MIN`/`ALERT_HORIZON_MIN`；atomic write |
| P3-WP-3 | Trainer labels integration | `trainer.py` | `materialize_walkaway_labels` 路徑改呼叫 labels cache；metrics `labels_cache_hit` |
| P3-WP-4 | Short PIT primitive manifest v2 | `feature_experiment/short_term_pit_cache.py` | Key 用 `supplier_family=short_term:w*`、`entity_set_fingerprint`、`source_month_fps`；`columns_fingerprint` 降為驗證欄位 |
| P3-WP-5 | Short PIT invalidation 收斂 | `short_term_pit_cache.py`, `trainer.py` | `recompute_months` 來自 P3-WP-1；移除 partition inventory fp 依賴 |
| P3-WP-6 | Entity set quantile delta | `entity_set_v1.py`, `universe_cache_v1.py` | `diff_selected_universe_added_players`；寫 `entity_set_v1/.../delta/`；metrics `entity_delta_row_count` |
| P3-WP-7 | Primitive delta fill (short PIT MVP) | `short_term_pit_cache.py` | quantile 降低時只 materialize added-entity 對應 shard rows；合併進 shard wide schema |
| P3-WP-8 | cache_report 擴充 L4/L5 | `source_manifest_v2.py` 或專用 builder | 每層 hit/miss/reason/elapsed；primitive hit ratio |
| P3-WP-9 | Unit + integration tests | `test_labels_cache_v1.py`, `test_cache_invalidation_v1.py`, 擴充 short PIT tests | P3-T-1～P3-T-10（見 §5） |
| P3-WP-10 | RUNBOOK §5.7 | `RUNBOOK.md` | labels / primitive / delta fill 路徑與失效語意 |

### P3-WP-2 Detail: Label manifest (excerpt)

```json
{
  "schema_version": 1,
  "kind": "walkaway_labels_v1",
  "month": "202503",
  "canonical_shard": 7,
  "entity_set_fingerprint": "...",
  "label_semantic_fingerprint": "...",
  "walkaway_gap_min": 30,
  "alert_horizon_min": 15,
  "row_count": 12345,
  "data_path": "artifacts/cache/labels_v1/month=202503/canonical_shard=7/data.parquet"
}
```

### P3-WP-4 Detail: Short PIT shard manifest v2 (excerpt)

```json
{
  "schema_version": 2,
  "supplier_family": "short_term:w1h",
  "yyyymm": "202503",
  "entity_set_fingerprint": "...",
  "source_invalid_months": ["202502", "202503"],
  "code_fingerprint": "...",
  "output_schema_fingerprint": "...",
  "training_universe_num_rows": 456
}
```

### P3-WP-6 Detail: Metrics keys

- `labels_cache_hit` / `labels_cache_elapsed_seconds`
- `labels_invalid_months`
- `short_term_pit_primitive_hit_ratio`
- `short_term_pit_recompute_months`
- `entity_delta_row_count`
- `entity_delta_fill_elapsed_seconds`

## 3) Artifact Paths

```text
trainer_hightier/artifacts/cache/
  labels_v1/
    month=YYYYMM/canonical_shard=<N>/
      data.parquet
      manifest.json
  feature_primitives_v1/
    short_term/
      window=w1h/entity_set=<fp>/yyyymm=YYYYMM/
        data.parquet
        manifest.json
  entity_set_v1/
    .../delta/run=<run_id>/yyyymm=YYYYMM.parquet   # quantile 降低
  reports/
    cache_report_<run_id>.json                     # 擴充 L4/L5
```

既有路徑過渡：

- `artifacts/labels/walkaway_labels.parquet` → Phase 3 仍產出（assembly 輸入）；來源改 labels cache 合併
- `artifacts/training_data/cache/short_term_pit_v1/` → manifest schema v2；舊 shard 自然 miss

## 4) Flow (Phase 3)

```mermaid
flowchart TD
  dirty[l1_recompute_months] --> expand[ExpandDependencyWindows]
  expand --> lbl[LabelsCachePerMonthShard]
  expand --> pit[ShortPITPrimitiveCache]
  entity[EntitySetV1] --> lbl
  entity --> pit
  qdown[QuantileDecrease] --> delta[EntityDeltaRows]
  delta --> fill[PrimitiveDeltaFill]
  pit --> report[CacheReportL4L5]
  lbl --> report
```

## 5) Validation Plan

### Unit tests (P3-WP-9)

| Case | Setup | Expected |
|------|-------|----------|
| P3-T-1 | dirty month `202503` | `label_invalid_months` = `{202502,202503,202504}` |
| P3-T-2 | dirty month at year boundary `202501` | prev = `202412` |
| P3-T-3 | short PIT neighbor expand | dirty `202503` → invalid `{202502,202503,202504}` |
| P3-T-4 | labels cache hit（entity + semantic 不變） | `labels_cache_hit=True` |
| P3-T-5 | entity set fp 變 | labels miss；source 不變時 L1 仍 hit |
| P3-T-6 | registry 移除 feature | short PIT shard **仍 hit**；assembly miss（Phase 4 測） |
| P3-T-7 | registry 新增既有 primitive 可衍生 feature | short PIT **仍 hit** |
| P3-T-8 | quantile 0.99→0.95 | entity delta > 0；short PIT delta fill 只算新 rows |
| P3-T-9 | quantile 0.95→0.99 | 無 delta fill；primitive 全 hit |
| P3-T-10 | single historical file modify | 僅 P3-WP-1 展開月份重算 labels + short PIT |

### Integration checks (smoke)

- [ ] `--skip-training-dataset` smoke 後 `labels_cache_*` / `entity_set_*` metrics 存在
- [ ] 完整 Step 3 run：short PIT `cache_hit_ratio` 上升（相對 Phase 2 前）
- [ ] quantile-only 變更：bet base hit + labels miss + short PIT partial delta

## 6) Definition of Done (Phase 3)

Phase 3 完成當且僅當：

1. P3-T-1～P3-T-10 測試全綠。
2. Labels 按月+shard cache 命中；dirty month ± neighbor 失效正確。
3. Short PIT 以 entity set fp + supplier family 為 miss 主因；`columns_fingerprint` 不再主導全 shard miss。
4. Quantile 降低觸發 delta fill；quantile 上升不重算既有 primitive rows。
5. `partition_inventory_fingerprint` 自 short PIT 路徑移除。
6. RUNBOOK §5.7 已更新。

## 7) Explicit Non-Goals (Phase 3)

本輪 **不做**：

- Assembly cache（Phase 4）。
- Full cache_report SQLite catalog。
- Label time-range 精準 invalidation（僅 MVP month 窗）。
- Mid-term / slow primitive cache 重構（可只加 P3-WP-1 展開 helper + stub manifest）。
- Serving runtime label/primitive 路徑變更。

## 8) Risks And Mitigations

| Risk | Mitigation |
|------|------------|
| Label shard 檔案過碎 | 固定 `canonical_shard` 桶數（如 32）；定期 compact 留 Phase 5 |
| Short PIT manifest v2 全 miss | 一次性 rebuild；文件註明升級 |
| Delta fill 與 wide shard schema 不一致 | merge 前 schema fp 驗證；fail-fast |
| `columns_fingerprint` 移除導致 silent stale | 保留為 output_schema 驗證欄位 |

## 9) Suggested PR Sequence

1. **PR-1**: P3-WP-1 + tests（dependency windows）
2. **PR-2**: P3-WP-2 + P3-WP-3 + tests（labels cache）
3. **PR-3**: P3-WP-4 + P3-WP-5 + tests（short PIT manifest v2 + invalidation）
4. **PR-4**: P3-WP-6 + P3-WP-7 + tests（quantile delta fill）
5. **PR-5**: P3-WP-8 + P3-WP-10 + smoke 文件

## 10) Prerequisites (Phase 2 → Phase 3)

- [x] Entity set v1 預設路徑
- [x] `l1_recompute_months` 由 source manifest v2 驅動
- [x] ADT rank / selected universe cache
- [ ] Phase 2 smoke run 通過（見 §11）

## 11) Phase 2 Smoke Checklist

- [ ] `entity_set_cache_hit` / `universe_adt_rank_cache_hit` 出現在 log 或 run_report
- [ ] `cleaned__gmwds_t_bet.cache.json` 不存在（segment sidecar 已退役）
- [ ] `bet_segment_legacy_fallback_used=false`（預設路徑）
- [ ] `l1_recompute_months` 與 `source_manifest_v2.changed_partitions` 一致（source 不變時為空）

## 12) Implementation Status

| ID | 狀態 |
|----|------|
| P3-WP-1 | ✅ 完成 | `label_invalid_months` / `short_pit_invalid_months` / `mid_term_invalid_months` |
| P3-WP-2 | ✅ 完成 | `labels_cache_v1.py` monolithic + `materialize_labels_v1_sharded_cached`（trainer 預設 `use_sharded_cache=False`） |
| P3-WP-3 | ✅ 完成 | `trainer.py` Step 2c 接 labels cache；metrics `labels_cache_hit` |
| P3-WP-4 | ✅ 完成 | short PIT shard manifest schema v2 + `entity_set_fingerprint` |
| P3-WP-5 | ✅ 完成 | `short_pit_invalid_months` 驅動 shard miss；移除 trainer 傳 `partition_inventory_fp` |
| P3-WP-6 | ✅ 完成 | quantile 降低寫 `delta/latest/added_player_ids.parquet` |
| P3-WP-7 | ✅ 完成 | short PIT shard delta merge（`restrict_player_ids` + merge） |
| P3-WP-8 | ✅ 完成 | `finalize_cache_report_from_metrics` L4/L5 layers |
| P3-WP-9 | 🔄 進行中 | 單元測試擴充中（P3-T-4～T-6 已覆蓋） |
| P3-WP-10 | ✅ 完成 | RUNBOOK §5.7 + §6.1 v2 更新 |
