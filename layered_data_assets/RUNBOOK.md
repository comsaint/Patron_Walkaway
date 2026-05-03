# Layered data assets（LDA）操作手冊 — Phase 1 Runbook

> **範圍**：L0 ingest、`t_bet` preprocess、L1 `run_fact`／`run_bet_map`／`run_day_bridge`、Gate 1 determinism、日區間編排器。  
> **非範圍**：`trip_*`、published、trainer Step 6/7 取代（見 `implementation plan/layered_data_assets_run_trip_execution_plan.md`）。

## 1. 前置條件

- 在**倉庫根目錄**執行下列指令（相對路徑以 `data/` 為準）。
- Python 3.12+（與 CI 對齊）、已安裝 `duckdb`（及編排／Gate 可選的 `tqdm`）。契約驗證需 `pyyaml`、`jsonschema`。
- 大型 Parquet 注意記憶體與磁碟；Gate 1 預設含低 `memory_limit` profile，大檔請用 `--profiles-json` 覆寫（見 §6）。

## 2. 目錄與 ID 慣例

| 路徑 | 說明 |
|------|------|
| `data/l0_layered/<snap>/snapshot_fingerprint.json` | L0 批次指紋；`snap_*` 由指紋推導（見 `doc/l0_ingest_governance_decisions.md`） |
| `data/l0_layered/<snap>/<table>/<partition_key>=<value>/part-*.parquet` | L0 Hive 分區 raw |
| `data/l1_layered/<source_snapshot_id>/t_bet/gaming_day=<YYYY-MM-DD>/cleaned.parquet` | preprocess 輸出 |
| `data/l1_layered/<id>/run_fact/run_end_gaming_day=.../` | `run_fact` 分區 |
| `data/l1_layered/<id>/run_bet_map/run_end_gaming_day=.../` | `run_bet_map` 分區 |
| `data/l1_layered/<id>/run_day_bridge/bet_gaming_day=.../` | `run_day_bridge` 分區（鍵為 **bet 日**） |
| `data/l1_layered/materialization_state.duckdb`（預設） | **LDA-E1-09** 日編排 materialization state（DuckDB）；可用 `--state-store` 覆寫 |

**`source_snapshot_id`（L1）**：通常與該批 L0 的 `snap_*` 對齊；若每日各做一次 L0 ingest（`partition_value` 不同），fingerprint 不同，**每日的 `snap_*` 可能不同** — 編排器在 raw 模式會自 ingest 輸出解析。

## 3. 契約與單元測試（無大檔）

```bash
make check-lda-l0
```

內含：`validate_layered_contracts` + L0/L1 路徑、fingerprint、ingest CLI、preprocess、run_*、Gate1、OOM runner 等單元測試。

## 4. 手動單日管線（逐步）

以下假設工作目錄為 repo 根，且已有一份 **L0／匯出形狀的 `t_bet` Parquet**（**至少**含 `player_id`、`bet_id`、`gaming_day`；`preprocess_bet_v1` 會在執行 SQL 前驗證）。  
**勿**使用 `data/baseline_for_baseline_models.parquet` 之類的訓練／特徵切片當 `t_bet` 來源（欄位不同，會失敗）。

### 4.1 L0 ingest（可選）

```bash
python scripts/l0_ingest.py --data-root data --table t_bet \
  --partition-key gaming_day --partition-value YYYY-MM-DD \
  --source path/to/source.parquet
```

建議先 `--dry-run` 確認終端印出的 `snapshot_id` 與路徑，再實寫。

### 4.2 Preprocess（L1 `t_bet` clean）

```bash
python scripts/preprocess_bet_v1.py --data-root data \
  --source-snapshot-id <snap_與L1一致> \
  --gaming-day YYYY-MM-DD \
  --input data/l0_layered/<snap>/t_bet/gaming_day=YYYY-MM-DD/part-000.parquet
```

可選：`--l0-fingerprint-json data/l0_layered/<snap>/snapshot_fingerprint.json`

可選：`--ingestion-fix-registry-yaml schema/preprocess_bet_ingestion_fix_registry.yaml`（及 `--ingestion-fix-registry-version-expected`，與 YAML 頂層 `registry_version` 對齊時 fail-fast）。啟用後 manifest 會帶 `ingestion_fix_*`／`applied_fix_rules`；dedup tie-break 使用 synthetic observed（見 SSOT LDA-014）。

**寫檔**：`preprocess_bet_v1` 與三個 `materialize_run_*_v1` 對 **`*.parquet` + `manifest.json`** 採 **`*.tmp` → `os.replace`**，避免長寫入途中留下半套產物。

### 4.3 物化 `run_fact` / `run_bet_map` / `run_day_bridge`

```bash
python scripts/materialize_run_fact_v1.py --data-root data \
  --source-snapshot-id <snap> --run-end-gaming-day YYYY-MM-DD \
  --l1-preprocess-gaming-day YYYY-MM-DD \
  --input data/l1_layered/<snap>/t_bet/gaming_day=YYYY-MM-DD/cleaned.parquet

python scripts/materialize_run_bet_map_v1.py --data-root data \
  --source-snapshot-id <snap> --run-end-gaming-day YYYY-MM-DD \
  --l1-preprocess-gaming-day YYYY-MM-DD \
  --input data/l1_layered/<snap>/t_bet/gaming_day=YYYY-MM-DD/cleaned.parquet

python scripts/materialize_run_day_bridge_v1.py --data-root data \
  --source-snapshot-id <snap> --bet-gaming-day YYYY-MM-DD \
  --l1-preprocess-gaming-day YYYY-MM-DD \
  --input data/l1_layered/<snap>/t_bet/gaming_day=YYYY-MM-DD/cleaned.parquet
```

`run_end_gaming_day`：該 run **最後一筆 bet** 的 `gaming_day`。測單日常與 preprocess 分區同日。跨日 run 需餵足夠多日資料（見 `layered_data_assets/run_fact_v1.py` 模組說明）。

### 4.4 Manifest 後補（可選）

```bash
python scripts/manifest_lineage_preview_v1.py --help
```

## 5. 日區間編排器（推薦一條龍）

腳本：`scripts/lda_l1_gate1_day_range_v1.py`

- **資料根目錄固定**為 `<repo>/data`（**不接受** `--data-root`，請在倉庫根執行）。
- **輸入模式**（可全省略一項，見下）：
  - **（預設）** 若未帶下列三旗標，且存在 **`data/gmwds_t_bet.parquet`**（與 README／trainer 本機匯出同路徑）：等同 **`--bet-parquet`** 指該檔，並使用 **`--source-snapshot-id snap_gmwds_t_bet_local`**（可自帶 `--source-snapshot-id` 覆寫）。**不**做每日 L0 ingest，避免對同一巨大檔重複落地。
  - **`--raw-t-bet-parquet <path>`**：每日先 `l0_ingest`（`t_bet`），再 preprocess → 三物化 → 三個 Gate1。可選 **`--raw-t-session-parquet`** 僅多寫 L0 `t_session`（本編排器無後續 L1 消費）。
  - **`--bet-parquet <path>`**：跳過 L0；**必須** **`--source-snapshot-id`**（若不用預設檔則必填）；preprocess 依 SQL 過濾各日 `gaming_day`。
  - **`--l0-existing`**：在 `data/l0_layered` 下依當日分區**自動尋找**既有 `snap_*`（多個時取字典序第一並 stderr 警告）。
  - 若未帶旗標且 **沒有** `data/gmwds_t_bet.parquet`，程式會 stderr 說明並 exit 2，請改用上述旗標之一。

常用旗標：`--date-from` / `--date-to`（含首尾）、`--dry-run`、`--verbose`（轉給 Gate1）、`--no-progress`（關閉編排器日進度條）、`--profiles-json`（轉給 Gate1）、`--gate1-output-parent`、**`--state-store`** / **`--resume`** / **`--force`** / **`--stop-after-date`**（見下 §5.1）。可選 **`--ingestion-fix-registry-yaml`**／**`--ingestion-fix-registry-version-expected`**：轉給每日 `preprocess_bet_v1`；`materialization_state` 之 preprocess **`input_hash`** 會納入 registry 檔 stat 與預期版本字串。

```bash
python scripts/lda_l1_gate1_day_range_v1.py --help
```

**範例（先看計畫不寫檔）** — `--bet-parquet` 須為含 `player_id` / `bet_id` / `gaming_day` 的 `t_bet` 匯出（例如 `data/gmwds_t_bet.parquet`），**不可**用 baseline 特徵檔。

```bash
python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 --date-to 2026-01-01 \
  --bet-parquet data/gmwds_t_bet.parquet \
  --source-snapshot-id snap_abcdefgh --dry-run
```

### 5.1 Resumable state（LDA-E1-09）

- **State DB**：DuckDB 表 `materialization_state`（DDL：`schema/materialization_state.schema.sql`）；程式模組：`layered_data_assets/materialization_state_store_v1.py`。
- **`--state-store PATH`**：啟用寫入／讀取狀態；每步成功後記 `succeeded` + `input_hash`（由 L0 輸入檔 stat、fingerprint 原文、（可選）ingestion registry 檔 stat／預期版本、`cleaned` stat 等組成穩定 JSON 再 sha256）。
- **`--resume`**（且未同時 **`--force`**）：若該步已是 `succeeded` 且 **`input_hash` 與本次計算相同**，則 **skip** 該 subprocess；若預期產物檔或 Gate1 輸出目錄已遺失，會 **WARN 並強制重跑** 該步。
- **`--force`**：忽略 `succeeded`，一律重跑日期區間內各步（仍寫回 state）。若同時傳 `--resume`，以 **`--force` 為準**。
- **僅 `--resume` 或 `--force` 而未給 `--state-store`**：使用預設檔 **`data/l1_layered/materialization_state.duckdb`**（倉庫根下之 `data/`）。
- **`--stop-after-date YYYY-MM-DD`**：必須落在 `--date-from`…`--date-to` 內；該曆日整條管線（含三個 Gate1）**成功結束後**即結束程式，後續日期不跑（方便中斷演練）。
- **G7（LDA-E1-10）**：`python -m pytest tests/integration/test_lda_e1_10_resume_g7_v1.py -q`（一條龍 vs `stop-after-date`+`--resume` 產物指紋一致；已含於 **`make check-lda-l0`**）。
- **原子寫**：`preprocess_bet_v1` 與三個 `materialize_run_*_v1` 對 **`*.parquet` + `manifest.json`** 採 **`*.tmp` → `os.replace`**；編排層在子程序 **exit 0** 後才標 `succeeded`（中斷時不應出現「state 成功但檔案未寫完」之組合，除非子程序誤報成功——與 SSOT 契約一致時應由子程序修正）。
- **E1-11 + Gate1（execution §5.3 列 12）**：`python -m pytest tests/integration/test_lda_e1_11_gate1_with_registry_v1.py -q`（已含於 **`make check-lda-l0`**）：帶 registry 之 preprocess 與無 registry 基線在固定 fixture 上 **L1 四產物 row fingerprint 一致**，且 manifest 含 **BET-INGEST-FIX-004**。

**範例（記錄 state，翌日續跑 skip）**：

```bash
python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 --date-to 2026-01-02 \
  --bet-parquet data/gmwds_t_bet.parquet --source-snapshot-id snap_local \
  --state-store data/l1_layered/materialization_state.duckdb

# 第二輪：僅 --resume（未指定 --state-store 時使用同上預設路徑）→ 已 succeeded 且 input_hash 相同則 SKIP
python scripts/lda_l1_gate1_day_range_v1.py --date-from 2026-01-01 --date-to 2026-01-02 \
  --bet-parquet data/gmwds_t_bet.parquet --source-snapshot-id snap_local --resume
```

## 6. Gate 1 單獨執行（LDA-E1-08）

腳本：`scripts/gate1_l1_determinism_v1.py`

- 對同一組 cleaned 輸入，在多部 DuckDB `memory_limit`／`threads` 下重跑物化並比對列數與 row fingerprint。
- **建議**：以 `--data-root data --l1-source-snapshot-id … --l1-preprocess-gaming-day …` 指到單日 `cleaned.parquet`，避免把整包巨型檔當 `--input`。
- 大檔或筆電：自訂 **`--profiles-json`**，例如 `'[[null,2],[null,1]]'`，避免預設的低記憶體 profile OOM。
- **`--verbose`**：stderr 階段日誌 + tqdm（可用 **`--no-progress`** 只留日誌）。

```bash
python scripts/gate1_l1_determinism_v1.py --artifact run_fact \
  --data-root data --l1-source-snapshot-id <snap> --l1-preprocess-gaming-day YYYY-MM-DD \
  --output-dir /tmp/gate1_run_fact --run-end-gaming-day YYYY-MM-DD \
  --profiles-json '[[null,2],[null,1]]' --verbose
```

成功時 stdout 為 JSON 報告，exit code `0`；不一致為 `1`。

## 7. DuckDB OOM 與重試（§7.1）

`preprocess_bet_v1` 與三個 `materialize_run_*_v1.py` 支援 `--duckdb-run-log`、`--duckdb-oom-failure-context`、`--duckdb-oom-max-attempts`、`--duckdb-initial-memory-limit-mb`（見 `layered_data_assets/oom_runner_v1.py`）。

## 8. 相關文件

| 文件 | 用途 |
|------|------|
| `doc/l0_ingest_governance_decisions.md` | L0 指紋、`source_hashes`、CI 與大檔策略 |
| `doc/preprocessing_layered_data_assets_v1.md` | preprocess 規則與 manifest |
| `implementation plan/layered_data_assets_run_trip_execution_plan.md` | Phase 任務與 DoD |
| `schema/time_semantics_registry.yaml` | 事件時間語意（與 ingest delay 預設欄位對齊） |

## 9. 疑難排解（精簡）

| 現象 | 檢查 |
|------|------|
| preprocess 找不到 L0 part | `gaming_day` 分區路徑、`snap_*` 是否與 `--source-snapshot-id` 一致 |
| Gate1／編排器報缺 `cleaned.parquet` | 該日是否已 preprocess；`--l1-preprocess-gaming-day` 是否與目錄 `gaming_day=` 一致 |
| raw 模式磁碟暴長 | 同一巨大 raw 檔按日重複 ingest 會每日一個 `snap_*` 全檔複本；改用小檔、按日 raw，或改用 `--bet-parquet`／`--l0-existing` |
| Gate1 極慢或 OOM | 縮小 `--profiles-json`；物化輸出列數極大時 fingerprint 的 `string_agg` 亦重 |
| Gate1 exit **3221226505**（Windows） | 多為 DuckDB 在有限 `memory_limit` 下對大輸入物化時**整個程序被系統結束**（非可捕捉 OOM）。預設 Gate1 只改 **`threads`**、不設 `memory_limit`；若仍崩潰請 `--verbose`，並避免對巨型 cleaned 傳極小 `--profiles-json` memory 步階 |

---

*本 runbook 與程式行為以倉庫內腳本為準；若與上層 SSOT／implementation plan 衝突，以上層文件為準並應回寫本檔。*
