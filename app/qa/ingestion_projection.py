"""600 GB ingestion qualification — projection helpers (Step H slice 5, bounded).

The full 600 GB qualification cannot run on this VM (1.5 shared CPUs, no
target-like hardware, HuggingFace unreachable): it needs a staging stack on
target hardware. What CAN be proven here is the method, and a measured
lower bound:

* ``synthetic_corpus`` builds a deterministic mixed corpus (TXT pages) so a
  small-scale run is reproducible;
* ``measure_chunk_throughput`` times the real ``split_text`` chunker over
  that corpus — chunking is the cheapest ingestion stage, so its throughput
  is a *ceiling* on whole-pipeline throughput, never the pipeline's speed;
* ``project_time_to_index`` turns a measured throughput into a time-to-index
  for 600 GB under a stated model (linear by default; sublinear when the
  caller supplies a scaling exponent). The projection always carries its
  assumptions in the result — a number without them is a guess.

Nothing here claims 600 GB is qualified. The projection is recorded in
``docs/capacity-benchmark.md`` next to the earlier vector benchmarks, with
the same "does not qualify 600 GB" caveat.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

TARGET_BYTES = 600 * 1024 * 1024 * 1024  # 600 GiB, the handover target

# A realistic mixed-corpus page, repeated with a per-page seed so the corpus
# is deterministic but not one repeated string (a compressor-friendly repeat
# would flatter every measurement).
_PAGE = (
    "InfraSpine quarterly operations report. Revenue recognised under the "
    "contract terms, vendor invoices verified against purchase orders, and "
    "payments released only after the exact-action approval. The continuity "
    "plan names a backup owner for every critical process, and the digital "
    "twin records the dependency graph of people, systems and suppliers. "
)


def synthetic_corpus(*, pages: int = 200, page_bytes: int = 4096) -> list[bytes]:
    """Deterministic synthetic corpus: ``pages`` pages of ~``page_bytes`` each.

    Each page is the base text repeated to length, prefixed with its page
    number and a short hash, so pages differ from each other.
    """
    corpus: list[bytes] = []
    repeats = page_bytes // len(_PAGE) + 1  # enough repeats to cover the page
    for index in range(pages):
        body = (_PAGE * repeats)[:page_bytes]
        seed = hashlib.sha256(f'step-h-page-{index}'.encode()).hexdigest()[:12]
        corpus.append(f'Page {index} [{seed}]\n{body}'.encode('utf-8'))
    return corpus


@dataclass(frozen=True)
class Throughput:
    """A measured throughput over a corpus."""

    bytes_total: int
    seconds: float
    files: int

    @property
    def bytes_per_second(self) -> float:
        return self.bytes_total / self.seconds if self.seconds > 0 else 0.0

    @property
    def mb_per_second(self) -> float:
        return round(self.bytes_per_second / (1024 * 1024), 3)

    def to_dict(self) -> dict:
        return {'bytes_total': self.bytes_total, 'seconds': round(self.seconds, 3),
                'files': self.files, 'mb_per_second': self.mb_per_second}


def measure_chunk_throughput(corpus: list[bytes], *, chunk_size: int = 1000,
                             overlap: int = 100) -> Throughput:
    """Time the real ``split_text`` chunker over ``corpus``.

    Chunking is the cheapest ingestion stage (no parsing, no embedding, no
    index write), so this throughput is a ceiling: the whole pipeline can
    only be slower. The measurement is wall-clock, single-process.
    """
    from app.rag.chunking import split_text

    started = time.perf_counter()
    total_chunks = 0
    total_bytes = 0
    for page in corpus:
        text = page.decode('utf-8', errors='replace')
        total_chunks += len(split_text(text, chunk_size, overlap))
        total_bytes += len(page)
    seconds = time.perf_counter() - started
    return Throughput(bytes_total=total_bytes, seconds=seconds, files=len(corpus))


@dataclass(frozen=True)
class Projection:
    """Time-to-index projection for a target corpus size."""

    target_bytes: int
    measured_mb_per_second: float
    model: str
    seconds: float
    hours: float
    days: float
    assumptions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'target_bytes': self.target_bytes,
                'measured_mb_per_second': self.measured_mb_per_second,
                'model': self.model, 'seconds': round(self.seconds, 1),
                'hours': round(self.hours, 2), 'days': round(self.days, 2),
                'assumptions': list(self.assumptions)}


def project_time_to_index(measured: Throughput, *, target_bytes: int = TARGET_BYTES,
                          model: str = 'linear', scaling_exponent: float = 1.0) -> Projection:
    """Project time-to-index for ``target_bytes`` from a measured throughput.

    ``model='linear'`` assumes throughput stays flat as the corpus grows
    (the optimistic case for a single worker). ``model='sublinear'`` applies
    ``scaling_exponent`` (< 1: throughput degrades as the index grows, e.g.
    0.9 for a 10% slowdown per order of magnitude). Either way the result
    carries its assumptions — and the chunking-only caveat — in the open.
    """
    if measured.bytes_per_second <= 0:
        raise ValueError('measured throughput must be positive')
    if model not in ('linear', 'sublinear'):
        raise ValueError("model must be 'linear' or 'sublinear'")
    if not 0 < scaling_exponent <= 1:
        raise ValueError('scaling_exponent must be in (0, 1]')
    mbps = measured.bytes_per_second / (1024 * 1024)
    seconds = target_bytes / measured.bytes_per_second
    if model == 'sublinear':
        # Throughput at the target scale is the measured rate scaled by the
        # size ratio raised to (exponent - 1): a 0.9 exponent over ~120x
        # growth (5 MB -> 600 GB) costs roughly a third of the rate.
        ratio = target_bytes / max(1, measured.bytes_total)
        seconds = target_bytes / (measured.bytes_per_second * ratio ** (scaling_exponent - 1))
    assumptions = (
        'chunking only: parsing, embedding and index writes are NOT included, '
        'so this is a lower bound on real time-to-index',
        'single worker, wall-clock on this VM (1.5 shared CPUs)',
        f'model={model}' + (f', scaling_exponent={scaling_exponent}' if model == 'sublinear' else ''),
        'throughput measured on a deterministic synthetic TXT corpus',
    )
    return Projection(target_bytes=target_bytes, measured_mb_per_second=round(mbps, 3),
                      model=model, seconds=seconds, hours=seconds / 3600,
                      days=seconds / 86400, assumptions=assumptions)
