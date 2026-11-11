// GLSL ES 3.00 sources. Shared chunks are concatenated so edges and pulses evaluate the same Bezier,
// and every pass uses the same fog as labels.ts (math.ts fogFactor).

const HEAD = `#version 300 es
precision highp float;
precision highp int;
`;

const COMMON = `
uniform mat4 uVP;
uniform vec2 uVPpx;
uniform float uDpr, uFogDens, uProj11;
uniform vec3 uEye;
float fogVis(float d){ float x = uFogDens * d; return max(exp(-x * x), 0.12); }
`;

const POS = `
uniform highp sampler2D uPos;
vec3 fetchPos(uint i){ return texelFetch(uPos, ivec2(int(i & 1023u), int(i >> 10u)), 0).xyz; }
vec3 bezCtl(vec3 a, vec3 b, float bend){
  vec3 d = b - a; float len = length(d);
  vec2 s = vec2(-d.z, d.x); float sl = length(s);
  s = sl < 1e-5 ? vec2(1.0, 0.0) : s / sl;
  return (a + b) * 0.5 + vec3(s.x * bend * len, 0.12 * len, s.y * bend * len);
}
vec3 bez(vec3 a, vec3 c, vec3 b, float u){ float v = 1.0 - u; return v * v * a + 2.0 * u * v * c + u * u * b; }
`;

export const BACKDROP_VS = HEAD + `
out vec2 vUV;
void main(){ vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)); vUV = p; gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }`;
export const BACKDROP_FS = HEAD + `
in vec2 vUV; uniform vec3 uBg; uniform vec3 uTint; out vec4 o;
void main(){
  vec2 q = vUV - vec2(0.5, 0.42);
  float r = length(q * vec2(1.4, 1.0));
  vec3 c = uBg + uTint * 0.06 * (1.0 - smoothstep(0.0, 0.9, r)) - uBg * 0.35 * smoothstep(0.4, 1.1, r);
  o = vec4(c, 1.0);
}`;

export const FLOOR_VS = HEAD + COMMON + `
uniform vec3 uCenter; uniform float uRadius;
out vec3 vW; out float vDepth;
void main(){
  vec2 c = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1)) * 2.0 - 1.0;
  vW = uCenter + vec3(c.x, 0.0, c.y) * uRadius * 1.05;
  vDepth = distance(vW, uEye);
  gl_Position = uVP * vec4(vW, 1.0);
}`;
export const FLOOR_FS = HEAD + COMMON + `
in vec3 vW; in float vDepth;
uniform vec3 uCenter, uColor; uniform float uRadius, uAlpha, uStep;
out vec4 o;
void main(){
  vec2 d = vW.xz - uCenter.xz;
  float r = length(d) / uRadius;
  if (r > 1.0) discard;
  vec2 g = abs(fract(d / uStep - 0.5) - 0.5) / fwidth(d / uStep);
  float grid = 1.0 - min(min(g.x, g.y), 1.0);
  float ring = abs(fract(r * 4.0 - 0.5) - 0.5) / fwidth(r * 4.0);
  float rl = 1.0 - min(ring, 1.0);
  float edge = 1.0 - min(abs(r - 0.995) / fwidth(r), 1.0);
  float a = (0.05 + grid * 0.16 + rl * 0.10 + edge * 0.5) * (1.0 - smoothstep(0.82, 1.0, r) * 0.6);
  a *= uAlpha * fogVis(vDepth);
  o = vec4(uColor * a, a);
}`;

export const STARS_VS = HEAD + COMMON + `
in vec3 aP; in float aB; out float vB;
void main(){ gl_Position = uVP * vec4(aP, 1.0); gl_PointSize = (1.0 + aB * 1.5) * uDpr; vB = aB; }`;
export const STARS_FS = HEAD + `
in float vB; uniform vec3 uColor; out vec4 o;
void main(){ float r = length(gl_PointCoord - 0.5) * 2.0; float a = (1.0 - smoothstep(0.3, 1.0, r)) * (0.12 + vB * 0.3); o = vec4(uColor * a, a); }`;

// Edges: instanced 16-vertex strips, constant pixel width, per-kind dash pattern.
export const EDGE_VS = HEAD + COMMON + POS + `
in uvec2 aSD; in uvec2 aKF; in vec3 aAGB;
uniform vec3 uEK[10]; uniform vec3 uLive;
out float vSide; out float vDash; out vec3 vCol; out float vA; flat out uint vKind; flat out uint vFlags; out float vDepth;
void main(){
  int seg = gl_VertexID >> 1;
  float u = float(seg) / 7.0, side = (gl_VertexID & 1) == 0 ? -1.0 : 1.0;
  vec3 A = fetchPos(aSD.x), B = fetchPos(aSD.y), C = bezCtl(A, B, aAGB.z);
  vec3 P = bez(A, C, B, u);
  vec4 c = uVP * vec4(P, 1.0);
  vec4 cn = uVP * vec4(bez(A, C, B, min(u + 0.03, 1.0)), 1.0);
  vec4 cp = uVP * vec4(bez(A, C, B, max(u - 0.03, 0.0)), 1.0);
  vec4 ca = uVP * vec4(A, 1.0), cb = uVP * vec4(B, 1.0);
  if (c.w <= 0.01 || cn.w <= 0.01 || cp.w <= 0.01) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec2 dir = (cn.xy / cn.w - cp.xy / cp.w) * uVPpx;
  dir = length(dir) < 1e-4 ? vec2(1.0, 0.0) : normalize(dir);
  vec2 nrm = vec2(-dir.y, dir.x);
  uint k = aKF.x, fl = aKF.y;
  float w = k <= 2u ? 1.0 : k <= 4u ? 1.4 : 2.0;
  if ((fl & 1u) != 0u) w *= 1.5;
  w += aAGB.y * 1.5;
  float hw = (w * 0.5 + 1.0) * uDpr;
  c.xy += nrm * side * hw * 2.0 / uVPpx * c.w;
  gl_Position = c;
  vSide = side * (w * 0.5 + 1.0) / max(w * 0.5, 0.5);
  vec2 sa = ca.xy / max(ca.w, 0.01) * uVPpx, sb = cb.xy / max(cb.w, 0.01) * uVPpx;
  vDash = u * length(sb - sa) * 0.5 / uDpr * 1.15;
  vCol = (fl & 1u) != 0u ? uLive : uEK[min(k, 9u)];
  vA = aAGB.x + aAGB.y * 0.6;
  vKind = k; vFlags = fl;
  vDepth = distance(P, uEye);
}`;
export const EDGE_FS = HEAD + COMMON + `
in float vSide; in float vDash; in vec3 vCol; in float vA; flat in uint vKind; flat in uint vFlags; in float vDepth;
out vec4 o;
void main(){
  float on = 1.0;
  uint k = vKind;
  bool inferred = (vFlags & 2u) != 0u;
  if (k == 7u) on = step(fract(vDash / 6.0), 0.4);                        // mention: dotted
  else if (k == 5u || inferred) on = step(fract(vDash / 10.0), 0.6);      // exposure / inferred: dashed
  else if (k == 8u) on = step(fract(vDash / 18.0), 0.72);                 // dm: long dash
  else if (k == 9u) { float f = fract(vDash / 16.0); on = (f < 0.5 || (f > 0.69 && f < 0.81)) ? 1.0 : 0.0; } // upvote: dash-dot
  float a = vA * on * (1.0 - smoothstep(0.55, 1.0, abs(vSide))) * fogVis(vDepth);
  if (a < 0.003) discard;
  o = vec4(vCol * a, a);
}`;

// Nodes, trail dots, hull rings and (uHalo = 1) glow halos: instanced camera-facing quads with SDF shapes.
export const NODE_VS = HEAD + COMMON + `
in vec3 aP; in float aS; in vec4 aC; in uint aSh; in uint aBi; in vec2 aXH;
uniform float uHalo;
out vec2 vUV; out vec4 vCol; flat out uint vShape; flat out uint vBits; out float vAux; out float vHeat; out float vDepth; out float vPx;
void main(){
  vec2 corner = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1)) * 2.0 - 1.0;
  vec4 c = uVP * vec4(aP, 1.0);
  uint shape = aSh;
  float pxw = uProj11 * uVPpx.y * 0.5 / max(c.w, 0.01);
  float d = 2.0 * abs(aS) * pxw;
  if (shape == 7u) d = clamp(d, 2.0 * uDpr, 10.0 * uDpr);
  else if (shape != 8u) d = clamp(d, 4.0 * uDpr, 48.0 * uDpr);
  if (uHalo > 0.5) d *= 2.8;
  vPx = d;
  c.xy += corner * d * 0.5 * 2.0 / uVPpx * c.w;
  gl_Position = c.w <= 0.01 ? vec4(2.0, 2.0, 2.0, 1.0) : c;
  vUV = corner; vCol = aC; vShape = shape; vBits = aBi; vAux = aXH.x; vHeat = aXH.y;
  vDepth = distance(aP, uEye);
  if (aS < 0.0) vBits |= 128u;
}`;
export const NODE_FS = HEAD + COMMON + `
in vec2 vUV; in vec4 vCol; flat in uint vShape; flat in uint vBits; in float vAux; in float vHeat; in float vDepth; in float vPx;
uniform float uHalo; uniform vec3 uBg, uLeak, uAccent, uLive, uSolve, uText;
out vec4 o;
float sdHex(vec2 p, float r){ p = abs(p); return max(dot(p, vec2(0.8660254, 0.5)), p.y) - r; }
float sdBox(vec2 p, float b){ vec2 q = abs(p) - b; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0); }
float fill(float d){ float aa = fwidth(d) * 0.9 + 1e-4; return 1.0 - smoothstep(-aa, aa, d); }
float arcMask(vec2 p, float frac){ float a = atan(p.x, p.y) / 6.2831853 + 0.5; return step(1.0 - frac, a); }
void main(){
  float r = length(vUV);
  bool hollow = (vBits & 128u) != 0u;
  if (uHalo > 0.5) {
    float g = vHeat;
    if ((vBits & 4u) != 0u) g = max(g, 0.55);
    if ((vBits & 8u) != 0u) g = max(g, 0.75);
    if ((vBits & 64u) != 0u) g = max(g, 0.45);
    if ((vBits & 2u) != 0u) g *= 0.25;
    if (g <= 0.01 || vShape >= 7u) discard;
    vec3 hc = (vBits & 256u) != 0u ? uLeak : (vBits & 8u) != 0u ? uAccent : (vBits & 64u) != 0u ? uText : vCol.rgb;
    float a = g * exp(-r * r * 5.0) * 0.7 * fogVis(vDepth);
    o = vec4(hc * a, a);
    return;
  }
  float d; vec3 col = vCol.rgb; float a = 0.0;
  if (vShape == 0u) d = sdHex(vUV, 0.6);
  else if (vShape == 1u) d = abs(r - 0.62) - 0.1;
  else if (vShape == 2u) d = r - 0.66;
  else if (vShape == 3u) d = (abs(vUV.x) + abs(vUV.y)) * 0.72 - 0.52;
  else if (vShape == 4u) d = sdBox(vUV, 0.48) - 0.12;
  else if (vShape == 5u) { d = abs(r - 0.58) - 0.1; hollow = false; }
  else if (vShape == 6u) d = r - 0.45;
  else if (vShape == 7u) d = r - 0.8;
  else d = abs(r - 0.96) - 0.012;
  if (hollow) d = abs(d + 0.12) - 0.12;
  a = fill(d);
  if (vShape == 8u) a *= 0.6;
  if (vShape == 2u || vShape == 0u) {
    float ring = fill(abs(r - 0.82) - 0.07) * arcMask(vUV, vAux);
    vec3 rc = vShape == 0u ? uSolve : ((vBits & 2048u) != 0u ? uLive : uText * 0.7);
    col = mix(col, rc, ring * (1.0 - a)); a = max(a, ring);
    if (vShape == 2u && (vBits & 2048u) != 0u) { float fr = fill(abs(r - 0.82) - 0.035); col = mix(col, uLive, fr * (1.0 - fill(r - 0.66)) * 0.5); a = max(a, fr * 0.5); }
  }
  if ((vBits & 256u) != 0u) { float lr = fill(abs(r - 0.86) - 0.08); col = mix(col, uLeak, lr); a = max(a, lr); }
  if ((vBits & 64u) != 0u) { float mr = fill(abs(r - 0.95) - 0.04); col = mix(col, uText, mr); a = max(a, mr); }
  if ((vBits & 12u) != 0u) { float hr = fill(abs(r - 0.95) - 0.05); col = mix(col, uAccent, hr); a = max(a, hr); }
  if ((vBits & 512u) != 0u) { float tr = fill(abs(r - 0.74) - 0.025) * step(0.5, fract(atan(vUV.y, vUV.x) * 1.2732)); col = mix(col, uText, tr * 0.8); }
  a *= vCol.a;
  if (a < 0.01) discard;
  col = mix(uBg, col, fogVis(vDepth));
  o = vec4(col * a, a);
}`;

export const PULSE_VS = HEAD + COMMON + POS + `
in uvec2 aSD; in uvec2 aKF; in vec3 aBUS;
uniform vec3 uEK[10]; uniform vec3 uLive;
out vec3 vCol; out float vA;
void main(){
  vec3 A = fetchPos(aSD.x), B = fetchPos(aSD.y);
  vec3 P = bez(A, bezCtl(A, B, aBUS.x), B, aBUS.y);
  vec4 c = uVP * vec4(P, 1.0);
  gl_Position = c;
  gl_PointSize = (4.0 + 5.0 * aBUS.z) * uDpr;
  vCol = (aKF.y & 1u) != 0u ? uLive : uEK[min(aKF.x, 9u)];
  vA = aBUS.z * (1.0 - aBUS.y * 0.6) * fogVis(distance(P, uEye));
}`;
export const PULSE_FS = HEAD + `
in vec3 vCol; in float vA; out vec4 o;
void main(){ float r = length(gl_PointCoord - 0.5) * 2.0; float a = vA * exp(-r * r * 3.5); if (a < 0.004) discard; vec3 c = mix(vCol, vec3(1.0), 0.35); o = vec4(c * a, a); }`;

// Worldline segments (instanced screen-space quads), pillar tops clamped to the now plane.
export const SEG_VS = HEAD + COMMON + `
in vec3 aA; in vec3 aB; in vec4 aC; in vec2 aWS;
uniform float uNowY, uClamp;
out vec4 vCol; out float vAlong; out float vSide; flat out float vStyle; out float vDepth; out vec2 vUV;
void main(){
  vec3 A = aA, B = aB;
  if (uClamp > 0.5) { A.y = min(A.y, uNowY); B.y = min(B.y, uNowY); }
  float along = float(gl_VertexID & 1), side = float(gl_VertexID >> 1) * 2.0 - 1.0;
  vec4 ca = uVP * vec4(A, 1.0), cb = uVP * vec4(B, 1.0);
  if (ca.w <= 0.01 || cb.w <= 0.01) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec2 sa = ca.xy / ca.w * uVPpx * 0.5, sb = cb.xy / cb.w * uVPpx * 0.5;
  vec2 d = sb - sa; float len = length(d);
  bool bead = aWS.y > 2.5;
  vec2 dir = len < 1e-3 ? vec2(1.0, 0.0) : d / len;
  vec2 nrm = vec2(-dir.y, dir.x);
  float hw = aWS.x * 0.5 * uDpr;
  vec4 c = along < 0.5 ? ca : cb;
  vec2 off = nrm * side * hw;
  if (bead) off += dir * (along * 2.0 - 1.0) * hw;
  c.xy += off * 2.0 / uVPpx * c.w;
  gl_Position = c;
  vCol = aC; vAlong = along * len / uDpr; vSide = side; vStyle = aWS.y;
  vDepth = distance(mix(A, B, along), uEye);
  vUV = vec2(along * 2.0 - 1.0, side);
}`;
export const SEG_FS = HEAD + COMMON + `
in vec4 vCol; in float vAlong; in float vSide; flat in float vStyle; in float vDepth; in vec2 vUV;
uniform float uAlpha; out vec4 o;
void main(){
  float on = 1.0;
  if (vStyle > 2.5) { if (length(vUV) > 1.0) discard; }
  else if (vStyle > 1.5) on = step(fract(vAlong / 5.0), 0.45);
  else if (vStyle > 0.5) on = step(fract(vAlong / 10.0), 0.6);
  float a = vCol.a * uAlpha * on * (vStyle > 2.5 ? 1.0 - smoothstep(0.7, 1.0, length(vUV)) : 1.0 - smoothstep(0.5, 1.0, abs(vSide))) * fogVis(vDepth);
  if (a < 0.003) discard;
  o = vec4(vCol.rgb * a, a);
}`;
