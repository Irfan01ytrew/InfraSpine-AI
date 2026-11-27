"""Live tenant/department predicates for existing document and citation queries."""

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.db.models import Document, User
from app.governance.models import Member, Resource, Tenant


def tenant_document_scope(user: User):
    """Legacy records stay in the legacy boundary; enrolled identities cannot cross it.

    Ownership is derived from retained membership rows, including revoked owners.
    No administrator bypass and no header supplied by a model or caller is trusted.
    Source ACLs still apply independently in app.rag.access.readable_by.
    """
    owner = aliased(Member)
    reader = aliased(Member)
    binding = aliased(Resource)
    enrolled_owner = exists().where(owner.user_id == Document.owner_id)
    enrolled_reader = exists().where(reader.user_id == user.id)
    has_binding = exists().where(binding.kind == "document", binding.external_id == Document.id)
    matching_binding = exists().where(
        binding.kind == "document", binding.external_id == Document.id,
        binding.tenant_id == reader.tenant_id,
        or_(binding.department_id.is_(None), reader.all_departments.is_(True), binding.department_id == reader.department_id),
    ).correlate(Document, reader)
    same_boundary = exists(select(1).select_from(owner).join(reader, reader.tenant_id == owner.tenant_id).join(Tenant, Tenant.id == reader.tenant_id).where(
        owner.user_id == Document.owner_id, reader.user_id == user.id,
        reader.active.is_(True), Tenant.enabled.is_(True),
        or_(reader.expires_at.is_(None), reader.expires_at > func.now()),
        or_(reader.all_departments.is_(True), owner.department_id.is_(None), reader.department_id == owner.department_id),
        or_(~has_binding, matching_binding),
    ))
    return or_(and_(~enrolled_owner, ~enrolled_reader, ~has_binding), same_boundary)
