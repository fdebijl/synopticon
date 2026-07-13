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
    from synopticon.syno.client import SynoClient, SynoError

    settings = _settings()
    conn = _conn(settings)
    typer.echo(f"NAS url:  {settings.nas.url or '<not set>'}")
    typer.echo(f"account:  {settings.nas.account or '<not set>'}")
    try:
        with SynoClient(settings, conn) as client:
            info = client.api_info
            typer.echo(f"reachable: yes ({len(info)} APIs discovered)")
            client._ensure_auth()
            session = client.session
            typer.echo(f"login:     OK (synotoken={'yes' if session.synotoken else 'no'}, "
                       f"2FA device token {'stored' if session.device_id else 'not needed/absent'})")
            person_api = client.api_name("personal", "Browse.Person")
            v = client.version_for(person_api, 3)
            typer.echo(f"photos:    {person_api} available at v{v}")
    except SynoError as exc:
        typer.echo(f"\nFAILED: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo("all good — you can run: synopticon sync")


def _progress(space: str, label: str):
    """Render a sync progress callback: a live line on a tty, periodic lines otherwise."""
    is_tty = sys.stdout.isatty()

    def cb(done: int, total: int | None):
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
):
    """Pull photos, persons, and ground-truth labels from the NAS (read-only)."""
    from synopticon.sync import items, persons
    from synopticon.syno.client import SynoClient

    settings = _settings()
    conn = _conn(settings)
    with SynoClient(settings, conn) as client:
        for sp in _spaces(settings, space):
            stats = items.sync_items(conn, client, sp, progress=_progress(sp, "items"))
            _finish_line()
            typer.echo(f"[{sp}] items: {stats}")
            stats = persons.sync_persons(conn, client, sp, progress=_progress(sp, "persons"))
            _finish_line()
            typer.echo(f"[{sp}] persons: {stats}")
            if not skip_faces:
                stats = persons.sync_faces(
                    conn, client, sp, only_tagged=not all_faces, resume=resume,
                    progress=_progress(sp, "faces"),
                )
                _finish_line()
                typer.echo(f"[{sp}] faces: {stats}")


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
    with SynoClient(settings, conn) as client:
        for sp in _spaces(settings, space):
            def fetch(row: sqlite3.Row) -> Path:
                return downloads.ensure_original(conn, client, settings, row)

            stats = run_extract(conn, settings, fetch, limit=limit, photo_id=photo_id, space=sp)
            typer.echo(f"[{sp}] {stats}")
    if not settings.storage.keep_originals:
        evicted = downloads.evict_originals(settings)
        typer.echo(f"evicted: {evicted}")


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


@app.command()
def review(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8686),
):
    """Serve the interactive review UI (approve/reject queue items)."""
    from synopticon.review.app import serve

    serve(_settings(), host=host, port=port)


@app.command()
def apply(
    kinds: str = typer.Option("assign", help="Comma-separated kinds: assign,merge,new_person."),
    person_id: int = typer.Option(None, help="Scope to a single person id."),
    apply_: bool = typer.Option(False, "--apply", help="Actually write to the NAS (default: dry-run)."),
    apply_merges: bool = typer.Option(False, help="Extra gate required to apply merges."),
    space: str = typer.Option("personal"),
    show_audit: bool = typer.Option(False, "--report", help="Print the audit trail afterwards."),
):
    """Apply approved review items to the NAS. Dry-run unless --apply is given."""
    from synopticon import audit
    from synopticon.syno.client import SynoClient
    from synopticon.syno.writeback import DryRunWriter, SynoWriter, apply_reviewed

    settings = _settings()
    conn = _conn(settings)
    kind_set = {k.strip() for k in kinds.split(",") if k.strip()}
    if apply_:
        with SynoClient(settings, conn) as client:
            writer = SynoWriter(client, conn, space)
            stats = apply_reviewed(conn, writer, kind_set, person_id=person_id, apply_merges=apply_merges)
    else:
        typer.echo("DRY RUN (pass --apply to write to the NAS)")
        stats = apply_reviewed(conn, DryRunWriter(conn), kind_set, person_id=person_id, apply_merges=apply_merges)
    typer.echo(f"{stats}")
    if show_audit:
        for row in audit.tail(conn, limit=50):
            typer.echo(dict(row))


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
