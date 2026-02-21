/**
 * useForgeEvents — subscribes to the Redis pub/sub event bridge (/ws/events).
 *
 * Every event published by ARIA's tools arrives here:
 *   { type: "CAMERA_MOVE",  payload: { position: [x,y,z], target: [x,y,z] } }
 *   { type: "TOGGLE_GROUP", payload: { group: "workholding", visible: false } }
 *
 * Returns the latest event object so ForgeViewer.jsx can react to it.
 * A `ts` field is added so React can distinguish repeated identical events.
 */
import { useState, useEffect } from 'react'

const _HOST         = import.meta.env.VITE_FORGE_API_HOST || 'localhost:8765'
const _PROTO        = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_EVENTS_URL = `${_PROTO}//${_HOST}/ws/events`

export function useForgeEvents() {
  const [lastEvent, setLastEvent] = useState(null)

  useEffect(() => {
    let alive = true
    let ws    = null

    function connect() {
      ws = new WebSocket(WS_EVENTS_URL)

      ws.onopen  = () => { /* connected */ }
      ws.onclose = () => { if (alive) setTimeout(connect, 3000) }
      ws.onerror = () => ws.close()

      ws.onmessage = (e) => {
        if (!alive) return
        try {
          const event = JSON.parse(e.data)
          setLastEvent({ ...event, ts: Date.now() })
        } catch { /* ignore malformed */ }
      }
    }

    connect()
    return () => {
      alive = false
      ws?.close()
    }
  }, [])

  return { lastEvent }
}
