// Scene's own screens for the display: what needs you, what Scene did today, meeting notes, a live
// "taking notes" card and your order's progress. Loaded after app.js and BEFORE followthrough.js
// (see index.html), and plugs into window.Scene's extension points like followthrough.js does.
// Everything comes from the backend (same origin): /inbox /activity /notes /session /orders.
// Lists render as iMessage-style bubbles (see the bubble helpers below): short cream text on
// black, a speaker label when the backend names one, a time, and a "more on phone" hint when
// text is cut. Full detail lives on the phone, never on the lens.
(() => {
  const { state, views, pollers, actions, heroes, navTiles, api, esc, ago } = window.Scene;
  Object.assign(state, { inbox: [], activity: [], notes: [], session: { notes: { active: false } }, orders: [],
                         openNeed: null, openNote: null });

  const nowS = () => Date.now() / 1000;

  // ---- iMessage-style bubbles -------------------------------------------------
  // Lens rows are short chat bubbles, not phone rows: cream body text on black,
  // a speaker label in the row's signal color when we know who said it, a time,
  // and a "more on phone" hint when the text is cut. Full detail lives on the
  // phone, never on the lens. Colors match the phone app: promises rose,
  // tasks sky, answers mint, money butter (same values as app.css).
  // Inline styles only: app.css is owned by another worker, so bubbles here
  // must be self-contained. Sizes use em so the Settings text-size toggle
  // (html.large) still scales them.
  const ACCENT = { promise: '#F0AEC4', ft: '#F0AEC4', task: '#C9D7FF', answer: '#C4E9A8',
                   notes: '#C4E9A8', payment: '#F7D27E', money: '#F7D27E', order: '#F7D27E' };
  const accentFor = kind => ACCENT[kind] || '#FFF8EC';
  // Speaker attribution: only ever from a backend field. Never invent a name.
  // TODO(face-enrollment): face enrollment/matching on the backend will fill
  // counterpart / speaker / from_whom (enrolled face -> name) before these
  // payloads reach the glasses; until then the label simply stays hidden.
  const speakerOf = x => (x && (x.counterpart || x.speaker || x.from_whom)) || null;
  const whoLine = (x, color) => {
    const who = speakerOf(x);
    return who ? `<span style="display:block;font-size:.75em;font-weight:600;color:${color}">${esc(who)} said</span>` : '';
  };
  // Clamp to ~2-3 lines on the 600px lens; returns [text, wasCut].
  const clampText = (t, n) => {
    t = String(t || '');
    return t.length > n ? [t.slice(0, n - 1) + '…', true] : [t, false];
  };
  const moreHint = color => `<span style="display:block;font-size:.72em;font-weight:600;color:${color};margin-top:4px">more on phone ›</span>`;
  // One bubble row: translucent cream fill (black stays see-through), rounded
  // like a message. Left padding leaves room for the .item focus sparkle.
  const bubbleStyle = 'display:block;width:100%;text-align:left;background:rgba(255,248,236,.08);' +
    'border-radius:18px;padding:12px 16px 12px 34px;margin:0 0 10px;';
  const bubbleBody = t => `<span style="display:-webkit-box;font-size:1.08em;line-height:1.25;font-weight:600;color:#FFF8EC;` +
    `-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${esc(t)}</span>`;
  const bubbleMeta = s => `<small style="display:block;font-weight:500;color:rgba(255,248,236,.56);margin-top:4px">${s}</small>`;

  // Shared with followthrough.js (loaded just after this file): same bubbles there.
  window.Scene.bubble = { ACCENT, accentFor, speakerOf, whoLine, clampText, moreHint, bubbleStyle, bubbleBody, bubbleMeta };
  const sameKey = i => i.type + ':' + i.id;
  const post = async (path, json) => {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: json ? JSON.stringify(json) : undefined });
    let d = {};
    try { d = await r.json(); } catch {}
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    return d;
  };

  pollers.push(async () => {
    const get = async (path, key, fallback) => { try { state[key] = await api(path); } catch { state[key] = fallback; } };
    await Promise.all([get('/inbox', 'inbox', []), get('/activity?limit=12', 'activity', []),
                       get('/notes?limit=6', 'notes', []), get('/session', 'session', { notes: { active: false } }),
                       get('/orders?limit=3', 'orders', [])]);
  });

  // ---- Home: live cards (a note session in progress, an order on its way) ----

  heroes.push(() => {
    const n = state.session.notes;
    if (n && n.active) {
      const mins = Math.max(0, Math.round((nowS() - n.started) / 60));
      return `<div class="card answer"><div class="eyebrow">Taking notes</div>
        <div class="title">${mins} min · ${n.slides} slide${n.slides === 1 ? '' : 's'} caught</div>
        <div class="body" style="color:#FFF8EC">Say “stop notes” or pinch Stop.</div>
        <div class="row"><button class="primary" data-act="notes-stop">Stop notes</button></div></div>`;
    }
    return null;
  });

  heroes.push(() => {
    const o = state.orders.find(x => x.stage === 'received' || x.stage === 'preparing' ||
      (x.stage === 'ready' && x.paid_at && nowS() - x.paid_at < 300 && !state.dismissed.has('order-' + x.id)));
    if (!o) return null;
    const line = { received: 'Order received', preparing: 'Preparing your food', ready: 'Ready for pickup' }[o.stage];
    const [itemsShort, itemsCut] = clampText(o.items.map(i => i.qty + '× ' + i.name).join(', ') + ' · Demo kitchen', 90);
    return `<div class="card task"><div class="eyebrow">${esc(o.place_name)}</div>
      <div class="title">${line}</div>
      <div class="body" style="color:#FFF8EC">${esc(itemsShort)}</div>
      ${itemsCut ? moreHint('#C9D7FF') : ''}
      ${o.stage === 'ready' ? `<div class="row"><button class="primary" data-act="order-seen" data-id="${o.id}">OK</button></div>` : ''}</div>`;
  });

  navTiles.push(() => state.inbox.length ? window.Scene.dockItem('needs', 'Needs you', state.inbox.length) : '');
  navTiles.push(() => window.Scene.dockItem('today', 'Today'));
  navTiles.push(() => state.notes.length ? window.Scene.dockItem('notes', 'Notes', state.notes.length) : '');

  // ---- Needs you: everything waiting on a yes, with the backend's ready-made actions ----

  views.needs = () => {
    const items = state.inbox.slice(0, 12).map(i => {
      const color = accentFor(i.type);
      const [title, titleCut] = clampText(i.title, 110);
      const [body, bodyCut] = clampText(i.body || '', 90);
      return `<button class="item" data-act="need-open" data-key="${esc(sameKey(i))}" style="${bubbleStyle}">${whoLine(i, color)}${bubbleBody(title)}
      ${body ? bubbleMeta(esc(body)) : ''}${titleCut || bodyCut ? moreHint(color) : ''}</button>`;
    }).join(''); 
    return `<h2>Needs you</h2>
      <div class="list">${items || '<div class="empty">Nothing waiting on you.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  views.need = () => {
    const i = state.inbox.find(x => sameKey(x) === state.openNeed);
    if (!i) return views.needs();
    const buttons = i.actions.map((a, n) => `<button class="${a.primary ? 'primary' : ''}" data-act="need-do" data-key="${esc(sameKey(i))}" data-n="${n}">${esc(a.label)}</button>`).join('');
    const color = accentFor(i.type);
    const [body, cut] = clampText(i.body || '', 200);   // show what was actually said, not just the first line
    const cardKind = i.type === 'task' ? 'task' : i.type === 'answer' ? 'answer' : (i.type === 'promise' || i.type === 'ft') ? 'ft' : 'payment';
    return `<div class="card ${cardKind}"><div class="eyebrow">${esc(i.title)}</div>
      ${whoLine(i, color)}
      <div class="title">${esc(body)}</div>
      ${cut ? moreHint(color) : ''}
      ${i.link ? `<div class="body" style="color:#FFF8EC">Pay on your phone: ${esc(i.link)}</div>` : ''}
      <div class="row">${buttons}<button data-view="needs">Back</button></div></div>`;
  };

  actions['need-open'] = async ({ key }) => { state.openNeed = key; state.view = 'need';
    const i = state.inbox.find(x => sameKey(x) === key); if (i) needFocus(i); };
  actions['need-do'] = async ({ key, n }) => {
    const i = state.inbox.find(x => sameKey(x) === key);
    const a = i && i.actions[Number(n)];
    if (!a) return;
    const r = await post(a.path, a.json);
    window.Scene.clearFocus();   // acted on: "this" no longer means this item
    state.note = r.result || (r.url ? `Pay on your phone: ${location.origin}/order/${i.id}` : r.accepted === false ? '' : 'Done.');
    state.view = 'needs';
  };

  // "Tell me more about this": the opened / focused Needs-you card is what
  // "this" means. Its body is what was actually said (see the detail view),
  // so for promises it doubles as the quote; other types have no saved quote.
  function needFocus(i) {
    window.Scene.setFocus({ id: sameKey(i), kind: i.type, title: i.title,
      quote: i.quote || (i.type === 'promise' ? i.body || null : null),
      speaker: speakerOf(i), status: i.status || null, action: null,
      due_local: i.due_local || null, body: i.body || '', context: [] });
  }
  window.Scene.needFocus = needFocus;

  // ---- Today: what Scene saw and did ----

  views.today = () => {
    const rows = state.activity.slice(0, 12).map(a => {
      const color = accentFor(a.kind);
      const label = a.kind === 'task' ? (a.status === 'done' ? 'Task done' : 'Task') : a.kind === 'notes' ? 'Meeting notes' : 'Scene';
      const [text, cut] = clampText(a.text, 110);
      return `<div class="item" tabindex="0" style="${bubbleStyle}">${whoLine(a, color)}${bubbleBody(text)}
      ${bubbleMeta(esc(label) + ' · ' + esc(ago(a.ts)))}${cut ? moreHint(color) : ''}</div>`;
    }).join(''); 
    return `<h2>Today</h2>
      <div class="list">${rows || '<div class="empty">Nothing yet today.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  // ---- Meeting notes ----

  views.notes = () => {
    const rows = state.notes.slice(0, 12).map(n => {
      const [summary, cut] = clampText(n.summary, 110);
      return `<button class="item" data-act="note-open" data-id="${n.id}" style="${bubbleStyle}">${whoLine(n, '#C4E9A8')}${bubbleBody(summary)}
      ${bubbleMeta(n.action_items + ' action item' + (n.action_items === 1 ? '' : 's') + ' · ' + n.slides + ' slide' + (n.slides === 1 ? '' : 's') + ' · ' + esc(ago(n.ts)))}${cut ? moreHint('#C4E9A8') : ''}</button>`;
    }).join(''); 
    return `<h2>Meeting notes</h2>
      <div class="list">${rows || '<div class="empty">No notes yet.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  views.note = () => {
    const n = state.openNote;
    if (!n) return views.notes();
    const dec = (n.decisions || []).slice(0, 3).map(d => `<div class="body" style="color:#FFF8EC">• ${esc(clampText(d, 80)[0])}</div>`).join('');
    const act = (n.action_items || []).slice(0, 4).map(a => `<div class="body" style="color:#FFF8EC">☐ ${esc(clampText(a.text, 80)[0])}</div>`).join('');
    const [summary, cut] = clampText(n.summary || 'No summary.', 140);
    return `<div class="card answer"><div class="eyebrow">Meeting notes</div>
      ${whoLine(n, '#C4E9A8')}
      <div class="title">${esc(summary)}</div>
      ${cut ? moreHint('#C4E9A8') : ''}
      ${dec ? `<div class="eyebrow">Decisions</div>${dec}` : ''}${act ? `<div class="eyebrow">Action items</div>${act}` : ''}
      <div class="row"><button class="primary" data-view="notes">Back</button></div></div>`;
  };

  actions['note-open'] = async ({ id }) => { state.openNote = await api('/notes/' + id); state.view = 'note'; };
  actions['notes-stop'] = async () => { await post('/notes/stop'); state.note = 'Notes saved.'; };
  actions['order-seen'] = async ({ id }) => { state.dismissed.add('order-' + id); };
})();
