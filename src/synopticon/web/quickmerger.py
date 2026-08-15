"""QuickMerger API: interactive triage of unnamed Synology persons.

Port of the ``har/quickmerger.js`` userscript into the Utilities page. The flow
is one unnamed person at a time: name it, merge it into an existing person, hide
it, or skip. Every route lives under ``/api/quickmerger/*`` and every write goes
through :class:`~synopticon.syno.writeback.SynoWriter` with the ``quickmerger``
action prefix, so the audit trail says which surface issued it.

Safety (this is the only GUI surface that writes to the NAS outside the
review-queue apply path — do not weaken):

* Every write needs an explicit ``confirm: true`` in the request body; without
  it the route answers 428, the same consent code the job layer uses.
* A merge re-fetches **both** people from the NAS immediately before writing and
  refuses (409) if the merged-away side carries a name. Named↔named merges — the
  most dangerous write in the project — are therefore unreachable from here, the
  same way ``apply-all``/``-Y`` are unreachable from the job layer.
* Naming and hiding are reversible; the merge is not, which is why the frontend
  confirms once per session before its first write.

NAS access is via one lazily-built, reused :class:`SynoClient`
(:class:`NasSession`): a client per request would re-login on every keystroke of
the suggest box. The client is synchronous and its SQLite connection hops
threadpool workers between requests, so every use is serialized under one lock
and that connection is opened with ``check_same_thread=False``. Handlers are
sync ``def`` (Starlette threadpools them) or ``async def`` + ``run_in_threadpool``
where a JSON body has to be awaited first — nothing here may block the loop.
"""

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from urllib.parse import quote

from ..config import Settings
from ..db import Connection, store
from ..syno import foto
from ..syno.client import SynoApiError, SynoClient, SynoError
from ..syno.models import Person

#: How long a fetched unnamed-person list stays usable without `refresh=true`.
#: The list is a triage worklist, not live state — the UI removes people as it
#: handles them, and a stale entry costs one skipped card.
_PERSONS_TTL = 300.0

#: Person thumbnails are immutable for a given cache_key, but they are private
#: user data — cache in the browser only.
_THUMB_CACHE_CONTROL = "private, max-age=86400"

_SUGGEST_LIMIT = 10


class NasSession:
    """A reused :class:`SynoClient` plus the lock that serializes access to it.

    ``use()`` yields the client with the lock held. A transport/auth-level
    failure discards the client so the next call rebuilds and re-logins; an
    API-level ``SynoApiError`` (the NAS answering "no") leaves it alone.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.RLock()
        self._client: SynoClient | None = None
        self._conn: Connection | None = None

    @contextmanager
    def use(self) -> Iterator[SynoClient]:
        with self._lock:
            if self._client is None:
                self._conn = store.connect(self._settings, check_same_thread=False)
                self._client = SynoClient(self._settings, self._conn)
            try:
                yield self._client
            except SynoError as exc:
                if not isinstance(exc, SynoApiError):
                    self.reset()
                raise

    def reset(self) -> None:
        with self._lock:
            client, conn = self._client, self._conn
            self._client, self._conn = None, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - teardown must never mask the cause
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class _PersonCache:
    """Per-space unnamed-person worklists, with removal on write."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, list[dict]]] = {}

    def get(self, space: str) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(space)
            if entry is None or time.monotonic() - entry[0] > _PERSONS_TTL:
                return None
            return entry[1]

    def put(self, space: str, persons: list[dict]) -> None:
        with self._lock:
            self._entries[space] = (time.monotonic(), persons)

    def drop_person(self, space: str, person_id: int) -> None:
        with self._lock:
            entry = self._entries.get(space)
            if entry is None:
                return
            self._entries[space] = (
                entry[0],
                [p for p in entry[1] if p.get("id") != person_id],
            )


def register_quickmerger_routes(
    app,
    settings: Settings,
    conn: Callable[[], Connection],
) -> None:
    """Attach the QuickMerger API to ``app``.

    ``conn`` is ``create_app``'s per-request connection factory (used for the
    audit rows and the local ``persons`` mirror); the NAS client keeps its own
    long-lived connection, see :class:`NasSession`.
    """
    from fastapi import Request, Response
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from ..review.queries import person_url, syno_web_base
    from ..syno.writeback import SynoWriter

    session = NasSession(settings)
    # The lifespan handler closes it on shutdown (it owns a live HTTPS
    # connection pool and a SQLite connection of its own).
    app.state.nas_session = session
    cache = _PersonCache()
    spaces = list(settings.nas.spaces)
    web_base = syno_web_base(settings)

    def _space(raw: str | None) -> str:
        """Validate a requested space against the configured ones."""
        space = (raw or "").strip() or (spaces[0] if spaces else "personal")
        if space not in ("personal", "shared"):
            raise ValueError(f"unknown space {space!r}")
        return space

    def _person_json(person: Person, space: str) -> dict[str, Any]:
        thumb = None
        if person.thumbnail_cache_key:
            thumb = (
                "/api/quickmerger/thumb"
                f"?space={quote(space)}&id={person.id}"
                f"&cache_key={quote(person.thumbnail_cache_key)}"
            )
        return {
            "id": person.id,
            "space": space,
            "name": person.name or "",
            "item_count": person.item_count,
            "thumb_url": thumb,
            "link": person_url(web_base, space, person.id),
        }

    def _nas_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, SynoApiError):
            return JSONResponse(
                {"error": str(exc), "code": exc.code}, status_code=502
            )
        return JSONResponse({"error": str(exc)}, status_code=502)

    def _needs_confirm() -> JSONResponse:
        return JSONResponse(
            {
                "error": "QuickMerger writes to the NAS; confirm required.",
                "requirement": "confirm",
            },
            status_code=428,
        )

    # -- read ------------------------------------------------------------- #
    @app.get("/api/quickmerger/status")
    def api_quickmerger_status():
        nas = settings.nas
        return {
            "spaces": spaces,
            "nas_configured": bool(
                nas.url.strip() and nas.account.strip() and nas.password.get_secret_value()
            ),
            "web_base": web_base,
        }

    @app.get("/api/quickmerger/persons")
    def api_quickmerger_persons(space: str = "", refresh: bool = False):
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        if not refresh:
            cached = cache.get(resolved)
            if cached is not None:
                return {"space": resolved, "persons": cached, "cached": True}

        try:
            with session.use() as client:
                # `show_more=True` is the userscript's listing: without it the
                # long tail of low-item-count people — which is precisely the
                # backlog QuickMerger exists to clear — never appears.
                people = [
                    _person_json(p, resolved)
                    for p in foto.list_persons(
                        client, resolved, show_hidden=False, show_more=True
                    )
                    if not (p.name or "").strip()
                ]
        except SynoError as exc:
            return _nas_error(exc)

        cache.put(resolved, people)
        return {"space": resolved, "persons": people, "cached": False}

    @app.get("/api/quickmerger/thumb")
    def api_quickmerger_thumb(space: str = "", id: int = 0, cache_key: str = ""):
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if not cache_key:
            return JSONResponse({"error": "cache_key is required"}, status_code=422)
        try:
            with session.use() as client:
                # Buffered, not streamed: a StreamingResponse would hold the
                # session lock across the whole response body.
                data = b"".join(
                    foto.download_person_thumbnail(client, resolved, id, cache_key)
                )
        except SynoError as exc:
            return _nas_error(exc)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": _THUMB_CACHE_CONTROL},
        )

    @app.get("/api/quickmerger/suggest")
    def api_quickmerger_suggest(space: str = "", prefix: str = ""):
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        needle = prefix.strip()
        if not needle:
            return {"suggestions": []}
        try:
            with session.use() as client:
                people = foto.suggest_person(
                    client, resolved, needle, limit=_SUGGEST_LIMIT
                )
        except SynoError as exc:
            return _nas_error(exc)
        return {"suggestions": [_person_json(p, resolved) for p in people]}

    # -- write ------------------------------------------------------------- #
    def _writer(client: SynoClient, c: Connection, space: str) -> SynoWriter:
        return SynoWriter(client, c, space, action_prefix="quickmerger")

    def _write_failed(result) -> JSONResponse:
        return JSONResponse(
            {"error": "NAS rejected the write", "code": result.error_code},
            status_code=502,
        )

    @app.post("/api/quickmerger/name")
    async def api_quickmerger_name(request: Request):
        body = await request.json()
        if body.get("confirm") is not True:
            return _needs_confirm()
        try:
            resolved = _space(body.get("space"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        person_id = body.get("person_id")
        name = (body.get("name") or "").strip()
        if not isinstance(person_id, int) or not name:
            return JSONResponse(
                {"error": "person_id (int) and a non-empty name are required"},
                status_code=422,
            )

        def work():
            with session.use() as client:
                c = conn()
                try:
                    result = _writer(client, c, resolved).rename(person_id, name)
                    if result.success:
                        c.execute(
                            "UPDATE persons SET name = ? WHERE space = ? AND id = ?",
                            (name, resolved, person_id),
                        )
                        c.commit()
                finally:
                    c.close()
                return result

        try:
            result = await run_in_threadpool(work)
        except SynoError as exc:
            return _nas_error(exc)
        if not result.success:
            return _write_failed(result)
        cache.drop_person(resolved, person_id)
        return {"ok": True, "person_id": person_id, "name": name}

    @app.post("/api/quickmerger/hide")
    async def api_quickmerger_hide(request: Request):
        body = await request.json()
        if body.get("confirm") is not True:
            return _needs_confirm()
        try:
            resolved = _space(body.get("space"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        person_id = body.get("person_id")
        if not isinstance(person_id, int):
            return JSONResponse({"error": "person_id (int) is required"}, status_code=422)

        def work():
            with session.use() as client:
                c = conn()
                try:
                    result = _writer(client, c, resolved).set_show(person_id, False)
                    if result.success:
                        c.execute(
                            "UPDATE persons SET show = 0 WHERE space = ? AND id = ?",
                            (resolved, person_id),
                        )
                        c.commit()
                finally:
                    c.close()
                return result

        try:
            result = await run_in_threadpool(work)
        except SynoError as exc:
            return _nas_error(exc)
        if not result.success:
            return _write_failed(result)
        cache.drop_person(resolved, person_id)
        return {"ok": True, "person_id": person_id}

    @app.post("/api/quickmerger/merge")
    async def api_quickmerger_merge(request: Request):
        body = await request.json()
        if body.get("confirm") is not True:
            return _needs_confirm()
        try:
            resolved = _space(body.get("space"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        source_id, target_id = body.get("source_id"), body.get("target_id")
        if not isinstance(source_id, int) or not isinstance(target_id, int):
            return JSONResponse(
                {"error": "source_id and target_id (ints) are required"}, status_code=422
            )
        if source_id == target_id:
            return JSONResponse(
                {"error": "cannot merge a person into itself"}, status_code=422
            )

        def work():
            with session.use() as client:
                # Ground truth, re-read immediately before the write: the local
                # mirror and the client's worklist can both be minutes stale,
                # and this is the check that keeps named<->named merges out of
                # the GUI entirely.
                source = foto.get_person(client, resolved, source_id)
                target = foto.get_person(client, resolved, target_id)
                if (source.name or "").strip():
                    return "named_source", source, target, None
                c = conn()
                try:
                    result = _writer(client, c, resolved).merge(
                        target.id, [source.id], target.name or ""
                    )
                    if result.success:
                        # The merged-away person no longer exists on the NAS;
                        # mirror that locally so the review UI stops offering
                        # it. `sync` reconciles the rest (person_photos, faces).
                        c.execute(
                            "UPDATE persons SET deleted = 1 WHERE space = ? AND id = ?",
                            (resolved, source_id),
                        )
                        c.commit()
                finally:
                    c.close()
                return "written", source, target, result

        try:
            status, source, target, result = await run_in_threadpool(work)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except SynoError as exc:
            return _nas_error(exc)

        if status == "named_source":
            return JSONResponse(
                {
                    "error": (
                        f"person {source_id} is named {source.name!r} on the NAS — "
                        "QuickMerger only merges unnamed people (a named↔named "
                        "merge destroys a human label and must go through review)."
                    ),
                    "requirement": "unnamed_source",
                },
                status_code=409,
            )
        if not result.success:
            return _write_failed(result)
        cache.drop_person(resolved, source_id)
        return {
            "ok": True,
            "source_id": source_id,
            "target_id": target_id,
            "name": target.name or "",
        }
