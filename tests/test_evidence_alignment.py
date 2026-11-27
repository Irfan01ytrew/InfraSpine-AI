"""Evidence checks aligned with whole documents, and locale-aware amounts (docs/document-discovery.md, Phase 2).

Regression cases from the enterprise workload suite: replies the active model actually gave, checked against
passages as the production parsers wrote them. A correct claim from a delivered document is not refused; a
fabricated attribution, a wrong value or an unverified calculation is never accepted. No database, no provider."""

import uuid

import pytest

from app.rag import tables
from app.rag.numbers import parse_amount, ungroup
from app.rag.retrieval import RetrievedChunk
from app.rag.sufficiency import assess_support

JULY = "Internet-Bills-July2026.docx"
HEADER = (
    "Northwind Traders\nInter Office Memo\nFrom: IT Department\nTo: CEO, Director Operations and CFO\n"
    "Subject: Agenda for Payments Approval.\nDate: 06.07.2026\n"
    "We are pleased to inform you that the following payments are required for approval."
)
TABLE = (
    "Table 1 · Rows 1–4 of 4 (columns: Sr. No, Description, Amount (Rs.))\n"
    "Row 1: Sr. No: 1; Description: Internet media link Nexlinx; Amount (Rs.): 18,450/-\n"
    "Row 2: Sr. No: 2; Description: Internet media link Optix 200mbps; Amount (Rs.): 14,600/-\n"
    "Row 3: Sr. No: 3; Description: Internet media link Optix 50mbps; Amount (Rs.): 4,100/-\n"
    "Row 4: Sr. No: 4; Description: Internet media link Optix 50mbps; Amount (Rs.): 4,100/-\n"
    "Total row: Description: Total; Amount (Rs.): 41,250/-"
)
CLOUDNET_TERMINATION = "Section: Termination\nEither party may terminate this agreement with 90 days written notice."
DATASERVE_TERMINATION = "Section: Termination\nEither party may terminate this agreement with 30 days written notice."


def chunk(filename: str, content: str, index: int = 0, label: str = "Document start") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=index + 1, document_id=uuid.uuid5(uuid.NAMESPACE_URL, filename), filename=filename, page_number=1,
        chunk_index=index, content=content, score=0.5, locator={"kind": "section", "position": 1, "label": label},
    )


# ------------------------------------------------------------------ false refusals (delivered evidence, correct claim)


@pytest.mark.parametrize(
    "question, answer, cited",
    [
        # The month is in the filename: the table is still "the July bill".
        ("july ka kitna bill tha", "July ke internet bills ka total **Rs. 41,250** tha. [S1]", [chunk(JULY, TABLE, 1)]),
        ("aur August ka?", "August 2026 ka internet bill total **Rs. 47,800** tha. [S1]",
         [chunk("Internet-Bills-August2026.docx", TABLE.replace("41,250", "47,800"), 1)]),
        # The vendor is named by the filename, the cited section is its termination clause.
        ("And DataServe?", "DataServe requires 30 days’ written notice to terminate the agreement. [S1]",
         [chunk("Vendor-Contract-DataServe.docx", DATASERVE_TERMINATION, 2)]),
        # "200 Mbps" in the answer is the "200mbps" of the source.
        ("What is in Internet-Bills-July2026.docx?",
         "The memo lists Nexlinx at Rs. 18,450 and Optix 200 Mbps at Rs. 14,600 [S1].", [chunk(JULY, TABLE, 1)]),
        # "addressed to" is not a postal address.
        ("Who is the July 2026 internet payment memo addressed to?",
         "The July 2026 internet payment memo is addressed to the **CEO, Director Operations, and CFO**. [S1]",
         [chunk(JULY, HEADER)]),
        # 06.07.2026 is stated as 6 July 2026.
        ("Internet-Bills-July2026.docx phr yeh file kya hai",
         "Yeh **6 July 2026 ka IT Department memo** hai, jisme CEO, Director Operations aur CFO se payment approval "
         "maangi gayi hai. [S1]", [chunk(JULY, HEADER)]),
    ],
)
def test_correct_claims_from_delivered_documents_are_not_refused(question, answer, cited):
    assert assess_support(question, answer, cited) != "unsupported"


def test_a_lakh_grouped_amount_is_one_number():
    cited = [chunk("Salary-Revision.xlsx", "Row 2: Grade: Manager; Revised salary 2026: PKR 3,85,000")]
    assert assess_support("What is the Manager's revised salary?", "PKR 385,000 [S1]", cited) != "unsupported"


# ------------------------------------------------------------------ ordinal and numeric equivalence (Phase 4)

DECISIONS = (
    "Section: Decisions\nThe board approved PKR 12 million for the CCTV upgrade across all sites. "
    "The ERP migration was deferred to the second quarter of 2027."
)


def test_q2_states_the_second_quarter():
    """Seen in 1 of 3 runs: the model wrote "Q2 2027" for "the second quarter of 2027" and was refused."""
    cited = [chunk("Board-Minutes-2026-09-15.docx", DECISIONS, 1)]
    answer = "The board approved **PKR 12 million** for the CCTV upgrade and deferred the ERP migration to **Q2 2027**. [S1]"
    assert assess_support("What did the board decide at the 15 September 2026 meeting?", answer, cited) != "unsupported"


def test_a_follow_up_is_checked_as_resolved():
    """ "aur docs ma deko" ("look in the docs") names no topic; its answer is checked against the question it was
    resolved with, as retrieval searched it. Seen in 3 of 3 runs: a correct, cited total refused."""
    cited = [chunk(JULY, TABLE, 1)]
    answer = "Docs ke mutabiq July ka internet bill **Rs 41,250** tha. [S1]"
    assert assess_support("aur docs ma deko", answer, cited) == "unsupported"  # the bare follow-up: no topic to match
    assert assess_support("net ka bill yr\naur docs ma deko", answer, cited) != "unsupported"


def test_a_synonym_or_an_excluded_term_sends_an_on_topic_answer_to_the_verifier():
    """Golden conflict-para, refused in 3 of 3 runs: "allowance" (the passage says "working days") and "not the
    withdrawn draft" are the question's distinctive words. The passage shares most of the question: verify it."""
    cited = [chunk("leave-policy.txt", "Annual leave policy\nAnnual leave for permanent staff is 18 working days "
                                       "under the current policy.\nCarry-over is limited to 5 days.")]
    question = "What is the current annual leave allowance for permanent staff, not the withdrawn draft?"
    answer = "Permanent staff receive **18 working days** of annual leave under the current policy. [S1]"
    assert assess_support(question, answer, cited) == "inconclusive"
    # Little shared with the question: still not about it.
    other = [chunk("refund-policy.txt", "The billing refund window is thirty days for invoice disputes.")]
    assert assess_support(question, "Customers have thirty days [S1].", other) == "unsupported"


def test_a_wrong_quarter_is_still_unsupported():
    cited = [chunk("Board-Minutes-2026-09-15.docx", DECISIONS, 1)]
    answer = "The board deferred the ERP migration to Q3 2027 [S1]."
    assert assess_support("When was the ERP migration deferred to?", answer, cited) == "unsupported"


def test_ordinals_and_compounds_are_numbers_but_a_discourse_first_is_not():
    from app.rag.sufficiency import _numbers

    assert _numbers("the twenty-first floor") == {"21"} and _numbers("twenty one days") == {"21"}
    assert _numbers("the second quarter", ordinals=True) == {"2"} and _numbers("doosri shift", ordinals=True) == {"2"}
    assert _numbers("First, the memo lists the links.") == set()  # an answer's "First," needs no source "1"


# ------------------------------------------------------------------ calculations and translations go to the verifier


def test_a_derived_number_is_verified_not_accepted():
    cited = [chunk("Vendor-Contract-CloudNet.docx", CLOUDNET_TERMINATION, 2),
             chunk("Vendor-Contract-DataServe.docx", DATASERVE_TERMINATION, 2)]
    answer = ("- **CloudNet:** 90 days’ written notice [S1]\n- **DataServe:** 30 days’ written notice [S2]\n\n"
              "CloudNet’s notice period is **60 days longer**. [S1][S2]")
    assert assess_support("Compare the notice periods in the CloudNet and DataServe contracts.", answer, cited) == "inconclusive"


def test_a_translation_follow_up_is_verified_not_refused():
    cited = [chunk("Leave-Policy-v3-2026.pdf", "Annual leave: permanent employees receive 20 working days of annual "
                                               "leave per calendar year.", 1)]
    answer = "مستقل ملازمین کو ہر کیلنڈر سال میں **20 کام کے دنوں کی سالانہ چھٹی** ملتی ہے۔ [S1]"
    assert assess_support("urdu mein batao", answer, cited, follow_up=True) == "inconclusive"


# ------------------------------------------------------------------ fabricated attributions and unsupported claims


def test_a_code_from_an_unrelated_passage_is_a_fabricated_attribution():
    """Model variance seen in the baseline: an asset register's code given as "the acquisition target code"."""
    cited = [chunk("asset-register.xlsx", "Row 3: Asset: Cooling unit; Location: Hall A; Units: 12; Code: CHL-A12")]
    answer = "The acquisition target code is **CHL-A12** for the cooling unit. [S1]"
    assert assess_support("What is the acquisition target code?", answer, cited) == "unsupported"


@pytest.mark.parametrize(
    "question, answer, cited",
    [
        # The filename names July; it never supplies a value: the July table says 41,250.
        ("What was the July 2026 internet bill total?", "The July 2026 total was Rs. 47,800. [S1]", [chunk(JULY, TABLE, 1)]),
        # A difference that the cited numbers do not give.
        ("Compare the notice periods in the CloudNet and DataServe contracts.",
         "CloudNet needs 90 days [S1], DataServe 30 days [S2]: 65 days longer. [S1][S2]",
         [chunk("Vendor-Contract-CloudNet.docx", CLOUDNET_TERMINATION, 2),
          chunk("Vendor-Contract-DataServe.docx", DATASERVE_TERMINATION, 2)]),
        # An amount from the wrong contract.
        ("What is the monthly fee in the CloudNet contract?", "The monthly fee is PKR 180,000. [S1]",
         [chunk("Vendor-Contract-CloudNet.docx", "Section: Fees\nThe monthly fee is PKR 240,000, invoiced quarterly.", 3)]),
        # A number glued to letters in the answer is still checked.
        ("Which Optix links are listed?", "Optix 300mbps at Rs. 14,600 [S1].", [chunk(JULY, TABLE, 1)]),
    ],
)
def test_unsupported_claims_stay_unsupported(question, answer, cited):
    assert assess_support(question, answer, cited) == "unsupported"


# ------------------------------------------------------------------ locale-aware amounts


@pytest.mark.parametrize(
    "text, value",
    [
        ("18,450/-", 18450.0), ("Rs. 18,450/-", 18450.0), ("PKR 1,25,000", 125000.0), ("₨ 60,000/=", 60000.0),
        ("$1,234.50", 1234.5), ("(1,200)", -1200.0), ("12%", 12.0), ("12,34,567", 1234567.0), ("-5", -5.0),
        ("1,2,3", None), ("1234,5", None), ("Q3", None), ("200mbps", None), ("Total", None), ("", None),
    ],
)
def test_amounts_are_read_in_western_and_south_asian_forms(text, value):
    assert parse_amount(text) == value


def test_only_valid_groupings_are_ungrouped():
    assert ungroup("Rs. 1,25,000; 18,450/-; 1,234,567; 1,2,3") == "Rs. 125000; 18450/-; 1234567; 1,2,3"


def test_a_table_of_rupee_amounts_sums_exactly():
    table = tables.Table(
        id="T1", sheet="Document", position=1, header_row=1, first_col=0,
        columns=("Sr. No", "Description", "Amount (Rs.)"),
        rows=(("1", "Internet media link Nexlinx", "18,450/-"), ("2", "Internet media link Optix 200mbps", "14,600/-"),
              ("3", "Internet media link Optix 50mbps", "4,100/-"), ("4", "Internet media link Optix 50mbps", "4,100/-")),
    )
    query = tables.Query(table, "sum", "Amount (Rs.)", (), (), "desc", 10)
    header, rows, matched, _, total = tables.run_query(query)
    assert matched == 4 and total == 41250 and rows == [["41250"]]
    biggest = tables.Query(table, "max", "Amount (Rs.)", (), (), "desc", 10)
    assert tables.run_query(biggest)[1] == [["18450"]]
