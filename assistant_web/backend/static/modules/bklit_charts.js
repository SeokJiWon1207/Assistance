// React는 이 모듈이 생성한 차트 자리만 소유한다. 앱의 기존 이벤트 위임은 유지한다.
const mounted = new Map();
const generations = new WeakMap();
let bundlePromise;
const escapeAttribute = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');

export function chartHost(spec, fallback = '<p class="bklit-empty">차트를 준비하고 있습니다.</p>') {
  const content = spec.kind === 'bar' ? `<details data-bklit-values open><summary>${spec.action ? "수치·필터 보기" : "수치 보기"}</summary>${fallback}</details>` : fallback;
  return `<div class="bklit-host" role="group" aria-label="${escapeAttribute(spec.title)}" data-bklit-kind="${escapeAttribute(spec.kind)}" data-bklit-spec="${escapeAttribute(JSON.stringify(spec))}"><div class="bklit-root" data-bklit-mount></div><div data-bklit-fallback>${content}</div></div>`;
}

export function releaseCharts(container) {
  if (!container) return;
  generations.set(container, (generations.get(container) || 0) + 1);
  for (const [host, cleanup] of mounted) {
    if (!host.isConnected || container.contains(host)) { cleanup(); mounted.delete(host); }
  }
}

export async function mountCharts(container) {
  if (!container?.querySelectorAll) return;
  const hosts = [...container.querySelectorAll('[data-bklit-spec]')];
  if (!hosts.length) return;
  const generation = generations.get(container) || 0;
  try {
    bundlePromise ||= import('../vendor/bklit/charts.js?v=20261027-1').catch(error => { bundlePromise = null; throw error; });
    const { mountChart } = await bundlePromise;
    if ((generations.get(container) || 0) !== generation) return;
    for (const host of hosts) {
      if (host.isConnected && !mounted.has(host)) mounted.set(host, mountChart(host, JSON.parse(host.dataset.bklitSpec)));
    }
  } catch (error) {
    // 수치와 필터 버튼이 들어 있는 기존 HTML 대체 화면을 계속 제공한다.
    for (const host of hosts) host.dataset.chartError = 'true';
    console.warn('차트 모듈을 불러오지 못해 기본 수치를 표시합니다.', error);
  }
}
