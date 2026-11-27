"""Department-scoped employee interviews that produce human-authored, reviewed SOP drafts.

The SOP renderer only formats employee responses; it does not call an LLM or create citations.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.knowledge_interview_models import (
    KnowledgeInterviewReview,
    KnowledgeInterviewSession,
    KnowledgeInterviewSopRevision,
    KnowledgeInterviewTemplate,
)
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, ROLE_ADMIN, ROLE_MANAGER, User
from app.services import audit
from app.services.access_control import find_active_grant
from app.services.audit import AuditContext

_REVIEW_PERMISSION = "knowledge.review"
_REVIEW_CATEGORY = "operational"
_MAX_ANSWER_CHARS = 10_000
_MAX_TOTAL_ANSWER_CHARS = 30_000


class KnowledgeInterviewService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def options(self) -> dict:
        managed_templates: list[KnowledgeInterviewTemplate] = []
        templates = list((await self.db.execute(
            select(KnowledgeInterviewTemplate)
            .where(KnowledgeInterviewTemplate.is_active.is_(True))
            .order_by(KnowledgeInterviewTemplate.built_in.desc(), KnowledgeInterviewTemplate.title)
        )).scalars())
        if self.user.role == ROLE_ADMIN:
            managed_templates = list((await self.db.execute(
                select(KnowledgeInterviewTemplate).order_by(
                    KnowledgeInterviewTemplate.built_in.desc(), KnowledgeInterviewTemplate.title
                )
            )).scalars())
            groups = list((await self.db.execute(
                select(AccessGroup)
                .where(AccessGroup.kind == "department", AccessGroup.source == "local")
                .order_by(AccessGroup.name)
            )).scalars())
        elif self.user.role == ROLE_MANAGER:
            groups = list((await self.db.execute(
                select(AccessGroup)
                .join(DepartmentManager, DepartmentManager.group_id == AccessGroup.id)
                .join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                .where(
                    DepartmentManager.manager_user_id == self.user.id,
                    AccessGroupMember.user_id == self.user.id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
                .order_by(AccessGroup.name)
            )).scalars())
        else:
            managed_templates = []
            groups = list((await self.db.execute(
                select(AccessGroup)
                .join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                .where(
                    AccessGroupMember.user_id == self.user.id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
                .order_by(AccessGroup.name)
            )).scalars())
        return {
            "templates": templates,
            "managed_templates": managed_templates,
            "departments": groups,
            "can_manage_templates": self.user.role == ROLE_ADMIN,
        }

    async def create_template(
        self, *, slug: str, title: str, description: str, questions: list[dict]
    ) -> KnowledgeInterviewTemplate:
        self._admin_only()
        self._validate_template(slug, title, description, questions)
        row = KnowledgeInterviewTemplate(
            slug=slug,
            title=" ".join(title.split()),
            description=" ".join(description.split()),
            questions=questions,
            created_by_user_id=self.user.id,
        )
        self.db.add(row)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("A template with that key already exists.") from None
        audit.record(self.db, self.ctx, "knowledge.interview_template_created", target=str(row.id),
                     details={"slug": row.slug, "question_count": len(questions)})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def update_template(self, template_id: uuid.UUID, values: dict) -> KnowledgeInterviewTemplate:
        self._admin_only()
        row = await self.db.get(KnowledgeInterviewTemplate, template_id, with_for_update=True)
        if row is None:
            raise NotFoundError("Interview template not found.")
        title = values.get("title", row.title)
        description = values.get("description", row.description)
        questions = values.get("questions", row.questions)
        self._validate_template(row.slug, title, description, questions)
        for key in ("title", "description", "questions", "is_active"):
            if key in values:
                value = values[key]
                if key in {"title", "description"}:
                    value = " ".join(value.split())
                if key == "questions":
                    value = [question if isinstance(question, dict) else question.model_dump() for question in value]
                setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "knowledge.interview_template_updated", target=str(row.id),
                     details={"active": row.is_active, "question_count": len(row.questions)})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def start(self, template_id: uuid.UUID, department_group_id: uuid.UUID | None) -> KnowledgeInterviewSession:
        template = (await self.db.execute(
            select(KnowledgeInterviewTemplate).where(
                KnowledgeInterviewTemplate.id == template_id,
                KnowledgeInterviewTemplate.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if template is None:
            raise NotFoundError("Active interview template not found.")
        if department_group_id is not None:
            await self._validate_member_scope(department_group_id)
        snapshot = {"id": str(template.id), "slug": template.slug, "title": template.title,
                    "questions": template.questions}
        session = KnowledgeInterviewSession(
            template_id=template.id,
            template_snapshot=snapshot,
            owner_user_id=self.user.id,
            department_group_id=department_group_id,
        )
        self.db.add(session)
        await self.db.flush()
        audit.record(self.db, self.ctx, "knowledge.interview_started", target=str(session.id),
                     details={"template_id": str(template.id), "department_id": str(department_group_id) if department_group_id else None})
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def list_sessions(self) -> list[KnowledgeInterviewSession]:
        own = list((await self.db.execute(
            select(KnowledgeInterviewSession)
            .where(KnowledgeInterviewSession.owner_user_id == self.user.id)
            .order_by(KnowledgeInterviewSession.updated_at.desc())
            .limit(250)
        )).scalars())
        if self.user.role == ROLE_ADMIN:
            reviewable = list((await self.db.execute(
                select(KnowledgeInterviewSession)
                .where(KnowledgeInterviewSession.owner_user_id != self.user.id)
                .order_by(KnowledgeInterviewSession.updated_at.desc())
                .limit(250)
            )).scalars())
        else:
            reviewable = list((await self.db.execute(
                select(KnowledgeInterviewSession)
                .where(
                    KnowledgeInterviewSession.owner_user_id != self.user.id,
                    KnowledgeInterviewSession.status.in_(("submitted", "approved")),
                )
                .order_by(KnowledgeInterviewSession.updated_at.desc())
                .limit(500)
            )).scalars())
            reviewable = [row for row in reviewable if await self._can_review(row)]
        unique = {row.id: row for row in [*own, *reviewable]}
        return sorted(unique.values(), key=lambda row: row.updated_at, reverse=True)[:500]

    async def get(self, session_id: uuid.UUID, *, lock: bool = False) -> KnowledgeInterviewSession:
        query = select(KnowledgeInterviewSession).where(KnowledgeInterviewSession.id == session_id)
        if lock:
            query = query.with_for_update()
        session = (await self.db.execute(query)).scalar_one_or_none()
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.owner_user_id != self.user.id and not await self._can_review(session):
            raise NotFoundError("Interview session not found.")
        return session

    async def save_progress(self, session_id: uuid.UUID, answers: dict[str, str], sop_title: str | None) -> KnowledgeInterviewSession:
        session = await self.get(session_id, lock=True)
        if session.owner_user_id != self.user.id:
            raise NotFoundError("Interview session not found.")
        if session.status not in ("in_progress", "changes_requested"):
            raise ConflictError("This interview is read-only. Start another session to make a new SOP revision.")
        session.answers = self._normalize_answers(session, {**session.answers, **answers})
        if sop_title is not None:
            clean_title = " ".join(sop_title.split())
            if not clean_title:
                raise InvalidInputError("The SOP title cannot be blank.")
            session.sop_title = clean_title
        elif session.answers.get("process_name"):
            session.sop_title = self._default_title(session)
        elif not session.sop_title:
            session.sop_title = self._default_title(session)
        session.sop_body = self._render_sop(session) if session.answers else ""
        session.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "knowledge.interview_progress_saved", target=str(session.id),
                     details={"answered_fields": len(session.answers), "status": session.status})
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def submit(self, session_id: uuid.UUID) -> KnowledgeInterviewSession:
        session = await self.get(session_id, lock=True)
        if session.owner_user_id != self.user.id:
            raise NotFoundError("Interview session not found.")
        if session.status not in ("in_progress", "changes_requested"):
            raise ConflictError("Only an in-progress or returned interview can be submitted.")
        required = [q["key"] for q in session.template_snapshot["questions"] if q.get("required", True)]
        missing = [key for key in required if not str(session.answers.get(key, "")).strip()]
        if missing:
            raise InvalidInputError("Complete every required interview answer before submitting the SOP.")
        if session.department_group_id is None and self.user.role != ROLE_ADMIN:
            # A private draft may be saved by an employee, but only scoped company content can enter review.
            raise InvalidInputError("Choose a department before submitting this SOP for review.")
        session.sop_title = session.sop_title or self._default_title(session)
        session.sop_body = self._render_sop(session)
        session.revision_number += 1
        session.submitted_revision = session.revision_number
        session.submitted_at = datetime.now(UTC)
        session.status = "submitted"
        session.updated_at = session.submitted_at
        self.db.add(KnowledgeInterviewSopRevision(
            session_id=session.id,
            revision_number=session.revision_number,
            title=session.sop_title,
            body=session.sop_body,
            answers_snapshot=dict(session.answers),
            submitted_by_user_id=self.user.id,
        ))
        audit.record(self.db, self.ctx, "knowledge.interview_submitted", target=str(session.id),
                     details={"revision": session.revision_number, "department_id": str(session.department_group_id)})
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def review(self, session_id: uuid.UUID, decision: str, rationale: str) -> KnowledgeInterviewSession:
        session = await self.get(session_id, lock=True)
        if session.owner_user_id == self.user.id or not await self._can_review(session):
            raise PermissionDeniedError("A different authorized department reviewer is required.")
        if session.status != "submitted" or session.submitted_revision != session.revision_number:
            raise ConflictError("Only the current submitted SOP revision can be reviewed.")
        if decision not in ("approved", "changes_requested"):
            raise InvalidInputError("Choose approve or request changes.")
        clean_rationale = " ".join(rationale.split())
        if len(clean_rationale) < 3 or len(clean_rationale) > 2000:
            raise InvalidInputError("Add a review rationale between 3 and 2000 characters.")
        review = KnowledgeInterviewReview(
            session_id=session.id,
            revision_number=session.revision_number,
            reviewer_user_id=self.user.id,
            decision=decision,
            rationale=clean_rationale,
        )
        self.db.add(review)
        session.status = decision
        if decision == "approved":
            session.approved_at = datetime.now(UTC)
        session.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, f"knowledge.interview_{decision}", target=str(session.id),
                     details={"revision": session.revision_number, "department_id": str(session.department_group_id)})
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def withdraw(self, session_id: uuid.UUID) -> KnowledgeInterviewSession:
        session = await self.get(session_id, lock=True)
        if session.owner_user_id != self.user.id:
            raise NotFoundError("Interview session not found.")
        if session.status not in ("in_progress", "submitted", "changes_requested"):
            raise ConflictError("An approved or already withdrawn SOP cannot be withdrawn.")
        session.status = "withdrawn"
        session.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "knowledge.interview_withdrawn", target=str(session.id),
                     details={"revision": session.revision_number})
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def view(self, session: KnowledgeInterviewSession) -> dict:
        row = (await self.db.execute(
            select(User.email, AccessGroup.name)
            .select_from(User)
            .outerjoin(AccessGroup, AccessGroup.id == session.department_group_id)
            .where(User.id == session.owner_user_id)
        )).one()
        reviews = (await self.db.execute(
            select(KnowledgeInterviewReview, User.email)
            .join(User, User.id == KnowledgeInterviewReview.reviewer_user_id)
            .where(KnowledgeInterviewReview.session_id == session.id)
            .order_by(KnowledgeInterviewReview.reviewed_at)
        )).all()
        sop_revisions = list((await self.db.execute(
            select(KnowledgeInterviewSopRevision)
            .where(KnowledgeInterviewSopRevision.session_id == session.id)
            .order_by(KnowledgeInterviewSopRevision.revision_number)
        )).scalars())
        own = session.owner_user_id == self.user.id
        can_review = not own and await self._can_review(session) and session.status == "submitted"
        return {
            "id": session.id,
            "template_id": session.template_id,
            "template_title": session.template_snapshot["title"],
            "template_questions": session.template_snapshot["questions"],
            "owner_user_id": session.owner_user_id,
            "owner_email": row[0],
            "department_group_id": session.department_group_id,
            "department_name": row[1],
            "status": session.status,
            "answers": session.answers,
            "sop_title": session.sop_title,
            "sop_body": session.sop_body,
            "revision_number": session.revision_number,
            "submitted_revision": session.submitted_revision,
            "submitted_at": session.submitted_at,
            "approved_at": session.approved_at,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "can_edit": own and session.status in ("in_progress", "changes_requested"),
            "can_review": can_review,
            "reviews": [{
                "id": review.id,
                "revision_number": review.revision_number,
                "reviewer_user_id": review.reviewer_user_id,
                "reviewer_email": email,
                "decision": review.decision,
                "rationale": review.rationale,
                "reviewed_at": review.reviewed_at,
            } for review, email in reviews],
            "sop_revisions": sop_revisions,
        }

    async def _validate_member_scope(self, group_id: uuid.UUID) -> None:
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != "department" or group.source != "local":
            raise InvalidInputError("Choose a local department.")
        member = (await self.db.execute(
            select(AccessGroupMember.user_id).where(
                AccessGroupMember.group_id == group_id,
                AccessGroupMember.user_id == self.user.id,
            )
        )).scalar_one_or_none()
        if self.user.role == ROLE_ADMIN:
            return
        if member is None:
            raise PermissionDeniedError("You must be an active department member to start this interview.")
        if self.user.role == ROLE_MANAGER:
            manager = (await self.db.execute(
                select(DepartmentManager.group_id).where(
                    DepartmentManager.group_id == group_id,
                    DepartmentManager.manager_user_id == self.user.id,
                )
            )).scalar_one_or_none()
            if manager is None:
                raise PermissionDeniedError("Managers can start department interviews only in a department they manage.")

    async def _can_review(self, session: KnowledgeInterviewSession) -> bool:
        if self.user.role == ROLE_ADMIN:
            return True
        if session.status not in ("submitted", "approved") or session.owner_user_id == self.user.id:
            return False
        group_id = session.department_group_id
        if group_id is not None and self.user.role == ROLE_MANAGER:
            managed_member = (await self.db.execute(
                select(DepartmentManager.group_id)
                .join(AccessGroupMember, AccessGroupMember.group_id == DepartmentManager.group_id)
                .join(AccessGroup, AccessGroup.id == DepartmentManager.group_id)
                .where(
                    DepartmentManager.group_id == group_id,
                    DepartmentManager.manager_user_id == self.user.id,
                    AccessGroupMember.user_id == self.user.id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
            )).scalar_one_or_none()
            if managed_member is not None:
                return True
        if group_id is not None:
            group = await self.db.get(AccessGroup, group_id)
            if group is None or group.kind != "department" or group.source != "local":
                return False
            member = (await self.db.execute(select(AccessGroupMember.user_id).where(
                AccessGroupMember.group_id == group_id, AccessGroupMember.user_id == self.user.id
            ))).scalar_one_or_none()
            if member is None:
                return False
        return await find_active_grant(
            self.db, self.user.id, _REVIEW_PERMISSION, _REVIEW_CATEGORY, group_id
        ) is not None

    def _normalize_answers(self, session: KnowledgeInterviewSession, answers: dict[str, str]) -> dict[str, str]:
        keys = {question["key"] for question in session.template_snapshot["questions"]}
        unknown = set(answers) - keys
        if unknown:
            raise InvalidInputError("Answers contain a question that is not in this saved template version.")
        normalized = {key: value.strip() for key, value in answers.items()}
        if any(len(value) > _MAX_ANSWER_CHARS for value in normalized.values()):
            raise InvalidInputError("Each interview answer must be 10,000 characters or fewer.")
        if sum(len(value) for value in normalized.values()) > _MAX_TOTAL_ANSWER_CHARS:
            raise InvalidInputError("The interview has reached its 30,000-character total answer limit.")
        return normalized

    @staticmethod
    def _default_title(session: KnowledgeInterviewSession) -> str:
        title = str(session.answers.get("process_name", "")).strip()
        return title[:180] if title else str(session.template_snapshot["title"])[:180]

    @staticmethod
    def _render_sop(session: KnowledgeInterviewSession) -> str:
        sections = [
            "# " + session.sop_title,
            "",
            "This draft is formatted from employee-provided interview responses. No AI-generated claims or citations have been added.",
        ]
        for question in session.template_snapshot["questions"]:
            answer = str(session.answers.get(question["key"], "")).strip()
            if answer:
                sections.extend(("", "## " + question["prompt"], "", answer))
        return "\n".join(sections)[:40_000]

    @staticmethod
    def _validate_template(slug: str, title: str, description: str, questions: list[dict]) -> None:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not 3 <= len(slug) <= 80:
            raise InvalidInputError("Use a 3-80 character lowercase template key with single hyphens.")
        if not title.strip() or len(title.strip()) > 160 or not description.strip() or len(description.strip()) > 800:
            raise InvalidInputError("Add a title up to 160 characters and a description up to 800 characters.")
        if not 1 <= len(questions) <= 20:
            raise InvalidInputError("An interview template must have 1-20 questions.")
        keys = []
        for question in questions:
            key = str(question.get("key", ""))
            prompt = str(question.get("prompt", ""))
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", key) or not 3 <= len(prompt.strip()) <= 300:
                raise InvalidInputError("Each template question needs a valid key and 3-300 character prompt.")
            if not isinstance(question.get("required", True), bool):
                raise InvalidInputError("Question required flags must be true or false.")
            keys.append(key)
        if len(keys) != len(set(keys)):
            raise InvalidInputError("Question keys must be unique.")

    def _admin_only(self) -> None:
        if self.user.role != ROLE_ADMIN:
            raise PermissionDeniedError("Only administrators can manage interview templates.")
