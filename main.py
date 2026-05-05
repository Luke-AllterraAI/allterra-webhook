from fastapi import FastAPI, Request, BackgroundTasks
import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Allterra AI Webhook")

# Deduplication — keyed by (from_number, to_number), value is timestamp
# Retell sometimes creates two call objects for one conversation with different call_ids
import time as _time
_recent_calls: dict[str, float] = {}
_DEDUP_WINDOW = 60  # seconds

# Shared credentials
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")

# ── Client config — keyed by the Telnyx number callers dial (to_number) ──────
# Add a new entry here for each client you onboard.
# twenty_api_key / twenty_api_url can be per-client or fall back to env vars.
CLIENTS: dict[str, dict] = {
    "+27600716833": {
        "business_name": "Allterra AI",
        "owner_whatsapp": "27837088951",
        "telnyx_from_number": "+27600716833",
        "twenty_api_key": os.getenv("TWENTY_API_KEY"),
        "twenty_api_url": os.getenv("TWENTY_API_URL", "https://api.twenty.com"),
    },
}

# Fallback for unknown numbers
DEFAULT_CLIENT: dict = {
    "business_name": "Allterra AI",
    "owner_whatsapp": os.getenv("OWNER_WHATSAPP", "27837088951"),
    "telnyx_from_number": os.getenv("TELNYX_FROM_NUMBER", ""),
    "twenty_api_key": os.getenv("TWENTY_API_KEY"),
    "twenty_api_url": os.getenv("TWENTY_API_URL", "https://api.twenty.com"),
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
async def whatsapp_reply(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        log.error(f"whatsapp-reply parse error: {e}")
        return {"status": "success"}

    log.info(f"whatsapp-reply payload: {data}")

    # Whapi sends messages in a few possible formats — handle all
    messages = data.get("messages") or []
    if not messages:
        msg = data.get("message") or data
        messages = [msg]

    for msg in messages:
        # Extract body from text.body or body directly
        body: str = ""
        if isinstance(msg.get("text"), dict):
            body = msg["text"].get("body", "")
        else:
            body = msg.get("body", "") or msg.get("text", "")

        body = body.strip().upper()
        log.info(f"WhatsApp reply received: '{body}'")

        if body == "DONE":
            _handle_done_reply()
        elif body == "BOOKED":
            _handle_done_reply(stage="MEETING_BOOKED")
        elif body == "QUOTE":
            _handle_done_reply(stage="QUOTE_SENT")

    return {"status": "success"}


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

        # Find most recent CONTACTED opportunity
        query = """
        query {
            opportunities(
                filter: { stage: { eq: CONTACTED } }
                orderBy: { createdAt: DescNullsLast }
                first: 1
            ) {
                edges { node { id name } }
            }
        }
        """
        r = requests.post(api_url, json={"query": query}, headers=headers, timeout=15)
        edges = r.json().get("data", {}).get("opportunities", {}).get("edges", [])
        if not edges:
            log.warning("No CONTACTED opportunity found to update")
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

    # ── Deduplicate on from+to within 60s (Retell creates 2 call_ids per call) ─
    call: dict = data.get("call") or data
    call_id: str = call.get("call_id", "")
    log.info(f"call_id={call_id} call_type={call.get('call_type')} direction={call.get('direction')} from={call.get('from_number')} to={call.get('to_number')}")

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
    owner_whatsapp: str = client["owner_whatsapp"]
    business_name: str = client["business_name"]
    telnyx_from: str = client["telnyx_from_number"]
    twenty_api_key: str = client["twenty_api_key"] or ""
    twenty_api_url: str = client["twenty_api_url"]

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
    )

    return {"status": "success"}


def process_call(
    caller_name, from_number, owner_whatsapp, business_name, telnyx_from,
    property_address, job_description, callback_time, call_summary, urgency_label,
    twenty_api_key, twenty_api_url,
):
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
    send_whatsapp(owner_whatsapp, wa_message)

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


# ── WhatsApp via Whapi ────────────────────────────────────────────────────────

def send_whatsapp(to: str, message: str):
    try:
        if not to:
            log.warning("send_whatsapp: no recipient number, skipping")
            return
        # Normalise to bare digits — Whapi expects "27831234567@s.whatsapp.net"
        number = to.lstrip("+").split("@")[0]
        headers = {
            "Authorization": f"Bearer {WHAPI_TOKEN}",
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
        person_id = result.get("data", {}).get("createPerson", {}).get("id")
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
                   orderBy: { createdAt: { direction: AscNullsLast } }) {
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
        edges = r.json().get("data", {}).get("people", {}).get("edges", [])
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
        opp_id = result.get("data", {}).get("createOpportunity", {}).get("id")
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
