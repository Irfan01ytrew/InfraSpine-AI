"""Ready-made custom roles that administrators (and industry templates) can add in one step.

Each entry is an ordinary role template definition; every right is still enforced on the server through
permission grants, so adding a library role grants nothing until it is assigned to someone.
"""

from __future__ import annotations

_ALL_CATEGORIES = ["policy", "financial", "legal", "client", "operational", "general"]


def _spec(permission: str, categories: list[str] | None = None) -> dict:
    return {"permission": permission, "categories": categories or []}


ROLE_LIBRARY: dict[str, dict] = {
    "hr_officer": {"name": "HR Officer", "base_role": "employee", "description": "Onboards new employees and follows usage.",
                   "permissions": [_spec("people.invite"), _spec("reports.view")]},
    "accountant": {"name": "Accountant", "base_role": "employee", "description": "Reviews financial knowledge and sees usage reports.",
                   "permissions": [_spec("knowledge.review", ["financial"]), _spec("reports.view")]},
    "auditor": {"name": "Auditor", "base_role": "employee", "description": "Read-only access to organization usage reports.",
                "permissions": [_spec("reports.view")]},
    "training_coordinator": {"name": "Training Coordinator", "base_role": "employee", "description": "Assigns training and follows progress.",
                             "permissions": [_spec("training.manage")]},
    "ai_agent_manager": {"name": "AI Agent Manager", "base_role": "employee", "description": "Creates and maintains department AI agents.",
                         "permissions": [_spec("agents.manage")]},
    "knowledge_reviewer": {"name": "Knowledge Reviewer", "base_role": "employee", "description": "Reviews policy and operational knowledge before publication.",
                           "permissions": [_spec("knowledge.review", ["policy", "operational", "general"])]},
    "legal_reviewer": {"name": "Legal Reviewer", "base_role": "employee", "description": "Reviews legal knowledge.",
                       "permissions": [_spec("knowledge.review", ["legal"])]},
    "knowledge_publisher": {"name": "Knowledge Publisher", "base_role": "manager", "description": "Publishes reviewed knowledge for the team.",
                            "permissions": [_spec("knowledge.publish", ["policy", "operational", "general"])]},
    "department_lead": {"name": "Department Lead", "base_role": "manager", "description": "Runs a team: invites, training, agents and reports.",
                        "permissions": [_spec("people.invite"), _spec("training.manage"), _spec("agents.manage"), _spec("reports.view")]},
    "data_steward": {"name": "Data Steward", "base_role": "manager", "description": "Inherits company knowledge when someone leaves.",
                     "permissions": [_spec("data.steward", _ALL_CATEGORIES)]},
    "compliance_officer": {"name": "Compliance Officer", "base_role": "employee", "description": "Reviews policy and legal knowledge and follows usage.",
                           "permissions": [_spec("knowledge.review", ["policy", "legal"]), _spec("reports.view")]},
    "quality_reviewer": {"name": "Quality Reviewer", "base_role": "employee", "description": "Reviews operating procedures and quality records.",
                         "permissions": [_spec("knowledge.review", ["operational", "policy"])]},
    "operations_supervisor": {"name": "Operations Supervisor", "base_role": "manager", "description": "Onboards and trains frontline staff.",
                              "permissions": [_spec("people.invite"), _spec("training.manage")]},
    "client_service_lead": {"name": "Client Service Lead", "base_role": "manager", "description": "Publishes client-facing knowledge and runs service agents.",
                            "permissions": [_spec("knowledge.publish", ["client"]), _spec("agents.manage")]},
    "procurement_officer": {"name": "Procurement Officer", "base_role": "employee", "description": "Reviews supplier, purchasing and financial procedures.",
                            "permissions": [_spec("knowledge.review", ["financial", "operational"])]},
}


def library_role(key: str) -> dict:
    return ROLE_LIBRARY[key]
