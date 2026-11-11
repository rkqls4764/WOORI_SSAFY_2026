"""정착 단계(추천 Agent) 도구와 고객 화면 조작.

Agent는 하나다. 이사가 끝나 정착 단계(30일째)에 들어가면 쓰는 도구 묶음이 바뀌고,
고객이 수락하면 대화의 '얼굴'이 소비 유형에 맞는 담당 캐릭터로 바뀐다(페르소나). 규칙과 가드는 그대로다.

- LLM이 부를 수 있는 도구: analyze_spending, recommend_for_goal, get_reward_status (+ 이사 상태 조회·후속 조치)
- 고객 화면 조작으로만 일어나는 일(LLM 도구 아님): 소비 분석 동의, 담당 캐릭터 수락, 목표 선택, 미션 참여, 스킨 구매
- 시스템이 판정·지급: 미션 달성 판정, 꿀머니 지급(중복 방지·한도·감사 로그)
- 이사 계획·승인 토큰과 완전히 분리: 여기서는 계획을 바꾸지 않는다(계획 해시 불변)
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import catalog as CT, data, rewards as RW, spending as SP, tools as T

CHAR_IMG = {"FOOD": "dalbo", "VALUE": "wibee", "SHOP": "bombom", "SPORT": "duji", "SAVE": "ku", "HOBBY": "pudding"}
MOVE_TOOLS = ["get_transactions", "detect_recurring", "calc_switch_quote", "find_items", "plan_schedule",
              "prepare_approval", "execute_plan", "get_move_status", "set_followup", "get_settlement_status"]
SETTLE_TOOLS = ["get_move_status", "set_followup", "get_settlement_status",
                "analyze_spending", "recommend_for_goal", "get_reward_status"]


def ledger(s) -> RW.Ledger:
    if s.ledger is None:
        s.ledger = RW.Ledger(log=s.log)
    return s.ledger


def persona_info(type_id: str | None) -> dict | None:
    if not type_id:
        return None
    t = SP.TYPES[type_id]
    return {"id": type_id, "name": t["character"], "type": t["type"], "img": f"/assets/chars/{CHAR_IMG[type_id]}.png"}


def characters() -> list:
    return [persona_info(k) for k in SP.TYPES]


def schemas_for(s) -> list:
    names = SETTLE_TOOLS if s.stage == "settle" else MOVE_TOOLS
    return [x for x in T.TOOL_SCHEMAS if x["function"]["name"] in names]


GOAL_RE = {"식비 절약": r"식비|외식|배달|절약|아끼|줄이", "저축": r"저축|모으|모아|저금|적금.{0,4}(들|가입|추천|하나|만들)"}


def goal_from_text(text: str) -> str | None:
    """고객 발화에 나온 목표(둘 중 하나만 분명할 때). 고객이 말하지 않은 목표는 만들지 않는다."""
    t = (text or "").replace(" ", "")
    hits = [g for g, rx in GOAL_RE.items() if re.search(rx, t)]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- LLM 도구
def analyze_spending(s) -> dict:
    if not s.consent.get("spending"):
        return {"error": "소비 분석 동의가 없어 분석하지 않음. 고객에게 화면의 '동의하고 분석하기'를 눌러 달라고 안내할 것",
                "need_consent": True}
    a = s.analysis or SP.analyze(data.TRANSACTIONS, data.TODAY)
    if s.analysis is None:
        s.analysis = a
        s.log("SPENDING_ANALYZED", "system", type=a["match"]["id"], why=a["match"]["why"], period=a["period"])
    return a | {"recommended_character": persona_info(a["match"]["id"]), "characters": characters(),
                "persona": persona_info(s.persona)}


def recommend_for_goal(s, goal: str | None = None) -> dict:
    if not s.consent.get("spending"):
        return analyze_spending(s)
    a = s.analysis or analyze_spending(s)
    said = goal_from_text(s.last_user)
    # 목표 근거 가드: 목표는 고객이 고른다. 화면에서 고른 목표나 이번 발화에 나온 목표만 인정.
    if goal not in CT.GOALS or (goal != s.goal and goal != said):
        if said and goal is None:
            goal = said
        else:
            s.log("GOAL_REQUIRED", "system", proposed=goal, said=said, chosen=s.goal)
            return {"error": "고객이 고른 목표가 없음. 추천하지 않음. 식비 절약·저축 중 무엇을 원하는지 고객에게 물을 것",
                    "need_goal": True, "options": CT.GOALS, "proposed": goal}
    if s.goal != goal:
        s.goal = goal
        s.log("GOAL_SET", "customer", goal=goal, via="대화")
    r = CT.recommend(goal, a)
    s.reco = r
    s.log("RECOMMENDED", "system", goal=goal, products=[p["id"] for p in r["products"]],
          missions=[m["id"] for m in r["missions"]])
    return r | {"joined": sorted(s.missions)}


def get_reward_status(s) -> dict:
    L = ledger(s)
    return {"ledger": L.summary(), "missions": [{"id": k, **v} for k, v in s.missions.items()],
            "skins": [sk | {"owned": sk["id"] in L.owned} for sk in CT.SKINS],
            "entries": L.entries[-6:], "persona": persona_info(s.persona),
            "rules": ["지급·판정은 시스템", "스킨 구매는 고객 화면에서만", f"1인 누적 한도 {RW.CAP:,}꿀"]}


# ---------------------------------------------------------------- 고객 화면 조작(LLM 경로 밖)
def grant_consent(s) -> dict:
    if not s.consent.get("spending"):
        s.consent["spending"] = True
        s.log("CONSENT_GRANTED", "customer", purpose="소비 분석·맞춤 추천", scope="카드 결제 내역 최근 90일",
              separate_from="이사(주거래 이전) 동의", revocable=True)
    return {"ok": True}


def set_persona(s, type_id: str) -> dict:
    if type_id not in SP.TYPES:
        return {"ok": False, "reason": "알 수 없는 캐릭터"}
    if not s.consent.get("spending"):
        return {"ok": False, "reason": "소비 분석 동의 필요"}
    prev = s.persona
    s.persona = type_id
    matched = (s.analysis or {}).get("match", {}).get("id")
    s.log("PERSONA_ASSIGNED", "customer", persona=type_id, character=SP.TYPES[type_id]["character"],
          matched=matched, chosen_other=bool(matched and matched != type_id), previous=prev)
    return {"ok": True, "persona": persona_info(type_id), "previous": persona_info(prev)}


def choose_goal(s, goal: str) -> dict:
    if goal not in CT.GOALS:
        return {"ok": False, "reason": "알 수 없는 목표"}
    s.goal = goal
    s.log("GOAL_SET", "customer", goal=goal, via="화면")
    return {"ok": True, "goal": goal}


def join_mission(s, mission_id: str) -> dict:
    m = next((x for x in (s.reco or {}).get("missions", []) if x["id"] == mission_id), None)
    if not m:
        return {"ok": False, "reason": "추천된 미션이 아님"}
    if mission_id in s.missions:
        return {"ok": True, "already": True, "mission": s.missions[mission_id]}
    s.missions[mission_id] = {"title": m["title"], "target": m["target"], "reward": m["reward"],
                              "period": CT.MISSIONS[mission_id]["period"], "start": s.today.isoformat(), "status": "참여 중"}
    s.log("MISSION_JOINED", "customer", mission=mission_id, title=m["title"], target=m["target"], via="화면 버튼")
    return {"ok": True, "mission": s.missions[mission_id]}


def buy_skin(s, skin_id: str) -> dict:
    sk = next((x for x in CT.SKINS if x["id"] == skin_id), None)
    if not sk:
        return {"status": "UNKNOWN"}
    return ledger(s).buy_skin(sk, s.today.isoformat())


# ---------------------------------------------------------------- 시스템 판정·지급
def grant_move_reward(s) -> dict | None:
    """이사 핵심(급여) 이전이 공식 결과로 완료됐을 때만 1회 지급."""
    if s.item_state.get("SAL") != "DONE":
        return None
    return ledger(s).earn("MOVE_SALARY_DONE", CT.MOVE_DONE_REWARD, "급여 이사 완료(공식 결과)", s.today.isoformat())


def judge_missions(s, start: date, end: date) -> list:
    """[start, end) 기간의 거래내역(모의)으로 참여 중인 미션을 판정하고 달성하면 꿀머니 지급."""
    txs = data.mission_week_txs(start)
    out = []
    for mid, m in s.missions.items():
        if m["status"] != "참여 중" or m.get("period") != "주":  # 월 단위 미션은 월말에 판정
            continue
        res = CT.judge({"id": mid, **m}, txs, start, end)
        m["status"] = "달성" if res["achieved"] else "미달성"
        m["result"] = res
        s.log("MISSION_JUDGED", "system", mission=mid, achieved=res["achieved"], measured=res["measured"], rule=res["rule"])
        pay = ledger(s).earn(f"{mid}:{start.isoformat()}", m["reward"], m["title"], end.isoformat()) if res["achieved"] else None
        out.append({"mission": mid, "title": m["title"], **res, "reward": pay})
    return out


def compact_summary(s) -> str:
    """담당 캐릭터로 넘길 때 이사 대화를 대신하는 짧은 상태 요약(문맥 절약 · 이전 숫자 허용 목록 초기화)."""
    st = {s.items[i]["label"]: T.STATES[v] for i, v in s.item_state.items() if i in s.items}
    done = [k for k, v in st.items() if v == "완료"]
    other = [f"{k} {v}" for k, v in st.items() if v not in ("완료", "계획")]
    a = s.analysis["match"] if s.analysis else None
    p = s.plan
    lines = [f"[이사 요약] 계획 v{p['version']} 고객 승인 후 신청함." if p else "[이사 요약] 이사 계획 없음.",
             f"완료: {', '.join(done) or '없음'}.", f"그 밖의 항목: {', '.join(other) or '없음'}.",
             "A은행 적금은 이사 항목(만기까지 유지)이고, 추천 상품(우리 SUPER주거래 적금)과는 다르다."]
    if a:
        lines.append(f"소비 분석: {a['type']} → {a['character']}({a['why']}).")
    return " ".join(lines)


# ---------------------------------------------------------------- 도구 등록
T.TOOL_IMPLS.update({"analyze_spending": analyze_spending, "recommend_for_goal": recommend_for_goal,
                     "get_reward_status": get_reward_status})
T.TOOL_SCHEMAS.extend([
    T._fn("analyze_spending", "고객이 동의한 범위의 카드 결제를 시스템이 업종별로 분석하고 소비 유형·담당 캐릭터를 제안한다. 동의가 없으면 need_consent."),
    T._fn("recommend_for_goal", "고객이 고른 목표(식비 절약 | 저축)에 맞는 예금성 상품과 미션을 시스템이 계산한다. '적금 들고 싶어'·'돈 모으고 싶어' → 저축, '식비·외식·배달 줄이고 싶어' → 식비 절약. 목표가 드러나지 않았으면 need_goal.",
          {"goal": {"type": "string", "enum": CT.GOALS, "description": "고객이 고른 목표"}}),
    T._fn("get_reward_status", "꿀머니 잔액·지급 내역·참여 미션·스킨 목록을 조회한다(지급·구매는 하지 않음). 꿀머니를 물으면 매번 새로 부른다(대화 속 이전 값은 지났을 수 있음)."),
])
