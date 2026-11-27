"""The invoice batch workflow template (system definition on the unified runtime)."""

from __future__ import annotations

from app.workflows.definition import Definition

TEMPLATE_REF = 'finance.invoice_batch'
TEMPLATE_NAME = 'Invoice verification and payment'
TEMPLATE_DESCRIPTION = ('Verify a batch of invoices against PO, goods receipt, vendor master and duplicates; request '
                        'approval of the exact payments; transfer through the payment connector with read-back.')


def invoice_spec() -> dict:
    values = {'batch_id': '$input.batch_id', 'round': '$input.round'}
    return Definition.model_validate({
        'source': 'template',
        'input_form': [{'name': 'batch_id', 'label': 'Invoice batch', 'type': 'text', 'required': True},
                       {'name': 'round', 'label': 'Round', 'type': 'number', 'required': True}],
        'nodes': [
            {'id': 'start', 'type': 'start', 'label': 'Invoices ingested', 'x': 60, 'y': 180},
            {'id': 'verify', 'type': 'action', 'label': 'Verify invoices (3-way match)', 'x': 260, 'y': 180,
             'risk': 'medium', 'timeout_seconds': 600, 'max_attempts': 5, 'deadline_seconds': 604800,
             'config': {'handler': 'finance.verify_invoices', 'permission': 'invoice.verify', 'values': values}},
            {'id': 'approve', 'type': 'action', 'label': 'Approval of exact payments', 'x': 480, 'y': 180,
             'risk': 'high', 'timeout_seconds': 300, 'max_attempts': 5, 'deadline_seconds': 604800,
             'config': {'handler': 'finance.request_payment_approvals', 'auto': True, 'values': values}},
            {'id': 'anything', 'type': 'decision', 'label': 'Anything approved to pay?', 'x': 700, 'y': 180,
             'config': {'condition': {'path': 'steps.approve.approved', 'operator': 'gt', 'value': 0}}},
            {'id': 'pay', 'type': 'action', 'label': 'Transfer funds (read-back verified)', 'x': 920, 'y': 120,
             'risk': 'critical', 'timeout_seconds': 900, 'max_attempts': 5, 'deadline_seconds': 604800,
             'config': {'handler': 'finance.execute_payments', 'permission': 'funds.transfer', 'values': values}},
            {'id': 'end', 'type': 'end', 'label': 'Batch closed', 'x': 1140, 'y': 180},
        ],
        'edges': [
            {'source': 'start', 'target': 'verify'}, {'source': 'verify', 'target': 'approve'},
            {'source': 'approve', 'target': 'anything'},
            {'source': 'anything', 'target': 'pay', 'branch': 'true'},
            {'source': 'anything', 'target': 'end', 'branch': 'false'},
            {'source': 'pay', 'target': 'end'},
        ],
    }).model_dump(mode='json')
