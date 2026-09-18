"""Deterministic demo data: one office, 3 providers, ~40 patients, 12 months of
history and the next 4 weeks of schedule (with gaps). Dates are relative to
today so the demo always looks current."""

import os
import json
import random
from datetime import datetime, timedelta

from .agent_files import DEFAULT_FILES
from .db import iso, now_local

SLOT_HOURS = [8, 9, 10, 11, 13, 14, 15, 16]  # 12:00 is lunch; office closes 17:00

PROCEDURES = [
    ("cleaning", "Cleaning (prophylaxis)", 150, 60),
    ("exam", "Comprehensive exam", 95, 60),
    ("xray", "X-rays (bitewing)", 120, 60),
    ("perio", "Periodontal maintenance", 180, 60),
    ("filling", "Filling", 220, 60),
    ("crown", "Crown", 1200, 120),
    ("root_canal", "Root canal", 1100, 120),
    ("whitening", "Whitening", 400, 60),
]

PROVIDERS = [
    (1, "Dr. Emily Chen", "doctor", "#2563eb"),
    (2, "Dr. Marcus Patel", "doctor", "#7c3aed"),
    (3, "Sarah Kim, RDH", "hygienist", "#0d9488"),
]

NAMES = [
    "Olivia Martinez", "Liam Johnson", "Emma Thompson", "Noah Williams", "Ava Brown",
    "Ethan Davis", "Sophia Garcia", "Mason Rodriguez", "Isabella Wilson", "Lucas Anderson",
    "Mia Taylor", "Logan Thomas", "Charlotte Moore", "James Jackson", "Amelia White",
    "Benjamin Harris", "Harper Martin", "Elijah Lee", "Evelyn Clark", "Henry Lewis",
    "Abigail Walker", "Alexander Hall", "Emily Allen", "Daniel Young", "Ella King",
    "Matthew Wright", "Scarlett Lopez", "David Hill", "Grace Scott", "Joseph Green",
    "Chloe Adams", "Samuel Baker", "Victoria Nelson", "Jack Carter", "Lily Mitchell",
    "Owen Perez", "Zoe Roberts", "Ryan Turner", "Hannah Phillips",
]
MEMORIES = [
    "Prefers mornings.", "Prefers late afternoons after work.", "Nervous about dental visits; be reassuring.",
    "Works night shifts; call after 11am.", "Prefers Dr. Chen.", "Likes texts over calls.",
    "Has two kids who are also patients.", "Asked about whitening last visit.", "",
    "Prefers Fridays.", "",
]
INSURANCE = ["Delta Dental PPO", "MetLife Dental", "Cigna Dental", "Aetna Dental", "Guardian"]

# The demo patient is the one who gets the real phone call. Point these at yourself in .env.
DEMO_NAME = os.getenv("VOICE_AGENT_DEMO_NAME", "Jordan Lee")
DEMO_PHONE = os.getenv("VOICE_AGENT_DEMO_PHONE", "+16505550100")
DEMO_EMAIL = os.getenv("VOICE_AGENT_DEMO_EMAIL", "demo@example.com")


def fake_dob(pid: int) -> str:
    """Deterministic fake date of birth for a seed patient (the demo patient has none on file)."""
    r = random.Random(pid * 7919)
    return f"{r.randint(1955, 2004)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"


def _weekday(d: datetime) -> bool:
    return d.weekday() < 5


class _Book:
    """Slot allocator: finds the nearest free weekday grid slot for a provider."""

    def __init__(self):
        self.taken: set[tuple[int, str]] = set()

    def free(self, pid: int, start: datetime, minutes: int) -> bool:
        return all((pid, iso(start + timedelta(hours=h))) not in self.taken for h in range(minutes // 60))

    def take(self, pid: int, start: datetime, minutes: int) -> None:
        for h in range(minutes // 60):
            self.taken.add((pid, iso(start + timedelta(hours=h))))

    def near(self, rng, pids, day: datetime, minutes: int, spread=10):
        for off in [0] + [s * k for k in range(1, spread) for s in (1, -1)]:
            d = day + timedelta(days=off)
            if not _weekday(d):
                continue
            hours = SLOT_HOURS[:]
            rng.shuffle(hours)
            for h in hours:
                if minutes > 60 and h in (11, 16):
                    continue  # 2h blocks can't straddle lunch or closing
                for pid in pids:
                    s = d.replace(hour=h, minute=0)
                    if self.free(pid, s, minutes):
                        return pid, s
        return None


def seed(conn) -> None:
    rng = random.Random(42)
    today = now_local().replace(hour=0, minute=0)
    created = iso(now_local())
    conn.execute(
        "INSERT INTO office VALUES (1, ?, ?, ?, ?)",
        ("Bright Smile Dental", "1200 El Camino Real, Suite 210, Palo Alto, CA 94306",
         "(650) 555-0142", "Mon-Fri 8:00am-5:00pm (closed 12-1pm for lunch)"),
    )
    conn.executemany("INSERT INTO providers VALUES (?,?,?,?)", PROVIDERS)
    conn.executemany("INSERT INTO procedures VALUES (?,?,?,?)", PROCEDURES)
    conn.executemany(
        "INSERT INTO agent_files VALUES (?,?,?)", [(k, v, created) for k, v in DEFAULT_FILES.items()]
    )
    price = {c: p for c, _, p, _ in PROCEDURES}
    mins = {c: m for c, _, _, m in PROCEDURES}
    book = _Book()
    appts = []

    def add(pid_patient, pids, day, proc, status, source="seed"):
        hit = book.near(rng, pids, day, mins[proc])
        if not hit:
            return
        prov, start = hit
        book.take(prov, start, mins[proc])
        end = start + timedelta(minutes=mins[proc])
        appts.append((pid_patient, prov, iso(start), iso(end), proc, status, price[proc], source, "", created))

    # Patient cohorts drive cleaning history (the recall logic keys off it).
    people = [(DEMO_NAME, DEMO_PHONE, "due")] + [
        (n, f"+1 (650) 555-01{i:02d}", c)
        for i, (n, c) in enumerate(
            zip(NAMES, ["ontrack"] * 14 + ["due"] * 13 + ["lapsed"] * 6 + ["done3"] * 3 + ["waitlist"] * 3)
        )
    ]
    for idx, (name, phone, cohort) in enumerate(people, start=1):
        memory = MEMORIES[idx % len(MEMORIES)]
        notes = ""
        if name == DEMO_NAME:
            memory = "Prefers mornings. Usually free before 10am. Demo patient (real phone)."
        if cohort == "waitlist":
            notes = "On waitlist: wants an earlier cleaning if a slot opens."
        conn.execute(
            "INSERT INTO patients (id,name,phone,email,insurance,status,notes,memory,waitlist,dob) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (idx, name, phone, DEMO_EMAIL if name == DEMO_NAME else name.lower().replace(" ", ".") + "@example.com",
             INSURANCE[idx % len(INSURANCE)], "active" if cohort != "lapsed" else "inactive",
             notes, memory, 1 if cohort == "waitlist" else 0, None if name == DEMO_NAME else fake_dob(idx)),
        )
        cleaners = [3, 1, 2]
        if cohort in ("ontrack", "waitlist"):
            last = today - timedelta(days=rng.randint(60, 110))
            dates = [last, last - timedelta(days=rng.randint(115, 130))]
            nxt = last + timedelta(days=rng.randint(122, 140))
            if cohort == "ontrack" and nxt <= today + timedelta(days=27):
                add(idx, cleaners, nxt, "cleaning", "scheduled")
            elif cohort == "waitlist":
                add(idx, cleaners, today + timedelta(days=rng.randint(22, 27)), "cleaning", "scheduled")
        elif cohort == "due":
            last = today - timedelta(days=rng.randint(135, 230))
            dates = [last] + ([last - timedelta(days=125)] if rng.random() < 0.4 else [])
            if name == DEMO_NAME:
                dates = [today - timedelta(days=150)]
        elif cohort == "lapsed":
            dates = [today - timedelta(days=rng.randint(290, 350))]
        else:  # done3: three cleanings already this year
            dates = [today - timedelta(days=d) for d in (20, 140, 250)]
        for d in dates:
            add(idx, cleaners, d, "cleaning", "completed")
        for _ in range(rng.randint(0, 2)):  # other past treatment
            proc = rng.choice(["exam", "xray", "filling", "crown", "whitening"])
            status = "no_show" if rng.random() < 0.08 else "completed"
            add(idx, [1, 2], today - timedelta(days=rng.randint(5, 360)), proc, status)

    # Non-cleaning load: a busy past month (so the calendar looks lived-in), then dense
    # next week and thinner later, so gaps exist.
    now = now_local()
    for off in range(-28, 29):
        day = today + timedelta(days=off)
        density = 0.6 if off <= 0 else [0.55, 0.42, 0.28, 0.22][min((off - 1) // 7, 3)]
        if not _weekday(day):
            continue
        for h in SLOT_HOURS:
            for pid in (1, 2, 3):
                if rng.random() > density:
                    continue
                proc = rng.choice(["perio", "xray", "exam"] if pid == 3 else ["exam", "filling", "filling", "crown", "root_canal", "whitening"])
                if mins[proc] > 60 and h in (11, 16):
                    proc = "filling"
                start = day.replace(hour=h)
                if not book.free(pid, start, mins[proc]):
                    continue
                book.take(pid, start, mins[proc])
                patient = rng.randint(2, len(people))
                status = "scheduled" if start > now else ("no_show" if rng.random() < 0.06 else "completed")
                appts.append((patient, pid, iso(start), iso(start + timedelta(minutes=mins[proc])),
                              proc, status, price[proc], "seed", "", created))

    conn.executemany(
        "INSERT INTO appointments (patient_id,provider_id,start,end,procedure,status,price,source,note,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        appts,
    )
    _seed_history(conn, rng, today)


def _seed_history(conn, rng, today) -> None:
    """A week of example past calls + unmet demand so the dashboard isn't empty."""
    outcomes = [("booked", 150), ("booked", 150), ("declined", 0), ("voicemail", 0),
                ("waitlisted", 0), ("booked", 150), ("rescheduled", 0), ("no_answer", 0)]
    for i, (outcome, revenue) in enumerate(outcomes):
        started = today - timedelta(days=7 - i, hours=-10 - (i % 5))
        dur = rng.randint(45, 190) if outcome not in ("voicemail", "no_answer") else rng.randint(10, 25)
        transcript = [
            {"role": "agent", "text": "Hi, this is Ava from Bright Smile Dental. You're due for a cleaning. Do you have a minute?"},
            {"role": "patient", "text": "Sure, what do you have?"},
        ]
        conn.execute(
            "INSERT INTO calls (patient_id,task_id,goal,mode,started_at,ended_at,duration_s,outcome,summary,transcript,events,revenue) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rng.randint(2, 40), None, "recall", "seed", iso(started), iso(started + timedelta(seconds=dur)),
             dur, outcome, f"Seed example: {outcome}", json.dumps(transcript), "[]", revenue),
        )
    for pid, pref, hours in [(12, "Tuesday mornings", 1), (25, "Saturday appointments", 1), (33, "Evenings after 5pm", 1)]:
        conn.execute(
            "INSERT INTO unmet_demand (patient_id,call_id,procedure,hours,preference,reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, None, "cleaning", hours, pref, "No slot available in the requested window", iso(today - timedelta(days=3))),
        )
