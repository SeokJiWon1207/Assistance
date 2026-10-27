import { MetricPie, MonthlyTrend } from './report-charts';
import { formatChartValue } from '../../backend/static/modules/chart_models.js';
import { curveLinear } from 'd3-shape';
import React, { Component, useEffect, useMemo, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { MotionConfig, useReducedMotion } from 'motion/react';
import { BarChart } from '../vendor/bklit/charts/bar-chart';
import { Bar } from '../vendor/bklit/charts/bar';
import { BarXAxis } from '../vendor/bklit/charts/bar-x-axis';
import { BarYAxis } from '../vendor/bklit/charts/bar-y-axis';
import { ChartLegendHoverProvider } from '../vendor/bklit/charts/chart-legend-hover';
import { useChartStable } from '../vendor/bklit/charts/chart-context';
import { ChartTooltip } from '../vendor/bklit/charts/tooltip/chart-tooltip';
import { Legend, LegendItem, LegendLabel, LegendMarker, LegendValue } from '../vendor/bklit/charts/legend';
import { useLegend, useLegendItem } from '../vendor/bklit/charts/legend/legend-context';
import { Sparkline } from './sparkline';
import { LineChart, Line } from '../vendor/bklit/charts/line-chart';
import { Grid } from '../vendor/bklit/charts/grid';
import { XAxis } from '../vendor/bklit/charts/x-axis';
import { YAxis } from '../vendor/bklit/charts/y-axis';
import { ChartBrush, type ChartBrushSelection } from '../vendor/bklit/charts/chart-brush';
import { PieChart } from '../vendor/bklit/charts/pie-chart';
import { PieSlice } from '../vendor/bklit/charts/pie-slice';
import { PieCenter } from '../vendor/bklit/charts/pie-center';
import { HeatmapChart, HeatmapCells, HeatmapTooltip } from '../vendor/bklit/charts/heatmap';
import { dueCalendar, selectTrend, cohortSlices } from '../../backend/static/modules/chart_models.js';

type Row = Record<string, unknown>;
type Point = { date: string; count: number };
type Series = { key: string; label: string; color: string };
export type Spec = {
  kind: 'bar' | 'sparkline' | 'trend' | 'cohort' | 'calendar' | 'pie' | 'monthly'; title: string; complete: boolean;
  rows?: Row[]; series?: Series[]; orientation?: 'horizontal' | 'vertical';
  action?: string; selected?: string; points?: Point[]; summary?: Record<string, number>;
  decimals?: number; unit?: string; animated?: boolean;
  sparklineVariant?: 'line' | 'bar'; compact?: boolean; dateBasis?: string; showTotal?: boolean;
  trend?: {periods: string[]; series: Array<{label:string; counts: Array<number|null>; efforts: Array<number|null>}>; effortDateBasis?: string; untypedCount?: number};
  issues?: Array<{ id: string | number; dueDate: string; title: string }>; today?: string;
};
const count = (value: unknown, complete: boolean) => `${Number(value || 0).toLocaleString('ko-KR')}건${complete ? '' : ' 이상'}`;
const dateLabel = (date: Date) => `${date.getMonth() + 1}/${date.getDate()}`;
const toDate = (day: string) => new Date(`${day}T12:00:00`);
const toDay = (date: Date) => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;

function KeyboardLegendItem({ complete, percentages = false }: { complete: boolean; percentages?: boolean }) {
  const { setHoveredIndex } = useLegend();
  const { index, item } = useLegendItem();
  return <LegendItem className="bklit-legend-item"><button type="button" aria-label={`${item.label} ${count(item.value,complete)} 강조`}
    onFocus={() => setHoveredIndex(index)} onBlur={() => setHoveredIndex(null)}
    onClick={() => setHoveredIndex(index)}>
    <LegendMarker /><LegendLabel /><LegendValue formatValue={value => count(value,complete)} showPercentage={complete && percentages} />
  </button></LegendItem>;
}

function BarActions({ spec }: { spec: Spec }) {
  const { barScale, bandWidth, innerWidth, innerHeight, orientation, data, barXAccessor } = useChartStable();
  if (!barScale || !barXAccessor || !spec.action) return null;
  return <g>{data.map((row, index) => {
    const position = barScale(barXAccessor(row)) || 0;
    const horizontal = orientation === 'horizontal';
    return <rect key={index} x={horizontal ? 0 : position} y={horizontal ? position : 0}
      width={horizontal ? innerWidth : bandWidth} height={horizontal ? bandWidth : innerHeight}
      fill={spec.selected === String(row.id) ? 'var(--blue-soft)' : 'transparent'} fillOpacity={0.3}
      stroke={spec.selected === String(row.id) ? 'var(--blue)' : 'none'} rx={4}
      style={{cursor:'pointer'}} aria-label={`${String(row.fullLabel || row.name)} · 목록 필터`} {...{[spec.action!]: String(row.id)}} />;
  })}</g>;
}

function BarValueLabels({spec}:{spec:Spec}) {
  const {barScale,bandWidth,innerWidth,data,barXAccessor}=useChartStable();
  if(!barScale||!barXAccessor||bandWidth===undefined||spec.decimals===undefined)return null;
  const key=spec.series?.[0]?.key || 'amount';
  return <g aria-hidden="true">{data.map((row,index)=><text key={index} className="bklit-value-label" x={innerWidth+8} y={(barScale(barXAccessor(row))||0)+bandWidth/2} dominantBaseline="middle">{formatChartValue(row[key],spec)}</text>)}</g>;
}

function Bars({ spec }: { spec: Spec }) {
  const rows = spec.rows || []; const series = spec.series || [];
  const [hover, setHover] = useState<number | null>(null);
  const reduced = useReducedMotion();
  if (!rows.length) return <p className="bklit-empty">확인한 범위에 표시할 이슈가 없습니다.</p>;
  const horizontal = spec.orientation !== 'vertical';
  const legend = series.map(s => ({ label:s.label, color:s.color, value:rows.reduce((sum,row)=>sum+Number(row[s.key] || 0),0) }));
  return <ChartLegendHoverProvider hoveredIndex={hover} onHoverChange={setHover}>
    <div className="bklit-bar-frame" style={{height: horizontal ? Math.max(180,rows.length*34+32) : 220}}>
      <BarChart data={rows} xDataKey="name" orientation={horizontal ? 'horizontal' : 'vertical'} stacked={series.length>1}
        aspectRatio="auto" animationDuration={reduced || spec.animated === false ? 0 : 450} barGap={0.38}
        margin={{top:8,right:spec.decimals!==undefined?76:12,bottom:horizontal?12:35,left:horizontal?(spec.decimals!==undefined?116:76):12}}>
        {series.map(s => <Bar key={s.key} dataKey={s.key} fill={s.color} animate={!reduced && spec.animated !== false} lineCap={3} />)}
        {horizontal ? <BarYAxis /> : <BarXAxis showAllLabels />}
        <ChartTooltip className="bklit-hover-panel" showDatePill={false} showDots={false} content={({point}) => <div className="bklit-tooltip">
          <strong>{String(point.fullLabel || point.name)}</strong>
          {series.map(s => <div key={s.key}><i style={{background:s.color}}/>{s.label}<b>{formatChartValue(point[s.key],spec)}</b></div>)}
          {!spec.complete && <small>확인한 범위 기준</small>}
        </div>} />
        <BarActions spec={spec} /><BarValueLabels spec={spec}/>
      </BarChart>
    </div>
    {series.length>1 && <Legend items={legend} hoveredIndex={hover} onHoverChange={setHover} className="bklit-legend"><KeyboardLegendItem complete={spec.complete}/></Legend>}
  </ChartLegendHoverProvider>;
}

function Trend({ spec }: { spec: Spec }) {
  const points = spec.points || [];
  const rows = useMemo(()=>points.map(p=>({...p,date:toDate(p.date)})),[points]);
  const [range, setRange] = useState<ChartBrushSelection | null>(null);
  const reduced = useReducedMotion();
  if (!points.length) return <p className="bklit-empty">등록 추이를 확인하지 못했습니다.</p>;
  const selected = selectTrend(points, range ? toDay(range.start) : '', range ? toDay(range.end) : '');
  const select = (selection: ChartBrushSelection | null) => {
    if (!selection) {setRange(null);return;}
    const safe = selectTrend(points,toDay(selection.start),toDay(selection.end));
    setRange({start:toDate(safe.from),end:toDate(safe.to)});
  };
  const domain: [Date, Date] = [toDate(selected.from),toDate(selected.to)];
  // 단일 날짜에도 x축 폭이 0이 되지 않도록 낮 시간 앞뒤를 확보한다.
  if(selected.from===selected.to){domain[0]=new Date(+domain[0]-3600000);domain[1]=new Date(+domain[1]+3600000);}
  return <div className="bklit-trend">
    <div className="bklit-range"><label>시작일<input type="date" value={selected.from} min={points[0].date} max={selected.to}
      onChange={event=>{if(event.target.value)select({start:toDate(event.target.value),end:toDate(selected.to)});}} /></label>
      <label>종료일<input type="date" value={selected.to} min={selected.from} max={points.at(-1)!.date}
      onChange={event=>{if(event.target.value)select({start:toDate(selected.from),end:toDate(event.target.value)});}} /></label>
      <button type="button" onClick={()=>setRange(null)} disabled={!range}>전체 기간</button></div>
    <p className="bklit-selection" aria-live="polite">선택 구간 신규 <strong>{count(selected.total,spec.complete)}</strong><span> · 등록일 기준</span></p>
    <LineChart data={rows} xDomain={domain} xDomainSlotCount={rows.length} animationDuration={reduced?0:450}
      style={{height:230,aspectRatio:'auto'}} margin={{top:12,left:38,right:18,bottom:36}}>
      <Grid horizontal /><Line dataKey="count" stroke="var(--chart-1)" animate={!reduced} curve={curveLinear} />
      <YAxis formatValue={value=>Number.isInteger(value)?String(value):''} /><XAxis numTicks={5} labels={rows.map(row=>dateLabel(row.date))}/>
      <ChartTooltip className="bklit-hover-panel" showDatePill={false} content={({point})=><div className="bklit-tooltip"><strong>{toDay(point.date as Date)}</strong><div>신규 이슈<b>{count(point.count,spec.complete)}</b></div></div>}/>
    </LineChart>
    <div className="bklit-brush" aria-label="드래그로 등록 추이 구간 선택 · 위 날짜 입력으로도 선택 가능">
      <LineChart data={rows} animationDuration={0} style={{height:64,aspectRatio:'auto'}} margin={{top:8,left:38,right:18,bottom:8}}>
        <Line dataKey="count" stroke="var(--chart-1)" animate={false} curve={curveLinear} showHighlight={false}/>
        <ChartBrush selection={range} onSelectionChange={select}/>
      </LineChart>
    </div>
    <p className="bklit-note">아래 막대를 드래그해 추이를 확대할 수 있어요. 전체 미종료 지표·목록의 조회 기준은 유지됩니다.</p>
    <details className="bklit-data"><summary>선택 구간 일별 수치</summary><table><thead><tr><th>등록일</th><th>신규</th></tr></thead><tbody>{points.filter(p=>p.date>=selected.from&&p.date<=selected.to).map(p=><tr key={p.date}><td>{p.date}</td><td>{count(p.count,spec.complete)}</td></tr>)}</tbody></table></details>
  </div>;
}

function Cohort({ spec }: { spec: Spec }) {
  const data = cohortSlices(spec.summary || {});
  const total = data.reduce((sum,row)=>sum+row.value,0);
  const [hover,setHover]=useState<number|null>(null);
  const reduced=useReducedMotion();
  if(!total) return <p className="bklit-empty">{spec.complete?'이 기간에 등록된 이슈가 없습니다.':'확인한 범위에 신규 이슈가 없습니다. 전체 결과는 미확정입니다.'}</p>;
  // 불완전한 표본에 전체 비율처럼 보이는 도넛을 그리지 않는다.
  return <div className="bklit-cohort">
    {spec.complete ? <PieChart data={data} size={190} innerRadius={65} padAngle={0.025} cornerRadius={3} hoveredIndex={hover} onHoverChange={setHover}>
      {data.map((row,index)=><PieSlice key={row.label} index={index} animate={!reduced} hoverEffect={reduced?'none':'translate'} showGlow={false}/>)}
      <PieCenter defaultLabel="기간 내 신규" suffix="건"/>
    </PieChart> : <p className="bklit-note">일부 조회 결과여서 비율은 표시하지 않습니다.</p>}
    <Legend items={data.map(row=>({...row,maxValue:total}))} hoveredIndex={hover} onHoverChange={setHover} className="bklit-legend is-vertical"><KeyboardLegendItem complete={spec.complete} percentages/></Legend>
    <p className="bklit-note">선택 기간에 등록된 이슈의 현재 상태입니다. 해당 기간의 처리 완료 건수가 아닙니다.</p>
  </div>;
}

function Calendar({ spec }: { spec: Spec }) {
  const calendar = useMemo(()=>dueCalendar(spec.issues || [],spec.today),[spec.issues,spec.today]);
  const reduced=useReducedMotion();
  if(!calendar.days.length)return <p className="bklit-empty">마감 일정을 확인하지 못했습니다.</p>;
  const first=toDate(calendar.days[0].date); const lead=first.getDay();
  const byDate=new Map(calendar.days.map(day=>[day.date,day]));
  const columns=Array.from({length:Math.ceil((lead+28)/7)},(_,week)=>({bin:week,bins:Array.from({length:7},(_,day)=>{
    const date=new Date(+first);date.setDate(date.getDate()+week*7+day-lead);
    return {bin:day,date,count:byDate.get(toDay(date))?.count ?? -1};
  })}));
  const populated=calendar.days.filter(day=>day.count>0);
  return <div className="bklit-calendar">
    <p className="bklit-note">{calendar.days[0].date}–{calendar.days.at(-1)!.date} · 현재 미종료 이슈의 완료예정일</p>
    <div className="bklit-calendar-grid"><div className="bklit-weekdays">{'일월화수목금토'.split('').map(day=><span key={day}>{day}</span>)}</div>
      <HeatmapChart data={columns} xDomain={[first,toDate(calendar.days.at(-1)!.date)]} weekStartDay={0} binSize={27} gap={5} animate={!reduced} margin={{top:0,left:0,right:0,bottom:0}}
        levelColors={['var(--surface-alt)','var(--bklit-calendar-low)','var(--bklit-calendar-medium)','var(--blue)','var(--blue)']}>
        <HeatmapCells cornerRadius={4} hideGhostCells={false}/><HeatmapTooltip className="bklit-hover-panel bklit-calendar-tooltip" formatLabel={(n,date)=>n < 0 ? "조회 기간 밖" : `${toDay(date)} · 완료예정 ${count(n,spec.complete)}`}/>
      </HeatmapChart>
    </div>
    <p className="bklit-note">색이 진할수록 예정 업무가 많습니다. 기한 미등록 {count(calendar.missing,spec.complete)} · 기간 밖 {count(calendar.outside,spec.complete)}</p>
    {populated.length ? <details className="bklit-data"><summary>날짜별 업무 보기 · {count(populated.reduce((sum,p)=>sum+p.count,0),spec.complete)}</summary>
      {populated.map(day=><div className="bklit-due-day" key={day.date}><strong>{day.date} · {count(day.count,spec.complete)}</strong>{(spec.issues||[]).filter(issue=>day.ids.includes(issue.id)).map(issue=><button key={issue.id} type="button" data-workspace-issue={issue.id}>#{issue.id} {issue.title}</button>)}</div>)}
    </details> : <p className="bklit-empty">확인한 범위에 앞으로 28일 안에 마감하는 업무가 없습니다.</p>}
  </div>;
}

class Boundary extends Component<{host: HTMLElement; children: ReactNode},{failed:boolean}> {
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  componentDidCatch(error: unknown){this.props.host.dataset.chartError='true';delete this.props.host.dataset.ready;console.warn('차트 표시 실패, 기본 목록 유지',error);}
  render(){return this.state.failed ? null : this.props.children;}
}
function Chart({spec,host}:{spec:Spec;host:HTMLElement}) {
  useEffect(()=>{host.dataset.ready='true';host.querySelector('[data-bklit-values]')?.removeAttribute('open');return ()=>{delete host.dataset.ready;};},[host]);
  return <MotionConfig reducedMotion="user">{spec.kind==='monthly'?<MonthlyTrend spec={spec}/>:spec.kind==='pie'?<MetricPie spec={spec}/>:spec.kind==='bar'?<Bars spec={spec}/>:spec.kind==='sparkline'?<Sparkline spec={spec}/>:spec.kind==='trend'?<Trend spec={spec}/>:spec.kind==='cohort'?<Cohort spec={spec}/>:<Calendar spec={spec}/>}</MotionConfig>;
}
export function mountChart(host: HTMLElement, spec: Spec) {
  const root=createRoot(host.querySelector('[data-bklit-mount]')!);
  root.render(<Boundary host={host}><Chart spec={spec} host={host}/></Boundary>);
  return ()=>root.unmount();
}
