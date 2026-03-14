# One-time patch scripts

**僅供參考、勿直接執行。**（Historical / one-off scripts; for reference only. Do not run unless re-applying the same migration on a fresh branch.)

These scripts were used during migration/refactors to apply regex-based edits to source files (e.g. `trainer/trainer.py`, `trainer/backtester.py`, `trainer/features.py`). They are **one-off**; after the target code was updated, the scripts are kept here for reference only.

**Do not run them** unless you are re-applying the same migration on a fresh branch—paths and regexes are tied to a specific code shape and may break or mis-patch if the target files have changed.

If you do run any script, **run from the project root** so relative paths like `trainer/trainer.py` resolve correctly:

```bash
python doc/one_time_scripts/patch_backtester.py
```

| Script | Target | Purpose |
|--------|--------|--------|
| `patch_trainer.py` | `trainer/trainer.py` | Incomplete: remove TRACK_B/LEGACY/ALL_FEATURE_COLS, adjust feature_list (no write-back). |
| `patch_trainer2.py` | `trainer/trainer.py` | Remove legacy feature logic; use feature_spec + get_all_candidate_feature_ids. |
| `patch_backtester.py` | `trainer/backtester.py` | Switch from legacy features to Track LLM + feature_spec. |
| `patch_reason_codes.py` | `trainer/trainer.py` | Generate reason_code_map from feature_spec YAML. |
| `patch_features.py` | `trainer/features.py` | Load PROFILE_FEATURE_COLS from features_candidates.yaml. |
| `fix_trainer.py` | `trainer/trainer.py` | save_artifact_bundle: add feature_spec load; fallbacks for get_candidate_feature_ids. |
