"""기관 연동 경계.

Agent·도구는 InstitutionAdapter 인터페이스(submit / poll)만 안다.
- 데모: DemoSimulator — 사전에 정한 시나리오대로 결과가 돌아온다(실제 기관과 통신하지 않음).
- 운영: 같은 인터페이스로 계좌이동서비스·기관별 어댑터를 붙인다(아래 AccountMoveServiceAdapter 자리).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class ResultEvent:
    date: date
    item_id: str
    new_state: str | None   # None이면 상태 변화 없는 관찰(거래내역 등)
    channel: str            # official(공식 결과) | customer(고객 확인) | observed(거래내역 관찰)
    message: str


class InstitutionAdapter(Protocol):
    name: str
    simulated: bool

    def submit(self, *, item_id: str, action: str, idempotency_key: str, approval_token: dict, on_date: date) -> dict:
        """승인된 변경 신청을 기관에 보낸다. 접수증({"receipt": ...})을 돌려준다. 같은 멱등키는 한 번만 접수한다."""

    def poll(self, *, until: date) -> list[ResultEvent]:
        """until까지 도착한 결과·관찰 이벤트를 날짜순으로 돌려준다."""


# 데모 시나리오: 날짜가 지나면 이 결과가 도착한다(김우리 시나리오 전용)
DEMO_SCENARIO = [
    ResultEvent(date(2026, 11, 19), "TEL", "DONE", "official", "공식 결과: 출금계좌 변경 완료"),
    ResultEvent(date(2026, 11, 20), "INS", "DONE", "official", "공식 결과: 12월 주기부터 변경 완료"),
    ResultEvent(date(2026, 11, 20), "SAL", "PENDING", "customer", "회사 처리 접수(고객 확인)"),
    ResultEvent(date(2026, 11, 25), "SAL", "DONE", "observed", "거래내역: 우리WON에 첫 급여 입금"),
    ResultEvent(date(2026, 11, 27), "TEL", None, "observed", "거래내역: 우리WON에서 첫 출금 정상"),
    ResultEvent(date(2026, 12, 2), "MGT", "FAILED", "official", "공식 결과: 변경 불가(기관 직접 변경 필요)"),
    ResultEvent(date(2026, 12, 15), "MGT", None, "observed", "거래내역: 기존 A은행에서 계속 출금"),
    ResultEvent(date(2026, 12, 18), "INS", None, "observed", "거래내역: 우리WON에서 첫 출금 정상"),
]


class DemoSimulator:
    """데모 시뮬레이터: 접수는 메모리에만 남기고, 결과는 DEMO_SCENARIO에서 꺼낸다."""
    name = "DemoSimulator"
    simulated = True
    describe = "모의 기관(사전 정의 시나리오)"

    def __init__(self, scenario=None):
        self.scenario = list(scenario or DEMO_SCENARIO)
        self.receipts: dict[str, dict] = {}

    def submit(self, *, item_id, action, idempotency_key, approval_token, on_date):
        if idempotency_key not in self.receipts:
            self.receipts[idempotency_key] = {"receipt": f"SIM-{len(self.receipts) + 1:04d}", "item_id": item_id,
                                              "action": action, "accepted_on": on_date.isoformat(), "simulated": True}
        return self.receipts[idempotency_key]

    def poll(self, *, until):
        return [e for e in self.scenario if e.date <= until]


class AccountMoveServiceAdapter:
    """운영 연동 자리(미구현). 자동이체 출금계좌 변경을 계좌이동서비스의 공식 절차로 신청·조회한다.

    기관별로 변경이 안 되는 경우(데모의 관리비 사례)는 같은 poll 결과의 FAILED로 돌아오고,
    Agent의 실패 대응 흐름(get_move_status → set_followup)은 그대로 쓴다.
    """
    name = "AccountMoveServiceAdapter"
    simulated = False
    describe = "계좌이동서비스 연동(운영)"

    def submit(self, **kw):
        raise NotImplementedError("운영 연동 자리: 계좌이동서비스 변경 신청")

    def poll(self, **kw):
        raise NotImplementedError("운영 연동 자리: 변경 결과 조회")
