"""Labeled synthetic purchase-approval evidence; never installed in a company database."""
from datetime import UTC, datetime, timedelta


def purchase_events(prefix="purchase", copies=1):
    records = []
    normal = [("Request", 0, "employee", "Procurement"), ("Manager approval", 1, "manager", "Operations"), ("Finance approval", 2, "finance", "Finance"), ("Paid", 3, "finance", "Finance")]
    extra = [normal[0], normal[1], ("Director approval", 26, "director", "Executive"), ("Finance approval", 30, "finance", "Finance"), ("Paid", 31, "finance", "Finance")]
    rework = [normal[0], normal[1], ("Request", 2, "employee", "Procurement"), ("Finance approval", 4, "finance", "Finance"), ("Paid", 6, "finance", "Finance")]
    for batch in range(copies):
        for case, sequence in enumerate([normal, normal, normal, extra, extra, rework]):
            start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=case, minutes=batch)
            key = f"{prefix}-{batch}-{case}"
            for index, (activity, hour, actor, department) in enumerate(sequence):
                timestamp = start + timedelta(hours=hour)
                records.append({"external_id": f"{key}-{index}", "case_id": key, "activity": activity,
                                "occurred_at": timestamp.isoformat(), "finished_at": timestamp.isoformat(),
                                "actor": actor, "department": department, "terminal": index == len(sequence)-1,
                                "evidence_ref": f"purchase-ledger:{key}:row:{index}"})
    return records


BASELINE = {"title": "Reviewed purchase SOP", "sequences": [["Request", "Manager approval", "Finance approval", "Paid"]], "sla_hours": 8, "evidence_ref": "sop:purchase:v1"}
