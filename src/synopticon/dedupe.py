"""Duplicate detection from persisted content hashes — NAS-free by construction.

Like `cluster/`, this module reads only from the local DB (the `photos.sha256` /
`photos.phash` columns `sync --hash` populates) and never imports `syno/` or
`pipeline/`, so duplicate detection works entirely offline. Actually deleting the
duplicates from the NAS is a separate concern (`dedupe_writeback.py`); the CLI
wires the two together.

Two levels:

- **exact** — byte-identical photos, grouped by `sha256`.
- **visual** — visually-near photos, grouped by `phash` hamming distance
  (never string equality; see `sync.hashes.compute_phash`).

Within every group the highest-resolution photo is kept and the rest are the
deletion candidates (`drop`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from synopticon.config import Space
from synopticon.sync.hashes import phash_hamming


@dataclass(frozen=True)
class DuplicateGroup:
    """One set of duplicates in a single space: keep one, drop the rest."""

    space: Space
    kind: str  # "exact" | "visual"
    keep: sqlite3.Row
    drop: list[sqlite3.Row]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(int(r["filesize"] or 0) for r in self.drop)


def _sort_key(row: sqlite3.Row) -> tuple[int, int, int]:
    """Keep-rule ordering: highest resolution, then largest file, then lowest id.

    Sorting ascending by this key puts the photo to keep first. For exact
    (byte-identical) duplicates resolution and filesize are equal, so the tie
    falls through to the lowest `id` — deterministic regardless of level.
    """
    width = int(row["width"] or 0)
    height = int(row["height"] or 0)
    filesize = int(row["filesize"] or 0)
    return (-(width * height), -filesize, int(row["id"]))


def _pick_keep(rows: Iterable[sqlite3.Row]) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    ordered = sorted(rows, key=_sort_key)
    return ordered[0], ordered[1:]


def find_exact(conn: sqlite3.Connection, space: Space) -> list[DuplicateGroup]:
    """Groups of byte-identical photos (same `sha256`), keep-rule applied."""
    rows = conn.execute(
        "SELECT * FROM photos WHERE space = ? AND deleted = 0 AND sha256 IS NOT NULL",
        (space,),
    ).fetchall()

    by_hash: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_hash.setdefault(row["sha256"], []).append(row)

    groups: list[DuplicateGroup] = []
    for members in by_hash.values():
        if len(members) < 2:
            continue
        keep, drop = _pick_keep(members)
        groups.append(DuplicateGroup(space=space, kind="exact", keep=keep, drop=drop))
    return groups


def find_visual(conn: sqlite3.Connection, space: Space, threshold: int) -> list[DuplicateGroup]:
    """Groups of visually-near photos: phashes within `threshold` bits.

    Builds connected components over the pairwise "hamming <= threshold" graph
    via union-find. The pairwise scan is O(n^2); runtime is explicitly not a
    constraint here, but a BK-tree over the phashes would cut it if needed.
    """
    rows = conn.execute(
        "SELECT * FROM photos WHERE space = ? AND deleted = 0 AND phash IS NOT NULL",
        (space,),
    ).fetchall()

    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if phash_hamming(rows[i]["phash"], rows[j]["phash"]) <= threshold:
                union(i, j)

    components: dict[int, list[sqlite3.Row]] = {}
    for idx, row in enumerate(rows):
        components.setdefault(find(idx), []).append(row)

    groups: list[DuplicateGroup] = []
    for members in components.values():
        if len(members) < 2:
            continue
        keep, drop = _pick_keep(members)
        groups.append(DuplicateGroup(space=space, kind="visual", keep=keep, drop=drop))
    return groups


def collect_drop_ids(groups: Iterable[DuplicateGroup]) -> list[int]:
    """De-duplicated, ordered list of item ids slated for deletion.

    When both levels run a photo can appear in an exact and a visual group
    (exact duplicates are a subset of visual ones once phashes exist); the same
    id must only be deleted once.
    """
    seen: dict[int, None] = {}
    for group in groups:
        for row in group.drop:
            seen.setdefault(int(row["id"]), None)
    return list(seen)
