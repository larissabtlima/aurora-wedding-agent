import os
import json
import threading
import datetime
import urllib.request
import urllib.parse

def search_web_flights(query):
    """Search Google for flight options and return a summary."""
    try:
        encoded = urllib.parse.quote(query)
        search_url = f"https://www.google.com/search?q={encoded}"
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AuroraBot/1.0)"}
        )
        result = urllib.request.urlopen(req, timeout=8)
        import sys
        print(f"FLIGHT SEARCH: {query} → status {result.status}", file=sys.stderr)
        return True
    except Exception as e:
        import sys
        print(f"FLIGHT SEARCH ERROR: {str(e)}", file=sys.stderr)
        return False
from flask import Flask, request, Response
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

conversations = {}       # phone -> [ {role, content}, ... ]  (guest/RSVP conversations)
admin_conversations = {} # phone -> [ {role, content}, ... ]  (admin analytics conversations)
phone_registry = {}      # phone -> name of the PHONE OWNER (not necessarily who's being RSVP'd)
rsvp_data = {}           # guest_name (lowercase) -> rsvp details
all_phones = set()
processing = set()
processed_message_ids = set()
last_processed_time = {}
guest_flags = {}         # guest_name (lowercase) -> flags (rsvp_done, passport_done, etc)
active_subject = {}      # phone -> name currently being RSVP'd on this phone
pending_subject = {}     # phone -> name Aurora just asked to confirm, awaiting yes/no

def _state_dict():
    return {
        "conversations": conversations,
        "admin_conversations": admin_conversations,
        "phone_registry": phone_registry,
        "rsvp_data": rsvp_data,
        "all_phones": list(all_phones),
        "guest_flags": guest_flags,
        "active_subject": active_subject,
        "pending_subject": pending_subject,
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
    global conversations, admin_conversations, phone_registry, rsvp_data, all_phones, guest_flags, active_subject, pending_subject
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
            active_subject = data.get("active_subject", {})
            pending_subject = data.get("pending_subject", {})
            import sys
            print(f"LOADED STATE: {len(all_phones)} phones, {len(rsvp_data)} rsvps", file=sys.stderr)
        else:
            import sys
            print("LOADED STATE: no existing data file, starting fresh", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"LOAD STATE ERROR: {str(e)}", file=sys.stderr)

load_state()

ADMIN_NUMBERS = {"+353833986529", "+19292277546", "+393490541017"}
ADMIN_NUMBERS_NORMALIZED = {n.lstrip("+") for n in ADMIN_NUMBERS}

def normalize_phone(p):
    return (p or "").replace("whatsapp:", "").replace(" ", "").replace("-", "").lstrip("+").strip()

def is_admin_phone(p):
    return normalize_phone(p) in ADMIN_NUMBERS_NORMALIZED

LARISSA_NUMBER = "+353833986529"
ROB_NUMBER = "+19292277546"
CARLOTTA_NUMBER = "+393490541017"
SPREADSHEET_ID = "1__SAxw3AMWy8Rb3LlRNzfw1MMIJ__4jc7PYpJ5RVDwk"

bridal_party_phones = set()
BRIDAL_PARTY_NAMES = {
    "anna laura teixeira", "thaíse silva", "thaise silva",
    "aline olden", "thaís rebuá", "thais rebua",
    "eduarda santana", "linda cahill", "will daly",
    "michael daly", "brendan daly", "chris daly",
    "cian mc donnell", "corey brennan"
}

# Full guest list, one name per line, used for admin "is X on the list?" /
# "add X to the list" checks. Kept separately from SYSTEM_PROMPT's prose
# version so the admin flow can do simple substring matching against it.
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

def find_known_guest(name_query):
    """Returns the matching guest name from the known list, or None.
    Handles informal phrasing ("Im Larissa", "eu sou o Robert") by
    stripping filler words and matching on the remaining name tokens,
    not just raw substrings."""
    import re
    FILLER_WORDS = {"im", "i'm", "eu", "sou", "meu", "nome", "name", "is", "e", "é", "the", "o", "a"}
    q = name_query.lower().strip()
    if not q:
        return None
    q_tokens = [t for t in re.findall(r"[a-zà-ú']+", q) if t not in FILLER_WORDS]
    if not q_tokens:
        return None
    q_clean = " ".join(q_tokens)

    best = None
    best_score = 0
    for known in KNOWN_GUEST_NAMES:
        k = known.lower()
        k_tokens = set(re.findall(r"[a-zà-ú']+", k))
        if k == q_clean or q_clean in k or k in q_clean:
            return known
        overlap = len(set(q_tokens) & k_tokens)
        if overlap > best_score:
            best_score = overlap
            best = known
    return best if best_score > 0 else None

BRAZIL_NAME_MARKERS = None  # placeholder, Brazilian guest list is matched via the guest list itself

def sanitize_for_whatsapp(text):
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\|?\s*[-:]+\s*\|.*$', '', text, flags=re.MULTILINE)  # markdown table separator rows
    text = re.sub(r'^\|(.+)\|$', lambda m: ' • '.join(c.strip() for c in m.group(1).split('|') if c.strip()), text, flags=re.MULTILINE)  # table rows -> plain list
    text = re.sub(r'\n{3,}', '\n\n', text)  # collapse 2+ blank lines down to just 1
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

def add_guest_to_sheet(name, added_by="admin", notes=""):
    origin = notes or f"Added via Aurora ({added_by})"
    log_to_sheets("add_guest", {"name": name, "origin": origin})
    if name not in KNOWN_GUEST_NAMES:
        KNOWN_GUEST_NAMES.append(name)
    save_state()

def alert_larissa(message):
    try:
        send_zapi_message(LARISSA_NUMBER, f"🔔 *Aurora Alert*\n\n{message}")
    except Exception as e:
        import sys
        print(f"ALERT ERROR: {str(e)}", file=sys.stderr)

def send_weekly_report():
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    pending = 249 - len(rsvp_data)
    report = (
        f"📊 *Aurora Weekly Wedding Report*\n"
        f"_Friday update — Larissa & Robert Wedding_\n\n"
        f"✅ Confirmed attending: *{attending}*\n"
        f"❌ Not attending: *{not_attending}*\n"
        f"⏳ Awaiting RSVP: *{pending}* of 249\n\n"
        f"💬 Total conversations: {len(all_phones)}\n\n"
        f"_Message Aurora to ask for names, who hasn't RSVPed, or any details!_"
    )
    for number in [LARISSA_NUMBER, ROB_NUMBER]:
        send_zapi_message(number, report)

def schedule_weekly_report():
    def run():
        while True:
            now = datetime.datetime.utcnow()
            if now.weekday() == 4 and now.hour == 13 and now.minute == 0:
                send_weekly_report()
                import time
                time.sleep(61)
            import time
            time.sleep(30)
    t = threading.Thread(target=run, daemon=True)
    t.start()

schedule_weekly_report()

SYSTEM_PROMPT = """Você é Aurora, a assistente virtual oficial do casamento de Larissa e Robert em Roma, junho de 2027. Quando fala em inglês, responde em inglês. Quando fala em português, responde em português brasileiro — sempre natural, correto e fluente, como uma brasileira falaria. Nunca use português europeu ou traduções literais estranhas.

PRIMEIRA MENSAGEM — OBRIGATÓRIO:
Quando alguém mandar mensagem pela primeira vez, SEMPRE comece assim:

Em português:
"Oi! 👋 Eu sou a *Aurora*, assistente virtual criada especialmente para o casamento de Larissa & Robert em Roma 🇮🇹💍

Estou disponível 24 horas e só consigo ler *mensagens de texto* — não consigo ouvir áudios, então escreva sua mensagem, tá?

Posso te ajudar com:
✅ Confirmação de presença
✈️ Voos e como chegar em Roma
🏨 Onde se hospedar
👗 O que vestir em cada dia
🍝 Restaurantes e dicas de Roma
🛂 Passaporte (se ainda não tiver, posso te ajudar a tirar)
🚌 Transporte entre os eventos
💰 Quanto dinheiro levar
❓ Qualquer dúvida sobre o casamento

Qual é o seu nome? Vou te procurar na lista de convidados! 😊"

Em inglês:
"Hi! 👋 I'm *Aurora*, the AI assistant created especially for Larissa & Robert's Rome wedding 🇮🇹💍

I'm available 24/7 and I only understand *text messages* — I can't listen to voice notes, so please type your message!

I can help you with:
✅ RSVP
✈️ Flights & travel to Rome
🏨 Where to stay
👗 What to wear each day
🍝 Rome restaurants & tips
🛂 Passport (important! tell me more)
🚌 Transport between venues
💰 Budget guide for Rome
❓ Any wedding questions

What's your name? I'd love to look you up on the guest list! 😊"

VOCÊ É UMA IA — deixe isso claro sempre.
SÓ TEXTO — não ouço áudios.
IDIOMA: PT brasileiro natural. EN quando em inglês. Nunca misture.
FORMATAÇÃO: Asterisco simples para negrito. UMA mensagem só, nunca divida. NUNCA deixe mais de UMA linha em branco entre seções — espaçamento apertado, não solto. Mensagens devem parecer uma conversa de WhatsApp normal, não um documento formal com respiros grandes entre parágrafos.
TEMPERATURA: Sempre °C E °F.
LINKS: Google Maps para tudo.
NUNCA ENCERRE — sempre sugira próximo tópico.

PERSONALIDADE — DIVIRTA-SE:
Aurora não precisa ser só eficiente — ela pode ter personalidade! Sinta-se à vontade pra usar um humor leve, brincadeiras, e um toque de humor irlandês (self-deprecating, seco, bem-humorado — tipo brincar consigo mesma ou com situações do dia a dia) de vez em quando, especialmente com convidados que já demonstraram um tom mais descontraído. MAS: a piada nunca pode comprometer a clareza — a pessoa sempre tem que entender exatamente o que Aurora está dizendo. Primeiro clareza, depois graça.

RSVP PARA OUTRA PESSOA — REGRA CRÍTICA:
Quem está te mandando mensagem (o número de telefone) NÃO é necessariamente quem está sendo confirmado. Uma pessoa pode confirmar presença dela mesma E de outras pessoas na mesma conversa (ex: Larissa confirmando a própria presença e também a da Anna Laura).
SEMPRE deixe claro, a cada novo RSVP dentro da mesma conversa, para QUEM é aquele RSVP específico — nunca assuma que é a mesma pessoa do RSVP anterior nessa conversa.
Quando o nome mudar de convidado dentro da mesma conversa, trate como um RSVP totalmente novo e separado — não misture dados de uma pessoa com a outra.


LISTA DE CONVIDADOS (249 pessoas):

LISTA DO ROB (EN): Robert Daly, Larissa Daly, Michael Daly, Mary Daly, Christopher Daly (acompanhante de Mary), Thomas O Brien, Kornel Cwiklinski, Alan Cwiklinski, Patryk Wesolowski, Natalie (acompanhante de Patryk), Linda Cahill, Conor Cahill (família de Linda), Cathy Cahill (família de Linda), Ayla Cahill (família de Linda), Avean Cahill (família de Linda), Caera Cahill (família de Linda), Will Daly, Ezgi Atakul (acompanhante de Will), Brendan Daly, Deirdre Daly (acompanhante de Brendan), Chris Daly, Guest (acompanhante de Chris Daly), Cian Mc Donnell, Guest (acompanhante de Cian), Corey Brennan, Guest (acompanhante de Corey), George O Mahony, Charlotte Barton (acompanhante de George), James Roche, Guest (acompanhante de James Roche), Luke Mccarthty, Guest (acompanhante de Luke), Sean Murphy, Joanne Murphy (acompanhante de Sean), Patrick Fitzgibbon, Stephanie Fitzgibbon (acompanhante de Patrick), Shane Burke, Guest (acompanhante de Shane Burke), Shane Galvin, Rebecca Perrott (acompanhante de Shane Galvin), Mikey O Donovan, Guest (acompanhante de Mikey), Peter Olden, Guest (acompanhante de Peter), Pauline Olden, Mike O'Riordan, Guest (acompanhante de Mike O'Riordan), Donica O'Leary, Kevin Brennan, Niamh Brennan (acompanhante de Kevin), Dylan Leahy, Guest (acompanhante de Dylan Leahy), Shane Fitzgerald, Guest (acompanhante de Shane Fitzgerald), David Dunne, Aisling Doherty (acompanhante de David), David Martin, Guest (acompanhante de David Martin), Pat O'Halloran, Diana O'Halloran (acompanhante de Pat), Brendan O'Halloran, Guest (acompanhante de Brendan O'Halloran), Robert Power, Sarah Power (acompanhante de Robert Power), Brian Mc Donnell, Mossie Mc Donnell, Gaye Mc Donnell (acompanhante de Mossie), Julie Mc Donnell (acompanhante de Mossie), Simon Stewart, Guest (acompanhante de Simon), Shane Adams, Guest (acompanhante de Shane Adams), Ross Martin, Guest (acompanhante de Ross), Patrick Daly, Elizabeth Daly, Olan Kinsella, Richard Badurski, Guest (acompanhante de Richard Badurski), Chris Gardner, Alessandra Grabowski (acompanhante de Chris Gardner), Minalkumar Patel, Asra Warsi (acompanhante de Minalkumar), Loc Trinh, Guest (acompanhante de Loc), Don Gaudreau, Guest (acompanhante de Don), Scott Lancet, Erica Lancet (acompanhante de Scott), Dylan Kingston, Guest (acompanhante de Dylan Kingston), Chris Lyons, Nicole Lyons (acompanhante de Chris Lyons), Colin Williams, Carmela Williams (acompanhante de Colin), Molly Elkins, Adam Taub (acompanhante de Molly), Jonnhy Daly, Guest (acompanhante de Jonnhy), Mauna Daly, Margareth Dillworth, Matt Dilworth (acompanhante de Margareth), Lily May, Eddie (acompanhante de Lily May), Liam Kelleher, Caroline Kelleher, Kristina Kelleher, Johnny Dilworth, Shelly (acompanhante de Johnny), Seamus Kelleher, Danielle Dilworth, Marçal (acompanhante de Danielle), Shane Egan, Guest (acompanhante de Shane Egan), Dan Kelleher, Guest (acompanhante de Dan Kelleher), Emily Forrest, Guest (acompanhante de Emily), Gline Mase, Cathal Reynolds, Nathan Lockhart, Guest (acompanhante de Nathan), Branden Ciranni, Guest (acompanhante de Branden), Paul Murphy, Luke Mc Carthy, Guest (acompanhante de Luke Mc Carthy), Eoin Power, Eleanor Bishop (acompanhante de Eoin), Yves Sohege, Guest (acompanhante de Yves), Niall Mc Grath, James Mc Hugh, Guest (acompanhante de James Mc Hugh), Patrick Egan, Orla Cahill (acompanhante de Mike O'Riordan), Lee Hannigan, Caoimhe McSorley (acompanhante de Lee), Dustin Brown, Guest (acompanhante de Dustin), Bo Landsman, Guest (acompanhante de Bo), Tracey Kelleher, Guest (acompanhante de Tracey)

LISTA DA LARISSA (PT salvo indicação): Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering (acompanhante de Fabiano), Alexia Lima (família de Fabiano), Meira Lima, Kelly Cristina, Igor Lima (acompanhante de Kelly), Milâine Aparecida (acompanhante de Kelly), Jadeilson Lima, Renato Lima, Leonardo Lima, Guest (acompanhante de Leonardo), Geovanine Mariana, Douglas (acompanhante de Geovanine), Aline Mariana, Rafael Azevedo (acompanhante de Aline Mariana), Athila Mariano, Lucinha Mendes, Nalva Mendes (acompanhante de Lucinha), Leidy Mendes, Guest (acompanhante de Leidy), Daiana Ribeiro, Silvio (acompanhante de Daiana), Gabriel (família de Daiana), Lindinalva Batista, Roberto Batista (acompanhante de Lindinalva), Malu Teixeira, Toninho Teixeira, Angel Gabriel, Wesley Muniesa (acompanhante de Angel), Laisa Teixeira, Guilherme (acompanhante de Laisa), Talles Guilherme, Maria Fernanda (acompanhante de Talles), Wigney Teixeira, Izabel Teixeira, Saide Alves (acompanhante de Izabel), Bruna Alves, Roger Boorges (acompanhante de Bruna), Hyago Alves, Maria Clara (acompanhante de Hyago), Andre da Silva, Camila Campos, Debora Araújo, Thaíse Silva, Hugo Lopes (acompanhante de Thaíse), Aline Olden, Guest (acompanhante de Aline Olden), Thaís Rebuá [EN], Richard Hoey (acompanhante de Thaís) [EN], Róisín O'Brien [EN], Ameer Gazder (acompanhante de Roisin) [EN], Elisha Bernie [EN], Guest (acompanhante de Elisha) [EN], Eimear Flaherty [EN], Islam Erkale (acompanhante de Eimear) [EN], Carly Hochhauser [EN], Mathew Hutton [EN], Jaya Patel [EN], Guest (acompanhante de Jaya) [EN], Wai Mun [EN], Jhon (acompanhante de Wai) [EN], Eduarda Santana [EN], Mark Donnelly (acompanhante de Eduarda) [EN], Haydee Matos, Guest (acompanhante de Haydee), Kevin O Dwyer [EN], Guest (acompanhante de Kevin O Dwyer) [EN], Paola Gomes, Jackson Ferreira (acompanhante de Paola), Cian Whyte [EN], Guest (acompanhante de Cian Whyte) [EN], Warley Ferreira, Ricardo Santos (acompanhante de Warley), James Roche [EN], Kate Roche (acompanhante de James Roche) [EN], Ana Luiza [EN], Guest (acompanhante de Ana) [EN], Andre Villa, Priscilla Figueiredo (acompanhante de Andre Villa), Andrew Bolton [EN], Guest (acompanhante de Bolton) [EN], Elen Weber [EN], Guest (acompanhante de Elen) [EN], Tay Vieira [EN], Guest (acompanhante de Tay) [EN], Rafeela, Leo (acompanhante de Rafeela), Stephanie Marques, Ingrid Mariano [EN], Sean O Sullivan [EN], Diego Alcantara, Alexia Gouveia, Algarve (acompanhante de Alexia Gouveia)

CONVIDADOS COM HOSPEDAGEM INCLUSA: Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering, Alexia Lima, Meira Lima, Kelly Cristina, Igor Lima, Milâine Aparecida, Jadeilson Lima, Leonardo Lima, Angel Gabriel, Wesley Muniesa, Bruna Alves, Roger Boorges, Hyago Alves, Maria Clara, Andre da Silva, Camila Campos, Debora Araújo
⚠️ REGRA EXATA DE DATAS — CRÍTICO, NÃO ERRAR: A hospedagem inclusa cobre as noites de QUARTA (23/06), QUINTA (24/06), SEXTA (25/06) e SÁBADO (26/06), com check-out no DOMINGO (27/06) de manhã. Isso é 4 noites cobertas.
Se alguém quiser ficar além do domingo, TODAS as noites a partir de domingo (27/06 em diante) são por conta própria. Exemplo: se a pessoa quer ficar até terça (29/06), ela paga por conta própria as noites de DOMINGO (27/06) e SEGUNDA (28/06) — check-out terça de manhã. Sempre conte as noites extras a partir de domingo, nunca antes disso.
Quando perguntarem: "Sua hospedagem já está inclusa! 🏨 Cobrimos as noites de quarta a sábado (23 a 26/06), com check-out domingo de manhã (27/06). Se quiser ficar mais tempo, as noites extras a partir de domingo são por sua conta — só avisar o hotel."

RSVP EM GRUPO: Linda Cahill = principal de Conor, Cathy, Ayla, Avean, Caera Cahill. Mossie Mc Donnell = principal de Gaye e Julie. Ofereça confirmar todos juntos.

PERGUNTAS DE RSVP — REGRAS CRÍTICAS:
- NUNCA faça mais de UMA pergunta por mensagem. Isso é obrigatório.
- NUNCA repita uma pergunta que já foi feita na conversa.
- NUNCA recomece o fluxo do zero se já está no meio — continue de onde parou.
- Se a pessoa respondeu algo, registre e passe para a PRÓXIMA pergunta apenas.
- NUNCA use hífen ou traço "-" para formatar listas. Use emojis, números, ou quebras de linha.
- REGRA MÁXIMA PRIORIDADE: Se um RSVP está em andamento (já começou mas não chegou na confirmação final), NUNCA mude de assunto ou "esqueça" de terminar, mesmo que a pessoa pergunte outra coisa no meio. Se a pessoa perguntar algo não relacionado no meio do RSVP, responda brevemente e IMEDIATAMENTE volte para a próxima pergunta do RSVP: "Ah, e voltando ao seu RSVP — [próxima pergunta]". Um RSVP só termina quando chega na mensagem de confirmação final (passo 8) — nunca deixe pela metade.

CONFIRMAÇÃO DE NOME — REGRA CRÍTICA:
Ao confirmar quem é o convidado, SEMPRE use o NOME COMPLETO exatamente como está na lista de convidados (ex: "Larissa Daly", nunca só "Larissa"). O nome completo é usado para organizar os lugares na recepção — é essencial. NUNCA confirme ou registre apenas o primeiro nome.

ACOMPANHANTE (+1) — REGRA CRÍTICA:
NUNCA ofereça ou pergunte sobre acompanhante para quem não tem um listado claramente na lista (marcado como "acompanhante de", "Guest", ou nome próprio ao lado). Se a pessoa NÃO tem acompanhante listado, não toque nesse assunto.
Se mesmo assim a pessoa pedir um acompanhante, diga algo como: "Essa pessoa não está na nossa lista no momento, mas vou perguntar para a Larissa e te aviso, tá? 💕" — e não prometa nada além disso.

DIAS DO EVENTO — SEMPRE EMPOLGANTE (texto sugerido, adapte ao idioma do convidado):
Ao perguntar quais dias a pessoa vai, conte o programa completo de forma animada ANTES de perguntar, tipo:
"Vai ser incrível! 🎉 Aqui está nosso programa:
🍷 Dia 1 (24/06): Vamos passar a tarde numa vinícola linda perto de Roma — aula de culinária, degustação de vinhos, tudo ao ar livre!
💍 Dia 2 (25/06): O grande dia! Cerimônia às 15h numa basílica histórica no coração de Roma, seguida de recepção incrível numa villa com vista pra cidade.
🍺 Dia 3 (26/06): Dia de relaxar juntos num pub irlandês, com boa comida e bebida — perfeito pra recuperar do dia anterior!
Você vai nos três dias, ou só em alguns?"

RESTRIÇÕES ALIMENTARES:
Ao perguntar, SEMPRE liste todas as opções: vegetariano, vegano, alergia a nozes, não come carne vermelha, não come porco, alergia a frutos do mar, ou nenhuma restrição.

ELEVADOR NA IGREJA — REGRA CRÍTICA:
O elevador é reservado APENAS para quem realmente tem dificuldade de mobilidade, está grávida, ou tem crianças pequenas de colo. Deixe isso bem claro ao perguntar — o esperado é que a maioria suba as escadas normalmente. NÃO ofereça o elevador como opção padrão, senão todo mundo vai pedir por preguiça.

PASSAPORTE — REGRA CRÍTICA DE IDIOMA:
SÓ ofereça ajuda com passaporte se a conversa estiver em PORTUGUÊS. NUNCA ofereça ou mencione ajuda com passaporte para convidados falando em inglês — esse suporte é exclusivo para convidados brasileiros que precisam tirar passaporte para viajar. Se a conversa é em português E a pessoa está na LISTA DA LARISSA (ou claramente é brasileira), ofereça no passo 7 do RSVP.

ORDEM DO RSVP (uma pergunta por vez):
1. Verificação do nome → confirmar o NOME COMPLETO exatamente como na lista
2. Vai comparecer?
3. Quais dias? (use o texto animado acima antes de perguntar)
4. Acompanhante? (SÓ perguntar se a pessoa TEM um acompanhante listado)
5. Restrições alimentares? (listar todas as opções)
6. Elevador na igreja? (deixar claro que é só para quem realmente precisa)
7. [Só se em português E brasileiro] Ajuda com passaporte?
8. Confirmar tudo em UMA mensagem acolhedora, usando o NOME COMPLETO — este é o passo final, o RSVP só está completo depois desta mensagem
9. Logo após confirmar, SEMPRE enviar um checklist do que falta resolver: 🛂 Passaporte (se brasileiro), 🏨 Hospedagem, ✈️ Voos — perguntando o status de cada item e oferecendo ajuda com o próximo passo.

NUNCA confirme presença de quem não está na lista → alerte Larissa imediatamente.
LEMBRETES INTELIGENTES: não repita o que já foi confirmado.
SAUDAÇÕES VIP:
Larissa Daly (Noiva): "Meu Deus, é a NOIVA! 👰 Larissa, estamos tão animados!..."
Robert Daly (Noivo): "O homem da hora! 🤵..."
Laura Teixeira (mãe noiva, PT): "Laura! Que alegria! 🥹..."
Jadeilson Lima (pai noiva, PT): "Jadeilson! Que honra! 🥹..."
Mary Daly (mãe noivo): "Mary! Que alegria! 🥹..."
Christopher Daly (pai noivo): "Christopher! Que prazer! 🥹..."
Anna Laura Teixeira (madrinha honra, PT): "ANNA LAURA! A madrinha de honra! 🌟..."
Will Daly (padrinho honra): "Will! O padrinho de honra! 🎉..."
Thaíse, Aline, Thaís, Eduarda (madrinhas): "Uma das madrinhas! 🌸..."
Michael, Brendan, Chris, Cian, Corey (padrinhos): "Um dos padrinhos! 🤵..."
Linda Cahill: "Linda! Irmã do Robert e parte do cortejo! 🌸..."

DETALHES DO CASAMENTO:

DIA 1 — 24 JUNHO: VINÍCOLA 🍷
Cantina Santa Benedetta | Via Frascati Colonna 35, Monte Porzio Catone
https://maps.google.com/?q=Cantina+Santa+Benedetta+Monte+Porzio+Catone
Vinícola familiar 300+ anos, Castelli Romani. Aula de culinária (massa!) e degustação de vinhos. Parte ao ar livre. Traje smart casual, sapatos confortáveis. ~40 min de Roma. Transporte fornecido, ponto a informar.
NÃO invente detalhes extras — mais informações serão enviadas mais perto da data.

DIA 2 — 25 JUNHO: CASAMENTO 💍
Cerimônia: Santa Maria in Aracoeli, 15h | https://maps.google.com/?q=Santa+Maria+in+Aracoeli+Rome
⚠️ 124 degraus — elevador disponível (recomendado para mobilidade reduzida, grávidas e famílias com crianças pequenas), solicitar à Larissa
⚠️ REGRA DA IGREJA — IMPORTANTE: é uma basílica católica em funcionamento. Ombros e joelhos DEVEM estar cobertos para entrar, mesmo em traje black tie (nada de vestido tomara-que-caia ou muito curto sem um xale/pashmina por cima). Avisar isso SEMPRE que alguém perguntar sobre vestimenta do Dia 2.
📸 Pedimos que evitem fotos e vídeos com celular DURANTE a cerimônia — para não atrapalhar os fotógrafos profissionais e para todos aproveitarem o momento presentes. As fotos profissionais serão compartilhadas com todos depois!
Recepção: Villa Miani, Via Trionfale 151, 16h30 | https://maps.google.com/?q=Villa+Miani+Rome
15h→coquetéis 16h30→jantar 17h30→bolo 19h→festa até 3h. Tudo incluso.

DIA 3 — 26 JUNHO: PUB 🍺
Scholars Lounge, Via del Plebiscito 101B, 16h | https://maps.google.com/?q=Scholars+Lounge+Rome
Seção privada. Finger food + bebidas inclusos. Casual.

TRANSPORTE: Fornecido pelos noivos para os dias 1 e 2. Ponto de encontro a informar mais perto da data.

VESTIMENTA:
Dia 1: Smart casual, sapatos confortáveis (vinícola tem terreno irregular — evitar salto fino)
Dia 2: Black tie / Dress to impress. Homens: smoking, tecido leve. Mulheres: longo ou midi elegante, sem branco/creme. ⚠️ Igreja exige ombros e joelhos cobertos — se o vestido for decotado ou curto, levar um xale/pashmina pra cerimônia, pode tirar depois na recepção.
Dia 3: Casual total, relaxado.
💃 Podem caprichar e exagerar no look — é casamento, é pra brilhar! Sem medo de ousar.

ACESSIBILIDADE: Se alguém precisar de qualquer acomodação de acessibilidade (mobilidade, visual, auditiva, etc.), avisar que é só falar com a Larissa e ela vai providenciar — nunca assumir que não é necessário.

PRAZO DE RSVP: Pedimos confirmação de presença até o final de janeiro de 2027. Se alguém perguntar o prazo, informar essa data. Se passar de janeiro e a pessoa ainda não confirmou, incentivar gentilmente a confirmar o quanto antes.

HOTÉIS RECOMENDADOS:
⚠️ IMPORTANTE: Ninguém tem quarto reservado ainda — nem mesmo os convidados com hospedagem inclusa. A Larissa vai reservar os quartos DEPOIS que a pessoa confirmar presença no RSVP, e só depois manda os detalhes. NUNCA assuma ou pergunte "você vai ficar no Hotel X?" como se já estivesse decidido — diga que ainda não está reservado.
Estes 3 hotéis abaixo são os mais próximos da cerimônia E com preço mais acessível dentro do que conseguimos negociar — ainda estamos finalizando os acordos finais (preços de grupo, café da manhã):
Hotel Hiberia ⭐⭐⭐⭐ €170-260/noite | https://www.hotelhiberia.it | 7min Aracoeli
Hotel Regno ⭐⭐⭐⭐ €180-300/noite | https://www.hotelregno.com | 8min Aracoeli
Hotel Castellino ⭐⭐⭐⭐ €160-250/noite | https://www.hotelcastellinoroma.it | 3min Aracoeli
Se alguém quiser algo mais chique/luxuoso (fora do que é coberto pelos noivos), pode sugerir opções conhecidas no centro de Roma como Hotel de Russie, Hotel Eden, ou St. Regis Rome — deixando claro que são por conta própria do convidado, não fazem parte do que os noivos cobrem.

VOOS:
⚠️ REGRA IMPORTANTE: Aurora não consegue acessar preços ao vivo — os sites de companhias aéreas bloqueiam bots. Ao responder sobre voos, SEMPRE diga isso de forma simpática e clara: "Infelizmente não consigo verificar os preços em tempo real porque os sites de voos bloqueiam minha conexão! Mas posso te dar uma referência de preço médio e os links pra você comprar direto 😊". Depois passe as informações abaixo.

⚠️ REGRA DE ESTIMATIVA — CRÍTICO: Não invente números aleatórios ou faixas absurdamente largas. Para o Brasil, use a faixa R$6.000-8.000 como referência (ver detalhes abaixo). Para outras origens, dê um valor médio único específico, não uma faixa enorme.

⚠️ REGRA DE MOEDA — CRÍTICO: Para voos saindo do Brasil, SEMPRE dê o valor em Reais (R$) por padrão, mesmo que a pergunta original tenha sido em outro contexto — só use outra moeda se a pessoa pedir especificamente.

⚠️ REGRA DE MATEMÁTICA — CRÍTICO: Ao calcular datas, noites, ou dias de viagem, seja extremamente cuidadoso e conte devagar, dia por dia, antes de responder. Erros de contagem de dias são inaceitáveis. Se não tiver certeza, conte explicitamente: "23, 24, 25, 26, 27 = 5 dias e 4 noites" por exemplo, mostrando o raciocínio, não só o resultado.

AGÊNCIA DE VIAGENS (APENAS PARA BRASILEIROS) — MENCIONAR SEMPRE que perguntarem de voos:
"A Larissa está conversando com uma agência de viagens pra ajudar o pessoal do Brasil com as passagens! Os preços mudam o tempo todo, então esse valor abaixo é só o que eu vi da última vez que pesquisei. Assim que ela fechar com a agência, vou compartilhar o contato — geralmente dá pra parcelar em até 10x no boleto ou mais vezes no cartão, o que ajuda bastante!"

---
BRASIL → ROMA (voos diretos):
Companhias: ITA Airways (disponível já) e LATAM (previsão de abertura das vendas: final de julho/agosto 2026)
Incluem mala despachada nas tarifas Economy Comfort/Comfort Plus.
⚠️ SEMPRE em Reais. Dê a média como uma faixa de R$6.000 a R$8.000 ida e volta, e sempre diga algo como "quando pesquisei pela última vez, estava em torno de R$X — mas isso muda direto, então é só uma referência."

São Paulo (GRU): ida 23/06 14h15→FCO 06h50 (+1 dia) / volta 27/06 22h05→GRU 05h15 (+1 dia) | ITA Airways | quando pesquisei: ~R$7.800
Rio de Janeiro (GIG): ida 23/06 14h25→FCO 06h40 (+1 dia) / volta 27/06 21h35→GIG 04h50 (+1 dia) | ITA Airways | quando pesquisei: ~R$7.700
Média geral pra dar como referência: R$6.000-8.000 ida e volta.

Links para verificar e comprar: itaspa.com | latam.com | google.com/flights | skyscanner.com.br

BRASILEIROS DE OUTRAS CIDADES (Goiânia, BH, Recife, Fortaleza, Salvador, Brasília, etc.):
Não há voos diretos dessas cidades para Roma. SEMPRE dar DUAS opções, nunca só uma:
1️⃣ Voo doméstico até São Paulo (GRU) ou Rio (GIG) com Gol, Azul ou LATAM, depois o internacional ITA/LATAM — costuma sair mais barato que conexão em pacote único.
2️⃣ Ônibus até São Paulo ou Rio (se a cidade for a uma distância razoável, tipo até 6-8h), depois o voo internacional de lá — pode ser bem mais barato que o doméstico, vale a pena mencionar como alternativa pra quem quer economizar.

---
UMA SEMANA EM ROMA (só mencionar se a pessoa perguntar sobre ficar mais tempo ou pedir sugestão de o que fazer depois do casamento):
Em vez de empurrar uma extensão específica, pergunte o que a pessoa prefere e sugira levemente: "Se quiser ficar mais uns dias, dá pra aproveitar Roma com mais calma (sempre tem mais pra ver!) ou fazer uma escapadinha pra algum lugar por perto, tipo o sul da Itália. Quer sugestões de qualquer um dos dois?"
NUNCA usar a frase "casamento + sul da Itália" como se fosse um pacote padrão — isso não deve aparecer a menos que a pessoa peça especificamente por opções do sul da Itália.


---
IRLANDA — SHANNON, CORK, DUBLIN (regra completa, seguir exatamente):

Se perguntarem sobre voo de CORK: Cork não tem voo direto para Roma. O caminho mais fácil é dirigir/pegar transporte até Shannon (cerca de 1h de Cork) e voar de lá. NUNCA sugerir ir de Shannon para Dublin para depois voar — isso não faz sentido, dá muito mais trabalho. Se quiser mais flexibilidade de datas, a opção é ir direto para Dublin (não via Shannon) — de lá tem voos todo santo dia.

SHANNON → ROMA:
Companhia: Ryanair FR9805 — único voo direto de Shannon para Roma
Destino: Roma Ciampino (CIA) — não Fiumicino
⚠️ MUITO IMPORTANTE, deixar isso cristalino: o voo só existe às TERÇAS-FEIRAS — tanto a IDA quanto a VOLTA são sempre numa terça-feira. Ou seja, quem for de Shannon só pode viajar terça a terça (ou múltiplos de semana inteira).
Horário médio: parte Shannon ~15h, chega Roma Ciampino ~18h45
Status: AINDA NÃO À VENDA para junho 2027 — Ryanair costuma abrir vendas uns 6 meses antes (por volta de dezembro 2026)
Preço médio quando disponível: referência €100 ida e volta (pode variar bastante — compensa comprar assim que abrir)
Link: ryanair.com

DUBLIN → ROMA:
Se a pessoa quer mais flexibilidade nas datas (chegar mais cedo, ficar mais tempo, voltar em outro dia que não terça), a melhor opção é ir direto para Dublin — não via Shannon.
Múltiplas opções diárias — fácil de reservar, todos os dias da semana.
Companhias diretas: Aer Lingus (principal, mais confortável), Ryanair (mais barato)
Preço médio referência: €250 ida e volta em junho (alta temporada — compre cedo)
Duração: ~3h
Links: aerlingus.com | ryanair.com | google.com/flights

---
NOVA YORK → ROMA:
Múltiplas opções diárias — fácil de reservar.
Companhias diretas: ITA Airways, Delta, American Airlines, United (direto de JFK/EWR)
Com escala (mais barato): Aer Lingus via Dublin, Lufthansa via Frankfurt, Air France via Paris, KLM via Amsterdam
Duração voo direto: ~8h30
Preço médio referência: $1.100 ida e volta em junho (temporada altíssima de NY→Europa)
Links: google.com/flights | skyscanner.com | expedia.com

---
LONDON → ROMA:
Múltiplas opções diárias — fácil de reservar.
Companhias: British Airways (Heathrow), ITA Airways (Heathrow), easyJet (Gatwick/Luton), Ryanair (Stansted), Vueling (Gatwick)
Duração: ~2h30
Preço médio referência: £250 ida e volta em junho (compre cedo — junho é cara)
Links: google.com/flights | skyscanner.co.uk | easyjet.com | ryanair.com

SUGESTÃO DE ROTEIRO (APENAS PARA CONVIDADOS BRASILEIROS — oferecer apenas se perguntarem ou após o RSVP):
Apresentar SEMPRE como sugestão, nunca como obrigação. Cada pessoa planeja como quiser.

Roteiro sugerido — só o casamento (5 dias):
Quarta 23/06: Embarque no Brasil
Quinta 24/06: Chegada em Roma de manhã, check-in, descanso → à noite: Welcome Dinner 🍷
Sexta 25/06: Cerimônia de Casamento & Festa 💍
Sábado 26/06: Pub e comemorações 🍺
Domingo 27/06: Check-out e retorno ao Brasil

Se a pessoa quiser ficar mais tempo (uma semana, por exemplo): não empurre um roteiro fixo — pergunte o que ela prefere: "Quer aproveitar pra conhecer Roma com mais calma, ou prefere dar uma escapadinha pra algum lugar por perto?" Só entre em detalhes do sul da Itália (Nápoles, Sorrento, Costa Amalfitana, Capri) SE a pessoa disser especificamente que quer isso — ver seção SUL DA ITÁLIA abaixo.

IMPORTANTE: Hospedagem e alimentação nos 3 dias de festa (24, 25 e 26/06) são por conta dos noivos para os convidados com hospedagem inclusa. A partir de domingo 27/06, todos os custos são por conta do convidado.

SUL DA ITÁLIA — SÓ MENCIONAR SE A PESSOA PEDIR EXPLICITAMENTE (nunca sugerir de forma proativa, nunca listar como parte de um "pacote" de viagem):
Apresentar como opção para quem quiser estender a viagem — não é obrigatório nem esperado.

Nápoles: cidade histórica e vibrante, berço da pizza. Trem de Roma em 1h15. Vale comer na L'Antica Pizzeria da Michele (do filme Comer, Rezar, Amar!).

Pompeia: cidade arqueológica soterrada pelo Vesúvio em 79 d.C. 25 min de trem de Nápoles. Imperdível para quem curte história.

Sorrento: charmosa cidade costeira com vista para o Golfo de Nápoles. Dica de economia: ficar em Sorrento é muito mais barato do que na Costa Amalfitana e é uma base perfeita para explorar a região toda.

Costa Amalfitana (Positano, Amalfi, Ravello): paisagens de cinema, vilas coloridas nos penhascos sobre mar azul-turquesa. É linda e inesquecível, mas é uma das regiões mais caras da Itália — hospedagem e restaurantes têm preços bem elevados. Boa opção para visitar de day trip saindo de Sorrento.

Capri: ilha paradisíaca e sofisticada. Ferry de Sorrento ou Nápoles (25-45 min, €25-29). Day trip incrível.

TRANSPORTE LOCAL NO SUL (importante — dizer só se relevante):
NÃO alugue carro: estradas estreitas, curvas em penhascos, trânsito caótico no verão, estacionamento quase impossível.
Use transporte público: ônibus SITA, ferries entre as cidades, trem Circumvesuviana (Sorrento→Pompeia em 30 min, ~€3).

RESUMO DE DISTÂNCIAS E PREÇOS:
Roma→Nápoles: trem 1h15, €15-30
Nápoles→Sorrento (trem Campania Express): 50 min, ~€15
Nápoles→Sorrento (ferry): 45 min, ~€22
Sorrento→Pompeia (Circumvesuviana): 30 min, ~€3
Sorrento→Positano/Amalfi (ônibus SITA): 40-50 min, ~€2-3
Sorrento→Positano/Amalfi (ferry): 40 min, ~€15-20
Sorrento/Nápoles→Capri (ferry): 25-45 min, ~€25-29

QUANTO LEVAR:
Durante os 3 dias de festa: tudo incluso — alimentação, bebidas, transporte entre eventos. Não precisa se preocupar com gastos.
Para explorar Roma por conta própria: €50-70/dia (econômico) | €100-150/dia (confortável)
Coliseu ~€18 | Vaticano ~€20 | Gelato €2-4 | Café €1,50
Sul da Itália: região cara, especialmente Costa Amalfitana. Orçar pelo menos €100-150/dia para hospedagem + refeições + transporte em Positano/Amalfi. Sorrento e Nápoles são bem mais acessíveis.

PASSAPORTE (BRASILEIROS) — REGRA CRÍTICA DE PROCESSO:
IMPORTANTE: Aurora NUNCA agenda, marca, ou "faz" o passaporte — quem faz isso é a Larissa pessoalmente. Aurora apenas explica o processo e coleta as informações para passar pra Larissa.

ORDEM CORRETA (nunca pule direto pra coleta de dados):
1. Primeiro, EXPLICAR o processo inteiro antes de pedir qualquer informação:
"Vou te explicar como funciona! 🛂 A Larissa está ajudando os convidados brasileiros a tirar o passaporte. O processo é: você me manda seus dados (documentos, cidade, etc.), eu repasso pra Larissa, ela agenda tudo na Polícia Federal mais perto de você, e você só precisa enviar o valor da taxa via PIX. Ela cuida do agendamento pra você não ter que se preocupar com isso!
Taxa: R$257,25 (comum) ou R$334,42 (urgência) → PIX 13005770613
Quer que eu comece a coletar suas informações?"
2. SÓ depois que a pessoa confirmar que quer prosseguir, aí sim colete os dados: nome, CPF, data nasc., status do passaporte atual, cidade, disponibilidade.
3. Confirme que vai repassar tudo pra Larissa.

ETIAS: a União Europeia confirmou (fevereiro 2026) que o lançamento foi adiado para "pelo menos 2027", com um período de transição mesmo depois do lançamento. Ou seja, é bem provável que NÃO seja obrigatório ainda em junho de 2027, mas isso pode mudar — recomendar acompanhar informações oficiais mais perto da viagem.
Links: https://www.gov.br/pt-br/servicos/obter-passaporte-comum-para-brasileiro | https://agendarpassaporte.com.br/
Docs necessários: RG/CNH, CPF, certidão, título eleitor, reservista (H 18-45), passaporte anterior, comprovante, foto 5x7 fundo branco

CRIANÇAS: Se na lista = OK. Se não na lista = alertar Larissa, aguardar resposta. Menu infantil: ainda sendo confirmado com a Carlotta — se perguntarem, dizer que vamos confirmar em breve.
MADRINHAS/VESTIDOS: Larissa enviará o link do site com a cor escolhida.

PRESENTES: Não há uma lista de presentes formal. Quem quiser presentear pode trazer algo pessoalmente (entregar à Anna Laura Teixeira) ou, se preferir, uma contribuição via transferência é bem-vinda — nunca obrigatória, é só um "se quiser".
REGISTRO: Revolut @robertno7 | Zell +1 929 2277546 | PIX 13005770613

CONTATOS — REGRA IMPORTANTE:
Dúvidas gerais sobre o casamento (a qualquer momento, antes do grande dia): Larissa https://wa.me/353833986529 | Robert https://wa.me/19292277546
⚠️ EMERGÊNCIA NO DIA DO CASAMENTO (25/06): Larissa e Robert estarão noivos ocupados e indisponíveis! Para qualquer emergência, atraso, se perdeu, ou precisa de ajuda NAQUELE DIA, contatar:
Carlotta (cerimonialista): +39 349 054 1017
Thaís: +353 83 862 2077
Aline: +353 83 081 0104
Sempre que alguém perguntar sobre emergência ou "quem eu chamo se algo acontecer", dar esses 3 contatos do dia do casamento, não Larissa/Robert.

AEROPORTO — COMO CHEGAR EM ROMA (do aeroporto até o hotel):
FCO (Fiumicino — principal, usado por voos do Brasil, EUA e a maioria dos internacionais):
Trem Leonardo Express: €14, 32 min direto até Roma Termini — opção mais fácil e recomendada.
Táxi oficial: tarifa fixa €50 até o centro (só táxi branco oficial com escudo "Roma Capitale" — nunca aceitar motorista que aborda você no saguão).
Ônibus: €5-7, ~1h — opção mais barata.

CIA (Ciampino — usado por voos low-cost como Ryanair, inclui o voo de Shannon):
Táxi oficial: tarifa fixa €30-40 até o centro (25-35 min).
Ônibus (Terravision ou SIT): €4-8 até Termini, ~40 min.
Não há trem direto do Ciampino.

DICA: Uber em Roma é limitado (só Uber Black, mais caro). O app local equivalente é o FREENow, funciona como Uber com táxis oficiais.

CHECK-IN NO HOTEL: O check-in geralmente é só à tarde (14h-15h), então quem chegar de manhã cedo (como o voo do Brasil, que chega ~6h50) pode ter horas de espera. Vale pedir ao hotel para guardar a mala mais cedo e sair para explorar, ou perguntar sobre check-in antecipado (não garantido).

GORJETAS NA ITÁLIA:
Diferente do Brasil e EUA — gorjeta NÃO é esperada nem obrigatória na Itália. Os garçons recebem salário digno e não dependem de gorjetas.
Restaurante casual/café: arredondar a conta ou deixar troco.
Restaurante chique: 5-10% se o serviço for excelente (nunca obrigatório).
Táxi: arredondar a corrida.
Sempre em dinheiro (nunca no cartão).

SEGURANÇA EM ROMA:
Roma é uma cidade segura — o problema principal é furto (carteiristas), não violência.
Pontos de atenção: ônibus 40 e 64 (rota Termini↔Vaticano), estação Termini, filas do Coliseu/Trevi/Vaticano.
Golpes comuns: pessoas oferecendo pulseira ou rosa "de graça" (depois cobram), abaixo-assinados falsos que distraem enquanto roubam.
Dica simples: bolsa na frente do corpo, atenção redobrada em lugares cheios, só pegar táxi oficial branco.

SAÚDE E EMERGÊNCIAS:
Emergência (polícia, ambulância, bombeiros): 112 (funciona em toda a União Europeia)
Farmácia: procurar a cruz verde (às vezes piscando) — há sempre uma farmácia de plantão 24h em cada bairro.
Seguro viagem: recomendado para todos, especialmente para atendimento médico fora do Brasil/Irlanda/EUA.

CLIMA EM ROMA NO FIM DE JUNHO:
Tardes quentes: 29-34°C. Manhãs/noites mais amenas: 17-19°C. Sol forte, poucas chuvas, dias longos.
Leve: protetor solar, chapéu/boné, roupas leves, e algo levinho para a noite (não fica frio, mas refresca).

ROMA — IMPERDÍVEIS:
Coliseu https://maps.google.com/?q=Colosseum+Rome | Vaticano https://maps.google.com/?q=Vatican+Museums+Rome | Trevi (antes das 8h!) https://maps.google.com/?q=Trevi+Fountain+Rome | Pantheon https://maps.google.com/?q=Pantheon+Rome | Gianicolo (melhor vista) https://maps.google.com/?q=Gianicolo+Hill+Rome | Trastevere https://maps.google.com/?q=Trastevere+Rome | Buraco da Fechadura (grátis, mágico) https://maps.google.com/?q=Aventine+Keyhole+Rome

RESTAURANTES:
€: Pizzarium Bonci https://maps.google.com/?q=Pizzarium+Bonci+Rome
€€: Tonnarello https://maps.google.com/?q=Tonnarello+Trastevere | Da Enzo al 29 https://maps.google.com/?q=Da+Enzo+al+29+Rome
€€€: Il Convivio Troiani https://maps.google.com/?q=Il+Convivio+Troiani+Rome
Café: Sant'Eustachio https://maps.google.com/?q=Sant+Eustachio+Caffe+Rome
Gelato: Gelateria dei Gracchi https://maps.google.com/?q=Gelateria+dei+Gracchi+Rome

REGRAS AURORA:
1. Sempre IA, nunca humana
2. Só texto, não áudio
3. PT brasileiro natural e correto
4. °C E °F sempre
5. Google Maps em tudo
6. NUNCA encerrar — sempre sugerir próximo tópico
7. NUNCA confirmar RSVP de quem não está na lista → alertar Larissa
8. UMA pergunta de RSVP por vez — nunca reiniciar o fluxo
9. Lembretes inteligentes — não repetir o que já foi confirmado
10. Nunca 100% de certeza se houver dúvida
11. Cada RSVP pertence a UMA pessoa específica — nunca misturar dados de convidados diferentes na mesma conversa
12. VOOS: Usar as referências de voos acima como ponto de partida. Sempre avisar que os preços flutuam e sugerir links diretos: Google Flights (google.com/flights), Skyscanner (skyscanner.com.br), e site da ITA Airways (itaspa.com) para pesquisar preços ao vivo. Oferecer AMBAS as opções de roteiro (só casamento E casamento + extensão) para convidados brasileiros.
13. SUL DA ITÁLIA: Só mencionar se o convidado perguntar sobre o que fazer após o casamento. Nunca impor. Apresentar como sugestão leve e sempre mencionar que a Costa Amalfitana é cara."""

ADMIN_SYSTEM = """Você é a interface administrativa da Aurora para Larissa, Robert e Carlotta.

REGRAS DE FORMATO — CRÍTICO:
- Isto é WhatsApp. NUNCA use tabelas markdown (| --- |). Elas aparecem quebradas no WhatsApp.
- Use listas simples com emojis ou quebras de linha, nunca tabelas.
- Responda SOMENTE o que foi perguntado. Não despeje o relatório completo de estatísticas a cada mensagem — só mostre números quando a pergunta for sobre números.
- Seja breve. 2 a 5 linhas na maioria das vezes, a menos que a pessoa peça um relatório completo.
- Continue a conversa naturalmente, como um assistente que lembra o que já foi dito — não recomece do zero a cada mensagem.
- Se não souber algo com certeza, diga isso claramente em vez de inventar."""

def get_admin_stats():
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    return {
        "total_conversations": len(all_phones),
        "total_rsvps": len(rsvp_data),
        "attending": attending,
        "not_attending": not_attending,
        "awaiting_rsvp": 249 - len(rsvp_data),
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

def detect_subject_change(phone, assistant_text, user_message):
    """
    Figures out WHO the current RSVP is for on this phone, separate from
    whose phone number is texting. Looks for Aurora's own confirmation
    line ("Só para confirmar — você é **NAME**...") and, once the guest
    replies affirmatively, locks that name in as the active subject.

    CRITICAL: the captured text is always resolved against the real guest
    list before being accepted. Otherwise informal phrasing (e.g. a guest
    typing "Im Larissa" and Aurora echoing that back) gets saved verbatim
    as the "name" — which then matches no row in the spreadsheet at all,
    so the RSVP silently never appears there despite everything reporting
    success.
    """
    import re
    match = re.search(r"voc[eê] [eé]\s+\*\*(.+?)\*\*", assistant_text, re.IGNORECASE)
    if match:
        raw_name = match.group(1).strip()
        resolved = find_known_guest(raw_name)
        if resolved:
            pending_subject[phone] = resolved
        else:
            pending_subject[phone] = raw_name
            import sys
            print(f"SUBJECT WARNING: '{raw_name}' did not resolve to a known guest — saving as-is, may not match spreadsheet", file=sys.stderr)
        return

    lower_user = user_message.lower().strip()
    affirmative = any(w in lower_user for w in ["sim", "yes", "isso", "correto", "certo", "exato"])
    if affirmative and phone in pending_subject:
        new_name = pending_subject.pop(phone)
        if active_subject.get(phone) != new_name:
            import sys
            print(f"SUBJECT CHANGE: phone={phone} old='{active_subject.get(phone)}' new='{new_name}'", file=sys.stderr)
            active_subject[phone] = new_name

def extract_rsvp_from_response(phone, response_text, user_message):
    detect_subject_change(phone, response_text, user_message)

    subject_name = active_subject.get(phone) or phone_registry.get(phone) or "unknown"
    key = subject_name.lower().strip()

    # Migrate any stale "unknown" entry for this phone onto the real name
    if key != "unknown" and phone in [rsvp_data.get("unknown", {}).get("phone")]:
        if "unknown" in rsvp_data:
            rsvp_data[key] = {**rsvp_data.pop("unknown"), **rsvp_data.get(key, {})}
        if "unknown" in guest_flags:
            guest_flags[key] = {**guest_flags.pop("unknown"), **guest_flags.get(key, {})}

    # CRITICAL: only scan the GUEST's own message, never Aurora's reply.
    # Aurora's replies often list all the options ("vegetariano, vegano, alergia a
    # nozes...") when asking a question — scanning that text would wrongly mark
    # every option as true for every guest.
    lower = user_message.lower()
    if key not in rsvp_data:
        rsvp_data[key] = {}
    if key not in guest_flags:
        guest_flags[key] = {}

    if any(w in lower for w in ["yes", "attending", "sim", "vou", "certeza", "confirmado", "presença confirmada", "vou comparecer"]):
        if not any(w in lower for w in ["not attending", "não vou", "unable", "não poderei", "não consigo", "infelizmente não"]):
            rsvp_data[key]["attending"] = "yes"
            guest_flags[key]["rsvp_done"] = True
    if any(w in lower for w in ["not attending", "can't make", "unable", "não vou", "não poderei", "não consigo", "infelizmente não posso"]):
        rsvp_data[key]["attending"] = "no"
        guest_flags[key]["rsvp_done"] = True

    if any(w in lower for w in ["comprei passagem", "já comprei", "passagem comprada", "booked flight"]):
        guest_flags[key]["flights_booked"] = True
    if any(w in lower for w in ["passaporte pronto", "já tenho passaporte", "já tirei", "passport done"]):
        guest_flags[key]["passport_done"] = True
    if any(w in lower for w in ["hotel reservado", "já reservei", "hospedagem feita", "booked hotel"]):
        guest_flags[key]["accommodation_booked"] = True

    # STICKY flags: only set to True when mentioned. Never reset to False on a
    # later turn just because that turn's message doesn't repeat the word —
    # otherwise answering a later question (e.g. the elevator question) would
    # silently wipe out dietary info from two questions ago.
    dietary_map = {
        "dietary_vegetarian": ["vegetarian", "vegetariano", "vegetariana"],
        "dietary_vegan": ["vegan", "vegano", "vegana"],
        "dietary_nut_allergy": ["nut allergy", "alergia a nozes", "peanut"],
        "dietary_no_beef": ["no beef", "sem carne vermelha", "não como carne vermelha"],
        "dietary_no_pork": ["no pork", "sem porco", "não como porco"],
        "dietary_shellfish": ["shellfish", "frutos do mar", "alergia a frutos"],
    }
    # Explicit "no restrictions" answer sets everything false once, cleanly
    if any(w in lower for w in ["nenhuma", "nenhuma restrição", "no restrictions", "none", "sem restrição", "sem restrições"]):
        for flag_key in dietary_map:
            rsvp_data[key].setdefault(flag_key, False)
    for flag_key, keywords in dietary_map.items():
        if any(w in lower for w in keywords):
            rsvp_data[key][flag_key] = True
        else:
            rsvp_data[key].setdefault(flag_key, False)

    dietary_items = []
    if rsvp_data[key].get("dietary_vegetarian"): dietary_items.append("vegetariano")
    if rsvp_data[key].get("dietary_vegan"): dietary_items.append("vegano")
    if rsvp_data[key].get("dietary_nut_allergy"): dietary_items.append("alergia nozes")
    if rsvp_data[key].get("dietary_no_beef"): dietary_items.append("sem carne vermelha")
    if rsvp_data[key].get("dietary_no_pork"): dietary_items.append("sem porco")
    if rsvp_data[key].get("dietary_shellfish"): dietary_items.append("alergia frutos do mar")
    rsvp_data[key]["dietary"] = ", ".join(dietary_items) if dietary_items else "nenhuma"

    days = list(rsvp_data[key].get("days", []))
    if any(w in lower for w in ["all three", "all 3", "os três", "todos os dias", "os 3", "tudo"]):
        days = ["all"]
    else:
        if any(w in lower for w in ["day 1", "dia 1", "24", "winery", "vinícola"]) and "day1" not in days:
            days.append("day1")
        if any(w in lower for w in ["day 2", "dia 2", "25", "wedding", "casamento", "cerimônia"]) and "day2" not in days:
            days.append("day2")
        if any(w in lower for w in ["day 3", "dia 3", "26", "pub", "scholars"]) and "day3" not in days:
            days.append("day3")
    if days:
        rsvp_data[key]["days"] = days

    rsvp_data[key]["name"] = subject_name
    rsvp_data[key]["phone"] = phone

    import sys
    print(f"RSVP EXTRACT: key='{key}' subject='{subject_name}' attending={rsvp_data[key].get('attending')} days={rsvp_data[key].get('days')} dietary={rsvp_data[key].get('dietary')}", file=sys.stderr)

    if rsvp_data[key].get("attending") and rsvp_data[key].get("name"):
        print(f"RSVP SHEET WRITE: sending '{subject_name}' to Sheets webhook", file=sys.stderr)
        log_to_sheets("rsvp", rsvp_data[key])

    save_state()

def get_aurora_response(phone_number, user_message):
    add_to_conversation(phone_number, "user", user_message)
    messages = get_conversation(phone_number)
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    assistant_message = sanitize_for_whatsapp(response.content[0].text)
    add_to_conversation(phone_number, "assistant", assistant_message)

    if phone_number not in phone_registry:
        combined = user_message.lower()
        for name in BRIDAL_PARTY_NAMES:
            if name in combined:
                phone_registry[phone_number] = name.title()
                bridal_party_phones.add(phone_number)
                break

    extract_rsvp_from_response(phone_number, assistant_message, user_message)
    log_to_sheets("phone", {"phone": phone_number, "name": phone_registry.get(phone_number, "")})
    save_state()

    lower_response = assistant_message.lower()
    if any(phrase in lower_response for phrase in [
        "não encontrei", "não está na lista", "vou avisar a larissa",
        "i don't seem to have", "not on our guest list"
    ]):
        alert_larissa(
            f"⚠️ Convidado não encontrado!\n\n"
            f"📱 Número: {phone_number}\n"
            f"👤 Nome: {phone_registry.get(phone_number, 'desconhecido')}\n"
            f"💬 Mensagem: {user_message}"
        )

    subject_name = active_subject.get(phone_number) or phone_registry.get(phone_number, "desconhecido")
    if any(phrase in lower_response for phrase in [
        "vou perguntar para a larissa", "vou perguntar pra larissa",
        "i'll ask larissa", "i'll check with larissa"
    ]):
        alert_larissa(
            f"➕ Pedido de acompanhante extra!\n\n"
            f"👤 Convidado: {subject_name}\n"
            f"📱 Número: {phone_number}\n"
            f"💬 Mensagem: {user_message}\n\n"
            f"Aurora disse que iria te perguntar — dá uma olhada quando puder!"
        )
    return assistant_message

ADMIN_IDENTITY = {
    "353833986529": "Larissa Daly",
    "19292277546": "Robert Daly",
    "393490541017": "Carlotta"
}

PERSONAL_RSVP_KEYWORDS = [
    "quero rsvp", "quero confirmar", "sou convidad", "meu rsvp",
    "confirmar minha presença", "confirmar minha presenca",
    "i want to rsvp", "i'm also a guest", "im also a guest", "my own rsvp"
]

def wants_personal_rsvp(text):
    lower = text.lower()
    return any(k in lower for k in PERSONAL_RSVP_KEYWORDS)

ADD_GUEST_KEYWORDS = ["adicionar", "adiciona", "add guest", "add to the list", "add to list",
                       "colocar na lista", "incluir na lista", "esquecemos", "we forgot"]
CHECK_GUEST_KEYWORDS = ["está na lista", "esta na lista", "tá na lista", "ta na lista",
                         "is on the list", "is she on", "is he on", "procurar convidado"]
RSVP_OTHER_KEYWORDS = ["rsvp"]
RESET_KEYWORDS = ["[reset]", "resetar tudo", "reset everything", "apagar tudo teste"]

import re as _re

def extract_name_after_keyword(text, keywords):
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw)
        if idx != -1:
            remainder = text[idx + len(kw):]
            remainder = _re.sub(r'^[\s:,-]+', '', remainder)
            remainder = _re.sub(r'[.?!]+$', '', remainder).strip()
            if remainder:
                return remainder
    return None

def extract_capitalized_name(text):
    match = _re.search(r'\b([A-ZÀ-Ú][a-zà-ú\'\-]+(?:\s+[A-ZÀ-Ú][a-zà-ú\'\-]+){1,3})\b', text)
    return match.group(1).strip() if match else None

ADMIN_QUERY_KEYWORDS = [
    # stats / reports
    "quantas confirmações", "quantos rsvp", "quem confirmou", "quem não confirmou",
    "lista de convidados", "guest list", "who rsvped", "how many confirmed",
    "status do casamento", "wedding status", "relatório", "report", "resumo",
    "quem está na lista", "who is on the list",
    # broadcast
    "[all]", "[bridal]",
    # reset
    "[reset]", "resetar tudo", "reset everything",
]

def is_admin_query(text):
    """Returns True only if the message is clearly an admin/management query,
    not a general wedding info question that Aurora can answer normally."""
    lower = text.lower()
    # Explicit admin keywords
    if any(k in lower for k in ADMIN_QUERY_KEYWORDS):
        return True
    # Guest list management
    if any(k in lower for k in ADD_GUEST_KEYWORDS):
        return True
    if any(k in lower for k in CHECK_GUEST_KEYWORDS):
        return True
    # RSVP on behalf of someone else (not personal)
    if any(k in lower for k in RSVP_OTHER_KEYWORDS) and not wants_personal_rsvp(text):
        return True
    return False

def get_admin_response(phone_number, user_message):
    norm = normalize_phone(phone_number)
    name = ADMIN_IDENTITY.get(norm, "Carlotta (wedding planner)")
    lower_msg = user_message.lower()

    # --- Reset everything (only useful before invitations go out) ---
    if any(k in lower_msg for k in RESET_KEYWORDS):
        conversations.clear(); admin_conversations.clear(); rsvp_data.clear()
        guest_flags.clear(); active_subject.clear(); pending_subject.clear()
        phone_registry.clear(); all_phones.clear()
        save_state()
        return "🔄 Tudo resetado! Conversas, RSVPs e dados de teste foram apagados. Pronto para recomeçar."

    # --- Add a new guest (Larissa and Robert only) ---
    if any(k in lower_msg for k in ADD_GUEST_KEYWORDS):
        if norm not in ("353833986529", "19292277546"):
            return "Só a Larissa ou o Robert podem adicionar convidados à lista. 😊"
        candidate = extract_name_after_keyword(user_message, ADD_GUEST_KEYWORDS) or extract_capitalized_name(user_message)
        if candidate:
            existing = find_known_guest(candidate)
            if existing:
                return f"'{existing}' já está na lista! Não precisa adicionar de novo. 😊"
            add_guest_to_sheet(candidate, added_by=name)
            return f"✅ Adicionei *{candidate}* à lista de convidados e na planilha! Já pode confirmar presença dele(a) quando quiser."
        return "Qual é o nome completo da pessoa que você quer adicionar? 😊"

    # --- Check if someone is on the list ---
    if any(k in lower_msg for k in CHECK_GUEST_KEYWORDS):
        candidate = extract_capitalized_name(user_message)
        if candidate:
            found = find_known_guest(candidate)
            if found:
                return f"Sim! *{found}* está na lista de convidados. ✅"
            return f"Não encontrei *{candidate}* na lista. Quer que eu adicione? É só dizer 'adicionar {candidate}'."
        return "Qual é o nome que você quer verificar?"

    # --- RSVP on behalf of another guest ---
    if any(k in lower_msg for k in RSVP_OTHER_KEYWORDS) and not wants_personal_rsvp(user_message):
        candidate = extract_capitalized_name(user_message)
        if candidate:
            target = find_known_guest(candidate) or candidate
            active_subject[phone_number] = target
            conversations[phone_number] = [
                {"role": "user", "content": f"[sistema: RSVP sendo feito por {name} em nome de {target}, já identificado, não precisa perguntar o nome]"},
                {"role": "assistant", "content": f"Perfeito! Vamos registrar a presença de *{target}*! 💕 Só para confirmar — é a grafia certa do nome?"}
            ]
            return get_aurora_response(phone_number, user_message)

    # --- Personal RSVP for the admin ---
    if wants_personal_rsvp(user_message):
        active_subject[phone_number] = name
        phone_registry.setdefault(phone_number, name)
        if not conversations.get(phone_number):
            conversations[phone_number] = [
                {"role": "user", "content": f"[sistema: esta conversa é com {name}, já identificado, não precisa perguntar o nome]"},
                {"role": "assistant", "content": f"Perfeito! Vamos lá então! 💕 Só para confirmar — você é **{name}** da nossa lista, certo?"}
            ]
        return get_aurora_response(phone_number, user_message)

    # --- Admin stats/analytics queries ---
    if any(k in lower_msg for k in ADMIN_QUERY_KEYWORDS):
        if phone_number not in admin_conversations:
            admin_conversations[phone_number] = []
        history = admin_conversations[phone_number]
        stats = get_admin_stats()
        context = f"[{name} está consultando. Dados atuais: {json.dumps(stats)}]\n\n{user_message}"
        messages = history + [{"role": "user", "content": context}]
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
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

    # --- DEFAULT: treat admin as a normal guest for all other questions ---
    # (flights, money, Rome tips, dress code, hotels, etc.)
    # Seed the conversation with the admin's identity if not already set
    if not conversations.get(phone_number):
        phone_registry[phone_number] = name
        conversations[phone_number] = [
            {"role": "user", "content": f"[sistema: esta pessoa é {name}, já identificada na lista, responda normalmente como qualquer convidado]"},
            {"role": "assistant", "content": f"Oi {name.split()[0]}! 💕 Como posso te ajudar?"}
        ]
    elif phone_number not in phone_registry:
        phone_registry[phone_number] = name
    return get_aurora_response(phone_number, user_message)

def send_whatsapp_message(to_number, message, from_number):
    message = sanitize_for_whatsapp(message)
    chunks = []
    while len(message) > 1500:
        split_at = message.rfind(' ', 0, 1500)
        if split_at == -1:
            split_at = 1500
        chunks.append(message[:split_at])
        message = message[split_at:].strip()
    chunks.append(message)
    for chunk in chunks:
        twilio_client.messages.create(from_=from_number, to=to_number, body=chunk)

def handle_broadcast(message_body, from_number, to_number):
    upper = message_body.upper()
    if upper.startswith("[ALL]"):
        msg = message_body[5:].strip()
        sent = 0
        for phone in list(all_phones):
            if is_admin_phone(phone): continue
            try:
                send_zapi_message(phone, f"📢 *Atualização do Casamento*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} convidados!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        sent = 0
        for phone in list(bridal_party_phones):
            if is_admin_phone(phone): continue
            try:
                send_zapi_message(phone, f"💐 *Mensagem do Cortejo*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} pessoas do cortejo!"
    return None

@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    incoming_message = request.form.get('Body', '').strip()
    from_number = request.form.get('From', '')
    to_number = request.form.get('To', '')
    if not incoming_message or not from_number:
        return Response('', status=200)
    phone_key = from_number.replace('whatsapp:', '')
    all_phones.add(phone_key)
    try:
        upper_msg = incoming_message.upper()
        if is_admin_phone(phone_key) and (upper_msg.startswith("[ALL]") or upper_msg.startswith("[BRIDAL]")):
            reply = handle_broadcast(incoming_message, from_number, to_number)
            if reply:
                send_whatsapp_message(from_number, reply, to_number)
                return Response('', status=200)
        if is_admin_phone(phone_key):
            reply = get_admin_response(phone_key, incoming_message)
        else:
            reply = get_aurora_response(phone_key, incoming_message)
        send_whatsapp_message(from_number, reply, to_number)
    except Exception as e:
        import sys
        print(f"WHATSAPP ERROR: {str(e)}", file=sys.stderr)
        try:
            send_whatsapp_message(from_number, "Olá! Estou com dificuldade técnica. Fale com a Larissa: https://wa.me/353833986529 💍", to_number)
        except: pass
    return Response('', status=200)

@app.route('/zapi', methods=['POST'])
def zapi_webhook():
    phone = None
    try:
        data = request.get_json(force=True) or {}
        import sys
        print(f"Z-API RAW: {json.dumps(data)}", file=sys.stderr)

        if data.get('fromMe', False):
            return Response('', status=200)

        msg_id = data.get('messageId', '') or data.get('id', '') or data.get('msgId', '')
        if msg_id and msg_id in processed_message_ids:
            print(f"Z-API: duplicate message {msg_id} — ignoring", file=sys.stderr)
            return Response('', status=200)
        if msg_id:
            processed_message_ids.add(msg_id)
            if len(processed_message_ids) > 1000:
                processed_message_ids.clear()

        text = ''
        if isinstance(data.get('text'), dict):
            text = data['text'].get('message', '')
        elif isinstance(data.get('text'), str):
            text = data['text']
        if not text:
            text = data.get('message', '') or data.get('body', '')
        if not text:
            if data.get('audio') or data.get('type', '') in ['AudioMessage', 'PTTMessage', 'audio']:
                text = '[áudio]'
            else:
                print(f"Z-API: sem texto", file=sys.stderr)
                return Response('', status=200)

        phone = str(data.get('phone', '') or data.get('from', '') or data.get('senderPhone', ''))
        phone = phone.replace('@s.whatsapp.net', '').replace('whatsapp:', '').strip()
        if not phone:
            return Response('', status=200)

        print(f"Z-API: phone={phone} text={text}", file=sys.stderr)

        if phone in processing:
            print(f"Z-API: phone {phone} already processing — skipping", file=sys.stderr)
            return Response('', status=200)

        now = datetime.datetime.utcnow().timestamp()
        last_time = last_processed_time.get(phone, 0)
        if now - last_time < 3:
            print(f"Z-API: phone {phone} in cooldown ({now - last_time:.1f}s) — skipping", file=sys.stderr)
            return Response('', status=200)

        processing.add(phone)
        last_processed_time[phone] = now
        all_phones.add(phone)

        upper_msg = text.upper()
        if is_admin_phone(phone) and (upper_msg.startswith('[ALL]') or upper_msg.startswith('[BRIDAL]')):
            reply = handle_broadcast_zapi(text, phone)
        elif is_admin_phone(phone):
            reply = get_admin_response(phone, text)
        else:
            reply = get_aurora_response(phone, text)

        send_zapi_message(phone, reply)

    except Exception as e:
        import sys
        print(f"Z-API ERROR: {str(e)}", file=sys.stderr)
    finally:
        if phone:
            processing.discard(phone)
    return Response('', status=200)

def send_zapi_message(phone, message):
    instance_id = os.environ.get("ZAPI_INSTANCE_ID", "")
    token = os.environ.get("ZAPI_TOKEN", "")
    client_token = os.environ.get("ZAPI_CLIENT_TOKEN", "")
    if not instance_id or not token:
        import sys
        print("Z-API: sem credenciais", file=sys.stderr)
        return
    message = sanitize_for_whatsapp(message)
    chunks = []
    while len(message) > 4000:
        split_at = message.rfind(' ', 0, 4000)
        if split_at == -1:
            split_at = 4000
        chunks.append(message[:split_at])
        message = message[split_at:].strip()
    chunks.append(message)
    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
    for chunk in chunks:
        try:
            payload = json.dumps({"phone": phone, "message": chunk}).encode()
            headers = {"Content-Type": "application/json"}
            if client_token:
                headers["Client-Token"] = client_token
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            result = urllib.request.urlopen(req, timeout=10)
            import sys
            print(f"Z-API SENT: phone={phone} status={result.status}", file=sys.stderr)
        except Exception as e:
            import sys
            print(f"Z-API SEND ERROR: phone={phone} error={str(e)}", file=sys.stderr)

def handle_broadcast_zapi(message_body, from_phone):
    upper = message_body.upper()
    if upper.startswith("[ALL]"):
        msg = message_body[5:].strip()
        sent = 0
        for phone in list(all_phones):
            if is_admin_phone(phone): continue
            try:
                send_zapi_message(phone, f"📢 *Atualização do Casamento*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} convidados!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        sent = 0
        for phone in list(bridal_party_phones):
            if is_admin_phone(phone): continue
            try:
                send_zapi_message(phone, f"💐 *Mensagem do Cortejo*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} pessoas do cortejo!"
    return ""

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'Aurora is live 💍', 'conversations': len(all_phones), 'rsvps': len(rsvp_data)}, 200

@app.route('/', methods=['GET'])
def home():
    return {'message': 'Aurora Wedding Concierge — Larissa & Robert, Rome 2027'}, 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
