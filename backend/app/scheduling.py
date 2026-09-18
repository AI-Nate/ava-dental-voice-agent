"""Schedule logic: patient recall status, open slots, book/cancel/reschedule.

Monolithic-ish on purpose (hackathon prototype): every function takes a sqlite
connection and returns plain dicts.
"""

from datetime import datetime, timedelta

from .db import iso, now_local, row, rows
from .seed import DEMO_PHONE, SLOT_HOURS

RECALL_GAP_DAYS = 120  # "last cleaning more than 4 months ago"
CLEANING_PROVIDERS = [3, 1, 2]  # hygienist first, doctors can also clean
DOCTORS = [1, 2]


def label(start: str, provider: str | None = None) -> str:
    dt = datetime.fromisoformat(start)
    text = dt.strftime("%A %b %-d at %-I:%M %p").replace(":00 ", " ")
    return f"{text} with {provider}" if provider else text


def providers(conn) -> dict[int, dict]:
    return {p["id"]: p for p in rows(conn, "SELECT * FROM providers")}


def procedures(conn) -> dict[str, dict]:
    return {p["code"]: p for p in rows(conn, "SELECT * FROM procedures")}


# ── patients ─────────────────────────────────────────────────────────────────

def patient_status(conn, pid: int | None = None) -> list[dict]:
    """Every patient (or one) with recall fields computed from appointments."""
    now = iso(now_local())
    year = now[:4]
    where = "WHERE p.id = ?" if pid else ""
    args = (now, now, year, now, now, now) + ((pid,) if pid else ())
    out = rows(
        conn,
        f"""
        SELECT p.*,
          (SELECT MAX(start) FROM appointments a WHERE a.patient_id=p.id AND a.status='completed' AND a.start < ?) AS last_appt,
          (SELECT MIN(start) FROM appointments a WHERE a.patient_id=p.id AND a.status='scheduled' AND a.start >= ?) AS next_appt,
          (SELECT COUNT(*) FROM appointments a WHERE a.patient_id=p.id AND a.procedure='cleaning'
              AND a.status='completed' AND substr(a.start,1,4)=?) AS cleanings_done,
          (SELECT MAX(start) FROM appointments a WHERE a.patient_id=p.id AND a.procedure='cleaning' AND a.status='completed' AND a.start < ?) AS last_cleaning,
          (SELECT MIN(start) FROM appointments a WHERE a.patient_id=p.id AND a.procedure='cleaning' AND a.status='scheduled' AND a.start >= ?) AS next_cleaning,
          (SELECT outcome FROM calls c WHERE c.patient_id=p.id ORDER BY c.id DESC LIMIT 1) AS last_call_outcome,
          (SELECT COUNT(*) FROM calls c WHERE c.patient_id=p.id AND c.started_at <= ?) AS call_count
        FROM patients p {where} ORDER BY p.name
        """,
        args,
    )
    cutoff = iso(now_local() - timedelta(days=RECALL_GAP_DAYS))
    for p in out:
        p["cleanings_left"] = max(0, 3 - p["cleanings_done"])
        p["due"] = bool(
            p["cleanings_done"] < 3
            and (p["last_cleaning"] is None or p["last_cleaning"] < cutoff)
            and p["next_cleaning"] is None
        )
    return out


def patient_detail(conn, pid: int) -> dict | None:
    found = patient_status(conn, pid)
    if not found:
        return None
    p = found[0]
    provs = providers(conn)
    p["appointments"] = rows(
        conn, "SELECT * FROM appointments WHERE patient_id=? ORDER BY start DESC", (pid,)
    )
    for a in p["appointments"]:
        a["provider"] = provs.get(a["provider_id"], {}).get("name")
    p["calls"] = rows(
        conn,
        "SELECT id,goal,mode,started_at,duration_s,outcome,summary,revenue,transcript,events FROM calls "
        "WHERE patient_id=? ORDER BY id DESC",
        (pid,),
    )
    return p


def waitlist(conn) -> list[dict]:
    """Waitlisted patients with their latest stated preference and current booking."""
    out = rows(conn, """
        SELECT p.id, p.name, p.phone,
          (SELECT GROUP_CONCAT(start) FROM (SELECT start FROM appointments a WHERE a.patient_id=p.id
              AND a.status='scheduled' AND a.start >= ? ORDER BY start)) AS upcoming,
          COALESCE(u.preference, p.notes) AS preference, u.procedure, u.call_id, u.created_at AS since
        FROM patients p LEFT JOIN unmet_demand u ON u.id =
          (SELECT MAX(id) FROM unmet_demand WHERE patient_id=p.id)
        WHERE p.waitlist=1 ORDER BY COALESCE(u.created_at, '') DESC, p.name""", (iso(now_local()),))
    for w in out:
        w["upcoming"] = [{"start": t, "label": label(t)} for t in (w["upcoming"] or "").split(",") if t]
    return out


def due_patients(conn) -> list[dict]:
    due = [p for p in patient_status(conn) if p["due"] and p["status"] == "active"]
    # Demo patient first, then the most overdue.
    return sorted(due, key=lambda p: (p["phone"] != DEMO_PHONE, p["last_cleaning"] or ""))


# ── slots ────────────────────────────────────────────────────────────────────

def _occupied(conn, start: str, end: str) -> set[tuple[int, str]]:
    taken = set()
    for a in rows(
        conn,
        "SELECT provider_id,start,end FROM appointments WHERE status='scheduled' AND start < ? AND end > ?",
        (end, start),
    ):
        t = datetime.fromisoformat(a["start"])
        while iso(t) < a["end"]:
            taken.add((a["provider_id"], iso(t)))
            t += timedelta(hours=1)
    return taken


def _parse_day(value: str | None, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(value[:10]) if value else default
    except ValueError:
        return default


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def open_slots(conn, date_from=None, date_to=None, part_of_day="any", procedure="cleaning",
               provider_id=None, limit=6, per_day=2, weekday=None) -> list[dict]:
    """Free grid slots, earliest first, at most `per_day` per day so options spread out.
    `weekday` (e.g. "friday") keeps only that day of the week."""
    wd = WEEKDAYS.index(weekday.lower()) if weekday and weekday.lower() in WEEKDAYS else None
    now = now_local()
    first = max(_parse_day(date_from, now), now.replace(hour=0, minute=0))
    last = _parse_day(date_to, first + timedelta(days=21))
    last = min(max(last, first), now + timedelta(days=120))
    minutes = procedures(conn).get(procedure, {"minutes": 60})["minutes"]
    pids = [int(provider_id)] if provider_id else (CLEANING_PROVIDERS if procedure == "cleaning" else DOCTORS)
    provs = providers(conn)
    taken = _occupied(conn, iso(first), iso(last + timedelta(days=1)))
    hours = [h for h in SLOT_HOURS if part_of_day in (None, "", "any")
             or (part_of_day == "morning" and h < 12) or (part_of_day == "afternoon" and h >= 12)]
    out, day = [], first
    while day.date() <= last.date() and len(out) < limit:
        if day.weekday() < 5 and wd in (None, day.weekday()):
            n = 0
            for h in hours:
                start = day.replace(hour=h, minute=0)
                if start <= now or (minutes > 60 and h in (11, 16)):
                    continue
                pid = next((p for p in pids if all(
                    (p, iso(start + timedelta(hours=k))) not in taken for k in range(minutes // 60))), None)
                if pid is None:
                    continue
                out.append({"slot_id": f"{pid}@{iso(start)}", "provider_id": pid,
                            "provider": provs[pid]["name"], "start": iso(start),
                            "label": label(iso(start), provs[pid]["name"])})
                n += 1
                if n >= per_day or len(out) >= limit:
                    break
        day += timedelta(days=1)
    return out


def gaps(conn, business_days=3) -> list[dict]:
    """Every free provider-hour in the next N business days (gap-fill targets)."""
    now = now_local()
    out, day, seen = [], now.replace(hour=0, minute=0), 0
    while seen < business_days:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        seen += 1
        for pid in CLEANING_PROVIDERS:
            out += open_slots(conn, iso(day), iso(day), provider_id=pid, limit=20, per_day=20)
    return sorted(out, key=lambda s: (s["start"], s["provider_id"]))


# ── mutations ────────────────────────────────────────────────────────────────

def parse_slot(slot_id: str) -> tuple[int, str]:
    try:
        pid, start = str(slot_id).split("@", 1)
        return int(pid), iso(datetime.fromisoformat(start))
    except ValueError:
        raise ValueError(f"unknown slot_id {slot_id!r}: use a slot_id returned by get_available_slots") from None


def book(conn, patient_id: int, slot_id: str, procedure="cleaning", source="agent", note="") -> dict:
    procs = procedures(conn)
    if procedure not in procs:
        raise ValueError(f"unknown procedure {procedure}")
    pid, start = parse_slot(slot_id)
    st = datetime.fromisoformat(start)
    minutes = procs[procedure]["minutes"]
    end = iso(st + timedelta(minutes=minutes))
    if st <= now_local() or st.weekday() >= 5 or st.hour not in SLOT_HOURS:
        raise ValueError("that time is outside office hours or in the past")
    if pid not in providers(conn):
        raise ValueError("unknown provider")
    if any((pid, iso(st + timedelta(hours=k))) in _occupied(conn, start, end) for k in range(minutes // 60)):
        raise ValueError("that slot was just taken")
    cur = conn.execute(
        "INSERT INTO appointments (patient_id,provider_id,start,end,procedure,status,price,source,note,created_at) "
        "VALUES (?,?,?,?,?,'scheduled',?,?,?,?)",
        (patient_id, pid, start, end, procedure, procs[procedure]["price"], source, note, iso(now_local())),
    )
    return appointment(conn, cur.lastrowid)


def appointment(conn, appt_id: int) -> dict | None:
    a = row(conn, "SELECT * FROM appointments WHERE id=?", (appt_id,))
    if a:
        prov = providers(conn).get(a["provider_id"], {}).get("name")
        a["provider"], a["label"] = prov, label(a["start"], prov)
    return a


def cancel(conn, appt_id: int, reason="", status="cancelled") -> dict:
    a = appointment(conn, appt_id)
    if not a:
        raise ValueError("appointment not found")
    conn.execute("UPDATE appointments SET status=?, note=? WHERE id=?",
                 (status, (a["note"] + " " + reason).strip(), appt_id))
    return appointment(conn, appt_id)


def reschedule(conn, appt_id: int, slot_id: str) -> dict:
    old = appointment(conn, appt_id)
    if not old:
        raise ValueError("appointment not found")
    new = book(conn, old["patient_id"], slot_id, old["procedure"], note=f"rescheduled from #{appt_id}")
    conn.execute("UPDATE appointments SET status='rescheduled', note=? WHERE id=?",
                 ((old["note"] + f" moved to #{new['id']}").strip(), appt_id))
    return {"old": appointment(conn, appt_id), "new": new}
