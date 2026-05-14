---
name: trainer-hightier-working-plan
overview: Execution-level working plan for implementing the trainer_hightier data pipeline architecture defined in the implementation plan, with task sequencing, dependencies, owners, and definition of done.
todos:
  - id: w1-inventory-recompute
    content: 完成 gaming_day 分片 inventory、fingerprint 與重算集合決策器（含 recent window/correction）
    status: pending
  - id: w2-decouple-threshold
    content: 完成 step 2b 解耦與 gaming_day 分片輸出（日鍵+月桶），確保 threshold 擴大只補新玩家
    status: pending
  - id: w3-incremental-feast-versioning
    content: 完成 date_slice × feature_group 快取、lookback dirty 擴張、Feast 分片重用 retrieval 與 training set 10 版保留
    status: pending
  - id: w4-metamorphic-guardrail-tests
    content: 完成 metamorphic A/B 與 cohort-global normalization guardrail 測試
    status: pending
  - id: w5-dvc-report-e2e
    content: 完成 DVC stage 化、每次 run 報表、端到端回歸驗收
    status: pending
isProject: false
---

# trainer_hightier - Data Pipeline Working Plan

本文件是 **Working / execution plan 層**，僅描述具體執行任務與順序，並回鏈 Implementation plan：

- [C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Data pipeline - IMPLEMENTATION_PLAN.md](C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Data pipeline - IMPLEMENTATION_PLAN.md)

## 執行原則

- 僅涵蓋資料管線、特徵物化、訓練資料產製與治理。
- 不包含模型訓練策略、調參、FE 試驗方法學。
- 每個階段必須完成 DoD 才能進下一階段。

## 時程分組（建議 5 個 iteration）

### Iteration 1: Pipeline 骨架與分區治理底座

- 任務 W1.1：建立分區 inventory 與 snapshot manifest 產出。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/01_data_ingest.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/01_data_ingest.py)
  - Owner：資料工程/平台
  - 依賴：無
  - DoD：可輸出 `gaming_day` 分片清單（含月桶索引）與整體 fingerprint，並能標記來源 snapshot id。
- 任務 W1.2：建立重算集合決策器（新增/變更日期分片 + recent window 回補 + correction days/months）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py)
  - Owner：資料工程/平台
  - 依賴：W1.1
  - DoD：輸入 inventory 與 correction 清單，可產出 deterministic 重算日期分片集合。
- 任務 W1.3：配置與驗證 runtime profile（workstation 預設）與 spill 行為。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/config.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/config.py)
  - Owner：資料工程/平台
  - 依賴：無
  - DoD：在大型分區資料下可穩定跑完，不出現系統性 OOM。

### Iteration 2: Step 2b 解耦（全玩家 base + 玩家分群投影）

- 任務 W2.1：將 bet preprocess 拆成 `cleaned_bet_base_all_players`（不含 ADT 過濾）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/bet_l0_preprocess.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/bet_l0_preprocess.py)
  - Owner：特徵工程
  - 依賴：W1.2
  - DoD：可在不依賴 ADT threshold 的情況下輸出全玩家 cleaned bet，並採 `gaming_month=YYYYMM/gaming_day=YYYY-MM-DD/` 分片落地。
- 任務 W2.1b：加入 `gaming_day` non-null gate 與分片品質檢查。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/bet_l0_preprocess.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/bet_l0_preprocess.py)
  - Owner：特徵工程
  - 依賴：W2.1
  - DoD：若 `gaming_day` 為 null 立即 fail 並輸出具體錯誤統計；分片 row_count 與 manifest 對齊。
- 任務 W2.2：獨立 player membership 產物（依 ADT quantile/手選清單）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/patron_session_metrics.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/patron_session_metrics.py)
  - Owner：特徵工程
  - 依賴：W2.1b
  - DoD：membership 檔可版本化，且可回溯 quantile 與來源。
- 任務 W2.3：建立 segmented projection（base + membership 半連接）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py)
  - Owner：特徵工程
  - 依賴：W2.2
  - DoD：擴大 threshold 時可只新增 `Q_new - Q_old` 對應事件，不重算舊玩家結果。

### Iteration 3: slow features、feature group 快取與 Feast 分片重用

- 任務 W3.1：slow-varying features 增量物化（按 `gaming_day` 分片，月桶批次）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/slow_patron_180d_monthly.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/utils/slow_patron_180d_monthly.py)
  - Owner：特徵工程
  - 依賴：W2.3
  - DoD：非受影響日期分片可命中快取，整體耗時較全量重跑明顯下降。
- 任務 W3.2：實作 `date_slice × feature_group` 快取鍵與 manifest。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py)
  - Owner：ML 工程
  - 依賴：W3.1
  - DoD：可依 `feature_group_signature` 與 `data_snapshot_signature` 命中；僅變動群組/分片失效。
- 任務 W3.3：加入 lookback dirty 擴張規則（來源分片變更影響後續 anchors）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py)
  - Owner：ML 工程
  - 依賴：W3.2
  - DoD：變更單日來源時，只重算受 lookback 影響日期；結果與全量重跑一致。
- 任務 W3.4：Feast historical retrieval 分片重用（執行層月桶批次）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py)
  - Owner：ML 工程
  - 依賴：W3.3
  - DoD：可完成大規模資料抽取且不依賴單次全量 entity_df 載入，並可重用已命中分片。
- 任務 W3.5：training set 版本命名與最近 10 版保留。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/03_build_training_data.py)
  - Owner：ML 工程
  - 依賴：W3.4
  - DoD：每次產物具版本 id 與 retention 行為，保留策略生效可驗證。

### Iteration 4: 測試防線（Metamorphic + Guardrail）

- 任務 W4.1：Metamorphic test A（all_players vs subset_players 重疊不變性）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/tests/test_bet_preprocess.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/tests/test_bet_preprocess.py)
  - Owner：特徵工程 + QA
  - 依賴：W2.3, W3.1
  - DoD：重疊 bet_id 的特徵值在誤差容忍範圍內一致。
- 任務 W4.2：Metamorphic test B（threshold 擴大 delta 只新增新玩家）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/tests/test_bet_preprocess.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/tests/test_bet_preprocess.py)
  - Owner：特徵工程 + QA
  - 依賴：W2.3
  - DoD：`Q_new` 結果 = `Q_old` 穩定結果 + `Q_new - Q_old` 新增事件。
- 任務 W4.3：Guardrail test（禁止未審核 cohort-global normalization 模式）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/tests/unit/test_dq_guardrails.py](C:/Users/longp/Patron_Walkaway/tests/unit/test_dq_guardrails.py)
  - Owner：QA
  - 依賴：W4.1
  - DoD：偵測到未白名單的 percentile/rank/zscore 全域模式即 fail。

### Iteration 5: DVC stage 化與每次 run 報表

- 任務 W5.1：DVC stage 落地（ingest / clean_session / clean_bet_base / membership / segmented / features / training_set / publish）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier](C:/Users/longp/Patron_Walkaway/trainer_hightier)
  - Owner：資料工程/平台
  - 依賴：W3.5
  - DoD：各 stage 可獨立重跑且依賴正確。
- 任務 W5.2：每次 run 報表標準化輸出。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py](C:/Users/longp/Patron_Walkaway/trainer_hightier/trainer.py)
  - Owner：ML 工程
  - 依賴：W5.1
  - DoD：每次 run 產生固定欄位報表（耗時、命中率、重算分區、輸出列數、警告摘要）。
- 任務 W5.3：端到端回歸驗收（舊流程對照）。
  - 主要落點：[C:/Users/longp/Patron_Walkaway/trainer_hightier](C:/Users/longp/Patron_Walkaway/trainer_hightier)
  - Owner：全體（平台/特徵/ML）
  - 依賴：W5.2
  - DoD：與基準流程結果差異在可接受範圍，並達成效能改善目標。

## 依賴圖（高層）

```mermaid
flowchart LR
    W11[W1.1Inventory] --> W12[W1.2RecomputeSet]
    W12 --> W21[W2.1BetBasePartitioned]
    W21 --> W21b[W2.1bGamingDayGate]
    W21b --> W22[W2.2Membership]
    W22 --> W23[W2.3SegmentedProjection]
    W23 --> W31[W3.1SlowFeatureIncremental]
    W31 --> W32[W3.2ShardGroupCache]
    W32 --> W33[W3.3LookbackDirtyExpansion]
    W33 --> W34[W3.4FeastShardReuse]
    W34 --> W35[W3.5TrainingVersionRetention]
    W23 --> W42[W4.2ThresholdDeltaTest]
    W31 --> W41[W4.1OverlapInvarianceTest]
    W41 --> W43[W4.3GuardrailTest]
    W35 --> W51[W5.1DVCStages]
    W51 --> W52[W5.2RunReport]
    W52 --> W53[W5.3E2ERegression]
```



## 優先序與執行策略

- P0（必做先行）：W1.1, W1.2, W2.1, W2.1b, W2.2, W2.3。
- P1（核心效能）：W3.1, W3.2, W3.3, W3.4, W3.5。
- P1（風險防線）：W4.1, W4.2, W4.3。
- P2（治理固化）：W5.1, W5.2, W5.3。

## 阻塞條件與升級規則

- 若 W2.3 未達「threshold 擴大不重算舊玩家」DoD，禁止進入 W3.*。
- 若 W2.1b 未通過 `gaming_day` non-null gate，禁止推進 W2.2 之後任務。
- 若 W3.3 lookback dirty 擴張與全量對照不一致，禁止推進 W3.4/W3.5。
- 若 W4.* 任一 fail，禁止推進 W5.3 驗收。
- 若 run report 欄位不完整，禁止標記為正式 training set publish。

## 完成定義（Working plan 層）

- 交付完整分區增量資料管線，且能支援 ADT threshold 擴大時僅補新玩家。
- 交付完整 `gaming_day` 分片輸出（「日鍵 + 月桶」），並落實 `gaming_day` non-null 品質門檻。
- 建立 `date_slice × feature_group` 快取與 lookback dirty 擴張，支援「改日期窗 / 改 feature 組」局部重算。
- 建立 metamorphic + guardrail 測試防線，保障特徵不依賴未審核 cohort-global normalization。
- 建立每次 run 報表與可重現治理流程，並以對照驗證通過驗收。

