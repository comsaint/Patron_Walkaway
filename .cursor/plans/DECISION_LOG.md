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

*本文件隨專案演進持續更新。新決策請沿用 `DEC-XXX` 編號格式。*
