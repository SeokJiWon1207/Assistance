// 앱 공통 아이콘. 모양은 index.html 의 스프라이트(#i-이름)에 한 번만 그리고 여기서는 참조만 만든다.
// 크기 규칙: 14 · 숫자·칩 옆, 16 · 버튼 안 글자 옆, 18 · 아이콘 버튼, 20 · 빈 상태·주요 버튼.
export function icon(name, size = 16, className = "") {
  const classes = className ? `ui-icon ${className}` : "ui-icon";
  return `<svg class="${classes}" width="${size}" height="${size}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
}
