import os
import json
import time
import random
import threading
import urllib.request
import urllib.parse
from flask import Flask, request, Response, jsonify
import anthropic

app = Flask(__name__)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ============================================================
# CHANGES IN THIS VERSION (v5) — summary for Larissa:
# 1. Removed Twilio entirely. Only Z-API (+353 833986529) is live now — the
#    Twilio backup route was a second, fully-functional copy of Aurora that
#    nobody was meant to use, so it's gone rather than left as a trap.
# 2. Removed the hardcoded ~180-name guest list. Aurora now matches guest
#    names against the LIVE Google Sheet (via the new directory endpoint in
#    Code.gs), so adding/removing guests in the sheet is immediately
#    reflected — no more editing this file to keep two lists in sync.
# 3. Admin stats ("how many people RSVPed?") now read live from the sheet
#    instead of an internal dict that was never actually being written to
#    (so it always silently reported 0).
# 4. Added real [ALL] and [BROADCAST] handling for admin numbers.
# 5. Removed a pile of dead state (pending_subject, active_companion, etc.)
#    left over from an earlier version where Aurora used to run RSVP inside
#    the chat. None of it was wired to anything anymore — it was just extra
#    surface area to get tangled up in.
# 6. Removed the "alert Larissa when an unknown guest messages" feature per
#    your call — instead, whenever Aurora doesn't know something or can't
#    identify the guest, she gives out Larissa's and Robert's numbers
#    directly so the guest can just message you.
# 7. Guest-facing Aurora can now answer "am I on the list", "is my
#    accommodation covered", "who's in my RSVP party" — scoped strictly to
#    the person she's talking to, using the new directory endpoint.
# ============================================================

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
DATA_FILE = os.path.join(DATA_DIR, "aurora_data.json")
_save_lock = threading.Lock()

conversations = {}
admin_conversations = {}
phone_registry = {}
all_phones = set()
processing = set()
processed_message_ids = set()


def with_phone_lock(phone, fn, *args, **kwargs):
    import time as _time
    if phone in processing:
        for _ in range(20):
            _time.sleep(0.5)
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


def _state_dict():
    return {
        "conversations": conversations,
        "admin_conversations": admin_conversations,
        "phone_registry": phone_registry,
        "all_phones": list(all_phones),
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
    global conversations, admin_conversations, phone_registry, all_phones
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            conversations = data.get("conversations", {})
            admin_conversations = data.get("admin_conversations", {})
            phone_registry = data.get("phone_registry", {})
            all_phones = set(data.get("all_phones", []))
            import sys
            print(f"LOADED STATE: {len(all_phones)} phones", file=sys.stderr)
        else:
            import sys
            print("LOADED STATE: no existing data file, starting fresh", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"LOAD STATE ERROR: {str(e)}", file=sys.stderr)


ADMIN_NUMBERS = {"+16463390886", "+19292277546", "+393490541017"}
ADMIN_NUMBERS_NORMALIZED = {n.lstrip("+") for n in ADMIN_NUMBERS}

LARISSA_NUMBER = "+16463390886"
ROB_NUMBER = "+19292277546"

ADMIN_IDENTITY = {
    "16463390886": "Larissa Daly",
    "19292277546": "Robert Daly",
    "393490541017": "Carlotta"
}

# ============================================================
# VIP ROLES — a short, fixed list of named roles that carry their own
# special greeting (mother/father of the bride, maid of honor, best man,
# etc.). This is intentionally kept separate from the live guest list:
# the guest list changes constantly as people are added/removed, but who
# the maid of honor or the parents are does not, so it's fine to hardcode
# this small set rather than depend on a spreadsheet column for it.
# General bridal party / groomsmen recognition still comes live from the
# sheet's "bridal party" column (see build_guest_context_note) — this map
# is only for the roles that need their OWN specific phrasing.
# ============================================================
VIP_ROLES = {
    "larissa lima": "a noiva (bride)",
    "robert daly": "o noivo (groom)",
    "laura teixeira": "mãe da noiva (mother of the bride)",
    "jadeilson lima": "pai da noiva (father of the bride)",
    "mary daly": "mãe do noivo (mother of the groom)",
    "christopher daly": "pai do noivo (father of the groom)",
    "anna laura teixeira": "madrinha de honra / dama de honra principal (maid of honor) e irmã da noiva",
    "brendan daly": "padrinho de honra (best man)",
}


def get_vip_role(name):
    if not name:
        return None
    return VIP_ROLES.get(name.split(" (")[0].strip().lower())


def normalize_phone(p):
    cleaned = (p or "").replace("whatsapp:", "").replace(" ", "").replace("-", "").strip()
    cleaned = cleaned.lstrip("+")
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
    return cleaned


def is_admin_phone(p):
    return normalize_phone(p) in ADMIN_NUMBERS_NORMALIZED


load_state()

# ============================================================
# GUEST DIRECTORY — the single source of truth for who's a guest.
# Loaded live from the "directory" endpoint in Code.gs (secret-gated).
# Replaces the old hardcoded KNOWN_GUEST_NAMES list entirely.
# Key = lowercased guest name (without any "(...)" suffix).
# ============================================================
GUEST_DIRECTORY = {}
PARTY_MAP = {}  # name (lowercase) -> list of linked/party member display names
_directory_last_load = 0


def load_guest_directory(force=False):
    global GUEST_DIRECTORY, PARTY_MAP, _directory_last_load
    if not force and GUEST_DIRECTORY and (time.time() - _directory_last_load) < 300:
        return
    webhook_url = os.environ.get("SHEETS_WEBHOOK_URL", "")
    secret = os.environ.get("GUEST_DIRECTORY_SECRET", "")
    if not webhook_url or not secret:
        return
    try:
        url = webhook_url + "?action=directory&secret=" + urllib.parse.quote(secret)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, dict) and data.get("error"):
            import sys
            print(f"GUEST DIRECTORY ERROR (server side): {data.get('error')}", file=sys.stderr)
            return
        new_directory = {}
        new_party_map = {}
        for row in data:
            full_name = (row.get("name") or "").strip()
            if not full_name:
                continue
            base_key = full_name.split(" (")[0].strip().lower()
            new_directory[base_key] = row
            # Build party links: "Conor Cahill (Linda Cahill)" -> attach Conor to Linda's party
            if "(" in full_name and full_name.endswith(")"):
                inside = full_name[full_name.index("(") + 1:-1].strip()
                outside = full_name[:full_name.index("(")].strip()
                if inside and inside.lower() != outside.lower():
                    new_party_map.setdefault(inside.lower(), []).append(outside)
        GUEST_DIRECTORY = new_directory
        PARTY_MAP = new_party_map
        _directory_last_load = time.time()
        import sys
        print(f"GUEST DIRECTORY: loaded {len(GUEST_DIRECTORY)} guests", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"GUEST DIRECTORY ERROR: {str(e)}", file=sys.stderr)


load_guest_directory(force=True)


def find_known_guest(name_query):
    """Fuzzy-match a free-text name against the LIVE guest directory."""
    import re
    load_guest_directory()
    FILLER_WORDS = {"im", "i'm", "eu", "sou", "meu", "nome", "name", "is", "e", "é", "the", "o", "a"}
    q = name_query.lower().strip()
    if not q:
        return None
    q_tokens = [t for t in re.findall(r"[a-zà-ú']+", q) if t not in FILLER_WORDS]
    if not q_tokens:
        return None
    q_clean = " ".join(q_tokens)

    all_names = [rec.get("name", key) for key, rec in GUEST_DIRECTORY.items()]
    for known in all_names:
        if known.lower() == q_clean:
            return known

    best = None
    best_score = 0
    for known in all_names:
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


def sanitize_for_whatsapp(text):
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|?\s*[-:]+\s*\|.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|(.+)\|$', lambda m: ' • '.join(c.strip() for c in m.group(1).split('|') if c.strip()), text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def build_guest_context_note(phone_number, user_message):
    load_guest_directory()
    name = phone_registry.get(phone_number)
    if not name:
        matched = find_known_guest(user_message)
        if matched:
            phone_registry[phone_number] = matched
            name = matched
            save_state()
    if not name:
        return ""
    record = GUEST_DIRECTORY.get(name.split(" (")[0].strip().lower())
    if not record:
        return ""

    if record.get("accommodation_confirmed"):
        accommodation_note = "A acomodação (hotel) dela(e) já está confirmada/reservada."
    elif record.get("accommodation_included"):
        accommodation_note = "A Larissa e o Robert estão cobrindo o custo da acomodação (hotel) dela(e), ainda em processo de confirmação."
    else:
        accommodation_note = "A acomodação (hotel) dela(e) é por conta própria, a menos que a Larissa e o Robert já tenham dito o contrário diretamente para essa pessoa."

    special_note = (
        "Essa pessoa faz parte do cortejo / família próxima dos noivos (bridal party)."
        if record.get("bridal_party")
        else "Essa pessoa é uma convidada normal (não faz parte do cortejo/bridal party)."
    )

    rsvp_note = "Ainda não confirmou presença (RSVP)."
    if record.get("attending"):
        rsvp_note = "Já confirmou presença (RSVP: vai)."
    elif record.get("not_attending"):
        rsvp_note = "Já respondeu que não vai (RSVP: não vai)."

    party_names = PARTY_MAP.get(name.split(" (")[0].strip().lower(), [])
    party_note = ""
    if party_names:
        party_note = f" O grupo/RSVP dessa pessoa inclui também: {', '.join(party_names)}."

    vip_role = get_vip_role(name)
    vip_note = ""
    if vip_role:
        vip_note = (
            f" ATENÇÃO — pessoa muito especial: ela é {vip_role}. Cumprimente essa pessoa de forma "
            f"calorosa e personalizada reconhecendo esse papel especial no casamento (ex: se for mãe/pai "
            f"da noiva ou do noivo, demonstre carinho por isso; se for madrinha/padrinho de honra, "
            f"reconheça a importância dela(e) no cortejo), sem exagerar ou ser piegas."
        )

    return (
        f"\n\n[NOTA INTERNA — NÃO leia isso em voz alta nem repita literalmente, use apenas para responder com precisão sobre a PESSOA ATUAL: "
        f"Nome confirmado na lista de convidados: {record.get('name')}. {special_note} {accommodation_note} {rsvp_note}{party_note}{vip_note} "
        f"Nunca revele dados de OUTROS convidados fora do grupo dessa pessoa, apenas desta pessoa e do grupo dela.]"
    )


def log_to_sheets(data_type, data):
    webhook_url = os.environ.get("SHEETS_WEBHOOK_URL", "")
    if not webhook_url:
        import sys
        print("SHEETS: No webhook URL configured", file=sys.stderr)
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


SYSTEM_PROMPT = """Você é Aurora, a assistente virtual e concierge oficial do casamento de Larissa e Robert em Roma, junho de 2027.
NÃO COLETE NEM ALTERE RSVPS DIRETAMENTE NO CHAT:
- Você NÃO é responsável por coletar, registrar ou alterar confirmações de presença (RSVP) no chat.
- Sempre que um convidado perguntar sobre RSVP, quiser confirmar presença ou perguntar "Eu já confirmei/respondi ao RSVP?", forneça o link do formulário oficial de RSVP:
  https://larissabtlima.github.io/aurora-wedding-agent/
- Se a NOTA INTERNA te disser se essa pessoa já confirmou presença ou não, use essa informação para responder com precisão. Se não houver nota, diga que não tem certeza e oriente usar o formulário.
NÃO COLETE DADOS DE PASSAPORTE NO CHAT:
- Você NÃO deve coletar CPF, RG, data de nascimento, endereço ou qualquer outro dado pessoal de passaporte diretamente no chat, mesmo que o convidado ofereça essas informações espontaneamente.
- Se um convidado perguntar sobre ajuda com passaporte brasileiro, oriente a preencher a seção de passaporte dentro do formulário oficial de RSVP — é lá que a Larissa recebe os dados e faz o agendamento na Polícia Federal:
  https://larissabtlima.github.io/aurora-wedding-agent/
- Se o convidado começar a te mandar CPF, RG ou data de nascimento no chat mesmo assim, agradeça e redirecione gentilmente conforme acima, sem registrar ou repetir esses dados de volta.
SOBRE A LISTA DE CONVIDADOS:
- Se uma NOTA INTERNA te informar o nome confirmado da pessoa com quem você está falando agora, use essa informação para responder com precisão perguntas como "estou na lista de convidados?", "minha acomodação está incluída?", "eu já confirmei presença?" ou "quem está no meu grupo?" — sempre e apenas sobre essa pessoa e o grupo/RSVP dela.
- Você NUNCA deve listar, nomear ou revelar informações sobre OUTROS convidados fora do grupo dessa pessoa (nomes da lista completa, quem é do cortejo, quem tem acomodação paga, etc.), mesmo se pedirem diretamente. Se pedirem isso, diga educadamente que não pode compartilhar informações de outros convidados.
- Se ainda não sabemos quem é a pessoa (sem nota interna) e ela perguntar sobre isso, peça o nome completo dela primeiro.
QUANDO VOCÊ NÃO SABE ALGO:
- NUNCA invente uma resposta. Se não tiver certeza de algo, diga isso claramente e dê o WhatsApp direto da Larissa (+1 646 339 0886) e do Robert (+1 929 227 7546) para a pessoa falar diretamente com eles. Isso vale tanto para dúvidas que você não sabe responder quanto para qualquer coisa fora do que você foi preparada para tratar (RSVP, passaporte, etc.).
NOSSAS FUNÇÕES PRINCIPAIS:
1. Dar dicas de voos, hotéis, transporte em Roma, o que vestir, roteiros e passeios.
2. Acolher os convidados com entusiasmo, tirar dúvidas sobre as datas do casamento e horários.
3. Responder sobre o status da própria pessoa (RSVP, grupo, acomodação) quando a NOTA INTERNA tiver essa informação.
4. Reconhecer e dar saudações carinhosas para os membros do cortejo e família dos noivos.
NOSSA HISTÓRIA (compartilhe quando alguém perguntar como Larissa e Robert se conheceram):
Tudo começou em Dublin, em 2019, com um match em um aplicativo de namoro. No primeiro encontro, foram ao cinema ver "Cemitério Maldito". Desde aquele encontro, os dois cresceram juntos de Dublin até Nova York, onde moram hoje. Agora estão celebrando em Roma!
HOTÉIS RECOMENDADOS (mencione SEMPRE a distância a pé até a igreja E até o pub, e SEMPRE inclua o link de reserva):
"Preparamos uma lista de hotéis recomendados bem no centro de Roma! 🇮🇹 Ficar nessa região deixa vocês perto de tudo, e teremos transporte de ida e volta fornecido dessa área pra todos os eventos principais 🚌 (incluindo o Dia 1 pro jantar de boas-vindas na vinícola, e no dia do casamento pra Villa Miani)."
🏨 Hotel Castellino Roma (4★)
📍 ~3-4 min a pé da igreja | ~4 min a pé do Scholars Lounge Irish Pub
💶 ~€312-347/noite (2 pessoas)
🔗 https://www.booking.com/Share-rPP6D1l
🏨 Hotel Hiberia (3★)
📍 ~6-8 min a pé da igreja | ~6 min a pé do Scholars Lounge Irish Pub
💶 ~US$250 / ~€218/noite (2 pessoas)
🔗 https://www.booking.com/Share-C3S3rD4
🏨 Hotel Regno (3★) — reservas de 2027 abrindo em breve
📍 ~8 min a pé da igreja | ~6 min a pé do Scholars Lounge Irish Pub
💶 Estimativa ~€180-280/noite (2 pessoas)
🔗 https://www.booking.com/Share-eQuUYXw
✨ Opções de Luxo 5 Estrelas (mesma região central):
🌟 NH Collection Roma Fori Imperiali — ~€400-550/noite — https://www.booking.com/Share-ex24085
🌟 Radisson Collection Hotel, Roma Antica — ~€450-550/noite — https://www.booking.com/Share-joubljX
🌟 Singer Palace Hotel Roma — ~€500-750/noite — https://www.booking.com/Share-Q1rOjM
🌟 Umiltà 36 — ~€600-750/noite — https://www.booking.com/Share-XglqAFK
Sempre termine com: "Fiquem à vontade pra reservar o hotel, pousada ou Airbnb que combinar mais com o estilo e orçamento de vocês — não tem nenhuma obrigação de ficar numa dessas propriedades específicas! É só se encontrar no ponto de encontro central do transporte nos dias dos eventos."
Sempre mostre TODAS as 7 opções juntas (não pergunte preferência antes) — nunca esconda a metade.
VOOS — REGRA CRÍTICA: SEMPRE mencione "preços com base em 1º de agosto de 2026" toda vez que citar um preço de voo — sem exceção, mesmo que já tenha dito isso antes na mesma conversa. SEMPRE mencione as companhias aéreas que voam nessa rota. SEMPRE mostre as DUAS opções de duração (viagem só do casamento E viagem de uma semana) juntas — nunca só uma. SEMPRE termine com: "Claro, vocês podem montar a viagem do jeito que quiserem — muita gente vai aproveitar pra conhecer outras partes da Itália ou até outros países também! Isso aqui é só uma referência de preço, não uma obrigação."
Dublin → Roma Fiumicino (Aer Lingus, Ryanair):
• Qui-Dom (4 dias): ida 24 Jun 07:25 FR5568 ($148.69) | volta 27 Jun 12:05 FR5569 ($178.66) — Ryanair
• Qua-Ter (7 dias): ida 23 Jun 17:00 FR9613 ($82.98) | volta 29 Jun 17:40 FR9612 ($148.69) — Ryanair
Shannon → Roma Ciampino (Ryanair):
Voos só ficam disponíveis pra compra em novembro — ainda não há preços. Voam toda terça e sábado. Datas de referência: terça 22 Jun a terça 29 Jun (7 dias).
Londres Stansted → Roma Ciampino (British Airways, Ryanair, Wizz Air, EasyJet):
• Qui-Dom (4 dias): ida 24 Jun 15:00 FR2672 ($61.94) | volta 27 Jun 15:35 FR2509 ($88.75) — Ryanair
• Qua-Ter (7 dias): ida 23 Jun 17:40 FR2672 ($61.94) | volta 29 Jun 17:25 FR3003 ($96.65) — Ryanair
Nova York → Roma (Delta, American Airlines, ITA Airways, United Airlines, Norse Atlantic Airways):
• Qua-Dom (5 dias): ida 23 Jun 16:05 AZ609 | volta 27 Jun 10:30 AZ608 ($1,061.83 sem bagagem / $1,361.83 com bagagem despachada) — ITA Airways
• Qua-Qua (7 dias): ida 23 Jun 16:05 AZ609 | volta 30 Jun 15:10 AZ610 ($1,011.83 sem bagagem / $1,311.83 com bagagem despachada) — ITA Airways
Brasil → Roma: essa informação ainda não está disponível — os voos ainda não abriram pra venda. Diga honestamente: "Ainda não tenho os preços e horários de voos do Brasil — devo ter essa informação atualizada em breve! Assim que tiver, aviso vocês." NUNCA invente ou estime preços pra essa rota.
AEROPORTO — RECOMENDAÇÃO PROATIVA: quando alguém perguntar sobre voos, recomende o aeroporto certo com base na origem, sem esperar ser perguntado:
• Vindo dos EUA ou do Brasil → recomende Fiumicino (FCO), o hub internacional principal de Roma, com ótimas conexões (inclui o trem Leonardo Express direto até Roma Termini, 30 min, ou táxi de 30-40 min até o centro).
• Vindo da Irlanda ou de voos econômicos europeus (Ryanair etc.) → provavelmente vai pousar em Ciampino (CIA), menor e mais perto do centro, mas depende mais de táxi/ônibus (25-30 min até a região central).
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
DETALHES DO CASAMENTO — FONTE OFICIAL: o site do casamento (romewed.my.canva.site) é a fonte de verdade. Se algo aqui conflitar com o site, o site manda.
DIA 1 — 24 JUNHO: VINÍCOLA 🍷 (Cantina Santa Benedetta) — saindo de Roma às 17h (5 PM)
DIA 2 — 25 JUNHO: CASAMENTO 💍 (Cerimônia na Santa Maria in Aracoeli às 15h30, Recepção na Villa Miani às 17h)
DIA 3 — 26 JUNHO: PUB 🍺 (Scholars Lounge Irish Pub às 16h)
PRAZO DE RSVP: 29 de Janeiro de 2027.

CRONOGRAMA DO DIA DO CASAMENTO (Dia 2):
15h30 — Cerimônia
17h00 — Coquetel
18h00 — Jantar
19h30 — Corte do Bolo

ENDEREÇOS E DETALHES DOS LOCAIS:
🍷 Dia 1 — Cantina Santa Benedetta, Via Frascati Colonna 35, Monte Porzio Catone (~30-40 min de Roma). Vinícola boutique entre vinhedos e olivais, com vistas panorâmicas, parte ao ar livre. Transporte de ida e volta fornecido pelos noivos saindo do centro de Roma às 17h.
💍 Dia 2 — Cerimônia: Basílica Santa Maria in Aracoeli, às 15h30. ⚠️ São 124 degraus pra subir até a igreja — tem elevador disponível pra quem realmente precisa (mobilidade reduzida, gravidez, crianças de colo), só avisar com antecedência. Recepção: Villa Miani, Via Trionfale 151, às 17h. Transporte fornecido da igreja até a Villa Miani, e depois de volta ao centro da cidade.
🍺 Dia 3 — Scholars Lounge Irish Pub, Via del Plebiscito 101B, às 16h. Dia totalmente casual, comida e bebida inclusas. Sem transporte fornecido nesse dia — o pub é bem central.

O QUE VESTIR:
Dia 1 (vinícola): smart casual, sapatos confortáveis — o terreno é irregular, evite salto fino.
Dia 2 (casamento) — "Summer Black Tie": smoking é super bem-vindo, mas ternos formais em tecidos leves e respiráveis também são perfeitamente aceitáveis (Roma estará bem quente). Para elas: vestidos longos formais, midi elegantes ou conjuntos sofisticados. Pedido especial: evitar tons de branco, off-white ou marfim (reservado pra noiva). Na igreja, ombros e joelhos cobertos (leve um xale se precisar).
Dia 3 (pub): totalmente casual, sem regras.

CLIMA EM ROMA NO PERÍODO DO CASAMENTO (sempre mencione °C e °F juntos):
Dia: 28°C a 35°C (82°F a 95°F). Noite: 18°C a 24°C (64°F a 75°F). Aviso: Roma costuma parecer ainda mais quente do que a previsão por causa das ruas de pedra e pouca sombra, principalmente à tarde. Recomendação: roupas leves e respiráveis, sapatos confortáveis, óculos de sol, protetor solar, chapéu e uma garrafa de água reutilizável.

COMO SE LOCOMOVER EM ROMA:
Roma é uma cidade muito boa pra andar a pé. Para distâncias maiores: metrô, ônibus, bondinhos ou táxi. Para aplicativo de corrida, recomende o FRENOW ao invés do Uber — é mais econômico e tem melhor disponibilidade em Roma.

ORÇAMENTO EM ROMA (referência geral, sempre avise que pode variar):
Refeição casual: €8-15 por pessoa. Restaurante mais chique/formal: €20-50+ por pessoa. Café/gelato: €2-5. Transporte público: bilhete único €1,50. Táxi curto dentro do centro: €10-15.
Gorjeta não é obrigatória na Itália — arredondar a conta já é suficiente.

PRESENTES / LUA DE MEL (mencione quando perguntarem sobre presentes ou lista de casamento):
A presença de cada convidado já é o maior presente. Para quem quiser contribuir com a lua de mel, os noivos disponibilizaram:
💳 Revolut (Ireland): @robertmo7
💳 Zelle (USA): +1 929 227 7546
💳 Pix (Brasil): 13005770613
Presentes físicos podem ser entregues à Anna Laura Teixeira caso os noivos não estejam disponíveis no momento.

SEGURANÇA E EMERGÊNCIA:
Emergência geral na Itália: 112. Cuidado com batedores de carteira em pontos turísticos movimentados (Coliseu, Termini, ônibus lotados) — sempre use táxi oficial (branco).

RESTAURANTES E DICAS DE ROMA (sugestões gerais, pode recomendar quando perguntarem):
Trastevere é ótimo pra jantar com boa comida tradicional e ambiente. Campo de' Fiori tem um mercado de manhã e vira ponto de bares à noite. Sempre vale reservar com antecedência em restaurantes populares. Para gelato de verdade, procure lugares com sorvete "artigianale", não os muito coloridos/vibrantes demais (geralmente são cheios de corante).
"""

ADMIN_SYSTEM = """Você é a interface administrativa da Aurora para Larissa, Robert e Carlotta. Responda com base nos dados fornecidos, sem inventar números."""


def get_admin_stats():
    load_guest_directory()
    records = list(GUEST_DIRECTORY.values())
    attending = sum(1 for r in records if r.get("attending"))
    not_attending = sum(1 for r in records if r.get("not_attending"))
    total = len(records)
    return {
        "total_guests": total,
        "attending": attending,
        "not_attending": not_attending,
        "awaiting_rsvp": total - attending - not_attending,
        "bridal_party_count": sum(1 for r in records if r.get("bridal_party")),
        "accommodation_included_count": sum(1 for r in records if r.get("accommodation_included")),
        "accommodation_confirmed_count": sum(1 for r in records if r.get("accommodation_confirmed")),
        "total_conversations_on_whatsapp": len(all_phones),
        "identified_guests_on_whatsapp": len(phone_registry),
        "attending_names": [r.get("name") for r in records if r.get("attending")],
        "not_attending_names": [r.get("name") for r in records if r.get("not_attending")],
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
    guest_note = build_guest_context_note(phone_number, user_message)
    system_text = SYSTEM_PROMPT + guest_note
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_text,
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
    if phone_number not in admin_conversations:
        admin_conversations[phone_number] = []
    history = admin_conversations[phone_number]
    context = f"[{name} está consultando. Dados atuais (ao vivo da planilha): {json.dumps(stats, ensure_ascii=False)}]\n\n{user_message}"
    messages = history + [{"role": "user", "content": context}]
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        system=ADMIN_SYSTEM,
        messages=messages
    )
    reply = sanitize_for_whatsapp(response.content[0].text)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    if len(history) > 20:
        admin_conversations[phone_number] = history[-20:]
    save_state()
    return reply


# ============================================================
# ADMIN BROADCASTS
# An admin (Larissa, Rob, Carlotta) can send:
#   [ALL] message text     -> sent to every guest who has ever messaged Aurora
#   [BRIDAL] message text  -> sent only to guests flagged as bridal_party in the sheet
#
# Sends happen in a BACKGROUND thread with a randomized delay between each
# message (not all at once). Firing 50-200 WhatsApp messages back-to-back
# in under a second is exactly the pattern WhatsApp's spam/bot detection
# flags — this spreads them out like a human slowly forwarding a message,
# which is much less likely to get the number blocked.
# ============================================================
BROADCAST_MIN_DELAY_SECONDS = 4
BROADCAST_MAX_DELAY_SECONDS = 9


def _run_broadcast(admin_phone, target, body, recipients):
    import sys
    sent = 0
    for phone in recipients:
        if phone == admin_phone:
            continue
        try:
            send_zapi_message(phone, body)
            sent += 1
        except Exception as e:
            print(f"BROADCAST SEND ERROR to {phone}: {str(e)}", file=sys.stderr)
        time.sleep(random.uniform(BROADCAST_MIN_DELAY_SECONDS, BROADCAST_MAX_DELAY_SECONDS))
    try:
        send_zapi_message(admin_phone, f"📣 Broadcast [{target.upper()}] concluído — enviado para {sent} contato(s).")
    except Exception as e:
        print(f"BROADCAST COMPLETION NOTICE ERROR: {str(e)}", file=sys.stderr)


def try_handle_broadcast(admin_phone, message):
    stripped = message.strip()
    target = None
    body = None
    if stripped.upper().startswith("[ALL]"):
        target = "all"
        body = stripped[len("[ALL]"):].strip()
    elif stripped.upper().startswith("[BRIDAL]"):
        target = "bridal"
        body = stripped[len("[BRIDAL]"):].strip()
    if not target or not body:
        return None

    load_guest_directory()
    recipients = []
    if target == "all":
        recipients = list(all_phones)
    else:
        for phone in all_phones:
            gname = phone_registry.get(phone)
            if not gname:
                continue
            record = GUEST_DIRECTORY.get(gname.split(" (")[0].strip().lower())
            if record and record.get("bridal_party"):
                recipients.append(phone)

    recipients = [p for p in recipients if p != admin_phone]
    thread = threading.Thread(
        target=_run_broadcast,
        args=(admin_phone, target, body, recipients),
        daemon=True
    )
    thread.start()

    est_minutes = round((len(recipients) * (BROADCAST_MIN_DELAY_SECONDS + BROADCAST_MAX_DELAY_SECONDS) / 2) / 60, 1)
    return (
        f"📣 Broadcast [{target.upper()}] iniciado para {len(recipients)} contato(s). "
        f"Enviando aos poucos (com pausas entre mensagens) para não ser bloqueado pelo WhatsApp — "
        f"deve levar cerca de {est_minutes} min. Aviso quando terminar."
    )


@app.route('/zapi', methods=['POST'])
def zapi_webhook():
    try:
        data = request.get_json(force=True) or {}
        if data.get('fromMe', False):
            return Response('', status=200)
        message_id = data.get('messageId') or data.get('messageID') or data.get('id')
        if message_id:
            if message_id in processed_message_ids:
                import sys
                print(f"Z-API: duplicate message {message_id} — skipping", file=sys.stderr)
                return Response('', status=200)
            processed_message_ids.add(message_id)
            if len(processed_message_ids) > 500:
                for old_id in list(processed_message_ids)[:250]:
                    processed_message_ids.discard(old_id)
        text = data.get('text', {}).get('message', '') if isinstance(data.get('text'), dict) else data.get('text', '')
        phone = str(data.get('phone', '') or data.get('from', '')).replace('@s.whatsapp.net', '').replace('whatsapp:', '').strip()
        if not phone or not text:
            return Response('', status=200)
        all_phones.add(phone)

        if is_admin_phone(phone):
            broadcast_result = try_handle_broadcast(phone, text)
            if broadcast_result is not None:
                send_zapi_message(phone, broadcast_result)
                save_state()
                return Response('', status=200)
            reply = with_phone_lock(phone, lambda: get_admin_response(phone, text))
        else:
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
    if is_admin_phone(phone):
        broadcast_result = try_handle_broadcast(phone, message)
        if broadcast_result is not None:
            reply = broadcast_result
        else:
            reply = with_phone_lock(phone, lambda: get_admin_response(phone, message))
    else:
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
