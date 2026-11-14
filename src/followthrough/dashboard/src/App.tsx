import { useCallback, useEffect, useState } from 'react'
import { MotionConfig } from 'framer-motion'
import { api } from './api'
import type { AppState, EditPatch } from './types'
import Transcript from './Transcript'
import Ledger from './Ledger'
import Voice from './Voice'

export default function App() {
  const [state, setState] = useState<AppState | null>(null)
  const [busy, setBusy] = useState('')            // what is running: '', 'run', 'add', or an item id
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [voiceStart, setVoiceStart] = useState(0)   // bumped after a successful find, starts voice approval when it is on
  const [view, setView] = useState<'commitments' | 'conversation'>('commitments')   // phone only

  const refresh = useCallback(async () => {
    try { setState(await api.state()); setError((e) => (e.startsWith('Cannot reach') ? '' : e)) }
    catch { setError('Cannot reach the Follow-Through service. Check that it is running and that this device is on the same network.') }
  }, [])
  useEffect(() => { refresh(); const t = setInterval(refresh, 2500); return () => clearInterval(t) }, [refresh])

  /** Run an action, show its error if it fails, then refresh. */
  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label); setError('')
    try { await fn() } catch (e) { setError((e as Error).message) } finally { setBusy('') }
    await refresh()
  }
  const find = () => act('run', async () => {
    const r = await api.run()
    const first = r.items.find((i) => i.status === 'pending') ?? r.items[0]
    if (first) setSelected(first.id)
    setVoiceStart((n) => n + 1)
  })
  const addThenFind = (fn: () => Promise<unknown>) => act('add', async () => { await fn(); const r = await api.run(); if (r.items[0]) setSelected(r.items[0].id); setVoiceStart((n) => n + 1) })

  const items = state?.items ?? []
  const segments = state?.segments ?? []
  const h = state?.health
  const realLlm = h?.llm === 'openai'
  const realExec = h?.executor === 'configured'
  const working = busy !== ''

  const input = {
    disabled: working,
    hasTranscript: segments.length > 0,
    onAudio: (b: Blob, n: string) => addThenFind(() => api.transcribe(b, n)),
    onText: (t: string) => addThenFind(() => api.addText(t)),
    onDemo: () => act('add', async () => { await api.loadDemo() }),
    onClear: () => act('add', async () => { await api.reset(); setSelected(null) }),
    onError: setError,
  }

  const Dot = ({ on }: { on: boolean }) => <span className={`mr-2 inline-block h-2.5 w-2.5 rounded-full ${on ? 'bg-blue' : 'bg-muted/50'}`} />

  return (
    <MotionConfig reducedMotion="user">
      <div className="flex h-dvh flex-col">
        <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b-2 border-black px-4 py-3 sm:px-6">
          <h1 className="order-1 flex items-center gap-3 text-2xl font-extrabold tracking-tight">
            <span aria-hidden className="inline-block h-6 w-6 rounded-full bg-blue" />Follow-Through
          </h1>
          <button onClick={find} disabled={working || segments.length === 0}
            className="order-2 ml-auto rounded-full bg-blue px-5 py-2.5 text-lg font-bold text-white hover:bg-blue-deep lg:order-3 lg:ml-0">
            {busy === 'run' || busy === 'add' ? 'Reading the conversation' : 'Find commitments'}
          </button>
          <div className="order-3 flex w-full flex-wrap items-center gap-x-5 gap-y-1 text-sm font-semibold sm:text-base lg:order-2 lg:ml-auto lg:w-auto">
            {state?.demo && <span className="rounded-full border-2 border-black px-3 py-0.5 whitespace-nowrap">Demo data</span>}
            <span><Dot on={realLlm} />{realLlm ? 'OpenAI connected' : 'No OpenAI key, demo only'}</span>
            <span><Dot on={realExec} />{realExec ? 'Sends real email and calendar' : 'Drafts only, nothing is sent'}</span>
          </div>
        </header>

        {error && (
          <div role="alert" className="flex items-start justify-between gap-4 border-b border-line border-l-[6px] border-l-danger bg-wash px-6 py-3 text-lg">
            <p>{error}</p>
            <button className="shrink-0 font-bold underline underline-offset-4" onClick={() => setError('')}>Dismiss</button>
          </div>
        )}

        <Voice items={items} voice={h?.voice ?? 'browser voice'} startSignal={voiceStart} onSelect={(id) => { setSelected(id); setView('commitments') }} onAfter={refresh} />

        <main className="grid min-h-0 flex-1 lg:grid-cols-[1.1fr_1fr] lg:divide-x lg:divide-line">
          <div className={`${view === 'conversation' ? 'flex' : 'hidden'} min-h-0 flex-col lg:flex [&>section]:min-h-0 [&>section]:flex-1`}>
            <Transcript segments={segments} items={items} selectedId={selected} onSelect={(id) => { setSelected(id); setView('commitments') }} input={input} />
          </div>
          <div className={`${view === 'commitments' ? 'flex' : 'hidden'} min-h-0 flex-col lg:flex [&>section]:min-h-0 [&>section]:flex-1`}>
            <Ledger items={items} segments={segments} todos={state?.todos ?? []} health={h} selectedId={selected} busyId={busy}
              onSelect={setSelected}
              onApprove={(id) => act(id, () => api.approve(id))}
              onDismiss={(id) => act(id, () => api.dismiss(id))}
              onRetry={(id) => act(id, () => api.retry(id))}
              onEdit={(id: string, p: EditPatch) => act(id, () => api.edit(id, p))} />
          </div>
        </main>

        <nav className="grid grid-cols-2 border-t-2 border-black pb-[env(safe-area-inset-bottom)] lg:hidden">
          {(['commitments', 'conversation'] as const).map((v) => (
            <button key={v} onClick={() => setView(v)} aria-current={view === v}
              className={`py-3 text-lg font-bold ${view === v ? 'bg-blue text-white' : 'bg-white text-black'}`}>
              {v === 'commitments' ? `Commitments${items.length ? ` (${items.length})` : ''}` : 'Conversation'}
            </button>
          ))}
        </nav>
      </div>
    </MotionConfig>
  )
}
