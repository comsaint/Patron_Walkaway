# Plan index

**Current execution plan**: [PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) (Phase 2 P0–P1).

**Phase 2 status**（2026-03-18）：**T0–T10 已完成**（詳見 PLAN_phase2_p0_p1.md § Ordered Tasks 與 Remaining items）。

This file exists so README and review tests (R384, R147) that reference `.cursor/plans/PLAN.md` pass. The 特徵整合計畫 section below is retained for round147 contract (no Step 9+ in that section).

---

## 特徵整合計畫：Feature Spec YAML 單一 SSOT（已實作）

### 目標與原則

1. **YAML = 三軌候選特徵的唯一真相來源**：所有 Track Profile / Track LLM / Track Human 的候選特徵均在 Feature Spec YAML 定義。
2. **Scorer 由 Trainer 產出驅動**：Scorer 計算的特徵清單與計算方式完全由 trainer 產出的 `feature_list.json` + `feature_spec.yaml` 決定。
3. **Serving 不依賴 session**：所有進模型的候選特徵計算**不得**依賴 session 資訊。
4. **Track LLM 單一 partition**：所有 Track LLM 的 window/aggregate 一律 `PARTITION BY canonical_id`。

### Step 1 — YAML 補完

（已實作；詳見 archive/PLAN_phase1.md § 特徵整合計畫。）

### Step 2 — Python helper（features.py）

（已實作。）

### Step 3 — 移除硬編碼，改用 YAML

（已實作。）

### Step 4 — compute_track_llm_features 擴充

（已實作。）

### Step 5 — Screening 改造

（已實作。）

### Step 6 — Scorer 對齊

（已實作。）

### Step 7 — Artifact 產出

（已實作。）

### Step 8 — 測試

（已實作。）

### 實作順序

1. Step 1 → 2 → 4 → 3 → 5 → 7 → 6 → 8。
