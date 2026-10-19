"""Hotel stay questions search live, then fall back to Larissa's saved list."""
import os
import tempfile
import threading
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="aurora-hotel-"))

import app


class Block:
    def __init__(self, type, text=None):
        self.type = type
        self.text = text


class Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


STAY_EN = "Where should I stay in Rome for the wedding?"
STAY_PT = "Onde ficar em Roma? Me indica um hotel perto da igreja."


def _live_hotels():
    return Resp([
        Block("server_tool_use"),
        Block("web_search_tool_result"),
        Block("text", (
            "Hotel Castellino is still the closest. "
            "Also Hotel Artemide, well reviewed, about €280 a night. "
            "https://www.booking.com/hotel/it/artemide.html"
        )),
    ])


def test_stay_questions_are_detected_and_status_questions_are_not():
    stay = [
        STAY_EN,
        "Can you help me find a hotel near the church?",
        "Any hotel recommendations?",
        "hotels in Rome",
        "Looking for an Airbnb near Piazza Venezia",
        "I need a hotel",
        STAY_PT,
        "Me ajuda a achar um hotel no centro",
        "Tem pousada perto da igreja?",
        "Quero um apartamento em Roma",
    ]
    not_stay = [
        "Is my accommodation covered?",
        "Is my hotel paid for?",
        "Minha hospedagem está paga?",
        "What time is the ceremony?",
        "Where is the RSVP form?",
        "Is there a bus to the church?",
        "Where is the hotel pickup?",
        "What's the dress code? We'll stay in a hotel.",
        "Is breakfast included at the hotel?",
        "Can you recommend a tour in Rome?",
    ]
    for text in stay:
        assert app.is_hotel_stay_question(text), text
    for text in not_stay:
        assert not app.is_hotel_stay_question(text), text


def test_acks_match_the_flight_pattern_and_the_guest_language():
    assert app.hotel_search_ack(STAY_EN) == app.HOTEL_SEARCH_ACK_EN
    assert "search now" in app.HOTEL_SEARCH_ACK_EN
    assert "good options" in app.HOTEL_SEARCH_ACK_EN
    assert app.hotel_search_ack(STAY_PT) == app.HOTEL_SEARCH_ACK_PT
    assert "pesquisar" in app.HOTEL_SEARCH_ACK_PT
    assert app.HOTEL_SEARCH_MAX_USES <= 3
    assert app.HOTEL_SEARCH_MAX_USES < app.FLIGHT_SEARCH_MAX_USES


def test_scam_warning_scope():
    assert app.needs_booking_scam_warning(STAY_EN)
    assert app.needs_booking_scam_warning("Can you recommend a tour in Rome?")
    assert app.needs_booking_scam_warning("Book a private transfer from the airport")
    assert app.needs_booking_scam_warning("Me indica um traslado do aeroporto?")
    assert not app.needs_booking_scam_warning("Is my accommodation covered?")
    assert not app.needs_booking_scam_warning("What time is the ceremony?")
    assert not app.needs_booking_scam_warning("Is there a bus to the church?")
    assert not app.needs_booking_scam_warning("What's the Pix for the honeymoon gift?")
    assert "bank transfer" in app.scam_warning(STAY_EN).lower()
    assert "reviews" in app.scam_warning(STAY_EN).lower()
    assert "transferência" in app.scam_warning(STAY_PT).lower()
    assert "avalia" in app.scam_warning(STAY_PT).lower()


def test_override_asks_for_saved_hotels_first_and_a_small_search():
    note = app.hotel_search_override(STAY_EN, [])
    assert "web_search" in note
    assert "Castellino" in note
    assert "Aracoeli" in note
    assert "22" in note and "27" in note
    assert "Booking.com" in note
    tool = app._hotel_tool(app.HOTEL_SEARCH_MAX_USES)
    assert tool == {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": app.HOTEL_SEARCH_MAX_USES,
    }


def test_reply_without_a_search_is_not_the_answer():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Resp([Block("text", "Hotel Castellino, about €312, from my notes.")])
        return _live_hotels()

    app._anthropic_messages_create = fake
    text = app.lookup_live_hotels(
        "Você é Aurora",
        [{"role": "user", "content": STAY_EN}],
        STAY_EN,
    )
    assert "Artemide" in text
    assert "€280" in text
    assert text.lower().index("castellino") < text.lower().index("artemide")
    assert "bank transfer" in text.lower()
    assert calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert calls[0]["extra_body"]["tools"][0]["type"] == "web_search_20250305"
    assert calls[0]["extra_body"]["tools"][0]["max_uses"] == app.HOTEL_SEARCH_MAX_USES
    assert calls[0]["timeout"] == app.HOTEL_SEARCH_TIMEOUT_SECONDS
    assert len(calls) == 2


def test_search_error_uses_labelled_saved_hotels():
    def fake(**kwargs):
        if "extra_body" in kwargs:
            raise TimeoutError("web search timed out")
        return Resp([Block("text", "Hotel Castellino and Hotel Hiberia are the closest.")])

    app._anthropic_messages_create = fake
    text = app.lookup_live_hotels(
        "Você é Aurora",
        [{"role": "user", "content": STAY_EN}],
        STAY_EN,
    )
    assert "not a live search" in text.lower()
    assert "Castellino" in text
    assert "Hiberia" in text
    assert "Regno" in text
    assert "booking.com" in text.lower()
    assert "bank transfer" in text.lower()
    assert "reviews" in text.lower()


def test_total_failure_sends_the_saved_list_in_portuguese():
    def fake(**kwargs):
        raise TimeoutError("down")

    app._anthropic_messages_create = fake
    text = app.lookup_live_hotels(
        "Você é Aurora",
        [{"role": "user", "content": STAY_PT}],
        STAY_PT,
    )
    low = text.lower()
    assert "não uma busca ao vivo" in low
    assert "castellino" in low
    assert "hiberia" in low
    assert "regno" in low
    assert "umiltà" in low or "umilta" in low
    assert "booking.com/share-rpp6d1l" in low
    assert "transferência" in low
    assert "avalia" in low


def test_followup_keeps_hotel_search_until_the_topic_changes():
    phone = "+15550005551"
    app._hotel_context_phones.add(phone)
    assert app.is_hotel_stay_question("Anything closer to the church?", phone)
    assert app.is_hotel_stay_question("A cheaper one", phone)
    assert not app.is_hotel_stay_question("What time is the ceremony?", phone)
    assert phone not in app._hotel_context_phones


def test_get_aurora_response_acks_then_returns_live_hotels():
    app._anthropic_messages_create = lambda **kwargs: _live_hotels()
    phone = "+15550005552"
    app.conversations.pop(phone, None)
    app._flight_inline.on = True
    try:
        reply = app.get_aurora_response(phone, STAY_EN)
    finally:
        app._flight_inline.on = False
    assert reply.startswith(app.HOTEL_SEARCH_ACK_EN)
    assert "Artemide" in reply
    assert "bank transfer" in reply.lower()
    assert app._flight_inline.messages[0] == app.HOTEL_SEARCH_ACK_EN
    assert "Artemide" in app._flight_inline.messages[1]


def test_portuguese_hotel_ack_on_the_guest_path():
    app._anthropic_messages_create = lambda **kwargs: _live_hotels()
    phone = "+15550005553"
    app.conversations.pop(phone, None)
    app._flight_inline.on = True
    try:
        reply = app.get_aurora_response(phone, STAY_PT)
    finally:
        app._flight_inline.on = False
    assert reply.startswith(app.HOTEL_SEARCH_ACK_PT)


def test_flight_question_still_uses_the_flight_search():
    app._anthropic_messages_create = lambda **kwargs: Resp([
        Block("server_tool_use"),
        Block("web_search_tool_result"),
        Block("text", "Delta, 1 stop, about $740. https://www.google.com/travel/flights"),
    ])
    phone = "+15550005554"
    app.conversations.pop(phone, None)
    app._hotel_context_phones.add(phone)
    app._flight_inline.on = True
    try:
        reply = app.get_aurora_response(phone, "What's the cheapest flight from nyc, and a hotel too?")
    finally:
        app._flight_inline.on = False
    assert reply.startswith(app.FLIGHT_SEARCH_ACK_EN)
    assert "$740" in reply
    assert phone not in app._hotel_context_phones


def test_rsvp_and_accommodation_status_do_not_search_or_warn():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return Resp([Block("text", "Here is what I know.")])

    app._anthropic_messages_create = fake
    for phone, message in (
        ("+15550005555", "Where is the RSVP form?"),
        ("+15550005556", "Is my accommodation covered?"),
        ("+15550005557", "What time is the ceremony?"),
        ("+15550005558", "Is there a bus to the church?"),
    ):
        calls.clear()
        app.conversations.pop(phone, None)
        reply = app.get_aurora_response(phone, message)
        assert calls, message
        assert "extra_body" not in calls[0], message
        assert "search now" not in reply.lower(), message
        assert "pesquisar agora" not in reply.lower(), message
        assert "bank transfer" not in reply.lower(), message
        assert "transferência" not in reply.lower(), message


def test_tour_and_transfer_get_a_scam_warning_without_a_hotel_search():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return Resp([Block("text", "A morning outing is a nice plan.")])

    app._anthropic_messages_create = fake
    phone = "+15550005559"
    app.conversations.pop(phone, None)
    reply = app.get_aurora_response(phone, "Can you recommend a tour in Rome?")
    assert "extra_body" not in calls[0]
    assert "morning outing" in reply
    assert "bank transfer" in reply.lower()
    assert "reviews" in reply.lower()
    assert "avaliações" in calls[0]["system"]

    calls.clear()
    phone = "+15550005560"
    app.conversations.pop(phone, None)
    reply = app.get_aurora_response(phone, "Me indica um traslado do aeroporto?")
    assert "extra_body" not in calls[0]
    assert not reply.startswith(app.HOTEL_SEARCH_ACK_PT)
    assert "transferência" in reply.lower()
    assert "avalia" in reply.lower()


def test_scam_warning_is_not_repeated():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return Resp([Block("text", "Book a small group tour. " + app.SCAM_WARNING_EN)])

    app._anthropic_messages_create = fake
    phone = "+15550005561"
    app.conversations.pop(phone, None)
    reply = app.get_aurora_response(phone, "Can you recommend a tour?")
    assert reply.lower().count("bank transfer") == 1


def test_admin_reply_does_not_run_a_hotel_search():
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return Resp([Block("text", "Twelve guests have replied.")])

    app._anthropic_messages_create = fake
    reply = app.get_admin_response("+16463390886", "how many rsvps, and any hotel tips?")
    assert calls
    assert all("extra_body" not in call for call in calls)
    assert "search now" not in reply.lower()
    assert reply == "Twelve guests have replied."


def test_test_chat_returns_the_ack_and_the_hotel_results(monkeypatch):
    monkeypatch.setenv("TEST_CHAT_SECRET", "hotel-secret")
    app._flight_inline.on = False
    app._anthropic_messages_create = lambda **kwargs: _live_hotels()
    phone = "+15550005571"
    app.conversations.pop(phone, None)
    client = app.app.test_client()
    resp = client.post(
        "/test-chat",
        json={"phone": phone, "message": STAY_EN},
        headers={"X-Test-Secret": "hotel-secret"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reply"].startswith(app.HOTEL_SEARCH_ACK_EN)
    assert "Artemide" in data["reply"]
    assert "bank transfer" in data["reply"].lower()
    assert data["messages"][0] == app.HOTEL_SEARCH_ACK_EN
    assert "Castellino" in data["messages"][1]


def test_webhook_acks_a_hotel_question_then_sends_results(monkeypatch):
    gate = threading.Event()
    sends = []

    def slow(**kwargs):
        if "extra_body" not in kwargs:
            return Resp([Block("text", "Hotel Castellino and Hotel Hiberia, saved list.")])
        gate.wait(3)
        return _live_hotels()

    app._anthropic_messages_create = slow
    monkeypatch.setattr(app, "send_zapi_message", lambda phone, message: sends.append(message))
    app.processed_message_ids.clear()
    phone = "15550005562"
    app.conversations.pop(phone, None)
    app.processing.discard(phone)
    client = app.app.test_client()
    payload = {
        "phone": phone,
        "messageId": "msg-hotel-1",
        "text": {"message": STAY_EN},
    }
    first = client.post("/zapi", json=payload)
    assert first.status_code == 200
    assert sends == [app.HOTEL_SEARCH_ACK_EN]
    duplicate = client.post("/zapi", json=payload)
    assert duplicate.status_code == 200
    assert sends == [app.HOTEL_SEARCH_ACK_EN]
    gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and len(sends) < 2:
        time.sleep(0.05)
    assert len(sends) == 2
    assert "Artemide" in sends[1]
    assert "bank transfer" in sends[1].lower()


def test_long_hotel_reply_stays_within_whatsapp_and_keeps_the_warning():
    text = app.finish_hotel_reply("A" * 5000, STAY_EN)
    assert len(text) <= app.WHATSAPP_TEXT_LIMIT
    assert "bank transfer" in text.lower()
    assert "Castellino" in text
