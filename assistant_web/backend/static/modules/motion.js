// anime.js 기반 UI 모션 헬퍼.
// 빌드 스텝 없이 vendor에 넣은 ESM 번들을 그대로 불러온다.
// 모든 애니메이션은 prefers-reduced-motion 설정을 존중하며, 해당 설정이 켜져
// 있으면 애니메이션을 건너뛰고 최종 상태만 즉시 적용한다.
import { animate, stagger, utils } from "../vendor/anime.esm.min.js";

export { animate, stagger, utils };

const reducedMotionQuery = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)");

export function prefersReducedMotion() {
  return Boolean(reducedMotionQuery?.matches);
}

// 문서가 숨겨진 동안에는 anime.js 엔진이 정지하므로 그 상태에서 애니메이션을
// 시작하면 중간 프레임에 멈춘 채로 남는다. 탭이 다시 보일 때까지 기다린다.
function whenDocumentVisible(callback) {
  if (!document.hidden) {
    callback();
    return;
  }
  const onVisibilityChange = () => {
    if (document.hidden) return;
    document.removeEventListener("visibilitychange", onVisibilityChange);
    callback();
  };
  document.addEventListener("visibilitychange", onVisibilityChange);
}

// 요소가 실제로 DOM에 붙고 화면에 보이는 시점에 콜백을 한 번만 실행한다.
// 렌더 시점에는 아직 분리(detached)된 요소이므로 즉시 애니메이션을 걸 수 없다.
export function onFirstVisible(element, callback) {
  if (!element) return;
  const run = () => whenDocumentVisible(() => callback(element));
  if (typeof IntersectionObserver !== "function") {
    requestAnimationFrame(run);
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      observer.disconnect();
      run();
    });
  }, { threshold: 0.15 });
  observer.observe(element);
}

// 숫자 카운트업. formatter로 소수점 자리수나 단위를 유지한다.
// shouldWrite로 쓰기를 잠글 수 있다. 호버 등 다른 상호작용이 같은 요소의
// 텍스트를 점유하는 동안 카운트업이 그 값을 덮어쓰는 것을 막는 용도다.
export function countUp(element, to, {
  duration = 900,
  formatter = (v) => String(Math.round(v)),
  shouldWrite = () => true,
} = {}) {
  if (!element) return;
  const target = Number(to);
  if (!Number.isFinite(target)) return;
  const write = (value) => {
    if (shouldWrite()) element.textContent = formatter(value);
  };
  if (prefersReducedMotion()) {
    write(target);
    return;
  }
  const counter = { value: 0 };
  write(0);
  animate(counter, {
    value: target,
    duration,
    ease: "outExpo",
    onUpdate: () => write(counter.value),
    // 반올림 오차로 최종 숫자가 어긋나지 않도록 목표값을 그대로 다시 쓴다.
    onComplete: () => write(target),
  });
}
