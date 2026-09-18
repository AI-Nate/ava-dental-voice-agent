"""A call: session config (instructions + tools), tool execution, call log."""

import json
import os
import re
from datetime import datetime, timedelta

import httpx

from . import campaigns, confirm_email, scheduling as sch
from .agent_files import FILE_NAMES, callback_line, opening
from .db import iso, now_local, row, rows

BOSON_BASE_URL = os.getenv("BOSON_BASE_URL", "https://api.boson.ai")
MODEL = "higgs-realtime"
OUTCOMES = ["booked", "rescheduled", "declined", "waitlisted", "callback", "voicemail", "wrong_person", "no_answer",
            "other"]
VOICES = ("default", "chloe", "eleanor", "nora", "oliver", "marcus", "jake")
DEFAULT_VOICE = "eleanor"
UNGATED = {"verify_identity", "end_call"}  # every other tool needs a confirmed identity first

TOOLS = [
    {"type": "function", "name": "verify_identity",
     "description": "Call as soon as the person confirms they are the patient. Unlocks the patient's details and "
                    "the reason for the call. Nothing about the patient may be shared before this returns ok: true.",
     "parameters": {"type": "object", "properties": {
         "method": {"type": "string", "enum": ["name", "dob", "phone_last4"],
                    "description": "name: they confirmed their full name. dob / phone_last4: they gave it to confirm."},
         "value": {"type": "string", "description": "Date of birth as YYYY-MM-DD, or the last 4 phone digits."}},
         "required": ["method"]}},
    {"type": "function", "name": "get_available_slots",
     "description": "Look up real open appointment times. Call this before offering any time. Returns at most one "
                    "time per day. If the patient wants a specific day of the week, pass weekday; never say a "
                    "day has no openings unless a search for that day came back empty.",
     "parameters": {"type": "object", "properties": {
         "weekday": {"type": "string", "enum": ["monday", "tuesday", "wednesday", "thursday", "friday"]},
         "date_from": {"type": "string", "description": "Earliest date, YYYY-MM-DD. Omit for 'as soon as possible'."},
         "date_to": {"type": "string", "description": "Latest date, YYYY-MM-DD."},
         "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
         "procedure": {"type": "string", "description": "Procedure code, default cleaning."}}}},
    {"type": "function", "name": "book_appointment",
     "description": "Book a slot for this patient after they agree to a specific time.",
     "parameters": {"type": "object", "properties": {
         "slot_id": {"type": "string", "description": "slot_id from get_available_slots"},
         "procedure": {"type": "string", "description": "Procedure code, default cleaning."}},
         "required": ["slot_id"]}},
    {"type": "function", "name": "cancel_appointment",
     "description": "Cancel one of this patient's appointments at their request.",
     "parameters": {"type": "object", "properties": {
         "appointment_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["appointment_id"]}},
    {"type": "function", "name": "reschedule_appointment",
     "description": "Move one of this patient's appointments (including one the office cancelled) to a new slot.",
     "parameters": {"type": "object", "properties": {
         "appointment_id": {"type": "integer"}, "slot_id": {"type": "string"}},
         "required": ["appointment_id", "slot_id"]}},
    {"type": "function", "name": "add_to_waitlist",
     "description": "The patient wants an appointment but no offered slot works. Records unmet demand.",
     "parameters": {"type": "object", "properties": {
         "preference": {"type": "string", "description": "When they would like to come, e.g. 'Saturday mornings'."},
         "procedure": {"type": "string"}}, "required": ["preference"]}},
    {"type": "function", "name": "save_patient_note",
     "description": "Save a short note to this patient's memory, e.g. 'prefers mornings'.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"type": "function", "name": "end_call",
     "description": "Call after you have said goodbye, to hang up and record the outcome.",
     "parameters": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": OUTCOMES},
         "summary": {"type": "string", "description": "One sentence summary of the call."}},
         "required": ["outcome"]}},
]


# ── instructions ─────────────────────────────────────────────────────────────

def agent_files(conn) -> dict[str, str]:
    return {r["name"]: r["content"] for r in rows(conn, "SELECT name, content FROM agent_files")}


def _goal(conn, p: dict, task: dict | None) -> tuple[str, str]:
    kind = task["kind"] if task else ("recall" if p["due"] else "next_visit" if _next_from(p) else "check_in")
    ctx = task["context"] if task else {}
    used = f"{p['cleanings_done']} of 3 insurance-covered cleanings used this year"
    if kind == "next_visit":
        return kind, (f"Next visit: they are already booked for a cleaning {sch.label(p['next_cleaning'])}. After it "
                      f"they still have one more insurance-covered cleaning this year ({used}). Confirm the upcoming "
                      f"one, then offer to book the following cleaning on or after {_next_from(p)} "
                      f"(call get_available_slots with date_from {_next_from(p)}).")
    if kind == "recall":
        return kind, f"Recall: the patient is due for a cleaning ({used}). Offer to book one that fits their preferences."
    if kind == "gap_fill":
        return kind, (f"Gap fill: a slot just opened, {ctx.get('slot_label')} (slot_id {ctx.get('slot_id')}). "
                      f"Offer it first ({used}). If it does not work, offer other times.")
    if kind == "reschedule":
        return kind, (f"Reschedule: the office had to cancel their {ctx.get('procedure')} on "
                      f"{ctx.get('appointment_label')} (appointment_id {ctx.get('appointment_id')}) because "
                      f"{ctx.get('reason')}. Apologize and move it with reschedule_appointment.")
    return "check_in", f"Check-in: confirm their upcoming appointment, or offer a cleaning if they want one ({used})."


def _next_from(p: dict) -> str | None:
    """Earliest date for the cleaning after an already-booked one, if a covered cleaning is still left."""
    if p["due"] or not p["next_cleaning"] or p["cleanings_done"] + 1 >= 3:
        return None
    return iso(datetime.fromisoformat(p["next_cleaning"]) + timedelta(days=56))[:10]


def _slot_hint(conn, kind: str, date_from: str | None = None) -> str:
    """Real openings so the opener never invents a time."""
    proc = "cleaning" if kind in ("recall", "gap_fill", "check_in", "next_visit") else None
    slots = sch.open_slots(conn, date_from, procedure=proc or "exam", limit=6)
    return "; ".join(f"{s['slot_id']}: {s['label']}" for s in slots) or "none, call get_available_slots"


def patient_brief(conn, p: dict, task: dict | None) -> dict:
    """What the agent may know once identity is confirmed: the goal and the patient's record."""
    kind, goal = _goal(conn, p, task)
    upcoming = [a for a in p["appointments"] if a["status"] == "scheduled" and a["start"] >= iso(now_local())]
    appts = "; ".join(f"appointment_id {a['id']}: {a['procedure']} {sch.label(a['start'], a['provider'])}"
                      for a in upcoming[:5]) or "none"
    return {"goal": goal, "insurance": p["insurance"], "last_cleaning": p["last_cleaning"] or "none on file",
            "upcoming": appts, "memory": p["memory"] or "nothing yet"}


def build_instructions(conn, p: dict, task: dict | None) -> tuple[str, str]:
    files = agent_files(conn)
    kind = _goal(conn, p, task)[0]
    prices = ", ".join(f"{p2['name']} ${p2['price']:.0f}" for p2 in sch.procedures(conn).values())
    now = now_local()
    first_line = f"{opening(files, kind)} Am I speaking with {p['name']}?"
    leave = callback_line(files)
    parts = [files.get(n, "") for n in FILE_NAMES] + [
        "# This call",
        f"Today is {now.strftime('%A, %B %-d, %Y, %-I:%M %p')} Pacific time.",
        f"You are on an OUTBOUND phone call you placed to {p['name']}. You speak first. Your first turn is exactly: "
        f'"{first_line}" Then stop and wait.',
        "# Identity check (before anything else)",
        "Share nothing about visits, appointments, insurance, history or this patient until verify_identity returns "
        "ok: true. If they confirm they are " + p["name"] + ", call verify_identity with method name. If they sound "
        "unsure, ask for their date of birth or the last 4 digits of their phone number and pass it to "
        "verify_identity. If it does not match, you have not verified them.",
        f'Wrong person, or nobody confirms: say only "{leave}" then call end_call with outcome wrong_person. '
        f'Voicemail, an answering machine or a beep: say only "{leave}" then call end_call with outcome voicemail. '
        "Never leave details on a voicemail or with someone else.",
        "verify_identity returns the goal of the call and the patient's record. Then continue: say why you're calling.",
        f"Prices: {prices}.",
        f"Open slots right now (slot_id: when): {_slot_hint(conn, kind, _next_from(p) if kind == 'next_visit' else None)}",
        "# Tools",
        "Never invent a time: call get_available_slots first and offer two or three options. As soon as the "
        "patient picks one, call book_appointment with that slot_id in the same turn. Never say an appointment is "
        "booked, cancelled or moved unless the tool returned ok: true. Name the provider exactly as that slot's "
        "text says; never guess who a time is with. If nothing works, call add_to_waitlist. Save any new preference "
        "with save_patient_note.",
        "Email: after a booking or reschedule the system emails a confirmation by itself. Only if that tool result "
        "has confirmation_email.ok true, say \"I've emailed you a confirmation.\" Otherwise don't mention email; if "
        "they ask, say the front desk will send it, and save any email address they give with save_patient_note. "
        "You cannot send texts.",
        "Ending: when the patient says bye, or the call is clearly done, say one short goodbye and call end_call in "
        "that same response. It is the only way to hang up. Never say goodbye twice.",
    ]
    return kind, "\n".join(x.strip() for x in parts if x)


def get_voice(conn) -> str:
    r = row(conn, "SELECT value FROM settings WHERE key='voice'")
    return r["value"] if r and r["value"] in VOICES else DEFAULT_VOICE


def set_voice(conn, voice: str) -> str:
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}; pick one of {', '.join(VOICES)}")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('voice', ?)", (voice,))
    return voice


def session_config(instructions: str, text_mode: bool = False, phone: bool = False,
                   voice: str = DEFAULT_VOICE) -> dict:
    fmt = {"type": "audio/pcmu"} if phone else {"type": "audio/pcm", "rate": 24000}  # phone: Twilio μ-law 8 kHz
    audio = {
        "input": {"format": fmt,
                  "turn_detection": {"type": "server_vad"},
                  "transcription": {"model": "higgs-stt-3.1", "language": "en"}},
        "output": {"format": fmt, "voice": voice},
    }
    cfg = {"type": "realtime", "model": MODEL, "instructions": instructions,
           "output_modalities": ["text"] if text_mode else ["audio"], "tools": TOOLS, "tool_choice": "auto"}
    if not text_mode:
        cfg["audio"] = audio
    return cfg


def mint_client_secret(session: dict, ttl: int = 600) -> dict:
    """Ephemeral key for the browser; the real BOSON_API_KEY never leaves the server."""
    key = os.getenv("BOSON_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BOSON_API_KEY is not configured")
    url = f"{BOSON_BASE_URL}/v1/realtime/client_secrets"
    headers = {"Authorization": f"Bearer {key}"}
    r = httpx.post(url, headers=headers, json={"expires_after": {"seconds": ttl}, "session": session}, timeout=20)
    if r.status_code >= 400:  # fall back to a bare key; the browser sends session.update anyway
        r = httpx.post(url, headers=headers, json={"expires_after": {"seconds": ttl}}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return {"value": data["value"], "expires_at": data.get("expires_at")}


# ── call lifecycle ───────────────────────────────────────────────────────────

def start(conn, patient_id: int, task_id: int | None = None, text_mode: bool = False, mode: str = "browser") -> dict:
    p = sch.patient_detail(conn, patient_id)
    if not p:
        raise ValueError("patient not found")
    task = campaigns.task(conn, task_id) if task_id else None
    kind, instructions = build_instructions(conn, p, task)
    cur = conn.execute(
        "INSERT INTO calls (patient_id,task_id,goal,mode,started_at,outcome) VALUES (?,?,?,?,?,NULL)",
        (patient_id, task_id, kind, mode, iso(now_local())),
    )
    if task:
        campaigns.set_task(conn, task_id, "in_progress", cur.lastrowid)
    session = session_config(instructions, text_mode, voice=get_voice(conn))
    return {"call_id": cur.lastrowid, "goal": kind, "session": session,
            "patient": {k: p[k] for k in ("id", "name", "phone", "memory", "cleanings_left", "due")}}


def _event(conn, call_id: int, entry: dict) -> None:
    c = row(conn, "SELECT events FROM calls WHERE id=?", (call_id,))
    events = json.loads(c["events"] or "[]") + [entry]
    conn.execute("UPDATE calls SET events=? WHERE id=?", (json.dumps(events), call_id))


def run_tool(conn, call_id: int, name: str, args: dict) -> dict:
    call = row(conn, "SELECT * FROM calls WHERE id=?", (call_id,))
    if not call:
        raise ValueError("call not found")
    try:
        if name not in UNGATED and not verified(call):
            raise ValueError("identity not confirmed yet: confirm who you are speaking with, then call verify_identity")
        result = _dispatch(conn, call, name, args or {})
    except (ValueError, KeyError, TypeError) as e:
        result = {"ok": False, "error": str(e)}
    appt_id = result.get("appointment_id") or result.get("new_appointment_id")
    mail = confirm_email.send(conn, appt_id) if result.get("ok") and appt_id else None
    if mail:
        result["confirmation_email"] = {k: mail[k] for k in ("ok", "sent_to", "error") if k in mail}
    _event(conn, call_id, {"at": iso(now_local()), "tool": name, "args": args, "result": result})
    if mail:
        _event(conn, call_id, {"at": iso(now_local()), "tool": "send_confirmation_email",
                               "args": {"appointment_id": appt_id}, "result": mail})
    return result


def verified(call: dict) -> bool:
    return any(e.get("tool") == "verify_identity" and (e.get("result") or {}).get("ok")
               for e in json.loads(call.get("events") or "[]"))


def _verify(conn, call: dict, a: dict) -> dict:
    p = sch.patient_detail(conn, call["patient_id"])
    method, value = a.get("method"), re.sub(r"[^0-9-]", "", str(a.get("value", "")))
    if method not in ("name", "dob", "phone_last4"):
        raise ValueError("method must be name, dob or phone_last4")
    if method == "phone_last4" and value[-4:] != re.sub(r"\D", "", p["phone"])[-4:]:
        raise ValueError("those digits do not match the number on file: not verified")
    if method == "dob":
        dob = row(conn, "SELECT dob FROM patients WHERE id=?", (p["id"],))["dob"]
        if not dob:
            raise ValueError("no date of birth on file: ask for the last 4 digits of their phone number instead")
        if value != dob:
            raise ValueError("that date of birth does not match: not verified")
    task = campaigns.task(conn, call["task_id"]) if call["task_id"] else None
    return {"ok": True, "verified": True, **patient_brief(conn, p, task)}


def _own_appt(conn, call, appt_id) -> dict:
    a = sch.appointment(conn, int(appt_id))
    if not a or a["patient_id"] != call["patient_id"]:
        raise ValueError("that appointment does not belong to this patient")
    return a


def _dispatch(conn, call: dict, name: str, a: dict) -> dict:
    pid = call["patient_id"]
    if name == "verify_identity":
        return _verify(conn, call, a)
    if name == "get_available_slots":
        slots = sch.open_slots(conn, a.get("date_from"), a.get("date_to"), a.get("part_of_day") or "any",
                               a.get("procedure") or "cleaning", limit=8, per_day=1, weekday=a.get("weekday"))
        return {"ok": True, "slots": [{"slot_id": s["slot_id"], "when": s["label"]} for s in slots]}
    if name == "book_appointment":
        appt = sch.book(conn, pid, a["slot_id"], a.get("procedure") or "cleaning")
        conn.execute("UPDATE calls SET revenue=revenue+?, outcome=COALESCE(outcome,'booked') WHERE id=?",
                     (appt["price"], call["id"]))
        return {"ok": True, "appointment_id": appt["id"], "booked": appt["label"], "price": appt["price"]}
    if name == "cancel_appointment":
        _own_appt(conn, call, a["appointment_id"])
        appt = sch.cancel(conn, int(a["appointment_id"]), a.get("reason", "patient request"))
        return {"ok": True, "cancelled": appt["label"]}
    if name == "reschedule_appointment":
        _own_appt(conn, call, a["appointment_id"])
        res = sch.reschedule(conn, int(a["appointment_id"]), a["slot_id"])
        conn.execute("UPDATE calls SET outcome=COALESCE(outcome,'rescheduled') WHERE id=?", (call["id"],))
        return {"ok": True, "new_appointment_id": res["new"]["id"], "moved_to": res["new"]["label"]}
    if name == "add_to_waitlist":
        proc = a.get("procedure") or "cleaning"
        hours = sch.procedures(conn).get(proc, {"minutes": 60})["minutes"] / 60
        conn.execute(
            "INSERT INTO unmet_demand (patient_id,call_id,procedure,hours,preference,reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, call["id"], proc, hours, a.get("preference", ""), "no suitable slot", iso(now_local())))
        conn.execute("UPDATE patients SET waitlist=1 WHERE id=?", (pid,))
        conn.execute("UPDATE calls SET outcome=COALESCE(outcome,'waitlisted') WHERE id=?", (call["id"],))
        return {"ok": True, "waitlisted": True}
    if name == "save_patient_note":
        note = str(a.get("note", "")).strip()[:300]
        p = row(conn, "SELECT memory FROM patients WHERE id=?", (pid,))
        stamp = now_local().strftime("%Y-%m-%d")
        memory = (p["memory"] + f"\n- {stamp}: {note}").strip()
        conn.execute("UPDATE patients SET memory=? WHERE id=?", (memory, pid))
        return {"ok": True, "saved": note}
    if name == "end_call":
        outcome = a.get("outcome") if a.get("outcome") in OUTCOMES else "other"
        conn.execute("UPDATE calls SET outcome=?, summary=? WHERE id=?", (outcome, a.get("summary", ""), call["id"]))
        return {"ok": True, "hang_up": True}
    raise ValueError(f"unknown tool {name}")


def events_summary(events: list) -> str:
    """Fallback summary from tool results, for calls where the model never called end_call."""
    labels = {"booked": "Booked", "moved_to": "Moved to", "cancelled": "Cancelled", "waitlisted": "Waitlisted"}
    parts = []
    for e in events:
        r = e.get("result") or {}
        for key, label in labels.items():
            if r.get("ok") and r.get(key):
                parts.append(label if r[key] is True else f"{label} {r[key]}")
    if parts:
        return "; ".join(parts) + "."
    if any(e.get("tool") == "verify_identity" and (e.get("result") or {}).get("verified") for e in events):
        return "Identity confirmed; nothing booked or changed."
    return ""


WRONG_NUMBER = re.compile(r"wrong (number|person)|no one (here )?by that name|(isn't|is not|not) (here|me)|don't know (him|her|them)", re.I)


def unverified_outcome(events: list, transcript: list | None) -> tuple[str, str] | None:
    """Outcome for calls that ended on the callback line without end_call: wrong person or voicemail."""
    if any(e.get("tool") == "verify_identity" and (e.get("result") or {}).get("verified") for e in events):
        return None
    turns = transcript or []
    if not any(t.get("role") == "agent" and "give us a call back" in t.get("text", "").lower() for t in turns):
        return None
    if any(t.get("role") == "patient" and WRONG_NUMBER.search(t.get("text", "")) for t in turns):
        return "wrong_person", "Wrong number; left only the office name and callback number."
    return "voicemail", "Left a voicemail with the office name and callback number."


def finish(conn, call_id: int, transcript: list | None, duration_s: float | None) -> dict:
    call = row(conn, "SELECT * FROM calls WHERE id=?", (call_id,))
    if not call:
        raise ValueError("call not found")
    if not duration_s:
        duration_s = (now_local() - datetime.fromisoformat(call["started_at"])).total_seconds()
    fallback = None if call["outcome"] else unverified_outcome(json.loads(call["events"] or "[]"), transcript)
    if fallback:
        conn.execute("UPDATE calls SET outcome=?, summary=? WHERE id=?", (*fallback, call_id))
    conn.execute(
        "UPDATE calls SET ended_at=?, duration_s=?, transcript=?, outcome=COALESCE(outcome,'other'),"
        " summary=COALESCE(NULLIF(summary,''),?) WHERE id=?",
        (iso(now_local()), round(float(duration_s), 1), json.dumps(transcript or [])[:200000],
         events_summary(json.loads(call["events"] or "[]")), call_id),
    )
    if call["task_id"]:
        campaigns.set_task(conn, call["task_id"], "done")
    return get(conn, call_id)


def get(conn, call_id: int) -> dict | None:
    c = row(conn, "SELECT c.*, p.name AS patient FROM calls c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", (call_id,))
    if c:
        c["transcript"], c["events"] = json.loads(c["transcript"] or "[]"), json.loads(c["events"] or "[]")
    return c
