// 화면에서만 정보를 정리한다. 답변 원문·합계·출처 식별자는 변경하지 않는다.
const SECTION = /^(?:핵심 요약|핵심 내용|핵심 결론|요약|결론|한눈에 보기|주요 결과|신규 이슈|미종료 이슈|장기 적체|공수·처리시간|권장 조치|다음 조치|다음으로 할 일|최근 월별 추이|플랫폼·화면번호|(?:[1-9]|1[0-2])월 주요 등록 이슈|전달 공수가 큰 이슈|다음 확인 항목)$/;
const SUMMARY = /^(?:핵심 요약|핵심 내용|핵심 결론|요약|결론|한눈에 보기|주요 결과)$/;
const METRIC_LABEL = /^(?:(?:현재|기간 내|이번 주|지난주|최근 \d+일|기간|총)\s+)?(?:신규(?: 등록)?|등록 이슈|미종료(?: 이슈)?|해결[·/]종료|해결|종료|완료|장기 적체|장기 미종료|기한 지남|일정 지연|전체|총(?: 이슈)?|조회(?: 결과)?|등록 건수|평균 처리기간|처리기간|공수(?: 합계)?|기간 공수 합계)$/;
const expandedByAnswer = new WeakMap();

export function isSummarySectionHeading(text) {
  return SECTION.test(String(text).trim());
}

export function parseSummaryMetric(text) {
  const line = String(text).trim().replace(/^[-*•]\s+/, '').replace(/\*\*/g, '');
  const match = line.match(/^([^:：\n]{2,32})[:：]\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(건|일|시간|분|%|M\/D|MD|MM)?(?:\s*(이상|이하|초과|미만))?(?:\s+(.+))?$/);
  if (!match || !METRIC_LABEL.test(match[1].trim()) || /#\d+/.test(match[5] || '')) return null;
  if (!match[3] && !/공수/.test(match[1])) return null;
  return {label: match[1].trim(), value: match[2], unit: [match[3], match[4]].filter(Boolean).join(' '), note: match[5] || ''};
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderAnswerByline(avatar) {
  const byline = element('div', 'answer-byline');
  byline.append(avatar, element('span', '', 'AI 비서'));
  return byline;
}

function rememberDisclosure(details, answer, key) {
  details.open = expandedByAnswer.get(answer)?.[key] === true;
  details.addEventListener('toggle', () => {
    expandedByAnswer.set(answer, {...expandedByAnswer.get(answer), [key]: details.open});
  });
}

function styleLead(body) {
  let first = body.firstElementChild;
  // 결정형 보고서의 첫 줄과 바로 뒤의 조회 기준을 분리한다. 원래 노드를 이동해 링크를 보존한다.
  if (first?.tagName === 'P') {
    const br = Array.from(first.childNodes).find(node => node.nodeName === 'BR');
    const firstLine = [];
    for (const node of first.childNodes) {
      if (node === br) break;
      firstLine.push(node);
    }
    const title = firstLine.map(node => node.textContent).join('').trim();
    if (title.length <= 110 && /(?:보고서|리포트|업무 요약|현황|조회 결과)$/.test(title)) {
      const heading = element('h3', 'answer-heading answer-title');
      firstLine.forEach(node => heading.appendChild(node));
      br?.remove();
      body.insertBefore(heading, first);
      if (!first.textContent.trim()) first.remove();
      first = heading;
    }
  }
  if (first?.matches('h1,h2,h3,h4') && !SUMMARY.test(first.textContent.trim())) first.classList.add('answer-title');
  const lead = Array.from(body.children).find(node => node.tagName === 'P'
    && !/^(?:기준|검색 기준|조회 기준|※|주의)/.test(node.textContent.trim()));
  if (lead && lead.textContent.length <= 180) lead.classList.add('answer-summary-lead');
}

function summarizeMetrics(body) {
  // 원문 근거·표·접힌 설명의 숫자는 사용하지 않는다. 표시된 첫 요약 목록에서만 가져온다.
  for (const node of Array.from(body.children)) {
    if (node.matches('.issue-table-wrap,.markdown-table-wrap,details,blockquote')) break;
    if (!node.matches('ul.answer-list')) continue;
    const metrics = Array.from(node.children).map(item => ({item, metric: item.querySelector('a,button') ? null : parseSummaryMetric(item.textContent)}))
      .filter(entry => entry.metric).slice(0, 3);
    if (metrics.length < 2) continue;
    const strip = element('dl', 'answer-metrics');
    strip.setAttribute('aria-label', '답변 핵심 수치');
    for (const {item, metric} of metrics) {
      const cell = element('div', 'answer-metric');
      cell.appendChild(element('dt', '', metric.label));
      const value = element('dd', /기한 지남|지연/.test(metric.label) ? 'is-overdue' : '', metric.value);
      value.appendChild(element('span', 'answer-metric-unit', metric.unit));
      cell.appendChild(value);
      if (metric.note) cell.appendChild(element('small', 'answer-metric-note', metric.note));
      strip.appendChild(cell);
      item.remove();
    }
    body.insertBefore(strip, node);
    if (!node.children.length) node.remove();
    break;
  }
}

function readableIssueRows(body, answer) {
  const tables = Array.from(body.children).filter(node => node.matches('.issue-table-wrap'));
  tables.forEach((wrapper, index) => {
    const table = wrapper.querySelector(':scope > .issue-table');
    if (!table) return;
    const rows = Array.from(table.querySelectorAll('tbody > tr'));
    if (!rows.length) return;
    const list = element('div', 'answer-issue-list');
    list.setAttribute('role', 'list');
    list.setAttribute('aria-label', '답변 이슈 목록');
    for (const row of rows) {
      const cells = Array.from(row.children);
      if (cells.length !== 7) return;
      const item = element('div', 'answer-issue-item');
      item.setAttribute('role', 'listitem');
      const action = element(row.dataset.sourceKey ? 'button' : 'div', 'answer-issue-row');
      if (row.dataset.sourceKey) {
        action.classList.add('issue-summary-clickable');
        action.type = 'button';
        action.dataset.sourceKey = row.dataset.sourceKey;
        action.setAttribute('aria-label', `${cells[0].textContent} ${cells[2].textContent} 상세 보기`);
      }
      const copy = element('span', 'answer-issue-copy');
      const title = element('span', 'answer-issue-title', cells[2].textContent);
      const status = cells[3].firstElementChild;
      if (status) title.appendChild(status.cloneNode(true));
      copy.appendChild(title);
      copy.appendChild(element('span', 'answer-issue-meta', [cells[0].textContent, cells[1].textContent, cells[5].textContent].filter(Boolean).join(' · ')));
      copy.appendChild(element('span', 'answer-issue-meta', `등록일 ${cells[6].textContent} · 중요도 ${cells[4].textContent}`));
      action.appendChild(copy);
      if (row.dataset.sourceKey) {
        const arrow = element('span', 'answer-issue-arrow', '›');
        arrow.setAttribute('aria-hidden', 'true');
        action.appendChild(arrow);
      }
      item.appendChild(action);
      list.appendChild(item);
    }
    const detail = element('details', 'answer-full-table');
    detail.appendChild(element('summary', '', `전체 항목 표로 보기 · ${rows.length}건`));
    const scroll = element('div', 'answer-table-scroll');
    scroll.appendChild(table);
    detail.appendChild(scroll);
    rememberDisclosure(detail, answer, `table-${index}`);
    wrapper.classList.add('is-summary-list');
    wrapper.append(list, detail);
  });
}

function readableIssueBullets(body) {
  for (const list of Array.from(body.children).filter(node => node.matches('ul.answer-list'))) {
    const items = Array.from(list.children);
    if (!items.length || !items.every(item => {
      const ref = item.querySelector('button.inline-issue-ref');
      return ref && item.textContent.trim().startsWith(ref.textContent) && item.querySelectorAll('button').length === 1;
    })) continue;
    const rows = element('div', 'answer-issue-list');
    rows.setAttribute('role', 'list');
    rows.setAttribute('aria-label', '답변 이슈 목록');
    for (const item of items) {
      const ref = item.querySelector('button.inline-issue-ref');
      const text = item.textContent.trim().slice(ref.textContent.length).trim();
      const separator = text.lastIndexOf(' — ');
      const title = separator >= 0 ? text.slice(0, separator) : text;
      const meta = separator >= 0 ? `${ref.textContent} · ${text.slice(separator + 3)}` : ref.textContent;
      const row = element('div', 'answer-issue-item');
      row.setAttribute('role', 'listitem');
      const action = element('button', 'answer-issue-row issue-summary-clickable');
      action.type = 'button';
      action.dataset.sourceKey = ref.dataset.sourceKey;
      action.setAttribute('aria-label', `${ref.textContent} ${title} 상세 보기`);
      const copy = element('span', 'answer-issue-copy');
      copy.append(element('span', 'answer-issue-title', title), element('span', 'answer-issue-meta', meta));
      const arrow = element('span', 'answer-issue-arrow', '›');
      arrow.setAttribute('aria-hidden', 'true');
      action.append(copy, arrow);
      row.appendChild(action);
      rows.appendChild(row);
    }
    list.replaceWith(rows);
  }
}

export function prepareSummaryAnswer(container, answer) {
  if (answer.loading || answer.streaming || answer.effortChart) return;
  const rendered = container.querySelector(':scope > .bubble > .assistant-answer');
  if (!rendered || rendered.dataset.summaryReady) return;
  rendered.dataset.summaryReady = 'true';
  const body = rendered.querySelector(':scope > .notion-answer') || rendered;
  styleLead(body);
  if (!answer.effortRanking && !answer.effortTrend) summarizeMetrics(body);
  readableIssueRows(body, answer);
  readableIssueBullets(body);
}

export function groupAnswerEvidence(container, answer) {
  if (answer.loading || answer.streaming) return;
  const scope = container.querySelector(':scope > details.answer-data-scope');
  const sources = container.querySelector(':scope > details.answer-related-sources');
  if (!scope && !sources) return;
  const details = element('details', 'answer-evidence');
  const summary = element('summary', '', '근거와 조회 기준');
  details.appendChild(summary);
  for (const [node, className, title] of [[scope, 'answer-evidence-scope', '조회 기준'], [sources, 'answer-evidence-sources answer-related-sources', '관련 자료']]) {
    if (!node) continue;
    const section = element('section', className);
    section.appendChild(element('h4', '', title));
    Array.from(node.children).filter(child => child.tagName !== 'SUMMARY').forEach(child => section.appendChild(child));
    details.appendChild(section);
    node.remove();
  }
  rememberDisclosure(details, answer, 'evidence');
  const verification = container.querySelector(':scope > .verification-panel');
  container.insertBefore(details, verification);
}
