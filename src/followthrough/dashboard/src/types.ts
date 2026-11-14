// Mirrors followthrough/models.py. Keep the two in sync.
export type Action = 'calendar' | 'email' | 'reply' | 'todo'
export type Status = 'pending' | 'needs_review' | 'done' | 'drafted' | 'failed' | 'dismissed'

export interface Segment { start: number | null; end: number | null; speaker: string; text: string }
export interface Proof { verified: boolean; first_segment: number | null; last_segment: number | null; start: number | null; end: number | null }
export interface Item {
  id: string; action: Action; title: string; owner: string; counterpart: string
  quote: string; proof: Proof; due_local: string; duration_min: number; changed_from: string
  to: string; subject: string; body: string; status: Status; issues: string[]
  result: string; link: string; demo: boolean
}
export interface Health {
  llm: string; model: string; transcribe_model: string; executor: string
  email_to: string; calendar_account: string; demo_loaded: boolean
}
export interface AppState { segments: Segment[]; items: Item[]; todos: string[]; demo: boolean; health: Health }
export type EditPatch = Partial<Pick<Item, 'title' | 'due_local' | 'duration_min' | 'subject' | 'body'>>
