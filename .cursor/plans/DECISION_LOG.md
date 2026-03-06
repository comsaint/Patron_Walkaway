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
- **主指標**：`val_ap`（average precision, AP）為首要報告與日誌指標。  
- **閾值選擇**：在 validation 上掃描閾值，取 **F1** 最大者（無 G1 約束）；若 F1 平手則取 recall 較高者。  
- **日誌**：`AP=...  F1=...  prec=...  rec=...  thr=...`  
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

## DEC-017：重新設計 Fast Mode 為 Data-Horizon 限制模式

**日期**：2026-03-04  
**SSOT 章節**：§4.3, §8.2  
**關聯**：DEC-015（被取代）、DEC-011（player_profile_daily）、DEC-014（Local Parquet）

**決策**：將 `--fast-mode` 的核心語義從「Rated Sampling + Snapshot 降頻」改為「**Data-Horizon 限制**」——假設只有最近 N 天的原始資料，所有子模組（identity、profile ETL、特徵計算、training）遵守同一個時間邊界，不得存取邊界外的資料。原 DEC-015 的 Rated Sampling 功能抽離為獨立的 `--sample-rated N` flag，與 `--fast-mode` 正交。

**背景與問題**：  
DEC-015 的設計在實際使用中暴露了兩個根本問題：

1. **Profile 日期範圍不受 training window 限制**：即使用 `--fast-mode --recent-chunks 1`（只要最後一個月的 chunk），`ensure_player_profile_daily_ready` 仍然從 `effective_start - 365 days` 開始建 profile snapshot，導致 pipeline 花大量時間在建歷史 profile。
2. **`canonical_map` 傳遞鏈斷裂**：`trainer.py` 在記憶體中建好 `canonical_map`，但呼叫 `backfill()` 時，`backfill()` 無法接收此 map——它自行嘗試讀取 `data/canonical_mapping.parquet`（本地不存在），導致每天的 profile 全部建失敗，產生大量 `No local canonical_mapping.parquet; cannot join canonical_id` 警告。

使用者對 fast-mode 的期望是「假設只有最近幾天/月的資料，一切據此運行」，本質上是 data-source 層級的過濾，而非 DEC-015 的抽樣策略。

**考慮過的替代方案**：

1. **修補 DEC-015（加傳 canonical_map + 限制 profile 日期）**：可解決 bug，但核心設計仍是「全量日期 + 抽樣玩家」，與使用者心智模型不符——使用者想要的是「有限資料 → 一切自動對齊」。
2. **DEC-017（Data-Horizon 模式）** ✓ 選定：fast-mode = data-horizon 限制，速度來自資料量減少，概念最簡潔。profile 特徵根據可用天數動態裁剪（< 90 天不算 180d/365d 窗口），避免產生無意義的等值特徵。

**新設計要點**：

1. **Data Horizon**：`data_horizon_days = (effective_end - effective_start).days`。Fast mode 下，profile `required_start = effective_start.date()`（不往前推 365 天）。Normal mode 不影響。
2. **Profile 特徵動態分層**：`_compute_profile` 接受 `max_lookback_days` 參數，只計算 ≤ 該值的時間窗口。`features.py` 新增 `get_profile_feature_cols(max_lookback_days)` 函數動態產生特徵清單。
3. **`canonical_map` 傳遞修復**（bug fix，兩種模式皆適用）：`backfill()` 和 `ensure_player_profile_daily_ready()` 新增 `canonical_map` 參數，`trainer.py` 把已建好的 map 傳到底。
4. **`--sample-rated N`**（獨立 flag，取代 DEC-015 的隱含抽樣）：預設不啟用；可與 `--fast-mode` 搭配或單獨使用。適用於「全量日期但少量 patron」的測試情境。

**與 DEC-015 的差異**：

| 面向 | DEC-015 | DEC-017 |
|------|---------|---------|
| 核心機制 | Rated Sampling（1000 人） | Data-Horizon 限制（effective window 內） |
| Profile 日期範圍 | 不限（仍回推 365 天） | 限制在 effective window 內 |
| Profile 特徵窗口 | 全部計算（7d–365d） | 動態裁剪（只算 ≤ data_horizon_days） |
| Rated Sampling | fast-mode 隱含 | 獨立 `--sample-rated N` flag，預設不啟用 |
| canonical_map 傳遞 | 有 bug（斷裂） | 修復 |

**保留自 DEC-015 的部分**：
- `--skip-optuna`（fast-mode 隱含或獨立使用）
- `snapshot_interval_days=7`（fast-mode 隱含）
- `--fast-mode-no-preload`（低 RAM 保護）
- schema hash 快取機制
- artifact metadata `fast_mode=True` 標記

**DEC-015 的狀態**：被 DEC-017 取代。DEC-015 中的 Rated Sampling 概念保留為 `--sample-rated N`，其餘行為由 DEC-017 重新定義。

---

## DEC-018：Pipeline 內部統一為 tz-naive HK 當地時間（消除 datetime tz 混用）

**日期**：2026-03-04  
**SSOT 章節**：§4.3（時間語義）  
**關聯**：DEC-008（Time Fold Splitter）、R23（apply_dq tz 正規化）

**決策**：在 `process_chunk()` 入口處，將 `window_start`、`window_end`、`extended_end` 等邊界時間 strip tz（`replace(tzinfo=None)`），使 **pipeline 內部統一只有 tz-naive 的 HK 當地時間**。同時在 `apply_dq()` 末尾統一 datetime resolution 為 `datetime64[ns]`。完成後移除所有逐點 tz-alignment 補丁。

**背景與問題**：  
Pipeline 中存在兩個 datetime「世界」混用：
1. **邊界時間**（`window_start`、`window_end`、`extended_end`）：由 `parse_window()` → `_to_hk()` → `time_fold.generate_chunks()` 產生，帶 tz-aware（`Asia/Hong_Kong`，`+08:00`）。
2. **資料欄位**（`payout_complete_dtm`、`snapshot_dtm` 等）：由 `apply_dq()` R23 正規化後為 tz-naive（localize/convert HK → strip tz）。

下游在 `features.py`、`labels.py`、`trainer.py` 中拿邊界與資料欄位比較時，反覆觸發 `TypeError: Cannot compare tz-naive and tz-aware datetime-like objects`。此外，不同 Parquet 來源的 datetime resolution 不一致（`[ms]` vs `[us]`）也導致 `pd.merge_asof` 的 `MergeError`。

在本次 training pipeline 除錯過程中，先後在 **6 處**逐點加入 tz-alignment 補丁（`features.py` 3 處、`labels.py` 1 處、`trainer.py` 2 處），每處都是「比較前先檢查雙方 tz 狀態再 localize/strip」。但此方法無法根治——每新增一個比較都可能再次觸發同類錯誤。

**考慮過的替代方案**：

1. **方案 A — 邊界 strip tz（pipeline 內部全 naive）** ✓ 選定  
   在 `process_chunk()` 入口 strip 邊界 tz。`apply_dq()` 已保證資料是 HK 當地時間，邊界本身也是 HK 產生的，strip 不會丟失語義。改動量最小（一處入口 + 移除 6 處補丁），且未來新增比較不需再考慮 tz 對齊。

2. **方案 B — 資料保持 tz-aware（pipeline 內部全 aware）**  
   不在 `apply_dq()` strip tz，讓 `payout_complete_dtm` 保持 `Asia/Hong_Kong` tz-aware。但影響面極大——`features.py`、`labels.py`、`scorer.py`、`backtester.py`、`validator.py` 等到處假設資料為 tz-naive；且 tz-aware 的 pandas 運算較慢。**不推薦**。

3. **繼續逐點補丁（維持現狀）**  
   每個比較處判斷 tz 再對齊。可以 work，但永遠追不完，且程式碼充斥重複的 tz 判斷邏輯。**不推薦**。

**最終理由**：
- 方案 A 修改量最小（核心只改一處入口），且能一次性消除所有 tz-naive vs tz-aware 比較問題。
- 語義正確：`apply_dq()` 的 R23 把資料轉成「HK 當地時間、無時區」，邊界（由 `_to_hk()` 產生）本來就是 HK 時間，strip tz 後語義完全一致。
- `time_fold.py`、`parse_window()` 等外層介面不需要改動——它們仍然產出 tz-aware 供 DB query 等外部用途；只有進入 `process_chunk()` 後才 strip。
- 附帶修復 datetime resolution mismatch：在 `apply_dq()` 統一轉為 `datetime64[ns]`，消除 `[ms]` vs `[us]` 的 `MergeError`。

**實作要點**：
1. `trainer.py` — `process_chunk()` 入口：strip `window_start`、`window_end`、`extended_end` 的 tz
2. `trainer.py` — `apply_dq()`：R23 末尾加 `.astype("datetime64[ns]")`
3. 移除 `features.py`（3 處）、`labels.py`（1 處）、`trainer.py`（2 處）的逐點 tz-alignment 補丁
4. 加入防呆 assertion：`apply_dq()` 出口檢查 tz-naive、`process_chunk()` 入口檢查邊界 tz-naive

**回退說明**：  
若未來需要 pipeline 內部使用 tz-aware（例如跨時區部署），可在 `process_chunk()` 入口改為 `tz_convert(HK_TZ)` 而非 strip，並在 `apply_dq()` 中不 strip tz。但目前無此需求。

---

## DEC-019：player_profile_daily 改為「每月最後一天」更新以縮短 Full Run 時間

**日期**：2026-03-04  
**SSOT 章節**：§4.3（時間語義）、DEC-011（player_profile_daily）  
**關聯**：`trainer/etl_player_profile.py` backfill、`trainer.py` ensure_player_profile_daily_ready

**決策**：將 player_profile_daily 的 snapshot 更新頻率從「每日」改為「每月最後一天」一次（例如每月只計算該月最後一天當日的 snapshot）。PIT join 語意不變（每個 bet 仍使用 `snapshot_dtm <= bet_time` 的最近一筆 snapshot），僅時間解析度由「日」變「月」，以大幅縮短 full run 時 profile ETL 階段耗時。

**背景與問題**：  
Full run（無 fast-mode、無 sample-rated）時，profile ETL（ensure_player_profile_daily_ready + backfill）需對 365 天逐日建 snapshot，每 day 約 30–60 秒（session filter + compute_profile），加上一次 session 全表 preload（約 3 分鐘），總計約 **4–6 小時**，成為整體 pipeline 最大瓶頸之一。使用者希望在不改程式邏輯的前提下，以「降低更新頻率」換取時間。

**考慮過的替代方案**：

1. **每月最後一天更新（月結）** ✓ 選定  
   只對「區間內每個月的最後一天」建 snapshot（一年 12 次）。PIT 仍有效：例如 2 月中旬的注單用 1/31 snapshot，3 月初用 2/28 snapshot。Profile 特徵（30d/90d/365d 聚合）最多落後約一個月，對 walkaway 預測多數情境可接受。實作上需在 backfill 改為迭代「月結日期列表」而非逐日 + `snapshot_interval_days`。

2. **維持 snapshot_interval_days=30（每 30 天一次）**  
   可減少迭代次數（365 → 約 12），但 snapshot 日期不會對齊日曆月結（例如 1/1、1/31、3/2…），語意上為「每 30 天一個點」而非「每月最後一天」。若產品希望報表或語意對齊「月」，仍以方案 1 較清晰。

3. **維持每日更新**  
   不變更，profile ETL 仍約 4–6 小時。**不採納**——使用者明確希望縮短時間。

**最終理由**：  
- 時間縮短：profile ETL 從約 4.5 h 降至約 12 min（12 次 × 約 45 s + 同一次 preload），約 **20–25× 加速**；整體 full run 可少約 4–5 小時。  
- 語意正確：PIT join 不需改動，僅 snapshot 的「產生日」變少；每個 bet 仍取「最近一筆 ≤ bet 時間」的 snapshot。  
- 特徵可接受：profile 多為長期窗口（30d/90d/365d），最多落後約一個月需由產品/ML 確認，但多數情境可接受。

**實作要點（僅記錄於計畫，不在此次改 code）**：  
1. `etl_player_profile.py` — backfill：新增排程模式（例如 `snapshot_schedule='month_end'`）或接受 `snapshot_dates: List[date]`；迭代「區間內每月最後一天」並對這些日期呼叫 `build_player_profile_daily`。  
2. `trainer.py` — ensure_player_profile_daily_ready：呼叫 backfill 時傳入上述月結排程或日期列表；coverage 檢查需接受「每月一個 snapshot」的間隔（例如沿用或擴充 `snapshot_interval_days` 的邏輯）。  
3. 月結日期計算：以 HK 時間為準，用 calendar 或 date 運算產生「每月最後一天」列表，避免 `snapshot_interval_days=30` 造成不對齊。

**回退說明**：  
若產品要求每日解析度，可改回 `snapshot_interval_days=1` 或移除 month_end 排程，恢復逐日 backfill。

---

## DEC-020：Track A 固定接入 pipeline、篩選改為「全特徵」、新增 `--no-afg`

**日期**：2026-03-04  
**SSOT 章節**：§8.2（軌道 A、特徵篩選 §8.2.C）  
**關聯**：DEC-002（兩階段 DFS）、DEC-001（雙軌架構）

**決策**：

1. **Track A 固定接入**：訓練 pipeline 必須在 process_chunk 迴圈**之前**執行 Track A 第一階段（在抽樣資料上跑 DFS 探索），並將篩選後的 feature definitions 存成 `saved_feature_defs/feature_defs.json`；後續每個 chunk 以 `calculate_feature_matrix` 套用該定義，不再出現「從未呼叫 run_track_a_dfs、Track A 永遠不跑」的設計斷裂。
2. **篩選改為「全特徵」**：Feature screening 的輸入改為**所有**候選特徵（player-level/profile、Track A、Track B），而非僅軌道 A。呼叫端組好完整 feature matrix 與全部 feature 名稱後傳入 `screen_features()`，回傳的清單即為 `feature_list.json` 的內容；訓練與 scorer 僅計算此清單內特徵，維持 train–serve parity。
3. **新增 CLI `--no-afg`（No Automatic Feature Generation）**：當設定時，**不**執行 Track A（不跑 DFS、不產出 `saved_feature_defs`）。篩選仍會執行，但僅針對 Track B + player-level/profile 等非–Track A 特徵；`feature_list.json` 僅含篩選後的這些特徵，scorer 僅計算 Track B + profile，不載入 Featuretools defs。與 `--fast-mode`、`--sample-rated` 等正交。

**背景與問題**：  
- 目前 `run_track_a_dfs` 從未被 `run_pipeline` 呼叫，導致 `feature_defs.json` 永遠不存在、Track A 在 process_chunk 中永遠被跳過。  
- SSOT §8.2.C 原描述為「軌道 A 篩選後 + 軌道 B 固定納入」；需求改為「篩選對象為全特徵」，使 player-level 與 Track B 也參與 ranking/redundancy 剔除，產出單一一致的特徵清單。  
- 需要一個明確開關以便在不用自動特徵時仍可跑 pipeline（篩選照常、僅無 Track A）。

**預設篩選保留數量**：  
特徵數上限 **由 config 控制**：在 `config.py` 中定義一參數（如 `SCREEN_FEATURES_TOP_K`）。`screen_features()` 呼叫時若未傳入 `top_k`，則以該 config 值為準：**若為整數 N**，篩選後最多保留 N 個特徵（Stage 1 通過者依 MI 排序取前 N；若啟用 Stage 2 則依 LGBM importance 取前 N）；**若為 `None`**，不設上限，Stage 1 通過者全部保留。

**實作要點（僅記錄於計畫，尚未改 code）**：  
- `run_pipeline`：在 process_chunk 迴圈前，若未設定 `--no-afg`，則載入首 chunk 抽樣 → `run_track_a_dfs` → 與 Track B + profile 合併成完整 feature matrix → `screen_features(..., feature_names=all_candidates)` → 依 screened 清單過濾 Track A defs 並 `save_feature_defs`，`feature_list.json` 寫入 screened 全清單。  
- `run_pipeline`：新增 `--no-afg` 參數；若設定，跳過 DFS 與 saved_feature_defs，僅對 Track B + profile 做 screening，寫入 `feature_list.json`。  
- By default, setting `--fast-mode` implies `--no-afg`.
- Scorer：行為不變，依 `feature_list.json` 與（若存在）`saved_feature_defs` 計算；`--no-afg` 產出的 bundle 無 `saved_feature_defs`，scorer 僅算 Track B + profile。

---

## DEC-021：改為單一模型架構（僅 Rated），不訓練無卡客模型

**日期**：2026-03-05
**SSOT 章節**：§3.1, §6.2, §8.2, §9.1, §10.2
**關聯**：Phase 1 Plan

**決策**：
基於業務決策，本專案將資源聚焦於核心價值較高的 Rated players（有 casino_player_id 者）。
- **取消雙模型架構**：僅對有卡客（Rated）訓練單一模型並進行推論。不再為無卡客（Non-rated）建置模型。
- **無卡客處理策略**：線上推論時，若判定為無卡客（兜底至 player_id），將 **不呼叫模型**、**不寫入警報**，僅記錄基礎統計量（volume log，如 ID 數、bet 數）供後續監控與容量評估。
- **打分與警報條件**：僅有「當前觀測能解析出 casino_player_id（直接存在，或經 mapping 得到 canonical_id 來自 casino_player_id）」的觀測點才進行模型打分與警報發布。
- **閾值策略**：從 2D 雙閾值搜尋改為針對 Rated 模型的 **單一閾值搜尋**。
- **模型命名與工件**：統一移除 `rated_`、`nonrated_` 前綴，產出單一 `model.pkl`、`threshold`。

**背景與問題**：
- 原設計包含 Rated 與 Non-rated 兩套模型。
- 業務評估認為：無卡客流動性高且對營收貢獻影響不明確，加上其特徵深度與訊號品質皆不如 Rated 玩家。強行開發與維運 Non-rated 模型會分散注意力，且可能引發較多品質不佳的警報，消耗 Host 團隊精力。

**最終理由**：
- **業務對齊**：最大化挽留具有明確價值的目標客群（有建檔、有長期歷史行為的 Rated patrons）。
- **系統簡化**：移除 Non-rated entity set、雙模型路由邏輯、雙維度閾值搜尋，降低線上推論與回測複雜度。
- **減少雜訊**：不再產生缺乏強證據支持的無卡客警報，有助於初期建立公關對警報系統的信任。

---

## DEC-022：廢棄 Featuretools 軌道 A，改為「Track Profile / Track LLM / Track Human」三軌架構

**日期**：2026-03-05  
**SSOT 章節**：§4.3, §8.2, §9.4  
**關聯**：DEC-001, DEC-002, DEC-011, DEC-021

**決策**：

1. **停止使用 Featuretools DFS 軌道 A** 作為自動特徵工程工具（不再建 EntitySet、不再使用 `save_features` / `calculate_feature_matrix`；僅保留歷史文檔作為參考）。  
2. **正式定名並採用三軌特徵工程架構**：
   - **Track Profile**：player-level 歷史輪廓特徵，來源為 `player_profile_daily`，以 PIT/as-of join（`snapshot_dtm <= bet_time` 最近一筆）貼到 bet 列。  
   - **Track LLM**：LLM 建議的 bet-level 特徵，**只依賴 `t_bet` + `canonical_id`**，以時間序列 window / lag / transform 為主，由 DuckDB window function 計算。  
   - **Track Human**：工程師手寫的狀態機/Run-level 特徵（例如 `loss_streak`, `run_id`, `minutes_since_run_start`），以向量化 Python（Pandas/Polars）實作。  
3. 三軌皆受相同的 leakage 防護與 Train–Serve Parity 護欄約束（事件時間、available_time、穩定排序等），並可在設定中獨立 opt-in/out（例如僅啟用 Track LLM + Track Human 進行實驗）。

**理由**：

- Featuretools 在 20GB+ 單表、4.38 億筆 bet 的場景下，DFS + EntitySet 的記憶體成本與工程複雜度過高，即使採用兩階段 DFS 仍然不易在單機穩定落地。  
- LLM 已可根據欄位語義快速發想大量窗口/聚合特徵，以 DuckDB SQL 表達更直覺、可讀且易於手工審核。  
- Track Profile / Track LLM / Track Human 三軌能自然對應「長期輪廓 / 當期投注節奏 / 複雜狀態機」三種訊號來源，比原本的「Featuretools 軌道 A + 手寫軌道 B」更清晰且可測試。  
- 移除 Featuretools 依賴可簡化部署與除錯，同時保留原本 SSOT 中最重要的護欄（PIT join、防洩漏與時間窗口一致性）。

---

## DEC-023：採用 DuckDB 作為訓練與推論的特徵計算引擎

**日期**：2026-03-05  
**SSOT 章節**：§4.3, §8.2, §11  
**關聯**：DEC-014, DEC-017, DEC-022

**決策**：

1. **訓練端**：從 Local Parquet 或 ClickHouse 匯出的 `t_bet` / `t_session` 資料，透過 DuckDB（單機 in-process）執行所有 Track LLM 的 window / lag / transform 特徵計算。  
2. **推論端（`scorer.py`）**：改為在 process 內嵌入 DuckDB；每個 polling cycle 從 ClickHouse 抓 raw bets 進入 DuckDB 的 in-memory 表（例如 `recent_bets`），再以與訓練端完全相同的 SQL（由 Feature Spec 產生）計算特徵。  
3. **歷史窗口保留策略**：`scorer.py` 僅保留最近 `HISTORY_WINDOW_MIN` 分鐘的 bets（例如 75 分鐘），並在每輪 polling 時 prune 過舊資料；`HISTORY_WINDOW_MIN` 必須 ≥ Track LLM 中宣告的 `max_window_minutes` + buffer。  
4. **Cold Start 回補**：當 `scorer.py` 啟動或重啟時，自動從 ClickHouse 回補過去 `HISTORY_WINDOW_MIN` 的 raw bets 到 DuckDB。在回補完成前暫停發警報（warmed_up = false）。

**理由**：

- DuckDB 對 Parquet 與 Window Function 的支援完整，能在數萬筆級別的 in-memory 表上於毫秒至百毫秒等級計算所有窗口特徵，完全滿足 45 秒 polling + 3 秒 scoring SLA。  
- Train–Serve Parity 得以徹底簡化：同一份 Feature Spec YAML 產生相同 DuckDB SQL，訓練與推論端皆在 DuckDB 執行，避免 ClickHouse 與 Featuretools/Polars 之間的語義差異。  
- ClickHouse 角色退化為「高效儲存與 raw 資料來源」，減少對其 window function 語義的依賴與運算負載。  
- Cold Start 回補 + 暫停警報策略避免「重啟後前幾輪沒有完整歷史卻開始打分」的隱性風險。

---

## DEC-024：定義 Feature Spec YAML Protocol（候選 vs 生產特徵）

**日期**：2026-03-05  
**SSOT 章節**：§8.2, §9.4, §10.2  
**關聯**：DEC-022, DEC-023

**決策**：

1. **Feature Spec 分層**：  
   - `features_candidates.yaml`：記錄 Track Profile / Track LLM / Track Human 三軌的**候選特徵全集**（含 `feature_id`, `type`, `dtype`, `expression`, `window_frame`, `depends_on`, `postprocess` 等）。  
   - `feature_list.json`（或 `features_active.yaml`）：由 Feature Screening 模組產生的**生產特徵清單**，僅列出最終要計算並送入模型的 feature_id。  
2. **Track LLM YAML Schema 要點**：  
   - 僅允許 LLM 填寫「原子算子」：`expression`（不含 SELECT/FROM/JOIN）與 `window_frame`，並明確標記 `type`（`window`/`lag`/`transform`/`derived`）。  
   - Parser 會自動將這些片段組裝為合法的 DuckDB Window Function，並強制施加護欄：  
     - 禁止 `FOLLOWING` 視窗（僅允許過去＋當前 row），避免未來洩漏。  
     - 嚴格限制可用欄位（白名單 `t_bet` 欄位）與可用函數（COUNT/SUM/AVG/…）。  
   - `max_window_minutes` 由 YAML 中集中定義；scorer 的 `HISTORY_WINDOW_MIN` 必須與之對齊。  
3. **Track Human / Track Profile YAML Schema 要點**：  
   - Track Human：記錄 `function_name`, `input_columns`, `output_columns`, `dtype`, `postprocess` 等，實作於 `features.py` 中的向量化函數。  
   - Track Profile：記錄 `source_table`, `join_key`, `snapshot_time_column`, `pit_rule`（如 `snapshot_dtm <= bet_time` as-of join）、`source_column` 與 `feature_id` 映射，以及缺失 profile 時的處理策略（zero-fill 或 reject）。  
4. **Reason Code 與 Spec Hash**：  
   - 每個特徵可選擇性標註 `reason_code_category`（例如 `BETTING_PACE_DROPPING`, `LOSS_STREAK`），用於從 SHAP top-k 對應到穩定的 reason codes。  
   - Pipeline 對 `features_candidates.yaml` 與 `feature_list.json` 計算 hash，將 `spec_hash`（候選）與 `active_hash`（生產）寫入 model artifact metadata，並透過 `/model_info` 暴露。

**理由**：

- YAML Protocol 提供一個對 LLM 友善、又對工程師透明的特徵定義格式，使「自動發想特徵」與「嚴格防洩漏/可測試性」可以共存。  
- 候選集合與生產集合分離，能保留 LLM 的探索成果，同時保持線上模型的特徵數量可控且經過 screening。  
- 類型化（window/lag/transform/derived/profile_column/python_vectorized）與欄位/函數白名單，有助於在載入 YAML 時做靜態檢查，降低 runtime 驚喜。  
- Spec hash 讓模型版本與特徵定義緊密綁定，方便追蹤與回溯。

---

## FND-04 與 FND-12 語義對齊（2026-03-05）

**背景**：FND-04 排除 ghost sessions（turnover=0 且 num_games_with_wager=0）；FND-12 偵測假帳號（1 session 且 ≤1 game）。

**決策**：FND-04 應用於 FND-12 dummy 偵測後，**ghost sessions 不再計入 session_cnt**。部分 player 可能從非 dummy 變為 dummy。此為刻意行為（SSOT §5），首次上線時應比對 mapping 變化量。

---

*本文件隨專案演進持續更新。新決策請沿用 `DEC-XXX` 編號格式。*
