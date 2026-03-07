import re

with open("trainer/backtester.py", "r", encoding="utf-8") as f:
    text = f.read()

# Replace imports
text = re.sub(
    r'        add_legacy_features,\n        ALL_FEATURE_COLS,',
    r'        compute_track_llm_features,\n        load_feature_spec,\n        get_all_candidate_feature_ids,',
    text
)

text = re.sub(
    r'    # --- Legacy features ---\n    labeled = add_legacy_features\(labeled, sessions\)\n    for col in ALL_FEATURE_COLS:\n        if col not in labeled\.columns:\n            labeled\[col\] = 0\n    labeled\[ALL_FEATURE_COLS\] = labeled\[ALL_FEATURE_COLS\]\.fillna\(0\)',
    '''    # --- Track LLM & default fills ---
    _spec_path = MODEL_DIR / "feature_spec.yaml"
    if _spec_path.exists():
        feature_spec = load_feature_spec(_spec_path)
    else:
        feature_spec = load_feature_spec(Path(__file__).parent / "feature_spec" / "features_candidates.template.yaml")

    try:
        _bets_llm_result = compute_track_llm_features(
            labeled,
            feature_spec=feature_spec,
            cutoff_time=window_end,
        )
        _llm_cand_ids = [
            c.get("feature_id")
            for c in (feature_spec.get("track_llm") or {}).get("candidates", [])
        ]
        _bets_llm_feature_cols = [
            fid for fid in _llm_cand_ids
            if fid and fid in _bets_llm_result.columns
        ]
        if _bets_llm_feature_cols and "bet_id" in _bets_llm_result.columns:
            labeled = labeled.merge(
                _bets_llm_result[["bet_id"] + _bets_llm_feature_cols].drop_duplicates("bet_id"),
                on="bet_id",
                how="left",
            )
    except Exception as exc:
        logger.error(f"Track LLM failed in backtester: {exc}")

    _all_candidate_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True)
    for col in _all_candidate_cols:
        if col not in labeled.columns:
            labeled[col] = 0
    labeled[_all_candidate_cols] = labeled[_all_candidate_cols].fillna(0)''',
    text
)

with open("trainer/backtester.py", "w", encoding="utf-8") as f:
    f.write(text)

print("Backtester patch applied")
