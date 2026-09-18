"""Voice agent waitlist view: preference from the agent's add_to_waitlist call, current booking, removal."""

import pytest

from app import seed, calls, db, scheduling as sch


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_DB", str(tmp_path / "va.db"))
    with db.connect() as c:
        yield c


def test_add_to_waitlist_shows_preference_and_booking(conn):
    call = calls.start(conn, 1, mode="phone")["call_id"]
    slot = sch.open_slots(conn, limit=1)[0]["slot_id"]
    sch.book(conn, 1, slot)
    calls.run_tool(conn, call, "verify_identity", {"method": "name", "value": seed.DEMO_NAME})
    assert calls.run_tool(conn, call, "add_to_waitlist", {"preference": "Friday morning"})["ok"]
    row = next(w for w in sch.waitlist(conn) if w["id"] == 1)
    assert row["preference"] == "Friday morning" and row["call_id"] == call
    assert slot.split("@")[1] in [u["start"] for u in row["upcoming"]]


def test_removed_patient_leaves_the_waitlist(conn):
    conn.execute("UPDATE patients SET waitlist=0 WHERE id=1")
    assert all(w["id"] != 1 for w in sch.waitlist(conn))
    assert all(w["preference"] for w in sch.waitlist(conn))  # seeded waitlist rows carry a preference
