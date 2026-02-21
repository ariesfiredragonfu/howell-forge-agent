/**
 * useForgeContext — subscribes to /ws/context and returns the live shop snapshot.
 * Reconnects automatically on disconnect.
 */
import { useState, useEffect, useRef } from 'react'

// VITE_FORGE_API_HOST is injected at build time via docker-compose build-args.
// Defaults to localhost:8765 for local development (npm run dev).
const _HOST   = import.meta.env.VITE_FORGE_API_HOST || 'localhost:8765'
const _PROTO  = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_URL  = `${_PROTO}//${_HOST}/ws/context`

export function useForgeContext() {
  const [ctx, setCtx]       = useState(null)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)

  useEffect(() => {
    let alive = true

    function connect() {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      ws.onopen  = () => alive && setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        if (alive) setTimeout(connect, 2000)   // auto-reconnect
      }
      ws.onerror = () => ws.close()
      ws.onmessage = (e) => {
        try { alive && setCtx(JSON.parse(e.data)) }
        catch {}
      }
    }

    connect()
    return () => { alive = false; wsRef.current?.close() }
  }, [])

  return { ctx, connected }
}
