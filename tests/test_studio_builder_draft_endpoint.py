"""Step AD slice 2: POST /scopes/{scope_id}/templates/{key}/draft.

Real PostgreSQL/API acceptance. No live resources. The draft is inert: it is
never published and never executed by this endpoint.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.conftest import USER2_EMAIL, login, run_db
from tests.test_workflow_engine import setup_scope


def draft_from_template(client, scope, key, body=None):
    return client.post(f'/api/workflow-engine/scopes/{scope}/templates/{key}/draft',
                       json=body or {})


def test_template_draft_creates_studio_draft(client):
    scope, _ = setup_scope(client)
    response = draft_from_template(client, scope, 'invoice-approval')
    assert response.status_code == 201, response.text
    body = response.json()
    workflow, version = body['workflow'], body['version']
    assert workflow['origin'] == 'studio'
    assert workflow['origin_ref'] == 'invoice-approval'
    assert workflow['name'] == 'Invoice approve karwao'
    assert workflow['enabled'] is False
    assert workflow['active_version'] is None
    assert version['status'] == 'draft'
    assert version['number'] == 1
    assert version['workflow_id'] == workflow['id']
    # The saved spec is the validated template graph, not an empty shell.
    assert version['spec']['nodes']
    assert any(n['type'] == 'approval' for n in version['spec']['nodes'])


def test_template_draft_name_override(client):
    scope, _ = setup_scope(client)
    response = draft_from_template(client, scope, 'invoice-approval',
                                   {'name': 'March vendor approvals', 'description': 'Q1 catch-up'})
    assert response.status_code == 201, response.text
    workflow = response.json()['workflow']
    assert workflow['name'] == 'March vendor approvals'
    assert workflow['description'] == 'Q1 catch-up'


def test_template_draft_unknown_key_404(client):
    scope, _ = setup_scope(client)
    response = draft_from_template(client, scope, 'no-such-template')
    assert response.status_code == 404, response.text


def test_template_draft_tampered_spec_422(client, monkeypatch):
    from app.agent_studio.templates import template_catalog
    scope, _ = setup_scope(client)
    real = template_catalog()

    def broken_catalog():
        catalog = [dict(t) for t in real]
        for template in catalog:
            if template['key'] == 'invoice-approval':
                template['spec'] = {'nodes': [], 'edges': []}
        return catalog

    monkeypatch.setattr('app.agent_studio.templates.template_catalog', broken_catalog)
    response = draft_from_template(client, scope, 'invoice-approval')
    assert response.status_code == 422, response.text


def test_template_draft_scope_errors(client):
    scope, people = setup_scope(client)
    assert draft_from_template(client, scope, 'invoice-approval').status_code == 201
    # Unknown scope is not revealed.
    assert draft_from_template(client, uuid4(), 'invoice-approval').status_code == 404
    # A removed member loses access (403, not 404: the scope exists).
    run_db(lambda conn: conn.execute(text(
        'DELETE FROM access_group_members WHERE group_id=:g AND user_id=:u'),
        {'g': scope, 'u': people[USER2_EMAIL]}))
    login(client, USER2_EMAIL)
    assert draft_from_template(client, scope, 'onboarding').status_code == 403


def test_template_draft_duplicate_409(client):
    scope, _ = setup_scope(client)
    assert draft_from_template(client, scope, 'invoice-approval').status_code == 201
    response = draft_from_template(client, scope, 'invoice-approval')
    assert response.status_code == 409, response.text
    # A different template in the same scope is still fine.
    assert draft_from_template(client, scope, 'onboarding').status_code == 201
