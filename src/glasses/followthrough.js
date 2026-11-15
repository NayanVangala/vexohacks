// Follow-Through on the glasses. Loaded after app.js; plugs into window.Scene's extension points.
// Follow-Through finds promises in what you heard ("I'll send you the deck by four"), proves each
// one with an exact quote, drafts the follow-up (calendar, email, reply, to-do), and acts only when
// you approve. Here, approving is a pinch. Its API is same-origin at /ft/api/... (the Scene backend
// proxies to Follow-Through on :8002). Brief: docs/FOLLOWTHROUGH-ON-GLASSES.md
// Promises render as iMessage-style bubbles (helpers shared from scene-views.js, see B below):
// short cream text, a "Srijan said" label only when the backend names a counterpart, and a
// "more on phone" hint when text is cut. Full detail lives on the phone.
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

  // Bubble rendering is shared with scene-views.js (loaded just before this
  // file); the fallback below keeps Promises working if that file ever loads
  // out of order. Speaker rule, same as there: only from a backend field.
  // TODO(face-enrollment): see speakerOf in scene-views.js — enrolled faces
  // will fill i.counterpart from the backend; no name is ever guessed here.
  const B = window.Scene.bubble || {
    speakerOf: i => (i && (i.counterpart || i.speaker || i.from_whom)) || null,
    whoLine: (x, color) => { const w = (x && (x.counterpart || x.speaker || x.from_whom)) || null;
      return w ? `<span style="display:block;font-size:.75em;font-weight:600;color:${color}">${esc(w)} said</span>` : ''; },
    clampText: (t, n) => { t = String(t || ''); return t.length > n ? [t.slice(0, n - 1) + '…', true] : [t, false]; },
    moreHint: color => `<span style="display:block;font-size:.72em;font-weight:600;color:${color};margin-top:4px">more on phone ›</span>`,
    bubbleStyle: 'display:block;width:100%;text-align:left;background:rgba(255,248,236,.08);' +
      'border-radius:18px;padding:12px 16px 12px 34px;margin:0 0 10px;',
    bubbleBody: t => `<span style="display:-webkit-box;font-size:1.08em;line-height:1.25;font-weight:600;color:#FFF8EC;` +
      `-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${esc(t)}</span>`,
    bubbleMeta: s => `<small style="display:block;font-weight:500;color:rgba(255,248,236,.56);margin-top:4px">${s}</small>`,
  };

  pollers.push(async () => {
    try {
      const s = await api('/ft/api/state');
      state.ft.items = s.items || [];
      state.ft.segments = s.segments || [];   // verbatim transcript, for the quote's context on the card
      state.ft.online = true;
    } catch { state.ft.online = false; }   // Follow-Through not running: Scene keeps working without it
  });

  // Verbatim transcript lines around the quote (two before, two after, never
  // the quote segment itself), formatted "Name: what was said". No paraphrase,
  // no invented lines: nothing shows when there is no proof or no segments.
  function contextLines(i) {
    const segs = state.ft.segments || [];
    const p = i.proof || {};
    if (!segs.length || p.first_segment == null || p.last_segment == null) return [];
    const lo = Math.max(0, p.first_segment - 2), hi = Math.min(segs.length - 1, p.last_segment + 2);
    const out = [];
    for (let n = lo; n <= hi; n++) {
      if (n >= p.first_segment && n <= p.last_segment) continue;
      const s = segs[n];
      if (!s || !s.text) continue;
      const [t] = B.clampText((s.speaker ? s.speaker + ': ' : '') + s.text, 90);
      out.push(t);
    }
    return out.slice(0, 2);
  }

  // "Tell me more about this": the opened / focused promise is what "this"
  // means. Post the full item (title, exact quote, speaker, status, action,
  // due, context lines) so the backend can answer about it without guessing.
  function ftFocus(i) {
    window.Scene.setFocus({ id: i.id, kind: 'promise', title: i.title, quote: i.quote || null,
      speaker: B.speakerOf(i), status: i.status, action: i.action, due_local: i.due_local || null,
      body: i.body || i.subject || '', context: contextLines(i) });
  }
  window.Scene.ftFocus = ftFocus;

  /** One commitment, with its proof and the two choices. */
  function card(i, back) {
    const meta = [KIND[i.action] || i.action, when(i.due_local)].filter(Boolean).join(', ');
    const canApprove = i.status === 'pending';
    const [title, titleCut] = B.clampText(i.title, 110);
    const [quote, quoteCut] = B.clampText(i.quote, 160);
    return `<div class="card ft"><div class="eyebrow">${esc(meta)}</div>
      ${B.whoLine(i, '#F0AEC4')}
      <div class="title">${esc(title)}</div>
      ${titleCut ? B.moreHint('#F0AEC4') : ''}
      <div class="quote" style="font-size:1.05em;color:#FFF8EC">${quote ? `“${esc(quote)}”` : 'No exact quote saved for this one.'}</div>
      ${quoteCut ? B.moreHint('#F0AEC4') : ''}
      ${contextLines(i).map(l => `<div class="body" style="font-size:.95em;color:rgba(255,248,236,.72)">${esc(l)}</div>`).join('')}
      ${i.status === 'needs_review' ? `<div class="body" style="font-size:1.05em;color:#FFF8EC">Couldn't verify: ${esc((i.issues || []).join('; ') || 'quote not found')}</div>` : ''}
      <div class="row">
        ${canApprove ? `<button class="primary" data-act="ft-approve" data-id="${esc(i.id)}">Approve</button>` : ''}
        ${back ? `<button data-act="ft-dismiss" data-id="${esc(i.id)}">Dismiss</button><button data-view="followups">Back</button>`
               : `<button data-act="ft-skip" data-id="${esc(i.id)}">Later</button>`}
      </div></div>`;
  }

  /** What really happened after a pinch: Follow-Through's own words ("Draft only, nothing was sent"). */
  function result(i) {
    const [title, titleCut] = B.clampText(i.title, 110);
    const [text, textCut] = B.clampText(i.result || '', 160);
    return `<div class="card ft"><div class="eyebrow">${i.status === 'done' ? 'Done' : i.status === 'drafted' ? 'Drafted' : esc(i.status)}</div>
      <div class="title">${esc(title)}</div>
      ${titleCut ? B.moreHint('#F0AEC4') : ''}
      <div class="body" style="font-size:1.05em;color:#FFF8EC">${esc(text)}</div>
      ${textCut ? B.moreHint('#F0AEC4') : ''}
      <div class="row"><button class="primary" data-act="ft-clear">OK</button></div></div>`;
  }

  // Home's big card: the result you just approved, else the next commitment waiting on you.
  heroes.push(() => {
    if (state.ft.last) return result(state.ft.last);
    const next = pending()[0];
    return next ? card(next, false) : null;
  });
  // Cards rendered inside Home's stage get the same treatment as Scene's own.

  // The Promises tile is always on the dock, so Follow-Through is findable even with nothing
  // pending: the count badge only shows when there is something to follow up.
  navTiles.push(() => {
    const n = state.ft.items.filter(i => i.status === 'pending' || i.status === 'needs_review').length;
    return window.Scene.dockItem('followups', 'Promises', n || undefined);
  });

  views.followups = () => {
    const open = state.ft.items.filter(i => i.status === 'pending' || i.status === 'needs_review').slice(0, 12);
    const items = open.map(i => {
      const [title, cut] = B.clampText(i.title, 110);
      // The bubble shows the exact quote too, not just the title: tap / pinch
      // for the full card with the context around it.
      const [quote, quoteCut] = B.clampText(i.quote || '', 90);
      return `<button class="item" data-act="ft-open" data-id="${esc(i.id)}" style="${B.bubbleStyle}">${B.whoLine(i, '#F0AEC4')}${B.bubbleBody(title)}
      ${quote ? `<span style="display:block;font-size:.95em;font-style:italic;color:rgba(255,248,236,.85);margin-top:4px">“${esc(quote)}”</span>` : `<span style="display:block;font-size:.9em;color:rgba(255,248,236,.6);margin-top:4px">No exact quote saved for this one.</span>`}
      ${B.bubbleMeta(esc(KIND[i.action] || i.action) + (i.due_local ? ', ' + esc(when(i.due_local)) : '') + (i.status === 'needs_review' ? ', couldn’t verify' : ''))}${cut || quoteCut ? B.moreHint('#F0AEC4') : ''}</button>`;
    }).join(''); 
    // "Approve all" covers verified, pending items only: anything that needs review stays manual.
    const ready = state.ft.items.filter(i => i.status === 'pending').length;
    return `<h2>Promises</h2>
      <div class="list">${items || `<div class="empty">${state.ft.online ? 'No promises yet. Say a promise out loud and it will appear here.' : 'Follow-Through isn’t running on the laptop.'}</div>`}</div>
      <div class="row nav">${ready > 1 ? `<button class="primary" data-act="ft-approve-all">Approve all ${ready}</button>` : ''}
        <button data-view="home">Back</button></div>`;
  };

  views.followup = () => {
    const i = find(state.ft.open);
    return i ? card(i, true) : views.followups();
  };

  actions['ft-open'] = async ({ id }) => { state.ft.open = id; state.view = 'followup'; const i = find(id); if (i) ftFocus(i); };
  actions['ft-skip'] = async ({ id }) => { state.ft.skipped.add(id); };
  actions['ft-clear'] = async () => { state.ft.last = null; };
  actions['ft-dismiss'] = async ({ id }) => {
    await api(`/ft/api/items/${encodeURIComponent(id)}/dismiss`, 'POST');
    window.Scene.clearFocus();   // dismissed: "this" no longer means this item
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
    window.Scene.clearFocus();   // approved: "this" no longer means these items
    state.view = 'home';
  };
  actions['ft-approve'] = async ({ id }) => {
    state.ft.last = await api(`/ft/api/items/${encodeURIComponent(id)}/approve`, 'POST');
    window.Scene.clearFocus();   // approved: "this" no longer means this item
    state.view = 'home';
  };
})();
