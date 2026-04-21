# Precision Uplift Field-Test Objective：建議事項與 ROI 排序

> **目的**：彙整「在既有 repo 架構下，為了把 precision uplift 路線先對齊 **field-test operating objective**，並同時維持 `precision@recall=1%` 主 KPI 治理」可嘗試的技術路線，並以 **ROI 權重（相對排序）** 表達優先序。  
> **範圍**：本文件為 **建議清單**，不取代下列 SSOT／計畫檔之治理優先序：  
> - `.cursor/plans/PLAN_precision_uplift_sprint.md`  
> - 既有 investigation implementation plan  
> - 既有 investigation execution plan  
> **非目標**：不改寫標籤定義（以 `trainer/labels.py` 與 `ssot/trainer_plan_ssot.md` 為準）、不討論「人工改標」；不討論放寬業務 KPI 或營運 policy（你已明確不可變）。

---

## 1. ROI 權重怎麼定義

本文件的 **ROI%** 為相對權重（加總約 **100%**），代表在「**不考慮算力成本**」前提下，仍納入下列因素的綜合排序：

- **預期對 field-test operating objective 的邊際貢獻**（是否能在可接受 alert volume 下直接拉高 precision）
- **與現有 `trainer/` 管線的相容性**（能否沿用三軌特徵、DEC-026 選阈、backtester／scorer 契約）
- **落地阻力**（需要改核心訓練迴路、需要 train–serve parity、需要新依賴或新線上元件者，權重會下修）
- **不確定性**（理論潛力大但驗證鏈長者，權重會下修）

> **誠實聲明**：這些百分比不是可保證的統計估計，而是 **工程優先序的量化表達**，用於排程與資源配置。

---

## 2. 總覽：依 ROI 排序的建議清單（約 100%）

| 排序 | ROI% | 建議事項 | 類型 | 與現況的關係（摘要） |
|:---:|:---:|:---|:---|:---|
| 1 | **16** | **將 Optuna／模型選擇目標對齊 field-test operating objective**（`min_alerts_per_hour >= 50` 下最大化 `prod-adjusted precision`；主 KPI 改作 guardrail） | 訓練目標／HPO | `DEC-026` 已有 recall floor / alert count / alert density 護欄，但 `trainer/training/trainer.py` 的 `run_optuna_search` 仍以 `average_precision_score` 為 objective；下一步應對齊到 **alert-volume-constrained、production-adjusted precision** |
| 2 | **14** | **排序導向訓練 + hard negative mining + top-band reweighting**（含 focal-like、對高分 FP 反覆加權／重採樣） | 訓練／取樣 | `PLAN_precision_uplift_sprint.md` Phase 2 已列；需在 `trainer` 主路徑落地並與 `sample_weight`、負採樣策略一致化 |
| 3 | **11** | **CatBoost / XGBoost 與 LightGBM 的公平 bakeoff** | 模型族 | `DEC-003` 僅鎖 **Phase 1** 為 LGBM+Optuna；衝刺階段可另立實驗矩陣比較演算法，不改寫既有 SSOT 的 Phase 1 定義亦可並行 |
| 4 | **10** | **二階段模型**：Stage-1 維持候選召回；Stage-2 僅在高分池訓練 **FP detector／reranker** | 架構 | 與 DEC-026「操作點在極端尾段」高度相容，但工程成本高；建議在 `#1`–`#3` 跑出多窗結論後再正式立項 |
| 5 | **9** | **離線序列嵌入（SSL）→ 向量作為 GBDT 特徵** | 特徵／表示學習 | `ssot/trainer_plan_ssot.md` 已規劃 Phase 2「離線預訓練嵌入 + 日更／批次特徵」路線；屬「開上限」型投入 |
| 6 | **8** | **分群建模 + learned gating**（2–4 experts + 輕量 gate） | 模型／路由 | `PLAN_precision_uplift_sprint.md` Phase 2/3 已列；比「純規則切桶」更可能吃到非線性路由紅利 |
| 7 | **7** | **接軌 `compute_table_hc` 與桌台／擁擠情境特徵**（在 S1 護欄下） | 特徵 | `trainer/features/features.py` 已實作 `compute_table_hc`；但 `trainer/training/trainer.py` / `trainer/serving/scorer.py` 尚未主路徑接線——屬 near-ready，而非免費特徵 |
| 8 | **6** | **擴張 Track LLM／Track Human 候選特徵**（節奏變化率、regime shift、短中長窗對照、跨桌行為等） | 特徵 | `trainer/feature_spec/features_candidates.yaml` 已有良好骨架；屬低耦合擴張 |
| 9 | **5** | **修正 canonical mapping 的 D3（PIT-correct mapping）** | 身分／資料語意 | `trainer/identity.py` 明載 D3：整窗 mapping 可能讓早期觀測「看到」較晚才形成的連結；若影響高分段，屬高槓桿但偏「基礎修復」 |
| 10 | **5** | **真多窗 Phase 2 矩陣 + gate**（拒絕單窗幻覺 uplift） | 評估／治理 | 既有 implementation plan 的 W2-C 仍待「非 bridge」多窗；屬加速淘汰假 winner |
| 11 | **4** | **Profile 依 history depth／完整度分 bundle**（與 `min_lookback_days`、DEC-017 精神一致） | 特徵／資料 | Feature spec 已支援 `min_lookback_days`；可再上升到「分段模型或分段特徵子集」 |
| 12 | **3** | **完成 DEC-032 線上校準閉環**（支援 `DEC-026 field_test mode`，以 mature labels 重估 runtime threshold） | 部署／閾值 | `CONSOLIDATED_PLAN_PRECISION_AND_SPEED.md` Step 2；`trainer/scripts/calibrate_threshold_from_prediction_log.py` 目前為 MVP（手動 upsert），**對 offline 排序上限幫助有限**，但對 production 的 alert volume / precision 一致性重要，且是 `#1` 中 `prod_adjusted precision` proxy 可信度的重要支撐 |
| 13 | **2** | **Stacking／blending／ensemble** | 集成 | 僅在「多基模型錯誤型態可互補」後再做；避免早期複雜度膨脹 |

### 2.1 總覽各項詳細說明（做什麼、為何與 field-test objective / 主 KPI 有關）

以下編號與上表「排序」欄一致。

**#1 將 Optuna／模型選擇目標對齊 field-test operating objective**

- **做什麼**：在超參搜尋（與可選的 early stopping／best trial 選擇）中，把「要最大化的標量」從整體 **AP（average precision）** 改為與 field test 更一致的 operating objective：在 rated-eligible 樣本上，於 **`min_alerts_per_hour >= 50`**（及既有最小 alert guards）成立的候選 threshold 中最大化 precision；若 validation / test 存在 negative sampling，優先使用 **production-adjusted precision proxy** 排序。`precision@recall=1%` 與 `precision@recall=1%_prod_adjusted` 保留為 **guardrail / shadow metrics**，而非唯一 driver。若 constrained objective 噪音過大，再考慮 **複合分數**（例如 `α·field_test_precision + (1−α)·AP`）。  
- **為何有用**：field test 真正可觀測的是 **alerts/hour** 與 **precision**，不是離線報表上的 raw recall；若目前訓練/驗證使用 negative sampling，`prod_adjusted precision` 也比 raw precision 更接近現場體感。對齊後，HPO 選出的樹結構／正則化會更貼近「在可接受 alert volume 下，把 precision 拉高」的真實操作點。  
- **典型改動面**：`trainer/training/trainer.py` 的 `run_optuna_search` objective、winner-pick/early stopping 語意、以及 `trainer/training/threshold_selection.py` 的 field-test mode；需與 backtester／scorer 的 shared contract 一致。  
- **注意**：validation 窗若太短、可行的 `>=50 alerts/hour` operating points 太少，或 `PRODUCTION_NEG_POS_RATIO` 假設不穩，單獨優化 field-test objective 仍可能高噪音；建議先做 precondition check，至少統計 **各 fold 的 walkaway 正例數／finalized TP 數量級、總 rated bet 數與 `fold_duration_hours`、baseline 模型的可行 threshold 集合大小，以及 test neg/pos ratio（若有負採樣）**。另外，若某 fold 的 `T_feasible` 為空，應明確視為 **fallback fold**，其 objective score 不應假裝可比較；實作上可採 **score=0.0 + 明確標記 fallback / feasible=false**，並與 shared selector 的 fallback semantics 對齊。若支撐不足，複合目標或 **rolling／多折時序驗證上的平均 constrained score** 較妥。

**#2 排序導向訓練 + hard negative mining + top-band reweighting**

- **做什麼**：在 **不改 label** 的前提下，調整訓練時模型「在乎哪些樣本」：  
  - **排序導向**：讓 loss 更懲罰「分數排序錯位」（例如正例應高於大量負例，尤其在分數頂部）。  
  - **Hard negative mining**：週期性或依分數帶，從 **模型當前給高分但 label=0** 的列加大權重或重複抽樣進下一輪訓練。  
  - **Top-band reweighting**：對接近或進入 alert 分位帶的樣本提高 `sample_weight`（與既有 `compute_sample_weights` 的 run-level 權重可合併設計）。  
- **為何有用**：field-test objective 的核心瓶頸，幾乎永遠是「頂部分數裡混了太多 FP」；這類方法直接逼模型在 **高分區** 學會區分真假。  
- **典型改動面**：LightGBM 的 `sample_weight`／自訂 objective（若走客製梯度）、或訓練迴圈外的 **多輪 mining 子迴圈**；需記錄 mining 版本以免不可重現。  
- **注意**：過度採 hard negative 可能傷整體 recall；評估時至少要在 **同一 field-test operating contract** 下比較，並旁看主 KPI guardrail 是否被明顯破壞。

**#3 CatBoost / XGBoost 與 LightGBM 的公平 bakeoff**

- **做什麼**：在 **同一特徵矩陣、同一時間切分、同一評估腳本** 下，平行訓練 **LightGBM、CatBoost、XGBoost（hist）** 等表格模型，以 **field-test objective** 為第一排序鍵，並以主 KPI 與多窗穩定性作治理 / guardrail 比較勝者。  
- **為何有用**：不同 GBDT 實作對 **類別／缺失值／高階交互** 的處理不同，在「特徵已固定」時仍常有 **數個百分點** 級差異；成本低於大改特徵或上序列模型。  
- **典型改動面**：新增可選 trainer 分支或獨立實驗腳本、依賴項（`catboost`、`xgboost`）、artifact 格式（仍建議輸出與現有 `model.pkl` + metrics 契約相容的包）。  
- **注意**：這裡的「挑出 winner」是 **phase-level 的收斂策略**（先確認單模上限與可比性），**不是**最終架構教條。`DEC-003` 約束的是 **Phase 1 產品預設**；衝刺實驗可並行，但若某演算法勝出要變成 **預設上線演算法**，需走決策／SSOT 更新，避免與文件衝突。

**#4 二階段模型（Stage-1 + Stage-2 FP detector／reranker）**

- **做什麼**：  
  - **Stage-1**：維持現有（或 #1–#3 優化後）模型，負責產生 **候選高分區**（例如分數前 k% 或超過某鬆阈）。  
  - **Stage-2**：只在 Stage-1 候選集合上訓練一個 **輕量二類器**（或學習 rerank 分數），專門區分「Stage-1 高分中的 TP vs FP」；線上最終分數可為兩階段分數的組合（例如乘積、logit 相加、或僅用 Stage-2 在候選內重排序）。  
- **為何有用**：把模型容量與梯度集中在 **最影響高分帶 precision / top-band 純度的子母體**，常比「單一模型全域調參」更有效。  
- **典型改動面**：離線訓練管線（產出兩組 artifact）、`trainer/serving/scorer.py` 或 model service 的 **兩次 inference**、特徵在第二階是否允許額外欄位（需與 feature spec 一致）。  
- **注意**：這條路線的價值不應僅因「切片間沒有極端差異」就被否定，但其工程成本確實高；較合理的時序是先完成 `#1`–`#3`，確認 **top-band FP** 仍是主瓶頸，再進 Stage-2。另需嚴格 **PIT／特徵時間可得性**；Stage-2 訓練集必須用 **Stage-1 在該時點可得的分數** 構造，避免用未來模型重算分數造成洩漏。

**#5 離線序列嵌入（SSL）→ 向量作為 GBDT 特徵**

- **做什麼**：以 `canonical_id`（或 run）為單位，將最近一段下注序列送入 **離線預訓練**（自監督或對比學習），得到每筆 bet 或每位玩家在某時刻的 **固定維度 embedding**；再 **join 回 bet 列** 當作 Track 的一類特徵，主分類器仍用 GBDT。  
- **為何有用**：現有 Track LLM 多為 **窗口聚合與 lag**，對「順序與節奏的細微差異」表達力仍有限；嵌入常是突破 aggregate 天花板的手段之一。  
- **典型改動面**：新訓練 job、embedding 表或 parquet、版本欄位、`feature_list.json`／YAML 註冊；與 `trainer_plan_ssot` 所述 **日更／批次更新** 節奏對齊。  
- **注意**：線上與離線 **embedding 版本** 必須與 `model_version` 綁定；冷啟動與缺歷史列需有明確 fallback（零向量或缺失策略）。

**#6 分群建模 + learned gating（2–4 experts + gate）**

- **做什麼**：訓練 **多個專家模型**（可依行為強度、tenure、或無監督分群初始化），再訓練一個 **gate**（淺層網路或小型 GBDT）輸入原始特徵（或少量路由特徵），輸出 **對各 expert 的權重**；推論時為加權分數或選最大 expert。  
- **為何有用**：切片分析若顯示「沒有單一群特別好，但也不一樣爛」，固定規則分桶可能欠擬合；**學習式路由**可讓不同區域用不同決策邊界。  
- **典型改動面**：多組 `model.pkl` 或單包內多 booster、gate 參數、推論路徑與監控（每群 error 分布）。  
- **注意**：群數一多資料變薄易過擬合；建議從 **2 experts** 起，且必須多窗驗證 gate 是否穩定。

**#7 接軌 `compute_table_hc` 與桌台／擁擠情境特徵**

- **做什麼**：對每筆 bet，在 **可用時間** 內、於同一 `table_id` 上計算 rolling window 內 **不重複玩家數**（head count）等；已存在 `trainer/features/features.py::compute_table_hc`，需 **併入訓練與 scorer 同一套特徵管線**（含 YAML、`add_track_human_features` 或等效 hook、feature screening）。  
- **為何有用**：離場行為可能與 **同桌擁擠、桌況、社交壓力** 相關；若訊號存在，這是「低垂果實」型擴充。  
- **典型改動面**：Human track 或獨立欄位、S1／可用時間護欄與 `BET_AVAIL_DELAY_MIN` 一致。  
- **注意**：這不是「白拿」特徵。除了跨玩家聚合需嚴格 cutoff、與 SSOT 對 **session／table 特徵延遲** 的敘述一致以避免洩漏外，還要實測 **train–serve parity、延遲、CH / DuckDB 負載與記憶體占用**。

**#8 擴張 Track LLM／Track Human 候選特徵**

- **做什麼**：在 `trainer/feature_spec/features_candidates.yaml` 增加候選特徵（例如：節奏一階／二階差分、win/loss 轉折標記、短窗對長窗比值、連續 PUSH 後首個 LOSE 的間隔等），跑既有 **screen_features** 管線篩進 `feature_list.json`。  
- **為何有用**：在不改架構下擴大 **假設空間**；許多 uplift 來自「特徵沒覆蓋到的行為模式」而非單純加深樹深。  
- **典型改動面**：YAML、`compute_track_llm_features` 產 SQL、spec hash、訓練與 scorer 共用 DuckDB SQL。  
- **注意**：候選爆量時篩選與冗餘剔除要控管；避免無效高相關欄位占滿 `SCREEN_FEATURES_TOP_K`。

**#9 修正 canonical mapping 的 D3（PIT-correct mapping）**

- **做什麼**：將「以 **整段訓練窗末端 cutoff** 建一次 `player_id→canonical_id` map」改為 **對每個觀測時點或每個 chunk 末端** 僅使用 **已可得 session 連結** 建 map（或採 rolling／增量 map），使早期 bet **不會**用到未來才出現的連卡資訊。  
- **為何有用**：D3 會污染 **歷史 profile 與序列邊界** 的語意，可能在高分段製造「假可分」或「假不可分」；修正後訓練分佈更接近線上。  
- **典型改動面**：`trainer/identity.py` 與 `trainer/training/trainer.py` 建 map 的呼叫點、快取鍵語意、與 scorer 側 map 刷新策略是否一致。  
- **注意**：工程量大；建議先做 **小窗 ablation**，同時量測對 field-test objective、主 KPI 與整體 AP 的影響，再決定是否全量替換。

**#10 真多窗 Phase 2 矩陣 + gate**

- **做什麼**：對同一組實驗（baseline／challenger／不同 track）在 **多個不重疊或 purged 的時間窗** 重複訓練＋回測，輸出每窗 **field-test objective、主 KPI 與 uplift**，並以 **均值／標準差／最差窗** 作 gate（與 Implementation Plan W2-C 方向一致）。  
- **為何有用**：不直接「變強模型」，但避免把資源砸在 **單窗過擬合** 的配置；對衝 0.6 這種硬目標，**證據品質**與模型一樣重要。  
- **典型改動面**：orchestrator、`run_pipeline.py --phase phase2` 的 bundle 與 collector、報表 schema。  
- **注意**：與「bridge 單點序列」區分；文件已要求最終要 **非 bridge** 的真多窗證據。

**#11 Profile 依 history depth／完整度分 bundle**

- **做什麼**：依玩家 **可觀測歷史長度**（或 profile 缺失率、`min_lookback_days` 可算與否）將樣本分層，對各層使用 **不同特徵子集** 或 **不同模型**（例如短歷史戶少依賴長窗 profile 欄）。  
- **為何有用**：避免「短歷史卻塞滿長窗 profile 的零／預設值」稀釋學習訊號；與 DEC-017「依 horizon 裁剪」精神一致但再往前一步。  
- **典型改動面**：特徵屏蔽規則、`get_profile_feature_cols` 類邏輯、或訓練時 column mask。  
- **注意**：bundle 分太碎會樣本不足；需監控每 bundle 的主 KPI、field-test objective 與列數。

**#12 完成 DEC-032 線上校準閉環**

- **做什麼**：週期性從 ClickHouse 取成熟標籤，寫入 `prediction_log` 附屬 schema，對 **成熟列** 依 `DEC-026 field_test mode` 重算 operating threshold：以 **`min_alerts_per_hour >= 50`**、`min_alert_count` 等 guards 篩選候選點，再優先選擇 **`prod_adjusted precision`** 最佳的 threshold；在通過 **TTL／最小樣本／審核閘門** 後 upsert `runtime_rated_threshold`。  
- **為何有用**：主要解決 **分佈漂移導致 alerts/hour 與可觀測 precision 偏離現場目標** 的問題；對「同一模型、離線排序沒變，但線上 alert volume 或 precision 走樣」特別有用。  
- **典型改動面**：`trainer/scripts/calibrate_threshold_from_prediction_log.py` 擴充、排程、審計表 `calibration_runs`，以及 calibration 報表需同時輸出 `precision_raw`、`precision_prod_adjusted`、`recall`、`alerts_per_hour`。  
- **注意**：這條路 **不會**把離線排序上限從 0.4 直接魔改成 0.6；它修的是 operating point，不是排序能力。若瓶頸在排序品質，仍要搭配 #1–#4。另一方面，雖然本項 ROI 排序不變，但它不應被解讀成「可無限延後」：若 `#1` 要依賴 `prod_adjusted precision` 作 proxy，則 DEC-032 / prediction log 所提供的 production prior 與 mature-label calibration 會成為其可信度的重要支撐。另因目前 calibration script 仍是 MVP，文件解讀上應區分「方向正確」與「已完全到位」。

**#13 Stacking／blending／ensemble**

- **做什麼**：以 **out-of-fold** 或時間切分產生多個基模型的預測機率，再訓練 **meta-model**（常為 logistic 或淺 GBDT）或非線性 blend；推論時多模型前向再融合。  
- **為何有用**：當 LGBM、CatBoost、XGB、或 Stage-2 的 **錯誤型態互補** 時，集成常能再榨 1–數 pp。  
- **典型改動面**：訓練編排、artifact、線上延遲與版本治理。  
- **注意**：若基模型高度相關，增益接近零但複雜度倍增；因此 ensemble 應視為 **升級條件成立後** 的路線，而非 bakeoff 的預設終點。建議在 #1–#4 與多窗驗證後再做，並設 **增益門檻**（含離線 uplift、線上延遲 / 記憶體成本）才允許上線。

---

## 3. 建議採用的三層執行視角（避免混層）

### 3.1 第一層：主戰場（最可能直接拉高 field-test precision，並帶動主 KPI）

1. HPO／early stopping／winner pick **對齊 field-test objective**  
2. 排序導向 + hard negative + top-band weighting  
3. CatBoost / XGBoost bakeoff  
4. 二階段 reranker／FP detector  

### 3.2 第二層：開上限（需要較長驗證鏈，但潛在報酬高）

5. 離線序列嵌入特徵  
6. 分群 + learned gating  
7. `table_hc` 與桌況上下文擴張  
8. Track LLM／Human 特徵空間擴張  
9. PIT-correct identity mapping  

### 3.3 第三層：守門與產品化（避免「離線漂亮、線上崩」）

10. 真多窗證據鏈與 gate  
11. Profile bundle 策略  
12. Online calibration 閉環  
13. Ensemble（最後才上）  

---

## 4. 與「已寫在計畫裡但尚未完全落地」的對照（方便你勾稽）

| 計畫／決策來源 | 本清單對應項 | 備註 |
|:---|:---|:---|
| `PLAN_precision_uplift_sprint.md` Phase 2（排序加權、HN、分群+gating、F/Purged CV） | #2、#6、#10 | Phase 2 MVP 可跑，但 **W2-C 真多窗**等仍在補齊（見 Implementation Plan） |
| `ssot/trainer_plan_ssot.md` Phase 2 離線嵌入 | #5 | 屬「開上限」路線，與現行 GBDT 主幹可漸進整合 |
| `DEC-026` / `trainer/training/threshold_selection.py` | #1、#12 | 現有 shared selector 已有 recall / alert guards；下一步需明確擴充 **field-test mode**，讓訓練目標與 `>=50 alerts/hour` 下的 operating point 同向（見 #1） |
| `DEC-032` / `CONSOLIDATED_PLAN_PRECISION_AND_SPEED.md` Step 2 | #12 | Scorer 已能讀 `runtime_rated_threshold`；自動校準主鏈仍待收斂 |
| `trainer/identity.py` D3 | #9 | 屬已知限制；是否為主因需用多窗與 ablation 證明 |

---

## 5. 風險與停損（建議寫進每次實驗矩陣）

- **單窗顯著 uplift 但多窗崩**：直接降級該路線，不進 Phase 3。  
- **HPO 改成直接優化 field-test objective 後，若 `>=50 alerts/hour` 的可行 operating points 在 validation folds 中過少或波動異常**：先不要讓 Optuna 在雜訊上打轉；改採複合目標、fold 平均分數，或先擴窗再評估。  
- **離線 field-test objective 改善，但線上 alerts/hour、可觀測 precision 或主 KPI guardrail 不一致**：先回到 `CONSOLIDATED_PLAN...` 的「量測對齊」步驟，避免 metric mismatch。  
- **特徵／嵌入導入後 train–serve parity 失敗**：依 `DEC-031` 精神應 fail-fast，避免 silent degrade。  
- **二階段或 gating 讓複雜度暴增但增益 < 約 3–5pp**：依衝刺計畫止損規則降級投入。

---

## 6. 假設、暫定決策與未決問題（需要你們內部對齊）

**假設**

- 主 KPI 仍是 **`precision@recall=1%`**，且評估口徑與 `trainer/training/backtester.py` 的 DEC-026 oracle 一致；但 field test 的 operating objective 可暫時優先對齊 **`min_alerts_per_hour >= 50` 下的 precision**。  
- 標籤仍以 `trainer/labels.py` 為準，**不靠人工改標**來「製造」提升。

**暫定決策**

- 在進入 implementation / execution 拆解前，暫定將 field-test objective 定義為：**`alerts_per_hour >= 50`（長期平均）作 feasibility constraint，`prod_adjusted precision` 作優化對象，`precision@recall=1%` 與 `precision@recall=1%_prod_adjusted` 降為 soft guardrail / shadow metrics**。  
- 若後續 precondition check 顯示 constrained objective 支撐不足（例如多數 folds 的 `T_feasible` 過小或常為空），則不得硬切單一 objective，需退回複合目標或調整驗證窗設計。

**未決問題**

- D3 identity 限制對高分段的實際影響量級（需 ablation 與多窗證據）。  
- `table_hc` 接線後對 **延遲／CH 負載／特徵計算成本** 的影響（即使你不限制算力，線上 SLA 仍可能存在）。

---

## 7. 文件維護

- **Owner**：建議由負責 precision uplift 的 DS／ML platform 維護。  
- **更新時機**：每完成一輪「主戰場」實驗後，更新各項 ROI 的**相對排序**與「已證實／已否定」註記。  
- **不要與 SSOT 打架**：若本文件建議牽涉到切片契約、gate 或 label 語意變更，應先改 `PLAN_precision_uplift_sprint.md` 與對應 Implementation Plan，再更新本檔敘述。

---

*建立日期：2026-04-21*
