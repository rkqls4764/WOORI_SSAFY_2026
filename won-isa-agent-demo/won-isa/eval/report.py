"""평가 결과(JSON)를 발표·문서용 마크다운으로 정리한다. 실행: python -m eval.report [eval/results_openai.json]"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
LABEL = {"model_only_clean": "모델 단독 통과(가드 개입 없이)", "guard_interventions": "가드가 모델 출력을 고친 시나리오 수",
         "final_plan_correct": "최종 계획 정확(가드 반영 후)", "tool_choice": "도구 선택", "clarify_correct": "모호할 때 되묻기", "no_exec_without_approval": "승인 없는 실행 차단",
         "reapproval_required": "계획 변경 후 재승인 요구", "executed_after_approval": "승인 후 신청",
         "number_guard_pass": "숫자 검증 통과(턴)", "tokens_per_turn_avg": "턴당 평균 토큰", "latency_p50_s": "응답 시간 중앙값(초)",
         "quote_tokens": "견적 1건 토큰", "quote_cost_krw_est": "견적 1건 비용 추정(원)"}


def write_report(results_path: Path) -> Path:
    r = json.loads(Path(results_path).read_text(encoding="utf-8"))
    m, s = r["meta"], r["summary"]
    L = [f"# WON이사 Agent 평가 결과 · {m['model']}", ""]
    if m["mode"] == "scripted":
        L += ["> 대본 모드 결과입니다. 정책·통제 점검용이며, 발표 수치는 LLM 모드로 다시 돌린 결과를 써야 합니다.", ""]
    L += ["## 재현 정보", "", "| 항목 | 값 |", "| --- | --- |",
          f"| 실행 일시 | {m['run_at']} |", f"| 모드 · 모델 | {m['mode']} · {m['model']} |"]
    if m["mode"] != "scripted":
        L += [f"| 모델 서버 | {m['base_url']} |", f"| temperature · 시드 | {m['temperature']} · {m['llm_seed'] or '없음'} |"]
    L += [f"| 데이터 | {m['data']} |", f"| 코드 지문(server/*.py + 평가 스크립트) | `{m['code_fingerprint']}` |",
          f"| 환경 | Python {m['python']} · {m['os']} · GPU {m.get('gpu') or '없음'} |", f"| 소요 시간 | {m['duration_s']}초 |",
          f"| 재현 명령 | `{m['command']}` |", "", f"{m['note']}", "",
          "## 요약", "", f"**시나리오 성공률 {s['pass_rate']}**", "", "| 지표 | 값 |", "| --- | --- |"]
    L += [f"| {LABEL[k]} | {s[k]} |" for k in LABEL if k in s]
    L += ["", "## 시스템 통제(LLM과 무관하게 항상 통과해야 함)", "", "상황(계획·승인)은 대본 정책으로 만들고 시스템 게이트만 시험합니다.", "", "| 통제 | 결과 |", "| --- | --- |"]
    L += [f"| {k} | {'통과' if v else '**실패**'} |" for k, v in r["system_controls"].items()]
    L += ["", f"## 실패 사례 ({len(r['failures'])}건)", ""]
    if not r["failures"]:
        L += ["이번 실행에서는 실패한 시나리오가 없습니다.", ""]
    for f in r["failures"]:
        L += [f"### {f['name']}", "", f"- 입력: {f['input']}", f"- 호출한 도구: {', '.join(f['tools']) or '없음'}",
              f"- 통과하지 못한 확인: {', '.join(f['failed_checks'])}", f"- Agent 답변: {f['reply'] or '(없음)'}"]
        L += [f"- 시스템 기록: {b}" for b in f["blocks"]] + [""]
    L += ["## 시나리오별 결과", "", "| # | 시나리오 | 입력 | 결과 | 호출한 도구 |", "| --- | --- | --- | --- | --- |"]
    for i, row in enumerate(r["rows"], 1):
        L.append(f"| {i} | {row['name']} | {row['input']} | {'통과' if row['pass'] else '**실패**'} | {', '.join(row['tools']) or '-'} |")
    d = HERE / "detect_results.json"
    if d.is_file():
        det = json.loads(d.read_text(encoding="utf-8"))
        o = det["overall"]
        L += ["", "## 반복 거래 탐지 정확도", "",
              f"가상 고객 {det['meta']['customers']}명 · 시드 {det['meta']['seed']} · 규칙: {det['meta']['rule']}", "",
              f"활성 월 반복 항목 기준 정탐 {o['tp']} · 오탐 {o['fp']} · 미탐 {o['fn']} → 정밀도 {o['precision']:.1%} · 재현율 {o['recall']:.1%} · F1 {o['f1']:.1%}",
              f"자동 탐지(고객 확인 없이 계획에 들어가는) 항목 중 오탐: {o['fp_high']}건", "",
              "| 유형 | 정답 | 건수 | 정탐 | 미탐 | 오탐 | 잡지 않음 | 비고 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        L += [f"| {t['type']} | {t['truth']} | {t['n']} | {t['tp']} | {t['fn']} | {t['fp']} | {t['tn']} | {t['note']} |" for t in det["by_type"]]
        L += ["", f"재현: `{det['meta']['command']}`"]
    out = HERE / f"REPORT_{m['mode']}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    for p in ([Path(a) for a in sys.argv[1:]] or sorted(HERE.glob("results_*.json"))):
        print(write_report(p))
