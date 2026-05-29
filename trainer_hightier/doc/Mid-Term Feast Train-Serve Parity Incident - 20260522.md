# Mid-Term Feast Train-Serve Parity Incident - 2026-05-22

本文件記錄 high-tier MVP 模型（`20260520-032615-df799bd`）在 production scorer v2 + Feast online 路徑上，mid-term / composite `fe__*` 大量缺失的事故根因、證據、訓練資料對照，以及建議修復方案。

## Current Status

- 事故狀態：**根因已確認；historical mid bootstrap + readiness gate 硬化已落地；2026-05-23 P0 閉環 E2E pass。**
- **P0 驗證（2026-05-23，v3）**：
  - `deploy_e2e_gate` + `--bootstrap-mid` + **training mid snapshot seed** 於 incident 模型 bundle（`20260520-032615-df799bd`）觸發 mid+slow refresh。
  - Mid Feast parquet canonical 覆蓋：**99 → 3582**（allowlist 3584；覆蓋率 **99.9%**）。
  - Refresh smoke：`mid_cell_null_rate=0.0`（core 8 權 smoke 欄位）。
  - Deploy smoke：`mid_cell_null_rate=0.0`；freshness **fresh**（`expected_anchor_gaming_day=2026-05-11`，data-bounded，local cleaned 資料上限）。
  - Scorability replay：**pass**（`out/p0_incident_deploy_e2e_gate_report_v3.json`，`verdict=pass`）。
  - Slow-only 最新模型（`20260522-232846-7961db8`）deploy E2E gate **verdict=pass**。
  - `build_deploy_package` 已接入 Step 6 parity gate（strict 預設要求 `feature_parity_verification.json` + slow gate pass）。
- **程式修復摘要**：training mid snapshot seed → carry-forward merge；core mid smoke columns（8 欄）；deploy lookup 合併 smoke 欄位避免 false null rate；`expected_anchor_gaming_day` data-bounded freshness。
- 影響模型：`out/models_high_tier_mvp/20260520-032615-df799bd`（deploy bundle `out/deploy_hightier/p0-incident-df799bd`）。
- 診斷工具：已新增 `trainer_hightier/serving/audit_supplier_root_cause.py`（逐列 supplier 主因分類）。

## Symptoms

- Production `prediction_log`：`model_features_missing` 偏高；`fe_derived` / mid-term / composite 欄位大量 null。
- 代表性 composite：`fe__wager_sum__w15m_over_w1d` 在 root-cause 樣本上 **834/834** 為 null，主因 **100%** `feast_online_mid_value_missing`。
- Short-term 分子（如 `fe__wager_sum__w15m`）在相同列上**有值** → 不是 6h PIT pool 問題，也不是 join bug（無 `join_failure`）。
- Feast refresh 報告顯示 `entity_missing_rate = 0`、`ok = true`，但 mid 欄位 `cell_null_counts` 在 smoke 樣本上為 **20/20**（全 null）。

## Production Evidence (artifacts)

| Artifact | 關鍵觀察 |
|----------|----------|
| `gmwds_deploy_supplier_root_cause.json` | `feast_online_mid_value_missing`: 4778；`short_term_pit_builder`: 179；無 `join_failure` |
| `gmwds_deploy_mid_term_spike_canonical.parquet` | 僅 **99** 列 canonical；`fe__wager_sum__w1d` 等 mid 欄位在 parquet 內 **非 null**（null_rate ≈ 0） |
| `gmwds_deploy_feast_online_refresh_report.json` | refresh `verdict=ok`；smoke `entity_missing_rate=0`；mid `cell_null_counts` 全 20 |
| `gmwds_deploy_feast_online_readiness.json` | `mid_term.row_count=99`；`distinct_canonical_count=99`；與 allowlist **3584** 嚴重不符 |

**解讀**：Feast online 對 smoke 請求的 entity 有回列，但 feature 值為 null；同時 materialized parquet 只覆蓋 99 個 canonical，代表 **refresh 產物覆蓋崩塌**，不是單純「欄位算錯」。

## Root Cause (confirmed)

### 1. Train-serve 語意不一致（主因）

| 階段 | Mid-term 供應語意 |
|------|------------------|
| **Training** | 產生多個 `anchor_gaming_day` 的 canonical daily snapshot；enrich 時 **ASOF**：`anchor_gaming_day < bet.gaming_day` 取最近一筆（無固定 N 天上限） |
| **Production（現況）** | Refresh 只算 **單日** `anchor_start = anchor_end = D-1`；`write_mid_feast_parquet()` 再壓成每 canonical 一列；若 D-1 無下注則該日無 snapshot row |

Training join（ASOF）：

```457:463:trainer_hightier/serving/feature_builder.py
  LEFT JOIN LATERAL (
    SELECT *
    FROM mid_snap AS s
    WHERE TRIM(CAST(s.canonical_id AS VARCHAR)) = bw._cid
      AND CAST(s.anchor_gaming_day AS DATE) < bw._gday
    ORDER BY CAST(s.anchor_gaming_day AS DATE) DESC
    LIMIT 1
```

Production refresh bounds（單日 anchor）：

```126:133:trainer_hightier/serving/feast_online_refresh.py
    anchor_end = expected_mid_term_anchor(serving_day)
    anchor_start = anchor_end
    lb = int(MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    bets_gday_end = anchor_end
    bets_gday_start = anchor_end - timedelta(days=lb - 1)
```

**結果**：玩家在「昨天沒下注」時，training 仍可回退到更早 anchor；production 常拿不到 `fe__wager_sum__w1d` 等 mid primitive → composite（如 `fe__wager_sum__w15m_over_w1d`）連鎖為 null。

### 2. Readiness / smoke gate 盲點（放大事故）

`run_allowlist_feast_lookup_smoke()` 的 `ok` 僅檢查 `entity_missing_rate <= fail_fraction`，**不檢查** `cell_null_counts`。因此可出現：

- entity 全 present（`entity_missing_rate = 0`）
- mid 欄位值全 null（`cell_null_counts = sample_size`）

→ deploy 仍判定 refresh 成功。

### 3. 與 allowlist 不一致無關（已排除）

- Scorer 與 refresh 皆使用 bundle 內 `adt_allowed_players_q0p99.parquet` + `canonical_player_mapping.parquet`。
- Packaged allowlist：**3584** players → **3584** mapped canonical（訓練 universe 同級）。
- Production parquet 僅 **99** canonical：問題是 **refresh 產物覆蓋**，不是兩邊用不同名單。

### 4. Long-term 未同等爆炸（預期內）

- Slow 路徑未走與 mid 相同的「單日 anchor + 昨日必須有 bet」約束；180d 聚合較稠密。
- Mid 對「昨日無 bet」敏感；slow 對同類稀疏性較不敏感。

## Training Data Study (parity quantification)

資料來源（模型 `20260520-032615-df799bd` 同次訓練）：

- `trainer_hightier/artifacts/training_data/_main_trainer_mid_term_daily_snapshot.parquet`
- `trainer_hightier/artifacts/training_data/training_set_fe_enriched.parquet`

### Snapshot 規模

| 指標 | 值 |
|------|-----|
| Snapshot rows | 69,734 |
| Distinct canonical | 3,582 |
| Anchor 日期範圍 | 2024-07-08 ~ 2026-05-10 |
| Training bet-day（有 canonical + gaming_day） | 69,798 |

### 玩家下注間隔（相鄰 anchor day）

| 分位 | 間隔天數 |
|------|----------|
| p50 | 1（69.5% 為隔天） |
| p75 | 3 |
| p90 | 32 |
| p95 | 63 |

當 production 只用 **N=1（昨天）** 而 miss 時，距離上一個可用 anchor 的中位數約 **16 天**（p90 ≈ 90 天）。

### `fe__wager_sum__w1d` 覆蓋模擬（bet-day 粒度）

規則：`anchor_end = gaming_day - 1`；在 `[anchor_end - (N-1), anchor_end]` 取最新 anchor（模擬 production `write_mid_feast_parquet`）；對照 training ASOF（`anchor < gaming_day`，無 N 上限）。

| 策略 | null rate |
|------|-----------|
| Training ASOF | **5.13%** |
| Production N=1 | **34.08%** |
| N=7 | 24.68% |
| N=14 | 20.55% |
| N=30 | 14.91% |
| N=32（= `MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS`） | 14.35% |
| N=60 | 10.12%（仍遠高於 training） |

- Training 實際 `fe__wager_sum__w15m_over_w1d` null = **5.13%**（與 ASOF 一致）。
- N=1 → N=7 可修復 **27.6%** 的 N=1 miss bet-day。

**結論**：加大 N 有幫助，但**無法用有限 N 完全復刻 training ASOF**；若要語意一致，production 需 **latest-ASOF / carry-forward**，或 **retrain 成 bounded-N 契約**。

## Supplier Responsibility (for `fe__wager_sum__w15m_over_w1d`)

Registry `runtime_inputs`：

- `short_term_pit_builder`: `fe__wager_sum__w15m`
- `feast_online_mid`: `fe__wager_sum__w1d`
- `runtime_supplier`: `composite`

缺 composite 時：先查 Feast `w1d`；若 short 有值、Feast `w1d` 為 null → **Feast mid-term**，非 short-term、非 join。

## Proposed Solutions

### Option A — 短期：修 production 對齊**現有已部署模型**（推薦先做）

**目標**：讓 serving 語意接近 training ASOF（latest prior anchor），不 retrain。

1. **Mid online store：latest-ASOF / carry-forward**
   - Bootstrap：對 allowlist canonical 建立 historical mid snapshot；每 canonical 保留 `anchor_gaming_day <= D-1` 的 **latest** row 寫入 Feast online。
   - Daily incremental：只為昨日有新 bet 的 canonical 計算並 upsert 新 anchor；**無昨日 bet 的 canonical 保留既有 online 值**（不變 null）。
2. **分離 schema apply 與 data refresh**
   - 避免每次 refresh 都 `reset_feast_repo_runtime_state()` 後只 materialize 單日，導致覆蓋從 3584 塌縮到 99。
   - Schema 變更才 `feast apply` + reset；日常 refresh 僅 upsert feature values。
3. **硬化 readiness gate**
   - 對模型實際需要的 `plan.feast_mid_cols` 檢查 **cell null rate**（例如 `fe__wager_sum__w1d` null_rate < 5% on allowlist smoke）。
   - 保留 `entity_missing_rate` 檢查，但不可作為唯一條件。
4. **持續診斷**
   - 部署後跑 `audit_supplier_root_cause.py`（prediction log 或 CH 抽樣）驗證主因分布。

**優點**：不動模型權重、最快恢復 scoring 覆蓋。  
**風險**：需改 refresh/materialize 流程與維運習慣；carry-forward 的「語意年齡」需在 log 中可觀測（建議寫入 `mid_term_snapshot_age_days` 或等價 audit）。

---

### Option B — 中期：bounded-N 對齊 + **retrain**

**目標**：明確定義 production contract（例如 `N=30`），訓練與 serving 共用同一套 bounded ASOF。

1. Training enrich 改為：仅允许 `anchor_gaming_day ∈ [gaming_day - N, gaming_day - 1]` 的 latest anchor（與 production 相同 N）。
2. 新增或強制 audit 欄位：`mid_term_snapshot_age_days`、`mid_term_snapshot_missing_flag`。
3. 重訓模型並對照 alert 指標（precision/recall、alerts/hour、feature null、score 分佈）。

**建議 N（依訓練模擬）**：

| N | 模擬 null rate | 備註 |
|---|----------------|------|
| 7 | 24.7% | 成本低，改善有限 |
| 14 | 20.5% | 務實折衷 |
| 30 | 14.9% | **建議 retrain 起點** |
| 60 | 10.1% | 仍高於 training ASOF 5.1% |

**優點**：契約清晰、長期可維護；特徵名稱與「最久多久沒更新」一致。  
**風險**：需完整 retrain + 部署新 bundle；線上行為與現模型不同。

---

### Option C — 長期：全量歷史 snapshot + 點查 ASOF（最重）

每天為 allowlist 保留完整 anchor 歷史（或 rolling 窗口），serving 時按 bet 的 `gaming_day` 做 ASOF lookup（等同 training SQL）。

**優點**：與 training 語意最接近。  
**缺點**：Feast online / 儲存與 refresh 成本最高；需評估 Feast 0.63 點查能力與延遲。

---

## Recommended Decision

| 時間軸 | 選擇 | 理由 |
|--------|------|------|
| **立即（現模型）** | **Option A** | 模型在 unlimited ASOF 上訓練；finite N-only refresh 會改變分佈，不應直接套在已部署權重上 |
| **下一版模型** | **Option B（N≈30）** | 把 staleness 變成明確契約；搭配 audit 欄位與 gate |
| **不建議作為首選** | 僅 Option B 的 N=7/14 不修 A | 無法充分修復現模型 parity；retrain 前 production 仍會大量 null |

**不建議**：在現模型上僅把 production 改成 N=14 而不做 carry-forward——訓練 study 顯示 N=14 仍有 ~20% null，與 training ~5% 差距大。

## Implementation Checklist (engineering)

### P0 — 恢復現模型 serving

- [x] Mid refresh：bootstrap + daily incremental upsert（carry-forward）
- [x] 禁止日常 refresh 清空 online store 後只寫單日 99 人
- [x] Readiness：mid 欄位 cell null rate gate
- [ ] 驗證：`audit_supplier_root_cause` + `audit_production_readiness` 對同一 bundle（生產環境 operator 執行）

### P1 — 契約與觀測

- [x] 在 prediction log / readiness 記錄 `mid_term_anchor_max`、`snapshot_age_days`、mid null top features
- [x] 文件化 train/serve mid contract 於 `Scorer Runtime Contract - SSOT.md`（Option A 決策）

### P2 — 下一版模型（若採 Option B）

- [ ] Training materialize/enrich 支援 `production_mid_anchor_backfill_days = N`
- [ ] Registry 標註 bounded ASOF 與 audit 欄位
- [ ] 重訓 + shadow 對比現模型

## Related Code & Docs

| 路徑 | 說明 |
|------|------|
| `trainer_hightier/serving/feast_online_refresh.py` | Bootstrap bounds、carry-forward merge、`write_mid_feast_parquet` |
| `trainer_hightier/serving/feast_readiness.py` | Allowlist smoke + cell null / coverage gate |
| `trainer_hightier/serving/feature_builder.py` | Training ASOF mid join |
| `trainer_hightier/serving/audit_supplier_root_cause.py` | 逐列 supplier 根因 |
| `trainer_hightier/trainer.py` | Step 3.5 mid snapshot（training scope） |
| `trainer_hightier/doc/Feature Serving Incident - 20260519.md` | 前一版 serving 事故（不同根因） |
| `trainer_hightier/doc/Feast Online Refresh - IMPLEMENTATION_PLAN.md` | Feast refresh 設計背景 |

## Operator Steps（Option A — scorer v2 Feast mid）

**首次 deploy / 事故修復（bootstrap）**

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --layers mid \
  --source clickhouse \
  --bootstrap-mid \
  --apply-schema \
  --feast-repo /path/to/bundle/feast_repo \
  --adt-allowlist /path/to/bundle/mapping/adt_allowed_players_q0p99.parquet \
  --canonical-mapping /path/to/bundle/mapping/canonical_player_mapping.parquet
```

**日常 incremental（carry-forward；不 reset online store）**

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --layers mid \
  --source clickhouse \
  --skip-apply \
  --feast-repo /path/to/bundle/feast_repo \
  --adt-allowlist /path/to/bundle/mapping/adt_allowed_players_q0p99.parquet \
  --canonical-mapping /path/to/bundle/mapping/canonical_player_mapping.parquet
```

**驗收**

1. `artifacts/feast/mid_term_spike_canonical.parquet` 列數 ≥ allowlist canonical 的 95%
2. Deploy readiness smoke：`mid_cell_null_rate` < 5%（`fe__wager_sum__w1d` 等 mid 欄）
3. `python -m trainer_hightier.serving.audit_supplier_root_cause ...` — `feast_online_mid_value_missing` 非主導
4. 重啟 deploy scorer（或 `--force-feast-refresh` 首次 bootstrap）

Deploy 會在 readiness 缺失或 mid 覆蓋不足時自動 `--bootstrap-mid`；日常 stale refresh 走 merge 路徑且 `skip_apply`。

## Open Questions

1. Feast online 是否支援 incremental upsert per `canonical_id`，還是必須 full `materialize` window？（影響 Option A 實作細節）
2. Carry-forward 最大可接受 staleness（天）是否需寫入 SSOT 與 alert 規則？
3. 下一版模型是否採 `N=30` 或 `N=60`，需以 shadow scoring + 業務指標確認，而非僅 null rate。

---

*Last updated: 2026-05-22. Evidence from deploy bundle `gmwds_deploy_20260520-032615-df799bd` and training artifacts under `trainer_hightier/artifacts/training_data/`.*
