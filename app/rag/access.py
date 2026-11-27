"""Document authorization rules, defined once and used by every read, write and retrieval path.
PostgreSQL is the authority: `readable_by` is a SQL condition evaluated inside each query, so group
membership, ACL and quarantine changes apply on the very next request (nothing is cached).

* private:    the owner only.
* shared:     every signed-in user; only admins may publish or change shared documents
              (so regular users cannot inject content into everyone's answers).
* restricted: the owner of an uploaded document, plus the users and groups in `resource_acl`.
* synced (file_server) documents are always restricted and readable only through their synced ACL.
* quarantined documents (unknown or unsupported source ACL) are readable by nobody.
Admins get no special read access to any document they could not otherwise read.
A second, live gate (`platform_permits`) requires an active platform membership with
`documents.read` on the organisation (revoking a user therefore cuts retrieval on the next query).
Until the organisation scope has been created (a fresh database before backfill), this gate is open
so existing checks alone apply.
"""

from sqlalchemy import ColumnElement, and_, exists, func, or_, select

from app.db.models import (
    ACL_ACTIVE,
    ROLE_ADMIN,
    SOURCE_UPLOAD,
    VISIBILITY_PRIVATE,
    VISIBILITY_RESTRICTED,
    VISIBILITY_SHARED,
    AccessGroupMember,
    CloudSourceAcl,
    DepartmentManager,
    Document,
    KnowledgeEvidence,
    KnowledgeRevision,
    ResourceAcl,
    User,
)


def _acl_allows(user: User) -> ColumnElement[bool]:
    groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == user.id)
    # A department manager receives the same read scope as membership in the department,
    # without receiving organization-wide administrator access.
    if user.role == "manager":
        groups = groups.union(
            select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == user.id)
        )
    return exists().where(
        ResourceAcl.document_id == Document.id,
        or_(ResourceAcl.principal_user_id == user.id, ResourceAcl.principal_group_id.in_(groups)),
    )


def platform_permits(user: User, permission: str) -> ColumnElement[bool]:
    """True when the user holds `permission` on the organisation, or when the platform org scope does not exist yet."""
    from app.platform.models import PlatformMembership, PlatformRolePermission, PlatformScope

    now = func.now()
    org = select(PlatformScope.id).where(PlatformScope.kind == "org", PlatformScope.active.is_(True)).limit(1).scalar_subquery()
    granted = exists().where(
        PlatformMembership.user_id == user.id,
        PlatformMembership.scope_id == org,
        PlatformMembership.revoked_at.is_(None),
        or_(PlatformMembership.valid_from.is_(None), PlatformMembership.valid_from <= now),
        or_(PlatformMembership.valid_to.is_(None), PlatformMembership.valid_to > now),
        PlatformRolePermission.role_id == PlatformMembership.role_id,
        or_(PlatformRolePermission.permission == permission, PlatformRolePermission.permission == "*"),
    )
    org_missing = ~exists().where(PlatformScope.kind == "org", PlatformScope.active.is_(True))
    active = exists().where(User.id == user.id, User.is_active.is_(True))
    return or_(org_missing, and_(active, granted))


def readable_by(user: User) -> ColumnElement[bool]:
    provider_acl = exists().where(
        CloudSourceAcl.document_id == Document.id,
        or_(
            CloudSourceAcl.principal_user_id == user.id,
            CloudSourceAcl.principal_group_id.in_(
                select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == user.id)
                .union(
                    select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == user.id)
                )
                if user.role == "manager"
                else select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == user.id)
            ),
        ),
    )
    is_cloud_source = and_(
        Document.source_system != SOURCE_UPLOAD,
        Document.source_uri.like("cloud://%").is_(True),
    )
    return and_(
        platform_permits(user, "documents.read"),
        Document.acl_status == ACL_ACTIVE,
        or_(
            and_(Document.source_system == SOURCE_UPLOAD, Document.owner_id == user.id),
            and_(Document.source_system == SOURCE_UPLOAD, Document.visibility == VISIBILITY_SHARED),
            and_(
                Document.visibility == VISIBILITY_RESTRICTED,
                _acl_allows(user),
                or_(~is_cloud_source, provider_acl),
            ),
        ),
    )


def knowledge_readable_by(user: User, *statuses: str, include_stale: bool = False) -> ColumnElement[bool]:
    """Knowledge scope plus live authorization to every cited source, evaluated before retrieval.

    A published statement never keeps access after one of its source documents becomes unreadable,
    stale, or orphaned. Department managers receive their managed department scope just like document ACLs.
    """
    statuses = statuses or ("published",)
    groups = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == user.id)
    if user.role == "manager":
        groups = groups.union(
            select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == user.id)
        )
    has_evidence = exists().where(KnowledgeEvidence.revision_id == KnowledgeRevision.id)
    missing_source = exists().where(
        KnowledgeEvidence.revision_id == KnowledgeRevision.id,
        KnowledgeEvidence.document_id.is_(None),
    )
    orphaned = exists().where(
        KnowledgeEvidence.revision_id == KnowledgeRevision.id,
        KnowledgeEvidence.orphan_reason.is_not(None),
    )
    unreadable_source = exists().where(
        KnowledgeEvidence.revision_id == KnowledgeRevision.id,
        ~KnowledgeEvidence.document_id.in_(select(Document.id).where(readable_by(user))),
    )
    conditions = [
        platform_permits(user, "documents.read"),
        KnowledgeRevision.status.in_(statuses),
        has_evidence,
        ~missing_source,
        ~orphaned,
        ~unreadable_source,
        or_(
            and_(KnowledgeRevision.visibility == VISIBILITY_PRIVATE, KnowledgeRevision.author_user_id == user.id),
            KnowledgeRevision.visibility == VISIBILITY_SHARED,
            and_(
                KnowledgeRevision.visibility == VISIBILITY_RESTRICTED,
                KnowledgeRevision.scope_group_id.in_(groups),
            ),
        ),
    ]
    if not include_stale:
        conditions.append(
            or_(KnowledgeRevision.review_due_at.is_(None), KnowledgeRevision.review_due_at > func.now())
        )
    return and_(*conditions)


def can_modify(user: User, document: Document) -> bool:
    """Only for documents the user can already read. Synced documents change only through their source."""
    if document.source_system != SOURCE_UPLOAD:
        return False
    if document.visibility == VISIBILITY_PRIVATE:
        return document.owner_id == user.id
    return user.role == ROLE_ADMIN


def can_publish(user: User, visibility: str) -> bool:
    return visibility == VISIBILITY_PRIVATE or user.role == ROLE_ADMIN
