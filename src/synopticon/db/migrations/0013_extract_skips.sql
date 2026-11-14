-- Migration 13: photos detection could not read, so later runs leave them out.
--
-- A photo that fails is never written to extract_log, so without this table it
-- is selected, downloaded and failed again on every run. Only failures that a
-- retry cannot change land here (an undecodable file, an image too large to
-- decode); a NAS outage or an unexpected error is retried as before. A row
-- stops applying when the photo's cache_key or the pipeline_version changes,
-- and `extract --retry-skipped` ignores the table altogether.

CREATE TABLE IF NOT EXISTS extract_skips (
    space            TEXT    NOT NULL,
    photo_id         INTEGER NOT NULL,
    cache_key        TEXT,
    pipeline_version TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    skipped_at       INTEGER NOT NULL,
    PRIMARY KEY (space, photo_id)
);
