"""숫자 검증: LLM 답변 속 숫자가 이번 대화의 도구 결과(또는 고객 발화)에 있는지 확인한다."""
from __future__ import annotations

import re

_UNIT = {"억": 100_000_000, "만": 10_000, "천": 1_000}
_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"

# 금액: '60,000원', '6만 원', '약 18만 원', '3,200,000'
_RE_MONEY = re.compile(rf"({_NUM})\s*(억|만|천)?\s*원")
_RE_MAN = re.compile(rf"({_NUM})\s*(억|만|천)(?!\s*원)")
# 비율: '3.1%', '3.0%p'
_RE_PCT = re.compile(rf"({_NUM})\s*%p?")
# 날짜: '11.25', '12월 15일', '2026-11-25'
_RE_DATE_DOT = re.compile(r"(?<![\d.])(1[0-2]|[1-9])\.(\d{1,2})(?![\d%])")
_RE_DATE_KO = re.compile(r"(1[0-2]|[1-9])월\s*(\d{1,2})일")
_RE_DATE_ISO = re.compile(r"20\d\d-(\d\d)-(\d\d)")
# 그 밖의 정수(건수 등)
_RE_INT = re.compile(rf"({_NUM})")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def extract(text: str) -> set:
    """텍스트에서 비교 가능한 숫자 토큰 집합을 뽑는다."""
    out, used = set(), []
    t = str(text)
    for m in _RE_DATE_ISO.finditer(t):
        out.add(("date", int(m.group(1)), int(m.group(2)))); used.append(m.span())
    for m in _RE_DATE_KO.finditer(t):
        out.add(("date", int(m.group(1)), int(m.group(2)))); used.append(m.span())
    for m in _RE_MONEY.finditer(t):
        v = _num(m.group(1)) * _UNIT.get(m.group(2) or "", 1)
        out.add(("num", round(v))); used.append(m.span())
    for m in _RE_MAN.finditer(t):
        if any(a <= m.start() < b for a, b in used):
            continue
        v = _num(m.group(1)) * _UNIT[m.group(2)]
        out.add(("num", round(v))); used.append(m.span())
    for m in _RE_PCT.finditer(t):
        out.add(("pct", round(_num(m.group(1)), 2))); used.append(m.span())
    for m in _RE_DATE_DOT.finditer(t):
        if any(a <= m.start() < b for a, b in used):
            continue
        out.add(("date", int(m.group(1)), int(m.group(2)))); used.append(m.span())
    for m in _RE_INT.finditer(t):
        if any(a <= m.start() < b for a, b in used):
            continue
        out.add(("num", round(_num(m.group(1)), 2)))
    return out


def allowed_from(obj, acc: set | None = None) -> set:
    """도구 결과(JSON)에 등장하는 모든 숫자·날짜를 허용 집합으로 만든다."""
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for v in obj.values():
            allowed_from(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            allowed_from(v, acc)
        acc.add(("num", len(obj)))  # 목록 길이(건수)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        acc.add(("num", round(obj, 2)))
        acc.add(("pct", round(float(obj), 2)))
    elif isinstance(obj, str):
        acc |= extract(obj)
        for tok in extract(obj):
            if tok[0] == "pct":
                acc.add(("num", tok[1]))
    return acc


def check(text: str, allowed: set) -> list:
    """허용 집합에 없는 숫자 토큰 목록(비어 있으면 통과). 0~10의 작은 정수는 서수·개수로 보고 허용."""
    bad = []
    for tok in extract(text):
        if tok[0] == "num" and 0 <= tok[1] <= 10 and float(tok[1]).is_integer():
            continue
        if tok in allowed:
            continue
        if tok[0] == "pct" and ("num", tok[1]) in allowed:
            continue
        bad.append(tok)
    return bad
