"""LLM 연결 확인: 견적 한 턴을 돌려 도구 호출·숫자 검증·시간을 출력한다."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from server import tools as T
from server.agent import Agent

t = time.time()
a = Agent(T.Session())
print("mode:", a.llm.name, "| model:", getattr(a.llm, "model", "-"))
r = a.run_turn("이사 견적 내줘")
print("tools:", [l["title"] for l in r["log"] if l["kind"] == "tool"])
print("cards:", [c["type"] for c in r["cards"]], "| guard_ok:", r["guard_ok"], "| tokens:", r["usage"], "| sec:", round(time.time() - t, 1))
print("reply:", " / ".join(r["before"] + r["after"]))
ok = a.llm.name != "scripted" and any(c["type"] == "quote" for c in r["cards"])
print("SMOKE_OK" if ok else "SMOKE_FAIL")
sys.exit(0 if ok else 1)
