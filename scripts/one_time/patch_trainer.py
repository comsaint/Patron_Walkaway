import re

with open("trainer/trainer.py", "r", encoding="utf-8") as f:
    text = f.read()

# 1. Remove definitions of TRACK_B_FEATURE_COLS, LEGACY_FEATURE_COLS, ALL_FEATURE_COLS
text = re.sub(r'# Track-B feature column list.*?ALL_FEATURE_COLS: List\[str\] = TRACK_B_FEATURE_COLS \+ LEGACY_FEATURE_COLS \+ PROFILE_FEATURE_COLS\n', '', text, flags=re.DOTALL)

# 2. Replace the feature list creation in `_train_models_and_evaluate`
text = re.sub(
    r'_legacy_set = set\(LEGACY_FEATURE_COLS\)\s*feature_list = \[\s*\{\s*"name": c,\s*"track": \(\s*"profile" if c in PROFILE_FEATURE_COLS\s*else "B" if c in TRACK_B_FEATURE_COLS\s*else "legacy" if c in _legacy_set\s*else "LLM"\s*# Track LLM \(DuckDB \+ feature spec\)\s*\),\s*\}\s*for c in feature_cols\s*\]',
    '''feature_list = [
        {
            "name": c,
            "track": (
                "track_profile" if c in PROFILE_FEATURE_COLS
                else "track_human" if c in _track_human_cols
                else "track_llm"
            ),
        }
        for c in feature_cols
    ]''',
    text
)

# 3. Replace _train_models_and_evaluate active_feature_cols = ALL_FEATURE_COLS
# Wait, we need to extract _track_human_cols first. Let's do it right inside `_train_models_and_evaluate`.
# Let's write a targeted script to do this safely.
