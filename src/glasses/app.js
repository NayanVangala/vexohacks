// Tell the laptop what the glasses' browser is doing: load, and any error. Shows in the server log
// as GLASSES ... lines, since there's no devtools on the glasses.
function report(msg) {
  try { navigator.sendBeacon('/client-log', String(msg).slice(0, 500)); } catch {}
}
window.addEventListener('error', e => report('error: ' + e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno));
window.addEventListener('unhandledrejection', e => report('promise: ' + (e.reason && e.reason.message || e.reason)));
report('loaded ' + innerWidth + 'x' + innerHeight + ' ' + navigator.userAgent);

// Kept dependency-free: well under the 300 KB first-load budget, one request.
const STAR = '<svg viewBox="0 0 100 100" aria-hidden="true"><path fill="#F7D27E" d="M50 2 C53 38 62 47 98 50 C62 53 53 62 50 98 C47 62 38 53 2 50 C38 47 47 38 50 2Z"/></svg>';
const $app = document.getElementById('app');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const usd = c => '$' + (c / 100).toFixed(2);
const ago = ts => { const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };

let state = { online: false, view: 'home', cards: [], tasks: [], things: [], payments: [], lastCard: 0,
              dismissed: new Set(), note: '', answer: null, asking: '' };

async function api(path, method = 'GET', form) {
  const r = await fetch(path, form ? { method, body: new URLSearchParams(form) } : { method });
  if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch {} throw new Error(d || 'HTTP ' + r.status); }
  return r.json();
}

async function poll() {
  try {
    const fresh = await api('/feed?after=' + state.lastCard);
    if (fresh.length) {
      state.lastCard = Math.max(...fresh.map(c => c.id));
      state.cards = [...fresh.reverse(), ...state.cards].slice(0, 30);
    }
    [state.tasks, state.things, state.payments] = await Promise.all([api('/tasks'), api('/items'), api('/payments')]);
    state.online = true;
  } catch { state.online = false; }
  render();
}

/** The one thing to show on Home: a payment waiting, else the newest unseen card. */
function spotlight() {
  const pay = state.payments.find(p => p.status === 'proposed');
  if (pay) return { kind: 'payment', pay };
  // An offer only matters while it is fresh (the server expires it after 60 s).
  const offer = state.cards.find(c => c.kind === 'offer' && !state.dismissed.has(c.id) && Date.now() / 1000 - c.ts < 60);
  if (offer) return { kind: 'offer', card: offer };
  const card = state.cards.find(c => !state.dismissed.has(c.id) && c.kind !== 'payment' && c.kind !== 'offer');
  return card ? { kind: card.kind, card } : null;
}

function home() {
  const s = spotlight();
  let hero;
  if (state.answer) {
    hero = `<div class="card answer"><div class="eyebrow">${esc(state.answer.question)}</div>
      <div class="title">${esc(state.answer.answer)}</div>
      <div class="row"><button class="primary" data-act="clear-answer">OK</button><button data-view="ask">Ask more</button></div></div>`;
  } else if (!s) {
    hero = `<div class="card"><div class="eyebrow">All clear</div>
      <div class="title">Scene is remembering</div>
      <div class="body">Say “hey scene” to ask anything you've seen or heard.</div></div>`;
  } else if (s.kind === 'payment') {
    hero = `<div class="card payment"><div class="eyebrow">Needs you</div>
      <div class="big">${usd(s.pay.amount_cents)}</div>
      <div class="body">from ${esc(s.pay.counterparty || 'someone')} · ${esc(s.pay.memo)}</div>
      <div class="row"><button class="primary" data-act="approve" data-id="${s.pay.id}">Approve</button>
      <button data-act="reject" data-id="${s.pay.id}">Not now</button></div></div>`;
  } else if (s.kind === 'offer') {
    hero = `<div class="card task"><div class="eyebrow">Scene noticed</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="offer-yes" data-id="${s.card.ref}" data-card="${s.card.id}">Yes, take notes</button>
      <button data-act="offer-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s.kind === 'email') {
    hero = `<div class="card payment"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="email-send" data-id="${s.card.ref}" data-card="${s.card.id}">Send</button>
      <button data-act="email-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s.kind === 'calendar') {
    hero = `<div class="card payment"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="event-add" data-id="${s.card.ref}" data-card="${s.card.id}">Add it</button>
      <button data-act="event-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s.kind === 'task') {
    hero = `<div class="card task"><div class="eyebrow">New task</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="done" data-id="${s.card.ref}" data-card="${s.card.id}">Done</button>
      <button data-act="dismiss" data-card="${s.card.id}">Later</button></div></div>`;
  } else {
    hero = `<div class="card answer"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="dismiss" data-card="${s.card.id}">OK</button></div></div>`;
  }
  const approved = state.payments.find(p => p.status === 'approved');
  return hero + (approved ? `<div class="body">Waiting on ${usd(approved.amount_cents)} from ${esc(approved.counterparty)}: show the QR on your phone.</div>` : '') +
    `<div class="grid nav"><button data-view="ask">Ask</button><button data-view="recent">Recent</button>
      <button data-view="tasks">Tasks · ${state.tasks.length}</button><button data-view="things">Things · ${state.things.length}</button></div>`;
}

function tasks() {
  const items = state.tasks.slice(0, 4).map(t =>
    `<button class="item" data-act="done" data-id="${t.id}">${esc(t.text)}<small>pinch to mark done · ${ago(t.ts)}</small></button>`).join('');
  return `<div class="eyebrow">Tasks</div><div class="list">${items || '<div class="empty">Nothing open.</div>'}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

function things() {
  const items = state.things.slice(0, 4).map(t =>
    `<div class="item" tabindex="0">${esc(t.name[0].toUpperCase() + t.name.slice(1))}<small>${esc(t.place || '')} · ${ago(t.ts)}</small></div>`).join('');
  return `<div class="eyebrow">Things</div><div class="list">${items || '<div class="empty">Nothing seen yet.</div>'}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

/** Questions you can ask with a pinch: the voice-free path. Built from what Scene has actually seen. */
function questions() {
  const qs = ['What do I need to do today?'];
  state.things.slice(0, 2).forEach(t => qs.push(`Where did I leave my ${t.name}?`));
  qs.push('Does anyone owe me money?', 'What did I hear in the last hour?');
  return qs.slice(0, 4);
}

function ask() {
  if (state.asking) return `<div class="eyebrow">Asking</div><div class="title">${esc(state.asking)}</div><div class="body">Thinking…</div>`;
  const items = questions().map(q => `<button class="item" data-act="ask" data-q="${esc(q)}">${esc(q)}</button>`).join('');
  return `<div class="eyebrow">Ask with a pinch</div><div class="list">${items}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

function recent() {
  const items = state.cards.slice(0, 4).map(c =>
    `<div class="item" tabindex="0">${esc(c.kind === 'answer' ? c.body : c.title)}<small>${esc(c.kind === 'answer' ? c.title : c.body)} · ${ago(c.ts)}</small></div>`).join('');
  return `<div class="eyebrow">Recent</div><div class="list">${items || '<div class="empty">Nothing yet today.</div>'}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

let lastHTML = '';
function render() {
  const header = `<header>${STAR}<h1>Scene</h1><span class="status"><span class="dot ${state.online ? '' : 'off'}"></span>${state.online ? 'Live' : 'Offline'}</span></header>`;
  const body = { home, tasks, things, ask, recent }[state.view]();
  const html = header + (state.note ? `<div class="body">${esc(state.note)}</div>` : '') + body;
  if (html === lastHTML) return;   // don't re-render (and steal focus) when nothing changed
  const key = el => (el?.dataset?.act || '') + (el?.dataset?.id || '') + (el?.dataset?.view || '') + (el?.dataset?.q || '');
  const focusKey = key(document.activeElement);
  $app.innerHTML = lastHTML = html;
  const focusables = [...$app.querySelectorAll('button, .item')];
  const keep = focusables.find(el => key(el) === focusKey);
  (keep || focusables[0])?.focus();
}

$app.addEventListener('click', async e => {
  const el = e.target.closest('button, .item');
  if (!el) return;
  const { act, id, card, view, q } = el.dataset;
  state.note = '';
  if (view) { state.view = view; render(); return; }
  try {
    if (act === 'approve') { await api(`/payments/${id}/approve`, 'POST'); state.note = 'Requested. Show the QR on your phone.'; }
    if (act === 'reject') await api(`/payments/${id}/reject`, 'POST');
    if (act === 'done' && id) await api(`/tasks/${id}/done`, 'POST');
    if (act === 'offer-yes') { await api(`/offers/${id}/accept`, 'POST').catch(() => {}); state.note = 'Taking notes.'; }
    if (act === 'offer-no') await api(`/offers/${id}/decline`, 'POST').catch(() => {});
    if (act === 'email-send') {
      const d = await api(`/emails/${id}/send`, 'POST');
      state.note = d.status === 'sent' ? 'Sent. ' + d.result : d.status === 'drafted' ? d.result : 'Not sent: ' + d.result;
    }
    if (act === 'event-add') {
      const d = await api(`/events/${id}/add`, 'POST');
      state.note = d.status === 'sent' ? d.result : d.status === 'drafted' ? d.result : 'Not added: ' + d.result;
    }
    if (act === 'event-no') await api(`/events/${id}/dismiss`, 'POST').catch(() => {});
    if (act === 'email-no') await api(`/emails/${id}/dismiss`, 'POST').catch(() => {});
    if (act === 'clear-answer') { state.answer = null; }
    if (act === 'ask') {
      state.asking = q; render();
      state.answer = await api('/ask', 'POST', { text: q });
      state.view = 'home';
    }
  } catch (err) {
    state.note = /test-mode|STRIPE/.test(err.message) ? 'Payments need a Stripe test key on the laptop.' : 'Couldn’t do that: ' + err.message;
  }
  state.asking = '';
  if (card) state.dismissed.add(Number(card));
  poll();
});

// D-pad: arrows move focus through whatever is on screen; Enter/pinch clicks it.
document.addEventListener('keydown', e => {
  const list = [...$app.querySelectorAll('button, .item')];
  const i = list.indexOf(document.activeElement);
  if (['ArrowDown', 'ArrowRight'].includes(e.key)) { list[(i + 1) % list.length]?.focus(); e.preventDefault(); }
  if (['ArrowUp', 'ArrowLeft'].includes(e.key)) { list[(i - 1 + list.length) % list.length]?.focus(); e.preventDefault(); }
  if (e.key === 'Enter' && document.activeElement?.classList.contains('item')) document.activeElement.click();
  if (e.key === 'Escape' || e.key === 'Backspace') { state.view = 'home'; render(); }
});

poll();
setInterval(poll, 2000);   // one light poll, not an animation loop: fine for the 30 Hz panel
