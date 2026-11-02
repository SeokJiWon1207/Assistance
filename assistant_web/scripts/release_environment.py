"""운영 .env를 보존하면서 누락된 OpenAI 키만 릴리스에 추가한다.

파일 내용은 출력하지 않는다. 애플리케이션 dotenv와 같은 첫 정의 우선 규칙을
사용하며, 기존 원문을 유지해 JSON·따옴표·이스케이프를 다시 직렬화하지 않는다.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

OVERLAY_KEYS = frozenset({'OPENAI_API_KEY'})


def _entries(text: str) -> dict[str, tuple[int, str, str]]:
    entries = {}
    for index, raw in enumerate(text.splitlines(keepends=True)):
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key not in entries:
            entries[key] = (index, value.strip().strip('"').strip("'"), raw.rstrip('\r\n'))
    return entries


def _write_private(path: Path, text: str) -> None:
    # 새 릴리스에만 기록한다. 기존 파일·활성 환경의 우발적 덮어쓰기를 거절한다.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_overlay(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise ValueError('입력 환경 파일을 덮어쓸 수 없습니다')
    entries = _entries(source.read_text(encoding='utf-8'))
    lines = [raw for key, (_index, value, raw) in entries.items() if key in OVERLAY_KEYS and value]
    _write_private(output, '\n'.join(lines) + ('\n' if lines else ''))


def build_environment(base: Path, overlay: Path, output: Path, *, bootstrap: Path | None = None) -> None:
    inputs = [base, overlay, *([bootstrap] if bootstrap else [])]
    if output.resolve() in {path.resolve() for path in inputs}:
        raise ValueError('입력 환경 파일을 덮어쓸 수 없습니다')
    if base.exists():
        if bootstrap is not None:
            raise ValueError('기존 운영 환경에는 bootstrap을 적용할 수 없습니다')
        text = base.read_text(encoding='utf-8')
    elif bootstrap is not None:
        text = bootstrap.read_text(encoding='utf-8')
    else:
        raise ValueError('운영 환경이 없습니다. 최초 설치에는 명시적인 bootstrap 환경 파일이 필요합니다')
    original = _entries(text)
    additions = _entries(overlay.read_text(encoding='utf-8'))
    lines = text.splitlines(keepends=True)
    for key, (_index, value, raw) in additions.items():
        if key not in OVERLAY_KEYS or not value or original.get(key, (0, '', ''))[1]:
            continue
        replacement = raw + '\n'
        if key in original:
            lines[original[key][0]] = replacement
        else:
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines[-1] += '\n'
            lines.append(replacement)
    _write_private(output, ''.join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    overlay = sub.add_parser('overlay')
    overlay.add_argument('--source', type=Path, required=True)
    overlay.add_argument('--output', type=Path, required=True)
    build = sub.add_parser('build')
    build.add_argument('--base', type=Path, required=True)
    build.add_argument('--overlay', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--bootstrap', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'overlay':
            write_overlay(args.source, args.output)
        else:
            build_environment(args.base, args.overlay, args.output, bootstrap=args.bootstrap)
    except (OSError, ValueError):
        # OS 오류도 원문/값을 포함하지 않는 고정 메시지로 끝낸다.
        parser.exit(1, '환경 파일 생성 실패: 입력·출력 경로, 기존 파일 및 bootstrap 조건을 확인하세요.\n')
    print('릴리스 환경 파일 준비 완료 (0600)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
