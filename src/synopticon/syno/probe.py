"""Reusable NAS connectivity probe (shared by the ``check`` CLI + setup wizard).

``probe(settings, conn)`` runs the same three read-only checks the ``check``
command has always run — API discovery, login, and Browse.Person version — but
returns them as structured :class:`ProbeResult` data instead of printing. The
CLI renders it byte-identically to the old inline output; the web setup wizard
serialises it with :meth:`ProbeResult.to_dict`.

Nothing here mutates NAS state. It *does* touch the DB: a successful login lands
the 2FA device token in ``sync_state`` (via the auth layer) so future logins
skip the OTP prompt — which is exactly why the wizard runs it against the real
connection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..db import Connection

from synopticon.config import Settings

# The steps in the order they run; used to attribute a failure to the step that
# did not complete.
_STEP_ORDER = ("reachable", "login", "photos")


@dataclass
class Step:
    """One probe step: a stable ``name``, whether it passed, and a human detail."""

    name: str
    ok: bool
    detail: str


@dataclass
class ProbeResult:
    ok: bool
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    # Structured facts the CLI needs to reproduce its exact output and the
    # wizard surfaces in the UI.
    api_count: int | None = None
    synotoken: bool = False
    device_token: bool = False
    person_api: str | None = None
    person_api_version: int | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "steps": [asdict(s) for s in self.steps],
            "error": self.error,
            "api_count": self.api_count,
            "synotoken": self.synotoken,
            "device_token": self.device_token,
            "person_api": self.person_api,
            "person_api_version": self.person_api_version,
        }


def _failed_step_name(steps: list[Step]) -> str:
    """The first step in the canonical order that did not record a result."""
    done = {s.name for s in steps}
    for name in _STEP_ORDER:
        if name not in done:
            return name
    return _STEP_ORDER[-1]


def probe(settings: Settings, conn: Connection) -> ProbeResult:
    """Run the read-only NAS connectivity checks and return structured results.

    Catches :class:`~synopticon.syno.client.SynoError` (covers unreachable NAS,
    bad credentials, version mismatches); the failing step is recorded with
    ``ok=False`` and ``error`` carries the message.
    """
    from synopticon.syno.client import SynoClient, SynoError

    result = ProbeResult(ok=True)
    try:
        with SynoClient(settings, conn) as client:
            info = client.api_info
            result.api_count = len(info)
            result.steps.append(Step("reachable", True, f"{len(info)} APIs discovered"))

            client._ensure_auth()
            session = client.session
            result.synotoken = bool(session and session.synotoken)
            result.device_token = bool(session and session.device_id)
            result.steps.append(
                Step(
                    "login",
                    True,
                    f"synotoken={'yes' if result.synotoken else 'no'}, "
                    f"2FA device token "
                    f"{'stored' if result.device_token else 'not needed/absent'}",
                )
            )

            person_api = client.api_name("personal", "Browse.Person")
            v = client.version_for(person_api, 3)
            result.person_api = person_api
            result.person_api_version = v
            result.steps.append(Step("photos", True, f"{person_api} available at v{v}"))
    except SynoError as exc:
        result.ok = False
        result.error = str(exc)
        result.steps.append(Step(_failed_step_name(result.steps), False, str(exc)))
    return result
