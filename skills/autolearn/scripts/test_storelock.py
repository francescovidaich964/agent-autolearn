"""Tests for the cross-process store lock and atomic writes (RL-INT-001..003)."""
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import storelock  # noqa: E402
import registry  # noqa: E402


def _writer(persona_dir: str, worker: int, n: int) -> None:
    reg = registry.MemoryRegistry(Path(persona_dir))
    for i in range(n):
        reg.add(f"worker {worker} lesson number {i} unique token w{worker}x{i}")


def test_concurrent_registry_adds_are_not_lost(tmp_path):
    (tmp_path / registry.REGISTRY_FILE).write_text("")
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), w, 15)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    recs = registry.MemoryRegistry(tmp_path).load()
    assert len(recs) == 60, "a concurrent read-modify-write dropped updates"
    assert not list(tmp_path.glob("*.tmp"))


def test_lock_is_reentrant(tmp_path):
    with storelock.store_lock(tmp_path):
        with storelock.store_lock(tmp_path):
            assert (tmp_path / storelock.LOCK_DIRNAME).is_dir()
        assert (tmp_path / storelock.LOCK_DIRNAME).is_dir()
    assert not (tmp_path / storelock.LOCK_DIRNAME).exists()


def test_stale_lock_is_broken(tmp_path):
    lock = tmp_path / storelock.LOCK_DIRNAME
    lock.mkdir()
    old = time.time() - storelock.STALE_AFTER_S - 5
    os.utime(lock, (old, old))
    with storelock.store_lock(tmp_path, timeout=2):
        pass
    assert not lock.exists()


def test_fresh_lock_times_out(tmp_path):
    (tmp_path / storelock.LOCK_DIRNAME).mkdir()
    with pytest.raises(storelock.StoreLockTimeout):
        with storelock.store_lock(tmp_path, timeout=0.3):
            pass


def test_atomic_write_retries_sharing_violation(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        return real(src, dst)

    monkeypatch.setattr(storelock.os, "replace", flaky)
    storelock.atomic_write_text(target, "ok")
    assert target.read_text() == "ok"
    assert calls["n"] == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_never_leaves_tmp_on_failure(tmp_path, monkeypatch):
    def always(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(storelock.os, "replace", always)
    with pytest.raises(PermissionError):
        storelock.atomic_write_text(tmp_path / "f.json", "x", retries=1)
    assert not list(tmp_path.glob("*.tmp"))


def test_sweep_tmp_removes_only_old_files(tmp_path):
    old = tmp_path / "memories.jsonl.123.tmp"
    new = tmp_path / "memories.jsonl.456.tmp"
    old.write_text("x")
    new.write_text("y")
    t = time.time() - 7200
    os.utime(old, (t, t))
    removed = storelock.sweep_tmp(tmp_path)
    assert removed == [old.name]
    assert new.exists()


def test_trim_lines_keeps_tail(tmp_path):
    f = tmp_path / "obs.jsonl"
    f.write_text("".join(f"{i}\n" for i in range(10)))
    assert storelock.trim_lines(f, 3)
    assert f.read_text().splitlines() == ["7", "8", "9"]
