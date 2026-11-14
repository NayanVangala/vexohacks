import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Item } from './types'

// Voice approval. For each pending item: speak the question, listen for yes or no, send what was heard to the
// server. The server decides what it means. Only a clear yes approves, and only that one item.
// The Yes, No and Skip buttons always work, so the demo never depends on the microphone.

type Phase = 'idle' | 'speaking' | 'listening' | 'working'

interface Props {
  items: Item[]
  voice: string                       // "elevenlabs" or "browser voice", from /api/health
  startSignal: number                 // App bumps this to start automatically after "Find commitments"
  onSelect: (id: string) => void
  onAfter: () => Promise<void>        // refresh the dashboard
}

const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition

export default function Voice({ items, voice, startSignal, onSelect, onAfter }: Props) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [text, setText] = useState('')        // the question being asked
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')        // what was heard, or what happened
  const [auto, setAuto] = useState(() => localStorage.getItem('ft-auto-voice') === '1')
  const stopped = useRef(false)
  const press = useRef<((v: string) => void) | null>(null)
  const audio = useRef<HTMLAudioElement | null>(null)
  const rec = useRef<any>(null)
  const micErr = useRef('')                // last speech recognition error, shown so a blocked mic is not silent
  const viaServer = useRef(!SR)           // no browser speech recognition (or it cannot reach its service): record and transcribe on the server
  const abortRec = useRef(false)
  const early = useRef('')                 // a button tapped while the question was still being spoken
  const pending = items.filter((i) => i.status === 'pending')

  /** Speak with ElevenLabs through the server. If that is not available, use the browser voice. */
  const say = async (id: string, sentence: string, n: number, total: number) => {
    try {
      const r = await fetch(`/api/items/${id}/speech?n=${n}&total=${total}`)
      if (!r.ok) throw new Error('no elevenlabs audio')
      const a = new Audio(URL.createObjectURL(await r.blob()))
      audio.current = a
      await new Promise<void>((done, fail) => { a.onended = () => done(); a.onerror = () => fail(new Error('audio')); a.play().catch(fail) })
    } catch {
      await new Promise<void>((done) => {
        if (!('speechSynthesis' in window)) return done()
        const u = new SpeechSynthesisUtterance(sentence)
        u.onend = () => done(); u.onerror = () => done()
        window.speechSynthesis.speak(u)
        setTimeout(done, 20000)
      })
    }
  }

  /** Record from the mic until the person stops talking, then have the server turn it into words. */
  const hear = async (): Promise<string> => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const ctx = new AudioContext()
    const an = ctx.createAnalyser(); ctx.createMediaStreamSource(stream).connect(an)
    const mr = new MediaRecorder(stream); const chunks: Blob[] = []
    mr.ondataavailable = (e) => chunks.push(e.data)
    const done = new Promise<void>((d) => { mr.onstop = () => d() })
    const buf = new Uint8Array(an.fftSize)
    const t0 = Date.now(); let last = 0; let spoke = false
    mr.start()
    while (!stopped.current && !abortRec.current) {
      await new Promise((d) => setTimeout(d, 100))
      an.getByteTimeDomainData(buf)
      const rms = Math.sqrt(buf.reduce((a, v) => a + ((v - 128) / 128) ** 2, 0) / buf.length)
      if (rms > 0.03) { spoke = true; last = Date.now() }
      if ((spoke && Date.now() - last > 900) || Date.now() - t0 > 10000) break
    }
    mr.stop(); await done
    stream.getTracks().forEach((t) => t.stop()); void ctx.close()
    if (!spoke || stopped.current || abortRec.current) return ''
    return (await api.hear(new Blob(chunks, { type: mr.mimeType }))).heard
  }

  /** Wait for a spoken answer or a button press. Returns the words heard, "" for silence. */
  const listen = () => new Promise<string>((resolve) => {
    press.current = resolve
    micErr.current = ''; abortRec.current = false
    const record = () => { hear().then(resolve, (e) => { micErr.current = e?.name || 'mic'; resolve('') }) }
    if (viaServer.current) return record()
    const r = new SR()
    rec.current = r
    r.lang = 'en-US'; r.interimResults = false; r.maxAlternatives = 1
    let finished = false
    const finish = (v: string) => { if (!finished) { finished = true; clearTimeout(timer); resolve(v) } }
    const timer = setTimeout(() => { try { r.abort() } catch { /* ok */ } finish('') }, 12000)
    r.onresult = (e: any) => finish(e.results[0][0].transcript)
    // Chrome ends a session after a short silence. Restart it until the 12 second window is up, unless the mic is blocked.
    r.onend = () => { if (finished) return; if (micErr.current) return finish(''); try { r.start() } catch { finish('') } }
    r.onerror = (e: any) => {
      if (e.error === 'network') { viaServer.current = true; finished = true; clearTimeout(timer); record(); return }   // this browser cannot reach its speech service
      micErr.current = e.error === 'no-speech' || e.error === 'aborted' ? '' : String(e.error)
    }
    try { r.start() } catch { /* already started */ }
  })

  const halt = () => {
    stopped.current = true
    window.speechSynthesis?.cancel()
    audio.current?.pause()
    try { rec.current?.abort() } catch { /* nothing to stop */ }
    press.current?.('skip')
  }

  const start = async () => {
    stopped.current = false
    setPhase('working')
    // Ask the server for the newest list, because the screen may not have refreshed yet.
    const fresh = (await api.state()).items.filter((i) => i.status === 'pending')
    for (const [n, it] of fresh.entries()) {
      const id = it.id
      if (stopped.current) break
      onSelect(id)
      setTitle(it.title); setNote('')
      try {
        setPhase('working')
        const sentence = (await api.question(id, n, fresh.length)).text
        setText(sentence)
        setPhase('speaking'); early.current = ''
        press.current = (v) => {              // Yes, No or Skip tapped mid-sentence: cut the voice and use it
          early.current = v
          window.speechSynthesis?.cancel(); audio.current?.pause(); audio.current?.dispatchEvent(new Event('ended'))
        }
        await say(id, sentence, n, fresh.length)
        for (let tries = 0; tries < 2 && !stopped.current; tries++) {
          setPhase('listening')
          const heard = early.current || await listen()
          early.current = ''; abortRec.current = true
          try { rec.current?.abort() } catch { /* ok */ }
          if (heard === 'skip') break
          setPhase('working')
          const r = await api.voiceAnswer(id, heard)
          if (r.answer === 'unclear') { setNote(micErr.current ? `Mic problem: ${micErr.current}. Allow the microphone for this site, or use the buttons.` : heard ? `Heard "${heard}", didn't catch a yes or no. Try again?` : "Didn't catch that. Try again, or tap a button."); continue }
          setNote(r.answer === 'yes' ? `Approved. ${r.item.result}` : 'Dismissed.')
          await onAfter()
          await new Promise((d) => setTimeout(d, 1400))
          break
        }
      } catch (e) { setNote((e as Error).message); break }
    }
    setPhase('idle'); press.current = null
    await onAfter()
  }

  // The dashboard bumps startSignal after "Find commitments" when "Ask out loud automatically" is on.
  useEffect(() => { if (startSignal > 0 && auto && phase === 'idle' && pending.length) void start() }, [startSignal]) // eslint-disable-line

  const active = phase !== 'idle'
  if (!active && pending.length === 0) return null

  return (
    <section aria-label="Voice approval" className="border-b border-line bg-wash px-4 py-3 sm:px-6">
      {!active ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          {note && <p className="w-full text-lg font-bold text-blue">{note}</p>}
          <button onClick={() => void start()}
            className="rounded-full bg-black px-5 py-2.5 text-lg font-bold text-white hover:bg-blue-deep">
            Ask me out loud ({pending.length})
          </button>
          <label className="flex items-center gap-2 text-base font-semibold">
            <input type="checkbox" className="h-5 w-5 accent-[#0064e0]" checked={auto}
              onChange={(e) => { setAuto(e.target.checked); localStorage.setItem('ft-auto-voice', e.target.checked ? '1' : '0') }} />
            Ask automatically after finding
          </label>
          <span className="text-base text-muted">Voice: {voice}</span>
        </div>
      ) : (
        <div role="status" aria-live="polite" className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <span aria-hidden className={`flex h-7 items-end gap-1 ${phase === 'listening' || phase === 'speaking' ? 'wave' : ''}`}>
              {[0, 1, 2, 3, 4].map((n) => <i key={n} style={{ animationDelay: `${n * 120}ms` }} className="block w-1.5 rounded-full bg-blue" />)}
            </span>
            <p className="text-lg font-extrabold">
              {phase === 'speaking' ? 'Speaking' : phase === 'listening' ? 'Listening' : 'One moment'}
              {title ? <span className="font-semibold text-muted"> about {title}</span> : null}
            </p>
          </div>
          {text && <p className="text-xl leading-snug">{text}</p>}
          {note && <p className="text-lg font-bold text-blue">{note}</p>}
          <div className="flex flex-wrap gap-3">
            <button disabled={phase === 'working'} onClick={() => press.current?.('yes')} className="rounded-full bg-blue px-6 py-2.5 text-lg font-bold text-white hover:bg-blue-deep">Yes</button>
            <button disabled={phase === 'working'} onClick={() => press.current?.('no')} className="rounded-full border-2 border-black px-6 py-2.5 text-lg font-bold">No</button>
            <button disabled={phase === 'working'} onClick={() => press.current?.('skip')} className="rounded-full border-2 border-line px-6 py-2.5 text-lg font-bold">Skip</button>
            <button onClick={halt} className="px-3 py-2.5 text-lg font-bold underline underline-offset-4">Stop</button>
          </div>
        </div>
      )}
    </section>
  )
}
