"""Explainable, bounded graph algorithms over an already authorized projection."""

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
import re

OWNERSHIP = {"owns", "responsible_for", "operates"}
FORWARD_IMPACT = OWNERSHIP | {"supplies"}
REVERSE_IMPACT = {"depends_on"}
OWNABLE = {"system", "asset", "process", "workflow", "project", "responsibility"}


def impact(graph: dict, failed_ids: list[str], depth: int = 6, scenario: str = "system_outage"):
    nodes = {n["id"]: n for n in graph["entities"]}
    roots = set(failed_ids)
    adjacency, backup_edges = defaultdict(list), defaultdict(list)
    for edge in graph["relationships"]:
        if edge["kind"] in FORWARD_IMPACT:
            adjacency[edge["source_id"]].append((edge["target_id"], edge))
        elif edge["kind"] in REVERSE_IMPACT:
            adjacency[edge["target_id"]].append((edge["source_id"], edge))
        elif edge["kind"] == "backup_for":
            backup_edges[edge["target_id"]].append(edge)
    paths, confidences = {x: [] for x in roots}, {x: 1.0 for x in roots}
    queue = deque(sorted(roots))
    depth_limited = False
    while queue:
        current = queue.popleft()
        for target, edge in sorted(adjacency[current], key=lambda v: v[1]["id"]):
            if target in paths or target not in nodes:
                continue
            if len(paths[current]) >= depth:
                depth_limited = True
                continue
            paths[target] = [*paths[current], edge]
            confidences[target] = min(confidences[current], edge["confidence"])
            queue.append(target)
    affected = []
    for target in sorted(paths.keys() - roots, key=lambda x: (len(paths[x]), nodes[x]["name"])):
        path = paths[target]
        backups = [{"entity": nodes[e["source_id"]], "relationship_id": e["id"], "evidence": e["evidence"], "confidence": e["confidence"]}
                   for e in backup_edges[target] if e["source_id"] in nodes and e["source_id"] not in paths]
        affected.append({"entity": nodes[target], "distance": len(path), "confidence": confidences[target],
                         "path": [{"relationship_id": e["id"], "kind": e["kind"], "source_id": e["source_id"], "target_id": e["target_id"], "evidence": e["evidence"]} for e in path],
                         "authorized_backups": backups})
    departments = sorted({n["entity"]["department_id"] for n in affected if n["entity"].get("department_id")})
    return {"scenario": scenario, "failed_ids": failed_ids, "affected": affected, "department_ids": departments,
            "truncated": graph.get("truncated", False) or depth_limited,
            "assumptions": ["Only approved, currently valid, authorized records participate.",
                            "Dependencies propagate potential impact, not a prediction of certain failure.",
                            "Backup suggestions are approved graph assignments; no access transfer is executed.",
                            "Path confidence is the lowest evidence confidence on one shortest explanatory path."],
            "as_of": graph.get("as_of")}


def quality(graph: dict, *, stale_days=90, moment=None):
    now = moment or datetime.now(UTC)
    nodes = {n["id"]: n for n in graph["entities"]}
    owners, backups, signatures = defaultdict(list), defaultdict(list), defaultdict(list)
    for edge in graph["relationships"]:
        if edge["kind"] in OWNERSHIP:
            owners[edge["target_id"]].append(edge["source_id"])
        if edge["kind"] == "backup_for":
            backups[edge["target_id"]].append(edge["source_id"])
        signatures[(edge["source_id"], edge["target_id"], edge["kind"])].append(edge["id"])
    ownable = [n for n in nodes.values() if n["kind"] in OWNABLE]
    gaps = [n for n in ownable if not owners[n["id"]]]
    missing = [n for n in ownable if owners[n["id"]] and not (set(backups[n["id"]]) - set(owners[n["id"]]))]
    stale = [r for r in [*nodes.values(), *graph["relationships"]]
             if datetime.fromisoformat(r["observed_at"]) < now - timedelta(days=stale_days)]
    conflicts = [{"kind": "duplicate_relationship", "record_ids": ids} for ids in signatures.values() if len(ids) > 1]
    conflicts += [{"kind": "multiple_primary_owners", "entity_id": n["id"], "owner_ids": sorted(set(owners[n["id"]]))}
                  for n in ownable if len(set(owners[n["id"]])) > 1 and n.get("attributes", {}).get("single_owner", True)]
    # Name matches are suggestions, never an automatic merge of distinct employees.
    names = defaultdict(list)
    for n in nodes.values():
        names[(n["kind"], n["name"].strip().casefold())].append(n["id"])
    duplicates = [ids for ids in names.values() if len(ids) > 1]
    critical = []
    for n in nodes.values():
        report = impact(graph, [n["id"]])
        unsupported = sum(not entry["authorized_backups"] for entry in report["affected"])
        if report["affected"]:
            critical.append({"entity": n, "affected_count": len(report["affected"]), "without_backup": unsupported})
    critical.sort(key=lambda x: (-x["without_backup"], -x["affected_count"], x["entity"]["name"]))
    return {"ownership_gaps": gaps, "missing_backups": missing, "stale_records": stale, "conflicts": conflicts,
            "resolution_candidates": duplicates, "critical_entities": critical[:50], "stale_after_days": stale_days,
            "entity_count": len(nodes), "relationship_count": len(graph["relationships"]), "truncated": graph.get("truncated", False),
            "scope": "Findings describe the visible projection only. Absence of visible ownership is not proof of absence."}


def query_plan(question: str, entities: list[dict]):
    text = question.casefold().strip()
    if re.search(r"\b(risks?|gaps?|stale|conflicts?)\b", text):
        return {"intent": "quality", "matches": []}
    intent = "impact" if re.search(r"\b(impact|resign\w*|absen\w*|outage|fail\w*|leaves?|unavailable|restructur\w*)\b", text) else (
        "backups" if re.search(r"\b(backups?|delegat\w*|handover)\b", text) else (
        "owners" if re.search(r"\b(owns?|owners?|responsib\w*)\b", text) else "relationships"))
    matches = [n for n in entities if n["name"].casefold() in text]
    if matches:
        longest = max(len(n["name"]) for n in matches)
        matches = [n for n in matches if len(n["name"]) == longest]
    if not matches:
        tokens = set(re.findall(r"\w+", text)) - {"the", "a", "an", "of", "who", "what", "is", "for", "show", "me", "system", "employee"}
        matches = [n for n in entities if tokens.intersection(re.findall(r"\w+", n["name"].casefold()))]
    return {"intent": intent, "matches": matches[:20]}


def answer_query(graph: dict, question: str):
    plan = query_plan(question, graph["entities"])
    base = {"question": question, "intent": plan["intent"], "scope": graph.get("scope"), "truncated": graph.get("truncated", False)}
    if plan["intent"] == "quality":
        return {**base, "answer": "Data quality findings in your authorized graph.", "result": quality(graph)}
    if len(plan["matches"]) != 1:
        return {**base, "answer": "Choose one matching entity." if plan["matches"] else "No matching authorized entity found. Use an exact entity name.", "choices": plan["matches"]}
    node = plan["matches"][0]
    if plan["intent"] == "impact":
        result = impact(graph, [node["id"]])
        return {**base, "answer": f"{len(result['affected'])} visible entities may be affected by loss of {node['name']}.", "result": result}
    kinds = OWNERSHIP if plan["intent"] == "owners" else {"backup_for"} if plan["intent"] == "backups" else None
    edges = [e for e in graph["relationships"] if (e["target_id"] == node["id"] if kinds else node["id"] in (e["source_id"], e["target_id"])) and (not kinds or e["kind"] in kinds)]
    return {**base, "answer": f"{len(edges)} approved relationships found for {node['name']}.", "entity": node, "relationships": edges,
            "entities": [n for n in graph["entities"] if any(n["id"] in (e["source_id"], e["target_id"]) for e in edges)]}
