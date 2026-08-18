// 월별 이슈 유형 추이 차트 (ECharts).
//
// ECharts 번들은 1MB가 넘어 모든 사용자가 부담할 이유가 없다. 추이 차트가 실제로
// 응답에 포함될 때만 동적 import 로 불러오고, 한 번 불러온 모듈은 재사용한다.
// 기존 도넛/막대 차트는 그대로 인라인 SVG를 쓰며 이 모듈과 무관하다.
import { prefersReducedMotion } from "./motion.js";

const SERIES_COLOR_VARIABLES = [
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--chart-5",
  "--chart-6",
];

let echartsModulePromise = null;

function loadECharts() {
  if (!echartsModulePromise) {
    echartsModulePromise = import("../vendor/echarts.esm.min.js").catch((error) => {
      // 실패한 프라미스를 캐시하면 재시도가 불가능해진다.
      echartsModulePromise = null;
      throw error;
    });
  }
  return echartsModulePromise;
}

// 서버 값이라도 마크업에 넣기 전에는 이스케이프한다.
function escapeText(value) {
  const holder = document.createElement("span");
  holder.textContent = String(value ?? "");
  return holder.innerHTML;
}

function cssValue(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// 202601 -> 26.01 처럼 축에서 읽기 쉬운 형태로 줄인다.
function formatPeriodTick(period) {
  const text = String(period);
  if (text.length !== 6) return text;
  return `${text.slice(2, 4)}.${text.slice(4)}`;
}

function formatPeriodFull(period) {
  const text = String(period);
  if (text.length !== 6) return text;
  return `${text.slice(0, 4)}년 ${Number(text.slice(4))}월`;
}

function buildOption(trend, metric, palette) {
  const valueKey = metric === "counts" ? "counts" : "efforts";
  const unit = metric === "counts" ? "건" : "";
  const decimals = metric === "counts" ? 0 : 2;

  return {
    animation: !prefersReducedMotion(),
    animationDuration: 700,
    animationEasing: "cubicOut",
    color: palette.series,
    textStyle: { fontFamily: "inherit", color: palette.textSoft },
    // containLabel 은 축 라벨만 고려하므로 하단 범례 높이는 직접 비워 둬야 한다.
    grid: { left: 8, right: 16, top: 16, bottom: 34, containLabel: true },
    tooltip: {
      trigger: "axis",
      backgroundColor: palette.surface,
      borderColor: palette.line,
      borderWidth: 1,
      padding: [8, 11],
      textStyle: { color: palette.text, fontSize: 12 },
      axisPointer: { type: "line", lineStyle: { color: palette.line, width: 1 } },
      formatter: (params) => {
        if (!params?.length) return "";
        const header = formatPeriodFull(params[0].axisValue);
        const rows = params
          .filter((item) => item.value !== null && item.value !== undefined)
          .map((item) => {
            const value = Number(item.value).toFixed(decimals);
            return `<div style="display:flex;gap:10px;align-items:center;justify-content:space-between">
              <span style="display:flex;gap:6px;align-items:center">${item.marker}${item.seriesName}</span>
              <strong style="font-variant-numeric:tabular-nums">${value}${unit}</strong>
            </div>`;
          })
          .join("");
        return `<div style="font-weight:800;margin-bottom:6px">${header}</div>${rows}`;
      },
    },
    legend: {
      bottom: 0,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 16,
      textStyle: { color: palette.textSoft, fontSize: 12 },
      inactiveColor: palette.muted,
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: trend.periods,
      axisLine: { lineStyle: { color: palette.line } },
      axisTick: { show: false },
      axisLabel: {
        color: palette.muted,
        fontSize: 11,
        // 개월 수가 많으면 라벨을 솎아 겹침을 막는다.
        interval: trend.periods.length > 14 ? 1 : 0,
        formatter: formatPeriodTick,
      },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: palette.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: palette.line, type: "dashed" } },
    },
    series: trend.series.map((item, index) => ({
      name: item.label,
      type: "line",
      smooth: 0.4,
      showSymbol: false,
      symbolSize: 7,
      emphasis: { focus: "series" },
      lineStyle: { width: 2 },
      areaStyle: { opacity: 0.16 },
      data: item[valueKey],
      z: trend.series.length - index,
    })),
  };
}

function readPalette() {
  return {
    series: SERIES_COLOR_VARIABLES.map((name, index) => cssValue(name, ["#3182f6", "#1cab63", "#f59f00", "#f04452", "#8b5cf6", "#0ea5b7"][index])),
    text: cssValue("--text", "#111827"),
    textSoft: cssValue("--text-soft", "#374151"),
    muted: cssValue("--muted", "#6b7280"),
    line: cssValue("--line", "#e5e7eb"),
    surface: cssValue("--surface", "#ffffff"),
  };
}

// 월별 추이 차트를 그린다. 탭으로 처리건수/처리공수를 전환한다.
export function renderEffortTrendChart(trend) {
  const wrap = document.createElement("section");
  wrap.className = "effort-trend";

  const hasData = Boolean(trend?.periods?.length && trend?.series?.length);
  if (!hasData) {
    wrap.innerHTML = `<p class="effort-trend-empty">표시할 추이 데이터가 없습니다.</p>`;
    return wrap;
  }

  wrap.innerHTML = `
    <header class="effort-trend-header">
      <div class="effort-trend-tabs" role="tablist" aria-label="추이 지표 선택">
        <button type="button" role="tab" data-trend-metric="counts" aria-selected="true">월별 처리건수</button>
        <button type="button" role="tab" data-trend-metric="efforts" aria-selected="false">월별 처리공수</button>
      </div>
      <span class="effort-trend-period">${escapeText(trend.scopeLabel || "전사")} · ${escapeText(trend.periodLabel || "")}</span>
    </header>
    <div class="effort-trend-canvas" role="img" aria-label="월별 이슈 유형 추이 차트"></div>
  `;

  const canvas = wrap.querySelector(".effort-trend-canvas");
  const tabs = Array.from(wrap.querySelectorAll("[data-trend-metric]"));
  let metric = "counts";
  let chart = null;
  let disposed = false;

  const applyOption = () => {
    if (!chart) return;
    chart.setOption(buildOption(trend, metric, readPalette()), { notMerge: true });
  };

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      metric = tab.dataset.trendMetric;
      tabs.forEach((other) => other.setAttribute("aria-selected", String(other === tab)));
      applyOption();
    });
  });

  // 컨테이너가 화면에 붙고 크기가 잡힌 뒤에 초기화해야 캔버스 크기가 0이 되지 않는다.
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting || chart || disposed) return;
      observer.disconnect();
      loadECharts()
        .then((echarts) => {
          if (disposed) return;
          chart = echarts.init(canvas, null, { renderer: "canvas" });
          applyOption();
        })
        .catch(() => {
          canvas.innerHTML = `<p class="effort-trend-empty">차트 라이브러리를 불러오지 못했습니다.</p>`;
        });
    });
  }, { threshold: 0.1 });
  observer.observe(wrap);

  // 대화 목록이 다시 그려지면 이전 인스턴스를 정리해 캔버스가 새지 않게 한다.
  const resizeObserver = new ResizeObserver(() => chart?.resize());
  resizeObserver.observe(canvas);
  // 테마 전환 시 색상 토큰을 다시 읽어 적용한다.
  const themeObserver = new MutationObserver(() => applyOption());
  themeObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });

  const cleanup = new MutationObserver(() => {
    if (wrap.isConnected) return;
    disposed = true;
    cleanup.disconnect();
    resizeObserver.disconnect();
    observer.disconnect();
    themeObserver.disconnect();
    chart?.dispose();
    chart = null;
  });
  cleanup.observe(document.body, { childList: true, subtree: true });

  return wrap;
}
