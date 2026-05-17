# trainer_hightier — Packaging（Working / execution plan）

本文件為 **Working / execution plan**，承接 `doc/Packaging - IMPLEMENTATION_PLAN.md`，記錄任務、DoD 與驗收；**不**重寫產品 SSOT。

## 執行護欄

- 預設 `high_adt_only=true`；除錯外不可用全量模式冒充正式。
- `training_metrics.json` 若含 `adt_allowlist_sha256`，建包與 runtime 預設須 **hash 一致**（建包階段強制比對）。
- 打包不包含 raw CH mirror／訓練中間大檔。
- `state.db` 與 API 欄位契約維持相容。

## 落地狀態（實作對照）

| 迭代 | 內容 | 主要產物 |
|------|------|----------|
| A | 目錄契約、CLI、`bundle_info.json` | `trainer_hightier/build_deploy_package.py` |
| B | 模型／manifest／parquet／mapping 收斂；manifest 路徑改為相對 `snapshots/` | 同上 + `feature_state_store.ActiveSnapshotManifest.from_dict(manifest_dir=...)` |
| C | strict preflight、allowlist SHA 與 `training_metrics` 對齊 | `build_deploy_package` 內 `_verify_allowlist_training_hash_or_raise` |
| D | deploy 統一入口、啟動可觀測 log | `trainer_hightier/deploy/main.py`、`set_hightier_serving_deploy_override` |
| E | 測試、RUNBOOK、本文件 | `tests/test_build_deploy_package.py`、`RUNBOOK.md` |

## CLI 速查

```bash
python -m trainer_hightier.build_deploy_package \
  --model-source <bundle_dir> \
  --snapshot-manifest-source <active_manifest.json 或目錄> \
  --mapping-source <canonical_mapping.parquet> \
  --output-dir <空目錄> \
  [--archive] [--strict/--no-strict]

python -m trainer_hightier.deploy.main --bundle-dir <交付根> [--mode all|api|scorer|validator]
```

## Release gate（簡表）

- 交付目錄符合約定、無多餘大檔。
- 目標機可啟動並 `GET /health`。
- `high_adt_only=true` 下 alerting 受 allowlist 約束。
- allowlist／manifest／`bundle_info` 可追溯一致。
