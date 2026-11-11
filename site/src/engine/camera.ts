// Orbit camera (SPEC 6.1): yaw/pitch/log-distance around a target, inertia, tweens, zoom toward the cursor.
// DOM-free.

import type { CameraMatrices, OrbitCamera, Vec3 } from '../types.js';
import { clamp, easeInOut, lookAt, m4, mul, perspective, projectPoint, v3 } from './math.js';

const DEG = Math.PI / 180;
export const CAM_DEFAULT = { yaw: -35 * DEG, pitch: 28 * DEG, distance: 46 };
const PITCH_MIN = -80 * DEG, PITCH_MAX = 85 * DEG, D_MIN = 6, D_MAX = 160;
const ORBIT_RAD_PER_PX = 0.005;
/** Distance for a bounding radius: r / sin(fov/2) * FIT (perspective + 16:10 aspect at the default yaw). */
export const FIT = 0.76;

interface Tween { t0: number; dur: number; from: number[]; to: number[] }

export interface EngineCamera extends OrbitCamera {
  readonly moving: boolean;
  home: { target: Vec3; radius: number };
  /** Stops inertia and tweens (any new input). */
  halt(): void;
  /** Distance that frames a sphere of radius r in the current viewport (narrow viewports back off). */
  fitDistance(r: number): number;
  /** Viewport edges covered by floating UI (CSS px): the target is drawn centred in the uncovered area
   *  (an off-axis lens shift) and fitDistance frames that area. Eases unless animate is false. */
  setInsets(i: Insets, animate?: boolean): void;
}

export interface Insets { left: number; top: number; right: number; bottom: number }
const INSET_KEYS = ['left', 'top', 'right', 'bottom'] as const;
/** At least this share of each viewport axis stays "free", however large the insets. */
const MIN_FREE = 0.3;

export function createCamera(): EngineCamera {
  const target = v3(0, 0, 0);
  let logD = Math.log(CAM_DEFAULT.distance);
  let yaw = CAM_DEFAULT.yaw, pitch = CAM_DEFAULT.pitch;
  let vel = [0, 0, 0, 0]; // orbit dx, dy, pan dx, dy in px/ms
  let tween: Tween | null = null, clock = 0, reduced = false;
  let lastW = 1, lastH = 1;
  const ins: Insets = { left: 0, top: 0, right: 0, bottom: 0 }, insTo: Insets = { ...ins };
  const fovY = 50 * DEG;
  const eye = v3(), up = v3(0, 1, 0);
  const view = m4(), proj = m4(), viewProj = m4();
  const mats: CameraMatrices = { view, proj, viewProj, eye, widthPx: 1, heightPx: 1, dpr: 1 };
  const fwd = v3(), right = v3(), upv = v3();

  const basis = () => {
    const cp = Math.cos(pitch);
    // eye offset direction (from target to eye)
    const ox = cp * Math.sin(yaw), oy = Math.sin(pitch), oz = cp * Math.cos(yaw);
    fwd[0] = -ox; fwd[1] = -oy; fwd[2] = -oz;
    // right = fwd x up
    let rx = fwd[1]! * 0 - fwd[2]! * 1, rz = fwd[0]! * 1 - fwd[1]! * 0;
    const rl = Math.hypot(rx, rz) || 1;
    rx /= rl; rz /= rl;
    right[0] = rx; right[1] = 0; right[2] = rz;
    // up = right x fwd
    upv[0] = right[1]! * fwd[2]! - right[2]! * fwd[1]!;
    upv[1] = right[2]! * fwd[0]! - right[0]! * fwd[2]!;
    upv[2] = right[0]! * fwd[1]! - right[1]! * fwd[0]!;
  };

  /** Free (uncovered) viewport size in px; insets shrink each axis to at most 1 - MIN_FREE. */
  const freeW = () => lastW - Math.min(ins.left + ins.right, lastW * (1 - MIN_FREE));
  const freeH = () => lastH - Math.min(ins.top + ins.bottom, lastH * (1 - MIN_FREE));
  /** NDC position of the free area's centre, where the target projects. */
  const shiftX = () => (lastW - freeW()) / lastW * (ins.left - ins.right) / Math.max(1e-6, ins.left + ins.right);
  const shiftY = () => (lastH - freeH()) / lastH * (ins.bottom - ins.top) / Math.max(1e-6, ins.top + ins.bottom);

  const cam: EngineCamera = {
    target,
    get distance() { return Math.exp(logD); },
    set distance(d: number) { logD = Math.log(clamp(d, D_MIN, D_MAX)); },
    get yaw() { return yaw; }, set yaw(v: number) { yaw = v; },
    get pitch() { return pitch; }, set pitch(v: number) { pitch = clamp(v, PITCH_MIN, PITCH_MAX); },
    fovY,
    home: { target: v3(0, 0, 0), radius: 20 },
    get moving() { return tween !== null || Math.abs(vel[0]!) + Math.abs(vel[1]!) + Math.abs(vel[2]!) + Math.abs(vel[3]!) > 0 || INSET_KEYS.some((k) => ins[k] !== insTo[k]); },

    orbit(dx, dy) {
      yaw -= dx * ORBIT_RAD_PER_PX;
      pitch = clamp(pitch + dy * ORBIT_RAD_PER_PX, PITCH_MIN, PITCH_MAX);
    },
    pan(dx, dy) {
      basis();
      const wpp = 2 * Math.exp(logD) * Math.tan(fovY / 2) / Math.max(1, lastH);
      for (let i = 0; i < 3; i++) target[i] = target[i]! - right[i]! * dx * wpp + upv[i]! * dy * wpp;
    },
    zoom(factor, ax, ay) {
      if (!(factor > 0) || !isFinite(factor)) return;
      const d0 = Math.exp(logD);
      const d1 = clamp(d0 / factor, D_MIN, D_MAX);
      const f = d0 / d1;
      if (ax !== undefined && ay !== undefined && f !== 1) {
        basis();
        const th = Math.tan(fovY / 2), aspect = lastW / Math.max(1, lastH);
        const nx = (ax / lastW) * 2 - 1 - shiftX(), ny = 1 - (ay / lastH) * 2 - shiftY();
        // the cursor ray hits the plane through target (perpendicular to fwd) at target + offset
        const k = 1 - 1 / f;
        for (let i = 0; i < 3; i++) {
          const off = (right[i]! * nx * th * aspect + upv[i]! * ny * th) * d0;
          target[i] = target[i]! + off * k;
        }
      }
      logD = Math.log(d1);
    },
    fling(vx, vy, px, py) {
      if (reduced) return;
      vel = [vx, vy, px, py];
    },
    halt() { vel = [0, 0, 0, 0]; tween = null; },
    fitDistance(r) {
      const fh = Math.max(1, freeH()), tanH = Math.tan(fovY / 2) * (freeW() / Math.max(1, lastH));
      return Math.max(r * FIT / Math.sin(fovY / 2) * (lastH / fh), r * 1.1 / tanH);
    },
    setInsets(i, animate = true) {
      for (const k of INSET_KEYS) { insTo[k] = Math.max(0, i[k] || 0); if (!animate || reduced) ins[k] = insTo[k]; }
    },
    animateTo(p, ms) {
      vel = [0, 0, 0, 0];
      const cur = [target[0]!, target[1]!, target[2]!, logD, yaw, pitch];
      const tgt = p.target ?? target;
      let ny = p.yaw ?? yaw;
      // shortest way round
      while (ny - yaw > Math.PI) ny -= 2 * Math.PI;
      while (ny - yaw < -Math.PI) ny += 2 * Math.PI;
      const to = [tgt[0]!, tgt[1]!, tgt[2]!, Math.log(clamp(p.distance ?? Math.exp(logD), D_MIN, D_MAX)), ny,
        clamp(p.pitch ?? pitch, PITCH_MIN, PITCH_MAX)];
      if (reduced || ms <= 0) { apply(to); tween = null; return; }
      tween = { t0: clock, dur: ms, from: cur, to };
    },
    fit(center, radius, animate) {
      const d = cam.fitDistance(radius);
      cam.animateTo({ target: center, distance: d }, animate ? 450 : 0);
    },
    reset(animate) {
      cam.animateTo({ target: cam.home.target, yaw: CAM_DEFAULT.yaw, pitch: CAM_DEFAULT.pitch,
                      distance: cam.fitDistance(cam.home.radius) }, animate ? 450 : 0);
    },
    setReducedMotion(on) { reduced = on; if (on) vel = [0, 0, 0, 0]; },
    update(dt) {
      clock += dt;
      let moved = false;
      for (const k of INSET_KEYS) {
        const d = insTo[k] - ins[k];
        if (!d) continue;
        ins[k] = Math.abs(d) < 0.5 ? insTo[k] : ins[k] + d * Math.min(1, dt / 90);
        moved = true;
      }
      if (tween) {
        const u = Math.min(1, (clock - tween.t0) / tween.dur);
        const e = easeInOut(u);
        apply(tween.from.map((a, i) => a + (tween!.to[i]! - a) * e));
        if (u >= 1) tween = null;
        moved = true;
      }
      if (vel[0] || vel[1] || vel[2] || vel[3]) {
        cam.orbit(vel[0]! * dt, vel[1]! * dt);
        if (vel[2] || vel[3]) cam.pan(vel[2]! * dt, vel[3]! * dt);
        const decay = Math.pow(0.92, dt / 16);
        let mag = 0;
        for (let i = 0; i < 4; i++) { vel[i] = vel[i]! * decay; mag += Math.abs(vel[i]!); }
        if (mag < 0.004) vel = [0, 0, 0, 0];
        moved = true;
      }
      return moved;
    },
    matrices(w, h, dpr) {
      lastW = w; lastH = h;
      const d = Math.exp(logD), cp = Math.cos(pitch);
      eye[0] = target[0]! + d * cp * Math.sin(yaw);
      eye[1] = target[1]! + d * Math.sin(pitch);
      eye[2] = target[2]! + d * cp * Math.cos(yaw);
      lookAt(view, eye, target, up);
      perspective(proj, fovY, w / Math.max(1, h), d / 100, d * 20);
      proj[8] = -shiftX(); proj[9] = -shiftY(); // lens shift: clip x/y gain +shift * w, so NDC moves by +shift
      mul(viewProj, proj, view);
      mats.widthPx = w; mats.heightPx = h; mats.dpr = dpr;
      return mats;
    },
    project(p, o, c, out, oo) {
      if (projectPoint(c.viewProj, p, o, c.widthPx, c.heightPx, out, oo)) {
        out[oo + 3] = c.proj[5]! * c.heightPx * 0.5 / out[oo + 2]!;
      } else out[oo + 3] = 0;
    },
  };
  function apply(v: number[]) {
    target[0] = v[0]!; target[1] = v[1]!; target[2] = v[2]!;
    logD = v[3]!; yaw = v[4]!; pitch = v[5]!;
  }
  return cam;
}
