import { useEffect, useRef, useState } from 'react'

import * as k from './iso-kit'
import { Dots, IsoFigure, Line, Plate, Solid } from './react/draw'
import { useStillness } from './react/live'
import './iso.css'
import './stock-gate.css'

/** A stock reservation gate (Anatomy skill, Figure tier): the idea behind
 * this page's own "Stock held and released in step with every
 * reservation" line, drawn as a real mechanism instead of said in words.
 *
 * A feed chute holds a stack of crates (on-hand units) over a sliding
 * gate. Reserving opens the gate and parks the bottom crate in a side
 * pocket -- the indicator lamp lights amber. Committing slides that same
 * crate down into the shipped tray and the lamp lights green. The cycle
 * then resets, as a real reservation lane's own throughput would.
 */

const WIDTH = 380
const HEIGHT = 480

const PLATE: k.Plan = { x: -95, y: -78, w: 190, d: 156, r: 10 }
const PLATE_H = 10
const FOOT_H = 6

const CHUTE: k.Plan = { x: -78, y: -55, w: 62, d: 50, r: 3 }
const WALL_T = 6
const CHUTE_WALL_H = 150

const CRATE_W = 50
const CRATE_D = 38
const CRATE_R = 3
const CRATE_H = 34
const CRATE_GAP = 4
const GATE_THICKNESS = 7
const CRATE_X = CHUTE.x + CHUTE.w / 2 - CRATE_W / 2 + 1
const CRATE_Y = CHUTE.y + CHUTE.d / 2 - CRATE_D / 2

const GATE_W = CRATE_W + 8
const GATE_D = CRATE_D + 8
const GATE_CLOSED_X = CRATE_X - 4
const GATE_OPEN_X = CRATE_X - 54

const POCKET: k.Plan = { x: 16, y: CRATE_Y, w: CRATE_W, d: CRATE_D, r: 3 }
const POCKET_Z = GATE_THICKNESS

const SHIP_TRAY: k.Plan = { x: 10, y: 56, w: 58, d: 36, r: 4 }

const LAMP: k.Flat = [CHUTE.x + CHUTE.w + 20, CHUTE.y - 14]
const LAMP_POST_H = 118
const LAMP_HEAD_R = 7

const LABEL: k.Plan = { x: -92, y: 55, w: 100, d: 18, r: 2 }

const P = k.fitProjection(
  [
    ...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0),
    ...k.boxCorners(CHUTE, 0, CHUTE_WALL_H),
    [LAMP[0], LAMP[1], LAMP_POST_H + LAMP_HEAD_R * 2],
  ],
  WIDTH,
  HEIGHT,
  { pad: 26, azimuth: 36, elevation: 32 },
)

const at = (point: k.Point3) => k.iso(point, P)

type GateState = 'idle' | 'reserved' | 'committed'

const STATES: GateState[] = ['idle', 'reserved', 'committed']
const HOLD_MS: Record<GateState, number> = { idle: 2200, reserved: 1500, committed: 1700 }

function Plate_() {
  const feet = k.corners(PLATE, 14)
  const screws = k.corners(PLATE, 12)
  return (
    <>
      <Solid paths={k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 10, 1.6)} tone="mid" halo />
      {feet.map(([x, y], i) => (
        <Solid key={i} paths={k.cylinder(x, y, 6, -PLATE_H - FOOT_H, FOOT_H, P, 20)} tone="lo" />
      ))}
      <Line d={k.planOutline(k.insetPlan(PLATE, 6), 0, P)} tone="lo" />
      <Line d={screws.map(([x, y]) => k.ring(x, y, 2, 0, P, 14)).join('')} tone="lo" />
      <Dots points={screws.map(([x, y]) => at([x, y, 0]))} size={0.5} />
    </>
  )
}

function Ruler() {
  const strip: k.Plan = { x: PLATE.x + 10, y: PLATE.y + PLATE.d - 14, w: PLATE.w - 20, d: 7, r: 1 }
  const ticks = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 5, 5, strip.y + 1, 0.8, [1.8, 3.4], P)
  return (
    <>
      <Solid paths={k.slabOf(strip, 0, 0.8, P, 3)} tone="lo" crease="none" />
      <Line d={ticks.minor} tone="lo" />
      <Line d={ticks.major} tone="mid" />
    </>
  )
}

function LabelPlate() {
  return (
    <>
      <Solid paths={k.slabOf(LABEL, 0, 1.4, P, 2)} tone="lo" crease="none" />
      <text
        className="iso-face-text"
        data-tone="mid"
        transform={k.topMatrix([LABEL.x + 8, LABEL.y + 11], 1.4, P, 'x')}
        fontSize={5.2}
      >
        SKU · LANE 1
      </text>
    </>
  )
}

function Chute() {
  const backWall: k.Plan = { x: CHUTE.x, y: CHUTE.y, w: CHUTE.w, d: WALL_T, r: 2 }
  const leftWall: k.Plan = { x: CHUTE.x, y: CHUTE.y, w: WALL_T, d: CHUTE.d, r: 2 }
  const rightWall: k.Plan = { x: CHUTE.x + CHUTE.w - WALL_T, y: CHUTE.y, w: WALL_T, d: CHUTE.d, r: 2 }
  const lip: k.Plan = { x: CHUTE.x, y: CHUTE.y + CHUTE.d - WALL_T, w: CHUTE.w, d: WALL_T, r: 2 }
  return (
    <>
      <Solid paths={k.slabOf(backWall, 0, CHUTE_WALL_H, P, 3)} tone="mid" />
      <Solid paths={k.slabOf(leftWall, 0, CHUTE_WALL_H, P, 3)} tone="mid" />
      <Solid paths={k.slabOf(rightWall, 0, CHUTE_WALL_H, P, 3)} tone="mid" />
      <Dots points={[30, 70, 110].map((z) => at([CHUTE.x + 1, CHUTE.y + 1, z]))} size={0.5} tone="lo" />
      <Dots
        points={[30, 70, 110].map((z) => at([CHUTE.x + CHUTE.w - 1, CHUTE.y + 1, z]))}
        size={0.5}
        tone="lo"
      />
      <Solid paths={k.slabOf(lip, 0, 10, P, 3)} tone="mid" />
    </>
  )
}

function Crate({ lit }: { lit?: boolean }) {
  const plan: k.Plan = { x: -CRATE_W / 2, y: -CRATE_D / 2, w: CRATE_W, d: CRATE_D, r: CRATE_R }
  return (
    <Solid paths={k.slabOf(plan, 0, CRATE_H, P, 3, 0.6)} tone="hi" crease="lo" lit={lit}>
      <Line d={k.planOutline(k.insetPlan(plan, 4), CRATE_H, P, 3)} tone="faint" />
    </Solid>
  )
}

function Gate() {
  const plan: k.Plan = { x: -GATE_W / 2, y: -GATE_D / 2, w: GATE_W, d: GATE_D, r: 2 }
  return (
    <Solid paths={k.slabOf(plan, 0, 7, P, 3, 0.5)} tone="mid">
      <Dots
        points={[at([-GATE_W / 2 + 6, 0, 7]), at([GATE_W / 2 - 6, 0, 7])]}
        size={0.45}
        tone="lo"
      />
    </Solid>
  )
}

function Pocket() {
  return (
    <>
      <Solid paths={k.slabOf(POCKET, -2, 3, P, 3)} tone="mid" />
      <Line d={k.planOutline(k.insetPlan(POCKET, 3), POCKET_Z + 1, P, 3)} tone="lo" />
    </>
  )
}

function ShippedTray() {
  return (
    <>
      <Solid paths={k.slabOf(SHIP_TRAY, 0, 2, P, 4)} tone="lo" />
      <Line d={k.planOutline(k.insetPlan(SHIP_TRAY, 4), 2, P, 4)} tone="faint" />
      <text
        className="iso-face-text"
        data-tone="faint"
        transform={k.topMatrix([SHIP_TRAY.x + 6, SHIP_TRAY.y + SHIP_TRAY.d - 6], 2, P, 'x')}
        fontSize={4.4}
      >
        SHIPPED
      </text>
    </>
  )
}

function LampPost() {
  const [x, y] = LAMP
  return (
    <>
      <Solid paths={k.cylinder(x, y, 4, 0, LAMP_POST_H, P, 20)} tone="mid" />
      <Solid paths={k.cylinder(x, y, 6, LAMP_POST_H, 3, P, 20)} tone="mid" />
    </>
  )
}

function LampHead({ state }: { state: GateState }) {
  const [x, y] = LAMP
  const z = LAMP_POST_H + 3
  const lit = state !== 'idle'
  const haloPlan: k.Plan = {
    x: x - LAMP_HEAD_R - 2,
    y: y - LAMP_HEAD_R - 2,
    w: (LAMP_HEAD_R + 2) * 2,
    d: (LAMP_HEAD_R + 2) * 2,
    r: LAMP_HEAD_R + 2,
  }
  return (
    <g className={`sg-lamp sg-lamp--${state}`}>
      {lit && (
        <path
          className="iso-halo"
          d={k.haloOf(haloPlan, z, LAMP_HEAD_R * 1.6, P)}
          style={{ filter: 'blur(6px)' }}
        />
      )}
      <Solid paths={k.cylinder(x, y, LAMP_HEAD_R, z, LAMP_HEAD_R * 1.5, P, 24, 1)} tone="hi" crease="none" lit={lit} />
    </g>
  )
}

function movingCratePosition(state: GateState): k.Point3 {
  if (state === 'idle') return [CRATE_X, CRATE_Y, GATE_THICKNESS]
  if (state === 'reserved') return [POCKET.x, POCKET.y, POCKET_Z]
  return [SHIP_TRAY.x + 4, SHIP_TRAY.y + 4, 2]
}

export function StockGateFigure() {
  const calm = useStillness()
  const [state, setState] = useState<GateState>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (calm) return undefined
    timer.current = setTimeout(() => {
      setState((prev) => STATES[(STATES.indexOf(prev) + 1) % STATES.length]!)
    }, HOLD_MS[state])
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [state, calm])

  const gateX = state === 'idle' ? GATE_CLOSED_X : GATE_OPEN_X

  return (
    <div className="iso sg-panel" data-theme="light">
      <Plate
        fig="Fig 1"
        title="Stock gate"
        hint="Runs on a loop"
        readout={state}
        keys={[
          { mark: 'lit', label: 'The crate and lamp: what moves on reserve and commit' },
          { mark: 'raised', label: 'On-hand units, stacked above the gate' },
        ]}
        caption="A feed chute holds crates over a sliding gate. Reserving opens the gate and parks one crate in the side pocket, lamp amber. Committing slides it down into the shipped tray, lamp green. Then the lane resets."
      >
        <IsoFigure
          width={WIDTH}
          height={HEIGHT}
          label="A stock reservation gate: a feed chute holds a stack of crates over a sliding gate. Reserving moves the bottom crate into a side pocket and lights the lamp amber; committing slides it into the shipped tray and lights the lamp green; the lane then resets."
        >
          <Plate_ />
          <Ruler />
          <LabelPlate />
          <Chute />
          <g
            className="sg-move"
            transform={k.translateAlong(P, [CRATE_X, CRATE_Y, GATE_THICKNESS + CRATE_H + CRATE_GAP])}
          >
            <Crate />
          </g>
          <g
            className="sg-move"
            transform={k.translateAlong(P, [CRATE_X, CRATE_Y, GATE_THICKNESS + (CRATE_H + CRATE_GAP) * 2])}
          >
            <Crate />
          </g>
          <g className="sg-move" transform={k.translateAlong(P, [gateX, CRATE_Y, 0])}>
            <Gate />
          </g>
          <Pocket />
          <ShippedTray />
          <g className="sg-move" transform={k.translateAlong(P, movingCratePosition(state))}>
            <Crate lit={state !== 'idle'} />
          </g>
          <LampPost />
          <LampHead state={state} />
        </IsoFigure>
      </Plate>
    </div>
  )
}
