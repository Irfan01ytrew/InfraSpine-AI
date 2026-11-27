"""Persistent source-version finance drafts, reviewer corrections, and bookkeeping CSV handoff."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    DOC_READY,
    ROLE_ADMIN,
    ROLE_MANAGER,
    Document,
    FinancialFormRecord,
    FinancialFormReviewRevision,
    User,
)
from app.rag.access import readable_by
from app.schemas.financial_forms import (
    FinancialFormReviewQueueItem,
    FinancialFormReviewQueueOut,
    FinancialFormReviewRequest,
    FinancialFormReviewRevisionOut,
)
from app.services import audit
from app.services.audit import AuditContext

_AMOUNT = re.compile(r"^-?\d+(?:\.\d{1,4})?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SUPPORTED_CURRENCIES = {"PKR", "USD", "EUR", "GBP", "INR", "AED", "SAR", "CAD", "AUD", "JPY"}
MAX_BOOKKEEPING_EXPORT_RECORDS = 500
MAX_BOOKKEEPING_EXPORT_ROWS = 25_000


class FinancialFormReviewService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def get_record(
        self, document_id: uuid.UUID, document_version: int, source_sha256: str, profile: str, date_order: str
    ) -> FinancialFormRecord | None:
        record = await self.db.scalar(select(FinancialFormRecord).where(
            FinancialFormRecord.document_id == document_id,
            FinancialFormRecord.document_version == document_version,
            FinancialFormRecord.source_sha256 == source_sha256,
            FinancialFormRecord.profile == profile,
            FinancialFormRecord.date_order == date_order,
        ))
        return record

    async def save_draft(
        self,
        document: Document,
        profile: str,
        date_order: str,
        extraction_data: dict,
        *,
        commit: bool = True,
    ) -> FinancialFormRecord:
        values = {
            "id": uuid.uuid4(),
            "document_id": document.id,
            "document_version": document.version,
            "source_sha256": document.sha256,
            "profile": profile,
            "date_order": date_order,
            "extraction_status": extraction_data["status"],
            "extraction_data": extraction_data,
            "created_by_user_id": self.user.id,
        }
        inserted = await self.db.execute(
            pg_insert(FinancialFormRecord)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_financial_form_records_source_profile_date_order")
        )
        record = await self.db.scalar(select(FinancialFormRecord).where(
            FinancialFormRecord.document_id == document.id,
            FinancialFormRecord.document_version == document.version,
            FinancialFormRecord.profile == profile,
            FinancialFormRecord.date_order == date_order,
        ).with_for_update())
        if record is None:
            raise ConflictError("The finance extraction draft could not be saved. Reload the current document and try again.")
        if record.source_sha256 != document.sha256:
            raise ConflictError("The source file changed without a new version. Extract the current document again.")
        if inserted.rowcount:
            audit.record(self.db, self.ctx, "financial_form.draft_saved", target=str(record.id), details={
                "document_id": str(document.id),
                "document_version": document.version,
                "profile": profile,
                "exception_count": len(extraction_data.get("exceptions", [])),
            })
        if commit:
            await self.db.commit()
            record = await self.db.get(FinancialFormRecord, record.id)
        return record

    async def current_review(
        self, record: FinancialFormRecord | None
    ) -> tuple[FinancialFormReviewRevisionOut | None, list[FinancialFormReviewRevisionOut]]:
        if record is None:
            return None, []
        rows = list((await self.db.execute(
            select(FinancialFormReviewRevision, User.email)
            .outerjoin(User, User.id == FinancialFormReviewRevision.reviewed_by_user_id)
            .where(FinancialFormReviewRevision.record_id == record.id)
            .order_by(FinancialFormReviewRevision.revision.desc())
            .limit(20)
        )).all())
        rows.reverse()
        history = [FinancialFormReviewRevisionOut(
            revision=revision.revision,
            reviewed_by_email=email or "Former employee",
            reviewed_at=revision.created_at,
            rationale=revision.rationale,
            fields=revision.reviewed_fields,
            line_items=revision.reviewed_line_items,
            reviewed_exception_indexes=revision.reviewed_exception_indexes,
        ) for revision, email in rows]
        return (history[-1] if history else None), history

    async def save_review(
        self, document: Document, record: FinancialFormRecord, payload: FinancialFormReviewRequest
    ) -> FinancialFormReviewRevisionOut:
        record = await self.db.scalar(select(FinancialFormRecord).where(
            FinancialFormRecord.id == record.id
        ).with_for_update())
        if record is None:
            raise NotFoundError("Finance extraction record not found.")
        if (
            record.document_id != document.id
            or record.document_version != document.version
            or record.source_sha256 != document.sha256
            or payload.expected_document_version != document.version
            or payload.profile != record.profile
            or payload.date_order != record.date_order
        ):
            raise ConflictError("The source or extraction settings changed. Reload and review the current version.")

        original_fields = record.extraction_data.get("fields", {})
        if set(payload.fields) != set(original_fields):
            raise InvalidInputError("Review every extracted field without adding or removing field names.")
        reviewed_fields = _validate_fields(record.profile, payload.fields)

        original_items = record.extraction_data.get("line_items", [])
        if len(payload.line_items) != len(original_items):
            raise InvalidInputError("Keep the extracted line item count so each correction retains its source location.")
        reviewed_items = []
        for index, (submitted, original) in enumerate(zip(payload.line_items, original_items, strict=True)):
            source = original.get("source") if isinstance(original, dict) else None
            if not isinstance(source, dict):
                raise InvalidInputError(f"Line item {index + 1} has no source location and cannot be approved.")
            reviewed = submitted.model_dump()
            for name in ("quantity", "unit_price", "amount"):
                reviewed[name] = _validate_decimal(reviewed[name], f"line item {index + 1} {name}")
            reviewed["currency"] = _validate_currency(reviewed["currency"], f"line item {index + 1} currency")
            reviewed["source"] = source
            reviewed_items.append(reviewed)

        _validate_exception_acknowledgements(
            record.extraction_data.get("exceptions", []), payload.reviewed_exception_indexes
        )
        _validate_reviewed_totals(record, reviewed_fields, reviewed_items)

        latest_revision = await self.db.scalar(select(func.max(FinancialFormReviewRevision.revision)).where(
            FinancialFormReviewRevision.record_id == record.id
        ))
        revision_number = (latest_revision or 0) + 1
        revision = FinancialFormReviewRevision(
            record_id=record.id,
            revision=revision_number,
            reviewed_by_user_id=self.user.id,
            rationale=payload.rationale,
            reviewed_fields=reviewed_fields,
            reviewed_line_items=reviewed_items,
            reviewed_exception_indexes=payload.reviewed_exception_indexes,
        )
        self.db.add(revision)
        audit.record(self.db, self.ctx, "financial_form.reviewed", target=str(record.id), details={
            "document_id": str(document.id),
            "document_version": document.version,
            "revision": revision_number,
            "profile": record.profile,
            "field_names": sorted(reviewed_fields),
            "line_item_count": len(reviewed_items),
            "reviewed_exception_count": len(payload.reviewed_exception_indexes),
        })
        await self.db.commit()
        return FinancialFormReviewRevisionOut(
            revision=revision_number,
            reviewed_by_email=self.user.email,
            reviewed_at=revision.created_at,
            rationale=revision.rationale,
            fields=reviewed_fields,
            line_items=reviewed_items,
            reviewed_exception_indexes=payload.reviewed_exception_indexes,
        )

    async def review_queue(self, documents) -> FinancialFormReviewQueueOut:
        reviewed_record = select(FinancialFormReviewRevision.id).where(
            FinancialFormReviewRevision.record_id == FinancialFormRecord.id
        ).exists()
        rows = list((await self.db.execute(
            select(FinancialFormRecord, Document)
            .join(Document, Document.id == FinancialFormRecord.document_id)
            .where(
                readable_by(self.user),
                Document.status == DOC_READY,
                Document.indexed_version == Document.version,
                FinancialFormRecord.document_version == Document.version,
                FinancialFormRecord.source_sha256 == Document.sha256,
                ~reviewed_record,
            )
            .order_by(FinancialFormRecord.created_at.asc())
            .limit(501)
        )).all())
        if len(rows) > 500:
            raise InvalidInputError("Narrow the finance review queue; only 500 records are returned at once.")
        items = []
        for record, document in rows:
            data = record.extraction_data
            items.append(FinancialFormReviewQueueItem(
                record_id=record.id,
                document_id=document.id,
                filename=document.filename,
                document_version=record.document_version,
                profile=record.profile,
                extraction_status=record.extraction_status,
                exception_count=len(data.get("exceptions", [])),
                created_at=record.created_at,
                can_review=await documents.can_modify(document),
            ))
        return FinancialFormReviewQueueOut(records=items)

    async def bookkeeping_csv(self, document_id: uuid.UUID | None = None) -> tuple[bytes, int]:
        if self.user.role not in (ROLE_ADMIN, ROLE_MANAGER):
            raise PermissionDeniedError("Only administrators or managers can export reviewed bookkeeping records.")
        if document_id is not None:
            await self.db.scalar(select(Document.id).where(Document.id == document_id, readable_by(self.user))) or _raise_not_found()
        has_review = select(FinancialFormReviewRevision.id).where(
            FinancialFormReviewRevision.record_id == FinancialFormRecord.id
        ).exists()
        rows = list((await self.db.execute(
            select(FinancialFormRecord, Document)
            .join(Document, Document.id == FinancialFormRecord.document_id)
            .where(
                readable_by(self.user),
                Document.status == DOC_READY,
                Document.indexed_version == Document.version,
                FinancialFormRecord.document_version == Document.version,
                FinancialFormRecord.source_sha256 == Document.sha256,
                has_review,
                *([FinancialFormRecord.document_id == document_id] if document_id is not None else []),
            )
            .order_by(FinancialFormRecord.created_at.asc())
            .limit(MAX_BOOKKEEPING_EXPORT_RECORDS + 1)
        )).all())
        if len(rows) > MAX_BOOKKEEPING_EXPORT_RECORDS:
            raise InvalidInputError("Select one document or narrow the export to 500 reviewed records.")
        revisions: dict[uuid.UUID, tuple[FinancialFormReviewRevision, str | None]] = {}
        if rows:
            latest_revision = (
                select(
                    FinancialFormReviewRevision.record_id.label("record_id"),
                    func.max(FinancialFormReviewRevision.revision).label("revision"),
                )
                .where(FinancialFormReviewRevision.record_id.in_([record.id for record, _ in rows]))
                .group_by(FinancialFormReviewRevision.record_id)
                .subquery()
            )
            review_rows = (await self.db.execute(
                select(FinancialFormReviewRevision, User.email)
                .join(latest_revision, and_(
                    FinancialFormReviewRevision.record_id == latest_revision.c.record_id,
                    FinancialFormReviewRevision.revision == latest_revision.c.revision,
                ))
                .outerjoin(User, User.id == FinancialFormReviewRevision.reviewed_by_user_id)
            )).all()
            for revision, email in review_rows:
                revisions[revision.record_id] = (revision, email)

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        headers = [
            "document_id", "filename", "document_version", "source_sha256", "profile",
            "invoice_number", "purchase_order_number", "document_date", "vendor", "subtotal", "tax", "total", "currency",
            "line_item_description", "line_item_quantity", "line_item_unit_price", "line_item_amount", "line_item_currency",
            "source_locations_json", "review_revision", "reviewed_by", "reviewed_at",
        ]
        writer.writerow(headers)
        export_row_count = sum(max(1, len(review.reviewed_line_items or [])) for review, _ in revisions.values())
        if export_row_count > MAX_BOOKKEEPING_EXPORT_ROWS:
            raise InvalidInputError("Narrow the export; reviewed bookkeeping handoff is limited to 25,000 rows.")
        for record, document in rows:
            reviewed, reviewer_email = revisions[record.id]
            data = record.extraction_data
            values = reviewed.reviewed_fields
            field_sources = {
                name: field.get("source")
                for name, field in data.get("fields", {}).items()
                if isinstance(field, dict) and field.get("source")
            }
            items = reviewed.reviewed_line_items or [{
                "description": None, "quantity": None, "unit_price": None, "amount": None, "currency": None, "source": None,
            }]
            for item in items:
                source_map = {"fields": field_sources, "line_item": item.get("source")}
                row = [
                    str(document.id), document.filename, record.document_version, record.source_sha256, record.profile,
                    values.get("invoice_number"), values.get("purchase_order_number"), values.get("document_date"),
                    values.get("vendor"), values.get("subtotal"), values.get("tax"), values.get("total"), values.get("currency"),
                    item.get("description"), item.get("quantity"), item.get("unit_price"), item.get("amount"),
                    item.get("currency"), _safe_csv(source_map), reviewed.revision, reviewer_email or "Former employee",
                    reviewed.created_at.isoformat(),
                ]
                writer.writerow([_safe_csv(value) for value in row])
        return output.getvalue().encode("utf-8-sig"), len(rows)

    async def export_accounting_batch(
        self, target_system: str, document_id: uuid.UUID | None = None
    ) -> tuple[bytes, str, str, int]:
        """Export reviewed financial records formatted for specific ERP / General Ledger systems."""
        if target_system not in ("quickbooks", "netsuite", "sap", "csv", "generic_csv"):
            raise InvalidInputError(f"Unsupported accounting target system: {target_system}")

        if target_system in ("csv", "generic_csv"):
            body, count = await self.bookkeeping_csv(document_id)
            return body, "text/csv; charset=utf-8", "infraspine-bookkeeping-reviewed.csv", count

        if self.user.role not in (ROLE_ADMIN, ROLE_MANAGER):
            raise PermissionDeniedError("Only administrators or managers can export reviewed bookkeeping records.")
        if document_id is not None:
            await self.db.scalar(select(Document.id).where(Document.id == document_id, readable_by(self.user))) or _raise_not_found()

        has_review = select(FinancialFormReviewRevision.id).where(
            FinancialFormReviewRevision.record_id == FinancialFormRecord.id
        ).exists()
        rows = list((await self.db.execute(
            select(FinancialFormRecord, Document)
            .join(Document, Document.id == FinancialFormRecord.document_id)
            .where(
                readable_by(self.user),
                Document.status == DOC_READY,
                Document.indexed_version == Document.version,
                FinancialFormRecord.document_version == Document.version,
                FinancialFormRecord.source_sha256 == Document.sha256,
                has_review,
                *([FinancialFormRecord.document_id == document_id] if document_id is not None else []),
            )
            .order_by(FinancialFormRecord.created_at.asc())
            .limit(MAX_BOOKKEEPING_EXPORT_RECORDS + 1)
        )).all())
        if len(rows) > MAX_BOOKKEEPING_EXPORT_RECORDS:
            raise InvalidInputError("Select one document or narrow the export to 500 reviewed records.")

        revisions: dict[uuid.UUID, tuple[FinancialFormReviewRevision, str | None]] = {}
        if rows:
            latest_revision = (
                select(
                    FinancialFormReviewRevision.record_id.label("record_id"),
                    func.max(FinancialFormReviewRevision.revision).label("revision"),
                )
                .where(FinancialFormReviewRevision.record_id.in_([record.id for record, _ in rows]))
                .group_by(FinancialFormReviewRevision.record_id)
                .subquery()
            )
            review_rows = (await self.db.execute(
                select(FinancialFormReviewRevision, User.email)
                .join(latest_revision, and_(
                    FinancialFormReviewRevision.record_id == latest_revision.c.record_id,
                    FinancialFormReviewRevision.revision == latest_revision.c.revision,
                ))
                .outerjoin(User, User.id == FinancialFormReviewRevision.reviewed_by_user_id)
            )).all()
            for revision, email in review_rows:
                revisions[revision.record_id] = (revision, email)

        if target_system == "quickbooks":
            out = io.StringIO(newline="")
            out.write("!TRNS\tTRNSID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\r\n")
            out.write("!SPL\tSPLID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\r\n")
            out.write("!ENDTRNS\r\n")
            for record, document in rows:
                reviewed, _ = revisions[record.id]
                values = reviewed.reviewed_fields
                vendor = values.get("vendor") or "Vendor"
                inv_num = values.get("invoice_number") or ""
                doc_date = values.get("document_date") or ""
                total = values.get("total") or "0.00"
                out.write(f"TRNS\t\tBILL\t{doc_date}\tAccounts Payable\t{vendor}\t-{total}\t{inv_num}\tInfraSpine Auto-Extract\r\n")
                items = reviewed.reviewed_line_items or [{"description": "General Expense", "amount": total}]
                for item in items:
                    item_amt = item.get("amount") or total
                    desc = item.get("description") or "Item"
                    out.write(f"SPL\t\tBILL\t{doc_date}\tExpense:Operating\t{vendor}\t{item_amt}\t{inv_num}\t{desc}\r\n")
                out.write("ENDTRNS\r\n")
            return out.getvalue().encode("utf-8"), "application/qbooks", "infraspine-quickbooks-bills.iif", len(rows)

        elif target_system == "netsuite":
            out = io.StringIO(newline="")
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow(["ExternalID", "Vendor", "Date", "PostingPeriod", "Memo", "Account", "Amount", "Currency", "TaxAmount", "RefNo"])
            for record, document in rows:
                reviewed, _ = revisions[record.id]
                values = reviewed.reviewed_fields
                writer.writerow([
                    str(document.id),
                    values.get("vendor") or "",
                    values.get("document_date") or "",
                    "",
                    f"InfraSpine Verified - {document.filename}",
                    "Accounts Payable",
                    values.get("total") or "0.00",
                    values.get("currency") or "USD",
                    values.get("tax") or "0.00",
                    values.get("invoice_number") or "",
                ])
            return out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "infraspine-netsuite-bills.csv", len(rows)

        elif target_system == "sap":
            out = io.StringIO(newline="")
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow(["EDI_DC40", "DOCNUM", "STATUS", "MESTYP", "SNDPRT", "SNDPRN", "RCVPRT", "RCVPRN", "ACTION", "CURCY", "BELNR", "DATUM", "VENDOR", "TOTAL", "LINE_COUNT"])
            for idx, (record, document) in enumerate(rows, 1):
                reviewed, _ = revisions[record.id]
                values = reviewed.reviewed_fields
                items = reviewed.reviewed_line_items or []
                writer.writerow([
                    "EDI_DC40",
                    f"{idx:010d}",
                    "53",
                    "INVOIC02",
                    "LS",
                    "INFRASPINE",
                    "LS",
                    "SAP_ERP",
                    "001",
                    values.get("currency") or "USD",
                    values.get("invoice_number") or "",
                    (values.get("document_date") or "").replace("-", ""),
                    values.get("vendor") or "",
                    values.get("total") or "0.00",
                    len(items),
                ])
            return out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "infraspine-sap-idoc.csv", len(rows)


def _validate_fields(profile: str, fields: dict[str, str | None]) -> dict[str, str | None]:
    normalized = {name: value.strip() if value is not None else None for name, value in fields.items()}
    if profile in ("invoice", "purchase_order"):
        for name in ("subtotal", "tax", "total"):
            if name in normalized:
                normalized[name] = _validate_decimal(normalized[name], name)
        if "document_date" in normalized and normalized["document_date"]:
            if not _ISO_DATE.fullmatch(normalized["document_date"]):
                raise InvalidInputError("Document date must use YYYY-MM-DD format.")
            try:
                normalized["document_date"] = date.fromisoformat(normalized["document_date"]).isoformat()
            except ValueError:
                raise InvalidInputError("Document date must use YYYY-MM-DD format.") from None
        if "currency" in normalized:
            normalized["currency"] = _validate_currency(normalized["currency"], "currency")
    return normalized


def _validate_decimal(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not _AMOUNT.fullmatch(value):
        raise InvalidInputError(f"{label.title()} must be a plain decimal value with up to four decimal places.")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        raise InvalidInputError(f"{label.title()} must be a valid decimal value.") from None
    if not decimal.is_finite():
        raise InvalidInputError(f"{label.title()} must be finite.")
    return format(decimal, "f")


def _validate_currency(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = value.upper()
    if not _CURRENCY.fullmatch(value) or value not in _SUPPORTED_CURRENCIES:
        raise InvalidInputError(f"{label.title()} must be a supported three-letter ISO currency code.")
    return value


def _validate_exception_acknowledgements(exceptions: list, reviewed_indexes: list[int]) -> None:
    if set(reviewed_indexes) != set(range(len(exceptions))):
        raise InvalidInputError("Review each listed extraction exception before saving this record.")


def _validate_reviewed_totals(record, fields, items) -> None:
    original_codes = {issue.get("code") for issue in record.extraction_data.get("exceptions", [])}
    subtotal, tax, total = (fields.get(name) for name in ("subtotal", "tax", "total"))
    if all(value is not None for value in (subtotal, tax, total)):
        if abs(Decimal(subtotal) + Decimal(tax) - Decimal(total)) > Decimal("0.01") and "total_components_mismatch" not in original_codes:
            raise InvalidInputError("Subtotal plus tax must match total, or review the source exception for discounts, fees, or rounding.")
    for index, item in enumerate(items):
        quantity, unit_price, amount = (item.get(name) for name in ("quantity", "unit_price", "amount"))
        if all(value is not None for value in (quantity, unit_price, amount)):
            if abs(Decimal(quantity) * Decimal(unit_price) - Decimal(amount)) > Decimal("0.01"):
                source_issues = record.extraction_data.get("exceptions", [])
                has_original_mismatch = any(
                    issue.get("code") == "line_total_mismatch"
                    and (issue.get("source") or {}).get("row_number") == (item.get("source") or {}).get("row_number")
                    for issue in source_issues
                )
                if not has_original_mismatch:
                    raise InvalidInputError(f"Line item {index + 1}: quantity × unit price must match its amount.")


def _safe_csv(value) -> str:
    if isinstance(value, (dict, list, tuple)):
        import json

        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value) if value is not None else ""
    stripped = text.lstrip(" \t\r\n")
    if text.startswith(("\t", "\r", "\n")) or (stripped and stripped[0] in "=+-@"):
        return "'" + text
    return text


def _raise_not_found():
    raise NotFoundError("Document not found.")
