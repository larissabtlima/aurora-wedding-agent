"""Local checks for webhook auth, the personal-number reply, and SQLite memory.

No live calls to Z-API, Anthropic, or Google.
"""
import json
import os
import tempfile

os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="aurora-test-")
os.environ.pop("SHEETS_WEBHOOK_URL", None)
os.environ.pop("GUEST_DIRECTORY_SECRET", None)
os.environ.pop("ZAPI_WEBHOOK_SECRET", None)
os.environ.pop("ZAPI_INSTANCE_ID", None)
os.environ.pop("ZAPI_TOKEN", None)

import app  # noqa: E402


class Boom(Exception):
    pass


class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _fail_if_called(*_a, **_k):
    raise Boom("model should not have been called")


def test_health():
    rv = app.app.test_client().get("/health")
    assert rv.status_code == 200
    assert rv.get_json()["status"].startswith("Aurora")


def test_personal_reply_languages_and_wedding_veto():
    number = "+1 646 339 0886"
    cases = {
        "Oi Larissa, tudo bem?": ("pt", "não recebe mais mensagens pessoais"),
        "Larissa me liga": ("pt", "não recebe mais mensagens pessoais"),
        "me liga quando puder": ("pt", "não recebe mais mensagens pessoais"),
        "Lari, saudades": ("pt", "não recebe mais mensagens pessoais"),
        "Hey is this Larissa?": ("en", "doesn't get personal messages"),
        "Larissa?": ("en", "doesn't get personal messages"),
        "call me when you can": ("en", "doesn't get personal messages"),
        "Ciao Larissa, come stai?": ("it", "non riceve più messaggi personali"),
        "Sei Larissa?": ("it", "non riceve più messaggi personali"),
        "Chiamami Larissa": ("it", "non riceve più messaggi personali"),
    }
    for message, (lang_bit, snippet) in cases.items():
        reply = app.personal_reply_for(message)
        assert reply, message
        assert number in reply, message
        assert snippet in reply, (message, reply)

    wedding = [
        "Oi! Qual o dress code?",
        "What time is the ceremony?",
        "Hi, this is Cian Mc Donnell. Is my accommodation paid for?",
        "Larissa, que horas é o casamento?",
        "Larissa, is the hotel booked?",
        "Hey Larissa, what's the dress code?",
        "Ciao, a che ora è la cerimonia?",
        "Ciao Larissa, a che ora è il matrimonio?",
        "Larissa, ci vediamo al ricevimento?",
        "Posso levar acompanhante?",
        "flights from Dublin",
        "Can you call me a taxi",
        "How did Larissa and Robert meet?",
        "",
    ]
    for message in wedding:
        assert app.personal_reply_for(message) is None, message


def test_personal_reply_does_not_call_the_model(monkeypatch):
    monkeypatch.setattr(app.anthropic_client.messages, "create", _fail_if_called)
    phone = "353830000001"
    reply = app.get_aurora_response(phone, "Oi Larissa, tudo bem?")
    assert "+1 646 339 0886" in reply
    assert "não recebe mais" in reply
    roles = [m["role"] for m in app.conversations[phone]]
    assert roles[-2:] == ["user", "assistant"]


def test_wedding_question_still_calls_the_model(monkeypatch):
    seen = {}

    def create(**kwargs):
        seen["messages"] = kwargs["messages"]
        seen["model"] = kwargs["model"]
        return _Resp("The ceremony is at 3:30 PM.")

    monkeypatch.setattr(app.anthropic_client.messages, "create", create)
    reply = app.get_aurora_response("353830000002", "What time is the ceremony?")
    assert "3:30" in reply
    assert seen["model"] == "claude-haiku-4-5-20251001"
    assert seen["messages"][-1]["content"] == "What time is the ceremony?"


def test_model_only_sees_the_recent_tail(monkeypatch):
    phone = "353830000003"
    app.conversations[phone] = []
    for i in range(STORED := 30):
        role = "user" if i % 2 == 0 else "assistant"
        app.add_to_conversation(phone, role, f"turn-{i}")
    seen = {}

    def create(**kwargs):
        seen["messages"] = kwargs["messages"]
        return _Resp("ok")

    monkeypatch.setattr(app.anthropic_client.messages, "create", create)
    app.get_aurora_response(phone, "latest question about the church")
    sent = seen["messages"]
    assert sent[0]["role"] == "user"
    assert sent[-1]["content"] == "latest question about the church"
    assert len(sent) <= app.MODEL_HISTORY_LIMIT
    assert len(app.conversations[phone]) <= app.STORED_HISTORY_LIMIT
    # The older turns are still stored, just not sent.
    assert app.conversations[phone][0]["content"] == "turn-0"
    assert STORED == 30


def test_sqlite_remembers_across_reload_and_migrates_json():
    conn = app._get_db()
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM phone_registry")
    conn.execute("DELETE FROM known_phones")
    conn.commit()
    app.conversations.clear()
    app.admin_conversations.clear()
    app.phone_registry.clear()
    app.all_phones.clear()

    with open(app.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "conversations": {"353830000009": [{"role": "user", "content": "from json"}]},
            "admin_conversations": {},
            "phone_registry": {"353830000009": "Mary Daly"},
            "all_phones": ["353830000009"],
        }, f)
    app.load_state()
    assert app.conversations["353830000009"][0]["content"] == "from json"
    assert app.phone_registry["353830000009"] == "Mary Daly"
    assert "353830000009" in app.all_phones

    # Once SQLite has the memory, a later edit to the old JSON file is ignored.
    with open(app.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "conversations": {"353830000009": [{"role": "user", "content": "json changed"}]},
            "phone_registry": {},
            "all_phones": [],
        }, f)
    app.conversations.clear()
    app.phone_registry.clear()
    app.all_phones.clear()
    app.load_state()
    assert app.conversations["353830000009"][0]["content"] == "from json"
    assert app.phone_registry["353830000009"] == "Mary Daly"

    app.add_to_conversation("353830000009", "assistant", "still here")
    app.save_state()
    app.conversations.clear()
    app.load_state()
    assert app.conversations["353830000009"][-1]["content"] == "still here"


def test_webhook_auth_and_admin_block(monkeypatch):
    monkeypatch.delenv("ZAPI_WEBHOOK_SECRET", raising=False)
    broadcasts = []
    admin_calls = []
    guest_calls = []
    sent = []

    def fake_broadcast(phone, message):
        stripped = (message or "").strip().upper()
        if stripped.startswith("[ALL]") or stripped.startswith("[BRIDAL]"):
            broadcasts.append((phone, message))
            return "started"
        return None

    monkeypatch.setattr(app, "try_handle_broadcast", fake_broadcast)
    monkeypatch.setattr(app, "get_admin_response", lambda *a, **k: admin_calls.append(a) or "admin-reply")
    monkeypatch.setattr(app, "get_aurora_response", lambda *a, **k: guest_calls.append(a) or "guest-reply")
    monkeypatch.setattr(app, "send_zapi_message", lambda *a, **k: sent.append(a))

    client = app.app.test_client()
    admin_phone = "16463390886"
    guest_phone = "353831110000"

    # Secret unset: a forged [ALL] must not broadcast. A normal guest still gets a reply.
    forged = client.post("/zapi", json={"phone": admin_phone, "text": {"message": "[ALL] hello everyone"}})
    assert forged.status_code == 200
    assert broadcasts == []
    assert sent == []

    admin_query = client.post("/zapi", json={"phone": admin_phone, "text": {"message": "[admin] resumo geral"}})
    assert admin_query.status_code == 200
    assert admin_calls == []

    guest = client.post("/zapi", json={"phone": guest_phone, "text": {"message": "What time is the ceremony?"}})
    assert guest.status_code == 200
    assert guest_calls and guest_calls[-1][1] == "What time is the ceremony?"
    assert sent and sent[-1][1] == "guest-reply"

    # An admin asking a normal question, with no admin prefix, still gets Aurora.
    before = len(guest_calls)
    normal_admin = client.post("/zapi", json={"phone": admin_phone, "text": {"message": "What time is the ceremony?"}})
    assert normal_admin.status_code == 200
    assert len(guest_calls) == before + 1
    assert broadcasts == []

    # Secret set: missing or wrong token is rejected and does nothing.
    monkeypatch.setenv("ZAPI_WEBHOOK_SECRET", "test-secret-value")
    rejected = client.post("/zapi", json={"phone": admin_phone, "text": {"message": "[ALL] hello everyone"}})
    assert rejected.status_code == 401
    assert broadcasts == []

    wrong = client.post("/zapi?token=nope", json={"phone": admin_phone, "text": {"message": "[ALL] hello everyone"}})
    assert wrong.status_code == 401
    assert broadcasts == []

    # Right token via query, path, or header: the broadcast is allowed.
    ok_query = client.post(
        "/zapi?token=test-secret-value",
        json={"phone": admin_phone, "text": {"message": "[ALL] hello everyone"}},
    )
    assert ok_query.status_code == 200
    assert len(broadcasts) == 1

    ok_path = client.post(
        "/zapi/test-secret-value",
        json={"phone": admin_phone, "text": {"message": "[BRIDAL] hello"}},
    )
    assert ok_path.status_code == 200
    assert len(broadcasts) == 2

    ok_header = client.post(
        "/zapi",
        json={"phone": admin_phone, "text": {"message": "[admin] resumo geral"}},
        headers={"Client-Token": "test-secret-value"},
    )
    assert ok_header.status_code == 200
    assert len(admin_calls) == 1

    # Guests also need the token once it is set, and then behave as before.
    blocked_guest = client.post("/zapi", json={"phone": guest_phone, "text": {"message": "dress code?"}})
    assert blocked_guest.status_code == 401
    allowed_guest = client.post(
        "/zapi?token=test-secret-value",
        json={"phone": guest_phone, "text": {"message": "dress code?"}},
    )
    assert allowed_guest.status_code == 200
    assert guest_calls[-1][1] == "dress code?"


def test_token_compare_does_not_throw_on_different_lengths():
    assert app._token_matches("short", "much-longer-secret") is False
    assert app._token_matches("same-secret", "same-secret") is True
    assert app._token_matches("", "same-secret") is False
