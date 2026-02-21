/**
 * useAriaChat — streaming ARIA chat + real-time Safety Agent interrupt.
 *
 * Two WebSocket connections run in parallel:
 *   /ws/chat    — full message exchange, streams Claude response chunks
 *   /ws/safety  — interim transcript checker, triggers hard interrupts
 *
 * Safety interrupt flow:
 *   1. SpeechRecognition fires onresult with interimResults=true
 *   2. Each interim chunk is sent to /ws/safety
 *   3. If { interrupt: true } comes back:
 *      a. window.speechSynthesis.cancel()  — stop any playing ARIA speech
 *      b. SpeechRecognition.abort()        — clear the mic buffer
 *      c. Speak ARIA's interrupt message immediately
 *      d. Push interrupt message into chat log with role "aria-interrupt"
 *   4. Normal final transcript → sendMessage() as before
 */
import { useState, useEffect, useRef, useCallback } from 'react'

const _HOST         = import.meta.env.VITE_FORGE_API_HOST || 'localhost:8765'
const _PROTO        = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_CHAT_URL   = `${_PROTO}//${_HOST}/ws/chat`
const WS_SAFETY_URL = `${_PROTO}//${_HOST}/ws/safety`

// ─── Voice picker ─────────────────────────────────────────────────────────────
function pickAriaVoice() {
  const voices  = window.speechSynthesis?.getVoices() || []
  return (
    voices.find(v => v.name === 'Samantha') ||
    voices.find(v => v.name === 'Karen')    ||
    voices.find(v => v.name.toLowerCase().includes('female')) ||
    voices.find(v => v.lang?.startsWith('en')) ||
    null
  )
}

function speak(text, { rate = 1.05, pitch = 0.9, onEnd } = {}) {
  if (!window.speechSynthesis || !text) return
  window.speechSynthesis.cancel()
  const utt   = new SpeechSynthesisUtterance(text.slice(0, 600))
  utt.rate    = rate
  utt.pitch   = pitch
  utt.voice   = pickAriaVoice()
  if (onEnd) utt.onend = onEnd
  window.speechSynthesis.speak(utt)
}


export function useAriaChat() {
  const [messages,  setMessages]  = useState([])
  const [streaming, setStreaming] = useState(false)
  const [connected, setConnected] = useState(false)
  const [listening, setListening] = useState(false)
  const [interrupt, setInterrupt] = useState(null)   // last interrupt event

  const chatWsRef   = useRef(null)
  const safetyWsRef = useRef(null)
  const bufRef      = useRef('')
  const recognRef   = useRef(null)
  const awakeRef    = useRef(false)   // wake-word state
  const listeningRef = useRef(false)  // mirror for closures

  // ── Chat WebSocket ───────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true

    function connectChat() {
      const ws = new WebSocket(WS_CHAT_URL)
      chatWsRef.current = ws

      ws.onopen  = () => alive && setConnected(true)
      ws.onclose = () => { setConnected(false); if (alive) setTimeout(connectChat, 2000) }
      ws.onerror = () => ws.close()

      ws.onmessage = (e) => {
        if (!alive) return
        let data
        try { data = JSON.parse(e.data) } catch { return }

        if (data.type === 'chunk') {
          bufRef.current += data.text
          setMessages(prev => {
            const msgs = [...prev]
            const last = msgs[msgs.length - 1]
            if (last?.role === 'aria' && last.streaming) {
              msgs[msgs.length - 1] = { ...last, text: bufRef.current }
            } else {
              msgs.push({ role: 'aria', text: bufRef.current, streaming: true })
            }
            return msgs
          })
        }

        if (data.type === 'done') {
          const finalText = bufRef.current
          bufRef.current  = ''
          setStreaming(false)
          setMessages(prev => {
            const msgs = [...prev]
            if (msgs[msgs.length - 1]?.role === 'aria') {
              msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], streaming: false }
            }
            return msgs
          })
          speak(finalText)
        }

        if (data.type === 'error') {
          setStreaming(false)
          bufRef.current = ''
          setMessages(prev => [...prev, { role: 'system', text: `Error: ${data.text}` }])
        }
      }
    }

    connectChat()
    return () => { alive = false; chatWsRef.current?.close() }
  }, [])

  // ── Safety WebSocket ─────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true

    function connectSafety() {
      const ws = new WebSocket(WS_SAFETY_URL)
      safetyWsRef.current = ws

      ws.onclose = () => { if (alive) setTimeout(connectSafety, 3000) }
      ws.onerror = () => ws.close()

      ws.onmessage = (e) => {
        if (!alive) return
        let data
        try { data = JSON.parse(e.data) } catch { return }

        if (data.interrupt) {
          // Hard interrupt — stop everything and speak the override
          window.speechSynthesis.cancel()
          recognRef.current?.abort()
          awakeRef.current = false

          const msg = data.message || 'Interrupting, Chris.'
          setInterrupt({ ...data, ts: Date.now() })
          setMessages(prev => [
            ...prev,
            { role: 'aria-interrupt', text: msg, category: data.category },
          ])
          // Speak immediately, then resume listening
          speak(msg, {
            rate: 1.1,
            pitch: 0.85,
            onEnd: () => {
              if (listeningRef.current) _startRecognition()
            },
          })
        }
      }
    }

    connectSafety()
    return () => { alive = false; safetyWsRef.current?.close() }
  }, [])

  // ── Send message ─────────────────────────────────────────────────────────
  const sendMessage = useCallback((text) => {
    if (!text.trim()) return
    if (!chatWsRef.current || chatWsRef.current.readyState !== WebSocket.OPEN) return
    setMessages(prev => [...prev, { role: 'user', text }])
    setStreaming(true)
    bufRef.current = ''
    chatWsRef.current.send(JSON.stringify({ message: text }))
  }, [])

  // ── SpeechRecognition helpers ─────────────────────────────────────────────
  function _sendInterim(transcript) {
    const ws = safetyWsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ transcript }))
    }
  }

  function _startRecognition() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) return

    const r = new SR()
    r.continuous     = true
    r.interimResults = true   // needed for real-time safety checks
    r.lang           = 'en-US'
    recognRef.current = r

    r.onresult = (e) => {
      const result     = e.results[e.results.length - 1]
      const transcript = result[0].transcript.trim()
      const isFinal    = result.isFinal

      // Stream interim chunks to safety agent
      if (!isFinal) {
        _sendInterim(transcript)
        return
      }

      const lower = transcript.toLowerCase()

      // Wake-word gate
      if (!awakeRef.current) {
        if (lower.includes('forge')) {
          awakeRef.current = true
          setMessages(prev => [...prev, { role: 'system', text: '🔥 ARIA online — speak your command' }])
          speak('Ready.')
        }
        return
      }

      // Final transcript after wake-word — send to ARIA
      if (transcript.length > 2) {
        sendMessage(transcript)
        awakeRef.current = false
      }
    }

    r.onerror = () => setListening(false)
    r.onend   = () => {
      if (listeningRef.current) r.start()   // keep alive
    }

    r.start()
  }

  // ── Toggle voice mode ─────────────────────────────────────────────────────
  const toggleVoice = useCallback(() => {
    if (!('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)) {
      alert('Speech recognition not supported in this browser.')
      return
    }

    if (listeningRef.current) {
      recognRef.current?.stop()
      listeningRef.current = false
      awakeRef.current     = false
      setListening(false)
      return
    }

    listeningRef.current = true
    setListening(true)
    _startRecognition()
  }, [sendMessage])

  return {
    messages,
    streaming,
    connected,
    listening,
    interrupt,
    sendMessage,
    toggleVoice,
  }
}
