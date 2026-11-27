"""Truthful behavior for action types without a real execution adapter."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest

from app.core.errors import AgentActionUnavailableError, PermissionDeniedError
from app.db.models import ACL_ACTIVE, ROLE_ADMIN, ROLE_MANAGER, SOURCE_UPLOAD, VISIBILITY_PRIVATE, VISIBILITY_SHARED
from app.services.agent_actions import _execute_adapter


@pytest.mark.parametrize(
    "action_type,parameters",
    [
        ("webhook_dispatch", {"url": "https://hooks.example.test/token", "payload": {"x": 1}}),
        ("notification_broadcast", {"title": "Notice", "message": "Synthetic text"}),
    ],
)
def test_unimplemented_external_actions_fail_closed_without_claiming_delivery(action_type, parameters):
    proposal = SimpleNamespace(action_type=action_type, parameters=parameters)

    with pytest.raises(AgentActionUnavailableError) as caught:
        asyncio.run(_execute_adapter(None, proposal))

    assert caught.value.status_code == 503
    assert caught.value.code == "agent_action_unavailable"
    assert "hooks.example.test" not in caught.value.message
    assert "Synthetic text" not in caught.value.message


class _DocumentDb:
    def __init__(self, document):
        self.document = document
        self.flushed = False

    async def get(self, model, document_id):
        assert document_id == self.document.id
        return self.document

    async def flush(self):
        self.flushed = True


def _publish_proposal(document_id):
    return SimpleNamespace(
        action_type="document_publish",
        parameters={"document_id": str(document_id)},
    )


def test_department_manager_cannot_publish_a_document_organization_wide():
    document = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        source_system=SOURCE_UPLOAD,
        acl_status=ACL_ACTIVE,
        visibility=VISIBILITY_PRIVATE,
        filename="synthetic.pdf",
    )
    actor = SimpleNamespace(id=uuid.uuid4(), role=ROLE_MANAGER)
    db = _DocumentDb(document)

    with pytest.raises(PermissionDeniedError):
        asyncio.run(_execute_adapter(db, _publish_proposal(document.id), actor))

    assert document.visibility == VISIBILITY_PRIVATE
    assert db.flushed is False


def test_admin_owner_can_publish_an_active_uploaded_document():
    actor = SimpleNamespace(id=uuid.uuid4(), role=ROLE_ADMIN)
    document = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=actor.id,
        source_system=SOURCE_UPLOAD,
        acl_status=ACL_ACTIVE,
        visibility=VISIBILITY_PRIVATE,
        filename="synthetic.pdf",
    )
    db = _DocumentDb(document)

    result = asyncio.run(_execute_adapter(db, _publish_proposal(document.id), actor))

    assert result["published"] is True
    assert result["visibility"] == VISIBILITY_SHARED
    assert document.visibility == VISIBILITY_SHARED
    assert db.flushed is True


@pytest.mark.parametrize(
    "source_system,acl_status",
    [("file_server", ACL_ACTIVE), (SOURCE_UPLOAD, "quarantined")],
)
def test_admin_cannot_publish_synced_or_quarantined_documents(source_system, acl_status):
    actor = SimpleNamespace(id=uuid.uuid4(), role=ROLE_ADMIN)
    document = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=actor.id,
        source_system=source_system,
        acl_status=acl_status,
        visibility=VISIBILITY_PRIVATE,
        filename="synthetic.pdf",
    )
    db = _DocumentDb(document)

    with pytest.raises(PermissionDeniedError):
        asyncio.run(_execute_adapter(db, _publish_proposal(document.id), actor))

    assert document.visibility == VISIBILITY_PRIVATE
    assert db.flushed is False
