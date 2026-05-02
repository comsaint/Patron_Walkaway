# Time Semantics Registry — PR 檢查清單（LDA-E0-01）

適用於修改 **`schema/time_semantics_registry.yaml`** 的 PR。合併前須完成下列項目；與自動檢查重疊處以 CI 為準，其餘為人工審核。

**GitHub（可選）**：建立 PR 時可選範本 **「time_semantics_registry」**（網址加 `?template=time_semantics_registry.md`）。

## 自動檢查（必須綠燈）

- [ ] 自 repo 根目錄執行：`python scripts/validate_time_semantics_registry.py`
- [ ] Phase 0 合約總查：`make check-layered-contracts` 或 `python scripts/validate_layered_contracts.py`

## 文件與治理

- [ ] 變更理由已寫在 PR 描述（新表登錄、欄位語意修正、`dedup_rule_id` 變更等）
- [ ] 已對照 **`ssot/layered_data_assets_run_trip_ssot.md` §4.4**（`event_time` / `observed_at`、correction vs late、manifest 留痕意圖）
- [ ] 若牽涉 **preprocessing** 規則：已註明將在 **`doc/preprocessing_layered_data_assets_v1.md`**（LDA-E0-02）如何對齊或已開後續追蹤項

## 每表必填欄位（結構）

下列鍵須存在且型別／內容符合腳本與 SSOT；詳見 registry 檔頭註解與 SSOT §4.4。

| 鍵 | 說明 |
|----|------|
| `description` | 表用途與在分層資產中的角色 |
| `business_key` | 業務主鍵欄位（列表）；須為 schema dict 中該表已列欄位 |
| `event_time_col` | 業務事件時間表達式（可含 `COALESCE(...)`）；所含識別字須皆出現在該表字典列 |
| `observed_at_col` | 可觀測／入湖 proxy；預設常為 `__etl_insert_Dtm` |
| `update_time_col` | 版本／更新時間欄；無則 `null` |
| `partition_cols` | 分區欄位列表（可為空列表，但須明示） |
| `dedup_rule_id` | 與 preprocessing 規格對齊之 rule id（如 `preprocess_*_v1`） |
| `preprocessing_contract` | 非空字串列表；具體 DQ／去重與 FND 對照見下節 |
| `correction_expected` | `true` / `false` 或尚未釐清時 **`TBD`** |
| `late_arrival_expected` | 同上 |
| `expected_delay_profile` | 延遲輪廓文字說明（可述明待監控合約補數值） |
| `late_threshold` | 數值閾值或 **`TBD`** |
| `notes` | 非空列表；至少一條（設計取捨、與 trainer／FND 對齊說明等） |

## Schema 字典對照（`schema/GDP_GMWDS_Raw_Schema_Dictionary.md`）

- [ ] 該表在字典中有 **`## N. t_xxx`** 一節，且 registry 內所有 **business_key**、**partition_cols**、以及 `event_time_col` / `observed_at_col` / `update_time_col` 拆出的識別字，皆可在該節表格的欄位列找到
- [ ] 若字典尚無完整欄位列（例如僅列名）：應先擴寫字典或註明「僅列名驗證」之例外並由 **Data Platform** 簽核（避免 CI 與實務脫節）

## FND / FINDINGS 對照（人工）

依表選擇性核對 **`doc/FINDINGS.md`**（同一 PR 不必重複全文，但新表或時間語意變更必查）：

| 表 | 建議至少核對之 FND（非 exhaustive） |
|----|--------------------------------------|
| `t_bet` | FND-06/07/08/13（時間與欄位可用性）；run/trip 排序與 `payout_complete_dtm` |
| `t_session` | FND-01（去重）、FND-02/03/04、FND-11/12/13/16（時間、身分、晚到） |
| `t_game` | FND-13/14/15（時間、重複版本、財務欄） |
| `t_shoe` | 字典可用性說明；欄位與延遲是否仍為 **TBD** |

- [ ] `preprocessing_contract` 條文與上列 FND **無明顯矛盾**（若有，於 PR 說明採用哪份文件為準並是否需回寫 FINDINGS）

## Reviewer

- [ ] **ML Platform** 或 **Data Platform** 至少一方已 Approve（與 execution plan Owner 對齊）

---

**非 GitHub CI 時**：請在 merge 前手動跑驗證腳本，或將同等步驟加入既有 CI（Azure DevOps、GitLab CI 等）。
