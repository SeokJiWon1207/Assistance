// 로그인 화면 뒤의 3D 장면. 외부 라이브러리 없이 WebGL 셰이더 하나로 광택 있는 도형을 그린다.
// 로그인·비밀번호 변경 폼이 보일 때만 시작하고, 로그인 후 #authGate가 숨겨지면 GPU 자원을 정리한다.
// 움직임 줄이기 설정에서는 정지 화면 한 장만 그리고, WebGL이 없으면 .auth-gate의 CSS 배경이 남는다.
(function initializeAuthScene(globalScope) {
  "use strict";

  const gate = document.querySelector("#authGate");
  const canvas = document.querySelector("#authScene");
  if (!gate || !canvas || typeof globalScope.MutationObserver !== "function") return;

  const CAMERA_Z = 8;
  const FOCAL = 1.6;
  const STILL_TIME = 2.4;
  const MAX_PIXEL_RATIO = 1.5;
  const MIN_QUALITY = 0.35;
  const SLOW_FRAME_MS = 34;

  // 화면 비율별 위치(가로 화면 wide, 세로 화면 tall). 값은 화면 절반 크기에 대한 비율이고 카드 바깥쪽에 둔다.
  const ANCHORS = {
    uBlob: { wide: [-0.6, 0.16, 0], tall: [-0.32, 0.7, 0], bob: 0.0 },
    uTorus: { wide: [0.62, -0.3, 0.2], tall: [0.42, -0.72, 0.2], bob: 1.7 },
    uBox: { wide: [0.44, 0.6, -0.4], tall: [0.55, 0.8, -0.4], bob: 3.1 },
    uOrbA: { wide: [-0.36, -0.66, 0.5], tall: [-0.55, -0.82, 0.5], bob: 4.4 },
    uOrbB: { wide: [0.84, 0.22, -0.2], tall: [0.7, 0.46, -0.2], bob: 5.6 },
  };

  const VERTEX_SHADER = `
attribute vec2 aPosition;
void main() { gl_Position = vec4(aPosition, 0.0, 1.0); }
`;

  const FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform vec2 uRes;
uniform float uTime;
uniform float uScale;
uniform vec2 uPointer;
uniform vec3 uBlob;
uniform vec3 uTorus;
uniform vec3 uBox;
uniform vec3 uOrbA;
uniform vec3 uOrbB;

mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
float smin(float a, float b, float k) { float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0); return mix(b, a, h) - k * h * (1.0 - h); }
float sdSphere(vec3 p, float r) { return length(p) - r; }
float sdTorus(vec3 p, vec2 t) { vec2 q = vec2(length(p.xz) - t.x, p.y); return length(q) - t.y; }
float sdRoundBox(vec3 p, vec3 b, float r) { vec3 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0) - r; }

vec2 map(vec3 p) {
  float t = uTime;
  float s = uScale;
  vec3 q = p - uBlob;
  float b = sdSphere(q - vec3(sin(t * 0.9) * 0.28, cos(t * 0.7) * 0.22, sin(t * 0.5) * 0.2) * s, 0.55 * s);
  b = smin(b, sdSphere(q - vec3(cos(t * 0.6 + 1.0) * 0.42, sin(t * 0.8 + 2.0) * 0.32, cos(t * 0.7) * 0.25) * s, 0.38 * s), 0.35 * s);
  b = smin(b, sdSphere(q - vec3(sin(t * 0.5 + 4.0) * 0.36, cos(t * 0.9 + 3.0) * 0.4, 0.0) * s, 0.3 * s), 0.35 * s);
  vec2 res = vec2(b, 1.0);

  q = p - uTorus;
  q.yz *= rot(1.1 + t * 0.35);
  q.xy *= rot(0.5 + t * 0.25);
  float d = sdTorus(q, vec2(0.5, 0.19) * s);
  if (d < res.x) res = vec2(d, 2.0);

  q = p - uBox;
  q.xz *= rot(t * 0.4);
  q.xy *= rot(0.6 + t * 0.3);
  d = sdRoundBox(q, vec3(0.3) * s, 0.1 * s);
  if (d < res.x) res = vec2(d, 3.0);

  d = sdSphere(p - uOrbA, 0.22 * s);
  if (d < res.x) res = vec2(d, 4.0);
  d = sdSphere(p - uOrbB, 0.14 * s);
  if (d < res.x) res = vec2(d, 5.0);
  return res;
}

vec3 calcNormal(vec3 p) {
  vec2 e = vec2(1.0, -1.0) * 0.0015;
  return normalize(e.xyy * map(p + e.xyy).x + e.yyx * map(p + e.yyx).x + e.yxy * map(p + e.yxy).x + e.xxx * map(p + e.xxx).x);
}

float calcAO(vec3 p, vec3 n) {
  float occ = 0.0;
  float w = 1.0;
  for (int i = 1; i <= 4; i++) {
    float h = 0.06 * float(i);
    occ += (h - map(p + n * h).x) * w;
    w *= 0.7;
  }
  return clamp(1.0 - 2.2 * occ, 0.0, 1.0);
}

vec3 material(float id, vec3 n) {
  if (id < 1.5) return mix(vec3(0.19, 0.51, 0.965), vec3(0.545, 0.36, 0.965), clamp(0.5 + 0.6 * dot(n, vec3(-0.4, 0.8, 0.3)), 0.0, 1.0));
  if (id < 2.5) return vec3(0.25, 0.74, 0.82);
  if (id < 3.5) return vec3(0.96, 0.97, 1.0);
  if (id < 4.5) return vec3(0.6, 0.45, 0.97);
  return vec3(0.98, 0.68, 0.18);
}

void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / uRes.y;
  vec2 sp = gl_FragCoord.xy / uRes;
  vec3 ro = vec3(0.0, 0.0, ${CAMERA_Z.toFixed(1)});
  vec3 rd = normalize(vec3(uv, -${FOCAL.toFixed(1)}));
  mat2 yaw = rot(uPointer.x * 0.07);
  mat2 pitch = rot(-uPointer.y * 0.05);
  ro.xz *= yaw; rd.xz *= yaw;
  ro.yz *= pitch; rd.yz *= pitch;

  vec3 bg = mix(vec3(0.945, 0.955, 0.972), vec3(0.975, 0.982, 0.992), sp.y);
  vec2 glow = (sp - vec2(0.5, 0.92)) * vec2(uRes.x / uRes.y, 1.0);
  bg += vec3(0.19, 0.51, 0.96) * 0.09 * exp(-dot(glow, glow) * 5.0);

  float t = 0.0;
  float minD = 1e3;
  vec2 h = vec2(1e3, 0.0);
  bool hit = false;
  for (int i = 0; i < 72; i++) {
    h = map(ro + rd * t);
    minD = min(minD, h.x);
    if (h.x < 0.0015 * t) { hit = true; break; }
    t += h.x;
    if (t > 14.0) break;
  }

  vec3 col = bg;
  if (hit) {
    vec3 p = ro + rd * t;
    vec3 n = calcNormal(p);
    vec3 v = -rd;
    vec3 l = normalize(vec3(-0.5, 0.8, 0.6));
    vec3 hv = normalize(l + v);
    vec3 base = material(h.y, n);
    float dif = clamp(dot(n, l) * 0.5 + 0.5, 0.0, 1.0);
    dif *= dif;
    float spe = pow(clamp(dot(n, hv), 0.0, 1.0), 56.0);
    float fre = pow(1.0 - clamp(dot(n, v), 0.0, 1.0), 3.0);
    float ao = calcAO(p, n);
    col = base * (0.4 + 0.7 * dif) * (0.55 + 0.45 * ao);
    col += base * 0.14 * clamp(-n.y, 0.0, 1.0);
    col += vec3(1.0) * spe * 0.75;
    col = mix(col, vec3(0.9, 0.94, 1.0), fre * 0.5);
  } else {
    bg = mix(bg, bg * vec3(0.9, 0.93, 1.0), exp(-minD * 5.0) * 0.35);
    col = bg;
  }
  vec2 vig = sp - 0.5;
  col *= 1.0 - dot(vig, vig) * 0.12;
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
`;

  const reducedMotion = globalScope.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
  const forms = ["#loginForm", "#passwordChangeForm"].map((selector) => document.querySelector(selector)).filter(Boolean);
  let renderer = null;
  let frameId = 0;
  let disposed = false;
  let startedAt = 0;
  let quality = 0.6;
  let slowFrames = 0;
  let lastFrameAt = 0;
  const pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };

  function smoothstep(edge0, edge1, value) {
    const t = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0)));
    return t * t * (3 - 2 * t);
  }

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error(log || "셰이더를 컴파일하지 못했습니다.");
    }
    return shader;
  }

  function createRenderer() {
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false, depth: false, powerPreference: "low-power" });
    if (!gl) return null;
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "셰이더를 연결하지 못했습니다.");
    gl.useProgram(program);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "aPosition");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const uniforms = {};
    ["uRes", "uTime", "uScale", "uPointer", ...Object.keys(ANCHORS)].forEach((name) => {
      uniforms[name] = gl.getUniformLocation(program, name);
    });
    return { gl, uniforms };
  }

  function resize() {
    if (!renderer) return;
    const ratio = Math.min(globalScope.devicePixelRatio || 1, MAX_PIXEL_RATIO) * quality;
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    renderer.gl.viewport(0, 0, width, height);
  }

  function draw(time) {
    const { gl, uniforms } = renderer;
    const aspect = canvas.width / canvas.height;
    const halfHeight = (CAMERA_Z * 0.5) / FOCAL;
    const halfWidth = halfHeight * aspect;
    const wide = smoothstep(0.8, 1.3, aspect);
    gl.uniform2f(uniforms.uRes, canvas.width, canvas.height);
    gl.uniform1f(uniforms.uTime, time);
    gl.uniform1f(uniforms.uScale, Math.min(1, halfWidth / 1.9));
    gl.uniform2f(uniforms.uPointer, pointer.x, pointer.y);
    Object.entries(ANCHORS).forEach(([name, anchor]) => {
      const [x, y, z] = anchor.wide.map((value, index) => anchor.tall[index] + (value - anchor.tall[index]) * wide);
      const bob = Math.sin(time * 0.7 + anchor.bob) * 0.12;
      gl.uniform3f(uniforms[name], x * halfWidth, y * halfHeight + bob, z);
    });
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    canvas.classList.add("is-ready");
  }

  function isStill() {
    return Boolean(reducedMotion?.matches);
  }

  function stopLoop() {
    if (frameId) globalScope.cancelAnimationFrame(frameId);
    frameId = 0;
  }

  // 느린 기기에서는 해상도를 낮추고, 최저 해상도에서도 느리면 정지 화면으로 바꾼다.
  function adaptQuality(now) {
    const elapsed = now - lastFrameAt;
    lastFrameAt = now;
    if (elapsed <= SLOW_FRAME_MS) {
      slowFrames = Math.max(0, slowFrames - 1);
      return true;
    }
    slowFrames += 1;
    if (slowFrames < 20) return true;
    slowFrames = 0;
    if (quality <= MIN_QUALITY) return false;
    quality = Math.max(MIN_QUALITY, quality * 0.75);
    resize();
    return true;
  }

  function tick(now) {
    frameId = 0;
    if (disposed || document.hidden) return;
    pointer.x += (pointer.targetX - pointer.x) * 0.05;
    pointer.y += (pointer.targetY - pointer.y) * 0.05;
    draw((now - startedAt) / 1000);
    if (adaptQuality(now)) frameId = globalScope.requestAnimationFrame(tick);
  }

  function render() {
    if (!renderer || disposed) return;
    stopLoop();
    resize();
    if (isStill()) {
      pointer.x = pointer.y = 0;
      draw(STILL_TIME);
      return;
    }
    slowFrames = 0;
    frameId = globalScope.requestAnimationFrame((now) => {
      lastFrameAt = now;
      tick(now);
    });
  }

  function onPointerMove(event) {
    pointer.targetX = (event.clientX / globalScope.innerWidth) * 2 - 1;
    pointer.targetY = (event.clientY / globalScope.innerHeight) * 2 - 1;
  }

  function onVisibilityChange() {
    if (!document.hidden) render();
  }

  function start() {
    if (renderer || disposed) return;
    try {
      renderer = createRenderer();
    } catch (error) {
      console.warn("로그인 배경 3D 장면을 끄고 기본 배경을 씁니다.", error);
      renderer = null;
    }
    if (!renderer) return;
    startedAt = globalScope.performance.now() - STILL_TIME * 1000;
    globalScope.addEventListener("resize", render);
    globalScope.addEventListener("pointermove", onPointerMove, { passive: true });
    document.addEventListener("visibilitychange", onVisibilityChange);
    reducedMotion?.addEventListener?.("change", render);
    canvas.addEventListener("webglcontextlost", dispose);
    render();
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    stopLoop();
    observer.disconnect();
    globalScope.removeEventListener("resize", render);
    globalScope.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("visibilitychange", onVisibilityChange);
    reducedMotion?.removeEventListener?.("change", render);
    renderer?.gl.getExtension("WEBGL_lose_context")?.loseContext();
    renderer = null;
    canvas.remove();
  }

  function sync() {
    if (gate.hidden) dispose();
    else if (forms.some((form) => !form.hidden)) start();
  }

  const observer = new globalScope.MutationObserver(sync);
  [gate, ...forms].forEach((element) => observer.observe(element, { attributes: true, attributeFilter: ["hidden"] }));
  sync();
})(globalThis);
