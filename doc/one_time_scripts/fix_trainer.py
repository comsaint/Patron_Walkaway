import re

with open("trainer/trainer.py", "r", encoding="utf-8") as f:
    text = f.read()

# Load feature spec inside save_artifact_bundle
replacement = """    spec_hash: Optional[str] = None
    feature_spec: Optional[dict] = None
    if feature_spec_path is not None:
        _fsp = Path(feature_spec_path)
        if _fsp.exists():
            import shutil as _shutil
            _shutil.copy2(_fsp, MODEL_DIR / "feature_spec.yaml")
            spec_hash = hashlib.md5(_fsp.read_bytes()).hexdigest()[:12]
            feature_spec = load_feature_spec(_fsp)
"""
text = re.sub(r'    spec_hash: Optional\[str\] = None\n    if feature_spec_path is not None:\n        _fsp = Path\(feature_spec_path\)\n        if _fsp\.exists\(\):\n            import shutil as _shutil\n            _shutil\.copy2\(_fsp, MODEL_DIR / "feature_spec\.yaml"\)\n            spec_hash = hashlib\.md5\(_fsp\.read_bytes\(\)\)\.hexdigest\(\)\[:12\]\n', replacement, text)

# Also handle the fallback for get_candidate_feature_ids if feature_spec is None
replacement2 = """    _profile_set = set(get_candidate_feature_ids(feature_spec, "track_profile", screening_only=True)) if feature_spec else set(PROFILE_FEATURE_COLS)
    _llm_set = set(get_candidate_feature_ids(feature_spec, "track_llm", screening_only=True)) if feature_spec else set()
    _human_set = set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=True)) if feature_spec else set()"""

text = re.sub(
    r'    _profile_set = set\(get_candidate_feature_ids\(feature_spec, "track_profile", screening_only=True\)\)\n    _llm_set = set\(get_candidate_feature_ids\(feature_spec, "track_llm", screening_only=True\)\)\n    _human_set = set\(get_candidate_feature_ids\(feature_spec, "track_human", screening_only=True\)\)',
    replacement2,
    text
)

with open("trainer/trainer.py", "w", encoding="utf-8") as f:
    f.write(text)

print("Fixed save_artifact_bundle")
