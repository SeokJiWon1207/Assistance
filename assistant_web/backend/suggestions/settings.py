"""추천은 명시적으로 켜며 모델 선택은 별도 허용 목록을 요구한다."""
import os

POLICY_VERSION = '2'
CACHE_SECONDS = 600
LEASE_SECONDS = 90
COOLDOWN_SECONDS = 60
QUEUE_LIMIT = 8


def enabled() -> bool:
    return os.getenv('HOME_SUGGESTIONS_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes'}


def allowed_values(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, '').split(',') if value.strip()}
