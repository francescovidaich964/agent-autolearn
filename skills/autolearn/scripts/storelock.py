"""Cross-process store lock, atomic writes and tmp sweeping.

Every autolearn process that mutates a persona store (reviewer CLI calls,
curator, plugin-spawned compose, the JS plugin itself) serializes its
read-modify-write sections through one lock: an atomic ``mkdir`` of
``<persona>/.store.lock/``. ``mkdir`` is atomic on every platform and is
available to Python, Node/Bun and POSIX sh alike, so the plugin and the
wrapper can take the same lock without a native dependency.

Without the lock, two processes that each ``load() -> modify -> save()`` the
registry silently drop one another's update (last writer wins), and on
Windows ``os.replace`` raises ``PermissionError`` while another process holds
the target open, leaving an orphaned ``*.tmp`` and losing the write.

Spec: docs/designs/robust-learning-loop/LLD.md (RL-INT-001..003).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from pathlib import Path

LOCK_DIRNAME = ".store.lock"
# A holder older than this is presumed dead (crashed mid-write) and its lock
# is broken. Lock scopes are single read-modify-write sections, so a live
# holder never comes close.
STALE_AFTER_S = 120.0
# Wait longer than the stale threshold so a dead holder's lock is always
# broken before a waiter gives up.
DEFAULT_TIMEOUT_S = 150.0
TMP_MAX_AGE_S = 3600.0

_local = threading.local()


class StoreLockTimeout(TimeoutError):
    pass


def _depths() -> dict:
    d = getattr(_local, "depths", None)
    if d is None:
        d = {}
        _local.depths = d
    return d


def _lock_age_s(lock: Path) -> float | None:
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return None


def _break_stale(lock: Path) -> None:
    # Rename first so two waiters breaking the same stale lock cannot both
    # succeed and then both "acquire": only one rename wins.
    grave = lock.with_name(f"{LOCK_DIRNAME}.stale.{os.getpid()}.{time.time_ns()}")
    try:
        os.rename(lock, grave)
    except OSError:
        return
    shutil.rmtree(grave, ignore_errors=True)


@contextlib.contextmanager
def store_lock(persona_dir, timeout: float = DEFAULT_TIMEOUT_S):
    """Hold the persona store lock. Re-entrant within one thread."""
    persona_dir = Path(persona_dir)
    persona_dir.mkdir(parents=True, exist_ok=True)
    key = str(persona_dir.resolve())
    depths = _depths()
    if depths.get(key, 0) > 0:
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    lock = persona_dir / LOCK_DIRNAME
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            age = _lock_age_s(lock)
            if age is not None and age > STALE_AFTER_S:
                _break_stale(lock)
                continue
            if time.monotonic() >= deadline:
                raise StoreLockTimeout(f"store lock busy: {lock}")
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        except PermissionError:
            # Windows: mkdir can transiently fail while a breaker's rename
            # of the same path is in flight.
            if time.monotonic() >= deadline:
                raise StoreLockTimeout(f"store lock unavailable: {lock}")
            time.sleep(delay)
    try:
        (lock / "owner").write_text(f"{os.getpid()} {time.time():.0f} py\n", encoding="utf-8")
    except OSError:
        pass
    depths[key] = 1
    try:
        yield
    finally:
        depths[key] = 0
        shutil.rmtree(lock, ignore_errors=True)


def atomic_write_text(path, text: str, *, retries: int = 8) -> None:
    """Write ``text`` to ``path`` via a pid-unique tmp + ``os.replace``.

    Retries the replace on ``PermissionError`` (a Windows sharing violation
    while a reader holds the target open) and never leaves the tmp behind.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        delay = 0.02
        for attempt in range(retries + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def append_line(path, line: str) -> None:
    path = Path(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def trim_lines(path, max_lines: int) -> bool:
    """Keep the last ``max_lines`` lines. Caller holds the store lock."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if len(lines) <= max_lines:
        return False
    atomic_write_text(path, "\n".join(lines[-max_lines:]) + "\n")
    return True


def sweep_tmp(persona_dir, max_age_s: float = TMP_MAX_AGE_S) -> list[str]:
    """Remove ``*.tmp`` files older than ``max_age_s`` left by crashed writers."""
    persona_dir = Path(persona_dir)
    removed: list[str] = []
    now = time.time()
    for base in (persona_dir, persona_dir / "skills"):
        try:
            candidates = list(base.glob("*.tmp"))
        except OSError:
            continue
        for f in candidates:
            try:
                if now - f.stat().st_mtime > max_age_s:
                    f.unlink()
                    removed.append(f.name)
            except OSError:
                continue
    return removed
