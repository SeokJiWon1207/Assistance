import { chartHost } from "./bklit_charts.js?v=20261026-1";
import { metricChartSpec, formatChartValue } from "./chart_models.js?v=20261026-1";
import { animate, stagger, prefersReducedMotion, onFirstVisible, countUp } from "./motion.js?v=20260911-6";

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = String(value ?? "");
  return span.innerHTML;
}

export const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

export function truncateChartLabel(label, maxLength = 16) {
  const text = String(label ?? "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

// 차트 데이터 객체 단위로 등장 애니메이션을 1회만 재생하기 위한 표시.
// renderMessages()가 DOM을 통째로 다시 그리기 때문에 DOM이 아니라 데이터
// 객체를 기준으로 판단해야 스트리밍 중 애니메이션이 반복되지 않는다.
// WeakSet이라 대화 저장(JSON) 결과에는 영향을 주지 않는다.
const animatedCharts = new WeakSet();

function shouldAnimateChart(chart) {
  if (prefersReducedMotion()) return false;
  if (!chart || typeof chart !== "object") return false;
  if (animatedCharts.has(chart)) return false;
  animatedCharts.add(chart);
  return true;
}

// 원형(도넛) 차트: 항목별 비중을 stroke-dasharray로 구간을 나눠 그린다.
// 외부 차트 라이브러리 없이도 실제 원형 차트로 렌더링하기 위한 방식이다.
function renderLegacyDonutChart(items, { centerLabel = "", valueFormatter = (v) => v.toFixed(2), animated = false } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "donut-chart-row";
  const total = items.reduce((sum, item) => sum + item.amount, 0) || 1;

  const size = 120;
  const radius = 42;
  const strokeWidth = 18;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const segments = items
    .map((item, index) => {
      const segLen = (item.amount / total) * circumference;
      const circle = `<circle data-donut-segment data-seg-index="${index}" data-seg-length="${segLen}" cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none" stroke="${CHART_COLORS[index % CHART_COLORS.length]}" stroke-width="${strokeWidth}" stroke-dasharray="${segLen} ${circumference - segLen}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${size / 2} ${size / 2})" />`;
      offset += segLen;
      return circle;
    })
    .join("");

  const svg = document.createElement("div");
  svg.className = "donut-chart-svg";
  svg.innerHTML = `
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="유형별 공수 비중 도넛 차트">
      ${segments}
      <text x="${size / 2}" y="${size / 2 - 4}" text-anchor="middle" class="donut-chart-total">${escapeHtml(valueFormatter(total))}</text>
      <text x="${size / 2}" y="${size / 2 + 12}" text-anchor="middle" class="donut-chart-total-label">${escapeHtml(centerLabel)}</text>
    </svg>
  `;
  wrap.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "chart-legend";
  items.forEach((item, index) => {
    const pct = Math.round((item.amount / total) * 100);
    const row = document.createElement("div");
    row.className = "chart-legend-row";
    row.dataset.segIndex = String(index);
    row.innerHTML = `
      <span class="chart-legend-swatch" style="background:${CHART_COLORS[index % CHART_COLORS.length]}"></span>
      <span>${escapeHtml(item.label)}</span>
      <span class="chart-legend-pct">${pct}%</span>
      <span class="chart-legend-value">${escapeHtml(valueFormatter(item.amount))}</span>
    `;
    legend.appendChild(row);
  });
  wrap.appendChild(legend);

  attachDonutHover(wrap, items, { total, centerLabel, valueFormatter });
  if (animated) animateDonutChart(wrap, { circumference, total, valueFormatter });
  return wrap;
}
// 구간과 범례 행을 양방향으로 연동한다. 어느 쪽에 마우스를 올려도 같은 항목이
// 강조되고, 중앙 숫자가 해당 항목의 값과 이름으로 바뀐다.
function attachDonutHover(wrap, items, { total, centerLabel, valueFormatter }) {
  const segments = Array.from(wrap.querySelectorAll("[data-donut-segment]"));
  const legendRows = Array.from(wrap.querySelectorAll(".chart-legend-row"));
  const totalText = wrap.querySelector(".donut-chart-total");
  const totalLabel = wrap.querySelector(".donut-chart-total-label");
  if (!segments.length) return;

  const focus = (index) => {
    // 애니메이션 중 카운트업이 중앙 숫자를 덮어쓰지 않도록 표시를 남긴다.
    wrap.dataset.hoverIndex = String(index);
    wrap.classList.add("has-hover");
    segments.forEach((segment) => segment.classList.toggle("is-active", Number(segment.dataset.segIndex) === index));
    legendRows.forEach((row) => row.classList.toggle("is-active", Number(row.dataset.segIndex) === index));
    const item = items[index];
    if (item && totalText) totalText.textContent = valueFormatter(item.amount);
    if (item && totalLabel) totalLabel.textContent = truncateChartLabel(item.label, 10);
  };

  const reset = () => {
    delete wrap.dataset.hoverIndex;
    wrap.classList.remove("has-hover");
    segments.forEach((segment) => segment.classList.remove("is-active"));
    legendRows.forEach((row) => row.classList.remove("is-active"));
    if (totalText) totalText.textContent = valueFormatter(total);
    if (totalLabel) totalLabel.textContent = centerLabel;
  };

  [...segments, ...legendRows].forEach((element) => {
    const index = Number(element.dataset.segIndex);
    element.addEventListener("mouseenter", () => focus(index));
  });
  wrap.addEventListener("mouseleave", reset);
}

// 도넛 등장 연출: 각 구간을 시작점에서부터 자라나게 하고 범례를 순차로 띄운다.
// 마크업은 항상 최종 상태로 만들어 두고, 애니메이션을 켤 때만 초기 상태로
// 되돌린다. 이렇게 해야 애니메이션이 중단돼도 잘못된 차트가 남지 않는다.
function animateDonutChart(wrap, { circumference, total, valueFormatter }) {
  const segments = Array.from(wrap.querySelectorAll("[data-donut-segment]"));
  const legendRows = Array.from(wrap.querySelectorAll(".chart-legend-row"));
  const totalText = wrap.querySelector(".donut-chart-total");

  // 아직 DOM에 붙기 전이므로 여기서 초기 상태로 접어두면 깜빡임이 없다.
  segments.forEach((segment) => segment.setAttribute("stroke-dasharray", `0 ${circumference}`));
  legendRows.forEach((row) => {
    row.style.opacity = "0";
    row.style.transform = "translateX(-6px)";
  });

  onFirstVisible(wrap, () => {
    segments.forEach((segment, index) => {
      const segLength = Number(segment.dataset.segLength) || 0;
      const proxy = { length: 0 };
      animate(proxy, {
        length: segLength,
        duration: 760,
        delay: index * 90,
        ease: "outCubic",
        onUpdate: () => {
          segment.setAttribute("stroke-dasharray", `${proxy.length} ${circumference - proxy.length}`);
        },
        // 부동소수점 오차 없이 정확한 최종 비중으로 마무리한다.
        onComplete: () => {
          segment.setAttribute("stroke-dasharray", `${segLength} ${circumference - segLength}`);
        },
      });
    });

    if (totalText) {
      countUp(totalText, total, {
        duration: 900,
        formatter: valueFormatter,
        // 사용자가 구간에 마우스를 올린 상태면 그 값이 우선이다.
        shouldWrite: () => !wrap.dataset.hoverIndex,
      });
    }

    if (legendRows.length) {
      animate(legendRows, {
        opacity: [0, 1],
        translateX: [-6, 0],
        duration: 420,
        delay: stagger(70, { start: 180 }),
        ease: "outQuad",
      });
    }
  });
}

// 그라디언트 등 SVG 내부 id가 여러 차트 사이에서 충돌하지 않도록 하는 카운터.
// 한 화면에 공수 차트와 순위 차트가 동시에 존재할 수 있다.
let chartIdSequence = 0;

// 축 눈금으로 쓸 "보기 좋은" 값들을 계산한다. 1/2/5 배수 단위로 맞춰
// 0.37 같은 눈금이 나오지 않게 한다.
// 올림이 아니라 가장 가까운 배수를 고른다. 올림으로 하면 9.5 같은 값에서
// 눈금이 0/5/10 세 개뿐인 지나치게 거친 축이 나온다.
function niceAxisTicks(maxAmount, targetCount = 4) {
  const safeMax = maxAmount > 0 ? maxAmount : 1;
  const rawStep = safeMax / targetCount;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalized = rawStep / magnitude;
  const niceStep = (normalized < 1.5 ? 1 : normalized < 3 ? 2 : normalized < 7 ? 5 : 10) * magnitude;
  const ticks = [];
  // 부동소수점 누적 오차를 피하려고 인덱스로 곱해 값을 만든다.
  for (let index = 0; index * niceStep < safeMax + niceStep; index += 1) {
    ticks.push(Number((index * niceStep).toPrecision(12)));
  }
  return ticks;
}

// 축 눈금 레이블은 값 레이블보다 짧아야 읽기 편하다.
// 정수는 정수로, 소수는 불필요한 0을 뗀 형태로 보여준다.
function formatAxisTick(value) {
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(2)));
}

// 가로 막대 차트: 축 눈금과 그리드, 막대, 값 레이블, 호버 툴팁을 그린다.
function renderLegacyBarChartSVG(items, { valueFormatter = (v) => v.toFixed(2), animated = false } = {}) {
  const viewWidth = 760;
  const labelWidth = 260;
  const valueAreaWidth = 62;
  const barAreaWidth = viewWidth - labelWidth - valueAreaWidth;
  const rowHeight = 34;
  const barHeight = 16;
  const topPadding = 8;
  const axisHeight = 24;
  const plotHeight = items.length * rowHeight + topPadding * 2;
  const height = plotHeight + axisHeight;
  const gradientPrefix = `barGradient${(chartIdSequence += 1)}`;

  const maxAmount = Math.max(...items.map((item) => item.amount), 0);
  const ticks = niceAxisTicks(maxAmount);
  // 마지막 눈금을 축 최대값으로 삼아 막대가 그리드를 넘어가지 않게 한다.
  const axisMax = ticks[ticks.length - 1] || 1;
  const scaleX = (amount) => (amount / axisMax) * barAreaWidth;

  const gradients = items
    .map((item, index) => {
      const color = CHART_COLORS[index % CHART_COLORS.length];
      return `
        <linearGradient id="${gradientPrefix}-${index}" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="${color}" stop-opacity="0.72" />
          <stop offset="1" stop-color="${color}" stop-opacity="1" />
        </linearGradient>`;
    })
    .join("");

  const grid = ticks
    .map((tick) => {
      const x = labelWidth + scaleX(tick);
      const isBase = tick === 0;
      return `
        <line x1="${x}" y1="0" x2="${x}" y2="${plotHeight}" class="${isBase ? "bar-chart-axis-line" : "bar-chart-grid-line"}" />
        <text x="${x}" y="${plotHeight + 15}" text-anchor="middle" class="bar-chart-tick-label">${escapeHtml(formatAxisTick(tick))}</text>`;
    })
    .join("");

  const rows = items
    .map((item, index) => {
      const y = topPadding + index * rowHeight + rowHeight / 2;
      const barWidth = Math.max(scaleX(item.amount), item.amount > 0 ? 2 : 0);
      const label = truncateChartLabel(item.label, 30);
      const formatted = valueFormatter(item.amount);
      return `
        <g data-bar-row data-row-label="${escapeHtml(item.label)}" data-row-value="${escapeHtml(formatted)}">
          <rect class="bar-chart-row-hit" x="0" y="${y - rowHeight / 2}" width="${viewWidth}" height="${rowHeight}" />
          <text x="0" y="${y}" dominant-baseline="middle" class="bar-chart-axis-label"><title>${escapeHtml(item.label)}</title>${escapeHtml(label)}</text>
          <rect data-bar-rect data-bar-width="${barWidth}" x="${labelWidth}" y="${y - barHeight / 2}" width="${barWidth}" height="${barHeight}" rx="5" fill="url(#${gradientPrefix}-${index})" />
          <text data-bar-value data-bar-amount="${item.amount}" x="${labelWidth + barWidth + 8}" y="${y}" dominant-baseline="middle" class="bar-chart-value-label">${escapeHtml(formatted)}</text>
        </g>`;
    })
    .join("");

  const wrap = document.createElement("div");
  wrap.className = "bar-chart-wrap";
  wrap.innerHTML = `
    <svg class="bar-chart-svg" viewBox="0 0 ${viewWidth} ${height}" role="img" aria-label="항목별 공수 막대 차트">
      <defs>${gradients}</defs>
      ${grid}
      ${rows}
    </svg>
    <div class="chart-tooltip" role="tooltip" aria-hidden="true"></div>
  `;

  attachBarChartTooltip(wrap);
  if (animated) animateBarChart(wrap, { labelWidth, valueFormatter });
  return wrap;
}

// 막대 행에 마우스를 올리면 해당 행을 강조하고 전체 레이블과 값을 툴팁으로 띄운다.
// 왼쪽 레이블은 30자에서 잘리므로 툴팁이 원문을 확인하는 통로가 된다.
function attachBarChartTooltip(wrap) {
  const tooltip = wrap.querySelector(".chart-tooltip");
  const rows = Array.from(wrap.querySelectorAll("[data-bar-row]"));
  if (!tooltip || !rows.length) return;

  const hide = () => {
    wrap.classList.remove("has-hover");
    rows.forEach((row) => row.classList.remove("is-active"));
    tooltip.classList.remove("is-visible");
    tooltip.setAttribute("aria-hidden", "true");
  };

  rows.forEach((row) => {
    const show = (event) => {
      wrap.classList.add("has-hover");
      rows.forEach((other) => other.classList.toggle("is-active", other === row));
      tooltip.innerHTML = `<strong>${escapeHtml(row.dataset.rowValue || "")}</strong><span>${escapeHtml(row.dataset.rowLabel || "")}</span>`;
      tooltip.classList.add("is-visible");
      tooltip.setAttribute("aria-hidden", "false");

      // 툴팁이 차트 밖으로 새지 않도록 래퍼 기준 좌표로 클램프한다.
      const bounds = wrap.getBoundingClientRect();
      const tipWidth = tooltip.offsetWidth;
      const rawLeft = event.clientX - bounds.left + 12;
      const maxLeft = Math.max(0, bounds.width - tipWidth - 4);
      tooltip.style.left = `${Math.min(Math.max(0, rawLeft), maxLeft)}px`;
      tooltip.style.top = `${event.clientY - bounds.top + 14}px`;
    };
    row.addEventListener("mouseenter", show);
    row.addEventListener("mousemove", show);
    row.addEventListener("mouseleave", hide);
  });

  wrap.addEventListener("mouseleave", hide);
}

// 막대 등장 연출: 축에서부터 막대가 뻗어 나오고 값 레이블이 끝을 따라간다.
// 값 레이블은 막대 끝에 붙어 있어야 하므로 x 좌표도 함께 갱신한다.
function animateBarChart(container, { labelWidth, valueFormatter }) {
  const bars = Array.from(container.querySelectorAll("[data-bar-rect]"));
  const values = Array.from(container.querySelectorAll("[data-bar-value]"));

  bars.forEach((bar) => bar.setAttribute("width", "0"));
  values.forEach((value) => {
    value.setAttribute("x", String(labelWidth + 8));
    value.style.opacity = "0";
  });

  onFirstVisible(container, () => {
    bars.forEach((bar, index) => {
      const targetWidth = Number(bar.dataset.barWidth) || 0;
      const valueLabel = values[index];
      const amount = Number(valueLabel?.dataset.barAmount);
      const proxy = { width: 0 };
      animate(proxy, {
        width: targetWidth,
        duration: 780,
        delay: index * 60,
        ease: "outCubic",
        onUpdate: () => {
          bar.setAttribute("width", String(proxy.width));
          if (valueLabel) valueLabel.setAttribute("x", String(labelWidth + proxy.width + 8));
        },
        // 부동소수점 오차 없이 정확한 최종 길이로 마무리한다.
        onComplete: () => {
          bar.setAttribute("width", String(targetWidth));
          if (valueLabel) valueLabel.setAttribute("x", String(labelWidth + targetWidth + 8));
        },
      });
      if (valueLabel && Number.isFinite(amount)) {
        countUp(valueLabel, amount, { duration: 780, formatter: valueFormatter });
      }
    });

    if (values.length) {
      animate(values, {
        opacity: [0, 1],
        duration: 320,
        delay: stagger(60),
        ease: "outQuad",
      });
    }
  });
}

// 스탯 카드 등장 연출: 카드가 살짝 떠오르고 숫자형 값은 카운트업한다.
// 사람 이름처럼 숫자가 아닌 값은 카운트업 대상에서 제외한다.
function animateStatCards(statCards) {
  const cards = Array.from(statCards.children);
  cards.forEach((card) => {
    card.style.opacity = "0";
    card.style.transform = "translateY(8px)";
  });

  onFirstVisible(statCards, () => {
    animate(cards, {
      opacity: [0, 1],
      translateY: [8, 0],
      duration: 460,
      delay: stagger(80),
      ease: "outQuad",
    });

    cards.forEach((card) => {
      const strong = card.querySelector("strong");
      const amount = Number(strong?.dataset.statAmount);
      if (!strong || !Number.isFinite(amount)) return;
      const suffix = strong.dataset.statSuffix || "";
      const decimals = Number(strong.dataset.statDecimals) || 0;
      countUp(strong, amount, {
        duration: 900,
        formatter: (value) => `${value.toFixed(decimals)}${suffix}`,
      });
    });
  });
}

function statCardMarkup({ label, value, amount, suffix = "", decimals = 0 }) {
  const numeric = Number.isFinite(Number(amount))
    ? ` data-stat-amount="${Number(amount)}" data-stat-suffix="${escapeHtml(suffix)}" data-stat-decimals="${decimals}"`
    : "";
  return `<strong${numeric}>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span>`;
}

export function renderEffortChart(chart, { complete = true, originalAnswer = null } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "effort-chart effort-answer";
  const animated = shouldAnimateChart(chart);
  const value = amount => escapeHtml(formatChartValue(amount, {decimals: 2, unit: "", complete}));
  const period = chart.periodFrom && chart.periodTo ? `${chart.periodFrom}–${chart.periodTo}` : chart.periodLabel || `${chart.year}년 ${chart.month}월`;
  const daily = chart.dailyEffort;
  const dailyFallback = Array.isArray(daily) && daily.length
    ? `<details class="bklit-data"><summary>일별 수치 보기</summary><div class="bklit-sparkline-table"><table><thead><tr><th>고객사전달일</th><th>공수</th></tr></thead><tbody>${daily.map(point => `<tr><td>${escapeHtml(point.date)}</td><td>${value(point.amount)}</td></tr>`).join("")}</tbody></table></div></details>` : "";
  const sparkline = dailyFallback ? chartHost({
    kind: "sparkline", title: "일별 공수", complete, sparklineVariant: "bar", dateBasis: "고객사전달일",
    points: daily.map(point => ({date: point.date, count: point.amount})), decimals: 2, unit: " 공수", showTotal: false, animated,
  }, dailyFallback) : `<p class="bklit-note">${daily === null ? "일별 추이는 1년 이하의 조회 기간에 표시됩니다." : daily === undefined ? "일별 공수 데이터가 없는 저장된 답변입니다. 다시 조회하면 추이를 볼 수 있습니다." : "표시할 일별 공수 데이터가 없습니다."}</p>`;
  wrap.innerHTML = `
    <header class="effort-answer-header"><h3>${escapeHtml(chart.person)} 공수</h3><p>${escapeHtml(period)} · 고객사전달일 기준</p></header>
    <div class="effort-answer-total"><div><span class="effort-answer-label">총 공수</span><strong>${value(chart.totalEffort)}</strong></div><p>반영 이슈 <b>${escapeHtml(formatChartValue(chart.issueCount, {complete}))}</b></p></div>
    ${!complete ? '<p class="bklit-note">확인한 범위의 공수입니다. 전체 합계는 미확정입니다.</p>' : ""}
    <div class="effort-answer-sparkline">${sparkline}</div>
    <div class="effort-answer-types" aria-label="유형별 공수">${(chart.byType || []).map(item => `<div><span>${escapeHtml(item.label)}</span><strong>${value(item.amount)}</strong></div>`).join("")}</div>
    <p class="effort-answer-basis">Mantis 고객사전달일 · 공수 커스텀 필드 합산 · 전달일 없는 이슈 제외</p>`;

  if (chart.topIssues?.length) {
    const section = document.createElement("details");
    section.className = "summary-section effort-answer-issues";
    section.innerHTML = `<summary>공수가 큰 이슈 보기</summary>`;
    const items = chart.topIssues.map(({ id, summary, amount }) => ({
      label: id ? `#${id} ${summary}` : summary,
      amount,
    }));
    section.appendChild(renderBarChartSVG(items, { animated: false, complete }));
    wrap.appendChild(section);
  }

  if (originalAnswer) {
    const details = document.createElement("details");
    details.className = "effort-answer-original";
    const summary = document.createElement("summary");
    summary.textContent = "답변 원문 보기";
    details.appendChild(summary);
    details.appendChild(originalAnswer);
    wrap.appendChild(details);
  }
  return wrap;
}

export function renderEffortRankingChart(ranking, { complete = true } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "effort-chart";
  const basis = document.createElement("p");
  basis.textContent = "고객사전달일 기준 · 전달일 없는 이슈 제외";
  wrap.appendChild(basis);
  const animated = shouldAnimateChart(ranking);

  const statCards = document.createElement("div");
  statCards.className = "summary-stat-grid effort-chart-stats";
  [
    { label: "기간", value: ranking.periodLabel },
    { label: "공수 기록 이슈", value: `${ranking.issueCount}건`, amount: ranking.issueCount, suffix: "건" },
    { label: "참여 인원", value: `${ranking.peopleCount}명`, amount: ranking.peopleCount, suffix: "명" },
  ].forEach((stat) => {
    const card = document.createElement("div");
    card.className = "summary-stat-card";
    card.innerHTML = statCardMarkup(stat);
    statCards.appendChild(card);
  });
  wrap.appendChild(statCards);
  if (animated) animateStatCards(statCards);

  if (ranking.topPeople?.length) {
    const section = document.createElement("div");
    section.className = "summary-section";
    section.innerHTML = `<h3>공수 순위</h3>`;
    section.appendChild(renderBarChartSVG(ranking.topPeople, { animated, complete }));
    wrap.appendChild(section);
  }

  return wrap;
}

// 기존 SVG는 모듈 실패 시에도 원본 수치를 볼 수 있는 대체 표시로 남긴다.
export function renderDonutChart(items, options = {}) {
  const fallback = renderLegacyDonutChart(items, {...options, animated:false});
  const wrap = document.createElement("div");
  wrap.className = "bklit-report-chart";
  wrap.innerHTML = chartHost(metricChartSpec(items, {kind:"pie", title:options.centerLabel || "유형별 공수", decimals:2, unit:"", complete:options.complete !== false, animated:options.animated}), (options.complete === false ? `<p>일부 조회 · 확인한 범위의 공수입니다.</p><dl>${items.map(item=>`<dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(Number(item.amount).toFixed(2))} 이상</dd>`).join("")}</dl>` : fallback.outerHTML));
  return wrap;
}
export function renderBarChartSVG(items, options = {}) {
  const fallback = renderLegacyBarChartSVG(items, {...options, animated:false});
  const wrap = document.createElement("div");
  wrap.className = "bklit-report-chart";
  wrap.innerHTML = chartHost(metricChartSpec(items, {title:"공수", decimals:2, unit:"", complete:options.complete !== false, animated:options.animated}), (options.complete === false ? `<p>일부 조회 · 확인한 범위의 공수입니다.</p><dl>${items.map(item=>`<dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(Number(item.amount).toFixed(2))} 이상</dd>`).join("")}</dl>` : fallback.outerHTML));
  return wrap;
}
