"""Studio template library: one-click validated workflow drafts.

Pure functions only — no DB, no network, no side effects. A draft produced
here is never published and never executed: review and publish stay
separate, owner-gated steps in ``app.workflows.service.WorkflowService``,
which re-validates scope and contracts before anything runs.

Templates themselves are data, never code: they come from the bounded
``app.workflows.templates`` catalog (validated ``Definition`` specs), and
this module only adds friendly metadata plus the draft payload shape
(``origin='studio'``, ``origin_ref=<template key>``, ``status='draft'``)
that ``wf_definitions`` expects.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.workflows.definition import Definition, validate_graph
from app.workflows.templates import template_catalog

CATEGORIES = {
    'procurement': 'finance',
    'invoice': 'finance',
    'invoice-approval': 'finance',
    'onboarding': 'hr',
    'resignation': 'hr',
    'offboarding': 'hr',
    'it-incident': 'it',
    'document-review': 'operations',
    'asset': 'operations',
    'customer-service': 'operations',
    'project': 'operations',
    'custom': 'operations',
}


def _catalog_by_key() -> dict[str, dict[str, Any]]:
    return {template['key']: template for template in template_catalog()}


def list_studio_templates() -> list[dict[str, Any]]:
    """Friendly metadata for every template; specs stay in the catalog."""
    templates = []
    for template in template_catalog():
        spec = template['spec']
        templates.append({
            'key': template['key'],
            'name': template['name'],
            'description': template['description'],
            'category': CATEGORIES.get(template['key'], 'operations'),
            'node_count': len(spec['nodes']),
            'steps': [{'id': n['id'], 'type': n['type'], 'label': n['label']}
                      for n in spec['nodes']],
            'required_inputs': [{'name': f['name'], 'label': f['label'], 'type': f['type']}
                                for f in spec.get('input_form', [])],
        })
    return templates


def get_studio_template(template_key: str) -> dict[str, Any]:
    """Metadata for one template; raises ValueError for an unknown key."""
    for template in list_studio_templates():
        if template['key'] == template_key:
            return template
    raise ValueError(f'Unknown template: {template_key}')


def create_draft_from_template(template_key: str, *, name: str | None = None,
                               description: str | None = None) -> dict[str, Any]:
    """Turn a template into a validated draft workflow definition.

    Returns a fresh payload every call (mutating it never corrupts the
    catalog). Raises ValueError for an unknown key or a template whose spec
    fails ``Definition`` / ``validate_graph`` checks. The draft is inert:
    ``status='draft'`` — nothing here saves, publishes, or executes it.
    """
    templates = _catalog_by_key()
    if template_key not in templates:
        raise ValueError(f'Unknown template: {template_key}')
    template = templates[template_key]
    spec = Definition.model_validate(deepcopy(template['spec']))
    errors = validate_graph(spec)
    if errors:
        raise ValueError(f'Template {template_key} failed validation: {errors}')
    return {
        'name': (name or '').strip() or template['name'],
        'description': (description or '').strip() or template['description'],
        'spec': spec.model_dump(mode='json'),
        'origin': 'studio',
        'origin_ref': template_key,
        'status': 'draft',
    }
