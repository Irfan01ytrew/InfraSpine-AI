"""Document discovery and hierarchical retrieval (docs/document-discovery.md): documents found by filename, month,
date and site; small documents returned whole; named documents winning over the conversation's focus; ACLs on
descriptors; source-absent replies and grounded history. All text is synthetic."""

import io

import pytest

from app.main import app
from app.rag.discovery import DESCRIPTOR_VERSION, build_descriptor, date_words, descriptor_terms, words
from tests.conftest import USER2_EMAIL, USER_EMAIL, run_db
from tests.rag_helpers import activate_provider, process_all, upload
from tests.test_access_control import set_access

# ------------------------------------------------------------------ descriptors (no database)


def test_filenames_split_into_words_and_dates():
    assert words("Internet-Bills-July2026 (1)") == ["internet", "bills", "july2026", "july", "2026", "1"]
    assert "cooling" in words("Q3-2026-CoolingAudit") and "q3" in words("Q3-2026-CoolingAudit")
    assert date_words("Board-Minutes-2026-09-15") == ["september", "sep", "15", "2026"]
    assert date_words("Note_30-09-2026") == ["september", "sep", "30", "2026"]  # day 30 cannot be a month
    assert date_words("06.07.2026", "dmy")[:1] == ["july"]
    assert date_words("06.07.2026", "mdy")[:1] == ["june"]


def test_descriptor_holds_identity_not_body_text():
    descriptor = build_descriptor(
        "Internet-Bills-July2026.docx",
        [("Northwind Traders\nInter Office Memo\nDate: 06.07.2026", None),
         ("Section: Termination\nEither party may terminate with 90 days notice.", {"heading_path": ["Termination"]})],
        ["Payments"],
    )
    for word in ("internet", "bills", "july", "2026", "northwind", "termination", "payments"):
        assert word in descriptor.split()
    assert "docx" not in descriptor.split() and "90" not in descriptor.split() and "notice" not in descriptor.split()


def test_question_terms_drop_stopwords_and_extensions():
    assert descriptor_terms("What is in Internet-Bills-July2026.docx?") == ["internet", "bills", "july2026", "july", "2026"]
    assert descriptor_terms("july ka kitna bill tha") == ["july", "kitna", "bill"]


# ------------------------------------------------------------------ retrieval (production run_chat, PostgreSQL)


def _docx(*paragraphs: str, table: list[tuple[str, ...]] | None = None) -> bytes:
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row, values in zip(grid.rows, table, strict=True):
            for cell, value in zip(row.cells, values, strict=True):
                cell.text = value
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _memo(date: str, total: str) -> bytes:
    return _docx(
        "Northwind Traders", "Inter Office Memo", "Subject: Agenda for Payments Approval.", f"Date: {date}",
        "We are pleased to inform you that the following payments are required for approval.",
        table=[("Description", "Amount (Rs.)"), ("Internet media link Nexlinx", total), ("Total", total)],
    )


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def library(client):
    activate_provider(client)  # signs in as the admin
    ids = {}
    for name, body, kind in (
        ("Internet-Bills-July2026.docx", _memo("06.07.2026", "41,250"), DOCX),
        ("Internet-Bills-August2026.docx", _memo("05.08.2026", "47,800"), DOCX),
        ("Q3-Cooling-Audit-Lahore-DC.txt", b"Data centre cooling audit.\nChiller 2 shows high vibration.\n", "text/plain"),
        ("Q3-Cooling-Audit-Karachi-DC.txt", b"Data centre cooling audit.\nA refrigerant leak was repaired.\n", "text/plain"),
        ("billing-dashboard-capture.txt", b"Billing and payments dashboard. Outstanding balance: $0.00.\n", "text/plain"),
        ("parking-notice.txt", b"Visitor parking notice.\n\n\n" + b"Visitors park on basement level B2.\n", "text/plain"),
    ):
        ids[name] = upload(client, name, body, visibility="shared", content_type=kind).json()["id"]
    secret = upload(client, "Salary-Revision-Confidential.txt", b"Manager revised salary PKR 385,000.\n",
                    content_type="text/plain").json()["id"]
    assert set_access(client, secret, "restricted", users=[USER2_EMAIL]).status_code == 200
    ids["Salary-Revision-Confidential.txt"] = secret
    process_all(client)
    return ids


_PORTAL: list = []


@pytest.fixture(autouse=True)
def _portal(client):
    _PORTAL[:] = [client.portal]
    yield
    _PORTAL.clear()


def chat(email: str, question: str, *, history=(), focus=None, reply: str = "Stub [S1].", **settings_update):
    """Production run_chat with a scripted model; returns the ChatResult."""
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.db.models import User
    from app.rag.chat import run_chat
    from app.schemas.chat import ChatResponse

    class Scripted:
        async def chat(self, message, *, system_prompt=None):
            return ChatResponse(reply="NONE" if "TABLES in" in message else reply, model="m", provider="p")

    async def get_llm():
        return Scripted()

    settings = get_settings().model_copy(update=settings_update)

    async def go():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            return await run_chat(db, user, app.state.rag, settings, get_llm, question, history=list(history),
                                  document_ids=None, focus_document_ids=focus)

    return _PORTAL[0].call(go)


def files(result) -> list[str]:
    return [chunk.filename for chunk in result.passages]


def contents(result) -> str:
    return "\n".join(chunk.content for chunk in result.passages)


def test_descriptors_are_written_at_ingestion(client, library):
    rows = run_db(lambda c: c.exec_driver_sql(
        "SELECT filename, descriptor, descriptor_version FROM documents ORDER BY filename"
    )).all()
    by_name = {row[0]: (row[1], row[2]) for row in rows}
    descriptor, version = by_name["Internet-Bills-July2026.docx"]
    assert version == DESCRIPTOR_VERSION and {"july", "internet", "bills"} <= set(descriptor.split())


def test_a_month_only_in_the_filename_finds_its_document(client, library):
    result = chat(USER_EMAIL, "What was the internet bill total for August 2026?")
    assert files(result)[0] == "Internet-Bills-August2026.docx"
    assert "47,800" in contents(result)


def test_a_site_only_in_the_filename_finds_its_document(client, library):
    result = chat(USER_EMAIL, "What did the Lahore DC cooling audit find?")
    assert files(result)[0] == "Q3-Cooling-Audit-Lahore-DC.txt"


def test_a_named_small_document_arrives_whole(client, library):
    """The memo's header, table and total are separate chunks; a question naming the memo gets all of them."""
    result = chat(USER_EMAIL, "What is in Internet-Bills-July2026?")
    july = [chunk for chunk in result.passages if chunk.filename == "Internet-Bills-July2026.docx"]
    assert len(july) >= 2 and "41,250" in "\n".join(chunk.content for chunk in july)


def test_two_named_documents_are_both_resolved(client, library):
    result = chat(USER_EMAIL, "Compare the Lahore and Karachi cooling audits.")
    assert {"Q3-Cooling-Audit-Lahore-DC.txt", "Q3-Cooling-Audit-Karachi-DC.txt"} <= set(files(result))


def test_a_named_file_wins_over_the_conversations_focus(client, library):
    from app.rag.answer import Turn

    dashboard = library["billing-dashboard-capture.txt"]
    result = chat(
        USER_EMAIL, "Internet-Bills-July2026.docx phr yeh file kya hai",
        history=[Turn("What is in the billing dashboard capture?", "Outstanding balance $0.00. [S1]", grounded=True)],
        focus=[dashboard],
    )
    assert files(result) and set(files(result)) == {"Internet-Bills-July2026.docx"}


def test_descriptors_respect_acls(client, library):
    """A restricted document is never resolved by its name for a user who cannot read it."""
    outsider = chat(USER_EMAIL, "What is in Salary-Revision-Confidential.txt?")
    assert "Salary-Revision-Confidential.txt" not in files(outsider)
    assert "385,000" not in contents(outsider)
    member = chat(USER2_EMAIL, "What is in Salary-Revision-Confidential.txt?")
    assert "Salary-Revision-Confidential.txt" in files(member)


def test_discovery_can_be_switched_off(client, library):
    from app.core.config import get_settings

    result = chat(USER_EMAIL, "What was the internet bill total for August 2026?", rag_hierarchical_retrieval=False)
    assert len(result.passages) <= get_settings().rag_top_k  # chunk search only: no document expansion


def test_refresh_rebuilds_missing_descriptors(client, library):
    from app.rag.discovery import refresh_descriptors

    run_db(lambda c: c.exec_driver_sql("UPDATE documents SET descriptor = NULL, descriptor_version = NULL"))

    async def go():
        async with app.state.sessionmaker() as db:
            return await refresh_descriptors(db)

    assert _PORTAL[0].call(go) == len(library)
    assert run_db(lambda c: c.exec_driver_sql("SELECT count(*) FROM documents WHERE descriptor IS NULL")).scalar() == 0


# ------------------------------------------------------------------ answers


def test_a_source_absent_reply_is_insufficient_evidence(client, library):
    """A cited reply that only says the sources do not state the fact is not shown as a verified answer."""
    result = chat(USER_EMAIL, "What is the late payment penalty in the July 2026 payment memo?",
                  reply="The provided memo does not state a late payment penalty. [S1]")
    assert result.answer.insufficient_evidence and not result.answer.cited


@pytest.mark.parametrize(
    "reply, absent",
    [
        ("The provided DataServe sources don’t specify a notice period. [S1][S2]", True),
        ("The source only confirms the meeting was held; it does not state what was decided. [S1]", True),
        ("July ka total source mein diya nahi gaya. [S1]", True),
        ("The sources do not identify an acquisition target code. [S1][S2]", True),
        ("The July 2026 total is Rs. 41,250 [S1]. The due date is not stated.", False),  # a partial answer is kept
        ("Visitors do not include deliveries in the B2 count [S1].", False),  # an ordinary fact, not an absence
        ("Casual leave carry-over nahi hota [S1].", False),
    ],
)
def test_source_absent_replies_are_told_from_partial_answers(reply, absent):
    from app.rag.sufficiency import states_only_absence

    assert states_only_absence(reply) is absent


def test_general_answers_never_see_document_grounded_history():
    from app.rag.answer import Turn, general_answer
    from app.schemas.chat import ChatResponse

    seen = []

    class Recorder:
        async def chat(self, message, *, system_prompt=None):
            seen.append(message)
            return ChatResponse(reply="General reply.", model="m", provider="p")

    import asyncio

    history = [Turn("What is in the dashboard?", "Outstanding balance $0.00. [S1]", grounded=True),
               Turn("What does SLA mean?", "Service level agreement.")]
    asyncio.run(general_answer(Recorder(), "And KPI?", history, None))
    assert "$0.00" not in seen[0] and "What is in the dashboard?" in seen[0] and "Service level agreement" in seen[0]


# ------------------------------------------------------------------ ties (equally scored documents)


def test_ties_are_kept_or_dropped_whole():
    import uuid as _uuid

    from app.rag.discovery import DocumentMatch, _whole_ties

    def match(score: float) -> DocumentMatch:
        return DocumentMatch(_uuid.uuid4(), score, ())

    best, tied = match(5), [match(3) for _ in range(3)]
    assert _whole_ties([best, *tied, match(2)], 3, complete=True) == [best, *tied]  # crosses the limit: kept whole
    many = [match(3) for _ in range(7)]
    assert _whole_ties([best, *many], 3, complete=True) == [best]  # too many to keep: dropped whole, never sampled
    assert _whole_ties([best, *tied], 3, complete=False) == [best]  # may continue past the fetched rows: dropped


def test_equally_named_documents_are_all_resolved(client, library):
    """Four documents match "internet bill" equally; with a limit of 3 none is dropped by its ID."""
    from tests.rag_helpers import process_all, upload

    for name in ("Internet-Bill-Sahiwal.txt", "Internet-Bill-Lahore.txt"):
        upload(client, name, b"Branch payment memo.\nTotal Rs. 1,000.\n", visibility="shared", content_type="text/plain")
    for n in range(10):  # a library large enough for a name shared by four documents to be distinctive
        upload(client, f"Unrelated-Notice-{n}.txt", f"Notice {n} about parking.\n".encode(), visibility="shared",
               content_type="text/plain")
    process_all(client)

    async def go():
        from sqlalchemy import select

        from app.db.models import Document, User
        from app.rag.retrieval import named_documents

        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(User.email == USER_EMAIL))).scalar_one()
            ids = await named_documents(db, user, app.state.rag.embedder.model_name, "internet bill", limit=3)
            names = (await db.execute(select(Document.filename).where(Document.id.in_(ids)))).scalars().all()
            return sorted(names)

    assert _PORTAL[0].call(go) == sorted([
        "Internet-Bill-Lahore.txt", "Internet-Bill-Sahiwal.txt", "Internet-Bills-August2026.docx",
        "Internet-Bills-July2026.docx",
    ])
