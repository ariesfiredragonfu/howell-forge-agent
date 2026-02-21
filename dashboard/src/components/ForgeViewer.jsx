/**
 * ForgeViewer.jsx — Howell Forge 3D Work Zone
 *
 * Machine: Howell_Forge_Main_CNC — 500 × 400 × 400 mm build volume
 * Source of truth: machine_config.json (served via /machine-config)
 *
 * Named groups (ARIA can toggle visibility of each):
 *   "Environment"   — machine table, T-slot grid, axis labels
 *   "Workholding"   — active fixtures (vise, clamps) per active_layout
 *   "Part"          — active order STL model
 *   "SafeZones"     — per-fixture no-fly zones (red) + safe working volume (green)
 *
 * Fixture data flows:  machine_config.json → /machine-config API → this component
 * Camera control:      ARIA tool set_dashboard_view → Redis → /ws/events → CameraRig
 * Toggle control:      ARIA tool toggle_fixture_visibility → Redis → /ws/events → state
 */

import { Suspense, useState, useRef, useCallback, useEffect } from 'react'
import { Canvas, useLoader, useFrame, useThree } from '@react-three/fiber'
import {
  OrbitControls,
  Grid,
  GizmoHelper,
  GizmoViewport,
  Html,
  Center,
  Environment,
} from '@react-three/drei'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader'
import * as THREE from 'three'
import { useForgeEvents } from '../hooks/useForgeEvents'

// ── Machine envelope — Howell_Forge_Main_CNC (mm) ────────────────────────────
// Source: machine_config.json build_volume

const ENV = {
  X: 500,    // table X travel
  Y: 400,    // table Y travel
  Z: 400,    // Z-axis travel
  CX: 250,   // table center X
  CY: 0,     // table surface Y
  CZ: 200,   // table center Z (depth)
}

// Workholding library — mirrors machine_config.json workholding_library
// Dimensions: length=X, width=Z (depth), height=Y (vertical)
const FIXTURE_LIBRARY = {
  standard_vise: {
    dim:    { x: 152.4, y: 73.0, z: 100.0 },
    buffer: 5.0,
    color:  '#3a5a8a',
    label:  'VISE',
  },
  toe_clamp_set: {
    dim:    { x: 50.0, y: 15.0, z: 20.0 },
    buffer: 2.0,
    color:  '#4a7a6a',
    label:  'TOE CLAMP',
  },
}

// Active layout from machine_config.json active_layout.current_fixtures
// position = [X, Z, Y] in machine coords → Three.js [x, y, z] = [X, 0, Z_machine]
const ACTIVE_FIXTURES = [
  { id: 'vise_01', type: 'standard_vise', position: [0, 0, 0], rotation: 0, status: 'active' },
]

// ── Camera rig — smooth lerp animation driven by ARIA's tool calls ────────────
//
// Receives a `cameraCmd` prop: { position: [x,y,z], target: [x,y,z], ts: n }
// Uses useFrame to lerp the camera + OrbitControls target each frame
// at a configurable speed so the view glides rather than snapping.

const LERP_SPEED = 0.06   // 0=no movement, 1=instant snap. 0.06 = ~1.2s glide

function CameraRig({ cameraCmd, controlsRef }) {
  const { camera } = useThree()

  const goalPos    = useRef(null)
  const goalTarget = useRef(null)
  const tmpVec     = useRef(new THREE.Vector3())

  // Load new goal when ARIA issues a camera command
  useEffect(() => {
    if (!cameraCmd?.payload) return
    const { position, target } = cameraCmd.payload
    if (position) goalPos.current    = new THREE.Vector3(...position)
    if (target)   goalTarget.current = new THREE.Vector3(...target)
  }, [cameraCmd?.ts])

  useFrame(() => {
    if (goalPos.current) {
      camera.position.lerp(goalPos.current, LERP_SPEED)
      if (camera.position.distanceTo(goalPos.current) < 0.5) {
        goalPos.current = null
      }
    }
    if (goalTarget.current && controlsRef.current) {
      tmpVec.current.copy(controlsRef.current.target)
      tmpVec.current.lerp(goalTarget.current, LERP_SPEED)
      controlsRef.current.target.copy(tmpVec.current)
      controlsRef.current.update()
      if (tmpVec.current.distanceTo(goalTarget.current) < 0.5) {
        goalTarget.current = null
      }
    }
  })

  return null
}


// ─── Geometry helpers ─────────────────────────────────────────────────────────

function Box({ pos, size, color, opacity = 1, wireframe = false, name }) {
  return (
    <mesh name={name} position={pos} castShadow receiveShadow>
      <boxGeometry args={size} />
      <meshStandardMaterial
        color={color}
        transparent={opacity < 1}
        opacity={opacity}
        wireframe={wireframe}
        depthWrite={opacity === 1}
      />
    </mesh>
  )
}

function Cylinder({ pos, r, h, color, opacity = 1, name }) {
  return (
    <mesh name={name} position={pos} castShadow>
      <cylinderGeometry args={[r, r, h, 24]} />
      <meshStandardMaterial color={color} transparent={opacity < 1} opacity={opacity} />
    </mesh>
  )
}

// ── Machine Table (Environment group) ────────────────────────────────────────

function MachineTable() {
  const TABLE_H = 10
  // T-slot spacing every 50mm across the 500mm X travel
  const tSlots = Array.from({ length: 9 }, (_, i) => 50 + i * 50)
  return (
    <group name="MachineTable">
      <Box
        name="table-surface"
        pos={[ENV.CX, -TABLE_H / 2, ENV.CZ]}
        size={[ENV.X, TABLE_H, ENV.Y]}
        color="#141420"
      />
      {tSlots.map(x => (
        <Box
          key={x}
          name={`tslot-${x}`}
          pos={[x, 0.6, ENV.CZ]}
          size={[3, 1.2, ENV.Y]}
          color="#0a0a16"
        />
      ))}
    </group>
  )
}

// ── Generic Fixture renderer (reads from FIXTURE_LIBRARY) ────────────────────
//
// machine_config position = [X, Y_machine, Z_machine] in machine coords.
// In Three.js (Y=up): position.x = machine X, position.y = 0 (on table), position.z = machine Z_machine.
// Rotation (degrees around Z_machine / Three.js Y-axis).

function Fixture({ fixture }) {
  const [hovered, setHovered] = useState(false)
  const def = FIXTURE_LIBRARY[fixture.type]
  if (!def) return null

  const { dim, buffer, color, label } = def
  const [mx, , mz] = fixture.position
  const rotRad = (fixture.rotation * Math.PI) / 180

  // Body centered at half-height above table
  const bodyCenter = [
    mx + dim.x / 2,
    dim.y / 2,
    mz + dim.z / 2,
  ]

  const isVise = fixture.type === 'standard_vise'

  return (
    <group
      name={fixture.id}
      rotation={[0, rotRad, 0]}
      onPointerOver={() => setHovered(true)}
      onPointerOut={() => setHovered(false)}
    >
      {/* Main body */}
      <mesh position={bodyCenter} castShadow receiveShadow>
        <boxGeometry args={[dim.x, dim.y, dim.z]} />
        <meshStandardMaterial
          color={hovered ? '#6a8aaa' : color}
          metalness={0.6}
          roughness={0.35}
        />
      </mesh>

      {/* Vise-specific: fixed jaw, moving jaw, lead screw */}
      {isVise && <>
        {/* Fixed jaw — at +X end */}
        <mesh position={[mx + dim.x - 8, dim.y / 2 + 10, mz + dim.z / 2]} castShadow>
          <boxGeometry args={[16, dim.y + 20, dim.z]} />
          <meshStandardMaterial color={hovered ? '#5a7a9a' : '#2a4a7a'} metalness={0.6} roughness={0.3} />
        </mesh>
        {/* Moving jaw — at X origin end */}
        <mesh position={[mx + 8, dim.y / 2 + 10, mz + dim.z / 2]} castShadow>
          <boxGeometry args={[16, dim.y + 20, dim.z]} />
          <meshStandardMaterial color={hovered ? '#5a7a9a' : '#2a4a7a'} metalness={0.6} roughness={0.3} />
        </mesh>
        {/* Lead screw */}
        <mesh
          position={[mx + dim.x / 2, dim.y * 0.6, mz + dim.z / 2]}
          rotation={[0, 0, Math.PI / 2]}
          castShadow
        >
          <cylinderGeometry args={[6, 6, dim.x - 20, 16]} />
          <meshStandardMaterial color="#aaaacc" metalness={0.8} roughness={0.15} />
        </mesh>
        {/* Handle stub */}
        <mesh position={[mx - 15, dim.y * 0.6, mz + dim.z / 2]} castShadow>
          <cylinderGeometry args={[5, 5, 30, 12]} />
          <meshStandardMaterial color="#888" metalness={0.7} roughness={0.2} />
        </mesh>
      </>}

      {/* Toe-clamp specific: strap + bolt */}
      {!isVise && <>
        <mesh position={[mx + dim.x / 2, dim.y, mz + dim.z / 2]} castShadow>
          <cylinderGeometry args={[4, 4, 25, 10]} />
          <meshStandardMaterial color="#aaaacc" metalness={0.8} roughness={0.2} />
        </mesh>
      </>}

      {/* Hover info card */}
      {hovered && (
        <Html
          position={[mx + dim.x / 2, dim.y + 30, mz + dim.z / 2]}
          center
          distanceFactor={300}
        >
          <div style={{
            background: '#06060fdd',
            border: `1px solid ${color}`,
            color: '#c8c8e8',
            padding: '4px 10px',
            borderRadius: 3,
            fontFamily: 'monospace',
            fontSize: 11,
            whiteSpace: 'nowrap',
            lineHeight: 1.6,
          }}>
            <div style={{ color, letterSpacing: '0.1em', marginBottom: 2 }}>
              {label} — {fixture.id.toUpperCase()}
            </div>
            <div>Pos: X{mx} Z{mz}</div>
            <div>Body: {dim.x} × {dim.z} × {dim.y}mm</div>
            <div>No-fly: +{buffer}mm all sides</div>
            <div style={{ color: fixture.status === 'active' ? '#22c55e' : '#ef4444' }}>
              ● {fixture.status.toUpperCase()}
            </div>
          </div>
        </Html>
      )}
    </group>
  )
}

// ── Workholding group — renders all active_layout fixtures ────────────────────

function WorkholdingGroup({ visible }) {
  return (
    <group name="Workholding" visible={visible}>
      {ACTIVE_FIXTURES.map(f => (
        <Fixture key={f.id} fixture={f} />
      ))}
    </group>
  )
}

// ── Safe Zone volumes — per-fixture no-fly zones ──────────────────────────────
//
// Each active fixture contributes a semi-transparent red volume showing the
// no-fly zone (fixture footprint + buffer on all sides).
// A green wireframe shows the remaining safe working volume.

function FixtureNoFlyZone({ fixture }) {
  const def = FIXTURE_LIBRARY[fixture.type]
  if (!def) return null
  const { dim, buffer } = def
  const [mx, , mz] = fixture.position

  const padX = dim.x + buffer * 2
  const padZ = dim.z + buffer * 2
  const padY = dim.y + buffer * 2

  return (
    <Box
      name={`noflyzone-${fixture.id}`}
      pos={[mx + dim.x / 2, padY / 2, mz + dim.z / 2]}
      size={[padX, padY, padZ]}
      color="#ff3333"
      opacity={0.12}
    />
  )
}

function SafeZones({ visible }) {
  return (
    <group name="SafeZones" visible={visible}>
      {/* Per-fixture no-fly zones */}
      {ACTIVE_FIXTURES.map(f => (
        <FixtureNoFlyZone key={f.id} fixture={f} />
      ))}

      {/* Machine envelope wireframe */}
      <Box
        pos={[ENV.CX, ENV.Z / 2, ENV.CZ]}
        size={[ENV.X, ENV.Z, ENV.Y]}
        color="#ffffff"
        opacity={0.05}
        wireframe
        name="envelope"
      />

      {/* Safe working volume — everything outside the no-fly zones */}
      {/* Approximate: X > 162.4+5, full Y, full Z — shown as green tint */}
      <Box
        pos={[ENV.CX + 90, ENV.Z / 2, ENV.CZ]}
        size={[ENV.X - 162.4 - 10, ENV.Z, ENV.Y]}
        color="#22ff88"
        opacity={0.035}
        name="safe-volume"
      />
    </group>
  )
}

// ── STL Part Model ────────────────────────────────────────────────────────────

function PartModel({ url, visible }) {
  const geometry = useLoader(STLLoader, url)
  const meshRef  = useRef()

  // Slow rotation to show detail
  useFrame((_, delta) => {
    if (meshRef.current && visible) {
      meshRef.current.rotation.y += delta * 0.1
    }
  })

  return (
    <group name="Part" visible={visible}>
      <Center position={[ENV.CX, 0, ENV.CZ]}>
        <mesh ref={meshRef} geometry={geometry} castShadow receiveShadow>
          <meshStandardMaterial
            color="#8899cc"
            metalness={0.7}
            roughness={0.3}
            envMapIntensity={1.2}
          />
        </mesh>
      </Center>
    </group>
  )
}

// ── Axis labels ───────────────────────────────────────────────────────────────

function AxisLabels() {
  const style = { fontFamily: 'monospace', fontSize: 10, pointerEvents: 'none' }
  return (
    <group name="AxisLabels">
      <Html position={[ENV.X + 15, 0, 0]}>
        <span style={{ ...style, color: '#cc4444' }}>+X 500</span>
      </Html>
      <Html position={[0, ENV.Z + 15, 0]}>
        <span style={{ ...style, color: '#44cc44' }}>+Z 400</span>
      </Html>
      <Html position={[0, 0, ENV.Y + 15]}>
        <span style={{ ...style, color: '#4444cc' }}>+Y 400</span>
      </Html>
      <Html position={[0, 0, 0]}>
        <span style={{ ...style, color: '#666688', fontSize: 9 }}>HOME</span>
      </Html>
    </group>
  )
}

// ── Visibility Toggle Panel ───────────────────────────────────────────────────

function TogglePanel({ visibility, onToggle }) {
  const groups = [
    { key: 'environment',  label: 'TABLE',       color: '#4a4a6a' },
    { key: 'workholding',  label: 'CLAMPS',      color: '#4a7a6a' },
    { key: 'safezones',    label: 'SAFE ZONES',  color: '#ef4444' },
    { key: 'part',         label: 'PART',        color: '#8899cc' },
  ]

  return (
    <div style={{
      position: 'absolute', top: 10, right: 10,
      display: 'flex', flexDirection: 'column', gap: 4,
      zIndex: 10,
    }}>
      {groups.map(g => (
        <button
          key={g.key}
          onClick={() => onToggle(g.key)}
          style={{
            background: visibility[g.key] ? '#0a0a18' : '#050508',
            border: `1px solid ${visibility[g.key] ? g.color : '#1a1a2a'}`,
            borderRadius: 3,
            padding: '3px 10px',
            color: visibility[g.key] ? g.color : '#2a2a3a',
            fontFamily: 'monospace',
            fontSize: '0.65rem',
            letterSpacing: '0.1em',
            cursor: 'pointer',
            textAlign: 'left',
            minWidth: 100,
          }}
        >
          <span style={{ marginRight: 6 }}>{visibility[g.key] ? '◉' : '○'}</span>
          {g.label}
        </button>
      ))}
      <div style={{
        color: '#2a2a3a',
        fontFamily: 'monospace',
        fontSize: '0.6rem',
        marginTop: 4,
        letterSpacing: '0.08em',
      }}>
        ARIA can toggle ↑
      </div>
    </div>
  )
}

// ── Main ForgeViewer Component ────────────────────────────────────────────────

export default function ForgeViewer({ stlUrl, ariaViewCmd }) {
  const [visibility, setVisibility] = useState({
    environment: true,
    workholding: true,
    safezones:   false,
    part:        true,
  })

  const controlsRef  = useRef(null)
  const [cameraCmd, setCameraCmd] = useState(null)
  const [activeView, setActiveView] = useState('perspective')

  // ── Redis pub/sub events from ARIA's tool calls ──────────────────────────
  const { lastEvent } = useForgeEvents()

  useEffect(() => {
    if (!lastEvent) return
    if (lastEvent.type === 'CAMERA_MOVE') {
      setCameraCmd(lastEvent)
      // Derive label from position for the badge
      const pos = lastEvent.payload?.position
      if (pos) {
        if (pos[1] > 300) setActiveView('top')
        else if (pos[0] > 400) setActiveView('side')
        else if (pos[2] > 400) setActiveView('front')
        else if (pos[1] < 80 && pos[2] < 250) setActiveView('collision_zoom')
        else setActiveView('perspective')
      }
    }
    if (lastEvent.type === 'TOGGLE_GROUP') {
      const { group, visible } = lastEvent.payload || {}
      if (group && group in visibility) {
        setVisibility(prev => ({ ...prev, [group]: visible ?? !prev[group] }))
      }
    }
  }, [lastEvent?.ts])

  const toggleGroup = useCallback((key) => {
    setVisibility(prev => ({ ...prev, [key]: !prev[key] }))
  }, [])

  // Legacy ariaViewCmd prop support (Web chat path)
  useEffect(() => {
    if (!ariaViewCmd) return
    const { toggle, visible } = ariaViewCmd
    const key = toggle?.toLowerCase()
    if (key && key in visibility) {
      setVisibility(prev => ({ ...prev, [key]: visible ?? !prev[key] }))
    }
  }, [ariaViewCmd])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <TogglePanel visibility={visibility} onToggle={toggleGroup} />

      {/* Dimension + view badge */}
      <div style={{
        position: 'absolute', bottom: 10, left: 10, zIndex: 10,
        fontFamily: 'monospace', fontSize: '0.6rem', color: '#2a2a4a',
        letterSpacing: '0.08em', display: 'flex', flexDirection: 'column', gap: 3,
      }}>
        <div>Howell_Forge_Main_CNC · 500 × 400 × 400 mm</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ color: '#3b82f6' }}>◈</span>
          <span style={{ color: '#3a3a5a', textTransform: 'uppercase' }}>
            {activeView}
          </span>
          {lastEvent?.type === 'CAMERA_MOVE' && (
            <span style={{
              color: '#3b82f6',
              fontSize: '0.55rem',
              animation: 'ariaPulse 1.5s ease-out forwards',
            }}>
              ← ARIA
            </span>
          )}
        </div>
      </div>

      <Canvas
        camera={{ position: [600, 350, 600], fov: 45, near: 1, far: 3000 }}
        shadows
        style={{ background: '#06060f' }}
        gl={{ antialias: true }}
      >
        {/* Lighting */}
        <ambientLight intensity={0.3} />
        <directionalLight
          position={[200, 300, 150]}
          intensity={1.2}
          castShadow
          shadow-mapSize={[2048, 2048]}
        />
        <pointLight position={[150, 150, 150]} intensity={0.4} color="#8888ff" />

        {/* HDRI environment for reflections */}
        <Environment preset="warehouse" />

        {/* ── Environment group ── */}
        <group name="Environment" visible={visibility.environment}>
          <MachineTable />
          <Grid
            args={[500, 400]}
            position={[ENV.CX, 0.1, ENV.CZ]}
            cellSize={10}
            cellThickness={0.3}
            cellColor="#1a1a3a"
            sectionSize={50}
            sectionThickness={0.7}
            sectionColor="#2a2a5a"
            fadeDistance={900}
            fadeStrength={1}
            infiniteGrid={false}
          />
          <AxisLabels />
        </group>

        {/* ── Workholding group ── */}
        <WorkholdingGroup visible={visibility.workholding} />

        {/* ── Safe Zones group ── */}
        <SafeZones visible={visibility.safezones} />

        {/* ── Part group ── */}
        {stlUrl ? (
          <Suspense fallback={null}>
            <PartModel url={stlUrl} visible={visibility.part} />
          </Suspense>
        ) : (
          // Placeholder stock block when no STL is loaded
          <group name="Part" visible={visibility.part}>
            <Box
              pos={[ENV.CX, 12, ENV.CZ]}
              size={[80, 25, 60]}
              color="#2a3a5a"
              opacity={0.6}
              name="stock-placeholder"
            />
            <Html position={[ENV.CX, 42, ENV.CZ]} center distanceFactor={350}>
              <div style={{
                color: '#2a2a4a',
                fontFamily: 'monospace',
                fontSize: 10,
                textAlign: 'center',
                pointerEvents: 'none',
              }}>
                NO PART LOADED
              </div>
            </Html>
          </group>
        )}

        {/* ── Camera rig — ARIA-driven smooth animation ── */}
        <CameraRig cameraCmd={cameraCmd} controlsRef={controlsRef} />

        {/* ── Camera controls ── */}
        <OrbitControls
          ref={controlsRef}
          target={[ENV.CX, 0, ENV.CZ]}
          minDistance={80}
          maxDistance={1400}
          maxPolarAngle={Math.PI / 2.05}
        />

        {/* ── Viewport gizmo ── */}
        <GizmoHelper alignment="bottom-left" margin={[60, 60]}>
          <GizmoViewport
            axisColors={['#cc4444', '#44cc44', '#4444cc']}
            labelColor="#888888"
          />
        </GizmoHelper>
      </Canvas>

      <style>{`
        @keyframes ariaPulse {
          0%   { opacity: 1; }
          60%  { opacity: 0.7; }
          100% { opacity: 0; }
        }
      `}</style>
    </div>
  )
}
