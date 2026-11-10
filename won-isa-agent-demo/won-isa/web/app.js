// WON이사 데모 화면. 서버 응답(LLM 말풍선 + 시스템 카드 + 실행 로그)을 그린다.
const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const won = (n) => Number(n).toLocaleString("ko-KR");
const md = (iso) => { const [, m, d] = iso.split("-").map(Number); return `${m}.${String(d).padStart(2, "0")}`; };

let busy = false, logNo = 0, currentHash = null;

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
}

// ---------------------------------------------------------------- 화면 전환
function show(view) {
  for (const v of ["home", "chat", "settle"]) $(`#view-${v}`).classList.toggle("hidden", v !== view);
}
const msgs = () => $("#msgs");
function scrollDown() { const m = msgs(); requestAnimationFrame(() => m.scrollTo({ top: m.scrollHeight, behavior: "smooth" })); }

// ---------------------------------------------------------------- 말풍선
function userBubble(text) {
  const r = el("div", "row me"); r.append(el("div", "bub", esc(text))); msgs().append(r);
}
function botBubble(text) {
  const r = el("div", "row"); r.append(img("/assets/avatar_wibee.png", "ava"), el("div", "bub", esc(text))); msgs().append(r); return r;
}
function img(src, cls) { const i = el("img", cls); i.src = src; i.alt = ""; return i; }
function typing() { const r = botBubble("•••"); r.classList.add("typing"); scrollDown(); return r; }

// ---------------------------------------------------------------- 카드(시스템 데이터)
const BADGE = { "먼저": "t-b", "진행": "t-b", "대기": "t-gray", "다음 달": "t-y", "만기 후": "t-gray" };

function quoteCard(c) {
  const k = el("div", "card quote indent");
  k.innerHTML = `<span class="tag t-g">시스템 계산</span>
    <div class="label">세전 연환산 추가 이자</div>
    <div class="big">약 ${won(c.extra_interest_pretax_year)}원</div>
    <div class="kv"><span>이전 전 · 가정</span><b>연 ${c.before_rate.toFixed(1)}%</b></div>
    <div class="kv"><span>이전 후 · 조건 충족 가정</span><b>연 ${c.after_rate.toFixed(1)}%</b></div>
    <div class="kv"><span>금리 차이</span><b class="blue">연 ${c.rate_diff.toFixed(1)}%p</b></div>
    <div class="muted">${won(c.assumed_balance / 10000)}만 원 × ${c.rate_diff.toFixed(1)}%p · ${esc(c.assumptions[0])}</div>
    <div class="warn">총순혜택은 ${esc(c.net_benefit)} <span>미확인 ${c.unconfirmed.length}개 ›</span></div>
    <div class="shield">${esc(c.rule)}요</div>`;
  return k;
}

function planCard(c) {
  const k = el("div", "card");
  const steps = c.steps.map((s, i) => `<li><span class="n">${i + 1}</span><div><div class="t">${esc(s.title)}</div><div class="s">${esc(s.sub)}</div></div>
      <span class="tag ${BADGE[s.badge] || "t-gray"}">${esc(s.badge)}</span></li>`).join("");
  const ex = c.excluded.map((x) => `<div><b>제외 · ${esc(x.label)}</b><br>${esc(x.reason)}</div>`).join("");
  k.innerHTML = `<h4>이사 순서 <span class="tag t-g">영업일 규칙</span></h4><ol class="steps">${steps}</ol>
    ${ex ? `<div class="excl">${ex}</div>` : ""}<div class="muted">계획 v${c.version} · 해시 ${c.plan_hash.slice(0, 6)}</div>`;
  return k;
}

function diffCard(c) {
  const k = el("div", "card indent");
  const rows = c.diff.filter((d) => d.id !== "SAL").map((d) =>
    `<tr class="${d.changed ? "chg" : ""}"><td>${esc(d.label)}</td><td>${esc(d.before)}</td><td>${esc(d.after)}</td></tr>`).join("");
  k.innerHTML = `<h4>바뀐 계획 ${c.reapproval_required ? '<span class="tag t-r">재승인 필요</span>' : '<span class="tag t-b">변경됨</span>'}</h4>
    <table class="diff"><tr><th>항목</th><th>변경 전</th><th>변경 후</th></tr>${rows}</table>
    <div class="muted">계획 v${c.version - 1} → v${c.version} · 이전 승인은 무효</div>`;
  const b = el("button", "cta", "변경 내용 확인 · 재승인"); b.onclick = () => send("변경 내용 확인했어요. 재승인할게요");
  k.append(b);
  return k;
}

function approvalCards(c) {
  const docs = el("div", "card");
  docs.innerHTML = `<h4>회사 제출 서류</h4>` + c.documents.map((d) =>
    `<div class="doc"><i>PDF</i><span>${esc(d.name)}</span><span class="tag ${d.tag.includes("AI") ? "t-b" : "t-g"}">${esc(d.tag)}</span></div>`).join("") +
    `<div class="hr"></div><div class="kv"><span>반영 목표</span><b>${esc(c.target)}</b></div>`;
  const ap = el("div", "card approve");
  ap.dataset.hash = c.plan_hash; ap.dataset.version = c.plan_version;
  ap.innerHTML = `<span class="tag t-r" style="background:#fdeee6">고객 승인</span><h5>승인하면 이렇게 진행돼요</h5>
    <ul class="checks">${c.on_approve.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  const b = el("button", "cta lock", "인증하고 승인하기");
  b.onclick = () => approve(c.plan_hash);
  ap.append(b, el("div", "muted", `계획 v${c.plan_version} · 해시 ${c.plan_hash.slice(0, 6)}에만 유효한 승인`));
  return [docs, ap];
}

function execCard(c) {
  if (c.status === "BLOCKED") {
    const k = el("div", "card blocked indent");
    k.innerHTML = `<h4>실행하지 않았어요 <span class="tag t-r">차단</span></h4><div class="kv"><span>이유</span><b>${esc(c.reason)}</b></div>
      <div class="muted">실행 도구는 현재 계획 버전에 묶인 고객 승인이 있어야만 동작합니다</div>`;
    return k;
  }
  const k = el("div", "card indent");
  k.innerHTML = `<h4>공식 절차 신청 <span class="tag t-g">시스템 실행</span></h4>` +
    c.requested.map((x) => `<div class="kv"><span>${esc(x)}</span><span class="tag t-b">신청</span></div>`).join("") +
    c.scheduled.map((x) => `<div class="kv"><span>${esc(x)}</span><span class="tag t-y">예약</span></div>`).join("") +
    (c.duplicate_prevented ? `<div class="muted">이미 신청된 ${c.duplicate_prevented}건은 다시 실행하지 않음(중복 방지)</div>` : "");
  return k;
}

function failureCard(c) {
  const k = el("div", "card fail");
  k.innerHTML = `<h4>${esc(c.label)} · ${esc(c.institution)} <span class="tag t-r">변경 실패</span></h4>
    <div class="kv"><span>공식 결과</span><b>${esc(c.official_result)}</b></div>
    <div class="kv"><span>다음 출금</span><b>${esc(c.next_debit)}</b></div>
    <div class="kv"><span>필요 잔액</span><b>${esc(c.needed_balance_text)}</b></div>
    <span class="tag t-g" style="margin-top:6px">시스템 확인</span>`;
  const opts = c.options.map((o, i) => { const b = el("button", i === 0 ? "option primary" : "option", `<b>${esc(o.title)}</b><span>${esc(o.sub)}</span>`); b.onclick = () => send(o.title); return b; });
  return { card: k, trailing: opts };
}

function followupCard(c) {
  const k = el("div", "card indent");
  if (c.option === "direct_request") {
    k.innerHTML = `<h4>직접 변경 요청 준비 <span class="tag t-y">${esc(c.state)}</span></h4>` +
      c.documents.map((d) => `<div class="doc"><i>DOC</i><span>${esc(d)}</span><span></span></div>`).join("") +
      `<div class="excl">${esc(c.request_text)}</div><div class="kv"><span>다음 확인</span><b>${esc(c.next_check)}</b></div>`;
  } else {
    k.innerHTML = `<h4>잔액 유지 알림 <span class="tag t-y">알림 예약</span></h4>
      <div class="kv"><span>A은행에 남길 금액</span><b>${won(c.keep_amount)}원</b></div><div class="kv"><span>알림</span><b>${esc(c.reminder)}</b></div>`;
  }
  return k;
}

function statusCard(c) {
  const k = el("div", "card indent");
  const tone = { "완료": "t-g", "실패": "t-r", "확인 대기": "t-y", "신청": "t-b", "예약": "t-y" };
  k.innerHTML = `<h4>진행 현황 <span class="tag t-g">시스템 확인</span></h4>` + c.items.filter((i) => i.state !== "계획").map((i) =>
    `<div class="kv"><span>${esc(i.label)}</span><span class="tag ${tone[i.state] || "t-gray"}">${esc(i.state)}</span></div>`).join("");
  return k;
}

function settleView(c, bubbles) {
  const s = $("#settle");
  const tone = { "거래내역": "t-g", "공식 결과": "t-g", "고객 확인": "t-r", "확인 대기": "t-y", "예약": "t-b" };
  s.innerHTML = `<div class="hero"><small>이사 ${c.day}일째</small><h3>거의 다 자리 잡았어요</h3>
      <div class="bar"><i style="width:${Math.round((c.done / c.total) * 100)}%"></i></div><p>${c.total}개 중 ${c.done}개 확인 완료</p>
      <img src="/assets/wibee_hero.png" alt=""></div>
    <div class="list">${c.rows.map((r) => `<div><div><b>${esc(r.title)}</b><span class="s">${esc(r.sub)}</span></div>
      <span class="tag ${tone[r.tag] || "t-gray"}">${esc(r.tag)}</span></div>`).join("")}</div>`;
  const row = el("div", "row"); row.append(img("/assets/avatar_wibee.png", "ava"), el("div", "bub", esc(bubbles.join("\n"))));
  const b = el("button", "cta", "남은 항목 확인하기"); b.onclick = () => { show("chat"); send("남은 항목 확인해 줘"); };
  s.append(row, b, el("div", "rule", esc(c.rule) + "요"));
  show("settle");
}

// ---------------------------------------------------------------- 응답 그리기
function renderReply(r) {
  if (r.divider) msgs().append(el("div", "divider", esc(r.divider)));
  const settle = r.cards.find((c) => c.type === "settle");
  if (settle) { settleView(settle, [...r.before, ...r.after]); return; }
  r.before.forEach(botBubble);
  if (r.tool_count) {
    const p = el("button", "pill-link", `도구 ${r.tool_count}개 실행 · 근거 로그 보기`);
    p.onclick = () => { const pn = $("#panel"); pn.classList.add("flash"); setTimeout(() => pn.classList.remove("flash"), 900); };
    msgs().append(p);
  }
  const trailing = [];
  for (const c of r.cards) {
    if (c.type === "quote") msgs().append(quoteCard(c));
    else if (c.type === "plan") { msgs().append(planCard(c)); const b = el("button", "cta", "이 계획 승인하기"); b.onclick = () => send("이 계획으로 진행할게요"); trailing.push(b); }
    else if (c.type === "plan_diff") msgs().append(diffCard(c));
    else if (c.type === "choices") { const w = el("div", "choices"); c.options.forEach((o) => { const b = el("button", null, esc(o)); b.onclick = () => send(o + "."); w.append(b); }); trailing.push(w); }
    else if (c.type === "approval") approvalCards(c).forEach((x) => msgs().append(x));
    else if (c.type === "exec") msgs().append(execCard(c));
    else if (c.type === "failure") { const f = failureCard(c); msgs().append(f.card); trailing.push(...f.trailing); }
    else if (c.type === "followup") msgs().append(followupCard(c));
    else if (c.type === "status") msgs().append(statusCard(c));
  }
  r.after.forEach(botBubble);
  trailing.forEach((t) => msgs().append(t));
  scrollDown();
}

// 지난 승인 화면은 무효 표시(서버도 해시가 다르면 거부함)
function markStaleApprovals() {
  document.querySelectorAll(".approve").forEach((a) => {
    const stale = currentHash && a.dataset.hash !== currentHash;
    a.classList.toggle("stale", stale);
    const b = a.querySelector(".cta");
    if (stale && b && !b.dataset.stale) { b.dataset.stale = 1; b.classList.add("ghost"); b.textContent = `v${a.dataset.version} 승인 화면 · 계획이 바뀌어 무효`; }
  });
}

// ---------------------------------------------------------------- 실행 로그
const KIND = { tool: "도구", observe: "관찰", check: "검증", block: "차단", approve: "승인", event: "이벤트", llm: "LLM", error: "오류" };
async function renderLog(entries, turnLabel) {
  const log = $("#log");
  log.querySelector(".empty")?.remove();
  log.append(el("li", "turn", esc(turnLabel)));
  for (const e of entries) {
    logNo += 1;
    const li = el("li", `e k-${e.kind}${e.fail ? " fail" : ""}`);
    li.innerHTML = `<span class="no">${String(logNo).padStart(2, "0")}</span><span class="bd">${KIND[e.kind] || e.kind}</span>
      <div><div class="tt ${e.kind === "tool" ? "mono" : ""}">${esc(e.title)}</div>${e.sub ? `<div class="ss">${esc(e.sub)}</div>` : ""}</div>`;
    log.append(li);
    log.scrollTop = log.scrollHeight;
    await new Promise((res) => setTimeout(res, 140));
  }
}

function renderState(s) {
  currentHash = s.plan_hash;
  $("#scene").textContent = `김우리 · 장면 ${s.scene} · 기준일 ${s.today}`;
  $("#subtitle").textContent = s.subtitle;
  $("#st-goal").textContent = s.goal;
  $("#st-plan").textContent = s.plan_version ? `v${s.plan_version} · ${s.approval}` : "계획 전";
  $("#st-unauth").textContent = `${s.executed_without_approval}건`;
  $("#st-sub").textContent = `차단 ${s.blocked}건 · 중복 실행 방지 ${s.dup_prevented}건 · 숫자 검증 실패 ${s.totals.guard_fail}건 · 기준일 ${s.today}`;
  $("#mode").textContent = s.mode === "scripted" ? "대본 모드(API 키 없음)" : `LLM 모드 · ${s.model}`;
  $("#llm-pill").textContent = s.mode === "scripted" ? "대본 모드 · 같은 도구·검증" : `${s.model} · temperature 0`;
  markStaleApprovals();
}

// ---------------------------------------------------------------- 동작
async function run(label, call, userText) {
  if (busy) return; busy = true;
  show("chat");
  if (userText) userBubble(userText);
  const t = typing();
  try {
    const r = await call();
    t.remove();
    renderReply(r);
    renderState(r.state);
    await renderLog(r.log, label);
  } catch (err) {
    t.remove(); botBubble("연결에 문제가 있어요: " + err.message);
  } finally { busy = false; }
}

function send(text) {
  text = text.trim(); if (!text) return;
  run(`고객: ${text}`, () => post("/api/chat", { message: text }), text);
}

async function approve(hash) {
  if (busy) return;
  $("#auth").classList.remove("hidden");
  await new Promise((r) => setTimeout(r, 700));
  $("#auth").classList.add("hidden");
  run("고객: 인증하고 승인", () => post("/api/approve", { plan_hash: hash }), null);
}

async function advance(preset) {
  run(preset === "2w" ? "시간 진행: 2주 뒤" : "시간 진행: 이사 30일째", () => post("/api/advance", { preset }), null);
}

async function reset() {
  await post("/api/reset");
  msgs().innerHTML = ""; $("#log").innerHTML = '<li class="empty">고객 요청이 들어오면 Agent가 부른 도구와 시스템 검증이 여기에 순서대로 쌓입니다.</li>';
  logNo = 0; currentHash = null; show("home");
  const s = await (await fetch("/api/state")).json(); renderState(s);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-say],[data-go],[data-act]");
  if (!t) return;
  if (t.dataset.say) send(t.dataset.say);
  if (t.dataset.go) show(t.dataset.go);
  if (t.dataset.act === "reset") reset();
  if (t.dataset.act === "2w" || t.dataset.act === "30d") advance(t.dataset.act);
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const i = $("#input"); send(i.value); i.value = ""; });
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "r" || e.key === "R") reset();
  if (e.key === "2") advance("2w");
  if (e.key === "3") advance("30d");
});

const REC = new URLSearchParams(location.search).has("rec");
if (REC) document.body.classList.add("rec");
function fit() {
  const k = REC ? Math.min(window.innerWidth / 1200, (window.innerHeight - 120) / 880) : Math.min(window.innerWidth / 1200, (window.innerHeight - 40) / 900, 1.4);
  document.documentElement.style.setProperty("--k", k);
}
// 녹화 스크립트가 쓰는 함수
window.__rec = {
  busy: () => busy || !!document.querySelector(".typing"),
  caption(html) { const c = $("#caption"); if (!html) { c.classList.add("hidden"); return; } c.innerHTML = html; c.classList.remove("hidden"); },
  overlay(html) { const o = $("#overlay"); if (!html) { o.classList.add("hidden"); return; } o.innerHTML = html; o.classList.remove("hidden"); },
  ripple(sel) {
    const t = document.querySelector(sel); if (!t) return false;
    const r = t.getBoundingClientRect(); const d = el("div", "ripple");
    d.style.left = r.left + r.width / 2 + "px"; d.style.top = r.top + r.height / 2 + "px";
    document.body.append(d); setTimeout(() => d.remove(), 800); return true;
  },
  scrollChat(to) { const m = msgs(); m.scrollTo({ top: to === "bottom" ? m.scrollHeight : to, behavior: "smooth" }); },
  scrollToSel(sel) { const all = document.querySelectorAll(sel); const t = all[all.length - 1]; if (t) t.scrollIntoView({ behavior: "smooth", block: "start" }); },
};
window.addEventListener("resize", fit); fit();
reset();
