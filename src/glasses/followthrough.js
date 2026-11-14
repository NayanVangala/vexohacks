// Follow-Through on the glasses. Loaded after app.js; plugs into window.Scene's extension points.
// Follow-Through finds promises in what you heard ("I'll send you the deck by four"), proves each
// one with an exact quote, drafts the follow-up (calendar, email, reply, to-do), and acts only when
// you approve. Here, approving is a pinch. Its API is same-origin at /ft/api/... (the Scene backend
// proxies to Follow-Through on :8002). Brief: docs/FOLLOWTHROUGH-ON-GLASSES.md
(() => {
  const { state, views, pollers, actions, heroes, navTiles, api, esc } = window.Scene;
  state.ft = { items: [], online: false, last: null, skipped: new Set(), open: null };

  const KIND = { calendar: 'Calendar', email: 'Email', reply: 'Reply', todo: 'To-do' };
  const when = d => {
    if (!d) return '';
    const t = new Date(d);
    return isNaN(t) ? d : t.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  };
  const pending = () => state.ft.items.filter(i => i.status === 'pending' && !state.ft.skipped.has(i.id));
  const find = id => state.ft.items.find(i => i.id === id);

  pollers.push(async () => {
    try {
      const s = await api('/ft/api/state');
      state.ft.items = s.items || [];
      state.ft.online = true;
    } catch { state.ft.online = false; }   // Follow-Through not running: Scene keeps working without it
  });

  /** One commitment, with its proof and the two choices. */
  function card(i, back) {
    const meta = [`${KIND[i.action] || i.action}${i.counterpart ? ' with ' + i.counterpart : ''}`, when(i.due_local)].filter(Boolean).join(', ');
    const canApprove = i.status === 'pending';
    return `<div class="card ft"><div class="eyebrow">${esc(meta)}</div>
      <div class="title">${esc(i.title)}</div>
      <div class="quote">“${esc(i.quote)}”</div>
      ${i.status === 'needs_review' ? `<div class="body">Couldn't verify: ${esc((i.issues || []).join('; ') || 'quote not found')}</div>` : ''}
      <div class="row">
        ${canApprove ? `<button class="primary" data-act="ft-approve" data-id="${esc(i.id)}">Approve</button>` : ''}
        ${back ? `<button data-act="ft-dismiss" data-id="${esc(i.id)}">Dismiss</button><button data-view="followups">Back</button>`
               : `<button data-act="ft-skip" data-id="${esc(i.id)}">Later</button>`}
      </div></div>`;
  }

  /** What really happened after a pinch: Follow-Through's own words ("Draft only, nothing was sent"). */
  function result(i) {
    return `<div class="card ft"><div class="eyebrow">${i.status === 'done' ? 'Done' : i.status === 'drafted' ? 'Drafted' : esc(i.status)}</div>
      <div class="title">${esc(i.title)}</div>
      <div class="body">${esc(i.result || '')}</div>
      <div class="row"><button class="primary" data-act="ft-clear">OK</button></div></div>`;
  }

  // Home's big card: the result you just approved, else the next commitment waiting on you.
  heroes.push(() => {
    if (state.ft.last) return result(state.ft.last);
    const next = pending()[0];
    return next ? card(next, false) : null;
  });
  // Cards rendered inside Home's stage get the same treatment as Scene's own.

  // A Home tile only when there's something to follow up.
  navTiles.push(() => {
    const n = state.ft.items.filter(i => i.status === 'pending' || i.status === 'needs_review').length;
    return n ? window.Scene.dockItem('followups', 'Promises', n) : '';
  });

  views.followups = () => {
    const open = state.ft.items.filter(i => i.status === 'pending' || i.status === 'needs_review').slice(0, 4);
    const items = open.map(i => `<button class="item" data-act="ft-open" data-id="${esc(i.id)}">${esc(i.title)}
      <small>${esc(KIND[i.action] || i.action)}${i.due_local ? ', ' + esc(when(i.due_local)) : ''}${i.status === 'needs_review' ? ', couldn’t verify' : ''}</small></button>`).join('');
    // "Approve all" covers verified, pending items only: anything that needs review stays manual.
    const ready = state.ft.items.filter(i => i.status === 'pending').length;
    return `<h2>Promises</h2>
      <div class="list">${items || `<div class="empty">${state.ft.online ? 'Nothing to follow up.' : 'Follow-Through isn’t running on the laptop.'}</div>`}</div>
      <div class="row nav">${ready > 1 ? `<button class="primary" data-act="ft-approve-all">Approve all ${ready}</button>` : ''}
        <button data-view="home">Back</button></div>`;
  };

  views.followup = () => {
    const i = find(state.ft.open);
    return i ? card(i, true) : views.followups();
  };

  actions['ft-open'] = async ({ id }) => { state.ft.open = id; state.view = 'followup'; };
  actions['ft-skip'] = async ({ id }) => { state.ft.skipped.add(id); };
  actions['ft-clear'] = async () => { state.ft.last = null; };
  actions['ft-dismiss'] = async ({ id }) => {
    await api(`/ft/api/items/${encodeURIComponent(id)}/dismiss`, 'POST');
    state.view = 'followups';
  };
  actions['ft-approve-all'] = async () => {
    const ready = state.ft.items.filter(i => i.status === 'pending');
    const done = [];
    for (const i of ready) {   // one at a time: each approval is its own audited action
      try { done.push(await api(`/ft/api/items/${encodeURIComponent(i.id)}/approve`, 'POST')); } catch {}
    }
    state.ft.last = done.length ? { title: `${done.length} of ${ready.length} approved`, status: 'done',
      result: done.map(d => d.title).join(' · ') +
        (done.every(d => d.status === 'drafted' || /draft/i.test(d.result)) ? '. Drafts only, nothing was sent.' : '') } : null;
    state.view = 'home';
  };
  actions['ft-approve'] = async ({ id }) => {
    state.ft.last = await api(`/ft/api/items/${encodeURIComponent(id)}/approve`, 'POST');
    state.view = 'home';
  };
})();
