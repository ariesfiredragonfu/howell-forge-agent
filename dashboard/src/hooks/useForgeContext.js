/**
 * useForgeContext — subscribes to /ws/context and returns the live shop snapshot.
 * Reconnects automatically on disconnect.
 */
import { useState, useEffect, useRef } from 'react'

const WS_URL = 'ws://localhost:8765/ws/context'

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
