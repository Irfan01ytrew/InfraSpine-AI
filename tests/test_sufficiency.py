"""Evidence checks at the answer boundary. Citations are not treated as proof."""

import asyncio
import logging
import uuid

from app.core.errors import LLMTimeoutError
from app.rag.answer import UNVERIFIED_FACT, answer_question
from app.rag.retrieval import RetrievedChunk
from app.rag.sufficiency import assess_support, is_company_specific
from app.schemas.chat import ChatResponse

REFUND = "Refund policy: customers may request a refund within 30 days of purchase."
NOTES = "Warehouse notes discuss the night shift. No door code is written here."
SECRET = "The warehouse door code is SILVERMAPLE for the night shift."
CURRENT = "Annual leave for permanent staff is 18 working days under the current policy."
DRAFT = "The withdrawn draft proposed 12 working days."


def _chunk(content: str, chunk_id: int = 1, filename: str = "policy.txt") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        filename=filename,
        page_number=1,
        chunk_index=0,
        content=content,
        score=0.8,
    )


class Scripted:
    provider = "test"

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str | None, str]] = []

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        self.calls.append((system_prompt, message))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResponse(reply=reply, model="m", provider="test")


def _run(llm, question: str, chunks: list[RetrievedChunk], *, general: bool = True):
    return asyncio.run(answer_question(llm, question, chunks, general_answers=general))


def test_a_related_refund_passage_does_not_answer_a_bank_question():
    refund = _chunk("Refund policy: customers may request a refund within 30 days by contacting billing support.")
    reply = "Billing support handles refund requests within 30 days [S1]."
    assert assess_support("Which bank account receives billing refunds?", reply, [refund]) == "unsupported"


def test_a_citation_without_the_fact_is_not_support():
    notes = _chunk(NOTES)
    assert assess_support("What is the warehouse door code?", "The night-shift door code is in the notes [S1].", [notes]) == "unsupported"
    result = _run(Scripted(["The night-shift door code is in the notes [S1]."]), "What is the warehouse door code?", [notes])
    assert result.answer == UNVERIFIED_FACT and result.cited == [] and result.general is False
    assert "SILVERMAPLE" not in result.answer


def test_maintenance_patch_followup_is_not_misclassified_as_a_time_request():
    audio = _chunk(
        "During the maintenance window, ticket number 482-1 was reviewed. "
        "The maintenance patch completed successfully.",
        filename="c06-synthetic-maintenance.wav",
    )
    answer = "The maintenance patch completed successfully. [S1]"

    assert assess_support(
        "What happened with that maintenance patch?", answer, [audio], follow_up=True
    ) == "supported"
    window = _chunk("The maintenance window begins at 9:15 a.m.", filename="maintenance-window.txt")
    assert assess_support(
        "When does the maintenance window begin?",
        "The maintenance window begins at 9:15 a.m. [S1]",
        [window],
    ) == "supported"


def test_missing_address_and_company_question_without_documents_are_not_guessed():
    handbook = _chunk("The company handbook covers leave and billing.")
    assert assess_support("What is the chief executive home address?", "The chief executive lives nearby [S1].", [handbook]) == "unsupported"
    result = _run(Scripted([]), "What is our refund window?", [])
    assert result.answer == UNVERIFIED_FACT and result.general is False
    assert is_company_specific("chief executive ka ghar ka pata kya hai?")
    assert is_company_specific("چیف ایگزیکٹو کے گھر کا پتہ کیا ہے؟")
    assert not is_company_specific("Which volcano erupted in 1815?")


def test_department_record_questions_are_not_mislabeled_as_general_knowledge():
    assert is_company_specific("What was the reconciliation target for Finance batch F-42?")
    assert not is_company_specific("How does finance work as an academic discipline?")


def test_restricted_fact_absent_from_the_cited_passage_is_not_revealed():
    result = _run(Scripted(["The door code is SILVERMAPLE [S1]."]), "What is the warehouse door code?", [_chunk(NOTES)])
    assert result.answer == UNVERIFIED_FACT and result.cited == []
    assert "SILVERMAPLE" not in result.answer


def test_withholding_keeps_the_cautious_answer_and_does_not_insert_the_secret():
    reply = "I won't share the door code; contact the shift owner [S1]."
    result = _run(Scripted([reply]), "What is the warehouse door code?", [_chunk(SECRET)])
    assert result.insufficient_evidence is False and result.cited
    assert result.answer == reply and "SILVERMAPLE" not in result.answer


def test_conflicting_sources_stay_when_both_values_are_cited():
    current, draft = _chunk(CURRENT), _chunk(DRAFT, 2)
    reply = "The current allowance is 18 working days [S1]. A withdrawn draft said 12 working days [S2]."
    assert assess_support("How many working days of annual leave do permanent staff receive?", reply, [current, draft]) == "supported"
    result = _run(Scripted([reply]), "How many working days of annual leave do permanent staff receive?", [current, draft])
    assert "18" in result.answer and "12" in result.answer and result.cited


def test_supported_answers_need_no_second_call_across_languages():
    source = _chunk("Customers may request a refund within thirty days. Quote REFUND-30.")
    question = "How many days is the billing refund window, and what code do customers quote?"
    for reply in (
        "Customers can request a refund within 30 days. The code is REFUND-30 [S1].",
        "The window is thirty days and the code is REFUND-30 [S1].",
        "Refund window tees din hai aur code REFUND-30 hai [S1].",
        "واپسی کی مدت تیس دن ہے اور کوڈ REFUND-30 ہے [S1].",
        "Customers have a month to ask. The code is REFUND-30 [S1].",
    ):
        llm = Scripted([reply])
        result = _run(llm, question, [source])
        assert result.insufficient_evidence is False and result.cited, reply
        assert len(llm.calls) == 1


def test_paraphrase_uses_one_verifier_call_and_only_the_cited_passage(caplog):
    caplog.set_level(logging.DEBUG)
    cited = _chunk(REFUND)
    secret = _chunk("Uncited restriction token OMEGA-HIDDEN.", 2)
    reply = "Purchasers receive a brief interval after buying [S1]."
    llm = Scripted([reply, "SUPPORTED"])
    result = _run(llm, "How many days do customers have to request a refund?", [cited, secret])
    assert result.answer == reply and result.cited and len(llm.calls) == 2
    assert "OMEGA-HIDDEN" not in llm.calls[1][1]
    assert REFUND[:40] in llm.calls[1][1]
    assert "OMEGA-HIDDEN" not in caplog.text and REFUND not in caplog.text


def test_verifier_rejection_and_outage_refuse_without_raising_or_leaking(caplog):
    caplog.set_level(logging.DEBUG)
    cited = _chunk(REFUND)
    reply = "Purchasers receive a brief interval after buying [S1]."
    question = "How many days do customers have to request a refund?"
    rejected = _run(Scripted([reply, "UNSUPPORTED"]), question, [cited])
    assert rejected.answer == UNVERIFIED_FACT and rejected.cited == [] and rejected.general is False

    failed = _run(Scripted([reply, LLMTimeoutError()]), question, [cited])
    assert failed.answer == UNVERIFIED_FACT and failed.cited == []
    assert REFUND not in caplog.text
    assert "LLMTimeoutError" in caplog.text


def test_general_conversations_still_answer_when_documents_are_silent():
    llm = Scripted(["Mount Tambora erupted in 1815."])
    result = _run(llm, "Which volcano erupted in 1815?", [])
    assert result.general is True and result.answer.startswith("Mount Tambora") and len(llm.calls) == 1
    assert "<source" not in llm.calls[0][1]

    math = Scripted(["4"])
    result = _run(math, "what is 2+2", [])
    assert result.general is True and result.answer == "4"


def test_urdu_code_shared_by_the_passage_is_supported_without_a_second_call():
    passage = _chunk("گودام کے دروازے کا کوڈ سلور میپل ہے۔")
    reply = "دروازے کا کوڈ سلور میپل ہے [S1]."
    llm = Scripted([reply])
    result = _run(llm, "گودام کے دروازے کا کوڈ کیا ہے؟", [passage])
    assert result.insufficient_evidence is False and result.cited and len(llm.calls) == 1


def test_r10_related_codes_do_not_answer_extension_bank_or_phone_questions():
    handbook = _chunk(
        "VPN tokens are rotated every 90 days. Rotation code SEC-90. "
        "The meal expense cap is PKR 1800. Meal code MEAL-1800."
    )
    contract = _chunk("The notice period is 60 days. Contract code VC-2260. Payment terms are net 45. Payment code PAY-45.")
    runbook = _chunk("The approved change ticket is CHG-4418. This runbook does not list a desk extension or a bank account.")

    assert assess_support(
        "What is the billing director desk extension in the facilities handbook?",
        "The desk extension is SEC-90 [S1].",
        [handbook],
    ) == "unsupported"
    assert assess_support(
        "Which bank account receives Harborline vendor payments?",
        "Vendor payments use payment code PAY-45 [S1].",
        [contract],
    ) == "unsupported"
    assert assess_support(
        "What phone extension reaches the Lahore network desk?",
        "The network desk extension is CHG-4418 [S1].",
        [runbook],
    ) == "unsupported"

    # A queued SUPPORTED label must not be consulted when the passage has no phone number.
    llm = Scripted(["The network desk extension is CHG-4418 [S1].", "SUPPORTED"])
    result = _run(llm, "What phone extension reaches the Lahore network desk?", [runbook])
    assert result.answer == UNVERIFIED_FACT and result.cited == [] and len(llm.calls) == 1

    bank = Scripted(["The bank account is PAY-45 [S1].", "SUPPORTED"])
    result = _run(bank, "Which bank account receives Harborline vendor payments?", [contract])
    assert result.answer == UNVERIFIED_FACT and result.cited == [] and len(bank.calls) == 1


def test_r10_budget_amount_and_urdu_shift_stay_when_the_passage_states_them():
    budget = _chunk("Row 2: Item (A2): Chiller replacement; Amount (B2): PKR 125000")
    inventory = _chunk("Row 3: Asset (A3): UPS battery; Units (C3): 40", 2)
    reply = "The chiller replacement amount is PKR 125,000 [S1]."
    llm = Scripted([reply])
    result = _run(llm, "What amount is listed for chiller replacement in the asset register budget?", [budget])
    assert result.answer == reply and result.cited and len(llm.calls) == 1

    assert assess_support(
        "What amount is listed for chiller replacement in the asset register budget?",
        "The register lists 40 [S1].",
        [inventory, budget],
    ) == "unsupported"

    passage = _chunk("گودام کے دروازے کا کوڈ سلور میپل ہے۔\nیہ کوڈ صرف رات کی شفٹ کے لیے ہے۔")
    urdu = "یہ رات کی شفٹ کے لیے ہے [S1]."
    llm = Scripted([urdu])
    result = _run(llm, "گودام کا کوڈ کس شفٹ کے لیے ہے؟", [passage])
    assert result.answer == urdu and result.cited and len(llm.calls) == 1


def test_attribute_paraphrases_and_real_values_still_count():
    extension = _chunk("The billing director desk extension is 214.")
    reply = "The desk extension is 214 [S1]."
    llm = Scripted([reply])
    result = _run(llm, "What is the billing director desk extension?", [extension])
    assert result.answer == reply and len(llm.calls) == 1

    withheld = "I won't share the desk extension; contact the shift owner [S1]."
    kept = _run(Scripted([withheld]), "What is the billing director desk extension?", [extension])
    assert kept.answer == withheld and kept.cited and "214" not in kept.answer

    account = _chunk("Harborline vendor payments go to bank account 440011.")
    assert assess_support(
        "Which bank account receives Harborline vendor payments?",
        "The bank account is 440011 [S1].",
        [account],
    ) == "supported"

    assert assess_support(
        "What is the hourly support rate?",
        "The rate is thirty rupees [S1].",
        [_chunk("The hourly support rate is PKR 30.")],
    ) == "supported"
    assert assess_support(
        "billing rate kitni hai?",
        "Rate tees hai [S1].",
        [_chunk("The billing rate is tees rupees.")],
    ) == "supported"

    # The amount is in the passage, so a numberless paraphrase may use the verifier.
    meal = _chunk("The meal expense cap is PKR 1800 per person.")
    vague = Scripted(["Staff have a fixed meal allowance [S1].", "SUPPORTED"])
    result = _run(vague, "What amount is the meal expense cap?", [meal])
    assert result.answer.startswith("Staff have a fixed meal allowance") and len(vague.calls) == 2

    register = _chunk(
        "Row 2: Asset (A2): UPS battery; Location (B2): Hall B; Units (C2): 40; Code (D2): UPS-B40\n"
        "Row 3: Asset (A3): Cooling unit; Location (B3): Hall A; Units (C3): 12; Code (D3): CHL-A12",
        filename="asset-register.xlsx",
    )
    assert assess_support(
        "What code and location are recorded for the UPS battery in the asset register?",
        "The UPS battery code is UPS-B40 and the location is Hall B [S1].",
        [register],
    ) == "supported"
    assert assess_support(
        "What asset code is assigned to the Hall A cooling unit?",
        "The Hall A cooling unit code is CHL-A12 [S1].",
        [register],
    ) == "supported"
    assert assess_support(
        "casual leave code kya hai?",
        "Casual leave code CL-8 hai [S1].",
        [_chunk("Casual leave sirf 8 din hai.\nCasual leave code CL-8 hai.", filename="casual-leave-roman.txt")],
    ) == "supported"
    budget = _chunk(
        "Row 2: Item (A2): Chiller replacement; Amount (B2): PKR 125000",
        filename="asset-register.xlsx",
    )
    assert assess_support(
        "What amount is listed for chiller replacement in the asset register budget?",
        "The amount is PKR 125,000 [S1].",
        [budget],
    ) == "supported"

    faq = _chunk(
        "Network desk FAQ\nThis FAQ does not name the Lahore window or a change ticket.",
        filename="network-faq.txt",
    )
    runbook = _chunk("The approved change ticket is CHG-4418.", 2, filename="incident-runbook.txt")
    assert assess_support(
        "What change ticket does the network desk FAQ itself print?",
        "The change ticket is CHG-4418 [S2].",
        [faq, runbook],
    ) == "unsupported"
    review = _chunk("Hall A chillers run at 60 percent capacity.", filename="quarterly-review.pptx")
    leave = _chunk("Annual leave for permanent staff is 18 working days.", 2, filename="leave-policy.txt")
    assert assess_support(
        "How many staff does the quarterly review say joined this quarter?",
        "18 staff joined [S2].",
        [review, leave],
    ) == "unsupported"

    # No amount is linked, so the same label cannot invent one.
    leave = _chunk("Annual leave for permanent staff is 18 working days.")
    invented = Scripted(["The leave amount is 18 [S1].", "SUPPORTED"])
    result = _run(invented, "What amount is paid for annual leave?", [leave])
    assert result.answer == UNVERIFIED_FACT and len(invented.calls) == 1


def test_deepseek_phase5_correct_answers_are_not_refused():
    """Replies DeepSeek gave in the Phase 5 run, each correct and cited, that the evidence check used to refuse."""
    handbook = _chunk("VPN tokens are rotated every 90 days by security operations. Rotation code SEC-90.", filename="handbook.pdf")
    contract = _chunk("The notice period is 60 days. Contract code VC-2260.", 2, filename="vendor-contract.docx")
    question = "How often are VPN tokens rotated, and how many days of notice does the vendor contract require?"
    reply = "- VPN tokens are rotated **every 90 days** [S1].\n- The vendor contract requires **60 days' notice** [S2]."
    assert assess_support(question, reply, [handbook, contract]) == "supported"

    review = _chunk("Hall A chillers run at 60 percent capacity.", filename="quarterly-review.pptx")
    register = _chunk("Row 3: Asset (A3): Cooling unit; Location (B3): Hall A; Code (D3): CHL-A12", 2, filename="asset-register.xlsx")
    question = "At what capacity do the Hall A chillers run, and what asset code does the Hall A cooling unit have?"
    reply = "- Hall A chillers run at **60 percent capacity** [S1].\n- The Hall A cooling unit has asset code **CHL-A12** [S2]."
    assert assess_support(question, reply, [review, register]) == "supported"

    # Naming another source that lacks the fact does not turn a stated, cited value into a refusal.
    faq = _chunk("Priority support FAQ. This FAQ does not state an hourly amount or a rate code.", filename="support-faq.txt")
    card = _chunk("The restricted billing rate for priority support is PKR 4500 per hour. Rate card code RATE-4500.", 2, filename="rate-card.txt")
    reply = (
        "- Hourly amount: PKR 4,500 per hour [S2]\n- Rate card code: RATE-4500 [S2]\n"
        "The priority support FAQ itself does not state an hourly amount or rate code [S1]."
    )
    assert assess_support("What hourly amount and code does the restricted priority support rate card charge?", reply, [faq, card]) == "supported"

    # The verifier sees each passage's document, so naming it is not mistaken for an added fact.
    llm = Scripted(["According to the quarterly review, Hall A chillers run at 60 percent capacity [S1].", "SUPPORTED"])
    result = _run(llm, "How heavily loaded are the Hall A chillers according to the quarterly review?", [review])
    assert result.cited and len(llm.calls) == 2 and "quarterly-review.pptx" in llm.calls[1][1]


def test_an_honest_refusal_that_lists_unrelated_cited_facts_is_still_a_refusal():
    """DeepSeek's Phase 5 replies to a missing fact: honest, and they list other, correctly cited facts (a meal
    cap, a change ticket). Those facts do not answer the question, so the reply is a refusal, not an answer."""
    meal = _chunk("The meal expense cap is PKR 1800 per person. Meal code MEAL-1800.", filename="handbook.pdf")
    runbook = _chunk(
        "The Lahore core switch maintenance window is Sunday 02:00 to 04:00 PKT. The approved change ticket is "
        "CHG-4418.", 2, filename="incident-runbook.txt",
    )
    question = "What monthly salary band for technicians is in the asset register?"
    for reply in (
        "The sources I have don't include an asset register, and none of them state a monthly salary band for "
        "technicians.\n\nWhat the sources do cover:\n- **Meal expenses:** cap is PKR 1800 per person, code MEAL-1800 [S1]\n"
        "- **Lahore network:** core switch maintenance window is Sunday 02:00–04:00 PKT, change ticket CHG-4418 [S2]",
        "The provided sources don't contain any information about technician salary bands or an asset register. "
        "I can see only:\n\n- A meal expense cap of PKR 1800 per person [S1]\n"
        "- The Lahore core switch maintenance window (Sunday 02:00–04:00 PKT) and change ticket CHG-4418 [S2]",
    ):
        assert assess_support(question, reply, [meal, runbook]) == "unsupported", reply
        result = _run(Scripted([reply, "SUPPORTED"]), question, [meal, runbook])
        assert result.answer == UNVERIFIED_FACT and result.insufficient_evidence and result.cited == []


def test_a_partial_answer_keeps_the_part_it_answers():
    contract = _chunk("Harborline vendor contract. The notice period is 60 days. Contract code VC-2260.", filename="vendor-contract.docx")
    reply = "The notice period is 60 days [S1]. The contract does not state a termination fee."
    assert assess_support("What notice period and termination fee does the vendor contract set?", reply, [contract]) == "supported"


def test_credentials_never_reach_the_model_or_a_snippet_but_business_figures_do():
    from app.rag.answer import build_user_message
    from app.rag.sufficiency import CREDENTIAL_PLACEHOLDER, redact_credentials, verify_support

    door = _chunk(SECRET, filename="warehouse-access.txt")
    urdu = _chunk("گودام کے دروازے کا کوڈ سلور میپل ہے۔\nیہ کوڈ صرف رات کی شفٹ کے لیے ہے۔", 2, filename="warehouse-urdu.txt")
    card = _chunk(
        "Restricted billing rate card. The restricted billing rate for priority support is PKR 4500 per hour. "
        "Rate card code RATE-4500.", 3, filename="rate-card.txt",
    )
    message = build_user_message("What is the warehouse door code?", [door, urdu, card])
    assert "SILVERMAPLE" not in message and "سلور میپل" not in message and CREDENTIAL_PLACEHOLDER in message
    assert "PKR 4500" in message and "RATE-4500" in message  # restricted business information is still shared
    assert redact_credentials("Wi-Fi password: Harbor#2024. Guest PIN 4471.") == (
        f"Wi-Fi password: {CREDENTIAL_PLACEHOLDER}. Guest PIN {CREDENTIAL_PLACEHOLDER}."
    )
    assert redact_credentials("The API key is sk-live-9f8a7b6c.") == f"The API key is {CREDENTIAL_PLACEHOLDER}."
    assert redact_credentials("Admins reset passwords every 90 days. Ticket CHG-4418.") == (
        "Admins reset passwords every 90 days. Ticket CHG-4418."
    )

    verifier = Scripted(["SUPPORTED"])
    asyncio.run(verify_support(verifier, "What is the door code?", "It is withheld.", [door]))
    assert "SILVERMAPLE" not in verifier.calls[0][1]

    # An answer that reports the placeholder is a withholding answer: kept, with its citation.
    reply = "The night-shift door code is [credential withheld] [S1]."
    result = _run(Scripted([reply]), "What is the warehouse door code?", [door])
    assert result.answer == reply and result.cited and "SILVERMAPLE" not in result.answer


def test_credential_detection_covers_lowercase_separated_tabular_and_formatted_secrets():
    from app.rag.answer import build_user_message
    from app.rag.sufficiency import CREDENTIAL_PLACEHOLDER as HIDDEN
    from app.rag.sufficiency import credential_values, mentions_credentials, redact_credentials

    assert redact_credentials("The guest wifi password is sunshine.") == f"The guest wifi password is {HIDDEN}."
    assert redact_credentials("Door code:\n4471\nUse it only at night.") == f"Door code:\n{HIDDEN}\nUse it only at night."
    assert credential_values("System | Username | Password\nVPN | admin | sunshine\nHR | hr.ops | bluebird7") == {
        "sunshine", "bluebird7",
    }
    assert redact_credentials("Row 2: System (A2): VPN; Password (C2): sunshine") == (
        f"Row 2: System (A2): VPN; Password (C2): {HIDDEN}"
    )
    assert redact_credentials('DB p' + 'ostgres://app:S3cretPw@db.local/main, key AKIA' + 'IOSFODNN7EXAMPLE') == (
        f"DB postgres://{HIDDEN}@db.local/main, key {HIDDEN}"
    )
    assert redact_credentials(('-' * 5) + 'BEGIN RSA PRIVATE KEY' + ('-' * 5) + '\nMIIabc\n' + ('-' * 5) + 'END RSA PRIVATE KEY' + ('-' * 5)) == HIDDEN
    # Wording around a credential is not a value; business figures and codes are untouched.
    assert redact_credentials("The admin password is reset every 90 days.") == "The admin password is reset every 90 days."
    card = "The restricted billing rate for priority support is PKR 4500 per hour. Rate card code RATE-4500."
    assert redact_credentials(card) == card and not mentions_credentials(card)
    assert mentions_credentials("Password policy: staff must use 12 characters.")

    # A value labelled in one passage is hidden where another passage repeats it without its label.
    labelled = _chunk("The night door password is sunshine.", filename="access.txt")
    repeated = _chunk("Shift note: sunshine was used at 22:00.", 2, filename="shift-log.txt")
    message = build_user_message("What happened on the night shift?", [labelled, repeated])
    assert "sunshine" not in message and message.count(HIDDEN) == 2


def test_a_value_from_another_file_still_does_not_answer_a_compound_question_about_a_named_file():
    review = _chunk("Hall A chillers run at 60 percent capacity.", filename="quarterly-review.pptx")
    leave = _chunk("Annual leave for permanent staff is 18 working days.", 2, filename="leave-policy.txt")
    # The named file's part is not answered from it, so the other file's value cannot stand in.
    assert assess_support(
        "How many staff does the quarterly review say joined, and at what capacity do the chillers run?",
        "18 staff joined [S2].",
        [review, leave],
    ) == "unsupported"


def test_every_passage_merged_into_one_citation_is_evidence():
    # A .docx has no pages, so all its passages share one citation number. The answer's facts come from
    # the second passage; checking only the first one made a correct answer look invented.
    memo = uuid.uuid4()
    header = RetrievedChunk(1, memo, "memo.docx", 1, 0, "INTER OFFICE MEMO. From: IT Department.", 0.8)
    body = RetrievedChunk(2, memo, "memo.docx", 1, 1, "All official laptops will be joined to the central domain.", 0.7)
    reply = "Inter office memo from the IT Department [S1]: all official laptops will be joined to the central domain [S2]."
    result = _run(Scripted([reply, "UNSUPPORTED"]), "what is IT policy?", [header, body])
    assert result.insufficient_evidence is False and result.answer != UNVERIFIED_FACT
    assert [n for n, _ in result.cited] == [1]


def test_a_quoted_file_name_or_page_label_is_evidence():
    note = RetrievedChunk(1, uuid.uuid4(), "SiteSummary_30-09-2026.docx", 8, 0, "140 cameras, 95.0% uptime.", 0.5)
    reply = "`SiteSummary_30-09-2026.docx` (page 8) reports 140 cameras [S1]."
    assert assess_support("check all doc", reply, [note], overview=True) == "supported"
    assert assess_support("check all doc", "It reports 305 cameras [S1].", [note], overview=True) == "unsupported"


def test_numbered_headings_are_formatting_not_facts():
    audit = _chunk("The audit covers 2875 log entries and 2055 report views.")
    reply = "### 1. Volume\n2875 entries [S1].\n\n### **2. Reports**\n2055 views [S1].\n\n**3.** Done [S1]."
    assert assess_support("what should I pay attention to?", reply, [audit], overview=True) == "supported"
    assert assess_support("what should I pay attention to?", "### 2. Reports\n2 views [S1].", [audit], overview=True) == "unsupported"


def test_an_overview_drops_a_derived_figure_but_keeps_the_grounded_rest():
    from app.rag.sufficiency import prune_unsupported

    audit = _chunk("The audit covers 2875 log entries. Form-1 was viewed 2055 times. No ledger access was found.")
    reply = (
        "### Findings\n* 2875 log entries were reviewed [S1].\n* Form-1 was viewed 2055 times [S1].\n"
        "* The logs show two types of actions [S1].\n* No ledger access was found [S1]."
    )
    text, dropped, total = prune_unsupported(reply, [audit])
    assert (dropped, total) == (1, 5) and "two types" not in text and "2055" in text and "### Findings" in text

    result = _run(Scripted([reply]), "what should I pay attention to?", [audit])
    assert result.insufficient_evidence is False and "two types" not in result.answer and "2875" in result.answer


def test_a_mostly_invented_overview_is_still_refused_and_a_fact_answer_is_never_pruned():
    audit = _chunk("The audit covers 2875 log entries.")
    invented = "* 9 logins [S1].\n* 4 reports [S1].\n* 2875 entries [S1]."
    assert _run(Scripted([invented]), "what should I pay attention to?", [audit]).answer == UNVERIFIED_FACT
    fact = "There were 2875 entries and 12 logins [S1]."
    assert _run(Scripted([fact]), "How many log entries does the audit cover?", [audit]).answer == UNVERIFIED_FACT


def test_an_uncited_document_answer_is_repaired_once_with_citations():
    llm = Scripted(["Customers may request a refund within 30 days.", "Customers may request a refund within 30 days of purchase [S1]."])
    result = _run(llm, "How many days do customers have to request a refund?", [_chunk(REFUND)], general=False)
    assert "[S1]" in result.answer and not result.insufficient_evidence and result.cited
    assert len(llm.calls) == 2 and "did not cite the sources" in llm.calls[1][1]


def test_a_repaired_answer_still_has_to_match_its_source():
    llm = Scripted(["Refunds are allowed within 90 days.", "Customers may request a refund within 90 days [S1]."])
    result = _run(llm, "How many days do customers have to request a refund?", [_chunk(REFUND)], general=False)
    assert result.insufficient_evidence and "90" not in result.answer


def test_an_answer_that_stays_uncited_is_still_withheld():
    llm = Scripted(["Customers may request a refund within 30 days.", "Within 30 days."])
    result = _run(llm, "How many days do customers have to request a refund?", [_chunk(REFUND)], general=False)
    assert result.insufficient_evidence and len(llm.calls) == 2
