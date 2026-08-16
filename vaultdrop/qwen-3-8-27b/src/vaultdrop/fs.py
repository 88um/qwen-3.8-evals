"""Durable filesystem primitives.

Every byte that must survive `kill -9` is written through this module:
stage to a temp file, fsync the file, atomically rename into place, then fsync
the directory. A file therefore appears at its final path either fully written
or not at all — never partially. Readers that observe the final path never see
a torn write.
"""

import os
import uuid
from pathlib import Path


def fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_write(tmp_dir: Path, data: bytes) -> Path:
    """Write `data` to a fresh temp file under tmp_dir and fsync it.

    Returns the temp path. Caller must rename it into place (durable_rename) or
    unlink it. The temp name is unique per call, so concurrent writers never
    collide.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f".{uuid.uuid4().hex}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except FileNotFoundError:
            pass
        raise
    return tmp


def durable_rename(tmp: Path, final: Path) -> None:
    """Atomically move a staged temp file to its final path and fsync the dir."""
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(tmp), str(final))   # atomic on POSIX; replaces if exists
    fsync_dir(final.parent)


def durable_write(tmp_dir: Path, final: Path, data: bytes) -> None:
    """stage_write + durable_rename in one call."""
    tmp = stage_write(tmp_dir, data)
    durable_rename(tmp, final)


def read_all(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def unlink_quiet(path: Path) -> bool:
    try:
        os.unlink(str(path))
        return True
    except FileNotFoundError:
        return False
