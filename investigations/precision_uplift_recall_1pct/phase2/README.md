# Phase 2：高槓桿建模路線（Track A / B / C）

## 為什麼要做這一階段

Phase 1 鎖定**同一評估契約**後，需要驗證「哪種建模策略」最能提升 `precision@recall=1%` 與前段排序品質，且不是單一時間窗僥倖。並行 A/B/C 可避免團隊押在單一路線上過早收斂。

## 如何調查

前提：**資料窗、切分、標籤契約與 Phase 1 結論對齊**，不可中途換口徑比較。

- **Track A（排序／誤報）**：排序導向目標、class weighting／focal-like、hard negative mining（針對高分假陽性加權）。
- **Track B（分群）**：依玩家狀態／活躍度等做 2～4 群，子模型 + gating，降低單一全局模型的欠擬合。
- **Track C（穩定性）**：Forward／Purged 時序驗證，輸出 mean／std，過濾只在單窗漂亮的配置。

每條路線應至少覆蓋 **2 個以上時間窗**（或等效時序折疊），並記錄實驗矩陣欄位（見總計畫 §實驗登錄契約）。

## 預期產出（應填寫檔案）

| 檔案 | 用途 |
| :--- | :--- |
| `track_a_results.md` | Track A 實驗與指標 |
| `track_b_results.md` | Track B 實驗與指標 |
| `track_c_results.md` | Track C 實驗與指標 |
| `phase2_gate_decision.md` | 勝者路線、淘汰理由、是否進 Phase 3 |

每份 track 建議至少包含：`precision@recall=1%`、輔助指標（如 PR-AUC、top-k）、`slice_metrics`、`cv_mean_std`（若適用）、與基線對照。

## 可依此做出的決策

- **進入 Phase 3**：至少 **1 條路線**相對基線有 **顯著 uplift**（建議門檻約 **+3～5pp**），且跨窗波動可接受；明確指定**勝者路線**與實驗 id。
- **某路線淘汰**：記錄淘汰理由（無 uplift／不穩定／成本過高），避免重複試錯。
- **重開 Phase 1**：若實驗過程發現契約或標籤定義仍漂移，應回到 Phase 1 再釐清后再比較。

---

## 應填寫檔案（清單）

- `track_a_results.md`
- `track_b_results.md`
- `track_c_results.md`
- `phase2_gate_decision.md`
