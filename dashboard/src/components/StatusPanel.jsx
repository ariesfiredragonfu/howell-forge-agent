/**
 * StatusPanel — live shop health from ForgeContextProvider WebSocket.
 * Shows biofeedback score, health color, order counts, recent events.
 */
export default function StatusPanel({ ctx, connected }) {
  const bf   = ctx?.biofeedback
  const ords = ctx?.orders
  const sys  = ctx?.system?.subsystems

  const healthColor = {
    HIGH:      '#22c55e',
    STABLE:    '#3b82f6',
    DEGRADED:  '#eab308',
    THROTTLED: '#ef4444',
  }[bf?.health] || '#4a4a6a'

  return (
    <div className="panel flex flex-col gap-3 p-3 h-full overflow-y-auto">

      {/* Connection */}
      <div className="flex items-center gap-2">
        <span className="label">FORGE STATUS</span>
        <span style={{
          width: 7, height: 7, borderRadius: '50%',
          background: connected ? '#22c55e' : '#ef4444',
          display: 'inline-block',
          boxShadow: connected ? '0 0 6px #22c55e' : 'none',
        }} />
        <span className="label" style={{ color: connected ? '#22c55e' : '#ef4444' }}>
          {connected ? 'LIVE' : 'DISCONNECTED'}
        </span>
      </div>

      {/* EWMA score */}
      {bf && (
        <div className="panel p-3">
          <div className="label mb-1">BIOFEEDBACK EWMA</div>
          <div style={{ fontSize: '2rem', fontWeight: 700, color: healthColor, lineHeight: 1 }}>
            {bf.score > 0 ? '+' : ''}{bf.score}
          </div>
          <div className="label mt-1" style={{ color: healthColor }}>
            ● {bf.health}
          </div>

          {/* Score bar */}
          <div style={{ marginTop: 8, height: 4, background: '#1e1e3a', borderRadius: 2 }}>
            <div style={{
              height: '100%',
              width: `${Math.max(2, Math.min(100, (bf.score + 10) / 20 * 100))}%`,
              background: healthColor,
              borderRadius: 2,
              transition: 'width 0.5s ease',
            }} />
          </div>
        </div>
      )}

      {/* Order counts */}
      {ords && (
        <div className="panel p-3">
          <div className="label mb-2">ORDERS</div>
          <div className="flex gap-4">
            {[
              { label: 'TOTAL',  val: ords.total,                color: '#c8c8e8' },
              { label: 'PAID',   val: ords.paid_count,           color: '#22c55e' },
              { label: 'PROD',   val: ords.in_production_count,  color: '#3b82f6' },
              { label: 'PEND',   val: ords.pending_count,        color: '#eab308' },
            ].map(({ label, val, color }) => (
              <div key={label} className="text-center">
                <div style={{ fontSize: '1.4rem', fontWeight: 700, color }}>{val}</div>
                <div className="label">{label}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Subsystem health */}
      {sys && (
        <div className="panel p-3">
          <div className="label mb-2">SUBSYSTEMS</div>
          {Object.entries(sys).map(([key, ok]) => (
            <div key={key} className="flex items-center gap-2 mb-1">
              <span style={{
                width: 6, height: 6, borderRadius: '50%',
                background: ok ? '#22c55e' : '#ef4444',
                display: 'inline-block',
              }} />
              <span className="label" style={{ color: ok ? '#22c55e' : '#ef4444' }}>
                {key.replace(/_ok$/, '').toUpperCase()}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Recent biofeedback events */}
      {bf?.recent_events?.length > 0 && (
        <div className="panel p-3 flex-1 overflow-y-auto">
          <div className="label mb-2">RECENT EVENTS</div>
          {bf.recent_events.slice(0, 10).map((ev, i) => {
            const w = parseFloat(ev.weight || 0)
            const col = w > 0 ? '#22c55e' : w < 0 ? '#ef4444' : '#4a4a6a'
            return (
              <div key={i} className="flex items-center justify-between mb-1"
                   style={{ fontSize: '0.65rem', fontFamily: 'monospace' }}>
                <span style={{ color: '#8888aa' }}>{ev.type}</span>
                <span style={{ color: col }}>{w > 0 ? '+' : ''}{w}</span>
              </div>
            )
          })}
        </div>
      )}

      {/* Timestamp */}
      {ctx?.timestamp && (
        <div className="label" style={{ textAlign: 'right', color: '#2a2a4a' }}>
          {ctx.timestamp.slice(11, 19)} UTC
        </div>
      )}
    </div>
  )
}
