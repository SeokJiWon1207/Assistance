// 월별 집계는 서버 원본 그대로, 표시만 Bklit 원본 Line으로 제공한다.
import { chartHost } from "./bklit_charts.js?v=20261026-1";

function escapeText(value) {
  const holder = document.createElement("span");
  holder.textContent = String(value ?? "");
  return holder.innerHTML;
}

export function renderEffortTrendChart(trend, { complete = true } = {}) {
  const wrap = document.createElement("section");
  wrap.className = "effort-trend";
  if (!trend?.periods?.length || !trend?.series?.length) {
    wrap.innerHTML = '<p class="effort-trend-empty">표시할 추이 데이터가 없습니다.</p>';
    return wrap;
  }
  // 번들 실패 때도 건수와 공수를 모두 확인할 수 있는 실제 수치 표.
  const fallback = `<div class="bklit-table-scroll"><table class="bklit-native-table"><caption>월별 원본 수치 · 등록건수 / ${trend.effortDateBasis === 'delivered_at' ? '전달 공수' : '등록일 기준 공수'}</caption><thead><tr><th>기간</th>${trend.series.map(item=>`<th>${escapeText(item.label)}</th>`).join("")}</tr></thead><tbody>${trend.periods.map((period,index)=>`<tr><th>${escapeText(period)}</th>${trend.series.map(item=>`<td>${escapeText(item.counts?.[index] ?? "미확인")} / ${escapeText(item.efforts?.[index] ?? "미확인")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  wrap.innerHTML = `<header class="effort-trend-header"><span class="effort-trend-period">${escapeText(trend.scopeLabel || "전사")} · ${escapeText(trend.periodLabel || "")}</span></header>${chartHost({kind:"monthly",title:"월별 이슈 유형 추이",complete,trend},fallback)}`;
  return wrap;
}
