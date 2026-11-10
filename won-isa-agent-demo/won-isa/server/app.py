"""WON이사 데모 서버. 실행: uvicorn server.app:app --port 8000  →  http://localhost:8000"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # .env가 있으면 읽는다(LLM 키 등)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from . import tools as T
from .agent import Agent
from .rules import fmt

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="WON이사 Agent 데모")
STATE: dict = {}

PRESETS = {"2w": date(2026, 12, 9), "30d": date(2026, 12, 15)}  # 30d = 기준일(11.16)부터 30일째


def _new():
    s = T.Session()
    STATE["agent"] = Agent(s)
    STATE["approve_log"] = []


def _agent() -> Agent:
    if "agent" not in STATE:
        _new()
    return STATE["agent"]


def _state() -> dict:
    a = _agent()
    s = a.session
    p = s.plan
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
        "executed_without_approval": s.counters["executed_without_approval"],
        "blocked": s.counters["blocked"],
        "dup_prevented": s.counters["dup_prevented"],
        "item_states": {s.items[i]["label"]: T.STATES[st] for i, st in s.item_state.items() if i in s.items},
        "totals": a.totals,
    }


class Chat(BaseModel):
    message: str


class Approve(BaseModel):
    plan_hash: str


class Advance(BaseModel):
    preset: str


@app.post("/api/reset")
def reset():
    _new()
    return {"ok": True, "state": _state()}


@app.post("/api/chat")
def chat(body: Chat):
    a = _agent()
    out = a.run_turn(body.message.strip()[:500])
    return out | {"state": _state()}


@app.post("/api/approve")
def approve(body: Approve):
    """고객 승인(모의 인증). 현재 계획 해시와 같아야만 승인된다."""
    a = _agent()
    s = a.session
    if not s.plan:
        raise HTTPException(400, "승인할 계획이 없습니다")
    if body.plan_hash != s.plan["hash"]:
        log = [{"kind": "block", "title": "승인 거부", "sub": "화면의 계획 버전이 현재 계획과 다름 → 새로고침 후 재승인"}]
        return {"before": ["화면의 계획이 최신이 아니에요. 바뀐 계획을 다시 확인해 주세요."], "after": [], "cards": [],
                "tool_count": 0, "log": log, "state": _state()}
    s.approval = {"plan_hash": s.plan["hash"], "version": s.plan["version"], "at": s.today.isoformat()}
    for st in s.plan["steps"]:
        for iid in st["items"]:
            if s.item_state.get(iid) == "PLANNED" and st["action"] in ("payroll", "move"):
                s.set_state(iid, "APPROVED", f"고객 승인 v{s.plan['version']}")
    pre = [{"kind": "approve", "title": f"고객 승인 · 계획 v{s.plan['version']}",
            "sub": f"모의 인증 완료 · 승인 토큰을 해시 {s.plan['hash'][:6]}에 묶음"}]
    # 실행은 고객 승인 이벤트가 트리거하고 시스템이 승인 토큰으로 수행한다. LLM은 결과 설명만(도구 없음).
    out = a.run_turn(f"[시스템 이벤트] 고객 승인 완료: 계획 v{s.plan['version']} · 시스템이 승인 토큰으로 실행함",
                     forced=("execute_plan", {}), allow_tools=False)
    out["log"] = pre + out["log"]
    return out | {"state": _state()}


@app.post("/api/advance")
def advance(body: Advance):
    a = _agent()
    s = a.session
    if body.preset not in PRESETS:
        raise HTTPException(400, "preset은 2w 또는 30d")
    target = PRESETS[body.preset]
    if target <= s.today:
        raise HTTPException(400, "이미 지난 날짜입니다")
    changes = T.advance_to(s, target)
    pre = [{"kind": "event", "title": f"시간 진행 → {fmt(target)}", "sub": f"상태 변화 {len(changes)}건"}]
    pre += [{"kind": "observe" if c["kind"] == "observed" else "event",
             "title": f"{fmt(date.fromisoformat(c['date']))} {c['item']} → {c['state']}", "sub": c["why"]} for c in changes]
    msg = "[시스템 이벤트] 이사 30일째: 정착 현황을 확인하세요" if body.preset == "30d" else f"[시스템 이벤트] {fmt(target)} 기준 상태 변화 {len(changes)}건"
    out = a.run_turn(msg)
    out["log"] = pre + out["log"]
    out["divider"] = "2주 뒤 · " + fmt(target) if body.preset == "2w" else None
    return out | {"state": _state()}


@app.get("/api/state")
def state():
    return _state()


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
