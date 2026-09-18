"""Appointment confirmation email (Mailgun) with an .ics calendar invite.

Demo data uses example.com addresses: those are never sent to, only skipped and logged.
"""

import logging
import httpx
import os
import re
from datetime import datetime, timezone

from . import scheduling as sch
from .db import TZ, row

logger = logging.getLogger(__name__)
_BLOCKED = re.compile(r"@(.+\.)?example\.(com|org|net)$", re.I)


def deliverable(email: str | None) -> str | None:
    """Why an address must not be emailed, or None if it may be."""
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return "no valid email on file"
    if _BLOCKED.search(email):
        return "example.com demo address"
    return None


def _utc(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso).replace(tzinfo=TZ).astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def ics(appt: dict, office: dict, provider: str) -> bytes:
    """A one-event iCalendar invite (times in UTC so every client places it right)."""
    esc = lambda s: re.sub(r"([,;\\])", r"\\\1", s or "")  # noqa: E731
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Bright Smile Voice Agent//EN", "METHOD:PUBLISH",
        "BEGIN:VEVENT", f"UID:appt-{appt['id']}@voice-agent", f"DTSTAMP:{_utc(datetime.now(TZ).strftime('%Y-%m-%dT%H:%M'))}",
        f"DTSTART:{_utc(appt['start'])}", f"DTEND:{_utc(appt['end'])}",
        f"SUMMARY:{esc(appt['procedure'].replace('_', ' ').title())} at {esc(office['name'])}",
        f"LOCATION:{esc(office['address'])}",
        f"DESCRIPTION:With {esc(provider)}. To cancel or reschedule call {esc(office['phone'])}.",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def compose(appt: dict, office: dict, provider: str, patient_name: str) -> dict:
    when = datetime.fromisoformat(appt["start"]).strftime("%A, %B %-d, %Y at %-I:%M %p")
    proc = appt["procedure"].replace("_", " ")
    first = patient_name.split()[0]
    text = (f"Hi {first},\n\nYou're booked for a {proc} with {provider} at {office['name']}.\n\n"
            f"When: {when} (Pacific)\nWhere: {office['address']}\nPhone: {office['phone']}\n\n"
            f"Need to cancel or reschedule? Just call us at {office['phone']}.\n"
            "A calendar invite is attached.\n\nSee you soon,\n" + office["name"])
    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return {"subject": f"Confirmed: {proc} on {when}", "text": text, "html": html}


def send(conn, appt_id: int) -> dict:
    """Email the patient a confirmation for one appointment. Never raises; returns the tool-log result."""
    a = sch.appointment(conn, appt_id)
    if not a:
        return {"ok": False, "error": "appointment not found"}
    p = row(conn, "SELECT name, email FROM patients WHERE id=?", (a["patient_id"],))
    why = deliverable(p and p["email"])
    if why:
        logger.info("voice-agent: confirmation for appointment %s not sent: %s", appt_id, why)
        return {"ok": False, "skipped": True, "error": f"not sent: {why}"}
    office = row(conn, "SELECT * FROM office WHERE id=1")
    provider = a["provider"] or "our team"
    msg = compose(a, office, provider, p["name"])
    ok, err = send_mailgun(p["email"], msg["subject"], msg["text"], msg["html"], log_label="Voice-agent confirmation",
                            from_name=office["name"],
                            attachments=[(f"appointment-{appt_id}.ics", ics(a, office, provider), "text/calendar")])
    return {"ok": True, "sent_to": p["email"], "subject": msg["subject"]} if ok else {"ok": False, "error": err}


def send_mailgun(to_email: str, subject: str, text: str, html: str, log_label: str = "Email",
                 from_name: str = "Ava", attachments: list[tuple[str, bytes, str]] | None = None) -> tuple[bool, str | None]:
    """Send one email through the Mailgun messages API. Returns (success, error)."""
    key, domain = os.getenv("MAILGUN_API_KEY", ""), os.getenv("MAILGUN_DOMAIN", "")
    if not key or not domain:
        return False, "MAILGUN_API_KEY / MAILGUN_DOMAIN not configured"
    try:
        r = httpx.post(f"https://api.mailgun.net/v3/{domain}/messages", auth=("api", key), timeout=10,
                       data={"from": f"{from_name} <noreply@{domain}>", "to": [to_email], "subject": subject,
                             "text": text, "html": html},
                       files=[("attachment", a) for a in attachments or []] or None)
    except httpx.HTTPError as e:
        return False, f"Failed to send email: {e}"
    if r.status_code == 200:
        logger.info("%s sent to %s", log_label, to_email)
        return True, None
    return False, f"Failed to send email (status {r.status_code})"
