"""Graph merge start (Step G slice 6): one identity across the two company graphs.

Continuity keeps its own graph (``bc_nodes``) and the company graph keeps
``company_graph_records``. A merge never rewrites either graph directly:
the seed proposes company-graph records through the governed
``discovery.synchronize`` path (pending ``GraphChange`` rows, approval
required), and records the pairing in ``bc_graph_links`` with one stable
``unified_key`` per identity:

* a person is ``person:<user_id>`` — the same user in both graphs;
* a department is ``department:<access_group_id>`` — the same access group;
* anything else is ``continuity:<bc_node_id>`` — continuity-only for now.

Link status is computed live in ``merge_status``: a link is active only
while the company-graph record exists and is not retired, and while the
continuity node still exists. Seeding twice proposes nothing twice:
``synchronize`` replays an identical sync and skips identical pending
proposals.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.company_graph import discovery
from app.company_graph.access import scope_for as cg_scope_for, visible as cg_visible
from app.company_graph.models import GraphRecord, GraphSource
from app.company_graph.schemas import SyncInput, SyncItem
from app.continuity import journal, service
from app.continuity.models import GraphLink, Member, Node
from app.db.models import AccessGroupMember, User
from app.platform.events import publish

SOURCE_NAME = "Continuity Engine"
SOURCE_KIND = "continuity"


def unified_key(kind: str, *, user_id=None, access_group_id=None, node_id=None) -> str:
    """The one stable identity for a node across both graphs."""
    if kind == "employee":
        if user_id is None:
            raise ValueError("A person identity needs a user id.")
        return f"person:{user_id}"
    if kind == "department":
        if access_group_id is None:
            raise ValueError("A department identity needs an access group id.")
        return f"department:{access_group_id}"
    if node_id is None:
        raise ValueError("A continuity-only identity needs a node id.")
    return f"continuity:{node_id}"


def seed_items(departments, persons) -> list[dict]:
    """The sync items for one seed, in deterministic order.

    ``departments`` are mappings with id/name/access_group_id;
    ``persons`` are mappings with user_id/name/node_id/department_id.
    Pure: no I/O, no clock.
    """
    items = []
    for department in sorted(departments, key=lambda d: str(d["id"])):
        key = unified_key("department", access_group_id=department["access_group_id"])
        items.append({"key": key, "kind": "department", "name": department["name"],
                      "quote": f"Continuity department {department['name']} bound to its access group.",
                      "attributes": {"continuity_node_id": str(department["id"]),
                                     "access_group_id": str(department["access_group_id"])}})
    for person in sorted(persons, key=lambda p: str(p["user_id"])):
        key = unified_key("employee", user_id=person["user_id"])
        items.append({"key": key, "kind": "person", "name": person["name"],
                      "quote": f"Continuity member {person['name']} with an active membership.",
                      "attributes": {"continuity_node_id": str(person["node_id"]),
                                     "user_id": str(person["user_id"])}})
    return items


def seed_idempotency_key(bc_tenant_id: uuid.UUID, items: list[dict]) -> str:
    digest = hashlib.sha256("\n".join(item["key"] for item in items).encode()).hexdigest()[:16]
    return f"continuity-seed:{bc_tenant_id}:{digest}"


# ------------------------------------------------------------------------------------------- service

async def _get_or_create_source(db, cg_scope, user):
    source = await db.scalar(select(GraphSource).where(
        GraphSource.tenant_id == cg_scope.tenant_id, GraphSource.kind == SOURCE_KIND,
        GraphSource.name == SOURCE_NAME))
    if source is None:
        source = GraphSource(tenant_id=cg_scope.tenant_id, name=SOURCE_NAME, kind=SOURCE_KIND,
                             created_by=user.id)
        db.add(source)
        await db.flush()
    return source


async def _ensure_employee_node(db, bc_scope, user_id, name, department_id, ctx):
    existing = await db.scalar(select(Node).where(
        Node.tenant_id == bc_scope.tenant_id, Node.kind == "employee", Node.user_id == user_id))
    if existing is not None:
        return existing
    from app.continuity.schemas import Evidence, NodeInput

    now = datetime.now(UTC)
    data = NodeInput(external_key=f"seed-person-{user_id}", name=name, kind="employee",
                     user_id=user_id, department_id=department_id, basis="verified_fact",
                     evidence=[Evidence(reference="continuity-graph-merge",
                                        note="Seeded from an active continuity membership.",
                                        observed_at=now)])
    await service.create_node(db, bc_scope, data, ctx)
    # create_node returns the public dict; callers need the Node row, so
    # re-read it (mirrors the existing-node lookup above).
    return await db.scalar(select(Node).where(
        Node.tenant_id == bc_scope.tenant_id, Node.kind == "employee", Node.user_id == user_id))


async def seed_graph_merge(db, bc_scope, cg_tenant_id: uuid.UUID, user, ctx) -> dict:
    """Seed the company graph from continuity memberships, via the governed path."""
    bc_scope.require("owner", "planner")
    cg_scope = await cg_scope_for(db, cg_tenant_id, user)
    cg_scope.require("owner")
    source = await _get_or_create_source(db, cg_scope, user)
    nodes, _links, _assessments = await service.graph(db, bc_scope)
    departments = [n for n in nodes if n.kind == "department" and n.access_group_id]
    department_by_group = {str(n.access_group_id): n for n in departments}
    member_rows = (await db.scalars(select(Member).where(
        Member.tenant_id == bc_scope.tenant_id, Member.active.is_(True)))).all()
    group_rows = (await db.scalars(select(AccessGroupMember).where(
        AccessGroupMember.group_id.in_([n.access_group_id for n in departments])))).all() if departments else []
    people: dict[uuid.UUID, uuid.UUID | None] = {}
    for member in member_rows:
        people[member.user_id] = None
    for row in group_rows:
        department = department_by_group.get(str(row.group_id))
        people.setdefault(row.user_id, department.id if department else None)
    users = {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(list(people))))).all()}
    persons = []
    for user_id, department_id in sorted(people.items(), key=lambda item: str(item[0])):
        member_user = users.get(user_id)
        if member_user is None:
            continue
        node = await _ensure_employee_node(db, bc_scope, user_id, member_user.email, department_id, ctx)
        persons.append({"user_id": user_id, "name": member_user.email,
                        "node_id": node.id, "department_id": department_id})
    items = seed_items(
        [{"id": n.id, "name": n.name, "access_group_id": n.access_group_id} for n in departments],
        persons)
    now = datetime.now(UTC)
    result = await discovery.synchronize(
        db, cg_scope, source.id,
        SyncInput(idempotency_key=seed_idempotency_key(bc_scope.tenant_id, items),
                  observed_at=now, reference="continuity-graph-merge",
                  visibility="restricted", readers=[user.id],
                  items=[SyncItem(key=item["key"], kind=item["kind"], name=item["name"],
                                  quote=item["quote"], attributes=item["attributes"]) for item in items]),
        ctx)
    links_created = 0
    node_by_key = {}
    for department in departments:
        node_by_key[unified_key("department", access_group_id=department.access_group_id)] = department.id
    for person in persons:
        node_by_key[unified_key("employee", user_id=person["user_id"])] = person["node_id"]
    for item in items:
        record_id = uuid.uuid5(source.id, item["key"])
        existing = await db.scalar(select(GraphLink).where(
            GraphLink.bc_tenant_id == bc_scope.tenant_id, GraphLink.cg_tenant_id == cg_tenant_id,
            GraphLink.unified_key == item["key"]))
        if existing is not None:
            continue
        db.add(GraphLink(bc_tenant_id=bc_scope.tenant_id, cg_tenant_id=cg_tenant_id,
                         bc_node_id=node_by_key[item["key"]], cg_record_id=record_id,
                         unified_key=item["key"], kind=item["kind"]))
        links_created += 1
    await journal.record(db, bc_scope, ctx, "graph_merge_seeded", bc_scope.tenant_id,
                         {"company_graph_tenant_id": str(cg_tenant_id), "source_id": str(source.id),
                          "proposed": len(result["proposal_ids"]), "links_created": links_created})
    await publish(db, "continuity.graph_merge_seeded", bc_scope.tenant_id,
                  {"company_graph_tenant_id": str(cg_tenant_id),
                   "proposed": len(result["proposal_ids"]), "links_created": links_created},
                  trace_id=uuid.uuid4())
    return {"source_id": str(source.id), "departments": len(departments), "persons": len(persons),
            "links_created": links_created, **result}


async def merge_status(db, bc_scope, cg_tenant_id: uuid.UUID, user) -> dict:
    """Live merge status: what is linked, what is not, and what is stale."""
    cg_scope = await cg_scope_for(db, cg_tenant_id, user)
    nodes, _links, _assessments = await service.graph(db, bc_scope)
    link_rows = (await db.scalars(select(GraphLink).where(
        GraphLink.bc_tenant_id == bc_scope.tenant_id, GraphLink.cg_tenant_id == cg_tenant_id))).all()
    linked_node_ids = {row.bc_node_id for row in link_rows}
    linked_record_ids = {row.cg_record_id for row in link_rows}
    # Visibility is enforced by the company-graph scope: only records the
    # caller may inspect count as linked or unlinked candidates.
    visible_records = (await db.scalars(select(GraphRecord).where(
        cg_visible(cg_scope), GraphRecord.tenant_id == cg_tenant_id,
        GraphRecord.record_type == "entity", GraphRecord.retired.is_(False)))).all()
    visible_ids = {r.id for r in visible_records}
    now = datetime.now(UTC)
    sources = (await db.scalars(select(GraphSource).where(GraphSource.tenant_id == cg_tenant_id))).all()
    stale = [{"id": str(s.id), "name": s.name, "kind": s.kind,
              "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None}
             for s in sources
             if s.last_synced_at is None or (now - s.last_synced_at).days >= s.stale_after_days]
    return {
        "continuity_tenant_id": str(bc_scope.tenant_id),
        "company_graph_tenant_id": str(cg_tenant_id),
        "bc_nodes": len(nodes),
        "bc_nodes_linked": len([n for n in nodes if n.id in linked_node_ids]),
        "bc_nodes_unlinked": [{"id": str(n.id), "kind": n.kind, "name": n.name}
                              for n in nodes if n.id not in linked_node_ids][:10],
        "cg_records": len(visible_ids),
        "cg_records_linked": len(linked_record_ids & visible_ids),
        "cg_records_unlinked": [{"id": str(r.id), "kind": r.kind, "name": r.name}
                                for r in visible_records if r.id not in linked_record_ids][:10],
        "links": len(link_rows),
        "stale_sources": stale,
    }
