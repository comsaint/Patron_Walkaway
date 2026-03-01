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

# ============================================================
# Phase 1 — Walkaway Model Constants (SSOT v9)
# ============================================================

# --- Business parameters ---
WALKAWAY_GAP_MIN = 30            # X: gap duration that qualifies as a walkaway (min)
ALERT_HORIZON_MIN = 15           # Y: prediction window before gap start (min)
LABEL_LOOKAHEAD_MIN = WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN  # X + Y; C1 extended pull

# --- Data availability delays (SSOT §4.2) ---
BET_AVAIL_DELAY_MIN = 1          # t_bet available ~1 min after payout_complete_dtm
SESSION_AVAIL_DELAY_MIN = 15     # t_session: conservative +15 min after session_end

# --- Run boundary ---
RUN_BREAK_MIN = WALKAWAY_GAP_MIN  # Gap >= this value starts a new betting run

# --- Gaming day / visit dedup (G4) ---
# Primary: use the gaming_day column from t_bet/t_session.
# This constant is a fallback only (sensitivity analysis or missing gaming_day).
GAMING_DAY_START_HOUR = 6

# --- G1 threshold gate ---
G1_PRECISION_MIN = 0.70          # Minimum per-model precision (provisional; needs biz sign-off)
G1_ALERT_VOLUME_MIN_PER_HOUR = 5 # Minimum combined alert volume/hour (provisional)
G1_FBETA = 0.5                   # F-beta weight (beta < 1 → precision-weighted)
OPTUNA_N_TRIALS = 300            # Optuna TPE trials for 2-D threshold search (I6)

# --- Track B constants ---
TABLE_HC_WINDOW_MIN = 30         # Lookback window for table headcount feature (D1)
PLACEHOLDER_PLAYER_ID = -1       # Invalid player_id sentinel in t_bet (E4/F1)
LOSS_STREAK_PUSH_RESETS = False  # Whether PUSH resets the loss-streak counter (F4)
HIST_AVG_BET_CAP = 500_000       # Winsorization cap for avg_bet (F2; validate with EDA)

# --- SQL fragment shared across all modules (FND-03) ---
CASINO_PLAYER_ID_CLEAN_SQL = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)