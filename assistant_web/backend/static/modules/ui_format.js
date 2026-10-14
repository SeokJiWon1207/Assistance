// 사용자 화면의 시각은 로컬 시간대, 한국어, 분 단위로 표시한다.
export function formatUiDateTime(value, { compact = false, now = new Date() } = {}) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const hour = date.getHours();
  const time = `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}:${String(date.getMinutes()).padStart(2, "0")}`;
  const sameYear = date.getFullYear() === now.getFullYear();
  if (compact && date.toDateString() === now.toDateString()) return time;
  const day = compact ? `${date.getMonth() + 1}/${date.getDate()}` : `${date.getMonth() + 1}월 ${date.getDate()}일`;
  return `${sameYear ? "" : `${date.getFullYear()}년 `}${day} ${time}`;
}
