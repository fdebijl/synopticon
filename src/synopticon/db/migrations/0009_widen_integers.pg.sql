-- Migration 9 (PostgreSQL only): widen every INTEGER column to BIGINT.
--
-- SQLite's INTEGER is 64-bit; PostgreSQL's is 32-bit. Migrations 1-8 handed
-- their `INTEGER` straight through, so a PostgreSQL library created before this
-- migration carries int4 where the schema means int8 -- and `photos.indexed_time`
-- (epoch *milliseconds*, ~1.8e12) overflows it, which is what made `db-migrate`
-- fail with "integer out of range" on the first batch of photos.
--
-- The DDL translator now emits BIGINT for a bare INTEGER, so a fresh database is
-- already correct and every ALTER below is a no-op it accepts silently. This file
-- has no SQLite half: `.pg.sql` migrations are skipped there (db/store.py), whose
-- INTEGER was 64-bit all along.
--
-- One ALTER per table, so each table is rewritten once. Indexes and foreign keys
-- are rebuilt by PostgreSQL as part of the type change.

ALTER TABLE photos
    ALTER COLUMN id TYPE BIGINT,
    ALTER COLUMN folder_id TYPE BIGINT,
    ALTER COLUMN filesize TYPE BIGINT,
    ALTER COLUMN time TYPE BIGINT,
    ALTER COLUMN indexed_time TYPE BIGINT,
    ALTER COLUMN unit_id TYPE BIGINT,
    ALTER COLUMN width TYPE BIGINT,
    ALTER COLUMN height TYPE BIGINT,
    ALTER COLUMN orientation TYPE BIGINT,
    ALTER COLUMN synced_at TYPE BIGINT,
    ALTER COLUMN deleted TYPE BIGINT,
    ALTER COLUMN hashed_at TYPE BIGINT,
    ALTER COLUMN similar_top_pick TYPE BIGINT;

ALTER TABLE persons
    ALTER COLUMN id TYPE BIGINT,
    ALTER COLUMN item_count TYPE BIGINT,
    ALTER COLUMN show TYPE BIGINT,
    ALTER COLUMN cover TYPE BIGINT,
    ALTER COLUMN synced_at TYPE BIGINT,
    ALTER COLUMN deleted TYPE BIGINT;

ALTER TABLE person_photos
    ALTER COLUMN person_id TYPE BIGINT,
    ALTER COLUMN photo_id TYPE BIGINT,
    ALTER COLUMN synced_at TYPE BIGINT;

ALTER TABLE syno_faces
    ALTER COLUMN syno_face_id TYPE BIGINT,
    ALTER COLUMN photo_id TYPE BIGINT,
    ALTER COLUMN person_id TYPE BIGINT,
    ALTER COLUMN synced_at TYPE BIGINT;

ALTER TABLE faces
    ALTER COLUMN photo_id TYPE BIGINT,
    ALTER COLUMN restored TYPE BIGINT,
    ALTER COLUMN created_at TYPE BIGINT;

ALTER TABLE embeddings
    ALTER COLUMN face_id TYPE BIGINT,
    ALTER COLUMN dim TYPE BIGINT,
    ALTER COLUMN created_at TYPE BIGINT;

ALTER TABLE cluster_runs
    ALTER COLUMN created_at TYPE BIGINT;

ALTER TABLE clusters
    ALTER COLUMN run_id TYPE BIGINT,
    ALTER COLUMN cluster_id TYPE BIGINT,
    ALTER COLUMN size TYPE BIGINT,
    ALTER COLUMN mapped_person_id TYPE BIGINT,
    ALTER COLUMN labeled_count TYPE BIGINT;

ALTER TABLE cluster_members
    ALTER COLUMN run_id TYPE BIGINT,
    ALTER COLUMN cluster_id TYPE BIGINT,
    ALTER COLUMN face_id TYPE BIGINT;

ALTER TABLE review_queue
    ALTER COLUMN run_id TYPE BIGINT,
    ALTER COLUMN decided_at TYPE BIGINT,
    ALTER COLUMN created_at TYPE BIGINT;

ALTER TABLE audit_log
    ALTER COLUMN ts TYPE BIGINT,
    ALTER COLUMN success TYPE BIGINT,
    ALTER COLUMN review_item_id TYPE BIGINT;

ALTER TABLE extract_log
    ALTER COLUMN photo_id TYPE BIGINT,
    ALTER COLUMN face_count TYPE BIGINT,
    ALTER COLUMN processed_at TYPE BIGINT;

ALTER TABLE web_users
    ALTER COLUMN created_at TYPE BIGINT;

ALTER TABLE web_sessions
    ALTER COLUMN user_id TYPE BIGINT,
    ALTER COLUMN created_at TYPE BIGINT,
    ALTER COLUMN expires_at TYPE BIGINT,
    ALTER COLUMN last_seen_at TYPE BIGINT;

ALTER TABLE web_api_keys
    ALTER COLUMN created_at TYPE BIGINT,
    ALTER COLUMN last_used_at TYPE BIGINT,
    ALTER COLUMN revoked TYPE BIGINT;

ALTER TABLE schedules
    ALTER COLUMN confirm TYPE BIGINT,
    ALTER COLUMN enabled TYPE BIGINT,
    ALTER COLUMN created_at TYPE BIGINT,
    ALTER COLUMN updated_at TYPE BIGINT,
    ALTER COLUMN next_run_at TYPE BIGINT,
    ALTER COLUMN last_run_at TYPE BIGINT;

ALTER TABLE schedule_runs
    ALTER COLUMN schedule_id TYPE BIGINT,
    ALTER COLUMN fired_at TYPE BIGINT;
