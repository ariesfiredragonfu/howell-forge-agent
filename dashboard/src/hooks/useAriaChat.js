/**
 * useAriaChat — manages streaming ARIA chat over /ws/chat.
 * Handles chunk accumulation, done signal, and voice wake-word detection.
 */
import { useState, useEffect, useRef, useCallback } from 'react'

const WS_URL = 'ws://localhost:8765/ws/chat'

export function useAriaChat() {
  const [messages, setMessages]   = useState([])
  const [streaming, setStreaming] = useState(false)
  const [connected, setConnected] = useState(false)
  const [listening, setListening] = useState(false)
  const wsRef      = useRef(null)
  const bufRef     = useRef('')
  const recognRef  = useRef(null)

  // ── WebSocket connection ───────────────────────────────────────────────────
  useEffect(() => {
    let alive = true

    function connect() {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen  = () => alive && setConnected(true)
      ws.onclose = () => { setConnected(false); if (alive) setTimeout(connect, 2000) }
      ws.onerror = () => ws.close()

      ws.onmessage = (e) => {
        if (!alive) return
        const data = JSON.parse(e.data)

        if (data.type === 'chunk') {
          bufRef.current += data.text
          setMessages(prev => {
            const msgs = [...prev]
            if (msgs.length && msgs[msgs.length - 1].role === 'aria' && msgs[msgs.length - 1].streaming) {
              msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], text: bufRef.current }
            } else {
              msgs.push({ role: 'aria', text: bufRef.current, streaming: true })
            }
            return msgs
          })
        }

        if (data.type === 'done') {
          bufRef.current = ''
          setStreaming(false)
          setMessages(prev => {
            const msgs = [...prev]
            if (msgs.length && msgs[msgs.length - 1].role === 'aria') {
              msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], streaming: false }
            }
            return msgs
          })
          // Speak ARIA's response aloud
          speakText(msgs => msgs[msgs.length - 1]?.text || '')
        }

        if (data.type === 'error') {
          setStreaming(false)
          bufRef.current = ''
          setMessages(prev => [...prev, { role: 'system', text: `Error: ${data.text}` }])
        }
      }
    }

    connect()
    return () => { alive = false; wsRef.current?.close() }
  }, [])

  // ── Send message ─────────────────────────────────────────────────────────
  const sendMessage = useCallback((text) => {
    if (!text.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setMessages(prev => [...prev, { role: 'user', text }])
    setStreaming(true)
    bufRef.current = ''
    wsRef.current.send(JSON.stringify({ message: text }))
  }, [])

  // ── Browser TTS — ARIA speaks back ───────────────────────────────────────
  function speakText(getText) {
    if (!window.speechSynthesis) return
    setMessages(msgs => {
      const text = getText(msgs)
      if (!text) return msgs
      const utt = new SpeechSynthesisUtterance(text.slice(0, 500))
      utt.rate  = 1.05
      utt.pitch = 0.9
      // Pick a female voice if available
      const voices = window.speechSynthesis.getVoices()
      const female = voices.find(v => v.name.toLowerCase().includes('female') ||
                                       v.name.includes('Samantha') ||
                                       v.name.includes('Karen'))
      if (female) utt.voice = female
      window.speechSynthesis.speak(utt)
      return msgs
    })
  }

  // ── Voice wake-word: say "Forge" to activate ─────────────────────────────
  const toggleVoice = useCallback(() => {
    if (!('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)) {
      alert('Speech recognition not supported in this browser.')
      return
    }

    if (listening) {
      recognRef.current?.stop()
      setListening(false)
      return
    }

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    const r  = new SR()
    r.continuous     = true
    r.interimResults = false
    r.lang           = 'en-US'
    recognRef.current = r

    let awake = false
    r.onresult = (e) => {
      const transcript = e.results[e.results.length - 1][0].transcript.trim().toLowerCase()
      if (!awake && transcript.includes('forge')) {
        awake = true
        setMessages(prev => [...prev, { role: 'system', text: '🔥 ARIA online — speak your command' }])
        speakText(() => 'Ready.')
        return
      }
      if (awake && transcript.length > 2) {
        sendMessage(transcript)
        awake = false
      }
    }

    r.onerror = () => setListening(false)
    r.onend   = () => { if (listening) r.start() }   // keep running

    r.start()
    setListening(true)
  }, [listening, sendMessage])

  return { messages, streaming, connected, listening, sendMessage, toggleVoice }
}
