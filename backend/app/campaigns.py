"""Call-task queue: recall campaign, gap fill, doctor cancellation."""

import json
from datetime import datetime

from . import scheduling as sch
from .db import iso, now_local, row, rows


def _open_task_patients(conn) -> set[int]:
    return {r["patient_id"] for r in rows(
        conn, "SELECT patient_id FROM tasks WHERE status IN ('queued','in_progress')")}


def add_task(conn, kind: str, patient_id: int, context: dict | None = None) -> int:
    ts = iso(now_local())
    cur = conn.execute(
        "INSERT INTO tasks (kind,patient_id,context,status,created_at,updated_at) VALUES (?,?,?,'queued',?,?)",
        (kind, patient_id, json.dumps(context or {}), ts, ts),
    )
    return cur.lastrowid


def queue_recall(conn, limit: int = 10) -> list[int]:
    busy = _open_task_patients(conn)
    due = [p for p in sch.due_patients(conn) if p["id"] not in busy][:limit]
    return [add_task(conn, "recall", p["id"]) for p in due]


def queue_gap_fill(conn, business_days: int = 3, max_tasks: int = 5, slot_id: str | None = None) -> list[dict]:
    """Pair each open slot with a waitlisted-then-due patient who has no open task."""
    slots = [s for s in sch.gaps(conn, business_days)] if not slot_id else [_slot_from_id(conn, slot_id)]
    busy = _open_task_patients(conn)
    waitlisted = [p for p in sch.patient_status(conn) if p["waitlist"] and p["id"] not in busy]
    candidates = waitlisted + [p for p in sch.due_patients(conn) if p["id"] not in busy and not p["waitlist"]]
    created, used_starts = [], set()
    for slot in slots:
        if len(created) >= max_tasks or not candidates:
            break
        if slot["start"] in used_starts:
            continue  # one task per time, not one per provider-hour
        used_starts.add(slot["start"])
        p = candidates.pop(0)
        tid = add_task(conn, "gap_fill", p["id"], {"slot_id": slot["slot_id"], "slot_label": slot["label"]})
        created.append({"task_id": tid, "patient": p["name"], "slot": slot["label"]})
    return created


def _slot_from_id(conn, slot_id: str) -> dict:
    pid, start = sch.parse_slot(slot_id)
    return {"slot_id": slot_id, "start": start, "label": sch.label(start, sch.providers(conn)[pid]["name"])}


def doctor_cancel(conn, provider_id: int, date: str, start_hour: int = 0, end_hour: int = 24) -> dict:
    """Cancel a provider's day (or block) and queue a reschedule call per patient."""
    day = datetime.fromisoformat(date[:10])
    lo, hi = iso(day.replace(hour=start_hour)), iso(day.replace(hour=min(end_hour, 23), minute=59))
    appts = rows(
        conn,
        "SELECT id FROM appointments WHERE provider_id=? AND status='scheduled' AND start>=? AND start<=?",
        (provider_id, lo, hi),
    )
    prov = sch.providers(conn)[provider_id]["name"]
    tasks = []
    for a in appts:
        ap = sch.cancel(conn, a["id"], f"office cancelled: {prov} unavailable", status="cancelled_office")
        tasks.append(add_task(conn, "reschedule", ap["patient_id"], {
            "appointment_id": ap["id"], "appointment_label": ap["label"], "procedure": ap["procedure"],
            "reason": f"{prov} is unavailable that day"}))
    return {"cancelled": len(appts), "tasks": tasks, "provider": prov, "date": date[:10]}


def list_tasks(conn, include_done: bool = False) -> list[dict]:
    where = "" if include_done else "WHERE t.status IN ('queued','in_progress')"
    out = rows(conn, f"""SELECT t.*, p.name AS patient, p.phone FROM tasks t JOIN patients p ON p.id=t.patient_id
                         {where} ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, t.id
                         LIMIT 200""")
    for t in out:
        t["context"] = json.loads(t["context"] or "{}")
    return out


def set_task(conn, task_id: int, status: str, call_id: int | None = None) -> None:
    conn.execute("UPDATE tasks SET status=?, call_id=COALESCE(?, call_id), updated_at=? WHERE id=?",
                 (status, call_id, iso(now_local()), task_id))


def task(conn, task_id: int) -> dict | None:
    t = row(conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
    if t:
        t["context"] = json.loads(t["context"] or "{}")
    return t
