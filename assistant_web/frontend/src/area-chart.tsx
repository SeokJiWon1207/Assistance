// bklit Area Chart(https://bklit.com/docs/components/area-chart)의 Area와 같은 속성을 가진 영역 채우기.
// 원본 vendor에는 AreaChart가 없어 LineChart 안에서 같은 축척으로 그라디언트 영역만 그리는 래퍼를 둔다.
// LineChart는 Line 외의 자식을 Line보다 먼저 받은 순서대로 그리므로 <AreaFill>을 <Line> 앞에 둔다.
import { area as d3Area, curveMonotoneX, curveStepAfter, curveLinear, type CurveFactory } from 'd3-shape';
import { useId, useMemo } from 'react';
import { useReducedMotion } from 'motion/react';
import { LineChart, Line } from '../vendor/bklit/charts/line-chart';
import { Grid } from '../vendor/bklit/charts/grid';
import { XAxis } from '../vendor/bklit/charts/x-axis';
import { YAxis } from '../vendor/bklit/charts/y-axis';
import { ChartTooltip } from '../vendor/bklit/charts/tooltip/chart-tooltip';
import { useChartStable, useYScale } from '../vendor/bklit/charts/chart-context';
import type { Spec } from './charts';

type AreaFillProps = {
  dataKey: string;
  fill?: string;
  fillOpacity?: number;
  gradientToOpacity?: number;
  curve?: CurveFactory;
  yAxisId?: string | number;
};

export function AreaFill({ dataKey, fill = 'var(--chart-1)', fillOpacity = 0.35, gradientToOpacity = 0, curve = curveMonotoneX, yAxisId }: AreaFillProps) {
  const { renderData, xScale, xAccessor, innerHeight } = useChartStable();
  const yScale = useYScale(yAxisId);
  const gradientId = `bklit-area-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const path = useMemo(() => d3Area<Record<string, unknown>>()
    .x((row) => xScale(xAccessor(row)) ?? 0)
    .y0(innerHeight)
    .y1((row) => {
      const value = Number(row[dataKey]);
      return Number.isFinite(value) ? (yScale(value) ?? innerHeight) : innerHeight;
    })
    .curve(curve)(renderData) || '', [renderData, xScale, xAccessor, yScale, innerHeight, dataKey, curve]);
  return <g className="bklit-area-fill" aria-hidden="true">
    <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
      {/* SVG 속성으로는 CSS 변수(var(--chart-1))가 풀리지 않으므로 style로 지정한다. */}
      <stop offset="0%" style={{ stopColor: fill, stopOpacity: fillOpacity }} />
      <stop offset="100%" style={{ stopColor: fill, stopOpacity: gradientToOpacity }} />
    </linearGradient></defs>
    <path d={path} fill={`url(#${gradientId})`} />
  </g>;
}

type Point = { date: string; count: number };
const toDate = (day: string) => new Date(`${day}T12:00:00`);
const dayLabel = (date: Date) => `${date.getMonth() + 1}/${date.getDate()}`;
const countLabel = (value: unknown, complete: boolean) => `${Number(value || 0).toLocaleString('ko-KR')}건${complete ? '' : ' 이상'}`;

// 기간 등록 추이: 그라디언트 영역 + 선 + 십자선 툴팁. 수치 표는 HTML 대체 화면이 유지한다.
// cumulative면 하루 건수가 적어도 흐름이 보이도록 기간 시작부터의 누적을 계단형으로 그린다.
export function AreaTrend({ spec }: { spec: Spec }) {
  const points: Point[] = spec.points || [];
  const rows = useMemo(() => {
    let total = 0;
    return points.map((point) => {
      total += Number(point.count) || 0;
      return { ...point, total, date: toDate(point.date) };
    });
  }, [points]);
  const reduced = useReducedMotion();
  if (!points.length) return <p className="bklit-empty">{spec.emptyMessage || '등록 추이를 확인하지 못했어요.'}</p>;
  const key = spec.cumulative ? 'total' : 'count';
  const curve = spec.cumulative ? curveStepAfter : curveMonotoneX;
  const max = Math.max(0, ...rows.map((row) => Number(row[key]) || 0));
  const color = spec.series?.[0]?.color || 'var(--chart-1)';
  return <div className="bklit-area-trend">
    <LineChart data={rows} animationDuration={reduced || spec.animated === false ? 0 : 700}
      style={{ height: spec.height || 220, aspectRatio: 'auto' }} margin={{ top: 12, left: 32, right: 16, bottom: 32 }}>
      <Grid horizontal numTicksRows={max <= 4 ? Math.max(1, max) : 4} />
      <AreaFill dataKey={key} fill={color} fillOpacity={0.4} curve={curve} />
      <Line dataKey={key} stroke={color} strokeWidth={2} curve={curve} animate={!reduced} fadeEdges={false} />
      <YAxis formatValue={(value) => (Number.isInteger(value) ? String(value) : '')} />
      <XAxis numTicks={5} labels={rows.map((row) => dayLabel(row.date))} />
      <ChartTooltip className="bklit-hover-panel" showDatePill={false} content={({ point }) => <div className="bklit-tooltip">
        <strong>{dayLabel(point.date as Date)}</strong>
        <div><i style={{ background: color }} />{spec.series?.[0]?.label || '신규 등록'}<b>{countLabel(point.count, spec.complete)}</b></div>
        {spec.cumulative && <div>기간 누적<b>{countLabel(point.total, spec.complete)}</b></div>}
      </div>} />
    </LineChart>
  </div>;
}

// 이번 기간 vs 직전 기간: 기간 시작부터 같은 날짜 순번끼리 누적을 겹쳐 그린다(Monarch 지출 비교 차트 구성).
// 이번 기간은 그라디언트 영역, 직전 기간은 흐린 선. x축 날짜는 이번 기간 기준이다.
export function CompareArea({ spec }: { spec: Spec }) {
  const current: Point[] = spec.points || [];
  const previous: Point[] = spec.previousPoints || [];
  const rows = useMemo(() => {
    let total = 0;
    let previousTotal = 0;
    return current.map((point, index) => {
      total += Number(point.count) || 0;
      const before = previous[index];
      if (before) previousTotal += Number(before.count) || 0;
      return { date: toDate(point.date), day: index + 1, count: Number(point.count) || 0, total,
        previousDate: before?.date || '', previousCount: Number(before?.count) || 0, previousTotal };
    });
  }, [current, previous]);
  const reduced = useReducedMotion();
  if (!current.length) return <p className="bklit-empty">{spec.emptyMessage || '등록 추이를 확인하지 못했어요.'}</p>;
  const color = spec.series?.[0]?.color || 'var(--chart-1)';
  const previousColor = spec.series?.[1]?.color || 'var(--muted)';
  const snow = spec.lineStyle === 'snow';
  return <div className={`bklit-compare-area${snow ? ' is-snow' : ''}`}>
    <LineChart data={rows} animationDuration={reduced || spec.animated === false ? 0 : 700}
      style={{ height: spec.height || 220, aspectRatio: 'auto' }} margin={{ top: 12, left: 36, right: 16, bottom: 32 }}>
      <Grid horizontal numTicksRows={4} />
      {/* snow: 채움 없는 부드러운 곡선, 직전 기간은 처음부터 점선(Snow 대시보드의 이번 주·지난주 비교 모양). */}
      {!snow && <AreaFill dataKey="total" fill={color} fillOpacity={0.32} curve={curveStepAfter} />}
      <Line dataKey="previousTotal" stroke={previousColor} strokeWidth={snow ? 2.5 : 1.75} curve={snow ? curveMonotoneX : curveStepAfter} animate={!reduced} fadeEdges={false} showHighlight={false}
        {...(snow ? { dashFromIndex: 0, dashArray: '6,6' } : {})} />
      <Line dataKey="total" stroke={color} strokeWidth={snow ? 2.5 : 2} curve={snow ? curveMonotoneX : curveStepAfter} animate={!reduced} fadeEdges={false} />
      <YAxis formatValue={(value) => (Number.isInteger(value) ? String(value) : '')} />
      <XAxis numTicks={5} labels={rows.map((row) => dayLabel(row.date))} />
      <ChartTooltip className="bklit-hover-panel" showDatePill={false} content={({ point }) => <div className="bklit-tooltip">
        <strong>{Number(point.day)}일째 · {dayLabel(point.date as Date)}</strong>
        <div><i style={{ background: color }} />{spec.series?.[0]?.label || '이번 기간'}<b>{countLabel(point.total, spec.complete)}</b></div>
        <div><i style={{ background: previousColor }} />{spec.series?.[1]?.label || '직전 기간'}<b>{countLabel(point.previousTotal, spec.complete)}</b></div>
      </div>} />
    </LineChart>
  </div>;
}

// 등록일과 고객사전달일 각각의 일별 건수. 수정일을 완료일로 추정하지 않는다.
export function ActivityTrend({ spec }: { spec: Spec }) {
  const reduced = useReducedMotion();
  const delivered = new Map((spec.secondaryPoints || []).map(p => [p.date, p.count]));
  const rows = (spec.points || []).map(p => ({ date: toDate(p.date), intake: p.count, delivered: delivered.get(p.date) || 0 }));
  if (!rows.length) return <p className="bklit-empty">선택 기간의 기록을 확인하지 못했어요.</p>;
  return <div className="bklit-activity"><LineChart data={rows} animationDuration={reduced ? 0 : 600}
    style={{height:spec.height || 320,aspectRatio:'auto'}} margin={{top:12,left:36,right:18,bottom:32}}>
    <Grid horizontal numTicksRows={4}/>
    <Line dataKey="intake" curve={curveLinear} stroke="var(--chart-1)" strokeWidth={2.5} animate={!reduced} fadeEdges={false}/>
    <Line dataKey="delivered" curve={curveLinear} stroke="var(--analytics-secondary)" strokeWidth={2.5} animate={!reduced} fadeEdges={false}/>
    <YAxis formatValue={value => Number.isInteger(value) ? String(value) : ''}/>
    <XAxis numTicks={5} labels={rows.map(r=>dayLabel(r.date))}/>
    <ChartTooltip className="bklit-hover-panel" showDatePill={false} content={({point})=><div className="bklit-tooltip">
      <strong>{dayLabel(point.date as Date)}</strong>
      <div><i style={{background:'var(--chart-1)'}}/>신규 등록<b>{countLabel(point.intake,spec.complete)}</b></div>
      <div><i style={{background:'var(--analytics-secondary)'}}/>고객사 전달<b>{countLabel(point.delivered,spec.complete)}</b></div>
    </div>}/>
  </LineChart></div>;
}
