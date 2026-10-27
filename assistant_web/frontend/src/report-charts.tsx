import { curveLinear } from 'd3-shape';
import { monthlyChartData } from '../../backend/static/modules/chart_models.js';
import { LineChart, Line } from '../vendor/bklit/charts/line-chart';
import { Grid } from '../vendor/bklit/charts/grid';
import { XAxis } from '../vendor/bklit/charts/x-axis';
import { YAxis } from '../vendor/bklit/charts/y-axis';
import { ChartTooltip } from '../vendor/bklit/charts/tooltip/chart-tooltip';
import { ChartLegendHoverProvider } from '../vendor/bklit/charts/chart-legend-hover';
import { useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { PieChart } from '../vendor/bklit/charts/pie-chart';
import { PieSlice } from '../vendor/bklit/charts/pie-slice';
import { PieCenter } from '../vendor/bklit/charts/pie-center';
import { Legend, LegendItem, LegendLabel, LegendMarker, LegendValue } from '../vendor/bklit/charts/legend';
import { useLegend, useLegendItem } from '../vendor/bklit/charts/legend/legend-context';
import { formatChartValue } from '../../backend/static/modules/chart_models.js';
import type { Spec } from './charts';

function ValueLegend({spec}:{spec:Spec}) {
  const {setHoveredIndex}=useLegend(); const {index,item}=useLegendItem();
  return <LegendItem className="bklit-legend-item"><button type="button" aria-label={`${item.label} ${formatChartValue(item.value,spec)} 강조`}
    onFocus={()=>setHoveredIndex(index)} onBlur={()=>setHoveredIndex(null)} onClick={()=>setHoveredIndex(index)}>
    <LegendMarker/><LegendLabel/><LegendValue formatValue={v=>formatChartValue(v,spec)} showPercentage={spec.complete}/>
  </button></LegendItem>;
}

export function MetricPie({spec}:{spec:Spec}) {
  const data=(spec.rows||[]).map((row,index)=>({label:String(row.fullLabel),value:Number(row.amount),color:`var(--chart-${index%6+1})`}));
  const [hover,setHover]=useState<number|null>(null); const reduced=useReducedMotion();
  const valid=data.every(row=>Number.isFinite(row.value)&&row.value>=0);
  const total=data.reduce((sum,row)=>sum+row.value,0);
  if(!valid)throw new Error('유효하지 않은 공수 데이터');
  if(!total)return <p className="bklit-empty">표시할 공수가 없습니다.</p>;
  return <div className="bklit-cohort bklit-metric-pie">
    {spec.complete ? <PieChart data={data} size={190} innerRadius={65} hoveredIndex={hover} onHoverChange={setHover}>
      {data.map((row,index)=><PieSlice key={index} index={index} animate={Boolean(spec.animated)&&!reduced} hoverEffect={reduced?'none':'translate'} showGlow={false}/>)}
      <PieCenter defaultLabel={spec.title} formatOptions={{minimumFractionDigits:spec.decimals??2,maximumFractionDigits:spec.decimals??2}} suffix={spec.unit}/>
    </PieChart> : <p className="bklit-note">일부 조회 결과여서 비율은 표시하지 않습니다.</p>}
    <Legend items={data.map(row=>({...row,maxValue:total}))} hoveredIndex={hover} onHoverChange={setHover} className="bklit-legend is-vertical"><ValueLegend spec={spec}/></Legend>
  </div>;
}

function SeriesToggle({hidden,onToggle,onHover,spec}:{hidden:Set<string>;onToggle:(key:string)=>void;onHover:(index:number|null)=>void;spec:Spec}) {
  const {item,index}=useLegendItem();const key=`series${index}`;const selected=!hidden.has(key);
  return <LegendItem className="bklit-legend-item"><button type="button" aria-pressed={selected} aria-label={`${item.label} 표시`} className={selected?'':'is-muted'}
    onClick={()=>onToggle(key)} onFocus={()=>onHover(index)} onBlur={()=>onHover(null)}>
    <LegendMarker/><LegendLabel/><LegendValue formatValue={v=>formatChartValue(v,spec)}/>
  </button></LegendItem>;
}

export function MonthlyTrend({spec}:{spec:Spec}) {
  const [metric,setMetric]=useState<'counts'|'efforts'>('counts');
  const [hidden,setHidden]=useState<Set<string>>(new Set());const [hover,setHover]=useState<number|null>(null);
  const model: {rows:Array<{date:string;period:string}&Record<string,string|number|null>>;series:Array<{key:string;label:string;color:string}>;missing:boolean;decimals:number;unit:string;effortBasis:string}=monthlyChartData(spec.trend||{periods:[],series:[]},metric);
  const formatSpec={...spec,decimals:model.decimals,unit:model.unit};
  const rows=model.rows.map(row=>({...row,date:new Date(`${row.date}T12:00:00`)}));
  const visible=model.series.filter(s=>!hidden.has(s.key));const reduced=useReducedMotion();
  const hoverPosition=hover===null?-1:visible.findIndex(s=>s.key===`series${hover}`);
  const toggle=(key:string)=>setHidden(old=>{const next=new Set(old);if(next.has(key))next.delete(key);else next.add(key);return next;});
  const changeMetric=(value:'counts'|'efforts')=>{setMetric(value);setHover(null);};
  const onKeys=(event:React.KeyboardEvent<HTMLDivElement>)=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();const value=event.key==='Home'?'counts':event.key==='End'?'efforts':metric==='counts'?'efforts':'counts';
    changeMetric(value);event.currentTarget.querySelector<HTMLButtonElement>(`[data-metric="${value}"]`)?.focus();
  };
  const totals=model.series.map(s=>({label:s.label,color:s.color,value:model.rows.reduce((sum,row)=>sum+Number(row[s.key]||0),0)}));
  return <div className="bklit-monthly">
    <div className="bklit-metric-tabs" role="tablist" aria-label="추이 지표 선택" onKeyDown={onKeys}>
      <button type="button" role="tab" data-metric="counts" aria-selected={metric==='counts'} tabIndex={metric==='counts'?0:-1} onClick={()=>changeMetric('counts')}>월별 등록건수</button>
      <button type="button" role="tab" data-metric="efforts" aria-selected={metric==='efforts'} tabIndex={metric==='efforts'?0:-1} onClick={()=>changeMetric('efforts')}>{model.effortBasis==='고객사전달일'?'월별 전달 공수':'월별 공수'}</button>
    </div>
    <p className="bklit-note">{metric==='counts'?'이슈 등록일 기준입니다. 처리·완료 시점 기준이 아닙니다.':model.effortBasis==='고객사전달일'?'고객사전달일 기준입니다. 전달일 없는 이슈는 제외합니다.':'저장된 답변의 등록일 기준 공수입니다.'}{!spec.complete?' · 일부 조회 결과입니다.':''}</p>
    {model.missing ? <p className="bklit-note">일부 월의 수치가 없어 차트 대신 아래 원본 표를 확인해 주세요.</p> : visible.length ? <ChartLegendHoverProvider hoveredIndex={hoverPosition<0?null:hoverPosition} onHoverChange={()=>{}}>
      <LineChart data={rows} animationDuration={reduced?0:350} style={{height:300,aspectRatio:'auto'}} margin={{top:12,left:48,right:20,bottom:36}}>
        <Grid horizontal/>{visible.map(s=><Line key={s.key} dataKey={s.key} stroke={s.color} animate={!reduced} curve={curveLinear} fadeEdges={false}/>)}
        <YAxis formatValue={value=>model.decimals?String(Number(value.toFixed(2))):Number.isInteger(value)?String(value):''}/>
        <XAxis numTicks={Math.min(6,rows.length)} labels={model.rows.map(row=>`${row.period.slice(2,4)}.${row.period.slice(4)}`)}/>
        <ChartTooltip className="bklit-hover-panel" showDatePill={false} content={({point})=><div className="bklit-tooltip"><strong>{String(point.period).slice(0,4)}년 {Number(String(point.period).slice(4))}월</strong>{visible.map(s=><div key={s.key}><i style={{background:s.color}}/>{s.label}<b>{formatChartValue(point[s.key],formatSpec)}</b></div>)}</div>}/>
      </LineChart>
    </ChartLegendHoverProvider> : <p className="bklit-empty">표시할 유형을 범례에서 선택해 주세요.</p>}
    <Legend items={totals} hoveredIndex={hover} onHoverChange={setHover} className="bklit-legend"><SeriesToggle hidden={hidden} onToggle={toggle} onHover={setHover} spec={{...formatSpec,complete:spec.complete&&!model.missing}}/></Legend>
    <p className="bklit-note">범례를 눌러 유형을 표시하거나 숨길 수 있어요. 범례 값은 해당 기간 합계입니다.{spec.trend?.untypedCount ? ` 유형 미분류 ${spec.trend.untypedCount}건은 제외했습니다.`:''}</p>
    <details className="bklit-data" open={model.missing || undefined}><summary>월별 수치 보기</summary><div className="bklit-table-scroll"><table><thead><tr><th>기간</th>{model.series.map(s=><th key={s.key}>{s.label}</th>)}</tr></thead><tbody>{model.rows.map((row,index)=><tr key={index}><th>{row.period}</th>{model.series.map(s=><td key={s.key}>{formatChartValue(row[s.key],formatSpec)}</td>)}</tr>)}</tbody></table></div></details>
  </div>;
}
