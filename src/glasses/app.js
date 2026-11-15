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

// Line icons (24 grid, drawn with currentColor) for the dock and header.
const ICON = {
  ask: '<path d="M4.5 5.5h15v10h-9l-4.5 3.8v-3.8h-1.5z"/>',
  recent: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  tasks: '<circle cx="12" cy="12" r="8.5"/><path d="M8.3 12.4l2.6 2.6 5-5.4"/>',
  things: '<circle cx="8.5" cy="8.5" r="4"/><path d="M11.5 11.5l8 8M16 16l2-2M18.2 18.2l1.6-1.6"/>',
  followups: '<path d="M5 12h12.5M13 7l5 5-5 5"/><path d="M5 7v10"/>',
  needs: '<path d="M4 13.5l2.2-7h11.6l2.2 7v5H4z"/><path d="M4 13.5h4.5l1 2h5l1-2H20"/>',
  today: '<circle cx="12" cy="12" r="3.6"/><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.4 1.4M16.6 16.6L18 18M6 18l1.4-1.4M16.6 7.4L18 6"/>',
  notes: '<path d="M6.5 3.5h8l3.5 3.5v13.5h-11.5z"/><path d="M14.5 3.5V7H18M9 11.5h6M9 15h6"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2.2"/><circle cx="9" cy="17" r="2.2"/>',
};
const icon = name => `<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || ''}</svg>`;
/** One dock entry: icon always, label and count only when focused (the CSS reveals them). */
const dockItem = (view, label, n) =>
  `<button data-view="${view}" aria-label="${label}${n ? ', ' + n : ''}">${icon(view)}${n ? `<span class="n">${n}</span>` : ''}<span class="lbl">${label}</span></button>`;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const usd = c => '$' + (c / 100).toFixed(2);
const ago = ts => { const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };

let state = { online: false, view: 'home', cards: [], tasks: [], things: [], payments: [], lastCard: 0,
              focus: null,   // the Follow-Through / Needs-you / task item on screen: what "this" means
              leadIgnoreBefore: 0, dismissed: new Set(), note: '', answer: null, asking: '',
              // Conversation Mode (see the block after speakerLine): live counts tallied
              // from /feed while a conversation runs; convSummary freezes them when it ends.
              conv: { active: false, available: false, mode: null, startedAt: 0, lastSpeechAt: 0, speaker: null, tasks: 0, lines: 0 },
              convSummary: null };

// Home scroll, volume-style: temple swipe forward/back is volume up/down on the
// Ray-Ban Display, and in this web app it arrives as a touch swipe (or a wheel).
// Swipe down = volume down = scroll the bubble list down one bubble.
// Swipe up = volume up = scroll up one bubble. Arrow keys and the wheel are the
// same move in a browser. D-pad focus movement is separate and untouched below:
// swipe/wheel/arrow scrolls the list, D-pad still moves focus.
let scrollHintSeen = (() => { try { return localStorage.getItem('scene.scrollHint') === '1'; } catch { return false; } })();
function bubbleList() { return $app.querySelector('.bubbles'); }
// The scrollable list on the current screen: Home's bubble feed, or the .list
// on Tasks / Things / Ask / Recent / Needs you / Today / Notes / Promises.
// Home was the only screen that scrolled, so the other lists were stuck at the
// top with items below the fold unreachable (their CSS was overflow: hidden).
// Every list is now scrollable the same way, one item per swipe / wheel notch.
function scrollableEl() { return $app.querySelector('.bubbles') || $app.querySelector('.list'); }
function bubbleStep(el) {
  const first = el.querySelector('.bubble, .item');
  if (!first) return 120;
  const gap = parseFloat(getComputedStyle(el).gap) || 10;
  return first.offsetHeight + gap;
}
function hideScrollHint() {
  if (scrollHintSeen) return;
  scrollHintSeen = true;
  try { localStorage.setItem('scene.scrollHint', '1'); } catch {}
  $app.querySelector('.scroll-hint')?.remove();
}
function scrollCurrent(dir) {
  // dir +1 = down one item, -1 = up one item. Snap, never past either end.
  // Works on every screen, not just Home: the swipe / wheel / arrow handlers
  // below used to return early on any non-Home view.
  const el = scrollableEl();
  if (!el || el.scrollHeight <= el.clientHeight + 1) return false;
  const step = bubbleStep(el);
  const max = el.scrollHeight - el.clientHeight;
  const index = Math.round(el.scrollTop / step);
  const target = Math.max(0, Math.min(max, (index + dir) * step));
  if (target === el.scrollTop) return false;
  hideScrollHint();
  el.scrollTo({ top: target, behavior: 'smooth' });
  return true;
}
function scrollBubbles(dir) { return scrollCurrent(dir); }   // Home-only name, kept for callers

// Extension points. Other files loaded after this one (src/glasses/followthrough.js) add screens,
// data, and actions here instead of editing this file. See docs/FOLLOWTHROUGH-ON-GLASSES.md.
const views = {};      // name -> () => html          (open with <button data-view="name">)
const pollers = [];    // async () => void            (runs every 2 s; write results into state)
const actions = {};    // data-act -> async dataset => void   (a pinch on <button data-act="...">)
const heroes = [];     // () => html | null            (Home's big card; first non-null wins, after payments)
const navTiles = [];   // () => html                   (extra buttons in Home's bottom grid)
window.Scene = { state, views, pollers, actions, heroes, navTiles, api, esc, ago, render, dockItem, icon, setFocus, clearFocus, postFocus };

async function api(path, method = 'GET', form) {
  const r = await fetch(path, form ? { method, body: new URLSearchParams(form) } : { method });
  if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch {} throw new Error(d || 'HTTP ' + r.status); }
  return r.json();
}

// ---- Display focus: "tell me more about this" ----
// The item the wearer is looking at (opened, or D-pad-focused on a list) is
// the only thing "this" can mean. Post it to the backend as the current focus;
// the backend keeps it for 5 min, and "tell me more about this" answers about
// exactly that item, never a guessed one. Re-posted on every display heartbeat
// while it is still on screen, so the 5 min clock stays fresh. Clearing happens
// on approve / dismiss (the item is done) and on display close (server-side).
function setFocus(item) {
  if (!item || !item.id || !item.title) return;
  state.focus = { ...item, id: String(item.id) };
  postFocus();
}
function clearFocus() {
  state.focus = null;
  try { navigator.sendBeacon('/focus/clear'); } catch {}
}
function postFocus() {
  if (!state.focus) return;
  try {
    fetch('/focus', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.focus), keepalive: true }).catch(() => {});
  } catch {}
}
// D-pad focus is "looking at": when a promise / Needs-you row / task takes
// focus, it becomes the current focus too, so "tell me more about this" works
// without opening the card first. Opening still sets it (ft-open / need-open);
// this only covers rows, and it never guesses an item. Re-focusing the same
// row on a poll re-post is idempotent, it just refreshes the 5 min clock.
document.addEventListener('focusin', e => {
  const el = e.target.closest && e.target.closest('button, .item');
  if (!el || !el.dataset) return;
  try {
    if (el.dataset.act === 'ft-open' && state.ft?.items && window.Scene.ftFocus) {
      const i = state.ft.items.find(x => String(x.id) === el.dataset.id);
      if (i) window.Scene.ftFocus(i);
    } else if (el.dataset.act === 'ft-approve' && state.ft?.items && window.Scene.ftFocus) {
      const i = state.ft.items.find(x => String(x.id) === el.dataset.id);
      if (i) window.Scene.ftFocus(i);
    } else if (el.dataset.act === 'need-open' && state.inbox && window.Scene.needFocus) {
      const i = state.inbox.find(x => (x.type + ':' + x.id) === el.dataset.key);
      if (i) window.Scene.needFocus(i);
    } else if (el.dataset.act === 'done' && state.tasks) {
      const t = state.tasks.find(x => String(x.id) === el.dataset.id);
      if (t) setFocus({ id: 'task:' + t.id, kind: 'task', title: t.text, quote: t.quote || null,
        speaker: t.speaker || t.from_whom || null, status: 'open', action: 'task', body: '', context: [] });
    }
  } catch {}
});

async function poll() {
  try {
    const fresh = await api('/feed?after=' + state.lastCard);
    if (fresh.length) {
      state.lastCard = Math.max(...fresh.map(c => c.id));
      state.cards = [...fresh.reverse(), ...state.cards].slice(0, 30);
      // Conversation Mode live counts: tally tasks + transcript lines as they arrive.
      if (state.conv.active) for (const c of fresh) {
        if (state.conv.startedAt && c.ts < state.conv.startedAt) continue;
        if (c.kind === 'task') state.conv.tasks++;
        else if (c.kind === 'transcript') state.conv.lines++;
      }
    }
    let settings;
    [state.tasks, state.things, state.payments, settings] = await Promise.all([api('/tasks'), api('/items'), api('/payments'), api('/settings')]);
    state.mode = settings.output;   // null until the wearer picks: render() then asks
    state.settings = settings;
    // Display Audio Mode: the web app is open, so the DAT camera cannot run.
    // Frames are paused server-side; the iPhone mic keeps listening. The lens
    // shows answers as bubbles in this mode instead of suppressing them.
    state.displayAudioMode = !!settings.display_audio_mode;
    state.audioAgeS = settings.audio_age_s ?? null;
    state.online = true;
    // Conversation Mode (backend: GET /conversation). A 404 means the laptop
    // predates it: keep polling /session + /feed exactly as today, no error.
    try { convUpdate(await api('/conversation')); }
    catch { state.conv.available = false; }
    await Promise.all(pollers.map(p => p().catch(err => report('poller: ' + err.message))));
    // Pollers (scene-views.js) just refreshed state.session: use its
    // conversation copy while GET /conversation itself is missing.
    if (!state.conv.available) convUpdateFromSession();
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
  // A reservation draft waits for a tap, like a calendar or order card. Confirming it only
  // confirms the draft: there is no restaurant booking API, so no table is booked.
  const reservation = state.cards.find(c => c.kind === 'reservation' && !state.dismissed.has(c.id));
  if (reservation) return { kind: 'reservation', card: reservation };
  // In "speaking" mode answers were already said out loud: don't repeat them as cards.
  // Except in Display Audio Mode: the answer bubble IS the visible proof it heard you.
  // Live transcript cards are feed-only, never the hero: they arrive every ~2.5 s
  // while someone talks, and must not steal the slot from a yes/no that needs a tap.
  const card = state.cards.find(c => !state.dismissed.has(c.id) && c.kind !== 'payment' && c.kind !== 'offer' && c.kind !== 'confirm' && c.kind !== 'order'
    && c.kind !== 'transcript'
    && !(state.mode === 'voice' && !state.displayAudioMode && c.kind === 'answer'));
  return card ? { kind: card.kind, card } : null;
}

// ---- Lead me there (backend: src/backend/lead.py + memory.py) ----
// The backend pushes kind=answer cards through /feed:
//   offer: title = the question ("Where are my keys?"),
//          body = "<place>. Want me to lead you there?" - only when a recent frame exists
//          (_can_lead: frame + age <= 180 min; no frame / stale sighting means no question at all)
//   step:  title = "Leading you to <name>", body = the one-line instruction
//   found: title = "Found: <name>", body = the arrival line (or a step saying "I can't find it")
//   stopped: title = "Stopped" (the lead ended: nothing to pin)
// A card that says no photo / no frame / no memory never matches: it carries no lead question
// and no "Leading you to" title, so it renders as-is and is never offered. No location is ever
// invented here: the place shown is the backend's own sentence, minus the question.
// Pinning: the lead card is the hero (flex none, above the scrolling .bubbles list), so it
// cannot scroll away, and it pins even in voice mode - the Yes pinch is the point.
// It pins only while fresh: the backend offer window is about 15 s after Scene finishes
// asking, and steps arrive every few seconds while leading.
const LEAD_OFFER_S = 30;    // offer pin lifetime (backend window is ~15 s + talk time)
const LEAD_STEP_S = 60;     // a step older than this means the lead is over
const LEAD_NO_LEAD_RE = /no (recent )?frame|no photo|don't have a memory|haven't seen/i;
function leadOf(c) {
  if (!c || c.kind !== 'answer' || state.dismissed.has(c.id) || c.ts <= state.leadIgnoreBefore) return null;
  const title = String(c.title || ''), body = String(c.body || '');
  if (LEAD_NO_LEAD_RE.test(title + ' ' + body)) return null;
  if (/^stopped$/i.test(title) || /no more directions/i.test(body)) return 'stopped';
  if (/lead you there/i.test(body)) return 'offer';
  if (/^leading you to /i.test(title)) return /can't find it/i.test(body) ? 'found' : 'step';
  if (/^found: /i.test(title)) return 'found';
  return null;
}
function leadCard() {
  const now = Date.now() / 1000;
  for (const c of state.cards) {   // newest first: the newest lead card decides
    const kind = leadOf(c);
    if (!kind) continue;
    if (kind === 'stopped') return null;   // the lead ended: nothing to pin
    const freshFor = kind === 'offer' ? LEAD_OFFER_S : LEAD_STEP_S;
    if (now - c.ts > freshFor) return null;   // newest lead card is stale: older ones are staler
    return { kind, card: c };
  }
  return null;
}
/** The pinned lead bubble: the offer with Yes / No, a live step with Stop, or the arrival line with OK. */
function leadBubble({ kind, card: c }) {
  const name = (String(c.title || '').match(/^leading you to (.+)/i) || [])[1]
    || (String(c.title || '').match(/^found: (.+)/i) || [])[1] || '';
  if (kind === 'offer') {
    const place = String(c.body || '').replace(/\s*want me to lead you there\??\s*$/i, '').trim() || String(c.body || '');
    return bubble('answer', `<div class="bubble-eyebrow"><span>${esc(c.title || 'Lead me there')}</span><span>${ago(c.ts)}</span></div>${speakerLine(c)}
      <div class="bubble-title">${esc(place)}</div>
      <div class="bubble-body">Want me to lead you there?</div>
      <div class="row"><button class="primary" data-act="lead-yes" data-card="${c.id}">Yes, lead me there</button>
      <button data-act="lead-no" data-card="${c.id}">No</button></div>`, true, 'lead');
  }
  if (kind === 'step') {
    return bubble('answer', `<div class="bubble-eyebrow"><span>Leading you to ${esc(name)}</span><span>${ago(c.ts)}</span></div>${speakerLine(c)}
      <div class="bubble-title">${esc(c.body)}</div>
      <div class="row"><button class="primary" data-act="lead-stop" data-card="${c.id}">Stop</button></div>`, true, 'lead');
  }
  // found (arrived, or "I can't find it"): the lead is over; one tap to clear it.
  return bubble('answer', `<div class="bubble-eyebrow"><span>${esc(c.title)}</span><span>${ago(c.ts)}</span></div>${speakerLine(c)}
    <div class="bubble-title">${esc(c.body)}</div>
    <div class="row"><button class="primary" data-act="lead-done" data-card="${c.id}">OK</button></div>`, true, 'lead');
}
// No /lead route on the backend yet: lead.try_answer only hears transcribed speech
// (memory._lead_audio). Try the route first so these buttons light up the moment the backend
// adds POST /lead/answer {text: yes|no} and POST /lead/stop; a 404 falls back to the working
// path (say it out loud) instead of an error. Never claim the lead started when it didn't.
async function leadPost(path, form) {
  try { await api(path, 'POST', form); return true; }
  catch { return false; }   // no route yet, or anything else: voice is the fallback
}

// TODO(face-enrollment): feed card.speaker from the backend once face enrollment /
// matching lands (enrolled reference photos -> a recognized name -> card.speaker).
// Until then this only fires when the backend already knows who spoke (a named sender,
// a calendar invitee, a follow-through counterpart). Never guess a name here: no field
// means no label, never a made-up one. Do NOT fake face recognition on the lens.
/** Who said it, when the backend knows. Returns null when unknown (no label, never invented). */
function speakerOf(c) {
  if (!c) return null;
  return c.speaker || c.from_whom || c.counterpart || null;
}
const speakerLine = c => { const s = speakerOf(c); return s ? `<div class="bubble-speaker">${esc(s)} said this</div>` : ''; };

// ---- Conversation Mode (backend: GET /conversation, POST /conversation/start|stop;
//      /session also carries it once the backend adds it there) ----
// While a conversation runs the lens is a notepad, not an answer machine: the
// header says so, transcript bubbles name who spoke (or "Unknown", never a
// guess), and tasks extracted mid-conversation appear as draft bubbles the
// moment the feed carries them. No face matching runs while the display is
// open (frames are paused), so a speaker is only a name the backend already
// knew: a face matched before the display opened, or a name spoken aloud.
// Until the backend ships these endpoints (GET /conversation 404s), the UI
// degrades to today's /session + /feed polling and Take notes says so.
const CONV_ACTIVE = ['active', 'listening', 'on', 'taking_notes', 'conversation'];
function convIsActive(d) {
  if (!d || typeof d !== 'object') return false;
  if (d.active === true || d.is_active === true) return true;
  const m = String(d.mode ?? d.status ?? '').toLowerCase();
  return CONV_ACTIVE.includes(m);
}
/** Who spoke: the card's own speaker when it names someone, else the
    conversation's current speaker, else Unknown. Never a guessed name. */
function convSpeakerName(c) {
  const s = speakerOf(c);
  if (s && s !== 'Unknown') return s;
  const cs = state.conv.speaker;
  if (cs && cs !== 'Unknown') return cs;
  return 'Unknown';
}
// In Conversation Mode the speaker line always shows, even for Unknown.
const convSpeakerLine = c => `<div class="bubble-speaker">${esc(convSpeakerName(c))} said this</div>`;
function convUpdate(d) {
  if (!d || typeof d !== 'object') return;
  state.conv.available = true;
  const was = state.conv.active;
  const now = convIsActive(d);
  state.conv.mode = d.mode ?? d.status ?? state.conv.mode;
  state.conv.startedAt = d.started_at ?? d.startedAt ?? state.conv.startedAt;
  state.conv.lastSpeechAt = d.last_speech_at ?? d.lastSpeechAt ?? state.conv.lastSpeechAt;
  const sp = d.speaker ?? d.current_speaker ?? null;
  if (sp) state.conv.speaker = sp;
  if (now && !was) {   // started: (re)seed counts from the cards already on screen
    state.convSummary = null;
    const from = state.conv.startedAt || 0;
    state.conv.tasks = state.cards.filter(c => c.kind === 'task' && c.ts >= from).length;
    state.conv.lines = state.cards.filter(c => c.kind === 'transcript' && c.ts >= from).length;
  }
  // Backend counts win when present; otherwise keep the counters tallied from /feed.
  const n = d.task_count ?? d.tasks_found ?? (Array.isArray(d.tasks) ? d.tasks.length : null);
  if (typeof n === 'number') state.conv.tasks = n;
  const l = d.lines ?? d.line_count ?? null;
  if (typeof l === 'number') state.conv.lines = l;
  if (!now && was) {   // ended: freeze the counts into the summary card
    state.convSummary = { tasks: state.conv.tasks, lines: state.conv.lines, endedAt: Date.now() / 1000 };
  }
  state.conv.active = now;
}
// /session carries conversation mode once the backend adds it there; only used
// while GET /conversation itself 404s, so an old backend never fights the new one.
function convUpdateFromSession() {
  const s = state.session && (state.session.conversation ?? state.session.conversation_mode ?? state.session.conversationMode);
  if (!s) return;
  convUpdate(typeof s === 'string' ? { mode: s } : s);
}
async function convStart() {
  try {
    convUpdate(await api('/conversation/start', 'POST'));
    state.note = state.conv.active ? 'Taking notes.' : 'Started — waiting for the laptop to confirm.';
  } catch (err) {
    state.note = /404/.test(err.message) ? 'Conversation notes need the laptop backend update.'
      : 'Couldn’t start notes: ' + err.message;
  }
}
async function convStop() {
  try {
    convUpdate(await api('/conversation/stop', 'POST'));
    state.note = 'Notes saved. See full notes on phone.';
  } catch (err) {
    state.note = /404/.test(err.message) ? 'Conversation notes need the laptop backend update.'
      : 'Couldn’t stop notes: ' + err.message;
  }
}
/** Home's hero while a conversation runs (live counts + Stop), or the summary
    card after it ends (frozen counts + "See full notes on phone"). */
function conversationHero() {
  if (state.conv.active) {
    const who = state.conv.speaker && state.conv.speaker !== 'Unknown' ? ` · ${state.conv.speaker}` : '';
    return bubble('task', `<div class="bubble-eyebrow"><span>Taking notes${esc(who)}</span><span>${state.conv.tasks} task${state.conv.tasks === 1 ? '' : 's'}</span></div>
      <div class="bubble-title">${state.conv.lines} line${state.conv.lines === 1 ? '' : 's'} heard</div>
      <div class="bubble-body">Listening — tasks and quotes land below as you talk.</div>
      <div class="row"><button class="primary" data-act="conv-stop">Stop notes</button></div>`, true, 'conv');
  }
  const s = state.convSummary;
  if (!s) return null;
  return bubble('task', `<div class="bubble-eyebrow"><span>Conversation ended</span><span>${ago(s.endedAt)}</span></div>
    <div class="bubble-title">${s.tasks} task${s.tasks === 1 ? '' : 's'} · ${s.lines} lines noted</div>
    <div class="bubble-body">See full notes on phone.</div>
    <div class="row"><button class="primary" data-act="conv-dismiss-summary">OK</button></div>`, true, 'conv');
}
/** A task extracted mid-conversation: title, speaker, exact quote, and the
    approve-on-phone hint. The card's body is the quote-verified task text
    ("Send the deck (from Srijan, due 5pm)"); the parenthetical meta is split
    off so the title stays short, and the full wording shows as the quote.
    A backend `quote` field, when it ships, is shown instead of the body. */
function convTaskBubble(c) {
  const body = String(c.body || c.title || '');
  const meta = (body.match(/\((from [^)]+)\)\s*$/) || [])[1] || '';
  const text = body.replace(/\s*\([^)]*\)\s*$/, '').trim();
  const title = (c.title && c.title !== 'New task') ? c.title : text;
  const quote = c.quote || (body && body !== title ? body : null);
  return bubble('task', `<div class="bubble-eyebrow"><span>Task · draft</span><span>${ago(c.ts)}</span></div>${convSpeakerLine(c)}
    <div class="bubble-title">${esc(title)}</div>
    ${quote ? `<div class="bubble-body">“${esc(quote)}”</div>` : ''}
    ${meta ? `<div class="bubble-more">${esc(meta)}</div>` : ''}
    <div class="bubble-more">Draft, tap to approve on phone</div>
    <div class="row"><button data-act="dismiss" data-card="${c.id}">Later</button></div>`, false, 'conv-task');
}

/** Bubble color by kind: money butter, promises rose, tasks sky, answers mint. */
const KIND_CLASS = { payment: 'money', order: 'money', email: 'money', calendar: 'money', slack: 'money', reservation: 'money',
  ft: 'promise', task: 'task', offer: 'task', confirm: 'task', answer: 'answer', transcript: 'answer' };
const KIND_LABEL = { payment: 'Payment', order: 'Order', email: 'Email', calendar: 'Calendar', slack: 'Slack', reservation: 'Reservation',
  ft: 'Promise', task: 'Task', offer: 'Scene noticed', confirm: 'Scene asks', answer: 'Answer', transcript: 'Transcript' };
const kindClass = k => KIND_CLASS[k] || 'answer';
const kindLabel = k => KIND_LABEL[k] || (k ? k[0].toUpperCase() + k.slice(1) : 'Scene');

/** One iMessage-style bubble. Long bodies clamp to 3 lines in CSS; the hint points at the phone. */
const LONG_BODY = 140;
function bubble(kind, inner, hero, extra) {
  return `<article class="bubble ${kindClass(kind)}${hero ? ' hero' : ''}${extra ? ' ' + extra : ''}">${inner}</article>`;
}

/** One summarized point as a bubble: short title/body, speaker when known, tap row only for tasks. */
function feedBubble(c) {
  // Conversation Mode: tasks extracted mid-conversation render as draft bubbles
  // (title, speaker, exact quote, approve-on-phone hint) the moment they arrive.
  if (c.kind === 'task' && state.conv.active && (!state.conv.startedAt || c.ts >= state.conv.startedAt - 1)) return convTaskBubble(c);
  const isAnswer = c.kind === 'answer';
  const title = isAnswer ? c.body : (c.title || c.body);
  const body = isAnswer ? '' : (c.title && c.body && c.body !== c.title ? c.body : '');
  const eyebrow = isAnswer ? (c.title || 'Answer') : kindLabel(c.kind);
  const long = (String(title) + ' ' + String(body)).length > LONG_BODY;
  const acts = c.kind === 'task'
    ? `<div class="row"><button class="primary" data-act="done" data-id="${c.ref}" data-card="${c.id}">Done</button><button data-act="dismiss" data-card="${c.id}">Later</button></div>` : '';
  // In Conversation Mode a transcript bubble always names its speaker: the
  // card's name, the conversation's current speaker, else Unknown.
  const who = c.kind === 'transcript' && state.conv.active ? convSpeakerLine(c) : speakerLine(c);
  return bubble(c.kind, `<div class="bubble-eyebrow"><span>${esc(eyebrow)}</span><span>${ago(c.ts)}</span></div>${who}
    <div class="bubble-title">${esc(title)}</div>
    ${body ? `<div class="bubble-body">${esc(body)}</div>` : ''}
    ${long ? '<div class="bubble-more">more on phone</div>' : ''}${acts}`);
}

function home() {
  const s = spotlight();
  const ext = s?.kind === 'payment' ? null : heroes.map(f => f()).find(Boolean);
  const lead = leadCard();   // pinned above the feed while fresh; null when no lead is live
  const convHero = conversationHero();   // live counts + Stop, or the ended summary card
  let hero = '';
  let usedCardId = null;   // the spotlight card, so it isn't repeated in the feed below
  if (state.answer) {
    hero = bubble('answer', `<div class="bubble-eyebrow"><span>${esc(state.answer.question)}</span></div>
      <div class="bubble-title">${esc(state.answer.answer)}</div>
      <div class="row"><button class="primary" data-act="clear-answer">OK</button><button data-view="ask">Ask more</button></div>`, true);
  } else if (lead) {   // Lead me there pins above everything except your own Ask answer: a fresh offer
    usedCardId = lead.card.id;   // or a live step must not scroll away while you walk to it.
    hero = leadBubble(lead);
  } else if (s?.kind === 'payment') {
    hero = bubble('payment', `<div class="bubble-eyebrow"><span>${esc(s.pay.counterparty || 'Someone')} owes you, for ${esc(s.pay.memo)}</span></div>
      <div class="big">${usd(s.pay.amount_cents)}</div>
      <div class="bubble-body">Request it? Your phone shows them a QR.</div>
      <div class="row"><button class="primary" data-act="approve" data-id="${s.pay.id}">Approve</button>
      <button data-act="reject" data-id="${s.pay.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'confirm') {
    usedCardId = s.card.id;
    hero = bubble('confirm', `<div class="bubble-eyebrow"><span>Scene asks</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="agent-yes" data-card="${s.card.id}">Yes</button>
      <button data-act="agent-no" data-card="${s.card.id}">No</button></div>`, true);
  } else if (s?.kind === 'order') {
    usedCardId = s.card.id;
    hero = bubble('order', `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="order-pay" data-id="${s.card.ref}" data-card="${s.card.id}">Pay</button>
      <button data-act="order-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'offer') {
    usedCardId = s.card.id;
    hero = bubble('offer', `<div class="bubble-eyebrow"><span>Scene noticed</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="offer-yes" data-id="${s.card.ref}" data-card="${s.card.id}">Yes, take notes</button>
      <button data-act="offer-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'slack') {
    usedCardId = s.card.id;
    hero = bubble('slack', `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="slack-send" data-id="${s.card.ref}" data-card="${s.card.id}">Send</button>
      <button data-act="slack-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'email') {
    usedCardId = s.card.id;
    hero = bubble('email', `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="email-send" data-id="${s.card.ref}" data-card="${s.card.id}">Send</button>
      <button data-act="email-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'calendar') {
    usedCardId = s.card.id;
    hero = bubble('calendar', `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="event-add" data-id="${s.card.ref}" data-card="${s.card.id}">Add it</button>
      <button data-act="event-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (s?.kind === 'reservation') {
    usedCardId = s.card.id;
    hero = bubble('reservation', `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="reservation-confirm" data-id="${s.card.ref}" data-card="${s.card.id}">Confirm</button>
      <button data-act="reservation-no" data-id="${s.card.ref}" data-card="${s.card.id}">Not now</button></div>`, true);
  } else if (convHero) {
    hero = convHero;   // Conversation Mode beats the notes-session hero: same job, live counts
  } else if (ext) {
    hero = ext;   // extension cards (notes session, order progress, follow-through) keep their own
                  // markup; CSS gives them the bubble treatment. The spotlight card stays in the feed.
  } else if (s?.kind === 'task') {
    usedCardId = s.card.id;
    hero = bubble('task', `<div class="bubble-eyebrow"><span>You were asked to</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="done" data-id="${s.card.ref}" data-card="${s.card.id}">Done</button>
      <button data-act="dismiss" data-card="${s.card.id}">Later</button></div>`, true);
  } else if (s) {
    usedCardId = s.card.id;
    hero = bubble(s.kind, `<div class="bubble-eyebrow"><span>${esc(s.card.title)}</span><span>${ago(s.card.ts)}</span></div>${speakerLine(s.card)}
      <div class="bubble-title">${esc(s.card.body)}</div>
      <div class="row"><button class="primary" data-act="dismiss" data-card="${s.card.id}">OK</button></div>`, true);
  }
  // The feed under the hero: newest at the bottom, max 4 bubbles on screen (hero + 3, or 4 alone).
  const feed = state.cards
    .filter(c => !state.dismissed.has(c.id) && c.id !== usedCardId && !(state.mode === 'voice' && !state.displayAudioMode && c.kind === 'answer'))
    .slice(0, hero ? 3 : 4).map(feedBubble).join('');
  const approved = state.payments.find(p => p.status === 'approved');
  // Mic-silent honesty: no real (non-silent) audio for 10 s while the display is
  // open means the phone mic went dead after the display opened. Say so on the
  // lens instead of pretending it heard him. audioAgeS is null until any sound.
  const micSilent = state.displayAudioMode && state.audioAgeS != null && state.audioAgeS > 10;
  const micHint = micSilent ? '<div class="bubble-more">Mic silent — tap Start on the phone to retry</div>' : '';
  const calmLine = state.displayAudioMode
    ? 'Listening (audio only, camera off) — say \'hey scene\' anytime'
    : 'Scene is remembering';
  // Take notes / Stop notes, always one pinch away on Home (the hero also has Stop).
  const convRow = `<div class="row"><button class="${state.conv.active ? '' : 'primary'}" data-act="conv-toggle">${state.conv.active ? 'Stop notes' : 'Take notes'}</button></div>`;
  const stage = hero || feed
    ? `<div class="stage bubbles-stage">${micHint}${convRow}${hero}${!scrollHintSeen ? '<div class="scroll-hint">Swipe up/down to scroll</div>' : ''}<div class="bubbles">${feed}</div></div>`
    : `<div class="stage"><div class="calm-line">${calmLine}</div>${micHint}${convRow}</div>`;
  return stage +
    (approved ? `<div class="hint">Waiting on ${usd(approved.amount_cents)} from ${esc(approved.counterparty || 'someone')}. QR is on your phone.</div>` : '') +
    `<nav class="dock">${dockItem('ask', 'Ask')}${dockItem('tasks', 'Tasks', state.tasks.length)}${dockItem('things', 'Things', state.things.length)}${navTiles.map(f => f()).join('')}${dockItem('recent', 'Recent')}</nav>`;
}

function tasks() {
  const items = state.tasks.slice(0, 12).map(t =>
    `<button class="item" data-act="done" data-id="${t.id}">${esc(t.text)}<small>${ago(t.ts)}, pinch when done</small></button>`).join('');
  return `<h2>Tasks</h2><div class="list">${items || '<div class="empty">Nothing open. Promises you make show up here.</div>'}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

function things() {
  const items = state.things.slice(0, 12).map(t =>
    `<div class="item" tabindex="0">${esc(t.name[0].toUpperCase() + t.name.slice(1))}<small>${esc(t.place || '')}, ${ago(t.ts)}</small></div>`).join('');
  return `<h2>Things</h2><div class="list">${items || '<div class="empty">Nothing seen yet. Look around for a few seconds.</div>'}</div>
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
  if (state.asking) return `<div class="stage"><div class="card answer thinking"><div class="eyebrow">Asking</div><div class="title">${esc(state.asking)}</div><div class="body">Thinking…</div></div></div>`;
  const items = questions().map(q => `<button class="item" data-act="ask" data-q="${esc(q)}">${esc(q)}</button>`).join('');
  return `<h2>Ask</h2><div class="list">${items}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

function recent() {
  const items = state.cards.slice(0, 12).map(c =>
    `<div class="item" tabindex="0">${esc(c.kind === 'answer' ? c.body : c.title)}<small>${speakerOf(c) ? esc(speakerOf(c)) + ' · ' : ''}${esc(c.kind === 'answer' ? c.title : c.body)}, ${ago(c.ts)}</small></div>`).join('');
  return `<h2>Recent</h2><div class="list">${items || '<div class="empty">Nothing yet today.</div>'}</div>
    <div class="row nav"><button data-view="home">Back</button></div>`;
}

let lastHTML = '';
let renderedView = null;
/** How Scene talks to you. Asked once (first open), changeable from the header pill. */
const MODES = [
  ['cards', 'Cards', 'Answers and approvals appear here. The laptop stays quiet.'],
  ['voice', 'Speaking', 'Scene answers out loud and asks before acting. Fewer cards.'],
  ['both', 'Both', 'Cards here, and Scene speaks too.'],
];
function mode() {
  const first = state.mode == null;
  const items = MODES.map(([id, name, desc]) =>
    `<button class="item" data-act="set-mode" data-q="${id}">${name}${state.mode === id ? ' · current' : ''}<small>${desc}</small></button>`).join('');
  return `<h2>${first ? 'Welcome to Scene' : 'How Scene talks to you'}</h2>
    <div class="title">How should Scene tell you things?</div>
    <div class="list">${items}</div>
    ${first ? '' : '<div class="row nav"><button data-view="home">Back</button></div>'}`;
}

/** Settings: each row cycles its value on a pinch. Saved on the laptop (except text size, per device). */
const NEXT = (list, cur) => list[(list.indexOf(cur) + 1) % list.length];
const NAMES = { cards: 'Cards', voice: 'Speaking', both: 'Both', glasses: 'Glasses', laptop: 'Laptop' };
let largeText = (() => { try { return localStorage.getItem('scene.largeText') === '1'; } catch { return false; } })();
document.documentElement.classList.toggle('large', largeText);

function settings() {
  const st = state.settings || {};
  const row = (act, name, value, hint) =>
    `<button class="item" data-act="${act}">${name}: ${value}<small>${hint}</small></button>`;
  return `<h2>Settings</h2><div class="list">
    ${row('set-output', 'Tell me with', NAMES[state.mode] || 'Both', 'Cards on the display, speaking, or both')}
    ${row('set-speak-on', 'Speak on', NAMES[st.speak_on] || 'Glasses', 'Where Scene’s voice plays')}
    ${row('set-text', 'Text size', largeText ? 'Large' : 'Normal', 'Easier reading on the lens')}
    ${row('set-paused', 'Remembering', st.paused ? 'Paused' : 'On', st.paused ? 'Camera and mic are ignored' : 'Pinch to pause for privacy')}
  </div><div class="row nav"><button data-view="home">Back</button></div>`;
}

function render() {
  // Display Audio Mode gets its own status so he can tell at a glance whether
  // the camera is also running (Live) or only the mic is (Listening, audio only).
  const modeText = !state.online ? 'Offline' : state.settings?.paused ? 'Paused'
    : state.conv.active ? 'Listening, taking notes'
    : state.displayAudioMode ? 'Listening · audio only' : 'Live';
  const header = `<header>${STAR}<h1>Scene</h1><button class="gear" data-view="settings" aria-label="Settings">${icon('settings')}</button><span class="status"><span class="dot ${state.online ? (state.settings?.paused ? 'paused' : '') : 'off'}"></span>${modeText}</span></header>`;
  const firstRun = state.online && state.mode == null;
  const body = firstRun ? mode() : ({ home, tasks, things, ask, recent, mode, settings, ...views }[state.view] || home)();
  const html = header + (state.note ? `<div class="body">${esc(state.note)}</div>` : '') + body;
  if (html === lastHTML) return;   // don't re-render (and steal focus) when nothing changed
  const key = el => (el?.dataset?.act || '') + (el?.dataset?.id || '') + (el?.dataset?.view || '') + (el?.dataset?.q || '');
  const focusKey = key(document.activeElement);
  const prevScroll = scrollableEl()?.scrollTop ?? 0;   // keep a manual swipe position across polls (any list, not just Home)
  $app.innerHTML = lastHTML = html;
  // Home's bubble list is newest-at-bottom: keep the newest in view, unless a hero
  // bubble needs a tap (then it stays pinned at the top instead of scrolling away).
  // Check for a real hero element, not firstElementChild: the scroll hint and the
  // mic-silent hint also sit above .bubbles and must not count as a hero, or the
  // list would stick at the top (oldest) instead of showing the newest bubble.
  const bubbles = $app.querySelector('.bubbles');
  if (bubbles) {
    const hasHero = !!$app.querySelector('.bubbles-stage > .bubble.hero, .bubbles-stage > .card');
    if (hasHero) bubbles.scrollTop = Math.min(prevScroll, Math.max(0, bubbles.scrollHeight - bubbles.clientHeight));
    else bubbles.scrollTop = bubbles.scrollHeight;
  } else {
    // Any other list (Tasks, Promises, Today, ...): the 2 s poll re-renders the
    // whole screen, so restore the swipe position instead of snapping to the top.
    const list = $app.querySelector('.list');
    if (list) list.scrollTop = Math.min(prevScroll, Math.max(0, list.scrollHeight - list.clientHeight));
  }
  const focusables = [...$app.querySelectorAll('button, .item')];
  // Keep the focused control only when the same screen re-renders (a poll); a new screen starts fresh.
  const sameScreen = renderedView === (firstRun ? 'first-run' : state.view);
  renderedView = firstRun ? 'first-run' : state.view;
  const keep = sameScreen ? focusables.find(el => key(el) === focusKey) : null;
  // New screen: land on its first control, never the header's Settings button.
  (keep || focusables.find(el => !el.closest('header')) || focusables[0])?.focus();
}

$app.addEventListener('click', async e => {
  const el = e.target.closest('button, .item');
  if (!el) return;
  const { act, id, card, view, q } = el.dataset;
  state.note = '';
  if (view) { state.view = view; render(); return; }
  try {
    if (actions[act]) await actions[act](el.dataset);
    if (act === 'conv-toggle') { if (state.conv.active) await convStop(); else await convStart(); }
    if (act === 'conv-stop') await convStop();
    if (act === 'conv-dismiss-summary') state.convSummary = null;
    if (act === 'set-mode') { await api('/settings', 'POST', { output: q }); state.mode = q; state.view = 'home'; }
    if (act === 'set-output') state.settings = await api('/settings', 'POST', { output: NEXT(['cards', 'voice', 'both'], state.mode || 'both') });
    if (act === 'set-speak-on') state.settings = await api('/settings', 'POST', { speak_on: NEXT(['glasses', 'laptop', 'both'], state.settings?.speak_on || 'glasses') });
    if (act === 'set-paused') state.settings = await api('/settings', 'POST', { paused: String(!state.settings?.paused) });
    if (act === 'set-text') {
      largeText = !largeText;
      document.documentElement.classList.toggle('large', largeText);
      try { localStorage.setItem('scene.largeText', largeText ? '1' : '0'); } catch {}
    }
    if (act?.startsWith('set-') && state.settings) state.mode = state.settings.output;
    if (act === 'approve') { await api(`/payments/${id}/approve`, 'POST'); state.note = 'Requested. Show the QR on your phone.'; }
    if (act === 'reject') await api(`/payments/${id}/reject`, 'POST');
    if (act === 'done' && id) await api(`/tasks/${id}/done`, 'POST');
    if (act === 'agent-yes' || act === 'agent-no') await api('/agent/reply', 'POST', { text: act === 'agent-yes' ? 'yes' : 'no' }).catch(() => {});
    if (act === 'lead-yes' || act === 'lead-no') {
      const yes = act === 'lead-yes';
      const handled = await leadPost('/lead/answer', { text: yes ? 'yes' : 'no' });
      state.leadIgnoreBefore = Date.now() / 1000;   // older lead cards must not re-pin
      state.note = handled ? (yes ? 'Leading you there.' : 'Okay, not leading.')
        : 'Say "' + (yes ? 'yes' : 'no') + '" out loud to answer.';
    }
    if (act === 'lead-stop') {
      const handled = await leadPost('/lead/stop');
      state.leadIgnoreBefore = Date.now() / 1000;
      state.note = handled ? 'Stopped.' : 'Say "stop" out loud.';
    }
    if (act === 'lead-done') state.leadIgnoreBefore = Date.now() / 1000;
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
    if (act === 'reservation-confirm') {
      const d = await api(`/reservations/${id}/confirm`, 'POST');
      state.note = d.status === 'drafted' ? 'Draft confirmed. No table booked, call the restaurant to book.' : 'Not confirmed: ' + (d.result || '');
    }
    if (act === 'reservation-no') await api(`/reservations/${id}/dismiss`, 'POST').catch(() => {});
    if (act === 'slack-send') {
      const d = await api(`/slack/${id}/send`, 'POST');
      state.note = d.result;
    }
    if (act === 'slack-no') await api(`/slack/${id}/dismiss`, 'POST').catch(() => {});
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
// ArrowUp/ArrowDown also scroll the current list one item, on every screen, so
// the same move works in a browser as a fallback for the temple swipe.
document.addEventListener('keydown', e => {
  const list = [...$app.querySelectorAll('button, .item')];
  const i = list.indexOf(document.activeElement);
  if (['ArrowDown', 'ArrowRight'].includes(e.key)) { if (e.key === 'ArrowDown') scrollCurrent(1); list[(i + 1) % list.length]?.focus(); e.preventDefault(); }
  if (['ArrowUp', 'ArrowLeft'].includes(e.key)) { if (e.key === 'ArrowUp') scrollCurrent(-1); list[(i - 1 + list.length) % list.length]?.focus(); e.preventDefault(); }
  if (e.key === 'Enter' && document.activeElement?.classList.contains('item')) document.activeElement.click();
  if (e.key === 'Escape' || e.key === 'Backspace') { state.view = 'home'; render(); }
});

// Volume-style swipe, on every screen: temple swipe forward (up) / back (down)
// arrives here as a touch swipe. One swipe = one item, then the gesture ends;
// a second swipe moves another item. Wheel is the same move in a browser,
// throttled to one step. This used to return early on any non-Home view, so
// Tasks / Promises / Today could not be swiped at all.
let swipeY = null, swipeX = null, swipeDone = false;
document.addEventListener('touchstart', e => {
  const t = e.changedTouches[0];
  swipeY = t.clientY; swipeX = t.clientX; swipeDone = false;
}, { passive: true });
document.addEventListener('touchmove', e => {
  if (swipeY == null || swipeDone) return;
  const t = e.changedTouches[0];
  const dy = t.clientY - swipeY, dx = t.clientX - swipeX;
  if (Math.abs(dy) > 24 && Math.abs(dy) > Math.abs(dx) * 1.5) {
    e.preventDefault();   // stop the native scroll so the list snaps one item
    swipeDone = true;
    scrollCurrent(dy > 0 ? 1 : -1);   // swipe down = volume down = list down
  }
}, { passive: false });
document.addEventListener('touchend', e => {
  if (swipeY == null || swipeDone) { swipeY = swipeX = null; return; }
  const t = e.changedTouches[0];
  const dy = t.clientY - swipeY, dx = t.clientX - swipeX;
  if (Math.abs(dy) > 50 && Math.abs(dy) > Math.abs(dx) * 1.5) scrollCurrent(dy > 0 ? 1 : -1);
  swipeY = swipeX = null;
}, { passive: true });
let lastWheelAt = 0;
$app.addEventListener('wheel', e => {
  const el = scrollableEl();
  if (!el || el.scrollHeight <= el.clientHeight + 1) return;
  e.preventDefault();
  const now = Date.now();
  if (now - lastWheelAt < 250 || Math.abs(e.deltaY) < 4) return;   // one notch = one item
  lastWheelAt = now;
  scrollCurrent(e.deltaY > 0 ? 1 : -1);
}, { passive: false });

// Display Audio Mode heartbeat: this web app being open is the only reliable
// signal the display is on (the phone SDK has no signal for it). The backend
// treats a fresh heartbeat as "display open: pause frames, keep the iPhone mic".
// It expires 25 s after the last beat, so a closed or crashed web app resumes
// normal capture on its own. Beacon, not fetch: it must also fire on pagehide.
function displayHeartbeat() {
  try { navigator.sendBeacon('/display/heartbeat'); } catch {}
  postFocus();   // keep the current focus fresh while the display is open
}
window.addEventListener('pagehide', () => {
  try { navigator.sendBeacon('/display/close'); } catch {}
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
  displayHeartbeat();
  setInterval(displayHeartbeat, 15000);
  poll();
  setInterval(poll, 2000);   // one light poll, not an animation loop: fine for the 30 Hz panel
});
