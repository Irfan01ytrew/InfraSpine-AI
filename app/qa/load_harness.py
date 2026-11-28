"""Transport-agnostic async load harness (Step H preparation).

The harness drives N virtual users against any injected async transport
callable, so the same scenario can run against a fake transport in unit tests,
a local ASGI app, or a staging API during the 1,000+ user load test. It makes
no network calls itself and knows nothing about providers or credentials.

This is deliberately separate from ``scripts/load_test_review_stack.py``, which
is tied to one live review stack (fixed API URL, login burst + read waves only,
LLM chat excluded). The harness is the reusable core the Step H load test and
the 600 GB ingestion qualification will build on.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass(frozen=True)
class LoadStep:
    """One request kind inside a scenario's mix."""

    name: str
    weight: float = 1.0


@dataclass(frozen=True)
class LoadScenario:
    """What a load pass does. Weights are relative frequencies; they must be > 0."""

    name: str
    virtual_users: int
    iterations_per_user: int = 1
    steps: tuple[LoadStep, ...] = (LoadStep("read"),)

    def __post_init__(self) -> None:
        if self.virtual_users < 1:
            raise ValueError("virtual_users must be at least 1")
        if self.iterations_per_user < 1:
            raise ValueError("iterations_per_user must be at least 1")
        if not self.steps:
            raise ValueError("at least one step is required")
        if any(step.weight <= 0 for step in self.steps):
            raise ValueError("step weights must be positive")


@dataclass
class LoadReport:
    """Result of one load pass. Latencies are milliseconds."""

    scenario: str
    requests: int
    errors: int
    seconds: float
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return self.requests - self.errors

    @property
    def error_rate(self) -> float | None:
        if self.requests == 0:
            return None
        return round(self.errors / self.requests, 4)

    @property
    def throughput_rps(self) -> float | None:
        if self.seconds <= 0:
            return None
        return round(self.requests / self.seconds, 1)

    def _percentile(self, q: float) -> float | None:
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)

    @property
    def p50_ms(self) -> float | None:
        return self._percentile(0.50)

    @property
    def p95_ms(self) -> float | None:
        return self._percentile(0.95)

    @property
    def p99_ms(self) -> float | None:
        return self._percentile(0.99)

    @property
    def max_ms(self) -> float | None:
        return round(max(self.latencies_ms), 2) if self.latencies_ms else None

    @property
    def mean_ms(self) -> float | None:
        return round(statistics.fmean(self.latencies_ms), 2) if self.latencies_ms else None

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "requests": self.requests,
            "errors": self.errors,
            "successes": self.successes,
            "error_rate": self.error_rate,
            "seconds": round(self.seconds, 2),
            "throughput_rps": self.throughput_rps,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
        }


# Transport: called as await transport(step, user_index). It raises on failure.
Transport = Callable[[LoadStep, int], Awaitable[None]]


def _weighted_round_robin(steps: tuple[LoadStep, ...], n: int) -> list[LoadStep]:
    """Deterministic step mix: expand weights into a repeating sequence.

    Deterministic on purpose — a load test must be reproducible, and unit tests
    must be able to assert the mix without flaky random sampling.
    """
    expanded: list[LoadStep] = []
    for step in steps:
        expanded.extend([step] * max(1, round(step.weight)))
    return [expanded[i % len(expanded)] for i in range(n)]


async def run_load_pass(
    scenario: LoadScenario,
    transport: Transport,
) -> LoadReport:
    """Run one load pass: ``virtual_users`` concurrent users, each performing
    ``iterations_per_user`` steps from the scenario mix. Every transport raise
    counts as one error; latency is measured per request including failures.
    """
    latencies: list[float] = []
    errors = 0
    lock = asyncio.Lock()

    async def one_user(user_index: int) -> None:
        nonlocal errors
        plan = _weighted_round_robin(scenario.steps, scenario.iterations_per_user)
        for step in plan:
            start = time.perf_counter()
            try:
                await transport(step, user_index)
            except Exception:
                async with lock:
                    errors += 1
            finally:
                async with lock:
                    latencies.append((time.perf_counter() - start) * 1000.0)

    started = time.perf_counter()
    await asyncio.gather(*(one_user(i) for i in range(scenario.virtual_users)))
    seconds = time.perf_counter() - started
    return LoadReport(
        scenario=scenario.name,
        requests=len(latencies),
        errors=errors,
        seconds=seconds,
        latencies_ms=latencies,
    )
