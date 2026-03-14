with open("trainer/features.py", "r", encoding="utf-8") as f:
    text = f.read()

# Replace imports
text = text.replace("import pandas as pd", "import pandas as pd\nimport pathlib\nimport yaml as _yaml")

# Find where PROFILE_FEATURE_COLS starts
start_idx = text.find("#: Phase 2 additions")
end_idx = text.find("\ndef get_profile_feature_cols")

if start_idx != -1 and end_idx != -1:
    replacement = """#: Phase 2 additions (wager_mean_180d, wager_p50_180d from t_bet) are not
#: included here.  See doc/player_profile_spec.md §14.

_yaml_path = pathlib.Path(__file__).parent / "feature_spec" / "features_candidates.yaml"
with open(_yaml_path, "r", encoding="utf-8") as _f:
    _TEMPLATE_SPEC = _yaml.safe_load(_f)

PROFILE_FEATURE_COLS: List[str] = [
    c["feature_id"]
    for c in _TEMPLATE_SPEC.get("track_profile", {}).get("candidates", [])
    if c.get("feature_id")
]

# Minimum lookback (days) required to compute each profile feature.
_PROFILE_FEATURE_MIN_DAYS: dict = {
    c["feature_id"]: c.get("min_lookback_days", 365)
    for c in _TEMPLATE_SPEC.get("track_profile", {}).get("candidates", [])
    if c.get("feature_id")
}

# R122: enforce at import time that _PROFILE_FEATURE_MIN_DAYS stays in sync with
# PROFILE_FEATURE_COLS.  Any missing or extra key means the dynamic-feature-layer
# logic (get_profile_feature_cols) will silently mis-classify features.
assert set(_PROFILE_FEATURE_MIN_DAYS) == set(PROFILE_FEATURE_COLS), (
    "_PROFILE_FEATURE_MIN_DAYS keys do not match PROFILE_FEATURE_COLS — "
    f"missing: {set(PROFILE_FEATURE_COLS) - set(_PROFILE_FEATURE_MIN_DAYS)}, "
    f"extra: {set(_PROFILE_FEATURE_MIN_DAYS) - set(PROFILE_FEATURE_COLS)}"
)"""
    new_text = text[:start_idx] + replacement + text[end_idx:]
    with open("trainer/features.py", "w", encoding="utf-8") as f:
        f.write(new_text)
    print("Patched features.py successfully.")
else:
    print("Failed to find replacement indices.")
