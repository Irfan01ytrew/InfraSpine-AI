from app.rag.answer import Turn
from app.services.conversations import _history_terms, _select_history_context


def test_long_conversation_keeps_relevant_older_topic_and_recent_dialogue():
    history = [Turn("Codelab proposal budget?", "The Codelab campaign budget was PKR 500,000.")]
    history.extend(
        Turn(f"Explain unrelated science topic {index}", f"A brief explanation of science topic {index}.")
        for index in range(60)
    )

    selected = _select_history_context(history, "Codelab ka budget kitna tha?", recent_limit=3)

    assert len(selected) <= 6
    assert selected[0] == history[0]
    assert selected[-3:] == history[-3:]


def test_short_elaboration_resolves_the_last_topic_after_many_followups():
    history = [Turn("Summarize the Codelab social media proposal", "It plans three campaign phases.")]
    history.extend(Turn("detail btao", "The previous explanation continued the same proposal.") for _ in range(50))

    selected = _select_history_context(history, "poori tafseel batao", recent_limit=3)

    assert len(selected) <= 6
    assert selected[0] == history[0]
    assert selected[-3:] == history[-3:]


def test_topic_switch_selects_matching_older_context_without_old_unrelated_topic():
    history = [Turn("What did the Codelab plan say about budget?", "It listed a campaign budget.")]
    history.extend(Turn(f"What is topic {index}?", f"Topic {index} explanation.") for index in range(10))
    history.append(Turn("Explain solar panels", "They convert sunlight into electricity."))
    history.extend(Turn(f"Solar follow-up {index}", f"Additional solar detail {index}.") for index in range(3))

    selected = _select_history_context(history, "How do solar panels generate electricity?", recent_limit=3)

    assert any(turn.question == "Explain solar panels" for turn in selected)
    assert not any(turn.question == "What did the Codelab plan say about budget?" for turn in selected)


def test_followup_can_find_a_topic_first_mentioned_in_an_older_answer():
    history = [Turn("Summarize the Codelab campaign plan", "It proposes an estimated budget of PKR 500,000.")]
    history.extend(Turn(f"Unrelated topic {index}", f"Unrelated explanation {index}.") for index in range(20))

    selected = _select_history_context(history, "uska budget kitna tha?", recent_limit=3)

    assert selected[0] == history[0]


def test_urdu_particles_do_not_outvote_the_older_relevant_topic():
    target = Turn("کوڈلیب کا بجٹ؟", "مہم کا بجٹ 500,000 روپے تھا۔")
    history = [target]
    history.extend(
        Turn("اس کی کیا تفصیل ہے؟", "یہ اس میں کیا ہے۔")
        for _ in range(20)
    )

    assert "کیا" not in _history_terms("اس کا بجٹ کیا تھا؟")
    selected = _select_history_context(history, "اس کا بجٹ کیا تھا؟", recent_limit=3)

    assert target in selected
