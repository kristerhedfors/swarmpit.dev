// Thin WebGL2 helpers: program compile/link, uniform lookup, instanced attribute setup.

export interface Prog { p: WebGLProgram; u: Record<string, WebGLUniformLocation | null>; a: Record<string, number> }

export function program(gl: WebGL2RenderingContext, vs: string, fs: string): Prog {
  const sh = (type: number, src: string) => {
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
      throw new Error('shader: ' + gl.getShaderInfoLog(s));
    }
    return s;
  };
  const p = gl.createProgram()!;
  const v = sh(gl.VERTEX_SHADER, vs), f = sh(gl.FRAGMENT_SHADER, fs);
  gl.attachShader(p, v); gl.attachShader(p, f);
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error('link: ' + gl.getProgramInfoLog(p));
  gl.deleteShader(v); gl.deleteShader(f);
  const u: Prog['u'] = {}, a: Prog['a'] = {};
  const nu = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS) as number;
  for (let i = 0; i < nu; i++) {
    const info = gl.getActiveUniform(p, i);
    if (info) { const name = info.name.replace(/\[0\]$/, ''); u[name] = gl.getUniformLocation(p, info.name); }
  }
  const na = gl.getProgramParameter(p, gl.ACTIVE_ATTRIBUTES) as number;
  for (let i = 0; i < na; i++) {
    const info = gl.getActiveAttrib(p, i);
    if (info) a[info.name] = gl.getAttribLocation(p, info.name);
  }
  return { p, u, a };
}

/** Attribute spec: [name, size, type, normalized|integer ('i'), offset]. */
export type AttrSpec = [string, number, number, boolean | 'i', number];

export function vao(gl: WebGL2RenderingContext, pr: Prog, buf: WebGLBuffer, stride: number, attrs: AttrSpec[],
                    divisor: number): WebGLVertexArrayObject {
  const v = gl.createVertexArray()!;
  gl.bindVertexArray(v);
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  for (const [name, size, type, norm, off] of attrs) {
    const loc = pr.a[name];
    if (loc === undefined || loc < 0) continue;
    gl.enableVertexAttribArray(loc);
    if (norm === 'i') gl.vertexAttribIPointer(loc, size, type, stride, off);
    else gl.vertexAttribPointer(loc, size, type, norm, stride, off);
    gl.vertexAttribDivisor(loc, divisor);
  }
  gl.bindVertexArray(null);
  return v;
}
