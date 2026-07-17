-- Synopticon canonical schema. Applied via db/store.py migrations
-- (PRAGMA user_version tracks the schema version; this file is migration 1).

CREATE TABLE photos (
    id            INTEGER NOT NULL,
    space         TEXT    NOT NULL,              -- 'personal' | 'shared'
    filename      TEXT,
    folder_id     INTEGER,
    filesize      INTEGER,
    time          INTEGER,                       -- epoch seconds (taken time)
    indexed_time  INTEGER,                       -- epoch milliseconds
    type          TEXT,                          -- 'photo' | 'video' | 'live'
    cache_key     TEXT,
    unit_id       INTEGER,
    width         INTEGER,
    height        INTEGER,
    orientation   INTEGER,
    synced_at     INTEGER NOT NULL,
    deleted       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (space, id)
);

CREATE TABLE persons (
    id          INTEGER NOT NULL,
    space       TEXT    NOT NULL,
    name        TEXT,
    item_count  INTEGER,
    show        INTEGER,
    cover       INTEGER,
    synced_at   INTEGER NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (space, id)
);

-- Photo-level ground truth from Browse.Item.list additional=["person"].
CREATE TABLE person_photos (
    space      TEXT    NOT NULL,
    person_id  INTEGER NOT NULL,
    photo_id   INTEGER NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'synology',
    synced_at  INTEGER NOT NULL,
    PRIMARY KEY (space, person_id, photo_id)
);

-- Face-level ground truth from Browse.Item.list_face (normalized 0-1 bboxes).
CREATE TABLE syno_faces (
    space         TEXT    NOT NULL,
    syno_face_id  INTEGER NOT NULL,
    photo_id      INTEGER NOT NULL,
    person_id     INTEGER,
    name          TEXT,
    x1            REAL NOT NULL,                 -- top_left.x, normalized
    y1            REAL NOT NULL,
    x2            REAL NOT NULL,                 -- bottom_right.x, normalized
    y2            REAL NOT NULL,
    synced_at     INTEGER NOT NULL,
    PRIMARY KEY (space, syno_face_id)
);
CREATE INDEX idx_syno_faces_photo ON syno_faces (space, photo_id);

-- Our detections. Pixel coordinates in the EXIF-orientation-corrected image.
CREATE TABLE faces (
    face_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    space                TEXT    NOT NULL,
    photo_id             INTEGER NOT NULL,
    detector             TEXT    NOT NULL,       -- 'scrfd' | 'yolo' | 'merged'
    x                    REAL NOT NULL,
    y                    REAL NOT NULL,
    w                    REAL NOT NULL,
    h                    REAL NOT NULL,
    det_score            REAL,
    det_score_secondary  REAL,                   -- other detector's score when merged
    landmarks            BLOB,                   -- 5x2 float32, pixel coords
    crop_path            TEXT,                   -- aligned 112x112 crop
    ctx_crop_path        TEXT,                   -- ~256px context crop for review/restore
    quality              REAL,                   -- MagFace pre-normalization norm
    restored             INTEGER NOT NULL DEFAULT 0,
    restore_disagreement REAL,
    pipeline_version     TEXT NOT NULL,
    created_at           INTEGER NOT NULL,
    UNIQUE (space, photo_id, detector, x, y, w, h)
);
CREATE INDEX idx_faces_photo ON faces (space, photo_id);
CREATE INDEX idx_faces_pipeline ON faces (pipeline_version);

CREATE TABLE embeddings (
    face_id        INTEGER NOT NULL REFERENCES faces (face_id) ON DELETE CASCADE,
    model          TEXT    NOT NULL,             -- 'arcface_r100' | 'adaface_ir101' | 'magface_r100'
    variant        TEXT    NOT NULL DEFAULT 'orig',  -- 'orig' | 'restored'
    dim            INTEGER NOT NULL,
    vec            BLOB    NOT NULL,             -- float32, L2-normalized
    model_version  TEXT,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (face_id, model, variant)
);

CREATE TABLE cluster_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    params_json  TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE clusters (
    run_id           INTEGER NOT NULL REFERENCES cluster_runs (run_id) ON DELETE CASCADE,
    cluster_id       INTEGER NOT NULL,
    size             INTEGER NOT NULL,
    mapped_person_id INTEGER,
    map_space        TEXT,
    vote_fraction    REAL,
    labeled_count    INTEGER,
    PRIMARY KEY (run_id, cluster_id)
);

CREATE TABLE cluster_members (
    run_id     INTEGER NOT NULL,
    cluster_id INTEGER NOT NULL,
    face_id    INTEGER NOT NULL REFERENCES faces (face_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, face_id)
);
CREATE INDEX idx_cluster_members_cluster ON cluster_members (run_id, cluster_id);

CREATE TABLE review_queue (
    item_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES cluster_runs (run_id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,  -- 'assign'|'merge'|'new_person'|'restore_disagreement'|'low_confidence'|'reassign'
    payload_json TEXT NOT NULL,
    confidence   REAL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|applied|failed
    decided_at   INTEGER,
    decided_by   TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX idx_review_status ON review_queue (status, kind);

CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    action         TEXT NOT NULL,
    api            TEXT,
    params_json    TEXT,
    response_json  TEXT,
    success        INTEGER,
    review_item_id INTEGER REFERENCES review_queue (item_id)
);

-- Cursors, cached API version table, persisted auth state (sid/did), etc.
CREATE TABLE sync_state (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
