# trainer_hightier Self-contained Baseline（Working-layer）

對應 [Self-contained - IMPLEMENTATION_PLAN.md](./Self-contained%20-%20IMPLEMENTATION_PLAN.md)。本檔定義 **training/preprocess + runtime** 去耦後的回歸基線（非產品 SSOT）。

## Import gate（每一輪 PR）

- `trainer_hightier/tests/test_no_trainer_imports.py`：禁止 `trainer.*`（不含 `trainer_hightier`）與 `pipelines.*`。

## 基線比對產物（runtime）

| Artifact | Must match | Allowed drift |
|-----------|-------------|---------------|
| `bundle_info.json` | `model_version`、`manifest_version`、`allowlist_sha256`、`frozen_fingerprint_sha256` | `build_time_iso` |
| `deploy_bundle_paths.json` | key schema 與主要路徑鍵存在性 | 路徑字串分隔符差異 |
| `snapshots/active_manifest.json` | required layer keys 存在（slow + allowlist） | optional trial layer 可 absent |
| `models/training_metrics.json` | `adt_allowlist_sha256`（若存在）與打包結果一致 | 非關鍵 debug 欄位 |
| `mapping/*.parquet` schema | `player_id` / `canonical_id` schema 不變 | row order |

## 基線比對產物（training / preprocess）

| Artifact / check | Must match | Allowed drift |
|------------------|------------|----------------|
| `trainer_hightier/contracts/preprocess_l0_data_contract_registry.yaml` | 與上游 registry 語意一致（FIX-004 cap） | registry version 字串 |
| cleaned bet manifest | `applied_fix_rules` 含 `BET-INGEST-FIX-004:v1`（契約啟用時） | debug 欄位 |
| pytest `trainer_hightier/tests/` | 全綠 | — |

## No-repo Smoke（target 視角，packaging 下一階段）

1. 在乾淨環境（無 repo）：建立 venv。
2. `pip install -r requirements.txt`（允許 PyPI；必要時可走內部 mirror）。
3. 複製 `.env.example` 為 `.env` 並填入 CH 憑證。
4. `python main.py`（或契約指定單一入口）。
5. 驗證 `/health` 與啟動 log 中 `model_version`/`manifest_version`/`allowlist_sha256`。

## 完成定義（P0）

- Import gate 與 `trainer_hightier/tests/` 全綠。
- 關鍵產物與上表可比對項目一致或有紀錄之允許漂移。
- No-repo smoke 於 packaging 里程碑時至少重現一次並留存紀錄。
