"""Dashboard numbers + capacity analysis from unmet demand and utilization."""

from datetime import timedelta

from . import scheduling as sch
from .db import iso, now_local, row, rows
from .seed import SLOT_HOURS


def utilization(conn, days: int = 28) -> dict:
    """Booked hours / open hours per provider over the next `days`."""
    now = now_local()
    start, end = iso(now), iso(now + timedelta(days=days))
    workdays = sum(1 for d in range(1, days + 1) if (now + timedelta(days=d)).weekday() < 5)
    capacity = workdays * len(SLOT_HOURS)
    out = {}
    for p in sch.providers(conn).values():
        booked = row(conn, """SELECT COALESCE(SUM((julianday(end)-julianday(start))*24),0) h FROM appointments
                              WHERE provider_id=? AND status='scheduled' AND start>=? AND start<?""",
                     (p["id"], start, end))["h"]
        out[p["name"]] = {"role": p["role"], "booked_h": round(booked, 1), "capacity_h": capacity,
                          "pct": round(100 * booked / capacity) if capacity else 0}
    return out


def capacity_analysis(demand_hours_30d: float, backlog_h: float, util: dict) -> dict:
    weekly = demand_hours_30d / (30 / 7)
    hyg = [u["pct"] for u in util.values() if u["role"] == "hygienist"]
    doc = [u["pct"] for u in util.values() if u["role"] == "doctor"]
    hyg_pct, doc_pct = (max(hyg) if hyg else 0), (sum(doc) / len(doc) if doc else 0)
    recs = []
    if weekly >= 32 or (hyg_pct >= 85 and weekly >= 16):
        recs.append(f"Hire a second full-time hygienist: about {weekly:.0f} unmet hours a week.")
    elif weekly >= 4 or hyg_pct >= 85:
        recs.append(f"Add a part-time hygienist or extend hygiene hours by ~{max(4, round(weekly)):.0f} h/week.")
    if doc_pct >= 85:
        recs.append("Doctors are above 85% booked: add a doctor day or an associate dentist.")
    if weekly >= 40 and doc_pct >= 85:
        recs.append("Demand exceeds one location: evaluate a second office.")
    if not recs:
        recs.append("Current capacity covers demand. Keep filling gaps with recall calls.")
    return {"unmet_hours_30d": round(demand_hours_30d, 1), "unmet_hours_per_week": round(weekly, 1),
            "recall_backlog_hours": round(backlog_h, 1), "hygienist_utilization_pct": hyg_pct,
            "doctor_utilization_pct": round(doc_pct), "recommendations": recs}


def summary(conn) -> dict:
    now = now_local()
    calls = row(conn, """SELECT COUNT(*) n, COALESCE(SUM(duration_s),0) talk, COALESCE(SUM(revenue),0) rev,
                         SUM(outcome='booked') booked, SUM(outcome='rescheduled') resched FROM calls""")
    tasks_done = row(conn, "SELECT COUNT(*) n FROM tasks WHERE status='done'")["n"]
    queued = row(conn, "SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','in_progress')")["n"]
    unmet = rows(conn, """SELECT u.*, p.name AS patient FROM unmet_demand u JOIN patients p ON p.id=u.patient_id
                          ORDER BY u.id DESC""")
    demand_30 = sum(u["hours"] for u in unmet if u["created_at"] >= iso(now - timedelta(days=30)))
    due = sch.due_patients(conn)
    util = utilization(conn)
    daily = rows(conn, """SELECT substr(started_at,1,10) day, COUNT(*) calls, COALESCE(SUM(revenue),0) revenue
                          FROM calls WHERE started_at >= ? GROUP BY day ORDER BY day""",
                 (iso(now - timedelta(days=13)),))
    recent = rows(conn, """SELECT c.id,c.patient_id,p.name AS patient,c.goal,c.mode,c.started_at,c.duration_s,
                           c.outcome,c.summary,c.revenue FROM calls c JOIN patients p ON p.id=c.patient_id
                           ORDER BY c.id DESC LIMIT 12""")
    return {
        "calls_made": calls["n"], "talk_time_s": round(calls["talk"]), "revenue": calls["rev"],
        "appointments_booked": calls["booked"] or 0, "rescheduled": calls["resched"] or 0,
        "tasks_done": tasks_done, "tasks_queued": queued, "patients_due": len(due),
        "unmet_demand": {"count": len(unmet), "hours": round(sum(u["hours"] for u in unmet), 1), "items": unmet[:10]},
        "capacity": capacity_analysis(demand_30, len(due) * 1.0, util), "utilization": util,
        "daily": daily, "recent_calls": recent,
    }
