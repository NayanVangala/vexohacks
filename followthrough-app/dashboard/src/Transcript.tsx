import { useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import type { Item, Segment } from './types'
import { mmss } from './format'
import InputBar from './InputBar'

interface Props {
  segments: Segment[]
  items: Item[]
  selectedId: string | null
  onSelect: (id: string) => void
  input: React.ComponentProps<typeof InputBar>
}

/** The conversation. Chatter is quiet grey. Lines that hold a verified commitment get a blue bar,
 *  and the line behind the selected commitment turns solid blue, so you see the proof in place. */
export default function Transcript({ segments, items, selectedId, onSelect, input }: Props) {
  const lineRefs = useRef<Record<number, HTMLButtonElement | null>>({})

  // Which commitment (if any) owns each line. Dismissed items stop highlighting.
  const owner: Record<number, { id: string; rank: number }> = {}
  items.forEach((it, rank) => {
    if (!it.proof.verified || it.status === 'dismissed' || it.proof.first_segment == null) return
    for (let i = it.proof.first_segment; i <= (it.proof.last_segment ?? it.proof.first_segment); i++) owner[i] ??= { id: it.id, rank }
  })

  useEffect(() => {
    const it = items.find((x) => x.id === selectedId)
    const first = it?.proof.first_segment
    if (first == null) return
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    lineRefs.current[first]?.scrollIntoView({ block: 'center', behavior: calm ? 'auto' : 'smooth' })
  }, [selectedId, items])

  return (
    <section className="flex min-h-0 flex-col" aria-label="Conversation">
      <div className="border-b border-line px-6 pb-4 pt-5">
        <h2 className="mb-3 text-3xl font-extrabold tracking-tight">Conversation</h2>
        <InputBar {...input} hasTranscript={segments.length > 0} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-16">
        {segments.length === 0 ? (
          <div className="px-6 py-16">
            <p className="max-w-md text-2xl font-bold leading-snug">Nothing recorded yet.</p>
            <p className="mt-2 max-w-md text-lg text-muted">Record a conversation, upload audio, paste a transcript, or load the demo to see how commitments are found.</p>
          </div>
        ) : segments.map((s, i) => {
          const o = owner[i]
          const on = !!o && o.id === selectedId
          return (
            <button key={i} ref={(el) => { lineRefs.current[i] = el }} disabled={!o}
              onClick={() => o && onSelect(o.id)}
              className={`relative grid w-full grid-cols-[3.75rem_1fr] gap-x-4 px-6 py-3 text-left sm:grid-cols-[4rem_6.5rem_1fr] ${o ? '' : 'cursor-default'}`}>
              {o && (
                <>
                  <motion.span aria-hidden className={`absolute inset-0 origin-left transition-colors duration-200 ${on ? 'bg-blue' : 'bg-wash'}`}
                    initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ duration: 0.45, delay: o.rank * 0.18 }} />
                  <span aria-hidden className="absolute inset-y-0 left-0 w-1.5 bg-blue" />
                </>
              )}
              <span className={`tnum relative pt-0.5 text-base ${on ? 'text-white/85' : 'text-muted'}`}>{mmss(s.start)}</span>
              <span className={`relative hidden pt-0.5 text-base font-bold sm:block ${on ? 'text-white' : o ? 'text-black' : 'text-muted'}`}>{s.speaker}</span>
              <span className={`relative text-xl leading-snug ${on ? 'font-semibold text-white' : o ? 'text-black' : 'text-muted'}`}>
                <span className="font-bold sm:hidden">{s.speaker ? s.speaker + ': ' : ''}</span>{s.text}
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
