"""영업일 계산과 이전 일정 규칙(시스템 규칙: LLM이 계산하지 않음)."""
from __future__ import annotations

from datetime import date, timedelta

try:
    import holidays as _hol
    _KR = _hol.KR(years=range(2025, 2028))
except Exception:  # holidays 패키지가 없으면 주말만 제외
    _KR = {}

# 근거: 하나은행 계좌이동서비스 FAQ — 매월 자동송금은 출금예정일 D-2영업일부터 변경 불가.
AUTOSEND_CUTOFF_BD = 2
# 가정: 자동납부(CMS) 출금계좌 변경은 신청 후 처리까지 3영업일이 걸린다고 보수적으로 가정.
AUTOPAY_PROCESSING_BD = 3


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _KR


def adjust_to_business_day(d: date, backward: bool = False) -> date:
    step = -1 if backward else 1
    while not is_business_day(d):
        d += timedelta(days=step)
    return d


def business_days_between(start: date, end: date) -> int:
    """start 다음 날부터 end까지(포함) 영업일 수."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_business_day(d):
            n += 1
    return n


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, 28) if d.day > 28 else d.day
    return date(y, m, day)


def next_occurrence(day: int, after: date) -> date:
    """after 이후(당일 포함) 처음 오는 매월 day일."""
    d = date(after.year, after.month, min(day, 28))
    if d < after:
        d = add_months(d, 1)
    return d


def fmt(d: date) -> str:
    return f"{d.month}.{d.day:02d}" if d.day < 10 else f"{d.month}.{d.day}"


def first_movable_cycle(kind: str, day: int, today: date, salary_date: date) -> tuple[date, str, str]:
    """자동이체 항목을 새 계좌로 옮겨 처음 출금될 날짜와 그 이유.

    규칙(순서대로):
      1) 새 계좌에 돈이 들어오기 전(첫 급여일 이전) 출금은 옮기지 않는다 → 다음 주기
      2) 자동송금: 출금예정일 D-2영업일 이내면 이번 주기 변경 불가 → 다음 주기
      3) 자동납부: 처리 기간(가정 3영업일) 안에 출금일이 있으면 → 다음 주기
    """
    due = adjust_to_business_day(next_occurrence(day, today))
    reasons, short = [], ""
    if due <= salary_date:
        reasons.append(f"{fmt(due)} 출금이 첫 급여({fmt(salary_date)}) 전")
        short = short or "첫 급여 전 출금"
    bd = business_days_between(today, due)
    if kind == "자동송금" and bd <= AUTOSEND_CUTOFF_BD:
        reasons.append("자동송금 D-2영업일 이내")
        short = short or "D-2영업일 이내"
    if kind == "자동납부" and bd < AUTOPAY_PROCESSING_BD:
        reasons.append(f"처리 기간({AUTOPAY_PROCESSING_BD}영업일, 가정) 안")
        short = short or "처리 기간 부족"
    if reasons:
        nxt = adjust_to_business_day(next_occurrence(day, add_months(date(due.year, due.month, 1), 1)))
        return nxt, f"{short} → 다음 주기", " · ".join(reasons) + " → 다음 주기"
    return due, "급여일 뒤 출금 → 이번 주기", "출금일이 첫 급여일 뒤이고 처리 기간도 충분 → 이번 주기"
