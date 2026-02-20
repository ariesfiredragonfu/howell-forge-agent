/**
 * StatusPanel — live shop health from ForgeContextProvider WebSocket.
 * Shows biofeedback score, health color, order counts, recent events.
 */
const FORGE_STATUS_COLOR = {
  IDLE:    '#4a4a6a',
  RUNNING: '#3b82f6',
  REVIEW:  '#eab308',
  ERROR:   '#ef4444',
}

export default function StatusPanel({ ctx, connected }) {
  const bf      = ctx?.biofeedback
  const ords    = ctx?.orders
  const sys     = ctx?.system?.subsystems
  const forge   = ctx?.forge_status
  const finances = ctx?.finances

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

      {/* Forge machine status */}
      {forge && (
        <div className="panel p-3">
          <div className="label mb-1">FORGE MACHINE</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              width: 8, height: 8, borderRadius: '50%',
              background: FORGE_STATUS_COLOR[forge.status] || '#4a4a6a',
              display: 'inline-block',
              boxShadow: forge.status === 'RUNNING'
                ? `0 0 8px ${FORGE_STATUS_COLOR.RUNNING}`
                : forge.status === 'REVIEW'
                  ? `0 0 8px ${FORGE_STATUS_COLOR.REVIEW}`
                  : 'none',
            }} />
            <span style={{
              fontSize: '0.75rem',
              color: FORGE_STATUS_COLOR[forge.status] || '#4a4a6a',
              letterSpacing: '0.1em',
            }}>
              {forge.status}
            </span>
          </div>
          {forge.detail && (
            <div style={{ fontSize: '0.6rem', color: '#4a4a6a', marginTop: 4 }}>
              {forge.detail}
            </div>
          )}
        </div>
      )}

      {/* Finances — USDC + MATIC on Polygon */}
      {finances && (
        <div className="panel p-3">
          <div className="label mb-2">POLYGON WALLET</div>
          <div style={{ display: 'flex', gap: 16 }}>
            <div>
              <div style={{
                fontSize: '1.1rem', fontWeight: 700,
                color: finances.usdc != null ? '#22c55e' : '#3a3a5a',
              }}>
                {finances.usdc != null ? `$${finances.usdc.toLocaleString()}` : '—'}
              </div>
              <div className="label">USDC</div>
            </div>
            <div>
              <div style={{
                fontSize: '1.1rem', fontWeight: 700,
                color: finances.matic != null ? '#8b5cf6' : '#3a3a5a',
              }}>
                {finances.matic != null ? `${finances.matic} MATIC` : '—'}
              </div>
              <div className="label">GAS</div>
            </div>
          </div>
          {finances.note && (
            <div style={{ fontSize: '0.6rem', color: '#3a3a5a', marginTop: 4 }}>
              {finances.note}
            </div>
          )}
          {finances.wallet && (
            <div style={{ fontSize: '0.55rem', color: '#2a2a4a', marginTop: 3,
                          fontFamily: 'monospace' }}
                 title={finances.wallet}>
              {finances.wallet.slice(0, 8)}…{finances.wallet.slice(-6)}
            </div>
          )}
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
