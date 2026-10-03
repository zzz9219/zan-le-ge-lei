"""Independent stdlib utilities for the public MIT collector's import contract.
Written without inspecting the proprietary core source.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class ProfileRegistry:
    """Record paths only; authentication stays inside the browser profile."""
    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / ".zan-le-ge-lei" / "profiles.json"

    def register_existing_profile(self, profile_dir, *, platform_name, origin):
        location = str(Path(profile_dir).expanduser().resolve())
        records = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        record = {"path": location, "platform": platform_name, "origin": origin}
        records[location] = record
        atomic_write_json(self.path, records)
        return record


@contextmanager
def profile_lock(profile_dir, *, blocking=True, timeout=60.0):
    import msvcrt
    folder = Path(profile_dir)
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with (folder / ".collector.lock").open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if not blocking or time.monotonic() >= deadline:
                    raise ValueError("browser_profile_busy") from error
                time.sleep(0.2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
