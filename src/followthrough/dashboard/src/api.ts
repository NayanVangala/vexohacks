import type { AppState, EditPatch, Item } from './types'

// All calls go to the same address that served this page, so there is no CORS and no setup on the phone.
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch('/api' + path, init)
  if (!r.ok) {
    let msg = r.statusText
    try { msg = (await r.json()).detail || msg } catch { /* keep statusText */ }
    throw new Error(msg)
  }
  return r.json()
}
const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })

export const api = {
  state: () => call<AppState>('/state'),
  run: () => post<{ items: Item[] }>('/run'),
  loadDemo: () => post('/demo/load'),
  reset: () => post('/reset'),
  addText: (text: string) => post('/transcript', { text }),
  transcribe: (file: Blob, name: string) => {
    const f = new FormData(); f.append('audio', file, name)
    return call('/transcribe', { method: 'POST', body: f })
  },
  hear: (clip: Blob) => {
    const f = new FormData(); f.append('audio', clip, 'answer.webm')
    return call<{ heard: string }>('/voice/hear', { method: 'POST', body: f })
  },
  question: (id: string, n = 0, total = 1) => call<{ text: string }>(`/items/${id}/question?n=${n}&total=${total}`),
  voiceAnswer: (id: string, heard: string) => post<{ answer: 'yes' | 'no' | 'unclear'; item: Item }>(`/items/${id}/voice`, { heard }),
  approve: (id: string) => post<Item>(`/items/${id}/approve`),
  retry: (id: string) => post<Item>(`/items/${id}/retry`),
  dismiss: (id: string) => post<Item>(`/items/${id}/dismiss`),
  edit: (id: string, patch: EditPatch) => call<Item>(`/items/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }),
}
