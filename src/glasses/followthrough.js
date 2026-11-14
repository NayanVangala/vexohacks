// Follow-Through on the glasses (owner: Srijan). Loaded after app.js; plugs into window.Scene.
// Follow-Through's API is reachable same-origin at /ft/api/... (the Scene backend proxies it to :8002).
// Full brief: docs/FOLLOWTHROUGH-ON-GLASSES.md
(() => {
  const { state, views, pollers, actions, heroes, navTiles, api, esc } = window.Scene;
  state.ft = { items: [], online: false };

  // 1. Data: pull Follow-Through's items every poll.
  pollers.push(async () => {
    try {
      const s = await api('/ft/api/state');
      state.ft = { items: s.items || [], online: true };
    } catch { state.ft.online = false; }
  });

  // TODO(Srijan): 2. heroes.push(() => html | null)  - Home's big card for the next pending item
  // TODO(Srijan): 3. views.followups = () => html     - a list screen, opened by a navTiles button
  // TODO(Srijan): 4. actions['ft-approve'] / actions['ft-dismiss'] = async ({ id }) => api(...)
})();
