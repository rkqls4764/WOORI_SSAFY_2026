"""Agent가 호출하는 도구와 세션 상태.

원칙
- 숫자는 전부 여기(시스템)에서 계산한다. LLM은 결과를 고르고 설명만 한다.
- 실행 도구(execute_plan)는 '현재 계획 버전'에 묶인 고객 승인이 있어야만 동작한다.
- 같은 계획 버전의 같은 항목은 한 번만 실행한다(멱등키 = 계획 해시 + 항목 id).
- 이전 항목은 상태 머신으로만 상태가 바뀐다.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import adapters, auth as authmod, data, detector
from .audit import AuditLog
from .rules import RULES, add_months, adjust_to_business_day, first_movable_cycle, fmt, next_occurrence

# ---------------------------------------------------------------- 상태 머신
STATES = {
    "PLANNED": "계획",
    "APPROVED": "승인",
    "REQUESTED": "신청",
    "DONE": "완료",
    "PENDING": "확인 대기",
    "FAILED": "실패",
    "KEPT": "유지",
    "DEFERRED": "예약",
}
TRANSITIONS = {
    "PLANNED": {"APPROVED", "KEPT", "DEFERRED"},
    "KEPT": {"PLANNED", "APPROVED"},
    "DEFERRED": {"PLANNED", "APPROVED", "REQUESTED"},
    "APPROVED": {"REQUESTED", "PLANNED", "KEPT", "DEFERRED"},
    "REQUESTED": {"DONE", "PENDING", "FAILED"},
    "PENDING": {"DONE", "FAILED"},
    "FAILED": {"PENDING", "REQUESTED"},
    "DONE": set(),
}


class TransitionError(Exception):
    pass


LABELS = {  # 시스템 정규화: 상대방명 → 표시 이름(원문 적요는 쓰지 않음)
    "(주)한빛소프트": ("SAL", "급여"),
    "SKT 통신요금": ("TEL", "통신비"),
    "한빛아파트관리사무소": ("MGT", "관리비"),
    "한화생명 보험료": ("INS", "보험료"),
    "김민수(풋살동호회)": ("CLB", "동호회비"),
    "A은행 정기적금": ("SAV", "적금"),
    "이서연": ("BOOK", "독서모임 회비"),
    "웨이브 구독": ("OTT", "웨이브 구독"),
}
KIND = detector.KIND
INSTITUTION = {"MGT": "한빛아파트관리사무소", "TEL": "SKT", "INS": "한화생명"}


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class Session:
    today: date = data.TODAY
    items: dict = field(default_factory=dict)          # id -> 탐지된 반복 항목
    item_state: dict = field(default_factory=dict)     # id -> 상태
    evidence: dict = field(default_factory=lambda: defaultdict(list))
    constraints: dict = field(default_factory=lambda: {"keep": [], "defer": [], "include": [], "exclude": []})
    plans: list = field(default_factory=list)          # 버전별 계획
    approval: dict | None = None                       # {"plan_hash", "version", "at"}
    pending_approval: dict | None = None               # 승인 화면을 띄운 계획 {"plan_hash","version"}
    executed: dict = field(default_factory=dict)       # 멱등키 -> 결과
    followups: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"blocked": 0, "executed_without_approval": 0, "dup_prevented": 0})
    fired_events: set = field(default_factory=set)
    last_user: str = ""                                # 마지막 고객 발화(시스템 이벤트 제외) — 모호성 가드용
    sid: str = "local"                                 # 사용자 세션 id(승인 토큰이 여기에 묶임)
    decisions: dict = field(default_factory=dict)      # id -> "confirmed" | "excluded" (고객이 항목별로 정함)
    audit: AuditLog = field(default_factory=AuditLog)  # 감사 이벤트 로그(해시 체인)
    adapter: object = field(default_factory=adapters.DemoSimulator)   # 기관 연동 어댑터(데모: 시뮬레이터)
    auth: object = field(default_factory=authmod.MockAuthenticator)   # 인증 어댑터(데모: 모의 인증)
    # ---- 정착 단계(추천 Agent): 이사 계획·승인과 분리된 상태
    stage: str = "move"                                # move(이사) → settle(정착, 30일째부터)
    consent: dict = field(default_factory=lambda: {"spending": False})  # 소비 분석·추천 동의(이사 동의와 별도)
    persona: str | None = None                         # 담당 캐릭터 유형 id(예: FOOD → 달보)
    goal: str | None = None                            # 고객이 고른 목표(식비 절약 | 저축)
    analysis: dict | None = None                       # 소비 분석 결과(시스템 계산)
    reco: dict | None = None                           # 마지막 추천(시스템 계산)
    missions: dict = field(default_factory=dict)       # mission id -> 참여 정보(고객 버튼으로만)
    ledger: object = None                              # 꿀머니 원장(rewards.Ledger)

    # ------------------------------------------------------------ 공용
    @property
    def plan(self):
        return self.plans[-1] if self.plans else None

    def set_state(self, item_id: str, new: str, why: str = ""):
        old = self.item_state.get(item_id, "PLANNED")
        if old == new:
            return
        if new not in TRANSITIONS.get(old, set()):
            raise TransitionError(f"{item_id}: {STATES[old]} → {STATES[new]} 전이 불가")
        self.item_state[item_id] = new
        self.evidence[item_id].append({"date": self.today.isoformat(), "state": STATES[new], "why": why})
        self.log("ITEM_STATE", "system", item=item_id, **{"from": STATES[old], "to": STATES[new]}, why=why)

    def log(self, type_: str, actor: str, **payload):
        return self.audit.append(type_, actor, payload, self.today.isoformat())

    def salary_date(self) -> date:
        return adjust_to_business_day(next_occurrence(25, self.today), backward=True)


# ---------------------------------------------------------------- 도구 구현
def get_transactions(s: Session, months: int = 6) -> dict:
    txs = data.TRANSACTIONS
    return {
        "count": len(txs),
        "period": f"{data.PERIOD_START.isoformat()}~{(data.TODAY - timedelta(days=1)).isoformat()}",
        "months": months,
        "accounts": ["A은행 입출금(마이데이터 동의 범위)"],
        "note": "원문 적요는 Agent(LLM)에 전달하지 않고, 정규화한 필드만 사용",
    }


def detect_recurring(s: Session) -> dict:
    found, observations = [], []
    for d in detector.detect(data.TRANSACTIONS, s.today):
        cp = d["counterparty"]
        iid, label = LABELS.get(cp, ("X" + hashlib.md5(cp.encode()).hexdigest()[:4], cp))
        item = {
            "id": iid, "label": label, "kind": d["kind"], "day": d["last_day"] if d["kind"] != "급여" else 25,
            "amount": d["amount"], "count": d["count"], "last": d["last"].isoformat(),
            "confidence": d["confidence"], "note": d["note"], "median_gap": d["median_gap"], "code": d["code"],
        }
        if iid == "SAV":
            item["maturity"] = data.SAVINGS["maturity"].isoformat()
        if iid in ("TEL", "MGT", "INS"):
            item["day"] = {"TEL": 27, "MGT": 15, "INS": 18}[iid]  # 휴일 보정 전 약정일
        if iid == "CLB":
            item["day"] = 5
        found.append(item)
        if item["note"]:
            observations.append(f"{label}: {item['note']}")
    order =["SAL", "TEL", "MGT", "INS", "CLB", "SAV", "BOOK", "OTT"]
    found.sort(key=lambda x: order.index(x["id"]) if x["id"] in order else 99)
    s.items = {i["id"]: i for i in found}
    for i in found:
        s.item_state.setdefault(i["id"], "PLANNED")
    candidates = [i for i in found if i["kind"] in ("급여", "자동납부", "자동송금", "적금") and i["confidence"] != "excluded"]
    s.log("ITEMS_DETECTED", "system", rule=detector.RULE_TEXT,
          items=[{"id": i["id"], "confidence": i["confidence"], "code": i["code"], "count": i["count"]} for i in found])
    return {
        "patterns_found": len(found),
        "move_candidates": len(candidates),
        "items": [{k: v for k, v in i.items() if k in ("id", "label", "kind", "day", "amount", "confidence", "note")} for i in found],
        "observations": observations,
        "rule": detector.RULE_TEXT,
    }


def items_card(s: Session) -> dict:
    """탐지 항목 확인 카드: 탐지 근거와 고객의 항목별 결정(포함·제외·확정) 상태."""
    rows = []
    for i in s.items.values():
        iid, d = i["id"], s.decisions.get(i["id"])
        if i["confidence"] == "excluded":
            status = "해지 추정"
        elif d == "excluded":
            status = "고객 제외"
        elif d == "confirmed":
            status = "고객 확정"
        elif i["confidence"] == "check":
            status = "확인 필요"
        else:
            status = "자동 탐지"
        in_plan = (i["confidence"] != "excluded" and d != "excluded"
                   and (i["kind"] != "직접송금" or iid in s.constraints["include"]))
        open_ = s.item_state.get(iid) in ("PLANNED", "KEPT", "DEFERRED", "APPROVED")
        rows.append({"id": iid, "label": i["label"], "kind": i["kind"], "amount": i["amount"],
                     "evidence": f"{i['count']}회 · 간격 중앙값 {i['median_gap']:g}일", "status": status, "note": i["note"],
                     "in_plan": in_plan, "can_decide": i["confidence"] != "excluded" and iid != "SAL" and open_})
    return {"type": "items", "rows": rows, "rule": detector.RULE_TEXT,
            "pending": sum(r["status"] == "확인 필요" for r in rows)}


def decide_item(s: Session, item_id: str, decision: str) -> dict:
    """고객이 화면에서 항목별로 정한다(LLM 경로 밖). decision: confirm(확정) | include(포함) | exclude(제외)."""
    if item_id not in s.items:
        return {"error": f"알 수 없는 항목 id: {item_id}"}
    i = s.items[item_id]
    if i["confidence"] == "excluded" or item_id == "SAL":
        return {"error": "이 항목은 바꿀 수 없습니다"}
    if s.item_state.get(item_id) in ("REQUESTED", "DONE", "PENDING", "FAILED"):
        return {"error": "이미 신청된 항목은 여기서 바꿀 수 없습니다(변경 신청 필요)"}
    c, before = s.constraints, _canon(s.constraints)
    if decision == "exclude":
        s.decisions[item_id] = "excluded"
        for k in ("include", "keep", "defer"):
            if item_id in c[k]:
                c[k].remove(item_id)
        if item_id not in c["exclude"]:
            c["exclude"].append(item_id)
    elif decision in ("include", "confirm"):
        s.decisions[item_id] = "confirmed"
        if item_id in c["exclude"]:
            c["exclude"].remove(item_id)
        if decision == "include" and i["kind"] == "직접송금" and item_id not in c["include"]:
            c["include"].append(item_id)
    else:
        return {"error": f"알 수 없는 결정: {decision}"}
    s.log("ITEM_DECIDED", "customer", item=item_id, decision=decision, detected_as=i["confidence"], reason=i["code"])
    return {"item": i["label"], "decision": decision, "plan_affected": before != _canon(c)}


def _won_text(n: int) -> str:
    """표시용 금액 문자열(예: 60000 → '6만 원'). 숫자 가드와 같은 표기."""
    man, rest = divmod(int(n), 10000)
    if man and not rest:
        return f"{man:,}만 원"
    return f"{int(n):,}원"


def calc_switch_quote(s: Session) -> dict:
    p = data.PRODUCT
    bal = data.ASSUMED_DAILY_BALANCE
    diff = round(p["max_rate"] - data.OLD_ACCOUNT_RATE, 1)
    interest = int(round(bal * diff / 100))
    return {
        "product": p["name"],
        "before_rate": data.OLD_ACCOUNT_RATE,
        "after_rate": p["max_rate"],
        "rate_diff": diff,
        "assumed_balance": bal,
        "extra_interest_pretax_year": interest,
        "extra_interest_text": f"연 {_won_text(interest)}(세전)",  # 단위 변환을 LLM에 맡기지 않음
        "assumptions": ["매일 잔액 200만 원 유지", "이전 후 우대조건 모두 충족", "1년 적용·세전"],
        "unconfirmed": ["A은행 기존 혜택 상실", "해지 손실·추가 비용"],
        "net_benefit": "확인 중",
        "rule": "미확인 금액을 0원으로 넣지 않음",
        "source": p["source"],
        "basis": {
            "sources": [f"상품 조건: {p['source']}", f"이전 전 금리: A은행 입출금 연 {data.OLD_ACCOUNT_RATE}%(가정값)"],
            "rules": ["추가 이자 = 가정 잔액 × 금리 차이 (시스템 계산, LLM은 계산하지 않음)",
                      "미확인 금액은 0원으로 넣지 않음 → 총순혜택은 '확인 중'"],
            "assumptions": [f"일평균 잔액 {bal // 10000}만 원 유지(우대금리 적용 한도 {p['cap_balance'] // 10000}만 원)",
                            "우대조건 모두 충족: " + " · ".join(f"{b['name']} +{b['rate']}%p" for b in p["bonus"]),
                            "1년 적용 · 세전"],
            "confirm": ["A은행 기존 혜택 상실 여부", "해지 손실·추가 비용"],
        },
    }


def find_items(s: Session, query: str) -> dict:
    q = query.replace(" ", "")
    hits = [i for i in s.items.values() if q and q in i["label"].replace(" ", "") and i["confidence"] != "excluded"]
    return {
        "query": query,
        "matches": [{"id": i["id"], "label": i["label"], "kind": i["kind"], "amount": i["amount"]} for i in hits],
        "ambiguous": len(hits) > 1,
    }


def _build_steps(s: Session) -> tuple[list, list, dict]:
    sal = s.salary_date()
    keep, defer, include = set(s.constraints["keep"]), set(s.constraints["defer"]), set(s.constraints["include"])
    exclude = set(s.constraints.get("exclude", []))
    steps, excluded, groups = [], [], defaultdict(list)
    applied = defaultdict(list)  # 규칙 id -> 적용된 항목 이름
    for i in s.items.values():
        iid = i["id"]
        if i["confidence"] == "excluded":
            excluded.append({"id": iid, "label": i["label"], "reason": i["note"]})
            continue
        if iid in exclude and iid != "SAL":
            excluded.append({"id": iid, "label": i["label"], "reason": "고객이 제외 → 옮기지 않음"})
            continue
        if i["kind"] == "직접송금":
            if iid not in include:
                excluded.append({"id": iid, "label": i["label"], "reason": "직접 보내는 송금 → 고객 확인 후 새 계좌에서 설정"})
            else:
                steps.append({"key": f"manual:{iid}", "title": i["label"], "items": [iid], "action": "manual",
                              "date": sal.isoformat(), "item_dates": {iid: sal.isoformat()},
                              "sub": "직접 보내는 송금 → 첫 급여 뒤 새 계좌에서 고객이 직접", "badge": "고객"})
            continue
        if i["kind"] in ("자동납부", "자동송금"):
            due, why, detail, rule_ids = first_movable_cycle(i["kind"], i["day"], s.today, sal)
            if iid in defer:
                due2 = adjust_to_business_day(next_occurrence(i["day"], add_months(date(due.year, due.month, 1), 1)))
                steps.append({"key": f"defer:{iid}", "title": i["label"], "items": [iid], "action": "defer",
                              "date": due2.isoformat(), "item_dates": {iid: due2.isoformat()},
                              "sub": f"고객 요청 → 다음 달({fmt(due2)} 출금부터)", "badge": "다음 달"})
                continue
            for rid in rule_ids:
                applied[rid].append(i["label"])
            groups[(why, "다음 주기" in why)].append((i, due, detail))
    # 1) 급여
    if "SAL" in s.items:
        steps.insert(0, {"key": "payroll", "title": "급여 이전 신청", "items": ["SAL"], "action": "payroll",
                         "date": sal.isoformat(), "item_dates": {"SAL": sal.isoformat()}, "sub": f"{fmt(sal)} 급여일 반영 목표", "badge": "먼저"})
        steps.insert(1, {"key": "payroll_check", "title": "첫 급여 입금 확인", "items": [], "action": "observe",
                         "date": sal.isoformat(), "item_dates": {}, "sub": "입금 내역으로 확인", "badge": "대기"})
    # 2) 자동이체(같은 이유끼리 묶음, 이번 주기 먼저)
    for (why, is_next), rows in sorted(groups.items(), key=lambda kv: kv[0][1]):
        rows.sort(key=lambda r: r[1])
        title = "·".join(r[0]["label"] for r in rows)
        dates = "·".join(fmt(r[1]) for r in rows)
        steps.append({"key": "move:" + ",".join(r[0]["id"] for r in rows), "title": title, "items": [r[0]["id"] for r in rows],
                      "action": "move", "date": rows[0][1].isoformat(),
                      "item_dates": {r[0]["id"]: r[1].isoformat() for r in rows}, "sub": f"{why} ({dates}부터)",
                      "detail": rows[0][2], "badge": "다음 달" if is_next else "대기"})
    # deferred·직접송금은 move 뒤로
    steps.sort(key=lambda st: {"payroll": 0, "observe": 1, "move": 2, "defer": 3, "manual": 4}.get(st["action"], 9))
    # 3) 적금
    if "SAV" in s.items and "SAV" not in exclude:
        mat = date.fromisoformat(s.items["SAV"]["maturity"])
        sub = f"만기 {fmt(mat)}까지 유지(고객 요청)" if "SAV" in keep else f"만기 {fmt(mat)} 후 이전(중도해지 손실 0원)"
        steps.append({"key": "savings", "title": "적금", "items": ["SAV"], "action": "keep", "date": mat.isoformat(), "item_dates": {"SAV": mat.isoformat()},
                      "sub": sub, "badge": "만기 후"})
        if "SAV" not in keep:
            applied["SAVINGS_MATURITY"].append("적금")
    return steps, excluded, _plan_basis(s, applied, steps)


def _pending_confirm(s: Session, steps: list) -> list:
    """계획에 들어 있는데 '확인 필요'로 탐지됐고 고객이 아직 정하지 않은 항목."""
    in_plan = {iid for st in steps for iid in st["items"]}
    return [i for i in s.items.values() if i["id"] in in_plan and i["confidence"] == "check" and i["id"] not in s.decisions]


def _plan_basis(s: Session, applied: dict, steps: list) -> dict:
    """계획 카드에 붙는 근거: 데이터 출처 / 적용 규칙 / 가정 / 고객 확인 필요."""
    tx = get_transactions(s)
    rules, assumptions = [], []
    for rid, labels in applied.items():
        r = RULES[rid]
        (assumptions if r["kind"] == "가정" else rules).append(f"{'·'.join(labels)}: {r['text']} — {r['basis']}")
    confirm = [f"{i['label']}: {i['note']}" for i in _pending_confirm(s, steps)]
    confirm += [f"{i['label']}: 직접 보내는 송금 → 포함할지 고객 확인" for i in s.items.values()
                if i["kind"] == "직접송금" and i["confidence"] != "excluded" and i["id"] not in s.decisions]
    if "SAL" in s.items:
        assumptions.append(f"급여일: 최근 {s.items['SAL']['count']}회 입금 기준 매월 25일(휴일이면 직전 영업일)")
        confirm.append("급여: 회사 담당자의 처리 일정(신청서는 고객이 전달)")
    return {
        "sources": [f"A은행 입출금 거래내역 {tx['count']}건 · {tx['months']}개월(마이데이터 동의 범위, 가상 데이터)",
                    "영업일: 공휴일 달력(holidays KR)"],
        "rules": rules, "assumptions": assumptions, "confirm": confirm,
    }


_PARTICLES = ("으로", "에서", "까지", "부터", "은", "는", "이", "가", "을", "를", "도", "만", "요", "로", "의", "랑", "과", "와")


def _ambiguous_refs(s: Session, text: str) -> list[tuple[str, list]]:
    """고객 발화 속 낱말 중 여러 항목에 걸리는 것(예: '회비' → 동호회비·독서모임 회비)."""
    live = [i for i in s.items.values() if i["confidence"] != "excluded"]
    out = []
    for tok in re.split(r"[^0-9A-Za-z가-힣]+", text):
        cands, t = [tok], tok
        for _ in range(2):  # 조사 최대 2개 떼어 보기(예: 회비는요)
            p = next((p for p in _PARTICLES if t.endswith(p) and len(t) - len(p) >= 2), None)
            if not p:
                break
            t = t[: -len(p)]
            cands.append(t)
        for c in cands:
            if len(c) < 2:
                continue
            hits = [i for i in live if c in i["label"].replace(" ", "")]
            if hits:
                if len(hits) > 1:
                    out.append((c, hits))
                break
    return out


def _said_words(text: str) -> set[str]:
    """고객 발화의 낱말(조사 최대 2개 뗀 형태 포함, 2글자 이상)."""
    out = set()
    for tok in re.split(r"[^0-9A-Za-z가-힣]+", text):
        t = tok
        out.add(t)
        for _ in range(2):
            p = next((p for p in _PARTICLES if t.endswith(p) and len(t) - len(p) >= 2), None)
            if not p:
                break
            t = t[: -len(p)]
            out.add(t)
    return {w for w in out if len(w) >= 2}


def _ambiguity_guard(s: Session, ids: list) -> dict | None:
    """LLM이 모호한 말을 한 항목으로 '찍어서' 계획을 바꾸려 하면 막고 되묻게 한다."""
    text = s.last_user.replace(" ", "")
    refs = _ambiguous_refs(s, s.last_user)

    def ask(term, hits, why):
        return {"error": f"고객이 말한 '{term}'이(가) {len(hits)}개 항목에 해당해 계획을 바꾸지 않음{why}. 어느 것인지 고객에게 되물을 것",
                "ambiguous": True, "query": term,
                "matches": [{"id": i["id"], "label": i["label"], "kind": i["kind"], "amount": i["amount"]} for i in hits]}

    for term, hits in refs:
        hit_ids = {i["id"] for i in hits}
        named = any(i["label"].replace(" ", "") in text for i in hits)
        if hit_ids & set(ids) and not named:
            return ask(term, hits, "")
    # 모호한 말이 있는 발화에서 고객이 말하지 않은 항목으로 바꾸려 하면(예: '회비' → '관리비') 되묻기
    extra = _ungrounded(s, ids)
    if extra and refs:
        term, hits = refs[0]
        return ask(term, hits, f"(고객이 말하지 않은 '{s.items[extra[0]]['label']}'(으)로 바꾸려 함)")
    return None


def _ungrounded(s: Session, ids: list) -> list:
    """이번 발화에 나오지 않았고 기존 계획 제약에도 없는 새 항목 id."""
    said, prior = _said_words(s.last_user), {i for v in s.constraints.values() for i in v}
    return [iid for iid in dict.fromkeys(ids) if iid in s.items and iid not in prior
            and not any(w in s.items[iid]["label"].replace(" ", "") for w in said)]


def _resolve_item_ids(s: Session, lists: list) -> tuple:
    """항목 대조: 모델이 id 대신 이름(예: '급여')을 넘기면 시스템이 실제 항목과 맞춘다.
    하나로 맞으면 id로 바꾸고, 여럿이면 되묻고, 없으면 버린다(계획에 넣지 않음)."""
    out, mapped, unknown = [], {}, []
    live = [i for i in s.items.values() if i["confidence"] != "excluded"]
    for ids in lists:
        res = []
        for x in ids or []:
            if x in s.items:
                res.append(x)
                continue
            q = str(x).replace(" ", "")
            hits = [i for i in live if i["label"].replace(" ", "") == q] or [i for i in live if q and q in i["label"].replace(" ", "")]
            if len(hits) == 1:
                res.append(hits[0]["id"]); mapped[str(x)] = hits[0]["id"]
            elif len(hits) > 1:
                return None, {"error": f"'{x}'이(가) {len(hits)}개 항목에 해당해 계획을 바꾸지 않음. 어느 것인지 고객에게 되물을 것",
                              "ambiguous": True, "query": str(x),
                              "matches": [{"id": i["id"], "label": i["label"], "kind": i["kind"], "amount": i["amount"]} for i in hits]}, []
            else:
                unknown.append(str(x))
        out.append(res)
    if mapped or unknown:
        s.log("ITEM_IDS_CHECKED", "system", mapped=mapped, unknown=unknown, why="모델이 넘긴 항목을 실제 항목과 대조")
    return out, mapped, unknown


def plan_schedule(s: Session, keep_item_ids=None, defer_item_ids=None, include_item_ids=None, undo_item_ids=None) -> dict:
    if not s.items:
        detect_recurring(s)
    lists, mapped, unknown = _resolve_item_ids(s, [keep_item_ids, defer_item_ids, include_item_ids, undo_item_ids])
    if lists is None:
        return mapped  # 여러 항목에 해당하는 이름 → 되묻기
    keep_item_ids, defer_item_ids, include_item_ids, undo_item_ids = lists
    blocked = _ambiguity_guard(s, (keep_item_ids or []) + (defer_item_ids or []) + (include_item_ids or []) + (undo_item_ids or []))
    if blocked:
        return blocked
    # 항목 근거 가드: 고객이 말하지 않은 새 항목은 계획에 넣지 않고 걸러낸다(막지 않음 → 말한 항목은 그대로 반영)
    ignored = _ungrounded(s, (keep_item_ids or []) + (defer_item_ids or []) + (include_item_ids or []) + (undo_item_ids or []))
    if ignored:
        keep_item_ids, defer_item_ids, include_item_ids, undo_item_ids = (
            [i for i in (x or []) if i not in ignored] for x in (keep_item_ids, defer_item_ids, include_item_ids, undo_item_ids))
        s.log("ITEMS_IGNORED", "system", items=ignored, why="고객 발화·기존 계획에 없는 항목")
    for k, ids in (("keep", keep_item_ids), ("defer", defer_item_ids), ("include", include_item_ids)):
        for iid in ids or []:
            if iid not in s.items:
                return {"error": f"알 수 없는 항목 id: {iid}", "known_ids": list(s.items)}
            if iid not in s.constraints[k]:
                s.constraints[k].append(iid)
            if k == "include" and iid in s.constraints["exclude"]:
                s.constraints["exclude"].remove(iid)
    for iid in undo_item_ids or []:
        for k in s.constraints:
            if iid in s.constraints[k]:
                s.constraints[k].remove(iid)
        if s.decisions.get(iid) == "excluded":
            s.decisions.pop(iid)
    steps, excluded, basis = _build_steps(s)
    body = {"steps": [{k: st[k] for k in ("key", "items", "action", "item_dates")} for st in steps], "constraints": s.constraints}
    h = hashlib.sha256(_canon(body).encode()).hexdigest()[:12]
    prev = s.plan
    changed = prev is None or prev["hash"] != h
    if prev and changed and _canon(s.constraints) == _canon(prev["constraints"]):
        # 고객 제약이 그대로인데 날짜만 지나 다시 계산된 경우: 계획을 바꾸지 않는다(승인 유지).
        # 예: 걸러진 항목만 들어온 호출, 실패 대안을 고르는데 계획 도구를 부른 경우
        s.log("PLAN_UNCHANGED", "system", why="고객 제약 변화 없음 → 기존 계획 유지", plan_hash=prev["hash"])
        changed, h = False, prev["hash"]
    if changed:
        version = (prev["version"] + 1) if prev else 1
        plan = {"version": version, "hash": h, "steps": steps, "excluded": excluded,
                "constraints": json.loads(_canon(s.constraints)), "created": s.today.isoformat()}
        s.plans.append(plan)
        s.log("PLAN_CREATED", "system", version=version, plan_hash=h, constraints=plan["constraints"],
              steps=[{"key": st["key"], "action": st["action"], "item_dates": st["item_dates"]} for st in steps])
    else:
        plan = prev
    stale = [x for x in (s.approval, s.pending_approval) if x and x["plan_hash"] != h]
    approval_invalidated = bool(changed and prev and stale)
    if approval_invalidated:
        s.log("APPROVAL_INVALIDATED", "system", invalidated_version=stale[0]["version"],
              invalidated_hash=stale[0]["plan_hash"], new_version=plan["version"], new_hash=h)
    already = [s.items[i]["label"] for i in (defer_item_ids or []) + (keep_item_ids or [])
               if s.item_state.get(i) in ("REQUESTED", "DONE", "PENDING", "FAILED")]
    # 승인됐던 항목은 계획이 바뀌면 다시 '계획' 상태로(재승인 필요)
    for iid, st in list(s.item_state.items()):
        if changed and st == "APPROVED":
            s.set_state(iid, "PLANNED", "계획 변경으로 재승인 필요")
    for iid in s.constraints["keep"]:
        if s.item_state.get(iid) in ("PLANNED",):
            s.set_state(iid, "KEPT", "고객 요청")
    for iid in s.constraints["defer"]:
        if s.item_state.get(iid) in ("PLANNED",):
            s.set_state(iid, "DEFERRED", "고객 요청")
    diff = []
    if changed and prev:
        before = {iid: st for st in prev["steps"] for iid in st["items"]}
        after = {iid: st for st in plan["steps"] for iid in st["items"]}
        for iid in after:
            b, a = before.get(iid), after[iid]
            lab = s.items[iid]["label"] if iid in s.items else iid
            def desc(st):
                if st is None:
                    return "제외"
                if st["action"] == "keep":
                    return "만기까지 유지" if iid in s.constraints["keep"] else "만기 후 이전"
                return f"{fmt(date.fromisoformat(st['item_dates'][iid]))} 출금부터" if st["action"] in ("move", "defer") else st["sub"]
            diff.append({"id": iid, "label": lab, "before": desc(b), "after": desc(a),
                         "changed": desc(b) != desc(a)})
    return {
        "version": plan["version"], "plan_hash": plan["hash"], "changed": changed,
        "steps": [{k: st[k] for k in ("title", "sub", "badge", "date")} | {"items": st["items"]} for st in plan["steps"]],
        "excluded": plan["excluded"], "constraints": plan["constraints"],
        "approval_status": _approval_status(s),
        "reapproval_required": approval_invalidated,
        "invalidated_version": stale[0]["version"] if approval_invalidated else None,
        "invalidated_what": ("승인" if (s.approval and s.approval in stale) else "승인 화면") if approval_invalidated else None,
        "already_requested": already,
        "ignored_items": [{"id": i, "label": s.items[i]["label"]} for i in ignored],
        "resolved_items": mapped, "unknown_items": unknown,
        "diff": diff,
        "basis": basis,
        "rules": ["첫 급여 전 출금은 다음 주기", "자동송금 D-2영업일 이내 변경 불가", "자동납부 처리 3영업일(가정)", "적금은 만기 후"],
    }


def _approval_status(s: Session) -> str:
    if not s.approval:
        return "승인 전"
    if s.plan and s.approval["plan_hash"] == s.plan["hash"]:
        return f"v{s.approval['version']} 승인됨"
    return f"v{s.approval['version']} 승인 → 계획 변경으로 무효(재승인 필요)"


def prepare_approval(s: Session) -> dict:
    if not s.plan:
        return {"error": "계획이 없습니다. 먼저 plan_schedule을 호출하세요."}
    p = s.plan
    moves = [st for st in p["steps"] if st["action"] == "move"]
    n_move = sum(len(st["items"]) for st in moves)
    sal = s.salary_date()
    s.pending_approval = {"plan_hash": p["hash"], "version": p["version"]}
    pending = _pending_confirm(s, p["steps"])
    s.log("APPROVAL_SCREEN_PREPARED", "system", version=p["version"], plan_hash=p["hash"],
          confirm_on_approve=[i["id"] for i in pending])
    return {
        "plan_version": p["version"], "plan_hash": p["hash"],
        "documents": [{"name": "급여계좌 변경 신청서", "tag": "AI 초안"}, {"name": "통장사본", "tag": "시스템 발급"}],
        "payroll_draft": (f"급여 지급 계좌 변경 신청\n성명: {data.CUSTOMER['name']}\n변경 계좌: {data.CUSTOMER['won_account']}\n"
                          f"적용 희망: {sal.year}년 {sal.month}월 급여분부터"),
        "target": f"다음 급여일 {fmt(sal)}",
        "on_approve": ["신청서를 회사 담당자에게 전달", f"자동이체 변경 {n_move}건을 공식 절차로 신청", "승인 전에는 아무것도 실행하지 않음"],
        "approval_status": _approval_status(s),
        "needs_customer_action": "고객이 화면에서 인증하고 승인해야 함(Agent는 승인할 수 없음)",
        "confirm_on_approve": [f"{i['label']}: {i['note']}" for i in pending],
        "auth_boundary": {"demo": s.auth.describe, "production": "본인 인증 + 전자서명 → 승인 토큰을 계획 해시에 서명"},
    }


def grant_approval(s: Session, plan_hash: str) -> dict:
    """고객 승인(화면의 '인증하고 승인하기'). LLM은 이 함수를 부를 수 없다(도구 목록에 없음).

    인증 어댑터가 발급한 승인 토큰을 현재 계획 해시에 묶는다. 화면의 해시가 현재 계획과 다르면 거부한다.
    """
    p = s.plan
    if not p:
        return {"ok": False, "reason": "승인할 계획이 없습니다"}
    if plan_hash != p["hash"]:
        s.log("APPROVAL_REJECTED", "system", reason="화면의 계획 해시가 현재 계획과 다름", presented_hash=plan_hash,
              current_hash=p["hash"], current_version=p["version"])
        return {"ok": False, "reason": "stale"}
    token = s.auth.authenticate(session_id=s.sid, plan_hash=p["hash"], plan_version=p["version"], at=s.today.isoformat())
    if not token:
        s.log("AUTH_FAILED", "customer", authenticator=s.auth.name, plan_hash=p["hash"])
        return {"ok": False, "reason": "인증 실패"}
    s.log("AUTHENTICATED", "customer", authenticator=s.auth.name, mock=s.auth.mock, method=token.get("method"))
    confirmed = [i["id"] for i in _pending_confirm(s, p["steps"])]
    for iid in confirmed:  # 승인 화면에 '확인 필요'로 나열된 항목은 승인과 함께 고객 확정으로 남긴다
        s.decisions[iid] = "confirmed"
        s.log("ITEM_DECIDED", "customer", item=iid, decision="confirm", via="승인 화면")
    s.approval = {"plan_hash": p["hash"], "version": p["version"], "at": s.today.isoformat(), "token": token}
    s.log("APPROVAL_GRANTED", "customer", version=p["version"], plan_hash=p["hash"], token_id=token["id"],
          authenticator=s.auth.name, mock=s.auth.mock)
    for st in p["steps"]:
        for iid in st["items"]:
            if s.item_state.get(iid) == "PLANNED" and st["action"] in ("payroll", "move"):
                s.set_state(iid, "APPROVED", f"고객 승인 v{p['version']}")
    return {"ok": True, "version": p["version"], "plan_hash": p["hash"], "token_id": token["id"], "confirmed": confirmed}


def execute_plan(s: Session) -> dict:
    """승인된 현재 계획을 기관 어댑터로 신청. 승인 토큰이 없거나 계획 해시·세션과 맞지 않으면 차단."""
    p = s.plan

    def blocked(reason, **extra):
        s.counters["blocked"] += 1
        s.log("EXEC_BLOCKED", "system", reason=reason, plan_hash=p["hash"] if p else None)
        return {"status": "BLOCKED", "reason": reason, **extra}

    if not p:
        return blocked("계획 없음")
    if not s.approval:
        return blocked("고객 승인 없음", plan_version=p["version"])
    if s.approval["plan_hash"] != p["hash"]:
        return blocked(f"승인한 계획(v{s.approval['version']})과 현재 계획(v{p['version']})이 다름 → 재승인 필요")
    token = s.approval.get("token")
    if not s.auth.verify(token, session_id=s.sid, plan_hash=p["hash"]):
        return blocked("승인 토큰 검증 실패 → 인증부터 다시 필요")
    requested, skipped, scheduled, dup = [], 0, [], 0
    mock = "모의" if s.adapter.simulated else "연동"
    for st in p["steps"]:
        for iid in st["items"]:
            key = f"{p['hash']}:{iid}"
            if key in s.executed:
                skipped += 1
                dup += 1
                s.counters["dup_prevented"] += 1
                continue
            if st["action"] in ("payroll", "move"):
                if s.item_state.get(iid) != "APPROVED":
                    skipped += 1
                    continue
                rc = s.adapter.submit(item_id=iid, action=st["action"], idempotency_key=key, approval_token=token, on_date=s.today)
                s.log("EXEC_REQUESTED", "system", item=iid, action=st["action"], plan_version=p["version"], plan_hash=p["hash"],
                      idempotency_key=key, token_id=token["id"], adapter=s.adapter.name, simulated=s.adapter.simulated,
                      receipt=rc.get("receipt"))
                s.set_state(iid, "REQUESTED", f"공식 절차 신청({mock})")
                s.executed[key] = {"item": iid, "at": s.today.isoformat(), "receipt": rc.get("receipt")}
                requested.append(s.items[iid]["label"])
            elif st["action"] == "defer":
                s.executed[key] = {"item": iid, "at": s.today.isoformat(), "scheduled": st["date"]}
                s.log("EXEC_SCHEDULED", "system", item=iid, plan_hash=p["hash"], idempotency_key=key, scheduled=st["date"])
                scheduled.append(f"{s.items[iid]['label']}({fmt(date.fromisoformat(st['date']))} 출금부터 예약)")
    if dup:
        s.log("EXEC_DUPLICATE_PREVENTED", "system", plan_hash=p["hash"], count=dup)
    return {"status": "REQUESTED", "plan_version": p["version"], "requested": requested,
            "scheduled": scheduled, "duplicate_prevented": skipped,
            "channel": f"{s.adapter.name} · {s.adapter.describe}"}


def advance_to(s: Session, new_today: date) -> list:
    """시간이 지나면 기관 어댑터에서 결과를 받아 상태 머신에 반영한다(데모: 시뮬레이터의 시나리오 결과)."""
    changes = []
    for ev in s.adapter.poll(until=new_today):
        d, iid, new, kind, why = ev.date, ev.item_id, ev.new_state, ev.channel, ev.message
        key = (d, iid, why)
        if key in s.fired_events or iid not in s.item_state:
            continue
        if s.item_state[iid] in ("PLANNED", "KEPT", "APPROVED") and new:
            continue  # 신청하지 않은 항목에는 결과가 오지 않음
        s.fired_events.add(key)
        s.today = d
        s.log("RESULT_RECEIVED", "institution", item=iid, channel=kind, new_state=new, message=why,
              adapter=s.adapter.name, simulated=s.adapter.simulated)
        if new:
            try:
                s.set_state(iid, new, why)
            except TransitionError:
                continue
        else:
            s.evidence[iid].append({"date": d.isoformat(), "state": STATES[s.item_state[iid]], "why": why})
        changes.append({"date": d.isoformat(), "item": s.items[iid]["label"], "state": STATES[s.item_state[iid]], "why": why, "kind": kind})
    s.today = new_today
    return changes


def get_move_status(s: Session) -> dict:
    rows, alerts = [], []
    for iid, st in s.item_state.items():
        if iid not in s.items or s.items[iid]["confidence"] == "excluded":
            continue
        ev = s.evidence.get(iid, [])
        rows.append({"id": iid, "label": s.items[iid]["label"], "state": STATES[st], "last_evidence": ev[-1]["why"] if ev else ""})
        if st == "FAILED" and iid not in s.followups:
            amt = s.items[iid]["amount"]
            nxt = adjust_to_business_day(next_occurrence(s.items[iid]["day"], s.today))
            alerts.append({
                "id": iid, "label": s.items[iid]["label"], "institution": INSTITUTION.get(iid, ""),
                "official_result": "변경 불가 (기관 직접 변경)", "next_debit": f"{fmt(nxt)} · 기존 A은행",
                "needed_balance": amt, "needed_balance_text": f"1회분 · 약 {amt // 10000}만 원",
                "options": [
                    {"id": "direct_request", "title": f"{INSTITUTION.get(iid, '기관')}에 직접 변경 요청", "sub": "필요 서류와 문구를 준비해 드려요"},
                    {"id": "keep_balance", "title": "이번 달은 A은행에 잔액 유지", "sub": f"{fmt(nxt)} 출금 실패를 막아요"},
                ],
            })
    return {"as_of": s.today.isoformat(), "items": rows, "alerts": alerts}


def set_followup(s: Session, item_id: str, option_id: str) -> dict:
    if item_id not in s.items:
        return {"error": f"알 수 없는 항목 id: {item_id}"}
    if s.item_state.get(item_id) != "FAILED":
        return {"error": "실패 상태인 항목만 후속 조치를 정할 수 있음"}
    lab = s.items[item_id]["label"]
    inst = INSTITUTION.get(item_id, "기관")
    nxt = adjust_to_business_day(next_occurrence(s.items[item_id]["day"], s.today))
    if option_id == "direct_request":
        s.set_state(item_id, "PENDING", "고객이 기관에 직접 변경 요청(고객 확인 필요)")
        s.followups[item_id] = option_id
        s.log("FOLLOWUP_SET", "customer", item=item_id, option=option_id)
        return {"item": lab, "option": "direct_request", "documents": ["출금계좌 변경 요청서(문구 초안)", "통장사본"],
                "request_text": f"{inst} 담당자님, {lab} 자동이체 출금계좌를 우리은행 {data.CUSTOMER['won_account']}로 변경 요청드립니다.",
                "next_check": f"{fmt(nxt)} 출금 계좌 확인", "state": STATES["PENDING"]}
    if option_id == "keep_balance":
        s.followups[item_id] = option_id
        s.log("FOLLOWUP_SET", "customer", item=item_id, option=option_id)
        amt = s.items[item_id]["amount"]
        remind = nxt - timedelta(days=1)
        return {"item": lab, "option": "keep_balance", "keep_amount": amt, "reminder": f"{fmt(remind)} 알림",
                "next_debit": fmt(nxt), "state": STATES["FAILED"]}
    return {"error": f"알 수 없는 선택지: {option_id}"}


def get_settlement_status(s: Session) -> dict:
    rows = []
    def tag_of(iid):
        kinds = [e["why"] for e in s.evidence.get(iid, [])]
        if any("공식 결과" in k for k in kinds):
            return "공식 결과"
        if any("거래내역" in k for k in kinds):
            return "거래내역"
        return ""
    if s.item_state.get("SAL") == "DONE":
        rows.append({"title": "첫 급여 입금", "sub": "입금 내역 확인", "tag": "거래내역", "done": True})
    if s.item_state.get("TEL") == "DONE":
        rows.append({"title": "통신비 정상 출금", "sub": "변경 결과 · 첫 출금", "tag": "공식 결과", "done": True})
    if s.item_state.get("INS") == "DONE":
        rows.append({"title": "보험료", "sub": "12월 주기 이전 완료", "tag": "공식 결과", "done": True})
    if s.item_state.get("MGT") in ("FAILED", "PENDING"):
        rows.append({"title": "기존 계좌 반복 납부", "sub": "관리비 · 기존 A은행에서 계속 출금", "tag": "고객 확인", "done": False})
    if s.item_state.get("CLB") == "DEFERRED":
        rows.append({"title": "동호회비", "sub": "1월 출금부터 예약", "tag": "예약", "done": True})
    rows.append({"title": "예상 혜택 · 실제 적용", "sub": "적용 내역 대조", "tag": "확인 대기", "done": False})
    done = sum(r["done"] for r in rows)
    start = data.TODAY
    return {"day": (s.today - start).days + 1, "rows": rows, "done": done, "total": len(rows),
            "rule": "거래 패턴만으로 공식 변경 완료를 확정하지 않음"}


# ---------------------------------------------------------------- 도구 스키마(OpenAI tools 형식)
def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


_ids = {"type": "array", "items": {"type": "string"}}
TOOL_SCHEMAS = [
    _fn("get_transactions", "마이데이터 동의 범위의 거래내역 요약(건수·기간)을 조회한다. 원문 적요는 주지 않는다.",
        {"months": {"type": "integer", "description": "조회 개월 수, 기본 6"}}),
    _fn("detect_recurring", "거래내역에서 반복 거래(급여·자동납부·자동송금·적금·직접송금)를 시스템 규칙으로 탐지한다. 항목 id를 돌려준다."),
    _fn("calc_switch_quote", "우리WON으로 옮길 때의 견적(추가 이자, 가정, 미확인 항목)을 시스템이 계산한다."),
    _fn("find_items", "고객이 말한 이름(예: '회비')에 해당하는 반복 항목을 찾는다. 여러 개면 ambiguous=true.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("plan_schedule", "이전 순서 계획을 만들거나 고친다. 제약은 누적된다. 계획이 바뀌면 새 버전이 되고 기존 승인은 무효가 된다. 실패 알림의 대안(직접 변경 요청·잔액 유지)을 고를 때는 부르지 않는다(→ set_followup).",
        {"keep_item_ids": {**_ids, "description": "옮기지 않고 유지할 항목 id(예: 적금 만기까지)"},
         "defer_item_ids": {**_ids, "description": "다음 달로 미룰 항목 id"},
         "include_item_ids": {**_ids, "description": "기본 제외 항목 중 포함할 id"},
         "undo_item_ids": {**_ids, "description": "앞서 준 제약을 취소할 항목 id"}}),
    _fn("prepare_approval", "현재 계획 버전의 승인 화면(급여계좌 변경 신청서 초안·통장사본·실행 내용)을 준비한다. 승인은 고객만 할 수 있다."),
    _fn("execute_plan", "고객이 승인한 현재 계획을 공식 절차(모의)로 신청한다. 승인이 없거나 계획이 바뀌었으면 BLOCKED."),
    _fn("get_move_status", "이전 항목별 상태(공식 결과·거래내역·고객 확인)와 실패 알림·대안을 조회한다."),
    _fn("set_followup", "실패 알림 뒤 고객이 고른 후속 조치를 기록한다. '직접 변경 요청할게요' → direct_request, '(A은행에) 잔액 유지할게요' → keep_balance. 이사 계획·승인은 바꾸지 않는다.",
        {"item_id": {"type": "string"}, "option_id": {"type": "string", "enum": ["direct_request", "keep_balance"]}},
        ["item_id", "option_id"]),
    _fn("get_settlement_status", "이사 후 정착 현황(확인 완료·고객 확인·확인 대기)을 조회한다."),
]

TOOL_IMPLS = {
    "get_transactions": get_transactions,
    "detect_recurring": detect_recurring,
    "calc_switch_quote": calc_switch_quote,
    "find_items": find_items,
    "plan_schedule": plan_schedule,
    "prepare_approval": prepare_approval,
    "execute_plan": execute_plan,
    "get_move_status": get_move_status,
    "set_followup": set_followup,
    "get_settlement_status": get_settlement_status,
}
EXEC_TOOLS = {"execute_plan"}
