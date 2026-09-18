"""Small filesystem and serialization boundary; no Hermes imports or external deps."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024


class StageError(Exception):
    """Safe reason code only. Never attach raw provider/command output."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise StageError("duplicate_json_key")
        result[key] = value
    return result


def parse_json(data: bytes | str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(StageError("nonfinite_json")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StageError("invalid_json") from exc


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise StageError("invalid_json_value") from exc


def read_regular(path: str | Path, maximum: int = MAX_FILE_BYTES) -> bytes:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StageError("not_regular_file")
            if info.st_size > maximum:
                raise StageError("file_too_large")
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise StageError("file_too_large")
            return data
    except OSError as exc:
        raise StageError("file_unavailable") from exc


def read_json(path: str | Path) -> Any:
    return parse_json(read_regular(path, MAX_JSON_BYTES))


def sha256_file(path: str | Path) -> str:
    # Bounded read, including executables pinned by the approved catalog.
    return hashlib.sha256(read_regular(path)).hexdigest()


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def create_file(path: Path, data: bytes) -> None:
    """Create-only; never follows a pre-existing destination symlink."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise StageError("destination_exists_or_unwritable") from exc


def save_json(path: Path, value: Any) -> None:
    data = canonical(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the rename when the filesystem supports directory fsync.
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def under_run(run: Path, relative: str) -> Path:
    candidate = run / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise StageError("path_escape")
    # Reject symlinks in *all* existing components inside the run.
    part = run
    for component in Path(relative).parts:
        part = part / component
        if part.is_symlink():
            raise StageError("symlink_not_allowed")
    if not candidate.resolve().is_relative_to(run.resolve()):
        raise StageError("path_escape")
    return candidate


def run_path(path: str | Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_dir():
        raise StageError("invalid_run_directory")
    return path.resolve()


@contextmanager
def run_lock(run: Path) -> Iterator[None]:
    try:
        fd = os.open(str(run / ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise StageError("lock_unavailable") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StageError("run_busy") from exc
        yield
    finally:
        os.close(fd)
