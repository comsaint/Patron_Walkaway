# Dynamic Schema Gate - IMPLEMENTATION_PLAN

本文件為 **Implementation plan 層**，描述如何把「frozen registry + dynamic schema gate + static minimum contracts」落地成穩定、可回滾、可驗證的建包防線。

## 1) 目標到方案映射

### 1.1 在 build 階段攔截契約錯誤
- 方案：在 `build_deploy_package` 導入雙層 gate。
  - **Static gate**：檢查各 artifact 的最小結構契約（join key / anchor / id 欄位）。
  - **Dynamic gate**：僅檢查 `model.pkl` 實際使用的 feature 欄位，並依 frozen registry 的 `source` 分派到對應 parquet。
- 預期效果：schema 與 feature 映射錯誤在建包前 fail-fast，不再延後到 serving 才爆錯。

### 1.2 讓 feature 常態變更不造成測試脆弱
- 方案：以 frozen registry + model feature_columns 作為唯一檢查集合，取消硬編碼 feature 清單。
- 預期效果：新增/移除 feature 只要 registry 與 parquet 一致即可通過，降低測試維護成本。

### 1.3 保留結構性防線，避免 join key 漏網
- 方案：把 static minimum contracts 視為不可降級契約；與 dynamic gate 分離維護。
- 預期效果：即使 dynamic gate 被 `--no-strict` 降級，結構欄位仍受保護。

## 2) 目標架構與模組邊界

### 2.1 訓練端（producer）
- 模組：`trainer_hightier/trainer.py`
- 職責：
  - Step 5 成功後凍結 registry snapshot 到 model bundle。
  - 寫入 `feature_candidate_registry_sha256` 到 `training_metrics.json` / `run_report.json`。
  - 在 `deploy_inputs` best-effort 攜帶 snapshot 供後續建包。

### 2.2 建包端（consumer + enforcer）
- 模組：`trainer_hightier/build_deploy_package.py`
- 職責：
  - 載入 `model.pkl` 的 `feature_columns`。
  - 載入 frozen registry snapshot 並做 SHA 驗證。
  - 執行 static gate（結構契約）與 dynamic gate（欄位契約）。
  - 在 `bundle_info.json` 輸出可追溯 metadata（包含 registry sha）。

### 2.3 契約來源（SSOT 依賴）
- `feature_candidate_registry.snapshot.yaml`：feature -> source 映射（訓練時凍結）。
- `model.pkl`：本次模型實際依賴的特徵欄位集合。
- artifact schema（parquet headers）：實際可供應欄位。

## 3) 主要工作流（Phase）

### Phase A：Freeze 與可追溯性建立
- 在訓練產物內建立 snapshot + hash。
- 失敗策略：snapshot 缺失或 hash 不一致時，在 strict 模式直接阻斷建包。

### Phase B：Gate 執行流程固定化
- 建包固定順序：
  1. 複製模型與快照依賴檔。
  2. 執行 static contracts。
  3. 驗證 frozen registry sha。
  4. 依 source 做 dynamic schema gate。
- 設計原則：先做結構契約，再做欄位契約，錯誤訊息可快速定位。

### Phase C：兼容與過渡機制
- `--strict` 預設啟用：完整 gate 生效。
- `--no-strict` 過渡：允許舊 bundle 缺 frozen registry 時跳過 dynamic gate，但保留 static gate。
- 目標：支援舊模型短期發版，同時推動重訓遷移到新契約。

### Phase D：文件化與運維落地
- RUNBOOK 補齊常見錯誤分類：
  - registry snapshot 缺失
  - registry hash mismatch
  - source 對應 parquet 缺欄
  - static key/anchor 缺失
- 明確提供修復方向（重訓、重物化、換 manifest、回滾）。

## 4) 里程碑與交付物

### M1：可追溯訓練輸出
- 交付物：bundle 內 snapshot、metrics 與 run_report 含 registry sha。

### M2：可運作建包 gate
- 交付物：build 端完整 static + dynamic gate，錯誤訊息帶 `[pack-schema]` 前綴。

### M3：測試與文件收斂
- 交付物：
  - 正反向測試矩陣（hash mismatch / dynamic missing / static missing / anchor alias）。
  - RUNBOOK 排障段落更新。

## 5) 風險、依賴與緩解

### 5.1 主要風險
- **Artifact grain 不一致風險**：gate 假設與 slow/trial parquet 真實 grain 不一致會造成系統性 fail。
- **舊 bundle 遷移風險**：歷史 bundle 缺 snapshot，短期發版受阻。
- **source 規則漂移風險**：registry `source` 語意若未固定，dynamic gate 會出現誤判。

### 5.2 緩解策略
- 以 `source` 作為唯一分派依據，禁止隱式前綴猜測。
- 明確定義 strict / non-strict 行為邊界與 sunset 時程。
- 導入最小 e2e 契約測試：以真實 materializer 產物驗證 gate，不僅用手工 fixture。

## 6) 驗證與發版策略

### 6.1 驗證策略
- 測試層級：
  - 單元：helper 函式與錯誤訊息格式。
  - 整合：`test_build_deploy_package.py` 契約矩陣。
  - 最小 e2e：Step 5 bundle -> build_deploy_package。
- 效能原則：schema gate 僅讀 parquet schema，不掃描全檔，降低記憶體風險。

### 6.2 發版與回滾
- 發版前：必跑建包測試矩陣 + 一條真實 bundle smoke。
- 回滾策略：保留上一版可部署 bundle；若新 gate 阻擋且非資料正確性問題，可短期用 `--no-strict` 過渡（僅限缺 snapshot 類）。

## 7) 治理與責任分工（高層）

- 訓練模組 owner：負責 snapshot freeze 與 metrics/run_report 可追溯欄位。
- 打包模組 owner：負責 gate 邏輯、錯誤語意一致性、bundle metadata。
- 資料契約 owner：負責 registry source 定義與 artifact schema 穩定性。
- On-call / release owner：負責 runbook 執行、例外升級、回滾判斷。

## 8) 假設與待決議事項

### 假設
- frozen registry 為 feature source 映射的發版準據。
- 打包時可取得完整 deploy_inputs（或可由預設路徑補齊）。

### 待決議
- slow artifact 的 grain 契約最終定版（bet-grain 或 player-grain）與對應 gate 靜態鍵。
- `--no-strict` 過渡期截止條件（版本或日期）。
- source 擴充規則（新增 source 時的 gate 行為與 owner 驗收）。
