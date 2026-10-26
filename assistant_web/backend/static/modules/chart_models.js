// 날짜 문자열은 서버 집계와 같은 일자 그대로 사용한다(UTC 변환으로 하루 이동 금지).
export function isoDay(value) {
  const day = String(value || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return "";
  const parsed = new Date(`${day}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === day ? day : "";
}

export function dueCalendar(issues, today) {
  const start = isoDay(today);
  if (!start) return { days: [], missing: issues.length, outside: 0 };
  const days = Array.from({ length: 28 }, (_, offset) => ({
    date: new Date(Date.parse(`${start}T00:00:00Z`) + offset * 86400000).toISOString().slice(0, 10), count: 0, ids: /** @type {Array<string | number>} */ ([]),
  }));
  const byDate = new Map(days.map(day => [day.date, day]));
  let missing = 0; let outside = 0;
  issues.forEach(issue => {
    const date = isoDay(issue.dueDate);
    if (!date) missing += 1;
    else if (!byDate.has(date)) outside += 1;
    else { const day = byDate.get(date); day.count += 1; day.ids.push(issue.id); }
  });
  return { days, missing, outside };
}

export function selectTrend(points, from, to) {
  if (!points.length) return { from: "", to: "", total: 0 };
  const first = points[0].date; const last = points.at(-1).date;
  const clamp = value => { const day = isoDay(value); return !day || day < first ? first : day > last ? last : day; };
  const bounds = [clamp(from || first), clamp(to || last)].sort();
  return { from: bounds[0], to: bounds[1], total: points.filter(p => p.date >= bounds[0] && p.date <= bounds[1]).reduce((sum, p) => sum + p.count, 0) };
}

export function cohortSlices(summary) {
  return [
    { label: "현재 종료", value: Number(summary.closedNow || 0), color: "var(--chart-2)" },
    { label: "현재 미종료", value: Number(summary.newOpen || 0), color: "var(--chart-1)" },
    { label: "상태 미상", value: Number(summary.unknownStatus || 0), color: "var(--muted)" },
  ];
}

// 수치를 바꾸지 않고 표시 전용 식별자/축 라벨만 붙인다.
export function metricChartSpec(items, {title, kind = 'bar', decimals = 0, unit = '건', complete = true, animated = false} = {}) {
  return {kind, title, decimals, unit, complete, animated,
    rows: items.map((item,index)=>({id:String(index),name:`${index+1}. ${String(item.label).slice(0,12)}${String(item.label).length>12?'…':''}`,
      fullLabel:String(item.label),amount:item.amount})),
    series:[{key:'amount',label:title,color:'var(--chart-1)'}],
  };
}

export function formatChartValue(value, {decimals=0,unit='건',complete=true}={}) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '미확인';
  return `${Number(value).toLocaleString('ko-KR',{minimumFractionDigits:decimals,maximumFractionDigits:decimals})}${unit}${complete?'':' 이상'}`;
}

export function monthlyChartData(trend, metric) {
  const key=metric==='efforts'?'efforts':'counts';
  let missing=false;
  const series=(trend.series||[]).map((item,index)=>({key:`series${index}`,label:item.label,color:`var(--chart-${index%6+1})`}));
  const rows=(trend.periods||[]).map((period,index)=>{
    const text=String(period);const date=isoDay(`${text.slice(0,4)}-${text.slice(4)}-01`);
    if(!/^\d{6}$/.test(text)||!date)missing=true;
    const row={date,period:text};
    series.forEach((s,i)=>{
      const value=trend.series[i][key]?.[index];
      const valid=typeof value==='number'&&Number.isFinite(value)&&value>=0;
      row[s.key]=valid?value:null;if(!valid)missing=true;
    });
    return row;
  });
  return {rows,series,missing,decimals:key==='efforts'?2:0,unit:key==='efforts'?'':'건',effortBasis:trend.effortDateBasis==='delivered_at'?'고객사전달일':'등록일'};
}
