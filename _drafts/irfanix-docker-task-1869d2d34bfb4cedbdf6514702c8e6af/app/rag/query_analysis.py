"""Query analysis and lexical search for hybrid retrieval (docs/hybrid-retrieval.md).

Everything here is derived from the question's text and the corpus itself; nothing names a company term,
an acronym or a typo:

* Terms are the question's words without generic function words (English and Roman Urdu stopwords). An
  all-capitals token in a question that is not all capitals is kept as an acronym even when it spells a
  function word ("IT" is not "it").
* Each term's chunk frequency comes from `rag_terms` (kept by triggers on `document_chunks`); its document
  frequency is counted at query time for the question's few terms. Weights use document frequency (BM25-style
  IDF), so a document that repeats a word in every chunk does not make that word look common.
* Typo tolerance: a term of five or more letters that no chunk contains is matched to vocabulary terms by
  trigram similarity and accepted only within a small edit distance (1 up to seven letters, 2 beyond). The
  original stays in the query; a correction only adds candidates.
* `lexical_candidates` runs one PostgreSQL full-text query with the caller's ACL predicate in the same SQL.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace

from sqlalchemy import case, desc, func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"\w+", re.UNICODE)
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,9}\b")
_LETTERS = re.compile(r"^[^\W\d_]+$", re.UNICODE)
_IDENTITY_QUERY = re.compile(
    r"^\s*(?:who\s+am\s+i|what\s+is\s+my\s+name|what\s+do\s+you\s+know\s+about\s+me)\s*[?.!]*$"
    r"|^\s*(?:mai|main|me)\s+(?:kaun|kon|kn)\s+(?:hoon|hun|hn|hu)\s*[?.!]*$"
    r"|^\s*(?:mera|meri)\s+naam\s+(?:kya|kiya)\s+(?:hai|he)\s*[?.!]*$"
    r"|\b(?:what|which|kis|kaunsi|kon\s+si)\b.{0,30}\b(?:email|e-mail|account)\b.{0,50}\b(?:sign\s*in|sign\s*up|signup|login|registered|use|used)\b"
    r"|\b(?:sign\s*in|sign\s*up|signup|login|logged\s+in)\b.{0,60}\b(?:kis|kaunsi|kon\s+si|email|e-mail|account)\b",
    re.IGNORECASE,
)


def is_identity_query(question: str) -> bool:
    """Recognize authenticated-session questions that should never be routed through document search."""
    return bool(_IDENTITY_QUERY.search(question))

# Generic function words (not domain vocabulary). English: the common stopword set; Roman Urdu: particles,
# pronouns, auxiliaries and question words.
STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being below between
    both but by can could did do does doing done down during each few for from further get got had has have having he
    her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now
    of off on once only or other our ours ourselves out over own please same she should so some such tell than that
    the their theirs them themselves then there these they this those through to too under until up very was we were
    what when where which while who whom whose why will with would you your yours yourself yourselves give show
    find say says said let lets like many much
    aap ap aur bata batao bhi de do hai hain hy ho hona hota hoti hote hum is ka kab kahan kaise kaisa kar karna
    karo ke kese ki kia kis kisi ko kon koi kya kyun kyu main mein me mera meri mujhe na nahi ne par pe raha rahi
    rahe se sab tha thi the tum un us wo woh ya ye yeh
    """.split()
)
SIMILARITY_FLOOR = 0.25  # trigram similarity for correction candidates (confirmed by edit distance)
DISTINCTIVE_SHARE = 0.2
_DOCUMENT_COUNT_MAX_CHUNKS = 20_000  # a term in more chunks than this is common: its documents are not counted
_SAME_DOCUMENT_DECAY = 0.7  # lexical order: weight of a document's 2nd, 3rd ... chunk relative to its previous one

_DO_NOT_CORRECT = frozenset({
    "audio", "recording", "recordings", "video", "videos", "image", "images", "picture", "pictures",
    "photo", "photos", "screenshot", "screenshots", "camera", "clip", "clips", "speech", "speaker",
    "transcript", "transcription", "voice", "sound", "sounds", "music", "listen", "listening",
    "document", "documents", "invoice", "invoices", "report", "reports", "contract", "contracts",
    "meeting", "meetings", "action", "actions", "tasks", "task", "table", "tables", "sheet", "sheets",
    "kya", "kia", "kiya", "kita", "baat", "bat", "batao", "btao", "bata", "btayein", "batayein",
    "mein", "main", "mai", "me", "karo", "karein", "karna", "krna", "raha", "rahi", "rahe",
    "hona", "hota", "hoti", "hote", "chal", "chala", "dekh", "dekho", "dikhao", "dikhta",
    "kaun", "kaunsa", "konsa", "kahan", "kyun", "kaisay", "kese", "kaisa", "kaisi",
    "samjhao", "wazahat", "guftagu", "wali", "wala", "wale", "diya", "dein", "dain", "gain", "gaya", "gayi",
    "address", "addressing", "called", "calling", "asking", "telling", "talking", "spoken", "speaking",
})


@dataclass(frozen=True)
class TermGroup:
    """One word of the question and the vocabulary forms it is searched as (itself and any correction)."""

    word: str
    forms: tuple[str, ...]
    chunks: int  # chunks containing the best form (0: the corpus does not contain it)
    corrected: bool = False
    documents: int = 0  # documents containing any form (as searched, prefixes included)

    @property
    def tsquery(self) -> str:
        # \w-only tokens, so this cannot inject tsquery operators. Four letters or more match as a prefix.
        return " | ".join(f"{form}:*" if len(form) >= 4 else form for form in self.forms)


@dataclass(frozen=True)
class QueryAnalysis:
    groups: tuple[TermGroup, ...]
    total_chunks: int
    corrected_text: str | None = None  # the question with corrections applied (searched semantically too)
    corrections: dict[str, str] = field(default_factory=dict)
    total_documents: int = 0

    @property
    def own_topics(self) -> int:
        """Words of the question the corpus knows (directly or after correction): its own topic."""
        return sum(1 for group in self.groups if group.chunks > 0)

    def idf(self, group: TermGroup) -> float:
        n = max(self.total_documents, group.documents, 1)
        return math.log(1 + (n - group.documents + 0.5) / (group.documents + 0.5))

    def distinctive(self, group: TermGroup) -> bool:
        """In at most a fifth of the documents (or in one): matching it is evidence on its own."""
        return 0 < group.documents <= max(1, DISTINCTIVE_SHARE * self.total_documents)


async def guarded(db: AsyncSession, operation, fallback):
    """Run a lexical step inside a savepoint; on any database error, log it and return `fallback` (the caller then
    searches semantically only). The savepoint keeps the request's transaction usable."""
    try:
        async with db.begin_nested():
            return await operation()
    except Exception as exc:
        logger.warning("Lexical search unavailable (%s); using semantic search only", type(exc).__name__)
        return fallback


def mask_acronyms(question: str) -> str:
    """Acronyms ("IT", "HR") replaced by a neutral token, so they are not read as words like the pronoun "it".
    A question written entirely in capitals is left as it is (there capitals carry no meaning)."""
    if not any(char.islower() for char in question):
        return question
    return _ACRONYM.sub(lambda match: "_" * len(match.group(0)), question)


def query_words(question: str) -> list[str]:
    """The question's informative words, lowercase, in order and without repeats."""
    acronyms = {match.group(0).casefold() for match in _ACRONYM.finditer(question)} if any(c.islower() for c in question) else set()
    words: list[str] = []
    for token in _TOKEN.findall(question):
        word = token.casefold()
        if len(word) < 2 or word in words:
            continue
        if word in STOPWORDS and word not in acronyms:
            continue
        words.append(word)
    return words


def edit_distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein distance (adjacent transpositions count once), or `limit + 1` when larger."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


async def analyze_query(db: AsyncSession, texts: list[str], *, correct: bool) -> QueryAnalysis:
    """Terms of the question (and of its other search forms, such as a Roman Urdu expansion) with their corpus
    frequencies, and typo corrections when `correct` is set."""
    words: list[str] = []
    for value in texts:
        words += [word for word in query_words(value) if word not in words]
    if not words:
        return QueryAnalysis((), 0)
    frequency = dict(
        (await db.execute(text("SELECT term, chunk_count FROM rag_terms WHERE term = ANY(:words)"), {"words": words})).all()
    )
    total = (await db.execute(text("SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE relname = 'document_chunks'"))).scalar() or 0
    if total <= 0:  # never analysed yet: count exactly (small tables)
        total = (await db.execute(select(func.count()).select_from(DocumentChunk))).scalar_one()
    groups: list[TermGroup] = []
    corrections: dict[str, str] = {}
    correctable = [
        word for word in words
        if int(frequency.get(word, 0)) == 0
        and correct
        and len(word) >= 5
        and _LETTERS.match(word)
        and word.casefold() not in _DO_NOT_CORRECT
    ]
    correction_matches = await _corrections(db, correctable) if correctable else {}
    for word in words:
        chunks = int(frequency.get(word, 0))
        fixed = correction_matches.get(word) if chunks == 0 else None
        if fixed is not None:
            corrections[word] = fixed[0]
            groups.append(TermGroup(word, (word, fixed[0]), fixed[1], corrected=True))
            continue
        groups.append(TermGroup(word, (word,), chunks))
    groups, documents = await _document_frequencies(db, groups)
    corrected_text = None
    if corrections:
        corrected_text = re.sub(
            r"\w+", lambda m: corrections.get(m.group(0).casefold(), m.group(0)), texts[0], flags=re.UNICODE
        )
    return QueryAnalysis(tuple(groups), int(total), corrected_text, corrections, documents)


async def _document_frequencies(db: AsyncSession, groups: list[TermGroup]) -> tuple[list[TermGroup], int]:
    """Each term's number of documents (one query over the chunks that contain any of them), and the number of
    indexed documents. Terms in very many chunks are not counted: they are common in any case."""
    total = (await db.execute(select(func.count()).select_from(Document).where(Document.indexed_version.is_not(None)))).scalar_one()
    counted = [group for group in groups if 0 < group.chunks <= _DOCUMENT_COUNT_MAX_CHUNKS]
    found: dict[str, int] = {}
    if counted:
        vector = literal_column("document_chunks.search_vector")
        columns = [
            func.count(func.distinct(DocumentChunk.document_id))
            .filter(vector.op("@@")(func.to_tsquery("simple", group.tsquery)))
            .label(f"d{index}")
            for index, group in enumerate(counted)
        ]
        any_term = func.to_tsquery("simple", " | ".join(f"({group.tsquery})" for group in counted))
        row = (await db.execute(select(*columns).where(vector.op("@@")(any_term)))).one()
        found = {group.word: int(value) for group, value in zip(counted, row, strict=True)}
    updated = [
        replace(group, documents=found.get(group.word, total if group.chunks > _DOCUMENT_COUNT_MAX_CHUNKS else 0))
        for group in groups
    ]
    return updated, int(total)


async def _correction(db: AsyncSession, word: str) -> tuple[str, int] | None:
    """Compatibility wrapper for callers correcting a single vocabulary term."""
    return (await _corrections(db, [word])).get(word)


async def _corrections(db: AsyncSession, words: list[str]) -> dict[str, tuple[str, int]]:
    """Correct several missing words with one database round trip.

    The old per-word query was especially expensive for multilingual questions: every unknown token incurred a
    separate pg_trgm request. The lateral query keeps the same 25 trigram candidates, ordering, edit-distance
    check, and frequency tie-break for each word while avoiding that N+1 network latency.
    """
    if not words:
        return {}
    await db.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_FLOOR}"))
    rows = (
        await db.execute(
            text(
                "WITH input_words AS ("
                " SELECT word, ordinal FROM unnest(CAST(:words AS text[])) WITH ORDINALITY AS i(word, ordinal)"
                ") "
                "SELECT input_words.word, candidate.term, candidate.chunk_count "
                "FROM input_words "
                "CROSS JOIN LATERAL ("
                " SELECT term, chunk_count, similarity(term, input_words.word) AS similarity "
                " FROM rag_terms WHERE term % input_words.word AND length(term) >= 4 "
                " ORDER BY similarity DESC, chunk_count DESC LIMIT 25"
                ") AS candidate "
                "ORDER BY input_words.ordinal, candidate.similarity DESC, candidate.chunk_count DESC"
            ),
            {"words": words},
        )
    ).all()
    best_by_word: dict[str, tuple[int, int, str]] = {}
    limits = {word: 1 if len(word) < 8 else 2 for word in words}
    for word, term, chunks in rows:
        if not _LETTERS.match(term):
            continue
        distance = edit_distance(word, term, limits[word])
        best = best_by_word.get(word)
        if distance <= limits[word] and (best is None or (distance, -chunks) < (best[0], -best[1])):
            best_by_word[word] = (distance, chunks, term)
    return {word: (best[2], best[1]) for word, best in best_by_word.items()}


async def lexical_candidates(
    db: AsyncSession, scope: list, analysis: QueryAnalysis, limit: int, per_document: int = 3
) -> list[tuple[int, float, float, bool]]:
    """(chunk id, IDF-weighted match score, share of the question's known words matched, whether a distinctive word
    matched), best first, among the chunks `scope` allows (the ACL predicate is part of this query). At most
    `per_document` chunks of one document are candidates, and each further chunk of a document counts
    `_SAME_DOCUMENT_DECAY` times less in the order, so a document that repeats a query word in every chunk cannot
    fill the list while a second chunk with a rarer word can still lead."""
    groups = [group for group in analysis.groups if group.chunks > 0]
    if not groups:
        return []
    vector = literal_column("document_chunks.search_vector")
    matches = [vector.op("@@")(func.to_tsquery("simple", group.tsquery)) for group in groups]
    score = sum(
        (case((match, analysis.idf(group)), else_=0.0) for match, group in zip(matches, groups, strict=True)),
        start=literal_column("0.0"),
    )
    matched = sum((case((match, 1), else_=0) for match in matches), start=literal_column("0"))
    distinctive = [match for match, group in zip(matches, groups, strict=True) if analysis.distinctive(group)]
    strong = sum((case((match, 1), else_=0) for match in distinctive), start=literal_column("0"))
    any_term = func.to_tsquery("simple", " | ".join(f"({group.tsquery})" for group in groups))
    density = func.ts_rank_cd(vector, any_term)
    ranked = (
        select(
            DocumentChunk.id.label("id"),
            score.label("score"),
            matched.label("matched"),
            strong.label("strong"),
            density.label("density"),
            func.row_number()
            .over(partition_by=DocumentChunk.document_id, order_by=(score.desc(), density.desc(), DocumentChunk.id))
            .label("place"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(vector.op("@@")(any_term), *scope)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ranked.c.id, ranked.c.score, ranked.c.matched, ranked.c.strong)
            .where(ranked.c.place <= per_document)
            .order_by(
                desc(ranked.c.score * func.power(_SAME_DOCUMENT_DECAY, ranked.c.place - 1)), desc(ranked.c.density), ranked.c.id
            )
            .limit(limit)
        )
    ).all()
    return [(chunk_id, float(value), count / len(groups), bool(hits)) for chunk_id, value, count, hits in rows]
