import os
import json
import threading
import datetime
import urllib.request
import urllib.parse
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

conversations = {}       # phone -> [ {role, content}, ... ]
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
    global conversations, phone_registry, rsvp_data, all_phones, guest_flags, active_subject, pending_subject
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            conversations = data.get("conversations", {})
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

BRAZIL_NAME_MARKERS = None  # placeholder, Brazilian guest list is matched via the guest list itself

def sanitize_for_whatsapp(text):
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
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
🛂 Passaporte (importante! me conta mais)
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
FORMATAÇÃO: Asterisco simples para negrito. UMA mensagem só, nunca divida.
TEMPERATURA: Sempre °C E °F.
LINKS: Google Maps para tudo.
NUNCA ENCERRE — sempre sugira próximo tópico.

RSVP PARA OUTRA PESSOA — REGRA CRÍTICA:
Quem está te mandando mensagem (o número de telefone) NÃO é necessariamente quem está sendo confirmado. Uma pessoa pode confirmar presença dela mesma E de outras pessoas na mesma conversa (ex: Larissa confirmando a própria presença e também a da Anna Laura).
SEMPRE deixe claro, a cada novo RSVP dentro da mesma conversa, para QUEM é aquele RSVP específico — nunca assuma que é a mesma pessoa do RSVP anterior nessa conversa.
Quando o nome mudar de convidado dentro da mesma conversa, trate como um RSVP totalmente novo e separado — não misture dados de uma pessoa com a outra.

LISTA DE CONVIDADOS (249 pessoas):

LISTA DO ROB (EN): Robert Daly, Larissa Daly, Michael Daly, Mary Daly, Christopher Daly (acompanhante de Mary), Thomas O Brien, Kornel Cwiklinski, Alan Cwiklinski, Patryk Wesolowski, Natalie (acompanhante de Patryk), Linda Cahill, Conor Cahill (família de Linda), Cathy Cahill (família de Linda), Ayla Cahill (família de Linda), Avean Cahill (família de Linda), Caera Cahill (família de Linda), Will Daly, Ezgi Atakul (acompanhante de Will), Brendan Daly, Deirdre Daly (acompanhante de Brendan), Chris Daly, Guest (acompanhante de Chris Daly), Cian Mc Donnell, Guest (acompanhante de Cian), Corey Brennan, Guest (acompanhante de Corey), George O Mahony, Charlotte Barton (acompanhante de George), James Roche, Guest (acompanhante de James Roche), Luke Mccarthty, Guest (acompanhante de Luke), Sean Murphy, Joanne Murphy (acompanhante de Sean), Patrick Fitzgibbon, Stephanie Fitzgibbon (acompanhante de Patrick), Shane Burke, Guest (acompanhante de Shane Burke), Shane Galvin, Rebecca Perrott (acompanhante de Shane Galvin), Mikey O Donovan, Guest (acompanhante de Mikey), Peter Olden, Guest (acompanhante de Peter), Pauline Olden, Mike O'Riordan, Guest (acompanhante de Mike O'Riordan), Donica O'Leary, Kevin Brennan, Niamh Brennan (acompanhante de Kevin), Dylan Leahy, Guest (acompanhante de Dylan Leahy), Shane Fitzgerald, Guest (acompanhante de Shane Fitzgerald), David Dunne, Aisling Doherty (acompanhante de David), David Martin, Guest (acompanhante de David Martin), Pat O'Halloran, Diana O'Halloran (acompanhante de Pat), Brendan O'Halloran, Guest (acompanhante de Brendan O'Halloran), Robert Power, Sarah Power (acompanhante de Robert Power), Brian Mc Donnell, Mossie Mc Donnell, Gaye Mc Donnell (acompanhante de Mossie), Julie Mc Donnell (acompanhante de Mossie), Simon Stewart, Guest (acompanhante de Simon), Shane Adams, Guest (acompanhante de Shane Adams), Ross Martin, Guest (acompanhante de Ross), Patrick Daly, Elizabeth Daly, Olan Kinsella, Richard Badurski, Guest (acompanhante de Richard Badurski), Chris Gardner, Alessandra Grabowski (acompanhante de Chris Gardner), Minalkumar Patel, Asra Warsi (acompanhante de Minalkumar), Loc Trinh, Guest (acompanhante de Loc), Don Gaudreau, Guest (acompanhante de Don), Scott Lancet, Erica Lancet (acompanhante de Scott), Dylan Kingston, Guest (acompanhante de Dylan Kingston), Chris Lyons, Nicole Lyons (acompanhante de Chris Lyons), Colin Williams, Carmela Williams (acompanhante de Colin), Molly Elkins, Adam Taub (acompanhante de Molly), Jonnhy Daly, Guest (acompanhante de Jonnhy), Mauna Daly, Margareth Dillworth, Matt Dilworth (acompanhante de Margareth), Lily May, Eddie (acompanhante de Lily May), Liam Kelleher, Caroline Kelleher, Kristina Kelleher, Johnny Dilworth, Shelly (acompanhante de Johnny), Seamus Kelleher, Danielle Dilworth, Marçal (acompanhante de Danielle), Shane Egan, Guest (acompanhante de Shane Egan), Dan Kelleher, Guest (acompanhante de Dan Kelleher), Emily Forrest, Guest (acompanhante de Emily), Gline Mase, Cathal Reynolds, Nathan Lockhart, Guest (acompanhante de Nathan), Branden Ciranni, Guest (acompanhante de Branden), Paul Murphy, Luke Mc Carthy, Guest (acompanhante de Luke Mc Carthy), Eoin Power, Eleanor Bishop (acompanhante de Eoin), Yves Sohege, Guest (acompanhante de Yves), Niall Mc Grath, James Mc Hugh, Guest (acompanhante de James Mc Hugh), Patrick Egan, Orla Cahill (acompanhante de Mike O'Riordan), Lee Hannigan, Caoimhe McSorley (acompanhante de Lee), Dustin Brown, Guest (acompanhante de Dustin), Bo Landsman, Guest (acompanhante de Bo), Tracey Kelleher, Guest (acompanhante de Tracey)

LISTA DA LARISSA (PT salvo indicação): Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering (acompanhante de Fabiano), Alexia Lima (família de Fabiano), Meira Lima, Kelly Cristina, Igor Lima (acompanhante de Kelly), Milâine Aparecida (acompanhante de Kelly), Jadeilson Lima, Renato Lima, Leonardo Lima, Guest (acompanhante de Leonardo), Geovanine Mariana, Douglas (acompanhante de Geovanine), Aline Mariana, Rafael Azevedo (acompanhante de Aline Mariana), Athila Mariano, Lucinha Mendes, Nalva Mendes (acompanhante de Lucinha), Leidy Mendes, Guest (acompanhante de Leidy), Daiana Ribeiro, Silvio (acompanhante de Daiana), Gabriel (família de Daiana), Lindinalva Batista, Roberto Batista (acompanhante de Lindinalva), Malu Teixeira, Toninho Teixeira, Angel Gabriel, Wesley Muniesa (acompanhante de Angel), Laisa Teixeira, Guilherme (acompanhante de Laisa), Talles Guilherme, Maria Fernanda (acompanhante de Talles), Wigney Teixeira, Izabel Teixeira, Saide Alves (acompanhante de Izabel), Bruna Alves, Roger Boorges (acompanhante de Bruna), Hyago Alves, Maria Clara (acompanhante de Hyago), Andre da Silva, Camila Campos, Debora Araújo, Thaíse Silva, Hugo Lopes (acompanhante de Thaíse), Aline Olden, Guest (acompanhante de Aline Olden), Thaís Rebuá [EN], Richard Hoey (acompanhante de Thaís) [EN], Róisín O'Brien [EN], Ameer Gazder (acompanhante de Roisin) [EN], Elisha Bernie [EN], Guest (acompanhante de Elisha) [EN], Eimear Flaherty [EN], Islam Erkale (acompanhante de Eimear) [EN], Carly Hochhauser [EN], Mathew Hutton [EN], Jaya Patel [EN], Guest (acompanhante de Jaya) [EN], Wai Mun [EN], Jhon (acompanhante de Wai) [EN], Eduarda Santana [EN], Mark Donnelly (acompanhante de Eduarda) [EN], Haydee Matos, Guest (acompanhante de Haydee), Kevin O Dwyer [EN], Guest (acompanhante de Kevin O Dwyer) [EN], Paola Gomes, Jackson Ferreira (acompanhante de Paola), Cian Whyte [EN], Guest (acompanhante de Cian Whyte) [EN], Warley Ferreira, Ricardo Santos (acompanhante de Warley), James Roche [EN], Kate Roche (acompanhante de James Roche) [EN], Ana Luiza [EN], Guest (acompanhante de Ana) [EN], Andre Villa, Priscilla Figueiredo (acompanhante de Andre Villa), Andrew Bolton [EN], Guest (acompanhante de Bolton) [EN], Elen Weber [EN], Guest (acompanhante de Elen) [EN], Tay Vieira [EN], Guest (acompanhante de Tay) [EN], Rafeela, Leo (acompanhante de Rafeela), Stephanie Marques, Ingrid Mariano [EN], Sean O Sullivan [EN], Diego Alcantara, Alexia Gouveia, Algarve (acompanhante de Alexia Gouveia)

CONVIDADOS COM HOSPEDAGEM INCLUSA: Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering, Alexia Lima, Meira Lima, Kelly Cristina, Igor Lima, Milâine Aparecida, Jadeilson Lima, Leonardo Lima, Angel Gabriel, Wesley Muniesa, Bruna Alves, Roger Boorges, Hyago Alves, Maria Clara, Andre da Silva, Camila Campos, Debora Araújo
Quando perguntarem: "Sua hospedagem já está inclusa! 🏨 Datas: 23 a 27 de junho de 2027. Para extensões, fale direto com o hotel."

RSVP EM GRUPO: Linda Cahill = principal de Conor, Cathy, Ayla, Avean, Caera Cahill. Mossie Mc Donnell = principal de Gaye e Julie. Ofereça confirmar todos juntos.

PERGUNTAS DE RSVP — REGRAS CRÍTICAS:
- NUNCA faça mais de UMA pergunta por mensagem. Isso é obrigatório.
- NUNCA repita uma pergunta que já foi feita na conversa.
- NUNCA recomece o fluxo do zero se já está no meio — continue de onde parou.
- Se a pessoa respondeu algo, registre e passe para a PRÓXIMA pergunta apenas.
- Ao perguntar sobre um dia específico, SEMPRE explique brevemente o que acontece naquele dia ANTES de perguntar se a pessoa vai. Nunca pergunte "vai no dia X?" sem dizer o que é o dia X.
- Ao perguntar sobre o elevador/acesso na igreja, SEMPRE inclua que é recomendado para quem tem mobilidade reduzida, grávidas, ou famílias com crianças pequenas — para que a pessoa responda pensando na própria situação real, e não só diga "não" por padrão.
- Se o convidado for brasileiro (está na LISTA DA LARISSA ou você identificar que é do Brasil), SEMPRE pergunte, em algum momento do RSVP, se precisa de ajuda com o passaporte — não espere ser perguntado.

ORDEM DO RSVP (uma pergunta por vez):
1. Verificação do nome → confirmar grafia
2. Vai comparecer?
3. Quais dias? (explique o que é cada dia antes de perguntar: Dia 1 Vinícola 24/06 / Dia 2 Casamento 25/06 / Dia 3 Pub 26/06 / Os três)
4. Acompanhante? (confirmar nome ou até janeiro)
5. Restrições alimentares?
6. Elevador na igreja? (124 degraus — SEMPRE mencionar que é recomendado para mobilidade reduzida, grávidas, famílias com crianças)
7. [Se brasileiro] Ajuda com passaporte?
8. Confirmar tudo em UMA mensagem acolhedora

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
Recepção: Villa Miani, Via Trionfale 151, 16h30 | https://maps.google.com/?q=Villa+Miani+Rome
15h→coquetéis 16h30→jantar 17h30→bolo 19h→festa até 3h. Tudo incluso.

DIA 3 — 26 JUNHO: PUB 🍺
Scholars Lounge, Via del Plebiscito 101B, 16h | https://maps.google.com/?q=Scholars+Lounge+Rome
Seção privada. Finger food + bebidas inclusos. Casual.

TRANSPORTE: Fornecido pelos noivos para os dias 1 e 2. Ponto de encontro a informar mais perto da data.

VESTIMENTA:
Dia 1: Smart casual, sapatos confortáveis
Dia 2: Black tie / Dress to impress. Homens: smoking (tuxedo) — vale alugar! Tecido leve. Mulheres: longo, midi elegante. SEM branco/creme.
Dia 3: Casual total.

HOTÉIS RECOMENDADOS:
Ainda estamos finalizando os acordos com os hotéis — essas são as opções recomendadas por enquanto, os detalhes finais (preços de grupo, café da manhã) virão em breve:
Hotel Hiberia ⭐⭐⭐⭐ €170-260/noite | https://www.hotelhiberia.it | 7min Aracoeli
Hotel Regno ⭐⭐⭐⭐ €180-300/noite | https://www.hotelregno.com | 8min Aracoeli
Hotel Castellino ⭐⭐⭐⭐ €160-250/noite | https://www.hotelcastellinoroma.it | 3min Aracoeli

VOOS:
Brasil: ITA Airways GRU→FCO nonstop, parte 22/06 14h15, chega 23/06 06h50
Shannon: Ryanair FR9805, terças, chega ~17h45 (junho 2027 ainda não à venda)
Dublin/Londres/EUA: múltiplas opções diárias

QUANTO LEVAR:
Eventos = tudo incluso! Para explorar Roma: €50-70/dia (econômico) | €100-150/dia (confortável)
Coliseu ~€18 | Vaticano ~€20 | Gelato €2-4 | Café €1,50

PASSAPORTE (BRASILEIROS):
Larissa organiza pessoalmente — agenda na PF perto de você.
Taxa: R$257,25 (comum) | R$334,42 (urgência) → PIX 13005770613
ETIAS: ainda não obrigatório para brasileiros mas pode ser exigido até 2027.
Links: https://www.gov.br/pt-br/servicos/obter-passaporte-comum-para-brasileiro | https://agendarpassaporte.com.br/
Docs: RG/CNH, CPF, certidão, título eleitor, reservista (H 18-45), passaporte anterior, comprovante, foto 5x7 fundo branco
Informações necessárias: nome, CPF, data nasc., status passaporte, WhatsApp, cidade, disponibilidade.

CRIANÇAS: Se na lista = OK. Se não na lista = alertar Larissa, aguardar resposta.
MADRINHAS/VESTIDOS: Larissa enviará o link do site com a cor escolhida.
PRESENTES: Podem entregar à Anna Laura Teixeira.
CONTATOS: Larissa https://wa.me/353833986529 | Robert https://wa.me/19292277546
REGISTRO: Revolut @robertno7 | Zell +1 929 2277546 | PIX 13005770613

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
11. Cada RSVP pertence a UMA pessoa específica — nunca misturar dados de convidados diferentes na mesma conversa"""

ADMIN_SYSTEM = """Você é a interface administrativa da Aurora para Larissa, Robert e Carlotta (cerimonialista).
Acesso completo a conversas, RSVPs e dados dos convidados.
Respostas honestas, específicas, concisas. Listas formatadas claramente."""

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
    """
    import re
    match = re.search(r"voc[eê] [eé]\s+\*\*(.+?)\*\*", assistant_text, re.IGNORECASE)
    if match:
        pending_subject[phone] = match.group(1).strip()
        return

    lower_user = user_message.lower().strip()
    affirmative = any(w in lower_user for w in ["sim", "yes", "isso", "correto", "certo", "exato"])
    if affirmative and phone in pending_subject:
        new_name = pending_subject.pop(phone)
        if active_subject.get(phone) != new_name:
            # subject changed (or set for the first time) — treat as a fresh RSVP
            active_subject[phone] = new_name

def extract_rsvp_from_response(phone, response_text, user_message):
    detect_subject_change(phone, response_text, user_message)

    subject_name = active_subject.get(phone) or phone_registry.get(phone) or "unknown"
    key = subject_name.lower().strip()

    lower = (user_message + " " + response_text).lower()
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

    rsvp_data[key]["dietary_vegetarian"] = any(w in lower for w in ["vegetarian", "vegetariano", "vegetariana"])
    rsvp_data[key]["dietary_vegan"] = any(w in lower for w in ["vegan", "vegano", "vegana"])
    rsvp_data[key]["dietary_nut_allergy"] = any(w in lower for w in ["nut allergy", "alergia a nozes", "peanut"])
    rsvp_data[key]["dietary_no_beef"] = any(w in lower for w in ["no beef", "sem carne vermelha", "não como carne vermelha"])
    rsvp_data[key]["dietary_no_pork"] = any(w in lower for w in ["no pork", "sem porco", "não como porco"])
    rsvp_data[key]["dietary_shellfish"] = any(w in lower for w in ["shellfish", "frutos do mar", "alergia a frutos"])

    dietary_items = []
    if rsvp_data[key]["dietary_vegetarian"]: dietary_items.append("vegetariano")
    if rsvp_data[key]["dietary_vegan"]: dietary_items.append("vegano")
    if rsvp_data[key]["dietary_nut_allergy"]: dietary_items.append("alergia nozes")
    if rsvp_data[key]["dietary_no_beef"]: dietary_items.append("sem carne vermelha")
    if rsvp_data[key]["dietary_no_pork"]: dietary_items.append("sem porco")
    if rsvp_data[key]["dietary_shellfish"]: dietary_items.append("alergia frutos do mar")
    rsvp_data[key]["dietary"] = ", ".join(dietary_items) if dietary_items else "nenhuma"

    days = []
    if any(w in lower for w in ["all three", "all 3", "os três", "todos os dias", "os 3", "tudo"]):
        days = ["all"]
    else:
        if any(w in lower for w in ["day 1", "dia 1", "24", "winery", "vinícola"]):
            days.append("day1")
        if any(w in lower for w in ["day 2", "dia 2", "25", "wedding", "casamento", "cerimônia"]):
            days.append("day2")
        if any(w in lower for w in ["day 3", "dia 3", "26", "pub", "scholars"]):
            days.append("day3")
    if days:
        rsvp_data[key]["days"] = days

    rsvp_data[key]["name"] = subject_name
    rsvp_data[key]["phone"] = phone

    if rsvp_data[key].get("attending") and rsvp_data[key].get("name"):
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
    return assistant_message

def get_admin_response(phone_number, user_message):
    stats = get_admin_stats()
    if "353833" in phone_number:
        name = "Larissa"
    elif "19292277" in phone_number:
        name = "Robert"
    else:
        name = "Carlotta (wedding planner)"
    context = f"Consulta administrativa de {name}.\n\nDados atuais:\n{json.dumps(stats, indent=2)}\n\nPergunta: {user_message}"
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=ADMIN_SYSTEM,
        messages=[{"role": "user", "content": context}]
    )
    return response.content[0].text

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
        for phone in list(all_phones - ADMIN_NUMBERS):
            try:
                send_zapi_message(phone, f"📢 *Atualização do Casamento*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} convidados!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        sent = 0
        for phone in list(bridal_party_phones - ADMIN_NUMBERS):
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
        if phone_key in ADMIN_NUMBERS and (upper_msg.startswith("[ALL]") or upper_msg.startswith("[BRIDAL]")):
            reply = handle_broadcast(incoming_message, from_number, to_number)
            if reply:
                send_whatsapp_message(from_number, reply, to_number)
                return Response('', status=200)
        if phone_key in ADMIN_NUMBERS:
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
        if phone in ADMIN_NUMBERS and (upper_msg.startswith('[ALL]') or upper_msg.startswith('[BRIDAL]')):
            reply = handle_broadcast_zapi(text, phone)
        elif phone in ADMIN_NUMBERS:
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
        for phone in list(all_phones - ADMIN_NUMBERS):
            try:
                send_zapi_message(phone, f"📢 *Atualização do Casamento*\n\n{msg}")
                sent += 1
            except: pass
        return f"✅ Mensagem enviada para {sent} convidados!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        sent = 0
        for phone in list(bridal_party_phones - ADMIN_NUMBERS):
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
