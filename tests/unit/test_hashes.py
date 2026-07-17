"""sync.hashes: hash computation properties + the sync --hash DB pass."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from PIL import Image

from synopticon.db import store
from synopticon.sync import hashes


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _textured_image(path, size=200, seed=1, jpeg_quality=None):
    """Deterministic low-frequency texture (seeded 8x8 field upscaled).

    A plain gradient is a degenerate pHash input (one dominant DCT coefficient,
    median threshold flips on noise); this mimics real photo content instead.
    """
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    img = Image.fromarray(small, "L").resize((size, size), Image.BICUBIC).convert("RGB")
    if jpeg_quality is not None:
        img.save(path, "JPEG", quality=jpeg_quality)
    else:
        img.save(path, "PNG")
    return path


# -- hash primitives ---------------------------------------------------------


def test_sha256_matches_hashlib(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"synopticon" * 100)
    assert hashes.compute_sha256(path) == hashlib.sha256(b"synopticon" * 100).hexdigest()


def test_phash_is_64_bit_hex_and_deterministic(tmp_path):
    path = _textured_image(tmp_path / "a.png")
    h1 = hashes.compute_phash(path)
    h2 = hashes.compute_phash(path)
    assert h1 == h2
    assert len(h1) == 16
    int(h1, 16)  # valid hex


def test_phash_near_for_recompressed_far_for_different(tmp_path):
    original = hashes.compute_phash(_textured_image(tmp_path / "orig.png"))
    # Same picture through lossy JPEG: different bytes, near-identical phash.
    recompressed = hashes.compute_phash(_textured_image(tmp_path / "re.jpg", jpeg_quality=40))
    # Different seed: visually different picture.
    different = hashes.compute_phash(_textured_image(tmp_path / "other.png", seed=2))
    assert _hamming(original, recompressed) <= 4
    assert _hamming(original, different) > 16


# -- migration + sync pass ---------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")
    yield conn
    conn.close()


def _insert_photo(conn, pid, cache_key="ck1", type_="photo"):
    conn.execute(
        "INSERT INTO photos (id, space, filename, cache_key, type, synced_at) "
        "VALUES (?, 'personal', ?, ?, ?, ?)",
        (pid, f"photo-{pid}.png", cache_key, type_, store.now()),
    )
    conn.commit()


def test_migration_adds_hash_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    assert {"sha256", "phash", "hash_cache_key", "hashed_at"} <= cols


def test_sync_hashes_populates_skips_and_rehashes_on_cache_key_change(conn, tmp_path):
    img_path = _textured_image(tmp_path / "orig.png")
    _insert_photo(conn, 1)
    _insert_photo(conn, 2, type_="video")  # never downloaded/hashed

    fetched: list[int] = []

    def fetch(row):
        fetched.append(row["id"])
        return img_path

    stats = hashes.sync_hashes(conn, fetch, "personal")
    assert stats == {"hashed": 1, "failed": 0}
    assert fetched == [1]

    row = conn.execute("SELECT * FROM photos WHERE id = 1").fetchone()
    assert row["sha256"] == hashes.compute_sha256(img_path)
    assert row["phash"] == hashes.compute_phash(img_path)
    assert row["hash_cache_key"] == "ck1"
    assert row["hashed_at"] is not None
    video = conn.execute("SELECT sha256 FROM photos WHERE id = 2").fetchone()
    assert video["sha256"] is None

    # Second pass: nothing to do, no re-download.
    fetched.clear()
    assert hashes.sync_hashes(conn, fetch, "personal") == {"hashed": 0, "failed": 0}
    assert fetched == []

    # Photo edited on the NAS (new cache_key) -> re-hashed.
    conn.execute("UPDATE photos SET cache_key = 'ck2' WHERE id = 1")
    conn.commit()
    assert hashes.sync_hashes(conn, fetch, "personal") == {"hashed": 1, "failed": 0}
    assert fetched == [1]
    row = conn.execute("SELECT hash_cache_key FROM photos WHERE id = 1").fetchone()
    assert row["hash_cache_key"] == "ck2"


def test_sync_hashes_continues_past_failures(conn, tmp_path):
    img_path = _textured_image(tmp_path / "orig.png")
    _insert_photo(conn, 1)
    _insert_photo(conn, 2)

    def fetch(row):
        if row["id"] == 1:
            raise OSError("download failed")
        return img_path

    stats = hashes.sync_hashes(conn, fetch, "personal")
    assert stats == {"hashed": 1, "failed": 1}
    ok = conn.execute("SELECT sha256 FROM photos WHERE id = 2").fetchone()
    assert ok["sha256"] is not None
    # The failed photo stays eligible for the next pass.
    assert [r["id"] for r in hashes._fetch_work(conn, "personal")] == [1]


def test_sync_hashes_stores_sha256_when_image_undecodable(conn, tmp_path):
    path = tmp_path / "photo-1.png"
    path.write_bytes(b"not an image at all")
    _insert_photo(conn, 1)

    stats = hashes.sync_hashes(conn, lambda row: path, "personal")
    assert stats == {"hashed": 1, "failed": 0}
    row = conn.execute("SELECT sha256, phash FROM photos WHERE id = 1").fetchone()
    assert row["sha256"] == hashes.compute_sha256(path)
    assert row["phash"] is None
