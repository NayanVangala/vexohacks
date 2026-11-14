import { useState } from 'react'
import { motion } from 'framer-motion'
import type { EditPatch, Health, Item, Segment } from './types'
import { actionName, approveLabel, dueLabel, mmss, statusLabel } from './format'

interface Props {
  items: Item[]
  segments: Segment[]
  todos: string[]
  health?: Health
  selectedId: string | null
  busyId: string
  onSelect: (id: string) => void
  onApprove: (id: string) => void
  onDismiss: (id: string) => void
  onRetry: (id: string) => void
  onEdit: (id: string, patch: EditPatch) => Promise<void>
}

const field = 'w-full rounded-xl border-2 border-line p-3 text-lg focus:border-blue focus:outline-none'

function Entry({ it, rank, segments, health, selected, busy, ...fn }:
  { it: Item; rank: number; segments: Segment[]; health?: Health; selected: boolean; busy: boolean } &
  Pick<Props, 'onSelect' | 'onApprove' | 'onDismiss' | 'onRetry' | 'onEdit'>) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<EditPatch>({})
  const speaker = it.proof.first_segment != null ? segments[it.proof.first_segment]?.speaker : ''
  const open = it.status === 'pending' || it.status === 'needs_review'
  const color = it.status === 'failed' ? 'text-danger' : it.status === 'needs_review' ? 'text-black' : 'text-muted'

  const startEdit = () => {
    setDraft({ title: it.title, due_local: it.due_local, duration_min: it.duration_min, subject: it.subject, body: it.body })
    setEditing(true)
  }
  const save = async () => { await fn.onEdit(it.id, draft); setEditing(false) }

  return (
    <motion.article
      initial={{ opacity: 0, x: 14 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.35, delay: rank * 0.18 + 0.25 }}
      className={`border-t border-line py-5 pl-5 pr-6 ${selected ? 'border-l-[6px] border-l-blue' : 'border-l-[6px] border-l-transparent'} ${it.status === 'dismissed' ? 'opacity-50' : ''}`}>
      <button className="w-full text-left" onClick={() => fn.onSelect(it.id)} aria-expanded={selected}>
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <span className="text-lg font-bold text-blue">
            {actionName[it.action]}{it.counterpart ? ` for ${it.counterpart}` : ''}
            {it.demo && <span className="ml-3 whitespace-nowrap rounded-full border border-black px-2 py-0.5 align-middle text-sm font-semibold text-black">Demo data</span>}
          </span>
          <span className={`shrink-0 text-base font-bold ${color}`}>{statusLabel[it.status]}</span>
        </div>
        <h3 className="mt-1 text-2xl font-extrabold leading-tight">{it.title}</h3>
        {it.summary && <p className="mt-1 text-lg text-muted">{it.summary}</p>}
        {(it.action === 'calendar' || it.due_local) && (
          <p className="mt-1 text-lg text-muted">
            {dueLabel(it.due_local)}{it.changed_from ? `, changed from ${it.changed_from}` : ''}
          </p>
        )}
      </button>

      <blockquote className={`mt-3 border-l-4 pl-4 text-xl leading-snug ${it.proof.verified ? 'border-blue' : 'border-danger'}`}>
        &ldquo;{it.quote}&rdquo;
      </blockquote>
      <p className={`mt-1 text-base ${it.proof.verified ? 'text-muted' : 'font-semibold text-danger'}`}>
        {it.proof.verified
          ? `${speaker || 'Someone'} said this${it.proof.start != null ? ` at ${mmss(it.proof.start)}` : ''}`
          : 'This quote is not in the transcript, so it cannot be approved as written.'}
      </p>

      {selected && (
        <div className="mt-4">
          {editing ? (
            <div className="space-y-3">
              <label className="block text-base font-semibold">Title<input className={field} value={draft.title ?? ''} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></label>
              {it.action === 'calendar' && (
                <div className="flex gap-3">
                  <label className="flex-1 text-base font-semibold">When<input type="datetime-local" className={field} value={draft.due_local ?? ''} onChange={(e) => setDraft({ ...draft, due_local: e.target.value })} /></label>
                  <label className="w-32 text-base font-semibold">Minutes<input type="number" min={5} className={field} value={draft.duration_min ?? 30} onChange={(e) => setDraft({ ...draft, duration_min: +e.target.value })} /></label>
                </div>
              )}
              {it.action === 'email' && <label className="block text-base font-semibold">Subject<input className={field} value={draft.subject ?? ''} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} /></label>}
              {(it.action === 'email' || it.action === 'reply') && <label className="block text-base font-semibold">Message<textarea rows={5} className={field} value={draft.body ?? ''} onChange={(e) => setDraft({ ...draft, body: e.target.value })} /></label>}
              <div className="flex gap-2">
                <button className="rounded-full bg-blue px-5 py-2 text-lg font-bold text-white hover:bg-blue-deep" onClick={save}>Save changes</button>
                <button className="px-3 py-2 text-base font-semibold text-muted underline underline-offset-4" onClick={() => setEditing(false)}>Cancel</button>
              </div>
            </div>
          ) : (
            <>
              {(it.action === 'email' || it.action === 'reply') && (
                <div className="rounded-xl bg-wash p-4 text-lg">
                  {it.action === 'email' && (
                    <p className="mb-2 text-base text-muted">To {it.to}<br /><span className="font-bold text-black">{it.subject || it.title}</span></p>
                  )}
                  <p className="whitespace-pre-wrap">{it.body || 'No text yet.'}</p>
                </div>
              )}
              {it.action === 'calendar' && it.due_local && (
                <p className="text-lg text-muted">{it.duration_min} minutes on {health?.calendar_account}&rsquo;s calendar</p>
              )}
              {it.status === 'needs_review' && (
                <p className="mt-3 text-lg font-semibold">Why it needs review: {it.issues.join(', ')}. Edit it, or dismiss it.</p>
              )}
              {!open && it.status !== 'dismissed' && (
                <div className="mt-3">
                  <p className={`text-lg font-semibold ${it.status === 'failed' ? 'text-danger' : ''}`}>{it.result}</p>
                  {it.link && <a className="mt-2 inline-block rounded-full border-2 border-black px-4 py-2 text-base font-bold hover:bg-black hover:text-white" href={it.link} target="_blank" rel="noreferrer">Open draft in {it.action === 'email' ? 'Gmail' : 'Google Calendar'}</a>}
                  {it.status === 'failed' && <button className="mt-2 rounded-full border-2 border-black px-4 py-2 text-base font-bold hover:bg-black hover:text-white" onClick={() => fn.onRetry(it.id)}>Try again</button>}
                </div>
              )}
              {open && (
                <div className="mt-4 flex flex-wrap items-center gap-3">
                  {it.status === 'pending' && (
                    <button disabled={busy} onClick={() => fn.onApprove(it.id)}
                      className="rounded-full bg-blue px-6 py-3 text-lg font-bold text-white hover:bg-blue-deep">
                      {busy ? 'Working' : approveLabel(it, health)}
                    </button>
                  )}
                  <button className="rounded-full border-2 border-black px-5 py-2.5 text-lg font-bold hover:bg-black hover:text-white" onClick={startEdit}>Edit</button>
                  <button disabled={busy} className="px-2 py-2 text-lg font-semibold text-muted underline underline-offset-4 hover:text-black" onClick={() => fn.onDismiss(it.id)}>Dismiss</button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </motion.article>
  )
}

export default function Ledger(p: Props) {
  const ready = p.items.filter((i) => i.status === 'pending').length
  const review = p.items.filter((i) => i.status === 'needs_review').length
  return (
    <section className="flex min-h-0 flex-col" aria-label="Commitments">
      <div className="border-b border-line px-6 pb-4 pt-5">
        <h2 className="text-3xl font-extrabold tracking-tight">Commitments</h2>
        <p className="mt-1 text-lg text-muted">
          {p.items.length === 0 ? 'Nothing runs until you approve it.' : `${ready} ready to approve${review ? `, ${review} needs review` : ''}. Nothing runs until you approve it.`}
        </p>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-16">
        {p.items.length === 0 ? (
          <div className="px-6 py-16">
            <p className="max-w-md text-2xl font-bold leading-snug">No commitments found yet.</p>
            <p className="mt-2 max-w-md text-lg text-muted">Add a conversation, then choose Find commitments. Each one will show the exact words that created it.</p>
          </div>
        ) : p.items.map((it, rank) => (
          <Entry key={it.id} it={it} rank={rank} segments={p.segments} health={p.health}
            selected={it.id === p.selectedId} busy={p.busyId === it.id}
            onSelect={p.onSelect} onApprove={p.onApprove} onDismiss={p.onDismiss} onRetry={p.onRetry} onEdit={p.onEdit} />
        ))}
        {p.todos.length > 0 && (
          <div className="border-t border-line px-6 py-5">
            <h3 className="text-2xl font-extrabold">To-do list</h3>
            <ul className="mt-2 list-disc space-y-1 pl-6 text-xl">{p.todos.map((t, i) => <li key={i}>{t}</li>)}</ul>
          </div>
        )}
      </div>
    </section>
  )
}
