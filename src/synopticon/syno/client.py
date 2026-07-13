"""Synology Web API client.

Centralizes the things that silently break integrations:
- `encode_params` — Synology's parameter encoding quirks (JSON arrays,
  quoted strings, lowercase booleans), unit-tested against captured payloads.
- Runtime API version discovery via SYNO.API.Info (cached in sync_state);
  the live NAS runs newer versions than the public docs, never hardcode.
- Token-bucket throttling (separate read/write buckets), tenacity retry on
  transport errors and 5xx, single auto re-login on session-expiry codes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, Iterator

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from synopticon.config import Settings, Space
from synopticon.db import store

if TYPE_CHECKING:
    from synopticon.syno.auth import AuthSession

SESSION_EXPIRED_CODES = frozenset({105, 106, 107, 119})
WRITE_METHODS = frozenset({"add_face", "merge", "set", "delete_face", "separate", "upload"})
API_INFO_STATE_KEY = "api_info"

_SPACE_PREFIX: dict[str, str] = {"personal": "SYNO.Foto", "shared": "SYNO.FotoTeam"}
_RETRY_ATTEMPTS = 6


class QuotedString(str):
    """Marker: encode this value as a JSON-quoted string on the wire.

    Synology quotes *some* string params (merge/set `name`, suggest
    `name_prefix`, Download v2 `cache_key`) and leaves others bare
    (`api`, `method`). Call sites opt in explicitly with this marker.
    """


class SynoError(Exception):
    """Base class for Synology client errors."""


class SynoApiError(SynoError):
    """The API returned success=false."""

    def __init__(self, code: int | None, api: str, method: str):
        super().__init__(f"{api}.{method} failed with error code {code}")
        self.code = code
        self.api = api
        self.method = method


class SynoVersionError(SynoError):
    """No usable API version (preferred < minVersion)."""


class _ServerError(SynoError):
    """5xx from the NAS; retried by tenacity."""


def _plain(value: Any) -> Any:
    """Recursively convert dataclasses to dicts for JSON encoding."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _encode_value(value: Any) -> str:
    if isinstance(value, QuotedString):
        return json.dumps(str(value), ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_plain(value), separators=(",", ":"), ensure_ascii=False)
    return str(value)


def encode_params(params: dict[str, Any]) -> dict[str, str]:
    """Encode a params dict into Synology's wire format.

    - lists/tuples/dicts (and dataclasses inside them) -> compact JSON:
      ``id=[2660]``, ``additional=["thumbnail"]``
    - ``QuotedString`` -> JSON string: ``name="Foo"``
    - bool -> ``true``/``false``; int/float -> bare; None -> dropped.
    """
    return {key: _encode_value(value) for key, value in params.items() if value is not None}


class _TokenBucket:
    def __init__(self, rate: float):
        self._rate = max(rate, 0.001)
        self._capacity = max(1.0, self._rate)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


class SynoClient:
    """Synchronous Synology Photos API client (httpx.Client under the hood)."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self._settings = settings
        self._conn = conn
        self.http = httpx.Client(
            base_url=settings.nas.url,
            verify=settings.nas.verify_tls,
            timeout=settings.nas.timeout_s,
        )
        self._read_bucket = _TokenBucket(settings.nas.requests_per_second)
        self._write_bucket = _TokenBucket(settings.nas.write_requests_per_second)
        self._api_info: dict[str, Any] | None = None
        self._session: AuthSession | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> SynoClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def session(self) -> AuthSession | None:
        return self._session

    # -- version discovery ---------------------------------------------------

    @property
    def api_info(self) -> dict[str, Any]:
        """{api: {minVersion, maxVersion, path}} — cached in memory + sync_state."""
        if self._api_info is None:
            cached = store.get_state(self._conn, API_INFO_STATE_KEY)
            self._api_info = cached if cached else self._discover_api_info()
        return self._api_info

    def _discover_api_info(self) -> dict[str, Any]:
        payload = encode_params(
            {"api": "SYNO.API.Info", "version": 1, "method": "query", "query": "all"}
        )
        resp = self._send("POST", "/webapi/query.cgi", data=payload)
        envelope = resp.json()
        if not envelope.get("success", False):
            code = (envelope.get("error") or {}).get("code")
            raise SynoApiError(code, "SYNO.API.Info", "query")
        info = envelope.get("data") or {}
        store.set_state(self._conn, API_INFO_STATE_KEY, info)
        return info

    def version_for(self, api: str, preferred: int) -> int:
        """min(preferred, maxVersion); errors below minVersion; preferred if unknown."""
        info = self.api_info.get(api)
        if info is None:
            return preferred
        version = min(preferred, info["maxVersion"])
        if version < info["minVersion"]:
            raise SynoVersionError(
                f"{api}: preferred version {preferred} below minVersion {info['minVersion']}"
            )
        return version

    def path_for(self, api: str) -> str:
        info = self.api_info.get(api) or {}
        return info.get("path", "entry.cgi")

    # -- space abstraction ---------------------------------------------------

    def api_name(self, space: Space, suffix: str) -> str:
        """personal -> SYNO.Foto.<suffix>, shared -> SYNO.FotoTeam.<suffix>."""
        return f"{_SPACE_PREFIX[space]}.{suffix}"

    # -- auth ----------------------------------------------------------------

    def _ensure_auth(self) -> None:
        if self._session is None and self._settings.nas.account:
            from synopticon.syno import auth

            self._session = auth.login(self, self._settings, self._conn)

    def invalidate_session(self) -> None:
        self._session = None

    def _headers(self) -> dict[str, str]:
        if self._session is not None and self._session.synotoken:
            return {"X-SYNO-TOKEN": self._session.synotoken}
        return {}

    # -- request plumbing ------------------------------------------------------

    def _bucket_for(self, method: str) -> _TokenBucket:
        return self._write_bucket if method in WRITE_METHODS else self._read_bucket

    def _send(
        self,
        http_method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        retryer = Retrying(
            retry=retry_if_exception_type((httpx.TransportError, _ServerError)),
            wait=wait_exponential_jitter(initial=0.5, max=60.0),
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            reraise=True,
        )
        for attempt in retryer:
            with attempt:
                resp = self.http.request(
                    http_method,
                    url,
                    params=params,
                    data=data,
                    files=files,
                    headers=self._headers(),
                )
                if resp.status_code >= 500:
                    raise _ServerError(f"HTTP {resp.status_code} from {url}")
                return resp
        raise AssertionError("unreachable")  # pragma: no cover

    # -- envelope calls ---------------------------------------------------------

    def call(
        self,
        api: str,
        method: str,
        version: int | None = None,
        http_method: str = "POST",
        **params: Any,
    ) -> dict[str, Any]:
        """Call an API method; returns the `data` dict of the envelope.

        Raises SynoApiError on success=false; auto re-logins once on
        session-expiry codes (105/106/107/119).
        """
        return self._call(api, method, version, http_method, True, params)

    def _call(
        self,
        api: str,
        method: str,
        version: int | None,
        http_method: str,
        allow_relogin: bool,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_auth()
        if version is None:
            info = self.api_info.get(api)
            version = info["maxVersion"] if info else 1
        payload = encode_params({"api": api, "method": method, "version": version, **params})
        self._bucket_for(method).acquire()
        url = f"/webapi/{self.path_for(api)}"
        if http_method.upper() == "GET":
            resp = self._send("GET", url, params=payload)
        else:
            resp = self._send("POST", url, data=payload)
        envelope = resp.json()
        if not envelope.get("success", False):
            code = (envelope.get("error") or {}).get("code")
            if code in SESSION_EXPIRED_CODES and allow_relogin and self._settings.nas.account:
                self.invalidate_session()
                self._ensure_auth()
                return self._call(api, method, version, http_method, False, params)
            raise SynoApiError(code, api, method)
        return envelope.get("data") or {}

    def paginate(
        self,
        api: str,
        method: str,
        page_size: int = 500,
        list_key: str = "list",
        version: int | None = None,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across offset/limit pages until a short (or empty) page."""
        offset = 0
        while True:
            data = self.call(api, method, version=version, offset=offset, limit=page_size, **params)
            page = data.get(list_key) or []
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    # -- non-envelope calls ----------------------------------------------------

    def upload(
        self,
        api: str,
        method: str = "upload",
        version: int = 1,
        *,
        fields: dict[str, Any],
        file: tuple[str, bytes, str],
    ) -> dict[str, Any]:
        """Multipart upload (SYNO.Foto.Upload.Face style, per HAR entry 9).

        api/method/version ride in both the query string and the multipart
        body, followed by `fields` and the file blob.
        """
        self._ensure_auth()
        self._bucket_for(method).acquire()
        url = f"/webapi/{self.path_for(api)}"
        query = encode_params({"api": api, "method": method, "version": version})
        data = {"api": api, "method": method, "version": str(version)}
        data.update({key: str(value) for key, value in fields.items()})
        resp = self._send("POST", url, params=query, data=data, files={"file": file})
        envelope = resp.json()
        if not envelope.get("success", False):
            code = (envelope.get("error") or {}).get("code")
            raise SynoApiError(code, api, method)
        return envelope.get("data") or {}

    def stream(
        self,
        api: str,
        method: str,
        version: int | None = None,
        quote_api: bool = False,
        chunk_size: int = 256 * 1024,
        **params: Any,
    ) -> Iterator[bytes]:
        """GET a raw-bytes endpoint (Download.download) as a chunk iterator.

        `quote_api=True` sends api/method themselves as quoted strings —
        the live NAS's Download v2 form (`api="SYNO.Foto.Download"`).
        Adds `_sid` (and `SynoToken`) query params so the URL alone works.
        """
        self._ensure_auth()
        if version is None:
            info = self.api_info.get(api)
            version = info["maxVersion"] if info else 1
        query: dict[str, Any] = {
            "api": QuotedString(api) if quote_api else api,
            "method": QuotedString(method) if quote_api else method,
            "version": version,
            **params,
        }
        if self._session is not None:
            query.setdefault("_sid", self._session.sid)
            if self._session.synotoken:
                query.setdefault("SynoToken", self._session.synotoken)
        encoded = encode_params(query)
        self._bucket_for(method).acquire()
        url = f"/webapi/{self.path_for(api)}"
        with self.http.stream("GET", url, params=encoded, headers=self._headers()) as resp:
            if resp.status_code >= 400:
                raise SynoError(f"HTTP {resp.status_code} while streaming {api}.{method}")
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                resp.read()
                envelope = resp.json()
                code = (envelope.get("error") or {}).get("code")
                raise SynoApiError(code, api, method)
            for chunk in resp.iter_bytes(chunk_size):
                yield chunk
