"""Multi-source trust scoring (Step K v2 slice 7).

Pure functions over the source-freshness list a twin version stores
(``app.digital_twin.versions``) plus the latest drift report
(``app.digital_twin.drift``). Stdlib only: no I/O, no database, no
network, no secrets.

Trust model (documented, deterministic):

- Every twin source has a weight (points of 100) reflecting how
  authoritative it is for the twin. The company graph is the
  authoritative source (Step S: the graph wins on conflicts),
  finance is money-critical, the rest are supporting feeds.
- A source's trust is freshness-based: fresh data scores 100, stale
  data scores 0 — a stale source's last-known values are still
  shown, but nothing relies on them. A source with no freshness
  entry is "unknown": it contributes nothing to the score and drops
  coverage instead of silently counting as fresh or stale.
- The aggregate is the weight-normalised mean over sources with a
  known status. ``coverage`` is the share of total weight that is
  known. With no known source the score is ``None`` and the band is
  ``"unknown"`` — never a fabricated number.
- The reported band is the worse of the freshness band and the drift
  trust verdict from the latest drift report: a twin can be fresh
  everywhere and still untrustworthy if it drifted far.

Trust bands (aligned with ``app.digital_twin.health``):

- ``"trustworthy"`` — score >= 75
- ``"review"`` — score >= 40
- ``"stale"`` — score < 40
- ``"unknown"`` — no known source and no drift verdict
"""

from __future__ import annotations

import math

# Weights sum to 100. A source the twin has never heard of keeps
# weight 0: it is listed, never dropped, and never moves the score.
TRUST_SOURCES: dict[str, int] = {
    "company_graph": 30,
    "finance": 20,
    "continuity": 15,
    "membership": 10,
    "workflow": 10,
    "agent_studio": 10,
    "connectors": 5,
}

TRUST_BANDS = ("trustworthy", "review", "stale", "unknown")

_BAND_ORDER = {"trustworthy": 0, "review": 1, "stale": 2}


def trust_band(score: int | None) -> str:
    """Band for a numeric trust score, or ``"unknown"`` without one."""
    if score is None:
        return "unknown"
    if not isinstance(score, int) or isinstance(score, bool) or score < 0 or score > 100:
        raise ValueError(f"trust score must be an int 0-100, got {score!r}")
    if score >= 75:
        return "trustworthy"
    if score >= 40:
        return "review"
    return "stale"


def _freshness_status(entry: dict) -> str:
    """One freshness entry's status: ``"fresh"``, ``"stale"`` or ``"unknown"``."""
    if not isinstance(entry, dict):
        raise ValueError(f"source freshness entries must be dicts, got {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"source freshness entries need a non-empty 'name', got {entry!r}")
    status = entry.get("status")
    if entry.get("stale") is True or status == "stale":
        return "stale"
    if status in (None, "fresh", "unknown"):
        return "fresh" if status != "unknown" else "unknown"
    raise ValueError(f"unknown source status {status!r} for source {name!r}")


def compute_trust(source_freshness: list, drift_report: dict | None = None) -> dict:
    """Score multi-source trust for one twin snapshot.

    ``source_freshness`` is the list a version stores (see
    ``app.digital_twin.versions.record_version``): entries with
    ``name`` and ``stale``/``status``. ``drift_report`` is the latest
    drift report (``app.digital_twin.drift``), or None when the twin
    has never been drifted.

    Returns a dict with ``score`` (int 0-100 or None), ``band``,
    ``coverage`` (0-1 share of total weight with a known status),
    ``sources`` (per-source status, score and weight, heaviest
    first), ``stale_sources`` (sorted names), ``drift_score`` and
    ``drift_trust`` (from the drift report when given).
    """
    if not isinstance(source_freshness, list):
        raise ValueError(f"source_freshness must be a list, got {type(source_freshness).__name__}")
    drift_score = None
    drift_trust = None
    if drift_report is not None:
        if not isinstance(drift_report, dict):
            raise ValueError(f"drift_report must be a dict, got {type(drift_report).__name__}")
        drift_score = drift_report.get("drift_score")
        if drift_score is not None:
            if not isinstance(drift_score, int) or isinstance(drift_score, bool) or drift_score < 0 or drift_score > 100:
                raise ValueError(f"drift_score must be an int 0-100, got {drift_score!r}")
        drift_trust = drift_report.get("trust")
        if drift_trust is not None and drift_trust not in ("trustworthy", "review", "stale"):
            raise ValueError(f"drift trust verdict must be trustworthy/review/stale, got {drift_trust!r}")

    # Last entry wins for a repeated source name (the freshest write).
    by_name: dict[str, str] = {}
    for entry in source_freshness:
        by_name[entry.get("name") if isinstance(entry, dict) else None] = _freshness_status(entry)
    known_names = set(by_name)

    sources = []
    covered_weight = 0
    weighted = 0
    for name in sorted(set(TRUST_SOURCES) | known_names, key=lambda n: (-TRUST_SOURCES.get(n, 0), n)):
        weight = TRUST_SOURCES.get(name, 0)
        status = by_name.get(name, "unknown")
        entry = {"source": name, "status": status, "weight": weight, "known_source": name in TRUST_SOURCES}
        if status == "unknown":
            entry["score"] = None
        else:
            entry["score"] = 0 if status == "stale" else 100
            covered_weight += weight
            weighted += weight * entry["score"]
        sources.append(entry)

    total_weight = sum(TRUST_SOURCES.values())
    coverage = covered_weight / total_weight if total_weight else 0.0
    score = int(round(weighted / covered_weight)) if covered_weight else None

    candidates = []
    if score is not None:
        candidates.append(trust_band(score))
    if drift_trust is not None:
        candidates.append(drift_trust)
    band = "unknown"
    if candidates:
        band = max(candidates, key=lambda b: _BAND_ORDER[b])
    return {
        "score": score,
        "band": band,
        "coverage": round(coverage, 4),
        "sources": sources,
        "stale_sources": sorted(n for n, s in by_name.items() if s == "stale"),
        "drift_score": drift_score,
        "drift_trust": drift_trust,
    }


def trust_summary(report: dict) -> str:
    """One-line, count-only summary of a ``compute_trust`` report."""
    score = report.get("score")
    score_text = "unknown" if score is None else str(score)
    stale = report.get("stale_sources") or []
    line = f"trust {score_text} ({report.get('band')}), coverage {report.get('coverage')}"
    if stale:
        line += f", stale: {', '.join(stale)}"
    return line
