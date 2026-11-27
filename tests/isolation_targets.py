"""Importable functions that tests run inside the parser child process (spawned processes can only run
module-level functions). They mimic misbehaving parsers: slow, memory-hungry or crashing."""

import os
import time
from pathlib import Path

from app.rag.isolation import parse_and_chunk


def sleep_then_parse(path: str, file_format: str, limits, chunk_size: int, overlap: int, *rest):
    Path(path + ".pid").write_text(str(os.getpid()))
    time.sleep(float(os.environ.get("TEST_PARSER_SLEEP", "3")))
    return parse_and_chunk(path, file_format, limits, chunk_size, overlap, *rest)


def hang(path: str, *args):
    Path(path + ".pid").write_text(str(os.getpid()))
    time.sleep(3600)


def allocate(path: str, *args):
    blocks = [bytearray(64 * 1024 * 1024) for _ in range(16)]  # 1 GB, well over the test limit
    return len(blocks)


def crash(path: str, *args):
    os._exit(3)


def docling_native_exit_then_fallback(
    path: str, file_format: str, limits, chunk_size: int, overlap: int, use_docling: bool = True,
    pictures: bool = False,
):
    """Model a native Docling access violation, then let the worker exercise its built-in parser fallback."""
    if use_docling:
        # Windows reports this as STATUS_ACCESS_VIOLATION; POSIX still observes an abnormal child exit.
        os._exit(0xC0000005)
    return parse_and_chunk(path, file_format, limits, chunk_size, overlap, False, pictures)


def docling_native_exit_then_empty(
    path: str, file_format: str, limits, chunk_size: int, overlap: int, use_docling: bool = True,
    pictures: bool = False,
):
    """Model a crashed parser followed by a fallback that returned no extracted content."""
    if use_docling:
        os._exit(0xC0000005)
    return None, [], [], []


def allocate_mb(megabytes: int) -> int:
    return len(bytearray(megabytes * 1024 * 1024))


def add(a: int, b: int) -> int:
    return a + b


def hold_index_lock(directory: str) -> str:
    from app.rag.vector_store import IndexLockedError, IndexOwnerLock

    try:
        IndexOwnerLock(Path(directory)).acquire()
    except IndexLockedError:
        return "locked"
    return "acquired"


def claim_index_role(directory: str, requested: str) -> str:
    from app.rag.components import claim_index_role as claim
    from app.rag.vector_store import IndexLockedError

    try:
        lock, role = claim(Path(directory), requested)
    except IndexLockedError:
        return "locked"
    if lock is not None:
        lock.release()
    return role
