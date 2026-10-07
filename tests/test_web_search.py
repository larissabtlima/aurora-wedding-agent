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


def test_get_aurora_response_searches_flights_and_answers_plain_questions():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if "extra_body" in kwargs:
            return Resp([
                Block("server_tool_use"),
                Block("text", "ITA Airways, nonstop, about $1,100. Prices change."),
            ])
        return Resp([Block("text", "Ceremony at 3:30 PM at Santa Maria in Aracoeli.")])

    app._anthropic_messages_create = fake
    app.conversations.pop("+15550001111", None)
    flight = app.get_aurora_response("+15550001111", "What flights are there from New York to Rome?")
    plain = app.get_aurora_response("+15550001111", "Thanks, and the church time?")

    assert "ITA Airways" in flight
    assert "server_tool_use" not in flight
    assert calls[0]["extra_body"]["tools"][0]["max_uses"] == 3
    assert "Ceremony at 3:30 PM" in plain
    assert "extra_body" not in calls[1]


def test_long_search_reply_stays_within_whatsapp_limit():
    def fake(**kwargs):
        return Resp([Block("text", "A" * 5000)])

    app._anthropic_messages_create = fake
    message = app.aurora_messages_create(**_aurora(
        messages=[{"role": "user", "content": "weather in Rome in June?"}],
    ))
    assert len(message.content[0].text) <= app.WHATSAPP_TEXT_LIMIT
    assert message.content[0].text.endswith("…")
