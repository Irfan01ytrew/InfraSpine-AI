"""Versioned deployment starter templates; they never contain customer data or permissions."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.db.models import AccessGroup, RoleTemplate, User
from app.schemas.deployment import IndustryTemplateApplyOut, IndustryTemplateOut
from app.services import audit
from app.services.audit import AuditContext
from app.services.role_library import ROLE_LIBRARY


# Each template: departments, knowledge areas and ready-made custom roles (keys of app.services.role_library).
# `previous_versions` keeps company profiles saved against an earlier version of a template valid.
_TEMPLATES: tuple[dict, ...] = (
    {'template_id': 'general', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'General organization', 'industry': 'General', 'description': 'A neutral starting point for organizations that want to define their own structure.', 'default_departments': ['Operations', 'People', 'Finance', 'Risk and Compliance'], 'knowledge_areas': ['Policies', 'Process documentation', 'Projects', 'Training'], 'default_roles': ['hr_officer', 'accountant', 'knowledge_reviewer', 'department_lead']},
    {'template_id': 'consulting', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Consulting and professional services', 'industry': 'Consulting', 'description': 'Starter departments for client delivery and reusable professional knowledge.', 'default_departments': ['Client Delivery', 'Methods and Knowledge', 'Sales and Proposals', 'Operations'], 'knowledge_areas': ['Engagement methods', 'Proposal evidence', 'Client delivery', 'Lessons learned'], 'default_roles': ['client_service_lead', 'knowledge_publisher', 'knowledge_reviewer', 'ai_agent_manager']},
    {'template_id': 'real_estate', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Real estate and property operations', 'industry': 'Real estate', 'description': 'Starter departments for property operations; configure local legal and privacy policies separately.', 'default_departments': ['Properties and Facilities', 'Leasing and Sales', 'Finance', 'Operations'], 'knowledge_areas': ['Property records', 'Leasing process', 'Maintenance', 'Customer service'], 'default_roles': ['client_service_lead', 'accountant', 'legal_reviewer', 'operations_supervisor']},
    {'template_id': 'healthcare', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Healthcare operations', 'industry': 'Healthcare', 'description': 'Operational starter only; it does not certify regulatory compliance or authorize patient data processing.', 'default_departments': ['Clinical Operations', 'Patient Services', 'Quality and Compliance', 'Administration'], 'knowledge_areas': ['Approved procedures', 'Quality processes', 'Facilities', 'Staff training'], 'default_roles': ['compliance_officer', 'quality_reviewer', 'training_coordinator', 'hr_officer']},
    {'template_id': 'education', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Education', 'industry': 'Education', 'description': 'Starter departments for teaching, learner services and administration.', 'default_departments': ['Teaching and Learning', 'Student Services', 'Administration', 'Quality and Compliance'], 'knowledge_areas': ['Curriculum', 'Learner services', 'Operations', 'Staff development'], 'default_roles': ['training_coordinator', 'knowledge_publisher', 'hr_officer', 'quality_reviewer']},
    {'template_id': 'manufacturing', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Manufacturing', 'industry': 'Manufacturing', 'description': 'Starter departments for production, quality and supply operations.', 'default_departments': ['Production', 'Quality and Safety', 'Maintenance', 'Supply Chain'], 'knowledge_areas': ['Work instructions', 'Quality records', 'Equipment', 'Safety procedures'], 'default_roles': ['quality_reviewer', 'operations_supervisor', 'procurement_officer', 'training_coordinator']},
    {'template_id': 'finance', 'version': '1.1.0', 'previous_versions': ['1.0.0'], 'name': 'Financial services', 'industry': 'Financial services', 'description': 'Operational starter only; configure and review your own regulatory controls.', 'default_departments': ['Client Services', 'Risk and Compliance', 'Finance Operations', 'Products'], 'knowledge_areas': ['Approved procedures', 'Products', 'Client service', 'Control evidence'], 'default_roles': ['compliance_officer', 'accountant', 'auditor', 'client_service_lead']},
    {'template_id': 'retail', 'version': '1.0.0', 'previous_versions': [], 'name': 'Retail', 'industry': 'Retail', 'description': 'Starter departments for stores, merchandising and customer service.', 'default_departments': ['Store Operations', 'Merchandising', 'Customer Service', 'Finance', 'People'], 'knowledge_areas': ['Store procedures', 'Product information', 'Returns and refunds', 'Staff onboarding'], 'default_roles': ['operations_supervisor', 'client_service_lead', 'accountant', 'hr_officer']},
    {'template_id': 'ecommerce', 'version': '1.0.0', 'previous_versions': [], 'name': 'E-commerce', 'industry': 'E-commerce', 'description': 'Starter departments for online catalog, fulfilment and customer care.', 'default_departments': ['Catalog and Content', 'Fulfilment', 'Customer Care', 'Marketing', 'Finance'], 'knowledge_areas': ['Product catalog', 'Shipping and returns', 'Customer care scripts', 'Promotions'], 'default_roles': ['client_service_lead', 'operations_supervisor', 'accountant', 'ai_agent_manager']},
    {'template_id': 'wholesale_distribution', 'version': '1.0.0', 'previous_versions': [], 'name': 'Wholesale and distribution', 'industry': 'Wholesale and distribution', 'description': 'Starter departments for purchasing, warehousing and account management.', 'default_departments': ['Purchasing', 'Warehouse', 'Sales and Accounts', 'Finance'], 'knowledge_areas': ['Supplier terms', 'Warehouse procedures', 'Price lists', 'Credit control'], 'default_roles': ['procurement_officer', 'operations_supervisor', 'accountant', 'client_service_lead']},
    {'template_id': 'logistics', 'version': '1.0.0', 'previous_versions': [], 'name': 'Logistics and transportation', 'industry': 'Logistics', 'description': 'Starter departments for fleet, dispatch, warehousing and safety.', 'default_departments': ['Dispatch and Planning', 'Fleet and Maintenance', 'Warehouse', 'Health and Safety', 'Customer Service'], 'knowledge_areas': ['Route procedures', 'Vehicle maintenance', 'Safety rules', 'Customer service'], 'default_roles': ['operations_supervisor', 'quality_reviewer', 'training_coordinator', 'client_service_lead']},
    {'template_id': 'construction', 'version': '1.0.0', 'previous_versions': [], 'name': 'Construction and engineering', 'industry': 'Construction', 'description': 'Starter departments for projects, site safety and procurement.', 'default_departments': ['Projects', 'Site Operations', 'Health and Safety', 'Procurement', 'Finance'], 'knowledge_areas': ['Method statements', 'Safety procedures', 'Contracts', 'Lessons learned'], 'default_roles': ['quality_reviewer', 'procurement_officer', 'operations_supervisor', 'legal_reviewer']},
    {'template_id': 'hospitality', 'version': '1.0.0', 'previous_versions': [], 'name': 'Hospitality and tourism', 'industry': 'Hospitality', 'description': 'Starter departments for guest services, food and beverage, and housekeeping.', 'default_departments': ['Front Office', 'Food and Beverage', 'Housekeeping', 'Sales and Events', 'People'], 'knowledge_areas': ['Service standards', 'Guest policies', 'Hygiene procedures', 'Staff training'], 'default_roles': ['operations_supervisor', 'training_coordinator', 'client_service_lead', 'hr_officer']},
    {'template_id': 'food_beverage', 'version': '1.0.0', 'previous_versions': [], 'name': 'Food and beverage production', 'industry': 'Food and beverage', 'description': 'Starter departments for production, food safety and distribution; it does not certify food-safety compliance.', 'default_departments': ['Production', 'Food Safety and Quality', 'Supply Chain', 'Sales'], 'knowledge_areas': ['Recipes and specifications', 'Hygiene procedures', 'Supplier approvals', 'Recalls'], 'default_roles': ['quality_reviewer', 'procurement_officer', 'operations_supervisor', 'compliance_officer']},
    {'template_id': 'legal', 'version': '1.0.0', 'previous_versions': [], 'name': 'Legal services', 'industry': 'Legal', 'description': 'Starter departments for practice groups and knowledge management; privilege and confidentiality rules must be configured by the firm.', 'default_departments': ['Practice Groups', 'Knowledge Management', 'Business Development', 'Operations'], 'knowledge_areas': ['Precedents', 'Practice notes', 'Matter procedures', 'Regulatory updates'], 'default_roles': ['legal_reviewer', 'knowledge_publisher', 'compliance_officer', 'data_steward']},
    {'template_id': 'insurance', 'version': '1.0.0', 'previous_versions': [], 'name': 'Insurance', 'industry': 'Insurance', 'description': 'Operational starter only; configure your own regulatory controls.', 'default_departments': ['Underwriting', 'Claims', 'Customer Service', 'Risk and Compliance', 'Finance'], 'knowledge_areas': ['Underwriting guidelines', 'Claims procedures', 'Product wording', 'Complaints handling'], 'default_roles': ['compliance_officer', 'client_service_lead', 'accountant', 'auditor']},
    {'template_id': 'banking', 'version': '1.0.0', 'previous_versions': [], 'name': 'Banking', 'industry': 'Banking', 'description': 'Operational starter only; it does not certify regulatory compliance.', 'default_departments': ['Retail Banking', 'Operations', 'Risk and Compliance', 'Treasury and Finance', 'Customer Service'], 'knowledge_areas': ['Product terms', 'KYC and onboarding procedures', 'Operations manuals', 'Control evidence'], 'default_roles': ['compliance_officer', 'auditor', 'accountant', 'client_service_lead']},
    {'template_id': 'pharmaceuticals', 'version': '1.0.0', 'previous_versions': [], 'name': 'Pharmaceuticals and life sciences', 'industry': 'Pharmaceuticals', 'description': 'Operational starter only; GxP validation and regulatory approval are outside this template.', 'default_departments': ['Research and Development', 'Quality Assurance', 'Regulatory Affairs', 'Manufacturing', 'Medical Affairs'], 'knowledge_areas': ['SOPs', 'Quality records', 'Regulatory submissions', 'Training records'], 'default_roles': ['quality_reviewer', 'compliance_officer', 'training_coordinator', 'data_steward']},
    {'template_id': 'agriculture', 'version': '1.0.0', 'previous_versions': [], 'name': 'Agriculture', 'industry': 'Agriculture', 'description': 'Starter departments for farm operations, agronomy and supply.', 'default_departments': ['Farm Operations', 'Agronomy', 'Procurement and Inputs', 'Sales and Distribution'], 'knowledge_areas': ['Crop procedures', 'Equipment', 'Supplier terms', 'Safety'], 'default_roles': ['operations_supervisor', 'procurement_officer', 'training_coordinator', 'accountant']},
    {'template_id': 'energy_utilities', 'version': '1.0.0', 'previous_versions': [], 'name': 'Energy and utilities', 'industry': 'Energy and utilities', 'description': 'Starter departments for network operations, safety and customer service.', 'default_departments': ['Network Operations', 'Field Services', 'Health and Safety', 'Customer Service', 'Regulatory'], 'knowledge_areas': ['Operating procedures', 'Safety rules', 'Asset records', 'Customer policies'], 'default_roles': ['quality_reviewer', 'operations_supervisor', 'compliance_officer', 'client_service_lead']},
    {'template_id': 'technology', 'version': '1.0.0', 'previous_versions': [], 'name': 'Technology and telecom', 'industry': 'Technology', 'description': 'Starter departments for product, engineering, support and sales.', 'default_departments': ['Product', 'Engineering', 'Customer Support', 'Sales', 'Security'], 'knowledge_areas': ['Product documentation', 'Runbooks', 'Support articles', 'Security policies'], 'default_roles': ['ai_agent_manager', 'knowledge_publisher', 'client_service_lead', 'compliance_officer']},
    {'template_id': 'media_entertainment', 'version': '1.0.0', 'previous_versions': [], 'name': 'Media and entertainment', 'industry': 'Media', 'description': 'Starter departments for content, production and rights management.', 'default_departments': ['Content and Editorial', 'Production', 'Rights and Legal', 'Marketing'], 'knowledge_areas': ['Editorial guidelines', 'Production procedures', 'Rights records', 'Brand guidelines'], 'default_roles': ['knowledge_publisher', 'legal_reviewer', 'ai_agent_manager', 'operations_supervisor']},
    {'template_id': 'research', 'version': '1.0.0', 'previous_versions': [], 'name': 'Research organization', 'industry': 'Research', 'description': 'Starter departments for research programs, data management and grants.', 'default_departments': ['Research Programs', 'Data Management', 'Grants and Finance', 'Ethics and Compliance'], 'knowledge_areas': ['Protocols', 'Data management plans', 'Publications', 'Grant procedures'], 'default_roles': ['data_steward', 'compliance_officer', 'knowledge_reviewer', 'accountant']},
    {'template_id': 'government', 'version': '1.0.0', 'previous_versions': [], 'name': 'Government and public sector', 'industry': 'Government', 'description': 'Starter departments for public services and administration; records, transparency and data-protection rules must be configured per jurisdiction.', 'default_departments': ['Public Services', 'Policy', 'Administration', 'Finance', 'Records and Information'], 'knowledge_areas': ['Policies and circulars', 'Service procedures', 'Citizen guidance', 'Records management'], 'default_roles': ['compliance_officer', 'knowledge_publisher', 'data_steward', 'auditor']},
    {'template_id': 'ngo', 'version': '1.0.0', 'previous_versions': [], 'name': 'NGO and non-profit', 'industry': 'Non-profit', 'description': 'Starter departments for programs, fundraising and finance.', 'default_departments': ['Programs', 'Fundraising and Partnerships', 'Finance', 'Monitoring and Evaluation', 'People'], 'knowledge_areas': ['Program procedures', 'Donor requirements', 'Safeguarding', 'Reports'], 'default_roles': ['accountant', 'auditor', 'training_coordinator', 'hr_officer']},
)


def _out(item: dict) -> IndustryTemplateOut:
    return IndustryTemplateOut(
        **{key: item[key] for key in ("template_id", "version", "name", "industry", "description", "default_departments", "knowledge_areas")},
        default_roles=[ROLE_LIBRARY[key]["name"] for key in item["default_roles"]],
    )


def list_industry_templates() -> list[IndustryTemplateOut]:
    return [_out(item) for item in _TEMPLATES]


def _raw_template(template_id: str) -> dict | None:
    return next((item for item in _TEMPLATES if item["template_id"] == template_id), None)


def get_industry_template(template_id: str) -> IndustryTemplateOut | None:
    value = _raw_template(template_id)
    return _out(value) if value else None


def validate_profile_template(template_id: str | None, version: str | None) -> None:
    if template_id is None:
        return
    template = _raw_template(template_id)
    if template is None or (version != template["version"] and version not in template["previous_versions"]):
        raise ValueError("The selected industry template version is unavailable. Reload the template catalog.")


async def apply_industry_template(
    db: AsyncSession,
    admin: User,
    ctx: AuditContext,
    template_id: str,
) -> IndustryTemplateApplyOut:
    template = get_industry_template(template_id)
    if template is None:
        raise InvalidInputError("The requested industry template is not available.")

    created: list[str] = []
    existing: list[str] = []
    for name in template.default_departments:
        result = await db.execute(
            insert(AccessGroup)
            .values(
                id=uuid.uuid4(),
                name=name,
                kind="department",
                source="local",
                created_by=admin.id,
            )
            .on_conflict_do_nothing(index_elements=[AccessGroup.kind, func.lower(AccessGroup.name)])
        )
        (created if result.rowcount else existing).append(name)

    await db.flush()
    # Ready-made custom roles for this industry; existing roles with the same name are left untouched.
    from app.services.role_templates import RoleTemplateService

    created_roles: list[str] = []
    existing_roles: list[str] = []
    existing_names = {name.lower() for name in (await db.execute(select(RoleTemplate.name))).scalars()}
    for key in _raw_template(template_id)["default_roles"]:
        role = ROLE_LIBRARY[key]
        if role["name"].lower() in existing_names:
            existing_roles.append(role["name"])
            continue
        await RoleTemplateService(db, admin, ctx).create_template(role["name"], role["description"], role["base_role"], role["permissions"])
        created_roles.append(role["name"])
        existing_names.add(role["name"].lower())

    audit.record(
        db,
        ctx,
        "deployment.industry_template_applied",
        target=template.template_id,
        details={
            "version": template.version,
            "created_department_count": len(created),
            "existing_department_count": len(existing),
            "created_role_count": len(created_roles),
            "existing_role_count": len(existing_roles),
        },
    )
    await db.commit()
    return IndustryTemplateApplyOut(
        template_id=template.template_id,
        version=template.version,
        created_departments=created,
        existing_departments=existing,
        created_roles=created_roles,
        existing_roles=existing_roles,
    )
