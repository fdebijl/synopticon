"""Synopticon CLI: sync -> extract -> cluster -> report -> review -> apply."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import typer

from synopticon.config import Settings, load_settings
from synopticon.db import store
from synopticon.progress import get_emitter

app = typer.Typer(help=__doc__, no_args_is_help=True)
models_app = typer.Typer(help="Model weight management.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluate clustering quality against held-out labels.", no_args_is_help=True)
app.add_typer(models_app, name="models")
app.add_typer(eval_app, name="eval")


def _settings() -> Settings:
    return load_settings()


def _conn(settings: Settings) -> sqlite3.Connection:
    return store.connect(settings.storage.db_path)


def _spaces(settings: Settings, space: str | None) -> list[str]:
    return [space] if space else list(settings.nas.spaces)


def _apply_overrides(settings: Settings, overrides: list[str]) -> Settings:
    """Apply `section.key=value` overrides, e.g. clustering.edge_threshold=0.47."""
    s = settings.model_copy(deep=True)
    for item in overrides:
        path, _, raw = item.partition("=")
        if not _ or "." not in path:
            raise typer.BadParameter(f"expected section.key=value, got {item!r}")
        section, key = path.split(".", 1)
        target = getattr(s, section)
        getattr(target, key)  # raises AttributeError on typos
        try:
            value = json.loads(raw)  # numbers, bools, lists
        except json.JSONDecodeError:
            value = raw  # bare strings, e.g. algorithm=hdbscan
        setattr(target, key, value)
    return s


@models_app.command("download")
def models_download(
    only: list[str] = typer.Option(None, "--only", help="Subset of model keys."),
    allow_record_hash: bool = typer.Option(False, help="Record sha256 of not-yet-pinned models."),
):
    """Download + verify model weights into storage.models_dir."""
    settings = _settings()
    script = Path(__file__).resolve().parents[2] / "scripts" / "download_models.py"
    if not script.is_file():
        typer.echo(f"error: {script} not found (run from a source checkout or the Docker image)", err=True)
        raise typer.Exit(1)
    cmd = [sys.executable, str(script), "--models-dir", str(settings.storage.models_dir)]
    if only:
        cmd += ["--only", *only]
    if allow_record_hash:
        cmd.append("--allow-record-hash")
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def check():
    """Verify NAS connectivity and credentials (read-only, fast)."""
    from synopticon.syno.probe import probe

    settings = _settings()
    conn = _conn(settings)
    emitter = get_emitter()
    typer.echo(f"NAS url:  {settings.nas.url or '<not set>'}")
    typer.echo(f"account:  {settings.nas.account or '<not set>'}")
    result = probe(settings, conn)
    # Render each step that completed, in order — on failure this reproduces the
    # partial progress the inline version used to print before raising.
    passed = {step.name for step in result.steps if step.ok}
    if "reachable" in passed:
        typer.echo(f"reachable: yes ({result.api_count} APIs discovered)")
    if "login" in passed:
        typer.echo(
            f"login:     OK (synotoken={'yes' if result.synotoken else 'no'}, "
            f"2FA device token "
            f"{'stored' if result.device_token else 'not needed/absent'})"
        )
    if "photos" in passed:
        typer.echo(f"photos:    {result.person_api} available at v{result.person_api_version}")
    if not result.ok:
        emitter.error(result.error or "")
        emitter.result(ok=False)
        typer.echo(f"\nFAILED: {result.error}", err=True)
        raise typer.Exit(1)
    emitter.result(
        stats={
            "apis": result.api_count,
            "person_api": result.person_api,
            "person_api_version": result.person_api_version,
        }
    )
    typer.echo("all good — you can run: synopticon sync")


def _progress(space: str, label: str):
    """Render a sync progress callback: a live line on a tty, periodic lines otherwise.

    Also emits a structured `sync.<label>` progress event when the progress
    protocol is enabled (SYNOPTICON_PROGRESS_FILE set); a no-op otherwise, so
    terminal output is unchanged.
    """
    is_tty = sys.stdout.isatty()
    emitter = get_emitter()

    def cb(done: int, total: int | None):
        emitter.progress(f"sync.{label}", done, total, space=space)
        suffix = f"{done}/{total}" if total is not None else str(done)
        if is_tty:
            typer.echo(f"\r[{space}] {label}: {suffix}", nl=False)
        else:
            typer.echo(f"[{space}] {label}: {suffix}")

    return cb


def _finish_line():
    """Terminate the in-place progress line so the summary lands on its own line."""
    if sys.stdout.isatty():
        typer.echo("")


def _skip_logger(space: str, label: str):
    """Render a per-item skip as a standalone console line, without clobbering progress."""

    def cb(photo_id: int, code, url):
        _finish_line()  # end any in-place progress line first
        link = f" -> {url}" if url else ""
        typer.secho(
            f"[{space}] {label}: skipped photo {photo_id} (error code {code}){link}",
            fg="yellow",
            err=True,
        )

    return cb


def _human_bytes(n: int | None) -> str:
    size = float(int(n or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable; keeps type-checkers happy


def _item_web_url(settings: Settings, space: str, photo_id: int) -> str | None:
    """Synology Photos deep link to one timeline item, or None if no base URL set."""
    base = (settings.nas.web_url or settings.nas.url or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/?launchApp=SYNO.Foto.AppInstance#/{space}_space/timeline/item/{photo_id}"


@app.command()
def hwinfo():
    """Print hardware/environment stats relevant to extraction & clustering (for bug reports)."""
    from synopticon import diagnostics

    typer.echo(diagnostics.render(_settings()), nl=False)


@app.command()
def sync(
    space: str = typer.Option(None, help="Limit to one space (default: all configured)."),
    skip_faces: bool = typer.Option(False, help="Skip the per-photo face-level ground-truth pass."),
    all_faces: bool = typer.Option(False, help="Fetch list_face for ALL photos, not just tagged ones."),
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Resume the faces pass from its saved cursor (default: resume)."
    ),
    hash_: bool = typer.Option(
        False, "--hash",
        help="Also download each image and store its sha256 + perceptual hash on the photos table.",
    ),
):
    """Pull photos, persons, and ground-truth labels from the NAS (read-only)."""
    from synopticon.sync import downloads, hashes, items, persons
    from synopticon.syno.client import SynoApiError, SynoClient

    settings = _settings()
    conn = _conn(settings)
    emitter = get_emitter()
    result_stats: dict[str, dict] = {}
    with SynoClient(settings, conn) as client:
        for sp in _spaces(settings, space):
            emitter.phase("sync.items", space=sp)
            stats = items.sync_items(conn, client, sp, progress=_progress(sp, "items"))
            _finish_line()
            typer.echo(f"[{sp}] items: {stats}")
            result_stats[f"{sp}.items"] = dict(stats)
            emitter.phase("sync.persons", space=sp)
            stats = persons.sync_persons(conn, client, sp, progress=_progress(sp, "persons"))
            _finish_line()
            typer.echo(f"[{sp}] persons: {stats}")
            result_stats[f"{sp}.persons"] = dict(stats)
            emitter.phase("sync.similar", space=sp)
            try:
                stats = items.sync_similar(conn, client, sp, progress=_progress(sp, "similar"))
                _finish_line()
                typer.echo(f"[{sp}] similar: {stats}")
                result_stats[f"{sp}.similar"] = dict(stats)
            except SynoApiError as exc:
                _finish_line()
                typer.secho(
                    f"[{sp}] similar: skipped (feature may be unavailable on this space: {exc})",
                    fg="yellow",
                    err=True,
                )
            if not skip_faces:
                emitter.phase("sync.faces", space=sp)
                stats = persons.sync_faces(
                    conn, client, sp, only_tagged=not all_faces, resume=resume,
                    progress=_progress(sp, "faces"),
                    on_skip=_skip_logger(sp, "faces"),
                )
                _finish_line()
                typer.echo(f"[{sp}] faces: {stats}")
                result_stats[f"{sp}.faces"] = dict(stats)
            if hash_:
                emitter.phase("sync.hashes", space=sp)

                def fetch(row: sqlite3.Row) -> Path:
                    return downloads.ensure_original(conn, client, settings, row)

                stats = hashes.sync_hashes(conn, fetch, sp, progress=_progress(sp, "hashes"))
                _finish_line()
                typer.echo(f"[{sp}] hashes: {stats}")
                result_stats[f"{sp}.hashes"] = dict(stats)
    if hash_ and not settings.storage.keep_originals:
        evicted = downloads.evict_originals(settings)
        typer.echo(f"evicted: {evicted}")
    emitter.result(stats=result_stats)


@app.command()
def extract(
    limit: int = typer.Option(None, help="Process at most N photos."),
    photo_id: int = typer.Option(None, help="Process a single photo id."),
    space: str = typer.Option(None, help="Limit to one space (default: all configured)."),
):
    """Detect faces and compute ensemble embeddings (resumable)."""
    from synopticon.pipeline.runner import run_extract
    from synopticon.sync import downloads
    from synopticon.syno.client import SynoClient

    settings = _settings()
    conn = _conn(settings)
    emitter = get_emitter()
    with SynoClient(settings, conn) as client:
        for sp in _spaces(settings, space):
            emitter.phase("extract", space=sp)

            def fetch(row: sqlite3.Row) -> Path:
                return downloads.ensure_original(conn, client, settings, row)

            stats = run_extract(conn, settings, fetch, limit=limit, photo_id=photo_id, space=sp)
            typer.echo(f"[{sp}] {stats}")
    if not settings.storage.keep_originals:
        evicted = downloads.evict_originals(settings)
        typer.echo(f"evicted: {evicted}")


@app.command()
def benchmark(
    limit: int = typer.Option(25, help="Number of photos to time (default: 25)."),
    photo_id: int = typer.Option(None, help="Benchmark a single photo id."),
    space: str = typer.Option("personal", help="Space to pull benchmark photos from."),
    warmup: int = typer.Option(2, help="Photos to process before timing (absorbs ONNX startup cost)."),
):
    """Measure extraction throughput (detect+embed) without writing anything.

    Read-only: reuses the extract pipeline but persists no faces/embeddings/crops.
    Originals are downloaded (and cached) exactly as `extract` would.
    """
    from synopticon.pipeline.benchmark import run_benchmark
    from synopticon.sync import downloads
    from synopticon.syno.client import SynoClient

    settings = _settings()
    conn = _conn(settings)
    with SynoClient(settings, conn) as client:
        def fetch(row: sqlite3.Row) -> Path:
            return downloads.ensure_original(conn, client, settings, row)

        stats = run_benchmark(
            conn, settings, fetch, limit=limit, photo_id=photo_id, space=space,
            warmup=warmup, progress=typer.echo,
        )
    typer.echo(str(stats))


@app.command()
def cluster():
    """Cluster all embeddings and cross-reference against Synology persons."""
    from synopticon.cluster.crossref import run_clustering

    settings = _settings()
    conn = _conn(settings)
    run_id = run_clustering(conn, settings)
    typer.echo(f"cluster run {run_id} complete; next: synopticon report")


@app.command()
def recluster(
    set_: list[str] = typer.Option(
        [], "--set", help="Override, e.g. --set clustering.edge_threshold=0.47 (repeatable)."
    ),
):
    """Re-run clustering from cached embeddings with parameter overrides. Never hits the NAS."""
    from synopticon.cluster.crossref import run_clustering

    settings = _apply_overrides(_settings(), set_)
    conn = _conn(settings)
    run_id = run_clustering(conn, settings)
    typer.echo(f"cluster run {run_id} complete")


# Tables produced by the local pipeline (safe to wipe and rebuild). Ordered
# children-first so the deletes hold with foreign keys enforced.
_DERIVED_TABLES = (
    "review_queue",
    "cluster_members",
    "clusters",
    "cluster_runs",
    "embeddings",
    "faces",
    "extract_log",
)
# Metadata mirrored from the NAS; only cleared with --all (forces a re-sync).
_SYNCED_TABLES = ("syno_faces", "person_photos", "persons", "photos", "sync_state")


@app.command()
def reset(
    all_: bool = typer.Option(
        False,
        "--all",
        help="Also drop synced NAS metadata (photos/persons/ground truth); forces a full re-sync.",
    ),
    keep_crops: bool = typer.Option(
        False, help="Leave crop images on disk (default: delete them along with the faces)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Clear locally-computed data so the pipeline can rebuild from scratch.

    Removes faces, embeddings, clusters and the review queue (and their crop
    images) — useful after tweaking detection/clustering settings, since the
    review UI pools items from every run and stale items would otherwise linger.
    Synced NAS metadata is kept unless --all is given. Never touches the NAS.
    """
    settings = _settings()
    conn = _conn(settings)

    tables = list(_DERIVED_TABLES) + (list(_SYNCED_TABLES) if all_ else [])
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    total = sum(counts.values())

    scope = "ALL data (including synced NAS metadata)" if all_ else "pipeline-derived data"
    typer.echo(f"Reset {scope} in {settings.storage.db_path}:")
    for t in tables:
        if counts[t]:
            typer.echo(f"  {t}: {counts[t]} rows")
    if not keep_crops:
        typer.echo(f"  crop images under {settings.storage.crops_dir}")

    if total == 0 and keep_crops:
        typer.echo("nothing to reset")
        return
    if not yes:
        typer.confirm("Proceed?", abort=True)

    # audit_log.review_item_id references review_queue with no cascade; null it
    # first so clearing the queue can't trip the constraint. This keeps the
    # NAS-write history, dropping only the now-stale local linkage.
    conn.execute("UPDATE audit_log SET review_item_id = NULL WHERE review_item_id IS NOT NULL")
    for t in tables:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    if not keep_crops:
        shutil.rmtree(settings.storage.crops_dir, ignore_errors=True)
        settings.storage.crops_dir.mkdir(parents=True, exist_ok=True)
    # Drop cached kNN graphs (keyed by face_ids, now stale).
    for npz in Path(settings.storage.data_dir).glob("graph_*.npz"):
        npz.unlink()

    nxt = "synopticon sync && synopticon extract" if all_ else "synopticon extract"
    typer.echo(f"reset complete — next: {nxt}")


@app.command("clear-queue")
def clear_queue(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Delete only the PENDING review-queue items so `cluster` can re-generate them.

    Pending rows regenerate cleanly on the next `cluster` run (crossref re-inserts
    them fresh, picking up any person names synced since the last run). Approved,
    applied and rejected rows are left untouched: they are the ledger crossref uses
    to avoid re-surfacing work you have already handled, so this command refuses to
    touch them. Never contacts the NAS.
    """
    settings = _settings()
    conn = _conn(settings)

    n = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
    ).fetchone()[0]
    if n == 0:
        typer.echo("no pending items to clear")
        return

    typer.echo(f"Clear {n} pending review-queue item(s) in {settings.storage.db_path}")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    # audit_log.review_item_id references review_queue with no cascade; null any
    # links to the rows we're about to drop so the delete can't trip the FK.
    conn.execute(
        "UPDATE audit_log SET review_item_id = NULL WHERE review_item_id IN "
        "(SELECT item_id FROM review_queue WHERE status = 'pending')"
    )
    conn.execute("DELETE FROM review_queue WHERE status = 'pending'")
    conn.commit()

    typer.echo(f"cleared {n} pending item(s) — next: synopticon cluster")


@app.command("regen-crops")
def regen_crops_cmd(
    space: str = typer.Option(None, help="Limit to one space (default: all configured)."),
    only_missing: bool = typer.Option(
        True, "--only-missing/--all",
        help="Only rebuild crops whose files are missing (default); --all rewrites every face's crops.",
    ),
    limit: int = typer.Option(None, help="Process at most N photos."),
):
    """Rebuild face crop images from stored bboxes + originals (fetched from the NAS).

    Crops are a derived artifact — the faces table keeps every bbox/landmark, so
    this reconstructs them without re-running detection or embedding. Use it after
    `delete-crops`, or to repair a partial wipe. Resumable: it commits per photo.
    """
    from synopticon.pipeline.crops import regen_crops
    from synopticon.sync import downloads
    from synopticon.syno.client import SynoClient

    settings = _settings()
    conn = _conn(settings)
    emitter = get_emitter()
    result_stats: dict[str, dict] = {}
    with SynoClient(settings, conn) as client:
        for sp in _spaces(settings, space):
            emitter.phase("crops.regen", space=sp)

            def fetch(row: sqlite3.Row) -> Path:
                return downloads.ensure_original(conn, client, settings, row)

            stats = regen_crops(
                conn, settings, fetch, space=sp, only_missing=only_missing,
                limit=limit, progress=_progress(sp, "regen"),
            )
            _finish_line()
            typer.echo(f"[{sp}] {stats}")
            result_stats[sp] = dict(stats)
    if not settings.storage.keep_originals:
        evicted = downloads.evict_originals(settings)
        typer.echo(f"evicted: {evicted}")
    emitter.result(stats=result_stats)


@app.command("delete-crops")
def delete_crops_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
):
    """Delete all crop images from disk to reclaim space (the DB is untouched).

    Crops can dominate on-disk size across intermittent runs; since they're a pure
    derived artifact, wiping them between passes is safe — rebuild on demand with
    `regen-crops`. Never touches the NAS or the faces table.
    """
    from synopticon.pipeline.crops import crops_disk_usage, delete_crops

    settings = _settings()
    crops_dir = settings.storage.crops_dir
    files, nbytes = crops_disk_usage(crops_dir)
    if files == 0:
        typer.echo(f"no crops on disk under {crops_dir}")
        return

    typer.echo(f"Delete {files} crop files ({_human_bytes(nbytes)}) under {crops_dir}")
    typer.echo("The faces table is untouched; rebuild later with `synopticon regen-crops`.")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    delete_crops(crops_dir)
    typer.echo("crops deleted")


@app.command()
def report(run_id: int = typer.Option(None, help="Cluster run to report on (default: latest).")):
    """Generate the static HTML review report."""
    from synopticon.review.report import generate

    settings = _settings()
    conn = _conn(settings)
    if run_id is None:
        row = conn.execute("SELECT MAX(run_id) AS r FROM cluster_runs").fetchone()
        if row["r"] is None:
            typer.echo("no cluster runs yet — run: synopticon cluster", err=True)
            raise typer.Exit(1)
        run_id = row["r"]
    path = generate(conn, settings, run_id)
    typer.echo(f"report: {path}")
    get_emitter().result(stats={"run_id": run_id, "path": str(path)})


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Interface to bind (default: localhost)."),
    port: int = typer.Option(8686, help="Port to listen on."),
):
    """Serve the full web GUI: setup wizard, pipeline jobs, review, apply, maintenance.

    Requires the [review] extra (fastapi/uvicorn). Login is mandatory; the first
    boot walks you through creating an admin account. Put a reverse proxy in
    front for TLS.
    """
    from synopticon.web.app import serve

    serve(_settings(), host=host, port=port)


@app.command()
def review(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8686),
):
    """[deprecated] Alias for `web` — opens the same server at its /review page."""
    from synopticon.web.app import serve

    typer.secho(
        "note: `synopticon review` is deprecated; use `synopticon web`.",
        fg="yellow",
        err=True,
    )
    typer.echo(f"review UI: http://{host}:{port}/review")
    serve(_settings(), host=host, port=port)


@app.command()
def apply(
    kinds: str = typer.Option(
        "assign,low_confidence",
        help="Comma-separated kinds: assign,low_confidence,reassign,merge,merge_named,new_person. "
        "assign and low_confidence are both approved face assignments and apply by default.",
    ),
    person_id: int = typer.Option(None, help="Scope to a single person id."),
    apply_: bool = typer.Option(False, "--apply", help="Actually write to the NAS (default: dry-run)."),
    apply_merges: bool = typer.Option(
        False, help="Extra gate required to apply merges (unnamed side involved)."
    ),
    apply_merges_named: bool = typer.Option(
        False,
        help="Extra gate required to apply merge_named items — joining two "
        "already-named people, irreversible and destroys a human label.",
    ),
    apply_reassigns: bool = typer.Option(
        False, help="Extra gate required to apply reassigns (moves an existing Synology face label to a different person)."
    ),
    space: str = typer.Option("personal"),
    show_audit: bool = typer.Option(False, "--report", help="Print the audit trail afterwards."),
):
    """Apply approved review items to the NAS. Dry-run unless --apply is given."""
    from synopticon import audit
    from synopticon.syno.client import SynoClient
    from synopticon.syno.writeback import (
        DryRunWriter,
        SynoWriter,
        apply_reviewed,
        configure_apply_logging,
    )

    settings = _settings()
    conn = _conn(settings)
    kind_set = {k.strip() for k in kinds.split(",") if k.strip()}

    # Per-operation apply log lives at the project root (alongside dry-runs too).
    logfile = configure_apply_logging(Path(__file__).resolve().parents[2] / "apply.log")
    typer.echo(f"logging apply operations to {logfile}")

    get_emitter().phase("apply")
    if apply_:
        with SynoClient(settings, conn) as client:
            writer = SynoWriter(client, conn, space)
            stats = apply_reviewed(
                conn, writer, kind_set, person_id=person_id,
                apply_merges=apply_merges, apply_merges_named=apply_merges_named,
                apply_reassigns=apply_reassigns,
            )
    else:
        typer.echo("DRY RUN (pass --apply to write to the NAS)")
        stats = apply_reviewed(
            conn, DryRunWriter(conn), kind_set, person_id=person_id,
            apply_merges=apply_merges, apply_merges_named=apply_merges_named,
            apply_reassigns=apply_reassigns,
        )
    typer.echo(f"{stats}")
    if show_audit:
        for row in audit.tail(conn, limit=50):
            typer.echo(dict(row))


@app.command("apply-all")
def apply_all(
    yes: bool = typer.Option(
        False, "--yes", "-Y", help="Skip the confirmation prompt."
    ),
    apply_merges_named: bool = typer.Option(
        False,
        help="Also apply named->named merges (required to include them with -Y; "
        "interactively they get a separate confirmation instead).",
    ),
    person_id: int = typer.Option(None, help="Scope to a single person id."),
    space: str = typer.Option("personal"),
    show_audit: bool = typer.Option(False, "--report", help="Print the audit trail afterwards."),
):
    """Apply ALL approved review items — assigns, reassigns, and merges — to the NAS.

    Unlike `apply` there is no dry-run stage and the merge/reassign gates are
    implicitly lifted; the confirmation prompt (or --yes/-Y) is the only gate.
    The exception is `merge_named` (joining two already-named people): it is
    always listed with a loud warning and requires a *separate* confirmation,
    or --apply-merges-named when running non-interactively with -Y.
    """
    from synopticon import audit
    from synopticon.syno.client import SynoClient
    from synopticon.syno.writeback import (
        ASSIGN_KINDS,
        MERGE_KIND,
        MERGE_NAMED_KIND,
        REASSIGN_KIND,
        SynoWriter,
        apply_reviewed,
        configure_apply_logging,
    )

    settings = _settings()
    conn = _conn(settings)
    kind_set = set(ASSIGN_KINDS) | {REASSIGN_KIND, MERGE_KIND, MERGE_NAMED_KIND}

    placeholders = ",".join("?" for _ in kind_set)
    counts = {
        row["kind"]: row["n"]
        for row in conn.execute(
            f"SELECT kind, COUNT(*) AS n FROM review_queue "
            f"WHERE status = 'approved' AND kind IN ({placeholders}) GROUP BY kind",
            sorted(kind_set),
        )
    }
    total = sum(counts.values())
    if not total:
        typer.echo("nothing to do: no approved review items")
        raise typer.Exit()

    summary = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
    typer.echo(f"approved items: {summary}")
    if counts.get(MERGE_KIND):
        typer.echo("note: merges are irreversible — consider a NAS snapshot first")

    # Named->named merges are the one dangerous class: list every pair and gate
    # them behind their own confirmation, separate from the bulk prompt.
    named_count = counts.get(MERGE_NAMED_KIND, 0)
    include_named = False
    if named_count:
        typer.secho(
            f"WARNING: {named_count} approved merge(s) join two already-named "
            "people — irreversible and each destroys a human-assigned label:",
            fg="red",
        )
        for row in conn.execute(
            "SELECT payload_json FROM review_queue "
            "WHERE status = 'approved' AND kind = ? ORDER BY item_id",
            (MERGE_NAMED_KIND,),
        ):
            payload = json.loads(row["payload_json"])
            a, b = payload.get("person_a") or {}, payload.get("person_b") or {}
            la = a.get("name") or a.get("person_id")
            lb = b.get("name") or b.get("person_id")
            typer.echo(f"  - {la} (id {a.get('person_id')}) ↔ {lb} (id {b.get('person_id')})")
        if apply_merges_named:
            include_named = True
        elif not yes:
            include_named = typer.confirm(
                "also apply these named->named merges?", default=False
            )
        else:
            typer.echo(
                "skipping named->named merges "
                "(pass --apply-merges-named to include them with -Y)"
            )

    write_total = total - (0 if include_named else named_count)
    if not yes:
        typer.confirm(f"write {write_total} item(s) to the NAS?", abort=True)

    logfile = configure_apply_logging(Path(__file__).resolve().parents[2] / "apply.log")
    typer.echo(f"logging apply operations to {logfile}")

    get_emitter().phase("apply")
    with SynoClient(settings, conn) as client:
        writer = SynoWriter(client, conn, space)
        stats = apply_reviewed(
            conn, writer, kind_set, person_id=person_id,
            apply_merges=True, apply_merges_named=include_named, apply_reassigns=True,
        )
    typer.echo(f"{stats}")
    if show_audit:
        for row in audit.tail(conn, limit=50):
            typer.echo(dict(row))


@app.command()
def dedupe(
    exact: bool = typer.Option(False, "--exact", help="Delete byte-identical (sha256) duplicates."),
    visual: bool = typer.Option(False, "--visual", help="Delete near-identical (phash) duplicates."),
    threshold: int = typer.Option(
        5, help="Max phash hamming distance (0-64) for --visual matches; lower is stricter."
    ),
    apply_: bool = typer.Option(False, "--apply", help="Actually delete from the NAS (default: dry-run)."),
    space: str = typer.Option(None, help="Limit to one space (default: all configured)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt when deleting."),
):
    """Delete duplicate photos from the NAS using synced content hashes.

    --exact removes byte-identical copies (grouped by sha256); --visual removes
    visually near-identical copies (phash within --threshold bits). The
    highest-resolution photo in each group is kept, the rest deleted. Hashes are
    trusted, so there is no review step — but this is dry-run unless --apply is
    given, and it prints a Synology Photos link per photo first.
    """
    from synopticon import dedupe as dd
    from synopticon.dedupe_writeback import delete_items
    from synopticon.syno.client import SynoClient
    from synopticon.syno.writeback import configure_apply_logging

    if not exact and not visual:
        typer.echo("nothing to do: pass --exact and/or --visual", err=True)
        raise typer.Exit(1)

    settings = _settings()
    conn = _conn(settings)
    spaces = _spaces(settings, space)

    drop_ids_by_space: dict[str, list[int]] = {}
    total_groups = total_drop = total_bytes = 0
    for sp in spaces:
        groups = []
        if exact:
            groups += dd.find_exact(conn, sp)
        if visual:
            groups += dd.find_visual(conn, sp, threshold)
        if not groups:
            continue
        typer.echo(f"\n[{sp}] {len(groups)} duplicate group(s):")
        unique_drop: dict[int, sqlite3.Row] = {}
        for grp in groups:
            keep = grp.keep
            kurl = _item_web_url(settings, sp, store.link_photo_id(conn, sp, keep["id"]))
            typer.echo(
                f"  {grp.kind}: keep {keep['id']} ({keep['filename']}, "
                f"{keep['width']}x{keep['height']}){' -> ' + kurl if kurl else ''}"
            )
            for d in grp.drop:
                unique_drop.setdefault(d["id"], d)
                url = _item_web_url(settings, sp, store.link_photo_id(conn, sp, d["id"]))
                typer.echo(
                    f"      drop {d['id']} ({d['filename']}, {_human_bytes(d['filesize'])})"
                    f"{' -> ' + url if url else ''}"
                )
        drop_ids_by_space[sp] = dd.collect_drop_ids(groups)
        total_groups += len(groups)
        total_drop += len(unique_drop)
        total_bytes += sum(int(r["filesize"] or 0) for r in unique_drop.values())

    if total_drop == 0:
        typer.echo("no duplicates found")
        return

    typer.echo(
        f"\ntotal: {total_groups} group(s), {total_drop} photo(s), "
        f"{_human_bytes(total_bytes)} reclaimable"
    )

    logfile = configure_apply_logging(Path(__file__).resolve().parents[2] / "apply.log")
    emitter = get_emitter()

    if not apply_:
        typer.echo("\nDRY RUN — pass --apply to delete these photos from the NAS")
        emitter.phase("dedupe.delete", dry_run=True)
        for sp, ids in drop_ids_by_space.items():
            delete_items(conn, None, sp, ids, dry_run=True)
        emitter.result(stats={"groups": total_groups, "drop": total_drop}, dry_run=True)
        return

    if not yes:
        typer.confirm(f"delete {total_drop} photo(s) from the NAS?", abort=True)
    typer.echo(f"logging delete operations to {logfile}")
    emitter.phase("dedupe.delete")
    agg = {"deleted": 0, "skipped": 0, "failed": 0}
    with SynoClient(settings, conn) as client:
        for sp, ids in drop_ids_by_space.items():
            stats = delete_items(conn, client, sp, ids, dry_run=False)
            for k in agg:
                agg[k] += stats[k]
    typer.echo(f"{agg}")
    emitter.result(stats=agg)


@eval_app.command("holdout")
def eval_holdout(
    mask_fraction: float = typer.Option(0.2),
    seed: int = typer.Option(42),
):
    """Mask a fraction of known labels, recluster, and measure recovery."""
    from synopticon.eval.holdout import run_holdout

    settings = _settings()
    conn = _conn(settings)
    metrics = run_holdout(conn, settings, mask_fraction=mask_fraction, seed=seed)
    typer.echo(json.dumps(metrics, indent=2))


@eval_app.command("grid")
def eval_grid(
    grid_json: str = typer.Argument(..., help='e.g. \'{"edge_threshold": [0.45, 0.5, 0.55]}\''),
    mask_fraction: float = typer.Option(0.2),
    seed: int = typer.Option(42),
):
    """Grid-search tunables; writes eval_grid.csv under data_dir."""
    from synopticon.eval.holdout import grid_search

    settings = _settings()
    conn = _conn(settings)
    rows = grid_search(conn, settings, json.loads(grid_json), mask_fraction=mask_fraction, seed=seed)
    typer.echo(f"{len(rows)} combinations evaluated -> {settings.storage.data_dir / 'eval_grid.csv'}")


if __name__ == "__main__":
    app()
