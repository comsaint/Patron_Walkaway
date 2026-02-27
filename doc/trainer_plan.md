This document is to describe how we intend to train our patron walkaway detection model.

# Background
Galaxy Entertainment Group (GEG), is one of the six major integrated resort operator in Macau SAR. This project focuses in our casino's Mass Gaming Floor in the Galaxy Macau property, more particularly on Baccarat which is a key contributor to our revenue.

Since 2024 July, we gradually deployed Smart Table technology on Baccarat tables which capture betting behavior in real-time. 

# Definitions
1. Walkaway event: a betting patron is said to *walkaway* if he/she do not place any more bets in upcoming X (default=30) minutes.

# Objectives
This project aims to build a detection system which predicts whether a betting patron will stop betting soon, to alert our hosts to approach and convince them to continue gaming.

More precisely, we want to detect for a patron, will a Walkaway event happens in upcoming Y (default=15) minutes.

# Technical Details
1. The data are stored in a Clickhouse database.
2. There are 4 tables available: bet, session, game, and shoe.
    * Bet table: 1 row per bet. The low-level betting behavior, captures when, where and what bet was made by whom, and the result (win/loss, wager, payout etc.). The records are available usually within 1 minute after payout.
    * Session table: this table is unique by player and table. A high-level aggregation of consecutive bets. This table is usually updated after a few tens of minutes after session end.
    * Game table: aggregation of bets on a table in a round of game, by >=1 players.
    * Shoe: table: related to exact cards draw etc..
    * For more info, refer to `schema\GDP_GMWDS_Raw_Schema_Dictionary.md`.
    * There is at least 1 more table on player-level, which we cannot access due to policy issue now. That table should contain player's PII data, their card tier and membership number change etc..
3. The most relevant tables should be bet and session; game table will be incorporated later; show table does not seem relevant.
4. There are 2 types of player-level IDs, `player_id` and `casino_player_id`. For details, refer to `doc/FINDINGS.md`.

# How we plan the model
1. Since the model is to be deployed in real-time, we have to be careful about data availability and leakage both in training and serving. Refer to  `schema\GDP_GMWDS_Raw_Schema_Dictionary.md` on each table's availability.
2. Since the bet table is updated in real-time, we will use it as the elementary table of patrons's recent betting behavior and to calculate the walkaway label.
3. The session table data arrive too late, possibly long after a walkaway event. We should not use it to calculate walkaway event, nor assuming we have the most updated info in production. However, this table is valuable in two senses: a bridge between the `player_id` and `casino_player_id` (and to distinguish rated and non-rated players); and a high-level view of each patron's past betting history. This table somehow fill the gap of the missing player-level table.
4. The objective function is not clear yet. We only know we target precision over recall, so we can start with metrics e.g. F0.5, or PR-ROC.
5. Given that we have rated and non-rated patrons, I intend to build two models for each group; rated patron has history (on session table), while non-rated players do not. Needless to say, it is essential to define logic to distinguish between them in real-time.
6. There are rich information in the session and bet table, and I want to leverage an automatic feature engineering solution first to see what features are useful. For modeling I also want to try AutoML solution first. The core requirements is that both solutions must be able to handle time-series training and backtesting.