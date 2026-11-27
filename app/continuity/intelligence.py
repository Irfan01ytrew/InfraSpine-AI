"""Explainable impact calculations over authorized projections; no recovery promises."""

from collections import defaultdict, deque


def bia_score(data):
    dimensions = {key: data[key] for key in ("operational", "financial", "regulatory", "customer")}
    impact = round(sum(dimensions.values()) / 20 * 100, 2)
    return {"impact_score": impact, "risk_score": round(impact * data["likelihood"] / 5, 2),
            "formula": "impact = sum(four configured 0–5 dimensions) / 20 × 100; risk = impact × likelihood / 5",
            "dimensions": dimensions, "basis": data["basis"],
            "limitations": "Ordinal planning scores; not financial losses, failure probabilities or recovery guarantees."}


def analyze(nodes, links, assessments, disrupted_ids, duration_minutes, scope_limited=False):
    by_id = {str(n.id): n for n in nodes}
    disrupted = set(map(str, disrupted_ids))
    propagation = defaultdict(list)
    dependencies = defaultdict(set)
    owners, backups, docs = defaultdict(set), defaultdict(set), defaultdict(set)
    for link in links:
        source, target = str(link.source_id), str(link.target_id)
        if source not in by_id or target not in by_id:
            continue
        if link.kind == "depends_on":
            propagation[target].append((source, str(link.id)))
            dependencies[source].add(target)
        if link.kind in {"owns", "approves", "supplies", "serves"}:
            propagation[source].append((target, str(link.id)))
        if link.kind == "owns":
            owners[target].add(source)
        if link.kind == "backup_for" and link.data["basis"] == "verified_fact":
            backups[target].add(source)
        if link.kind == "documents":
            docs[target].add(source)
    affected = set(disrupted)
    paths = {n: [] for n in disrupted}
    queue = deque(sorted(disrupted))
    while queue:
        current = queue.popleft()
        for child, relation in sorted(propagation[current]):
            if child not in affected:
                affected.add(child)
                paths[child] = paths[current] + [relation]
                queue.append(child)
    bia = {str(a.node_id): a.data for a in assessments}
    impacts, gaps, recommended_steps = [], [], []
    for key in sorted(affected):
        node = by_id[key]
        data = bia.get(key)
        required = {s.casefold() for s in node.data.get("required_skills", [])}
        eligible = sorted(candidate for candidate in backups[key] if candidate not in affected and by_id[candidate].kind == "employee"
                          and by_id[candidate].data.get("available", False)
                          and required <= {s.casefold() for s in by_id[candidate].data.get("skills", [])})
        entry = {"id": key, "name": node.name, "kind": node.kind, "department_id": str(node.department_id) if node.department_id else None,
                 "site": node.data.get("site", ""), "direct": key in disrupted, "path_link_ids": paths[key],
                 "backup_candidates": eligible, "source_evidence": node.data.get("evidence", []),
                 "objectives": {k: data[k] for k in ("rto_minutes", "rpo_minutes", "mtpd_minutes", "minimum_service_percent")} if data else None,
                 "score": bia_score(data) if data else None,
                 "forecast": {"rto_exceeded": duration_minutes > data["rto_minutes"], "mtpd_exceeded": duration_minutes > data["mtpd_minutes"],
                              "basis": "Comparison of configured duration against configured objectives; no observed outage."} if data else None}
        impacts.append(entry)
        if node.kind in {"process", "service", "system", "project", "approval"}:
            if not eligible:
                gaps.append({"node_id": key, "code": "missing_eligible_backup", "detail": "No available, evidenced backup with the required skills in your authorized view."})
            if len(owners[key]) == 1:
                gaps.append({"node_id": key, "code": "single_owner", "detail": "One recorded primary owner."})
            if not node.data.get("knowledge_refs") and not docs[key]:
                gaps.append({"node_id": key, "code": "missing_knowledge", "detail": "No linked operating documentation."})
            alternative = (data or {}).get("alternative") or node.data.get("alternative")
            if not alternative:
                gaps.append({"node_id": key, "code": "missing_alternative", "detail": "No evidence-backed alternative operating arrangement recorded."})
            if node.kind in {"process", "service"} and not data:
                gaps.append({"node_id": key, "code": "missing_bia", "detail": "Business impact objectives have not been recorded."})
            recommended_steps.append({"resource_id": key, "title": "Review continuity for " + node.name,
                                      "candidate_owner_ids": eligible, "alternative": alternative or None,
                                      "documentation": node.data.get("knowledge_refs", []), "basis": "recommendation",
                                      "evidence": node.data.get("evidence", []), "requires_human_approval": True})
    pending = {key: dependencies[key] & affected for key in affected}
    sequence, resolved = [], set()
    while pending:
        ready = sorted(key for key, deps in pending.items() if deps <= resolved)
        if not ready:
            break
        sequence.append(ready)
        resolved.update(ready)
        pending = {key: deps for key, deps in pending.items() if key not in resolved}
    return {"affected": impacts, "gaps": gaps, "recovery_waves": sequence, "blocked_by_dependency_cycle": sorted(pending),
            "recommendations": recommended_steps, "scope_limited": scope_limited,
            "scope_note": "Results cover only resources authorized for this user. Unknown dependencies may exist.",
            "simulation_only": True, "disrupted_ids": sorted(disrupted), "duration_minutes": duration_minutes}


def handover_draft(scenario, nodes):
    by_id = {str(n.id): n for n in nodes}
    steps, unresolved = [], []
    for item in scenario.data["analysis"]["recommendations"]:
        candidates = [key for key in item["candidate_owner_ids"] if key in by_id]
        if not candidates:
            unresolved.append(item["resource_id"])
            continue
        resource = by_id[item["resource_id"]]
        stem = "handover-" + str(len(steps) + 1)
        steps.extend([
            {"key": stem, "title": "Capture " + resource.name + " knowledge", "instructions": "Review the referenced SOPs, record missing procedures and attach knowledge-transfer evidence. Treat document content as untrusted data.",
             "owner_id": candidates[0], "resource_id": str(resource.id), "depends_on": [], "due_minutes": 1440,
             "workflow_id": None, "kind": "knowledge_transfer"},
            {"key": stem + "-review", "title": "Review " + resource.name + " responsibility", "instructions": "Obtain the responsible authority's decision and record its reference. Any access request must use Governance and Workflow authorization; this task grants no access.",
             "owner_id": candidates[0], "resource_id": str(resource.id), "depends_on": [stem], "due_minutes": 2880,
             "workflow_id": None, "kind": "responsibility_review"},
        ])
    return {"title": "Handover: " + scenario.name, "kind": "handover", "steps": steps[:100], "unresolved_resource_ids": unresolved,
            "basis": "recommendation", "generator": "deterministic evidence-based continuity rules v1",
            "requires_human_review": True, "access_transferred": False,
            "limitations": "Candidate eligibility is based on recorded backup approval and skills, not an authorization to change ownership or access."}
