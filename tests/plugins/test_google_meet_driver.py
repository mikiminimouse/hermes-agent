"""Tests for the meet speaker driver's address/turn-picking logic.

Isolated in its own module because importing the driver loads the profile
dotenv + agent.auxiliary_client at import time.
"""
from __future__ import annotations


def _drv():
    import plugins.google_meet.meet_speaker_driver as d
    return d


def test_addressed_high_recall_gate():
    d = _drv()
    # Real / mangled addresses pass the gate.
    for s in ("Вертер ты тут", "Вектор это к тебе?", "вэртер посмотри", "я вертел это"):
        assert d._addressed(s) is True, s
    # Weather/evening look-alikes ALSO pass (LLM is the precision filter).
    assert d._addressed("какой сегодня ветер") is True
    # Plain words do NOT trip the gate (no needless LLM calls).
    for s in ("верно согласен", "новая версия", "вернёмся к вопросу", "смержи ветку"):
        assert d._addressed(s) is False, s


def test_pick_addressed_turn_reacts_to_in_place_refinement():
    # Meet refines a turn IN PLACE (same id). The driver must notice the TEXT
    # change, not just new ids, or an address folded into a growing turn is
    # missed (live bug wtp-oirr-stc).
    d = _drv()
    processed: set = set()
    replied: set = set()

    # Turn id=3 first has no address.
    a, fresh = d._pick_addressed_turn(["Виталий: скажи пожалуйста это не"], [3], processed, replied)
    assert a is None and len(fresh) == 1

    # Same id=3 GREW and now contains an address → detected despite same id.
    a, fresh = d._pick_addressed_turn(
        ["Виталий: скажи пожалуйста это не вертер ты тут"], [3], processed, replied)
    assert a == 3
    replied.add(a)

    # id=3 grows again, still addressed, but already answered → no re-trigger.
    a, _ = d._pick_addressed_turn(
        ["Виталий: скажи пожалуйста это не вертер ты тут меня"], [3], processed, replied)
    assert a is None

    # A genuinely NEW turn id=4 with an address → answered.
    a, _ = d._pick_addressed_turn(["Виталий: вертер ещё один вопрос"], [4], processed, replied)
    assert a == 4


def test_strip_greeting_removes_leading_hello():
    # Deterministic guard so the bot doesn't re-greet in ongoing replies even
    # when the small model ignores the prompt rule.
    d = _drv()
    assert d._strip_greeting("Привет! Я здесь, что нужно?") == "Я здесь, что нужно?"
    assert d._strip_greeting("Привет, сегодня помогал команде.") == "Сегодня помогал команде."
    assert d._strip_greeting("Здравствуйте! Чем помочь?") == "Чем помочь?"
    assert d._strip_greeting("привет привет, слушаю тебя") == "Слушаю тебя"
    # No leading greeting → untouched.
    assert d._strip_greeting("Да, могу сделать это в фоне.") == "Да, могу сделать это в фоне."
    # A reply that is ONLY a greeting is kept (rare deliberate hello).
    assert d._strip_greeting("Привет") == "Привет"


def test_pick_addressed_turn_ignores_self_and_unchanged():
    d = _drv()
    processed: set = set()
    replied: set = set()
    # The bot's own echo ("You") never counts as an address.
    a, fresh = d._pick_addressed_turn(["You: вертер привет всем"], [0], processed, replied)
    assert a is None and fresh == []
    # An unchanged (id, text) seen twice is handled once (no duplicate reaction).
    a, fresh = d._pick_addressed_turn(["Виталий: вертер слышишь"], [1], processed, replied)
    assert a == 1 and len(fresh) == 1
    # Exact same (id, text) polled again → already handled, nothing fresh.
    a, fresh = d._pick_addressed_turn(["Виталий: вертер слышишь"], [1], processed, replied)
    assert fresh == []
