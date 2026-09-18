"""Dental voice agent: CRM, scheduler, call tasks, Higgs Realtime calls.

Every route except /health needs a Firebase (Google sign-in) ID token for an email in VOICE_AGENT_ALLOWED_EMAILS.
Routes are thin; logic lives in application/voice_agent/.
"""

import asyncio
import logging
import os

import jwt as pyjwt
from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel

from app import calls, campaigns, dashboard, db, phone, scheduling as sch
from app.agent_files import FILE_NAMES

logger = logging.getLogger(__name__)

ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.getenv("VOICE_AGENT_ALLOWED_EMAILS", "").split(",")
    if e.strip()
}


SITE_PROJECTS = {p.strip() for p in os.getenv("FIREBASE_PROJECT_ID", "").split(",")
                 if p.strip()}


def _site_user(token: str) -> dict | None:
    """Verified user from a website-project Firebase ID token, or None if the token is for another audience."""
    try:
        aud = pyjwt.decode(token, options={"verify_signature": False}).get("aud")
    except pyjwt.PyJWTError:
        return None
    if aud not in SITE_PROJECTS:
        return None
    try:
        claims = google_id_token.verify_firebase_token(token, google_requests.Request(), audience=aud)
    except Exception as e:  # noqa: BLE001 - any verification failure is a 401
        logger.warning("voice-agent: site token rejected: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not claims or not claims.get("email_verified"):
        raise HTTPException(status_code=403, detail="Not authorized for the voice agent")
    return {"uid": claims.get("sub"), "email": claims.get("email", ""), "name": claims.get("name", ""),
            "picture": claims.get("picture", "")}


async def require_owner(authorization: str | None = Header(None)) -> dict:
    """401 without a valid token, 403 for any account but the allowlisted one."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    user = _site_user(authorization[7:])
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if (user.get("email") or "").lower() not in ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="Not authorized for the voice agent")
    return user


public_router = APIRouter(prefix="/voice-agent", tags=["voice-agent"])
router = APIRouter(prefix="/voice-agent", tags=["voice-agent"], dependencies=[Depends(require_owner)])


def _bad(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@public_router.get("/health")
def health():
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(require_owner)):
    with db.connect() as conn:
        office = db.row(conn, "SELECT * FROM office WHERE id=1")
    return {"email": user.get("email"), "name": user.get("name"), "picture": user.get("picture"),
            "plan": "Pro (hackathon)", "office": office}


@router.get("/dashboard")
def get_dashboard():
    with db.connect() as conn:
        return dashboard.summary(conn)


@router.get("/providers")
def get_providers():
    with db.connect() as conn:
        return {"providers": list(sch.providers(conn).values()), "procedures": list(sch.procedures(conn).values())}


@router.get("/appointments")
def get_appointments(start: str, end: str):
    with db.connect() as conn:
        out = db.rows(conn, """SELECT a.*, p.name AS patient, p.phone FROM appointments a
                               JOIN patients p ON p.id=a.patient_id WHERE a.start>=? AND a.start<? ORDER BY a.start""",
                      (start, end))
        return {"appointments": out, "gaps": sch.gaps(conn, 5)}


class CancelBody(BaseModel):
    reason: str = ""
    by: str = "patient"  # patient | office
    queue_gap_fill: bool = True


@router.post("/appointments/{appt_id}/cancel")
def cancel_appointment(appt_id: int, body: CancelBody):
    with db.connect() as conn:
        try:
            status = "cancelled_office" if body.by == "office" else "cancelled"
            appt = sch.cancel(conn, appt_id, body.reason or f"{body.by} cancelled", status)
        except ValueError as e:
            _bad(e)
        queued = []
        if body.queue_gap_fill and appt["start"] > db.iso(db.now_local()):
            queued = campaigns.queue_gap_fill(conn, slot_id=f"{appt['provider_id']}@{appt['start']}", max_tasks=1)
        return {"appointment": appt, "gap_fill_tasks": queued}


@router.delete("/appointments/{appt_id}")
def delete_appointment(appt_id: int):
    """Hard delete (used to undo test bookings)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
    return {"ok": True}


class DoctorCancelBody(BaseModel):
    provider_id: int
    date: str
    start_hour: int = 0
    end_hour: int = 24


@router.post("/doctor-cancel")
def doctor_cancel(body: DoctorCancelBody):
    with db.connect() as conn:
        try:
            return campaigns.doctor_cancel(conn, body.provider_id, body.date, body.start_hour, body.end_hour)
        except (ValueError, KeyError) as e:
            _bad(e)


@router.get("/patients")
def list_patients():
    with db.connect() as conn:
        ps = sch.patient_status(conn)
    for p in ps:
        p["dialable"] = phone.dialable(p.get("phone", ""))
    return {"patients": ps}


@router.get("/patients/{pid}")
def get_patient(pid: int):
    with db.connect() as conn:
        p = sch.patient_detail(conn, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    p["dialable"] = phone.dialable(p.get("phone", ""))
    return p


class MemoryBody(BaseModel):
    memory: str = ""
    notes: str | None = None


@router.put("/patients/{pid}/memory")
def put_memory(pid: int, body: MemoryBody):
    with db.connect() as conn:
        conn.execute("UPDATE patients SET memory=?, notes=COALESCE(?, notes) WHERE id=?",
                     (body.memory[:4000], body.notes, pid))
    return {"ok": True}


@router.get("/waitlist")
def get_waitlist():
    with db.connect() as conn:
        return {"waitlist": sch.waitlist(conn)}


@router.delete("/waitlist/{pid}")
def remove_from_waitlist(pid: int):
    with db.connect() as conn:
        conn.execute("UPDATE patients SET waitlist=0 WHERE id=?", (pid,))
    return {"ok": True}


@router.get("/files")
def get_files():
    with db.connect() as conn:
        return {"files": db.rows(conn, "SELECT * FROM agent_files ORDER BY name")}


class FileBody(BaseModel):
    content: str


@router.put("/files/{name}")
def put_file(name: str, body: FileBody):
    if name not in FILE_NAMES:
        raise HTTPException(status_code=404, detail="Unknown agent file")
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO agent_files VALUES (?,?,?)",
                     (name, body.content[:20000], db.iso(db.now_local())))
    return {"ok": True}


@router.get("/settings")
def get_settings():
    with db.connect() as conn:
        return {"voice": calls.get_voice(conn), "voices": list(calls.VOICES)}


class SettingsBody(BaseModel):
    voice: str


@router.put("/settings")
def put_settings(body: SettingsBody):
    with db.connect() as conn:
        try:
            return {"voice": calls.set_voice(conn, body.voice)}
        except ValueError as e:
            _bad(e)


@router.get("/tasks")
def get_tasks(include_done: bool = False):
    with db.connect() as conn:
        return {"tasks": campaigns.list_tasks(conn, include_done)}


class CampaignBody(BaseModel):
    limit: int = 10
    business_days: int = 3


@router.post("/campaigns/recall")
def run_recall(body: CampaignBody = CampaignBody()):
    with db.connect() as conn:
        ids = campaigns.queue_recall(conn, body.limit)
    return {"queued": len(ids), "task_ids": ids}


@router.post("/campaigns/gap-fill")
def run_gap_fill(body: CampaignBody = CampaignBody()):
    with db.connect() as conn:
        created = campaigns.queue_gap_fill(conn, body.business_days, min(body.limit, 10))
    return {"queued": len(created), "tasks": created}


@router.post("/tasks/{task_id}/skip")
def skip_task(task_id: int):
    with db.connect() as conn:
        campaigns.set_task(conn, task_id, "skipped")
    return {"ok": True}


@router.delete("/tasks")
def clear_tasks():
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET status='skipped' WHERE status IN ('queued','in_progress')")
    return {"ok": True}


class StartCallBody(BaseModel):
    patient_id: int | None = None
    task_id: int | None = None
    text_mode: bool = False
    mode: str = "browser"  # browser | phone


@router.post("/calls")
def start_call(body: StartCallBody):
    """Create the call log row and either mint a Higgs client secret (browser) or dial via Twilio (phone)."""
    with db.connect() as conn:
        pid = body.patient_id
        if body.task_id:
            t = campaigns.task(conn, body.task_id)
            if not t:
                raise HTTPException(status_code=404, detail="Task not found")
            pid = t["patient_id"]
        if not pid:
            raise HTTPException(status_code=400, detail="patient_id or task_id required")
        if body.mode == "phone":
            return _start_phone_call(conn, pid, body.task_id)
        try:
            call = calls.start(conn, pid, body.task_id, body.text_mode)
        except ValueError as e:
            _bad(e)
    try:
        call["client_secret"] = calls.mint_client_secret(call["session"])
    except Exception as e:  # noqa: BLE001 - surface upstream failure to the UI
        logger.warning("voice-agent: client secret mint failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Could not start the voice session: {e}")
    call["ws_url"] = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
    return call


def _start_phone_call(conn, pid: int, task_id: int | None) -> dict:
    p = db.row(conn, "SELECT phone FROM patients WHERE id=?", (pid,))
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    if not phone.dialable(p["phone"]):
        raise HTTPException(status_code=403, detail="Real phone calls are limited to the demo number. "
                                                    "Use a browser call for this patient.")
    call = calls.start(conn, pid, task_id, mode="phone")
    call.pop("session", None)
    conn.commit()
    try:
        call["phone"] = phone.place_call(conn, call["call_id"], p["phone"])
    except Exception as e:  # noqa: BLE001 - surface Twilio failure to the UI and close the log row
        logger.warning("voice-agent: phone dial failed: %s", e)
        calls.finish(conn, call["call_id"], [], 0)
        conn.commit()
        raise HTTPException(status_code=502, detail=f"Could not place the call: {e}")
    return call


@router.post("/calls/{call_id}/hangup")
def hangup_phone_call(call_id: int):
    try:
        phone.hangup(call_id)
    except Exception as e:  # noqa: BLE001
        _bad(e)
    return {"ok": True}


@public_router.websocket("/phone/stream/{token}")
async def phone_stream(ws: WebSocket, token: str):
    """Twilio Media Streams endpoint. Auth = the single-use token baked into this call's TwiML."""
    with db.connect() as conn:
        call_id = phone.claim(conn, token)
        session = phone.phone_session(conn, call_id) if call_id else None
    if not call_id:
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        higgs = await phone.connect_higgs()
    except Exception as e:  # noqa: BLE001
        logger.warning("voice-agent phone %s: higgs connect failed: %s", call_id, e)
        phone.set_status(call_id, "failed")
        await ws.close()
        return
    try:
        await phone.Bridge(call_id, ws, higgs, session).run()
    except Exception:  # noqa: BLE001 - the call is over either way; keep the log row
        logger.exception("voice-agent phone %s: bridge crashed", call_id)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - already closed by Twilio
            pass
        try:  # closing the stream should end the call; make sure the line is actually released
            await asyncio.to_thread(phone.hangup, call_id)
        except Exception as e:  # noqa: BLE001 - already completed
            logger.info("voice-agent phone %s: hangup after bridge: %s", call_id, e)


class ToolBody(BaseModel):
    name: str
    arguments: dict = {}


@router.post("/calls/{call_id}/tool")
def call_tool(call_id: int, body: ToolBody):
    with db.connect() as conn:
        try:
            return calls.run_tool(conn, call_id, body.name, body.arguments)
        except ValueError as e:
            _bad(e)


class EndBody(BaseModel):
    transcript: list = []
    duration_s: float | None = None


@router.post("/calls/{call_id}/end")
def end_call(call_id: int, body: EndBody):
    with db.connect() as conn:
        try:
            return calls.finish(conn, call_id, body.transcript, body.duration_s)
        except ValueError as e:
            _bad(e)


@router.get("/calls/{call_id}")
def get_call(call_id: int):
    with db.connect() as conn:
        c = calls.get(conn, call_id)
        if c:
            c["phone"] = db.row(conn, "SELECT to_number, status FROM phone_calls WHERE call_id=?", (call_id,))
    if not c:
        raise HTTPException(status_code=404, detail="Call not found")
    return c


@router.delete("/calls/{call_id}")
def delete_call(call_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM unmet_demand WHERE call_id=?", (call_id,))
        conn.execute("UPDATE tasks SET call_id=NULL WHERE call_id=?", (call_id,))
        conn.execute("DELETE FROM calls WHERE id=?", (call_id,))
    return {"ok": True}


@router.post("/reset")
def reset_demo():
    db.reset()
    return {"ok": True}
