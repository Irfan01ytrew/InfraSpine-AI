"""Run untrusted-file parsing in a separate, killable child process with time and memory limits.

Parsing and chunking are the CPU-heavy, risk-bearing part of ingestion (malformed or hostile files, C
extensions). Each job runs `parse_and_chunk` in a fresh child process (the "spawn" start method on every
platform, so Windows development behaves like production):

* time limit: the child is killed when it runs longer than the processing timeout;
* memory limit: the child caps its own memory (a Windows Job Object, or RLIMIT_AS on POSIX), so an
  oversized document fails with MemoryError instead of exhausting the machine;
* cancellation: the parent kills the child as soon as the job is cancelled, replaced or deleted;
* orphan safety: the child exits by itself if its parent process dies.

Only plain data crosses the process boundary: file path, format and limits in; text chunks with their
locators out. The child never touches the database, the vector index or credentials.
"""

import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable

from app.rag.chunking import TextChunk
from app.rag.parsers.base import ParseError, ParseLimits

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.25


class ProcessingCancelled(Exception):
    """The job was cancelled, replaced or deleted while its file was being processed."""


class ProcessingCrashed(Exception):
    """The child process exited without a result (for example killed by the operating system)."""

    def __init__(self, exitcode: int | None) -> None:
        super().__init__(f"processor exited with code {exitcode}")
        self.exitcode = exitcode


def parse_and_chunk(
    path: str, file_format: str, limits: ParseLimits, chunk_size: int, overlap: int, use_docling: bool = True,
    pictures: bool = False,
) -> tuple[int | None, list[TextChunk], list, list]:
    """The work done per document: parse into sections, then chunk. Returns (PDF page count, chunks, pictures,
    tables). Pictures are PNG bytes with their page: those of a PDF when `pictures` is set (the worker sets it when
    they can be described), and an uploaded image itself. The worker, which can reach the LLM provider, describes
    them; this process never can. Tables are a PDF's or DOCX's tables as rows and columns (Docling only)."""
    from pathlib import Path

    from app.rag.chunking import chunk_sections
    from app.rag.parsers.image_parser import IMAGE_FORMATS
    from app.rag.parsers.registry import parse_document

    images: list | None = [] if pictures or file_format in IMAGE_FORMATS else None
    tables: list = []
    sections = parse_document(Path(path), file_format, limits, use_docling=use_docling, images=images, tables=tables)
    chunks = chunk_sections(sections, chunk_size, overlap)
    # PDFs report every page (blank ones too: every page has a section, a page with a table may have several);
    # slide and sheet counts were recorded at upload.
    page_count = max((s.locator.position for s in sections), default=0) if file_format == "pdf" else None
    return page_count, chunks, images or [], tables


# ------------------------------------------------------------------ child side


def limit_memory(max_bytes: int) -> bool:
    """Cap this process's memory. Returns False when the platform refused (the job still runs)."""
    if max_bytes <= 0:
        return True
    try:
        if sys.platform == "win32":
            return _limit_memory_windows(max_bytes)
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        return True
    except Exception:  # pragma: no cover - platform specific
        return False


def _limit_memory_windows(max_bytes: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_limit_process_memory = 0x00000100
    job_object_extended_limit_information = 9
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = job_object_limit_process_memory
    limits.ProcessMemoryLimit = max_bytes
    if not kernel32.SetInformationJobObject(job, job_object_extended_limit_information, ctypes.byref(limits), ctypes.sizeof(limits)):
        return False
    return bool(kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()))


def _exit_with_parent() -> None:
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        parent.join()  # returns when the parent process is gone
        os._exit(70)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _child_main(conn, target: Callable, args: tuple, memory_bytes: int) -> None:
    _exit_with_parent()
    if not limit_memory(memory_bytes):
        logger.warning("Parser memory limit could not be applied on this platform")
    try:
        result = target(*args)
    except ParseError as exc:
        message: tuple = ("parse_error", exc.code, exc.message)
    except MemoryError:
        message = ("memory",)
    except BaseException as exc:  # report the type only: never document content
        message = ("error", type(exc).__name__)
    else:
        message = ("ok", result)
    try:
        conn.send(message)
    except MemoryError:
        conn.send(("memory",))
    finally:
        conn.close()


# ------------------------------------------------------------------ parent side


def run_isolated(
    target: Callable,
    args: tuple,
    *,
    timeout_seconds: float,
    memory_bytes: int,
    cancelled: threading.Event | None = None,
    on_start: Callable[[multiprocessing.Process], None] | None = None,
):
    """Run `target(*args)` in a child process and return its result. Blocking: call from a thread.

    Raises `ParseError` (the file's own problem, a timeout or the memory limit: permanent),
    `ProcessingCancelled` or `ProcessingCrashed`. The child is always gone when this returns.
    """
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_main, args=(sender, target, args, memory_bytes), name="document-parser", daemon=True
    )
    process.start()
    sender.close()
    if on_start is not None:
        on_start(process)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise ProcessingCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParseError(
                    "processing_timeout",
                    f"Processing took longer than {int(timeout_seconds)} seconds and was stopped. Split the file "
                    "into smaller parts, or ask an administrator to raise RAG_PROCESSING_TIMEOUT_SECONDS.",
                )
            if receiver.poll(min(_POLL_SECONDS, remaining)):
                try:
                    message = receiver.recv()
                except (EOFError, OSError):
                    break  # the child exited without sending a result
                return _unwrap(message, memory_bytes)
            if not process.is_alive() and not receiver.poll(0):
                break
        process.join(5)
        raise ProcessingCrashed(process.exitcode)
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        receiver.close()


def _unwrap(message: tuple, memory_bytes: int):
    kind = message[0]
    if kind == "ok":
        return message[1]
    if kind == "parse_error":
        raise ParseError(message[1], message[2])
    if kind == "memory":
        raise ParseError(
            "memory_limit",
            f"Processing needed more than {memory_bytes // (1024 * 1024)} MB of memory and was stopped. Split the "
            "file into smaller parts, or ask an administrator to raise RAG_PARSER_MEMORY_MB.",
        )
    raise ProcessingCrashed(None)
