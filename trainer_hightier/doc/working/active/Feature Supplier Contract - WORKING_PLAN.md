# trainer_hightier - Feature Supplier Contract Working Plan（執行計畫）

本文件屬於 **Working / execution plan 層**，承接：

- SSOT：[`Feature Supplier Contract - SSOT.md`](../../ssot/Feature%20Supplier%20Contract%20-%20SSOT.md)
- Implementation Plan：[`Feature Supplier Contract - IMPLEMENTATION_PLAN.md`](../../implementation/active/Feature%20Supplier%20Contract%20-%20IMPLEMENTATION_PLAN.md)

內容包含 supplier contract 的可執行任務拆解、實作順序、DoD、驗證與 rollout。**不重新定義** SSOT 或 implementation decisions；若規則需要改動，應先更新上層文件再執行。

> **狀態（2026-06-15）**：active；待 Phase A 開始 code slice。

---

## 1) 範圍與護欄

### 1.1 In Scope

- 新增 `feature_contract.py`：requirement map、validator registry、contract builder、cross-check。
- Package 階段生成 `models/deploy_contract.json`（`deploy_contract_v1`）。
- 統一 package / deploy e2e / deploy preflight 的 supplier contract gate。
- 初版 validators 覆蓋 scorer v2 supplier taxonomy。
- Focused tests：contract 生成、missing validator、`feast_trial_cols`、static artifact、drift detection。

### 1.2 Out Of Scope

- 不改 feature 計算語意、PIT 規則、mid/slow bounded ASOF。
- 不實作 [`t_casino_txn Short-PIT Runtime - IMPLEMENTATION_PLAN.md`](../../implementation/active/t_casino_txn%20Short-PIT%20Runtime%20-%20IMPLEMENTATION_PLAN.md) 本體；只把 `txn_lite_builder` 納入 contract / validator。
- 不重做 Feast refresh supervisor、Step 6 parity runner、training pipeline。
- 不新增 env path override 或 production feature algorithm knobs。

### 1.3 已鎖定決策

| ID | 決策 |
|----|------|
| FSC-IMPL-001 | `deploy_contract.json` 僅 package 生成；preflight 讀取但須 cross-check registry + plan builder |
| FSC-IMPL-002 | Code 落點：`trainer_hightier/serving/feature_contract.py` |
| FSC-IMPL-003 | 缺 validator → package / deploy hard fail |
| FSC-IMPL-004 | Schema：`deploy_contract_v1`；path `models/deploy_contract.json` |
| FSC-IMPL-005 | `.env` 僅 credentials / emergency override |
| FSC-IMPL-006 | Rollout：report-only → strict package → strict deploy preflight |
| FSC-IMPL-007 | `active_manifest.json` 只保留 summary flags；supplier 詳情以 contract 為準 |
| FSC-IMPL-008 | `bundle_static_artifact` 無條件進 contract（mapping + ADT allowlist） |
| FSC-IMPL-009 | `feast_trial_cols` 必須為空；非空 hard fail |

---

## 2) Work Breakdown

### Phase A — Contract Core

| Task | 內容 | DoD |
|------|------|-----|
| A1 | 新增 `trainer_hightier/serving/feature_contract.py` | 模組可 import；不與 `feature_supply.py` 循環依賴 |
| A2 | 定義 `SupplierRequirement`、`SupplierValidatorSpec`、`DeployFeatureContract` dataclasses | 含 type annotations 與 docstrings |
| A3 | 實作 `SUPPLIER_REQUIREMENTS` 初版 | 覆蓋 clickhouse_raw、short_term_pit、txn_lite、feast mid/slow、composite、bundle_static |
| A4 | 實作 `assert_no_legacy_feast_trial_cols(plan)` | 非空 `plan.feast_trial_cols` → raise |
| A5 | 實作 `resolve_supplier_requirements(plan, include_bundle_static=True)` | mapping / allowlist 即使不在 plan 也會出現 |
| A6 | 實作 `assert_all_requirements_have_validators(requirements)` | 缺 validator entry → raise |

**檔案：** `serving/feature_contract.py`

---

### Phase B — Deploy Contract Generator

| Task | 內容 | DoD |
|------|------|-----|
| B1 | 實作 `build_deploy_contract(...)` | 輸入 plan + requirements + registry fingerprint |
| B2 | 定義 `deploy_contract_v1` top-level fields | schema_version、model_version、feature_count、supplier_plan、requirements、validators、flags、fingerprint |
| B3 | 實作 stable `contract_fingerprint` | 同一輸入產生相同 hash |
| B4 | 實作 `write_deploy_contract_json(path, contract)` | 原子寫入 bundle `models/deploy_contract.json` |
| B5 | 實作 `load_deploy_contract_json(path)` | 解析失敗 raise 明確錯誤 |
| B6 | 實作 `assert_contract_matches_recomputed(...)` | mutate bucket / feature count / registry fingerprint 會 fail |

**檔案：** `serving/feature_contract.py`

---

### Phase C — Package Wiring

| Task | 內容 | DoD |
|------|------|-----|
| C1 | 在 `build_deploy_package.py` 的 `build_scorer_supplier_plan()` 後接 contract builder | 不複製 routing 邏輯 |
| C2 | 寫入 `{bundle_root}/models/deploy_contract.json` | strict pack 產物存在且可讀 |
| C3 | 保留 `active_manifest.json` summary flags | 如 `deploy_requires_ch_txn_supplier`；不作 supplier SSOT |
| C4 | Phase 1：write + log compare；Phase 2 起 missing validator / legacy bucket hard fail | rollout 可切換 |
| C5 | 更新 bundle README（Phase 3） | 說明 ops 如何讀 contract；不引入人工 config |

**檔案：** `build_deploy_package.py`、`README_DEPLOY.md`（Phase 3）

---

### Phase D — Deploy E2E / Preflight Wiring

| Task | 內容 | DoD |
|------|------|-----|
| D1 | 在 `deploy_e2e_gate.py` 新增 `supplier_contract` gate step | 在 bundle_contract 之後執行 |
| D2 | 在 `deploy/main.py` `_preflight_feature_supplyability()` 讀 contract + cross-check | 與 e2e 同一套 registry |
| D3 | 將 `external_data_roots` / Feast smoke 逐步收斂到 validator registry | 避免分散 gate 邏輯 |
| D4 | report-only mode 輸出 drift detail；strict mode hard fail | 錯誤訊息指出 drift 欄位 |
| D5 | e2e report 寫入 contract fingerprint、validator results、failure reason | `deploy_e2e_gate_report.json` 可稽核 |

**檔案：** `serving/deploy_e2e_gate.py`、`deploy/main.py`

---

### Phase E — Validators And Tests

| Task | 內容 | DoD |
|------|------|-----|
| E1 | 新增 `tests/test_feature_contract.py` | unit tests 覆蓋 core contract API |
| E2 | 測 stable fingerprint、contract generation、drift detection | mutate → cross-check raises |
| E3 | 測 `bundle_static_artifact` always included | 即使 plan 無對應 bucket 也出現在 requirements |
| E4 | 測 non-empty `feast_trial_cols` hard fail | package assertion raises |
| E5 | 測 missing validator hard fail | registry 缺 entry → raise |
| E6 | 擴充 `test_build_deploy_package.py` | strict pack 產出 `deploy_contract.json` |
| E7 | 擴充 `test_deploy_e2e_gate.py` | `supplier_contract` step pass/fail paths |
| E8 | Reference model 驗收 | `20260613-162313-3eb8de4`：42/42 routable；flags 符合 SSOT |

**檔案：** `tests/test_feature_contract.py`、`tests/test_build_deploy_package.py`、`tests/test_deploy_e2e_gate.py`

---

## 3) Execution Sequence

1. **Phase A**：先建立 contract core 與 hard-fail policy（`feast_trial_cols`、static artifact、missing validator）。
2. **Phase B**：建立 contract JSON generator / loader / cross-check。
3. **Phase C**：接 package；Phase 1 先產物可見，Phase 2 起 strict fail。
4. **Phase D**：接 deploy e2e / preflight；同一 validator registry。
5. **Phase E**：補 focused tests 與 reference bundle 驗證。

---

## 4) Rollout Checklist

| Phase | Mode | Package | Deploy Preflight | Deploy E2E |
|-------|------|---------|------------------|------------|
| 1 | report-only | 寫 contract + log compare | log drift，不 fail | optional compare |
| 2 | strict package | missing validator / legacy bucket fail | 仍 report-only | 跑 validators |
| 3 | strict deploy | contract 必備 | drift hard fail | drift hard fail |

**Phase 1 退出條件：** reference model bundle 可產出 contract；fingerprint 穩定；無 import / schema 錯誤。

**Phase 2 退出條件：** package CI fail on missing validator；txn model 不再要求 cleaned partition path。

**Phase 3 退出條件：** production deploy 在 scorer 啟動前 fail-fast 所有 supplier contract violations。

---

## 5) Definition Of Done

- Bundle 含 versioned `deploy_contract.json`，由 package 自動生成。
- 每個 active model 的 `runtime_supplier` 在 registry 有 validator；缺則 package fail。
- Deploy preflight 與 e2e 使用同一 validator registry；結果可稽核。
- Contract cross-check 可偵測 plan drift；strict mode hard fail。
- Production 不要求 training/package absolute data path；`.env` 不控制 supplier 行為。
- Mapping / ADT allowlist 以 `bundle_static_artifact` 無條件進 contract。
- `feast_trial_cols` 在 scorer v2 contract 中為空；非空 hard fail。
- Reference model `20260613-162313-3eb8de4`：42/42 features 可路由；contract flags 與 SSOT taxonomy 一致。

---

## 6) Validation Commands

建議實作後依序執行：

```bash
python -m pytest trainer_hightier/tests/test_feature_contract.py -q
python -m pytest trainer_hightier/tests/test_build_deploy_package.py -q
python -m pytest trainer_hightier/tests/test_deploy_e2e_gate.py -q
```

Reference bundle 驗證（Phase E 後）：

```bash
# 以現有 deploy package / e2e gate 命令重跑 reference model bundle
# 確認 models/deploy_contract.json 與 deploy_e2e_gate_report.json 含 supplier contract evidence
```

---

## 7) Risks And Rollback

| Risk | Mitigation | Rollback |
|------|------------|----------|
| Contract 與 registry drift | cross-check fingerprint | Phase 1 report-only 不 block deploy |
| Validator 僅檢查 existence | SSOT 最低要求寫入 spec | 暫時關閉 strict deploy gate |
| Rollout 一次 strict 阻斷 deploy | 三階段 rollout | 退回 Phase 1 report-only |
| `feature_supply.py` 循環 import | contract 模組只 import plan types | 保持 builder 接受 plan 參數 |

---

## 8) Related Documents

| 層級 | 文件 |
|------|------|
| SSOT | [`Feature Supplier Contract - SSOT.md`](../../ssot/Feature%20Supplier%20Contract%20-%20SSOT.md) |
| Implementation | [`Feature Supplier Contract - IMPLEMENTATION_PLAN.md`](../../implementation/active/Feature%20Supplier%20Contract%20-%20IMPLEMENTATION_PLAN.md) |
| Txn short-PIT（scoped） | [`t_casino_txn Short-PIT Runtime - WORKING_PLAN.md`](t_casino_txn%20Short-PIT%20Runtime%20-%20WORKING_PLAN.md) |
| Deploy E2E | [`Production-like Deploy E2E Gate - IMPLEMENTATION_PLAN.md`](../../implementation/active/Production-like%20Deploy%20E2E%20Gate%20-%20IMPLEMENTATION_PLAN.md) |
