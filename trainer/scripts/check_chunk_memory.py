"""
One-off script to report whether loading all chunk Parquets in run_pipeline could OOM.

Usage:
  python -m trainer.scripts.check_chunk_memory   # from repo root
  python trainer/scripts/check_chunk_memory.py   # or from trainer/

Reads:
  - trainer/.data/chunks/*.parquet (if present) for actual chunk sizes
  - data/gmwds_t_bet.parquet (if present) to estimate chunk size from raw export

Output: Prints total chunk size, estimated RAM, and OOM risk.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as script or as module
if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent.parent
else:
    repo = Path(__file__).resolve().parent.parent
trainer_dir = repo / "trainer"
data_dir = repo / "data"
chunk_dir = trainer_dir / ".data" / "chunks"

def main() -> None:
    print("Chunk concat memory check")
    print("=" * 50)

    # 1. Actual chunk Parquets (if pipeline was run before)
    chunk_files = list(chunk_dir.glob("chunk_*.parquet")) if chunk_dir.exists() else []
    if chunk_files:
        total_bytes = sum(f.stat().st_size for f in chunk_files)
        total_gb = total_bytes / (1024**3)
        try:
            from trainer import config as _cfg
            factor = getattr(_cfg, "CHUNK_CONCAT_RAM_FACTOR", 3)
        except Exception:
            factor = 3
        est_ram_gb = (total_bytes * factor) / (1024**3)
        print("Chunk Parquets (trainer/.data/chunks/):")
        print("  files: %d" % len(chunk_files))
        print("  total on disk: %.2f GB" % total_gb)
        print("  estimated RAM (x%.1f): %.1f GB" % (factor, est_ram_gb))
        warn_bytes = 2 * (1024**3)
        try:
            import trainer.config as _c
            warn_bytes = getattr(_c, "CHUNK_CONCAT_MEMORY_WARN_BYTES", warn_bytes)
        except Exception:
            pass
        if total_bytes >= warn_bytes:
            print("  --> WARNING: above 2 GB; OOM risk on machines with < 16 GB RAM.")
        else:
            print("  --> Likely OK for typical 16 GB RAM.")
    else:
        print("No chunk Parquets found at %s" % chunk_dir)
        print("(Run pipeline once to generate chunks, or see raw-data estimate below.)")

    # 2. Raw data estimate (data/ at repo root)
    bets_file = data_dir / "gmwds_t_bet.parquet"
    if not bets_file.exists():
        print("")
        print("Raw data: data/gmwds_t_bet.parquet not found.")
        return

    bets_size_gb = bets_file.stat().st_size / (1024**3)
    print("")
    print("Raw data (data/):")
    print("  gmwds_t_bet.parquet: %.2f GB" % bets_size_gb)
    try:
        import pandas as pd
        n = len(pd.read_parquet(bets_file, columns=["bet_id"]))
        print("  rows: %d" % n)
    except Exception as e:
        print("  (row count skipped: %s)" % e)
        n = None

    # Rough: if raw is 12 months, 1 month on disk ~ size/12; processed chunk has more cols -> similar or larger. RAM ~ 2-3x.
    months_assumed = 12
    one_month_disk_gb = bets_size_gb / months_assumed
    one_month_ram_gb = one_month_disk_gb * 2.5
    print("  If export spans ~12 months: 1 month chunk ~ %.2f GB on disk -> ~ %.2f GB RAM per chunk." % (one_month_disk_gb, one_month_ram_gb))
    print("  TRAINER_DAYS=7 usually yields 1 chunk -> low OOM risk.")
    print("  Long window (e.g. 6+ months) -> %d chunks -> ~ %.1f GB RAM estimated -> OOM risk on 16 GB." % (6, 6 * one_month_ram_gb))


if __name__ == "__main__":
    main()
    sys.exit(0)
