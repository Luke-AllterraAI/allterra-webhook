from fastapi import FastAPI, Request, BackgroundTasks
import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Allterra AI Webhook")

# Deduplication — prevents double-processing if Retell sends the event twice
_processed_calls: set[str] = set()

# Default credentials — overridden per-client via Retell metadata
WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
DEFAULT_TELNYX_FROM = os.getenv("TELNYX_FROM_NUMBER")
TWENTY_API_KEY = os.getenv("TWENTY_API_KEY")
TWENTY_BASE_URL = os.getenv("TWENTY_API_URL", "https://api.twenty.com")
DEFAULT_OWNER_WHATSAPP = os.getenv("OWNER_WHATSAPP")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise_za_number(number: str) -> str:
    """Convert 08xxxxxxxx → +2783xxxxxxxx. Leave +27 and other countries alone."""
    n = number.strip()
    if n.startswith("0") and len(n) == 10:
        return "+27" + n[1:]
    if n and not n.startswith("+"):
        return "+" + n
    return n or "Unknown"


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

    # ── Deduplicate — ignore if we already processed this call ───────────────
    call: dict = data.get("call") or data
    call_id: str = call.get("call_id", "")
    log.info(f"call_id={call_id} call_type={call.get('call_type')} direction={call.get('direction')} from={call.get('from_number')} to={call.get('to_number')}")
    if call_id and call_id in _processed_calls:
        log.info(f"Duplicate call_analyzed ignored: {call_id}")
        return {"status": "success"}
    if call_id:
        _processed_calls.add(call_id)
        if len(_processed_calls) > 1000:
            _processed_calls.clear()

    from_number: str = _normalise_za_number(call.get("from_number", ""))
    metadata: dict = call.get("metadata") or {}
    analysis: dict = call.get("call_analysis") or {}

    # Log analysis so we can see exactly what Retell extracted
    log.info(f"call_analysis: {analysis}")

    # ── Multi-client: metadata wins, env vars are the fallback ───────────────
    owner_whatsapp: str = metadata.get("owner_whatsapp") or DEFAULT_OWNER_WHATSAPP or ""
    business_name: str = metadata.get("business_name") or "Allterra AI"
    telnyx_from: str = metadata.get("telnyx_from_number") or DEFAULT_TELNYX_FROM or ""

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
    )

    return {"status": "success"}


def process_call(
    caller_name, from_number, owner_whatsapp, business_name, telnyx_from,
    property_address, job_description, callback_time, call_summary, urgency_label,
):
    wa_message = (
        f"*NEW LEAD — {business_name}* [{urgency_label}]\n\n"
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
):
    try:
        api_url = TWENTY_BASE_URL.rstrip("/") + "/graphql"
        headers = {
            "Authorization": f"Bearer {TWENTY_API_KEY}",
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
