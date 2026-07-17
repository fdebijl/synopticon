-- Migration 3: per-photo content hashes (populated by `sync --hash`).
--
-- sha256 is the cryptographic hash of the original file bytes; phash is a
-- 64-bit DCT perceptual hash (hex, hamming-comparable between rows).
-- hash_cache_key records the Synology cache_key the hashes were computed
-- against, so a photo edited on the NAS (new cache_key) is re-hashed on the
-- next pass -- the same skip mechanism extract_log uses.

ALTER TABLE photos ADD COLUMN sha256 TEXT;
ALTER TABLE photos ADD COLUMN phash TEXT;
ALTER TABLE photos ADD COLUMN hash_cache_key TEXT;
ALTER TABLE photos ADD COLUMN hashed_at INTEGER;
