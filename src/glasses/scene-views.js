// Scene's own screens for the display: what needs you, what Scene did today, meeting notes, a live
// "taking notes" card and your order's progress. Loaded after app.js and BEFORE followthrough.js
// (see index.html), and plugs into window.Scene's extension points like followthrough.js does.
// Everything comes from the backend (same origin): /inbox /activity /notes /session /orders.
(() => {
  const { state, views, pollers, actions, heroes, navTiles, api, esc, ago } = window.Scene;
  Object.assign(state, { inbox: [], activity: [], notes: [], session: { notes: { active: false } }, orders: [],
                         openNeed: null, openNote: null });

  const nowS = () => Date.now() / 1000;
  const short = (t, n) => (t.length > n ? t.slice(0, n - 1) + '…' : t);
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
        <div class="body">Say “stop notes” or pinch Stop.</div>
        <div class="row"><button class="primary" data-act="notes-stop">Stop notes</button></div></div>`;
    }
    return null;
  });

  heroes.push(() => {
    const o = state.orders.find(x => x.stage === 'received' || x.stage === 'preparing' ||
      (x.stage === 'ready' && x.paid_at && nowS() - x.paid_at < 300 && !state.dismissed.has('order-' + x.id)));
    if (!o) return null;
    const line = { received: 'Order received', preparing: 'Preparing your food', ready: 'Ready for pickup' }[o.stage];
    return `<div class="card task"><div class="eyebrow">${esc(o.place_name)}</div>
      <div class="title">${line}</div>
      <div class="body">${o.items.map(i => esc(i.qty + '× ' + i.name)).join(', ')} · Demo kitchen</div>
      ${o.stage === 'ready' ? `<div class="row"><button class="primary" data-act="order-seen" data-id="${o.id}">OK</button></div>` : ''}</div>`;
  });

  navTiles.push(() => state.inbox.length ? window.Scene.dockItem('needs', 'Needs you', state.inbox.length) : '');
  navTiles.push(() => window.Scene.dockItem('today', 'Today'));
  navTiles.push(() => state.notes.length ? window.Scene.dockItem('notes', 'Notes', state.notes.length) : '');

  // ---- Needs you: everything waiting on a yes, with the backend's ready-made actions ----

  views.needs = () => {
    const items = state.inbox.slice(0, 4).map(i => `<button class="item" data-act="need-open" data-key="${esc(sameKey(i))}">${esc(i.title)}
      <small>${esc(short(i.body || '', 60))}</small></button>`).join('');
    return `<h2>Needs you</h2>
      <div class="list">${items || '<div class="empty">Nothing waiting on you.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  views.need = () => {
    const i = state.inbox.find(x => sameKey(x) === state.openNeed);
    if (!i) return views.needs();
    const buttons = i.actions.map((a, n) => `<button class="${a.primary ? 'primary' : ''}" data-act="need-do" data-key="${esc(sameKey(i))}" data-n="${n}">${esc(a.label)}</button>`).join('');
    return `<div class="card payment"><div class="eyebrow">${esc(i.title)}</div>
      <div class="title">${esc(i.body || '')}</div>
      ${i.link ? `<div class="body">Pay on your phone: ${esc(i.link)}</div>` : ''}
      <div class="row">${buttons}<button data-view="needs">Back</button></div></div>`;
  };

  actions['need-open'] = async ({ key }) => { state.openNeed = key; state.view = 'need'; };
  actions['need-do'] = async ({ key, n }) => {
    const i = state.inbox.find(x => sameKey(x) === key);
    const a = i && i.actions[Number(n)];
    if (!a) return;
    const r = await post(a.path, a.json);
    state.note = r.result || (r.url ? `Pay on your phone: ${location.origin}/order/${i.id}` : r.accepted === false ? '' : 'Done.');
    state.view = 'needs';
  };

  // ---- Today: what Scene saw and did ----

  views.today = () => {
    const rows = state.activity.slice(0, 6).map(a => `<div class="item" tabindex="0">${esc(short(a.text, 70))}
      <small>${esc(a.kind === 'task' ? (a.status === 'done' ? 'Task done' : 'Task') : a.kind === 'notes' ? 'Meeting notes' : 'Scene')} · ${esc(ago(a.ts))}</small></div>`).join('');
    return `<h2>Today</h2>
      <div class="list">${rows || '<div class="empty">Nothing yet today.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  // ---- Meeting notes ----

  views.notes = () => {
    const rows = state.notes.slice(0, 4).map(n => `<button class="item" data-act="note-open" data-id="${n.id}">${esc(short(n.summary, 70))}
      <small>${n.action_items} action item${n.action_items === 1 ? '' : 's'} · ${n.slides} slide${n.slides === 1 ? '' : 's'} · ${esc(ago(n.ts))}</small></button>`).join('');
    return `<h2>Meeting notes</h2>
      <div class="list">${rows || '<div class="empty">No notes yet.</div>'}</div>
      <div class="row nav"><button data-view="home">Back</button></div>`;
  };

  views.note = () => {
    const n = state.openNote;
    if (!n) return views.notes();
    const dec = (n.decisions || []).slice(0, 3).map(d => `<div class="body">• ${esc(short(d, 80))}</div>`).join('');
    const act = (n.action_items || []).slice(0, 4).map(a => `<div class="body">☐ ${esc(short(a.text, 80))}</div>`).join('');
    return `<div class="card answer"><div class="eyebrow">Meeting notes</div>
      <div class="title">${esc(short(n.summary || 'No summary.', 140))}</div>
      ${dec ? `<div class="eyebrow">Decisions</div>${dec}` : ''}${act ? `<div class="eyebrow">Action items</div>${act}` : ''}
      <div class="row"><button class="primary" data-view="notes">Back</button></div></div>`;
  };

  actions['note-open'] = async ({ id }) => { state.openNote = await api('/notes/' + id); state.view = 'note'; };
  actions['notes-stop'] = async () => { await post('/notes/stop'); state.note = 'Notes saved.'; };
  actions['order-seen'] = async ({ id }) => { state.dismissed.add('order-' + id); };
})();
