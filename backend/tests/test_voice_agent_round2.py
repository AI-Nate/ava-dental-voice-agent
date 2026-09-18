"""Voice agent round 2: identity-first opening, confirmation email, voice setting, farewell hangup."""

import json

import pytest

from app import seed, calls, confirm_email, db, phone, scheduling as sch
from app.agent_files import DEFAULT_FILES, opening


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("app.confirm_email.send_mailgun", lambda *a, **k: out.append((a, k)) or (True, None))
    return out


@pytest.fixture
def conn(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("VOICE_AGENT_DB", str(tmp_path / "va.db"))
    with db.connect() as c:
        yield c


def _call(conn, name):
    pid = db.row(conn, "SELECT id FROM patients WHERE name=?", (name,))["id"]
    return calls.start(conn, pid, mode="phone")["call_id"]


def test_opening_is_built_from_identity():
    assert opening(DEFAULT_FILES, "recall") == ("Hi, this is Ava calling from Dr. Emily Chen's office at Bright Smile "
                                                "Dental in Palo Alto, about scheduling your next cleaning.")
    edited = {"IDENTITY.md": DEFAULT_FILES["IDENTITY.md"].replace("Agent name: Ava", "Agent name: Mia")}
    assert opening(edited, "recall").startswith("Hi, this is Mia calling")


def test_instructions_ask_for_identity_and_hold_back_details(conn):
    p = sch.patient_detail(conn, 1)
    _, text = calls.build_instructions(conn, p, None)
    assert f"Am I speaking with {seed.DEMO_NAME}?" in text
    assert p["insurance"] not in text and "Prefers mornings" not in text


def test_tools_are_gated_until_identity_is_confirmed(conn):
    cid = _call(conn, seed.DEMO_NAME)
    assert "identity not confirmed" in calls.run_tool(conn, cid, "get_available_slots", {})["error"]
    assert not calls.run_tool(conn, cid, "verify_identity", {"method": "phone_last4", "value": "1234"})["ok"]
    assert "no date of birth" in calls.run_tool(conn, cid, "verify_identity", {"method": "dob", "value": "1980-01-01"})["error"]
    ok = calls.run_tool(conn, cid, "verify_identity", {"method": "phone_last4", "value": seed.DEMO_PHONE[-4:]})
    assert ok["verified"] and "goal" in ok and "Prefers mornings" in ok["memory"]
    assert calls.run_tool(conn, cid, "get_available_slots", {})["ok"]


def test_booking_emails_the_demo_patient_with_an_ics(conn, sent):
    cid = _call(conn, seed.DEMO_NAME)
    calls.run_tool(conn, cid, "verify_identity", {"method": "name"})
    slot = calls.run_tool(conn, cid, "get_available_slots", {})["slots"][0]["slot_id"]
    res = calls.run_tool(conn, cid, "book_appointment", {"slot_id": slot})
    assert res["confirmation_email"] == {"ok": True, "sent_to": seed.DEMO_EMAIL}
    (to, subject, text, _html), kw = sent[0]
    assert to == seed.DEMO_EMAIL and "Bright Smile Dental" in text and "cancel or reschedule" in text
    name, ics, mime = kw["attachments"][0]
    assert mime == "text/calendar" and b"BEGIN:VEVENT" in ics and b"DTSTART:" in ics
    events = calls.get(conn, cid)["events"]
    assert events[-1]["tool"] == "send_confirmation_email" and events[-1]["result"]["ok"]


def test_example_com_is_never_emailed(conn, sent):
    cid = _call(conn, "Olivia Martinez")
    calls.run_tool(conn, cid, "verify_identity", {"method": "name"})
    slot = calls.run_tool(conn, cid, "get_available_slots", {})["slots"][0]["slot_id"]
    res = calls.run_tool(conn, cid, "book_appointment", {"slot_id": slot})
    assert res["ok"] and res["confirmation_email"]["ok"] is False and sent == []


@pytest.mark.parametrize("addr", ["a@example.com", "A@EXAMPLE.COM", "x@mail.example.org", "", None, "nope"])
def test_undeliverable_addresses(addr):
    assert confirm_email.deliverable(addr)


def test_voice_setting_defaults_to_eleanor_and_reaches_the_session(conn):
    assert calls.get_voice(conn) == "eleanor"
    calls.set_voice(conn, "nora")
    cid = _call(conn, seed.DEMO_NAME)
    assert phone.phone_session(conn, cid)["audio"]["output"]["voice"] == "nora"
    with pytest.raises(ValueError):
        calls.set_voice(conn, "bogus")


@pytest.mark.parametrize("text,bye", [("Perfect, see you then. Bye!", True), ("Have a great day!", True),
                                      ("Goodbye.", True), ("Bye for now, and before you go, one thing?", False),
                                      ("Hi, this is Ava.", False)])
def test_farewell_detection(text, bye):
    assert phone.is_farewell(text) is bye


def test_wrong_person_is_a_valid_outcome(conn):
    cid = _call(conn, seed.DEMO_NAME)
    calls.run_tool(conn, cid, "end_call", {"outcome": "wrong_person"})
    assert calls.get(conn, cid)["outcome"] == "wrong_person"
    assert json.dumps(calls.get(conn, cid)["events"]).count("identity not confirmed") == 0


@pytest.mark.parametrize("said,outcome", [("No, sorry, you have the wrong number.", "wrong_person"),
                                          ("Hi, you've reached Olivia, leave a message.", "voicemail")])
def test_callback_line_without_end_call_sets_outcome(conn, said, outcome):
    cid = calls.start(conn, 2, mode="phone")["call_id"]
    transcript = [{"role": "agent", "text": "Hi, this is Ava... Am I speaking with Olivia Martinez?"},
                  {"role": "patient", "text": said},
                  {"role": "agent", "text": "This is Ava from Bright Smile Dental. Please give us a call back at (650) 555-0142. Thank you, goodbye!"}]
    assert phone.is_farewell(transcript[-1]["text"])
    assert calls.finish(conn, cid, transcript, 20)["outcome"] == outcome
