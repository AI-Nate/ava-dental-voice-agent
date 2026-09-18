/* Higgs Realtime browser call: mic -> PCM16 24 kHz -> WS; agent audio -> playback worklet.
   Tools run on our backend (POST /calls/{id}/tool); the Boson key never reaches the browser. */
(function () {
  const RATE = 24000;
  const $ = (id) => document.getElementById(id);
  let S = null; // active call state

  function resample(input, from, to) {
    if (from === to) return input;
    const n = Math.round(input.length * to / from);
    const out = new Float32Array(n);
    const step = from / to;
    for (let i = 0; i < n; i++) {
      const x = i * step, j = Math.floor(x), f = x - j;
      const a = input[j] || 0, b = input[j + 1] !== undefined ? input[j + 1] : a;
      out[i] = a + (b - a) * f;
    }
    return out;
  }

  function f32ToB64(f32) {
    const pcm = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    const bytes = new Uint8Array(pcm.buffer);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  function b64ToF32(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
    const out = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 0x8000;
    return out;
  }

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  function line(key, role, text) {
    let el = S.lines[key];
    if (!el) {
      el = document.createElement('div');
      el.className = 'line ' + role;
      el.innerHTML = `<b>${role === 'agent' ? 'Ava' : 'Patient'}</b><span></span>`;
      $('call-transcript').appendChild(el);
      S.lines[key] = el;
      S.order.push({ key, role });
    }
    if (text !== undefined) el.querySelector('span').textContent = text;
    $('call-transcript').scrollTop = 1e9;
    return el;
  }
  function appendLine(key, role, delta) {
    const el = line(key, role);
    const sp = el.querySelector('span');
    sp.textContent = (sp.textContent === '…' ? '' : sp.textContent) + delta;
    $('call-transcript').scrollTop = 1e9;
  }
  function toolLog(html, cls) {
    const el = document.createElement('div');
    el.className = 'tool ' + (cls || '');
    el.innerHTML = `<span class="mono muted">${new Date().toLocaleTimeString()}</span> ${html}`;
    $('call-tools').appendChild(el);
    $('call-tools').scrollTop = 1e9;
  }
  const FAREWELL = /\b(good ?bye|bye|take care|have a (great|good|nice|wonderful|lovely) (day|one|evening|afternoon|weekend|night))\b[\s.!,]*$/i;
  function status(s, cls) { const el = $('call-status'); el.textContent = s; el.className = 'pill ' + (cls || ''); }
  function send(ev) { if (S && S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(ev)); }

  async function start({ patientId, taskId, onEnded }) {
    if (S || P) { window.App.toast('A call is already in progress'); return; }
    S = { lines: {}, order: [], handled: new Set(), onEnded, t0: 0, ended: false, muted: false,
          pushed: 0, played: 0, playing: false, agentItem: null, agentItemStart: 0 };
    $('call-transcript').innerHTML = ''; $('call-tools').innerHTML = '';
    $('call-panel').classList.remove('hidden');
    $('call-title').textContent = 'Starting call…'; $('call-sub').textContent = '';
    status('connecting', 'warn');
    try {
      // Audio first: mic permission must come from the click gesture.
      S.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      S.ctx = new (window.AudioContext || window.webkitAudioContext)();
      await S.ctx.audioWorklet.addModule('capture-processor.js');
      await S.ctx.audioWorklet.addModule('playback-processor.js');
      S.play = new AudioWorkletNode(S.ctx, 'playback-processor');
      S.play.connect(S.ctx.destination);
      S.play.port.onmessage = (e) => { if (e.data.type === 'state') { S.played = e.data.played; S.playing = e.data.playing; } };
      S.src = S.ctx.createMediaStreamSource(S.stream);
      S.cap = new AudioWorkletNode(S.ctx, 'capture-processor');
      S.src.connect(S.cap);
      S.cap.port.onmessage = (e) => {
        if (!S || !S.live || S.muted) return;
        send({ type: 'input_audio_buffer.append', audio: f32ToB64(resample(e.data, S.ctx.sampleRate, RATE)) });
      };

      const c = await VA.call('POST', '/calls', { patient_id: patientId || null, task_id: taskId || null });
      S.call = c;
      $('call-title').textContent = c.patient.name;
      $('call-sub').textContent = `${c.patient.phone} · ${c.goal.replace('_', ' ')} · call #${c.call_id}`;
      toolLog(`session minted (expires ${new Date(c.client_secret.expires_at * 1000).toLocaleTimeString()})`);
      S.ws = new WebSocket(c.ws_url, ['realtime', 'bai-client-secret.' + c.client_secret.value]);
      S.ws.onopen = () => send({ type: 'session.update', session: c.session });
      S.ws.onmessage = (m) => onEvent(JSON.parse(m.data));
      S.ws.onerror = () => toolLog('websocket error', 'err');
      S.ws.onclose = (e) => { if (S && !S.ended) { toolLog(`connection closed (${e.code})`, 'err'); hangup('disconnected'); } };
    } catch (e) {
      toolLog('could not start: ' + esc(e.message), 'err');
      status('failed', 'err');
      await cleanup();
      if (S && S.call) await finish();
      S = null;
    }
  }

  async function onEvent(e) {
    if (!S) return;
    switch (e.type) {
      case 'session.created':
        if (S.live) break;
        S.live = true; S.t0 = Date.now();
        S.timer = setInterval(() => {
          const s = Math.round((Date.now() - S.t0) / 1000);
          $('call-timer').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
        }, 500);
        status('live', 'ok');
        send({ type: 'response.create' }); // outbound call: Ava speaks first
        break;
      case 'response.output_audio.delta': {
        if (S.agentItem !== e.item_id) { S.agentItem = e.item_id; S.agentItemStart = S.pushed; }
        const f = resample(b64ToF32(e.delta), RATE, S.ctx.sampleRate);
        S.pushed += f.length;
        S.play.port.postMessage({ type: 'push', data: f });
        break;
      }
      case 'response.output_audio_transcript.done':
        if (FAREWELL.test(e.transcript || '')) S.closing = true; // goodbye said: hang up after it plays
        break;
      case 'response.output_audio_transcript.delta':
      case 'response.output_text.delta':
        appendLine(e.item_id || e.response_id, 'agent', e.delta || '');
        break;
      case 'input_audio_buffer.speech_started':
        bargeIn();
        line(e.item_id || ('u' + Date.now()), 'patient', '…');
        break;
      case 'conversation.item.input_audio_transcription.completed':
        line(e.item_id, 'patient', e.transcript || '(inaudible)');
        break;
      case 'response.done':
        await onResponseDone(e.response || {});
        break;
      case 'error':
        toolLog('server: ' + esc((e.error && e.error.message) || JSON.stringify(e)), 'err');
        break;
    }
  }

  function bargeIn() {
    if (!S.playing || !S.agentItem) return;
    const ms = Math.max(0, Math.round((S.played - S.agentItemStart) / S.ctx.sampleRate * 1000));
    S.play.port.postMessage({ type: 'flush' });
    send({ type: 'conversation.item.truncate', item_id: S.agentItem, content_index: 0, audio_end_ms: ms });
    S.playing = false;
  }

  async function onResponseDone(resp) {
    const fcs = (resp.output || []).filter((o) => o.type === 'function_call' && !S.handled.has(o.call_id));
    let hangAfter = !!S.closing;
    if (!fcs.length && !hangAfter) return;
    for (const fc of fcs) {
      S.handled.add(fc.call_id);
      let args = {};
      try { args = JSON.parse(fc.arguments || '{}'); } catch (err) { /* sent as {} */ }
      let res;
      try { res = await VA.call('POST', `/calls/${S.call.call_id}/tool`, { name: fc.name, arguments: args }); }
      catch (err) { res = { ok: false, error: err.message }; }
      toolLog(`<b>${esc(fc.name)}</b> <span class="mono">${esc(JSON.stringify(args))}</span> → ` +
              `<span class="${res.ok ? 'okc' : 'errc'}">${esc(summarize(fc.name, res))}</span>`, res.ok ? '' : 'err');
      if (res.confirmation_email) {
        const m = res.confirmation_email;
        toolLog(`<b>send_confirmation_email</b> → <span class="${m.ok ? 'okc' : 'errc'}">${esc(summarize('send_confirmation_email', m))}</span>`, m.ok ? '' : 'err');
      }
      if (fc.name === 'end_call') hangAfter = true;
      send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: fc.call_id, output: JSON.stringify(res) } });
    }
    if (hangAfter) {
      status('wrapping up', 'warn');
      const until = Date.now() + 8000;
      const wait = () => (!S || S.ended) ? null : (S.playing && Date.now() < until ? setTimeout(wait, 250) : hangup('agent ended'));
      setTimeout(wait, 600);
    } else {
      send({ type: 'response.create' });
    }
  }

  function summarize(name, r) {
    if (!r.ok) return 'error: ' + (r.error || 'failed');
    if (r.verified) return 'identity confirmed';
    if (r.sent_to) return `emailed ${r.sent_to}`;
    if (r.slots) return r.slots.length ? r.slots.map((s) => s.when).slice(0, 3).join('; ') + (r.slots.length > 3 ? '…' : '') : 'no openings';
    if (r.booked) return `booked ${r.booked} (+$${r.price})`;
    if (r.moved_to) return `moved to ${r.moved_to}`;
    if (r.cancelled) return `cancelled ${r.cancelled}`;
    if (r.waitlisted) return 'added to waitlist';
    if (r.saved) return `note saved: ${r.saved}`;
    if (r.hang_up) return 'hanging up';
    return 'ok';
  }

  async function cleanup() {
    if (!S) return;
    clearInterval(S.timer);
    try { S.ws && S.ws.close(); } catch (e) { /* closed */ }
    try { S.stream && S.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* stopped */ }
    try { S.ctx && await S.ctx.close(); } catch (e) { /* closed */ }
  }

  async function finish() {
    const transcript = S.order.map(({ key, role }) => ({ role, text: S.lines[key].querySelector('span').textContent }))
      .filter((l) => l.text && l.text !== '…');
    const duration = S.t0 ? (Date.now() - S.t0) / 1000 : 0;
    try { return await VA.call('POST', `/calls/${S.call.call_id}/end`, { transcript, duration_s: duration }); }
    catch (e) { toolLog('could not save call log: ' + esc(e.message), 'err'); return null; }
  }

  async function hangup(reason) {
    if (!S || S.ended) return;
    S.ended = true;
    status('ended', '');
    toolLog('call ended: ' + esc(reason || 'hung up'));
    await cleanup();
    const result = S.call ? await finish() : null;
    const cb = S.onEnded;
    S = null;
    if (result) toolLog(`logged: outcome <b>${esc(result.outcome)}</b>, revenue $${result.revenue || 0}`);
    if (cb) cb(result);
  }

  function sayText(text) {
    if (!S || !S.live) return;
    bargeIn();
    line('t' + Date.now(), 'patient', text);
    send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] } });
    send({ type: 'response.create' });
  }

  // ── phone mode: Twilio dials the patient; the backend bridges audio to Higgs and we poll the call log ──
  let P = null;
  function phoneUi(on) { $('call-say').classList.toggle('hidden', on); $('call-mute').classList.toggle('hidden', on); }

  async function startPhone({ patientId, taskId, onEnded }) {
    if (S || P) { window.App.toast('A call is already in progress'); return; }
    P = { onEnded, lines: 0, tools: 0, t0: Date.now(), answered: 0 };
    S = null;
    $('call-transcript').innerHTML = ''; $('call-tools').innerHTML = '';
    $('call-panel').classList.remove('hidden'); phoneUi(true);
    $('call-title').textContent = 'Placing phone call…'; $('call-sub').textContent = '';
    status('dialing', 'warn');
    try {
      const c = await VA.call('POST', '/calls', { patient_id: patientId || null, task_id: taskId || null, mode: 'phone' });
      P.call = c;
      $('call-title').textContent = c.patient.name;
      $('call-sub').textContent = `📞 ${c.phone.to} from the office line · ${c.goal.replace('_', ' ')} · call #${c.call_id}`;
      pToolLog(`ringing ${esc(c.phone.to)} (Twilio ${esc(c.phone.sid || '')})`);
      P.timer = setInterval(pollPhone, 1000);
    } catch (e) {
      pToolLog(esc(e.message), 'err'); status('refused', 'err');
      P = null; phoneUi(false);
    }
  }
  function pToolLog(html, cls) {
    const el = document.createElement('div');
    el.className = 'tool ' + (cls || '');
    el.innerHTML = `<span class="mono muted">${new Date().toLocaleTimeString()}</span> ${html}`;
    $('call-tools').appendChild(el); $('call-tools').scrollTop = 1e9;
  }
  async function pollPhone() {
    if (!P || P.busy) return;
    P.busy = true;
    try {
      const c = await VA.call('GET', `/calls/${P.call.call_id}`);
      const st = (c.phone && c.phone.status) || 'dialing';
      if (st === 'live' && !P.answered) { P.answered = Date.now(); status('live', 'ok'); }
      if (P.answered) {
        const s = Math.round((Date.now() - P.answered) / 1000);
        $('call-timer').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      }
      for (const l of c.transcript.slice(P.lines)) {
        const el = document.createElement('div');
        el.className = 'line ' + l.role;
        el.innerHTML = `<b>${l.role === 'agent' ? 'Ava' : 'Patient'}</b><span>${esc(l.text)}</span>`;
        $('call-transcript').appendChild(el); $('call-transcript').scrollTop = 1e9;
      }
      P.lines = c.transcript.length;
      for (const ev of c.events.slice(P.tools)) {
        const r = ev.result || {};
        pToolLog(`<b>${esc(ev.tool)}</b> <span class="mono">${esc(JSON.stringify(ev.args || {}))}</span> → ` +
                 `<span class="${r.ok ? 'okc' : 'errc'}">${esc(summarize(ev.tool, r))}</span>`, r.ok ? '' : 'err');
      }
      P.tools = c.events.length;
      if (st === 'ended' || st === 'failed' || c.ended_at) return endPhone(c, st === 'failed' ? 'voice bridge failed' : 'call ended');
      if (st === 'dialing' && Date.now() - P.t0 > 60000) {  // no answer: stop ringing and close the log
        try { await VA.call('POST', `/calls/${P.call.call_id}/hangup`); } catch (e) { /* already gone */ }
        const done = await VA.call('POST', `/calls/${P.call.call_id}/end`, { transcript: [], duration_s: 0 });
        return endPhone(done, 'no answer');
      }
    } catch (e) {
      pToolLog('poll: ' + esc(e.message), 'err');
    } finally { if (P) P.busy = false; }
  }
  function endPhone(result, reason) {
    clearInterval(P.timer);
    status('ended', '');
    pToolLog('call ended: ' + esc(reason));
    if (result) pToolLog(`logged: outcome <b>${esc(result.outcome || 'other')}</b>, revenue $${result.revenue || 0}`);
    const cb = P.onEnded; P = null; phoneUi(false);
    if (cb) cb(result);
  }
  async function hangupPhone() {
    if (!P || !P.call) return;
    pToolLog('hanging up…');
    try { await VA.call('POST', `/calls/${P.call.call_id}/hangup`); } catch (e) { pToolLog(esc(e.message), 'err'); }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('call-hangup').onclick = () => (P ? hangupPhone() : S ? hangup('hung up') : $('call-panel').classList.add('hidden'));
    $('call-mute').onclick = () => { if (!S) return; S.muted = !S.muted; $('call-mute').textContent = S.muted ? 'Unmute mic' : 'Mute mic'; };
    $('call-say').onsubmit = (ev) => { ev.preventDefault(); const t = $('call-text').value.trim(); if (t) { sayText(t); $('call-text').value = ''; } };
  });

  window.Call = { start, startPhone, hangup, active: () => !!(S || P), autoNext: () => $('call-autonext').checked };
})();
