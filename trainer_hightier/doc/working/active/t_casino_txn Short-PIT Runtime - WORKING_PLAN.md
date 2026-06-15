# t_casino_txn Short-PIT Runtime - Working Plan

本文件是 **Working / execution plan 層**，承接：

- Implementation plan：[`t_casino_txn Short-PIT Runtime - IMPLEMENTATION_PLAN.md`](../../implementation/active/t_casino_txn%20Short-PIT%20Runtime%20-%20IMPLEMENTATION_PLAN.md)

不重新定義 scope 或 runtime 決策。若與 implementation plan 衝突，先更新上層文件再執行。

## Scope Guardrails

- 只處理 `t_casino_txn` production runtime supplier（7 個 `txn__*`）。
- `txn_lite_builder` supplier name 不變；production 改 ClickHouse live short-PIT。
- Training / offline / parity 繼續使用 L0 cleaned parquet。
- 不做 feature supplier contract、`deploy_contract.json`、mid/long Feast 重構、模型重訓或 feature list 變更。
- Production **不保留** cleaned parquet fallback；CH source 不可用時 fail-fast。

## 執行護欄

- Runtime 行為收斂在 `config.py` / `HightierServingConfig`；CH 憑證仍由 `.env` overlay。
- 不新增多餘抽象層；CH fetch + DuckDB 聚合沿用 `materialize_txn_lite.py` 語意。
- PIT availability cutoff：production 使用 `prediction_visible_ts_cf`；cleaned offline 使用 `payout_complete_dtm`。

---

## Phase A — Source Contract And Schema Probe

| ID | Task | DoD |
|----|------|-----|
| A-1 | 鎖定 CH `{source_db}.t_casino_txn` 欄位對應 | `player_id`, `start_dtm`, `__etl_insert_Dtm`, `type`, `sub_type`, `txn_value`, `action`, `status`, `buyin_status` |
| A-2 | 實作 conservative availability mapping | `GREATEST(LEAST(__etl_insert_Dtm, start_dtm + 128s), start_dtm)`；禁止 silent event-time fallback |
| A-3 | Deploy preflight schema smoke | 可查表、必需欄位可解析、最近資料可 scan（`LIMIT 1`） |

**檔案：** `serving/txn_lite_ch_runtime.py`（`assert_ch_txn_supplier_ready_or_raise`）

---

## Phase B — Production CH Fetch And PIT Aggregation

| ID | Task | DoD |
|----|------|-----|
| B-1 | Batch-scoped CH fetch | 依 scoring batch `player_id` + bounded `[min_pcd - 1h, max_pcd]` 查詢 |
| B-2 | L1 filter + 聚合 | 輸出 7 個 `txn__*`；no-match zero-fill 與 `materialize_txn_lite.py` 一致 |
| B-3 | PIT predicate | `event_ts < pcd`；`available_ts <= prediction_visible_ts_cf`；window 1h / 15m |
| B-4 | 共用 SQL | `_build_materialize_copy_sql` 支援 availability cutoff 參數 |

**檔案：** `serving/txn_lite_ch_runtime.py`、`feature_experiment/materialize_txn_lite.py`

---

## Phase C — Wire Production Scorer Path

| ID | Task | DoD |
|----|------|-----|
| C-1 | `attach_txn_lite_features` production default → CH | `use_cleaned_parquet=False` 為預設 |
| C-2 | Cleaned parquet path 保留 | training / offline / parity 明確呼叫 |
| C-3 | `feature_supply` deploy gate | production 檢查 CH txn readiness，不再要求 `cleaned_casino_txn_root` |

**檔案：** `serving/feature_builder.py`、`serving/feature_supply.py`、`serving/scorer.py`（無需改動，經 attach 間接）

---

## Phase D — Focused Tests

| ID | Task | DoD |
|----|------|-----|
| D-1 | 15m / 1h window boundary | synthetic txn rows |
| D-2 | Availability boundary | event 已發生但 available 晚於 cutoff → 排除 |
| D-3 | BUYIN / CASHOUT / prize redemption / net cash flow | 與既有 materialize tests 對齊 |
| D-4 | No-match zero-fill | 空 txn pool → 全零 |
| D-5 | CH path injection seam | `txn_rows=` 參數供 unit test 不連 live CH |

**檔案：** `tests/test_txn_lite_ch_runtime.py`

---

## Phase E — Parity Gate

| ID | Task | DoD |
|----|------|-----|
| E-1 | Cleaned vs CH-runtime parity report | 同一批 sample bets，比較 7 個 `txn__*` |
| E-2 | Thresholds | hard fail > 0.5% diff fraction；warning ≤ 2% |
| E-3 | Deploy e2e 整合 | parity summary 寫入 gate report |

**檔案：** `serving/txn_lite_ch_runtime.py`（`run_txn_lite_parity_gate`）、`serving/deploy_e2e_gate.py`

---

## Phase F — Deploy E2E And Runtime Diagnostics

| ID | Task | DoD |
|----|------|-----|
| F-1 | Step 6 / deploy e2e scorer replay | 經 CH runtime 產生 `txn__*` |
| F-2 | Deploy report txn diagnostics | schema smoke、output columns、parity summary |
| F-3 | Production `main.py` | 不再因缺 `cleaned__gmwds_t_casino_txn` bundle path fail |

**檔案：** `serving/deploy_e2e_gate.py`、`deploy/main.py`、`build_deploy_package.py`

---

## Phase G — Cleanup And Docs

| ID | Task | DoD |
|----|------|-----|
| G-1 | 移除 production cleaned txn root workaround | deploy bundle 預設不再設 `cleaned_casino_txn_root` |
| G-2 | 更新 deploy README / `.env.example` | `txn__*` 由 ClickHouse short-PIT supply |
| G-3 | 保留 `cleaned_casino_txn_root` 僅供 offline / parity CLI | config 欄位保留但非 production 必要 |

---

## Execution Sequence

1. Phase A → B → C（schema → fetch/aggregate → scorer wiring）
2. Phase D（tests 固定語意）
3. Phase E（parity gate）
4. Phase F（deploy e2e）
5. Phase G（cleanup）

---

## Definition Of Done

- [ ] Active model 7 個 `txn__*` 可由 ClickHouse `t_casino_txn` production runtime 供應
- [ ] `score_once()` 不需 cleaned txn parquet root 即可建立完整 feature matrix
- [ ] 缺 CH txn source / required fields 時 deploy preflight hard fail
- [ ] Txn cleaned-vs-CH parity report 通過 agreed thresholds
- [ ] Deploy E2E report 含 txn supplier diagnostics 且 verdict pass
- [ ] Training / offline cleaned parquet 行為未被破壞
- [ ] Production deploy 不再要求手動複製 `cleaned__gmwds_t_casino_txn` partition

---

## Key Files

| File | Role |
|------|------|
| `serving/txn_lite_ch_runtime.py` | CH fetch、schema smoke、parity gate |
| `feature_experiment/materialize_txn_lite.py` | cleaned offline + 共用聚合 SQL |
| `serving/feature_builder.py` | production attach path |
| `serving/feature_supply.py` | deploy supplier readiness |
| `serving/deploy_e2e_gate.py` | txn diagnostics / e2e |
| `tests/test_txn_lite_ch_runtime.py` | focused unit + parity tests |

---

## Validation Commands

```bash
# Unit tests (no live CH required for synthetic path)
pytest trainer_hightier/tests/test_txn_lite_ch_runtime.py trainer_hightier/tests/test_materialize_txn_lite.py -q

# Deploy e2e gate (requires CH + model bundle)
python -m trainer_hightier.serving.deploy_e2e_gate --help
```

---

## Risks / Open Items

- CH `FINAL` dedup 與 L0 window dedup 可能有微小差異 → parity gate 量化；warning band ≤ 2%。
- Production availability cutoff 使用 `prediction_visible_ts_cf`，cleaned training 使用 `pcd` → parity 比較時 CH path 可選 `use_pcd_cutoff=True` 對齊 offline replay。
