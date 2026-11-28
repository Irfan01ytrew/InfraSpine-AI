"""Step AD slice 1: Studio template library — pure functions, no DB."""

import pytest

from app.agent_studio.templates import (
    create_draft_from_template,
    get_studio_template,
    list_studio_templates,
)
from app.workflows.definition import Definition, validate_graph


def test_list_has_all_templates_with_metadata():
    templates = list_studio_templates()
    keys = [t['key'] for t in templates]
    assert 'invoice-approval' in keys
    assert len(keys) == len(set(keys))
    for template in templates:
        assert template['name'] and template['description']
        assert template['category'] in {'finance', 'hr', 'it', 'operations'}
        assert template['node_count'] >= 2
        assert template['steps']
        assert all(step['label'] for step in template['steps'])
        assert template['required_inputs']


def test_every_template_makes_a_valid_draft():
    for template in list_studio_templates():
        draft = create_draft_from_template(template['key'])
        assert draft['origin'] == 'studio'
        assert draft['origin_ref'] == template['key']
        assert draft['status'] == 'draft'
        assert draft['spec']['source'] == 'template'
        assert validate_graph(Definition.model_validate(draft['spec'])) == []


def test_invoice_approval_draft_shape():
    draft = create_draft_from_template('invoice-approval')
    assert draft['name'] == 'Invoice approve karwao'
    nodes = {n['id']: n for n in draft['spec']['nodes']}
    approval = nodes['approval']
    assert approval['type'] == 'approval' and approval['risk'] == 'high'
    assert approval['config']['financial_category'] == 'expense'
    assert approval['config']['amount_variable'] == 'input.amount'
    labels = [n['label'] for n in draft['spec']['nodes']]
    assert 'Verify invoice evidence' in labels
    assert 'Record payment authorization' in labels
    inputs = {f['name'] for f in draft['spec']['input_form']}
    assert {'amount', 'invoice_number', 'vendor', 'finance_owner'} <= inputs


def test_unknown_template_key_raises():
    with pytest.raises(ValueError, match='Unknown template'):
        create_draft_from_template('no-such-template')
    with pytest.raises(ValueError, match='Unknown template'):
        get_studio_template('no-such-template')


def test_get_studio_template_metadata():
    template = get_studio_template('invoice-approval')
    assert template['category'] == 'finance'
    assert template['steps'][0]['type'] == 'start'
    assert template['steps'][-1]['type'] == 'end'
    assert {i['name'] for i in template['required_inputs']} >= {'amount', 'vendor'}


def test_custom_name_and_description_override():
    draft = create_draft_from_template(
        'invoice-approval', name='  March vendor payments ', description='Custom')
    assert draft['name'] == 'March vendor payments'
    assert draft['description'] == 'Custom'
    default = create_draft_from_template('invoice-approval')
    assert default['name'] == 'Invoice approve karwao'


def test_draft_is_a_fresh_copy_each_call():
    first = create_draft_from_template('invoice-approval')
    first['spec']['nodes'][0]['label'] = 'MUTATED'
    first['spec']['input_form'].append(
        {'name': 'extra', 'label': 'Extra', 'type': 'text', 'required': False})
    second = create_draft_from_template('invoice-approval')
    assert second['spec']['nodes'][0]['label'] == 'Invoice received'
    assert 'extra' not in {f['name'] for f in second['spec']['input_form']}
    listed = [t for t in list_studio_templates() if t['key'] == 'invoice-approval'][0]
    assert listed['steps'][0]['label'] == 'Invoice received'
