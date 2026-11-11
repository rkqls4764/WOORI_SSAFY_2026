"""반복 거래 탐지 정확도 평가: 정답이 붙은 가상 고객 N명에게 server/detector.py를 그대로 돌려 정탐·오탐·미탐을 센다.

실행: python -m eval.detect_eval [--n 100] [--seed 2026]
출력: 표 + eval/detect_results.json  (eval/REPORT_*.md 와 /eval.html 에 함께 실린다)

정답 기준
- 활성 월 반복(급여·자동납부·자동송금·적금·매월 직접송금): 탐지돼야 한다(자동 탐지든 '확인 필요'든). 못 찾으면 미탐.
- 해지된 구독: '해지 추정'으로 빠지거나 안 잡혀야 한다. 살아 있는 항목으로 잡히면 오탐.
- 월 반복이 아닌 것(불규칙 지인 송금·매주 송금·격월/분기 납부): 안 잡혀야 한다. 잡히면 오탐.
  격월·분기 납부는 실제로는 옮겨야 할 반복 거래지만 현재 규칙(월 1회)의 범위 밖이라 따로 센다.
모든 고객·거래는 가상이다. 이 수치는 '규칙이 설계대로 동작하는지'와 '어디서 틀리는지'를 보여 주는 것이고,
실제 거래 데이터에서의 정확도를 뜻하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import data, detector  # noqa: E402
from server.data import Tx, _monthly  # noqa: E402
from server.rules import adjust_to_business_day  # noqa: E402

TODAY, START = data.TODAY, data.PERIOD_START
END = TODAY - timedelta(days=1)

# 유형: (이름, 정답, 비고)
TYPES = [
    ("급여", "활성", "매월 고정일, 휴일이면 직전 영업일"),
    ("자동납부(고정 금액)", "활성", "보험·구독"),
    ("자동납부(금액 변동)", "활성", "통신·관리비·공과금 → '확인 필요'로 잡히는 것이 정상"),
    ("자동송금", "활성", "월세·회비"),
    ("적금", "활성", ""),
    ("직접송금(매월)", "활성", "날짜가 며칠씩 흔들림 → '확인 필요'"),
    ("한 달 건너뜀", "활성", "잔액 부족 등으로 1회 빠진 자동납부"),
    ("신규(2회뿐)", "활성", "최근 2개월 전에 시작 → 최소 3회 규칙의 한계"),
    ("해지된 구독", "해지", "2~4개월 전 마지막 출금"),
    ("격월·분기 납부", "범위 밖", "실제 반복이지만 월 1회 규칙 밖"),
    ("불규칙 지인 송금", "비반복", "같은 사람에게 3~7회, 날짜 무작위"),
    ("매주 송금", "비반복", "간격 7일"),
]


def _jit(d: date, rng, k: int) -> date:
    return min(max(d + timedelta(days=rng.randint(-k, k)), START), END)


def make_customer(rng: random.Random, cid: int) -> tuple[list, dict]:
    """거래 목록과 정답표 {(상대, 채널): 유형}."""
    txs, truth = [], {}

    def add(name, ch, typ, dates, amount_fn):
        cp = f"C{cid}-{name}"
        for d in dates:
            txs.append(Tx(d.isoformat(), amount_fn(), ch, cp))
        truth[(cp, ch)] = typ

    months = lambda day, s=START, e=END: list(_monthly(day, s, e))
    if rng.random() < 0.9:
        pay = rng.choice([10, 15, 21, 25])
        add("회사", "급여입금", "급여", [adjust_to_business_day(d, backward=True) for d in months(pay)], lambda: 2_800_000)
    for k in range(rng.randint(1, 3)):
        amt = rng.randrange(9_900, 150_000, 100)
        add(f"고정납부{k}", "CMS자동납부", "자동납부(고정 금액)", [adjust_to_business_day(d) for d in months(rng.randint(1, 28))], lambda a=amt: -a)
    for k in range(rng.randint(1, 2)):
        base, spread = rng.randrange(40_000, 250_000, 1_000), rng.choice([0.02, 0.08, 0.2, 0.3])
        add(f"변동납부{k}", "CMS자동납부", "자동납부(금액 변동)", [adjust_to_business_day(d) for d in months(rng.randint(1, 28))],
            lambda b=base, sp=spread: -int(b * (1 + rng.uniform(-sp, sp))))
    if rng.random() < 0.6:
        amt = rng.choice([30_000, 50_000, 450_000, 600_000])
        add("자동송금", "자동송금", "자동송금", [adjust_to_business_day(d) for d in months(rng.randint(1, 28))], lambda a=amt: -a)
    if rng.random() < 0.5:
        amt = rng.choice([100_000, 300_000, 500_000])
        add("적금", "적금이체", "적금", [adjust_to_business_day(d) for d in months(rng.randint(1, 28))], lambda a=amt: -a)
    if rng.random() < 0.4:
        amt = rng.choice([10_000, 15_000, 20_000, 50_000])
        add("모임회비", "즉시이체", "직접송금(매월)", [_jit(d, rng, 5) for d in months(rng.randint(6, 22))], lambda a=amt: -a)
    if rng.random() < 0.25:
        ds = [adjust_to_business_day(d) for d in months(rng.randint(1, 28))]
        if len(ds) >= 5:
            ds.pop(rng.randint(1, len(ds) - 2))
        add("건너뜀납부", "CMS자동납부", "한 달 건너뜀", ds, lambda: -55_000)
    if rng.random() < 0.3:
        s = date(TODAY.year, TODAY.month, 1) - timedelta(days=45)
        add("신규구독", "CMS자동납부", "신규(2회뿐)", [adjust_to_business_day(d) for d in months(rng.randint(1, 14), s)][-2:], lambda: -14_900)
    if rng.random() < 0.4:
        e = TODAY - timedelta(days=rng.randint(60, 120))
        add("해지구독", "CMS자동납부", "해지된 구독", [adjust_to_business_day(d) for d in months(rng.randint(1, 28), START, e)], lambda: -10_900)
    if rng.random() < 0.3:
        step, day = rng.choice([2, 3]), rng.randint(1, 28)
        add("분기납부", "CMS자동납부", "격월·분기 납부", [adjust_to_business_day(d) for d in months(day)][::step], lambda: -120_000)
    for k in range(rng.randint(0, 3)):
        n = rng.randint(3, 7)
        ds = sorted(START + timedelta(days=rng.randrange((END - START).days)) for _ in range(n))
        add(f"지인{k}", "즉시이체", "불규칙 지인 송금", ds, lambda: -rng.randrange(10_000, 200_000, 1_000))
    if rng.random() < 0.15:
        d0 = START + timedelta(days=rng.randint(0, 6))
        add("용돈", "즉시이체", "매주 송금", [d0 + timedelta(days=7 * i) for i in range(((END - d0).days // 7) + 1)], lambda: -20_000)
    for _ in range(rng.randint(120, 260)):  # 일상 거래(체크카드·ATM): 탐지 대상 채널이 아님
        d = START + timedelta(days=rng.randrange((END - START).days))
        txs.append(Tx(d.isoformat(), -rng.randrange(3_000, 48_000, 100), rng.choice(["체크카드", "체크카드", "ATM"]),
                      rng.choice(["GS25", "스타벅스", "배달의민족", "쿠팡", "CU", "ATM 출금"])))
    txs.sort(key=lambda t: t.date)
    return txs, truth


def run(n: int = 100, seed: int = 2026) -> dict:
    rng = random.Random(seed)
    stat = defaultdict(Counter)
    codes = Counter()
    fp_high, examples = 0, []
    for cid in range(n):
        txs, truth = make_customer(rng, cid)
        found = {(d["counterparty"], d["channel"]): d for d in detector.detect(txs, TODAY)}
        for key, typ in truth.items():
            d = found.get(key)
            kind = dict((t[0], t[1]) for t in TYPES)[typ]
            c = stat[typ]
            c["n"] += 1
            live = d is not None and d["confidence"] != "excluded"
            if d:
                codes[d["code"]] += 1
            if kind == "활성":
                c["tp" if live else "fn"] += 1
                if not live and len(examples) < 6:
                    examples.append({"type": typ, "result": "미탐", "why": "해지 추정으로 분류" if d else "규칙에 걸리지 않음(횟수·간격)"})
                if live:
                    c["high" if d["confidence"] == "high" else "check"] += 1
            else:
                c["fp" if live else "tn"] += 1
                if live:
                    fp_high += d["confidence"] == "high"
                    if len(examples) < 6:
                        examples.append({"type": typ, "result": "오탐", "why": f"{d['count']}회 · 간격 중앙값 {d['median_gap']:g}일 → {d['confidence']}"})
    tp = sum(c["tp"] for c in stat.values()); fn = sum(c["fn"] for c in stat.values()); fp = sum(c["fp"] for c in stat.values())
    prec, rec = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn) if tp + fn else 0
    by_type = []
    for name, kind, note in TYPES:
        c = stat[name]
        extra = f" · 자동 {c['high']} / 확인 필요 {c['check']}" if c["tp"] else ""
        by_type.append({"type": name, "truth": kind, "n": c["n"], "tp": c["tp"], "fn": c["fn"], "fp": c["fp"], "tn": c["tn"],
                        "note": (note + extra).strip(" ·")})
    return {
        "meta": {"customers": n, "seed": seed, "today": TODAY.isoformat(), "period": f"{START.isoformat()}~{END.isoformat()}",
                 "rule": detector.RULE_TEXT, "command": f"python -m eval.detect_eval --n {n} --seed {seed}",
                 "note": "가상 데이터 기준. 실제 거래 데이터에서의 정확도가 아니라 규칙의 동작과 한계를 보여 준다."},
        "overall": {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec,
                    "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0, "fp_high": fp_high},
        "by_type": by_type,
        "confidence_codes": dict(codes),
        "examples": examples,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    r = run(a.n, a.seed)
    o = r["overall"]
    print(f"가상 고객 {a.n}명 · 시드 {a.seed}\n규칙: {r['meta']['rule']}\n")
    print(f"{'유형':<16}{'정답':<6}{'건수':>5}{'정탐':>5}{'미탐':>5}{'오탐':>5}{'안잡음':>5}")
    for t in r["by_type"]:
        print(f"{t['type']:<16}{t['truth']:<6}{t['n']:>5}{t['tp']:>5}{t['fn']:>5}{t['fp']:>5}{t['tn']:>5}")
    print(f"\n정탐 {o['tp']} · 오탐 {o['fp']} · 미탐 {o['fn']} → 정밀도 {o['precision']:.1%} · 재현율 {o['recall']:.1%} · F1 {o['f1']:.1%}")
    print(f"자동 탐지(고객 확인 없이 계획에 들어가는) 항목 중 오탐: {o['fp_high']}건")
    out = Path(__file__).with_name("detect_results.json")
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out}")
