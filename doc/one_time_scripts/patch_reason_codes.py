import re

with open("trainer/trainer.py", "r", encoding="utf-8") as f:
    text = f.read()

# Replace _STATIC_REASON_CODES with generating from YAML category
new_block = """    # reason_code_map.json: feature name -> short reason code for SHAP output.
    # Generated from feature_spec (DEC-024 / TRN-XX).
    reason_code_map: dict[str, str] = {}
    if feature_spec is not None:
        for track in ["track_llm", "track_human", "track_profile"]:
            for c in feature_spec.get(track, {}).get("candidates", []):
                fid = c.get("feature_id")
                rcode = c.get("reason_code_category")
                if fid and rcode:
                    reason_code_map[fid] = rcode

    # Fallback for any missing code
    for feat in feature_cols:
        if feat not in reason_code_map:
            if feat in PROFILE_FEATURE_COLS:
                reason_code_map[feat] = f"PROFILE_{feat[:28].upper()}"
            else:
                reason_code_map[feat] = f"FEAT_{feat[:30].upper()}"

    (MODEL_DIR / "reason_code_map.json").write_text(
        json.dumps(reason_code_map, indent=2, ensure_ascii=False), encoding="utf-8"
    )"""

text = re.sub(
    r'    # reason_code_map\.json.*?encoding="utf-8"\n    \)',
    new_block,
    text,
    flags=re.DOTALL
)

with open("trainer/trainer.py", "w", encoding="utf-8") as f:
    f.write(text)
print("Updated reason_code_map generation in trainer.py")