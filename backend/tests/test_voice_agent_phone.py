"""Dial guard for real phone calls: only allowlisted numbers can ever be rung."""

import pytest

from app import phone


@pytest.mark.parametrize("raw", ["+16505550100", "(650) 555-0100", "650-555-0100", "1 650 555 0100"])
def test_demo_number_is_dialable(raw):
    assert phone.dialable(raw)


@pytest.mark.parametrize("raw", ["(650) 555-0101", "+16505550199", "", "6505550109"])
def test_other_numbers_are_refused(raw):
    assert not phone.dialable(raw)


def test_place_call_refuses_before_touching_db_or_twilio():
    with pytest.raises(PermissionError):
        phone.place_call(conn=None, call_id=1, phone="(650) 555-0101")


def test_twiml_connects_stream():
    assert phone.twiml("wss://x/s/t") == '<Response><Connect><Stream url="wss://x/s/t" /></Connect></Response>'


def test_dead_air_hangs_up_when_model_never_calls_end_call(monkeypatch):
    import asyncio
    import json

    class Twilio:  # sends start, then nothing: the line is silent
        def __init__(self):
            self.sent = [json.dumps({"event": "start", "streamSid": "MZ1"})]

        async def receive_text(self):
            if self.sent:
                return self.sent.pop()
            await asyncio.sleep(3600)

        async def send_text(self, _):
            pass

    class Higgs:
        async def send(self, _):
            pass

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(phone, "IDLE_HANGUP_S", 1.5)
    monkeypatch.setattr(phone, "set_status", lambda *_: None)
    monkeypatch.setattr(phone, "_finish", lambda cid, tr, d: {"call_id": cid, "duration": d})
    res = asyncio.run(asyncio.wait_for(phone.Bridge(7, Twilio(), Higgs(), {}).run(), timeout=10))
    assert res["call_id"] == 7 and res["duration"] < 5


def test_events_summary_covers_calls_without_end_call():
    from app.calls import events_summary

    events = [{"tool": "get_available_slots", "result": {"ok": True, "slots": []}},
              {"tool": "book_appointment", "result": {"ok": True, "booked": "Friday Oct 2 at 9 AM with Sarah Kim, RDH"}},
              {"tool": "book_appointment", "result": {"ok": False, "error": "taken"}}]
    assert events_summary(events) == "Booked Friday Oct 2 at 9 AM with Sarah Kim, RDH."
    assert events_summary([]) == ""
    verified = [{"tool": "verify_identity", "result": {"ok": True, "verified": True}}]
    assert events_summary(verified) == "Identity confirmed; nothing booked or changed."
