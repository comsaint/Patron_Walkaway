# trainer_hightier - Cache Redesign SSOT

本文件屬於 **SSOT 層**，定義 high-tier training cache redesign 的治理真相：要解決什麼問題、哪些語意不可改、cache 邊界如何劃分、以及成功標準。  
本文件不包含 ticket 級任務拆解；實作策略由 `Cache Redesign - IMPLEMENTATION_PLAN.md` 承接，具體 execution plan 應另建 working plan。

## 1) Objective

重新設計 `trainer_hightier` 訓練管線快取，使其在以下產品與資料假設下仍可正確、可追溯且高重用：

- Source Parquet folder 在更新時可能整包覆寫，檔案 `mtime` 不可信。
- 實際資料通常只新增或修改近期月份，但偶爾會修改一兩個歷史檔案。
- Training universe 只需要 rated patrons。
- ADT quantile 會隨實驗或營運策略上升 / 下降。
- Feature registry 會頻繁新增、移除或調整特徵。
- Cache hit 必須可解釋；source identity 不確定時不得 silent reuse。

核心目標是把 cache 從「整份資料源 / 整份 cleaned artifact / exact feature list」重構為：

- content-addressed source layer
- latest/global rated universe layer
- shared entity set layer
- supplier-family feature primitive layer
- registry-driven assembly layer

## 2) Scope

### In Scope

- Source Parquet fingerprint 與 per-file / per-partition invalidation。
- Session / bet source cleaning cache 邊界重定義。
- ADT rated universe 與 quantile selection cache。
- Training entity set cache。
- Labels cache invalidation contract。
- Feast retrieval / short-term PIT / mid-term / slow feature primitive cache 邊界。
- Feature registry 變更時的 cache reuse 規則。
- Cache manifest、atomic write、hit/miss observability 與 correctness gate。

### Non-Scope

- 變更 walkaway label business definition。
- 變更 ADT 訓練策略為 historical row-level as-of ADT。
- 模型家族、threshold policy 或 alert policy 重設計。
- Serving runtime 的完整重構；本 SSOT 只要求 training artifact 與 serving contract 不漂移。
- Ticket 級 work breakdown。

## 3) Governing Decisions

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| D-001 | Source fingerprint 不使用 `mtime` 作 correctness key。 | 更新流程會整包覆寫 folder，`mtime` 會 false miss；`mtime` 僅可作診斷欄位。 |
| D-002 | MVP source fingerprint 使用 full file SHA-256。 | Parquet 沒有可普遍信任的 whole-file content hash；MVP 優先正確性。 |
| D-003 | Source cache 以 file / partition 為失效粒度，不以整份 data source 為粒度。 | 偶發歷史檔修改時只重算受影響 partition 與 downstream dependency window。 |
| D-004 | Source cleaned cache 不包含 ADT quantile、selected universe 或 feature registry。 | ADT 與 feature selection 是下游策略，不是 L0 cleaning 語意。 |
| D-005 | ADT universe SSOT 為 `canonical_id` level latest/global rank table，`player_id` 為 projection。 | ADT/profile 是 canonical patron 語意；raw bet/session join 才需要 `player_id`。 |
| D-006 | Training 接受用 latest/global ADT 去篩 historical training rows。 | 這是現有設計；不採 row-level historical as-of ADT。 |
| D-007 | Quantile 是 rank table filter，不進 source cache key。 | Quantile 上升 / 下降不應讓 source cleaning 或 primitive feature cache 全量失效。 |
| D-008 | Quantile 降低時支援 delta fill。 | 只對新增 `canonical_id` / `player_id` / entity rows 補算 feature，不重算既有 entity rows。 |
| D-009 | Shared entity set 是 Step 3 / Step 3.5 的共同輸入。 | 先收斂到 rated + training-scope entity，再供 Feast / short PIT / mid snapshot 使用，避免全量算完再 filter。 |
| D-010 | Feature primitive cache 按 supplier family + window，而非 exact baseline feature list。 | Feature add/remove 應多數停在 assembly 層，不重跑 expensive supplier。 |
| D-011 | Current cleaned bet segment cache 由 entity set 路徑直接替換。 | ADT segmentation 不再屬於 cleaned bet preprocess；不維持長期雙軌。 |
| D-012 | Labels 受 canonical sequence 與 horizon/gap 規則影響，需有自己的 invalidation window。 | 修改某 bet 可能影響同 canonical 前後相鄰 bet 與 horizon 內 labels。 |
| D-013 | Cache write 必須 atomic。 | 不可因 OOM / 中斷留下看似可用的半成品。 |
| D-014 | Source identity 不確定、schema drift 或 manifest corrupt 時 fail-fast。 | Cache miss 可 recompute；cache correctness 不確定不可 silent fallback。 |
| D-015 | Shared cache 與 run-local artifact 分層。 | Source / primitive feature cache 可 shared；assembly / model output 為 run-local 或 run-scoped。 |

## 4) Domain Contracts

### 4.1 Source Identity

Source Parquet identity 由 content-addressed manifest 定義：

- table name
- relative path
- partition key / month
- file size
- row count
- row group count
- schema hash
- file SHA-256
- optional diagnostic fields such as `mtime_ns`

`mtime_ns` 不得參與 hit/miss correctness。

### 4.2 ADT Universe

ADT universe 以 latest/global profile 與 canonical mapping 產生 rank table：

- `canonical_id`
- `player_id` projection
- `adt`
- `adt_rank`
- `adt_percentile`
- `has_slow_window_coverage`
- profile snapshot fingerprint
- mapping fingerprint
- slow active anchor / coverage policy

Quantile selection 是 rank table filter，例如 `adt_percentile >= q`，不是重建 source cache 的理由。

### 4.3 Entity Set

Entity set 是訓練與 feature supplier 的共同輸入，至少包含：

- `bet_id`
- `canonical_id`
- `player_id`
- event / prediction timestamp
- `gaming_day_event`
- source partition month
- selected quantile / universe fingerprint
- training scope fingerprint

Entity set 變更才驅動 downstream feature primitive 的新增補算或受影響月份重算。

### 4.4 Labels

Walkaway labels 由 canonical-level ordered bet sequence 決定：

- `WALKAWAY_GAP_MIN` 決定 gap 是否成立。
- `ALERT_HORIZON_MIN` 決定 gap start 是否落在 label horizon 內。
- `LABEL_LOOKAHEAD_MIN` 在目前 offline label path 主要是 boundary / validator 語意，不是 offline label 的主 horizon。

Label cache invalidation 必須考慮 same canonical 的前後相鄰 bet。MVP 的 month-level 安全規則為：

- dirty bet month
- previous month
- next month

後續可升級為精準 time-range invalidation。

### 4.5 Feature Primitive

Feature primitive cache 按 supplier family + window 定義，例如：

- `short_term:w1h`
- `short_term:w6h`
- `mid_term:w30d`
- `slow:monthly_180d`
- `feast_group:<service>/<group>`

Feature registry 的 exact baseline feature list 只影響 assembly cache。新增 feature 若可由既有 primitive 推出，不得導致 primitive cache miss。

## 5) Invalidation Rules

### 5.1 Source Changes

Per-file manifest diff 產生：

- added files
- removed files
- modified files
- unchanged files

Changed files 映射到 changed partitions，再由 feature family dependency window 展開 downstream affected partitions。

### 5.2 Historical File Changes

歷史檔被修改時，只 invalidate：

- 該檔所屬 source partition。
- 依 supplier dependency window 展開的 downstream partitions。
- Label 的 previous/current/next month safety window。

不得因單一歷史檔變更而使整份 data source cache 失效，除非 schema / code / hash algorithm 層級失效。

### 5.3 Quantile Changes

- Quantile 上升：selected universe 是 subset；可重用既有 entity / feature rows，assembly 重新產生。
- Quantile 下降：找出新增 canonical / player / entity rows，對新增 rows 做 delta fill。
- Quantile 不得 invalidate source cleaning cache。

### 5.4 Registry Changes

- Remove feature：assembly miss only。
- Add derived feature from existing primitive：assembly miss only。
- Add new primitive：該 supplier family + window miss。
- Change formula / primitive semantics：對應 primitive semantic version miss。
- Change registry ordering only：通常 assembly miss only。

## 6) Correctness And Safety Requirements

- Cache hit 後至少驗證 row count、key uniqueness、schema fingerprint 與 manifest output stat。
- High-risk layer 可做 sample hash / sample recompute validation。
- Manifest 必須包含 `schema_version` 與 semantic / policy fingerprint。
- 寫入順序必須為 staging artifact → output stat validation → atomic publish artifact → atomic publish manifest。
- Cache corrupt、source identity uncertain、schema drift：fail-fast。
- Cache miss：recompute。

## 7) Observability Requirements

每輪 run 必須輸出 run-level `cache_report.json`，至少包含：

- layer
- artifact kind
- partition / month
- hit / miss / partial-hit
- miss reason
- affected source files
- affected months
- reused rows
- recomputed rows
- elapsed seconds
- output path
- manifest path

Run report 應保留 cache summary，方便比較不同 run 的重用率與耗時。

## 8) Success Criteria

- Folder overwrite 但內容不變時，source layer 仍 hit。
- 修改單一歷史 parquet file 時，只重算該 partition 與 downstream dependency window。
- ADT quantile 上升 / 下降不重算 source cleaning。
- Quantile 降低時只補算新增 entity rows 的 features。
- Feature registry add/remove 多數只重跑 assembly，不重跑 expensive supplier。
- Cache miss / hit reason 可從 `cache_report.json` 解釋。
- Source identity 不確定時 fail-fast，不產生 silent stale training set。

## 9) Assumptions

- Source parquet 檔名或資料夾仍可穩定映射到 partition month。
- `canonical_id` 是 rated patron universe 的商業主鍵。
- Current accepted training design 使用 latest/global ADT 篩 historical training rows。
- Full file SHA-256 的初始成本可接受，後續若太慢再加入 footer shortcut。
- MVP 可先使用 sidecar JSON manifest，不強制導入 SQLite catalog。

## 10) Open Questions

- 是否需要在第一版就實作 row-level delta compaction，或先用 delta parquet + periodic compact。
- Label cache 是否第一版即採 `canonical_id` shard + month，或先 month-level。
- Cache retention 的預設天數 / run 數上限。
- `DuckDbRuntimeConfig` 中哪些行為型參數需要納入 policy fingerprint。
