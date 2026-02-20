/**
 * Viewport3D — react-three/fiber 3D viewer.
 * Renders STL files served from the forge orders directory.
 * Drag-and-drop a .stl or .glb file to load it directly.
 */
import { Suspense, useState, useCallback } from 'react'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Center, Environment, useProgress, Html } from '@react-three/drei'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader'
import { useLoader } from '@react-three/fiber'
import * as THREE from 'three'

// ── STL mesh component ────────────────────────────────────────────────────────
function STLMesh({ url }) {
  const geometry = useLoader(STLLoader, url)
  return (
    <mesh geometry={geometry} castShadow receiveShadow>
      <meshStandardMaterial
        color="#8899bb"
        metalness={0.6}
        roughness={0.3}
        envMapIntensity={0.8}
      />
    </mesh>
  )
}

function Loader() {
  const { progress } = useProgress()
  return <Html center><span style={{ color: '#3b82f6', fontFamily: 'monospace' }}>Loading {progress.toFixed(0)}%</span></Html>
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Viewport3D({ stlUrl }) {
  const [dropUrl, setDropUrl] = useState(null)
  const [dragging, setDragging] = useState(false)

  const activeUrl = dropUrl || stlUrl

  const onDrop = useCallback((e) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file && (file.name.endsWith('.stl') || file.name.endsWith('.glb'))) {
      setDropUrl(URL.createObjectURL(file))
    }
  }, [])

  return (
    <div
      className="relative w-full h-full panel"
      onDrop={onDrop}
      onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      style={{ border: dragging ? '2px solid #3b82f6' : undefined }}
    >
      {/* Header bar */}
      <div className="absolute top-2 left-3 z-10 flex items-center gap-3">
        <span className="label">3D VIEWPORT</span>
        {activeUrl && (
          <span className="label" style={{ color: '#3b82f6' }}>
            {dropUrl ? '⊕ custom file' : '⊕ forge output'}
          </span>
        )}
      </div>

      {/* Drop hint */}
      {!activeUrl && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div className="text-center" style={{ color: '#4a4a6a' }}>
            <div style={{ fontSize: '2.5rem' }}>⬡</div>
            <div className="label mt-2">drop .stl or .glb to load</div>
            <div className="label">or approve an order to auto-load</div>
          </div>
        </div>
      )}

      {dragging && (
        <div className="absolute inset-0 flex items-center justify-center z-20"
             style={{ background: 'rgba(59,130,246,0.08)', pointerEvents: 'none' }}>
          <span style={{ color: '#3b82f6', fontSize: '1.1rem', letterSpacing: '0.1em' }}>DROP FILE</span>
        </div>
      )}

      <Canvas
        shadows
        camera={{ position: [0, 0, 5], fov: 50 }}
        style={{ background: 'transparent' }}
      >
        <ambientLight intensity={0.4} />
        <directionalLight position={[5, 8, 5]} intensity={1.2} castShadow />
        <directionalLight position={[-5, -3, -5]} intensity={0.3} />
        <Environment preset="city" />

        {activeUrl && (
          <Suspense fallback={<Loader />}>
            <Center>
              <STLMesh url={activeUrl} />
            </Center>
          </Suspense>
        )}

        <OrbitControls
          enableDamping
          dampingFactor={0.05}
          minDistance={0.5}
          maxDistance={50}
        />

        {/* Grid floor */}
        <gridHelper args={[20, 20, '#1e1e3a', '#1e1e3a']} position={[0, -2, 0]} />
      </Canvas>
    </div>
  )
}
