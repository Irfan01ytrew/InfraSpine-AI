"""Reviewed execution allowlist and acceptance contracts. Existence is not verification."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    (1, "Knowledge & AI Brain", "app/rag/chat.py", ["Answer accuracy", "Grounded citations", "Retrieval", "Multimodal processing", "Chat reliability"]),
    (2, "IRFANIX Master AI Agent", "app/agent_studio/graph.py", ["Real task execution", "Tool use", "Delegation", "Memory", "Verified outcomes"]),
    (3, "Company Intelligence Graph", "app/company_graph/service.py", ["Entity relationships", "Provenance", "Dependency analysis", "Graph permissions"]),
    (4, "Adaptive Process Intelligence", "app/process_intelligence/service.py", ["Process discovery", "Conformance", "Bottlenecks", "Evidence accuracy"]),
    (5, "Universal Workflow Engine", "app/services/automation_worker.py", ["Durable execution", "Action-bound approvals", "Retries", "Versioning", "Recovery"]),
    (6, "Governance & Security", "app/services/access_control.py", ["RBAC and ABAC", "Tenant isolation", "Agent authorization", "Audit integrity"]),
    (7, "Exception & Recovery", "app/exception_recovery/service.py", ["Detection", "Diagnosis", "Bounded recovery", "Incident verification"]),
    (8, "Business Continuity", "app/business_continuity/service.py", ["Impact analysis", "Handovers", "Continuity plans", "Recovery coordination"]),
    (9, "Enterprise Frontend & UX", "frontend/src/App.tsx", ["Functional interfaces", "Responsive design", "Accessibility", "Real data"]),
    (10, "Integrations & Communication", "app/services/webhooks.py", ["Webhooks", "WhatsApp", "Email", "External APIs", "Synchronization", "Delivery verification"]),
    (11, "Infrastructure & Deployment", "compose.onprem.yaml", ["Installation", "Service health", "Persistence", "Backups", "Restore", "Operational reliability"]),
    (12, "QA & Benchmarking", "app/qa/service.py", ["Discover and execute tests", "API and PostgreSQL integration", "Authenticated browser screenshots", "Actual RAG answer evaluation", "Verified agent outcomes", "Injected defect detection", "Performance regression detection", "Unauthorized access rejection", "Accurate result classification", "Dashboard real results", "Release-readiness report", "Rerun affected tests", "Reproducibility and history", "Production data safety", "Twelve-module traceability"]),
]

# Only these repository-owned commands can execute. No submitted command, path, URL or argument.
SUITES = {
    "qa-unit": {"name": "QA scoring, evidence and release gates", "kind": "unit", "module": 12,
        "targets": ["tests/unit/test_qa_domain.py"], "timeout": 90,
        "verifies": ["12.06", "12.07", "12.09", "12.11", "12.15"]},
    "qa-integration": {"name": "QA API, PostgreSQL, isolation and worker recovery", "kind": "integration", "module": 12,
        "targets": ["tests/test_qa_platform.py"], "timeout": 600,
        "verifies": ["12.02", "12.08", "12.12", "12.13"]},
    "security": {"name": "Authentication and access-control regression", "kind": "integration", "module": 6,
        "targets": ["tests/test_auth.py", "tests/test_access_control.py", "tests/test_database_safety.py"], "timeout": 900, "verifies": []},
    "rag": {"name": "RAG regression (deterministic provider)", "kind": "integration", "module": 1,
        "targets": ["tests/test_rag.py", "tests/test_conversations.py", "tests/test_eval_gate.py", "tests/test_answer_eval.py"], "timeout": 1200, "verifies": []},
    "workflow": {"name": "Workflow, queue and approval regression", "kind": "integration", "module": 5,
        "targets": ["tests/test_automation.py", "tests/test_agent_queue.py", "tests/test_agent_actions_approval.py"], "timeout": 1200, "verifies": []},
    "operations": {"name": "Deployment, health and webhook contracts", "kind": "integration", "module": 11,
        "targets": ["tests/test_deployment_templates.py", "tests/test_health_readiness_unit.py", "tests/test_webhooks.py", "tests/test_worker_healthcheck.py"], "timeout": 900, "verifies": []},
    "qa-browser": {"name": "Authenticated Quality Command Center", "kind": "browser", "module": 12,
        "targets": ["tests/test_qa_browser.py"], "timeout": 300, "verifies": ["12.03", "12.10"]},
}

def requirements():
    return [{"id": f"{number:02}.{i:02}", "module": number, "module_name": name, "title": title,
             "mandatory": True, "source": source, "implemented": (ROOT / source).exists()}
            for number, name, source, titles in MODULES for i, title in enumerate(titles, 1)]

def catalog():
    return [{"id": key, **value} for key, value in SUITES.items()]
