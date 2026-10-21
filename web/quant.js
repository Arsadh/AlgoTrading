// Quant Lab tab: multi-decade rule tests, the combined algorithm, and move studies.
// Uses helpers defined in app.js ($, fmt, pct, sgn, esc, cls, table, chartjs, css, api).
const Q = { rep: null, ens: "NIFTY", move: "NIFTY", period: "all" };
const star = (r) => r.significant ? " ★" : "";

window.loadQuant = async function loadQuant() {
  const r = await api("/api/quant");
  $("#btnQuant").disabled = r.running;
  $("#btnQuant").textContent = r.running ? "Running… (1–2 min)" : "Re-run study";
  if (!r.report) {
    $("#qMeta").textContent = r.running ? "First run: downloading ~56,000 days of history and testing every rule…" : "No study yet.";
    if (r.running) setTimeout(loadQuant, 5000);
    return;
  }
  Q.rep = r.report;
  const rep = Q.rep;
  $("#qMeta").textContent = `Generated ${rep.generated.replace("T", " ")} in ${rep.seconds}s · cost ${rep.cost_bps / 100}% per trade · ${rep.n_random} random strategies per rule`;
  $("#qFindings").innerHTML = rep.findings.map((f) => `<li>${esc(f)}</li>`).join("");
  $("#qCoverage").innerHTML = rep.coverage.map((c) => `<div class="tile"><div class="label">${esc(c.name)}</div>
    <div class="value">${c.years} yrs</div><div class="sub">${esc(c.from)} → ${esc(c.to)} · ${c.days.toLocaleString("en-IN")} days</div></div>`).join("");
  segButtons("#qEnsMarkets", Object.keys(rep.ensemble.markets).map((k) => [k, rep.ensemble.markets[k].name]), Q.ens, (k) => { Q.ens = k; renderEnsemble(); });
  const mname = Object.fromEntries(rep.coverage.map((c) => [c.key, c.name]));
  segButtons("#qMoveMarkets", Object.keys(rep.moves).map((k) => [k, mname[k] || k]), Q.move, (k) => { Q.move = k; renderMoves(); });
  document.querySelectorAll("#qMovePeriod button").forEach((b) => b.onclick = () => {
    Q.period = b.dataset.p;
    document.querySelectorAll("#qMovePeriod button").forEach((x) => x.classList.toggle("active", x === b));
    renderMoves();
  });
  const cats = [...new Set(rep.rules.map((x) => x.category))];
  $("#qCategory").innerHTML = `<option value="">All categories</option>` + cats.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#qVerdict").onchange = renderRules;
  $("#qCategory").onchange = renderRules;
  renderEnsemble();
  renderRules();
  renderMoves();
};

function segButtons(sel, items, active, onPick) {
  $(sel).innerHTML = items.map(([k, label]) => `<button data-k="${esc(k)}" class="${k === active ? "active" : ""}">${esc(label)}</button>`).join("");
  $(sel).querySelectorAll("button").forEach((b) => b.onclick = () => {
    $(sel).querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    onPick(b.dataset.k);
  });
}

// ------------------------------------------------------------ the algorithm
function renderEnsemble() {
  const e = Q.rep.ensemble, m = e.markets[Q.ens];
  $("#qEnsRule").textContent = `Selection rule: ${e.rule}. ${e.members.length} rules qualified, and each gets an equal vote. Train: before ${m.test_from}; test: ${m.test_from} to ${m.holdout_from}; holdout: ${m.holdout_from} onwards.`;
  const votes = m.today.votes;
  $("#qMembers").innerHTML = e.members.map((x) => {
    const v = votes[x.key];
    return `<span class="chip ${v > 0 ? "bull" : v < 0 ? "bear" : ""}" title="${esc(x.category)}">${v > 0 ? "▲" : v < 0 ? "▼" : "•"} ${esc(x.name)}</span>`;
  }).join("");
  const b = m.today.bias;
  $("#qBias").textContent = `${sgn(b)} · ${b >= 0.5 ? "strongly bullish" : b > 0 ? "bullish" : b <= -0.5 ? "strongly bearish" : b < 0 ? "bearish" : "neutral"} (close of ${m.today.date})`;
  $("#qBias").className = "verdict small-v " + (b > 0 ? "call" : b < 0 ? "put" : "wait");
  const fill = $("#qBiasFill");
  fill.style.left = b >= 0 ? "50%" : `${50 + b * 50}%`;
  fill.style.width = `${Math.abs(b) * 50}%`;
  fill.style.background = b >= 0 ? css("--up") : css("--down");

  const cell = (d) => d && d.sharpe != null ? `${pct(d.cagr)} · ${fmt(d.sharpe)} · ${pct(d.max_dd, 0)}` : "–";
  const per = ["train", "test", "holdout", "full"];
  $("#qEnsTable").innerHTML = `<thead><tr><th>CAGR · Sharpe · worst fall</th>${per.map((p) => `<th class="num">${p}</th>`).join("")}</tr></thead><tbody>` +
    [["Algorithm long/short", m.composite], ["Algorithm long-only", m.long_only], ["Buy & hold", m.buy_hold]]
      .map(([n, d]) => `<tr><td>${n}</td>${per.map((p) => `<td class="num">${cell(d[p])}</td>`).join("")}</tr>`).join("") + "</tbody>";

  const colors = [css("--s1"), css("--s3"), css("--bench")];
  const series = [["Algorithm long/short", m.curve.composite], ["Algorithm long-only", m.curve.long_only], ["Buy & hold", m.curve.buy_hold]];
  chartjs("qCurve", {
    type: "line",
    data: { datasets: series.map(([label, pts], i) => ({ label, data: pts.map((p) => ({ x: p[0], y: p[1] })), borderColor: colors[i], backgroundColor: colors[i],
      borderWidth: 2, pointRadius: 0, borderDash: i === 2 ? [4, 4] : [] })) },
    options: { maintainAspectRatio: false, parsing: false, plugins: { legend: { position: "top", labels: { boxWidth: 12, color: css("--ink-2") } },
      tooltip: { callbacks: { title: (c) => c[0]?.raw.x, label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y, 2)}` } } },
      scales: { x: { type: "category", labels: series[2][1].map((p) => p[0]), ticks: { color: css("--muted"), maxTicksLimit: 10, callback: function (v) { return this.getLabelForValue(v).slice(0, 4); } }, grid: { display: false } },
                y: { type: "logarithmic", ticks: { color: css("--muted"), callback: (v) => [0.5, 1, 2, 5, 10, 20, 50, 100, 200].includes(v) ? v : null }, grid: { color: css("--grid") } } } },
  });
  const names = { strong_bull: "Strongly bullish (≥ +0.5)", bull: "Bullish", neutral: "Neutral", bear: "Bearish", strong_bear: "Strongly bearish (≤ −0.5)" };
  table("#qHits", [{ h: "Bias", k: "name" }, { h: "Days", k: "days", num: 1 }, { h: "Next day up", k: "next_day_up", num: 1, f: (v) => pct(v, 0) },
    { h: "Avg next day", k: "next_day_avg", num: 1, color: 1, f: (v) => v == null ? "–" : sgn(v * 100, 2) + "%" },
    { h: "Avg next 5 days", k: "next_5d_avg", num: 1, color: 1, f: (v) => v == null ? "–" : sgn(v * 100, 2) + "%" }],
    Object.entries(m.direction_hit).map(([k, v]) => ({ name: names[k], ...v })));
}

// ------------------------------------------------------------- leaderboard
function renderRules() {
  const vf = $("#qVerdict").value, cf = $("#qCategory").value;
  const rows = Q.rep.rules.filter((r) => (!vf || r.verdict === vf) && (!cf || r.category === cf))
    .sort((a, b) => (b.markets.NIFTY.holdout.sharpe ?? -9) - (a.markets.NIFTY.holdout.sharpe ?? -9));
  const S = (mk, p = "full") => (r) => r.markets[mk]?.[p]?.sharpe;
  const sh = (v) => v == null ? "–" : fmt(v);
  table("#qRules", [
    { h: "Rule", k: (r) => r, f: (_, r) => `${r.member ? "✓ " : ""}${esc(r.name)}` }, { h: "Type", k: "category" },
    { h: "Nifty train", k: S("NIFTY", "train"), num: 1, color: 1, f: sh }, { h: "test", k: S("NIFTY", "test"), num: 1, color: 1, f: sh },
    { h: "holdout", k: S("NIFTY", "holdout"), num: 1, color: 1, f: sh },
    { h: "Beats random", k: (r) => r.random ? 1 - r.random.p_value : null, num: 1, f: (v) => v == null ? "–" : pct(v, 0) },
    { h: "Bank Nifty", k: S("BANKNIFTY"), num: 1, color: 1, f: sh }, { h: "Sensex", k: S("SENSEX"), num: 1, color: 1, f: sh },
    { h: "S&P 500", k: S("SP500"), num: 1, color: 1, f: sh }, { h: "Nasdaq", k: S("NASDAQ"), num: 1, color: 1, f: sh },
    { h: "Long-only", k: (r) => r.markets.NIFTY.long_only.sharpe, num: 1, f: sh },
    { h: "Trades/yr", k: (r) => r.markets.NIFTY.full.trades_per_year, num: 1, f: (v) => v == null ? "–" : fmt(v, 0) },
    { h: "Verdict", k: "verdict", f: (v, r) => `<span class="verdict-chip ${esc(v)}" title="${esc(r.why)}">${esc(v)}</span>` },
  ], rows);
}

// ------------------------------------------------------------------ moves
function renderMoves() {
  const blob = Q.rep.moves[Q.move];
  const s = blob[Q.period];
  $("#qMoveMeta").textContent = `${s.name}: ${s.from} → ${s.to}, ${s.days.toLocaleString("en-IN")} days. Base rates: up ${pct(s.base.p_up_1d, 0)} of days, ` +
    `${pct(s.base.p_up_5d, 0)} of weeks, ${pct(s.base.p_up_250d, 0)} of years.` + (Q.period === "recent" ? ` (Recent decade = since ${blob.recent_from}.)` : "");
  $("#qMoveFindings").innerHTML = s.findings.map((f) => `<li>${esc(f)}</li>`).join("");
  renderToday(s.today);

  const P = (d = 0) => (v) => v == null ? "–" : pct(v, d);
  const A = (v) => v == null ? "–" : sgn(v * 100, 2) + "%";
  const bucketCols = (h, keys) => [{ h, k: (r) => r, f: (_, r) => esc(r.bucket) + star(r) }, { h: "Cases", k: "n", num: 1 },
    { h: "p(up)", k: "p_up", num: 1, f: P() }, { h: "z", k: "z", num: 1, color: 1, f: (v) => sgn(v, 1) },
    ...keys.map((k) => ({ h: `Avg ${k}`, k: `avg_${k}`, num: 1, color: 1, f: A }))];
  table("#qDay", bucketCols("Day move", ["1d", "5d"]), s.day_moves);
  table("#qWeek", bucketCols("5-day move", ["5d", "20d"]), s.week_moves);
  table("#qDD", bucketCols("Below peak", ["60d", "250d"]), s.drawdowns);
  table("#qDist", bucketCols("vs 200-DMA", ["20d", "60d"]), s.dist200);
  table("#qRsi", bucketCols("RSI", ["5d", "20d"]), s.rsi);
  table("#qHL", bucketCols("Event", ["20d", "60d"]), s.highs_lows);
  table("#qMarkov", [{ h: "State", k: "state" }, { h: "Cases", k: "n", num: 1 }, { h: "Next day up", k: "p_up_next", num: 1, f: P() },
    { h: "z", k: "z", num: 1, color: 1, f: (v) => sgn(v, 1) }], s.markov);
  table("#qGaps", [{ h: "Gap at open", k: "gap" }, { h: "Days", k: "n", num: 1 }, { h: "Avg open→close", k: "avg_open_to_close", num: 1, color: 1, f: A },
    { h: "Closed above open", k: "p_close_above_open", num: 1, f: P() }, { h: "Gap filled", k: "p_filled", num: 1, f: P() }], s.gaps);
  table("#qTom", [{ h: "Day", k: "day" }, { h: "Avg", k: "avg", num: 1, color: 1, f: A }, { h: "p(up)", k: "p_up", num: 1, f: P() }, { h: "Days", k: "n", num: 1 }], s.turn_of_month);
  table("#qDow", [{ h: "Day", k: "day" }, { h: "Avg", k: "avg", num: 1, color: 1, f: A }, { h: "p(up)", k: "p_up", num: 1, f: P() }, { h: "Days", k: "n", num: 1 }], s.weekday);
  table("#qMonth", [{ h: "Month", k: "month" }, { h: "Avg month", k: "avg_month", num: 1, color: 1, f: A }, { h: "Months up", k: "p_up_month", num: 1, f: P() }], s.month);
  const vc = s.vol_clustering;
  $("#qVol").innerHTML = `After a top-10% move, the next day was also a top-10% move <b>${pct(vc.p_big_after_big, 0)}</b> of the time (normally ${pct(vc.p_big_base, 0)}).
    After a big down day the next day rose ${pct(vc.p_up_after_big_down, 0)} of the time, and after a big up day ${pct(vc.p_up_after_big_up, 0)}.`;

  const st = s.streaks;
  chartjs("qStreakChart", {
    data: { labels: st.map((r) => r.bucket.replace(" or more", "+")),
      datasets: [
        { type: "bar", label: "Next day up", data: st.map((r) => r.p_up), backgroundColor: st.map((r) => r.bucket.startsWith("-") ? css("--neg-3") : css("--pos-3")), borderRadius: 4, maxBarThickness: 28 },
        { type: "line", label: "Base rate", data: st.map(() => s.base.p_up_1d), borderColor: css("--ink-2"), borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 },
      ] },
    options: { maintainAspectRatio: false, plugins: { legend: { labels: { boxWidth: 12, color: css("--ink-2") } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${pct(c.parsed.y, 1)}` + (c.datasetIndex === 0 ? ` (${st[c.dataIndex].n} cases)` : "") } } },
      scales: { x: { title: { display: true, text: "consecutive up (+) / down (−) days", color: css("--muted") }, grid: { display: false }, ticks: { color: css("--muted") } },
                y: { min: 0.35, max: 0.7, ticks: { color: css("--muted"), callback: (v) => pct(v, 0) }, grid: { color: css("--grid") } } } },
  });
  const ac = s.autocorr_by_decade;
  chartjs("qAuto", {
    type: "bar",
    data: { labels: ac.map((d) => d.decade), datasets: [{ label: "Lag-1 autocorrelation", data: ac.map((d) => d.lag1_autocorr),
      backgroundColor: ac.map((d) => d.lag1_autocorr >= 0 ? css("--pos-3") : css("--neg-3")), borderRadius: 4, maxBarThickness: 36 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: css("--muted") } }, y: { ticks: { color: css("--muted") }, grid: { color: css("--grid") } } } },
  });
}

function renderToday(t) {
  const s = t.state, a = t.analogs;
  const row = (k, label) => `<tr><td>${label}</td><td class="num">${a[k].n}</td><td class="num">${pct(a[k].p_up, 0)}</td>
    <td class="num ${cls(a[k].median)}">${sgn(a[k].median * 100, 2)}%</td><td class="num">${sgn(a[k].p10 * 100, 1)}% … ${sgn(a[k].p90 * 100, 1)}%</td></tr>`;
  $("#qToday").innerHTML = `<p class="small">As of <b>${esc(t.date)}</b> close ${fmt(t.close)}: day ${sgn(s.day_move * 100, 2)}%, week ${sgn(s.week_move * 100, 1)}%, month ${sgn(s.month_move * 100, 1)}%,
      streak ${s.streak > 0 ? "+" : ""}${s.streak}, ${sgn(s.dist200 * 100, 1)}% vs 200-DMA, ${sgn(s.drawdown * 100, 1)}% from peak, RSI ${s.rsi14}, volatility ×${s.vol_ratio} normal.</p>
    <table><thead><tr><th>Next</th><th class="num">Analogs</th><th class="num">p(up)</th><th class="num">Median</th><th class="num">10th … 90th pct</th></tr></thead>
    <tbody>${row("1d", "1 day")}${row("5d", "5 days")}${row("20d", "20 days")}</tbody></table>
    <p class="small muted">Closest matches: ${t.analog_dates.map((d) => `${esc(d.date)}${d.next_5d != null ? ` (${sgn(d.next_5d * 100, 1)}%)` : ""}`).join(", ")}</p>`;
}

$("#btnQuant").onclick = async () => {
  await api("/api/quant/run", "POST");
  $("#btnQuant").disabled = true;
  $("#btnQuant").textContent = "Running… (1–2 min)";
  const t = setInterval(async () => { const r = await api("/api/quant"); if (!r.running) { clearInterval(t); window.loadQuant(); } }, 5000);
};
