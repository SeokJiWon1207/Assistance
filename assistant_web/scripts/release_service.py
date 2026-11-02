"""기존 systemd 환경 연결을 유지하면서 릴리스 ID만 동기화한다."""
import argparse
import os
from pathlib import Path
import re
import stat
import tempfile


def sync_release_id(directory: Path, release_id: str) -> None:
    if not re.fullmatch(r'\d{8}T\d{6}Z', release_id):
        raise ValueError('올바르지 않은 릴리스 ID')
    if directory.is_symlink():
        raise ValueError('추가 설정 디렉터리는 심볼릭 링크일 수 없습니다')
    if not directory.exists():
        return
    changes = []
    for path in sorted(directory.glob('*.conf')):
        if path.is_symlink():
            raise ValueError('추가 설정은 심볼릭 링크일 수 없습니다')
        original = path.read_text(encoding='utf-8')
        if 'APP_RELEASE_ID=' not in original:
            continue
        updated, count = re.subn(r'^Environment=APP_RELEASE_ID=[^\r\n]*$',
                                 'Environment=APP_RELEASE_ID=' + release_id, original, flags=re.M)
        if count != 1:
            raise ValueError('릴리스 ID 추가 설정은 한 줄이어야 합니다')
        changes.append((path, updated, path.stat()))
    # 어떤 설정도 교체하기 전에 전체 변경을 검증한다. 운영 호출자는 별도로 원본을 백업한다.
    for path, updated, info in changes:
        descriptor, temporary_name = tempfile.mkstemp(prefix='.release-', dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if (temporary.stat().st_uid, temporary.stat().st_gid) != (info.st_uid, info.st_gid):
                os.chown(temporary, info.st_uid, info.st_gid)
            temporary.chmod(stat.S_IMODE(info.st_mode))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--release-id', required=True)
    arguments = parser.parse_args()
    sync_release_id(arguments.directory, arguments.release_id)
