"""고객 인증 경계.

승인 API는 '계획 해시가 맞는지'만 보지 않는다. 인증 어댑터가 발급한 승인 토큰(세션·계획 해시에 서명)이 있어야
실행 게이트(tools.execute_plan)를 통과한다. 데모는 MockAuthenticator를 쓰고, 운영에서는 같은 인터페이스에
은행 본인 인증·전자서명을 연결한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Protocol


class Authenticator(Protocol):
    name: str
    mock: bool
    describe: str

    def authenticate(self, *, session_id: str, plan_hash: str, plan_version: int, at: str) -> dict | None:
        """고객 본인 확인에 성공하면 승인 토큰을, 실패하면 None을 돌려준다."""

    def verify(self, token: dict | None, *, session_id: str, plan_hash: str) -> bool:
        """토큰이 이 인증기가 발급했고, 이 세션·이 계획 해시에 묶여 있는지 확인한다."""


class MockAuthenticator:
    """데모용: 화면의 '인증하고 승인하기' 버튼을 누르면 본인 확인에 성공한 것으로 본다(본인 확인 없음)."""
    name = "MockAuthenticator"
    mock = True
    describe = "모의 인증(버튼 확인만, 본인 확인 없음)"

    def __init__(self):
        self._secret = secrets.token_bytes(32)

    def _sign(self, body: dict) -> str:
        msg = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def authenticate(self, *, session_id, plan_hash, plan_version, at):
        tok = {"id": "apv-" + secrets.token_hex(4), "method": "mock", "subject": session_id,
               "plan_hash": plan_hash, "plan_version": plan_version, "issued_at": at}
        tok["sig"] = self._sign(tok)
        return tok

    def verify(self, token, *, session_id, plan_hash):
        if not isinstance(token, dict) or "sig" not in token:
            return False
        body = {k: v for k, v in token.items() if k != "sig"}
        return (hmac.compare_digest(str(token["sig"]), self._sign(body))
                and body.get("subject") == session_id and body.get("plan_hash") == plan_hash)


class BankAuthenticator:
    """운영 연동 자리(미구현). 은행 앱의 본인 인증(생체·인증서)과 전자서명을 여기에 연결한다.

    - authenticate: 로그인된 고객 세션에서 본인 인증을 요청하고, 계획 해시를 전자서명한 승인 토큰을 받는다.
    - verify: 서명 검증 + 토큰 만료·폐기 확인.
    """
    name = "BankAuthenticator"
    mock = False
    describe = "본인 인증 + 전자서명(운영)"

    def authenticate(self, **kw):
        raise NotImplementedError("운영 연동 자리: 은행 본인 인증·전자서명")

    def verify(self, token, **kw):
        raise NotImplementedError("운영 연동 자리: 서명 검증")
