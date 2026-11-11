"""추천 Agent 시스템 통제 점검(LLM 무관). 실행: python -m eval.reco_controls → eval/reco_controls.json"""
from __future__ import annotations

import json
from pathlib import Path

from server import catalog as CT, data, rewards as RW, spending as SP


def run() -> dict:
    out = {}
    a = SP.analyze(data.TRANSACTIONS, data.TODAY)
    out["김우리 → 외식·미식형(달보), 근거 표시"] = a["match"]["character"] == "달보" and "평균의" in a["match"]["why"]
    out["목표 없이 추천 안 함(목표를 물음)"] = CT.recommend(None, a).get("need_goal") is True
    recs = [CT.recommend(g, a) for g in CT.GOALS]
    out["예금성 상품만 추천"] = all(p["id"] in {"SUPER_FREE", "SUPER_FIXED"} for r in recs for p in r["products"])
    out["추천 숫자는 소비 데이터로 계산"] = all(m.get("calc") for r in recs for m in r["missions"])
    ev = []
    L = RW.Ledger(lambda *x, **k: ev.append(x[0]))
    L.earn("MOVE_DONE", 1_000, "이사 완료", "2026-12-16")
    out["보상 중복 지급 방지"] = L.earn("MOVE_DONE", 1_000, "이사 완료", "2026-12-16")["status"] == "DUPLICATE" and L.issued == 1_000
    out["잔액 부족 시 스킨 구매 거부"] = L.buy_skin({"id": "SK_HONEY", "name": "황금 벌집 액자", "price": 1_500}, "d")["status"] == "INSUFFICIENT"
    L.earn("BIG", 100_000, "한도 시험", "d")
    out["1인 누적 한도 초과 지급 차단"] = L.issued == RW.CAP and L.earn("MORE", 100, "한도 후", "d")["status"] == "CAPPED"
    out["모든 지급·구매가 로그에 남음"] = {"REWARD_GRANTED", "REWARD_DUPLICATE_PREVENTED", "SKIN_PURCHASE_REJECTED", "REWARD_CAPPED"} <= set(ev)
    m = next(x for x in recs[0]["missions"] if x["id"] == "M_DELIVERY")
    res = CT.judge(m, data.TRANSACTIONS, data.TODAY, data.TODAY)
    out["미션 판정은 기간 안 거래만"] = res["measured"] == 0
    return out


if __name__ == "__main__":
    r = run()
    for k, v in r.items():
        print(f"  {'통과' if v else '실패'}  {k}")
    print(f"{sum(r.values())}/{len(r)} 통과")
    Path(__file__).with_name("reco_controls.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
