"""Live web search is attached only when a guest needs current info."""
import os
import tempfile

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="aurora-search-")

import app


class Block:
    def __init__(self, type, text=None):
        self.type = type
        self.text = text


class Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def _aurora(**kwargs):
    base = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": "Você é Aurora, a assistente do casamento.",
    }
    base.update(kwargs)
    return base


def test_search_reply_joins_text_blocks_and_caps_uses():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return Resp([
            Block("text", "Ryanair, one stop in Dublin."),
            Block("server_tool_use"),
            {"type": "web_search_tool_result", "content": []},
            Block("text", "About €90 today."),
        ])

    app._anthropic_messages_create = fake
    message = app.aurora_messages_create(**_aurora(
        messages=[{"role": "user", "content": "Flights from Dublin to Rome FCO?"}],
    ))

    assert len(calls) == 1
    tool = calls[0]["extra_body"]["tools"][0]
    assert tool == {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
    }
    assert calls[0]["timeout"] == app.WEB_SEARCH_TIMEOUT_SECONDS
    assert message.content[0].text == "Ryanair, one stop in Dublin. About €90 today."
    assert "server_tool_use" not in message.content[0].text


def test_tool_failure_falls_back_to_a_normal_reply():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if "extra_body" in kwargs:
            raise TimeoutError("web search timed out")
        return Resp([Block("text", "From the notes: Ryanair, prices as of 1 August 2026.")])

    app._anthropic_messages_create = fake
    message = app.aurora_messages_create(**_aurora(
        messages=[{"role": "user", "content": "quanto custa a passagem saindo de São Paulo?"}],
    ))

    assert len(calls) == 2
    assert "extra_body" not in calls[1]
    assert calls[1]["messages"] == calls[0]["messages"]
    assert message.content[0].text == "From the notes: Ryanair, prices as of 1 August 2026."


def test_normal_question_does_not_search():
    calls = []
    reply = Resp([Block("text", "The ceremony is at 3:30 PM.")])

    def fake(**kwargs):
        calls.append(kwargs)
        return reply

    app._anthropic_messages_create = fake
    message = app.aurora_messages_create(**_aurora(
        messages=[{"role": "user", "content": "What time is the ceremony?"}],
    ))

    assert len(calls) == 1
    assert "extra_body" not in calls[0]
    assert message is reply
    assert message.content[0].text == "The ceremony is at 3:30 PM."


LARISSA_FLIGHT = (
    "Hey aurora, what's the cheapest flight you can find for the wedding next year for me from nyc?"
)


def _live_fare():
    return Resp([
        Block("server_tool_use"),
        Block("web_search_tool_result"),
        Block("text", "Delta, 1 stop, about $740. Approximate as of today. https://www.google.com/travel/flights"),
    ])


def test_larissa_question_is_a_flight_search_with_wedding_defaults():
    assert app.is_flight_or_fare_question(LARISSA_FLIGHT)
    assert app.guess_origin(LARISSA_FLIGHT) == "New York"
    assert app.assume_return_date(LARISSA_FLIGHT) == "Sunday 27 June 2027"
    note = app.flight_search_override(LARISSA_FLIGHT, [])
    assert "New York" in note
    assert "24 de junho de 2027" in note
    assert "web_search" in note
    assert app.flight_search_ack(LARISSA_FLIGHT) == app.FLIGHT_SEARCH_ACK_EN


def test_saved_prices_without_a_search_are_not_the_answer():
    """The live bug: Haiku quoted the August 2026 table and never called the tool."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Resp([Block("text", "ITA Airways — ~$1,061.83 without baggage.")])
        return _live_fare()

    app._anthropic_messages_create = fake
    text = app.lookup_live_flights(
        "Você é Aurora",
        [{"role": "user", "content": LARISSA_FLIGHT}],
        LARISSA_FLIGHT,
    )
    assert "1,061.83" not in text
    assert "$740" in text
    assert calls[0]["extra_body"]["tools"][0]["type"] == "web_search_20250305"
    assert calls[0]["extra_body"]["tools"][0]["max_uses"] == 5
    assert "1º de agosto de 2026" in calls[0]["system"]
    assert len(calls) == 2


def test_search_error_still_sends_labelled_earlier_research():
    def fake(**kwargs):
        if "extra_body" in kwargs:
            raise TimeoutError("web search timed out")
        return Resp([Block("text", "ITA Airways from the saved table, about $1,061.")])

    app._anthropic_messages_create = fake
    text = app.lookup_live_flights(
        "Você é Aurora",
        [{"role": "user", "content": "flights from nyc"}],
        "flights from nyc",
    )
    assert "earlier research" in text.lower()
    assert "1,061" in text
    assert "google.com/travel/flights" in text.lower() or "Google Flights" in text or "google.com/travel/flights" in text


def test_followup_keeps_searching_until_the_topic_changes():
    phone = "+15550002222"
    app._flight_context_phones.add(phone)
    assert app.is_flight_or_fare_question("Can you do that yourself now?", phone)
    assert app.is_flight_or_fare_question("Just for the wedding", phone)
    assert not app.is_flight_or_fare_question("What time is the ceremony?", phone)
    assert phone not in app._flight_context_phones


def test_get_aurora_response_acks_then_returns_live_fares_for_test_chat():
    app._anthropic_messages_create = lambda **kwargs: _live_fare()
    app.conversations.pop("+15550003333", None)
    app._flight_inline.on = True
    try:
        reply = app.get_aurora_response("+15550003333", LARISSA_FLIGHT)
    finally:
        app._flight_inline.on = False
    assert reply.startswith(app.FLIGHT_SEARCH_ACK_EN)
    assert "$740" in reply
    assert app._flight_inline.messages[0] == app.FLIGHT_SEARCH_ACK_EN
    assert "$740" in app._flight_inline.messages[1]


def test_portuguese_flight_ack():
    assert "buscar" in app.flight_search_ack("quanto custa a passagem saindo de São Paulo?")


def test_webhook_acks_immediately_and_does_not_duplicate(monkeypatch):
    import threading
    import time
    gate = threading.Event()
    sends = []

    def slow(**kwargs):
        if "extra_body" not in kwargs:
            return Resp([Block("text", "earlier research from Larissa: about $900")])
        gate.wait(3)
        return _live_fare()

    app._anthropic_messages_create = slow
    monkeypatch.setattr(app, "send_zapi_message", lambda phone, message: sends.append(message))
    app.processed_message_ids.clear()
    app.conversations.pop("15550004444", None)
    client = app.app.test_client()
    payload = {
        "phone": "15550004444",
        "messageId": "msg-flight-1",
        "text": {"message": LARISSA_FLIGHT},
    }
    first = client.post("/zapi", json=payload)
    assert first.status_code == 200
    assert sends == [app.FLIGHT_SEARCH_ACK_EN]
    duplicate = client.post("/zapi", json=payload)
    assert duplicate.status_code == 200
    assert sends == [app.FLIGHT_SEARCH_ACK_EN]
    gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and len(sends) < 2:
        time.sleep(0.05)
    assert len(sends) == 2
    assert "$740" in sends[1]


def test_long_search_reply_stays_within_whatsapp_limit():
    def fake(**kwargs):
        return Resp([Block("text", "A" * 5000)])

    app._anthropic_messages_create = fake
    message = app.aurora_messages_create(**_aurora(
        messages=[{"role": "user", "content": "weather in Rome in June?"}],
    ))
    assert len(message.content[0].text) <= app.WHATSAPP_TEXT_LIMIT
    assert message.content[0].text.endswith("…")
