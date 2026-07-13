from __future__ import annotations

import numpy as np

from synopticon.eval import holdout


def test_perfect_clustering_recovers(db_helpers, tmp_settings):
    h = db_helpers
    # Two persons, 5 faces each, well-separated embeddings, all labeled.
    persons = {1: [1.0, 0.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0, 0.0]}
    for pid, name in ((1, "Alice"), (2, "Bob")):
        h.insert_person("personal", pid, name)

    photo_id = 0
    syno_id = 0
    face_id = 0
    for pid, vec in persons.items():
        for _ in range(5):
            h.insert_photo("personal", photo_id)
            fid = h.insert_face("personal", photo_id, 100, 100, 200, 200)
            face_id = fid
            h.insert_embedding(fid, "arcface", vec)
            syno_id += 1
            h.insert_syno_face("personal", syno_id, photo_id, pid, (0.1, 0.1, 0.3, 0.3))
            photo_id += 1
    h.commit()

    metrics = holdout.run_holdout(h.conn, tmp_settings, mask_fraction=0.2, seed=42)

    assert metrics["recovery_recall"] >= 0.99
    assert metrics["bcubed_f1"] >= 0.99
    assert metrics["pairwise_f1"] >= 0.99
    assert metrics["merge_false_positives"] == 0


def test_grid_search_writes_csv(db_helpers, tmp_settings):
    h = db_helpers
    for pid in (1, 2):
        h.insert_person("personal", pid, f"P{pid}")
    vecs = {1: [1.0, 0.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0, 0.0]}
    photo_id = 0
    syno_id = 0
    for pid, vec in vecs.items():
        for _ in range(4):
            h.insert_photo("personal", photo_id)
            fid = h.insert_face("personal", photo_id, 100, 100, 200, 200)
            h.insert_embedding(fid, "arcface", vec)
            syno_id += 1
            h.insert_syno_face("personal", syno_id, photo_id, pid, (0.1, 0.1, 0.3, 0.3))
            photo_id += 1
    h.commit()

    grid = {"edge_threshold": [0.4, 0.6], "majority": [0.6]}
    rows = holdout.grid_search(h.conn, tmp_settings, grid, mask_fraction=0.2, seed=1)
    assert len(rows) == 2
    csv_path = tmp_settings.storage.data_dir / "eval_grid.csv"
    assert csv_path.is_file()
