import os
import json
import threading
import datetime
import urllib.request
import urllib.parse

from flask import Flask, request, Response, jsonify
from twilio.rest import Client
import anthropic

app = Flask(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"]
)

# ============================================================
# PERSISTENT MEMORY
# Everything Aurora "remembers" is saved to a file on Render's
# persistent disk, so restarts/deploys no longer wipe her memory.
# DATA_DIR must point at the mounted disk (set in Render settings).
# ============================================================
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
DATA_FILE = os.path.join(DATA_DIR, "aurora_data.json")
_save_lock = threading.Lock()

conversations = {}       # phone -> [ {role, content}, ... ]
admin_conversations = {} # phone -> [ {role, content}, ... ]
phone_registry = {}      # phone -> name of the PHONE OWNER
rsvp_data = {}           # guest_name (lowercase) -> rsvp details
all_phones = set()
processing = set()

def with_phone_lock(phone, fn, *args, **kwargs):
    import time
    if phone in processing:
        for _ in range(20):
            time.sleep(0.5)
            if phone not in processing:
                break
        else:
            import sys
            print(f"PHONE LOCK: {phone} still busy after waiting — proceeding anyway to avoid silently dropping a message", file=sys.stderr)

    processing.add(phone)
    try:
        return fn(*args, **kwargs)
    finally:
        processing.discard(phone)

processed_message_ids = set()
guest_flags = {}         # guest_name (lowercase) -> flags
passport_requests = {}   # guest_name (lowercase) -> full collected passport data dict
active_subject = {}      # phone -> name currently being discussed
active_companion = {}    # phone -> LIST of companion names
pending_subject = {}     # phone -> name Aurora just asked to confirm
pending_group_second = {}
pending_companion = {}   
pending_add_plusone = {} 
pending_rsvp_whom = {}   

def _state_dict():
    return {
        "conversations": conversations,
        "admin_conversations": admin_conversations,
        "phone_registry": phone_registry,
        "rsvp_data": rsvp_data,
        "all_phones": list(all_phones),
        "guest_flags": guest_flags,
        "passport_requests": passport_requests,
        "active_subject": active_subject,
        "active_companion": active_companion,
        "pending_subject": pending_subject,
        "pending_group_second": pending_group_second,
        "pending_companion": pending_companion,
        "pending_add_plusone": pending_add_plusone,
        "pending_rsvp_whom": pending_rsvp_whom,
        "known_guest_names": KNOWN_GUEST_NAMES,
        "bridal_party_phones": list(bridal_party_phones),
    }

def save_state():
    with _save_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp_path = DATA_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_state_dict(), f)
            os.replace(tmp_path, DATA_FILE)
        except Exception as e:
            import sys
            print(f"SAVE STATE ERROR: {str(e)}", file=sys.stderr)

def load_state():
    global conversations, admin_conversations, phone_registry, rsvp_data, all_phones, guest_flags, passport_requests, active_subject, pending_subject
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            conversations = data.get("conversations", {})
            admin_conversations = data.get("admin_conversations", {})
            phone_registry = data.get("phone_registry", {})
            rsvp_data = data.get("rsvp_data", {})
            all_phones = set(data.get("all_phones", []))
            guest_flags = data.get("guest_flags", {})
            passport_requests = data.get("passport_requests", {})
            active_subject = data.get("active_subject", {})
            active_companion.update(data.get("active_companion", {}))
            pending_subject = data.get("pending_subject", {})
            pending_group_second.update(data.get("pending_group_second", {}))
            pending_companion.update(data.get("pending_companion", {}))
            pending_add_plusone.update(data.get("pending_add_plusone", {}))
            pending_rsvp_whom.update(data.get("pending_rsvp_whom", {}))
            for extra_name in data.get("known_guest_names", []):
                if extra_name not in KNOWN_GUEST_NAMES:
                    KNOWN_GUEST_NAMES.append(extra_name)
            bridal_party_phones.update(data.get("bridal_party_phones", []))
            import sys
            print(f"LOADED STATE: {len(all_phones)} phones, {len(rsvp_data)} rsvps, {len(KNOWN_GUEST_NAMES)} known guests", file=sys.stderr)
        else:
            import sys
            print("LOADED STATE: no existing data file, starting fresh", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"LOAD STATE ERROR: {str(e)}", file=sys.stderr)

ADMIN_NUMBERS = {"+16463390886", "+19292277546", "+393490541017"}
ADMIN_NUMBERS_NORMALIZED = {n.lstrip("+") for n in ADMIN_NUMBERS}

def normalize_phone(p):
    cleaned = (p or "").replace("whatsapp:", "").replace(" ", "").replace("-", "").strip()
    cleaned = cleaned.lstrip("+")
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
    return cleaned

def is_admin_phone(p):
    return normalize_phone(p) in ADMIN_NUMBERS_NORMALIZED

LARISSA_NUMBER = "+16463390886"
ROB_NUMBER = "+19292277546"

bridal_party_phones = set()
BRIDAL_PARTY_NAMES = {
    "anna laura teixeira", "thaíse silva", "thaise silva",
    "aline olden", "thaís rebuá", "thais rebua",
    "eduarda santana", "linda cahill", "will daly",
    "michael daly", "brendan daly", "chris daly",
    "cian mc donnell", "corey brennan"
}

KNOWN_GUEST_NAMES = [
    "Robert Daly", "Larissa Daly", "Michael Daly", "Mary Daly", "Christopher Daly",
    "Thomas O Brien", "Kornel Cwiklinski", "Alan Cwiklinski", "Patryk Wesolowski",
    "Linda Cahill", "Conor Cahill", "Cathy Cahill", "Ayla Cahill", "Avean Cahill", "Caera Cahill",
    "Will Daly", "Ezgi Atakul", "Brendan Daly", "Deirdre Daly", "Chris Daly", "Cian Mc Donnell",
    "Corey Brennan", "George O Mahony", "Charlotte Barton", "James Roche", "Luke Mccarthty",
    "Sean Murphy", "Joanne Murphy", "Patrick Fitzgibbon", "Stephanie Fitzgibbon", "Shane Burke",
    "Shane Galvin", "Rebecca Perrott", "Mikey O Donovan", "Peter Olden", "Pauline Olden",
    "Mike O'Riordan", "Donica O'Leary", "Kevin Brennan", "Niamh Brennan", "Dylan Leahy",
    "Shane Fitzgerald", "David Dunne", "Aisling Doherty", "David Martin", "Pat O'Halloran",
    "Diana O'Halloran", "Brendan O'Halloran", "Robert Power", "Sarah Power", "Brian Mc Donnell",
    "Mossie Mc Donnell", "Gaye Mc Donnell", "Julie Mc Donnell", "Simon Stewart", "Shane Adams",
    "Ross Martin", "Patrick Daly", "Elizabeth Daly", "Olan Kinsella", "Richard Badurski",
    "Chris Gardner", "Alessandra Grabowski", "Minalkumar Patel", "Asra Warsi", "Loc Trinh",
    "Don Gaudreau", "Scott Lancet", "Erica Lancet", "Dylan Kingston", "Chris Lyons", "Nicole Lyons",
    "Colin Williams", "Carmela Williams", "Molly Elkins", "Adam Taub", "Jonnhy Daly", "Mauna Daly",
    "Margareth Dillworth", "Matt Dilworth", "Lily May", "Liam Kelleher", "Caroline Kelleher",
    "Kristina Kelleher", "Johnny Dilworth", "Seamus Kelleher", "Danielle Dilworth", "Shane Egan",
    "Dan Kelleher", "Emily Forrest", "Gline Mase", "Cathal Reynolds", "Nathan Lockhart",
    "Branden Ciranni", "Paul Murphy", "Luke Mc Carthy", "Eoin Power", "Eleanor Bishop",
    "Yves Sohege", "Niall Mc Grath", "James Mc Hugh", "Patrick Egan", "Orla Cahill", "Lee Hannigan",
    "Caoimhe McSorley", "Dustin Brown", "Bo Landsman", "Tracey Kelleher",
    "Laura Teixeira", "Anna Laura Teixeira", "Fabiano Lima", "Jhenifer Bering", "Alexia Lima",
    "Meira Lima", "Kelly Cristina", "Igor Lima", "Milâine Aparecida", "Jadeilson Lima",
    "Renato Lima", "Leonardo Lima", "Geovanine Mariana", "Aline Mariana", "Rafael Azevedo",
    "Athila Mariano", "Lucinha Mendes", "Nalva Mendes", "Leidy Mendes", "Daiana Ribeiro",
    "Silvio", "Gabriel", "Lindinalva Batista", "Roberto Batista", "Malu Teixeira",
    "Toninho Teixeira", "Angel Gabriel", "Wesley Muniesa", "Laisa Teixeira", "Guilherme",
    "Talles Guilherme", "Maria Fernanda", "Wigney Teixeira", "Izabel Teixeira", "Saide Alves",
    "Bruna Alves", "Roger Boorges", "Hyago Alves", "Maria Clara", "Andre da Silva",
    "Camila Campos", "Debora Araújo", "Thaíse Silva", "Hugo Lopes", "Aline Olden",
    "Thaís Rebuá", "Richard Hoey", "Róisín O'Brien", "Ameer Gazder", "Elisha Bernie",
    "Eimear Flaherty", "Islam Erkale", "Carly Hochhauser", "Mathew Hutton", "Jaya Patel",
    "Wai Mun", "Eduarda Santana", "Mark Donnelly", "Haydee Matos", "Kevin O Dwyer",
    "Paola Gomes", "Jackson Ferreira", "Cian Whyte", "Warley Ferreira", "Ricardo Santos",
    "Ana Luiza", "Andre Villa", "Priscilla Figueiredo", "Andrew Bolton", "Elen Weber",
    "Tay Vieira", "Rafeela", "Leo", "Stephanie Marques", "Ingrid Mariano", "Sean O Sullivan",
    "Diego Alcantara", "Alexia Gouveia"
]
ORIGINAL_GUEST_COUNT = len(KNOWN_GUEST_NAMES)

def find_known_guest(name_query):
    import re
    FILLER_WORDS = {"im", "i'm", "eu", "sou", "meu", "nome", "name", "is", "e", "é", "the", "o", "a"}
    q = name_query.lower().strip()
    if not q:
        return None
    q_tokens = [t for t in re.findall(r"[a-zà-ú']+", q) if t not in FILLER_WORDS]
    if not q_tokens:
        return None
    q_clean = " ".join(q_tokens)

    for known in KNOWN_GUEST_NAMES:
        if known.lower() == q_clean:
            return known

    best = None
    best_score = 0
    for known in KNOWN_GUEST_NAMES:
        k = known.lower()
        k_tokens = set(re.findall(r"[a-zà-ú']+", k))
        if q_clean in k_tokens:
            overlap = 100
        else:
            overlap = len(set(q_tokens) & k_tokens)
        if overlap == 0:
            for qt in q_tokens:
                if not (3 <= len(qt) <= 4):
                    continue
                for kt in k_tokens:
                    if len(kt) >= 3 and (kt.startswith(qt) or qt.startswith(kt)):
                        overlap += 1
                        break
        if overlap > best_score:
            best_score = overlap
            best = known
    return best if best_score > 0 else None

load_state()

def sanitize_for_whatsapp(text):
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|?\s*[-:]+\s*\|.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|(.+)\|$', lambda m: ' • '.join(c.strip() for c in m.group(1).split('|') if c.strip()), text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def log_to_sheets(data_type, data):
    webhook_url = os.environ.get("SHEETS_WEBHOOK_URL", "")
    if not webhook_url:
        import sys
        print(f"SHEETS: No webhook URL configured", file=sys.stderr)
        return
    try:
        payload = json.dumps({"type": data_type, "data": data}).encode()
        req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        result = urllib.request.urlopen(req, timeout=10)
        import sys
        print(f"SHEETS: Logged {data_type} — status {result.status}", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"SHEETS ERROR: {str(e)}", file=sys.stderr)

def add_guest_to_sheet(name, added_by="admin", notes="", with_plus_one=False):
    origin = notes or f"Added via Aurora ({added_by})"
    log_to_sheets("add_guest", {"name": name, "origin": origin, "with_plus_one": with_plus_one})
    if name not in KNOWN_GUEST_NAMES:
        KNOWN_GUEST_NAMES.append(name)
    if with_plus_one:
        first_name = name.split()[0]
        placeholder = f"Guest ({first_name})"
        if placeholder not in KNOWN_GUEST_NAMES:
            KNOWN_GUEST_NAMES.append(placeholder)
    save_state()

def alert_larissa(message):
    try:
        send_zapi_message(LARISSA_NUMBER, f"🔔 *Aurora Alert*\n\n{message}")
    except Exception as e:
        import sys
        print(f"ALERT ERROR: {str(e)}", file=sys.stderr)

SYSTEM_PROMPT = """Você é Aurora, a assistente virtual e concierge oficial do casamento de Larissa e Robert em Roma, junho de 2027. 

NÃO COLETE NEM ALTERE RSVPS DIRETAMENTE NO CHAT:
- Você NÃO é responsável por coletar, registrar ou alterar confirmações de presença (RSVP) no chat.
- Sempre que um convidado perguntar sobre RSVP, quiser confirmar presença ou perguntar "Eu já confirmei/respondi ao RSVP?", forneça o link do formulário oficial de RSVP:
  https://larissabtlima.github.io/aurora-wedding-agent/
- Se o convidado tiver dúvidas específicas sobre o status do RSVP dele, explique gentilmente: "Não tenho acesso ao livro de presenças em tempo real, mas você pode confirmar ou atualizar suas opções pelo formulário no link acima ou falar diretamente com a Larissa e o Robert!"

NOSSAS FUNÇÕES PRINCIPAIS:
1. Dar dicas de voos, hotéis, transporte em Roma, o que vestir, roteiros e passeios.
2. Acolher os convidados com entusiasmo, tirar dúvidas sobre as datas do casamento e horários.
3. Se for convidado brasileiro e perguntar sobre passaporte, coletar as informações para passar para a Larissa agendar na Polícia Federal.
4. Reconhecer e dar saudações carinhosas para os membros do cortejo e família dos noivos.

NOSSA HISTÓRIA (compartilhe quando alguém perguntar como Larissa e Robert se conheceram):
Tudo começou em Dublin, em 2019, com um match em um aplicativo de namoro. No primeiro encontro, foram ao cinema ver "Cemitério Maldito". Desde aquele encontro, os dois cresceram juntos de Dublin até Nova York, onde moram hoje. Agora estão celebrando em Roma!

HONESTIDADE — REGRA CRÍTICA: se você não tiver certeza de algo, NUNCA invente uma resposta. Diga claramente que não tem certeza e oriente falar com Larissa e Robert.

PRIMEIRA MENSAGEM — OBRIGATÓRIO:
Quando alguém mandar mensagem pela primeira vez, comece assim:

Em português:
"Oi! 👋 Eu sou a *Aurora*, assistente virtual e concierge criada para o casamento de Larissa & Robert em Roma 🇮🇹💍

Estou disponível 24 horas para te ajudar a planejar sua viagem! Para confirmar ou atualizar sua presença no casamento, use nosso formulário oficial de RSVP:
🔗 https://larissabtlima.github.io/aurora-wedding-agent/

Posso te ajudar com:
✈️ Voos e como chegar em Roma
🏨 Onde se hospedar e hotéis recomendados
👗 O que vestir em cada dia do evento
🍝 Restaurantes e dicas imperdíveis em Roma
🛂 Ajuda com passaporte brasileiro
🚌 Transporte para os eventos
❓ Qualquer dúvida sobre a viagem e o casamento!"

Em inglês:
"Hi! 👋 I'm *Aurora*, the AI travel concierge for Larissa & Robert's wedding in Rome 🇮🇹💍

I'm available 24/7 to help you plan your trip! To confirm or update your RSVP for the wedding events, please visit our official RSVP web form:
🔗 https://larissabtlima.github.io/aurora-wedding-agent/

I can help you with:
✈️ Flights & travel tips to Rome
🏨 Hotel options and recommended areas
👗 Dress code guidelines for each day
🍝 Rome sightseeing & restaurant tips
🚌 Event schedules & transportation details
❓ Any questions about the celebration!"

DETALHES DO CASAMENTO:
DIA 1 — 24 JUNHO: VINÍCOLA 🍷 (Cantina Santa Benedetta)
DIA 2 — 25 JUNHO: CASAMENTO 💍 (Cerimônia na Santa Maria in Aracoeli às 15h, Festa na Villa Miani às 16h30)
DIA 3 — 26 JUNHO: PUB 🍺 (Scholars Lounge Irish Pub às 16h)
PRAZO DE RSVP: 29 de Janeiro de 2027.
"""

ADMIN_SYSTEM = """Você é a interface administrativa da Aurora para Larissa, Robert e Carlotta."""

def get_admin_stats():
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    return {
        "total_conversations": len(all_phones),
        "total_rsvps": len(rsvp_data),
        "attending": attending,
        "not_attending": not_attending,
        "awaiting_rsvp": len(KNOWN_GUEST_NAMES) - len(rsvp_data),
        "identified_guests": len(phone_registry),
        "rsvp_names": [r.get("name", "Unknown") for r in rsvp_data.values()],
        "identified_list": list(phone_registry.values()),
        "all_phones": list(all_phones),
        "bridal_party_phones": list(bridal_party_phones),
        "rsvp_details": rsvp_data,
        "guest_flags": guest_flags
    }

def get_conversation(phone_number):
    if phone_number not in conversations:
        conversations[phone_number] = []
    return conversations[phone_number]

def add_to_conversation(phone_number, role, content):
    if phone_number not in conversations:
        conversations[phone_number] = []
    conversations[phone_number].append({"role": role, "content": content})
    if len(conversations[phone_number]) > 40:
        conversations[phone_number] = conversations[phone_number][-40:]

def get_aurora_response(phone_number, user_message):
    add_to_conversation(phone_number, "user", user_message)
    messages = get_conversation(phone_number)
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    raw_text = response.content[0].text
    assistant_message = sanitize_for_whatsapp(raw_text)
    add_to_conversation(phone_number, "assistant", assistant_message)
    save_state()
    return assistant_message

def get_admin_response(phone_number, user_message):
    norm = normalize_phone(phone_number)
    name = ADMIN_IDENTITY.get(norm, "Carlotta")
    stats = get_admin_stats()
    context = f"[{name} está consultando. Dados: {json.dumps(stats)}]\n\n{user_message}"
    messages = [{"role": "user", "content": context}]
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=ADMIN_SYSTEM,
        messages=messages
    )
    return sanitize_for_whatsapp(response.content[0].text)

ADMIN_IDENTITY = {
    "16463390886": "Larissa Daly",
    "19292277546": "Robert Daly",
    "393490541017": "Carlotta"
}

def send_whatsapp_message(to_number, message, from_number):
    message = sanitize_for_whatsapp(message)
    twilio_client.messages.create(from_=from_number, to=to_number, body=message)

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    incoming_message = request.form.get('Body', '').strip()
    from_number = request.form.get('From', '')
    to_number = request.form.get('To', '')
    if not incoming_message or not from_number:
        return Response('', status=200)
    phone_key = from_number.replace('whatsapp:', '')
    all_phones.add(phone_key)

    reply = with_phone_lock(phone_key, lambda: get_aurora_response(phone_key, incoming_message))
    send_whatsapp_message(from_number, reply, to_number)
    return Response('', status=200)

@app.route('/zapi', methods=['POST'])
def zapi_webhook():
    try:
        data = request.get_json(force=True) or {}
        if data.get('fromMe', False):
            return Response('', status=200)

        text = data.get('text', {}).get('message', '') if isinstance(data.get('text'), dict) else data.get('text', '')
        phone = str(data.get('phone', '') or data.get('from', '')).replace('@s.whatsapp.net', '').replace('whatsapp:', '').strip()
        if not phone or not text:
            return Response('', status=200)

        all_phones.add(phone)
        reply = with_phone_lock(phone, lambda: get_aurora_response(phone, text))
        send_zapi_message(phone, reply)
    except Exception as e:
        print(f"Z-API ERROR: {str(e)}")
    return Response('', status=200)

def send_zapi_message(phone, message):
    instance_id = os.environ.get("ZAPI_INSTANCE_ID", "")
    token = os.environ.get("ZAPI_TOKEN", "")
    client_token = os.environ.get("ZAPI_CLIENT_TOKEN", "")
    if not instance_id or not token:
        return
    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
    payload = json.dumps({"phone": phone, "message": sanitize_for_whatsapp(message)}).encode()
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=10)

@app.route('/test-chat', methods=['POST', 'OPTIONS'])
def test_chat():
    if request.method == 'OPTIONS':
        resp = Response('', status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Test-Secret'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return resp

    secret = os.environ.get("TEST_CHAT_SECRET", "")
    provided = request.headers.get("X-Test-Secret", "")
    if not secret or provided != secret:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True) or {}
    phone = str(data.get("phone", "")).strip()
    message = str(data.get("message", "")).strip()
    if not phone or not message:
        return jsonify({"error": "phone and message are required"}), 400

    all_phones.add(phone)
    reply = with_phone_lock(phone, lambda: get_aurora_response(phone, message))
    resp = jsonify({"reply": reply})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp, 200

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'Aurora is live 💍', 'conversations': len(all_phones)}, 200

@app.route('/', methods=['GET'])
def home():
    return {'message': 'Aurora Wedding Concierge — Larissa & Robert, Rome 2027'}, 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
