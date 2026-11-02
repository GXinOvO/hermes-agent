"""Visible, elapsed-time activity for synchronous update maintenance."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import sys
import threading
import time

HEARTBEAT_SECONDS = 10.0


@contextmanager
def maintenance_stage(message: str):
    """Mirror phase activity to CLI/Desktop; stop the heartbeat on every exit."""
    from hermes_cli.update_stage import publish_stage

    started = time.monotonic()
    stopped = threading.Event()
    stream = sys.stdout

    def emit(status: str) -> None:
        text = f"{message} — {status}, elapsed {time.monotonic() - started:.1f}s"
        try:
            print(f"  → {text}", file=stream, flush=True)
        except OSError:
            # A closed terminal must not turn a read-only check into a failed
            # maintenance operation; Desktop's stage channel still receives it.
            pass
        publish_stage(text)

    interval = HEARTBEAT_SECONDS

    def heartbeat() -> None:
        while not stopped.wait(interval):
            emit("still running")

    emit("starting")
    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(heartbeat,), name="update-maintenance-progress", daemon=True)
    worker.start()
    try:
        yield
    except BaseException:
        stopped.set()
        worker.join()
        emit("stopped without completion")
        raise
    else:
        stopped.set()
        worker.join()
        emit("finished")
