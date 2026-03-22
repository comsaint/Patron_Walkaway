# trainer.etl — 項目 2.2：etl 子包（etl_player_profile、profile_schedule）。
#
# 不在此預先 import 子模組：`import trainer.etl.etl_player_profile_argparse` 會先載入本包，
# 若此處 eager import etl_player_profile 會拉整條 pandas／ClickHouse 鏈，造成
# `python -m trainer.etl_player_profile --help` 等冷子程序逾時。
