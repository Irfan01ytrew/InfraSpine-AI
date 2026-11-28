"""Step AE — WhatsApp auto-reply from Knowledge + leads to CRM.

Slice 1: pure-function auto-reply decider (see decider.py).
Slice 2: Knowledge answer path for customers (see knowledge_reply.py).
Slice 3: CRM lead capture (see leads.py).
"""

from app.whatsapp_auto.decider import (
    Decision,
    Evidence,
    IncomingMessage,
    LeadSignals,
    classify_intent,
    decide,
    extract_lead_signals,
)
from app.whatsapp_auto.knowledge_reply import (
    KnowledgeReply,
    answer_customer_question,
    customer_visible_document_ids,
    load_customer_visible_document_ids,
    strip_citations,
    triage_customer_message,
    try_auto_reply,
)
from app.whatsapp_auto.leads import capture_lead, lead_idempotency_key, list_leads
from app.whatsapp_auto.replies import CANNED_REPLIES, INSUFFICIENT_EVIDENCE_REPLY, render

__all__ = [
    "CANNED_REPLIES",
    "Decision",
    "Evidence",
    "INSUFFICIENT_EVIDENCE_REPLY",
    "IncomingMessage",
    "KnowledgeReply",
    "LeadSignals",
    "answer_customer_question",
    "capture_lead",
    "classify_intent",
    "customer_visible_document_ids",
    "decide",
    "extract_lead_signals",
    "lead_idempotency_key",
    "list_leads",
    "load_customer_visible_document_ids",
    "render",
    "strip_citations",
    "triage_customer_message",
    "try_auto_reply",
]
