"""Session 表在進入 canonical mapping 之前的管線邊界。

Canonical mapping **只**讀這裡回傳的 Parquet 路徑（視為「供 mapping 用的已清洗
session」）。目前尚無列級清洗：回傳 raw L0 路徑本身，等同 cleaned = raw。

未來在此實作 backfill 對帳、去重等時，請改為寫出獨立 cleaned Parquet 並回傳該路徑；
並在適當時機 **遞增** ``SESSION_MAPPING_CLEAN_LOGIC_VERSION``，以便在來源位元組不變
時仍能失效 mapping 快取。
"""

from __future__ import annotations

from pathlib import Path

# Bump when session→mapping 的列級規則變更（含 passthrough 實作替換），即使 raw 檔未變。
SESSION_MAPPING_CLEAN_LOGIC_VERSION = "v0-passthrough"


def prepare_session_parquet_for_canonical_mapping(raw_session_parquet: Path) -> Path:
    """從 L0 ``t_session`` 產出（或解析出）供 DuckDB canonical 使用的 Parquet 路徑。

    Args:
        raw_session_parquet: L0 ``gmwds_t_session.parquet``（或同等匯出）。

    Returns:
        目前為 ``raw_session_parquet`` 的 resolve 路徑；未來可改為 cleaned 產物路徑。

    Raises:
        FileNotFoundError: 若路徑不存在或不是檔案。
    """
    p = raw_session_parquet.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"raw t_session parquet not found: {p}")
    return p
