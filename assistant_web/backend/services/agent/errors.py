"""공통 실행기와 호출 어댑터가 공유하는 종료 신호."""


class AgentStop(Exception):
    def __init__(self, reason: str, detail: str = ''):
        super().__init__(detail or reason)
        self.reason = reason
