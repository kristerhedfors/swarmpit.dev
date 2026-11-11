// Small vec3/mat4 toolkit (column-major, Float32Array) plus the shared scalar helpers.
// DOM-free: imported by the Node tests.

import type { Mat4, Vec3 } from '../types.js';

export const v3 = (x = 0, y = 0, z = 0): Vec3 => {
  const o = new Float32Array(3);
  o[0] = x; o[1] = y; o[2] = z;
  return o;
};
export const m4 = (): Mat4 => new Float32Array(16);

export const clamp = (x: number, a: number, b: number): number => (x < a ? a : x > b ? b : x);
export const lerp = (a: number, b: number, u: number): number => a + (b - a) * u;
export const smoothstep = (u: number): number => {
  const x = clamp(u, 0, 1);
  return x * x * (3 - 2 * x);
};
export const easeOut = (u: number): number => 1 - (1 - u) * (1 - u);
export const easeInOut = (u: number): number => (u < 0.5 ? 2 * u * u : 1 - 2 * (1 - u) * (1 - u));

/** FNV-1a 32-bit hash of a string. */
export function hash32(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}
export const hash01 = (s: string): number => hash32(s) / 4294967296;

/** Deterministic PRNG (mulberry32). */
export function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** First index i with arr[i] > x within [lo, hi). */
export function upperBound(arr: ArrayLike<number>, x: number, lo = 0, hi = arr.length): number {
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((arr[mid] as number) <= x) lo = mid + 1; else hi = mid;
  }
  return lo;
}
/** First index i with arr[i] >= x within [lo, hi). */
export function lowerBound(arr: ArrayLike<number>, x: number, lo = 0, hi = arr.length): number {
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((arr[mid] as number) < x) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/** Visibility in [0.12, 1] for a view-space depth. Shaders use the same formula (see gl/shaders.ts). */
export function fogFactor(depth: number, camDistance: number): number {
  const dens = 1.2 / (camDistance * 2.5);
  const f = Math.exp(-(dens * depth) * (dens * depth));
  return f < 0.12 ? 0.12 : f;
}

export function perspective(out: Mat4, fovY: number, aspect: number, near: number, far: number): Mat4 {
  const f = 1 / Math.tan(fovY / 2), nf = 1 / (near - far);
  out.fill(0);
  out[0] = f / aspect; out[5] = f;
  out[10] = (far + near) * nf; out[11] = -1;
  out[14] = 2 * far * near * nf;
  return out;
}

export function lookAt(out: Mat4, eye: Vec3, target: Vec3, up: Vec3): Mat4 {
  let zx = eye[0]! - target[0]!, zy = eye[1]! - target[1]!, zz = eye[2]! - target[2]!;
  let l = Math.hypot(zx, zy, zz) || 1;
  zx /= l; zy /= l; zz /= l;
  let xx = up[1]! * zz - up[2]! * zy, xy = up[2]! * zx - up[0]! * zz, xz = up[0]! * zy - up[1]! * zx;
  l = Math.hypot(xx, xy, xz) || 1;
  xx /= l; xy /= l; xz /= l;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  out[0] = xx; out[1] = yx; out[2] = zx; out[3] = 0;
  out[4] = xy; out[5] = yy; out[6] = zy; out[7] = 0;
  out[8] = xz; out[9] = yz; out[10] = zz; out[11] = 0;
  out[12] = -(xx * eye[0]! + xy * eye[1]! + xz * eye[2]!);
  out[13] = -(yx * eye[0]! + yy * eye[1]! + yz * eye[2]!);
  out[14] = -(zx * eye[0]! + zy * eye[1]! + zz * eye[2]!);
  out[15] = 1;
  return out;
}

export function mul(out: Mat4, a: Mat4, b: Mat4): Mat4 {
  for (let c = 0; c < 4; c++) {
    const b0 = b[c * 4]!, b1 = b[c * 4 + 1]!, b2 = b[c * 4 + 2]!, b3 = b[c * 4 + 3]!;
    for (let r = 0; r < 4; r++) {
      out[c * 4 + r] = a[r]! * b0 + a[4 + r]! * b1 + a[8 + r]! * b2 + a[12 + r]! * b3;
    }
  }
  return out;
}

/** Projects world point p[o..o+2] with viewProj into CSS px. Writes x, y, clipW (view depth) into out[oo..oo+2].
 *  Returns false when the point is behind the near plane. */
export function projectPoint(vp: Mat4, p: ArrayLike<number>, o: number, w: number, h: number,
                             out: Float32Array, oo: number): boolean {
  const x = p[o]!, y = p[o + 1]!, z = p[o + 2]!;
  const cw = vp[3]! * x + vp[7]! * y + vp[11]! * z + vp[15]!;
  if (cw <= 1e-4) { out[oo] = -1e5; out[oo + 1] = -1e5; out[oo + 2] = -1; return false; }
  const cx = vp[0]! * x + vp[4]! * y + vp[8]! * z + vp[12]!;
  const cy = vp[1]! * x + vp[5]! * y + vp[9]! * z + vp[13]!;
  out[oo] = (cx / cw * 0.5 + 0.5) * w;
  out[oo + 1] = (0.5 - cy / cw * 0.5) * h;
  out[oo + 2] = cw;
  return true;
}

/** Packs linear 0..1 rgba into one uint32 (r in the low byte, little-endian RGBA8). */
export function packRGBA(r: number, g: number, b: number, a: number): number {
  return ((clamp(a, 0, 1) * 255 + 0.5) << 24 | (clamp(b, 0, 1) * 255 + 0.5) << 16 |
          (clamp(g, 0, 1) * 255 + 0.5) << 8 | (clamp(r, 0, 1) * 255 + 0.5)) >>> 0;
}

/** Quadratic Bezier control point for an edge a->b: lift 0.12|ab| in y plus `bend`|ab| sideways (SPEC 5.2).
 *  Mirrored in GLSL (gl/shaders.ts BEZ). */
export function bezierCtl(p: ArrayLike<number>, a: number, b: number, bend: number, out: Float32Array, oo: number): void {
  const ax = p[a * 3]!, ay = p[a * 3 + 1]!, az = p[a * 3 + 2]!, bx = p[b * 3]!, by = p[b * 3 + 1]!, bz = p[b * 3 + 2]!;
  const dx = bx - ax, dy = by - ay, dz = bz - az, len = Math.sqrt(dx * dx + dy * dy + dz * dz);
  let sx = -dz, sz = dx;
  const sl = Math.sqrt(sx * sx + sz * sz);
  if (sl < 1e-5) { sx = 1; sz = 0; } else { sx /= sl; sz /= sl; }
  out[oo] = (ax + bx) / 2 + sx * bend * len;
  out[oo + 1] = (ay + by) / 2 + 0.12 * len;
  out[oo + 2] = (az + bz) / 2 + sz * bend * len;
}
