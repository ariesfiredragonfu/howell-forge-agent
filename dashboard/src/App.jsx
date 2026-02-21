/**
 * App — Howell Forge Mission Control Dashboard
 *
 * Layout (left → right):
 *   [Order Queue 18%] [3D Viewport 50%] [Status + ARIA Chat 32%]
 *
 * Live data: ForgeContextProvider WebSocket on port 8765
 * ARIA chat: streaming Claude Code CLI via WebSocket
 * Voice: browser Web Speech API — say "Forge" to wake ARIA
 */
import { useState, useEffect } from 'react'
import ForgeViewer from './components/ForgeViewer'
import StatusPanel from './components/StatusPanel'
import AriaChat    from './components/AriaChat'
import OrderQueue  from './components/OrderQueue'
import { useForgeContext } from './hooks/useForgeContext'
import { useAriaChat }     from './hooks/useAriaChat'

export default function App() {
  const { ctx, connected } = useForgeContext()
  const ariaHook = useAriaChat()
  const [selectedOrder, setSelectedOrder] = useState(null)
  const [stlUrl, setStlUrl]               = useState(null)
  const [ariaViewCmd, setAriaViewCmd]     = useState(null)

  const handleSelectOrder = (order, url) => {
    setSelectedOrder(order)
    setStlUrl(url)
  }

  // Listen for ARIA forge_view commands in the chat message stream
  // e.g. ARIA sends: {"type":"forge_view","toggle":"workholding","visible":false}
  useEffect(() => {
    const msgs = ariaHook.messages
    if (!msgs.length) return
    const last = msgs[msgs.length - 1]
    if (last?.role === 'aria' && last.text) {
      const match = last.text.match(/\{"type"\s*:\s*"forge_view"[^}]+\}/)
      if (match) {
        try {
          const cmd = JSON.parse(match[0])
          setAriaViewCmd({ ...cmd, ts: Date.now() })
        } catch { /* ignore malformed */ }
      }
    }
  }, [ariaHook.messages])

  const bf = ctx?.biofeedback
  const healthColor = {
    HIGH:      '#22c55e',
    STABLE:    '#3b82f6',
    DEGRADED:  '#eab308',
    THROTTLED: '#ef4444',
  }[bf?.health] || '#1e1e3a'

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',
      background: '#0a0a12',
      color: '#c8c8e8',
      fontFamily: 'monospace',
    }}>

      {/* ── Top bar ──────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '6px 16px',
        background: '#0d0d1a',
        borderBottom: '1px solid #1e1e3a',
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: '0.85rem', letterSpacing: '0.2em', color: '#8888cc' }}>
            ⬡ HOWELL FORGE
          </span>
          <span style={{ fontSize: '0.6rem', letterSpacing: '0.15em', color: '#2a2a4a' }}>
            MISSION CONTROL v1.0
          </span>
        </div>

        {/* Health indicator strip */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {bf && (
            <>
              <span style={{ fontSize: '0.6rem', color: '#4a4a6a', letterSpacing: '0.1em' }}>EWMA</span>
              <span style={{
                fontSize: '0.75rem', fontWeight: 700, color: healthColor,
                textShadow: `0 0 8px ${healthColor}66`,
              }}>
                {bf.score > 0 ? '+' : ''}{bf.score}
              </span>
              <div style={{
                width: 8, height: 8, borderRadius: '50%',
                background: healthColor,
                boxShadow: `0 0 8px ${healthColor}`,
              }} />
              <span style={{ fontSize: '0.6rem', color: healthColor, letterSpacing: '0.1em' }}>
                {bf.health}
              </span>
            </>
          )}
          <span style={{ fontSize: '0.6rem', color: '#2a2a4a', marginLeft: 12 }}>
            {ctx?.timestamp?.slice(11, 19) || '--:--:--'} UTC
          </span>
        </div>
      </div>

      {/* ── Main layout ──────────────────────────────────────────────── */}
      <div style={{ display: 'flex', flex: 1, gap: 6, padding: 6, overflow: 'hidden' }}>

        {/* Order queue — left column */}
        <div style={{ width: '18%', flexShrink: 0, overflow: 'hidden' }}>
          <OrderQueue ctx={ctx} onSelectOrder={handleSelectOrder} />
        </div>

        {/* 3D Work Zone — ForgeViewer with Environment/Workholding groups */}
        <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ flex: 1, overflow: 'hidden' }}>
            <ForgeViewer stlUrl={stlUrl} ariaViewCmd={ariaViewCmd} />
          </div>

          {/* Selected order info bar */}
          {selectedOrder && (
            <div className="panel" style={{ padding: '6px 12px', flexShrink: 0 }}>
              <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
                <span className="label">SELECTED</span>
                <span style={{ fontSize: '0.75rem', color: '#8888cc' }}>{selectedOrder.order_id}</span>
                {selectedOrder.forge_run?.bbox_mm && (
                  <span className="label">
                    {selectedOrder.forge_run.bbox_mm.map(v => v?.toFixed(1)).join(' × ')} mm
                  </span>
                )}
                {selectedOrder.forge_run?.description && (
                  <span className="label" style={{ color: '#6666aa', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {selectedOrder.forge_run.description}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right column — status + ARIA */}
        <div style={{ width: '30%', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 6, overflow: 'hidden' }}>
          <div style={{ height: '42%', overflow: 'hidden' }}>
            <StatusPanel ctx={ctx} connected={connected} />
          </div>
          <div style={{ flex: 1, overflow: 'hidden' }}>
            <AriaChat hook={ariaHook} />
          </div>
        </div>

      </div>
    </div>
  )
}
