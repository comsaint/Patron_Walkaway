# Marks trainer/ as a regular Python package so that
# `from trainer.time_fold import ...` works from the project root.
#
# When installed as walkaway_ml (deploy wheel), register trainer -> walkaway_ml
# so that existing "import trainer.xxx" / "from trainer.xxx" work on the target.
import sys
if __name__ == "walkaway_ml":
    sys.modules["trainer"] = sys.modules["walkaway_ml"]
