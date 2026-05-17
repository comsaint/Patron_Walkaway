"""CLI entry: ``python -m trainer_hightier.run_hightier_api``."""

from trainer_hightier.serving.api_server import main

if __name__ == "__main__":
    raise SystemExit(main())
