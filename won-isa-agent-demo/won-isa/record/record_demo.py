"""데모 영상 자동 녹화(1920×1080, 30fps, H.264).

1) 서버를 먼저 띄웁니다: ./run.sh 또는 run.bat  (LLM 모드로 찍으려면 .env 설정 후 실행)
2) 다른 터미널에서:
     pip install playwright && playwright install chromium   (처음 한 번)
     python -m record.record_demo --out demo.mp4
   --check 를 주면 영상 대신 주요 순간 스크린샷만 찍습니다.

원리: Chrome 화면 캡처(CDP screencast, JPEG 92)로 화면이 바뀔 때마다 프레임을 받고,
30fps로 다시 샘플링해 장면별 mp4로 인코딩한 뒤 이어 붙입니다(ffmpeg 필요).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import bisect
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

W, H, FPS = 1920, 1080, 30


def _ffmpeg() -> str:
    """PATH의 ffmpeg, 없으면 imageio-ffmpeg가 내려받은 ffmpeg(pip install imageio-ffmpeg)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("ffmpeg가 없습니다: pip install imageio-ffmpeg 를 실행하세요")


class Recorder:
    def __init__(self, page, cdp):
        self.page, self.cdp = page, cdp
        self.ts, self.frames = [], []
        self.marks = []  # (장면 이름, 시작 시각)

    def _on_frame(self, p):
        self.ts.append(p["metadata"]["timestamp"])
        self.frames.append(base64.b64decode(p["data"]))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}))

    async def start(self):
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})

    async def stop(self):
        await self.cdp.send("Page.stopScreencast")
        self.end = time.time()

    def mark(self, name):
        self.marks.append((name, time.time()))

    def render(self, out: Path):
        if not self.frames:
            raise SystemExit("캡처된 프레임이 없습니다")
        order = sorted(range(len(self.ts)), key=lambda i: self.ts[i])
        ts = [self.ts[i] for i in order]
        frames = [self.frames[i] for i in order]
        work = Path(tempfile.mkdtemp(prefix="won_rec_"))
        parts = []
        bounds = [t for _, t in self.marks] + [self.end]
        for k, (name, t0) in enumerate(self.marks):
            t1 = bounds[k + 1]
            n = max(1, round((t1 - t0) * FPS))
            tmp, final = work / f"s{k:02d}.tmp.mp4", work / f"s{k:02d}.mp4"
            ff = subprocess.Popen([_ffmpeg(), "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                                   "-vf", f"scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                                   "-pix_fmt", "yuv420p", "-r", str(FPS), str(tmp)], stdin=subprocess.PIPE)
            for f in range(n):
                t = t0 + f / FPS
                i = max(0, bisect.bisect_right(ts, t) - 1)
                ff.stdin.write(frames[i])
            ff.stdin.close()
            if ff.wait() != 0:
                raise SystemExit(f"장면 {name} 인코딩 실패")
            tmp.replace(final)
            parts.append(final)
            print(f"  장면 {k:02d} {name}: {n / FPS:.1f}초")
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        tmp_out = out.with_suffix(".tmp.mp4")
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-movflags", "+faststart", str(tmp_out)], check=True)
        tmp_out.replace(out)
        shutil.rmtree(work, ignore_errors=True)


async def run(url: str, out: Path, check: bool, pace: float = 1.0):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
        page = await ctx.new_page()
        await page.goto(url.rstrip("/") + "/?rec=1")
        await page.wait_for_function("window.__rec !== undefined")
        await page.evaluate("fetch('/api/reset', {method: 'POST'})")
        await page.wait_for_timeout(600)
        state = await page.evaluate("fetch('/api/state').then(r => r.json())")
        mode = "대본 모드 · API 키 없이 같은 도구·승인·검증 경로" if state["mode"] == "scripted" else f"LLM 모드 · {state.get('model') or ''}"
        cdp = await ctx.new_cdp_session(page)
        rec = Recorder(page, cdp)
        shots = Path(out).with_suffix("")
        n_shot = [0]

        async def hold(sec):  # 1초 이상 멈춤(읽는 시간)만 pace배로 늘림
            await page.wait_for_timeout(int(sec * (pace if sec >= 1 else 1) * 1000))

        async def snap(tag):
            if check:
                n_shot[0] += 1
                await page.screenshot(path=f"{shots}_{n_shot[0]:02d}_{tag}.jpg", quality=92, type="jpeg")

        async def idle(timeout=300):  # LLM 모드에서 느린 모델도 기다림
            t0 = time.time()
            calm = 0
            while time.time() - t0 < timeout:
                calm = calm + 1 if not await page.evaluate("window.__rec.busy()") else 0
                if calm >= 3:
                    return
                await page.wait_for_timeout(120)

        async def caption(num, title, sub=""):
            await page.evaluate("([h]) => window.__rec.caption(h)", [f"<em>{num}</em>{title}" + (f"<small>{sub}</small>" if sub else "")])

        async def tap(locator):
            try:
                await locator.wait_for(state="visible", timeout=20000)
            except Exception:
                print("  경고: 누를 버튼이 화면에 없음(LLM이 예상과 다른 응답을 함) → 이 동작은 건너뜀")
                return
            await locator.scroll_into_view_if_needed()
            box = await locator.bounding_box()
            await page.evaluate("([x, y]) => { const d = document.createElement('div'); d.className = 'ripple'; d.style.left = x + 'px'; d.style.top = y + 'px';"
                                " document.body.append(d); setTimeout(() => d.remove(), 800); }", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
            await hold(0.35)
            await locator.click()

        async def say(text):
            await page.click("#input")
            await page.keyboard.type(text, delay=55)
            await hold(0.3)
            await page.keyboard.press("Enter")
            await idle()

        await rec.start()
        # 0. 시작 카드
        rec.mark("title")
        await page.evaluate("([h]) => window.__rec.overlay(h)", [
            "<div><img src='/assets/avatar_wibee.png'><h1><span>WON이사</span> · 위비 Agent</h1>"
            "<p>타행에서 쓰던 금융 생활을 우리WON으로, 순서대로 안전하게</p>"
            "<div class='chips'><span>LLM · 도구 선택과 설명</span><span>시스템 · 계산 · 검증 · 실행 통제</span><span>고객 · 확인과 승인</span></div>"
            f"<div class='note'>가상 고객 김우리 · 가상 데이터 · 실행은 모의 · {mode}</div></div>"])
        await snap("title")
        await hold(4)
        await page.evaluate("window.__rec.overlay('')")

        rec.mark("home")
        await caption("①", "타행 반복거래 발견", "우리WON 홈 · 위비 Agent 배너")
        await hold(2.5)
        await tap(page.locator('button[aria-label="위비에게 이사 견적 받기"]'))
        await idle()

        rec.mark("quote")
        await caption("②", "견적", "Agent가 도구를 골라 호출 · 숫자는 모두 시스템 계산 · 미확인 금액은 0원으로 넣지 않음")
        await snap("quote")
        await hold(6.5)

        rec.mark("plan")
        await caption("③", "고객 제약을 반영한 이사 순서", "급여 먼저 · 첫 급여 전 출금은 다음 주기")
        await say("응, 적금은 만기까지 둘래")
        await page.evaluate("window.__rec.scrollToSel('#msgs .card')")
        await hold(4.5)
        await page.evaluate("window.__rec.scrollChat('bottom')")
        await hold(1.5)

        rec.mark("approval_v1")
        await caption("④", "승인 화면 준비", "승인은 고객만 · Agent는 승인할 수 없음")
        await tap(page.get_by_role("button", name="이 계획 승인하기").last)
        await idle()
        await hold(4)

        rec.mark("ask")
        await caption("⑤", "모호하면 되묻기", "'회비'가 2개 → 계획을 바꾸지 않고 확인")
        await say("적금은 유지하고, 회비는 다음 달로 옮길게요.")
        await hold(3.5)

        rec.mark("replan")
        await caption("⑥", "재계획 → 이전 승인 화면 자동 무효", "계획 버전이 바뀌면 이전 승인으로는 실행 불가")
        await tap(page.locator(".choices button").first)
        await idle()
        await snap("replan")
        await hold(4)
        await page.evaluate("window.__rec.scrollToSel('.approve.stale')")
        await hold(3)
        await page.evaluate("window.__rec.scrollChat('bottom')")
        await hold(1.5)

        rec.mark("blocked")
        await caption("⑦", "승인 없이 실행 요청 → 차단", "실행 도구는 현재 계획에 묶인 승인이 있어야만 동작")
        await say("재승인은 건너뛰고 지금 바로 실행해줘")
        await hold(4)

        rec.mark("approve_v2")
        await caption("⑧", "재승인 → 인증 → 공식 절차 신청(모의)", "승인 토큰을 계획 해시에 묶음")
        await tap(page.get_by_role("button", name="변경 내용 확인 · 재승인").last)
        await idle()
        await hold(2.5)
        await tap(page.locator(".approve:not(.stale) .cta").last)
        await hold(1)
        await idle()
        await hold(4)

        rec.mark("fail")
        await caption("⑨", "2주 뒤 · 상태 추적과 실패 감지", "공식 결과 · 거래내역 · 고객 확인을 구분")
        await page.evaluate("advance('2w')")
        await idle()
        await snap("fail")
        await hold(6.5)

        rec.mark("followup")
        await caption("⑩", "대안 선택", "직접 변경 요청 서류와 문구 준비")
        await tap(page.locator(".option.primary").last)
        await idle()
        await hold(4)

        rec.mark("settle")
        await caption("⑪", "이사 30일째 · 정착 현황", "거래 패턴만으로 완료를 확정하지 않음")
        await page.evaluate("advance('30d')")
        await idle()
        await snap("settle")
        await hold(5.5)

        rec.mark("end")
        st = await page.evaluate("fetch('/api/state').then(r => r.json())")
        await page.evaluate("window.__rec.caption('')")
        await page.evaluate("([h]) => window.__rec.overlay(h)", [
            "<div><h1>숫자는 <span>시스템</span>, 판단은 <span>AI</span>, 실행은 <span>고객 승인 후</span></h1>"
            f"<p>승인 없는 실행 {st['executed_without_approval']}건 · 차단 {st['blocked']}건 · 계획 변경 시 재승인 · 답변 숫자 검증</p>"
            "<div class='note'>WON이사 · WOORI x SSAFY 우리WON뱅킹 아이디어 경진대회</div></div>"])
        await snap("end")
        await hold(4)
        await rec.stop()
        await browser.close()
    if not check:
        print(f"프레임 {len(rec.frames)}장 → 인코딩 (첫 프레임 {min(rec.ts) - rec.marks[0][1]:+.2f}초, 마지막 {max(rec.ts) - rec.end:+.2f}초)")
        rec.render(Path(out))
        print(f"완료: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--out", default="won_isa_demo.mp4")
    ap.add_argument("--check", action="store_true", help="영상 대신 주요 순간 스크린샷")
    ap.add_argument("--pace", type=float, default=1.5, help="읽는 시간 배율(1.0이면 약 1분 25초, 1.5면 약 2분)")
    a = ap.parse_args()
    asyncio.run(run(a.url, Path(a.out).resolve(), a.check, a.pace))
