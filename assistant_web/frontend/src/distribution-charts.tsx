import { useEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { BarChart } from '../vendor/bklit/charts/bar-chart';
import { Bar } from '../vendor/bklit/charts/bar';
import { useChartStable } from '../vendor/bklit/charts/chart-context';
import { ChartTooltip } from '../vendor/bklit/charts/tooltip/chart-tooltip';
import { PieChart } from '../vendor/bklit/charts/pie-chart';
import { PieSlice } from '../vendor/bklit/charts/pie-slice';
import { PieCenter } from '../vendor/bklit/charts/pie-center';
import { HeatmapChart, useHeatmap } from '../vendor/bklit/charts/heatmap';
import type { Spec } from './charts';

const count = (value: number, complete: boolean) => `${value.toLocaleString('ko-KR')}건${complete ? '' : ' 이상'}`;
const total = (row: Record<string, unknown>, spec: Spec) => (spec.series || []).reduce((n, s) => n + Number(row[s.key] || 0), 0);
const ageColors = ['var(--chart-1)', 'var(--distribution-age-recent)', 'var(--distribution-age-long)', 'var(--chart-3)'];

function CustomerLabels({ spec }: { spec: Spec }) {
  const group = useRef<SVGGElement>(null);
  useEffect(() => {
    const svg = group.current?.ownerSVGElement;
    svg?.removeAttribute('aria-hidden');
    svg?.setAttribute('role', 'group');
    svg?.setAttribute('aria-label', '고객사별 미종료 건수와 목록 필터');
  }, []);
  const { data, barScale, barXAccessor, bandWidth, innerWidth } = useChartStable();
  if (!barScale || !barXAccessor || bandWidth === undefined) return null;
  return <g ref={group}>{data.map(row => {
    const y = barScale(barXAccessor(row)) || 0;
    const label = String(row.fullLabel || row.name);
    return <foreignObject key={String(row.id)} x={0} y={y - 32} width={innerWidth} height={bandWidth + 34}>
      <button type="button" className="bklit-customer-select" aria-pressed={spec.selected === String(row.id)}
        style={{ height: bandWidth + 34 }} {...(spec.action ? { [spec.action]: String(row.id) } : {})}
        aria-label={`${label} 미종료 ${count(total(row, spec), spec.complete)}, ${(spec.series || []).map(s => `${s.label} ${count(Number(row[s.key] || 0), spec.complete)}`).join(', ')}`}>
        <span className="bklit-customer-name" title={label}>{label}</span><strong>{count(total(row, spec), spec.complete)}</strong>
      </button>
    </foreignObject>;
  })}</g>;
}

export function CustomerRanking({ spec }: { spec: Spec }) {
  const rows = spec.rows || [], series = spec.series || [];
  const reduced = useReducedMotion();
  if (!rows.length) return <p className="bklit-empty">확인한 범위에 표시할 고객사가 없습니다.</p>;
  return <div className="bklit-customer-ranking">
    <div className="bklit-ranking-frame" style={{ height: rows.length * 66 + 26 }}>
      <BarChart data={rows} xDataKey="id" orientation="horizontal" stacked aspectRatio="auto" barGap={0.62}
        margin={{ top: 30, right: 6, bottom: 0, left: 6 }} animationDuration={reduced ? 0 : 400}>
        {series.map(s => <Bar key={s.key} dataKey={s.key} fill={s.color} lineCap={3} animate={!reduced}
          showValue valueLabelColor="var(--distribution-on-color)" valueLabelOrientation="horizontal" />)}
        <ChartTooltip className="bklit-hover-panel" showDatePill={false} showDots={false} content={({ point }) => <div className="bklit-tooltip">
          <strong>{String(point.fullLabel || point.name)}</strong>{series.map(s => <div key={s.key}><i style={{ background: s.color }} />{s.label}<b>{count(Number(point[s.key] || 0), spec.complete)}</b></div>)}
        </div>} />
        <CustomerLabels spec={spec} />
      </BarChart>
    </div>
    <div className="bklit-distribution-legend">{series.map(s => <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>)}</div>
  </div>;
}

export function AgeDonut({ spec }: { spec: Spec }) {
  const rows = spec.rows || [];
  const data = rows.map((row, index) => ({ label: String(row.name), value: Number(row.count || 0), color: ageColors[index % ageColors.length] }));
  const sum = data.reduce((n, row) => n + row.value, 0);
  const aging = rows.reduce((n, row) => n + (Number(row.minDays) >= 30 ? Number(row.count || 0) : 0), 0);
  const [hover, setHover] = useState<number | null>(null);
  const reduced = useReducedMotion();
  const active = hover ?? (spec.selected ? rows.findIndex(row => String(row.id) === spec.selected) : -1);
  const index = active < 0 ? null : active;
  return <div className="bklit-age-donut">
    {spec.complete && sum > 0 ? <div className="bklit-age-circle" role="img" aria-label={`등록일 확인 ${sum}건 중 30일 이상 ${aging}건, ${(aging / sum * 100).toFixed(1)}%`}>
      <PieChart data={data} size={240} innerRadius={78} padAngle={0.025} cornerRadius={3} hoveredIndex={index} onHoverChange={setHover}>
        {data.map((row, i) => <g key={String(rows[i].id)} {...(spec.action ? { [spec.action]: String(rows[i].id) } : {})} className="bklit-age-slice">
          <PieSlice index={i} animate={!reduced} hoverEffect={reduced ? 'none' : 'translate'} showGlow={false} />
        </g>)}
        <PieCenter>{({ value, label, isHovered }) => <div className="bklit-age-center"><strong>{isHovered ? `${value}건` : `${(aging / sum * 100).toFixed(1)}%`}</strong><span>{isHovered ? label : `30일 이상 · ${aging}건`}</span></div>}</PieCenter>
      </PieChart>
    </div> : <p className="bklit-note">{!spec.complete ? '일부 조회 결과여서 비율은 표시하지 않습니다.' : '등록일을 확인한 미종료 이슈가 없어 비율을 표시하지 않습니다.'}</p>}
    <div className="bklit-age-options">{data.map((row, i) => <button key={String(rows[i].id)} type="button"
      aria-pressed={spec.selected === String(rows[i].id)} {...(spec.action ? { [spec.action]: String(rows[i].id) } : {})}
      onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}>
      <i style={{ background: row.color }} /><span>{row.label}</span><strong>{count(row.value, spec.complete)}</strong>
      {spec.complete && sum > 0 && <small>{Math.round(row.value / sum * 100)}%</small>}
    </button>)}</div>
    {Boolean(spec.unknown) && <p className="bklit-note">비율은 등록일 확인 {count(sum, spec.complete)} 기준입니다.</p>}
  </div>;
}

function MemberCells({ spec, onSelect }: { spec: Spec; onSelect: (text: string) => void }) {
  const group = useRef<SVGGElement>(null);
  useEffect(() => {
    // 기본 heatmap SVG는 장식용이다. 이 레이어의 실제 버튼과 수치는 접근 가능하게 연다.
    const svg = group.current?.ownerSVGElement;
    svg?.removeAttribute('aria-hidden');
    svg?.setAttribute('role', 'group');
    svg?.setAttribute('aria-label', '팀원별 지연·장기·그 외 미종료 건수');
  }, []);
  const { width, margin, yScale, binHeight } = useHeatmap();
  const rows = spec.rows || [], series = spec.series || [];
  const cellWidth = Math.max(0, width - margin.left - margin.right) / Math.max(1, series.length);
  const max = Math.max(1, ...rows.flatMap(row => series.map(s => Number(row[s.key] || 0))));
  return <g ref={group}>
    {series.map((s, i) => <text key={s.key} x={cellWidth * (i + .5)} y={-12} textAnchor="middle" className="bklit-heat-label">{s.key === 'aging' ? '장기*' : s.label}</text>)}
    <text x={cellWidth * series.length + 10} y={-12} className="bklit-heat-label">합계</text>
    {rows.map((row, r) => <g key={String(row.id)}>
      <text x={-10} y={yScale(r) + binHeight / 2} textAnchor="end" dominantBaseline="middle" className="bklit-heat-name">{String(row.name)}</text>
      {series.map((s, c) => {
        const value = Number(row[s.key] || 0);
        const text = `${String(row.name)} · ${s.label} ${count(value, spec.complete)} / 미종료 ${count(total(row, spec), spec.complete)}`;
        return <foreignObject key={s.key} x={c * cellWidth} y={yScale(r)} width={Math.max(0, cellWidth - 5)} height={binHeight - 5}>
          <button type="button" className="bklit-member-cell" aria-label={text} onClick={() => onSelect(text)}
            style={{ background: value ? `color-mix(in srgb, ${s.color} ${15 + value / max * 60}%, var(--surface))` : 'var(--surface-alt)' }}>
            {value ? value.toLocaleString('ko-KR') : spec.complete ? '–' : '0+'}
          </button>
        </foreignObject>;
      })}
      <text x={cellWidth * series.length + 10} y={yScale(r) + binHeight / 2} dominantBaseline="middle" className="bklit-heat-total">{total(row, spec).toLocaleString('ko-KR')}{spec.complete ? '' : '+'}</text>
    </g>)}
  </g>;
}

export function MemberHeatmap({ spec }: { spec: Spec }) {
  const rows = spec.rows || [], series = spec.series || [];
  const [detail, setDetail] = useState('셀을 선택하면 상태별 건수를 확인할 수 있습니다.');
  const data = series.map((s, c) => ({ bin: c, bins: rows.map((row, r) => ({ bin: r, count: Number(row[s.key] || 0), date: new Date(2000, 0, 1) })) }));
  if (!rows.length) return <p className="bklit-empty">확인한 팀원이 없습니다.</p>;
  return <div className="bklit-member-heatmap">
    <div className="bklit-member-frame" style={{ height: rows.length * 33 + 32 }}>
      <HeatmapChart data={data} layout="fill" binSize={33} margin={{ top: 32, left: 68, right: 43, bottom: 0 }} animate={false}>
        <MemberCells spec={spec} onSelect={setDetail} />
      </HeatmapChart>
    </div>
    <p className="bklit-note" aria-live="polite">{detail}</p>
    <p className="bklit-note">* 장기: 지연 제외 · 색이 진할수록 건수가 많습니다.{!spec.complete && ' 수치는 확인한 범위의 하한입니다.'}</p>
  </div>;
}
