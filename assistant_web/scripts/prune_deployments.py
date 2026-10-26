#!/usr/bin/env python3
"""현재·직전 두 릴리스와 참조 중인 파일을 보존하는 배포 보관 도구."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

RELEASE = re.compile(r'\d{8}T\d{6}Z')
BACKUP = re.compile(r'(?:deploy-)?(\d{8}T\d{6}Z)(?:-retry\d+)?')
LOOSE = re.compile(r'(?:users|workspace|assistant_audit)-(\d{8}-\d{6})\.sqlite3(?:\.(?:gz|xz))?')


def under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def timestamp(value: str, fmt: str = '%Y%m%dT%H%M%SZ') -> datetime:
    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)


def process_references() -> list[Path]:
    """권한 때문에 실행 중 파일을 확인할 수 없으면 삭제를 중단한다."""
    proc = Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('운영 프로세스 참조를 확인하려면 Linux /proc가 필요합니다.')
    paths = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            links = [process / 'cwd', process / 'exe', *list((process / 'fd').iterdir())]
            for link in links:
                try:
                    target = os.readlink(link)
                except FileNotFoundError:
                    continue
                if target.startswith('/'):
                    paths.append(Path(target.removesuffix(' (deleted)')))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return paths


def symlink_references(root: Path) -> list[Path]:
    refs = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            if path.is_symlink():
                refs.append(path.resolve())
    return refs


def build_plan(app: Path, state: Path, legacy: Path, *, keep: int, refs: list[Path], now: datetime) -> dict:
    if keep < 3:
        raise ValueError('현재 배포본과 직전 2개 이상을 보존해야 합니다.')
    releases_root = app / 'releases'
    current_link = app / 'current'
    if not current_link.is_symlink():
        raise ValueError('현재 배포 링크가 없습니다.')
    current = current_link.resolve(strict=True)
    if current.parent != releases_root or not RELEASE.fullmatch(current.name):
        raise ValueError('현재 배포가 관리 대상 릴리스가 아닙니다.')
    releases = []
    for path in releases_root.iterdir():
        if path.is_symlink() or not path.is_dir() or not RELEASE.fullmatch(path.name):
            continue
        timestamp(path.name)
        # 미완성 스테이징은 롤백 슬롯으로 세거나 자동으로 삭제하지 않는다.
        if ((path / 'assistant_web/.venv/bin/python').is_file()
                and (path / 'data/mantis_chroma_db/chroma.sqlite3').is_file()
                and (path == current or (path / '.deployment-ready.json').is_file()
                     or (path / 'release-manifest.json').is_file())):
            releases.append(path)
    if current not in releases:
        raise ValueError('현재 배포가 완전하지 않습니다.')
    if len(releases) < keep:
        return {'current': str(current), 'retained': sorted(map(str, releases)),
                'candidates': [], 'reason': 'insufficient_published_history'}
    older = sorted((p for p in releases if p.name < current.name), reverse=True)
    kept = {current, *older[:keep - 1], *(p for p in releases if p.name > current.name)}
    # 구 버전으로 롤백했을 때 더 새로운 배포/진행 중 스테이징도 보호한다.
    refs = [ref.resolve() for ref in refs]
    for release in releases:
        if any(under(ref, release) for ref in refs):
            kept.add(release)
    pending = list(kept)
    scanned = set()
    while pending:
        release = pending.pop()
        if release in scanned:
            continue
        scanned.add(release)
        refs.extend(symlink_references(release))
        for dependency in releases:
            if dependency not in kept and any(under(ref, dependency) for ref in refs):
                kept.add(dependency)
                pending.append(dependency)
    candidates = [p for p in releases if p not in kept]
    oldest = min(p.name for p in kept)
    cutoff = now - timedelta(days=7)
    backup_roots = [app / 'backups', state / 'deployment-backups', legacy]
    for root in backup_roots:
        if root.is_symlink() or not root.is_dir():
            continue
        loose_groups = sorted({m.group(1) for p in root.iterdir() if not p.is_symlink()
                               and p.is_file() and (m := LOOSE.fullmatch(p.name))}, reverse=True)
        for path in root.iterdir():
            if path.is_symlink():
                continue
            match = BACKUP.fullmatch(path.name)
            if match and path.is_dir():
                name = match.group(1)
                date = timestamp(name)
                complete = (path / 'manifest.json').is_file() or (path / 'compressed-backups.json').is_file()
                if name < oldest and (complete or date < cutoff):
                    candidates.append(path)
            match = LOOSE.fullmatch(path.name)
            if match and path.is_file():
                stamp = match.group(1)
                if stamp not in loose_groups[:3] and timestamp(stamp, '%Y%m%d-%H%M%S') < cutoff:
                    candidates.append(path)
    candidates = sorted({p for p in candidates if not any(under(ref, p) for ref in refs)})
    return {'current': str(current), 'retained': sorted(map(str, kept)),
            'candidates': list(map(str, candidates))}


def prune(app: Path, state: Path, legacy: Path, *, apply: bool = False,
          healthy: Callable[[], bool] | None = None, process_paths: list[Path] | None = None,
          now: datetime | None = None, keep: int = 3) -> dict:
    if any(p.is_symlink() for p in (app, state, legacy)):
        raise ValueError('관리 루트의 심볼릭 링크는 정리하지 않습니다.')
    app, state, legacy = app.resolve(), state.resolve(), legacy.resolve()
    if app == Path('/') or state == app or under(state, app / 'releases'):
        raise ValueError('관리 경로가 올바르지 않습니다.')
    now = now or datetime.now(timezone.utc)
    lock_path = app / '.deployment.lock'
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ValueError('설치된 배포 잠금 파일이 필요합니다.')
    with lock_path.open('r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'skipped_locked', 'removed': []}
        refs = process_references() if process_paths is None else process_paths
        plan = build_plan(app, state, legacy, keep=keep, refs=refs, now=now)
        result = {**plan, 'status': 'preview', 'removed': [], 'at': now.isoformat()}
        if not apply:
            return result
        if healthy is None or not healthy():
            return {**result, 'status': 'skipped_unhealthy'}
        # 정상 상태 확인 중 현재 배포나 파일 참조가 바뀌면 삭제하지 않는다.
        refs = process_references() if process_paths is None else process_paths
        confirmed = build_plan(app, state, legacy, keep=keep, refs=refs, now=now)
        if plan != confirmed:
            raise RuntimeError('현재 배포 또는 참조가 변경되어 정리를 중단했습니다.')
        before = shutil.disk_usage(app).free
        identities = {name: (Path(name).stat().st_dev, Path(name).stat().st_ino) for name in plan['candidates']}
        result['status'] = 'running'
        def save() -> None:
            state.mkdir(parents=True, exist_ok=True)
            temporary = state / '.retention-last-run.json.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            temporary.chmod(0o600)
            temporary.replace(state / 'retention-last-run.json')
        save()
        try:
            for name in plan['candidates']:
                path = Path(name)
                if (app / 'current').resolve() != Path(plan['current']):
                    raise RuntimeError('현재 배포가 변경되어 정리를 중단했습니다.')
                stat = path.lstat()
                if path.is_symlink() or (stat.st_dev, stat.st_ino) != identities[name]:
                    raise RuntimeError('정리 대상이 변경되었습니다.')
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                result['removed'].append(name)
                save()
            result.update(status='complete', freeBytes=shutil.disk_usage(app).free,
                          reclaimedBytes=shutil.disk_usage(app).free - before)
        except Exception:
            result['status'] = 'failed'
            raise
        finally:
            save()
        return result


def is_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=10) as response:
            return json.load(response).get('status') == 'ready'
    except (OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=Path('/opt/content-ai-assistant'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/content-ai-assistant'))
    parser.add_argument('--legacy-backups', type=Path, default=Path('/var/backups/content-ai-assistant'))
    parser.add_argument('--keep-releases', type=int, default=3)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = prune(args.app_root, args.state, args.legacy_backups, apply=args.apply,
                       healthy=lambda: is_ready(args.port), keep=args.keep_releases)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f'배포 보관 정리 중단: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
