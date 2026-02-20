/**
 * OrderQueue — lists active orders with forge run status and approve button.
 * Clicking an order loads its STL into the 3D viewport.
 */
export default function OrderQueue({ ctx, onSelectOrder }) {
  const orders = ctx?.orders?.orders || []
  if (!orders.length) return (
    <div className="panel p-3 h-full flex items-center justify-center">
      <span className="label">no orders</span>
    </div>
  )

  const statusColor = {
    PAID:          '#22c55e',
    Success:       '#22c55e',
    in_production: '#3b82f6',
    Pending:       '#eab308',
    Processing:    '#eab308',
    Failed:        '#ef4444',
  }

  const approve = async (orderId, description) => {
    try {
      const r = await fetch(`http://localhost:8765/orders/${orderId}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description }),
      })
      const data = await r.json()
      alert(`Order ${orderId}: ${data.status || JSON.stringify(data)}`)
    } catch (e) {
      alert(`Approve failed: ${e.message}`)
    }
  }

  return (
    <div className="panel h-full overflow-y-auto">
      <div className="px-3 py-2 label" style={{ borderBottom: '1px solid #1e1e3a' }}>
        ORDER QUEUE — {orders.length} orders
      </div>
      <div className="flex flex-col">
        {orders.slice(0, 20).map((order) => {
          const run   = order.forge_run || {}
          const color = statusColor[order.status] || '#4a4a6a'
          const stlPath = run.has_stl
            ? `http://localhost:8765/forge-file/${order.order_id}/part.stl`
            : null

          return (
            <div
              key={order.order_id}
              className="px-3 py-2"
              style={{
                borderBottom: '1px solid #1a1a2a',
                cursor: stlPath ? 'pointer' : 'default',
              }}
              onClick={() => stlPath && onSelectOrder(order, stlPath)}
            >
              <div className="flex items-center justify-between">
                <span style={{ fontSize: '0.72rem', color: '#8888aa', fontFamily: 'monospace' }}>
                  {order.order_id}
                </span>
                <span style={{ fontSize: '0.65rem', color, letterSpacing: '0.1em' }}>
                  ● {order.status}
                </span>
              </div>

              {run.run_exists && (
                <div style={{ marginTop: 3 }}>
                  <div style={{ fontSize: '0.65rem', color: '#4a4a6a' }}>
                    {run.description && <span>{run.description.slice(0, 48)} · </span>}
                    {run.bbox_mm && <span>{run.bbox_mm[0]?.toFixed(0)}×{run.bbox_mm[1]?.toFixed(0)} mm · </span>}
                    {run.render_count > 0 && <span>{run.render_count} renders · </span>}
                    <span style={{ color: run.gcode_valid ? '#22c55e' : '#ef4444' }}>
                      gcode {run.gcode_valid ? '✓' : '✗'}
                    </span>
                  </div>

                  {/* SHA-256 hash display */}
                  {run.hashes?.manifest_sha256 && (
                    <div style={{
                      marginTop: 4,
                      padding: '3px 6px',
                      background: '#0a0a18',
                      border: '1px solid #1a1a2e',
                      borderRadius: 3,
                      fontFamily: 'monospace',
                    }}>
                      <div style={{ fontSize: '0.55rem', color: '#3a3a5a', letterSpacing: '0.1em', marginBottom: 2 }}>
                        SHA-256 MANIFEST
                      </div>
                      <div style={{ fontSize: '0.6rem', color: '#5555aa', letterSpacing: '0.05em' }}
                           title={run.hashes.manifest_sha256}>
                        {run.hashes.manifest_sha256.slice(0, 12)}…{run.hashes.manifest_sha256.slice(-8)}
                      </div>
                      <div style={{ display: 'flex', gap: 8, marginTop: 3 }}>
                        {['step', 'gcode', 'stl'].map(k => {
                          const h = run.hashes[`${k}_sha256`]
                          return h ? (
                            <span key={k} style={{ fontSize: '0.55rem', color: '#3a3a5a' }}
                                  title={h}>
                              {k}: {h.slice(0,6)}…
                            </span>
                          ) : null
                        })}
                      </div>
                      <div style={{ marginTop: 3, fontSize: '0.55rem' }}>
                        <span style={{
                          color: run.hashes.on_chain ? '#22c55e' : '#3a3a5a',
                          letterSpacing: '0.1em',
                        }}>
                          {run.hashes.on_chain
                            ? `⛓ ON-CHAIN ${run.hashes.chain_tx?.slice(0,10)}…`
                            : '⛓ LOCAL — awaiting Kaito LIVE'}
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {order.status === 'PAID' && (
                <button
                  onClick={(e) => { e.stopPropagation(); approve(order.order_id, run.description) }}
                  style={{
                    marginTop: 4,
                    background: '#0a1a0a',
                    border: '1px solid #1e3a1e',
                    borderRadius: 3,
                    padding: '2px 8px',
                    color: '#22c55e',
                    fontFamily: 'monospace',
                    fontSize: '0.62rem',
                    cursor: 'pointer',
                    letterSpacing: '0.1em',
                  }}
                >
                  → APPROVE PRODUCTION
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
