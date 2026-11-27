import uuid
from datetime import UTC, datetime
from io import BytesIO

import pytest
from pydantic import ValidationError

from app.schemas.proposal_coverage import (
    ProposalCoverageCreate,
    ProposalCoverageDetail,
    ProposalCoverageReviewRequest,
    ProposalRequirementOut,
    ProposalRequirementExtractRequest,
    ProposalRequirementExtractionDraft,
    ProposalRequirementIn,
)
from app.schemas.deployment import CompanyProfile
from app.core.errors import InvalidInputError
from app.main import required_license_feature
from app.services.proposal_coverage import ProposalCoverageService, validate_requirement_assessment
from app.services.proposal_coverage_export import render_proposal_coverage_export


def test_requirements_are_trimmed_bounded_and_duplicate_free():
    payload = ProposalCoverageCreate(
        title="  Network services  ",
        requirements=[{"requirement": "  Provide a 24/7 support desk.  "}, {"requirement": "Document escalation."}],
    )
    assert payload.title == "Network services"
    assert [item.requirement for item in payload.requirements] == [
        "Provide a 24/7 support desk.",
        "Document escalation.",
    ]
    with pytest.raises(ValidationError, match="duplicate requirements"):
        ProposalCoverageCreate(
            title="Repeated", requirements=[{"requirement": "Provide support."}, {"requirement": " provide support. "}]
        )
    with pytest.raises(ValidationError):
        ProposalRequirementIn(requirement="tiny")
    with pytest.raises(ValidationError):
        ProposalCoverageCreate(title="Too many", requirements=[{"requirement": f"requirement {i} text"} for i in range(31)])
    with pytest.raises(ValidationError, match="only once"):
        ProposalRequirementIn(requirement="Retain the operating records.", source_chunk_ids=[1, 1])
    with pytest.raises(ValidationError):
        ProposalRequirementIn(requirement="Retain the operating records.", source_chunk_ids=["1"])


def test_review_rationale_is_normalized_and_required():
    request = ProposalCoverageReviewRequest(decision="approved", rationale="  Checked   against evidence. ")
    assert request.rationale == "Checked against evidence."
    with pytest.raises(ValidationError):
        ProposalCoverageReviewRequest(decision="approved", rationale="ok")


def test_proposal_routes_use_the_existing_research_entitlement():
    assert required_license_feature("/api/proposal-coverage") == "research"
    assert required_license_feature("/api/proposal-coverage/options") == "research"
    assert required_license_feature("/api/proposal-coverage/123/review") == "research"
    assert required_license_feature("/api/proposal-coverage/extract-requirements") == "research"
    assert required_license_feature("/api/proposal-coverage/123/export") == "research"


def test_rfp_extraction_inputs_are_bounded_unique_and_require_strict_source_ids():
    document_id = uuid.uuid4()
    request = ProposalRequirementExtractRequest(document_ids=[document_id])
    assert request.document_ids == [document_id]
    with pytest.raises(ValidationError, match="only once"):
        ProposalRequirementExtractRequest(document_ids=[document_id, document_id])
    with pytest.raises(ValidationError):
        ProposalRequirementExtractRequest(document_ids=[uuid.uuid4() for _ in range(6)])

    draft = ProposalRequirementExtractionDraft.model_validate({
        "requirements": [{
            "requirement": "Submit an annual service continuity plan.",
            "evidence_chunk_ids": [123],
        }]
    })
    assert draft.requirements[0].evidence_chunk_ids == [123]
    with pytest.raises(ValidationError):
        ProposalRequirementExtractionDraft.model_validate({
            "requirements": [{
                "requirement": "Submit an annual service continuity plan.",
                "evidence_chunk_ids": ["123"],
            }]
        })
    with pytest.raises(ValidationError):
        ProposalRequirementExtractionDraft.model_validate({
            "requirements": [{
                "requirement": "Submit an annual service continuity plan.",
                "evidence_chunk_ids": [123],
                "extra": "must not be accepted",
            }]
        })


def test_citation_freshness_requires_exact_source_version_and_digest():
    document_id = uuid.uuid4()
    current = {document_id: (4, "a" * 64)}
    citation = {"document_id": str(document_id), "document_version": 4, "document_sha256": "a" * 64}
    assert ProposalCoverageService._citation_current(citation, current)
    assert not ProposalCoverageService._citation_current({**citation, "document_version": 3}, current)
    assert not ProposalCoverageService._citation_current({**citation, "document_sha256": "b" * 64}, current)
    assert not ProposalCoverageService._citation_current({**citation, "document_id": str(uuid.uuid4())}, current)


def test_human_meets_decision_cannot_override_an_insufficient_evidence_result():
    assert validate_requirement_assessment(
        "partial", "Some evidence supports a portion.", has_citations=True, insufficient_evidence=True
    ) == "Some evidence supports a portion."
    assert validate_requirement_assessment(
        "gap", "No internal source was found.", has_citations=False, insufficient_evidence=True
    ) == "No internal source was found."
    with pytest.raises(InvalidInputError, match="AI marked evidence as insufficient"):
        validate_requirement_assessment(
            "meets", "No citation supports all parts.", has_citations=True, insufficient_evidence=True
        )
    with pytest.raises(InvalidInputError, match="requires at least one source citation"):
        validate_requirement_assessment(
            "partial", "Evidence is incomplete.", has_citations=False, insufficient_evidence=True
        )


@pytest.mark.parametrize("format,media_type,signature", [
    ("pdf", "application/pdf", b"%PDF-"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04"),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04"),
])
def test_proposal_coverage_exports_are_branded_and_formula_safe(format, media_type, signature):
    now = datetime(2026, 10, 26, tzinfo=UTC)
    case_id = uuid.uuid4()
    case = ProposalCoverageDetail(
        id=case_id,
        title="Synthetic proposal",
        status="approved",
        revision_number=2,
        owner_user_id=uuid.uuid4(),
        owner_email="owner@example.test",
        department_group_id=None,
        department_name="Operations",
        requirement_count=1,
        analyzed_count=1,
        assessment_count=1,
        created_at=now,
        updated_at=now,
        submitted_at=now,
        decided_at=now,
        requirements=[ProposalRequirementOut(
            id=uuid.uuid4(),
            position=1,
            requirement='=HYPERLINK("https://invalid.example")',
            answer="Saved finding withheld because its source authorization or version changed.",
            insufficient_evidence=True,
            retrieved_chunks=0,
            analyzed_at=now,
            evidence_current=False,
            citations=[],
            origin_evidence=[],
            origin_evidence_current=False,
            assessment="gap",
            assessment_rationale="The source is no longer available.",
            assessed_by_user_id=uuid.uuid4(),
            assessed_at=now,
        )],
        reviews=[],
        can_edit=False,
        can_submit=False,
        can_review=False,
    )
    profile = CompanyProfile(
        company_name="Example Infrastructure",
        industry="Infrastructure",
        brand_name="Example",
        primary_color="#146B61",
    )

    content, actual_type, filename = render_proposal_coverage_export(case, profile, format)
    assert content.startswith(signature)
    assert actual_type == media_type
    assert filename.endswith(f".{format}") and str(case_id)[:8] in filename
    if format == "docx":
        from docx import Document

        word_file = Document(BytesIO(content))
        exported_text = "\n".join(
            [paragraph.text for paragraph in word_file.paragraphs]
            + [cell.text for table in word_file.tables for row in table.rows for cell in row.cells]
        )
        assert "Example" in exported_text and "Synthetic proposal" in exported_text
        assert "withheld" in exported_text.lower()
    if format == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(content), data_only=False)
        assert workbook["Requirements"]["A2"].value == '=HYPERLINK("https://invalid.example")'
        assert workbook["Requirements"]["A2"].data_type == "s"
        assert "withheld" in workbook["Requirements"]["E2"].value.lower()
