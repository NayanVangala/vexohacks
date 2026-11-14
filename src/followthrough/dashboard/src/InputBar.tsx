import { useRef, useState } from 'react'

interface Props {
  hasTranscript: boolean
  disabled: boolean
  onAudio: (blob: Blob, name: string) => void
  onText: (text: string) => void
  onDemo: () => void
  onClear: () => void
  onError: (msg: string) => void
}

const outline = 'rounded-full border-2 border-black px-4 py-2 text-base font-bold hover:bg-black hover:text-white transition-colors'

/** Four ways to add a conversation: record, upload audio, paste text, or load the staged demo. */
export default function InputBar({ hasTranscript, disabled, onAudio, onText, onDemo, onClear, onError }: Props) {
  const [recording, setRecording] = useState(false)
  const [pasting, setPasting] = useState(false)
  const [text, setText] = useState('')
  const rec = useRef<MediaRecorder | null>(null)
  const file = useRef<HTMLInputElement>(null)

  async function toggleRecord() {
    if (recording) { rec.current?.stop(); return }
    if (!navigator.mediaDevices?.getUserMedia) {
      onError('Recording needs a secure page (https or localhost). Upload an audio file or paste text instead.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const chunks: BlobPart[] = []
      const r = new MediaRecorder(stream)
      r.ondataavailable = (e) => e.data.size && chunks.push(e.data)
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        setRecording(false)
        onAudio(new Blob(chunks, { type: r.mimeType }), 'recording.webm')
      }
      rec.current = r
      r.start()
      setRecording(true)
    } catch {
      onError('Microphone access was blocked. Allow the microphone for this page, or upload an audio file.')
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <button className={recording ? 'rounded-full bg-danger px-4 py-2 text-base font-bold text-white' : outline}
          onClick={toggleRecord} disabled={disabled && !recording}>
          {recording ? 'Stop and transcribe' : 'Record'}
        </button>
        <button className={outline} onClick={() => file.current?.click()} disabled={disabled}>Upload audio</button>
        <input ref={file} type="file" accept="audio/*,video/*" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) onAudio(f, f.name); e.target.value = '' }} />
        <button className={outline} onClick={() => setPasting((p) => !p)} disabled={disabled}>Paste text</button>
        <button className={outline} onClick={onDemo} disabled={disabled}>Load demo</button>
        {hasTranscript && (
          <button className="px-2 py-2 text-base font-semibold text-muted underline underline-offset-4 hover:text-black"
            onClick={onClear} disabled={disabled}>Clear</button>
        )}
      </div>
      {pasting && (
        <div className="mt-3">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={5}
            placeholder={'One line per turn, like:\nSam: Can you send the report by five?\nMe: Yes, I will send it by five.'}
            className="w-full rounded-xl border-2 border-line p-3 text-lg focus:border-blue focus:outline-none" />
          <button className="mt-2 rounded-full bg-blue px-5 py-2 text-base font-bold text-white hover:bg-blue-deep disabled:opacity-40"
            disabled={!text.trim() || disabled}
            onClick={() => { onText(text); setText(''); setPasting(false) }}>Add to conversation</button>
        </div>
      )}
    </div>
  )
}
