"""추천 Agent의 상품·미션·스킨 목록과 목표별 추천(시스템 계산).

상품은 예금성만(팀 결정, 11-11). 상품 구조는 우리은행 공식 상품 페이지에서 확인(2026-11-11).
금리는 공식 페이지에 '금리보기'로만 있어 숫자로 넣지 않는다 → 화면에는 '가입 시점 고시 금리 확인'.
"""
from __future__ import annotations

import math

AS_OF = "2026-11-11"
PRODUCTS = [
    {"id": "SUPER_FREE", "name": "우리 SUPER주거래 자유적금", "kind": "자유적립식 적금",
     "terms": "1·2·3년 · 매월 50만 원 이하 · 개인 및 개인사업자", "goals": ["식비 절약", "저축"],
     "fit": {"식비 절약": "아낀 식비를 그때그때 넣기 좋은 자유적립식", "저축": "금액이 들쭉날쭉해도 넣을 수 있음"},
     "rate": "가입 시점 고시 금리 확인(주거래 고객 우대금리 있음)",
     "link_to_move": "이사로 급여를 옮기면 주거래 우대 조건을 채울 가능성(정확한 조건은 상품설명서)",
     "url": "https://spot.wooribank.com/pot/Dream?PRD_CD=P010000110&PRD_YN=Y&withyou=PODEP0001"},
    {"id": "SUPER_FIXED", "name": "우리 SUPER주거래 적금", "kind": "적금",
     "terms": "1·2·3년 · 매월 50만 원 이하 · 개인 및 개인사업자", "goals": ["저축"],
     "fit": {"저축": "매달 정한 금액을 꾸준히 모으는 목표에 맞음"},
     "rate": "가입 시점 고시 금리 확인(주거래 고객 우대금리 있음)",
     "link_to_move": "이사로 급여를 옮기면 주거래 우대 조건을 채울 가능성(정확한 조건은 상품설명서)",
     "url": "https://spot.wooribank.com/pot/Dream?PRD_CD=P010000109&PRD_YN=Y&withyou=PORMG0031"},
]
GOALS = ["식비 절약", "저축"]
MISSIONS = {
    "M_DELIVERY": {"name": "배달 줄이기", "goal": "식비 절약", "period": "주", "reward": 100},
    "M_LEFTOVER": {"name": "아낀 식비 저금", "goal": "식비 절약", "period": "주", "reward": 100},
    "M_MONTHLY": {"name": "매달 정한 금액 저금", "goal": "저축", "period": "월", "reward": 300},
}
MOVE_DONE_REWARD = 1_000  # 이사 완료 꿀머니(가정)
SKINS = [
    {"id": "SK_CHEF", "name": "달보 셰프 모자", "price": 300},
    {"id": "SK_RAIN", "name": "노란 우비", "price": 500},
    {"id": "SK_NIGHT", "name": "봄밤 배경", "price": 800},
    {"id": "SK_HONEY", "name": "황금 벌집 액자", "price": 1_500},
]
WEEKS_PER_MONTH = 4.3


def recommend(goal: str | None, analysis: dict) -> dict:
    """고객이 고른 목표에 맞는 예금성 상품과 미션. 목표가 없으면 추천하지 않고 목표를 묻는다."""
    if goal not in GOALS:
        return {"need_goal": True, "options": GOALS,
                "why": "소비 특징만으로 목표를 정하지 않음 → 고객이 목표를 고르면 추천"}
    products = [{k: p[k] for k in ("id", "name", "kind", "terms", "rate", "link_to_move", "url")} | {"fit": p["fit"][goal]}
                for p in PRODUCTS if goal in p["goals"]]
    missions = []
    food = sum(r["monthly_avg"] for r in analysis["by_category"] if r["category"] in ("외식·배달", "카페"))
    d = analysis["delivery"]
    if goal == "식비 절약":
        target = max(0, math.ceil(d["weekly_avg"]) - 1)
        save = int(round(max(0, d["weekly_avg"] - target) * d["avg_order"] * WEEKS_PER_MONTH, -3))
        missions.append({"id": "M_DELIVERY", "title": f"배달 주 {target}회 이하", "reward": MISSIONS["M_DELIVERY"]["reward"], "target": target,
                         "effect_monthly": save,
                         "calc": f"지금 주 {d['weekly_avg']}회 · 1건 평균 {d['avg_order']:,}원 → 주 {target}회로 줄이면 월 약 {save:,}원 덜 씀(주문 금액 유지 가정)"})
        budget = int(round(food * 0.9, -4))
        missions.append({"id": "M_LEFTOVER", "title": f"식비 예산 월 {budget:,}원 안에서 쓰고 남은 돈 저금",
                         "reward": MISSIONS["M_LEFTOVER"]["reward"], "effect_monthly": food - budget, "target": 10_000,
                         "calc": f"최근 외식·카페 월평균 {food:,}원의 90%를 예산으로 · 남는 돈은 자유적금에"})
    else:
        monthly = min(500_000, int(round(analysis["card_monthly_avg"] * 0.1, -4)))
        missions.append({"id": "M_MONTHLY", "title": f"매달 {monthly:,}원 저금", "reward": MISSIONS["M_MONTHLY"]["reward"], "target": monthly,
                         "effect_monthly": monthly,
                         "calc": f"카드 지출 월평균 {analysis['card_monthly_avg']:,}원의 10% 제안 · 금액은 고객이 바꿀 수 있음"})
    return {"goal": goal, "products": products, "missions": missions,
            "rules": ["예금성 상품만 추천", "목표는 고객이 고름", "미션 참여는 고객 버튼으로만", "꿀머니 지급은 시스템 판정 후"],
            "as_of": AS_OF}


def judge(mission: dict, txs, start, end) -> dict:
    """미션 달성 판정(시스템). 기간 [start, end)의 거래내역만 본다. LLM은 판정하지 않는다."""
    from datetime import date as _d
    from .spending import DELIVERY
    period = [t for t in txs if start <= _d.fromisoformat(t.date) < end]
    if mission["id"] == "M_DELIVERY":
        n = sum(1 for t in period if t.counterparty in DELIVERY and t.amount < 0)
        return {"achieved": n <= mission["target"], "measured": n, "rule": f"배달앱 결제 {n}건 ≤ 목표 {mission['target']}건"}
    saved = sum(-t.amount for t in period if t.channel == "적금이체")
    return {"achieved": saved >= mission["target"], "measured": saved,
            "rule": f"적금 납입 {saved:,}원 ≥ 목표 {mission['target']:,}원"}
