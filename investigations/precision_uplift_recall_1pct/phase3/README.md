# Phase 3：特徵深化與集成收斂

## 為什麼要做這一階段

Phase 2 勝者路線已證明「策略方向」有效；Phase 3 在**不推倒重來**的前提下，用**定向特徵**與**受控集成**再擠增益，並處理**高分段校準**，避免整體數字改善但實務告警品質（top band）仍差。

## 如何調查

- **只做在勝者路線上**：新特徵與集成變更須能對應到同一 `experiment_id`／特徵版本脈絡。
- **動態行為特徵**：短中長期差值、變化率、波動、連續性等；以 `feature_uplift_table` 對照「加什麼、長多少、主指標與切片怎麼動」。
- **拖累切片 feature pack**：僅對 Phase 1/2 識別的 top 拖累切片擴充相關特徵，避免全庫盲擴。
- **集成／融合消融**：群內最佳 + 群間融合等非盲目堆疊；記錄複雜度與 uplift 是否值得。
- **高分段校準**：針對高分區段做校準與 decision policy 檢查，對齊營運可解釋的誤報率。

全程須監控 **跨窗穩定性**（不能只為單一 holdout 優化）。

## 預期產出（應填寫檔案）

| 檔案 | 用途 |
| :--- | :--- |
| `feature_uplift_table.md` | 特徵加入與指標對照 |
| `slice_targeted_features.md` | 切片定向特徵包與效果 |
| `ensemble_ablation.md` | 集成方案與消融結論 |
| `top_band_calibration_report.md` | 高分段校準與政策 |
| `phase3_gate_decision.md` | Phase 3 Gate 與定版候選傾向 |

## 可依此做出的決策

- **進入 Phase 4（定版前最後加碼完成）**：相對 Phase 2 勝者**再提升**，且**未犧牲跨窗穩定性**；拖累切片有**可驗證改善**（至少若干個關鍵切片）。
- **捨棄複雜集成**：若 ensemble 僅微幅提升但維運成本／延遲明顯上升 → 採較簡方案進 Phase 4。
- **退回 Phase 2**：若加深後穩定性崩壞或主指標回吐，應回到勝者較簡配置或調整路線。

---

## 應填寫檔案（清單）

- `feature_uplift_table.md`
- `slice_targeted_features.md`
- `ensemble_ablation.md`
- `top_band_calibration_report.md`
- `phase3_gate_decision.md`
