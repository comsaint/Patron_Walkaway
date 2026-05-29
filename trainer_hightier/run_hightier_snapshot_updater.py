"""CLI entry: ``python -m trainer_hightier.run_hightier_snapshot_updater``."""

from trainer_hightier.serving.snapshot_updater import main

if __name__ == "__main__":
    raise SystemExit(main())
