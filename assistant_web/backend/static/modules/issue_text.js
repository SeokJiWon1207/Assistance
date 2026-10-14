// 구분 문자를 포함한 원문 필드는 JSON 문자열로 인용한다. DOM에는 복원된 문자만 표시한다.
export function decodeIssueField(value) {
  if (value.startsWith('"') && value.endsWith('"')) {
    try {
      const decoded = JSON.parse(value);
      if (typeof decoded === 'string') return decoded;
    } catch {}
  }
  return value;
}

export function parseIssueListLine(line) {
  const trimmed = String(line || '').trim();
  if (!/^\d+\.\s/.test(trimmed) || !/\*?\*?ID\*?\*?\s*:/.test(trimmed)) return null;
  const parts = trimmed.replace(/^\d+\.\s*/, '').split('|');
  const keys = { ID: 'id', '프로젝트': 'project', '제목': 'title', '상태': 'status', '담당자': 'handler', '등록일': 'createdAt' };
  const issue = {};
  for (const part of parts) {
    const separator = part.indexOf(':');
    if (separator < 0) continue;
    const label = part.slice(0, separator).replace(/\*\*|`/g, '').trim();
    const key = keys[label];
    if (!key) continue;
    const value = decodeIssueField(part.slice(separator + 1).trim());
    issue[key] = key === 'id' ? value.replace(/^#/, '') : value;
  }
  return issue.id && issue.title ? issue : null;
}
