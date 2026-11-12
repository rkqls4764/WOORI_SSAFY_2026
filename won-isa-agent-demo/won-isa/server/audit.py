"""감사 이벤트 로그.

- 추가만 한다(append-only). 고치거나 지우는 함수가 없다.
- 이벤트마다 앞 이벤트의 해시를 품는다(해시 체인). 중간을 고치거나 빼면 verify()가 끊어진 위치를 찾는다.
- 데모: 메모리 + JSONL 파일(logs/audit.jsonl). 운영: 같은 형식을 변경 불가 저장소(WORM)·감사 DB로 보낸다(_persist 자리).

남기는 것: 계획 버전·해시, 승인 화면 준비, 인증·승인(토큰 id), 승인 무효, 실행 차단, 실행 요청(멱등키·접수증),
결과 수신, 항목 상태 전이, 고객의 항목 결정, 후속 조치, 도구 호출, 숫자 검증 실패.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
GENESIS = "0" * 64
ROOT = Path(__file__).resolve().parent.parent
_FIELDS = ("seq", "case_id", "ts", "sim_date", "actor", "type", "payload", "prev")


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(ev: dict) -> str:
    return hashlib.sha256(_canon({k: ev[k] for k in _FIELDS}).encode()).hexdigest()


def verify(events: list[dict]) -> dict:
    """체인 검증. 깨진 첫 이벤트 번호를 broken_at에 담는다."""
    prev = GENESIS
    for i, ev in enumerate(events, 1):
        if ev.get("seq") != i or ev.get("prev") != prev or ev.get("hash") != digest(ev):
            return {"ok": False, "count": len(events), "broken_at": i, "head": prev}
        prev = ev["hash"]
    return {"ok": True, "count": len(events), "broken_at": None, "head": prev}


class AuditLog:
    def __init__(self, case_id: str | None = None, path: Path | None = None):
        self.case_id = case_id or "case-" + secrets.token_hex(4)
        self._events: list[dict] = []
        if path is None and os.getenv("AUDIT_PERSIST", "1") != "0":
            path = Path(os.getenv("AUDIT_PATH") or ROOT / "logs" / "audit.jsonl")
        self.path = path

    def append(self, type_: str, actor: str, payload: dict | None = None, sim_date: str = "") -> dict:
        """actor: customer | llm | system | institution"""
        ev = {"seq": len(self._events) + 1, "case_id": self.case_id,
              "ts": datetime.now(KST).isoformat(timespec="milliseconds"), "sim_date": sim_date,
              "actor": actor, "type": type_, "payload": json.loads(_canon(payload or {})),
              "prev": self._events[-1]["hash"] if self._events else GENESIS}
        ev["hash"] = digest(ev)
        self._events.append(ev)
        self._persist(ev)
        return ev

    def _persist(self, ev: dict):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(_canon(ev) + "\n")
        except OSError:
            self.path = None  # 파일에 못 쓰는 환경이면 메모리에만 둔다

    @property
    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    def verify(self) -> dict:
        return verify(self._events)

    def exec_without_approval(self) -> int:
        """로그만 보고 센다: 같은 계획 해시에 대한 승인 이벤트 없이 나간 실행 요청 수(항상 0이어야 함)."""
        granted, n = set(), 0
        for ev in self._events:
            if ev["type"] == "APPROVAL_GRANTED":
                granted.add(ev["payload"].get("plan_hash"))
            elif ev["type"] == "EXEC_REQUESTED" and ev["payload"].get("plan_hash") not in granted:
                n += 1
        return n
