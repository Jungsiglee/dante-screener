const LABEL = {
  cross_256: "256 골든크로스",
  settled_112: "112 안착",
  break_224: "224 돌파",
  reverse_align: "장기 역배열",
  bottomed: "바닥 이력",
  volume: "거래량 급증",
};
const KEY = ["cross_256", "settled_112"]; // 핵심 조건은 진하게

const won = (n) => (n == null ? "—" : n.toLocaleString("ko-KR"));
const eok = (n) => (n ? (n / 1e8 >= 10000 ? (n / 1e12).toFixed(1) + "조" : Math.round(n / 1e8).toLocaleString("ko-KR") + "억") : "—");
const pct = (n, d = 1) => (n == null ? "—" : (n > 0 ? "+" : "") + n.toFixed(d) + "%");
const dir = (n) => (n > 0 ? "up" : n < 0 ? "down" : "flat");

let DATA = null, sortKey = "score";

function render() {
  const ul = document.getElementById("list");
  const items = [...DATA.items].sort((a, b) => (b[sortKey] ?? -Infinity) - (a[sortKey] ?? -Infinity));
  if (!items.length) {
    ul.innerHTML = `<div class="empty"><b>오늘은 걸린 종목이 없습니다</b>
      조건이 좁을 수 있습니다. scan.py 의 상수를 조정해 다시 돌려보세요.</div>`;
    return;
  }
  ul.innerHTML = items.map(row).join("");
  ul.querySelectorAll(".row").forEach((el) =>
    el.addEventListener("click", () => el.parentElement.classList.toggle("open"))
  );
}

function row(it) {
  const chips = it.flags
    .map((f) => `<span class="chip${KEY.includes(f) ? " hi" : ""}">${LABEL[f] || f}</span>`)
    .join("");
  const age = it.crossAge === 0 ? "오늘 발생" : it.crossAge != null ? `${it.crossAge}일 전` : null;
  return `<li>
    <div class="row">
      <div class="gauge"><i style="height:${Math.max(4, it.score)}%"></i></div>
      <div>
        <div class="nm">${it.name}<span class="code">${it.ticker}</span></div>
        <div class="chips">${chips}</div>
      </div>
      <div class="px">
        <div class="p">${won(it.close)}</div>
        <div class="d ${dir(it.change)}">${pct(it.change, 2)}</div>
        <div class="sc"><b>${it.score}</b> · 차트 ${it.chart} · 재무 ${it.fin}</div>
      </div>
    </div>
    <div class="detail">
      <div class="grid">
        ${cell("5일선", won(it.ma.ma5))}
        ${cell("20일선", won(it.ma.ma20))}
        ${cell("60일선", won(it.ma.ma60))}
        ${cell("112일선", won(it.ma.ma112))}
        ${cell("224일선", won(it.ma.ma224))}
        ${cell("448일선", won(it.ma.ma448))}
        ${cell("시가총액", eok(it.cap))}
        ${cell("고점 대비", pct(it.fromPeak))}
        ${cell("거래량 배수", it.volRatio ? it.volRatio.toFixed(2) + "x" : "—")}
        ${cell("매출 3년", pct(it.revCagr), it.revCagr == null)}
        ${cell("영업익 3년", it.opStatus || pct(it.opCagr), it.opCagr == null && !it.opStatus)}
        ${age ? cell("크로스", age) : cell("크로스", "—", true)}
      </div>
      <div class="plan">
        <span>손절 <b>${won(it.stop)}</b> (20일선)</span>
        <span>1차 <b>${target(it.t1, it.close)}</b></span>
        <span>2차 <b>${target(it.t2, it.close)}</b></span>
      </div>
      <div class="links">
        <a href="https://m.stock.naver.com/domestic/stock/${it.ticker}/total">네이버 증권</a>
        <a href="https://dart.fss.or.kr/dsab007/main.do?textCrpNm=${encodeURIComponent(it.name)}">DART 공시</a>
      </div>
    </div>
  </li>`;
}

const cell = (k, v, dim) => `<div class="cell${dim ? " dim" : ""}">${k}<b>${v}</b></div>`;
const target = (t, close) =>
  t == null ? "—" : t <= close ? `${won(t)} 이미 위` : `${won(t)} (+${(((t / close) - 1) * 100).toFixed(0)}%)`;

function head() {
  const p = DATA.params;
  document.getElementById("meta").innerHTML =
    `<span>거래일 <b>${DATA.tradeDate}</b></span>
     <span>포착 <b>${DATA.items.length}</b>종목</span>
     <span>모집단 <b>${DATA.universe.toLocaleString("ko-KR")}</b></span>`;
  document.getElementById("note").innerHTML =
    `스캔 ${DATA.generatedAt.replace("T", " ").slice(0, 16)} · 일봉 ${DATA.sessions}거래일 ·
     시총 ${eok(p.minCap)} 이상 · 안착 ${p.settleDays}일 · 낙폭 ${(p.drawdown * 100).toFixed(0)}% ·
     거래량 ${p.volMult}배<br><br>
     조건 판정 기준은 임의로 정한 출발점입니다. 백테스트로 검증하기 전까지 점수를 매매 근거로 쓰지 마세요.`;
}

async function boot() {
  try {
    const res = await fetch("result.json?v=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    DATA = await res.json();
  } catch (e) {
    try {
      DATA = await (await caches.match("result.json")).json();
      document.getElementById("meta").textContent = "오프라인 — 마지막으로 받은 결과입니다";
    } catch {
      document.getElementById("list").innerHTML =
        `<div class="empty"><b>결과를 불러오지 못했습니다</b>
         GitHub Actions 가 아직 result.json 을 만들지 않았거나, 네트워크가 끊겼습니다.</div>`;
      return;
    }
  }
  head();
  render();
}

document.getElementById("sorts").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  sortKey = b.dataset.k;
  document.querySelectorAll("#sorts button").forEach((x) =>
    x.setAttribute("aria-pressed", String(x === b))
  );
  render();
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
boot();
