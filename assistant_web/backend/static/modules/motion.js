// anime.js 기반 UI 모션 헬퍼.
// 빌드 스텝 없이 vendor에 넣은 ESM 번들을 그대로 불러온다.
// 모든 애니메이션은 prefers-reduced-motion 설정을 존중하며, 해당 설정이 켜져
// 있으면 애니메이션을 건너뛰고 최종 상태만 즉시 적용한다.
import { animate, createTimeline, stagger, utils } from "../vendor/anime.esm.min.js?v=20260911-6";

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

// 여러 묶음을 위에서 아래로 차례대로 드러내는 등장 타임라인.
// 같은 묶음 안에서는 stagger로 엇갈려 나타나고, 다음 묶음은 이전 묶음이
// 끝나기 조금 전에 시작해 끊기지 않게 이어진다.
// 끝나면 인라인 opacity/transform을 지운다. transform이 남으면 그 안의
// 툴팁·드롭다운 위치 기준이 바뀌기 때문이다.
export function revealSequence(groups, { distance = 8, duration = 420, interval = 70, overlap = 200 } = {}) {
  const steps = groups.map((group) => group.filter(Boolean)).filter((group) => group.length);
  if (!steps.length || prefersReducedMotion()) return;
  const targets = steps.flat();
  targets.forEach((element) => {
    element.style.opacity = "0";
    element.style.transform = `translateY(${distance}px)`;
  });
  const clear = () => targets.forEach((element) => {
    element.style.opacity = "";
    element.style.transform = "";
  });
  whenDocumentVisible(() => {
    const timeline = createTimeline({ defaults: { duration, ease: "outQuad" }, onComplete: clear });
    steps.forEach((step, index) => {
      timeline.add(step, {
        opacity: [0, 1],
        translateY: [distance, 0],
        delay: stagger(interval),
      }, index ? `<-=${overlap}` : 0);
    });
  });
}

// 목록을 통째로 다시 그려도 같은 항목이 이전 자리에서 새 자리로 미끄러지게 하는
// FLIP 재배치. data-layout-key가 같은 요소를 같은 항목으로 본다.
// 위치는 컨테이너 내용 기준으로 재므로 다시 그리며 바뀐 스크롤은 움직임에 섞이지 않는다.
export function captureLayout(container, selector) {
  const positions = new Map();
  if (!container) return positions;
  const origin = container.getBoundingClientRect().top - container.scrollTop;
  container.querySelectorAll(selector).forEach((element) => {
    positions.set(element.dataset.layoutKey, element.getBoundingClientRect().top - origin);
  });
  return positions;
}

// captureLayout으로 기억한 위치에서 현재 위치로 옮기고, 새로 생긴 항목은 서서히 보인다.
// 처음 그릴 때와 탭이 숨겨진 동안에는 재생하지 않는다.
export function animateLayoutChange(container, selector, previous, { duration = 280 } = {}) {
  if (!container || !previous?.size || prefersReducedMotion() || document.hidden) return;
  const current = captureLayout(container, selector);
  container.querySelectorAll(selector).forEach((element) => {
    const key = element.dataset.layoutKey;
    const clear = () => {
      element.style.opacity = "";
      element.style.transform = "";
    };
    if (!previous.has(key)) {
      animate(element, { opacity: [0, 1], duration: 200, ease: "outQuad", onComplete: clear });
      return;
    }
    const offset = previous.get(key) - current.get(key);
    if (Math.abs(offset) < 1) return;
    animate(element, { translateY: [offset, 0], duration, ease: "outCubic", onComplete: clear });
  });
}
