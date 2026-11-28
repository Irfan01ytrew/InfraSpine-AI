"""Aggregate twin health score (Step K v2 slice 3).

One 0–100 number answering "how healthy is the twin?" — computed
from the latest drift report plus per-source freshness. Pure
functions over plain data (stdlib only, except for the drift module
itself): no I/O, no database, no network, no secrets.

Score definition (documented, deterministic):

- Start at 100.
- Subtract the latest ``drift_score`` (0–100, slice 1 weights).
  The drift score already includes money movement (up to 20 points),
  so the reported components split it back out: ``money`` is the
  money part recomputed from the report's per-currency totals and
  ``drift`` is the remainder (structural change). The two always sum
  to the drift score (when the score is below its 100 cap).
- Subtract a freshness penalty: 10 points per stale source, capped
  at 30. A source is stale when its freshness entry says so
  (``stale: true`` or ``status: "stale"``) or when the drift report
  lists it in ``stale_sources``.
- The first recorded version has no drift report yet, so its health
  is freshness only (100 with no stale sources).

Bands (aligned with the drift trust bands, plan §6 — confirm or
change with the owner):

- ``healthy``: score >= 75 (drift <= 25 and fresh — the trustworthy
  band).
- ``degraded``: score >= 40 (drift <= 60 — the review band).
- ``critical``: score < 40 (drift > 60 — the stale band, or enough
  stale sources to drag a healthy twin down).

Money figures here are exposure ("money at risk"), never payment
instructions, and the health assessment carries no amounts at all —
only score points. Snapshot text is untrusted data: names and
attributes are compared as data only, never evaluated or interpreted
as instructions.
"""

from __future__ import annotations

from decimal import Decimal

from app.digital_twin import drift

#: Health bands (see module docstring).
HEALTHY_MIN_SCORE = 75
DEGRADED_MIN_SCORE = 40

#: Freshness penalty: points per stale source, and its cap.
STALE_SOURCE_POINTS = 10
STALE_SOURCE_CAP = 30

#: Money weight inside the drift score (same weight as
#: ``drift._MONEY_POINTS`` — kept in sync by the money-decomposition
#: test, not by import, so this module stays stdlib-only).
MONEY_POINTS = 20

BANDS = ("healthy", "degraded", "critical")

__all__ = [
    "BANDS",
    "DEGRADED_MIN_SCORE",
    "HEALTHY_MIN_SCORE",
    "MONEY_POINTS",
    "STALE_SOURCE_CAP",
    "STALE_SOURCE_POINTS",
    "compute_health",
    "health_summary",
    "prediction_is_stale",
]


def _money_penalty(drift_report):
    """Money part of a drift score, recomputed from the report totals.

    Largest per-currency |delta| share of the larger total, times
    :data:`MONEY_POINTS` — the same formula slice 1 uses inside the
    drift score, so ``drift_component + money == drift_score`` (below
    the score's 100 cap). Returns 0 when the report has no money.
    """
    money = (drift_report or {}).get("money") or {}
    old = (money.get("old") or {}).get("totals") or {}
    new = (money.get("new") or {}).get("totals") or {}
    largest_share = 0.0
    for currency in sorted(set(old) | set(new)):
        old_total = Decimal(str(old.get(currency, 0.0)))
        new_total = Decimal(str(new.get(currency, 0.0)))
        base = max(old_total, new_total)
        if base > 0:
            share = float(abs(new_total - old_total) / base)
            largest_share = max(largest_share, share)
    return int(MONEY_POINTS * largest_share)


def _stale_source_names(drift_report, source_freshness):
    """Stale source names from the drift report + freshness list.

    A freshness entry is stale when ``stale`` is true or ``status``
    is ``"stale"``. Names from the drift report's ``stale_sources``
    count too (they are the snapshot's own stale list). Deduped and
    sorted, so the result is deterministic.
    """
    stale = set()
    if source_freshness is not None:
        if not isinstance(source_freshness, list):
            raise ValueError("source_freshness must be a list of source dicts.")
        for item in source_freshness:
            if not isinstance(item, dict):
                raise ValueError("source_freshness entries must be dicts.")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("source_freshness entries need a non-empty string 'name'.")
            if item.get("stale") is True or item.get("status") == "stale":
                stale.add(name)
    if drift_report is not None:
        if not isinstance(drift_report, dict):
            raise ValueError("drift_report must be a dict.")
        for name in drift_report.get("stale_sources") or []:
            if isinstance(name, str) and name:
                stale.add(name)
    return sorted(stale)


def _validate_drift_report(drift_report):
    if drift_report is None:
        return None
    if not isinstance(drift_report, dict):
        raise ValueError("drift_report must be a dict.")
    score = drift_report.get("drift_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("drift_report 'drift_score' must be an int 0–100.")
    trust = drift_report.get("trust")
    if trust not in drift._TRUST_VALUES:
        raise ValueError(f"drift_report 'trust' must be one of {drift._TRUST_VALUES}.")
    return drift_report


def compute_health(*, drift_report=None, source_freshness=None,
                   fingerprint=None, as_of=None):
    """Aggregate 0–100 health for one twin version.

    ``drift_report`` is the slice-1 report for the version (``None``
    for the first recorded version — health is freshness only then).
    ``source_freshness`` is the version's per-source freshness list.
    ``fingerprint`` / ``as_of`` are carried through for the watch
    and the history endpoint. Returns a plain dict — no amounts, only
    score points, so it is safe to show to any tenant member.
    """
    report = _validate_drift_report(drift_report)
    if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
        raise ValueError("fingerprint must be a non-empty string.")
    stale_sources = _stale_source_names(report, source_freshness)
    freshness_penalty = min(STALE_SOURCE_CAP, len(stale_sources) * STALE_SOURCE_POINTS)
    if report is None:
        drift_score = None
        trust = None
        money_penalty = 0
        drift_component = 0
    else:
        drift_score = report["drift_score"]
        trust = report["trust"]
        money_penalty = _money_penalty(report)
        drift_component = max(0, drift_score - money_penalty)
    health_score = 100 - min(100, (drift_score or 0) + freshness_penalty)
    if health_score >= HEALTHY_MIN_SCORE:
        band = "healthy"
    elif health_score >= DEGRADED_MIN_SCORE:
        band = "degraded"
    else:
        band = "critical"
    return {
        "fingerprint": fingerprint,
        "as_of": as_of,
        "health_score": health_score,
        "band": band,
        "trust": trust,
        "drift_score": drift_score,
        "is_first_version": report is None,
        "components": {
            "drift": drift_component,
            "money": money_penalty,
            "freshness": freshness_penalty,
        },
        "stale_sources": stale_sources,
        "stale_source_count": len(stale_sources),
    }


def health_summary(assessment):
    """One deterministic summary line for a health assessment.

    Score points and counts only — no attribute values, no names
    beyond a stale-source count, no secrets.
    """
    if assessment["is_first_version"]:
        drift_part = "no drift recorded yet"
    else:
        components = assessment["components"]
        drift_part = (
            f"drift {assessment['drift_score']} "
            f"(structural {components['drift']}, money {components['money']})"
        )
    return (
        f"Twin health {assessment['health_score']}/100 ({assessment['band']}): "
        f"{drift_part}, {assessment['stale_source_count']} stale source(s)."
    )


def prediction_is_stale(prediction, latest_assessment, *,
                        threshold=drift.REVIEW_MAX_SCORE):
    """Is a prediction stale against the latest twin health?

    A prediction records the twin fingerprint it was computed from
    (plan §1 item 2). It is stale when its fingerprint differs from
    the latest version's AND the latest drift score exceeds
    ``threshold`` (default 60 — the stale trust band). Fail-closed:
    a prediction with no fingerprint, or one that does not match the
    only recorded version (drift unknown), is stale — it cannot be
    verified against the twin. A matching fingerprint is never stale.

    ``prediction`` is a plain dict with a ``fingerprint`` (or
    ``twin_fingerprint``) key. ``latest_assessment`` is a
    :func:`compute_health` result, or ``None`` when no twin version
    is recorded. Returns ``{"stale": bool, "reason": str}``.
    """
    if latest_assessment is None:
        return {"stale": False, "reason": "no twin version recorded; nothing to compare against"}
    if not isinstance(prediction, dict):
        raise ValueError("prediction must be a dict.")
    fingerprint = prediction.get("twin_fingerprint") or prediction.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return {
            "stale": True,
            "reason": "prediction carries no twin fingerprint; it cannot be checked against the twin",
        }
    latest_fingerprint = latest_assessment.get("fingerprint")
    if not isinstance(latest_fingerprint, str) or not latest_fingerprint:
        return {
            "stale": True,
            "reason": "latest twin version has no fingerprint; the prediction cannot be verified",
        }
    if fingerprint == latest_fingerprint:
        return {"stale": False, "reason": "prediction matches the latest twin version"}
    drift_score = latest_assessment.get("drift_score")
    if drift_score is None:
        return {
            "stale": True,
            "reason": "prediction fingerprint does not match the only recorded twin version; drift is unknown",
        }
    if drift_score > threshold:
        return {
            "stale": True,
            "reason": f"twin drift {drift_score} exceeds the staleness threshold {threshold}",
        }
    return {
        "stale": False,
        "reason": f"twin drift {drift_score} is within the staleness threshold {threshold}",
    }
