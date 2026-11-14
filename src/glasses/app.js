// Tell the laptop what the glasses' browser is doing: load, and any error. Shows in the server log
// as GLASSES ... lines, since there's no devtools on the glasses.
function report(msg) {
  try { navigator.sendBeacon('/client-log', String(msg).slice(0, 500)); } catch {}
}
window.addEventListener('error', e => report('error: ' + e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno));
window.addEventListener('unhandledrejection', e => report('promise: ' + (e.reason && e.reason.message || e.reason)));
report('loaded ' + innerWidth + 'x' + innerHeight + ' ' + navigator.userAgent);

// Kept dependency-free: well under the 300 KB first-load budget, one request.
const STAR = '<svg viewBox="0 0 100 100" aria-hidden="true"><path fill="#F7D27E" d="M50 0 C51.5 40 60 48.5 100 50 C60 51.5 51.5 60 50 100 C48.5 60 40 51.5 0 50 C40 48.5 48.5 40 50 0Z"/><path fill="#F7D27E" opacity=".85" d="M50 26 C51 46 54 49 74 50 C54 51 51 54 50 74 C49 54 46 51 26 50 C46 49 49 46 50 26Z" transform="rotate(45 50 50)"/></svg>';
const $app = document.getElementById('app');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const usd = c => '$' + (c / 100).toFixed(2);
const ago = ts => { const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };

let state = { online: false, view: 'home', cards: [], tasks: [], things: [], payments: [], lastCard: 0,
              dismissed: new Set(), note: '', answer: null, asking: '' };

// Extension points. Other files loaded after this one (src/glasses/followthrough.js) add screens,
// data, and actions here instead of editing this file. See docs/FOLLOWTHROUGH-ON-GLASSES.md.
const views = {};      // name -> () => html          (open with <button data-view="name">)
const pollers = [];    // async () => void            (runs every 2 s; write results into state)
const actions = {};    // data-act -> async dataset => void   (a pinch on <button data-act="...">)
const heroes = [];     // () => html | null            (Home's big card; first non-null wins, after payments)
const navTiles = [];   // () => html                   (extra buttons in Home's bottom grid)
window.Scene = { state, views, pollers, actions, heroes, navTiles, api, esc, ago, render };

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
    await Promise.all(pollers.map(p => p().catch(err => report('poller: ' + err.message))));
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
  // The dining agent's question (fresh for 2 minutes) and the order it proposed come before everything else.
  const ask = state.cards.find(c => c.kind === 'confirm' && !state.dismissed.has(c.id) && Date.now() / 1000 - c.ts < 120);
  if (ask) return { kind: 'confirm', card: ask };
  const order = state.cards.find(c => c.kind === 'order' && !state.dismissed.has(c.id));
  if (order) return { kind: 'order', card: order };
  const card = state.cards.find(c => !state.dismissed.has(c.id) && c.kind !== 'payment' && c.kind !== 'offer' && c.kind !== 'confirm' && c.kind !== 'order');
  return card ? { kind: card.kind, card } : null;
}

function home() {
  const s = spotlight();
  const ext = s?.kind === 'payment' ? null : heroes.map(f => f()).find(Boolean);
  let hero;
  if (state.answer) {
    hero = `<div class="card answer"><div class="eyebrow">${esc(state.answer.question)}</div>
      <div class="title">${esc(state.answer.answer)}</div>
      <div class="row"><button class="primary" data-act="clear-answer">OK</button><button data-view="ask">Ask more</button></div></div>`;
  } else if (s?.kind === 'payment') {
    hero = `<div class="card payment"><div class="eyebrow">Needs you</div>
      <div class="big">${usd(s.pay.amount_cents)}</div>
      <div class="body">from ${esc(s.pay.counterparty || 'someone')} · ${esc(s.pay.memo)}</div>
      <div class="row"><button class="primary" data-act="approve" data-id="${s.pay.id}">Approve</button>
      <button data-act="reject" data-id="${s.pay.id}">Not now</button></div></div>`;
  } else if (s?.kind === 'confirm') {
    hero = `<div class="card task"><div class="eyebrow">Scene asks</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="agent-yes" data-card="${s.card.id}">Yes</button>
      <button data-act="agent-no" data-card="${s.card.id}">No</button></div></div>`;
  } else if (s?.kind === 'order') {
    hero = `<div class="card payment"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="order-pay" data-id="${s.card.ref}" data-card="${s.card.id}">Pay</button>
      <button data-act="order-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s?.kind === 'offer') {
    hero = `<div class="card task"><div class="eyebrow">Scene noticed</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="offer-yes" data-id="${s.card.ref}" data-card="${s.card.id}">Yes, take notes</button>
      <button data-act="offer-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s?.kind === 'email') {
    hero = `<div class="card payment"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="email-send" data-id="${s.card.ref}" data-card="${s.card.id}">Send</button>
      <button data-act="email-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (s?.kind === 'calendar') {
    hero = `<div class="card payment"><div class="eyebrow">${esc(s.card.title)}</div>
      <div class="title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="event-add" data-id="${s.card.ref}" data-card="${s.card.id}">Add it</button>
      <button data-act="event-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div></div>`;
  } else if (ext) {
    hero = ext;
  } else if (!s) {
    hero = `<div class="card"><div class="eyebrow">All clear</div>
      <div class="title">Scene is remembering</div>
      <div class="body">Say “hey scene” to ask anything you've seen or heard.</div></div>`;
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
  return hero + (approved ? `<div class="hint">${usd(approved.amount_cents)} from ${esc(approved.counterparty || 'someone')}: QR on your phone</div>` : '') +
    `<div class="grid nav"><button data-view="ask">Ask</button><button data-view="recent">Recent</button>
      <button data-view="tasks">Tasks · ${state.tasks.length}</button><button data-view="things">Things · ${state.things.length}</button>
      ${navTiles.map(f => f()).join('')}</div>`;
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
  const body = ({ home, tasks, things, ask, recent, ...views }[state.view] || home)();
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
    if (actions[act]) await actions[act](el.dataset);
    if (act === 'approve') { await api(`/payments/${id}/approve`, 'POST'); state.note = 'Requested. Show the QR on your phone.'; }
    if (act === 'reject') await api(`/payments/${id}/reject`, 'POST');
    if (act === 'done' && id) await api(`/tasks/${id}/done`, 'POST');
    if (act === 'agent-yes' || act === 'agent-no') await api('/agent/reply', 'POST', { text: act === 'agent-yes' ? 'yes' : 'no' }).catch(() => {});
    if (act === 'order-pay') {
      await api(`/orders/${id}/approve`, 'POST');
      state.note = `Pay on your phone: ${location.origin}/order/${id}`;
    }
    if (act === 'order-no') await api(`/orders/${id}/reject`, 'POST').catch(() => {});
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

// Start after every script has loaded, so extensions are registered before the first render.
// Also share the device position for "nearest McDonald's". Silent when the browser has none or it is denied.
function sendLocation() {
  navigator.geolocation?.getCurrentPosition(
    p => api('/location', 'POST', { lat: p.coords.latitude, lng: p.coords.longitude }).catch(() => {}), () => {}, { maximumAge: 300000 });
}
window.addEventListener('load', () => {
  sendLocation();
  setInterval(sendLocation, 300000);
  poll();
  setInterval(poll, 2000);   // one light poll, not an animation loop: fine for the 30 Hz panel
});
