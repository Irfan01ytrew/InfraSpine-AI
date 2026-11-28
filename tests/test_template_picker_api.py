"""Step AD slice 4: GET /api/workflow-engine/templates lists the picker metadata.

The picker needs category/steps/required inputs per template. The response shape
is preserved (key/name/description/spec) and gains only an additive `category`
from the slice-1 studio template library.
"""


def test_templates_list_has_category_and_spec(admin_client):
    response = admin_client.get('/api/workflow-engine/templates')
    assert response.status_code == 200, response.text
    body = response.json()
    templates = body['templates']
    assert len(templates) >= 12
    keys = {template['key'] for template in templates}
    assert 'invoice-approval' in keys
    assert len(keys) == len(templates)
    for template in templates:
        assert template['category'] in {'finance', 'hr', 'it', 'operations'}
        # Shape preserved: the picker still gets the full validated spec.
        assert template['spec']['nodes']
    invoice = next(t for t in templates if t['key'] == 'invoice-approval')
    assert invoice['category'] == 'finance'
    assert any(n['type'] == 'approval' for n in invoice['spec']['nodes'])
    assert 'adapters' in body and 'actions' in body
