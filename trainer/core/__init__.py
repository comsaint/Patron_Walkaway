# trainer.core — keep package __init__ minimal.
#
# Do not eagerly import schema_io / duckdb_schema here: they pull pandas and
# slow down `import trainer.core.db_conn` in cold subprocesses (pytest timeouts).
# Use `from trainer.core import config` or `import trainer.core.db_conn` explicitly.
