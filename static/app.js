// AlgTrd live analysis dashboard: plain JS against the FastAPI backend (app/api.py)
const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const sgn = (v, d = 2) => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cls = (v) => v > 0 ? "up" : v < 0 ? "down" : "";
const hhmm = (iso) => (iso || "").slice(11, 16);
const IST = 19800;

// Published copy (GitHub Pages): no server, every /api/... answer is a JSON file in snapshot/,
// exported by the local app (app/snapshot.py) every ~15 minutes while markets are open.
const STATIC = !!window.ALGTRD_STATIC;
let snapVer = "";

function staticUrl(path) {
  const [p, q] = path.replace(/^\/api\//, "").split("?");
  const parts = p.split("/");
  if (parts[0] === "chart") return `snapshot/chart/${parts[1]}_${new URLSearchParams(q || "").get("tf") || 5}.json`;
  return `snapshot/${parts.join("/")}.json`;
}

async function api(path, method = "GET", body) {
  if (STATIC) {
    if (method !== "GET") throw new Error("Not available on the published copy. Run the app on your PC for this.");
    const r = await fetch(staticUrl(path) + "?v=" + encodeURIComponent(snapVer));
    if (!r.ok) throw new Error("Not in this snapshot");
    const j = await r.json();
    const key = new URLSearchParams(path.split("?")[1] || "").get("key");
    return path.startsWith("/api/signals") && key ? { ...j, items: j.items.filter((x) => x.symbol === key) } : j;
  }
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const state = { key: "NIFTY", tf: 5, config: null, overview: null, analysis: null, fitNext: true, tab: "live" };

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  state.tab = b.dataset.tab;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  ({ live: refreshLive, market: renderMarket, signals: loadSignals, news: loadNews, history: loadHistory, quant: () => window.loadQuant?.() })[b.dataset.tab]?.();
}));

// ------------------------------------------------------------- price chart
let pc, candleSeries, ema9S, ema21S, stS, rsiC, rsiS, priceLines = [];

function chartTheme() {
  return {
    layout: { background: { type: "solid", color: css("--surface") }, textColor: css("--ink-2"), fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif" },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--axis") },
    timeScale: { borderColor: css("--axis"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  };
}

function buildCharts() {
  $("#priceChart").innerHTML = "";
  $("#rsiChart").innerHTML = "";
  pc = LightweightCharts.createChart($("#priceChart"), { ...chartTheme(), autoSize: true });
  candleSeries = pc.addCandlestickSeries({ upColor: css("--up"), downColor: css("--down"), borderUpColor: css("--up"),
    borderDownColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down") });
  ema9S = pc.addLineSeries({ color: css("--ema9"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  ema21S = pc.addLineSeries({ color: css("--ema21"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  stS = pc.addLineSeries({ lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  rsiC = LightweightCharts.createChart($("#rsiChart"), { ...chartTheme(), autoSize: true, timeScale: { visible: false } });
  rsiS = rsiC.addLineSeries({ color: css("--ema21"), lineWidth: 2, priceLineVisible: false });
  [70, 30].forEach((p) => rsiS.createPriceLine({ price: p, color: css("--axis"), lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true }));
  // keep the RSI pane aligned with the price chart
  pc.timeScale().subscribeVisibleLogicalRangeChange((r) => r && rsiC.timeScale().setVisibleLogicalRange(r));
  state.fitNext = true;
}

async function loadChart() {
  let c;
  try { c = await api(`/api/chart/${state.key}?tf=${state.tf}`); } catch (e) { return; }
  candleSeries.setData(c.candles);
  ema9S.setData(c.ema9);
  ema21S.setData(c.ema21);
  stS.setData(c.supertrend.map((p) => ({ time: p.time, value: p.value, color: p.color === "up" ? css("--up") : css("--down") })));
  rsiS.setData(c.rsi);

  priceLines.forEach((l) => candleSeries.removePriceLine(l));
  priceLines = [];
  const style = LightweightCharts.LineStyle;
  for (const [name, price] of Object.entries(c.levels)) {
    const wall = name.startsWith("Call") || name.startsWith("Put") || name.startsWith("Max");
    priceLines.push(candleSeries.createPriceLine({ price, color: wall ? css("--ink-2") : css("--muted"), lineWidth: 1,
      lineStyle: wall ? style.Dashed : style.Dotted, axisLabelVisible: false, title: name }));
  }
  // support / resistance: only the key levels for the day get a line, the rest are listed under the chart
  for (const l of c.sr?.lines || []) {
    priceLines.push(candleSeries.createPriceLine({ price: l.price, color: l.side === "support" ? css("--up") : css("--down"),
      lineWidth: 1, lineStyle: style.LargeDashed, axisLabelVisible: true, title: l.title }));
  }
  renderSR(c.sr);
  const act = state.analysis?.active;
  if (act) {
    const add = (price, title, color) => priceLines.push(candleSeries.createPriceLine({ price, color, lineWidth: 2, lineStyle: style.Solid, axisLabelVisible: true, title }));
    add(act.entry, "Entry", css("--accent"));
    add(act.sl, "Stop", css("--down"));
    add(act.t1, "T1", css("--good-mark"));
    add(act.t2, "T2", css("--good-mark"));
  }

  // markers: historical rule fires (small) + live tracked signals (arrows)
  const bucket = (iso) => { const t = Math.floor(Date.parse(iso) / 1000) + IST; return t - (t % (state.tf * 60)); };
  const markers = c.rule_markers.map((m) => ({ time: m.time, position: m.side === "CALL" ? "belowBar" : "aboveBar",
    color: m.side === "CALL" ? css("--up") : css("--down"), shape: "circle", size: 0.6, text: m.side === "CALL" ? "C" : "P" }));
  (state.liveSignals || []).filter((s) => s.symbol === state.key).forEach((s) => markers.push({
    time: bucket(s.ts), position: s.side === "CALL" ? "belowBar" : "aboveBar", color: s.side === "CALL" ? css("--call") : css("--put"),
    shape: s.side === "CALL" ? "arrowUp" : "arrowDown", size: 1.6, text: `${s.opt_type} ${Math.round(s.strike)}` }));
  const first = c.candles[0]?.time ?? 0;
  candleSeries.setMarkers(markers.filter((m) => m.time >= first).sort((a, b) => a.time - b.time));
  state.lastBar = c.candles.length ? { ...c.candles[c.candles.length - 1] } : null;

  if (state.fitNext) { pc.timeScale().fitContent(); state.fitNext = false; }
  $("#chartTitle").textContent = `${state.analysis?.name || state.key} · ${state.tf}-min`;
}

function renderSR(sr) {
  if (!sr?.periods?.length) {
    $("#srTable").innerHTML = `<tr><td class="muted">Needs a few days of price history.</td></tr>`;
    $("#srMeta").textContent = ""; $("#srNote").textContent = "";
    return;
  }
  const cols = [["S2", "S2"], ["S1", "S1"], ["L", "Low"], ["P", "Pivot"], ["H", "High"], ["R1", "R1"], ["R2", "R2"]];
  const key = new Set(sr.key);
  $("#srMeta").textContent = `· for ${sr.for}`;
  $("#srTable").innerHTML = `<thead><tr><th>Period</th><th>From</th>${cols.map(([, n]) => `<th class="num">${n}</th>`).join("")}</tr></thead><tbody>` +
    sr.periods.map((p) => `<tr><td>${esc(p.name)}</td><td class="muted">${p.from === p.to ? esc(p.from) : `${esc(p.from)} – ${esc(p.to)}`}</td>` +
      cols.map(([k]) => {
        const v = p.levels[k], on = key.has(`${p.key}.${k}`);
        return `<td class="num${on ? " key " + (v >= sr.spot ? "r" : "s") : ""}">${fmt(v)}</td>`;
      }).join("") + "</tr>").join("") + "</tbody>";
  $("#srNote").textContent = `Pivots from the previous day, week, month and expiry cycle. Only the key levels for the day ` +
    `(bold: nearest above and below ${fmt(sr.spot)} per period, within one daily ATR of ${fmt(sr.atr)}) get a horizontal line on the chart; the rest are for reference.`;
}

document.querySelectorAll("#tfButtons button").forEach((b) => b.onclick = () => {
  state.tf = +b.dataset.tf;
  document.querySelectorAll("#tfButtons button").forEach((x) => x.classList.toggle("active", x === b));
  state.fitNext = true;
  loadChart();
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { buildCharts(); loadChart(); });

// ------------------------------------------------------------------ zoom
// Mouse wheel / pinch zoom and drag-to-pan are built into the chart; these add buttons and keys.
function zoomChart(action) {
  if (!pc) return;
  const ts = pc.timeScale(), r = ts.getVisibleLogicalRange();
  const n = candleSeries?.data?.().length || 0;
  if (action === "fit") {
    ts.fitContent();
    pc.priceScale("right").applyOptions({ autoScale: true });
    rsiC.priceScale("right").applyOptions({ autoScale: true });
  } else if (action === "latest") {
    ts.scrollToRealTime();
  } else if (action === "full") {
    const card = $(".chartcard");
    if (document.fullscreenElement) document.exitFullscreen();
    else card.requestFullscreen?.();
  } else if (r) {
    const span = r.to - r.from;
    const next = Math.max(10, Math.min(Math.max(n * 1.2, 20), action === "in" ? span * 0.6 : span / 0.6));
    // keep the right edge pinned when you are looking at the latest candles, otherwise zoom around the centre
    const atEnd = n && r.to >= n - 2;
    const to = atEnd ? r.to : (r.from + r.to) / 2 + next / 2;
    ts.setVisibleLogicalRange({ from: to - next, to });
  }
}
document.querySelectorAll("#zoomButtons button").forEach((b) => b.onclick = () => zoomChart(b.dataset.zoom));
document.addEventListener("keydown", (e) => {
  if (state.tab !== "live" || e.ctrlKey || e.metaKey || e.altKey || /input|select|textarea/i.test(e.target.tagName)) return;
  const action = { "+": "in", "=": "in", "-": "out", "_": "out", "0": "fit" }[e.key];
  if (action) { e.preventDefault(); zoomChart(action); }
});
document.addEventListener("fullscreenchange", () => {
  const b = $('#zoomButtons [data-zoom="full"]');
  if (b) b.classList.toggle("active", !!document.fullscreenElement);
});

// ----------------------------------------------------------- live verdict
function moodText(m) { return m ? `${sgn(m.score)} ${m.label}` : "–"; }

function renderInstrButtons() {
  const ov = state.overview?.instruments || {};
  const groups = [...new Set(state.config.instruments.map((i) => i.group))];
  $("#instrButtons").innerHTML = groups.map((g) => `<div class="igroup"><div class="glabel">${esc(g)}</div>` +
    state.config.instruments.filter((i) => i.group === g).map((i) => {
      const a = ov[i.key];
      const sig = a?.signal ? `<span class="pill ${a.signal === "CALL" ? "call" : "put"}">BUY ${a.signal === "CALL" ? "CE" : "PE"}</span>` : "";
      if (!i.available) return `<button disabled title="No free price feed for this contract. Add Groww API keys in .env.">
        <span class="iname">${esc(i.name)} <span class="tag">${esc(i.exchange)}</span></span><span class="imeta">needs Groww API keys (no free feed)</span></button>`;
      return `<button data-key="${i.key}" class="${i.key === state.key ? "active" : ""}">
        <span class="iname">${esc(i.name)} <span class="tag">${esc(i.exchange)}</span> ${sig}</span>
        <span class="imeta">${a ? `${fmt(a.spot)} <span class="${cls(a.change)}">${sgn(a.change_pct)}%</span> · score ${sgn(a.score, 0)}` : "loading…"}</span></button>`;
    }).join("") + "</div>").join("");
  $("#instrButtons").querySelectorAll("button[data-key]").forEach((b) => b.onclick = () => {
    state.key = b.dataset.key; state.fitNext = true; state.expiry = null; refreshLive();
  });
}

function renderVerdict(a) {
  const opt = a.option || {};
  const v = $("#verdict");
  if (a.signal) {
    v.textContent = a.signal === "CALL" ? "BUY CALL (CE)" : "BUY PUT (PE)";
    v.className = "verdict " + (a.signal === "CALL" ? "call" : "put");
    $("#verdictSub").innerHTML = `${esc(a.name)} <b>${fmt(opt.strike, 0)} ${opt.type}</b>${opt.expiry ? " · expiry " + esc(opt.expiry) : ""}`;
  } else {
    v.textContent = "WAIT";
    v.className = "verdict wait";
    $("#verdictSub").textContent = `No trade. Bias is ${a.bias}.`;
  }
  const s = a.score;
  const fill = $("#meterFill");
  fill.style.left = s >= 0 ? "50%" : `${50 + s / 2}%`;
  fill.style.width = `${Math.abs(s) / 2}%`;
  fill.style.background = s >= 0 ? css("--up") : css("--down");
  $("#scoreText").textContent = `${sgn(s, 0)} (needs ±${state.config.signal_threshold})`;

  const act = a.active;
  $("#activeBox").classList.toggle("hidden", !act);
  if (act) {
    const now = a.spot;
    const pts = act.side === "CALL" ? now - act.entry : act.entry - now;
    $("#activeBox").innerHTML = `<b>Open signal:</b> BUY ${act.opt_type} ${fmt(act.strike, 0)} at ${hhmm(act.ts)}, spot ${fmt(act.entry)}.
      Now <b class="${cls(pts)}">${sgn(pts, 1)} pts (${sgn(pts / act.risk, 2)}R)</b>${act.t1_hit ? ` · T1 hit, stop moved to ${fmt(act.sl)}` : ""}`;
  }

  const lv = a.levels;
  const side = a.signal || (a.score >= 0 ? "CALL" : "PUT");
  const optRows = opt.premium != null;
  $("#levelsTable").innerHTML = `<thead><tr><th></th><th class="num">${esc(a.name)} spot</th>${optRows ? `<th class="num">${fmt(opt.strike, 0)} ${opt.type} ${(opt.premium_source || "").startsWith("theoretical") ? "est. premium†" : "premium*"}</th>` : ""}</tr></thead><tbody>
    <tr><td>Entry</td><td class="num">${fmt(lv.entry)}</td>${optRows ? `<td class="num">₹${fmt(opt.premium)}</td>` : ""}</tr>
    <tr><td>Stop-loss</td><td class="num down">${fmt(lv.sl)}</td>${optRows ? `<td class="num down">₹${fmt(opt.premium_sl)}</td>` : ""}</tr>
    <tr><td>Target 1</td><td class="num up">${fmt(lv.t1)}</td>${optRows ? `<td class="num up">₹${fmt(opt.premium_t1)}</td>` : ""}</tr>
    <tr><td>Target 2</td><td class="num up">${fmt(lv.t2)}</td>${optRows ? `<td class="num up">₹${fmt(opt.premium_t2)}</td>` : ""}</tr>
    ${optRows && opt.lot_size ? `<tr><td>Per lot (${esc(opt.lot_desc || opt.lot_size + " qty")})</td><td class="num" colspan="2">cost ₹${fmt(opt.cost_per_lot, 0)} · risk <span class="down">₹${fmt(opt.risk_per_lot, 0)}</span> · T1 <span class="up">₹${fmt(opt.reward_t1_per_lot, 0)}</span> · T2 <span class="up">₹${fmt(opt.reward_t2_per_lot, 0)}</span></td></tr>` : ""}
    ${(opt.stream_keys || []).length && (state.config.groww || a.exchange === "MCX") ? `<tr><td>Premium live</td><td></td><td class="num" id="livePrem">…</td></tr>` : ""}</tbody>`;
  $("#levelsNote").textContent = (a.signal ? "" : `Levels shown for a hypothetical ${side === "CALL" ? "call" : "put"}, for reference only. `) +
    ((opt.premium_source || "").startsWith("theoretical") ? `†Live option prices are not loaded yet: premium is a ${opt.premium_source}. ` : "") +
    (optRows ? `*Premium levels are estimates from delta ${opt.delta} (IV ${opt.iv ?? "–"}%, ${opt.days_to_expiry} days to expiry) and ignore time decay. ` : "") +
    "Risk is about 1.5–2.5 ATR; T1 = 1R, T2 = 2R. After T1, move the stop to entry + 0.3R to lock a small profit.";

  renderSetup(a.setup);
  $("#blockers").innerHTML = (a.blockers || []).map((b) => `<div class="note">⏸ ${esc(b)}</div>`).join("");
  $("#warnings").innerHTML = (a.warnings || []).map((w) => `<div class="note warn">⚠ ${esc(w)}</div>`).join("");
  $("#reasons").innerHTML = a.reasons.map((r) => {
    const icon = r.vote > 0.2 ? "▲" : r.vote < -0.2 ? "▼" : "•";
    return `<li><span class="${cls(r.vote)}">${icon}</span><div>${esc(r.label)}<div class="d">${esc(r.detail)}</div></div>
      <span class="v ${cls(r.vote)}" title="vote × weight">${sgn(r.vote, 1)} ×${r.weight}</span></li>`;
  }).join("");
}

function renderSetup(s) {
  if (!s) { $("#setupBox").innerHTML = `<span class="muted">Historical setup loads after the Quant Lab study finishes.</span>`; return; }
  const st = s.state, an = s.analogs;
  $("#setupBox").innerHTML = `<b>Today's setup in history</b> (as of ${esc(s.date)} close): day ${sgn(st.day_move * 100, 1)}%, week ${sgn(st.week_move * 100, 1)}%,
    ${st.streak > 0 ? st.streak + " up day(s) in a row" : -st.streak + " down day(s) in a row"}, ${sgn(st.dist200 * 100, 1)}% from 200-DMA, ${sgn(st.drawdown * 100, 0)}% from peak, RSI ${st.rsi14}.
    The ${an["1d"].n} most similar past days were followed by an up day <b>${pct(an["1d"].p_up, 0)}</b> of the time and an up week ${pct(an["5d"].p_up, 0)}
    (median week ${sgn(an["5d"].median * 100, 1)}%, range ${sgn(an["5d"].p10 * 100, 1)}% to ${sgn(an["5d"].p90 * 100, 1)}%). Details in Quant Lab.`;
}

function renderTiles(a, ov) {
  $("#tSpotLabel").textContent = a.name;
  $("#tSpot").textContent = fmt(a.spot);
  $("#tSpotSub").innerHTML = `<span class="${cls(a.change)}">${sgn(a.change)} (${sgn(a.change_pct)}%)</span>`;
  $("#tRange").textContent = `${fmt(a.day_low, 0)} – ${fmt(a.day_high, 0)}`;
  $("#tUpdated").textContent = `last candle ${hhmm(a.last_tick)} · ${a.source}`;
  const cx = a.context;
  $("#tVixLabel").textContent = cx ? cx.label : "India VIX";
  if (cx) {
    $("#tVix").textContent = cx.value;
    $("#tVixSub").innerHTML = `${cx.change_pct != null ? `<span class="${cls(cx.change_pct)}">${sgn(cx.change_pct)}%</span> · ` : ""}${esc(cx.sub)}`;
  } else {
    const vx = ov?.vix;
    $("#tVix").textContent = vx ? fmt(vx.value) : "–";
    $("#tVixSub").innerHTML = vx ? `<span class="${cls(-vx.change_pct)}">${sgn(vx.change_pct)}%</span> ${vx.value > 20 ? "· high fear" : vx.value < 13 ? "· calm" : ""}` : "";
  }
  const ch = a.chain;
  $("#tPcr").textContent = ch?.pcr ?? "–";
  $("#tPcrSub").textContent = ch ? (ch.pcr > 1.2 ? "put-heavy (support)" : ch.pcr < 0.8 ? "call-heavy (resistance)" : "balanced") : (a.chain_note ? "needs Groww API keys" : "");
  $("#tPain").textContent = ch?.max_pain ? fmt(ch.max_pain, 0) : "–";
  $("#tWalls").textContent = ch ? `support ${fmt(ch.put_wall, 0)} · resistance ${fmt(ch.call_wall, 0)}` : (a.chain_note ? "needs Groww API keys" : "");
  const md = a.reasons.find((r) => r.key === "news");
  $("#tMood").textContent = a.kind === "commodity" ? (md ? `${sgn(md.vote)} ${a.news_topic} news` : "–") : moodText(ov?.mood);
  $("#tMoodSub").textContent = a.kind === "commodity" ? `${a.news_topic} headlines only` : (ov?.mood ? `${ov.mood.headlines} headlines` : "");
  $("#mktPill").textContent = `${a.exchange} ${a.open ? "open" : "closed"}`;
  $("#mktPill").className = "pill" + (a.open ? " on" : "");
}

async function loadChain() {
  let c;
  if (state.analysis?.chain_note) {
    $("#chainTable").innerHTML = `<tr><td class="muted" style="text-align:left;white-space:normal">${esc(state.analysis.chain_note)}. Signals still work. Option premiums shown above are Black-Scholes estimates.</td></tr>`;
    $("#chainStats").innerHTML = ""; $("#chainMeta").textContent = ""; $("#selExpiry").innerHTML = "";
    return;
  }
  try { c = await api(`/api/chain/${state.key}` + (state.expiry ? `?expiry=${encodeURIComponent(state.expiry)}` : "")); } catch (e) { $("#chainTable").innerHTML = `<tr><td class="muted">${esc(e.message)}</td></tr>`; return; }
  const m = c.meta, an = c.analytics;
  $("#selExpiry").innerHTML = (m.expiries || []).map((e) => `<option ${e === m.expiry ? "selected" : ""}>${esc(e)}</option>`).join("");
  $("#chainMeta").textContent = `· ${m.source.toUpperCase()} · as of ${m.timestamp}`;
  $("#chainStats").innerHTML = [["Spot", fmt(c.spot)], ["ATM", fmt(an.atm, 0)], ["PCR (OI)", an.pcr ?? "–"], ["Change-in-OI PCR", an.chg_pcr ?? "–"],
    ["Max pain", fmt(an.max_pain, 0)], ["Support (put wall)", fmt(an.put_wall, 0)], ["Resistance (call wall)", fmt(an.call_wall, 0)], ["ATM IV", an.atm_iv ? an.atm_iv + "%" : "–"]]
    .map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join("");
  const maxOi = Math.max(1, ...c.rows.flatMap((r) => [r.ce?.oi || 0, r.pe?.oi || 0]));
  const n = (v, d = 0) => v == null ? "–" : fmt(v, d);
  const head = `<thead><tr><th>Call OI</th><th>Chg OI</th><th>IV</th><th>Call LTP</th><th class="strike">Strike</th><th>Put LTP</th><th>IV</th><th>Chg OI</th><th>Put OI</th></tr></thead>`;
  const body = c.rows.map((r) => {
    const atm = r.strike === an.atm, ceItm = r.strike < c.spot, peItm = r.strike > c.spot;
    const ce = r.ce || {}, pe = r.pe || {};
    return `<tr class="${atm ? "atm" : ""}">
      <td class="${ceItm ? "itm" : ""}"><span class="bar ce" style="width:${(ce.oi || 0) / maxOi * 100}%"></span><span class="n">${n(ce.oi)}</span></td>
      <td class="${ceItm ? "itm" : ""} ${cls(ce.chg_oi)}">${n(ce.chg_oi)}</td><td class="${ceItm ? "itm" : ""}">${n(ce.iv, 1)}</td>
      <td class="${ceItm ? "itm" : ""}">${n(ce.ltp, 2)}</td><td class="strike">${fmt(r.strike, 0)}</td>
      <td class="${peItm ? "itm" : ""}">${n(pe.ltp, 2)}</td><td class="${peItm ? "itm" : ""}">${n(pe.iv, 1)}</td>
      <td class="${peItm ? "itm" : ""} ${cls(pe.chg_oi)}">${n(pe.chg_oi)}</td>
      <td class="${peItm ? "itm" : ""}"><span class="bar pe" style="width:${(pe.oi || 0) / maxOi * 100}%"></span><span class="n">${n(pe.oi)}</span></td></tr>`;
  }).join("");
  $("#chainTable").innerHTML = head + `<tbody>${body}</tbody>`;
}
$("#selExpiry").onchange = (e) => { state.expiry = e.target.value; loadChain(); };

async function loadBtSummary() {
  try {
    const b = await api(`/api/backtest/${state.key}`);
    const s = b.stats;
    $("#btSummary").innerHTML = s.trades
      ? `<b>Track record of these rules</b> (last ${b.days} days, ${s.trades} signals): win rate ${pct(s.win_rate, 0)}, target 1 reached ${pct(s.t1_hit_rate, 0)},
         avg ${sgn(s.avg_points, 1)} pts per signal, <b class="${cls(s.avg_points_after_costs)}">${sgn(s.avg_points_after_costs, 1)} pts after est. costs</b>. Details in the Signals tab.`
      : "Backtest has no trades yet.";
  } catch (e) { $("#btSummary").textContent = ""; }
}

async function refreshLive(full = true) {
  try {
    state.overview = await api("/api/overview");
    state.analysis = await api(`/api/analysis/${state.key}`);
  } catch (e) {
    $("#verdict").textContent = "Loading data…";
    setTimeout(() => refreshLive(full), 3000);
    return;
  }
  renderStatus(state.overview);
  renderInstrButtons();
  renderVerdict(state.analysis);
  renderTiles(state.analysis, state.overview);
  const sig = await api(`/api/signals?key=${state.key}&limit=50`).catch(() => ({ items: [] }));
  state.liveSignals = sig.items;
  loadChart();
  if (full) { loadChain(); loadBtSummary(); }
}

function renderStatus(ov) {
  if (state.tab !== "live") {
    $("#mktPill").textContent = ov.any_open ? "market open" : "markets closed";
    $("#mktPill").className = "pill" + (ov.any_open ? " on" : "");
  }
  const errs = ov.errors || [];
  $("#errBanner").textContent = errs.length ? "Data problems: " + errs.join(" · ") : "";
  $("#errBanner").classList.toggle("hidden", !errs.length);
}

// ---------------------------------------------------------------- market
function heat(v) {
  const a = Math.abs(v), k = a < 0.005 ? 0 : a < 0.01 ? 1 : a < 0.02 ? 2 : a < 0.03 ? 3 : 4;
  return k === 0 ? [css("--mid"), css("--ink")] : [css(`--${v > 0 ? "pos" : "neg"}-${k}`), k >= 3 ? "#fff" : css("--ink")];
}

async function renderMarket() {
  const [ov, br] = await Promise.all([api("/api/overview"), api("/api/breadth")]);
  const vx = ov.vix;
  $("#mVix").textContent = vx ? fmt(vx.value) : "–";
  $("#mVixSub").textContent = vx ? `${sgn(vx.change_pct)}% today` : "";
  $("#mAdv").textContent = ov.breadth.advances;
  $("#mDec").textContent = ov.breadth.declines;
  $("#mMood").textContent = moodText(ov.mood);
  $("#heatGrid").innerHTML = br.map((s) => {
    const [bg, fg] = heat(s.change);
    return `<div style="background:${bg};color:${fg}" title="${esc(s.symbol)} ${fmt(s.price)} (${sgn(s.change * 100)}%)"><b>${esc(s.symbol)}</b>${sgn(s.change * 100)}% · ${fmt(s.price)}</div>`;
  }).join("") || `<p class="muted">Loading…</p>`;
  const tbl = (rows) => `<thead><tr><th>Stock</th><th class="num">Price</th><th class="num">Change</th></tr></thead><tbody>` +
    rows.map((s) => `<tr><td>${esc(s.symbol)}</td><td class="num">${fmt(s.price)}</td><td class="num ${cls(s.change)}">${sgn(s.change * 100)}%</td></tr>`).join("") + "</tbody>";
  $("#gainTable").innerHTML = tbl(br.slice(0, 8));
  $("#loseTable").innerHTML = tbl(br.slice(-8).reverse());
}

// --------------------------------------------------------------- signals
const charts = {};
function chartjs(id, cfg) { charts[id]?.destroy(); charts[id] = new Chart(document.getElementById(id), cfg); }
Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';

function table(sel, cols, rows, empty = "No data yet.") {
  $(sel).innerHTML = `<thead><tr>${cols.map((c) => `<th class="${c.num ? "num" : ""}">${c.h}</th>`).join("")}</tr></thead><tbody>` +
    (rows.length ? rows.map((r) => `<tr>${cols.map((c) => {
      const v = typeof c.k === "function" ? c.k(r) : r[c.k];
      return `<td class="${c.num ? "num" : ""} ${c.color ? cls(v) : ""}">${c.f ? c.f(v, r) : esc(v)}</td>`;
    }).join("")}</tr>`).join("") : `<tr><td colspan="${cols.length}" class="muted">${empty}</td></tr>`) + "</tbody>";
}

async function loadSignals() {
  const insts = state.config.instruments.filter((i) => i.available && !i.needs_groww);  // backtests need the free 60-day history
  const [sig, ...bts] = await Promise.all([api("/api/signals?limit=300"),
    ...insts.map((i) => api(`/api/backtest/${i.key}`).catch(() => ({ stats: { trades: 0 }, trades: [], days: 0 })))]);
  $("#btCompare").innerHTML = bts.map((b, i) => {
    const w = b.stats, wo = b.stats_without_daily;
    if (!b.with_daily_algo || !wo?.trades) return "";
    return `${esc(insts[i].name)}: with the daily algorithm vote <b class="${cls(w.avg_points_after_costs)}">${sgn(w.avg_points_after_costs, 1)}</b> pts/signal after costs,
      without it <b class="${cls(wo.avg_points_after_costs)}">${sgn(wo.avg_points_after_costs, 1)}</b>.`;
  }).filter(Boolean).join(" · ") + (bts.some((b) => b.with_daily_algo) ? " With about 120 signals per index, differences this small can be noise." : "");
  $("#btCards").innerHTML = bts.map((b, i) => {
    const s = b.stats, name = insts[i].name;
    if (!s.trades) return `<div class="btcard"><b>${esc(name)}</b><p class="muted">No trades.</p></div>`;
    const rows = [["Signals", s.trades], ["Win rate", pct(s.win_rate, 0)], ["Reached target 1", pct(s.t1_hit_rate, 0)],
      ["Avg points / signal", sgn(s.avg_points, 1)], [`After est. costs (${b.cost_points} pts)`, sgn(s.avg_points_after_costs, 1)],
      ["Total points after costs", sgn(s.total_points_after_costs, 0)], ["Profit factor", s.profit_factor ?? "–"],
      ["Avg R", sgn(s.avg_r, 2)], ["Worst losing streak", s.max_losing_streak],
      ["CALL signals: win rate / avg R", `${pct(s.call.win_rate, 0)} / ${sgn(s.call.avg_r, 2)}`],
      ["PUT signals: win rate / avg R", `${pct(s.put.win_rate, 0)} / ${sgn(s.put.avg_r, 2)}`]];
    return `<div class="btcard"><b>${esc(name)}</b> <span class="muted small">${b.days} days · ${esc(b.data || "")}</span>
      <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
  }).join("");
  const colors = [css("--s1"), css("--s2"), css("--s3"), css("--ema9"), css("--ema21"), css("--bench")];
  const dashes = [[], [6, 3], [2, 2]];
  chartjs("btChart", {
    type: "line",
    data: { datasets: bts.map((b, i) => {
      let cum = 0;
      const c = colors[i % colors.length];
      // points differ hugely between contracts (gold vs gas), so each line is in % of its first entry price
      const base = b.trades[0]?.entry || 1;
      return { label: `${insts[i].name}: cumulative % after costs`, borderColor: c, backgroundColor: c,
        borderDash: dashes[Math.floor(i / colors.length) % dashes.length], borderWidth: 2, pointRadius: 0, stepped: true,
        data: b.trades.map((t, j) => ({ x: j + 1, y: +(cum += (t.points - b.cost_points) / base * 100).toFixed(3) })) };
    }) },
    options: { maintainAspectRatio: false, plugins: { legend: { position: "top", labels: { boxWidth: 12, color: css("--ink-2") } } },
      scales: { x: { type: "linear", title: { display: true, text: "signal #", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { display: false } },
                y: { ticks: { color: css("--muted") }, grid: { color: css("--grid") } } } },
  });

  const sc = sig.scorecard;
  table("#scoreTable", [{ h: "Instrument", k: "key" }, { h: "Closed signals", k: "trades", num: 1 }, { h: "Win rate", k: "win_rate", num: 1, f: (v) => pct(v, 0) },
    { h: "Target 1 hit", k: "t1_hit_rate", num: 1, f: (v) => pct(v, 0) }, { h: "Avg R", k: "avg_r", num: 1, color: 1, f: (v) => sgn(v, 2) },
    { h: "Total points", k: "total_points", num: 1, color: 1, f: (v) => sgn(v, 1) }, { h: "Profit factor", k: "profit_factor", num: 1, f: (v) => v ?? "–" }],
    Object.entries(sc).map(([k, v]) => ({ key: k, ...v })), "No live signals graded yet. They appear here while the app runs during market hours.");
  table("#sigTable", [{ h: "Time", k: "ts", f: (v) => esc(v.replace("T", " ").slice(0, 16)) }, { h: "Instrument", k: "symbol" },
    { h: "Signal", k: (r) => r, f: (_, r) => `<b class="${r.side === "CALL" ? "up" : "down"}">BUY ${r.opt_type} ${fmt(r.strike, 0)}</b> <span class="muted">${esc(r.expiry || "")}</span>` },
    { h: "Score", k: "score", num: 1, f: (v) => sgn(v, 0) }, { h: "Entry", k: "entry", num: 1, f: (v) => fmt(v) }, { h: "SL", k: "sl", num: 1, f: (v) => fmt(v) },
    { h: "T1", k: "t1", num: 1, f: (v) => fmt(v) }, { h: "T2", k: "t2", num: 1, f: (v) => fmt(v) }, { h: "Prem.*", k: "premium", num: 1, f: (v) => v ? "₹" + fmt(v) : "–" },
    { h: "Result", k: "status" }, { h: "Points", k: "points", num: 1, color: 1, f: (v) => v == null ? "–" : sgn(v, 1) }, { h: "R", k: "r", num: 1, color: 1, f: (v) => v == null ? "–" : sgn(v, 2) }],
    sig.items, "No signals yet.");
}

// ------------------------------------------------------------------ news
function sentPill(s) {
  const k = s > 0.15 ? "pos" : s < -0.15 ? "neg" : "neu";
  return `<span class="sent ${k}">${{ pos: "Bullish", neg: "Bearish", neu: "Neutral" }[k]} ${sgn(s)}</span>`;
}
let newsCache = [];
function renderNews() {
  const f = $("#selImpact").value;
  $("#newsList").innerHTML = newsCache.filter((n) => !f || n.impact === f).map((n) => `<li>${sentPill(n.sentiment)}<div>
    <a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>
    <div class="meta">${esc(n.source)} · ${esc((n.ts || "").replace("T", " ").slice(0, 16))}
      <span class="tag ${n.impact === "high" ? "high" : ""}">${esc(n.impact)} impact</span><span class="tag">${esc(n.scope)}</span>
      ${(n.symbols || []).map((s) => `<span class="tag">${esc(s)}</span>`).join("")}<span class="tag">${esc(n.analyzer)}</span></div></div></li>`).join("")
    || `<li class="muted">No headlines yet.</li>`;
}
async function loadNews() {
  const r = await api("/api/news?limit=150");
  newsCache = r.items;
  const m = r.mood;
  $("#nMood").textContent = moodText(m);
  $("#nMoodSub").textContent = `${m.headlines} headlines, time-weighted`;
  $("#nPos").textContent = m.counts.positive; $("#nNeg").textContent = m.counts.negative; $("#nNeu").textContent = m.counts.neutral;
  renderNews();
}
$("#selImpact").onchange = renderNews;
$("#btnNews").onclick = async () => { $("#btnNews").disabled = true; try { await api("/api/news/refresh", "POST"); await loadNews(); } finally { $("#btnNews").disabled = false; } };

// --------------------------------------------------------------- history
async function loadHistory() {
  const r = await api("/api/research");
  $("#btnResearch").disabled = r.running || STATIC;
  $("#btnResearch").textContent = r.running ? "Running… (1–3 min)" : "Re-run analysis";
  if (!r.report) return;
  const rep = r.report, m = rep.market;
  $("#resMeta").textContent = `Data ${rep.data_range[0]} → ${rep.data_range[1]} · generated ${rep.generated.replace("T", " ")}`;
  $("#findings").innerHTML = rep.findings.map((f) => `<li>${esc(f)}</li>`).join("");
  const bar = (id, labels, data, colorFn) => chartjs(id, { type: "bar", data: { labels, datasets: [{ data, backgroundColor: data.map(colorFn), borderRadius: 4, maxBarThickness: 40 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => pct(c.parsed.y, 2) } } },
      scales: { x: { grid: { display: false }, ticks: { color: css("--muted") } }, y: { ticks: { color: css("--muted"), callback: (v) => pct(v, 1) }, grid: { color: css("--grid") } } } } });
  bar("vixChart", m.vix_regimes.map((v) => "VIX " + v.vix), m.vix_regimes.map((v) => v.avg_fwd_20d), () => css("--s1"));
  bar("monthChart", m.month.map((v) => v.month), m.month.map((v) => v.avg_month), (v) => v >= 0 ? css("--pos-3") : css("--neg-3"));
  const P = (d = 1) => (v) => pct(v, d);
  table("#gapTable", [{ h: "Opening gap", k: "gap" }, { h: "Days", k: "days", num: 1 }, { h: "Avg open→close", k: "avg_intraday", num: 1, color: 1, f: P(2) },
    { h: "Gap continued", k: "pct_continued", num: 1, f: P(0) }, { h: "Gap filled", k: "pct_gap_filled", num: 1, f: P(0) }], m.gaps);
  table("#shockTable", [{ h: "Day type", k: "day" }, { h: "Count", k: "count", num: 1 }, { h: "Next 1d", k: "fwd_1d", num: 1, color: 1, f: P() },
    { h: "Next 5d", k: "fwd_5d", num: 1, color: 1, f: P() }, { h: "Next 20d", k: "fwd_20d", num: 1, color: 1, f: P() }, { h: "20d up", k: "pct_up_20d", num: 1, f: P(0) }], m.shocks);
  table("#trendTable", [{ h: "Regime", k: "regime" }, { h: "Days", k: "days", num: 1 }, { h: "Ann. vol", k: "ann_vol", num: 1, f: P(0) }, { h: "Up days", k: "pct_positive", num: 1, f: P(0) }], m.trend_regimes);
  table("#eventTable", [{ h: "Date", k: "date" }, { h: "Type", k: "type" }, { h: "Event", k: "description" }, { h: "5d before", k: "pre_5d", num: 1, color: 1, f: P() },
    { h: "Day 0", k: "day0", num: 1, color: 1, f: P() }, { h: "Day +1", k: "day1", num: 1, color: 1, f: P() }, { h: "Day +5", k: "day5", num: 1, color: 1, f: P() },
    { h: "Day +20", k: "day20", num: 1, color: 1, f: P() }], rep.events);
  table("#stratTable", [{ h: "Strategy", k: "name" }, { h: "CAGR", k: (r) => r.full.cagr, num: 1, color: 1, f: P() }, { h: "Max DD", k: (r) => r.full.max_drawdown, num: 1, f: P(0) },
    { h: "Sharpe", k: (r) => r.full.sharpe, num: 1, f: (v) => fmt(v) }, { h: "2019+ CAGR", k: (r) => r.out_of_sample.cagr, num: 1, color: 1, f: P() },
    { h: "2019+ Sharpe", k: (r) => r.out_of_sample.sharpe, num: 1, f: (v) => fmt(v) }], rep.strategies);
}
$("#btnResearch").onclick = async () => {
  await api("/api/research/run", "POST"); loadHistory();
  const t = setInterval(async () => { const r = await api("/api/research"); if (!r.running) { clearInterval(t); loadHistory(); } }, 4000);
};

// ------------------------------------------------------------ live ticks
function flash(el, dir) {
  if (!el || !dir) return;
  el.classList.remove("flash-up", "flash-down");
  void el.offsetWidth;
  el.classList.add(dir > 0 ? "flash-up" : "flash-down");
}

function handleTick(t) {
  state.ticks = t;
  const p = t.prices[state.key];
  const vix = t.prices.VIX;
  const prev = state.lastTickPrice?.[state.key];
  state.lastTickPrice = Object.fromEntries(Object.entries(t.prices).map(([k, v]) => [k, v.price]));
  // instrument buttons
  document.querySelectorAll("#instrButtons button").forEach((b) => {
    const q = t.prices[b.dataset.key];
    const meta = b.querySelector(".imeta");
    if (q && meta) meta.innerHTML = `${fmt(q.price)} <span class="${cls(q.change)}">${sgn(q.change_pct)}%</span> · score ${sgn(state.overview?.instruments?.[b.dataset.key]?.score, 0)}`;
  });
  const selInst = state.config.instruments.find((i) => i.key === state.key);
  if (selInst?.exchange === "MCX") {
    const w = t.prices[state.analysis?.context?.key], fx = t.prices.USDINR;
    if (w) { $("#tVix").textContent = "$" + fmt(w.price); $("#tVixSub").innerHTML = `<span class="${cls(w.change_pct)}">${sgn(w.change_pct)}%</span>${fx ? " · USD/INR " + fmt(fx.price, 3) : ""}`; }
  } else if (vix) { $("#tVix").textContent = fmt(vix.price); $("#tVixSub").innerHTML = `<span class="${cls(-vix.change_pct)}">${sgn(vix.change_pct)}%</span>`; }
  const age = p ? Math.max(0, Math.round(Date.now() / 1000 - p.ts + (t.clock_behind_s || 0))) : null;
  $("#wsPill").textContent = p ? `live · ${new Date(p.ts * 1000).toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" })}` : "live";
  $("#wsPill").title = `Price source: ${t.source}. Pushed every second.` + (t.clock_behind_s ? ` Your PC clock is ~${t.clock_behind_s}s behind exchange time.` : "");
  if (!p || state.tab !== "live") return;
  $("#tSpot").textContent = fmt(p.price);
  $("#tSpotSub").innerHTML = `<span class="${cls(p.change)}">${sgn(p.change)} (${sgn(p.change_pct)}%)</span>`;
  if (prev != null && prev !== p.price) flash($("#tSpot").closest(".tile"), p.price - prev);
  $("#tUpdated").innerHTML = `exchange time ${new Date(p.ts * 1000).toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" })}` +
    ` · ${esc(t.source)}` + (t.clock_behind_s > 5 ? ` · <span class="down">PC clock ${t.clock_behind_s}s behind</span>` : "");
  // live candle
  if (state.lastBar && candleSeries) {
    const ts = Math.floor(p.ts) + IST, bucket = ts - (ts % (state.tf * 60));
    const b = state.lastBar;
    if (bucket === b.time) { b.high = Math.max(b.high, p.price); b.low = Math.min(b.low, p.price); b.close = p.price; candleSeries.update({ ...b }); }
    else if (bucket > b.time) { state.lastBar = { time: bucket, open: p.price, high: p.price, low: p.price, close: p.price }; candleSeries.update({ ...state.lastBar }); }
  }
  // open signal P&L and live option premium
  const a = state.analysis;
  if (a?.active) {
    const s = a.active, pts = s.side === "CALL" ? p.price - s.entry : s.entry - p.price;
    const opt = t.options?.[`${state.key}:${Math.round(s.strike)}:${s.opt_type}`];
    $("#activeBox").innerHTML = `<b>Open signal:</b> BUY ${s.opt_type} ${fmt(s.strike, 0)} at ${hhmm(s.ts)}, spot ${fmt(s.entry)}.
      Now ${fmt(p.price)} → <b class="${cls(pts)}">${sgn(pts, 1)} pts (${sgn(pts / s.risk, 2)}R)</b>${s.t1_hit ? ` · T1 hit, stop at ${fmt(s.sl)}` : ""}` +
      (opt ? ` · premium now <b>₹${fmt(opt.price)}</b>${s.premium ? ` (${sgn(opt.price - s.premium, 2)})` : ""}` : "");
  }
  const o = a?.option;
  if (o && t.options) {
    const live = t.options[`${state.key}:${Math.round(o.strike)}:${o.type}`];
    const cell = document.getElementById("livePrem");
    if (live && cell) { cell.textContent = "₹" + fmt(live.price); }
  }
}

function alertUpdate(m) {
  const text = `${m.name} ${m.signal.opt_type} ${Math.round(m.signal.strike)}: ${m.message}. Spot ${fmt(m.spot)}`;
  const box = $("#activeBox");
  if (box) { box.classList.remove("hidden"); box.innerHTML = `<b>${esc(text)}</b>`; }
  if (!$("#chkAlerts").checked) return;
  if ("Notification" in window && Notification.permission === "granted") new Notification("AlgTrd", { body: text });
  if ("speechSynthesis" in window) { const u = new SpeechSynthesisUtterance(text); u.lang = "en-IN"; speechSynthesis.speak(u); }
}

// ---------------------------------------------------------------- alerts
$("#chkAlerts").onchange = (e) => { if (e.target.checked && "Notification" in window && Notification.permission === "default") Notification.requestPermission(); };
function alertSignal(s, name) {
  if (!$("#chkAlerts").checked) return;
  const text = `${name}: BUY ${s.opt_type} ${Math.round(s.strike)}. Spot ${fmt(s.entry)}, stop ${fmt(s.sl)}, target ${fmt(s.t1)}`;
  if ("Notification" in window && Notification.permission === "granted") new Notification("AlgTrd signal", { body: text });
  if ("speechSynthesis" in window) { const u = new SpeechSynthesisUtterance(text); u.lang = "en-IN"; speechSynthesis.speak(u); }
}
function speakNews(items) {
  if (!$("#chkSpeak").checked || !("speechSynthesis" in window)) return;
  items.filter((n) => n.impact === "high").slice(0, 3).forEach((n) => { const u = new SpeechSynthesisUtterance(n.title); u.lang = "en-IN"; speechSynthesis.speak(u); });
}

// ------------------------------------------------------------- websocket
let chainTick = 0;
function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { $("#wsPill").textContent = "live"; $("#wsPill").className = "pill on"; };
  ws.onclose = () => { $("#wsPill").textContent = "offline"; $("#wsPill").className = "pill"; setTimeout(connect, 3000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "update") {
      if (state.tab === "live") refreshLive(++chainTick % 3 === 0);
      else if (state.tab === "market") renderMarket();
      else renderStatus(m.overview);
    } else if (m.type === "tick") {
      handleTick(m);
    } else if (m.type === "signal_update") {
      alertUpdate(m);
      if (state.tab === "live") refreshLive(false);
    } else if (m.type === "signal") {
      alertSignal(m.signal, m.name);
      if (state.tab === "signals") loadSignals();
    } else if (m.type === "news") {
      speakNews(m.items);
      if (state.tab === "news") loadNews();
    }
  };
}

// ------------------------------------------------------- published copy
// Instead of the websocket: check every minute for a newer snapshot and redraw the open tab.
async function loadMeta() {
  const r = await fetch("snapshot/meta.json?t=" + Date.now()).catch(() => null);
  const m = r && r.ok ? await r.json() : null;
  if (!m) return false;
  const changed = m.updated !== snapVer;
  snapVer = m.updated;
  const t = new Date(m.updated).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  $("#wsPill").textContent = `snapshot ${t}`;
  $("#wsPill").className = "pill" + (m.any_open ? " on" : "");
  $("#snapBanner").innerHTML = `<b>Published copy</b>, data as of ${esc(t)} IST. It refreshes about every ${m.every_minutes} minutes while a market is open,
    so prices do not tick here; the app on the PC is live. · <a href="log/">Prediction log &amp; accuracy</a>`;
  return changed;
}

function setupStatic() {
  const b = document.createElement("div");
  b.id = "snapBanner";
  b.className = "banner";
  $("main").prepend(b);
  ["#btnQuant", "#btnNews", "#btnResearch"].forEach((s) => { const el = $(s); if (el) { el.disabled = true; el.title = "Runs only in the app on the PC"; } });
  setInterval(async () => {
    if (!(await loadMeta())) return;
    state.config = await api("/api/config");
    if (state.tab === "live") refreshLive(true);
    else if (state.tab === "market") renderMarket();
    else if (state.tab === "signals") loadSignals();
    else if (state.tab === "news") loadNews();
  }, 60000);
}

// ------------------------------------------------------------------ boot
(async function boot() {
  if (STATIC) { setupStatic(); await loadMeta(); }
  state.config = await api("/api/config");
  buildCharts();
  await refreshLive();
  if (!STATIC) connect();
  setInterval(() => state.tab === "live" && refreshLive(false), 30000);
})();
