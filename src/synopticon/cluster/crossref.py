"""Cross-reference clustered faces against Synology ground truth.

Produces the review queue (assign / low_confidence / new_person / merge).
The cluster+map logic is factored into ``_crossref_core`` so evaluation
(holdout) can reuse it with an injected label map and no DB writes.

Module-boundary rule: no imports from syno/ or pipeline/.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field

import numpy as np

from ..db import Connection

from ..config import Settings
from ..db import store
from ..progress import get_emitter
from . import chinese_whispers, graph, hdbscan_alt

PersonKey = tuple[str, int]  # (space, person_id)


# --------------------------------------------------------------------------- #
# Ground-truth labelling
# --------------------------------------------------------------------------- #
def _iou(box: tuple[float, float, float, float], other: tuple) -> float:
    ax1, ay1, ax2, ay2 = box
    bx1, by1, bx2, by2 = other
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _load_face_meta(conn: Connection) -> dict[int, dict]:
    """face_id -> {space, photo_id, bbox_norm:[x1,y1,x2,y2]} in normalized coords."""
    dims: dict[tuple[str, int], tuple[int, int]] = {}
    for row in conn.execute("SELECT space, id, width, height FROM photos"):
        if row["width"] and row["height"]:
            dims[(row["space"], int(row["id"]))] = (int(row["width"]), int(row["height"]))

    meta: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT face_id, space, photo_id, x, y, w, h FROM faces"
    ):
        space, pid = row["space"], int(row["photo_id"])
        wh = dims.get((space, pid))
        if wh is None:
            bbox = None
        else:
            W, H = wh
            bbox = [
                float(row["x"]) / W,
                float(row["y"]) / H,
                (float(row["x"]) + float(row["w"])) / W,
                (float(row["y"]) + float(row["h"])) / H,
            ]
        meta[int(row["face_id"])] = {
            "space": space,
            "photo_id": pid,
            "bbox_norm": bbox,
        }
    return meta


def label_faces(conn: Connection, settings: Settings) -> dict[int, PersonKey]:
    """Map our detected faces to ground-truth persons (labels only)."""
    return label_faces_with_ids(conn, settings)[0]


def label_faces_with_ids(
    conn: Connection, settings: Settings
) -> tuple[dict[int, PersonKey], dict[int, int]]:
    """Map our detected faces to ground-truth persons.

    Face-level ground truth first (IoU >= 0.3 greedy match against
    ``syno_faces``), then a photo-level fallback for photos with no syno_faces
    rows AND exactly one detected face AND exactly one tagged person.

    Returns ``(labels, syno_face_ids)`` where ``syno_face_ids`` maps our
    face_id to the matched Synology face id. Only the IoU branch contributes
    to it — photo-level fallback labels have no syno_faces row, so those
    faces are ineligible for reassignment (nothing to separate).
    """
    meta = _load_face_meta(conn)

    # Group our faces by (space, photo_id).
    faces_by_photo: dict[tuple[str, int], list[int]] = {}
    for fid, m in meta.items():
        faces_by_photo.setdefault((m["space"], m["photo_id"]), []).append(fid)

    # Syno faces (with a person) by (space, photo_id).
    syno_by_photo: dict[tuple[str, int], list[dict]] = {}
    photos_with_syno: set[tuple[str, int]] = set()
    for row in conn.execute(
        "SELECT space, syno_face_id, photo_id, person_id, x1, y1, x2, y2 FROM syno_faces"
    ):
        key = (row["space"], int(row["photo_id"]))
        photos_with_syno.add(key)
        if row["person_id"] is None:
            continue
        syno_by_photo.setdefault(key, []).append(
            {
                "syno_face_id": int(row["syno_face_id"]),
                "person_id": int(row["person_id"]),
                "box": (
                    float(row["x1"]),
                    float(row["y1"]),
                    float(row["x2"]),
                    float(row["y2"]),
                ),
            }
        )

    labels: dict[int, PersonKey] = {}
    syno_face_ids: dict[int, int] = {}

    # Face-level IoU matching (greedy, best pair first).
    for key, syno_list in syno_by_photo.items():
        space = key[0]
        our = [fid for fid in faces_by_photo.get(key, []) if meta[fid]["bbox_norm"]]
        pairs = []
        for fid in our:
            for si, sf in enumerate(syno_list):
                iou = _iou(tuple(meta[fid]["bbox_norm"]), sf["box"])
                if iou >= 0.3:
                    pairs.append((iou, fid, si))
        pairs.sort(reverse=True)
        used_faces: set[int] = set()
        used_syno: set[int] = set()
        for iou, fid, si in pairs:
            if fid in used_faces or si in used_syno:
                continue
            used_faces.add(fid)
            used_syno.add(si)
            labels[fid] = (space, syno_list[si]["person_id"])
            syno_face_ids[fid] = syno_list[si]["syno_face_id"]

    # Photo-level fallback.
    person_photos_by_photo: dict[tuple[str, int], list[int]] = {}
    for row in conn.execute(
        "SELECT space, person_id, photo_id FROM person_photos"
    ):
        person_photos_by_photo.setdefault(
            (row["space"], int(row["photo_id"])), []
        ).append(int(row["person_id"]))

    for key, our in faces_by_photo.items():
        if key in photos_with_syno:
            continue
        persons = person_photos_by_photo.get(key, [])
        if len(our) == 1 and len(persons) == 1:
            labels[our[0]] = (key[0], persons[0])

    return labels, syno_face_ids


# --------------------------------------------------------------------------- #
# Cross-reference core (no DB writes)
# --------------------------------------------------------------------------- #
@dataclass
class ClusterInfo:
    cluster_id: int
    members: list[int]  # positions into face_ids / X
    votes: dict[PersonKey, int] = field(default_factory=dict)
    labeled_total: int = 0
    mapped_person: PersonKey | None = None
    vote_fraction: float | None = None
    centroid: np.ndarray | None = None


@dataclass
class ClusterResult:
    face_ids: np.ndarray
    labels: np.ndarray  # contiguous cluster id per position
    clusters: dict[int, ClusterInfo]
    assigns: list[dict]
    new_persons: list[dict]
    merges: list[dict]
    predicted_person: dict[int, PersonKey]  # face_id -> mapped person of its cluster
    reassigns: list[dict] = field(default_factory=list)


def _relabel_contiguous(labels: np.ndarray) -> np.ndarray:
    """Map arbitrary labels to 0..C-1; each ``-1`` (noise) becomes a singleton."""
    out = np.empty(labels.shape[0], dtype=np.int64)
    next_id = 0
    mapping: dict[int, int] = {}
    for i, lab in enumerate(labels.tolist()):
        if lab == -1:
            out[i] = next_id
            next_id += 1
            continue
        if lab not in mapping:
            mapping[lab] = next_id
            next_id += 1
        out[i] = mapping[lab]
    return out


def _crossref_core(
    face_ids: np.ndarray,
    X: np.ndarray,
    labels: np.ndarray,
    label_map: dict[int, PersonKey],
    settings: Settings,
    face_meta: dict[int, dict],
    person_photos: set[tuple[str, int, int]],
    *,
    syno_face_ids: dict[int, int] | None = None,
) -> ClusterResult:
    cfg = settings.crossref
    labels = _relabel_contiguous(labels)
    pos_of = {int(fid): i for i, fid in enumerate(face_ids.tolist())}

    clusters: dict[int, ClusterInfo] = {}
    for pos, cid in enumerate(labels.tolist()):
        clusters.setdefault(cid, ClusterInfo(cluster_id=cid, members=[])).members.append(pos)

    predicted_person: dict[int, PersonKey] = {}

    for cid, info in clusters.items():
        member_fids = [int(face_ids[p]) for p in info.members]
        for fid in member_fids:
            pk = label_map.get(fid)
            if pk is not None:
                info.votes[pk] = info.votes.get(pk, 0) + 1
        info.labeled_total = sum(info.votes.values())
        if X.shape[0]:
            info.centroid = _l2(X[info.members].mean(axis=0))
        if info.labeled_total:
            top_pk, top_count = max(info.votes.items(), key=lambda kv: (kv[1], kv[0]))
            info.vote_fraction = top_count / info.labeled_total
            if info.labeled_total >= cfg.min_labeled and info.vote_fraction >= cfg.majority:
                info.mapped_person = top_pk
                for fid in member_fids:
                    predicted_person[fid] = top_pk

    assigns: list[dict] = []
    new_persons: list[dict] = []
    merges: list[dict] = []
    reassigns: list[dict] = []
    merge_pairs_seen: set[tuple] = set()

    # Corpus-wide index of labeled positions per person, for reassign evidence
    # (mean similarity of a disputed face to the rest of its Synology person).
    positions_by_person: dict[PersonKey, list[int]] = {}
    if syno_face_ids is not None:
        for fid, pk in label_map.items():
            pos = pos_of.get(fid)
            if pos is not None:
                positions_by_person.setdefault(pk, []).append(pos)

    emitter = get_emitter()
    mapped = sum(1 for i in clusters.values() if i.mapped_person is not None)
    emitter.log(
        "info",
        f"cluster.crossref: {len(clusters)} cluster(s), {mapped} mapped to a known person, "
        f"{len(label_map)} ground-truth labelled face(s)",
        phase="cluster.crossref",
    )
    for done, (cid, info) in enumerate(clusters.items()):
        emitter.progress("cluster.crossref", done, len(clusters))
        labeled_positions = [p for p in info.members if int(face_ids[p]) in label_map]

        # --- assigns / low_confidence ---
        if info.mapped_person is not None and labeled_positions:
            p_space, p_id = info.mapped_person
            lab_mat = X[labeled_positions]
            for pos in info.members:
                fid = int(face_ids[pos])
                if fid in label_map:
                    continue
                meta = face_meta.get(fid)
                if meta is None:
                    continue
                photo_id = meta["photo_id"]
                if (p_space, p_id, photo_id) in person_photos:
                    continue  # photo already tagged with this person
                conf = float((X[pos] @ lab_mat.T).mean()) if lab_mat.shape[0] else 0.0
                payload = {
                    "face_id": fid,
                    "photo_id": photo_id,
                    "space": meta["space"],
                    "person_id": p_id,
                    "person_name": None,
                    "bbox_normalized": meta["bbox_norm"],
                    "confidence": conf,
                }
                kind = "assign" if conf >= cfg.assign_sim else "low_confidence"
                assigns.append({"kind": kind, "confidence": conf, "payload": payload,
                                "person_key": info.mapped_person})

        # --- reassign: Synology says Y, this cluster says X ---
        # Leave-one-out: dropping the disputed face's non-X vote can only
        # raise X's vote fraction, so the LOO majority condition is implied
        # by the full mapping — only the support floor can fail. Do not
        # "fix" this by recomputing the fraction; it is intentional.
        if (
            syno_face_ids is not None
            and info.mapped_person is not None
            and info.labeled_total - 1 >= cfg.min_labeled
        ):
            x_pk = info.mapped_person
            x_positions = [
                p for p in info.members if label_map.get(int(face_ids[p])) == x_pk
            ]
            x_mat = X[x_positions]
            for pos in info.members:
                fid = int(face_ids[pos])
                y_pk = label_map.get(fid)
                if y_pk is None or y_pk == x_pk:
                    continue
                sfid = syno_face_ids.get(fid)
                if sfid is None:
                    continue  # photo-fallback label: no syno face to separate
                meta = face_meta.get(fid)
                if meta is None or meta["bbox_norm"] is None:
                    continue
                if y_pk[0] != x_pk[0] or meta["space"] != x_pk[0]:
                    continue  # cannot move a face across spaces
                conf = float((X[pos] @ x_mat.T).mean()) if x_mat.shape[0] else 0.0
                if conf < cfg.assign_sim:
                    continue  # a weak "Synology is wrong" claim is noise
                others = [p for p in positions_by_person.get(y_pk, []) if p != pos]
                from_sim = (
                    float((X[pos] @ X[others].T).mean()) if others else None
                )
                reassigns.append(
                    {
                        "kind": "reassign",
                        "confidence": conf,
                        "person_key": x_pk,
                        "from_person_key": y_pk,
                        "payload": {
                            "face_id": fid,
                            "photo_id": meta["photo_id"],
                            "space": meta["space"],
                            "syno_face_id": sfid,
                            "from_person_id": y_pk[1],
                            "from_person_name": None,
                            "person_id": x_pk[1],
                            "person_name": None,
                            "bbox_normalized": meta["bbox_norm"],
                            "confidence": conf,
                            "from_similarity": from_sim,
                        },
                    }
                )

        # --- new_person ---
        if info.labeled_total == 0 and len(info.members) >= cfg.new_person_min_faces:
            exemplars = sorted(int(face_ids[p]) for p in info.members)[:20]
            new_persons.append(
                {
                    "kind": "new_person",
                    "confidence": None,
                    "payload": {
                        "face_ids": exemplars,
                        "size": len(info.members),
                        "suggested_name": None,
                    },
                }
            )

        # --- merge: two persons splitting a single cluster ---
        if info.labeled_total:
            strong = [
                pk
                for pk, cnt in info.votes.items()
                if cnt / info.labeled_total >= cfg.merge_vote_fraction
            ]
            for a, b in itertools.combinations(sorted(strong), 2):
                pair = (a, b)
                if pair in merge_pairs_seen:
                    continue
                merge_pairs_seen.add(pair)
                merges.append(
                    _merge_payload(
                        a,
                        b,
                        evidence={
                            "cluster_id": cid,
                            "vote_fractions": {
                                _pk_str(a): info.votes[a] / info.labeled_total,
                                _pk_str(b): info.votes[b] / info.labeled_total,
                            },
                            "exemplars": {
                                _pk_str(a): _exemplars(face_ids, info, label_map, a),
                                _pk_str(b): _exemplars(face_ids, info, label_map, b),
                            },
                        },
                    )
                )

    # --- merge: two clusters mapped to different persons, close centroids ---
    mapped = [info for info in clusters.values() if info.mapped_person is not None]
    for ca, cb in itertools.combinations(mapped, 2):
        if ca.mapped_person == cb.mapped_person:
            continue
        if ca.centroid is None or cb.centroid is None:
            continue
        sim = float(ca.centroid @ cb.centroid)
        if sim <= cfg.merge_centroid_sim:
            continue
        a, b = sorted([ca.mapped_person, cb.mapped_person])
        if (a, b) in merge_pairs_seen:
            continue
        merge_pairs_seen.add((a, b))
        # Order exemplar lookup by which cluster maps to which person.
        info_a, info_b = (ca, cb) if ca.mapped_person == a else (cb, ca)
        merges.append(
            _merge_payload(
                a,
                b,
                evidence={
                    "cluster_ids": [info_a.cluster_id, info_b.cluster_id],
                    "centroid_sim": sim,
                    "exemplars": {
                        _pk_str(a): _exemplars(face_ids, info_a, label_map, a),
                        _pk_str(b): _exemplars(face_ids, info_b, label_map, b),
                    },
                },
            )
        )

    emitter.progress("cluster.crossref", len(clusters), len(clusters))
    emitter.log(
        "info",
        f"cluster.crossref: proposed {len(assigns)} assign(s), {len(reassigns)} reassign(s), "
        f"{len(merges)} merge(s), {len(new_persons)} new person(s)",
        phase="cluster.crossref",
    )
    return ClusterResult(
        face_ids=face_ids,
        labels=labels,
        clusters=clusters,
        assigns=assigns,
        new_persons=new_persons,
        merges=merges,
        predicted_person=predicted_person,
        reassigns=reassigns,
    )


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v if n == 0 else (v / n)


def _pk_str(pk: PersonKey) -> str:
    return f"{pk[0]}:{pk[1]}"


def _exemplars(
    face_ids: np.ndarray, info: ClusterInfo, label_map: dict[int, PersonKey], pk: PersonKey
) -> list[int]:
    out = [
        int(face_ids[p])
        for p in info.members
        if label_map.get(int(face_ids[p])) == pk
    ]
    return sorted(out)[:8]


def _merge_payload(a: PersonKey, b: PersonKey, evidence: dict) -> dict:
    return {
        "kind": "merge",
        "confidence": None,
        "person_a": {"space": a[0], "person_id": a[1], "name": None},
        "person_b": {"space": b[0], "person_id": b[1], "name": None},
        "person_key_a": a,
        "person_key_b": b,
        "payload": {
            "person_a": {"space": a[0], "person_id": a[1], "name": None},
            "person_b": {"space": b[0], "person_id": b[1], "name": None},
            "evidence": evidence,
        },
    }


# --------------------------------------------------------------------------- #
# Orchestration (writes DB)
# --------------------------------------------------------------------------- #
def _cluster_labels(indices, sims, settings: Settings) -> np.ndarray:
    if settings.clustering.algorithm == "hdbscan":
        return hdbscan_alt.cluster_hdbscan(indices, sims, settings)
    return chinese_whispers.chinese_whispers(
        indices,
        sims,
        edge_threshold=settings.clustering.edge_threshold,
        iterations=settings.clustering.cw_iterations,
        seed=settings.clustering.seed,
    )


def _load_person_photos(conn: Connection) -> set[tuple[str, int, int]]:
    return {
        (row["space"], int(row["person_id"]), int(row["photo_id"]))
        for row in conn.execute(
            "SELECT space, person_id, photo_id FROM person_photos"
        )
    }


def _load_person_names(conn: Connection) -> dict[PersonKey, str | None]:
    return {
        (row["space"], int(row["id"])): row["name"]
        for row in conn.execute("SELECT space, id, name FROM persons")
    }


def _existing_identities(conn: Connection) -> dict[str, set]:
    """Payload identities already pending/approved/applied/hidden from ANY run.

    ``hidden`` counts as seen — that is the whole difference between hiding a
    suggestion and rejecting it. A rejected row is deliberately re-proposed on
    the next run; a hidden one is a human saying "never again".
    """
    assigns: set[tuple[int, int]] = set()
    merges: set[tuple] = set()
    new_persons: set[tuple] = set()
    reassigns: set[tuple[int, int]] = set()
    for row in conn.execute(
        "SELECT kind, payload_json FROM review_queue "
        "WHERE status IN ('pending','approved','applied','hidden')"
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        kind = row["kind"]
        if kind in ("assign", "low_confidence"):
            if "face_id" in payload and "person_id" in payload:
                assigns.add((int(payload["face_id"]), int(payload["person_id"])))
                # A human retargeted this face: the suggestion they overruled is
                # no longer in any payload, so register it too or the next run
                # proposes the wrong person all over again.
                if payload.get("original_person_id") is not None:
                    assigns.add(
                        (int(payload["face_id"]), int(payload["original_person_id"]))
                    )
        elif kind in ("merge", "merge_named"):
            pa, pb = payload.get("person_a"), payload.get("person_b")
            if pa and pb:
                key = tuple(
                    sorted(
                        [
                            (pa["space"], int(pa["person_id"])),
                            (pb["space"], int(pb["person_id"])),
                        ]
                    )
                )
                merges.add(key)
        elif kind == "new_person":
            fids = payload.get("face_ids")
            if fids:
                new_persons.add(tuple(sorted(int(f) for f in fids)))
        elif kind == "reassign":
            # Keyed on (our face, target person): the claim "this face is X"
            # stays the same even if Synology relabels it to a different
            # wrong person between runs.
            if "face_id" in payload and "person_id" in payload:
                reassigns.add((int(payload["face_id"]), int(payload["person_id"])))
    return {
        "assign": assigns,
        "merge": merges,
        "new_person": new_persons,
        "reassign": reassigns,
    }


def run_clustering(conn: Connection, settings: Settings) -> int:
    """Full clustering + cross-reference pass. Returns the new run_id.

    Deterministic given ``settings.clustering.seed``.
    """
    emitter = get_emitter()

    emitter.phase("cluster.load")
    face_ids, X = graph.load_fused(conn, settings)

    emitter.phase("cluster.graph")
    indices, sims = graph.build_or_load_graph(settings, face_ids, X)

    emitter.phase("cluster.labels")
    labels = _cluster_labels(indices, sims, settings)

    emitter.phase("cluster.crossref")
    all_labels, all_syno_ids = label_faces_with_ids(conn, settings)
    present = set(int(f) for f in face_ids.tolist())
    label_map = {fid: pk for fid, pk in all_labels.items() if fid in present}
    syno_face_ids = {
        fid: sfid for fid, sfid in all_syno_ids.items() if fid in present
    }
    face_meta = _load_face_meta(conn)
    person_photos = _load_person_photos(conn)
    result = _crossref_core(
        face_ids, X, labels, label_map, settings, face_meta, person_photos,
        syno_face_ids=syno_face_ids,
    )

    emitter.phase("cluster.persist")
    names = _load_person_names(conn)

    def name_of(pk: PersonKey) -> str | None:
        return names.get(pk)

    existing = _existing_identities(conn)
    ts = store.now()

    params = {
        "clustering": settings.clustering.model_dump(),
        "crossref": settings.crossref.model_dump(),
    }
    cur = conn.execute(
        "INSERT INTO cluster_runs (params_json, created_at) VALUES (?, ?)",
        (json.dumps(params), ts),
    )
    run_id = int(cur.lastrowid)

    # clusters + cluster_members
    for done, cid in enumerate(sorted(result.clusters)):
        emitter.progress("cluster.persist", done, len(result.clusters))
        info = result.clusters[cid]
        mapped = info.mapped_person
        conn.execute(
            "INSERT INTO clusters (run_id, cluster_id, size, mapped_person_id, "
            "map_space, vote_fraction, labeled_count) VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                cid,
                len(info.members),
                mapped[1] if mapped else None,
                mapped[0] if mapped else None,
                info.vote_fraction,
                info.labeled_total,
            ),
        )
        for pos in info.members:
            conn.execute(
                "INSERT INTO cluster_members (run_id, cluster_id, face_id) VALUES (?,?,?)",
                (run_id, cid, int(face_ids[pos])),
            )

    # review_queue with cross-run dedup
    for item in sorted(result.assigns, key=lambda x: x["payload"]["face_id"]):
        pk = item["person_key"]
        fid = item["payload"]["face_id"]
        if (fid, pk[1]) in existing["assign"]:
            continue
        existing["assign"].add((fid, pk[1]))
        payload = dict(item["payload"])
        payload["person_name"] = name_of(pk)
        conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, item["kind"], json.dumps(payload), item["confidence"], ts),
        )

    for item in sorted(result.reassigns, key=lambda x: x["payload"]["face_id"]):
        pk = item["person_key"]
        fid = item["payload"]["face_id"]
        if (fid, pk[1]) in existing["reassign"]:
            continue
        existing["reassign"].add((fid, pk[1]))
        payload = dict(item["payload"])
        payload["person_name"] = name_of(pk)
        payload["from_person_name"] = name_of(item["from_person_key"])
        conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, "reassign", json.dumps(payload), item["confidence"], ts),
        )

    for item in result.new_persons:
        key = tuple(sorted(item["payload"]["face_ids"]))
        if key in existing["new_person"]:
            continue
        existing["new_person"].add(key)
        conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, "new_person", json.dumps(item["payload"]), None, ts),
        )

    for item in result.merges:
        a, b = item["person_key_a"], item["person_key_b"]
        key = tuple(sorted([a, b]))
        if key in existing["merge"]:
            continue
        existing["merge"].add(key)
        payload = item["payload"]
        name_a, name_b = name_of(a), name_of(b)
        payload["person_a"]["name"] = name_a
        payload["person_b"]["name"] = name_b
        # Joining two already-named people destroys a human label and is
        # irreversible — split it into a distinct, more strictly gated kind.
        kind = (
            "merge_named"
            if (name_a or "").strip() and (name_b or "").strip()
            else "merge"
        )
        conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, kind, json.dumps(payload), None, ts),
        )

    emitter.progress("cluster.persist", len(result.clusters), len(result.clusters))
    queue_inserts = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    conn.commit()
    emitter.log(
        "info",
        f"cluster run {run_id}: {len(result.clusters)} cluster(s) stored, "
        f"{queue_inserts} new review item(s) queued (cross-run duplicates skipped)",
        phase="cluster.persist",
    )
    emitter.result(
        stats={
            "run_id": run_id,
            "clusters": len(result.clusters),
            "queue_inserts": int(queue_inserts),
        }
    )
    return run_id
