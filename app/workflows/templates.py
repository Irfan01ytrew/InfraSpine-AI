"""Configurable templates containing executable definitions, never tenant records."""

from app.workflows.definition import Definition


def node(id, kind, label, x, y, **kw):
    return {'id': id, 'type': kind, 'label': label, 'x': x, 'y': y, **kw}


def human(id, label, owner, x, y):
    return node(id, 'human', label, x, y, config={'assignee_variable': f'input.{owner}',
        'form': [{'name': 'evidence', 'label': 'Completion evidence', 'type': 'text', 'required': True}]})


def edge(source, target, branch='always'):
    return {'source': source, 'target': target, 'branch': branch}


def template_catalog():
    templates = []
    def add(key, name, description, nodes, edges, owners, extra_fields=None):
        spec = Definition.model_validate({'source': 'template', 'nodes': nodes, 'edges': edges,
            'input_form': [{'name': key, 'label': label, 'type': 'text', 'required': True} for key, label in owners]
                + (extra_fields or [])}).model_dump(mode='json')
        templates.append({'key': key, 'name': name, 'description': description, 'spec': spec})
    add('onboarding', 'Employee onboarding', 'Parallel HR, IT and Administration checklists with verified completion.', [
        node('start', 'start', 'Employee approved', 80, 230),
        node('parallel', 'parallel', 'Prepare in parallel', 340, 230),
        human('hr', 'HR orientation', 'hr_owner', 600, 80),
        human('it', 'IT access checklist', 'it_owner', 600, 230),
        human('admin', 'Administration setup', 'admin_owner', 600, 380),
        node('join', 'parallel', 'All teams ready', 860, 230),
        node('end', 'end', 'Onboarding complete', 1120, 230),
    ], [edge('start', 'parallel'), *[edge('parallel', n) for n in ['hr', 'it', 'admin']],
        *[edge(n, 'join') for n in ['hr', 'it', 'admin']], edge('join', 'end')],
        [('employee', 'Employee name'), ('hr_owner', 'HR owner user ID'), ('it_owner', 'IT owner user ID'), ('admin_owner', 'Administration owner user ID')])
    add('resignation', 'Resignation and handover', 'Dependency review, recorded handover, independent approval and verified closure.', [
        node('start', 'start', 'Resignation received', 60, 160),
        human('dependencies', 'Review dependencies', 'manager_id', 320, 160),
        human('handover', 'Prepare handover evidence', 'employee_id', 580, 160),
        node('approval', 'approval', 'Authorize continuity plan', 840, 160, risk='high'),
        human('verify', 'Verify transferred responsibilities', 'successor_id', 1100, 160),
        node('end', 'end', 'Continuity verified', 1360, 160),
    ], [edge(a, b) for a, b in zip(['start', 'dependencies', 'handover', 'approval', 'verify'],
                                  ['dependencies', 'handover', 'approval', 'verify', 'end'])],
        [('employee_id', 'Departing employee user ID'), ('manager_id', 'Dependency reviewer user ID'), ('successor_id', 'Successor user ID')])
    add('procurement', 'Procurement and purchase approval', 'Manager approval constrained by existing financial authority; fulfillment remains a human task.', [
        node('start', 'start', 'Purchase requested', 80, 160),
        node('approval', 'approval', 'Purchase authorization', 340, 160, risk='high',
             config={'financial_category': 'purchase', 'amount_variable': 'input.amount', 'currency': 'USD'}),
        human('fulfill', 'Verify authorized procurement', 'buyer_id', 600, 160),
        node('end', 'end', 'Purchase recorded', 860, 160),
    ], [edge('start', 'approval'), edge('approval', 'fulfill'), edge('fulfill', 'end')],
        [('buyer_id', 'Procurement owner user ID')], [{'name': 'amount', 'label': 'Amount (USD; change currency in approval policy)', 'type': 'number', 'required': True}])
    for key, name, action in [
        ('offboarding', 'Employee offboarding', 'Verify access, assets and retained records'),
        ('it-incident', 'IT incident resolution', 'Diagnose, remediate and verify incident'),
        ('invoice', 'Invoice review and payment authorization', 'Verify invoice evidence and obtain payment authority'),
        ('document-review', 'Document and contract review', 'Review document and record evidence'),
        ('asset', 'Asset allocation and maintenance', 'Inspect, allocate or maintain asset'),
        ('customer-service', 'Customer complaints and service', 'Investigate and verify resolution'),
        ('project', 'Project coordination and reporting', 'Complete project task and report outcome'),
        ('custom', 'Custom department workflow', 'Complete configured department task'),
    ]:
        financial = key == 'invoice'
        approval_config = {'financial_category': 'expense', 'amount_variable': 'input.amount', 'currency': 'USD'} if financial else {}
        add(key, name, 'Configure owners, forms, deadlines and policy before review.', [
            node('start', 'start', 'Request received', 80, 160),
            human('work', action, 'owner_id', 340, 160),
            node('review', 'approval', 'Independent review', 600, 160, risk='high' if financial else 'medium', config=approval_config),
            node('end', 'end', 'Verified completion', 860, 160),
        ], [edge('start', 'work'), edge('work', 'review'), edge('review', 'end')], [('owner_id', 'Task owner user ID')],
            [{'name': 'amount', 'label': 'Amount (USD)', 'type': 'number', 'required': True}] if financial else [])
    return templates
