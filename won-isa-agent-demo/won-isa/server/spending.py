"""소비 분석 · 유형 분류 · 위비 캐릭터 매칭 (추천 Agent의 시스템 부분).

숫자는 모두 여기서 계산한다. LLM은 결과를 설명만 한다.
유형 규칙은 팀이 정한 기준이 없어 데모용으로 정한 '가정'이다(RULE_TEXT에 그대로 밝힘).
"""
from __future__ import annotations

from datetime import date, timedelta

# 가맹점 → 업종. 김우리 데이터 + 유형 분류 평가(가상 고객)에 쓰는 가맹점.
CATEGORY = {
    "배달의민족": "외식·배달", "쿠팡이츠": "외식·배달", "맘스터치": "외식·배달", "김밥천국": "외식·배달", "동네 한식당": "외식·배달",
    "스타벅스": "카페", "동네 카페": "카페",
    "GS25": "편의점", "CU": "편의점", "이마트24": "편의점",
    "쿠팡": "쇼핑", "올리브영": "쇼핑", "다이소": "쇼핑", "무신사": "쇼핑", "29CM": "쇼핑",
    "카카오T": "교통", "지하철·버스": "교통",
    "헬스장": "스포츠·레저", "골프연습장": "스포츠·레저", "스포츠용품점": "스포츠·레저",
    "멜론": "음악·취미", "공연 예매": "음악·취미", "서점": "음악·취미",
}
DELIVERY = {"배달의민족", "쿠팡이츠"}

# 슬라이드 8의 6가지 유형과 캐릭터
TYPES = {
    "FOOD": {"type": "외식·미식형", "character": "달보", "cats": ["외식·배달", "카페"]},
    "SHOP": {"type": "쇼핑·트렌드형", "character": "봄봄", "cats": ["쇼핑"]},
    "SPORT": {"type": "스포츠·레저형", "character": "두지", "cats": ["스포츠·레저"]},
    "HOBBY": {"type": "음악·취미형", "character": "푸딩", "cats": ["음악·취미"]},
    "SAVE": {"type": "소액저축·성장형", "character": "쿠", "cats": []},
    "VALUE": {"type": "혜택·실속형", "character": "위비", "cats": []},
}
# 평균 소비 구성(가정). 실제 서비스에서는 같은 연령대의 카드 업종별 평균 비중(카드사·마이데이터 통계)으로 바꾼다.
BASELINE = {"외식·배달": 0.24, "카페": 0.06, "편의점": 0.12, "쇼핑": 0.22, "교통": 0.10,
            "스포츠·레저": 0.06, "음악·취미": 0.06, "기타": 0.14}
INDEX_MIN = 1.5    # 평균보다 1.5배 이상 많이 쓰는 유형 업종이 있으면 그 유형
MARGIN_MIN = 0.05  # 그리고 평균보다 5%p 이상 많아야 함(원래 비중이 작은 업종의 우연한 쏠림 방지)
SAVE_RATIO = 0.5   # 적금 납입 ÷ 카드 지출이 이 이상이면 소액저축·성장형
RULE_TEXT = (f"최근 90일 카드 지출에서 평균 소비 구성(가정)보다 {INDEX_MIN}배 이상이면서 {MARGIN_MIN:.0%}p 이상 많이 쓰는 유형 업종이 있으면 그 유형, "
             f"없으면 적금 납입이 카드 지출의 {SAVE_RATIO:.0%} 이상일 때 소액저축·성장형, 둘 다 아니면 혜택·실속형 "
             "(팀 기준이 없어 데모용으로 정한 가정 · 고객이 다른 캐릭터를 직접 고를 수 있음)")
CARD_CHANNELS = {"체크카드", "신용카드"}


def classify(shares: dict, save_ratio: float) -> dict:
    """업종 비중(dict: 업종 → 0~1)과 적금 비율로 유형을 정한다. 근거 문장을 함께 돌려준다.
    외식처럼 원래 비중이 큰 업종이 늘 이기지 않도록 '평균 대비 몇 배'(지수)로 비교한다."""
    type_share, index = {}, {}
    for k, v in TYPES.items():
        if not v["cats"]:
            continue
        type_share[k] = round(sum(shares.get(c, 0) for c in v["cats"]), 4)
        index[k] = round(type_share[k] / sum(BASELINE[c] for c in v["cats"]), 2)
    ok = {k: i for k, i in index.items()
          if i >= INDEX_MIN and type_share[k] - sum(BASELINE[c] for c in TYPES[k]["cats"]) >= MARGIN_MIN}
    best = max(ok or index, key=(ok or index).get)
    if ok:
        tid = best
        why = f"{'+'.join(TYPES[best]['cats'])} 비중 {type_share[best]:.0%} = 평균의 {index[best]}배 ≥ {INDEX_MIN}배"
    elif save_ratio >= SAVE_RATIO:
        tid = "SAVE"
        why = f"평균보다 뚜렷이 많은 업종 없음(최대 {index[best]}배) · 적금 납입이 카드 지출의 {save_ratio:.0%}"
    else:
        tid = "VALUE"
        why = f"평균보다 뚜렷이 많은 업종 없음(최대 {index[best]}배) · 적금 비율 {save_ratio:.0%} → 기본 유형"
    return {"id": tid, **{k: TYPES[tid][k] for k in ("type", "character")}, "why": why,
            "type_shares": {TYPES[k]["type"]: v for k, v in type_share.items()},
            "type_index": {TYPES[k]["type"]: v for k, v in index.items()}}


def analyze(txs, today: date, days: int = 90) -> dict:
    """최근 days일 카드 지출을 업종별로 나누고 유형·캐릭터를 정한다. txs: data.Tx 목록."""
    start = today - timedelta(days=days)
    card = [t for t in txs if t.channel in CARD_CHANNELS and start <= date.fromisoformat(t.date) < today and t.amount < 0]
    saved = sum(-t.amount for t in txs if t.channel == "적금이체" and start <= date.fromisoformat(t.date) < today)
    total = sum(-t.amount for t in card)
    months = days / 30
    by = {}
    for t in card:
        c = CATEGORY.get(t.counterparty, "기타")
        a = by.setdefault(c, {"amount": 0, "count": 0})
        a["amount"] += -t.amount
        a["count"] += 1
    rows = sorted(({"category": c, "amount": v["amount"], "count": v["count"],
                    "share": round(v["amount"] / total, 4) if total else 0,
                    "monthly_avg": int(round(v["amount"] / months, -2))} for c, v in by.items()),
                  key=lambda r: -r["amount"])
    deliv = [t for t in card if t.counterparty in DELIVERY]
    weeks = days / 7
    shares = {r["category"]: r["share"] for r in rows}
    save_ratio = round(saved / total, 4) if total else 0
    kind = classify(shares, save_ratio)
    return {
        "period": {"from": start.isoformat(), "to": (today - timedelta(days=1)).isoformat(), "days": days},
        "card_total": total,
        "card_monthly_avg": int(round(total / months, -2)),
        "by_category": rows,
        "delivery": {"count": len(deliv), "weekly_avg": round(len(deliv) / weeks, 1),
                     "amount": sum(-t.amount for t in deliv),
                     "avg_order": int(round(sum(-t.amount for t in deliv) / len(deliv), -2)) if deliv else 0},
        "savings_in_period": saved,
        "save_ratio": save_ratio,
        "match": kind,
        "rule": RULE_TEXT,
        "basis": {
            "sources": ["마이데이터 카드 결제 내역(동의 범위) · 가상 데이터"],
            "rules": [RULE_TEXT],
            "assumptions": ["가맹점 이름으로 업종을 나눔(가맹점 업종 코드가 있으면 그것을 씀)", "평균 소비 구성은 가정값(실서비스는 연령대 평균 통계)"],
            "confirm": ["캐릭터는 제안일 뿐 · 고객이 직접 바꿀 수 있음", "소비 특징만으로 목표를 정하지 않음 → 목표는 고객이 고름"],
        },
    }
