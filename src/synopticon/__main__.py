"""`python -m synopticon` entry point.

Wraps the Typer app so an uncaught exception is reported into the structured
progress stream before it propagates. Click prints the traceback to stderr, which
a terminal user sees anyway — but a web job consumer needs the machine-readable
`error` event to show a cause rather than a bare "failed" chip.
"""

from __future__ import annotations

import traceback

from synopticon.cli import app


def main() -> None:
    try:
        app()
    except SystemExit:
        raise  # normal exit path, including typer.Exit(code)
    except BaseException as exc:  # noqa: BLE001 - reported, then re-raised
        from synopticon.progress import get_emitter

        get_emitter().error(
            f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()
        )
        raise


if __name__ == "__main__":
    main()
