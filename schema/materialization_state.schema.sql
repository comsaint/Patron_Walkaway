-- LDA-E1-09: per-day, per-artifact materialization state (DuckDB).
-- Aligns with implementation plan §2.3 (resumable orchestration).
-- Unique key: (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)

CREATE TABLE IF NOT EXISTS materialization_state (
  artifact_kind VARCHAR NOT NULL,
  gaming_day VARCHAR NOT NULL,
  source_snapshot_id VARCHAR NOT NULL,
  definition_version VARCHAR NOT NULL,
  transform_version VARCHAR NOT NULL,
  input_hash VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  attempt INTEGER NOT NULL,
  output_uri VARCHAR,
  row_count BIGINT,
  row_hash VARCHAR,
  error_summary VARCHAR,
  updated_at TIMESTAMP NOT NULL,
  PRIMARY KEY (
    artifact_kind,
    gaming_day,
    source_snapshot_id,
    definition_version,
    transform_version
  )
);
