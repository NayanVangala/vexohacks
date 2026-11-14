import type { Action, Health, Item } from './types'

export const mmss = (t: number | null) =>
  t == null ? '' : `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`

/** "2026-11-14T16:00" to "Today 4:00 PM", "Tomorrow 10:00 AM" or "Sat, Nov 21 at 4:00 PM". */
export function dueLabel(s: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(s)
  if (!m) return 'No time set'
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  const day0 = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day0(d) - day0(new Date())) / 86400000)
  if (diff === 0) return `Today ${time}`
  if (diff === 1) return `Tomorrow ${time}`
  return `${d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} at ${time}`
}

export const actionName: Record<Action, string> = {
  calendar: 'Calendar event', email: 'Email', reply: 'Reply', todo: 'To-do',
}

/** The button says exactly what will happen. Without an executor, approving only makes a draft. */
export function approveLabel(it: Item, h: Health | undefined): string {
  const real = h?.executor === 'configured'
  if (it.action === 'todo') return 'Add to to-do list'
  if (it.action === 'reply') return 'Save reply draft'
  if (it.action === 'email') return real ? `Send email to ${it.to}` : 'Create email draft'
  return real ? 'Add to calendar' : 'Create calendar draft'
}

export const statusLabel: Record<Item['status'], string> = {
  pending: 'Ready to approve', needs_review: 'Needs review', done: 'Done',
  drafted: 'Draft only', failed: 'Failed', dismissed: 'Dismissed',
}
