/* WC Mexico 26 — Edge Finder · client app + bet optimizer
   The optimizer below mirrors backend/optimizer.py so the published (static)
   calculator and the live API produce the same plan. */
"use strict";

/* ---------------- flags + format ---------------- */
const ISO = {
  "Mexico":"mx","South Africa":"za","South Korea":"kr","Czech Republic":"cz",
  "Canada":"ca","Bosnia and Herzegovina":"ba","Qatar":"qa","Switzerland":"ch",
  "Brazil":"br","Haiti":"ht","Morocco":"ma","Scotland":"gb-sct",
  "Australia":"au","Paraguay":"py","Turkey":"tr","United States":"us",
  "Curaçao":"cw","Ecuador":"ec","Germany":"de","Ivory Coast":"ci",
  "Japan":"jp","Netherlands":"nl","Sweden":"se","Tunisia":"tn",
  "Belgium":"be","Egypt":"eg","Iran":"ir","New Zealand":"nz",
  "Cape Verde":"cv","Saudi Arabia":"sa","Spain":"es","Uruguay":"uy",
  "France":"fr","Iraq":"iq","Norway":"no","Senegal":"sn",
  "Algeria":"dz","Argentina":"ar","Austria":"at","Jordan":"jo",
  "Colombia":"co","DR Congo":"cd","Portugal":"pt","Uzbekistan":"uz",
  "Croatia":"hr","England":"gb-eng","Ghana":"gh","Panama":"pa",
};
function flag(team) {
  const c = ISO[team];
  if (!c) return "🏳️";
  if (c === "gb-sct") return "🏴󠁧󠁢󠁳󠁣󠁴󠁿";
  if (c === "gb-eng") return "🏴󠁧󠁢󠁥󠁮󠁧󠁿";
  return [...c.toUpperCase()].map(ch => String.fromCodePoint(0x1F1A5 + ch.charCodeAt(0))).join("");
}
const CODE = {
  "Mexico":"MEX","South Africa":"RSA","South Korea":"KOR","Czech Republic":"CZE",
  "Canada":"CAN","Bosnia and Herzegovina":"BIH","Qatar":"QAT","Switzerland":"SUI",
  "Brazil":"BRA","Haiti":"HAI","Morocco":"MAR","Scotland":"SCO","Australia":"AUS",
  "Paraguay":"PAR","Turkey":"TUR","United States":"USA","Curaçao":"CUW","Ecuador":"ECU",
  "Germany":"GER","Ivory Coast":"CIV","Japan":"JPN","Netherlands":"NED","Sweden":"SWE",
  "Tunisia":"TUN","Belgium":"BEL","Egypt":"EGY","Iran":"IRN","New Zealand":"NZL",
  "Cape Verde":"CPV","Saudi Arabia":"KSA","Spain":"ESP","Uruguay":"URU","France":"FRA",
  "Iraq":"IRQ","Norway":"NOR","Senegal":"SEN","Algeria":"ALG","Argentina":"ARG",
  "Austria":"AUT","Jordan":"JOR","Colombia":"COL","DR Congo":"COD","Portugal":"POR",
  "Uzbekistan":"UZB","Croatia":"CRO","England":"ENG","Ghana":"GHA","Panama":"PAN",
};
const code = t => CODE[t] || (t ? t.replace(/[^A-Za-z]/g,"").slice(0,3).toUpperCase() : "—");
const pct = x => x == null ? "—" : (100 * x).toFixed(1) + "%";
const pp = x => (x >= 0 ? "+" : "") + (100 * x).toFixed(2) + "pp";
const fair = p => (p && p > 0) ? (1 / p).toFixed(1 / p >= 100 ? 0 : 2) : "—";
const money = x => "$" + (Math.round(x * 100) / 100).toFixed(2);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dayName = iso => iso ? new Date(iso).toDateString().toUpperCase() : "TBD";
const clockUTC = iso => iso ? new Date(iso).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : "";

/* ---------------- state ---------------- */
let S = null, LIVE = false, ACTIVE = "today";
let ODDS = {};                       // key -> decimal odds (sample ∪ user edits)
let BANKROLL = 200, KELLY = 0.25;
const LS = {
  odds: "wc26_odds_v1", bank: "wc26_bankroll", kelly: "wc26_kelly",
};

async function loadSnapshot(bust) {
  // On localhost the FastAPI server is live (fresh data + write actions); on a
  // static host (Netlify / GitHub Pages) read the exported snapshot file. The
  // hostname check avoids a spurious /api 404 in the console when static.
  const q = bust ? ("?t=" + Date.now()) : "";   // beat CDN cache on manual refresh
  const local = ["localhost", "127.0.0.1", ""].includes(location.hostname);
  if (local) {
    try {
      const r = await fetch("/api/snapshot" + q, {cache:"no-store"});
      if (r.ok && (r.headers.get("content-type") || "").includes("application/json")) {
        S = await r.json(); LIVE = true; return;
      }
    } catch {}
  }
  const r = await fetch("./data/snapshot.json" + q, {cache:"no-store"});
  S = await r.json(); LIVE = false;
}

function initState() {
  BANKROLL = parseFloat(localStorage.getItem(LS.bank)) || S.meta.bankroll || 200;
  KELLY = parseFloat(localStorage.getItem(LS.kelly)) || S.meta.kelly_fraction || 0.25;
  const saved = JSON.parse(localStorage.getItem(LS.odds) || "{}");
  ODDS = Object.assign({}, S.sample_odds, saved);
  applyControls();
  initStateLabel();
}
function applyControls() {
  for (const id of ["in-bankroll", "t-bankroll"]) { const el = document.getElementById(id); if (el) el.value = BANKROLL; }
  for (const id of ["in-kelly", "t-kelly"]) { const el = document.getElementById(id); if (el) el.value = KELLY; }
}
const userOdds = () => JSON.parse(localStorage.getItem(LS.odds) || "{}");
function setOdds(key, val) {
  const u = userOdds();
  if (val && val > 1) { u[key] = val; ODDS[key] = val; }
  else { delete u[key]; ODDS[key] = S.sample_odds[key]; }
  localStorage.setItem(LS.odds, JSON.stringify(u));
}

/* ================= OPTIMIZER (mirror of backend/optimizer.py) ============= */
const edge = (p, o) => p * o - 1;
const kellyF = (p, o) => { const b = o - 1; return b <= 0 ? 0 : Math.max(0, (p*b-(1-p))/b); };
function* combos(arr, r, start, pick) {
  pick = pick || []; start = start || 0;
  if (pick.length === r) { yield pick.slice(); return; }
  for (let i = start; i < arr.length; i++) { pick.push(arr[i]); yield* combos(arr, r, i+1, pick); pick.pop(); }
}
function optimize(cands, bankroll, mult, opt) {
  opt = Object.assign({maxLegs:4, minP:0.04, pool:12, cap:0.5, maxParlays:3}, opt||{});
  const pool = cands.filter(c => c.model_p != null && c.decimal_odds > 1).map(c => ({
    ...c, _teams: new Set(c.teams || [c.selection]),
    edge: edge(c.model_p, c.decimal_odds), kelly: kellyF(c.model_p, c.decimal_odds),
    implied_p: 1 / c.decimal_odds,
  }));
  // cap the edge — a model "edge" far above the market is model error, not value
  const MAX_EDGE = 0.20;
  const value = pool.filter(c => c.edge > 1e-9 && c.edge <= MAX_EDGE).sort((a,b) => b.edge - a.edge);

  // singles: one bet per mutually-exclusive group (can't back two outcomes of
  // a match, and only one team wins each outright) — keep best edge per group.
  const mgroup = c => c.mutex || (c.market.startsWith("match:") ? c.market : "outright:" + c.market);
  const seenG = new Set(); const dedup = [];
  for (const c of value) { const g = mgroup(c); if (seenG.has(g)) continue; seenG.add(g); dedup.push(c); }
  let singles = dedup.map(c => ({...c, stake: bankroll * mult * c.kelly}));
  const total = singles.reduce((s,x) => s + x.stake, 0), capAmt = bankroll * opt.cap;
  const scale = (total > capAmt && total > 0) ? capAmt / total : 1;
  singles.forEach(s => { s.stake = Math.round(s.stake*scale*100)/100; s.exp_profit = Math.round(s.stake*s.edge*100)/100; });

  // parlays: built from the SAME one-per-market value picks as the singles, so
  // a match's recommended side never differs between the singles and the parlay.
  const legs = dedup.slice(0, opt.pool), scored = [];
  for (let r = 2; r <= opt.maxLegs; r++) {
    for (const combo of combos(legs, r)) {
      const tset = new Set(); let ok = true;
      for (const l of combo) { for (const t of l._teams) { if (tset.has(t)) { ok = false; break; } tset.add(t); } if (!ok) break; }
      if (!ok) continue;
      let O = 1, P = 1;
      for (const l of combo) { O *= l.decimal_odds; P *= l.model_p; }
      if (P < opt.minP) continue;
      const E = P*O - 1; if (E <= 0) continue;
      scored.push({growth: E*E/(O-1), combo, O, P, E});
    }
  }
  scored.sort((a,b) => b.growth - a.growth);
  const parlays = [], seen = new Set();
  for (const s of scored) {
    const sig = s.combo.map(l => l.key).sort().join("·");
    if (seen.has(sig)) continue; seen.add(sig);
    const f = kellyF(s.P, s.O), stake = Math.round(Math.min(bankroll*mult*f, bankroll*0.05)*100)/100;
    parlays.push({
      legs: s.combo.map(l => ({label:l.label, selection:l.selection, decimal_odds:l.decimal_odds,
                               model_p:l.model_p, edge:l.edge, teams:l.teams, team:l.team, kind:l.kind})),
      combined_odds: Math.round(s.O*100)/100, model_p: s.P, implied_p: 1/s.O, edge: s.E,
      kelly: f, stake, potential_return: Math.round(stake*s.O*100)/100, exp_profit: Math.round(stake*s.E*100)/100,
    });
    if (parlays.length >= opt.maxParlays) break;
  }
  const sStake = Math.round(singles.reduce((s,x)=>s+x.stake,0)*100)/100;
  const sProfit = Math.round(singles.reduce((s,x)=>s+x.exp_profit,0)*100)/100;
  return {singles, parlays, summary: {
    n_value: value.length, singles_stake: sStake, singles_exp_profit: sProfit,
    singles_roi: sStake ? sProfit/sStake : 0,
  }};
}
function candidates() {
  return S.markets.filter(m => ODDS[m.key] > 1)
    .map(m => ({...m, decimal_odds: ODDS[m.key]}));
}

/* ================= BEST BETS tab ================= */
let showAllSingles = false;
function renderBets() {
  const plan = optimize(candidates(), BANKROLL, KELLY);
  // singles board = match 1X2 + outrights (props live on the Today tab + parlays)
  const singles = plan.singles.filter(s => s.kind !== "prop");
  const stake = Math.round(singles.reduce((a,x)=>a+x.stake,0)*100)/100;
  const profit = Math.round(singles.reduce((a,x)=>a+x.exp_profit,0)*100)/100;
  document.getElementById("bets-summary").innerHTML = [
    ["Value bets", singles.length, "+EV singles", ""],
    ["Total stake", money(stake), `${(KELLY*100)|0}% Kelly`, ""],
    ["Exp. profit", money(profit), "if odds hold", "good"],
    ["Exp. ROI", (stake ? profit/stake*100 : 0).toFixed(1)+"%", "on staked", "good"],
  ].map(([k,v,n,c]) => `<div class="stat"><div class="k">${k}</div>
    <div class="v ${c}">${v}</div><div class="note">${n}</div></div>`).join("");

  const shown = showAllSingles ? singles : singles.slice(0, 6);
  document.getElementById("singles-list").innerHTML = shown.map((s,i) => betCard(s, i===0)).join("")
    || `<div class="empty">No positive-edge singles at these odds.<br>Lower your prices or check back after results move the model.</div>`;
  const more = document.getElementById("singles-more");
  if (singles.length > 6) {
    more.classList.remove("hidden");
    more.textContent = showAllSingles ? "Show fewer" : `Show all ${singles.length} value singles`;
  } else more.classList.add("hidden");

  document.getElementById("parlays-list").innerHTML = plan.parlays.map((p,i) => parlayCard(p,i)).join("")
    || `<div class="empty">No positive-edge parlay from independent legs right now.</div>`;
  renderUpsets();
}
/* +EV underdogs / draws: a high-variance portfolio where the rare winners pay
   for the many losers — works ONLY because each leg is +EV (model > market). */
function renderUpsets() {
  const picks = []; const seen = new Set();
  candidates()
    .filter(c => (c.kind==="match"||c.kind==="outright") && c.decimal_odds>=2.8)
    .map(c => ({...c, edge: edge(c.model_p,c.decimal_odds), kelly: kellyF(c.model_p,c.decimal_odds), implied_p: 1/c.decimal_odds}))
    .filter(c => c.edge > 1e-9 && c.edge <= 0.20).sort((a,b) => b.edge - a.edge)
    .forEach(c => { const g=c.mutex||c.market; if (!seen.has(g)){ seen.add(g); picks.push(c); } });
  picks.forEach(c => c.stake = BANKROLL*KELLY*c.kelly);
  const raw = picks.reduce((a,c)=>a+c.stake,0), cap = BANKROLL*0.5, sc = (raw>cap&&raw>0)?cap/raw:1;
  picks.forEach(c => c.stake = Math.round(c.stake*sc*100)/100);
  const stake = Math.round(picks.reduce((a,c)=>a+c.stake,0)*100)/100;
  const expWin = picks.reduce((a,c)=>a+c.model_p,0);
  const expProfit = Math.round(picks.reduce((a,c)=>a+c.stake*c.edge,0)*100)/100;
  const pOne = 1 - picks.reduce((a,c)=>a*(1-c.model_p),1);
  document.getElementById("upsets-summary").innerHTML = picks.length ? [
    ["Upset plays", picks.length, "odds ≥ 2.8", ""],
    ["Stake", money(stake), `${(KELLY*100)|0}% Kelly`, ""],
    ["Exp. winners", expWin.toFixed(1), `of ${picks.length} · ${(pOne*100).toFixed(0)}% ≥1 lands`, ""],
    ["Exp. profit", money(expProfit), stake?`+${(expProfit/stake*100).toFixed(0)}% ROI`:"", expProfit>0?"good":""],
  ].map(([k,v,n,c]) => `<div class="stat"><div class="k">${k}</div>
    <div class="v ${c}">${v}</div><div class="note">${n}</div></div>`).join("") : "";
  document.getElementById("upsets-list").innerHTML = picks.slice(0,10).map(c => {
    const team = c.team || (c.teams && c.teams[0]);
    return `<div class="bet"><div class="bet-head"><span class="flag">${flag(team)}</span>
      <div class="bet-title">${esc(c.label)}<span class="mk">${settleLabel(c)} · pays ${(c.decimal_odds*1).toFixed(2)}×</span></div>
      <span class="edge-chip">+${(c.edge*100).toFixed(0)}%</span></div>
      <div class="bet-grid">
        <div class="cell"><div class="k">Odds</div><div class="v">${c.decimal_odds.toFixed(2)}</div></div>
        <div class="cell"><div class="k">Model</div><div class="v">${pct(c.model_p)}</div></div>
        <div class="cell"><div class="k">Implied</div><div class="v">${pct(c.implied_p)}</div></div>
        <div class="cell"><div class="k">Stake</div><div class="v stake">${money(c.stake)}</div></div>
      </div></div>`;
  }).join("") || `<div class="empty">No +EV underdogs at these odds — the market isn't mispricing any longshots right now.</div>`;
  document.getElementById("upsets-note").textContent = picks.length
    ? "Most of these lose — that's expected. You come out ahead only because each is +EV (the model rates it likelier than the price implies), so the few winners more than cover the rest over many bets. High variance: expect long dry runs, keep stakes small, and judge it over a big sample — not one slate."
    : "";
}
function settleLabel(s) {
  if (s.kind === "match" || s.market.startsWith("match:"))
    return s.kickoff ? "settles " + new Date(s.kickoff).toLocaleDateString([], {month:"short", day:"numeric"}) : "settles match day";
  return "long-term · settles Jul";
}
function betCard(s, top) {
  const team = s.team || (s.teams && s.teams[0]);
  const mk = s.kind === "prop" ? esc(s.match || "player prop")
    : {champion:"World Cup winner", final:"Reach final", sf:"Reach semis"}[s.market]
    || (s.market.startsWith("match:") ? (s.stage?s.stage.toUpperCase():"")+" · match" : s.market);
  const w = Math.min(100, 100 * s.model_p), wi = Math.min(100, 100 * s.implied_p);
  return `<div class="bet ${top?"top":""}">
    <div class="bet-head"><span class="flag">${flag(team)}</span>
      <div class="bet-title">${esc(s.label)}<span class="mk">${esc(mk)} · ${settleLabel(s)}</span></div>
      <span class="edge-chip">+${(s.edge*100).toFixed(1)}%</span></div>
    <div class="bet-bar"><i class="model" style="width:${wi}%"></i><i class="gap" style="width:${Math.max(0,w-wi)}%"></i></div>
    <div class="bet-grid">
      <div class="cell"><div class="k">Your odds</div>
        <input class="odds-edit" inputmode="decimal" data-key="${esc(s.key)}" value="${s.decimal_odds}"></div>
      <div class="cell"><div class="k">Model</div><div class="v">${pct(s.model_p)}</div></div>
      <div class="cell"><div class="k">Implied</div><div class="v">${pct(s.implied_p)}</div></div>
      <div class="cell"><div class="k">Stake</div><div class="v stake">${money(s.stake)}</div></div>
    </div></div>`;
}
function parlayCard(p, i) {
  const legs = p.legs.map(l => `<div class="leg"><span class="flag">${flag(l.team || (l.teams&&l.teams[0]))}</span>
    <span class="ln">${esc(l.label)}</span><span class="lo">${l.decimal_odds.toFixed(2)}</span></div>`).join("");
  return `<div class="parlay ${i===0?"best":""}">
    <div class="parlay-top"><div><div class="lbl">${i===0?"★ Top parlay":"Alt parlay"} · ${p.legs.length} legs</div>
      <div class="micro" style="margin-top:3px">stake ${money(p.stake)} → returns <b style="color:var(--gold)">${money(p.potential_return)}</b></div></div>
      <div class="ret"><div class="v">+${(p.edge*100).toFixed(0)}%</div><div class="k">edge</div></div></div>
    ${legs}
    <div class="parlay-foot">
      <div class="cell"><div class="k">Odds</div><div class="v">${p.combined_odds.toFixed(2)}</div></div>
      <div class="cell"><div class="k">Win prob</div><div class="v">${pct(p.model_p)}</div></div>
      <div class="cell"><div class="k">Stake</div><div class="v stake">${money(p.stake)}</div></div>
      <div class="cell"><div class="k">Returns</div><div class="v">${money(p.potential_return)}</div></div>
    </div></div>`;
}

/* ================= TODAY tab ================= */
function todaySlate() {
  // the next slate to be played: matches sharing the earliest upcoming *local*
  // day (grouping by local date keeps it consistent with the times shown).
  const up = S.matches.filter(m => m.home_score == null && m.home_team && m.away_team
    && m.kickoff_utc && m.forecast)
    .sort((a,b) => a.kickoff_utc.localeCompare(b.kickoff_utc));
  if (!up.length) return null;
  const localDay = m => new Date(m.kickoff_utc).toDateString();
  const day = localDay(up[0]);
  const matches = up.filter(m => localDay(m) === day);
  return {day, matches, isToday: day === new Date().toDateString()};
}
function todayCandidates(slate) {
  const nos = new Set(slate.matches.map(m => m.number));
  return S.markets.filter(m => m.match_no != null && nos.has(m.match_no) && ODDS[m.key] > 1)
    .map(m => ({...m, decimal_odds: ODDS[m.key]}));
}
function renderToday() {
  const slate = todaySlate();
  if (!slate) {
    document.getElementById("today-matches").innerHTML = `<div class="empty">No upcoming matches in the data.</div>`;
    document.getElementById("today-summary").innerHTML = "";
    document.getElementById("today-acca").innerHTML = ""; return;
  }
  const plan = optimize(todayCandidates(slate), BANKROLL, KELLY);
  const matchSingles = plan.singles.filter(s => s.kind === "match");
  const pickBy = {}; matchSingles.forEach(s => pickBy[s.match_no] = s);
  const stake = Math.round(matchSingles.reduce((a,x)=>a+x.stake,0)*100)/100;
  const expP = Math.round(matchSingles.reduce((a,x)=>a+x.exp_profit,0)*100)/100;
  const bestCase = Math.round(matchSingles.reduce((a,x) => a + x.stake*(x.decimal_odds-1), 0)*100)/100;
  document.getElementById("today-summary").innerHTML = [
    ["Today's picks", matchSingles.length, "settle tonight", ""],
    ["Stake", money(stake), `${(KELLY*100)|0}% Kelly`, ""],
    ["Exp. profit", money(expP), "on average", "good"],
    ["If all win", "+"+money(bestCase), `bankroll → ${money(BANKROLL+bestCase)}`, "good"],
  ].map(([k,v,n,c]) => `<div class="stat"><div class="k">${k}</div>
    <div class="v ${c}">${v}</div><div class="note">${n}</div></div>`).join("");
  document.getElementById("today-date").textContent = (slate.isToday ? "TODAY · " : "") + slate.day.toUpperCase();
  document.getElementById("today-matches").innerHTML =
    slate.matches.map(m => todayMatchCard(m, pickBy[m.number])).join("");
  renderSGP(slate);
  renderTodayProps(slate);
  renderAccuracy();
  document.getElementById("today-acca").innerHTML = plan.parlays.length ? parlayCard(plan.parlays[0], 0)
    : `<div class="empty">No +EV multi-match parlay from today's card at these odds.</div>`;
  document.getElementById("today-sub").textContent = slate.isToday
    ? "Bets that settle tonight — win, update your bankroll, repeat tomorrow."
    : "Next match day — these settle the same day, so you can iterate fast.";
}
function todayMatchCard(m, pick) {
  const fc = m.forecast || {};
  const labels = {home: code(m.home_label), draw: "Draw", away: code(m.away_label)};
  const cells = ["home","draw","away"].map(sel => {
    const key = `match:${m.number}|${sel}`, o = ODDS[key], p = fc[sel];
    const e = o > 1 ? p*o - 1 : null, ec = e == null ? "flat" : e > 0 ? "up" : "down";
    const isPick = pick && pick.selection === sel;
    return `<div class="tm-out ${isPick?"pick":""}"><div class="s">${labels[sel]}</div>
      <div class="mp">${pct(p)}</div>
      <input class="odds-edit tm-odds" inputmode="decimal" data-key="${esc(key)}" value="${o ?? ""}">
      <div class="e ${ec}">${e == null ? "—" : (e>0?"+":"")+(e*100).toFixed(0)+"%"}</div></div>`;
  }).join("");
  const pickLine = pick
    ? `<div class="tm-pick"><b>✓ ${esc(pick.label)}</b> @ ${pick.decimal_odds} · edge +${(pick.edge*100).toFixed(1)}% → stake <b>${money(pick.stake)}</b></div>`
    : `<div class="tm-pick none">No value at these odds — try your bookmaker's prices.</div>`;
  return `<div class="tmatch">
    <div class="tm-head"><span>M${m.number} · ${esc(m.stage)}${m.group_letter?" "+m.group_letter:""}</span>
      <span>${clockUTC(m.kickoff_utc)}</span></div>
    <div class="tm-teams">${esc(m.home_label)} ${flag(m.home_label)} <span class="vs">v</span> ${flag(m.away_label)} ${esc(m.away_label)}</div>
    <div class="tm-grid">${cells}</div>
    ${pickLine}</div>`;
}
function renderAccuracy() {
  const a = S.meta && S.meta.accuracy, el = document.getElementById("model-accuracy");
  if (!el) return;
  if (!a) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="acc-card">
    <div class="acc-head">📊 Model accuracy <span class="hint">match 1X2 · backtested on ${(a.n||0).toLocaleString()} internationals</span></div>
    <div class="acc-grid">
      <div><b>${(a.accuracy*100).toFixed(0)}%</b><span>correct<br>(chance ${(a.baseline_accuracy*100).toFixed(0)}%)</span></div>
      <div><b class="up">+${a.logloss_edge_pct}%</b><span>better than<br>no-skill</span></div>
      <div><b>${(a.ece*100).toFixed(1)}%</b><span>calibration<br>error (low=good)</span></div>
    </div>
    <p class="micro">Genuinely well-calibrated on single matches. Champion/outright odds are noisier (the tournament sim over-credits underdog deep runs). Any new model feature has to beat these numbers out-of-sample to ship.</p>
  </div>`;
}
function renderTodayProps(slate) {
  const nos = new Set(slate.matches.map(m => m.number));
  const byMatch = {};
  S.markets.filter(m => m.kind === "prop" && nos.has(m.match_no))
    .forEach(p => { (byMatch[p.match_no] = byMatch[p.match_no] || []).push(p); });
  const html = slate.matches.filter(m => byMatch[m.number]).map(m => {
    const rows = byMatch[m.number].sort((a,b) => b.model_p - a.model_p).slice(0, 10).map(pr => {
      const o = ODDS[pr.key], e = o > 1 ? pr.model_p*o - 1 : null;
      const ec = e == null ? "flat" : e > 0 ? "up" : "down";
      return `<div class="prop"><span class="flag">${flag(pr.team)}</span>
        <span class="pn">${esc(pr.player)} <i>${esc(pr.prop_type)}</i></span>
        <span class="pp">${pct(pr.model_p)}</span>
        <input class="odds-edit prop-odds" inputmode="decimal" data-key="${esc(pr.key)}" value="${o ?? ""}">
        <span class="pe ${ec}">${e == null ? "—" : (e>0?"+":"")+(e*100).toFixed(0)+"%"}</span></div>`;
    }).join("");
    return `<details class="prop-match" open><summary>${esc(code(m.home_label))} v ${esc(code(m.away_label))}
      <span class="hint">${byMatch[m.number].length} props</span></summary>${rows}</details>`;
  }).join("");
  document.getElementById("today-props").innerHTML = html ||
    `<div class="empty">No player props for today's matches.</div>`;
}

/* ===== same-game parlay engine (correlation-aware via the score matrix) ===== */
const SGP = {};                               // matchNo -> builder state
function scoreMatrix(lh, la, rho, N) {
  N = N || 10;
  const f = [1]; for (let i=1;i<=N;i++) f[i]=f[i-1]*i;
  const ph=[], pa=[];
  for (let g=0;g<=N;g++){ ph[g]=Math.exp(-lh)*Math.pow(lh,g)/f[g]; pa[g]=Math.exp(-la)*Math.pow(la,g)/f[g]; }
  const m=[]; for (let i=0;i<=N;i++){ m[i]=[]; for (let j=0;j<=N;j++) m[i][j]=ph[i]*pa[j]; }
  m[0][0]*=1-lh*la*rho; m[0][1]*=1+lh*rho; m[1][0]*=1+la*rho; m[1][1]*=1-rho;
  let s=0; for (let i=0;i<=N;i++) for (let j=0;j<=N;j++) s+=m[i][j];
  for (let i=0;i<=N;i++) for (let j=0;j<=N;j++) m[i][j]/=s;
  return m;
}
function legPred(cat, side, line) {
  if (cat==="winner") return side==="home"?(i,j)=>i>j : side==="away"?(i,j)=>i<j : (i,j)=>i===j;
  if (cat==="total")  return side==="over"?(i,j)=>i+j>line : (i,j)=>i+j<line;
  if (cat==="hometot")return side==="over"?(i,j)=>i>line   : (i,j)=>i<line;
  if (cat==="awaytot")return side==="over"?(i,j)=>j>line   : (i,j)=>j<line;
  return () => true;
}
function jointGoalProb(m, preds) {
  let p=0; for (let i=0;i<m.length;i++) for (let j=0;j<m.length;j++)
    if (preds.every(fn=>fn(i,j))) p+=m[i][j];
  return p;
}
const legMarginal = (m,cat,side,line) => jointGoalProb(m,[legPred(cat,side,line)]);
const cornerMean = (lh,la) => (2.6+1.7*lh)+(2.6+1.7*la);
function poisCdf(k,lam){ let s=0,t=Math.exp(-lam); for(let i=0;i<=k;i++){ s+=t; t*=lam/(i+1); } return s; }
const cornersProb = (side,line,lam) => side==="over" ? 1-poisCdf(Math.floor(line),lam) : poisCdf(Math.floor(line),lam);
const GOAL_CATS = ["winner","total","hometot","awaytot"];

function suggestSGP(m, fc) {
  // Anchor the winner on the SAME side the Today card / value board bets — the
  // +EV 1X2 outcome with the best edge vs your odds (model favourite only as a
  // fallback when no odds make a side +EV) — then build a positively-correlated
  // narrative around it. Keeps the recommended team consistent everywhere.
  const lh=fc.exp_goals_home, la=fc.exp_goals_away;
  const mat=scoreMatrix(lh,la,S.meta.rho), cm=cornerMean(lh,la);
  const st = {winner:{on:false,side:"home"}, total:{on:false,side:"over",line:2.5},
    hometot:{on:false,side:"over",line:1.5}, awaytot:{on:false,side:"over",line:1.5},
    corners:{on:false,side:"over",line:9.5}, odds:""};
  const bestOver = (fn, lines) => { let b=null; for (const L of lines) if (L>=0.5 && fn(L)>=0.5) b=L; return b; };
  const sideEdge = s => { const o=ODDS[`match:${m.number}|${s}`]; return o>1 ? fc[s]*o-1 : null; };
  const valued = ["home","draw","away"].map(s => ({s, e: sideEdge(s)}))
    .filter(x => x.e != null && x.e > 1e-9).sort((a,b) => b.e - a.e);
  let win = valued.length ? valued[0].s : null;          // best value side
  if (!win) {                                            // no odds → model favourite
    const wmax=Math.max(fc.home,fc.draw,fc.away);
    if (wmax>=0.45) win = fc.home===wmax?"home":fc.away===wmax?"away":"draw";
  }
  if (win) st.winner = {on:true, side:win};

  if (win==="home" || win==="away") {          // winner-dominance, positively correlated
    const favCat = win==="home"?"hometot":"awaytot";
    const fl = bestOver(L=>legMarginal(mat,favCat,"over",L), [0.5,1.5,2.5]);
    if (fl!=null) st[favCat] = {on:true, side:"over", line:fl};
    const tl = bestOver(L=>legMarginal(mat,"total","over",L), [1.5,2.5,3.5,4.5]);
    if (tl!=null) st.total = {on:true, side:"over", line:tl};
    const cl = bestOver(L=>cornersProb("over",L,cm),
      [Math.floor(cm)-2.5, Math.floor(cm)-1.5, Math.floor(cm)-0.5, Math.floor(cm)+0.5]);
    if (cl!=null) st.corners = {on:true, side:"over", line:cl};
  } else {                                     // draw / no clear winner → low-scoring lean
    st.total = {on:true, side:"under", line:Math.max(1.5, Math.floor(lh+la)+0.5)};
    const cl = Math.max(0.5, Math.floor(cm)+0.5);
    st.corners = {on:true, side: cornersProb("under",cl,cm)>=0.5?"under":"over", line:cl};
  }
  return st;
}
function computeSGP(m) {
  const fc=m.forecast, lh=fc.exp_goals_home, la=fc.exp_goals_away;
  const mat=scoreMatrix(lh,la,S.meta.rho), st=SGP[m.number], cm=cornerMean(lh,la);
  const preds=[]; let indep=1, n=0;
  GOAL_CATS.forEach(c => { if (st[c].on){ preds.push(legPred(c,st[c].side,st[c].line));
    indep*=legMarginal(mat,c,st[c].side,st[c].line); n++; } });
  let P = preds.length?jointGoalProb(mat,preds):1;
  if (st.corners.on){ const cp=cornersProb(st.corners.side,st.corners.line,cm); P*=cp; indep*=cp; n++; }
  return {P, indep, n, mat, cm};
}
function sgpLegName(m, cat, st) {
  const c=st[cat];
  if (cat==="winner") return c.side==="draw" ? "Draw" : `${code(c.side==="home"?m.home_label:m.away_label)} win`;
  if (cat==="total")  return `${c.side==="over"?"Over":"Under"} ${c.line} goals`;
  if (cat==="hometot")return `${code(m.home_label)} ${c.side==="over"?"o":"u"}${c.line}`;
  if (cat==="awaytot")return `${code(m.away_label)} ${c.side==="over"?"o":"u"}${c.line}`;
  return `${c.side==="over"?"Over":"Under"} ${c.line} corners`;
}
function sgpCard(m) {
  // refresh the suggestion as odds change, until the user hand-tweaks this card
  if (!SGP[m.number] || !SGP[m.number]._touched) SGP[m.number]=suggestSGP(m, m.forecast);
  const st=SGP[m.number], mat=scoreMatrix(m.forecast.exp_goals_home, m.forecast.exp_goals_away, S.meta.rho);
  const cm=cornerMean(m.forecast.exp_goals_home, m.forecast.exp_goals_away);
  const cats=[...GOAL_CATS,"corners"];
  const legRows = cats.map(cat => {
    const c=st[cat];
    const mp = cat==="corners" ? cornersProb(c.side,c.line,cm) : legMarginal(mat,cat,c.side,c.line);
    const stepper = cat==="winner" ? "" :
      `<span class="sgp-line"><button data-sgp="step" data-mno="${m.number}" data-leg="${cat}" data-dir="-1">–</button>
       ${c.line}<button data-sgp="step" data-mno="${m.number}" data-leg="${cat}" data-dir="1">+</button></span>`;
    const sideTxt = cat==="winner" ? (c.side==="home"?"1":c.side==="draw"?"X":"2") : (c.side==="over"?"Over":"Under");
    return `<div class="sgp-leg ${c.on?"on":""}">
      <button class="sgp-tog" data-sgp="tog" data-mno="${m.number}" data-leg="${cat}">${c.on?"✓":"+"}</button>
      <span class="sgp-name">${esc(sgpLegName(m,cat,st))}</span>
      <button class="sgp-side" data-sgp="side" data-mno="${m.number}" data-leg="${cat}">${sideTxt}</button>
      ${stepper}<span class="sgp-mp">${pct(mp)}</span></div>`;
  }).join("");
  const {P, indep, n} = computeSGP(m);
  const lift = (n>=2 && indep>0) ? (P/indep - 1) : null;
  const o = parseFloat(st.odds), hasO = o>1;
  const e = hasO ? P*o - 1 : null;
  const stake = hasO ? Math.round(Math.min(BANKROLL*KELLY*kellyF(P,o), BANKROLL*0.05)*100)/100 : 0;
  const foot = P<=0 ? `<div class="sgp-foot bad">impossible combo</div>` :
    `<div class="sgp-foot">
      <div class="sgp-stat"><span class="k">True prob</span><b>${pct(P)}</b></div>
      <div class="sgp-stat"><span class="k">Fair odds</span><b>${fair(P)}</b></div>
      <div class="sgp-stat"><span class="k">Corr. lift</span><b class="${lift>0?"up":lift<0?"down":""}">${lift==null?"—":(lift>0?"+":"")+(lift*100).toFixed(0)+"%"}</b></div>
      <div class="sgp-stat odds"><span class="k">Your SGP odds</span>
        <input class="sgp-odds" inputmode="decimal" data-mno="${m.number}" value="${esc(st.odds)}" placeholder="${fair(P)}"></div>
      <div class="sgp-stat"><span class="k">Edge</span><b class="${e>0?"up":e<0?"down":""}">${e==null?"—":(e>0?"+":"")+(e*100).toFixed(0)+"%"}</b></div>
      <div class="sgp-stat"><span class="k">Stake</span><b class="stake">${stake>0?money(stake):"—"}</b></div>
    </div>`;
  return `<div class="sgp">
    <div class="sgp-head"><b>${esc(code(m.home_label))} v ${esc(code(m.away_label))}</b>
      <span>M${m.number} · ${clockUTC(m.kickoff_utc)}</span></div>
    <div class="sgp-legs">${legRows}</div>${foot}</div>`;
}
function renderSGP(slate) {
  document.getElementById("today-sgp").innerHTML =
    slate.matches.map(m => sgpCard(m)).join("") ||
    `<div class="empty">No matches to build.</div>`;
}
document.addEventListener("click", e => {
  const t = e.target.closest("[data-sgp]"); if (!t) return;
  const mno=+t.dataset.mno, st=SGP[mno]; if (!st) return;
  st._touched = true;
  const act=t.dataset.sgp, leg=t.dataset.leg;
  if (act==="tog") st[leg].on=!st[leg].on;
  else if (act==="side") {
    if (leg==="winner"){ const o=["home","draw","away"]; st.winner.side=o[(o.indexOf(st.winner.side)+1)%3]; }
    else st[leg].side = st[leg].side==="over"?"under":"over";
  } else if (act==="step") st[leg].line = Math.max(0.5, st[leg].line + (+t.dataset.dir));
  renderSGP(todaySlate());
});
document.addEventListener("change", e => {
  if (e.target.classList.contains("sgp-odds")) {
    const mno=+e.target.dataset.mno; if (SGP[mno]){ SGP[mno]._touched=true; SGP[mno].odds=e.target.value; renderSGP(todaySlate()); }
  }
});

/* odds editing (event-delegated, recompute on commit) */
function recomputeActive() {
  if (ACTIVE === "today") renderToday();
  else if (ACTIVE === "bets") { renderBets(); if (browseOpen()) renderBrowse(); }
}
function commitOdds(el) {
  const v = parseFloat(el.value);
  setOdds(el.dataset.key, isFinite(v) ? v : 0);
  initStateLabel(); recomputeActive();
}
function initStateLabel() {
  const a = document.getElementById("odds-source"); if (!a) return;
  const os = (S.meta && S.meta.odds_source) || {};
  if (os.live) {
    a.textContent = `Live market odds — median of ${os.books} books (match 1X2 + champion). `
      + `Your book Draftea isn't covered, so use these as the fair-price reference and compare on your app. `
      + `Props / corners / final / SF still use model estimates.`;
  } else {
    const userN = Object.keys(userOdds()).length;
    a.textContent = userN
      ? `Using your odds on ${userN} selection${userN>1?"s":""} · sample odds elsewhere.`
      : "Showing illustrative bookmaker odds — enter your book's prices to find your real edges.";
  }
}
document.addEventListener("change", e => {
  if (e.target.classList.contains("odds-edit") || e.target.classList.contains("brow-odds")) commitOdds(e.target);
});
document.getElementById("singles-more").addEventListener("click", () => { showAllSingles = !showAllSingles; renderBets(); });
function setBankroll(v) {
  BANKROLL = Math.max(0, parseFloat(v) || 0);
  localStorage.setItem(LS.bank, BANKROLL); applyControls(); syncSettings(); recomputeActive();
}
function setKelly(v) {
  KELLY = parseFloat(v) || 0.25;
  localStorage.setItem(LS.kelly, KELLY); applyControls(); syncSettings(); recomputeActive();
}
["in-bankroll", "t-bankroll"].forEach(id => document.getElementById(id)?.addEventListener("change", e => setBankroll(e.target.value)));
["in-kelly", "t-kelly"].forEach(id => document.getElementById(id)?.addEventListener("change", e => setKelly(e.target.value)));
async function syncSettings() {
  if (!LIVE) return;
  try { await fetch("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({bankroll: BANKROLL, kelly_fraction: KELLY})}); } catch {}
}

/* ---------------- browse / edit all odds ---------------- */
const browseOpen = () => document.getElementById("value-browse").open;
const MK_LABEL = {champion:"World Cup winner", final:"Reach final", sf:"Reach semis"};
function renderBrowse() {
  const q = (document.getElementById("browse-search").value || "").toLowerCase();
  const groups = {};
  for (const m of S.markets) {
    if (q && !(m.label.toLowerCase().includes(q) || (m.selection||"").toLowerCase().includes(q))) continue;
    const g = m.kind === "outright" ? MK_LABEL[m.market] : m.kind === "prop" ? "Player props" : "Match result (1X2)";
    (groups[g] = groups[g] || []).push(m);
  }
  const order = ["Match result (1X2)","Player props","World Cup winner","Reach final","Reach semis"];
  document.getElementById("browse-list").innerHTML = order.filter(g => groups[g]).map(g => {
    const rows = groups[g].slice(0, 200).map(m => {
      const o = ODDS[m.key], e = o > 1 ? edge(m.model_p, o) : null;
      const ec = e == null ? "flat" : e > 0 ? "up" : "down";
      return `<div class="brow"><span class="ln">${esc(m.label)}</span>
        <span class="mp">${pct(m.model_p)}</span>
        <input class="brow-odds" inputmode="decimal" data-key="${esc(m.key)}" value="${o ?? ""}" placeholder="odds">
        <span class="be ${ec}">${e == null ? "—" : (e>0?"+":"")+(e*100).toFixed(0)+"%"}</span></div>`;
    }).join("");
    return `<div class="brow-grp">${g}</div>${rows}`;
  }).join("") || `<div class="empty">no markets match "${esc(q)}"</div>`;
}
document.getElementById("browse-search").addEventListener("input", () => { if (browseOpen()) renderBrowse(); });
document.getElementById("value-browse").addEventListener("toggle", e => { if (e.target.open) renderBrowse(); });
document.getElementById("browse-reset").addEventListener("click", () => {
  localStorage.removeItem(LS.odds); ODDS = Object.assign({}, S.sample_odds);
  initStateLabel(); renderBrowse(); renderBets();
});

/* ================= TITLE RACE tab ================= */
let titleMetric = "champion";
function strengthBreakdown(s) {
  if (!s) return "";
  const adj = (k, v) => `<div class="sb-chip"><span>${k}</span><b class="${v>0?"up":v<0?"down":""}">${v>0?"+":""}${v}</b></div>`;
  return `<div class="tdetail hidden">
    <div class="sb-row">
      <div class="sb-chip"><span>Elo</span><b>${Math.round(s.elo)}</b></div>
      ${adj("Market value", s.mv_adj)}${adj("Intl form", s.form_adj)}
      ${adj("Club form", s.club_form_adj ?? 0)}${adj("Injuries", s.injury_adj)}
    </div>
    <div class="sb-tot">Strength <b>${Math.round(s.strength)}</b> — higher is stronger.
      "Club form" = squad's current club xG + minutes (live). Tap to close.</div></div>`;
}
function renderTitle() {
  const rows = [...S.teams].sort((a,b) => b.probs[titleMetric] - a.probs[titleMetric]);
  const max = rows[0].probs[titleMetric] || 1;
  const strip = document.getElementById("movers-strip");
  if (titleMetric === "champion" && (S.movers||[]).length) {
    strip.innerHTML = S.movers.filter(m => Math.abs(m.delta) > 0.0005).slice(0,8).map(m => {
      const c = m.delta > 0 ? "up" : "down";
      return `<div class="mvr"><span class="nm">${flag(m.team)} ${esc(m.team)}</span>
        <span class="dl ${c}">${m.delta>0?"▲":"▼"} ${pp(m.delta)}</span></div>`;
    }).join("");
  } else strip.innerHTML = "";
  document.getElementById("title-list").innerHTML = rows.map((t,i) => {
    const p = t.probs[titleMetric], d = t.delta ? t.delta[titleMetric] : 0;
    const dc = d > 0.0005 ? "up" : d < -0.0005 ? "down" : "flat";
    const dt = dc === "flat" ? "·" : `${d>0?"▲":"▼"} ${pp(d).replace("+","")}`;
    return `<div class="titem">
      <div class="trow"><span class="rank">${i+1}</span><span class="flag">${flag(t.team)}</span>
      <div class="who"><div class="nm">${esc(t.team)}</div><div class="gp">GROUP ${t.group}</div>
        <div class="bar"><i style="width:${100*p/max}%"></i></div></div>
      <div class="pr"><div class="p">${pct(p)}</div><div class="o">${fair(p)}</div></div>
      <div class="dl ${dc}">${dt}</div></div>
      ${strengthBreakdown(t.strength)}</div>`;
  }).join("");
}
document.getElementById("title-metric").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#title-metric button").forEach(x => x.classList.toggle("active", x===b));
  titleMetric = b.dataset.m; renderTitle();
});
document.getElementById("title-list").addEventListener("click", e => {
  const item = e.target.closest(".titem"); if (!item) return;
  const d = item.querySelector(".tdetail"); if (d) d.classList.toggle("hidden");
});

/* ================= MATCHES tab ================= */
function renderMatches() {
  const up = document.getElementById("matches-upcoming").checked;
  let last = "";
  const ms = S.matches
    .filter(m => !up || m.home_score == null)
    .sort((a, b) => (a.kickoff_utc || "9999").localeCompare(b.kickoff_utc || "9999") || a.number - b.number);
  document.getElementById("matches-list").innerHTML = ms.map(m => {
    const day = m.kickoff_utc ? new Date(m.kickoff_utc).toDateString() : "TBD";
    const hdr = day !== last ? `<div class="mday">${day.toUpperCase()}</div>` : "";
    last = day;
    const played = m.home_score != null;
    const score = played
      ? `<span class="ft">${m.home_score}–${m.away_score}</span>${m.pen_home!=null?` <span class="tm">(p ${m.pen_home}–${m.pen_away})</span>`:""}`
      : `<span class="tm">${clockUTC(m.kickoff_utc)}</span>`;
    const fc = m.forecast;
    let viz = "";
    if (fc && !played) {
      const mx = Math.max(fc.home, fc.draw, fc.away);
      const seg = (v, c) => `<i class="${c}" style="width:${100*v}%"></i>`;
      viz = `<div class="mr-bar">${seg(fc.home,"h")}${seg(fc.draw,"d")}${seg(fc.away,"a")}</div>
        <div class="mr-odds">
          <span class="o ${fc.home===mx?"lead":""}"><b>${esc(code(m.home_label))}</b> ${pct(fc.home)}</span>
          <span class="o ${fc.draw===mx?"lead":""}">Draw ${pct(fc.draw)}</span>
          <span class="o ${fc.away===mx?"lead":""}"><b>${esc(code(m.away_label))}</b> ${pct(fc.away)}</span></div>`;
    }
    return hdr + `<div class="mrow ${played?"done":""}">
      <div class="mr-top"><span class="mr-tag">M${m.number} · ${esc(m.stage)}${m.group_letter?" "+m.group_letter:""}</span>
        <span class="mr-when">${score}</span></div>
      <div class="mr-teams">
        <span class="mr-team h"><span class="flag">${flag(m.home_label)}</span> ${esc(m.home_label)}</span>
        <span class="mr-vs">${played?"":"vs"}</span>
        <span class="mr-team a">${esc(m.away_label)} <span class="flag">${flag(m.away_label)}</span></span></div>
      ${viz}</div>`;
  }).join("") || `<div class="empty">nothing to show</div>`;
}
document.getElementById("matches-upcoming").addEventListener("change", renderMatches);

/* ================= GROUPS tab ================= */
function renderGroups() {
  const g = S.groups;
  document.getElementById("groups-grid").innerHTML = Object.keys(g).sort().map(k => {
    const rows = g[k].map((r,i) => `<tr class="${i<2?"qual":""}">
      <td class="t">${flag(r.team)} ${esc(r.team)}</td>
      <td>${r.pld}</td><td>${r.pts}</td><td>${r.gd>0?"+":""}${r.gd}</td>
      <td class="adv">${pct(r.r32)}</td></tr>`).join("");
    return `<div class="gcard"><h3>GROUP ${k}</h3><table class="gtbl">
      <tr><th>Team</th><th>P</th><th>Pts</th><th>GD</th><th>Adv</th></tr>${rows}</table></div>`;
  }).join("");
}

/* ================= nav + boot ================= */
const RENDER = {today: renderToday, bets: renderBets, title: renderTitle,
                matches: renderMatches, groups: renderGroups};
function show(tab) {
  ACTIVE = tab;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab===tab));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id==="tab-"+tab));
  RENDER[tab]();
  window.scrollTo({top:0, behavior:"instant"});
}
document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => show(b.dataset.tab)));

document.getElementById("btn-refresh").addEventListener("click", async e => {
  const b = e.currentTarget; b.textContent = "…"; b.disabled = true;
  const before = S && S.meta && S.meta.generated;
  try {
    if (LIVE) { try { await fetch("/api/refresh", {method:"POST"}); } catch {} }
    await loadSnapshot(true);                        // bypass cache
    ODDS = Object.assign({}, S.sample_odds, userOdds());
    setMeta(); show(ACTIVE);
    const fresh = LIVE || (S.meta && S.meta.generated !== before);
    b.textContent = fresh ? "✓" : "✓";
    b.title = fresh ? "Updated to the latest data" : "Already showing the latest published data";
  } catch { b.textContent = "!"; b.title = "Couldn't reach the data source"; }
  setTimeout(() => { b.textContent = "↻"; b.disabled = false; }, 1400);
});

function setMeta() {
  const m = S.meta;
  const when = m.run && m.run.ts ? new Date(m.run.ts).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}) : "—";
  document.getElementById("sub-meta").textContent =
    `${m.matches_played}/${m.matches_total} PLAYED · ${LIVE?"LIVE":"SNAPSHOT"} ${when}`;
}

(async function boot() {
  try {
    await loadSnapshot();
    initState(); setMeta();
    show("today");
  } catch (err) {
    document.querySelector("main").innerHTML =
      `<div class="empty">Couldn't load model data.<br><span class="micro">${esc(err.message||err)}</span></div>`;
    console.error(err);
  }
})();
