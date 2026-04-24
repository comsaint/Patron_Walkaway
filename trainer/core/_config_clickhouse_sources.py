from __future__ import annotations

import os

"""Internal ClickHouse / source-table config shard."""

CH_HOST = os.getenv("CH_HOST", "gdpedw")
CH_TEAMDB_HOST = os.getenv("CH_TEAMDB_HOST", "GAD10DMTDBSP21")
CH_PORT = int(os.getenv("CH_PORT", 8123))
CH_USER = os.getenv("CH_USER", "")
CH_PASS = os.getenv("CH_PASS", "")
CH_PASSWORD = CH_PASS
CH_SECURE = os.getenv("CH_SECURE", "False").lower() in ("true", "1", "t")
SOURCE_DB = os.getenv("SOURCE_DB", "GDP_GMWDS_Raw")

TBET = "t_bet"
TSESSION = "t_session"
TGAME = "t_game"
TPROFILE = "player_profile"

CASINO_PLAYER_ID_CLEAN_SQL = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)

