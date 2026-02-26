import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

NUMEXPR_MAX_THREADS = 12

# ------------------ ClickHouse (READ ONLY for now) ------------------
CH_HOST    = os.getenv("CH_HOST", "gdpedw")
CH_TEAMDB_HOST = os.getenv("CH_TEAMDB_HOST", "GAD10DMTDBSP21")
CH_PORT    = int(os.getenv("CH_PORT", 8123))
CH_USER    = os.getenv("CH_USER", "")
CH_PASS    = os.getenv("CH_PASS", "")
CH_PASSWORD = CH_PASS
CH_SECURE  = os.getenv("CH_SECURE", "False").lower() in ("true", "1", "t")
SOURCE_DB  = os.getenv("SOURCE_DB", "GDP_GMWDS_Raw")

# ------------------ Source tables ---------------------------------
TBET     = "t_bet"
TSESSION = "t_session"
TGAME    = "t_game"

# ------------------ Time / locality -------------------------------
HK_TZ = "Asia/Hong_Kong"

# ------------------ Model/Backtest Time Window Defaults -----------
# All times are in HK time
TRAINER_DAYS = 7  # How many days back for training window (default)
BACKTEST_HOURS = 6  # How many hours back for backtest window (default)
BACKTEST_OFFSET_HOURS = 1  # How many hours before now to end backtest window

# ------------------ Runtime Retention -----------------------------
# Trim local CSV buffers to avoid unbounded growth
SCORER_ALERT_RETENTION_DAYS = 30      # Keep scorer alerts for last N days
SCORER_STATE_RETENTION_HOURS = 24     # Keep scorer SQLite state for this many hours
VALIDATOR_ALERT_RETENTION_DAYS = 30   # Keep alerts visible to validator for last N days
VALIDATION_RESULTS_RETENTION_DAYS = 180  # Keep validation_results history for last N days

# ------------------ Status Server -----------------------------
TABLE_STATUS_REFRESH_SECONDS = 45  # How often to refresh table occupancy snapshot
TABLE_STATUS_LOOKBACK_HOURS = 12   # Only consider sessions started within this many hours for status
TABLE_STATUS_RETENTION_HOURS = 24  # Keep status snapshots for this many hours in SQLite
TABLE_STATUS_HC_RETENTION_DAYS = 30  # Keep headcount history for this many days

# ------------------ Validator behavior -------------------------
# Whether to finalize a "PENDING" alert as a "MISS" once it reaches the
# horizon (default 45 minutes after the bet). This prevents alerts from
# remaining in PENDING indefinitely when no evidence of a walkaway arrives.
VALIDATOR_FINALIZE_ON_HORIZON = True
VALIDATOR_FINALIZE_MINUTES = 45  # Horizon minutes to finalize as MISS when enabled