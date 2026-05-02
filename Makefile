# Local Phase-0 contracts (LDA-E0-07). Requires: python, pyyaml, jsonschema.
.PHONY: check-layered-contracts refresh-layered-contracts-artifacts

check-layered-contracts:
	python scripts/validate_layered_contracts.py

refresh-layered-contracts-artifacts:
	python scripts/enumerate_deploy_features.py
