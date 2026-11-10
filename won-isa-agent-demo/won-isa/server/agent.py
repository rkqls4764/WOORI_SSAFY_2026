"""Agent 루프: LLM이 도구를 고르고, 시스템이 실행·검증한다.

LLM 백엔드
- OpenAICompatLLM: OpenAI 호환 Chat Completions + tools (OpenAI, GMS 등 base_url만 바꾸면 됨)
- ScriptedLLM: API 키 없이 돌아가는 대본 정책(개발·리허설·영상 백업용). 같은 루프·도구·검증을 그대로 탄다.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from . import guard, tools as T
from .prompts import SYSTEM_PROMPT
from .rules import fmt

MAX_STEPS = 8


# ---------------------------------------------------------------- LLM 백엔드
def _strip_think(text):
    """Qwen3 같은 추론 모델이 답변에 섞는 <think>…</think> 블록을 지운다."""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


class OpenAICompatLLM:
    name = "openai"

    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(base_url=os.getenv("LLM_BASE_URL") or None, api_key=os.getenv("LLM_API_KEY"))
        self.model = os.getenv("LLM_MODEL")
        if not self.model:
            raise ValueError("LLM_MODEL을 .env에 설정하세요(팀이 쓰는 tool calling 지원 모델 이름)")

    def chat(self, messages, tools):
        clean = []
        for m in messages:  # tool 메시지는 role·tool_call_id·content만 보낸다
            clean.append({k: m[k] for k in ("role", "tool_call_id", "content")} if m["role"] == "tool" else m)
        # tool_choice는 보내지 않음(기본값이 auto이고, Ollama 등 일부 서버는 지원하지 않음)
        kw = {"tools": tools} if tools else {}
        r = self.client.chat.completions.create(model=self.model, messages=clean, temperature=0, **kw)
        m = r.choices[0].message
        calls = []
        for i, c in enumerate(m.tool_calls or []):
            args = c.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": c.id or f"call_{i}", "name": c.function.name, "arguments": args or {}})
        u = r.usage
        return {"content": _strip_think(m.content), "tool_calls": calls,
                "usage": {"prompt": getattr(u, "prompt_tokens", 0) or 0, "completion": getattr(u, "completion_tokens", 0) or 0}}


class ScriptedLLM:
    """대본 정책. 실제 LLM을 대신해 같은 형식(content/tool_calls)으로 응답한다."""
    name = "scripted"

    def __init__(self, session: T.Session):
        self.s = session
        self.pending = None   # 되묻기 후 기다리는 요청
        self.expect_diff = False
        self._n = 0

    def _call(self, name, **args):
        self._n += 1
        return {"content": None, "tool_calls": [{"id": f"call_{self._n}", "name": name, "arguments": args}],
                "usage": {"prompt": 0, "completion": 0}}

    @staticmethod
    def _say(text):
        return {"content": text, "tool_calls": [], "usage": {"prompt": 0, "completion": 0}}

    def _id_by_label(self, text):
        for i in self.s.items.values():
            if i["label"].replace(" ", "") in text.replace(" ", ""):
                return i["id"]
        return None

    def chat(self, messages, tools):
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        u = messages[last_user]["content"]
        done = {}
        for m in messages[last_user + 1:]:
            if m["role"] == "tool":
                done[m["name"]] = json.loads(m["content"])
        had = lambda n: n in done

        # --- 시스템 이벤트
        if u.startswith("[시스템 이벤트]"):
            if "승인 완료" in u:
                if not had("execute_plan"):
                    return self._call("execute_plan")
                r = done["execute_plan"]
                if r.get("status") == "BLOCKED":
                    return self._say(f"승인 내용과 현재 계획이 맞지 않아 실행하지 않았어요. {r['reason']}.")
                return self._say(f"승인하신 계획 v{r['plan_version']}대로 공식 절차에 신청했어요.\n\n결과가 오면 근거와 함께 알려 드릴게요.")
            if "정착" in u:
                if not had("get_settlement_status"):
                    return self._call("get_settlement_status")
                return self._say("남은 출금 항목을 확인해 볼까요?")
            if not had("get_move_status"):
                return self._call("get_move_status")
            r = done["get_move_status"]
            if r["alerts"]:
                a = r["alerts"][0]
                return self._say(f"{a['label']} 계좌 변경이 처리되지 않았어요.\n\n이렇게 해 볼까요?")
            return self._say("진행 상황을 확인했어요. 아직 문제는 없어요.")

        # --- 되묻기에 대한 답
        if self.pending and not had("plan_schedule"):
            iid = None
            for opt in self.pending["options"]:
                if opt["label"].replace(" ", "") in u.replace(" ", "") or u.replace(" ", "").rstrip("요.").startswith(opt["label"][:2]):
                    iid = opt["id"]
            if iid:
                p = self.pending
                self.pending = None
                self.expect_diff = True
                return self._call("plan_schedule", keep_item_ids=p["keep"], defer_item_ids=[iid])
        if had("plan_schedule") and self.expect_diff:
            self.expect_diff = False
            r = done["plan_schedule"]
            ch = [d["label"] for d in r.get("diff", []) if d["changed"]]
            return self._say(f"{', '.join(ch)}만 다음 달로 바꿨어요.\n바뀐 부분을 다시 확인해 주세요." if ch else "계획을 다시 확인했어요.")

        # --- 견적
        if "견적" in u or ("얼마" in u and "1년" in u) or "이득" in u:
            for n in ("get_transactions", "detect_recurring", "calc_switch_quote"):
                if not had(n):
                    return self._call(n)
            c = done["get_transactions"]["count"]
            return self._say(f"거래 {c}건을 보고 견적을 냈어요.\n확인이 필요한 항목은 따로 표시했어요.\n\n옮기는 순서도 짜 드릴까요?")

        keep = ["SAV"] if ("적금" in u and any(k in u for k in ("유지", "만기", "둘래", "두고"))) else []

        # --- 다음 달로 미루기(모호하면 되묻기)
        if "다음 달" in u:
            clause = next((c for c in re.split(r"[,.]|하고 |고 ", u) if "다음 달" in c), u)
            exact = self._id_by_label(clause)  # '동호회비'처럼 정확한 이름이면 바로 반영
            if exact in keep:
                exact = None
            if exact and not had("plan_schedule"):
                self.expect_diff = True
                return self._call("plan_schedule", keep_item_ids=keep, defer_item_ids=[exact])
            target = "회비" if "회비" in u else None
            if target and not had("find_items"):
                return self._call("find_items", query=target)
            if had("find_items"):
                r = done["find_items"]
                if r["ambiguous"]:
                    self.pending = {"keep": keep, "options": r["matches"]}
                    return self._say(f"어느 {r['query']}인가요?")
                if r["matches"] and not had("plan_schedule"):
                    self.expect_diff = True
                    return self._call("plan_schedule", keep_item_ids=keep, defer_item_ids=[r["matches"][0]["id"]])
            iid = self._id_by_label(u)
            if iid and not had("plan_schedule"):
                self.expect_diff = True
                return self._call("plan_schedule", keep_item_ids=keep, defer_item_ids=[iid])

        # --- 실행 요청(승인 게이트 시험)
        if any(k in u for k in ("바로", "실행", "다 옮겨", "해지", "무시")):
            if not had("execute_plan"):
                return self._call("execute_plan")
            r = done["execute_plan"]
            if r["status"] == "BLOCKED":
                if "재승인" in r["reason"]:
                    return self._say("승인하신 건 바뀌기 전 계획이라 실행하지 않았어요.\n바뀐 계획을 확인하고 다시 승인해 주세요.")
                return self._say("아직 승인 전이라 아무것도 실행하지 않았어요.\n계획을 확인하고 승인해 주시면 진행할게요.")
            return self._say("승인하신 계획대로 신청했어요.")

        # --- 승인 진행
        if any(k in u for k in ("진행할게", "재승인", "확인했어", "승인할게")):
            if not had("prepare_approval"):
                return self._call("prepare_approval")
            return self._say("급여계좌 변경 신청 초안을 만들었어요.\n확인 후 승인하면 공식 절차로 보낼게요.")

        # --- 실패 후속 조치
        if "직접 변경" in u or "잔액 유지" in u:
            opt = "direct_request" if "직접 변경" in u else "keep_balance"
            fid = next((a for a, st in self.s.item_state.items() if st == "FAILED"), None)
            if fid and not had("set_followup"):
                return self._call("set_followup", item_id=fid, option_id=opt)
            r = done.get("set_followup", {})
            if r.get("option") == "direct_request":
                return self._say("요청 문구와 통장사본을 준비했어요.\n제출하셨다고 알려 주시면 다음 출금일에 확인할게요.")
            if r.get("option") == "keep_balance":
                return self._say(f"{r['next_debit']} 출금 전에 A은행에 약 {r['keep_amount'] // 10000}만 원을 남겨 두세요.\n하루 전에 알려 드릴게요.")

        # --- 순서·계획
        if keep or any(k in u for k in ("순서", "계획", "짜", "생활비", "응", "네")):
            if not had("plan_schedule"):
                return self._call("plan_schedule", keep_item_ids=keep)
            msg = "급여부터 옮기는 게 안전해요.\n새 통장에 돈이 들어온 뒤 자동이체를 옮길게요."
            if keep:
                msg = "적금은 만기까지 그대로 두고, 급여부터 옮기는 순서로 짰어요.\n새 통장에 돈이 들어온 뒤 자동이체를 옮길게요."
            return self._say(msg)

        if "남은" in u:
            if not had("get_move_status"):
                return self._call("get_move_status")
            r = done["get_move_status"]
            left = [x["label"] for x in r["items"] if x["state"] in ("실패", "확인 대기")]
            return self._say(f"남은 항목은 {', '.join(left)}이에요.\n기관 처리 결과를 받으면 바로 알려 드릴게요." if left else "남은 항목이 없어요.")

        if "정착" in u or "현황" in u:
            if not had("get_settlement_status"):
                return self._call("get_settlement_status")
            return self._say("남은 출금 항목을 확인해 볼까요?")

        return self._say("견적, 이전 순서, 계획 변경 중에 무엇을 도와드릴까요?")


def make_llm(session):
    mode = os.getenv("LLM_MODE") or ("openai" if os.getenv("LLM_API_KEY") else "scripted")
    return OpenAICompatLLM() if mode == "openai" else ScriptedLLM(session)


# ---------------------------------------------------------------- 로그·카드 만들기(시스템)
def _summary(name, args, r) -> list[dict]:
    if r.get("ambiguous") and "error" in r:
        a = ", ".join(f"{k}={v}" for k, v in args.items() if v)
        labs = ", ".join(m["label"] for m in r["matches"])
        return [{"kind": "tool", "title": f"{name}({a})", "sub": "계획 변경 요청"},
                {"kind": "block", "title": "모호성 가드", "sub": f"고객 발화 '{r['query']}' → {len(r['matches'])}건({labs}) · 계획 안 바꾸고 되묻기"}]
    if "error" in r:
        return [{"kind": "error", "title": f"{name}()", "sub": r["error"]}]
    a = ", ".join(f"{k}={v}" for k, v in args.items() if v)
    head = {"kind": "tool", "title": f"{name}({a})"}
    if name == "get_transactions":
        return [head | {"sub": f"동의 범위 거래 {r['count']}건 · {r['months']}개월 · 원문 적요 미전달"}]
    if name == "detect_recurring":
        out = [head | {"sub": f"반복 패턴 {r['patterns_found']}건 → 이전 후보 {r['move_candidates']}건"}]
        out += [{"kind": "observe", "title": o.split(":")[0], "sub": o.split(": ", 1)[1]} for o in r["observations"]]
        return out
    if name == "calc_switch_quote":
        return [head | {"sub": f"추가 이자 {r['extra_interest_pretax_year']:,}원(세전·가정) · 미확인 {len(r['unconfirmed'])}개 → 총순혜택 확인 중"}]
    if name == "find_items":
        labs = ", ".join(m["label"] for m in r["matches"])
        out = [head | {"sub": f"'{r['query']}' → {len(r['matches'])}건 ({labs})"}]
        if r["ambiguous"]:
            out.append({"kind": "observe", "title": "대상이 모호함", "sub": "계획을 바꾸지 않고 고객에게 되묻기"})
        return out
    if name == "plan_schedule":
        out = [head | {"sub": f"계획 v{r['version']} · 단계 {len(r['steps'])}개 · 해시 {r['plan_hash'][:6]}" + ("" if r["changed"] else " (변경 없음)")}]
        if r["reapproval_required"]:
            out.append({"kind": "block", "title": f"v{r['invalidated_version']} {r['invalidated_what']} 무효", "sub": "계획 버전이 바뀜 → 이전 버전으로는 승인·실행 불가, 재승인 필요"})
        if r.get("already_requested"):
            out.append({"kind": "observe", "title": "이미 신청된 항목", "sub": ", ".join(r["already_requested"]) + " → 변경 신청 필요"})
        return out
    if name == "prepare_approval":
        return [head | {"sub": f"승인 화면 준비 · v{r['plan_version']} · 문서 {len(r['documents'])}개 · 승인은 고객만"}]
    if name == "execute_plan":
        if r["status"] == "BLOCKED":
            return [head | {"sub": "실행 요청"}, {"kind": "block", "title": "실행 차단", "sub": r["reason"]}]
        return [head | {"sub": f"신청 {len(r['requested'])}건 · 예약 {len(r['scheduled'])}건 · 중복 방지 {r['duplicate_prevented']}건"}]
    if name == "get_move_status":
        return [head | {"sub": f"{r['as_of']} 기준 항목 {len(r['items'])}개 · 실패 {len(r['alerts'])}건"}]
    if name == "set_followup":
        return [head | {"sub": f"{r['item']} → {'직접 변경 요청' if r['option'] == 'direct_request' else '잔액 유지 알림'}"}]
    if name == "get_settlement_status":
        return [head | {"sub": f"확인 완료 {r['done']}/{r['total']} · 거래 패턴만으로 완료 확정 안 함"}]
    return [head]


def _cards(name, r, s: T.Session) -> list[dict]:
    if r.get("ambiguous") and "error" in r:
        return [{"type": "choices", "options": [m["label"] + "요" for m in r["matches"]]}]
    if "error" in r:
        return []
    if name == "calc_switch_quote":
        return [{"type": "quote", **r}]
    if name == "plan_schedule":
        if r["diff"]:
            return [{"type": "plan_diff", **r}]
        return [{"type": "plan", **r}]
    if name == "find_items" and r["ambiguous"]:
        return [{"type": "choices", "options": [m["label"] + "요" for m in r["matches"]]}]
    if name == "prepare_approval":
        return [{"type": "approval", **r}]
    if name == "execute_plan":
        return [{"type": "exec", **r}]
    if name == "get_move_status":
        return [{"type": "failure", **a} for a in r["alerts"]] or [{"type": "status", **r}]
    if name == "set_followup":
        return [{"type": "followup", **r}]
    if name == "get_settlement_status":
        return [{"type": "settle", **r}]
    return []


SCENE = {"quote": "K2 견적", "plan": "K3 순서별 계획", "plan_diff": "K4 재계획", "choices": "K4 재계획",
         "approval": "K5 승인 게이트", "exec": "K5 승인 게이트", "failure": "K6 실패 감지", "followup": "K6 실패 감지",
         "settle": "K7 정착 현황", "status": "K6 상태 추적"}
SUBTITLE = {"plan_diff": "계획 수정 중", "choices": "계획 수정 중", "approval": "고객 승인 대기", "exec": "공식 절차 진행 중",
            "failure": "진행 상태 추적 중", "followup": "진행 상태 추적 중", "status": "진행 상태 추적 중"}


# ---------------------------------------------------------------- Agent
@dataclass
class Agent:
    session: T.Session
    llm: object = None
    history: list = field(default_factory=list)
    allowed: set = field(default_factory=set)
    scene: str = "K1 홈 · Agent 배너"
    subtitle: str = "이사 진행 중"
    totals: dict = field(default_factory=lambda: {"turns": 0, "prompt": 0, "completion": 0, "guard_fail": 0})

    def __post_init__(self):
        self.llm = self.llm or make_llm(self.session)

    def _system(self):
        d = self.session.today
        text = SYSTEM_PROMPT.format(today=f"{d.year}.{fmt(d)}")
        if os.getenv("LLM_NO_THINK") == "1":  # Qwen3 계열: 추론(생각) 출력 끄기
            text += "\n/no_think"
        return {"role": "system", "content": text}

    def run_turn(self, text: str, forced: tuple | None = None, allow_tools: bool = True) -> dict:
        """forced=(도구, 인자): LLM이 아니라 시스템이 먼저 실행하는 도구(예: 고객 승인 이벤트 → execute_plan).
        allow_tools=False면 LLM은 도구 없이 결과 설명만 한다."""
        t0 = time.time()
        if not text.startswith("[시스템 이벤트]"):
            self.session.last_user = text
        self.history.append({"role": "user", "content": text})
        self.allowed |= guard.extract(text)
        log, cards, n_tools, usage = [], [], 0, {"prompt": 0, "completion": 0}
        n_sys = 0
        if forced:
            name, args = forced
            r = T.TOOL_IMPLS[name](self.session, **args)
            n_sys += 1
            self.allowed |= guard.allowed_from(r)
            entries = _summary(name, args, r)
            entries[0] = entries[0] | {"sub": "시스템 실행(고객 승인 이벤트 · LLM 경로 밖) · " + entries[0].get("sub", "")}
            log += entries
            cards += _cards(name, r, self.session)
            cid = f"sys_{name}_{len(self.history)}"
            self.history.append({"role": "assistant", "content": "",
                                 "tool_calls": [{"id": cid, "type": "function",
                                                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
            self.history.append({"role": "tool", "tool_call_id": cid, "name": name,
                                 "content": json.dumps(r, ensure_ascii=False, default=str)})
        final = None
        for _ in range(MAX_STEPS):
            out = self.llm.chat([self._system()] + self.history, T.TOOL_SCHEMAS if allow_tools else [])
            usage["prompt"] += out["usage"]["prompt"]
            usage["completion"] += out["usage"]["completion"]
            if not out["tool_calls"]:
                final = out["content"] or ""
                break
            self.history.append({"role": "assistant", "content": out["content"],
                                 "tool_calls": [{"id": c["id"], "type": "function",
                                                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                                                for c in out["tool_calls"]]})
            for c in out["tool_calls"]:
                n_tools += 1
                fn = T.TOOL_IMPLS.get(c["name"])
                try:
                    r = fn(self.session, **c["arguments"]) if fn else {"error": f"없는 도구: {c['name']}"}
                except T.TransitionError as e:
                    r = {"error": f"상태 전이 거부: {e}"}
                except TypeError as e:
                    r = {"error": f"잘못된 인자: {e}"}
                self.allowed |= guard.allowed_from(r)
                log += _summary(c["name"], c["arguments"], r)
                cards += _cards(c["name"], r, self.session)
                self.history.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                                     "content": json.dumps(r, ensure_ascii=False, default=str)})
        if final is None:
            final = "처리 단계가 너무 길어 여기서 멈췄어요. 다시 말씀해 주세요."
        final = re.sub(r"^\s*\[시스템 이벤트\][\s:]*", "", final)
        bad = guard.check(final, self.allowed)
        if bad:
            self.totals["guard_fail"] += 1
            log.append({"kind": "check", "title": "숫자 검증 실패", "sub": f"도구 결과에 없는 숫자 {len(bad)}개 → 템플릿 문장으로 대체", "fail": True})
            final = "결과를 카드로 정리했어요. 숫자는 카드의 시스템 계산 값을 확인해 주세요." if cards else "확인한 내용을 정리하지 못했어요. 다시 말씀해 주세요."
        elif re.search(r"\d", final):
            log.append({"kind": "check", "title": "숫자 검증", "sub": "답변 속 숫자가 모두 도구 결과와 일치 ✓"})
        self.history.append({"role": "assistant", "content": final})
        dt = time.time() - t0
        self.totals["turns"] += 1
        self.totals["prompt"] += usage["prompt"]
        self.totals["completion"] += usage["completion"]
        log.append({"kind": "llm", "title": f"LLM 응답 · {self.llm.name}",
                    "sub": f"도구 {n_tools}개 · 토큰 {usage['prompt'] + usage['completion']:,} · {dt:.1f}초"})
        if cards:
            last = cards[-1]["type"]
            self.scene = SCENE.get(last, self.scene)
            self.subtitle = SUBTITLE.get(last, "이사 진행 중")
        paras = [p for p in final.split("\n\n") if p.strip()]
        return {"before": paras[:1], "after": paras[1:], "cards": cards, "tool_count": n_tools + n_sys, "log": log,
                "usage": usage, "latency": round(dt, 2), "guard_ok": not bad}
