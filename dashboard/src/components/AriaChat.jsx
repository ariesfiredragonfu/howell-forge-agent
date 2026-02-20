/**
 * AriaChat — streaming ARIA chat panel with voice wake-word support.
 * Say "Forge" to wake ARIA, then speak your command.
 * Type messages in the input bar normally.
 */
import { useRef, useEffect, useState } from 'react'

export default function AriaChat({ hook }) {
  const { messages, streaming, connected, listening, sendMessage, toggleVoice } = hook
  const [draft, setDraft] = useState('')
  const bottomRef = useRef(null)

  // Auto-scroll to latest message
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const submit = (e) => {
    e.preventDefault()
    if (!draft.trim()) return
    sendMessage(draft)
    setDraft('')
  }

  return (
    <div className="panel flex flex-col h-full overflow-hidden">

      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2"
           style={{ borderBottom: '1px solid #1e1e3a' }}>
        <div className="flex items-center gap-2">
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: connected ? '#22c55e' : '#ef4444',
            display: 'inline-block',
            boxShadow: connected ? '0 0 8px #22c55e88' : 'none',
          }} />
          <span className="label" style={{ color: '#8888cc', fontSize: '0.75rem' }}>
            ARIA / MISSION CONTROL
          </span>
        </div>

        {/* Voice toggle */}
        <button
          onClick={toggleVoice}
          title={listening ? 'Voice active — say "Forge" to wake' : 'Click to enable voice'}
          style={{
            background: listening ? '#1a1a2e' : 'transparent',
            border: `1px solid ${listening ? '#3b82f6' : '#1e1e3a'}`,
            borderRadius: 4,
            padding: '3px 8px',
            cursor: 'pointer',
            fontSize: '0.65rem',
            color: listening ? '#3b82f6' : '#4a4a6a',
            letterSpacing: '0.1em',
            display: 'flex',
            alignItems: 'center',
            gap: 5,
          }}
        >
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: listening ? '#3b82f6' : '#4a4a6a',
            display: 'inline-block',
            animation: listening ? 'pulse 1.4s infinite' : 'none',
          }} />
          {listening ? 'LISTENING' : 'VOICE OFF'}
        </button>
      </div>

      {/* Message list */}
      <div className="flex-1 overflow-y-auto px-3 py-2" style={{ gap: 12, display: 'flex', flexDirection: 'column' }}>
        {messages.length === 0 && (
          <div style={{ color: '#2a2a4a', textAlign: 'center', marginTop: '2rem', fontSize: '0.75rem' }}>
            <div style={{ fontSize: '1.5rem', marginBottom: 8 }}>⬡</div>
            <div className="label">ARIA is standing by</div>
            <div className="label" style={{ marginTop: 4 }}>say "Forge" or type a message</div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} style={{
            alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
            maxWidth: '90%',
          }}>
            <div className="label" style={{
              color: msg.role === 'user' ? '#3b82f6' : msg.role === 'system' ? '#4a4a6a' : '#8888aa',
              marginBottom: 3,
              textAlign: msg.role === 'user' ? 'right' : 'left',
            }}>
              {msg.role === 'user' ? 'YOU' : msg.role === 'system' ? '— SYSTEM —' : 'ARIA'}
            </div>
            <div style={{
              background: msg.role === 'user'
                ? '#0f1729'
                : msg.role === 'system'
                  ? 'transparent'
                  : '#111120',
              border: `1px solid ${msg.role === 'user' ? '#1e2d4a' : msg.role === 'system' ? 'transparent' : '#1e1e3a'}`,
              borderRadius: 4,
              padding: msg.role === 'system' ? '2px 0' : '8px 12px',
              fontSize: '0.78rem',
              lineHeight: 1.6,
              color: msg.role === 'system' ? '#3a3a5a' : '#c8c8e8',
              whiteSpace: 'pre-wrap',
              fontFamily: 'monospace',
            }}>
              {msg.text}
              {msg.streaming && (
                <span style={{ animation: 'blink 1s infinite', color: '#3b82f6' }}>▋</span>
              )}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input bar */}
      <form onSubmit={submit} style={{ borderTop: '1px solid #1e1e3a', padding: '8px 12px', display: 'flex', gap: 8 }}>
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          placeholder={streaming ? 'ARIA is responding…' : 'message ARIA…'}
          disabled={streaming}
          style={{
            flex: 1,
            background: '#0a0a12',
            border: '1px solid #1e1e3a',
            borderRadius: 4,
            padding: '6px 10px',
            color: '#c8c8e8',
            fontFamily: 'monospace',
            fontSize: '0.78rem',
            outline: 'none',
          }}
          onFocus={e => e.target.style.borderColor = '#3b82f6'}
          onBlur={e  => e.target.style.borderColor = '#1e1e3a'}
        />
        <button
          type="submit"
          disabled={streaming || !draft.trim()}
          style={{
            background: '#0f1729',
            border: '1px solid #1e2d4a',
            borderRadius: 4,
            padding: '6px 14px',
            color: '#3b82f6',
            fontFamily: 'monospace',
            fontSize: '0.75rem',
            cursor: streaming ? 'not-allowed' : 'pointer',
            opacity: streaming ? 0.4 : 1,
            letterSpacing: '0.1em',
          }}
        >
          SEND
        </button>
      </form>

      <style>{`
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
      `}</style>
    </div>
  )
}
