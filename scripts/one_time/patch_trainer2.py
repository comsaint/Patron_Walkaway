import re

with open("trainer/trainer.py", "r", encoding="utf-8") as f:
    text = f.read()

# Remove add_legacy_features function definition
text = re.sub(
    r'# ---------------------------------------------------------------------------\n# Legacy features.*?# ---------------------------------------------------------------------------\n\ndef add_legacy_features\(.*?\n    return df\n\n\n',
    '',
    text,
    flags=re.DOTALL
)

# Remove the call to add_legacy_features
text = re.sub(
    r'    # --- Legacy (Track Human) features ---\n    labeled = add_legacy_features\(labeled, sessions\)\n\n',
    '',
    text,
    flags=re.DOTALL
)

# Replace ALL_FEATURE_COLS with feature_spec usage in _process_chunk
text = re.sub(
    r'    _non_profile_cols = \[c for c in ALL_FEATURE_COLS if c not in PROFILE_FEATURE_COLS\]',
    '''    _all_candidate_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True) if feature_spec else PROFILE_FEATURE_COLS
    _non_profile_cols = [c for c in _all_candidate_cols if c not in PROFILE_FEATURE_COLS]''',
    text
)

# In _train_models_and_evaluate
text = re.sub(
    r'    active_feature_cols = ALL_FEATURE_COLS\n',
    '    active_feature_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True)\n',
    text
)

# In _train_models_and_evaluate, feature_list generation
text = re.sub(
    r'    _legacy_set = set\(LEGACY_FEATURE_COLS\)\n    feature_list = \[\n        \{\n            "name": c,\n            "track": \(\n                "profile" if c in PROFILE_FEATURE_COLS\n                else "B" if c in TRACK_B_FEATURE_COLS\n                else "legacy" if c in _legacy_set\n                else "LLM"   # Track LLM \(DuckDB \+ feature spec\)\n            \),\n        \}\n        for c in feature_cols\n    \]',
    '''    _profile_set = set(get_candidate_feature_ids(feature_spec, "track_profile", screening_only=True))
    _llm_set = set(get_candidate_feature_ids(feature_spec, "track_llm", screening_only=True))
    _human_set = set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=True))

    feature_list = [
        {
            "name": c,
            "track": (
                "track_profile" if c in _profile_set
                else "track_human" if c in _human_set
                else "track_llm"
            ),
        }
        for c in feature_cols
    ]''',
    text
)

# Import get_all_candidate_feature_ids and get_candidate_feature_ids
text = re.sub(
    r'        PROFILE_FEATURE_COLS,\n        get_profile_feature_cols,\n    \)',
    '        PROFILE_FEATURE_COLS,\n        get_profile_feature_cols,\n        get_all_candidate_feature_ids,\n        get_candidate_feature_ids,\n    )',
    text
)

# Remove the warnings related to TRACK_B_FEATURE_COLS
text = re.sub(
    r'        if not _screened_set\.intersection\(TRACK_HUMAN_FEATURE_COLS\):.*?len\(_missing_track_human\),\n                \)\n                screened_cols\.extend\(_missing_track_human\)\n',
    '',
    text,
    flags=re.DOTALL
)

with open("trainer/trainer.py", "w", encoding="utf-8") as f:
    f.write(text)

print("Trainer patch applied")
