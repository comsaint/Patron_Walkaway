# Local Phase-0 contracts (LDA-E0-07). Requires: python, pyyaml, jsonschema.
.PHONY: check-layered-contracts refresh-layered-contracts-artifacts check-lda-l0

check-layered-contracts:
	python scripts/validate_layered_contracts.py

refresh-layered-contracts-artifacts:
	python scripts/enumerate_deploy_features.py

# Phase-1 L0 ingest helpers + path/fingerprint unit tests (no large data required).
check-lda-l0: check-layered-contracts
	python -m pytest tests/unit/test_layered_l0_paths.py tests/unit/test_l1_paths.py tests/unit/test_l0_fingerprint.py tests/unit/test_l0_ingest_cli.py tests/unit/test_preprocess_bet_v1.py tests/unit/test_run_id_v1.py tests/unit/test_run_fact_v1.py tests/unit/test_run_bet_map_v1.py tests/unit/test_run_day_bridge_v1.py tests/unit/test_ingestion_delay_summary_v1.py tests/unit/test_manifest_lineage_v1.py -q --tb=short
