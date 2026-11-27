"""Deterministic discovery. Observed intervals do not prove causality or concurrency."""
from collections import Counter, defaultdict
from datetime import datetime
from math import ceil
from statistics import median


def hours(a, b):
    return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600)


def percentile(values, fraction=.95):
    return sorted(values)[max(0, ceil(len(values) * fraction) - 1)] if values else None


def build_trace(events: list[dict]) -> dict:
    rows = sorted(events, key=lambda e: (e["occurred_at"], e["external_id"]))
    ambiguous = any(a["occurred_at"] == b["occurred_at"] for a, b in zip(rows, rows[1:]))
    terminal_positions = [i for i, e in enumerate(rows) if e["terminal"]]
    invalid_terminal = bool(terminal_positions and terminal_positions != [len(rows) - 1])
    completed = bool(rows and rows[-1]["terminal"] and not invalid_terminal)
    first, last = rows[0], rows[-1]
    return {
        "case_id": first["case_id"], "source_id": first["source_id"], "events": rows,
        "sequence": [e["activity"] for e in rows], "ambiguous": ambiguous or invalid_terminal,
        "completed": completed, "cycle_hours": hours(first["occurred_at"], last.get("finished_at") or last["occurred_at"]) if completed else None,
        "start": first["occurred_at"], "end": last.get("finished_at") or last["occurred_at"],
        "departments": sorted({e["department"] for e in rows if e.get("department")}),
        "actors": sorted({e["actor"] for e in rows if e.get("actor")}),
    }


def alignment(actual: list[str], expected: list[str]) -> list[dict]:
    """Minimum edit alignment; substitutions are explicit deviations, not hidden matches."""
    n, m = len(actual), len(expected)
    costs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        costs[i][0] = i
    for j in range(m + 1):
        costs[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            costs[i][j] = min(costs[i-1][j] + 1, costs[i][j-1] + 1, costs[i-1][j-1] + (actual[i-1] != expected[j-1]))
    changes = []
    i, j = n, m
    while i or j:
        if i and j and actual[i-1] == expected[j-1] and costs[i][j] == costs[i-1][j-1]:
            i, j = i-1, j-1
        elif i and costs[i][j] == costs[i-1][j] + 1:
            changes.append({"kind": "extra", "activity": actual[i-1], "index": i-1})
            i -= 1
        elif j and costs[i][j] == costs[i][j-1] + 1:
            changes.append({"kind": "missing", "activity": expected[j-1]})
            j -= 1
        else:
            changes.append({"kind": "substitution", "activity": actual[i-1], "expected": expected[j-1], "index": i-1})
            i, j = i-1, j-1
    return list(reversed(changes))


def discover(traces: list[dict], baseline: dict | None = None, previous: dict | None = None) -> dict:
    nodes, edges, variants = {}, {}, {}
    findings, cases, cycles = [], [], []
    departments, owners = defaultdict(list), defaultdict(list)
    daily = Counter()
    evidence = {}
    completed = [t for t in traces if t["completed"]]
    confidence = round(sum(not t["ambiguous"] for t in traces) / len(traces), 4) if traces else 0

    def finding(kind, title, ids, **details):
        findings.append({"id": f"finding-{len(findings)+1}", "kind": kind, "title": title,
                         "classification": "observed", "evidence_ids": ids, **details})

    for trace in traces:
        ev = trace["events"]
        ids = [e["id"] for e in ev]
        evidence.update({e["id"]: e for e in ev})
        case = {k: v for k, v in trace.items() if k != "events"}
        case["event_ids"] = ids
        case["deviations"] = []
        case["sla_breached"] = None
        if trace["ambiguous"]:
            finding("data_quality", "Ambiguous order or inconsistent terminal event", ids, case_id=trace["case_id"])
        if trace["completed"]:
            cycles.append(trace["cycle_hours"])
            daily[trace["end"][:10]] += 1
            if baseline:
                case["sla_breached"] = trace["cycle_hours"] > baseline["sla_hours"]
                if case["sla_breached"]:
                    finding("sla", "Completed case exceeded baseline SLA", ids, case_id=trace["case_id"], hours=trace["cycle_hours"], limit_hours=baseline["sla_hours"])
                if not trace["ambiguous"]:
                    case["deviations"] = min((alignment(trace["sequence"], seq) for seq in baseline["sequences"]), key=len)
                    if case["deviations"]:
                        finding("conformance", "Observed sequence differs from reviewed baseline", ids, case_id=trace["case_id"], deviations=case["deviations"])
        cases.append(case)
        if not trace["ambiguous"]:
            key = tuple(trace["sequence"])
            v = variants.setdefault(key, {"sequence": list(key), "count": 0, "case_keys": [], "completed": 0})
            v["count"] += 1
            v["completed"] += int(trace["completed"])
            v["case_keys"].append([trace["source_id"], trace["case_id"]])
        seen = Counter()
        for e in ev:
            node = nodes.setdefault(e["activity"], {"activity": e["activity"], "count": 0, "durations": [], "actors": set(), "departments": set(), "evidence_ids": []})
            node["count"] += 1
            node["evidence_ids"].append(e["id"])
            duration = hours(e["occurred_at"], e["finished_at"]) if e.get("finished_at") else None
            if duration is not None:
                node["durations"].append(duration)
            if e.get("actor"):
                node["actors"].add(e["actor"])
                owners[e["actor"]].append((e["id"], duration))
            else:
                finding("ownership", "Event has no recorded owner", [e["id"]], activity=e["activity"])
            if e.get("department"):
                node["departments"].add(e["department"])
                departments[e["department"]].append((e["id"], duration))
            seen[e["activity"]] += 1
            if seen[e["activity"]] > 1:
                finding("rework", "Repeated activity within a case", [e["id"]], case_id=trace["case_id"], activity=e["activity"])
        if not trace["ambiguous"]:
            for a, b in zip(ev, ev[1:]):
                edge = edges.setdefault((a["activity"], b["activity"]), {"from": a["activity"], "to": b["activity"], "count": 0, "intervals": [], "waits": [], "handoffs": 0, "evidence_pairs": []})
                edge["count"] += 1
                edge["intervals"].append(hours(a["occurred_at"], b["occurred_at"]))
                if a.get("finished_at") and a["finished_at"] <= b["occurred_at"]:
                    edge["waits"].append(hours(a["finished_at"], b["occurred_at"]))
                edge["handoffs"] += int(bool(a.get("department") and b.get("department") and a["department"] != b["department"]))
                edge["evidence_pairs"].append([a["id"], b["id"]])

    for node in nodes.values():
        durations = node.pop("durations")
        node.update(actors=sorted(node["actors"]), departments=sorted(node["departments"]), service_hours=sum(durations), measured_durations=len(durations))
        if len(node["actors"]) == 1:
            finding("dependency", "Activity observed with only one owner", node["evidence_ids"], activity=node["activity"], actor=node["actors"][0], qualification="Observed concentration; backup availability is unknown.")
    for edge in edges.values():
        intervals, waits = edge.pop("intervals"), edge.pop("waits")
        edge.update(median_interval_hours=median(intervals), p95_interval_hours=percentile(intervals), median_wait_hours=median(waits) if waits else None, measured_waits=len(waits))
    slow = max(edges.values(), key=lambda e: e["median_interval_hours"], default=None)
    if slow:
        finding("bottleneck", "Largest observed median transition interval", [i for pair in slow["evidence_pairs"] for i in pair], transition=[slow["from"], slow["to"]], hours=slow["median_interval_hours"], qualification="Interval includes service time when completion timestamps are absent; this is not a causal diagnosis.")
    if len(cycles) >= 5:
        center = median(cycles)
        mad = median([abs(x-center) for x in cycles])
        threshold = center + max(3 * 1.4826 * mad, center * .5, 1 / 60)
        for c in cases:
            if c["cycle_hours"] is not None and c["cycle_hours"] > threshold:
                finding("anomaly", "Cycle duration exceeds robust historical threshold", c["event_ids"], case_id=c["case_id"], hours=c["cycle_hours"], threshold_hours=threshold, classification="inference")
    variants_out = sorted(variants.values(), key=lambda v: (-v["count"], v["sequence"]))
    for i, v in enumerate(variants_out):
        v["id"] = f"variant-{i+1}"
    drift = None
    if previous and traces:
        old = {tuple(v["sequence"]): v["count"] for v in previous["variants"]}
        new = {tuple(v["sequence"]): v["count"] for v in variants_out}
        old_n, new_n = sum(old.values()), sum(new.values())
        if old_n and new_n:
            distance = sum(abs(old.get(k, 0)/old_n - new.get(k, 0)/new_n) for k in old.keys() | new.keys()) / 2
            drift = {"classification": "inference", "total_variation": round(distance, 4), "threshold": .2,
                     "detected": distance >= .2, "new_sequences": [list(k) for k in new.keys() - old.keys()],
                     "qualification": "Cumulative observed variant distributions, not proof of a policy change."}
            if drift["detected"]:
                finding("drift", "Observed variant distribution changed", list(evidence), classification="inference", total_variation=drift["total_variation"])
    measured = [c for c in cases if c["completed"] and not c["ambiguous"]]
    deviations = sum(bool(c["deviations"]) for c in measured)
    def aggregates(groups):
        return [{"name": name, "events": len(rows), "measured_service_hours": round(sum(d for _, d in rows if d is not None), 4), "measured_durations": sum(d is not None for _, d in rows), "evidence_ids": [i for i, _ in rows]} for name, rows in sorted(groups.items())]
    recommendations = []
    for f in findings:
        if f["kind"] in ("bottleneck", "dependency", "conformance"):
            recommendations.append({"id": f"proposal-{len(recommendations)+1}", "classification": "inference", "status": "proposed",
                                    "title": {"bottleneck": "Review the slowest handoff and validate its cause", "dependency": "Validate backup ownership for this activity", "conformance": "Review the deviation against the current SOP"}[f["kind"]],
                                    "finding_id": f["id"], "evidence_ids": f["evidence_ids"], "requires_human_review": True})
    return {"classification": "observed", "summary": {"events": len(evidence), "cases": len(cases), "completed_cases": len(completed), "open_cases": len(cases)-len(completed),
            "variants": len(variants), "median_cycle_hours": median(cycles) if cycles else None, "p95_cycle_hours": percentile(cycles),
            "sla_breaches": sum(c["sla_breached"] is True for c in cases) if baseline else None,
            "conformance_rate": round((len(measured)-deviations)/len(measured), 4) if baseline and measured else None,
            "conformance_cases": len(measured) if baseline else 0, "handoffs": sum(e["handoffs"] for e in edges.values()),
            "order_confidence": confidence, "ambiguous_cases": sum(t["ambiguous"] for t in traces)},
            "nodes": list(nodes.values()), "edges": list(edges.values()), "variants": variants_out, "cases": cases,
            "findings": findings, "recommendations": recommendations, "departments": aggregates(departments), "owners": aggregates(owners),
            "daily_throughput": [{"date": d, "completed": n} for d, n in sorted(daily.items())], "drift": drift,
            "baseline": baseline, "evidence": evidence,
            "limitations": ["Case IDs are namespaced by source; cross-source identity needs validated mapping.", "Activity order is observational, not a dependency or concurrency proof.", "Resource capacity and working calendars are unknown; utilization percentages are not estimated.", "Open and ambiguously ordered cases are excluded from completed conformance scoring."]}


def simulate(model: dict, actor: str, absence_hours: float, replacement_capacity: float) -> dict:
    delay = absence_hours * (1-replacement_capacity)
    affected = [c for c in model["cases"] if actor in c["actors"]]
    baseline = model.get("baseline")
    completed = [c for c in affected if c["cycle_hours"] is not None]
    return {"classification": "hypothetical", "actor": actor, "affected_cases": len(affected), "additional_hours_per_affected_case": delay,
            "estimated_additional_case_hours": delay * len(affected),
            "additional_sla_breaches": sum(c["cycle_hours"] <= baseline["sla_hours"] < c["cycle_hours"]+delay for c in completed) if baseline else None,
            "evidence_ids": [i for c in affected for i in c["event_ids"]],
            "assumptions": ["Replay the observed case mix with a single additive delay per affected case.", "The absence overlaps every affected case; no queue, parallelism or working-calendar model is assumed.", f"Replacement capacity is supplied by the user: {replacement_capacity:.0%}.", "No workflow, authority or policy is changed."]}
