from fastapi import FastAPI, Request, BackgroundTasks
import os
import requests
import logging
from dotenv import load_dotenv
import anthropic

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Allterra AI Webhook")

import time as _time
_recent_calls: dict[str, float] = {}
_DEDUP_WINDOW = 300  # seconds — blocks duplicate calls from same from/to within 5 minutes

# WhatsApp AI conversation history — keyed by sender phone number
_conversations: dict[str, list] = {}
_ai_replies_enabled: bool = False  # owner must send "AI ON" to activate
_redirect_enabled: bool = False    # owner must send "REDIRECT ON" to activate

REDIRECT_MESSAGE = (
    "Hi! 👋 Thanks for reaching out. For immediate assistance please call us on "
    "*+27 60 071 6833* — our AI receptionist is available 24/7 and will take your details right away."
)

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")

# ── Client config — keyed by the Telnyx number callers dial (to_number) ──────
#
# CORE (every client):
#   business_name         — displayed in WhatsApp notifications and CRM
#   business_type         — e.g. "Plumbing and Solar" — used in auto-replies
#   telnyx_from_number    — the Telnyx number Retell uses for this client
#   twenty_api_key        — Twenty CRM API key
#   twenty_api_url        — Twenty CRM Railway URL
#   owner_whatsapp        — owner's WhatsApp number for call summary notifications
#   whapi_token           — Whapi token for sending call summaries
#
# ADD-ON (optional):
#   whatsapp_mode         — "off" | "missed_calls_only" | "all_messages"
#                           Controls the /whatsapp-event endpoint behaviour
#
CLIENTS: dict[str, dict] = {
    "+27600716833": {
        # ── Core ──
        "business_name":      "Allterra AI",
        "business_type":      "AI Solutions",
        "telnyx_from_number": "+27600716833",
        "twenty_api_key":     os.getenv("TWENTY_API_KEY"),
        "twenty_api_url":     os.getenv("TWENTY_API_URL", "https://api.twenty.com"),
        "owner_whatsapp":     "27837088951",
        "whapi_token":        os.getenv("WHAPI_TOKEN"),
        # ── Add-on ──
        "whatsapp_mode":      "all_messages",
    },
    "+27600485594": {
        # ── Core ──
        "business_name":      "Renewable Plumbing and Solar Experts",
        "business_type":      "Plumbing and Solar",
        "telnyx_from_number": "+27600485594",
        "twenty_api_key":     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3M2U1ZDJhNi0wNDcyLTRiNDktYWUyYi05ZTY2MjFmNzczNmYiLCJ0eXBlIjoiQVBJX0tFWSIsIndvcmtzcGFjZUlkIjoiNzNlNWQyYTYtMDQ3Mi00YjQ5LWFlMmItOWU2NjIxZjc3MzZmIiwiaWF0IjoxNzc4MTgxNTAyLCJleHAiOjQ5MzE2OTUxMDEsImp0aSI6ImFlYmUwNzc1LTRmYTYtNGFlMy1hZjU3LTMyMzZhN2UwZWFlNiJ9.LFncKK8Jt-54houNowblF0oDd_keWqRgzR0c8SYVqtE",
        "twenty_api_url":     "https://twenty-production-9955.up.railway.app",
        "owner_whatsapp":     "27748887981",
        "whapi_token":        os.getenv("WHAPI_TOKEN"),
        # ── Add-on ──
        "whatsapp_mode":      "missed_calls_only",  # upgrade to "all_messages" when bot SIM is active
    },
}

# Fallback for unknown numbers
DEFAULT_CLIENT: dict = {
    "business_name":      "Allterra AI",
    "business_type":      "AI Solutions",
    "telnyx_from_number": os.getenv("TELNYX_FROM_NUMBER", ""),
    "twenty_api_key":     os.getenv("TWENTY_API_KEY"),
    "twenty_api_url":     os.getenv("TWENTY_API_URL", "https://api.twenty.com"),
    "owner_whatsapp":     os.getenv("OWNER_WHATSAPP", "27837088951"),
    "whapi_token":        os.getenv("WHAPI_TOKEN"),
    "whatsapp_mode":      "off",
}


def get_client(to_number: str, metadata: dict) -> dict:
    """Resolve client config: CLIENTS dict → metadata → DEFAULT_CLIENT."""
    client = dict(CLIENTS.get(to_number) or DEFAULT_CLIENT)
    # Retell metadata can still override any field at runtime
    for key in ("business_name", "owner_whatsapp", "telnyx_from_number",
                "twenty_api_key", "twenty_api_url"):
        if metadata.get(key):
            client[key] = metadata[key]
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise_za_number(number: str) -> str:
    """Convert 08xxxxxxxx → +2783xxxxxxxx. Leave +27 and other countries alone."""
    n = number.strip()
    if n.startswith("0") and len(n) == 10:
        return "+27" + n[1:]
    if n and not n.startswith("+"):
        return "+" + n
    return n or "Unknown"


# ── WhatsApp reply webhook ────────────────────────────────────────────────────

@app.post("/whatsapp-reply")
async def whatsapp_reply(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception as e:
        log.error(f"whatsapp-reply parse error: {e}")
        return {"status": "success"}

    log.info(f"whatsapp-reply payload: {data}")

    global _ai_replies_enabled
    whapi_token = DEFAULT_CLIENT.get("whapi_token") or os.getenv("WHAPI_TOKEN", "")
    owner = DEFAULT_CLIENT.get("owner_whatsapp", "")

    # ── Missed calls — always auto-reply, no toggle needed ───────────────────
    # Whapi may send calls in a dedicated "calls" array or as messages with type="call"
    def _dispatch_missed_call(caller_jid: str):
        phone = caller_jid.split("@")[0] if "@" in caller_jid else caller_jid
        if not phone:
            return
        log.info(f"Missed WhatsApp call from {phone}")
        background_tasks.add_task(
            _handle_whatsapp_missed_call,
            phone=phone,
            whapi_token=whapi_token,
            owner_whatsapp=owner,
            business_name=DEFAULT_CLIENT.get("business_name", ""),
            business_type=DEFAULT_CLIENT.get("business_type", ""),
            twenty_api_key=DEFAULT_CLIENT.get("twenty_api_key", ""),
            twenty_api_url=DEFAULT_CLIENT.get("twenty_api_url", ""),
        )

    for call in (data.get("calls") or []):
        if call.get("type") == "missed":
            _dispatch_missed_call(call.get("from", ""))

    # ── Inbound messages ─────────────────────────────────────────────────────
    messages = data.get("messages") or []
    if not messages:
        msg = data.get("message") or data
        messages = [msg]

    for msg in messages:
        chat_id: str = msg.get("chat_id", "") or msg.get("from", "")
        if chat_id.endswith("@g.us"):
            log.info(f"Skipping group message from {chat_id}")
            continue

        sender: str = msg.get("from", "") or chat_id
        if "@" in sender:
            sender = sender.split("@")[0]

        # Skip bot's own outgoing messages to customers, but keep owner self-messages
        if msg.get("from_me") and sender != owner:
            continue

        msg_type = msg.get("type", "")

        # Missed call sent as a message event
        if msg_type == "call":
            call_status = (msg.get("call") or {}).get("type", "") or msg.get("status", "")
            log.info(f"WhatsApp call event from {sender}: status={call_status}")
            if call_status in ("missed", "") and sender != owner:
                _dispatch_missed_call(sender)
            continue

        body: str = ""
        if isinstance(msg.get("text"), dict):
            body = msg["text"].get("body", "")
        else:
            body = msg.get("body", "") or msg.get("text", "")

        body = body.strip()
        log.info(f"WhatsApp message from {sender}: '{body}' type={msg_type}")

        if not body:
            continue

        upper = body.upper()

        # Owner control commands
        if sender == owner:
            if upper == "AI ON":
                _ai_replies_enabled = True
                send_whatsapp(owner, "AI replies enabled. I will respond to incoming messages.", whapi_token=whapi_token)
            elif upper == "AI OFF":
                _ai_replies_enabled = False
                send_whatsapp(owner, "AI replies disabled.", whapi_token=whapi_token)
            elif upper == "REDIRECT ON":
                _redirect_enabled = True
                send_whatsapp(owner, "Call redirect enabled.", whapi_token=whapi_token)
            elif upper == "REDIRECT OFF":
                _redirect_enabled = False
                send_whatsapp(owner, "Call redirect disabled.", whapi_token=whapi_token)
            else:
                stage = _detect_stage(upper)
                if stage:
                    _handle_done_reply(stage=stage)
            continue

        # Non-owner messages — only act when AI is on
        if _ai_replies_enabled:
            if _redirect_enabled:
                send_whatsapp(sender, REDIRECT_MESSAGE, whapi_token=whapi_token)
            else:
                background_tasks.add_task(
                    _handle_whatsapp_message,
                    phone=sender,
                    body=body,
                    whapi_token=whapi_token,
                    owner_whatsapp=owner,
                    business_name=DEFAULT_CLIENT.get("business_name", ""),
                    business_type=DEFAULT_CLIENT.get("business_type", ""),
                    twenty_api_key=DEFAULT_CLIENT.get("twenty_api_key", ""),
                    twenty_api_url=DEFAULT_CLIENT.get("twenty_api_url", ""),
                )

    return {"status": "success"}


def _detect_stage(text: str) -> str | None:
    """Return a Twenty opportunity stage based on keywords in the owner's message."""
    _MEETING = {"book", "booked", "booking", "meeting", "appointment", "scheduled",
                "confirmed", "done", "sorted", "set up", "arranged"}
    _QUOTE =   {"quote", "quoted", "quoting", "proposal", "price", "pricing",
                "estimate", "send quote", "sent quote"}
    _LOST =    {"cancel", "cancelled", "cancellation", "abandoned", "lost", "dead",
                "not interested", "no show", "noshow", "declined", "gone cold",
                "withdrew", "withdrawn", "no longer"}

    words = set(text.lower().split())
    # Also check multi-word phrases
    phrase = text.lower()

    if any(w in words for w in _MEETING) or any(p in phrase for p in {"set up", "sorted out"}):
        return "MEETING_BOOKED"
    if any(w in words for w in _QUOTE) or "sent quote" in phrase or "send quote" in phrase:
        return "QUOTE_SENT"
    if any(w in words for w in _LOST) or any(p in phrase for p in {"not interested", "no show", "gone cold", "no longer"}):
        return "CLOSED_LOST"
    return None


def _handle_done_reply(stage: str = "MEETING_BOOKED"):
    """Find the most recent CONTACTED opportunity and advance its stage."""
    try:
        # Use default client CRM credentials
        client = DEFAULT_CLIENT
        api_url = client["twenty_api_url"].rstrip("/") + "/graphql"
        headers = {
            "Authorization": f"Bearer {client['twenty_api_key']}",
            "Content-Type": "application/json",
        }

        # Find most recent open opportunity (CONTACTED or MEETING_BOOKED or QUOTE_SENT)
        query = """
        query {
            opportunities(
                filter: { stage: { in: [CONTACTED, MEETING_BOOKED, QUOTE_SENT] } }
                orderBy: { createdAt: DescNullsLast }
                first: 1
            ) {
                edges { node { id name } }
            }
        }
        """
        r = requests.post(api_url, json={"query": query}, headers=headers, timeout=15)
        edges = (r.json().get("data") or {}).get("opportunities", {}).get("edges", [])
        if not edges:
            log.warning("No open opportunity found to update")
            return

        opp_id = edges[0]["node"]["id"]
        opp_name = edges[0]["node"]["name"]

        # Update stage
        mutation = """
        mutation UpdateOpportunity($id: ID!, $input: OpportunityUpdateInput!) {
            updateOpportunity(id: $id, data: $input) { id stage }
        }
        """
        r2 = requests.post(
            api_url,
            json={"query": mutation, "variables": {"id": opp_id, "input": {"stage": stage}}},
            headers=headers,
            timeout=15,
        )
        log.info(f"Opportunity '{opp_name}' updated to {stage}: {r2.json()}")

    except Exception as e:
        log.error(f"handle_done_reply error: {e}")


# ── Telnyx inbound SMS ────────────────────────────────────────────────────────

@app.post("/telnyx-sms")
async def telnyx_sms(request: Request):
    try:
        data = await request.json()
        payload = data.get("data", {}).get("payload", {})
        from_number = (payload.get("from") or {}).get("phone_number", "Unknown")
        to_list = payload.get("to") or []
        to_number = to_list[0].get("phone_number", "Unknown") if to_list else "Unknown"
        text = payload.get("text", "")
        log.info(f"Telnyx SMS received — from={from_number} to={to_number} text={text}")
        # Forward to owner WhatsApp so verification codes are visible instantly
        owner = DEFAULT_CLIENT.get("owner_whatsapp", "")
        if owner and text:
            send_whatsapp(owner, f"📩 *SMS to {to_number}*\nFrom: {from_number}\n\n{text}")
    except Exception as e:
        log.error(f"telnyx-sms error: {e}")
    return {"status": "success"}


# ── WhatsApp event webhook (missed calls / inbound messages) ─────────────────

@app.post("/whatsapp-event")
async def whatsapp_event(request: Request, background_tasks: BackgroundTasks, telnyx: str = None):
    try:
        data = await request.json()
    except Exception as e:
        log.error(f"whatsapp-event parse error: {e}")
        return {"status": "success"}

    log.info(f"whatsapp-event telnyx={telnyx} payload={data}")

    client = CLIENTS.get(telnyx) or DEFAULT_CLIENT
    mode: str = client.get("whatsapp_mode", "off")

    if mode == "off":
        log.info(f"whatsapp_mode=off for {client.get('business_name')}, ignoring")
        return {"status": "success"}

    event_type: str = data.get("event", "")
    call_type: str  = data.get("type", "")
    from_jid: str   = data.get("from", "")
    phone: str      = from_jid.split("@")[0] if "@" in from_jid else from_jid

    whapi_token    = client.get("whapi_token") or os.getenv("WHAPI_TOKEN", "")
    owner_whatsapp = client.get("owner_whatsapp", "")
    business_name  = client.get("business_name", "")
    business_type  = client.get("business_type", "")
    twenty_api_key = client.get("twenty_api_key", "")
    twenty_api_url = client.get("twenty_api_url", "")

    if event_type == "call" and call_type == "missed":
        # Missed calls always trigger auto-reply regardless of AI toggle
        if mode in ("missed_calls_only", "all_messages"):
            background_tasks.add_task(
                _handle_whatsapp_missed_call,
                phone=phone,
                whapi_token=whapi_token,
                owner_whatsapp=owner_whatsapp,
                business_name=business_name,
                business_type=business_type,
                twenty_api_key=twenty_api_key,
                twenty_api_url=twenty_api_url,
            )

    elif event_type == "message":
        # Inbound messages only auto-reply when owner has sent "AI ON"
        if mode == "all_messages" and _ai_replies_enabled:
            body: str = data.get("body", "")
            background_tasks.add_task(
                _handle_whatsapp_message,
                phone=phone,
                body=body,
                whapi_token=whapi_token,
                owner_whatsapp=owner_whatsapp,
                business_name=business_name,
                business_type=business_type,
                twenty_api_key=twenty_api_key,
                twenty_api_url=twenty_api_url,
            )

    return {"status": "success"}


def is_contact_saved(phone: str, whapi_token: str) -> bool:
    """Return True if the number is saved in the WhatsApp contact list (has a custom name)."""
    try:
        if not whapi_token:
            return False
        number = phone.lstrip("+").split("@")[0]
        headers = {"Authorization": f"Bearer {whapi_token}"}
        r = requests.get(
            f"https://gate.whapi.cloud/contacts/{number}@s.whatsapp.net",
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200:
            contact = r.json()
            saved_name = contact.get("name", "")
            push_name  = contact.get("notify", "")
            # A saved contact has a custom name distinct from the push name
            return bool(saved_name and saved_name != push_name)
        return False
    except Exception as e:
        log.error(f"is_contact_saved error: {e}")
        return False


def _handle_whatsapp_missed_call(
    phone, whapi_token, owner_whatsapp, business_name, business_type, twenty_api_key, twenty_api_url
):
    try:
        saved = is_contact_saved(phone, whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_missed_call contact check error: {e}")
        saved = False

    if saved:
        log.info(f"Missed WA call from saved contact {phone} — no action")
        return

    try:
        auto_reply = (
            f"Hi! You just tried calling *{business_name}* via WhatsApp. "
            f"We've noted your call and someone will be in touch with you shortly. "
            f"Alternatively, call us directly and our AI receptionist will take your details right away. 😊"
        )
        send_whatsapp(phone, auto_reply, whapi_token=whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_missed_call auto-reply error: {e}")

    try:
        _create_twenty_contact_from_whatsapp(phone, twenty_api_key, twenty_api_url)
    except Exception as e:
        log.error(f"_handle_whatsapp_missed_call CRM error: {e}")

    try:
        if owner_whatsapp:
            msg = (
                f"📞 *Missed WhatsApp Call — {business_name}*\n\n"
                f"*Number:* +{phone}\n"
                f"_Unknown contact — auto-reply sent & CRM contact created_"
            )
            send_whatsapp(owner_whatsapp, msg, whapi_token=whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_missed_call owner notify error: {e}")


def _handle_whatsapp_message(
    phone, body, whapi_token, owner_whatsapp, business_name, business_type, twenty_api_key, twenty_api_url
):
    try:
        saved = is_contact_saved(phone, whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_message contact check error: {e}")
        saved = False

    if saved:
        log.info(f"WA message from saved contact {phone} — no action")
        return

    try:
        auto_reply = (
            f"Hi! Thanks for reaching out to *{business_name}*. "
            f"We've received your message and someone will be in touch shortly. "
            f"For urgent {business_type} needs, you can also call us directly — "
            f"our AI receptionist is available 24/7. 😊"
        )
        send_whatsapp(phone, auto_reply, whapi_token=whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_message auto-reply error: {e}")

    try:
        _create_twenty_contact_from_whatsapp(phone, twenty_api_key, twenty_api_url)
    except Exception as e:
        log.error(f"_handle_whatsapp_message CRM error: {e}")

    try:
        if owner_whatsapp:
            preview = (body[:120] + "…") if len(body) > 120 else body
            msg = (
                f"💬 *New WhatsApp Message — {business_name}*\n\n"
                f"*From:* +{phone}\n"
                f"*Message:* {preview}\n\n"
                f"_Unknown contact — auto-reply sent & CRM contact created_"
            )
            send_whatsapp(owner_whatsapp, msg, whapi_token=whapi_token)
    except Exception as e:
        log.error(f"_handle_whatsapp_message owner notify error: {e}")


def _create_twenty_contact_from_whatsapp(phone: str, twenty_api_key: str, twenty_api_url: str) -> str | None:
    """Create a minimal CRM contact from a WhatsApp interaction."""
    if not twenty_api_key or not twenty_api_url:
        return None
    try:
        api_url = twenty_api_url.rstrip("/") + "/graphql"
        headers = {
            "Authorization": f"Bearer {twenty_api_key}",
            "Content-Type": "application/json",
        }
        normalised = "+" + phone.lstrip("+")
        search_number = normalised.lstrip("+")
        if search_number.startswith("27") and len(search_number) == 11:
            search_number = search_number[2:]

        existing_id = _find_twenty_person_by_phone(api_url, headers, search_number)
        if existing_id:
            log.info(f"WA CRM contact already exists: {existing_id}")
            return existing_id

        mutation = """
        mutation CreatePerson($input: PersonCreateInput!) {
            createPerson(data: $input) { id }
        }
        """
        variables = {
            "input": {
                "name": {"firstName": "WhatsApp", "lastName": "Lead"},
                "phones": {
                    "primaryPhoneNumber": normalised,
                    "primaryPhoneCountryCode": "ZA",
                    "primaryPhoneCallingCode": "+27",
                },
            }
        }
        r = requests.post(api_url, json={"query": mutation, "variables": variables}, headers=headers, timeout=15)
        result = r.json()
        if result.get("errors"):
            log.error(f"WA CRM create contact errors: {result['errors']}")
        person_id = (result.get("data") or {}).get("createPerson", {}).get("id")
        log.info(f"WA CRM contact created: {person_id}")
        return person_id
    except Exception as e:
        log.error(f"_create_twenty_contact_from_whatsapp error: {e}")
        return None


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "Allterra AI Webhook"}


# ── Main webhook ──────────────────────────────────────────────────────────────

@app.post("/call-ended")
async def call_ended(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse request body: {e}")
        return {"status": "success"}

    # ── Only process call_analyzed — it fires after AI extraction is complete ──
    event = data.get("event", "")
    if event != "call_analyzed":
        log.info(f"Ignoring event: {event}")
        return {"status": "success"}

    # ── Filter out error calls (duplicate SIP leg shows as error) ─────────────
    call_status = (data.get("call") or data).get("call_status", "")
    if call_status == "error":
        log.info(f"Ignoring error call: {call_status}")
        return {"status": "success"}

    call: dict = data.get("call") or data
    call_id: str = call.get("call_id", "")
    log.info(f"call_id={call_id} call_type={call.get('call_type')} direction={call.get('direction')} from={call.get('from_number')} to={call.get('to_number')}")

    # Deduplicate on from+to — blocks both duplicate calls since they share the same numbers
    dedup_key = f"{call.get('from_number')}>{call.get('to_number')}"
    now = _time.time()
    if dedup_key in _recent_calls and now - _recent_calls[dedup_key] < _DEDUP_WINDOW:
        log.info(f"Duplicate ignored (same from/to within {_DEDUP_WINDOW}s): {dedup_key}")
        return {"status": "success"}
    _recent_calls[dedup_key] = now
    if len(_recent_calls) > 1000:
        _recent_calls.clear()

    from_number: str = _normalise_za_number(call.get("from_number", ""))
    to_number: str = _normalise_za_number(call.get("to_number", ""))
    metadata: dict = call.get("metadata") or {}
    analysis: dict = call.get("call_analysis") or {}

    log.info(f"call_analysis: {analysis}")

    # ── Resolve client config from to_number ─────────────────────────────────
    client = get_client(to_number, metadata)
    owner_whatsapp: str = client.get("owner_whatsapp", "")
    business_name: str = client["business_name"]
    telnyx_from: str = client["telnyx_from_number"]
    twenty_api_key: str = client["twenty_api_key"] or ""
    twenty_api_url: str = client["twenty_api_url"]
    whapi_token: str = client.get("whapi_token") or os.getenv("WHAPI_TOKEN", "")

    # ── Custom analysis fields — Retell puts them under custom_analysis_data ──
    # Strip whitespace from keys in case of accidental spaces in Retell config
    raw_custom = analysis.get("custom_analysis_data") or analysis
    custom: dict = {k.strip(): v for k, v in raw_custom.items()}

    caller_name: str = custom.get("caller_name") or "Unknown"
    property_address: str = custom.get("property_address") or "Not provided"
    job_description: str = custom.get("job_description") or "Not provided"
    urgency: str = custom.get("urgency") or "Standard"
    callback_time: str = custom.get("callback_time") or "as soon as possible"
    is_emergency: bool = bool(custom.get("is_emergency", False))
    call_summary: str = analysis.get("call_summary") or custom.get("call_summary") or ""

    urgency_label = "EMERGENCY" if is_emergency else urgency

    log.info(f"Call ended — {caller_name} ({from_number}) | {business_name} | {urgency_label}")

    # ── Return 200 immediately, process in background to avoid Retell timeout ─
    background_tasks.add_task(
        process_call,
        caller_name=caller_name,
        from_number=from_number,
        owner_whatsapp=owner_whatsapp,
        business_name=business_name,
        telnyx_from=telnyx_from,
        property_address=property_address,
        job_description=job_description,
        callback_time=callback_time,
        call_summary=call_summary,
        urgency_label=urgency_label,
        twenty_api_key=twenty_api_key,
        twenty_api_url=twenty_api_url,
        whapi_token=whapi_token,
    )

    return {"status": "success"}


def process_call(
    caller_name, from_number, owner_whatsapp, business_name, telnyx_from,
    property_address, job_description, callback_time, call_summary, urgency_label,
    twenty_api_key, twenty_api_url, whapi_token=None,
):
    if owner_whatsapp and whapi_token:
        wa_message = (
            f"*INCOMING CALL — {business_name}* [{urgency_label}]\n\n"
            f"*Name:* {caller_name}\n"
            f"*Number:* {from_number}\n"
            f"*Address:* {property_address}\n"
            f"*Job:* {job_description}\n"
            f"*Callback:* {callback_time}\n\n"
            f"*Summary:* {call_summary}\n\n"
            f"_Reply DONE when contacted_"
        )
        send_whatsapp(owner_whatsapp, wa_message, whapi_token=whapi_token)
    else:
        log.info(f"WhatsApp notification skipped for {business_name} — no whapi_token configured")

    sms_text = (
        f"Hi {caller_name} — thanks for calling {business_name}! "
        f"We have your details and will call you back {callback_time}."
    )
    send_sms(from_number, sms_text, telnyx_from)

    create_crm_contact_and_task(
        name=caller_name,
        phone=from_number,
        address=property_address,
        job=job_description,
        urgency=urgency_label,
        summary=call_summary,
        callback_time=callback_time,
        twenty_api_key=twenty_api_key,
        twenty_api_url=twenty_api_url,
    )


# ── WhatsApp AI reply ─────────────────────────────────────────────────────────

WHATSAPP_AI_SYSTEM = """You are a friendly and professional AI assistant for Allterra AI, \
a South African company that provides AI voice agent solutions for businesses. \
You help answer questions from leads and clients over WhatsApp. \
Keep replies concise and conversational — this is WhatsApp, not email. \
If someone wants to book a meeting or get a quote, encourage them to do so and let them know the team will follow up. \
Never make up pricing or specific technical details you don't know — offer to have someone from the team reach out instead."""


def _ai_whatsapp_reply(sender: str, message: str) -> str | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — skipping AI reply")
        return None
    try:
        history = _conversations.setdefault(sender, [])
        history.append({"role": "user", "content": message})
        # Keep last 20 messages to avoid excessive token usage
        if len(history) > 20:
            history[:] = history[-20:]

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=WHATSAPP_AI_SYSTEM,
            messages=history,
        )
        reply = response.content[0].text.strip()
        history.append({"role": "assistant", "content": reply})
        log.info(f"AI reply to {sender}: {reply}")
        return reply
    except Exception as e:
        log.error(f"AI reply error: {e}")
        return None


# ── WhatsApp via Whapi ────────────────────────────────────────────────────────

def send_whatsapp(to: str, message: str, whapi_token: str = None):
    token = whapi_token or os.getenv("WHAPI_TOKEN")
    try:
        if not to:
            log.warning("send_whatsapp: no recipient number, skipping")
            return
        if not token:
            log.warning("send_whatsapp: no Whapi token available, skipping")
            return
        # Normalise to bare digits — Whapi expects "27831234567@s.whatsapp.net"
        number = to.lstrip("+").split("@")[0]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {"to": f"{number}@s.whatsapp.net", "body": message}
        r = requests.post(
            "https://gate.whapi.cloud/messages/text",
            json=payload,
            headers=headers,
            timeout=10,
        )
        log.info(f"WhatsApp → {number}: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"WhatsApp error: {e}")


# ── SMS via Telnyx ────────────────────────────────────────────────────────────

def send_sms(to: str, message: str, from_number: str):
    try:
        if not to or to == "Unknown":
            log.warning("send_sms: no recipient number, skipping")
            return
        if not from_number:
            log.warning("send_sms: no sender number configured, skipping")
            return
        headers = {
            "Authorization": f"Bearer {TELNYX_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {"from": from_number, "to": to, "text": message}
        r = requests.post(
            "https://api.telnyx.com/v2/messages",
            json=payload,
            headers=headers,
            timeout=10,
        )
        log.info(f"SMS → {to}: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"SMS error: {e}")


# ── Twenty CRM via GraphQL ────────────────────────────────────────────────────

def create_crm_contact_and_task(
    name: str,
    phone: str,
    address: str,
    job: str,
    urgency: str,
    summary: str,
    callback_time: str,
    twenty_api_key: str,
    twenty_api_url: str,
):
    try:
        api_url = twenty_api_url.rstrip("/") + "/graphql"
        headers = {
            "Authorization": f"Bearer {twenty_api_key}",
            "Content-Type": "application/json",
        }

        name_parts = name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Create person / contact
        person_id = _create_twenty_person(
            api_url, headers, first_name, last_name, phone, address
        )

        # Create opportunity
        opportunity_id = _create_twenty_opportunity(
            api_url, headers, first_name, job, summary, person_id
        )

        # Create follow-up task linked to both person and opportunity
        _create_twenty_task(
            api_url, headers, first_name, urgency, job, address, callback_time, summary, person_id
        )

    except Exception as e:
        log.error(f"Twenty CRM error: {e}")


def _create_twenty_person(
    api_url: str,
    headers: dict,
    first_name: str,
    last_name: str,
    phone: str,
    address: str,
) -> str | None:
    try:
        # Twenty stores numbers without the calling code — strip +27 for search
        search_number = phone.lstrip("+")
        if search_number.startswith("27") and len(search_number) == 11:
            search_number = search_number[2:]  # +27837088951 → 837088951

        # Check if person already exists
        existing_id = _find_twenty_person_by_phone(api_url, headers, search_number)
        if existing_id:
            log.info(f"Twenty existing person found: {existing_id}")
            # Update name if we now have it
            if first_name and first_name != "Unknown":
                _update_twenty_person_name(api_url, headers, existing_id, first_name, last_name)
            return existing_id

        # Create new person
        mutation = """
        mutation CreatePerson($input: PersonCreateInput!) {
            createPerson(data: $input) {
                id
            }
        }
        """
        variables = {
            "input": {
                "name": {"firstName": first_name, "lastName": last_name},
                "phones": {
                    "primaryPhoneNumber": phone,
                    "primaryPhoneCountryCode": "ZA",
                    "primaryPhoneCallingCode": "+27",
                },
                "city": address,
            }
        }
        r = requests.post(
            api_url,
            json={"query": mutation, "variables": variables},
            headers=headers,
            timeout=15,
        )
        result = r.json()
        if result.get("errors"):
            log.error(f"Twenty createPerson errors: {result['errors']}")
        person_id = (result.get("data") or {}).get("createPerson", {}).get("id")
        log.info(f"Twenty person created: {person_id}")
        return person_id
    except Exception as e:
        log.error(f"Twenty create person error: {e}")
        return None


def _find_twenty_person_by_phone(api_url: str, headers: dict, phone: str) -> str | None:
    try:
        query = """
        query FindPerson($filter: PersonFilterInput!) {
            people(filter: $filter, first: 1,
                   orderBy: { createdAt: AscNullsLast }) {
                edges { node { id name { firstName } } }
            }
        }
        """
        r = requests.post(
            api_url,
            json={"query": query, "variables": {
                "filter": {"phones": {"primaryPhoneNumber": {"eq": phone}}}
            }},
            headers=headers,
            timeout=15,
        )
        result = r.json()
        if result.get("errors"):
            log.error(f"Twenty findPerson errors: {result['errors']}")
        edges = ((result.get("data") or {}).get("people") or {}).get("edges", [])
        if edges:
            return edges[0]["node"]["id"]
        return None
    except Exception as e:
        log.error(f"Twenty find person error: {e}")
        return None


def _update_twenty_person_name(
    api_url: str, headers: dict, person_id: str, first_name: str, last_name: str
):
    try:
        mutation = """
        mutation UpdatePerson($id: ID!, $input: PersonUpdateInput!) {
            updatePerson(id: $id, data: $input) { id }
        }
        """
        requests.post(
            api_url,
            json={"query": mutation, "variables": {
                "id": person_id,
                "input": {"name": {"firstName": first_name, "lastName": last_name}},
            }},
            headers=headers,
            timeout=15,
        )
        log.info(f"Twenty person name updated: {person_id}")
    except Exception as e:
        log.error(f"Twenty update person error: {e}")


def _create_twenty_opportunity(
    api_url: str,
    headers: dict,
    first_name: str,
    job: str,
    summary: str,
    person_id: str | None,
) -> str | None:
    try:
        mutation = """
        mutation CreateOpportunity($input: OpportunityCreateInput!) {
            createOpportunity(data: $input) {
                id
            }
        }
        """
        opp_input: dict = {
            "name": f"{first_name} — {job[:60]}",
            "stage": "CONTACTED",
        }
        if person_id:
            opp_input["pointOfContactId"] = person_id

        r = requests.post(
            api_url,
            json={"query": mutation, "variables": {"input": opp_input}},
            headers=headers,
            timeout=15,
        )
        result = r.json()
        if result.get("errors"):
            log.error(f"Twenty createOpportunity errors: {result['errors']}")
        opp_id = (result.get("data") or {}).get("createOpportunity", {}).get("id")
        log.info(f"Twenty opportunity created: {opp_id}")

        if opp_id and summary:
            _create_twenty_note(api_url, headers, summary, opp_id, person_id)

        return opp_id
    except Exception as e:
        log.error(f"Twenty create opportunity error: {e}")
        return None


def _create_twenty_note(
    api_url: str,
    headers: dict,
    summary: str,
    opp_id: str,
    person_id: str | None,
):
    try:
        # Create note
        note_mutation = """
        mutation CreateNote($input: NoteCreateInput!) {
            createNote(data: $input) {
                id
            }
        }
        """
        r = requests.post(
            api_url,
            json={
                "query": note_mutation,
                "variables": {
                    "input": {
                        "title": "Call Summary",
                        "bodyV2": {"markdown": summary, "blocknote": None},
                    }
                },
            },
            headers=headers,
            timeout=15,
        )
        note_id = r.json().get("data", {}).get("createNote", {}).get("id")
        log.info(f"Twenty note created: {note_id}")

        if not note_id:
            return

        # Link note to opportunity (and person if available)
        target_mutation = """
        mutation CreateNoteTarget($input: NoteTargetCreateInput!) {
            createNoteTarget(data: $input) {
                id
            }
        }
        """
        target_input: dict = {"noteId": note_id, "targetOpportunityId": opp_id}
        requests.post(
            api_url,
            json={"query": target_mutation, "variables": {"input": target_input}},
            headers=headers,
            timeout=15,
        )
        if person_id:
            target_input_person: dict = {"noteId": note_id, "targetPersonId": person_id}
            requests.post(
                api_url,
                json={"query": target_mutation, "variables": {"input": target_input_person}},
                headers=headers,
                timeout=15,
            )
        log.info("Twenty note linked to opportunity and person")
    except Exception as e:
        log.error(f"Twenty create note error: {e}")


def _create_twenty_task(
    api_url: str,
    headers: dict,
    first_name: str,
    urgency: str,
    job: str,
    address: str,
    callback_time: str,
    summary: str,
    person_id: str | None,
):
    try:
        mutation = """
        mutation CreateTask($input: TaskCreateInput!) {
            createTask(data: $input) {
                id
            }
        }
        """
        markdown_body = (
            f"**Job:** {job}\n\n"
            f"**Address:** {address}\n\n"
            f"**Callback:** {callback_time}\n\n"
            f"**Summary:** {summary}"
        )
        task_input: dict = {
            "title": f"Call back {first_name} — {urgency}",
            "status": "TODO",
            "bodyV2": {"markdown": markdown_body, "blocknote": None},
        }

        r = requests.post(
            api_url,
            json={"query": mutation, "variables": {"input": task_input}},
            headers=headers,
            timeout=15,
        )
        result = r.json()
        task_id = result.get("data", {}).get("createTask", {}).get("id")
        log.info(f"Twenty task created: {task_id}")

        # Link task to person via separate mutation
        if task_id and person_id:
            _link_task_to_person(api_url, headers, task_id, person_id)

    except Exception as e:
        log.error(f"Twenty create task error: {e}")


def _link_task_to_person(api_url: str, headers: dict, task_id: str, person_id: str):
    try:
        mutation = """
        mutation CreateTaskTarget($input: TaskTargetCreateInput!) {
            createTaskTarget(data: $input) {
                id
            }
        }
        """
        r = requests.post(
            api_url,
            json={
                "query": mutation,
                "variables": {"input": {"taskId": task_id, "targetPersonId": person_id}},
            },
            headers=headers,
            timeout=15,
        )
        result = r.json()
        link_id = result.get("data", {}).get("createTaskTarget", {}).get("id")
        log.info(f"Twenty task linked to person: {link_id}")
    except Exception as e:
        log.error(f"Twenty link task error: {e}")
