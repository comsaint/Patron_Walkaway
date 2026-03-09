# Phase 2 規劃草稿（點子備忘）

> 重點：**穩定、可維護的部署**，而非效能優化。以下為待擴充的點子列表。

## 方向

| # | 方向 | 點子 | 備註 |
|---|------|------|------|
| 2 | 告警與人因 | Human-oriented reason for alerts |  |
| 7 | 告警與人因 | 告警與 runbook | 誰收警、收到後怎麼處理（搭配 #2） |
| 6 | 可追溯性 | Artifact 與 pipeline 溯源 | 哪份 code/data 產出哪個 model；rollback、審計用 |
| 10 | 部署 | 新模型 staged rollout（canary / 比例流量） | 先打一部份再全量，降低一次換錯風險 |
| 1 | 模型監控 | 模型監控（model versioning、drift detection 等） |  |
| 4 | 評估 | Run-level / Macro-by-run 評估（DEC-012） | 以 run 為單位的評估口徑 |
| 3 | 特徵 | Leverage game table | Track Human `table_hc` 啟用（Phase 1 未啟用） |
| 8 | 維運 | 設定與閾值管理（config / feature flag、model version 切換） | 閾值或模型版本可配置、可快速回滾 |
| 9 | 穩定性 | Health / SLA 定義與監控 | 明確定義「正常」、延遲與可用率 |
| 5 | 資料品質 | 資料品質監控（DQ dashboard / 定期檢查） | 輸入 null、schema、volume 等，問題早發現 |
|  |  | （待補充） |  |

---

## 文獻與業界建議（Phase 2 可考量）

以下為針對「即時預測 + 表格式/時序」場景，從 Kaggle 競賽解法與生產 ML 文獻整理出的建議，供 Phase 2 規劃參考。

### 訓練與推論一致

- **特徵與模型一起版本化**：特徵計算邏輯與清單納入 model artifact 或 model registry，確保 rollback 時特徵一致。
- **單一執行層**：訓練與 serving 盡量共用同一套程式與執行環境（同一 query/aggregation 語意），避免「同邏輯、不同實作」造成的 training–serving skew。

### 特徵與延遲

- **線上 Feature Store**：若未來需 &lt;50ms 延遲或高 QPS，可引入 Online Feature Store（如 Redis）；區分 batch 特徵（離線算好）與 real-time 特徵（即時計算），並用同一 API 取特徵。
- **雙路架構**：離線路供訓練與 backfill，串流路供即時更新與低延遲推論；必要時再評估。

### 模型重訓與監控

- **Drift 觸發重訓**：除排程重訓外，可加入 **事件驅動重訓**（concept / covariate / label drift 達閾值時觸發），搭配 Evidently、SageMaker Model Monitor 或自建 PSI/分佈檢定。
- **自動化 pipeline**：重訓流程用 Airflow、SageMaker Pipelines、Kubeflow 等編排，並與 drift 告警整合。

### 部署與風險控制

- **Staged rollout**：新模型先 canary 或比例流量，再全量，降低一次換錯風險（與上表 #10 一致）。
- **Health / SLA**：明確定義「正常」、推論延遲與可用率，並納入監控。

### 方法論進階（可選）

- **Time-to-event**：若需更細時間預測，可評估 Temporal Point Process（TPP）或 survival 模型（預測「離場時間」再轉成 15 分鐘內機率）；實務上固定窗二元分類多已足夠。
- **線上學習**：在分佈持續快速變化時，可評估 River、Vowpal Wabbit 等 incremental learning；多數場景仍以週期性重訓 + 即時推論為主。
