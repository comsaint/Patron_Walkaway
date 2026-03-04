# Decision Log — Patron Walkaway Predictor

本文件記錄本專案在設計與規劃過程中做出的**關鍵架構與策略決策**，包含背景、考慮過的替代方案、最終選擇及其理由。供未來計畫更新、Spec Review 或團隊 onboarding 時參考。

> **格式**：每條決策以 `DEC-XXX` 編號，記錄日期、決策內容、理由、相關 SSOT 章節。

---

## DEC-001：特徵工程採用「雙軌架構」而非純 Featuretools 或純手工

**日期**：2026-02-28  
**SSOT 章節**：§8.2  
**決策**：特徵工程分為兩軌並行——**軌道 A（Featuretools DFS）** 負責系統性探索聚合/窗口/組合特徵；**軌道 B（手寫向量化特徵）** 負責狀態機類特徵（`loss_streak`、`run_boundary`、`table_hc`）。

**考慮過的替代方案**：
1. **純 Featuretools（含 Custom Primitives）**：一度是首選。但經深入評估，`loss_streak`（WIN 重置、PUSH 條件不重置）和 `table_hc`（跨玩家即時聚合）的 Custom Primitive 實作要麼邏輯極度複雜，要麼觸發全桌掃描導致效能不可接受。
2. **純手工特徵（Pandas/Polars）**：等於回到「人工列舉特徵清單」的舊路，放棄了自動化探索的優勢，且 SSOT 過去的特徵清單不代表最佳或最終特徵集。
3. **Featuretools 探索 → Polars 手寫生產**：一度提出的方案。但這引入了 **train-serve parity 風險**——探索階段和生產階段用不同的程式碼計算同一批特徵，微妙的語義差異（邊界條件、NULL 處理）可能導致不一致。

**最終理由**：
- 雙軌各司其職：Featuretools 擅長自動展開聚合組合空間，手寫程式碼擅長狀態機。
- 軌道 A 內部使用 `save_features` / `calculate_feature_matrix` 確保 parity；軌道 B 的函數抽取至共用 `features.py`，由 trainer 與 scorer 共同匯入。
- 兩軌共用同一個 `cutoff_time` 框架，防洩漏機制統一。

---

## DEC-002：Featuretools 採用「兩階段 DFS」流程（探索 → 生產）

**日期**：2026-02-28  
**SSOT 章節**：§8.2（軌道 A 第 4 點）  
**決策**：
- **第一階段（探索）**：在多數類下採樣後的月度資料上跑完整 DFS + Feature screening，選出高潛力特徵，以 `featuretools.save_features()` 持久化特徵定義。
- **第二階段（生產）**：以 `featuretools.calculate_feature_matrix()` 載入相同的特徵定義，逐月計算全量資料。

**背景**：
- 單機硬體（AMD Ryzen 9 9900X, 64GB RAM）無法對全量 4.38 億筆做一次性 DFS。
- 下採樣後的探索集每月約幾百萬筆，可在合理時間內完成 DFS。
- 生產階段**不做 DFS 搜索**，僅按已存的定義計算，計算量可控。

**核心理由**：
- **消除 parity 風險**：探索與生產使用同一份 `saved_feature_defs`，不存在「探索時用 Featuretools、生產時改寫 Polars」的語義偏差問題。
- **可重現性**：特徵定義是 JSON/pickle 格式，可版本化管理。

---

## DEC-003：Phase 1 不引入 FLAML/AutoML，鎖定 LightGBM + Optuna

**日期**：2026-02-28  
**SSOT 章節**：§9.2, §14  
**決策**：Phase 1 鎖定 LightGBM 單一演算法，使用 Optuna（TPE Sampler）進行超參調優與雙模型閾值搜索。FLAML 延至 Phase 2+ 用於集成探索。

**考慮過的替代方案**：
1. **Phase 1 就用 FLAML**：可自動搜索多種演算法並建集成模型。但引入額外依賴（FLAML 的 `task="classification"` pipeline），增加 debug 與可解釋性複雜度。
2. **AutoGluon**：功能更完整（自動處理 ensemble + stacking），但偏重深度學習，對單機 CPU 環境不友好，且黑箱程度更高。

**最終理由**：
- **速度**：目標是「盡快出結果」。LightGBM 是已知最快的 GBDT 實作，Optuna TPE 搜索效率遠高於 grid search。
- **可解釋性**：Host 團隊需要理解「為何觸發警報」。LightGBM + SHAP 是最成熟的解釋方案，不需要額外的解釋工具鏈。
- **最小依賴**：Phase 1 的關鍵依賴為 `featuretools` + `lightgbm` + `optuna`，不再引入 FLAML。
- **Optuna 已在計畫中**：閾值搜索（§10.2 I6）已決定採用 Optuna，擴展到超參調優是自然延伸，不增加新依賴。
- **FLAML 保留為 Phase 2+**：當 Phase 1 模型穩定上線且有基準指標後，FLAML 可以在此基礎上探索 stacking/blending，此時有足夠的對照基線來判斷集成是否值得。

---

## DEC-004：SSOT 特徵清單定位為「起點」而非「規範」

**日期**：2026-02-28  
**SSOT 章節**：§8.2  
**決策**：SSOT 過去列出的 A–E 類特徵（如 `bets_last_5m`, `avg_bet_size`, `loss_streak` 等）僅反映「過去曾經嘗試的特徵」，**不代表最終或最佳特徵集**。新架構的目標是讓 Featuretools DFS 去系統性發現人腦難以窮舉的聚合模式。

**背景**：
- 過去的特徵清單是在第一代 trainer 中手工設計的，受限於開發者的領域直覺。
- 用戶明確指出：「these features are by no means good or final, we should never restrict ourselves by them.」

**最終理由**：
- 人工列舉特徵的搜索空間有限，容易遺漏有價值的組合（例如「過去 N 把中，連續 PUSH 後第一次 LOSE 的間隔」）。
- Featuretools DFS 可以系統性地搜索多深度、多窗口的聚合組合，再透過 Feature screening 篩選。
- 過去的特徵仍可作為 baseline / sanity check，但不應限制探索空間。

---

## DEC-005：時間窗口化採「月度」粒度

**日期**：2026-02-28  
**SSOT 章節**：§4.3  
**決策**：時間窗口以**月**為單位（~2,300 萬筆 bet/月），而非週。

**考慮過的替代方案**：
- **週**：更細粒度，但增加 19 → ~80 個窗口，I/O 與管理開銷大幅增加。
- **季度**：~4–5 個窗口，記憶體可能仍然緊張（每季 ~7,000 萬筆）。

**最終理由**：
- 月度每窗 ~2,300 萬筆，下採樣後幾百萬筆，在 64GB RAM 環境下可處理。
- 19 個月 → ~19 個窗口，管理成本適中。
- 與 C1 延伸拉取（至少 X+Y = 45 min，推薦 1 天）的邊界重疊管理簡單。
- ClickHouse partition 通常以 `gaming_day` 為單位，月度查詢可自然對齊。

---

## DEC-006：Optuna 用於雙模型閾值搜索（取代 Grid Search）

**日期**：2026-02-28  
**SSOT 章節**：§10.2  
**決策**：使用 Optuna TPE Sampler 在 2D 空間（rated_threshold, nonrated_threshold）搜索最佳閾值組合，取代原先規劃的 99×99 grid search。

**背景**：
- 原先 I4 提出的 grid search 需 9,801 次評估，每次需計算兩個模型的 precision/recall/alert volume，耗時且不必要。
- Optuna TPE 可在 ~50–200 次 trial 內收斂到接近最優解。

**最終理由**：
- 計算效率：試驗次數從 ~10,000 降至 ~100–200。
- 已在計畫中：超參調優也用 Optuna，無額外依賴。
- 約束式最佳化：Optuna 原生支援 `study.optimize()` + pruning，可自然表達 G1 的 precision 下限與最小警報量約束。

---

## DEC-007：`t_bet` 與 `t_session` 的 EntitySet 關係鍵為 `session_id`

**日期**：2026-02-28  
**SSOT 章節**：§8.2（軌道 A 第 1 點）  
**決策**：EntitySet 中 `t_bet` → `t_session` 的父子關係以 **`session_id`** 為唯一連結鍵（many-to-one），而非 `table_id` 或 `canonical_id`。

**背景**：
- `table_id` 雖為兩表共有欄位，但語義為「桌台 ID」，一張桌台有多個 session 和大量 bet，不構成有意義的 EntitySet 父子關係。
- `canonical_id` 是身份歸戶後的衍生鍵，不存在於原始表中。
- 經 schema 確認，`t_bet.session_id` 與 `t_session.session_id` 是原始表的直接 FK 關係。

---

## DEC-008：建立統一的「時間折疊與窗口定義器 (Time Fold Splitter)」

**日期**：2026-02-28  
**SSOT 章節**：§4.3  
**決策**：必須實作一個集中式的 `Time Fold Splitter / Window Definer` 模組，來統一核發所有月度資料拉取的邊界、C1 延伸拉取緩衝區、以及 Train/Valid/Test 的 cutoff 點。

**背景與理由**：
- 本專案涉及高度時間依賴的資料，任何步驟（ETL 抽資料、標籤計算、特徵 cutoff、模型切分）若各自手寫時間過濾邏輯（例如 `where date >= 'X'`），極易引入 off-by-one errors (差一錯誤) 或未來資料洩漏。
- 透過單一工具發放所有時間邊界，能保證整條 pipeline 的時間語義完全一致。

---

## DEC-009：Trainer 閾值與主指標改為 PR-AUC + F1（簡化，可回退）

**日期**：2026-03-02  
**SSOT 章節**：§10.2（閾值策略）  

**決策**：Trainer 內單模型閾值選擇改為以 **PR-AUC 為主要報告指標**，閾值則以 **F1** 最大化選出；**不再使用 G1 約束**（F-beta、G1_PRECISION_MIN、fallback 邏輯）。

**背景**：  
- 原為 G1 對齊：在滿足 precision ≥ G1_PRECISION_MIN 下最大化 F-beta (β=0.5)，邏輯與 fallback 較複雜。  
- 需求為簡化指標，以 PR-ROC / PR-AUC 為主。

**實作要點**：  
- **主指標**：`val_prauc`（PR-AUC）為首要報告與日誌指標。  
- **閾值選擇**：在 validation 上掃描閾值，取 **F1** 最大者（無 G1 約束）；若 F1 平手則取 recall 較高者。  
- **日誌**：`PR-AUC=...  F1=...  prec=...  rec=...  thr=...`  
- **Backtester**：仍保留 G1 閾值搜尋（Optuna 2D + F-beta + 約束），未在此次變更範圍；若日後希望一致可再改。

**回退說明**：  
若未來需恢復 G1 策略，可還原 `trainer/trainer.py` 中 `_train_one_model` 的閾值區塊為：  
- 目標：最大化 F-beta (G1_FBETA)，約束：precision ≥ G1_PRECISION_MIN；  
- Fallback：無可行解時改取最佳 F-beta 並打 warning。  
- 並將 `f1_score` 改回 `fbeta_score`，metrics 與 log 恢復 `val_fbeta_{G1_FBETA}` 與對應格式。

---

## DEC-010：Backtester Optuna 閾值搜尋改為 F1（與 Trainer 對齊，移除 G1 約束）

**日期**：2026-03-02  
**SSOT 章節**：§10.2  
**關聯**：DEC-009  

**決策**：Backtester 的 Optuna 2D 閾值搜尋 **objective** 從 F-beta (G1_FBETA) 改為 **F1**，與 Trainer 的閾值選擇準則一致。**不再使用 G1 約束**（per-model precision、combined alerts/hour 下限均移除），與 SSOT §10.2 對齊。

**理由**：  
- DEC-009 將 Trainer 閾值改為 F1，且移除 G1 約束。  
- Backtester 與 Trainer 閾值選擇邏輯應完全一致。  
- 具體權衡底線尚待業務端確認，暫不預設 precision/alert volume 門檻。

**實作**：  
- `run_optuna_threshold_search` 的 objective：`fbeta_score(..., beta=G1_FBETA)` → `f1_score(...)`  
- 移除 G1 約束檢查（`rated_prec >= G1_PRECISION_MIN`、`nonrated_prec >= G1_PRECISION_MIN`、`total_alerts_per_hour >= G1_ALERT_VOLUME_MIN_PER_HOUR`）。  
- 日誌：`F-beta=...` → `F1=...`  
- `compute_micro_metrics` / `compute_macro_by_visit_metrics` 仍報告 `fbeta_{G1_FBETA}` 供參考，未改動。

**回退**：恢復 G1 約束檢查與 `fbeta_score` objective；見 DEC-009 回退說明。

---

## DEC-011：建立 Cached Player-Level 彙總表

**日期**：2026-03-02  
**SSOT 章節**：待補充（Phase 1 後延伸）  
**關聯**：`doc/FINDINGS.md` — Session History Distribution

**決策**：進行 **cached player-level 彙總表** 的設計與實作，以支援 rated model 的 patron profile 特徵，避免每次 chunk 從 session 表反覆彙總。

**背景**：  
- Rated model 的 patron profile 目前僅依賴 chunk ±1 天內的 sessions，歷史範圍過短。  
- 全量 session 表（`gmwds_t_session.parquet`）DuckDB 全掃描分析顯示：
  - Rated patrons 共 332,813 人，平均每人 151.7 sessions、105.8 天歷史。  
  - 79.3% rated 有 ≥5 sessions；44.3% 有 ≥30 天 history；26.3% 有 ≥180 天 history。

**理由**：  
- 多數 rated patrons 具備足夠的歷史深度，建立 player-level 彙總可避免從 7,100 萬筆 sessions 反覆計算。  
- 預期設計：每日 snapshot + PIT，訓練/推論時按時間點取對應 snapshot，再與 chunk 內增量 sessions 合併。

**待辦**：  
- 具體 schema、更新頻率、PIT 策略：已補入 **`doc/player_profile_daily_spec.md`**（含欄位清單、計算公式、來源表、Phase 1/2 範圍、特徵取捨理由）。

---

## DEC-012：Phase 1 簡化為 Bet-level 評估指標，Run-level / Cooldown 延後

**日期**：2026-03-02  
**SSOT 章節**：§10.1, §10.2, §10.3, §14  

**決策**：Phase 1 **統一採用 Bet-level 指標**（Precision, Recall, F-beta, PR-AUC）進行閾值選擇與回測報告；不引入 Run-level 聚合（R-Precision, R-Recall, R-Volume）或 Per-player 警報冷卻期（Cooldown）。

**背景**：  
- Run-level 指標與 Cooldown 設計需反映公關真實體驗（如成功挽留多少獨立玩家、同一 run 內多次假警報的懲罰），涉及業務語義與 KPI 協商。  
- 目前尚無明確業務輸入可定義具體 metric 與門檻，為保持簡單與可落地，先以 bet-level 為主。

**Future TODO**：  
- 待業務校準後，考慮引入：  
  - Run-level 聚合指標（Run 定義見 SSOT §2.2）  
  - Per-player 警報冷卻期（避免同一 run 內多次假警報打擾公關）  
- 具體 metric 定義、Run 語義（如同一 run 內多 FP 如何計數）、閾值策略需與業務端協商後補入 SSOT。

---

## DEC-013：術語統一為 Run，採用 Run-level 樣本加權

**日期**：2026-03-02  
**SSOT 章節**：§2.2, §9  

**決策**：
1. **術語統一**：全專案統一使用 **Run**（bet-derived 連續下注段：相鄰 bet 間距 ≥ `RUN_BREAK_MIN` 即切分為新 run）。不再使用「Visit」一詞以避免與其他語境的 visit 混淆。
2. **Run-level 樣本加權**：Phase 1 採用 **run-level 反比權重** 作為 `sample_weight`：對每個觀測點計算 `sample_weight = 1 / N_run`，其中 `N_run` 為同一 run（由 `compute_run_boundary` 產生的 `run_id`）在訓練集內的觀測點數。與 `class_weight='balanced'` 並用：`class_weight` 處理正/負例標籤不平衡，`sample_weight` 處理跨 run 的長度偏誤（Length Bias），使每個 run 對 loss 的貢獻較為均等。

**理由**：
- Run 與原「Visit（bet-derived）」定義相同（皆以 30 分鐘 gap 切割），統一術語減少困惑。
- Run-level 加權可校正高頻/長 run 主導訓練的偏誤，同時與 Run 術語一致；僅使用 `class_weight` 無法處理此偏誤。

---

## DEC-014：採用根目錄 `./data` 作為 Local Parquet 預設路徑

**日期**：2026-03-03  
**SSOT 章節**：§4.3  

**決策**：將 `trainer.py`、`etl_player_profile.py` 與 `scorer.py` 等模組中 `--use-local-parquet` 的預設讀取路徑，從 `trainer/.data/local/` 變更為專案根目錄下的 `./data/`。同時將預期檔名對齊實際匯出檔名（`gmwds_t_bet.parquet` 與 `gmwds_t_session.parquet`）。

**理由**：
- 實際的 raw data 檔案龐大（19GB+），通常會放在專案根目錄的 `data/` 資料夾中（並加入 `.gitignore`），而非深埋在 `trainer/.data/local/` 中。
- 檔名直接對應 ClickHouse 匯出的原始表名，減少重新命名的手動步驟，降低開發者 onboarding 阻力。

---

## DEC-015：Training Pipeline Fast Mode（Option B — Rated Sampling + Full Nonrated）

**日期**：2026-03-04  
**SSOT 章節**：§4.3, §8.2  
**關聯**：DEC-011（player_profile_daily）、DEC-014（Local Parquet）

**決策**：新增 `--fast-mode` CLI flag 至 `trainer.py` 與 `etl_player_profile.py`，使整條 pipeline（含 ETL + 訓練 + 評估）可在筆電上 <10 分鐘跑完 3 個月資料。

**Fast Mode 行為定義**：

| 面向 | Normal Mode | Fast Mode |
|------|-------------|-----------|
| **Rated 玩家範圍** | 所有 rated（mapping 全量） | 從 canonical_map 中 deterministic 抽樣 N 人（預設 N=1000，seed 固定） |
| **Nonrated 玩家** | 全量 | 全量（不受影響） |
| **Profile ETL snapshot 頻率** | 每天 1 snapshot | 每 7 天 1 snapshot（可由參數覆蓋） |
| **Session Parquet 讀取** | backfill 每天各讀一次 | backfill 一次性讀入 Memory，每天 in-memory filter（解決 90× I/O 瓶頸） |
| **Optuna 超參搜索** | OPTUNA_N_TRIALS 次 | 跳過 Optuna，使用 default hyperparameters |
| **模型輸出** | `rated_model.pkl` + `nonrated_model.pkl` | 同上（artifact 結構不變，但標記 `fast_mode=True`） |
| **Profile 快取** | Schema hash 機制照常運作 | 照常（但 snapshot 頻率降低會自然減少需要計算的日期數） |

**考慮過的替代方案**：

1. **Option A — Nonrated-only**：只跑 nonrated 模型，完全跳過 player_profile_daily。最快但無法驗證 rated 路徑。
2. **Option B — Rated Sampling + Full Nonrated**（✓ 選定）：rated 模型仍有代表性樣本，nonrated 全量保留；兩條路徑都能驗證。
3. **Option C — 只算當期 bets 出現的 rated IDs**：節省最多計算，但需要先掃全量 bets 找出 ID 集合，增加 orchestration 複雜度。

**最終理由**：
- Option B 在「能驗證完整 pipeline（含 rated + nonrated 雙路徑）」與「執行時間」之間取得最佳平衡。
- 1,000 名 rated 玩家通常佔全量的 0.3%，但足以驗證 identity mapping、D2 join、profile PIT join、rated model training 全流程。
- Deterministic sampling（hash-based 或 fixed seed）確保每次 fast-mode 跑出相同結果，不影響 CI regression。
- Nonrated 全量保留，因為 nonrated 不需要 player_profile_daily，本身就很快。

**效能估算（3 個月 Local Parquet）**：

| 瓶頸 | Normal | Fast Mode | 加速因素 |
|------|--------|-----------|---------|
| Profile ETL（90 天 × 全量 rated） | ~60 min | ~2 min | 抽樣 + 降頻 + 一次讀取 |
| Session I/O（90 次 read_parquet） | ~15 min | ~1 min | 一次讀取 |
| Optuna（300 trials × 2 models） | ~10 min | ~0 min | 跳過 |
| Training（LightGBM） | ~3 min | ~1 min | 資料量少 |
| **總計** | ~90 min | **~5 min** | ~18× |

**實作要點**：

1. `trainer.py`：新增 `--fast-mode` CLI flag；在 `run_pipeline` 中：
   - 若 `fast_mode`：從 canonical_map 抽 N 個 canonical_id（deterministic seed）
   - 傳抽樣結果給 `ensure_player_profile_daily_ready` 與 `load_player_profile_daily`
   - 跳過 Optuna，使用 default HP
2. `etl_player_profile.py`：
   - `backfill()` 新增 `canonical_id_whitelist` 參數：inner join 後立刻做 `isin(whitelist)` 過濾
   - `backfill()` 新增 `snapshot_interval_days` 參數（fast-mode 傳 7）
   - `backfill()` 一次性讀取 session parquet（而非每天各讀一次），per-day 只做 in-memory filter
3. `doc/player_profile_daily_spec.md`：
   - 新增 §2.3「Population 約束」：canonical_id 只能來自 rated mapping
   - 新增 §2.4「Consumer 約束」：只有 rated model 使用；nonrated model 不依賴此表
4. Artifact metadata：`model_version` 檔案加入 `fast_mode: true/false` 欄位，防止 fast-mode 產出被誤用於生產

**警告 / 限制**：
- Fast-mode 產出的模型**不得用於生產推論**（rated 模型只用 0.3% 玩家訓練）
- Fast-mode 的 profile 快取（7 天一次 snapshot）不可與 normal-mode 快取混用；schema hash 機制會自然處理此問題（不同 snapshot 頻率 → 不同 date coverage → backfill 會自動補齊）

---

## DEC-016：Round 28 風險處理範圍收斂（僅先處理 R118）

**日期**：2026-03-04  
**關聯**：Review Round 28（R116–R119）

**決策**：本輪只優先處理 `R118`（`--fast-mode-no-preload` 在非 `--fast-mode` 下靜默無效），其餘 `R116/R117/R119` 先不改 production code。

**理由**：
- `R118` 為使用者體驗與操作語義問題，修復成本最低（一條 warning 路徑）。
- `R116` 屬極端邊界（超長 session > 395 天），實務風險低。
- `R117` 為微小效能優化（schema 讀取開銷毫秒級），非當前瓶頸。
- `R119` 屬程式碼重複（Code Smell），可於後續整理階段處理。

**執行策略**：
1. 先將 `R118` 轉成最小可重現 guardrail 測試（tests-only）。  
2. 測試先紅（證明問題存在）後，再安排小幅 production 修復。  
3. 修復完成後以該 guardrail 測試轉綠作為驗收標準。

---

*本文件隨專案演進持續更新。新決策請沿用 `DEC-XXX` 編號格式。*
