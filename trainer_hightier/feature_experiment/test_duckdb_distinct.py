import duckdb
import pandas as pd

df = pd.DataFrame({
    'session_id': [1, 1, 1, 2, 2],
    'game_id': ['A', 'A', 'B', 'C', 'C'],
    'bet_id': [10, 11, 12, 13, 14],
    'wager': [10.0, 20.0, 5.0, 50.0, 10.0],
    'theo_win': [0.1, 0.2, 0.05, 0.5, 0.1],
    'pcd': pd.to_datetime(['2026-01-01 10:00:00', '2026-01-01 10:01:00', '2026-01-01 10:02:00', '2026-01-01 11:00:00', '2026-01-01 11:05:00'])
})

con = duckdb.connect(':memory:')
con.register('df', df)

sql = """
WITH prep AS (
    SELECT 
        session_id, pcd, bet_id, wager, theo_win, game_id,
        CASE WHEN ROW_NUMBER() OVER (PARTITION BY session_id, game_id ORDER BY pcd, bet_id) = 1 THEN 1 ELSE 0 END AS is_first_bet_of_game
    FROM df
),
prep2 AS (
    SELECT *,
        SUM(is_first_bet_of_game) OVER (PARTITION BY session_id ORDER BY pcd, bet_id) AS games_cnt
    FROM prep
)
SELECT * FROM prep2
"""
try:
    print(con.execute(sql).df())
except Exception as e:
    print("Error:", e)
