"""운영체제와 컨테이너의 신규 작업 수용 여유를 읽는다."""
from pathlib import Path


def available_memory_bytes(
    proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int | None:
    """파일 캐시 회수를 반영한 호스트 여유와 cgroup 한도 중 작은 값."""
    limits: list[int] = []
    try:
        for line in (proc_root / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                value = int(line.split()[1]) * 1024
                if value >= 0:
                    limits.append(value)
                break
    except (OSError, ValueError, IndexError):
        pass

    try:
        for line in (proc_root / "self/cgroup").read_text().splitlines():
            if not line.startswith("0::"):
                continue
            relative = Path(line[3:].lstrip("/"))
            if ".." in relative.parts:
                break
            # 상위 slice/container의 제한도 현재 프로세스에 적용된다.
            group = cgroup_root / relative
            while True:
                try:
                    maximum = int((group / "memory.max").read_text().strip())
                    current = int((group / "memory.current").read_text().strip())
                    if maximum >= 0 and current >= 0:
                        limits.append(max(0, maximum - current))
                except (OSError, ValueError):
                    pass
                if group == cgroup_root:
                    break
                group = group.parent
            break
    except OSError:
        pass
    return min(limits) if limits else None
