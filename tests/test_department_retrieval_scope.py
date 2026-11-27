"""SQL-level checks for the department boundary used by scoped agent runs."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models import Document, User
from app.rag.retrieval import _scope


def test_department_scope_is_applied_in_the_same_document_authorization_query():
    user = User(id=uuid.uuid4(), role="employee")
    department_id = uuid.uuid4()
    statement = select(Document.id).where(
        *_scope(user, "test-embedding-model", department_group_id=department_id)
    )
    compiled = statement.compile(dialect=postgresql.dialect())

    sql = str(compiled)
    assert "resource_acl.principal_group_id =" in sql
    assert "resource_acl.document_id = documents.id" in sql
    assert department_id in compiled.params.values()


def test_default_retrieval_does_not_add_a_department_restriction():
    user = User(id=uuid.uuid4(), role="employee")
    department_id = uuid.uuid4()
    statement = select(Document.id).where(*_scope(user, "test-embedding-model"))
    compiled = statement.compile(dialect=postgresql.dialect())

    assert department_id not in compiled.params.values()
    assert str(compiled).count("resource_acl.principal_group_id =") == 0
