/* Ava dental voice agent: hash-routed pages (Dashboard, Scheduler, CRM, Settings). */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const money = (n) => '$' + Math.round(n || 0).toLocaleString();
  const mins = (s) => { s = Math.round(s || 0); return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${s % 60}s`; };
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const HOURS = [8, 9, 10, 11, 12, 13, 14, 15, 16];
  const pad = (n) => String(n).padStart(2, '0');
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parse = (s) => new Date(s.length === 10 ? s + 'T00:00' : s);
  const when = (s) => s ? parse(s).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—';
  const day = (s) => s ? parse(s).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
  const hr = (h) => `${h % 12 || 12} ${h < 12 ? 'AM' : 'PM'}`;
  const json = (s, d) => { try { return typeof s === 'string' ? JSON.parse(s) : (s || d); } catch (e) { return d; } };

  const state = { me: null, providers: [], procedures: [], view: 'week', anchor: new Date(), provider: 'all' };

  function toast(msg, err) {
    const t = $('toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), 3500);
  }
  function modal(html) { $('modal-card').innerHTML = html; $('modal').classList.remove('hidden'); }
  function closeModal() { $('modal').classList.add('hidden'); }
  async function api(method, path, body) {
    try { return await VA.call(method, path, body); } catch (e) { toast(e.message, true); throw e; }
  }

  // ── gate ─────────────────────────────────────────────────────────────────
  function gate(kind, info) {
    $('app').classList.add('hidden'); $('gate').classList.remove('hidden');
    const body = $('gate-body');
    if (kind === 'signin') {
      body.innerHTML = '<button id="g-in" class="btn primary">Sign in with Google</button><p class="muted small">Private demo. Access is limited to one account.</p>';
      $('g-in').onclick = () => VA.signIn().catch((e) => toast(e.message, true));
    } else if (kind === 'denied') {
      body.innerHTML = `<div class="denied">Not authorized</div><p class="muted">${esc(info || 'This account')} does not have access to this app.</p><button id="g-out" class="btn ghost">Sign out</button>`;
      $('g-out').onclick = () => VA.signOut();
    } else {
      body.innerHTML = `<p class="errc">${esc(info || 'Something went wrong.')}</p><button id="g-retry" class="btn ghost">Retry</button>`;
      $('g-retry').onclick = () => location.reload();
    }
  }

  async function ready(me) {
    state.me = me;
    $('gate').classList.add('hidden'); $('app').classList.remove('hidden');
    $('nav-user').textContent = me.email;
    const p = await api('GET', '/providers');
    state.providers = p.providers; state.procedures = p.procedures;
    route();
  }

  // ── router ───────────────────────────────────────────────────────────────
  const pages = { dashboard, scheduler, crm, settings };
  function route() {
    if (!state.me) return;
    const [name, arg] = (location.hash.replace('#/', '') || 'dashboard').split('/');
    const page = pages[name] ? name : 'dashboard';
    document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === page));
    $('page').innerHTML = '<p class="muted">Loading…</p>';
    pages[page](arg).catch((e) => { $('page').innerHTML = `<p class="errc">${esc(e.message)}</p>`; });
  }
  function refresh() { if (state.me) route(); }

  // ── calls ────────────────────────────────────────────────────────────────
  function callPatient(patientId, taskId, mode) {
    if (mode === 'phone') {
      const p = (state.patients || []).find((x) => x.id === patientId);
      if (p && !p.dialable) { toast(`Real phone calls are limited to the demo number. Use a browser call for ${p.name}.`); return; }
    }
    (mode === 'phone' ? Call.startPhone : Call.start)({ patientId, taskId, onEnded: (res) => {
      if (res) toast(`Call logged: ${res.outcome}${res.revenue ? ', +' + money(res.revenue) : ''}`);
      refresh();
      if (state.runQueue && Call.autoNext()) setTimeout(runNext, 2500); else state.runQueue = false;
    } });
  }
  async function runNext() {
    const t = (await api('GET', '/tasks')).tasks.find((x) => x.status === 'queued');
    if (!t) { state.runQueue = false; toast('Queue is empty'); return; }
    state.runQueue = true;
    callPatient(t.patient_id, t.id);
  }

  // ── dashboard ────────────────────────────────────────────────────────────
  async function dashboard() {
    const [d, t] = await Promise.all([api('GET', '/dashboard'), api('GET', '/tasks')]);
    const cap = d.capacity;
    const util = Object.entries(d.utilization).map(([n, u]) =>
      `<div class="bar-row"><span>${esc(n)}</span><div class="bar"><i style="width:${Math.min(100, u.pct)}%"></i></div><b>${u.pct}%</b></div>`).join('');
    const maxRev = Math.max(1, ...d.daily.map((x) => x.revenue));
    $('page').innerHTML = `
      <div class="head"><h1>Dashboard</h1><span class="muted">Goal: every patient at 3 covered cleanings a year</span></div>
      <div class="stats">
        ${stat('Calls made', d.calls_made)}${stat('Talk time', mins(d.talk_time_s))}
        ${stat('Tasks done', d.tasks_done, `${d.tasks_queued} queued`)}${stat('Revenue gained', money(d.revenue), `${d.appointments_booked} booked · ${d.rescheduled} moved`)}
        ${stat('Unmet demand', d.unmet_demand.count, `${d.unmet_demand.hours} h requested`)}${stat('Patients due', d.patients_due, 'recall backlog')}
      </div>
      <div class="grid2">
        <div class="card">
          <h3>Call campaigns</h3>
          <p class="muted small">Recall: fewer than 3 cleanings this year, last one over 4 months ago, nothing booked. Gap fill: open slots in the next 3 business days go to waitlisted, then due patients.</p>
          <div class="row">
            <button class="btn" id="q-recall">Queue recall calls</button>
            <button class="btn" id="q-gap">Queue gap fill</button>
            <button class="btn primary" id="q-run">▶ Run queue</button>
            <button class="btn ghost" id="q-clear">Clear queue</button>
          </div>
          <div class="tasks">${t.tasks.length ? t.tasks.map(taskRow).join('') : '<p class="muted small">No queued calls.</p>'}</div>
        </div>
        <div class="card">
          <h3>Capacity analysis</h3>
          <div class="kv"><span>Unmet demand (30 days)</span><b>${cap.unmet_hours_30d} h · ${cap.unmet_hours_per_week} h/week</b></div>
          <div class="kv"><span>Recall backlog</span><b>${cap.recall_backlog_hours} h</b></div>
          <h4>Utilization, next 4 weeks</h4>${util}
          <h4>Recommendation</h4><ul class="recs">${cap.recommendations.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
        </div>
      </div>
      <div class="grid2">
        <div class="card"><h3>Recent calls</h3>
          ${d.recent_calls.map((c) => `<div class="list-row click" data-pid="${c.patient_id}">
            <span><b>${esc(c.patient)}</b> <span class="muted small">${esc(c.goal)} · ${when(c.started_at)} · ${mins(c.duration_s)}</span></span>
            <span class="pill ${c.outcome === 'booked' || c.outcome === 'rescheduled' ? 'ok' : ''}">${esc(c.outcome || '—')}</span></div>`).join('') || '<p class="muted">No calls yet.</p>'}
        </div>
        <div class="card"><h3>Unmet demand</h3>
          ${d.unmet_demand.items.map((u) => `<div class="list-row"><span><b>${esc(u.patient)}</b> <span class="muted small">${esc(u.procedure)} · ${esc(u.preference || '')}</span></span><span class="muted small">${u.hours} h</span></div>`).join('') || '<p class="muted">None logged.</p>'}
          <h4>Revenue by day (14 days)</h4>
          <div class="spark">${d.daily.map((x) => `<i title="${x.day}: ${money(x.revenue)}, ${x.calls} calls" style="height:${Math.max(4, 100 * x.revenue / maxRev)}%"></i>`).join('')}</div>
        </div>
      </div>`;
    $('q-recall').onclick = async () => { const r = await api('POST', '/campaigns/recall', { limit: 10 }); toast(`Queued ${r.queued} recall calls`); refresh(); };
    $('q-gap').onclick = async () => { const r = await api('POST', '/campaigns/gap-fill', { limit: 5 }); toast(`Queued ${r.queued} gap-fill calls`); refresh(); };
    $('q-run').onclick = () => { $('call-autonext').checked = true; runNext(); };
    $('q-clear').onclick = async () => { await api('DELETE', '/tasks'); refresh(); };
    bindTaskRows(); bindPatientLinks();
  }
  const stat = (label, value, sub) => `<div class="stat"><span>${label}</span><b>${esc(value)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}</div>`;
  function taskRow(t) {
    const c = t.context || {};
    const why = t.kind === 'gap_fill' ? `slot: ${c.slot_label}` : t.kind === 'reschedule' ? `was: ${c.appointment_label}` : 'due for cleaning';
    return `<div class="list-row"><span><span class="pill kind-${t.kind}">${t.kind.replace('_', ' ')}</span> <b>${esc(t.patient)}</b> <span class="muted small">${esc(why)}</span></span>
      <span><button class="btn small" data-call="${t.patient_id}" data-task="${t.id}">Call</button> <button class="btn ghost small" data-skip="${t.id}">Skip</button></span></div>`;
  }
  function bindTaskRows() {
    document.querySelectorAll('[data-call]').forEach((b) => (b.onclick = () => callPatient(+b.dataset.call, b.dataset.task ? +b.dataset.task : null)));
    document.querySelectorAll('[data-skip]').forEach((b) => (b.onclick = async () => { await api('POST', `/tasks/${b.dataset.skip}/skip`); refresh(); }));
  }
  function bindPatientLinks() {
    document.querySelectorAll('[data-pid]').forEach((el) => (el.onclick = () => { location.hash = '#/crm/' + el.dataset.pid; }));
  }

  // ── scheduler ────────────────────────────────────────────────────────────
  function weekStart(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; }
  async function scheduler() {
    const a = state.anchor;
    let start, end, title;
    if (state.view === 'week') {
      start = weekStart(a); end = new Date(start); end.setDate(end.getDate() + 5);
      title = `Week of ${start.toLocaleDateString([], { month: 'long', day: 'numeric', year: 'numeric' })}`;
    } else {
      start = weekStart(new Date(a.getFullYear(), a.getMonth(), 1));
      end = new Date(start); end.setDate(end.getDate() + 42);
      title = a.toLocaleDateString([], { month: 'long', year: 'numeric' });
    }
    const data = await api('GET', `/appointments?start=${ymd(start)}&end=${ymd(end)}`);
    const pf = (x) => state.provider === 'all' || String(x.provider_id) === state.provider;
    const appts = data.appointments.filter(pf);
    const color = Object.fromEntries(state.providers.map((p) => [p.id, p.color]));
    const pname = Object.fromEntries(state.providers.map((p) => [p.id, p.name]));
    $('page').innerHTML = `
      <div class="head"><h1>Scheduler</h1>
        <div class="row">
          <select id="s-prov"><option value="all">All providers</option>${state.providers.map((p) => `<option value="${p.id}" ${String(p.id) === state.provider ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <div class="seg"><button data-v="week" class="${state.view === 'week' ? 'on' : ''}">Week</button><button data-v="month" class="${state.view === 'month' ? 'on' : ''}">Month</button></div>
          <button class="btn ghost small" id="s-prev">◀</button><button class="btn ghost small" id="s-today">Today</button><button class="btn ghost small" id="s-next">▶</button>
          <button class="btn danger" id="s-dcancel">Cancel doctor's day</button>
        </div></div>
      <div class="legend">${state.providers.map((p) => `<span><i style="background:${p.color}"></i>${esc(p.name)}</span>`).join('')}<span><i class="open"></i>Open slot (next 5 business days)</span></div>
      <h3>${esc(title)}</h3>
      <div id="cal"></div>
      <div id="wl"></div>`;
    waitlistPanel();
    if (state.view === 'week') weekGrid(start, appts, data.gaps.filter(pf), color);
    else monthGrid(start, a.getMonth(), appts, color);
    $('s-prov').onchange = (e) => { state.provider = e.target.value; refresh(); };
    document.querySelectorAll('.seg button').forEach((b) => (b.onclick = () => { state.view = b.dataset.v; refresh(); }));
    const step = (n) => { const d = new Date(state.anchor); if (state.view === 'week') d.setDate(d.getDate() + 7 * n); else d.setMonth(d.getMonth() + n, 1); state.anchor = d; refresh(); };
    $('s-prev').onclick = () => step(-1); $('s-next').onclick = () => step(1);
    $('s-today').onclick = () => { state.anchor = new Date(); refresh(); };
    $('s-dcancel').onclick = () => doctorCancelModal();
    document.querySelectorAll('[data-appt]').forEach((el) => (el.onclick = () => apptModal(appts.find((x) => x.id === +el.dataset.appt), pname)));
    document.querySelectorAll('[data-day]').forEach((el) => (el.onclick = () => { state.anchor = parse(el.dataset.day); state.view = 'week'; refresh(); }));
  }
  async function waitlistPanel() {
    const { waitlist } = await api('GET', '/waitlist');
    const el = $('wl'); if (!el) return;
    el.innerHTML = `<h3>Waitlist <span class="pill">${waitlist.length}</span></h3>
      <p class="muted small">Patients who asked for a time we didn't have. Gap-fill calls them first when a matching slot opens.</p>
      ${waitlist.length ? `<div class="table-wrap"><table><thead><tr><th>Patient</th><th>Wants</th><th>Booked now</th><th>Added</th><th></th></tr></thead><tbody>
      ${waitlist.map((w) => `<tr><td><a href="#/crm/${w.id}"><b>${esc(w.name)}</b></a></td><td>${esc(w.preference || '')}${w.procedure ? ` <span class="muted small">· ${esc(w.procedure)}</span>` : ''}</td>
        <td>${w.upcoming.length ? w.upcoming.slice(0, 2).map((u) => esc(u.label)).join('<br>') + (w.upcoming.length > 2 ? ` <span class="muted small">+${w.upcoming.length - 2} more</span>` : '') : '<span class="muted">nothing booked</span>'}</td>
        <td class="muted small">${w.since ? when(w.since) + (w.call_id ? ` · call ${w.call_id}` : '') : ''}</td>
        <td><button class="btn ghost small" data-wl="${w.id}">Remove</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">Nobody is waiting.</p>'}`;
    el.querySelectorAll('[data-wl]').forEach((b) => (b.onclick = async () => { await api('DELETE', `/waitlist/${b.dataset.wl}`); toast('Removed from the waitlist'); waitlistPanel(); }));
  }
  function weekGrid(start, appts, gaps, color) {
    const days = [0, 1, 2, 3, 4].map((i) => { const d = new Date(start); d.setDate(d.getDate() + i); return d; });
    const today = ymd(new Date());
    let html = `<div class="week"><div class="wh"></div>${days.map((d) => `<div class="wh ${ymd(d) === today ? 'today' : ''}">${DAYS[d.getDay()]} ${d.getDate()}</div>`).join('')}`;
    for (const h of HOURS) {
      html += `<div class="wt">${hr(h)}</div>`;
      for (const d of days) {
        const key = `${ymd(d)}T${pad(h)}`;
        const here = appts.filter((x) => x.start.startsWith(key));
        const open = gaps.filter((g) => g.start.startsWith(key));
        html += `<div class="wc ${h === 12 ? 'lunch' : ''}">${here.map((x) => apptChip(x, color)).join('')}${open.map((g) => `<div class="chip open" title="Open: ${esc(g.label)}">open · ${esc(g.provider.replace('Dr. ', ''))}</div>`).join('')}</div>`;
      }
    }
    $('cal').innerHTML = html + '</div>';
  }
  function apptChip(x, color) {
    const cancelled = x.status !== 'scheduled' && x.status !== 'completed';
    return `<div class="chip ${cancelled ? 'cx' : ''}" data-appt="${x.id}" style="border-left-color:${color[x.provider_id]}" title="${esc(x.patient)} · ${esc(x.procedure)} · ${esc(x.status)}">
      <b>${esc(x.patient)}</b> <span>${esc(x.procedure)}${cancelled ? ' · ' + esc(x.status.replace('_', ' ')) : ''}</span></div>`;
  }
  function monthGrid(start, month, appts, color) {
    const today = ymd(new Date());
    let html = `<div class="month">${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => `<div class="mh">${d}</div>`).join('')}`;
    for (let i = 0; i < 42; i++) {
      const d = new Date(start); d.setDate(d.getDate() + i);
      const k = ymd(d);
      const here = appts.filter((x) => x.start.startsWith(k) && x.status !== 'rescheduled');
      const by = {};
      here.forEach((x) => { if (x.status === 'scheduled' || x.status === 'completed') by[x.provider_id] = (by[x.provider_id] || 0) + 1; });
      const cx = here.filter((x) => x.status.startsWith('cancelled')).length;
      html += `<div class="mc ${d.getMonth() !== month ? 'dim' : ''} ${k === today ? 'today' : ''}" data-day="${k}"><span class="dn">${d.getDate()}</span>
        ${Object.entries(by).map(([p, n]) => `<div class="mbar" style="background:${color[p]}">${n}</div>`).join('')}${cx ? `<div class="small errc">${cx} cancelled</div>` : ''}</div>`;
    }
    $('cal').innerHTML = html + '</div>';
  }
  function apptModal(x, pname) {
    if (!x) return;
    const live = x.status === 'scheduled';
    modal(`<h3>${esc(x.patient)}</h3>
      <div class="kv"><span>When</span><b>${when(x.start)} – ${parse(x.end).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</b></div>
      <div class="kv"><span>Provider</span><b>${esc(pname[x.provider_id])}</b></div>
      <div class="kv"><span>Procedure</span><b>${esc(x.procedure)} · ${money(x.price)}</b></div>
      <div class="kv"><span>Status</span><b>${esc(x.status)}</b></div>
      <div class="kv"><span>Phone</span><b>${esc(x.phone)}</b></div>
      ${x.note ? `<div class="kv"><span>Note</span><b>${esc(x.note)}</b></div>` : ''}
      <label class="small"><input type="checkbox" id="m-gap" checked> Queue a gap-fill call for the freed slot</label>
      <div class="row end">
        <button class="btn ghost" id="m-close">Close</button>
        <button class="btn" id="m-crm">Open in CRM</button>
        ${live ? '<button class="btn danger" id="m-cxp">Patient cancelled</button><button class="btn danger" id="m-cxo">Office cancel</button>' : ''}
      </div>`);
    $('m-close').onclick = closeModal;
    $('m-crm').onclick = () => { closeModal(); location.hash = '#/crm/' + x.patient_id; };
    const cx = async (by) => {
      const r = await api('POST', `/appointments/${x.id}/cancel`, { by, reason: by === 'office' ? 'office cancelled' : 'patient cancelled', queue_gap_fill: $('m-gap').checked });
      closeModal(); toast(`Cancelled. ${r.gap_fill_tasks.length ? `Gap-fill call queued: ${r.gap_fill_tasks[0].patient}` : 'No gap-fill queued'}`); refresh();
    };
    if (live) { $('m-cxp').onclick = () => cx('patient'); $('m-cxo').onclick = () => cx('office'); }
  }
  function doctorCancelModal() {
    const docs = state.providers;
    const def = new Date(); def.setDate(def.getDate() + 1);
    modal(`<h3>Cancel a doctor's day</h3>
      <p class="muted small">Cancels every appointment in the block and queues a reschedule call for each patient.</p>
      <label>Provider <select id="d-p">${docs.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></label>
      <label>Date <input type="date" id="d-d" value="${ymd(def)}"></label>
      <div class="row"><label>From <select id="d-f">${HOURS.map((h) => `<option value="${h}">${hr(h)}</option>`).join('')}</select></label>
      <label>To <select id="d-t">${HOURS.concat(17).map((h) => `<option value="${h}" ${h === 17 ? 'selected' : ''}>${hr(h)}</option>`).join('')}</select></label></div>
      <div class="row end"><button class="btn ghost" id="d-x">Back</button><button class="btn danger" id="d-go">Cancel block &amp; queue calls</button></div>`);
    $('d-x').onclick = closeModal;
    $('d-go').onclick = async () => {
      const r = await api('POST', '/doctor-cancel', { provider_id: +$('d-p').value, date: $('d-d').value, start_hour: +$('d-f').value, end_hour: +$('d-t').value - 1 });
      closeModal(); toast(`${r.provider}: cancelled ${r.cancelled} appointments, queued ${r.tasks.length} reschedule calls`); refresh();
    };
  }

  // ── CRM ──────────────────────────────────────────────────────────────────
  async function crm(pid) {
    const { patients } = await api('GET', '/patients');
    state.patients = patients;
    $('page').innerHTML = `
      <div class="head"><h1>CRM</h1><div class="row"><input id="c-q" placeholder="Search name or phone"><label class="small"><input type="checkbox" id="c-due"> Due only</label></div></div>
      <div class="crm"><div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Phone</th><th>Next appt</th><th>Last appt</th><th>Cleanings left</th><th>Status</th><th></th></tr></thead>
        <tbody id="c-rows"></tbody></table></div>
        <div id="c-detail" class="detail hidden"></div></div>`;
    const draw = () => {
      const q = $('c-q').value.toLowerCase(), due = $('c-due').checked;
      $('c-rows').innerHTML = patients.filter((p) => (!q || (p.name + p.phone).toLowerCase().includes(q)) && (!due || p.due)).map((p) => `
        <tr data-row="${p.id}"><td><b>${esc(p.name)}</b></td><td class="mono small">${esc(p.phone)}</td><td>${when(p.next_appt)}</td><td>${day(p.last_appt)}</td>
        <td><span class="dots">${'●'.repeat(3 - p.cleanings_left)}${'○'.repeat(p.cleanings_left)}</span> ${p.cleanings_left}</td>
        <td>${p.due ? '<span class="pill warn">due</span>' : ''}${p.waitlist ? '<span class="pill">waitlist</span>' : ''}${p.last_call_outcome ? `<span class="pill">${esc(p.last_call_outcome)}</span>` : ''} <span class="muted small">${esc((p.notes || '').slice(0, 40))}</span></td>
        <td class="nowrap"><button class="btn small ${p.dialable ? 'primary' : 'ghost'}" data-callnow="${p.id}" data-mode="phone" title="${p.dialable ? 'Real call from the office line' : 'Real calls are limited to the demo number'}">📞 Phone</button> <button class="btn small" data-callnow="${p.id}" data-mode="browser">🎙 Browser</button></td></tr>`).join('');
      document.querySelectorAll('[data-row]').forEach((r) => (r.onclick = (e) => { if (!e.target.dataset.callnow) showPatient(+r.dataset.row); }));
      document.querySelectorAll('[data-callnow]').forEach((b) => (b.onclick = () => callPatient(+b.dataset.callnow, null, b.dataset.mode)));
    };
    $('c-q').oninput = draw; $('c-due').onchange = draw; draw();
    if (pid) showPatient(+pid);
  }
  async function showPatient(pid) {
    const p = await api('GET', `/patients/${pid}`);
    const el = $('c-detail'); el.classList.remove('hidden');
    el.innerHTML = `<div class="row between"><h3>${esc(p.name)}</h3><button class="btn ghost small" id="p-x">✕</button></div>
      <div class="kv"><span>Phone</span><b class="mono">${esc(p.phone)}</b></div>
      <div class="kv"><span>Insurance</span><b>${esc(p.insurance)}</b></div>
      <div class="kv"><span>Cleanings this year</span><b>${p.cleanings_done} of 3 (${p.cleanings_left} left)</b></div>
      <div class="kv"><span>Next / last</span><b>${when(p.next_appt)} / ${day(p.last_appt)}</b></div>
      <div class="row"><button class="btn primary" id="p-phone" title="${p.dialable ? 'Real call from the office line' : 'Real calls are limited to the demo number'}">📞 Phone call</button><button class="btn" id="p-call">🎙 Browser call</button></div>
      <label>Status / notes<input id="p-notes" value="${esc(p.notes || '')}"></label>
      <label>Agent memory <span class="muted small">(read before each call, appended after)</span><textarea id="p-mem" rows="5">${esc(p.memory || '')}</textarea></label>
      <button class="btn small" id="p-save">Save</button>
      <h4>Appointments</h4>${p.appointments.slice(0, 12).map((a) => `<div class="list-row small"><span>${when(a.start)} · ${esc(a.procedure)} · ${esc(a.provider)}</span><span class="pill">${esc(a.status)}</span></div>`).join('')}
      <h4>Call history</h4>${p.calls.map((c) => `<details class="callrec"><summary>${when(c.started_at)} · ${esc(c.goal)} · ${mins(c.duration_s)} · <b>${esc(c.outcome || '—')}</b>${c.revenue ? ' · ' + money(c.revenue) : ''}</summary>
        ${c.summary ? `<p class="small">${esc(c.summary)}</p>` : ''}${json(c.transcript, []).map((l) => `<div class="line ${l.role}"><b>${l.role === 'agent' ? 'Ava' : 'Patient'}</b><span>${esc(l.text)}</span></div>`).join('')}
        ${json(c.events, []).map((ev) => `<div class="tool small mono">${esc(ev.tool)} ${esc(JSON.stringify(ev.args || {}))}</div>`).join('')}</details>`).join('') || '<p class="muted small">No calls yet.</p>'}`;
    $('p-x').onclick = () => el.classList.add('hidden');
    $('p-call').onclick = () => callPatient(pid, null, 'browser');
    $('p-phone').onclick = () => callPatient(pid, null, 'phone');
    $('p-save').onclick = async () => { await api('PUT', `/patients/${pid}/memory`, { memory: $('p-mem').value, notes: $('p-notes').value }); toast('Saved'); };
  }

  // ── settings ─────────────────────────────────────────────────────────────
  async function settings() {
    const [{ files }, { patients }, st] = await Promise.all([api('GET', '/files'), api('GET', '/patients'), api('GET', '/settings')]);
    const me = state.me, o = me.office || {};
    $('page').innerHTML = `
      <div class="head"><h1>Settings</h1></div>
      <div class="grid2">
        <div class="card"><h3>Account</h3>
          <div class="kv"><span>Signed in as</span><b>${esc(me.email)}</b></div>
          <div class="kv"><span>Plan</span><b>${esc(me.plan)}</b></div>
          <div class="kv"><span>Office</span><b>${esc(o.name)}</b></div>
          <div class="kv"><span>Address</span><b>${esc(o.address)}</b></div>
          <div class="kv"><span>Hours</span><b>${esc(o.hours)}</b></div>
          <div class="kv"><span>Voice</span><select id="st-voice">${st.voices.map((v) => `<option ${v === st.voice ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div>
          <p class="muted small">Boson AI Higgs Realtime voice for browser and phone calls.</p>
          <button class="btn ghost small" id="st-reset">Reset demo data</button>
        </div>
        <div class="card"><h3>Patient memory</h3>
          <p class="muted small">The agent reads this before a call and appends to it after.</p>
          <select id="st-p">${patients.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
          <textarea id="st-mem" rows="8"></textarea><button class="btn small" id="st-msave">Save memory</button>
        </div>
      </div>
      ${files.map((f) => `<div class="card"><div class="row between"><h3 class="mono">${esc(f.name)}</h3><span class="muted small">updated ${esc(f.updated_at || '')}</span></div>
        <textarea class="mono" rows="10" data-file="${esc(f.name)}">${esc(f.content)}</textarea><button class="btn small" data-save="${esc(f.name)}">Save ${esc(f.name)}</button></div>`).join('')}`;
    const loadMem = () => { const p = patients.find((x) => x.id === +$('st-p').value); $('st-mem').value = p ? p.memory || '' : ''; };
    $('st-p').onchange = loadMem; loadMem();
    $('st-msave').onclick = async () => {
      const id = +$('st-p').value; await api('PUT', `/patients/${id}/memory`, { memory: $('st-mem').value });
      patients.find((x) => x.id === id).memory = $('st-mem').value; toast('Memory saved');
    };
    document.querySelectorAll('[data-save]').forEach((b) => (b.onclick = async () => {
      await api('PUT', `/files/${b.dataset.save}`, { content: document.querySelector(`[data-file="${b.dataset.save}"]`).value });
      toast(`${b.dataset.save} saved`);
    }));
    $('st-voice').onchange = async () => { const r = await api('PUT', '/settings', { voice: $('st-voice').value }); toast(`Voice set to ${r.voice}`); };
    $('st-reset').onclick = async () => { if (confirm('Reset all demo data to the seed?')) { await api('POST', '/reset'); toast('Demo data reset'); refresh(); } };
  }

  // ── boot ─────────────────────────────────────────────────────────────────
  window.App = { toast };
  window.addEventListener('hashchange', route);
  document.addEventListener('DOMContentLoaded', () => {
    $('sign-out').onclick = () => VA.signOut();
    $('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(); };
    VA.init(ready, gate);
  });
})();
