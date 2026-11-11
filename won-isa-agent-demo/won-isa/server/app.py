"""WON이사 데모 서버. 실행: uvicorn server.app:app --port 8000  →  http://localhost:8000

데모와 운영의 경계(자세한 그림: /boundary.html)
- 사용자 세션: 브라우저 쿠키별로 Agent·세션을 따로 둔다(메모리 보관, 프로세스가 꺼지면 사라짐).
  운영에서는 로그인 세션·고객 식별자에 묶인 저장소가 이 자리에 온다.
- 인증: server/auth.py의 Authenticator. 데모는 MockAuthenticator(본인 확인 없음).
- 기관 연동: server/adapters.py의 InstitutionAdapter. 데모는 DemoSimulator(사전 정의 시나리오).
- 감사 로그: server/audit.py의 해시 체인 이벤트 로그(JSONL). 운영에서는 변경 불가 저장소로 보낸다.
"""
from __future__ import annotations

import json
import re
import secrets
from collections import OrderedDict
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # .env가 있으면 읽는다(LLM 키 등)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from . import coach as CO, tools as T
from .agent import Agent
from .rules import fmt

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="WON이사 Agent 데모")

COOKIE = "won_sid"
MAX_SESSIONS = 50
SESSIONS: "OrderedDict[str, Agent]" = OrderedDict()  # 세션 id -> Agent (데모: 메모리)

PRESETS = {"2w": date(2026, 12, 9), "30d": date(2026, 12, 15), "1w": date(2026, 12, 22)}  # 30d = 기준일(11.16)부터 30일째 · 1w = 정착 후 미션 1주


def _sid(request: Request, response: Response) -> str:
    """세션 id. 데모는 로그인 없이 쿠키로만 구분한다(운영: 로그인 세션)."""
    sid = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        sid = secrets.token_hex(8)
        response.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
    return sid


def _new(sid: str) -> Agent:
    s = T.Session(sid=sid)
    a = Agent(s)
    SESSIONS[sid] = a
    SESSIONS.move_to_end(sid)
    while len(SESSIONS) > MAX_SESSIONS:
        SESSIONS.popitem(last=False)
    s.log("SESSION_STARTED", "system", session=sid, authenticator=s.auth.name, adapter=s.adapter.name,
          llm=a.llm.name, model=getattr(a.llm, "model", None))
    return a


def _agent(sid: str = Depends(_sid)) -> Agent:
    return SESSIONS.get(sid) or _new(sid)


def _state(a: Agent) -> dict:
    s = a.session
    p = s.plan
    v = s.audit.verify()
    return {
        "mode": a.llm.name,
        "model": getattr(a.llm, "model", None),
        "today": f"{s.today.year}.{fmt(s.today)}",
        "scene": a.scene,
        "subtitle": a.subtitle,
        "goal": "이사 완료 · 출금 실패 0건",
        "plan_version": p["version"] if p else None,
        "plan_hash": p["hash"] if p else None,
        "approval": T._approval_status(s),
        # 감사 로그만 보고 센 값: 승인 이벤트 없이 나간 실행 요청 수
        "executed_without_approval": s.audit.exec_without_approval(),
        "blocked": s.counters["blocked"],
        "dup_prevented": s.counters["dup_prevented"],
        "item_states": {s.items[i]["label"]: T.STATES[st] for i, st in s.item_state.items() if i in s.items},
        "totals": a.totals,
        "session": s.sid[:6],
        "case_id": s.audit.case_id,
        "audit": {"count": v["count"], "ok": v["ok"], "head": v["head"][:12]},
        "boundary": {"auth": s.auth.name, "auth_mock": s.auth.mock, "adapter": s.adapter.name,
                     "adapter_simulated": s.adapter.simulated},
        "confirm_pending": sum(1 for i in s.items.values() if i["confidence"] == "check" and i["id"] not in s.decisions),
        # 정착 단계(추천 Agent)
        "stage": s.stage,
        "consent": dict(s.consent),
        "persona": CO.persona_info(s.persona),
        "goal": s.goal,
        "missions": s.missions,
        "honey": CO.ledger(s).summary(),
        "reco_products": [x["id"] for x in (s.reco or {}).get("products", [])],
    }


class Chat(BaseModel):
    message: str


class Approve(BaseModel):
    plan_hash: str


class Advance(BaseModel):
    preset: str


class Persona(BaseModel):
    type_id: str


class Goal(BaseModel):
    goal: str


class Mission(BaseModel):
    mission_id: str


class Skin(BaseModel):
    skin_id: str


class Decide(BaseModel):
    item_id: str
    decision: str  # confirm | include | exclude


@app.post("/api/reset")
def reset(sid: str = Depends(_sid)):
    a = _new(sid)
    return {"ok": True, "state": _state(a)}


@app.post("/api/chat")
def chat(body: Chat, a: Agent = Depends(_agent)):
    out = a.run_turn(body.message.strip()[:500])
    return out | {"state": _state(a)}


@app.post("/api/approve")
def approve(body: Approve, a: Agent = Depends(_agent)):
    """고객 승인. 인증 어댑터(데모: 모의 인증)가 발급한 토큰을 현재 계획 해시에 묶는다. LLM은 이 경로에 없다."""
    s = a.session
    if not s.plan:
        raise HTTPException(400, "승인할 계획이 없습니다")
    g = T.grant_approval(s, body.plan_hash)
    if not g["ok"]:
        if g["reason"] != "stale":
            raise HTTPException(401, g["reason"])
        log = [{"kind": "block", "title": "승인 거부", "sub": "화면의 계획 버전이 현재 계획과 다름 → 새로고침 후 재승인"}]
        return {"before": ["화면의 계획이 최신이 아니에요. 바뀐 계획을 다시 확인해 주세요."], "after": [], "cards": [],
                "tool_count": 0, "log": log, "state": _state(a)}
    how = "모의 인증 완료(운영: 본인 인증·전자서명)" if s.auth.mock else "본인 인증 완료"
    pre = [{"kind": "approve", "title": f"고객 승인 · 계획 v{g['version']}",
            "sub": f"{how} · 승인 토큰을 해시 {g['plan_hash'][:6]}에 묶음"}]
    if g["confirmed"]:
        pre.append({"kind": "approve", "title": "확인 필요 항목 확정",
                    "sub": ", ".join(s.items[i]["label"] for i in g["confirmed"]) + " → 승인 화면에서 고객이 확인"})
    # 실행은 고객 승인 이벤트가 트리거하고 시스템이 승인 토큰으로 수행한다. LLM은 결과 설명만(도구 없음).
    out = a.run_turn(f"[시스템 이벤트] 고객 승인 완료: 계획 v{g['version']} · 시스템이 승인 토큰으로 실행함",
                     forced=("execute_plan", {}), allow_tools=False)
    out["log"] = pre + out["log"]
    return out | {"state": _state(a)}


@app.post("/api/items/decide")
def decide(body: Decide, a: Agent = Depends(_agent)):
    """고객이 탐지 항목을 항목별로 확정·포함·제외한다. 계획이 바뀌면 새 버전이 되고 기존 승인은 무효가 된다."""
    s = a.session
    r = T.decide_item(s, body.item_id, body.decision)
    if "error" in r:
        raise HTTPException(400, r["error"])
    word = {"confirm": "확정", "include": "포함", "exclude": "제외"}[body.decision]
    pre = [{"kind": "approve", "title": f"고객 결정 · {r['item']} {word}", "sub": "항목별 확정·포함·제외는 고객이 화면에서 정함(LLM 경로 밖)"}]
    if s.plan and r["plan_affected"]:
        out = a.run_turn(f"[시스템 이벤트] 고객 항목 결정: {r['item']} {word} · 시스템이 계획을 다시 계산함",
                         forced=("plan_schedule", {}), allow_tools=False)
        out["log"] = pre + out["log"]
    else:
        out = {"before": [f"{r['item']} 항목을 '{word}'(으)로 표시했어요."], "after": [], "cards": [],
               "tool_count": 0, "log": pre, "usage": {"prompt": 0, "completion": 0}, "latency": 0, "guard_ok": True}
    out["cards"] = out["cards"] + [T.items_card(s) | {"open": True}]
    return out | {"state": _state(a)}


@app.post("/api/advance")
def advance(body: Advance, a: Agent = Depends(_agent)):
    s = a.session
    if body.preset not in PRESETS:
        raise HTTPException(400, "preset은 2w 또는 30d")
    target = PRESETS[body.preset]
    if target <= s.today:
        raise HTTPException(400, "이미 지난 날짜입니다")
    if body.preset == "1w":
        return _mission_week(a, target)
    changes = T.advance_to(s, target)
    src = f"{s.adapter.name}({'모의' if s.adapter.simulated else '연동'})"
    pre = [{"kind": "event", "title": f"시간 진행 → {fmt(target)}", "sub": f"상태 변화 {len(changes)}건 · 결과 수신: {src}"}]
    pre += [{"kind": "observe" if c["kind"] == "observed" else "event",
             "title": f"{fmt(date.fromisoformat(c['date']))} {c['item']} → {c['state']}", "sub": c["why"]} for c in changes]
    if body.preset == "30d" and s.stage != "settle":
        s.stage = "settle"
        s.log("STAGE_CHANGED", "system", to="settle", why="이사 30일째 · 정착 단계 도구로 전환")
        pre.append({"kind": "event", "title": "정착 단계로 전환", "sub": "이사 계획·승인·실행 도구는 숨김 · 상태 조회·추천 도구만"})
        pay = CO.grant_move_reward(s)
        if pay and pay["granted"]:
            pre.append({"kind": "approve", "title": f"꿀머니 {pay['granted']:,} 지급", "sub": "급여 이사 완료(공식 결과) · 시스템 지급 · 1회만"})
    msg = "[시스템 이벤트] 이사 30일째: 정착 현황을 확인하세요" if body.preset == "30d" else f"[시스템 이벤트] {fmt(target)} 기준 상태 변화 {len(changes)}건"
    out = a.run_turn(msg)
    out["log"] = pre + out["log"]
    out["divider"] = "2주 뒤 · " + fmt(target) if body.preset == "2w" else None
    return out | {"state": _state(a)}


def _mission_week(a: Agent, target: date):
    s = a.session
    if s.stage != "settle":
        raise HTTPException(400, "정착 단계가 아닙니다")
    start = s.today
    T.advance_to(s, target)
    res = CO.judge_missions(s, start, target)
    pre = [{"kind": "event", "title": f"시간 진행 → {fmt(target)}", "sub": f"미션 1주 · 판정 {len(res)}건 · 거래내역(모의) 기준"}]
    for r in res:
        pay = r["reward"]
        pre.append({"kind": "observe" if r["achieved"] else "block", "title": f"{r['title']} · {'달성' if r['achieved'] else '미달성'}",
                    "sub": r["rule"] + (f" → 꿀머니 {pay['granted']:,} 지급" if pay and pay["granted"] else "")})
    out = a.run_turn(f"[시스템 이벤트] 미션 결과: 판정 {len(res)}건 · 시스템이 꿀머니 지급", forced=("get_reward_status", {}),
                     allow_tools=False)
    out["log"] = pre + out["log"]
    out["divider"] = "미션 1주 뒤 · " + fmt(target)
    return out | {"state": _state(a)}


def _reply(a: Agent, before: str, log: list, cards: list | None = None) -> dict:
    return {"before": [before], "after": [], "cards": cards or [], "tool_count": 0, "log": log,
            "usage": {"prompt": 0, "completion": 0}, "latency": 0, "guard_ok": True, "state": _state(a)}


@app.post("/api/consent")
def consent(a: Agent = Depends(_agent)):
    """고객이 소비 분석·추천에 동의(이사 동의와 별도). 동의 후 시스템이 분석하고 LLM은 결과만 설명한다."""
    s = a.session
    if s.stage != "settle":
        raise HTTPException(400, "정착 단계에서만 동의할 수 있습니다")
    CO.grant_consent(s)
    pre = [{"kind": "approve", "title": "고객 동의 · 소비 분석·맞춤 추천", "sub": "카드 결제 최근 90일 · 이사 동의와 별도 · 언제든 철회"}]
    out = a.run_turn("[시스템 이벤트] 고객 소비 분석 동의 · 시스템이 분석함", forced=("analyze_spending", {}), allow_tools=False)
    out["log"] = pre + out["log"]
    return out | {"state": _state(a)}


@app.post("/api/persona")
def persona(body: Persona, a: Agent = Depends(_agent)):
    """고객이 담당 캐릭터를 수락(또는 다른 캐릭터 선택). 이사 대화를 요약으로 압축하고 담당을 넘긴다."""
    s = a.session
    r = CO.set_persona(s, body.type_id)
    if not r["ok"]:
        raise HTTPException(400, r["reason"])
    first = r["previous"] is None
    pre = [{"kind": "approve", "title": f"고객 선택 · 담당 캐릭터 {r['persona']['name']}", "sub": f"{r['persona']['type']} · 화면 조작(LLM 경로 밖)"}]
    if first:
        a.handoff()
        pre.append({"kind": "event", "title": "담당 넘기기 · 대화 압축", "sub": "이사 대화 → 상태 요약 1개 · 숫자 허용 목록 초기화 · 규칙·가드는 그대로"})
    out = a.run_turn(f"[시스템 이벤트] 담당 캐릭터: {r['persona']['name']}({r['persona']['type']}) · 고객에게 인사하고 목표를 물을 것",
                     allow_tools=False)
    out["log"] = pre + out["log"]
    if not s.goal:
        out["cards"] = out["cards"] + [{"type": "goal", "options": CO.CT.GOALS}]
    return out | {"state": _state(a)}


@app.post("/api/goal")
def goal(body: Goal, a: Agent = Depends(_agent)):
    s = a.session
    r = CO.choose_goal(s, body.goal)
    if not r["ok"]:
        raise HTTPException(400, r["reason"])
    pre = [{"kind": "approve", "title": f"고객 선택 · 목표 '{body.goal}'", "sub": "목표는 고객이 고름(소비 특징만으로 정하지 않음)"}]
    out = a.run_turn(f"[시스템 이벤트] 고객 목표 선택: {body.goal} · 시스템이 추천을 계산함",
                     forced=("recommend_for_goal", {"goal": body.goal}), allow_tools=False)
    out["log"] = pre + out["log"]
    return out | {"state": _state(a)}


@app.post("/api/missions/join")
def mission_join(body: Mission, a: Agent = Depends(_agent)):
    s = a.session
    r = CO.join_mission(s, body.mission_id)
    if not r["ok"]:
        raise HTTPException(400, r["reason"])
    pre = [{"kind": "approve", "title": f"고객 참여 · {r['mission']['title']}", "sub": "미션 참여는 화면 버튼으로만 · 판정·지급은 시스템"}]
    if r.get("already"):
        return _reply(a, "이미 참여 중인 미션이에요.", pre)
    out = a.run_turn(f"[시스템 이벤트] 미션 참여: {r['mission']['title']}", allow_tools=False)
    out["log"] = pre + out["log"]
    out["cards"] = out["cards"] + [{"type": "next", "label": "일주일 뒤 미션 결과 보기", "preset": "1w"}]
    return out | {"state": _state(a)}


@app.post("/api/skins/buy")
def skin_buy(body: Skin, a: Agent = Depends(_agent)):
    s = a.session
    r = CO.buy_skin(s, body.skin_id)
    names = {x["id"]: x["name"] for x in CO.CT.SKINS}
    nm = names.get(body.skin_id, body.skin_id)
    if r["status"] == "PURCHASED":
        log = [{"kind": "approve", "title": f"고객 구매 · {nm}", "sub": f"꿀머니 {r['price']:,} 사용 · 잔액 {r['balance']:,} · 현금 유출 없음"}]
        msg = f"{nm}로 꾸몄어요!"
    elif r["status"] == "INSUFFICIENT":
        log = [{"kind": "block", "title": "구매 거부 · 꿀머니 부족", "sub": f"가격 {r['price']:,} · 잔액 {r['balance']:,}"}]
        msg = "꿀머니가 조금 모자라요. 미션으로 더 모아 봐요."
    elif r["status"] == "ALREADY_OWNED":
        log = [{"kind": "approve", "title": f"착용 · {nm}", "sub": "이미 가진 스킨"}]
        msg = f"{nm}로 바꿨어요."
    else:
        raise HTTPException(400, "알 수 없는 스킨")
    return _reply(a, msg, log, [{"type": "rewards", **CO.get_reward_status(s)}])


@app.get("/api/state")
def state(a: Agent = Depends(_agent)):
    return _state(a)


@app.get("/api/audit")
def audit(a: Agent = Depends(_agent)):
    """이 세션의 감사 이벤트 로그와 체인 검증 결과."""
    log = a.session.audit
    return {"case_id": log.case_id, "verify": log.verify(), "exec_without_approval": log.exec_without_approval(),
            "file": str(log.path.relative_to(ROOT)) if log.path and ROOT in log.path.parents else None,
            "events": log.events}


@app.get("/api/eval")
def eval_results():
    """저장된 평가 결과(eval/results_*.json, eval/detect_results.json)를 그대로 돌려준다."""
    out = {"runs": [], "detect": None}
    for f in sorted((ROOT / "eval").glob("results_*.json")):
        try:
            out["runs"].append(json.loads(f.read_text(encoding="utf-8")) | {"file": f"eval/{f.name}"})
        except (OSError, ValueError):
            continue
    d = ROOT / "eval" / "detect_results.json"
    if d.is_file():
        try:
            out["detect"] = json.loads(d.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return out


app.mount("/assets", StaticFiles(directory=ROOT / "web" / "assets"), name="assets")


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/{name}")
def web_file(name: str):
    f = ROOT / "web" / name
    if f.is_file() and f.suffix in (".js", ".css", ".html"):
        return FileResponse(f)
    raise HTTPException(404)
