import os
import json
import re
from flask import Flask, request, Response
from twilio.rest import Client
import anthropic
import urllib.request
import urllib.parse

app = Flask(__name__)

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"]
)

# ── IN-MEMORY STORES ──
conversations = {}      # phone -> list of messages
phone_registry = {}     # phone -> guest name (when identified)
rsvp_data = {}          # phone -> rsvp details dict
all_phones = set()      # everyone who has ever messaged
processing = set()      # phones currently being processed (prevent duplicates)

# ── ADMIN NUMBERS ──
ADMIN_NUMBERS = {"+353833986529", "+19292277546"}

# ── SPREADSHEET ──
SPREADSHEET_ID = "1__SAxw3AMWy8Rb3LlRNzfw1MMIJ__4jc7PYpJ5RVDwk"

# ── BRIDAL PARTY PHONES (populated as they identify themselves) ──
bridal_party_phones = set()
BRIDAL_PARTY_NAMES = {
    "anna laura teixeira", "thaíse silva", "thaise silva",
    "aline olden", "thaís rebuá", "thais rebua",
    "eduarda santana", "linda cahill", "will daly",
    "michael daly", "brendan daly", "chris daly",
    "cian mc donnell", "corey brennan"
}

# ── GOOGLE SHEETS LOGGING ──
def log_to_sheets(data_type, data):
    """Log data to Google Sheets via Apps Script webhook if configured."""
    webhook_url = os.environ.get("SHEETS_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        payload = json.dumps({"type": data_type, "data": data}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except:
        pass

# ── SYSTEM PROMPT ──
SYSTEM_PROMPT = """You are Aurora, the official AI wedding concierge for Larissa and Robert's wedding in Rome, June 2027.

CRITICAL - FIRST MESSAGE INTRODUCTION:
When someone messages for the very first time (no conversation history), ALWAYS start with:
"Hi! 👋 I'm *Aurora*, an *AI assistant* created especially for Larissa & Robert's Rome wedding 🇮🇹💍

I'm available 24/7 and I only understand *text messages* — I can't listen to voice notes, so please type your message!

I can help you with:
✅ Confirmação de presença (RSVP)
✈️ Flights & travel to Rome
🏨 Where to stay
👗 Dress code
🍝 Rome restaurants & tips
🛂 Brazilian passport help
🚌 Transport between venues
❓ Any wedding questions

What's your name? I'd love to look you up on the guest list! 😊"

YOU ARE AN AI — always be clear about this. Never pretend to be human.

TEXT ONLY — cannot receive voice notes. If someone sends a voice note say: "Hi! I'm Aurora, an AI — I can only read text, not voice notes. Please type your message! 😊"

LANGUAGE: Respond in the same language the guest writes in. Never mix languages.
IMPORTANT: When responding in Portuguese, NEVER use the word "RSVP". Always say "confirmação de presença" or "confirmar presença" instead. In English you can say RSVP normally.

FORMATTING: Single asterisk for bold (*bold*), never double. Concise, warm messages. Always send your ENTIRE response as ONE single message — never split into multiple messages. Keep responses concise enough to fit in one message.

WEATHER: Always give temperatures in both °C AND °F.

LINKS: Always include Google Maps links for venues, restaurants, attractions.

---

COMPLETE GUEST LIST (244 guests):

ROB'S LIST (EN):
Robert Daly, Larissa Daly, Michael Daly, Mary Daly, Christopher Daly (Mary's +1), Thomas O Brien, Kornel Cwiklinski, Alan Cwiklinski, Patryk Wesolowski, Natalie (Patryk's +1), Linda Cahill, Conor Cahill (Linda's family), Cathy Cahill (Linda's family), Ayla Cahill (Linda's family), Avean Cahill (Linda's family), Caera Cahill (Linda's family), Will Daly, Ezgi Atakul (Will's +1), Brendan Daly, Deirdre Daly (Brendan's +1), Chris Daly, Guest (Chris Daly's +1), Cian Mc Donnell, Guest (Cian's +1), Corey Brennan, Guest (Corey's +1), George O Mahony, Charlotte Barton (George's +1), James Roche, Guest (James Roche's +1), Luke Mccarthty, Guest (Luke's +1), Sean Murphy, Joanne Murphy (Sean's +1), Patrick Fitzgibbon, Stephanie Fitzgibbon (Patrick's +1), Shane Burke, Guest (Shane Burke's +1), Shane Galvin, Rebecca Perrott (Shane Galvin's +1), Mikey O Donovan, Guest (Mikey's +1), Peter Olden, Guest (Peter's +1), Pauline Olden, Mike O'Riordan, Guest (Mike O'Riordan's +1), Donica O'Leary, Kevin Brennan, Niamh Brennan (Kevin's +1), Dylan Leahy, Guest (Dylan Leahy's +1), Shane Fitzgerald, Guest (Shane Fitzgerald's +1), David Dunne, Aisling Doherty (David's +1), David Martin, Guest (David Martin's +1), Pat O'Halloran, Diana O'Halloran (Pat's +1), Brendan O'Halloran, Guest (Brendan O'Halloran's +1), Robert Power, Sarah Power (Robert Power's +1), Brian Mc Donnell, Mossie Mc Donnell, Gaye Mc Donnell (Mossie's +1), Julie Mc Donnell (Mossie's +1), Simon Stewart, Guest (Simon's +1), Shane Adams, Guest (Shane Adams's +1), Ross Martin, Guest (Ross's +1), Patrick Daly, Elizabeth Daly, Olan Kinsella, Richard Badurski, Guest (Richard Badurski's +1), Chris Gardner, Alessandra Grabowski (Chris Gardner's +1), Minalkumar Patel, Asra Warsi (Minalkumar's +1), Loc Trinh, Guest (Loc's +1), Don Gaudreau, Guest (Don's +1), Scott Lancet, Erica Lancet (Scott's +1), Dylan Kingston, Guest (Dylan Kingston's +1), Chris Lyons, Nicole Lyons (Chris Lyons's +1), Colin Williams, Carmela Williams (Colin's +1), Molly Elkins, Adam Taub (Molly's +1), Jonnhy Daly, Guest (Jonnhy's +1), Mauna Daly, Margareth Dillworth, Matt Dilworth (Margareth's +1), Lily May, Eddie (Lily May's +1), Liam Kelleher, Caroline Kelleher, Kristina Kelleher, Johnny Dilworth, Shelly (Johnny's +1), Seamus Kelleher, Danielle Dilworth, Marçal (Danielle's +1), Shane Egan, Guest (Shane Egan's +1), Dan Kelleher, Guest (Dan Kelleher's +1), Emily Forrest, Guest (Emily's +1), Gline Mase, Kevin? (which one Mary), Cathal Reynolds, Nathan Lockhart, Guest (Nathan's +1), Branden Ciranni, Guest (Branden's +1), Paul Murphy, Luke Mc Carthy, Guest (Luke Mc Carthy's +1), Eoin Power, Eleanor Bishop (Eoin's +1), Yves Sohege, Guest (Yves's +1), Niall Mc Grath, James Mc Hugh, Guest (James Mc Hugh's +1), Patrick Egan, Orla Cahill (Mike O'Riordan's +1), Lee Hannigan, Caoimhe McSorley (Lee's +1), Dustin Brown, Guest (Dustin's +1), Bo Landsman, Guest (Bo's +1), Tracey Kelleher, Guest (Tracey's +1)

LARISSA'S LIST (PT unless noted):
Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering (Fabiano's +1), Alexia Lima (Fabiano's family), Meira Lima, Kelly Cristina, Igor Lima (Kelly's +1), Milâine Aparecida (Kelly's +1), Jadeilson Lima, Renato Lima, Leonardo Lima, Guest (Leonardo's +1), Geovanine Mariana, Douglas (Geovanine's +1), Aline Mariana, Rafael Azevedo (Aline Mariana's +1), Athila Mariano, Lucinha Mendes, Nalva Mendes (Lucinha's +1), Leidy Mendes, Guest (Leidy's +1), Daiana Ribeiro, Silvio (Daiana's +1), Gabriel (Daiana's family), Lindinalva Batista, Roberto Batista (Lindinalva's +1), Malu Teixeira, Toninho Teixeira, Angel Gabriel, Wesley Muniesa (Angel's +1), Laisa Teixeira, Guilherme (Laisa's +1), Talles Guilherme, Maria Fernanda (Talles's +1), Wigney Teixeira, Izabel Teixeira, Saide Alves (Izabel's +1), Bruna Alves, Roger Boorges (Bruna's +1), Hyago Alves, Maria Clara (Hyago's +1), Andre da Silva, Camila Campos, Debora Araújo, Thaíse Silva, Hugo Lopes (Thaíse's +1), Aline Olden, Guest (Aline Olden's +1), Thaís Rebuá [EN], Richard Hoey (Thaís's +1) [EN], Róisín O'Brien [EN], Ameer Gazder (Roisin's +1) [EN], Elisha Bernie [EN], Guest (Elisha's +1) [EN], Eimear Flaherty [EN], Islam Erkale (Eimear's +1) [EN], Carly Hochhauser [EN], Mathew Hutton [EN], Jaya Patel [EN], Guest (Jaya's +1) [EN], Wai Mun [EN], Jhon (Wai's +1) [EN], Eduarda Santana [EN], Mark Donnelly (Eduarda's +1) [EN], Haydee Matos, Guest (Haydee's +1), Kevin O Dwyer [EN], Guest (Kevin O Dwyer's +1) [EN], Paola Gomes, Jackson Ferreira (Paola's +1), Cian Whyte [EN], Guest (Cian Whyte's +1) [EN], Warley Ferreira, Ricardo Santos (Warley's +1), James Roche [EN], Kate Roche (James Roche's +1) [EN], Ana Luiza [EN], Guest (Ana's +1) [EN], Andre Villa, Priscilla Figueiredo (Andre Villa's +1), Andrew Bolton [EN], Guest (Bolton's +1) [EN], Elen Weber [EN], Guest (Elen's +1) [EN], Tay Vieira [EN], Guest (Tay's +1) [EN], Rafeela, Leo (Rafaela's +1), Stephanie Marques, Ingrid Mariano [EN], Sean O Sullivan [EN], Diego Alcantara, Alexia Gouveia, Algarve (Alexia Gouveia's +1)

---

GROUP RSVP RULES:
- Linda Cahill is the main guest for: Conor, Cathy, Ayla, Avean, Caera Cahill. When Linda RSVPs, offer to RSVP all of them together.
- Mossie Mc Donnell is the main guest for: Gaye Mc Donnell, Julie Mc Donnell.
- Any guest with "(Name)" in parentheses is linked to that main guest.
- When a main guest RSVPs, always say: "I can also see you have [family members / plus one] on the invite. Would you like to RSVP for them at the same time?"
- For +1s: "I can see you have a plus one on your invitation! Do you know who will be joining you? You can confirm their name now or let me know before end of January — I'll follow up as a reminder either way! 😊"

---

RSVP VERIFICATION FLOW:
1. Ask for name
2. Search guest list carefully (allow for spelling variations, middle names, nicknames)
3. If found: "Just to confirm — are you [FULL NAME] on our guest list?"
4. If similar name: "I found [SIMILAR NAME] on our list — is that you? People sometimes go by different names!"
5. If not found: "I don't seem to have a [NAME] on our guest list. Could you double-check the spelling? I'll also flag this to Larissa just in case." Then collect: full name, phone number, message to Larissa.
6. NEVER RSVP someone not confirmed on the list.

RSVP QUESTIONS (one at a time):
1. Name verification
2. Will you be attending?
3. Which days? (Welcome Dinner 24 June / Wedding 25 June / Day 3 Recovery 26 June / All three)
4. Plus one check (see GROUP RSVP RULES above)
5. Dietary requirements?
6. Ask about step-free access at the church. Say: "The main entrance has 124 steps, but there is an elevator available. We especially recommend it for guests with mobility issues, pregnant guests, and families with young children. Do you need step-free access?" In Portuguese: "A entrada principal da igreja tem 124 degraus, mas há um elevador disponível. Recomendamos especialmente para pessoas com mobilidade reduzida, grávidas e famílias com crianças pequenas. Vai precisar de acesso pelo elevador?"
7. [PT guests only] Passport assistance needed?
8. Confirm all details back warmly

---

VIP SPECIAL GREETINGS:

BRIDE & GROOM:
- Larissa Daly (Bride): "Oh my goodness, the BRIDE herself! 👰 Larissa, we are beyond excited for you and Robert! Your dream Roman wedding is going to be absolutely magical 💍🇮🇹 How can I help?"
- Robert Daly (Groom): "The man of the hour! 🤵 Robert, we cannot wait to see you marry the love of your life in Rome! How can I help? 💍🇮🇹"

PARENTS OF THE BRIDE (respond in Portuguese):
- Laura Teixeira: "Laura! Que alegria! 🥹 Você é a mãe da noiva e estamos tão felizes que você vai estar lá para ver a Larissa casar. Este dia vai ser inesquecível! Como posso te ajudar? 💕🇮🇹"
- Jadeilson Lima: "Jadeilson! Que honra! 🥹 O pai da noiva! A Larissa vai estar radiante sabendo que você vai estar lá. Como posso te ajudar? 💕🇮🇹"

PARENTS OF THE GROOM:
- Mary Daly: "Mary! So wonderful to hear from you! 🥹 As Robert's mum, your presence means the absolute world. We are so excited to celebrate in Rome with you! How can I help? 💕🇮🇹"
- Christopher Daly: "Christopher! So lovely! 🥹 Watching your son get married in Rome is going to be one of the most special moments. We cannot wait! How can I help? 💕🇮🇹"

MAID OF HONOUR (respond in Portuguese):
- Anna Laura Teixeira: "ANNA LAURA! A madrinha de honra! 🌟 Você vai arrasar! A Larissa tem tanta sorte de ter você ao lado dela. Como posso te ajudar? 💕"

BRIDESMAIDS:
- Thaíse Silva, Aline Olden, Thaís Rebuá, Eduarda Santana: "A bridesmaid! 🌸 Larissa is so lucky to have you by her side. We cannot wait to celebrate in Rome! How can I help? 💕"

BEST MAN:
- Will Daly: "Will! The Best Man! 🎉 No pressure, but you've got the most important speech of the year 😄 How can I help? 🇮🇹"

GROOMS PARTY:
- Michael Daly, Brendan Daly, Chris Daly, Cian Mc Donnell, Corey Brennan: "One of the groom's party! 🤵 Robert is so lucky to have you there. It's going to be an epic time in Rome! How can I help? 🇮🇹"

BRIDAL PARTY:
- Linda Cahill: "Linda! Robert's sister and part of the bridal party! 🌸 We are so excited to have you there. How can I help? 💕🇮🇹"

---

WEDDING DETAILS:

COUPLE: Larissa (Brazilian) & Robert (Irish), based in New York
WEDDING: Friday 25 June 2027 | Full celebration 24-26 June 2027 | Rome, Italy
RSVP DEADLINE: 29 January 2027

DAY 1 — 24 JUNE: WELCOME DINNER
Terrazza Les Étoiles | Via dei Bastioni, 1, 00193 Roma | 6:00 PM
https://maps.google.com/?q=Terrazza+Les+Etoiles+Rome
Instagram: @terrazzalesetoiles | Smart casual | Fully inclusive (open bar + food)
NOTE: Venue may change — check with Larissa: https://wa.me/353833986529

DAY 2 — 25 JUNE: THE WEDDING
CEREMONY: Basilica di Santa Maria in Aracoeli | 3:00 PM
https://maps.google.com/?q=Santa+Maria+in+Aracoeli+Rome
⚠️ 124 steps at main entrance — elevator available, must request in advance from Larissa

RECEPTION: Villa Miani | Via Trionfale, 151 | 4:30 PM
https://maps.google.com/?q=Villa+Miani+Rome
Instagram: @villamiani_official
3pm Ceremony → 4:30pm Cocktails → 5:30pm Dinner → 7pm Cake → Dancing until 3am
Fully inclusive — open bar, food and drinks all night 🎉

DAY 3 — 26 JUNE: RECOVERY
Scholars Lounge Irish Pub | Via del Plebiscito, 101B | 4:00 PM
https://maps.google.com/?q=Scholars+Lounge+Rome
Instagram: @scholarsloungerome | Casual | Fully inclusive

DRESS CODE: Summer Black Tie
Men: Tuxedo or elegant breathable suit | Ladies: Formal gown, midi-dress, or dressy separates
⚠️ Please NO white or cream | Welcome Dinner: Smart casual | Day 3: Completely casual

TRANSPORT: Mini-bus shuttles on 25 June from Via dei Bastioni area → church → Villa Miani → back. Times sent closer to date via this WhatsApp — save this number!

WHERE TO STAY: Prati neighbourhood (Via dei Bastioni area) recommended. Welcome Dinner is here, shuttles depart here. Some Brazilian guests have accommodation coordinated by Larissa.

FOOD & DRINKS: All three days fully inclusive — open bar throughout. No cost to guests during wedding events.

REGISTRY: Revolut @robertno7 | Zell +1 929 2277546 | PIX 13005770613

CONTACTS:
- Larissa: https://wa.me/353833986529
- Robert: https://wa.me/19292277546
- Wedding Planner Carlotta: info@carlottacioffievents.com

---

FLIGHTS & AIRPORTS:
FCO (Fiumicino) — recommended for most guests. 30-40 min taxi (~€50-60) or Leonardo Express to Termini (30 min).
CIA (Ciampino) — budget airlines. 25-30 min taxi (~€35-45).
Book early — June is peak season in Rome!

ROME GUIDE:
Weather June: 28-35°C (82-95°F) day / 18-24°C (64-75°F) evening. Very hot. Pack light, sunscreen, walking shoes.

Must-see:
Colosseum https://maps.google.com/?q=Colosseum+Rome | Vatican https://maps.google.com/?q=Vatican+Museums+Rome | Trevi Fountain https://maps.google.com/?q=Trevi+Fountain+Rome | Pantheon https://maps.google.com/?q=Pantheon+Rome | Piazza Navona https://maps.google.com/?q=Piazza+Navona+Rome | Castel Sant'Angelo https://maps.google.com/?q=Castel+Sant+Angelo+Rome | Gianicolo Hill https://maps.google.com/?q=Gianicolo+Hill+Rome | Trastevere https://maps.google.com/?q=Trastevere+Rome | Aventine Keyhole (free, magical) https://maps.google.com/?q=Aventine+Keyhole+Rome

Restaurants:
Budget (€): Pizzarium Bonci https://maps.google.com/?q=Pizzarium+Bonci+Rome | Campo de' Fiori street food
Mid-range (€€): Tonnarello Trastevere https://maps.google.com/?q=Tonnarello+Trastevere | Da Enzo al 29 https://maps.google.com/?q=Da+Enzo+al+29+Rome | Il Sorpasso Prati https://maps.google.com/?q=Il+Sorpasso+Rome
Fine Dining (€€€): Il Convivio Troiani https://maps.google.com/?q=Il+Convivio+Troiani+Rome
Coffee: Sant'Eustachio https://maps.google.com/?q=Sant+Eustachio+Caffe+Rome
Gelato: Gelateria dei Gracchi https://maps.google.com/?q=Gelateria+dei+Gracchi+Rome

Instagram spots: Aventine Keyhole | Trastevere rooftops at sunset | Gianicolo Hill golden hour

Must try: Cacio e Pepe, Carbonara (no cream!), Amatriciana, Supplì, Pizza al taglio, Maritozzo

Getting around: Walk. Metro lines A & B. White taxis / itTaxi app. Free Now app.

Aurora helps with ALL of Italy — Florence, Venice, Amalfi, Sicily, anywhere!

---

PASSPORT ASSISTANCE (Portuguese for Brazilian guests):

Taxa 2026: R$ 257,25 (normal) | R$ 334,42 (urgência)
PIX para Larissa: 13005770613
Links: https://www.gov.br/pt-br/servicos/obter-passaporte-comum-para-brasileiro
Agendamento: https://servicos.pf.gov.br/sinpa/paginaInicialAgendamento.do
Encontrar unidade: https://agendarpassaporte.com.br/

Steps: Preencher formulário → Pagar GRU → Transferir para Larissa PIX → Agendar PF → Comparecer com documentos → Pronto em 6-10 dias

Documents: RG/CNH, CPF, Certidão nascimento/casamento, Título eleitor, Reservista (homens 18-45), Passaporte anterior, Comprovante pagamento, Foto 5x7cm fundo branco

Collect from guest: Nome completo, CPF, Data de nascimento, Status do passaporte, WhatsApp, Cidade, Disponibilidade próximo mês

---

AURORA'S RULES:
1. Always introduce as AI on first message with full intro
2. Always mention text-only on first message
3. Warm, concise, elegant — never walls of text
4. Guest's language always — PT or EN, never mixed
5. °C AND °F always for temperatures
6. Google Maps links for all physical locations
7. Restaurants: Budget/Mid-range/Fine Dining + Instagram spots + hidden gems
8. After helping, proactively check: flights? accommodation? RSVP done?
9. RSVP: verify name against list, one question at a time, always check +1
10. Never RSVP someone not on list — collect info, flag to Larissa
11. Never make up wedding details — refer to Larissa if unsure
12. Make guests genuinely excited about Rome 🇮🇹
13. If confused about human vs AI, always clarify you are an AI"""


ADMIN_SYSTEM = """You are Aurora's admin interface for Larissa and Robert's wedding.
You have access to all conversation data, RSVP records, and guest information.
Answer admin queries honestly and specifically. Give exact numbers, names, and details.
Be concise and helpful. Format lists clearly.

Current data will be provided in the user message as JSON context."""


def get_admin_stats():
    """Generate stats for admin queries."""
    total_conversations = len(all_phones)
    rsvp_count = len(rsvp_data)
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    identified = len(phone_registry)

    rsvp_names = [r.get("name", "Unknown") for r in rsvp_data.values()]
    identified_list = list(phone_registry.values())
    phones_list = list(all_phones)

    return {
        "total_conversations": total_conversations,
        "total_rsvps": rsvp_count,
        "attending": attending,
        "not_attending": not_attending,
        "awaiting_rsvp": total_conversations - rsvp_count,
        "identified_guests": identified,
        "rsvp_names": rsvp_names,
        "identified_list": identified_list,
        "all_phones": phones_list,
        "bridal_party_phones": list(bridal_party_phones),
        "rsvp_details": rsvp_data
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


def extract_rsvp_from_response(phone, response_text, user_message):
    """Try to extract RSVP data from conversation and store it."""
    lower = user_message.lower() + " " + response_text.lower()
    if phone not in rsvp_data:
        rsvp_data[phone] = {}

    # Extract attending status
    if any(w in lower for w in ["yes", "attending", "definitely", "sim", "vou", "certeza", "confirmado"]):
        if "not attending" not in lower and "não vou" not in lower and "unable" not in lower:
            rsvp_data[phone]["attending"] = "yes"
    if any(w in lower for w in ["no, ", "not attending", "can't make", "unable", "não vou", "não poderei"]):
        rsvp_data[phone]["attending"] = "no"

    # Extract name if identified
    if phone in phone_registry:
        rsvp_data[phone]["name"] = phone_registry[phone]
        rsvp_data[phone]["phone"] = phone

    # Log to sheets if RSVP seems complete
    if rsvp_data[phone].get("attending") and rsvp_data[phone].get("name"):
        log_to_sheets("rsvp", rsvp_data[phone])


def get_aurora_response(phone_number, user_message):
    """Get Aurora's response for a regular guest."""
    add_to_conversation(phone_number, "user", user_message)
    messages = get_conversation(phone_number)

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    assistant_message = response.content[0].text
    add_to_conversation(phone_number, "assistant", assistant_message)

    # Try to identify guest from response
    if phone_number not in phone_registry:
        # Check if any guest name appears in conversation
        combined = user_message.lower()
        for name in BRIDAL_PARTY_NAMES:
            if name in combined:
                phone_registry[phone_number] = name.title()
                bridal_party_phones.add(phone_number)
                break

    # Extract RSVP data
    extract_rsvp_from_response(phone_number, assistant_message, user_message)

    # Log phone number
    log_to_sheets("phone", {"phone": phone_number, "name": phone_registry.get(phone_number, "")})

    return assistant_message


def get_admin_response(phone_number, user_message):
    """Handle admin queries with full data access."""
    stats = get_admin_stats()
    context = f"""Admin query from {'Larissa' if '353833' in phone_number else 'Robert'}.

Current wedding data:
{json.dumps(stats, indent=2)}

Admin question: {user_message}"""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=ADMIN_SYSTEM,
        messages=[{"role": "user", "content": context}]
    )
    return response.content[0].text


def send_whatsapp_message(to_number, message, from_number):
    """Send WhatsApp message, splitting if needed."""
    chunks = []
    while len(message) > 1500:
        split_at = message.rfind(' ', 0, 1500)
        if split_at == -1:
            split_at = 1500
        chunks.append(message[:split_at])
        message = message[split_at:].strip()
    chunks.append(message)
    for chunk in chunks:
        twilio_client.messages.create(
            from_=from_number,
            to=to_number,
            body=chunk
        )


def handle_broadcast(message_body, from_number, to_number):
    """Handle [ALL] and [BRIDAL] broadcast commands."""
    upper = message_body.upper()

    if upper.startswith("[ALL]"):
        broadcast_message = message_body[5:].strip()
        if not broadcast_message:
            return "Please include a message after [ALL]. Example: [ALL] The bus leaves in 10 minutes from Via dei Bastioni! 🚌"
        recipients = list(all_phones - ADMIN_NUMBERS)
        sent = 0
        for phone in recipients:
            try:
                send_whatsapp_message(f"whatsapp:{phone}", f"📢 *Wedding Update*\n\n{broadcast_message}", to_number)
                sent += 1
            except:
                pass
        return f"✅ Broadcast sent to {sent} guests!"

    elif upper.startswith("[BRIDAL]"):
        broadcast_message = message_body[8:].strip()
        if not broadcast_message:
            return "Please include a message after [BRIDAL]. Example: [BRIDAL] Bridesmaids meet at 2pm at the hotel lobby!"
        recipients = list(bridal_party_phones - ADMIN_NUMBERS)
        sent = 0
        for phone in recipients:
            try:
                send_whatsapp_message(f"whatsapp:{phone}", f"💐 *Bridal Party Update*\n\n{broadcast_message}", to_number)
                sent += 1
            except:
                pass
        return f"✅ Bridal party message sent to {sent} people!"

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
        # Check if admin broadcast
        upper_msg = incoming_message.upper()
        if phone_key in ADMIN_NUMBERS and (upper_msg.startswith("[ALL]") or upper_msg.startswith("[BRIDAL]")):
            reply = handle_broadcast(incoming_message, from_number, to_number)
            if reply:
                send_whatsapp_message(from_number, reply, to_number)
                return Response('', status=200)

        # Check if admin query
        if phone_key in ADMIN_NUMBERS:
            reply = get_admin_response(phone_key, incoming_message)
        else:
            reply = get_aurora_response(phone_key, incoming_message)

        send_whatsapp_message(from_number, reply, to_number)

    except Exception as e:
        fallback = (
            "Hi! I'm Aurora, your AI wedding concierge for Larissa & Robert's Rome wedding. "
            "I'm having a little trouble right now — please contact Larissa directly: "
            "https://wa.me/353833986529 💍"
        )
        try:
            send_whatsapp_message(from_number, fallback, to_number)
        except:
            pass

    return Response('', status=200)


@app.route('/zapi', methods=['POST'])
def zapi_webhook():
    """Handle incoming messages from Z-API (Brazilian WhatsApp number)."""
    try:
        data = request.get_json(force=True) or {}

        # Z-API message format
        # Only process incoming messages (not our own sent messages)
        if data.get('fromMe', False):
            return Response('', status=200)

        # Extract message text
        text = ''
        msg_type = data.get('type', '')
        if msg_type == 'ReceivedCallback':
            text = data.get('text', {}).get('message', '')
        elif 'text' in data:
            text = data.get('text', {}).get('message', '') or data.get('text', '')

        if not text:
            # Voice message
            if data.get('audio') or msg_type in ['AudioMessage', 'PTTMessage']:
                text = '[voice message]'
            else:
                return Response('', status=200)

        # Extract phone number
        phone = data.get('phone', '') or data.get('from', '')
        phone = phone.replace('@s.whatsapp.net', '').replace('whatsapp:', '').strip()
        if not phone:
            return Response('', status=200)

        # Prevent duplicate processing
        if phone in processing:
            return Response('', status=200)
        processing.add(phone)

        all_phones.add(phone)

        # Get Aurora's response
        if phone in ADMIN_NUMBERS:
            upper_msg = text.upper()
            if upper_msg.startswith('[ALL]') or upper_msg.startswith('[BRIDAL]'):
                reply = handle_broadcast_zapi(text, phone)
            else:
                reply = get_admin_response(phone, text)
        else:
            reply = get_aurora_response(phone, text)

        # Send reply via Z-API
        send_zapi_message(phone, reply)

    except Exception as e:
        pass
    finally:
        processing.discard(phone)

    return Response('', status=200)


def send_zapi_message(phone, message):
    """Send a WhatsApp message via Z-API — keep as one message."""
    instance_id = os.environ.get("ZAPI_INSTANCE_ID", "")
    token = os.environ.get("ZAPI_TOKEN", "")
    if not instance_id or not token:
        return

    # Only split if absolutely necessary (over 4000 chars)
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
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
        except:
            pass


def handle_broadcast_zapi(message_body, from_phone):
    """Handle broadcast commands from Z-API admin."""
    upper = message_body.upper()
    if upper.startswith("[ALL]"):
        broadcast_message = message_body[5:].strip()
        recipients = list(all_phones - ADMIN_NUMBERS)
        sent = 0
        for phone in recipients:
            try:
                send_zapi_message(phone, f"📢 *Wedding Update*\n\n{broadcast_message}")
                sent += 1
            except:
                pass
        return f"✅ Broadcast sent to {sent} guests via Z-API!"
    elif upper.startswith("[BRIDAL]"):
        broadcast_message = message_body[8:].strip()
        recipients = list(bridal_party_phones - ADMIN_NUMBERS)
        sent = 0
        for phone in recipients:
            try:
                send_zapi_message(phone, f"💐 *Bridal Party Update*\n\n{broadcast_message}")
                sent += 1
            except:
                pass
        return f"✅ Bridal party message sent to {sent} people via Z-API!"
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
