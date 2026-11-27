"""Document discovery: find the documents a question names, by their filename, title, headings and dates.

Retrieval used to see documents only through their chunk text, so a question naming "the July internet bills",
"the 15 September board minutes" or "the Lahore DC audit" could not find a document whose month, date or site is
written only in its filename (see docs/document-discovery.md). This module adds a document layer:

* `build_descriptor()` - at ingestion, one normalised descriptor per document: the filename split into words
  ("Internet-Bills-July2026" -> internet bills july 2026), dates in the filename and at the top of the document
  written out as month names, the title (first line), section headings, sheet names, slide titles and table titles.
  It is identity, not evidence: it is never sent to the model and never cited.
* `descriptor_terms()` - the question's words, split by the same rules, without stopwords or file extensions.
* `resolve_documents()` - documents whose descriptor matches the question, ranked by IDF-weighted term matches,
  inside the caller's ACL scope (the predicate is in the same SQL; document frequencies are counted inside it too,
  so a name the user cannot read never influences a result).

Everything is derived from the documents and the question. There are no lists of customer terms, filenames or
acronyms; the month names are the calendar's.
"""

from __future__ import annotations

import math
import re
from itertools import groupby
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import case, desc, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.rag.query_analysis import STOPWORDS

DESCRIPTOR_VERSION = 1
_MAX_DESCRIPTOR_CHARS = 2000
_MAX_HEADINGS = 20
_HEADER_CHARS = 1500  # dates at the top of a document (a memo's "Date:" line) describe the document
_MAX_HEADER_DATES = 3
_MAX_TERMS = 16
_TABULAR = frozenset({"rows", "sheet"})  # CSV rows and spreadsheet ranges start with column headers, not a title
# Share of a question's weight the resolved documents' names must cover (see `resolve_documents`).
MIN_COVERAGE = 0.35

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_NUMBER = {name: index for index, name in enumerate(_MONTHS, start=1)} | {
    name[:3]: index for index, name in enumerate(_MONTHS, start=1)
} | {"sept": 9}
# Extensions say nothing about which document is meant ("Internet-Bills-July2026.docx").
_EXTENSIONS = frozenset({
    "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt", "csv", "png", "jpg", "jpeg",
    "wav", "mp3", "m4a", "flac", "ogg", "opus", "aac", "mp4", "mov", "mkv", "webm", "avi", "m4v",
})
_PART = re.compile(r"[^\W_]+", re.UNICODE)
_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_A-Za-z]+", re.UNICODE)
_ISO_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_. /]?(0[1-9]|1[0-2])[-_. /]?(0[1-9]|[12]\d|3[01])(?!\d)")
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[-_. /](\d{1,2})[-_. /]((?:19|20)\d{2})(?!\d)")
# Query intent and Roman Urdu connective words do not identify a filename. Keep this local to
# document discovery: these can still be meaningful words in semantic passage retrieval.
_DISCOVERY_INTENT_WORDS = frozenset({
    "detail", "details", "tafseel", "pooch", "poochi", "poocha", "poochay", "poochna", "pochi",
    "bta", "btao", "btaein", "batayein", "bata", "batao", "bataya", "samjhao",
    "mai", "maine", "nay", "mujai", "mujhe", "mujhy", "chahiye", "chahye", "do", "dein",
    "hai", "hain", "tha", "thi", "ka", "ki", "ke", "kya", "kia", "kiya", "aur",
})


def words(text: str) -> list[str]:
    """Lowercase words, split at separators, camelCase and letter/digit boundaries: "Q3-2026-CoolingAudit" ->
    q3 q 3 2026 cooling audit. A mixed part is also kept whole ("q3", "july2026")."""
    out: list[str] = []
    for part in _PART.findall(text):
        pieces = _PIECE.findall(part)
        if len(pieces) > 1:
            out.append(part.casefold())
        out.extend(piece.casefold() for piece in pieces)
    return out


def _stem(filename: str) -> str:
    stem, dot, extension = filename.rpartition(".")
    return stem if dot and extension.casefold() in _EXTENSIONS else filename


def date_words(text: str, order: str = "dmy") -> list[str]:
    """Dates in the text written out: "2026-09-15" -> september sep 15 2026. Numeric day/month dates follow
    `order` ("dmy" or "mdy") unless one part cannot be a month."""
    found: list[tuple[int, int, int]] = []
    for year, month, day in _ISO_DATE.findall(text):
        found.append((int(year), int(month), int(day)))
    for first, second, year in _NUMERIC_DATE.findall(text):
        a, b = int(first), int(second)
        day, month = (a, b) if order == "dmy" else (b, a)
        if month > 12 and day <= 12:
            day, month = month, day
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.append((int(year), month, day))
    out: list[str] = []
    for year, month, day in found:
        name = _MONTHS[month - 1]
        out += [name, name[:3], str(day), str(year)]
    return out


def build_descriptor(
    filename: str,
    sections: Sequence[tuple[str, dict | None]],
    table_titles: Iterable[str] = (),
    *,
    date_order: str = "dmy",
) -> str:
    """The document's identity as normalised words. `sections` are (text, locator) pairs in document order."""
    stem = _stem(filename)
    parts: list[str] = [*words(stem), *date_words(stem, date_order)]
    header = ""
    title = ""
    headings: list[str] = []
    for index, (text, locator) in enumerate(sections):
        if len(header) < _HEADER_CHARS:
            header += "\n" + text
        locator = locator or {}
        if index == 0 and locator.get("kind") not in _TABULAR:  # a header row is not a title
            title = next((line.strip() for line in text.splitlines() if line.strip()), "")
        for heading in locator.get("heading_path") or ():
            if heading not in headings:
                headings.append(str(heading))
        for key in ("sheet", "title"):
            value = locator.get(key)
            if isinstance(value, str) and value and value not in headings:
                headings.append(value)
    title = title.removeprefix("Section:").strip()
    if _looks_like_title(title):  # a first line that is a sentence is body text, not identity
        parts += words(title)
    for heading in headings[:_MAX_HEADINGS]:
        parts += words(heading[:160])
    for table_title in table_titles:
        parts += words(str(table_title)[:160])
    header_dates = date_words(header[:_HEADER_CHARS], date_order)[: 4 * _MAX_HEADER_DATES]
    parts += header_dates
    unique = list(dict.fromkeys(word for word in parts if _useful(word) and word not in STOPWORDS))
    descriptor = " ".join(unique)
    return descriptor[:_MAX_DESCRIPTOR_CHARS].rsplit(" ", 1)[0] if len(descriptor) > _MAX_DESCRIPTOR_CHARS else descriptor


def _looks_like_title(line: str) -> bool:
    """At most ten words and no sentence inside it ("Data centre cooling audit, Q3 2026." is a title; "IT report:
    purpose is rollout progress and the agenda for the CEO." is a sentence)."""
    body = line.rstrip(".").strip()
    return bool(body) and len(body.split()) <= 10 and not re.search(r"[.!?؟۔]\s", body)


def _useful(word: str) -> bool:
    """Single characters ("(1)" copy markers, the "3" of "q3", which is kept whole too) name nothing."""
    return len(word) >= 2 and word not in _EXTENSIONS


def descriptor_terms(question: str) -> list[str]:
    """The question's words that can name a document, split as descriptors are."""
    terms = [
        word for word in dict.fromkeys(words(question))
        if _useful(word) and word not in STOPWORDS and word not in _DISCOVERY_INTENT_WORDS
    ]
    return terms[:_MAX_TERMS]


@dataclass(frozen=True)
class DocumentMatch:
    document_id: uuid.UUID
    score: float  # sum of the IDF of the question terms its descriptor contains
    matched: tuple[str, ...]


def _tsquery(term: str) -> str:
    """The term or its plural/singular ("bill" matches "bills", "contracts" matches "contract"). Not a prefix match:
    "person" must not name "personal notes". \\w-only terms (from `words`), so no tsquery operator can be injected."""
    if term.isdigit() or len(term) < 3:
        return term
    forms = [term, term[:-1]] if term.endswith("s") and len(term) > 3 else [term, f"{term}s"]
    return " | ".join(forms)


async def resolve_documents(
    db: AsyncSession,
    scope: list,
    question: str,
    *,
    limit: int = 3,
    ratio: float = 0.6,
    distinctive_share: float = 0.25,
    min_coverage: float = MIN_COVERAGE,
) -> list[DocumentMatch]:
    """Documents in `scope` whose descriptor matches the question, best first.

    A term's weight is its IDF over the descriptors in scope. The best document must match at least one
    distinctive term (in at most `distinctive_share` of the documents, or in two); documents scoring at least
    `ratio` of the best are kept, up to `limit` ("the CloudNet and DataServe contracts" resolves both). The kept
    names must cover `min_coverage` of the query's *known descriptor terms*. Topic words absent from filenames or
    headings are content-search terms, not evidence against a real filename match. The distinctive-term check
    prevents a common word such as "person" from naming an unrelated file."""
    terms = descriptor_terms(question)
    if not terms:
        return []
    vector = literal_column("documents.descriptor_search")
    matches = [vector.op("@@")(func.to_tsquery("simple", _tsquery(term))) for term in terms]
    counts = (
        await db.execute(
            select(
                func.count().label("total"),
                *(func.count().filter(match).label(f"t{index}") for index, match in enumerate(matches)),
            ).where(Document.descriptor.is_not(None), *scope)
        )
    ).one()
    total = int(counts[0])
    frequency = {term: int(value) for term, value in zip(terms, counts[1:], strict=True)}
    known = [term for term in terms if frequency[term] > 0]
    if total == 0 or not known:
        return []
    idf = {term: math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in known}
    distinctive = {term for term in known if frequency[term] <= max(2, distinctive_share * total)}
    known_matches = [(term, matches[terms.index(term)]) for term in known]
    score = sum((case((match, idf[term]), else_=0.0) for term, match in known_matches), start=literal_column("0.0"))
    any_term = func.to_tsquery("simple", " | ".join(f"({_tsquery(term)})" for term in known))
    rows = (
        await db.execute(
            select(Document.id, score.label("score"), *(case((match, 1), else_=0) for _, match in known_matches))
            .where(vector.op("@@")(any_term), *scope)
            .order_by(desc("score"), Document.id)
            .limit(window := max(limit * 4, 12))
        )
    ).all()
    found = [
        DocumentMatch(row[0], float(row[1]), tuple(term for (term, _), hit in zip(known_matches, row[2:], strict=True) if hit))
        for row in rows
    ]
    if not found or not set(found[0].matched) & distinctive:
        return []
    best = found[0].score
    eligible = [match for match in found if match.score >= ratio * best and set(match.matched) & distinctive]
    kept = _whole_ties(eligible, limit, complete=len(rows) < window)
    weight = sum(idf[term] for term in known)
    covered = sum(idf[term] for term in {term for match in kept for term in match.matched})
    return kept if weight and covered / weight >= min_coverage else []


def _whole_ties(matches: list[DocumentMatch], limit: int, *, complete: bool) -> list[DocumentMatch]:
    """Up to `limit` documents, best first, never splitting documents that score the same: the order inside a tie is
    only their IDs, so cutting through one would keep or drop a valid match arbitrarily. A tie that crosses `limit`
    is kept whole within twice the limit, otherwise dropped whole. A tie that may continue past the fetched rows
    (`complete` false and the tie reaches the last row) is incomplete and dropped whole."""
    kept: list[DocumentMatch] = []
    for _, group in groupby(matches, key=lambda match: round(match.score, 6)):
        tie = list(group)
        reaches_end = not complete and tie[-1] is matches[-1]
        if reaches_end or len(kept) >= limit:
            break
        if len(kept) + len(tie) > 2 * limit:
            break
        kept += tie
    return kept


async def refresh_descriptors(db: AsyncSession, *, date_order: str = "dmy", batch: int = 200) -> int:
    """Build missing or outdated descriptors from the stored chunks and tables (no re-parsing). Returns the count."""
    from app.db.models import DocumentChunk, DocumentTable

    stale = list(
        (
            await db.execute(
                select(Document.id)
                .where(
                    Document.indexed_version.is_not(None),
                    (Document.descriptor_version.is_(None)) | (Document.descriptor_version < DESCRIPTOR_VERSION),
                )
                .limit(batch)
            )
        ).scalars()
    )
    for document_id in stale:
        doc = await db.get(Document, document_id)
        if doc is None:
            continue
        chunks = (
            await db.execute(
                select(DocumentChunk.content, DocumentChunk.locator)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
            )
        ).all()
        titles = (
            await db.execute(select(DocumentTable.title).where(DocumentTable.document_id == document_id))
        ).scalars()
        doc.descriptor = build_descriptor(
            doc.indexed_filename or doc.filename, [(c.content, c.locator) for c in chunks], titles, date_order=date_order
        )
        doc.descriptor_version = DESCRIPTOR_VERSION
    await db.commit()
    return len(stale)
