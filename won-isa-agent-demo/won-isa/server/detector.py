"""반복 거래 탐지(시스템 규칙). LLM은 관여하지 않는다.

임계값은 이 파일 한 곳에만 둔다. 정확도(정탐·오탐·미탐)는 eval/detect_eval.py가 이 함수를 그대로 불러서 잰다.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from .rules import fmt

KIND = {"급여입금": "급여", "CMS자동납부": "자동납부", "자동송금": "자동송금", "적금이체": "적금", "즉시이체": "직접송금"}

MIN_AMOUNT = 100        # 이 금액 이하(1원 송금 등)는 보지 않음
MIN_COUNT = 3           # 같은 상대·같은 채널로 3회 이상
GAP_MIN, GAP_MAX = 25, 35  # 거래 간격 중앙값(일) → 월 1회 반복으로 봄
ACTIVE_DAYS = 45        # 마지막 거래가 이보다 오래되면 해지된 것으로 봄
MAD_MAX = 3             # 간격의 중앙값 절대편차(일)가 이보다 크면 '날짜 불규칙'
CV_MAX = 0.10           # 금액 변동계수가 이보다 크면 '금액 변동'

RULE_TEXT = (f"같은 상대·같은 채널 {MIN_COUNT}회 이상 · 간격 중앙값 {GAP_MIN}~{GAP_MAX}일 · "
             f"마지막 거래 {ACTIVE_DAYS}일 이내(넘으면 해지 추정)")


def detect(txs, today: date) -> list[dict]:
    """거래 목록 → 월 반복 후보. confidence: high(자동 탐지) | check(고객 확인 필요) | excluded(해지 추정)."""
    groups = defaultdict(list)
    for t in txs:
        if t.channel not in KIND or abs(t.amount) <= MIN_AMOUNT:
            continue  # 원문 적요는 쓰지 않는다
        groups[(t.counterparty, t.channel)].append(t)
    out = []
    for (cp, ch), rows in groups.items():
        if len(rows) < MIN_COUNT:
            continue
        rows = sorted(rows, key=lambda r: r.date)
        ds = [date.fromisoformat(r.date) for r in rows]
        gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
        med = statistics.median(gaps)
        if not GAP_MIN <= med <= GAP_MAX:
            continue
        mad = statistics.median([abs(g - med) for g in gaps])
        amts = [abs(r.amount) for r in rows]
        cv = statistics.pstdev(amts) / statistics.mean(amts)
        kind = KIND[ch]
        last = ds[-1]
        conf, note, code = "high", "", "OK"
        if (today - last).days > ACTIVE_DAYS:
            conf, note, code = "excluded", f"마지막 출금 {fmt(last)} → 해지된 것으로 보고 옮기지 않음", "ENDED"
        elif kind == "직접송금":
            conf, note, code = "check", "직접 보내는 송금·날짜 불규칙 → 고객 확인 필요", "MANUAL"
        elif mad > MAD_MAX:
            conf, note, code = "check", "날짜 불규칙 → 고객 확인 필요", "IRREGULAR_DATE"
        elif cv > CV_MAX:
            conf, note, code = "check", f"금액 변동(변동계수 {cv:.2f}) → 금액은 '확인 필요'로 표시", "VARIABLE_AMOUNT"
        out.append({"counterparty": cp, "channel": ch, "kind": kind, "count": len(rows), "last": last,
                    "last_day": last.day, "median_gap": med, "mad": mad, "cv": cv, "amount": amts[-1],
                    "confidence": conf, "note": note, "code": code})
    return out
