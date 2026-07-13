-- Migration 2: per-photo extraction bookkeeping.
--
-- extract_log records that a photo was processed by the detection/embedding
-- pipeline at a given pipeline_version against a given cache_key. The runner
-- skips a photo when its extract_log row already matches the current
-- (cache_key, pipeline_version), making extraction crash-resumable and cheap
-- to re-run. Zero-face photos still get a row (face_count = 0) so they are
-- never re-scanned needlessly.

CREATE TABLE extract_log (
    space            TEXT    NOT NULL,
    photo_id         INTEGER NOT NULL,
    cache_key        TEXT,
    pipeline_version TEXT    NOT NULL,
    face_count       INTEGER NOT NULL,
    processed_at     INTEGER NOT NULL,
    PRIMARY KEY (space, photo_id)
);
