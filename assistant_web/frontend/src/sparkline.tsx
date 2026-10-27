import React from 'react';
import { useReducedMotion } from 'motion/react';
import { BarSparkline, LineSparkline, useSparklineHover } from '../vendor/bklit/charts/sparkline';
import { formatChartValue } from '../../backend/static/modules/chart_models.js';
import type { Spec } from './charts';

type Point = { date: string; count: number };

export function Sparkline({ spec }: { spec: Spec }) {
  const points = spec.points || [];
  const hover = useSparklineHover<Point>();
  const reduced = useReducedMotion();
  const isEffort = spec.dateBasis === '고객사전달일';
  const basis = spec.dateBasis || '등록일';
  const showSummary = !spec.compact && spec.showTotal !== false;
  if (!points.length) return <p className="bklit-empty">{isEffort ? '일별 공수' : '등록 추이'} 데이터가 없습니다.</p>;
  const shown = hover.datum;
  const total = points.reduce((sum, point) => sum + point.count, 0);
  const value = shown ? shown.count : total;
  const formatted = formatChartValue(value, spec);
  const label = <div className="bklit-sparkline-label">
    <span>{shown ? shown.date : showSummary ? `${points[0].date.slice(5)}–${points.at(-1)!.date.slice(5)} · ${basis} 기준` : `${isEffort ? '일별 공수' : '일별 등록 추이'} · ${basis} 기준`}</span>
    {(shown || showSummary) && <strong>{formatted}</strong>}
  </div>;
  const props = {
    data: points, dataKey: 'count' as const, height: spec.compact ? 48 : showSummary ? 54 : 62,
    yDomain: [0, undefined] as [number, undefined], color: 'var(--chart-1)',
    animate: !reduced && spec.animated !== false, animationDuration: 450,
    ...hover.sparklineProps,
    'aria-label': `${spec.title} · ${basis} 기준 · ${points[0].date}부터 ${points.at(-1)!.date}까지 · ${formatChartValue(total, spec)}`,
  };
  return <div className={`bklit-sparkline${spec.compact ? ' is-compact' : ''}`}>
    {!spec.compact && label}
    {spec.sparklineVariant === 'bar'
      ? <BarSparkline {...props} barGap={2} barRadius={2} />
      : <LineSparkline {...props} curve="linear" endDot={points.length === 1} />}
    {spec.compact && label}
    {!showSummary && <div className="bklit-sparkline-dates"><span>{points[0].date.slice(5).replace('-', '/')}</span><span>{points.at(-1)!.date.slice(5).replace('-', '/')}</span></div>}
    {!spec.complete && <p className="bklit-note">확인한 범위 기준 · 실제 값은 더 클 수 있습니다.</p>}
    <details className="bklit-data"><summary>일별 수치 보기</summary><div className="bklit-sparkline-table"><table><thead><tr><th>{basis}</th><th>{isEffort ? '공수' : '신규 이슈'}</th></tr></thead><tbody>{points.map(point => <tr key={point.date}><td>{point.date}</td><td>{formatChartValue(point.count, spec)}</td></tr>)}</tbody></table></div></details>
  </div>;
}
