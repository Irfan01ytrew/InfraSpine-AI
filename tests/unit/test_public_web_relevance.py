import asyncio

from app.services.public_web import (
    _CRICBUZZ_SCOREBOARD_URL,
    _cricket_events,
    _cricket_snapshot_answer,
    _CricbuzzScoreboard,
    _CricbuzzMatchDescription,
    _cricbuzz_live_scoreboard,
    _cricbuzz_state,
    is_current_cricket_request,
    _model_denies_available_web,
    _candidate_matches,
    _normalize_public_timestamp,
    _page_published_at,
    _prefer_fresh_sources,
    _public_search_queries,
    _source_excerpt,
    public_sources_relevant,
)
from app.rag.chat import _format_web_answer, _public_web_followup_history, _web_context
from app.rag.answer import Turn, is_contextual_followup, is_web_lookup_request, requested_web_urls


def test_direct_cricbuzz_scoreboard_extracts_explicit_state_score_overs_and_source():
    listing = _CricbuzzScoreboard()
    listing.feed(
        '<a href="/live-cricket-scores/177571/scow-vs-pakw-live" '
        'title="Scotland Women vs Pakistan Women, 25th Match - Live">'
        '<span>Scotland Women vs Pakistan Women</span><span>LIVE</span></a>'
        '<a href="/live-cricket-scores/177572/example-preview" title="Example vs Other - Preview">'
        '<span>Example vs Other</span></a>'
    )
    listing.close()
    assert len(listing.matches) == 2
    assert _cricbuzz_state(listing.matches[0]) == "live"
    assert _cricbuzz_state(listing.matches[1]) == "scheduled"
    assert listing.matches[0]["url"].startswith("https://www.cricbuzz.com/live-cricket-scores/")

    details = _CricbuzzMatchDescription()
    details.feed(
        '<html><head><meta name="description" content="Follow PAKW 83/1 (13) '
        'vs SCOW 124/5 (20) with live cricket score updates"></head></html>'
    )
    details.close()
    answer = _cricket_snapshot_answer(
        [{"state": "live", "excerpt": details.description, "source": 1}],
        "2026-11-22T07:25:19Z",
        "CURRENT QUESTION: Is waqt cricket ke matches live chal rahe hain?",
    )
    assert "83/1 (13)" in answer
    assert "Retrieved" not in answer
    assert "fetch ka waqt" in answer


def test_direct_cricbuzz_fetch_ignores_generic_and_duplicate_navigation_links(monkeypatch):
    from app.services import public_web

    listing_html = (
        '<a href="/live-cricket-scores/177571/scow-vs-pakw-live" '
        'title="Scotland Women vs Pakistan Women, 25th Match - Live">Scotland Women vs Pakistan Women LIVE</a>'
        '<a href="/live-cricket-scores/177571/scow-vs-pakw-other-slug" '
        'title="Scotland Women vs Pakistan Women - Live">Scotland Women vs Pakistan Women LIVE</a>'
        '<a href="/live-cricket-scores/177599/generic" title="Live Score">Live Score</a>'
    )
    detail_html = (
        '<meta name="description" content="Follow PAKW 83/1 (13) vs SCOW 124/5 (20) '
        'with live cricket score updates">'
    )

    async def fake_get_public_page(_settings, url, **_kwargs):
        if url == _CRICBUZZ_SCOREBOARD_URL:
            return "text/html", listing_html.encode()
        return "text/html", detail_html.encode()

    monkeypatch.setattr(public_web, "_get_public_page", fake_get_public_page)
    result = asyncio.run(_cricbuzz_live_scoreboard(None, "Is waqt cricket ke matches live hain?"))

    assert result is not None
    assert result["provider"] == "Cricbuzz live scoreboard"
    assert "83/1 (13)" in result["answer"]
    assert len(result["sources"]) == 1
    assert "177571" in result["sources"][0]["url"]


def test_web_synthesis_rejects_stale_roman_urdu_browsing_denial_after_search():
    stale_denial = (
        "Mere paas live internet browsing ya real-time sports scores ki live feeds "
        "tak direct access nahi hai, is liye main matches ki tafseel nahi de sakta."
    )

    assert _model_denies_available_web(stale_denial)
    assert _model_denies_available_web("I cannot browse the internet or access live scores.")
    assert not _model_denies_available_web("I found two current scorecards; one is live.")


def test_roman_urdu_live_cricket_uses_explicit_states_from_trusted_scorecards():
    prompt = "CURRENT QUESTION: axha tm btao is waqt cricket kai kn kn say amatches ho rhyai hain?"
    sources = [
        {
            "title": "ESPN cricket scores",
            "url": "https://www.espn.com/cricket/scores",
            "content": (
                "LIVE LIVE 1st Test, at Wellington, Nov 19-23 2026 New Zealand NZ 368 & 119/2 * "
                "(44.5 ov) North West trail by 209 runs STUMPS 2nd Test at Pune Day 1: Stumps "
                "UPCOMING 3rd Match at Dubai Match yet to begin RESULT 1st T20I England 187/9 Australia "
                "191/7 Australia won by 3 wickets"
            ),
        },
        {
            "title": "Demo score page",
            "url": "https://crickcore.example/live",
            "content": "Live 99th Match Sample data shown 200/1 after 12 overs",
        },
    ]

    events, accepted = _cricket_events(sources)

    assert is_current_cricket_request(prompt)
    assert [event["state"] for event in events] == ["live", "stumps", "scheduled", "completed"]
    assert "North West" not in events[0]["excerpt"]
    assert "North West" not in events[1]["excerpt"]
    assert len(accepted) == 1


def test_cricket_parser_does_not_turn_score_page_navigation_into_fixtures():
    sources = [{
        "title": "Live Cricket Score, Schedule, Latest News, Stats & Videos",
        "url": "https://www.cricbuzz.com/live-cricket-scores/",
        "content": (
            "UPCOMING International, domestic and T20 Series. Cricket Match Archives - "
            "For past scorecards, commentary, and results. Latest Cricket News and photo gallery."
        ),
    }]
    events, accepted = _cricket_events(sources)
    assert events == []
    assert accepted == []


def test_result_heading_alone_does_not_claim_a_match_is_completed():
    sources = [{
        "title": "ESPN cricket scores",
        "url": "https://www.espn.com/cricket/scores",
        "content": "RESULT 1st Match (N), Dubai MI Emirates 174/8 Desert Vipers (19.1/20 ov)",
    }]
    events, accepted = _cricket_events(sources)
    assert events == []
    assert accepted == []


def test_current_match_list_does_not_clutter_with_completed_games_unless_requested():
    events = [
        {"state": "live", "excerpt": "13th Match Team A 56/0", "source": 1},
        {"state": "completed", "excerpt": "1st T20I Team B won by 3 wickets", "source": 1},
    ]

    current = _cricket_snapshot_answer(
        events, "2026-11-22T06:15:00Z", "CURRENT QUESTION:\nIs waqt cricket ke matches ka haal batao"
    )
    results = _cricket_snapshot_answer(
        events, "2026-11-22T06:15:00Z", "CURRENT QUESTION:\nKal kis ne cricket match jeeta?"
    )

    assert "13th Match" in current
    assert "won by 3 wickets" not in current
    assert "won by 3 wickets" in results


def test_live_lookup_keeps_official_iana_and_rfc_evidence_for_test_domains():
    query = "Search the web for IANA special-use domain registry and official RFC guidance. What is .test for?"
    sources = [
        {
            "title": "Special-Use Domain Names",
            "url": "https://www.iana.org/assignments/special-use-domain-names/special-use-domain-names.xhtml",
            "snippet": "IANA maintains a registry of special-use names including test.",
        },
        {
            "title": "RFC 6761: Special-Use Domain Names",
            "url": "https://www.rfc-editor.org/rfc/rfc6761.html",
            "content": "The domain name test. is reserved for testing and examples.",
        },
    ]

    assert all(_candidate_matches(query, source) for source in sources)
    assert public_sources_relevant(query, sources)
    assert "reserved for testing" in _source_excerpt(sources[1], query)


def test_live_lookup_rejects_sources_about_an_unrelated_download_page():
    query = "Search the web for IANA special-use domain registry and official RFC guidance. What is .test for?"
    unrelated = {
        "title": "Download the latest browser",
        "url": "https://downloads.example.org/browser",
        "snippet": "Click here for updates and testing the installer download.",
    }

    assert not _candidate_matches(query, unrelated)
    assert not public_sources_relevant(query, [unrelated])


def test_current_question_adds_compact_topic_query_after_conversational_terms():
    prompt = "CURRENT QUESTION:\nAs of November 21, 2026, what are the latest scores or status of ongoing international cricket matches? If none are live, say so. Give source links."
    queries = _public_search_queries(prompt)

    assert len(queries) == 2
    assert queries[0] == "international cricket live scores today"
    assert queries[-1] == "latest scores international cricket"


def test_compact_current_query_removes_negative_instructions():
    prompt = "CURRENT QUESTION:\nWhat is the latest status of international cricket matches today? If none are live, say so."
    queries = _public_search_queries(prompt)

    assert queries[-1] == "latest international cricket today"


def test_live_cricket_question_searches_score_pages_first():
    prompt = "CURRENT QUESTION:\nWhat is the latest status of international cricket matches today? If none are live, say so."

    assert _public_search_queries(prompt)[0] == "international cricket live scores today"


def test_roman_urdu_spelling_variants_use_canonical_live_cricket_search():
    prompt = "CURRENT QUESTION: axha tm btao is waqt cricket kai kn kn say amatches ho rhyai hain?"

    assert _public_search_queries(prompt)[0] == "international cricket live scores today"


def test_roman_urdu_current_cricket_queries_enter_live_web_route():
    queries = (
        "axha tm btao is waqt cricket kai kn kn say amatches ho rhyai hain?",
        "is waqt cricket ke kon kon se matches ho rahe hain?",
        "aaj cricket matches ka score kya hai?",
        "cricket ka latest score batao",
        "What matches are live in cricket right now?",
    )

    for query in queries:
        assert is_web_lookup_request(query), query
        assert is_current_cricket_request("CURRENT QUESTION: " + query), query


def test_general_questions_do_not_enter_live_web_or_cricket_route():
    for query in ("photosynthesis kya hai?", "Explain how a database index works", "What is compound interest?"):
        assert not is_web_lookup_request(query), query
        assert not is_current_cricket_request("CURRENT QUESTION: " + query), query


def test_universal_browse_intent_handles_bare_domains_and_roman_urdu():
    cases = {
        "infraspine.com par jao": ["https://infraspine.com"],
        "infraspine.com ki website kholo aur company ke bare mein batao": ["https://infraspine.com"],
        "internet se AI agents ka latest comparison search karo": [],
        "Please visit https://www.python.org/about/ and summarize the page": ["https://www.python.org/about/"],
        "www.who.int par ja kar latest dengue guidance dekho": ["https://www.who.int"],
    }
    for question, expected_urls in cases.items():
        assert is_web_lookup_request(question), question
        assert requested_web_urls(question) == expected_urls, question


def test_domain_mention_routes_to_real_site_content_but_email_does_not_become_url():
    assert is_web_lookup_request("What does infraspine.com say about its services?")
    assert requested_web_urls("Contact irfan@infraspine.com about the project") == []


def test_roman_urdu_search_query_discards_request_filler():
    queries = _public_search_queries("CURRENT QUESTION: internet se AI agents ka latest comparison search karo")
    assert queries[0] == "ai agents latest comparison"


def test_current_search_query_drops_output_instructions_and_focuses_on_subject():
    queries = _public_search_queries(
        "CURRENT QUESTION: Find NASA’s latest official Artemis program update. "
        "Summarize it in three points and include the publication date and source link."
    )
    assert queries[0] == "nasa latest artemis"
    assert "nasa latest official artemis" in queries
    assert queries[-1] == "nasa latest artemis official website"


def test_public_search_query_removes_evidence_and_link_format_instructions():
    queries = _public_search_queries(
        "CURRENT QUESTION: Search the official NASA website for the latest Artemis program update. "
        "Give only facts explicitly stated on the official source and include its publication date and clickable source link."
    )

    assert queries[0] == "nasa latest artemis"
    assert "explicitly" not in queries[0]
    assert "clickable" not in queries[0]
    assert "website" not in queries[0]


def test_official_update_rejects_third_party_pages_even_if_they_repeat_the_entity():
    query = "Search the official NASA website for the latest Artemis program update."
    third_party = {
        "title": "NASA Artemis latest update",
        "url": "https://example.com/nasa-artemis-update",
        "snippet": "NASA announced the latest Artemis program update.",
    }

    assert not _candidate_matches(query, third_party)


def test_latest_request_drops_stale_dated_background_page_when_newer_evidence_exists():
    sources = [
        {"title": "Program overview", "published_at": "2022-12-05T17:03:34Z"},
        {"title": "Recent Artemis update", "published_at": "2026-10-07T13:58:40Z"},
        {"title": "Current news hub", "published_at": None},
    ]

    selected = _prefer_fresh_sources(sources, "CURRENT QUESTION: What is the latest Artemis program update?")

    assert [source["title"] for source in selected] == ["Recent Artemis update", "Current news hub"]


def test_named_subjects_and_each_source_must_be_relevant():
    query = "Find NASA’s latest official Artemis program update."
    generic_nasa = {
        "title": "NASA", "url": "https://www.nasa.gov/",
        "snippet": "NASA.gov brings the latest news from America's space agency.",
    }
    artemis_story = {
        "title": "NASA Artemis II mission update", "url": "https://www.nasa.gov/example-artemis-update/",
        "snippet": "NASA Artemis program latest mission update.",
    }
    assert not _candidate_matches(query, generic_nasa)
    assert public_sources_relevant(query, [artemis_story])
    assert not public_sources_relevant(query, [artemis_story, generic_nasa])


def test_cricket_live_lookup_rejects_generic_news_homepages():
    query = "latest international cricket matches today"
    generic_news = {
        "title": "Express News | Latest News",
        "url": "https://www.express.pk/",
        "snippet": "Latest international news, business, and general headlines from around the world.",
    }
    score_page = {
        "title": "Live Cricket Scores and Match Scorecards",
        "url": "https://www.espn.com/cricket/scores",
        "snippet": "Latest international cricket scores and live match scorecards.",
    }

    assert not _candidate_matches(query, generic_news)
    assert _candidate_matches(query, score_page)


def test_public_source_timestamps_are_normalized_and_not_invented():
    html = b'<html><head><meta property="article:published_time" content="2026-11-21T10:15:00+05:00"></head></html>'

    assert _normalize_public_timestamp("Sat, 21 Nov 2026 05:15:00 GMT") == "2026-11-21T05:15:00Z"
    assert _page_published_at("text/html", html) == "2026-11-21T05:15:00Z"
    assert _page_published_at("text/html", b"<html><body>No publication metadata</body></html>") is None


def test_chat_source_links_show_publication_or_retrieval_time():
    published = _format_web_answer({
        "answer": "Current score [S1].",
        "sources": [{
            "title": "Official score page", "url": "https://scores.example.com/live",
            "published_at": "2026-11-21T05:15:00Z", "retrieved_at": "2026-11-21T05:16:00Z",
        }],
    })
    retrieved = _format_web_answer({
        "answer": "Current result [S1].",
        "sources": [{"title": "Live results", "url": "https://scores.example.com/results"}],
    })

    assert "[W1]" in published and "Published: 2026-11-21T05:15:00Z" in published
    assert "Retrieved:" in retrieved and "2026-" in retrieved


def test_live_web_followup_reuses_public_topic_and_resolves_second_source():
    history = [Turn(
        question="Search today's international cricket match status",
        answer=("Two current score pages were found.\n\n### Web sources\n"
                "- [W1] [ESPN live cricket scores](<https://www.espn.com/cricket/scores>)\n"
                "- [W2] [ICC live scores](<https://www.icc-cricket.com/matches>)"),
    )]

    context = _web_context("tell me more about the second one", history, [])

    assert "NORMALIZED LIVE SEARCH QUESTION:" in context
    assert "ICC live scores" in context
    assert "ESPN live cricket scores" not in context.split("NORMALIZED LIVE SEARCH QUESTION:", 1)[1].split("\n\n", 1)[0]


def test_unrelated_web_history_is_not_sent_with_a_new_search_topic():
    history = [Turn(
        question="What services does infraspine.com list?",
        answer="InfraSpine lists IT services [S1].\n\n### Web sources\n- [W1] [InfraSpine](<https://infraspine.com>)",
    )]
    context = _web_context("Find NASA Artemis news", history, [])
    assert "PUBLIC CONVERSATION CONTEXT" not in context
    assert "infraspine.com" not in context


def test_standalone_question_with_relative_that_does_not_inherit_web_sources():
    from app.rag.answer import is_contextual_followup, is_followup_transform, refers_to_previous_document

    news = Turn(
        question="What are the latest major technology news stories today?",
        answer="A technology story was reported.\n\n### Web sources\n- [W1] BBC Technology",
        grounded=False,
    )
    questions = (
        "Explain photosynthesis in three short bullet points.",
        "Write a Python function that removes duplicates while preserving order.",
    )

    for question in questions:
        assert not is_followup_transform(question)
        assert not refers_to_previous_document(question)
        assert not is_contextual_followup(question)
        assert _public_web_followup_history(question, [news]) == []


def test_format_only_web_followup_keeps_its_immediately_previous_public_topic():
    from app.rag.answer import is_contextual_followup

    news = Turn(
        question="Search today's technology news",
        answer="A technology story was reported.\n\n### Web sources\n- [W1] BBC Technology",
        grounded=False,
    )
    assert is_contextual_followup("explain more")
    assert _public_web_followup_history("explain more", [news]) == [news]


def test_roman_urdu_web_followup_uses_matching_older_public_source_not_last_web_topic():
    nasa = Turn(
        question="Search the official NASA website for the latest Artemis program update.",
        answer="Artemis II Preliminary Lunar Science Report was published on October 7, 2026. ### Web sources\n- [W1] NASA Science",
        grounded=False,
    )
    infraspine = Turn(
        question="infraspine.com par jao",
        answer="InfraSpine provides IT services. ### Web sources\n- [W1] InfraSpine",
        grounded=False,
    )
    question = "Us Artemis II report ki publication date kya thi?"

    selected = _public_web_followup_history(question, [nasa, infraspine])

    assert is_contextual_followup(question)
    assert selected == [nasa]
    prompt = _web_context(question, selected, [])
    assert "NASA" in prompt and "InfraSpine" not in prompt


def test_web_context_never_reuses_private_document_answers():
    history = [Turn(
        question="Summarize the confidential HR document",
        answer="Private passage about employee compensation [S1].",
        grounded=True,
    )]

    context = _web_context("what is the current exchange rate?", history, [])

    assert "confidential HR" not in context
    assert "employee compensation" not in context


def test_date_word_alone_does_not_make_an_unrelated_result_topical():
    query = "What is the latest international cricket score for November 21?"
    unrelated = {
        "title": "November",
        "url": "https://en.wikipedia.org/wiki/November",
        "snippet": "November is the eleventh month of the year.",
    }

    assert not _candidate_matches(query, unrelated)
    assert not public_sources_relevant(query, [unrelated])


def test_live_score_source_remains_relevant_after_date_terms_are_ignored():
    query = "latest scores international cricket November 21 2026"
    source = {
        "title": "Desert Vipers vs MI Emirates live score - ILT20",
        "url": "https://www.espncricinfo.com/series/ilt20-2026",
        "snippet": "International League T20 live cricket score and match details.",
    }

    assert _candidate_matches(query, source)


def test_single_subject_news_query_can_match_relevant_page_content():
    from app.services.public_web import _query_terms

    question = "CURRENT QUESTION: What are the latest major technology news stories today? Cite publishers and dates."
    source = {
        "title": "Technology News Today",
        "url": "https://news.example.org/technology",
        "content": "Technology companies announced several developments today.",
    }
    assert _query_terms("technology news stories") == {"technology"}
    assert _candidate_matches(question, source)
    assert public_sources_relevant(question, [source])


def test_short_query_overlap_threshold_never_exceeds_available_terms():
    from app.services.public_web import _query_terms

    terms = _query_terms("technology")
    threshold = min(len(terms), 3, max(1, (len(terms) + 2) // 3)) if terms else 0
    assert threshold == 1


def test_current_news_query_adds_date_qualified_article_search():
    queries = _public_search_queries(
        "CURRENT QUESTION: What are the latest major technology news stories today? Cite publishers and dates."
    )
    assert queries[0].startswith("technology news ")
    assert "2026" in queries[0]
    assert "technology" in queries[0]


def test_current_news_requires_recent_dated_story_pages_not_topic_hubs():
    from datetime import UTC, datetime

    from app.services.public_web import _is_current_news_article, _requires_current_news_articles

    now = datetime(2026, 11, 22, 12, tzinfo=UTC)
    assert _requires_current_news_articles(
        "CURRENT QUESTION: What are the latest major technology news stories today?"
    )
    article = {
        "title": "Technology companies announce new AI safety agreement",
        "url": "https://publisher.example.com/technology/2026/11/21/ai-safety-agreement",
        "published_at": "2026-11-21T10:00:00Z",
    }
    hub = {
        "title": "Technology News",
        "url": "https://publisher.example.com/technology/",
        "published_at": "2026-11-22T08:00:00Z",
    }
    old_story = {
        **article,
        "published_at": "2026-10-01T10:00:00Z",
    }
    assert _is_current_news_article(article, now=now)
    assert not _is_current_news_article(hub, now=now)
    assert not _is_current_news_article(old_story, now=now)


def test_news_and_live_score_request_together_does_not_crash_and_keeps_both_queries():
    # Regression: the news branch replaced the topic-term set with a list, so the following
    # live-score check raised AttributeError for any "news" question that also mentioned cricket.
    queries = _public_search_queries(
        "CURRENT QUESTION: latest cricket news and live match scores today, with sources."
    )
    assert queries[0].startswith("cricket")
    assert " news " in queries[0]
    assert "international cricket live scores today" in queries


def test_recency_word_without_news_noun_does_not_add_a_news_query():
    queries = _public_search_queries("CURRENT QUESTION: taaza AI agents ka comparison batao")
    assert not any(" news " in query for query in queries)


def test_web_capability_questions_do_not_enter_live_lookup():
    from app.rag.answer import is_web_capability_question

    questions = (
        "live web search ki acess h tmhin",
        "live web search ki access hai tumhin?",
        "Do you have live web search access?",
        "can you search the live web?",
        "kya tum web search kar sakte ho?",
        "web search access available hai?",
    )
    for question in questions:
        assert is_web_capability_question(question), question
        assert not is_web_lookup_request(question), question


def test_actual_search_requests_are_not_capability_questions():
    from app.rag.answer import is_web_capability_question

    questions = (
        "Search the official NASA website for the latest Artemis program update",
        "Search the live web for today's cricket scores",
        "internet se AI agents ka latest comparison search karo",
        "can you search the web for the latest Artemis news",
        "What are the latest monsoon rains in Karachi today?",
    )
    for question in questions:
        assert not is_web_capability_question(question), question
        assert is_web_lookup_request(question), question


def test_official_website_query_is_added_for_authority_lookups():
    queries = _public_search_queries(
        "CURRENT QUESTION: Search the official NASA website for the latest Artemis program update."
    )
    assert queries[0] == "nasa latest artemis"
    assert any(query.endswith("official website") and "nasa" in query and "artemis" in query for query in queries)


def test_relaxed_fallback_keeps_subject_pages_when_official_host_is_not_required():
    from app.services.public_web import _candidate_matches, _candidate_matches_relaxed, _requires_official_source

    query = "What is the latest Artemis program update?"
    third_party = {
        "title": "NASA Artemis II mission update",
        "url": "https://www.bbc.com/news/science-artemis-ii",
        "snippet": "NASA announced a new Artemis program update.",
        "content": "NASA Artemis program latest mission update from the space agency.",
    }
    assert not _requires_official_source(query)
    assert _candidate_matches_relaxed(query, third_party)
    # Strict official-host rule does not apply, and the page names both subjects.
    assert _candidate_matches(query, third_party) or _candidate_matches_relaxed(query, third_party)


def test_relaxed_fallback_does_not_override_official_website_requirement():
    from app.services.public_web import _candidate_matches, _candidate_matches_relaxed, _requires_official_source

    query = "Search the official NASA website for the latest Artemis program update."
    third_party = {
        "title": "NASA Artemis latest update",
        "url": "https://example.com/nasa-artemis-update",
        "snippet": "NASA announced the latest Artemis program update.",
        "content": "NASA announced the latest Artemis program update.",
    }
    assert _requires_official_source(query)
    assert not _candidate_matches(query, third_party)
    # Relaxed matching may see the entity names, but the search loop must not use it for official requests.
    assert _candidate_matches_relaxed(query, third_party)
