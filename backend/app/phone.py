"""Real phone calls: Twilio outbound call + Media Streams bridged to Higgs Realtime.

Twilio Media Streams with Higgs Realtime as the voice model. Audio stays G.711 μ-law 8 kHz end to end (Higgs accepts `audio/pcmu`), so the
bridge only relays base64 payloads. The call's TwiML is sent inline with the REST request, so no
Twilio webhook is needed.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time

import httpx

from . import calls, db

logger = logging.getLogger(__name__)

# Hard dial guard: only these numbers can ever be rung. Set VOICE_AGENT_PHONE_ALLOW to your own test phone.
ALLOWED_NUMBERS = {
    n.strip() for n in os.getenv("VOICE_AGENT_PHONE_ALLOW", "").split(",") if n.strip()}
FROM_NUMBER = os.getenv("VOICE_AGENT_FROM_NUMBER", "")
WS_BASE = os.getenv("VOICE_AGENT_PUBLIC_WS_BASE", "")
HIGGS_WS = os.getenv("BOSON_REALTIME_WS", "wss://api.boson.ai/v1/realtime?model=higgs-realtime")
TOKEN_TTL_S = 600
HANGUP_GRACE_S = 10
FAREWELL = re.compile(r"\b(good ?bye|bye|take care|have a (great|good|nice|wonderful|lovely) "
                      r"(day|one|evening|afternoon|weekend|night))\b[\s.!,]*$", re.I)
IDLE_HANGUP_S = float(os.getenv("VOICE_AGENT_IDLE_HANGUP_S", "15"))  # dead air after the last word -> hang up


def e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return "+" + (digits if len(digits) != 10 else "1" + digits)


def dialable(phone: str) -> bool:
    return e164(phone) in ALLOWED_NUMBERS


def is_farewell(text: str) -> bool:
    """The agent's turn ends on a goodbye: the call is over even if the model skips end_call."""
    return bool(FAREWELL.search(text or ""))


def twiml(stream_url: str) -> str:
    return f'<Response><Connect><Stream url="{stream_url}" /></Connect></Response>'


def _twilio(method: str, path: str, data: dict) -> dict:
    sid, token = os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        raise RuntimeError("Twilio credentials are not configured")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/{path}"
    r = httpx.request(method, url, auth=(sid, token), data=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Twilio {r.status_code}: {r.json().get('message', r.text[:200])}")
    return r.json()


def place_call(conn, call_id: int, phone: str) -> dict:
    """Dial an allowlisted number; its audio stream connects back to /voice-agent/phone/stream/<token>."""
    to = e164(phone)
    if to not in ALLOWED_NUMBERS:
        raise PermissionError(f"{phone} is not on the phone-call allowlist; use a browser call")
    token = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO phone_calls (call_id,token,to_number,status,created_at) VALUES (?,?,?,?,?)",
                 (call_id, token, to, "dialing", time.time()))
    conn.commit()  # the stream may reach the other uvicorn worker before this request returns
    res = _twilio("POST", "Calls.json", {"To": to, "From": FROM_NUMBER, "Twiml": twiml(
        f"{WS_BASE}/voice-agent/phone/stream/{token}"), "Timeout": "30"})
    conn.execute("UPDATE phone_calls SET sid=? WHERE call_id=?", (res.get("sid"), call_id))
    return {"to": to, "sid": res.get("sid"), "status": "dialing"}


def claim(conn, token: str) -> int | None:
    """Single-use, time-limited stream token -> call_id."""
    r = db.row(conn, "SELECT call_id FROM phone_calls WHERE token=? AND status='dialing' AND created_at>?",
               (token, time.time() - TOKEN_TTL_S))
    if not r:
        return None
    conn.execute("UPDATE phone_calls SET status='connected' WHERE call_id=?", (r["call_id"],))
    return r["call_id"]


def set_status(call_id: int, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE phone_calls SET status=? WHERE call_id=?", (status, call_id))


def hangup(call_id: int) -> None:
    with db.connect() as conn:
        r = db.row(conn, "SELECT sid FROM phone_calls WHERE call_id=?", (call_id,))
    if r and r["sid"]:
        _twilio("POST", f"Calls/{r['sid']}.json", {"Status": "completed"})


async def connect_higgs():
    import websockets

    key = os.getenv("BOSON_API_KEY", "").strip()
    return await websockets.connect(HIGGS_WS, additional_headers={"Authorization": f"Bearer {key}"},
                                    max_size=None)


class Bridge:
    """Relays one call. `twilio` needs async receive_text()/send_text(); `higgs` is a websockets client."""

    def __init__(self, call_id: int, twilio, higgs, session: dict):
        self.call_id, self.twilio, self.higgs, self.session = call_id, twilio, higgs, session
        self.stream_sid = None
        self.transcript: list[dict] = []
        self.handled: set[str] = set()
        self.hanging = False
        self.t0 = time.monotonic()
        self.audio_out_bytes = 0
        self.quiet_from = time.monotonic()  # when the line last went (or will go) silent

    async def _to_twilio(self, ev: dict) -> None:
        await self.twilio.send_text(json.dumps({**ev, "streamSid": self.stream_sid}))

    async def _to_higgs(self, ev: dict) -> None:
        await self.higgs.send(json.dumps(ev))

    def _say(self, role: str, text: str) -> None:
        if text:
            self.transcript.append({"role": role, "text": text})
            with db.connect() as conn:  # live transcript for the UI poller
                conn.execute("UPDATE calls SET transcript=? WHERE id=?", (json.dumps(self.transcript), self.call_id))

    async def twilio_loop(self) -> None:
        while True:
            msg = json.loads(await self.twilio.receive_text())
            ev = msg.get("event")
            if ev == "start":
                self.stream_sid = msg.get("streamSid") or msg.get("start", {}).get("streamSid")
                self.t0 = time.monotonic()
                await asyncio.to_thread(set_status, self.call_id, "live")
                await self._to_higgs({"type": "response.create"})  # outbound call: Ava speaks first
            elif ev == "media":
                await self._to_higgs({"type": "input_audio_buffer.append", "audio": msg["media"]["payload"]})
            elif ev == "mark" and msg.get("mark", {}).get("name") == "hangup":
                return
            elif ev == "stop":
                return

    async def higgs_loop(self) -> None:
        async for raw in self.higgs:
            e = json.loads(raw)
            t = e.get("type")
            if t == "response.output_audio.delta" and self.stream_sid:
                n = len(e["delta"]) * 3 // 4
                self.audio_out_bytes += n
                self.quiet_from = max(self.quiet_from, time.monotonic()) + n / 8000  # audio plays at 8 kB/s
                await self._to_twilio({"event": "media", "media": {"payload": e["delta"]}})
            elif t in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
                self.quiet_from = time.monotonic()
                if t == "input_audio_buffer.speech_started" and self.stream_sid:
                    await self._to_twilio({"event": "clear"})  # barge-in: drop queued agent audio
            elif t == "conversation.item.input_audio_transcription.completed":
                await asyncio.to_thread(self._say, "patient", e.get("transcript") or "(inaudible)")
            elif t == "response.output_audio_transcript.done":
                await asyncio.to_thread(self._say, "agent", e.get("transcript", ""))
                if is_farewell(e.get("transcript", "")):
                    self.hanging = True  # hang up once this goodbye has played (mark sent on response.done)
            elif t == "response.done":
                await self._on_response_done(e.get("response") or {})
            elif t == "error":
                logger.warning("voice-agent phone %s: higgs error %s", self.call_id, e.get("error"))
            elif t in ("session.max_duration_reached", "session.idle_timeout"):
                logger.info("voice-agent phone %s: %s", self.call_id, t)
                return

    async def idle_loop(self) -> None:
        """Model forgot end_call, or nobody is talking: end the call after IDLE_HANGUP_S of dead air."""
        while True:
            await asyncio.sleep(1)
            if self.stream_sid and time.monotonic() - self.quiet_from > IDLE_HANGUP_S:
                logger.info("voice-agent phone %s: %ss of dead air, hanging up", self.call_id, IDLE_HANGUP_S)
                return

    async def _on_response_done(self, resp: dict) -> None:
        fcs = [o for o in resp.get("output", []) if o.get("type") == "function_call"
               and o.get("call_id") not in self.handled]
        if not fcs:
            if self.hanging and self.stream_sid:  # goodbye audio queued; Twilio echoes the mark after playback
                await self._to_twilio({"event": "mark", "mark": {"name": "hangup"}})
            return
        for fc in fcs:
            self.handled.add(fc["call_id"])
            try:
                args = json.loads(fc.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            res = await asyncio.to_thread(_run_tool, self.call_id, fc["name"], args)
            self.hanging = self.hanging or fc["name"] == "end_call"
            await self._to_higgs({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": fc["call_id"], "output": json.dumps(res)}})
        if self.hanging:
            if self.stream_sid:
                await self._to_twilio({"event": "mark", "mark": {"name": "hangup"}})
        else:
            await self._to_higgs({"type": "response.create"})

    async def run(self) -> dict:
        await self._to_higgs({"type": "session.update", "session": self.session})
        loops = [asyncio.create_task(x) for x in (self.twilio_loop(), self.higgs_loop(), self.idle_loop())]
        try:
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                logger.info("voice-agent phone %s: %s loop ended (%s)", self.call_id,
                            ("twilio", "higgs", "idle")[loops.index(t)], t.exception() or "clean")
            if self.hanging and loops[0] not in done:  # wait for the goodbye to play, then hang up
                await asyncio.wait([loops[0]], timeout=HANGUP_GRACE_S)
        finally:
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await self.higgs.close()
        duration = time.monotonic() - self.t0
        return await asyncio.to_thread(_finish, self.call_id, self.transcript, duration)


def _run_tool(call_id: int, name: str, args: dict) -> dict:
    with db.connect() as conn:
        return calls.run_tool(conn, call_id, name, args)


def _finish(call_id: int, transcript: list, duration: float) -> dict:
    with db.connect() as conn:
        conn.execute("UPDATE phone_calls SET status='ended' WHERE call_id=?", (call_id,))
        return calls.finish(conn, call_id, transcript, duration)


def phone_session(conn, call_id: int) -> dict:
    """Session config for an already-started call (instructions rebuilt from the call's patient + task)."""
    c = db.row(conn, "SELECT patient_id, task_id FROM calls WHERE id=?", (call_id,))
    from . import campaigns, scheduling as sch

    p = sch.patient_detail(conn, c["patient_id"])
    task = campaigns.task(conn, c["task_id"]) if c["task_id"] else None
    _, instructions = calls.build_instructions(conn, p, task)
    return calls.session_config(instructions, phone=True, voice=calls.get_voice(conn))
