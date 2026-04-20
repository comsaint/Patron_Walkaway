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
2. **篩選改為「全特徵」**：Feature screening 的輸入改為**所有**候選特徵（player-level/profile、Track A、Track Human），而非僅軌道 A。呼叫端組好完整 feature matrix 與全部 feature 名稱後傳入 `screen_features()`，回傳的清單即為 `feature_list.json` 的內容；訓練與 scorer 僅計算此清單內特徵，維持 train–serve parity。
3. **新增 CLI `--no-afg`（No Automatic Feature Generation）**：當設定時，**不**執行 Track A（不跑 DFS、不產出 `saved_feature_defs`）。篩選仍會執行，但僅針對 Track Human + player-level/profile 等非–Track A 特徵；`feature_list.json` 僅含篩選後的這些特徵，scorer 僅計算 Track Human + profile，不載入 Featuretools defs。與 `--fast-mode`、`--sample-rated` 等正交。

**背景與問題**：  
- 目前 `run_track_a_dfs` 從未被 `run_pipeline` 呼叫，導致 `feature_defs.json` 永遠不存在、Track A 在 process_chunk 中永遠被跳過。  
- SSOT §8.2.C 原描述為「軌道 A 篩選後 + 軌道 B 固定納入」；需求改為「篩選對象為全特徵」，使 player-level 與 Track Human 也參與 ranking/redundancy 剔除，產出單一一致的特徵清單。  
- 需要一個明確開關以便在不用自動特徵時仍可跑 pipeline（篩選照常、僅無 Track A）。

**預設篩選保留數量**：  
特徵數上限 **由 config 控制**：在 `config.py` 中定義一參數（如 `SCREEN_FEATURES_TOP_K`）。`screen_features()` 呼叫時若未傳入 `top_k`，則以該 config 值為準：**若為整數 N**，篩選後最多保留 N 個特徵（Stage 1 通過者依 MI 排序取前 N；若啟用 Stage 2 則依 LGBM importance 取前 N）；**若為 `None`**，不設上限，Stage 1 通過者全部保留。

**實作要點（僅記錄於計畫，尚未改 code）**：  
- `run_pipeline`：在 process_chunk 迴圈前，若未設定 `--no-afg`，則載入首 chunk 抽樣 → `run_track_a_dfs` → 與 Track Human + profile 合併成完整 feature matrix → `screen_features(..., feature_names=all_candidates)` → 依 screened 清單過濾 Track A defs 並 `save_feature_defs`，`feature_list.json` 寫入 screened 全清單。  
- `run_pipeline`：新增 `--no-afg` 參數；若設定，跳過 DFS 與 saved_feature_defs，僅對 Track Human + profile 做 screening，寫入 `feature_list.json`。  
- By default, setting `--fast-mode` implies `--no-afg`.
- Scorer：行為不變，依 `feature_list.json` 與（若存在）`saved_feature_defs` 計算；`--no-afg` 產出的 bundle 無 `saved_feature_defs`，scorer 僅算 Track Human + profile。

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

## DEC-025：Canonical mapping Step 6 parity 測試改採抽樣資料（避免 OOM）

**日期**：2026-03-09  
**相關**：PLAN § Canonical mapping 全歷史 + DuckDB 步驟 6。

**決策**：步驟 6 要求「DuckDB 路徑與全量 pandas 建 map 結果一致」。全量 pandas 建 map（`load_local_parquet` 全段 session + `build_canonical_mapping_from_df`）於大資料（如 70M+ session 行）易 OOM，故**不**在生產規模資料上執行全量 pandas 路徑。改為在**同一份小型或抽樣 session 資料**上分別執行 DuckDB 路徑與 pandas 路徑，比對兩者產出之 canonical map（及可選 dummy 集合）一致，以驗證邏輯 parity。

**理由**：小資料 parity 足以保證兩路徑語義一致；全量比對既不可行（OOM）亦非必要。

---

## DEC-026：閾值優化目標改為 Precision at recall=0.01，並擴充 Precision-at-Recall 報告

**日期**：2026-03-10  
**相關**：PLAN § 閾值策略、§ Backtester precision-at-recall 指標、§ 閾值策略與 Precision-at-Recall 報告更新（DEC-026）。  
**關聯**：DEC-009（Trainer 閾值）、DEC-010（Backtester Optuna）。

**決策**：

1. **閾值選擇目標**：Trainer 與 Backtester 之閾值選擇由「在 min recall / min alerts 約束下最大化 F-beta（或 F1）」改為 **「在 recall ≥ 0.01（及既有 min alerts 約束）下最大化 Precision」**。即：優化目標為 **Precision at recall=0.01**。
2. **Target recalls 擴充**：所有 Precision@Recall 報告之 recall 水準由 `(0.01, 0.1, 0.5)` 擴充為 **`(0.001, 0.01, 0.1, 0.5)`**，即新增 **Recall=0.001**。
3. **每 recall 水準之額外產出**：在所有 performance 日誌與評估輸出（含 `training_metrics.json`、`backtest_metrics.json`）中，對每個 recall 水準除既有 **Precision@Recall** 外，一併產出：
   - **該 operating point 之 threshold**（`threshold_at_recall_{r}`）；
   - **該閾值下之 alerts per minute**（`alerts_per_minute_at_recall_{r}`；Trainer 之 test set 若無評估窗長則可選填或僅產出 alert 數）。

**理由**：業務端確認以「Precision at recall=0.01」為優化目標；報告需涵蓋更低 recall（0.001）以供監控，且每個 recall 水準需可見對應閾值與告警量（APM）以利營運判斷。

**實作**：見 PLAN「閾值策略與 Precision-at-Recall 報告更新（DEC-026）」一節；程式變更待後續依 PLAN 實作。

---

## DEC-027：Config 集中化與合併策略（排除 Retention／Refresh・Poll）

**日期**：2026-03-11  
**相關**：PLAN § Config 集中化與合併變更草案（DEC-027）。

**決策**：對 `trainer/config.py` 中重複語意的常數群組進行集中化合併，以降低維護成本與 OOM 調校時的混淆；但以下兩類常數**明確排除，維持現狀**：

1. **Retention 常數**（SCORER_ALERT_RETENTION_DAYS、VALIDATOR_ALERT_RETENTION_DAYS、SCORER_STATE_RETENTION_HOURS、TABLE_STATUS_RETENTION_HOURS、TABLE_STATUS_HC_RETENTION_DAYS）：各自服務不同介面（scorer、validator、status_server），雖數值巧合相同，但語意與調整時機不同，合併反而增加耦合。
2. **Refresh／Poll 間隔**（SCORER_POLL_INTERVAL_SECONDS、TABLE_STATUS_REFRESH_SECONDS）：同為 45 秒但屬不同系統的週期，未來可能獨立調整。

**納入合併的變更**：

| 項目 | 策略 |
|------|------|
| **DuckDB 記憶體** | 新增一組共用 `DUCKDB_*` 常數（FRACTION、MIN/MAX_GB、RAM_MAX_FRACTION、THREADS、PRESERVE_INSERTION_ORDER）為 SSOT；各 stage（Profile ETL、Step 7、Canonical mapping）只保留與共用不同的覆寫；新增 helper `get_duckdb_memory_config(stage)` 統一取用。 |
| **Validator SSOT** | 補入 config 缺少的三個常數：`VALIDATOR_FRESHNESS_BUFFER_MINUTES`、`VALIDATOR_EXTENDED_WAIT_MINUTES`、`VALIDATOR_FINALITY_HOURS`（目前只在 getattr 的 magic default 裡）。 |
| **HISTORY_BUFFER_DAYS** | 從 `trainer.py` 模組常數移至 `config.py`，與 `TRAINER_DAYS`、`BACKTEST_*` 同區塊。 |
| **Threshold 命名** | `MIN_THRESHOLD_ALERT_COUNT` 更名為 `THRESHOLD_MIN_ALERT_COUNT`，與同系列 `THRESHOLD_MIN_RECALL`、`THRESHOLD_MIN_ALERTS_PER_HOUR` 一致。 |
| **OOM 區塊** | 不合併名稱，但在 config 內分成「Chunk 記憶體估計」「Neg sampling / OOM 預檢」「Profile ETL 記憶體」三個有明確標題的子區塊，並引用 `doc/training_oom_and_runtime_audit.md`。 |
| **Data availability delay** | 可選：命名對齊 `AVAIL_DELAY_*` 風格或保留現名，僅確保同區塊。 |

**理由**：  
- DuckDB 三組參數（PROFILE_*、STEP7_*、CANONICAL_MAP_*）的計算邏輯與命名風格不一致，合併後統一 `MEMORY_LIMIT_*` 命名並封裝在 helper 中，各 call site 只需指定 stage，不再重複讀取邏輯。  
- Validator 的三個常數目前只存在於 getattr 的 default 值中，屬隱性配置，補入 config 使其可見、可調。  
- HISTORY_BUFFER_DAYS 應與其他時間視窗常數同屬 SSOT，不應分散在 trainer.py。  
- Retention 與 Refresh/Poll 排除的原因：各自服務不同元件，合併會造成不必要的耦合，且未來調整某元件時需區分影響範圍。

**回退說明**：各項變更均為「重命名＋搬移＋引入 helper」，無演算法改變；若需回退，可逐項還原（helper 內部仍讀相同數值）。

---

## DEC-028：Deploy 套件帶出 player_profile.parquet、canonical mapping 僅在目標機持久化

**日期**：2026-03-12  
**相關**：DEPLOY_PLAN §8、package/deploy、scorer 路徑與重啟行為。

**決策**：

1. **player_profile 打包**  
   - 建包時若 **repo 根目錄 `data/player_profile.parquet`** 存在（與 trainer / etl_player_profile 使用之 `LOCAL_PARQUET_DIR` 一致），則複製到 deploy 套件的 `data/player_profile.parquet`。  
   - 若不存在則不複製，但在建包**全部完成後**於 console 印出一行錯誤級訊息，提醒未帶出 profile、scorer 將以 profile 特徵為 NaN 運行。

2. **目標機 profile 讀取**  
   - 部署端由 main.py 設定 `DATA_DIR = DEPLOY_ROOT / "data"` 並寫入環境變數，scorer 優先從 `DATA_DIR / "player_profile.parquet"` 讀取；若不存在則維持現有 warning、profile 特徵為 NaN。

3. **canonical mapping**  
   - **不**在套件內預先打包 canonical mapping（建包原則）；持久化路徑為 `DATA_DIR` 下的 `canonical_mapping.parquet` 與 `canonical_mapping.cutoff.json`。  
   - **重建時**由 `build_canonical_mapping_from_df(sessions, cutoff_dtm=now_hk)` 從當輪 sessions 建出，並將結果寫入上述二檔（僅在觸發重建分支且 map 非空時）。  
   - **穩定態載入**：見下方「實作行為澄清」——與「每輪輪詢都重新從 sessions 計算」不同。

**理由**：  
- 單一來源路徑（repo `data/`）與現有 trainer/etl 一致，避免多處路徑設定。  
- 有 profile 就帶出、沒有就明確錯誤提示，方便建包時發現遺漏。  
- Canonical 不預建、僅在目標機持久化；**設計意圖**是身分對照可在目標機**首次建立或重建時**反映當下 CH sessions，且**重啟後**可讀檔跳過重算（DEC-028 註解）；**實際是否隨時間自動刷新**依 scorer 實作（見下）。

**設計理由（為何帶 profile 不帶 canonical mapping）**：  
- **player_profile**：計算成本高、變動慢（目前僅每月更新一次），適合隨套件一併帶出；且模型本身約每月也會用新資料重訓幾次，帶出 profile 與重訓節奏一致。  
- **canonical_mapping**：變動快、具動態性，且計算量不大，故**原則上**不在建包時預先打包，改由部署端在**觸發重建時**依當下 sessions 建出並持久化；**不**將「變動快」解讀為「deploy 長跑過程中每輪自動重算」——目前實作並未如此（見下）。

**實作行為澄清（2026-03-25 追加，對齊 `trainer/serving/scorer.py`）**：

- **`DATA_DIR` 已設定**（`package/deploy/main.py` 在 import 前寫入 `os.environ["DATA_DIR"]`）：`score_once` 內 `use_persisted` 在載入邏輯中為真，只要 `canonical_mapping.parquet` 與 `canonical_mapping.cutoff.json` **存在且讀取成功**，**每一輪** scoring 都**從磁碟載入**同一份 mapping，**不會**每輪呼叫 `build_canonical_mapping_from_df`。**長時間運行**下檔案 **mtime 不變**屬預期，除非發生重建。  
- **何時重建並覆寫 Parquet**：檔案缺失、讀檔/解析失敗、或 `rebuild_canonical_mapping=True`。Deploy 路徑 `run_scorer_loop(...)` **固定**傳 `rebuild_canonical_mapping=False`，故穩定態幾乎永遠走「讀檔」。若建包時**已將** `canonical_mapping.parquet` 複製進部署 `data/`，程序可能**從未**走過「首輪從 sessions 現建」。  
- **無 `DATA_DIR`（本機預設）**：僅當 sidecar 的 `cutoff_dtm` 仍視為有效（程式以 `cutoff >= now` 判斷）才沿用磁碟檔；否則當輪重建。重建結果**僅在** `_DATA_DIR is not None` 時寫回磁碟（本機無 `DATA_DIR` 時通常不落盤，與 trainer 另寫 `repo/data/` 的路徑分離）。  
- **與 player_profile 對照**：profile 為**讀取**為主、TTL 快取；canonical 在 deploy 下為「**有檔則每輪讀檔**」，二者**都非**「每輪從 ClickHouse 重算整份 canonical map」。若產品需要**定期**反映新 `player_id` ↔ 卡號連結，需另案：排程刪檔、`--rebuild-canonical-mapping`、或改 scorer 邏輯（例如僅啟動時讀檔、之後每輪重建／節流重建）。

---

## DEC-029：部署階段預測日誌採用 MLflow（on-prem）

**日期**：2026-03-16  
**SSOT 章節**：doc/phase2_planning.md § 方向 #5 模型監控、§ 模型重訓與監控  

**決策**：部署環境的**推論／預測日誌**（每筆或每批預測的 request id、score、model_version、必要識別欄位等）以 **MLflow** 記錄。MLflow Tracking Server 自架於公司內網（on-prem），資料不輸出至外網。日誌用途包含：(1) **極端尾部人工抽查**（例如撈出高分 FP 的 patron/session 明細，判斷是否為內部測試、VIP、機台異常等）；(2) 後續 drift、分佈與表現分析。

**考慮過的替代方案**：
- **Arize Phoenix / WhyLabs / Fiddler 等**：專用於 ML 推論日誌與監控，但需額外引入一套軟體；團隊已規劃建置 MLflow，故不增加新依賴。
- **自建 SQLite/Parquet + 腳本**：輕量且 on-prem，但需自行維護 schema、查詢與權限；MLflow 現成可記錄 run/artifact/metric，自架即可滿足「記錄＋查詢」且與實驗/模型版本整合。
- **Datadog / New Relic 等 APM**：若公司已有，可打點 custom event；未列為首選因 MLflow 已納入規劃，集中於 MLflow 可單一維護。

**理由**：
- **單一軟體**：不引入新工具，與既有 MLflow 規劃一致，降低維運與學習成本。
- **On-prem**：Tracking Server 自架，資料留存公司內網，符合資安要求。
- **免費、可自架**：MLflow 開源，無授權費用。
- **可擴充**：日後可與 model registry、重訓 pipeline 同一套 MLflow 整合。

---

## DEC-030：Validator 與 Trainer 標籤／常數對齊（常數共用 config、僅 bet-based 邏輯）

**日期**：2026-03-17  
**SSOT 章節**：doc/validator_trainer_parity_plan.md、PLAN.md 項目 24  

**決策**：

1. **常數單一來源**：Validator（`trainer/serving/validator.py`）不再寫死 15／30／45 分鐘，改為從與 trainer 相同的 config 讀取：`WALKAWAY_GAP_MIN`、`ALERT_HORIZON_MIN`、`LABEL_LOOKAHEAD_MIN`（及既有 `VALIDATOR_EXTENDED_WAIT_MINUTES`、`VALIDATOR_FRESHNESS_BUFFER_MINUTES`）。`find_gap_within_window` 與 `validate_alert_row` 內所有時長均改為使用上述 config。
2. **標籤定義對齊**：Validator 的 MATCH／MISS 判決改為**僅依 bet stream**（與 `trainer/labels.py` 的 `compute_labels` 一致）。移除依 session 的 early return 與 late-arrival 判定；late arrival 僅以「是否有 bet 落在 (ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN]」為準，不再參考 session start。`session_cache` 參數保留以維持 API 相容，但不再用於 verdict。

**背景**：
- Trainer 的 label 來自 `labels.py`（僅 bet、使用 config 常數）；Validator 原先寫死 15/30/45 且含 session 路徑，存在常數與定義雙軌，導致 train–serve 標籤 parity 風險。
- 嚴謹的 precision@recall 與離線 true label 計算需與訓練時標籤定義一致，故 production 驗證邏輯應與 trainer 對齊。

**理由**：
- 常數改 config：未來若調整 WALKAWAY_GAP_MIN／ALERT_HORIZON_MIN，一處修改即同步 trainer 與 validator，避免隱性偏離。
- Bet-only：與訓練標籤定義一致，利於事後全量標註、precision@recall 計算與模型監控。

**實作計畫**：見 doc/validator_trainer_parity_plan.md（Step 1 常數改 config；Step 2 移除 session 路徑、late arrival 僅看 bet；測試與驗收）。

---

## DEC-031：特徵工程失敗即中止；數值 float32；train 指標避免全量稠密 predict

**日期**：2026-03-22  
**SSOT 章節**：§8.2（特徵工程）、§4.3（管線穩定性／記憶體）  
**關聯**：DEC-022（三軌）、DEC-023（DuckDB）、Plan B+ LibSVM 路徑  
**執行計畫**：[PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) 一節 **T-DEC031**

**背景（合併兩項問題）**：

1. **Silent failure**：部分 chunk 在 Track LLM 結果 `merge` 回 `bets` 時 OOM，外層 `try/except` 吞掉後管線繼續，導致**同一模型在不同月份缺 LLM 欄位**，產出與 Feature Spec 不一致，卻仍耗時完成訓練。  
2. **Train 指標 OOM**：Plan B+ 訓練雖可走 LibSVM，但 `_compute_train_metrics` 曾對整份 in-memory `X_train` 呼叫 `predict_proba`，觸發**稠密 float64 矩陣**（例如 50 × 3.9e7）配置失敗。與此相關，`merge`／pandas consolidate 亦常因 **float64** 放大單塊配置。

**決策**：

1. **Fail-fast（目前無可選軌道）**  
   Track Profile / Track LLM / Track Human 在 chunk 級特徵工程中**任一失敗即中止整條 pipeline**（不再僅 log 後繼續）。未來若引入可選軌道，再以顯式設定（`required` / `optional_tracks`）縮小範圍。

2. **數值預設 float32**  
   Track LLM（DuckDB 產出）、合併回主表之浮點特徵、以及進入 LightGBM 前可統一之數值欄，**預設 float32**（SQL `CAST`／pandas `astype`／Arrow），以降低峰值 RAM；更低精度（float16 等）另案評估。

3. **Train 指標：檔案推論優先 + 分批 fallback**  
   - 當 **Plan B+** 且存在 **`train_for_lgb.libsvm`**、路徑在 **`DATA_DIR` 下**、模型為 **Booster**：train 指標改為 **`_labels_from_libsvm(train_path)`** + **`booster.predict(str(train_path))`**（與 valid／test 檔案推論同一契約），避免在 Python 配置全訓練集稠密矩陣。  
   - **其餘路徑**（無 LibSVM、或檔案 predict 失敗回退）：`_compute_train_metrics` 內對 `X_train` **按列分批**，每批 **`to_numpy(dtype=float32)`** 後 **`booster.predict`**，批次大小由 **`TRAIN_METRICS_PREDICT_BATCH_ROWS`**（預設例如 `500_000`）控制。  
   - 無 `booster_` 的估計器維持既有 `predict_proba`（僅小資料或測試 mock）。

**實作計畫（checklist）**：

| 區域 | 工作 |
|------|------|
| `process_chunk` | 移除特徵工程「吞例外」；錯誤向上傳播或 log 後 **`raise`**。 |
| `features.py`／Track LLM SQL | 浮點產出統一 float32；merge 前避免被動升回 float64。 |
| Profile／Human | 盤點進模組型別，與缺失值策略相容下維持 float32。 |
| `train_single_rated_model` | `use_from_libsvm` 且路徑合法時，train 指標走 **LibSVM + 共用 scores→metrics 函式**；否則走 **`_compute_train_metrics`（分批）**。 |
| `trainer.py` | 新增 **`_batched_booster_predict_scores`**、**`_train_metrics_dict_from_y_scores`**；重構 `_compute_train_metrics` 使用分批 + 共用 dict。 |
| `trainer/core/config.py` | 新增 **`TRAIN_METRICS_PREDICT_BATCH_ROWS`**；`trainer.py` config 匯入兩路（package／legacy `config`）。 |
| 測試 | 特徵步驟失敗 → 非零 exit；可選：小表 dtype／train 指標分批或 mock LibSVM 路徑。 |
| 文件 | `doc/training_oom_and_runtime_audit.md` 或 PLAN 一句話引用 **DEC-031**。 |

**非本次必達／後續**：

- Step 9 前 **`pd.read_parquet` 全 train** 仍佔大量 RAM——與「train 指標 predict」分開；若要再降峰值需另議（例如延後載入 train 或僅 export 後不保留 `train_df`）。  
- 若需本機實驗「壞 chunk 略過」，未來可加 **`STRICT_FEATURE_ENGINEERING=False`**；**預設 strict**。

**回退**：還原 `try/except`、還原 train 指標單次 `predict_proba`、還原 float64（不建議於 full-window 本機跑）。

---

## DEC-032：線上閾值校準（prediction_log 標註）、runtime 閾值覆寫與 train／backtest 約束一致化

**日期**：2026-03-22  
**關聯**：DEC-026（Precision at recall=0.01）、DEC-027（`THRESHOLD_MIN_*` 命名與 config SSOT）、DEC-030（validator 與訓練標籤 bet-only 對齊）  
**執行計畫**：[PLAN_phase2_p0_p1.md](PLAN_phase2_p0_p1.md) 一節 **T-OnlineCalibration**

**背景**：

- 訓練時在 validation 上以 DEC-026 規則選阈（recall ≥ `THRESHOLD_MIN_RECALL`、min alert 筆數、可選 min alerts/hour）；線上僅使用 artifact 固定閾值時，分佈偏移會使實際 recall 偏離目標。
- `prediction_log`（Phase 2）已記錄每次 scorer 之 rated 預測；validator 僅對 **alerts** 做 MATCH/MISS，無法單獨還原「全量 PR 曲線」所需之負樣本標註。
- 需一條**低頻**、與 scorer／validator **並行**的管線：為預測補 ground truth、報表、並可選更新線上有效閾值。

**決策**：

1. **新腳本（不取代 validator）**  
   - **不**做推論（推論僅 `trainer/serving/scorer.py`）。  
   - 以 **ClickHouse** 為資料來源（與 validator 同源）；**標籤語意以訓練定義為準**（與 `compute_labels`／chunk 標籤管線一致，延續 DEC-030 之 bet-only 精神）。實作應**重用或對齊**訓練標籤邏輯，而非僅複製 validator 產出當唯一真理。  
   - **頻率**顯著低於 scorer（例如每 30 分鐘）；具體間隔與樣本下限可 config。

2. **Ground truth 儲存（`prediction_log.db`，方案 A）**  
   - 新增表（名稱可定稿，例如 **`prediction_ground_truth`**）：**一 `bet_id` 一筆**；欄位含 `label`（0/1）、`status`（**太新未成熟 → `pending`**）、`labeled_at`；可選 `prediction_id` 利於 join。  
   - **`pending` 不納入本輪校準**。遲到資料可能導致日後更正——現階段接受，不強制版本鏈；仍建議保留 `labeled_at` 以利稽核。

3. **可選稽核**：表 **`calibration_runs`**（每輪建議阈、是否寫入 state、skip 原因、窗長與樣本數摘要等）。

4. **Runtime 閾值覆寫（state DB，與 `alerts` 同庫）**  
   - 新增表（例如 **`runtime_rated_threshold`**）存放當前建議／生效之 `rated_threshold` 與 metadata。  
   - **Scorer 讀取順序**：有效且（可選）未過期之 runtime 列 → 否則 fallback **artifact `rated.threshold`**。  
   - **`prediction_log.db` 不作** scorer 讀取閾值來源（日誌與執行期參數分離）。  
   - 可選 config：**`RUNTIME_THRESHOLD_MAX_AGE_HOURS`**，避免校準 job 長停後長期沿用舊阈。

5. **選阈與約束 — 全專案單一 config 來源**  
   - **`trainer/core/config.py`**：`THRESHOLD_MIN_RECALL`、`THRESHOLD_MIN_ALERT_COUNT`、`THRESHOLD_MIN_ALERTS_PER_HOUR`。  
   - 若 **`THRESHOLD_MIN_ALERTS_PER_HOUR is None`**：trainer、backtest、線上校準**一律不**套用每小時密度檢查（僅 recall + min alert count）。  
   - **線上校準與 backtest** 對 **每小時告警**之口徑：**整段校準／評估窗一個 `window_hours`**，要求 `n_alerts_at_threshold / window_hours >= THRESHOLD_MIN_ALERTS_PER_HOUR`（當該常數非 `None`）。`window_hours` 由本輪納入校準之成熟列之時間跨度（如 `scored_at` max−min）導出，並設下限避免除零。  
   - **不**在本階段要求逐小時桶或逐 gaming_day 強制通過。

6. **共用實作**  
   - 抽出 **`select_threshold_dec026(scores, labels, window_hours=None)`**（名稱可定稿）：trainer、backtester、校準腳本共用。validation 路徑傳 **`window_hours=None`** 時僅套用 min alert **筆數** + recall；有 `window_hours` 時另套用 **`THRESHOLD_MIN_ALERTS_PER_HOUR`**。  
   - **Backtest**：`compute_micro_metrics` 之 PR oracle（`test_precision_at_recall_*` / `threshold_at_recall_*`）須與上述 **同一 `valid_mask`**（現況 oracle 僅 `pr_rec >= r`，**待程式對齊**）。

7. **與 validator 的關係**  
   - Scorer、validator、校準腳本**並行**監控；validator **不**被本腳本取代。  
   - 因遲到資料與快照差異，**validator 結果不強求與校準 label 逐筆一致**；營運驗證仍以 validator 為準，離線 metric／選阈語意以訓練標籤為準。

**考慮過的替代方案**：

- **僅用 validator 對 alerts 標註推全 PR**：負樣本不完整，無法還原與訓練一致之 operating point 搜尋。  
- **把 runtime 阈寫在 `prediction_log.db`**：與高頻日誌同庫、語意混淆；scorer 已分離 state／prediction log，維持分離較清晰。  
- **未校準之未標記資料強行當負例**：會扭曲 recall／precision，拒絕採用。

**理由**：

- 單一 config 與單一選阈函式可降低「訓練說一套、backtest／線上另一套」之混淆。  
- 成熟門檻 + `pending` 避免在 label 未結算時誤校準。  
- State DB 覆寫使線上可漸進適應分佈偏移，且 failure 時可 fallback bundle。

**實作**：見 **T-OnlineCalibration**（新腳本路徑、測試、Rollback、DoD 已列於該節）。

---

## DEC-033：線上 Scorer payout-age cap + Deploy flush 參數分流（state / prediction / all）

**日期**：2026-03-24  
**SSOT 章節**：[.cursor/plans/PATCH_20260324.md](PATCH_20260324.md) — Task 1  
**關聯**：Guardrails（特徵全窗 vs 進入模型之路徑）

**決策**：

1. **環境變數 `SCORER_COLD_START_WINDOW_HOURS`**（名稱保留 “cold start”，但**每輪皆套用**）：當設為正數時，僅對 **`payout_complete_dtm` 落在最近 N 小時內**（香港時間、與欄位對齊後比較）的 **new bets** 執行 **模型推論、寫入 prediction log、產生 alerts**。未設或 `<=0` 或非法 → **關閉過濾**（行為與舊版一致）；數值 **cap** 於 **`SCORER_LOOKBACK_HOURS_MAX`**。
2. **不縮減特徵視窗**：`update_state_with_new_bets`、ClickHouse fetch 視窗、**`build_features_for_scoring(bets, …)`** 仍依完整 lookback；**UNRATED／rated 計數 telemetry** 仍依 **全部** `new_bets`（如 `new_bet_ids_all`），避免日誌語意失真。
3. **`package/deploy/main.py` flush 參數**（預設不 flush；建議互斥使用）：
   - `--flush-state`：僅於啟動 scorer／validator／Flask **前** 刪除 **`STATE_DB_PATH`** 對應之 SQLite 主檔與 `-wal`/`-shm`。
   - `--flush-prediction`：僅刪除 **`PREDICTION_LOG_DB_PATH`** 對應之 SQLite 主檔與 `-wal`/`-shm`。
   - `--flush-all`：同時刪除 state 與 prediction 兩者。

**理由**：在維持與訓練一致之特徵建置前提下，降低進入模型與 I/O 的負載；同時將 flush 控制顯式分流，讓運維可依場景精準清理 state、prediction 或兩者，避免誤刪。

---

## DEC-034：`/alerts` 與 `/validation` 無查詢參數時預設視窗改為最近 1 小時

**日期**：2026-03-23–24  
**SSOT 章節**：[package/ML_API_PROTOCOL.md](../../package/ML_API_PROTOCOL.md) §1–2；[PATCH_20260324.md](PATCH_20260324.md) — Task 2  
**關聯**：現場回報大 payload／慢回應（舊預設 24h + 全表進 pandas 再過濾）

**決策**：

1. 對外協定由「無參數 = 最近 **24** 小時」改為「無參數 = 最近 **1** 小時」（香港時間語意、與既有 timestamp 欄位一致）。
2. **`/alerts`**：`ts` → 僅回傳 `ts_dt` 在該時間**之後**；`limit` → **僅在未提供 `ts`** 時截尾；無參數 → `ts_dt > now_HK − 1h`。
3. **`/validation`**：`ts` → `validated_at` 在該時間之後；`bet_id`／`bet_ids` → 依 ID 篩選（此時**不**套用 1h 預設窗）；僅在無上述參數時套用 **1h** 預設窗。
4. Task 2 **未**做 SQL 層級下推；全表讀取後記憶體過濾若仍成瓶頸，改由 Task 3 Phase 4 處理。

**理由**：預設 1h 可大幅縮小單次 JSON；參數語意維持利於既有增量拉取（`ts`）與 `/alerts` 之 `limit` 協定。

---

## DEC-035：推論路徑效能優化工作流（不改模型／不中斷輪詢週期）

**日期**：2026-03-24  
**SSOT 章節**：[PATCH_20260324.md](PATCH_20260324.md) — Task 3  
**關聯**：DEC-030（validator bet-only、`session_cache` 不參與 verdict）、DEC-027（**不**合併 `SCORER_POLL_INTERVAL_SECONDS`）

**決策**：

1. **範圍與護欄**：優化 scorer／validator／deploy Flask 路徑之延遲與 I/O；**不重訓、不改模型權重**。**刻意不**以調整 **`SCORER_POLL_INTERVAL_SECONDS`** 作為此工作流手段（維持現狀，與 DEC-027「各元件週期獨立」精神一致）。
2. **分階段執行**（實作順序見 PATCH）：Phase 0 量測基線 → Phase 1 Validator 停 session CH → Phase 2 Validator `validation_results` 增量讀 → Phase 4 Flask SQL 下推 → Phase 3 Scorer 增量特徵與 SQLite 批次等 → Phase 5 ClickHouse 文件分析。
3. **Validator Phase 1（相容過渡）**：walkaway 判決已僅依 **bet 流**（DEC-030）；**保留** `validate_alert_row(..., session_cache, ...)` **簽名**，但 **不再**呼叫 `fetch_sessions_by_canonical_id`，**一律傳入空 `dict`**。canonical_id ↔ player_id 合併仍來自 **alerts** 與 **bet** fetch，**不依賴** validator 之 session 快取。
4. **Validator Phase 2 前提**：目前**無**「手動改 DB」或「全量重算 processed 狀態」之維運計畫，增量讀取**不需**為此另建完整 fallback 路徑（若日後維運需求出現再單獨議題補上）。
5. **Scorer 增量特徵**：允許相對全量重算之 **極小數值差**（浮點／合併順序），须於驗收或文件中註明容許範圍或對照方式；**schema／下游欄位相容**仍為必達。
6. **Phase 4 範圍**：API 之 SQL 優化 **僅限** [`package/deploy/main.py`](../../package/deploy/main.py) 內 Flask 路由；端點與查詢參數 **以** [`package/ML_API_PROTOCOL.md`](../../package/ML_API_PROTOCOL.md) **為準**。
7. **Phase 5 交付物**：於 [`doc/task3_clickhouse_sql_analysis.md`](../../doc/task3_clickhouse_sql_analysis.md) 對 **每條相關 SQL** 做專項設計分析（目的、欄位、時間窗、風險、可瘦身點）；**不要求**附線上實測或 `EXPLAIN` 實跑。
8. **輔助手段**：確認 **numba** 於 production 可用；**SHAP reason code** 維持預設關閉或限縮（與既有 `SCORER_ENABLE_SHAP_REASON_CODES` 等設定一致）。

**理由**：在不大改模型與決策語意之前提下，優先拿掉無效 I/O（validator session）、降低 SQLite／API 全表讀取、並以增量化與查詢下推處理資料成長；CH 以文件化分析先固化風險再逐步改 SQL。

---

## DEC-036：Task 7 R1（chunk cache `data_hash`）順序不敏感——若採序列模型須重審

**日期**：2026-03-24  
**SSOT 章節**：[PATCH_20260324.md](PATCH_20260324.md) — Task 7 / R1  
**關聯**：Step 6 chunk Parquet 快取（[`trainer/training/trainer.py`](../../trainer/training/trainer.py) `_chunk_cache_key`／`process_chunk`）

**背景**：

- R1 目標是以**順序不敏感**的 `data_hash`（或等效 commutative 指紋）減少「同一批 `bets`、僅因來源回傳列順不同」造成的假 cache miss。
- **目前主力模型**為 tabular／tree-based（LightGBM），訓練表徵在慣例上把每筆 bet 當獨立列；同一組合內列重排通常**不改**標籤與多數手算特徵的語意。

**決策（紀錄性／條件式）**：

1. **在現行 GBDT／tabular 路線下**，R1 與「多重集合」式 `bets` 語意對齊，可作為 cache 指紋策略的預設假設。
2. **若未來引入序列模型**（例如 **Temporal Fusion Transformer** 或其他 **explicit sequence** 架構），**原始或預處理後的列順序可能成為模型輸入語意的一部分**；此時**不得**在未重新審查的情況下沿用「純順序不敏感」的 chunk `data_hash` 作為唯一有效性判斷。
3. **遷移時應至少擇一**（可多項並行）：
   - 改回 **order-aware** fingerprint，或
   - **先將 bets 依契約排序**（例如 `bet_time`、tie-break `bet_id`）再計算 hash，使「語意序列」穩定，或
   - 將 `.cache_key`／`data_hash` **與 model family + sequence 定義版本**綁定，避免錯誤共用舊 chunk cache。

**理由**：避免在模型假設從「列無序等價」變為「序列有意義」時，仍以舊快取指紋誤判命中，導致訓練資料與模型假設不一致（高風險語意錯誤）。

---

## DEC-037：Validator ClickHouse bet 拉取視窗改為「最舊待驗時間窗 + 上限保護」（方案 1）

**日期**：2026-03-25  
**SSOT 章節**：[PATCH_20260324.md](PATCH_20260324.md) — Task 9  
**關聯**：DEC-030（validator bet-based verdict）、DEC-035（推論路徑效能優化護欄）

**背景**：

- Validator 需從 ClickHouse 拉取 bet 時序（依 `player_id` 合併到 `canonical_id`）以驗證 alerts（MATCH/MISS/PENDING）。
- 現行以固定 `fetch_start = min(effective_ts[pending]) - 1h` 拉 bet，對「alert 新但 `bet_ts` 舊」（例如補資料、重跑、延遲發報）容易**窗太短**，造成 `bet_cache` 空、重複出現 `No bet data ... leaving PENDING`，且即使超過政策允許的 late-arrival 時間仍無法定案。
- 既定政策：walkaway 驗證最遲接受窗口約 **45–47 分鐘**（`LABEL_LOOKAHEAD_MIN=45` + `VALIDATOR_FRESHNESS_BUFFER_MINUTES=2`；另有執行期 grace），超過後不再接受新 late arrival。

**考慮過的替代方案**：

1. **Per-alert union 視窗**：對每筆 pending alert 用 `[bet_ts - x, bet_ts + y]` 取 union 後查詢（語意最精準，但 SQL 複雜、CH 壓力與維護成本高）。
2. **只讀 scorer 本地資料**：validator 不查 ClickHouse，改從 scorer 的本地資料（例如 state DB 或另存 DM）驗證（需高度依賴 scorer uptime／retention；仍需 fallback 以補洞）。

**決策**：

- 採用 **方案 1（最舊待驗時間窗）**：以「所有待驗 alerts 中最舊的 `effective_ts`」決定本輪 ClickHouse 拉取起點，並加入「最大回看上限」避免無界回溯造成 ClickHouse 壓力失控。
- 將參數 **config 化**（`VALIDATOR_FETCH_PRE_CONTEXT_MINUTES`、`VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES`），並加入自洽檢查：`MAX_LOOKBACK` 必須覆蓋 policy late window + pre-context，否則提升/告警。

**理由**：

- 以最小實作複雜度修正 correctness：避免固定 `-1h` 對舊 `bet_ts` 漏資料，降低長期 PENDING 與誤判風險。
- ClickHouse 壓力可控：最大回看上限提供硬邊界，避免「積壓很久的 pending」造成大窗掃描。
- 與既有架構相容：仍沿用現行 `IN` chunking（R44）與「DB error abort cycle」（R41）設計，不引入 per-alert SQL 拼接。

**後果 / 實作要點**：

- Validator 每輪需額外記錄（DEBUG）`pending_min_ts`、`candidate_start`、`hard_floor`、`fetch_start`、policy 相關常數，以便 production 調參與追查。
- 若 scorer/validator 長時間停機導致 pending 積壓，仍可能觸發較大 `max_lookback` 視窗；需搭配 retention/runbook 或後續 hybrid（本地 bet cache + CH fallback）策略。

---

## DEC-038：Validator 滾動 precision 上界採「驗證週期結束時刻」（與 `validated_at` 一致）

**日期**：2026-03-26  
**SSOT 章節**：[PATCH_20260324.md](PATCH_20260324.md) — **Task 11**；延續 Task 4／DEC-034 護欄下之 serving 觀測 KPI  

**背景**：

- 線上滾動 KPI：`trainer/serving/validator.py` 之 `_rolling_precision_by_validated_at` 以 **`validated_at` 落於 `[now − window, now]`**（HK）計算 15m／1h precision，並以 15m 結果寫入 `validator_metrics`。
- 實作上 `validate_once` 曾以**週期開頭**之 `now_hk` 作為該 `now`，而每筆結果之 **`validated_at`** 於 `validate_alert_row` 內以**該筆驗證當下**之 `datetime.now(HK_TZ)` 寫入。若週期內有 I/O 與多筆處理，常見 **`validated_at` 晚於週期起點**，導致 **`validated_at <= now`（週期起點）** 不成立，同一輪剛驗證之列被濾光，日誌出現 **`0/0`**，與同輪「N alert(s) verified」並存；**首輪**無歷史列時尤其明顯。

**考慮過的替代方案**：

1. **在呼叫滾動 KPI 前刷新 `now`**（上界＝**週期結束時刻**）：保留 `validated_at` 為「實際驗證完成時間」；僅將 KPI 上界與其對齊。實作面小、稽核語意不變。
2. **將 `validated_at` 改為週期起點或統一錨點時間**：可機械對齊窗格，但**扭曲** `validation_results`／API **`sync_ts`** 之「何時完成驗證」語意，不利稽核與除錯。
3. **移除上界、僅 `validated_at >= now − window`**：會把**未來時間戳**（時鐘誤差、異常資料）納入，需另定防禦規則。

**決策**：

- 採用 **方案 1**：滾動 KPI（15m／1h）之 **`now` 上界**語意為 **`validate_once` 執行至滾動計算／寫入 metrics 時之「週期結束」時刻**（由呼叫端以 `datetime.now(HK_TZ)` 取得，與現行每筆 `validated_at` 之產生方式相容）。
- **週期開頭**之 `now_hk` **仍保留**於 retention、finality、fetch 窗、pending 篩選等路徑，避免單一變更牽動整輪時間語意。
- **`validator_metrics.recorded_at`**：預設與該 KPI 上界（週期結束錨點）一致，除非產品明確要求以週期起點記錄（須文件化二選一）。

**理由**：

- **與 `validated_at` 定義一致**：完成驗證的列應納入「以週期結束為上界」之滾動窗，避免假陰性與「延遲一輪才出現分母」的運維困惑。
- **不犧牲資料真實性**：相較統一改寫 `validated_at`，更利於對外 API 與事後追查。

**實作追蹤**：見 [PATCH_20260324.md](PATCH_20260324.md) **Task 11**。

---

## DEC-039：Step 6 兩階段 prefeatures 快取預設開啟；local `data_hash` 可攜式指紋（fp_v2）

**日期**：2026-04-07  
**SSOT 章節**：`.cursor/plans/PLAN_chunk_cache_portable_hit.md`；`trainer/core/config.py`；`trainer/training/trainer.py`（Task 7 R5/R6）

**決策**：

1. **`CHUNK_TWO_STAGE_CACHE`**：預設改為 **啟用**（`CHUNK_TWO_STAGE_CACHE_DEFAULT=True`），以重用 `*.prefeatures.parquet`、在僅變更下游 spec／neg 時略過 Track Human。環境變數 **顯式** `0`／`false`／`no`／`off`／`on`／`1` 等可覆寫；非法值記 warning 後回退預設。
2. **Local Parquet `data_hash`**：移除對 **`st_mtime_ns`** 的依賴；改以 **檔案大小**、footer **`num_rows`**、以及 **`fp_v=2`** 之 **row group 統計 + Parquet schema（路徑／physical／logical type）** 之 SHA256 短 digest 組 token，使 **複製／換機** 仍可比對命中；**首次升級後**既有 `.cache_key` 與 prefeatures sidecar 會與新指紋不一致 → **預期全 miss 重算 Step 6**。

**理由**：mtime 僅反映複製時間，導致可攜 miss；schema／row group 納入 digest 可降低「同 size／同 nrows 不同欄位」之 silent false hit，並排除 file-level `created_by` 依賴。

**風險與緩解**：prefeatures hit 仍為整表 `read_parquet`、miss 時雙寫 Parquet — 見 `doc/training_oom_and_runtime_audit.md` 與 PLAN 文件。

---

## DEC-040：模型載入與建包僅接受 `model.pkl`（廢止 rated／walkaway 備援與產出）

**日期**：2026-04-20  
**SSOT 章節**：`README.md`（產物／部署）；`package/PLAN.md`；`trainer/serving/scorer.py`、`trainer/training/backtester.py`

**決策**：

1. **`trainer.serving.scorer.load_dual_artifacts`** 與 **`trainer.training.backtester.load_dual_artifacts`** 僅從 bundle 目錄載入 **`model.pkl`**。若檔案不存在則 **立即** `FileNotFoundError`；**不**讀取 `rated_model.pkl` 或 `walkaway_model.pkl`（若僅存在這些 legacy 檔，錯誤訊息可提示其存在但未被載入）。
2. **訓練產物**：`save_artifact_bundle` **不再**寫入 `walkaway_model.pkl`。`run_pipeline` 成功收尾時一併刪除殘留的 `walkaway_model.pkl`（與既有 `nonrated_model.pkl`／`rated_model.pkl` 清理一致）。
3. **`package/build_deploy_package.py`**：`--model-source` 必須含 **`model.pkl`** 與 `feature_list.json`；不再將 `rated_model.pkl`／`walkaway_model.pkl` 視為可替代主模型或列入預設複製清單。

**理由**：

- 備援鏈易在部署錯置時 **靜默使用舊模型**，與「單一真實來源」的 v10 bundle 契約相衝突。
- `walkaway_model.pkl` 與 `model.pkl` 內容重複，維運成本高且易誤解。

**遷移**：僅有 legacy pickle、尚無 `model.pkl` 的目錄必須重新訓練或由維運手動產出 v10 bundle；不接受再以舊檔名啟動 scorer／backtester。

---

*本文件隨專案演進持續更新。新決策請沿用 `DEC-XXX` 編號格式。*
