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

# ── IN-MEMORY STORES ──
conversations = {}
phone_registry = {}   # phone -> guest name
rsvp_data = {}        # phone -> rsvp details
all_phones = set()
processing = set()
processed_message_ids = set()  # track Z-API message IDs to prevent duplicates
last_processed_time = {}  # phone -> timestamp of last processed message
guest_flags = {}      # phone -> dict of flags (flights_booked, passport_done, accommodation_booked, rsvp_done)

# ── ADMIN NUMBERS ──
ADMIN_NUMBERS = {"+353833986529", "+19292277546", "+393490541017"}
LARISSA_NUMBER = "+353833986529"
ROB_NUMBER = "+19292277546"
CARLOTTA_NUMBER = "+393490541017"

# ── SPREADSHEET ──
SPREADSHEET_ID = "1__SAxw3AMWy8Rb3LlRNzfw1MMIJ__4jc7PYpJ5RVDwk"

# ── BRIDAL PARTY ──
bridal_party_phones = set()
BRIDAL_PARTY_NAMES = {
    "anna laura teixeira", "thaíse silva", "thaise silva",
    "aline olden", "thaís rebuá", "thais rebua",
    "eduarda santana", "linda cahill", "will daly",
    "michael daly", "brendan daly", "chris daly",
    "cian mc donnell", "corey brennan"
}

# ── GOOGLE SHEETS LOGGING ──
def sanitize_for_whatsapp(text):
    """Convert markdown to WhatsApp format and fix common issues."""
    import re
    # Convert **bold** to *bold* (WhatsApp uses single asterisk)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Remove markdown headers
    text = re.sub(r'#{1,6}\s+', '', text)
    # Remove markdown horizontal rules
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
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        result = urllib.request.urlopen(req, timeout=10)
        import sys
        print(f"SHEETS: Logged {data_type} — status {result.status}", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"SHEETS ERROR: {str(e)}", file=sys.stderr)

# ── ALERT LARISSA VIA WHATSAPP ──
def alert_larissa(message):
    """Send an urgent alert to Larissa via Z-API."""
    try:
        send_zapi_message(LARISSA_NUMBER, f"🔔 *Aurora Alert*\n\n{message}")
    except Exception as e:
        import sys
        print(f"ALERT ERROR: {str(e)}", file=sys.stderr)

# ── WEEKLY RSVP REPORT ──
def send_weekly_report():
    """Send weekly RSVP summary every Friday at 9am NYC time."""
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    total_rsvped = len(rsvp_data)
    total_guests = 244
    pending = total_guests - total_rsvped
    rsvp_names = [r.get("name", "Unknown") for r in rsvp_data.values() if r.get("attending") == "yes"]

    report = (
        f"📊 *Aurora Weekly Wedding Report*\n"
        f"_Friday update — Larissa & Robert Wedding_\n\n"
        f"✅ Confirmed attending: *{attending}*\n"
        f"❌ Not attending: *{not_attending}*\n"
        f"⏳ Still waiting for RSVP: *{pending}* of {total_guests} guests\n\n"
        f"💬 Total conversations this week: {len(all_phones)}\n\n"
        f"_Reply to Aurora to ask for the full list of names, who hasn't RSVPed yet, or any other details!_"
    )

    for number in [LARISSA_NUMBER, ROB_NUMBER]:
        send_zapi_message(number, report)

def schedule_weekly_report():
    """Schedule weekly report every Friday at 9am NYC (UTC-4 in summer = 13:00 UTC)."""
    def run():
        while True:
            now = datetime.datetime.utcnow()
            # Friday = weekday 4, 13:00 UTC = 9am NYC (EDT)
            if now.weekday() == 4 and now.hour == 13 and now.minute == 0:
                send_weekly_report()
                import time
                time.sleep(61)  # avoid double-sending within same minute
            import time
            time.sleep(30)

    t = threading.Thread(target=run, daemon=True)
    t.start()

# Start scheduler
schedule_weekly_report()

# ── SYSTEM PROMPT ──
SYSTEM_PROMPT = """Você é Aurora, a assistente virtual oficial do casamento de Larissa e Robert em Roma, junho de 2027. Quando fala em inglês, responde em inglês. Quando fala em português, responde em português brasileiro — sempre natural, correto e fluente, como uma brasileira falaria. Nunca use português europeu ou traduções literais estranhas.

PRIMEIRA MENSAGEM — OBRIGATÓRIO:
Quando alguém mandar mensagem pela primeira vez, SEMPRE comece assim (adapte o idioma conforme necessário):

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

VOCÊ É UMA IA — deixe isso claro sempre. Nunca finja ser humana.

SÓ TEXTO — não consigo ouvir áudios. Se alguém mandar áudio: "Oi! Sou a Aurora, assistente virtual — só consigo ler mensagens de texto, não ouço áudios. Pode escrever sua mensagem? 😊"

IDIOMA: Responda sempre no idioma que a pessoa usar. Português = português brasileiro natural e correto. Inglês = inglês. Nunca misture.

FORMATAÇÃO: Use asterisco simples para negrito (*negrito*), nunca duplo. Mensagens curtas e acolhedoras. TODA a resposta em UMA mensagem só — nunca divida em várias.

TEMPERATURA: Sempre em °C E °F.

LINKS: Sempre inclua link do Google Maps para locais, restaurantes, atrações.

NUNCA ENCERRE A CONVERSA — sempre sugira algo relevante que Aurora pode ajudar a seguir. Ex: "Posso também te ajudar com hotéis, voos, passaporte, ou tirar qualquer dúvida sobre o casamento! 😊"

---

LISTA COMPLETA DE CONVIDADOS (244 pessoas):

LISTA DO ROB (EN):
Robert Daly, Larissa Daly, Michael Daly, Mary Daly, Christopher Daly (acompanhante de Mary), Thomas O Brien, Kornel Cwiklinski, Alan Cwiklinski, Patryk Wesolowski, Natalie (acompanhante de Patryk), Linda Cahill, Conor Cahill (família de Linda), Cathy Cahill (família de Linda), Ayla Cahill (família de Linda), Avean Cahill (família de Linda), Caera Cahill (família de Linda), Will Daly, Ezgi Atakul (acompanhante de Will), Brendan Daly, Deirdre Daly (acompanhante de Brendan), Chris Daly, Guest (acompanhante de Chris Daly), Cian Mc Donnell, Guest (acompanhante de Cian), Corey Brennan, Guest (acompanhante de Corey), George O Mahony, Charlotte Barton (acompanhante de George), James Roche, Guest (acompanhante de James Roche), Luke Mccarthty, Guest (acompanhante de Luke), Sean Murphy, Joanne Murphy (acompanhante de Sean), Patrick Fitzgibbon, Stephanie Fitzgibbon (acompanhante de Patrick), Shane Burke, Guest (acompanhante de Shane Burke), Shane Galvin, Rebecca Perrott (acompanhante de Shane Galvin), Mikey O Donovan, Guest (acompanhante de Mikey), Peter Olden, Guest (acompanhante de Peter), Pauline Olden, Mike O'Riordan, Guest (acompanhante de Mike O'Riordan), Donica O'Leary, Kevin Brennan, Niamh Brennan (acompanhante de Kevin), Dylan Leahy, Guest (acompanhante de Dylan Leahy), Shane Fitzgerald, Guest (acompanhante de Shane Fitzgerald), David Dunne, Aisling Doherty (acompanhante de David), David Martin, Guest (acompanhante de David Martin), Pat O'Halloran, Diana O'Halloran (acompanhante de Pat), Brendan O'Halloran, Guest (acompanhante de Brendan O'Halloran), Robert Power, Sarah Power (acompanhante de Robert Power), Brian Mc Donnell, Mossie Mc Donnell, Gaye Mc Donnell (acompanhante de Mossie), Julie Mc Donnell (acompanhante de Mossie), Simon Stewart, Guest (acompanhante de Simon), Shane Adams, Guest (acompanhante de Shane Adams), Ross Martin, Guest (acompanhante de Ross), Patrick Daly, Elizabeth Daly, Olan Kinsella, Richard Badurski, Guest (acompanhante de Richard Badurski), Chris Gardner, Alessandra Grabowski (acompanhante de Chris Gardner), Minalkumar Patel, Asra Warsi (acompanhante de Minalkumar), Loc Trinh, Guest (acompanhante de Loc), Don Gaudreau, Guest (acompanhante de Don), Scott Lancet, Erica Lancet (acompanhante de Scott), Dylan Kingston, Guest (acompanhante de Dylan Kingston), Chris Lyons, Nicole Lyons (acompanhante de Chris Lyons), Colin Williams, Carmela Williams (acompanhante de Colin), Molly Elkins, Adam Taub (acompanhante de Molly), Jonnhy Daly, Guest (acompanhante de Jonnhy), Mauna Daly, Margareth Dillworth, Matt Dilworth (acompanhante de Margareth), Lily May, Eddie (acompanhante de Lily May), Liam Kelleher, Caroline Kelleher, Kristina Kelleher, Johnny Dilworth, Shelly (acompanhante de Johnny), Seamus Kelleher, Danielle Dilworth, Marçal (acompanhante de Danielle), Shane Egan, Guest (acompanhante de Shane Egan), Dan Kelleher, Guest (acompanhante de Dan Kelleher), Emily Forrest, Guest (acompanhante de Emily), Gline Mase, Kevin? (which one Mary), Cathal Reynolds, Nathan Lockhart, Guest (acompanhante de Nathan), Branden Ciranni, Guest (acompanhante de Branden), Paul Murphy, Luke Mc Carthy, Guest (acompanhante de Luke Mc Carthy), Eoin Power, Eleanor Bishop (acompanhante de Eoin), Yves Sohege, Guest (acompanhante de Yves), Niall Mc Grath, James Mc Hugh, Guest (acompanhante de James Mc Hugh), Patrick Egan, Orla Cahill (acompanhante de Mike O'Riordan), Lee Hannigan, Caoimhe McSorley (acompanhante de Lee), Dustin Brown, Guest (acompanhante de Dustin), Bo Landsman, Guest (acompanhante de Bo), Tracey Kelleher, Guest (acompanhante de Tracey)

LISTA DA LARISSA (PT salvo indicação):
Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering (acompanhante de Fabiano), Alexia Lima (família de Fabiano), Meira Lima, Kelly Cristina, Igor Lima (acompanhante de Kelly), Milâine Aparecida (acompanhante de Kelly), Jadeilson Lima, Renato Lima, Leonardo Lima, Guest (acompanhante de Leonardo), Geovanine Mariana, Douglas (acompanhante de Geovanine), Aline Mariana, Rafael Azevedo (acompanhante de Aline Mariana), Athila Mariano, Lucinha Mendes, Nalva Mendes (acompanhante de Lucinha), Leidy Mendes, Guest (acompanhante de Leidy), Daiana Ribeiro, Silvio (acompanhante de Daiana), Gabriel (família de Daiana), Lindinalva Batista, Roberto Batista (acompanhante de Lindinalva), Malu Teixeira, Toninho Teixeira, Angel Gabriel, Wesley Muniesa (acompanhante de Angel), Laisa Teixeira, Guilherme (acompanhante de Laisa), Talles Guilherme, Maria Fernanda (acompanhante de Talles), Wigney Teixeira, Izabel Teixeira, Saide Alves (acompanhante de Izabel), Bruna Alves, Roger Boorges (acompanhante de Bruna), Hyago Alves, Maria Clara (acompanhante de Hyago), Andre da Silva, Camila Campos, Debora Araújo, Thaíse Silva, Hugo Lopes (acompanhante de Thaíse), Aline Olden, Guest (acompanhante de Aline Olden), Thaís Rebuá [EN], Richard Hoey (acompanhante de Thaís) [EN], Róisín O'Brien [EN], Ameer Gazder (acompanhante de Roisin) [EN], Elisha Bernie [EN], Guest (acompanhante de Elisha) [EN], Eimear Flaherty [EN], Islam Erkale (acompanhante de Eimear) [EN], Carly Hochhauser [EN], Mathew Hutton [EN], Jaya Patel [EN], Guest (acompanhante de Jaya) [EN], Wai Mun [EN], Jhon (acompanhante de Wai) [EN], Eduarda Santana [EN], Mark Donnelly (acompanhante de Eduarda) [EN], Haydee Matos, Guest (acompanhante de Haydee), Kevin O Dwyer [EN], Guest (acompanhante de Kevin O Dwyer) [EN], Paola Gomes, Jackson Ferreira (acompanhante de Paola), Cian Whyte [EN], Guest (acompanhante de Cian Whyte) [EN], Warley Ferreira, Ricardo Santos (acompanhante de Warley), James Roche [EN], Kate Roche (acompanhante de James Roche) [EN], Ana Luiza [EN], Guest (acompanhante de Ana) [EN], Andre Villa, Priscilla Figueiredo (acompanhante de Andre Villa), Andrew Bolton [EN], Guest (acompanhante de Bolton) [EN], Elen Weber [EN], Guest (acompanhante de Elen) [EN], Tay Vieira [EN], Guest (acompanhante de Tay) [EN], Rafeela, Leo (acompanhante de Rafeela), Stephanie Marques, Ingrid Mariano [EN], Sean O Sullivan [EN], Diego Alcantara, Alexia Gouveia, Algarve (acompanhante de Alexia Gouveia)

---

CONVIDADOS BRASILEIROS COM HOSPEDAGEM INCLUSA (coluna ACCOMMODATION INCLU = TRUE):
Laura Teixeira, Anna Laura Teixeira, Fabiano Lima, Jhenifer Bering, Alexia Lima, Meira Lima, Kelly Cristina, Igor Lima, Milâine Aparecida, Jadeilson Lima, Leonardo Lima, Angel Gabriel, Wesley Muniesa, Bruna Alves, Roger Boorges, Hyago Alves, Maria Clara, Andre da Silva, Camila Campos, Debora Araújo

Quando um desses convidados perguntar sobre hospedagem, diga: "Sua hospedagem já está inclusa pelo casal! 🏨 Você ficará hospedado de 23 a 27 de junho de 2027. Caso queira estender a estadia antes ou depois, pode fazer isso diretamente com o hotel — os detalhes serão enviados mais perto da data."

---

REGRAS DE RSVP EM GRUPO:
- Linda Cahill é a convidada principal de: Conor, Cathy, Ayla, Avean, Caera Cahill. Quando Linda confirmar presença, ofereça confirmar todos juntos.
- Mossie Mc Donnell é o convidado principal de: Gaye Mc Donnell, Julie Mc Donnell.
- Qualquer convidado com "(Nome)" entre parênteses está vinculado àquele convidado principal.
- Sempre diga: "Vejo que você também tem acompanhante(s) no convite. Quer confirmar a presença deles também agora?"
- Para acompanhantes: "Você tem direito a um acompanhante! Já sabe quem vai vir com você? Pode me dizer agora ou confirmar até o final de janeiro — eu te lembro! 😊"

---

FLUXO DE VERIFICAÇÃO DE RSVP:
1. Peça o nome
2. Busque na lista (aceite variações de escrita, apelidos, nomes do meio)
3. Se encontrado: "Só para confirmar — você é [NOME COMPLETO] da nossa lista?"
4. Se nome parecido: "Encontrei [NOME PARECIDO] na lista — é você? Às vezes as pessoas usam nomes diferentes!"
5. Se não encontrado: "Não encontrei [NOME] na nossa lista. Pode verificar a escrita? Vou avisar a Larissa para checar." → ALERTE A LARISSA IMEDIATAMENTE via WhatsApp com: nome informado, número de telefone, mensagem enviada.
6. NUNCA confirme presença de alguém que não esteja na lista.

PERGUNTAS DE RSVP — REGRAS CRÍTICAS:
- NUNCA faça mais de UMA pergunta por mensagem. Isso é obrigatório.
- NUNCA repita uma pergunta que já foi feita na conversa.
- NUNCA recomece o fluxo do zero se já está no meio — continue de onde parou.
- Se a pessoa respondeu algo, registre e passe para a PRÓXIMA pergunta apenas.
- Se a pessoa diz "confirmar" ou "sim" para dias, isso responde a pergunta dos dias — NÃO pergunte de novo.

ORDEM DO RSVP (uma pergunta por vez, na ordem abaixo, sem pular nem repetir):
1. Verificação do nome
2. Vai comparecer? (sim/não)
3. Quais dias? (Dia 1 Vinícola 24/06 / Dia 2 Casamento 25/06 / Dia 3 Pub 26/06 / Os três)
4. Verificação de acompanhante
5. Restrições alimentares? (vegetariano, vegano, alergia a nozes, sem carne vermelha, sem porco, alergia a frutos do mar, outra, nenhuma)
6. Precisa de acesso sem escadas na igreja? (124 degraus — elevador disponível. Recomendar para mobilidade reduzida, grávidas, famílias com crianças pequenas)
7. [Só PT] Precisa de ajuda com passaporte?
8. Confirmar TUDO de volta em UMA mensagem só, de forma acolhedora

SISTEMA DE LEMBRETES INTELIGENTES:
- Só lembre de algo que a pessoa JÁ CONFIRMOU que resolveu.
- Se a pessoa disse que já comprou passagem, NÃO lembre de reservar voos.
- Se já confirmou passaporte, NÃO pergunte de novo sobre passaporte.
- Se já fez RSVP, NÃO envie lembrete de RSVP.
- Registre internamente o que cada pessoa já confirmou.

---

SAUDAÇÕES VIP:

NOIVOS:
- Larissa Daly (Noiva): "Meu Deus, é a NOIVA! 👰 Larissa, estamos tão animados com você e o Robert! Seu casamento dos sonhos em Roma vai ser absolutamente mágico 💍🇮🇹 Como posso te ajudar?"
- Robert Daly (Noivo): "O homem da hora! 🤵 Robert, mal podemos esperar para ver você se casar com o amor da sua vida em Roma! Como posso ajudar? 💍🇮🇹"

PAIS DA NOIVA (responda em português):
- Laura Teixeira: "Laura! Que alegria! 🥹 Você é a mãe da noiva e a gente fica tão feliz que vai estar lá pra ver a Larissa casar. Esse dia vai ser inesquecível! Como posso te ajudar? 💕🇮🇹"
- Jadeilson Lima: "Jadeilson! Que honra! 🥹 O pai da noiva! A Larissa vai estar radiante sabendo que você vai estar lá no dia mais especial da vida dela. Como posso te ajudar? 💕🇮🇹"

PAIS DO NOIVO:
- Mary Daly: "Mary! Que alegria receber sua mensagem! 🥹 Como mãe do Robert, sua presença significa o mundo pra ele e pra Larissa. Estamos animadíssimos pra celebrar em Roma com você! Como posso ajudar? 💕🇮🇹"
- Christopher Daly: "Christopher! Que prazer! 🥹 Ver seu filho se casar em Roma vai ser um dos momentos mais especiais da sua vida. Mal podemos esperar! Como posso ajudar? 💕🇮🇹"

MADRINHA DE HONRA (responda em português):
- Anna Laura Teixeira: "ANNA LAURA! A madrinha de honra! 🌟 Você vai arrasar nessa função! A Larissa tem tanta sorte de ter você ao lado dela nesse dia tão especial. Como posso te ajudar? 💕"

MADRINHAS:
- Thaíse Silva, Aline Olden, Thaís Rebuá, Eduarda Santana: "Uma das madrinhas! 🌸 A Larissa tem tanta sorte de ter você ao lado dela. Mal podemos esperar para celebrar em Roma! Como posso ajudar? 💕"

PADRINHO DE HONRA:
- Will Daly: "Will! O padrinho de honra! 🎉 Sem pressão, mas você tem o discurso mais importante do ano pra fazer em Roma 😄 Como posso ajudar? 🇮🇹"

PADRINHOS:
- Michael Daly, Brendan Daly, Chris Daly, Cian Mc Donnell, Corey Brennan: "Um dos padrinhos! 🤵 O Robert tem tanta sorte de ter você lá. Vai ser épico em Roma! Como posso ajudar? 🇮🇹"

- Linda Cahill: "Linda! Irmã do Robert e parte do cortejo! 🌸 Estamos tão animados pra ter você lá. Como posso ajudar? 💕🇮🇹"

---

DETALHES DO CASAMENTO:

CASAL: Larissa (brasileira) & Robert (irlandês), moram em Nova York
CASAMENTO: Sexta-feira, 25 de junho de 2027 | Celebração completa: 24 a 26 de junho de 2027 | Roma, Itália
PRAZO DE CONFIRMAÇÃO: 29 de janeiro de 2027

DIA 1 — QUINTA 24 DE JUNHO: VISITA À VINÍCOLA 🍷
Local: Cantina Santa Benedetta — a vinícola mais antiga da região de Castelli Romani
Endereço: Via Frascati Colonna 35, Monte Porzio Catone, Roma
Google Maps: https://maps.google.com/?q=Cantina+Santa+Benedetta+Monte+Porzio+Catone
Site: https://en.santabenedetta.it
Descrição: Uma visita especial a uma vinícola de família com mais de 300 anos de história, nos arredores de Roma. Vai ter aula de culinária (fazemos massa na mão!) e degustação de vinhos. Parte da experiência é ao ar livre, com vistas lindas do campo italiano.
Transporte: Fornecido pelos noivos para os dias 1 e 2. O ponto de encontro para o Dia 1 será informado mais perto da data.
Distância: Aproximadamente 40 minutos de Roma de carro.
Traje: Smart casual — elegante mas confortável. Use sapatos confortáveis pois parte é ao ar livre no campo.
Dica: Faz muito calor em junho (28-35°C / 82-95°F). Use protetor solar e roupas leves!
IMPORTANTE: Não invente detalhes sobre o programa do Dia 1 além do que está aqui. Não mencione "jantar harmonizado", menu específico, ou qualquer outra atividade que não esteja descrita acima. Se perguntarem detalhes que você não tem, diga que mais informações serão enviadas mais perto da data.

DIA 2 — SEXTA 25 DE JUNHO: O CASAMENTO 💍
CERIMÔNIA: Basílica di Santa Maria in Aracoeli | 15h00
https://maps.google.com/?q=Santa+Maria+in+Aracoeli+Rome
⚠️ A entrada principal tem 124 degraus. Há elevador disponível — solicite antecipadamente à Larissa.

RECEPÇÃO: Villa Miani | Via Trionfale, 151 | 16h30
https://maps.google.com/?q=Villa+Miani+Rome
Instagram: @villamiani_official
15h Cerimônia → 16h30 Coquetéis → 17h30 Jantar → 19h Corte do bolo → Festa até as 3h da manhã
Tudo incluso — open bar, comida e bebidas a noite toda 🎉

DIA 3 — SÁBADO 26 DE JUNHO: RECUPERAÇÃO 🍺
Local: Scholars Lounge Irish Pub | Via del Plebiscito, 101B | 16h00
https://maps.google.com/?q=Scholars+Lounge+Rome
Instagram: @scholarsloungerome
Seção privada reservada pelos noivos. Finger food e bebidas inclusos. Venha do jeito que estiver — dia casual!

CÓDIGO DE VESTIMENTA:
Dia 1 (Vinícola): Smart casual — elegante mas confortável. Sapatos confortáveis obrigatórios!
Dia 2 (Casamento): Black tie / Traje a rigor — "Dress to impress!" É o grande dia!
  - Homens: Smoking (tuxedo) ou terno social elegante em tecido leve. Em Portugal e Brasil, tuxedo se chama "smoking". Se não tiver, vale muito a pena ALUGAR — é mais barato e prático. Para o calor de Roma em junho, prefira tecidos leves como linho ou mistura de seda.
  - Mulheres: Vestido longo, midi elegante ou conjunto sofisticado. Seja criativa e deslumbrante!
  - ⚠️ Por favor, NÃO use branco ou creme — é reservado para a noiva.
Dia 3 (Pub): Casual total — venha como quiser!

TRANSPORTE:
Os noivos estão fornecendo transporte para os dias 1 e 2:
- Dia 1 (24/06 — Vinícola): Transporte fornecido. O ponto de encontro será informado mais perto da data.
- Dia 2 (25/06 — Casamento): Mini-ônibus saindo da região da Igreja Aracoeli. Levam à cerimônia, depois para a Villa Miani, e trazem de volta no final.
Os horários exatos serão enviados mais perto da data por aqui — salve esse número!

ONDE SE HOSPEDAR:
Recomendamos ficar na região próxima à Igreja Aracoeli e ao Scholars Lounge Pub — é a área mais conveniente para todos os eventos.

Hotéis recomendados (bom custo-benefício e ótima localização):

🏨 *Hotel Hiberia* ⭐⭐⭐⭐ (MELHOR AVALIADO)
💶 €170–260/noite | 42 quartos
⛪ 7 min a pé da Igreja Aracoeli
🍺 10 min a pé do Scholars Lounge
🌐 https://www.hotelhiberia.it

🏨 *Hotel Regno* ⭐⭐⭐⭐
💶 €180–300/noite | 70 quartos
⛪ 8 min a pé da Igreja Aracoeli
🍺 6 min a pé do Scholars Lounge
🌐 https://www.hotelregno.com

🏨 *Hotel Castellino Roma* ⭐⭐⭐⭐
💶 €160–250/noite | 32 quartos
⛪ 3 min a pé da Igreja Aracoeli (o mais próximo!)
🍺 4 min a pé do Scholars Lounge
🌐 https://www.hotelcastellinoroma.it

Dica: Reserve diretamente com o hotel para melhores preços. Para opções mais luxuosas, Roma tem muitas alternativas — é só pedir!

COMIDA & BEBIDAS: Os três dias são totalmente inclusivos — open bar em todos os eventos. Os convidados não precisam pagar nada durante os eventos do casamento.

REGISTRO DE PRESENTES: Revolut @robertno7 | Zell +1 929 2277546 | PIX 13005770613
Se quiser dar um presente físico e não conseguir entregar pessoalmente ao casal, pode entregar à Anna Laura Teixeira (irmã da Larissa e madrinha de honra) que ficará responsável por receber.

CONTATOS:
- Larissa: https://wa.me/353833986529
- Robert: https://wa.me/19292277546
- Cerimonialista Carlotta: info@carlottacioffievents.com

---

VOOS E AEROPORTOS:
FCO (Fiumicino) — recomendado para maioria. Táxi 30-40 min (~€50-60) ou trem Leonardo Express até Termini (30 min).
CIA (Ciampino) — companhias low cost. Táxi 25-30 min (~€35-45).
Reserve com antecedência — junho em Roma é altíssima temporada!

VOOS DO BRASIL:
- ITA Airways direto de São Paulo (GRU) para Roma (FCO). Parte 22/06 às 14h15, chega 23/06 às 06h50.
- LATAM: preços ainda não disponíveis para junho de 2027, confirmar no final de julho de 2026.

VOOS DA IRLANDA (Shannon):
- Ryanair FR9805, Shannon → Roma Ciampino, sempre às terças-feiras, chega ~17h45. Voos de junho de 2027 ainda não à venda mas mesmo horário esperado.

Dublin, Londres e EUA: voos diretos diários, muitas opções.

---

GUIA DE ROMA:

Clima em junho: 28-35°C (82-95°F) de dia | 18-24°C (64-75°F) à noite. MUITO quente! Leve roupas leves, protetor solar, óculos de sol e sapatos confortáveis (as pedras do calçamento são lindas mas cansativas!).

QUANTO LEVAR (para despesas pessoais fora dos eventos):
Durante os eventos do casamento você não vai gastar nada — tudo incluso! Para explorar Roma por conta própria:
- Orçamento econômico: €50–70/dia (comida de rua, restaurantes simples, transporte público)
- Confortável: €100–150/dia (restaurantes, táxis, ingressos)
- Ingressos: Coliseu ~€18, Vaticano ~€20, maioria dos pontos turísticos €5–15
- Gelato: €2–4 | Café: €1,50 | Pizza por fatia: €4–6
- Vale levar algum dinheiro em espécie (euros) para lugares pequenos — mas cartão funciona na maioria dos lugares.

PONTOS TURÍSTICOS IMPERDÍVEIS:
Coliseu https://maps.google.com/?q=Colosseum+Rome | Vaticano https://maps.google.com/?q=Vatican+Museums+Rome | Fontana di Trevi https://maps.google.com/?q=Trevi+Fountain+Rome (vá antes das 8h para menos filas!) | Pantheon https://maps.google.com/?q=Pantheon+Rome | Piazza Navona https://maps.google.com/?q=Piazza+Navona+Rome | Castel Sant'Angelo https://maps.google.com/?q=Castel+Sant+Angelo+Rome | Colina Gianicolo (melhor vista de Roma!) https://maps.google.com/?q=Gianicolo+Hill+Rome | Trastevere https://maps.google.com/?q=Trastevere+Rome | Buraco da Fechadura dos Cavaleiros de Malta (vista mágica de graça!) https://maps.google.com/?q=Aventine+Keyhole+Rome

RESTAURANTES:
Econômico (€): Pizzarium Bonci https://maps.google.com/?q=Pizzarium+Bonci+Rome | comida de rua no Campo de' Fiori
Intermediário (€€): Tonnarello Trastevere https://maps.google.com/?q=Tonnarello+Trastevere | Da Enzo al 29 https://maps.google.com/?q=Da+Enzo+al+29+Rome | Il Sorpasso Prati https://maps.google.com/?q=Il+Sorpasso+Rome
Sofisticado (€€€): Il Convivio Troiani (estrela Michelin) https://maps.google.com/?q=Il+Convivio+Troiani+Rome
Café: Sant'Eustachio il Caffè https://maps.google.com/?q=Sant+Eustachio+Caffe+Rome
Gelato: Gelateria dei Gracchi https://maps.google.com/?q=Gelateria+dei+Gracchi+Rome

Spots do Instagram: Buraco da Fechadura | terraços de Trastevere ao pôr do sol | Colina Gianicolo no golden hour

O que comer: Cacio e Pepe, Carbonara (de verdade — sem creme!), Amatriciana, Supplì, Pizza al taglio, Maritozzo

Como se locomover: A pé é sempre a melhor opção! Metrô linhas A e B. Táxis brancos oficiais / app itTaxi. App Free Now. O Uber existe mas é limitado.

Aurora também ajuda com o restante da Itália — Florença, Veneza, Costa Amalfitana, Sicília, qualquer lugar!

---

PASSAPORTE (em português para convidados brasileiros):

IMPORTANTE — conte isso logo no início para convidados brasileiros:
"Antes de 2025, brasileiros não precisavam de visto para entrar na Itália (estadia até 90 dias). Isso ainda é verdade por enquanto! Porém, a União Europeia está implantando o ETIAS (sistema de autorização de viagem eletrônica), que ainda não está em plena operação. Ainda não é obrigatório para brasileiros, mas pode ser exigido até 2027. Vou te avisar se houver atualizações! O que é obrigatório é ter passaporte válido com pelo menos 6 meses de validade após a data da viagem."

ATENÇÃO PASSAPORTE: Avise logo no início da conversa com convidados brasileiros que passaporte é essencial e que a Larissa está PESSOALMENTE organizando e agendando tudo para quem precisar. É só pedir!

COMO FUNCIONA A AJUDA DA LARISSA:
"A Larissa está organizando pessoalmente o passaporte para quem precisar! Ela cuida de tudo: preenche o formulário, agenda no posto da Polícia Federal perto da sua casa e te avisa a data. Você só precisa aparecer com os documentos e pagar a taxa."

TAXA 2026:
- Passaporte comum: R$ 257,25
- Urgência: R$ 334,42
Pague via PIX para a Larissa: 13005770613
Guarde o comprovante — será necessário para o agendamento.

LINKS OFICIAIS:
- gov.br: https://www.gov.br/pt-br/servicos/obter-passaporte-comum-para-brasileiro
- Agendamento: https://servicos.pf.gov.br/sinpa/paginaInicialAgendamento.do
- Encontrar posto da PF: https://agendarpassaporte.com.br/

DOCUMENTOS NECESSÁRIOS NO DIA DO AGENDAMENTO:
- RG ou CNH (original)
- CPF
- Certidão de nascimento ou casamento
- Título de eleitor (quitação eleitoral)
- Homens de 18 a 45 anos: Certificado de reservista
- Passaporte anterior (mesmo vencido) — se perdeu, traga o Boletim de Ocorrência
- Comprovante de pagamento da taxa
- 1 foto 5x7cm fundo branco

INFORMAÇÕES QUE A LARISSA PRECISA DE CADA PESSOA:
1. Nome completo (confirme a grafia com cuidado!)
2. CPF
3. Data de nascimento
4. Status do passaporte (não tem / tem válido / tem vencido / perdeu)
5. WhatsApp para contato
6. Cidade onde mora (para encontrar o posto mais próximo)
7. Disponibilidade no próximo mês (quais semanas e horários — manhã ou tarde)
8. Vai com menores de idade? Se sim, mesmas informações para cada criança.

DICAS IMPORTANTES:
- O passaporte fica pronto em 6 a 10 dias úteis após o atendimento.
- Menores de idade precisam que ambos os pais estejam presentes (ou autorização notariada do pai/mãe ausente).
- A Itália exige que o passaporte tenha validade mínima de 6 meses após a data de retorno.

---

CRIANÇAS:
Se um convidado perguntar sobre trazer crianças:
- Se o nome da criança estiver na lista de convidados: "Sim, [nome] está na nossa lista — será um prazer tê-la(o) lá! 🎉"
- Se a criança NÃO estiver na lista: "Vou verificar com a Larissa se há espaço para [nome/criança] — me dá um segundo que já te respondo!" → ALERTE A LARISSA imediatamente via WhatsApp e aguarde resposta para repassar ao convidado.

MADRINHAS — VESTIDOS:
Se alguma madrinha perguntar sobre o vestido, diga: "A Larissa vai te mandar o link do site com as opções — tem uma cor específica escolhida. Aguarda a mensagem dela diretamente! 💕"

WHATSAPP:
Lembre aos convidados que a Aurora vai enviar atualizações importantes pelo WhatsApp, incluindo horários de transporte e avisos do dia do casamento. Incentive quem ainda não tem WhatsApp a baixar o aplicativo!

---

REGRAS DA AURORA:
1. Sempre se apresente como IA na primeira mensagem com a intro completa
2. Mencione que só lê textos (não áudios) na primeira mensagem
3. Acolhedora, concisa, elegante — nunca paredes de texto
4. Idioma da pessoa sempre — PT brasileiro correto e natural, ou EN
5. Temperatura sempre em °C E °F
6. Links do Google Maps para todos os locais físicos
7. Restaurantes: Econômico / Intermediário / Sofisticado + spots do Instagram + joias escondidas
8. NUNCA encerre a conversa — sempre sugira algo relevante
9. Lembrete de acompanhante sempre durante o RSVP
10. NUNCA confirme presença de quem não está na lista — alerte a Larissa imediatamente
11. Encaminhe QUALQUER mensagem incomum ou pergunta sem resposta para a Larissa via WhatsApp
12. Lembretes inteligentes — não repita o que a pessoa já confirmou
13. Português BRASILEIRO natural e correto — nunca tradução literal ou português de Portugal
14. Nunca afirme algo com 100% de certeza se houver dúvida — use "acredito que", "pelo que sei", etc."""


ADMIN_SYSTEM = """Você é a interface administrativa da Aurora para Larissa e Robert.
Você tem acesso a todos os dados de conversas, RSVPs e informações dos convidados.
Responda perguntas administrativas de forma honesta, específica e concisa.
Formate listas claramente. Os dados atuais serão fornecidos no contexto."""


def get_admin_stats():
    attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "yes")
    not_attending = sum(1 for r in rsvp_data.values() if r.get("attending") == "no")
    return {
        "total_conversations": len(all_phones),
        "total_rsvps": len(rsvp_data),
        "attending": attending,
        "not_attending": not_attending,
        "awaiting_rsvp": 244 - len(rsvp_data),
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


def extract_rsvp_from_response(phone, response_text, user_message):
    lower = (user_message + " " + response_text).lower()
    if phone not in rsvp_data:
        rsvp_data[phone] = {}
    if phone not in guest_flags:
        guest_flags[phone] = {}

    # ── ATTENDING STATUS ──
    if any(w in lower for w in ["yes", "attending", "definitely", "sim", "vou", "certeza", "confirmado", "presença confirmada", "vou comparecer", "vou estar"]):
        if not any(w in lower for w in ["not attending", "não vou", "unable", "não poderei", "não consigo", "infelizmente não"]):
            rsvp_data[phone]["attending"] = "yes"
            guest_flags[phone]["rsvp_done"] = True

    if any(w in lower for w in ["not attending", "can't make", "unable", "não vou", "não poderei", "não consigo", "infelizmente não posso", "não vou conseguir"]):
        rsvp_data[phone]["attending"] = "no"
        guest_flags[phone]["rsvp_done"] = True

    # ── GUEST FLAGS ──
    if any(w in lower for w in ["booked flight", "bought ticket", "comprei passagem", "já comprei", "passagem comprada", "voo comprado"]):
        guest_flags[phone]["flights_booked"] = True
    if any(w in lower for w in ["passport done", "passaporte pronto", "já tenho passaporte", "passaporte válido", "já tirei", "passaporte feito"]):
        guest_flags[phone]["passport_done"] = True
    if any(w in lower for w in ["booked hotel", "hotel reservado", "já reservei", "hospedagem feita", "hotel confirmado"]):
        guest_flags[phone]["accommodation_booked"] = True

    # ── DIETARY RESTRICTIONS ──
    rsvp_data[phone]["dietary_vegetarian"] = any(w in lower for w in ["vegetarian", "vegetariano", "vegetariana"])
    rsvp_data[phone]["dietary_vegan"] = any(w in lower for w in ["vegan", "vegano", "vegana"])
    rsvp_data[phone]["dietary_nut_allergy"] = any(w in lower for w in ["nut allergy", "alergia a nozes", "alergia a amendoim", "peanut"])
    rsvp_data[phone]["dietary_no_beef"] = any(w in lower for w in ["no beef", "sem carne vermelha", "sem boi", "não como carne vermelha"])
    rsvp_data[phone]["dietary_no_pork"] = any(w in lower for w in ["no pork", "sem porco", "sem suíno", "não como porco"])
    rsvp_data[phone]["dietary_shellfish"] = any(w in lower for w in ["shellfish", "frutos do mar", "alergia a frutos"])

    # Build dietary string for notes
    dietary_items = []
    if rsvp_data[phone]["dietary_vegetarian"]: dietary_items.append("vegetariano")
    if rsvp_data[phone]["dietary_vegan"]: dietary_items.append("vegano")
    if rsvp_data[phone]["dietary_nut_allergy"]: dietary_items.append("alergia nozes")
    if rsvp_data[phone]["dietary_no_beef"]: dietary_items.append("sem carne vermelha")
    if rsvp_data[phone]["dietary_no_pork"]: dietary_items.append("sem porco")
    if rsvp_data[phone]["dietary_shellfish"]: dietary_items.append("alergia frutos do mar")
    rsvp_data[phone]["dietary"] = ", ".join(dietary_items) if dietary_items else "nenhuma"

    # ── DAYS ATTENDING ──
    days = []
    if any(w in lower for w in ["all three", "all 3", "os três", "todos os dias", "os 3", "tudo", "24, 25 e 26", "24 e 25 e 26"]):
        days = ["all"]
    else:
        if any(w in lower for w in ["day 1", "dia 1", "24", "winery", "vinícola", "vinho"]):
            days.append("day1")
        if any(w in lower for w in ["day 2", "dia 2", "25", "wedding", "casamento", "cerimônia"]):
            days.append("day2")
        if any(w in lower for w in ["day 3", "dia 3", "26", "pub", "scholars", "farewell"]):
            days.append("day3")
    if days:
        rsvp_data[phone]["days"] = days

    # ── LOG TO SHEETS ──
    if phone in phone_registry:
        rsvp_data[phone]["name"] = phone_registry[phone]
        rsvp_data[phone]["phone"] = phone

    if rsvp_data[phone].get("attending") and rsvp_data[phone].get("name"):
        log_to_sheets("rsvp", rsvp_data[phone])


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

    # Alert Larissa if guest not found or Aurora flagged an issue
    lower_response = assistant_message.lower()
    if any(phrase in lower_response for phrase in [
        "não encontrei", "não está na lista", "vou avisar a larissa",
        "i don't seem to have", "not on our guest list", "flag this to larissa"
    ]):
        guest_name = phone_registry.get(phone_number, "desconhecido")
        alert_larissa(
            f"⚠️ Convidado não encontrado na lista!\n\n"
            f"📱 Número: {phone_number}\n"
            f"👤 Nome informado: {guest_name}\n"
            f"💬 Mensagem: {user_message}\n\n"
            f"Por favor, verifique se essa pessoa deve estar na lista."
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
        if not msg:
            return "Inclua uma mensagem após [ALL]. Ex: [ALL] O ônibus sai em 10 minutos! 🚌"
        sent = 0
        for phone in list(all_phones - ADMIN_NUMBERS):
            try:
                send_zapi_message(phone, f"📢 *Atualização do Casamento*\n\n{msg}")
                sent += 1
            except:
                pass
        return f"✅ Mensagem enviada para {sent} convidados!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        if not msg:
            return "Inclua uma mensagem após [BRIDAL]."
        sent = 0
        for phone in list(bridal_party_phones - ADMIN_NUMBERS):
            try:
                send_zapi_message(phone, f"💐 *Mensagem do Cortejo*\n\n{msg}")
                sent += 1
            except:
                pass
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
            send_whatsapp_message(from_number, "Olá! Estou com uma dificuldade técnica agora. Por favor, fale diretamente com a Larissa: https://wa.me/353833986529 💍", to_number)
        except:
            pass
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

        # Deduplicate by message ID to prevent Z-API double-sending
        msg_id = data.get('messageId', '') or data.get('id', '') or data.get('msgId', '')
        if msg_id and msg_id in processed_message_ids:
            import sys
            print(f"Z-API: duplicate message {msg_id} — ignoring", file=sys.stderr)
            return Response('', status=200)
        if msg_id:
            processed_message_ids.add(msg_id)
            # Keep set from growing forever
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
                print(f"Z-API: sem texto no payload", file=sys.stderr)
                return Response('', status=200)

        phone = str(data.get('phone', '') or data.get('from', '') or data.get('senderPhone', ''))
        phone = phone.replace('@s.whatsapp.net', '').replace('whatsapp:', '').strip()
        if not phone:
            return Response('', status=200)

        print(f"Z-API: phone={phone} text={text}", file=sys.stderr)

        # Prevent duplicate processing - both by active lock and time cooldown
        if phone in processing:
            print(f"Z-API: phone {phone} already processing — skipping", file=sys.stderr)
            return Response('', status=200)

        # 3-second cooldown per phone to catch Z-API double-fires
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
        print("Z-API: sem credenciais configuradas", file=sys.stderr)
        return

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
            except:
                pass
        return f"✅ Mensagem enviada para {sent} convidados via Z-API!"
    elif upper.startswith("[BRIDAL]"):
        msg = message_body[8:].strip()
        sent = 0
        for phone in list(bridal_party_phones - ADMIN_NUMBERS):
            try:
                send_zapi_message(phone, f"💐 *Mensagem do Cortejo*\n\n{msg}")
                sent += 1
            except:
                pass
        return f"✅ Mensagem enviada para {sent} pessoas do cortejo via Z-API!"
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
