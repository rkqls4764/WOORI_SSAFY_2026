"""꿀머니 원장(시스템). 지급·사용은 모두 여기서만 일어나고 감사 로그에 남는다.

- 같은 키로 두 번 지급하지 않음(중복 지급 방지)
- 1인 누적 지급 한도(CAP) · 마이데이터 사업자의 경제적 이익 제공 한도(3만 원)를 참고한 가정
- 스킨 구매는 고객 화면 조작으로만(LLM 도구 아님) · 잔액 안에서만
- 실제 우리WON꿀머니는 1꿀 = 1원 · 계좌 입금·ATM 출금이 되는 현금성 포인트.
  그래서 '비용 0'이 아니라 '스킨으로 쓰인 만큼은 현금으로 나가지 않음'을 따로 집계한다(spent_on_skins).
"""
from __future__ import annotations

CAP = 30_000


class Ledger:
    def __init__(self, log=None):
        self._log = log or (lambda *a, **k: None)
        self.entries: list[dict] = []
        self.keys: set[str] = set()
        self.issued = 0
        self.spent_on_skins = 0
        self.owned: list[str] = []
        self.equipped: str | None = None

    @property
    def balance(self) -> int:
        return self.issued - self.spent_on_skins

    def earn(self, key: str, amount: int, why: str, on_date: str) -> dict:
        if key in self.keys:
            self._log("REWARD_DUPLICATE_PREVENTED", "system", key=key, amount=amount)
            return {"status": "DUPLICATE", "key": key, "granted": 0, "balance": self.balance}
        grant = max(0, min(amount, CAP - self.issued))
        self.keys.add(key)
        if grant == 0:
            self._log("REWARD_CAPPED", "system", key=key, requested=amount, cap=CAP)
            return {"status": "CAPPED", "key": key, "granted": 0, "balance": self.balance, "cap": CAP}
        self.issued += grant
        self.entries.append({"date": on_date, "kind": "earn", "key": key, "amount": grant, "why": why})
        self._log("REWARD_GRANTED", "system", key=key, amount=grant, why=why, balance=self.balance)
        return {"status": "GRANTED" if grant == amount else "PARTIAL", "key": key, "granted": grant,
                "balance": self.balance, "cap": CAP}

    def buy_skin(self, skin: dict, on_date: str) -> dict:
        if skin["id"] in self.owned:
            self.equipped = skin["id"]
            return {"status": "ALREADY_OWNED", "skin": skin["id"], "balance": self.balance}
        if self.balance < skin["price"]:
            self._log("SKIN_PURCHASE_REJECTED", "customer", skin=skin["id"], price=skin["price"], balance=self.balance)
            return {"status": "INSUFFICIENT", "skin": skin["id"], "price": skin["price"], "balance": self.balance}
        self.spent_on_skins += skin["price"]
        self.owned.append(skin["id"])
        self.equipped = skin["id"]
        self.entries.append({"date": on_date, "kind": "skin", "key": skin["id"], "amount": -skin["price"], "why": skin["name"]})
        self._log("SKIN_PURCHASED", "customer", skin=skin["id"], price=skin["price"], balance=self.balance)
        return {"status": "PURCHASED", "skin": skin["id"], "price": skin["price"], "balance": self.balance}

    def summary(self) -> dict:
        return {"balance": self.balance, "issued": self.issued, "spent_on_skins": self.spent_on_skins,
                "cap": CAP, "owned": list(self.owned), "equipped": self.equipped,
                "skin_use_rate": round(self.spent_on_skins / self.issued, 3) if self.issued else 0,
                "note": "스킨으로 쓰인 꿀머니는 현금으로 나가지 않음(꿀머니 자체는 1꿀 = 1원 현금성)"}
