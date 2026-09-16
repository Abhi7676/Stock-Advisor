/**
 * app.js — Indian F&O AI Signal Advisor Frontend
 * Polls backend API, renders CALL/PUT signals, options chain,
 * PCR gauge, technical indicators, and signal history.
 */

const API = "";          // Same-origin (Flask serves this file)
const POLL_INTERVAL = 15; // Fast 15-second live refresh!
const RETRY_INTERVAL = 4; // Retry every 4s on cold-start/503

// ─── State ────────────────────────────────────────────────────
let countdown = POLL_INTERVAL;
let countdownTimer = null;
let pollTimer = null;
let _dataLoaded = { NIFTY: false, BANKNIFTY: false }; // track if we got real data
let _retryTimer = null;

// ─── Theme Toggle ─────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.body.classList.toggle("light");
  localStorage.setItem("fao_theme", isLight ? "light" : "dark");
  _applyThemeUI(isLight);
}

function _applyThemeUI(isLight) {
  const icon  = document.getElementById("themeIcon");
  const label = document.getElementById("themeLabel");
  if (isLight) {
    if (icon)  icon.textContent  = "☀️";
    if (label) label.textContent = "Dark Mode";
  } else {
    if (icon)  icon.textContent  = "🌙";
    if (label) label.textContent = "Light Mode";
  }
}

// ─── Init ──────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  // Restore saved theme preference
  const savedTheme = localStorage.getItem("fao_theme");
  if (savedTheme === "light") {
    document.body.classList.add("light");
    _applyThemeUI(true);
  }

  checkMarketStatus();
  checkGeminiStatus();
  fetchMarketTicker();
  fetchAllData();
  startCountdown();

  document.getElementById("btnRefreshAll").addEventListener("click", async () => {
    const btn = document.getElementById("btnRefreshAll");
    btn.disabled = true;
    btn.textContent = "⏳ Refreshing...";
    resetCountdown();
    await fetchAllData(true);
    btn.disabled = false;
    btn.innerHTML = "🔄 Refresh Signals";
  });

  document.getElementById("btnClearHistory").addEventListener("click", () => {
    document.getElementById("historyList").innerHTML =
      `<div style="text-align:center;padding:16px;color:var(--text-3);">History cleared.</div>`;
  });
});

// ─── Market Status ────────────────────────────────────────────
function checkMarketStatus() {
  const now = new Date();
  const ist = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const h = ist.getHours(), m = ist.getMinutes();
  const mins = h * 60 + m;
  const day = ist.getDay(); // 0=Sun, 6=Sat

  const isWeekday = day >= 1 && day <= 5;
  const isOpen = isWeekday && mins >= (9 * 60 + 15) && mins < (15 * 60 + 30);

  const pill = document.getElementById("marketStatusPill");
  const dot = document.getElementById("marketPulseDot");
  const txt = document.getElementById("marketStatusText");

  if (isOpen) {
    pill.className = "status-pill market-open";
    dot.className = "pulse green";
    txt.textContent = "Market Open";
  } else {
    pill.className = "status-pill market-closed";
    dot.className = "pulse red";
    txt.textContent = isWeekday ? "Market Closed" : "Weekend";
  }
}

// ─── Gemini Status ────────────────────────────────────────────
async function checkGeminiStatus() {
  const pill = document.getElementById("geminiPill");
  const dot = document.getElementById("geminiDot");
  const txt = document.getElementById("geminiText");
  try {
    const r = await fetch(`${API}/api/gemini-status`);
    const d = await r.json();
    if (d.running && d.model) {
      pill.className = "status-pill gemini-on";
      dot.className = "pulse indigo";
      txt.textContent = `✨ Gemini: ${d.model}`;
    } else if (d.api_key_set === false) {
      pill.className = "status-pill gemini-off";
      dot.className = "pulse amber";
      txt.textContent = "⚠ Gemini: No API Key (rule-based)";
    } else {
      pill.className = "status-pill gemini-off";
      dot.className = "pulse amber";
      txt.textContent = "⚠ Gemini: Unavailable (rule-based)";
    }
  } catch {
    pill.className = "status-pill gemini-off";
    dot.className = "pulse amber";
    txt.textContent = "⚠ Gemini: Offline (rule-based)";
  }
}

async function fetchMarketTicker() {
  try {
    const r = await fetch(`${API}/api/ticker`);
    if (!r.ok) return;
    const json = await r.json();
    if (json.data && json.data.length > 0) {
      renderTicker(json.data);
    }
  } catch (e) {
    console.warn("Ticker fetch failed:", e);
  }
}

function renderTicker(items) {
  const container = document.getElementById("tickerContainer");
  if (!container) return;
  // Duplicate list x2 so seamless infinite CSS scrolling never leaves blank space
  const list = [...items, ...items];
  container.innerHTML = list.map(item => {
    const pos = (item.change_pct || 0) >= 0;
    const sign = pos ? "+" : "";
    return `<div class="ticker-item">
      <span class="ticker-name">${item.name}</span>
      <span class="ticker-price">₹${fmt(item.ltp)}</span>
      <span class="ticker-change ${pos ? 'pos' : 'neg'}">${sign}${item.change_pct.toFixed(2)}%</span>
    </div>`;
  }).join("");
}

// ─── Main Data Fetch ──────────────────────────────────────────
async function fetchAllData(force = false) {
  checkMarketStatus();
  checkGeminiStatus();
  fetchMarketTicker();

  await Promise.allSettled([
    fetchSignal("NIFTY", force),
    fetchSignal("BANKNIFTY", force),
  ]);

  await Promise.allSettled([
    fetchOptionsChain("NIFTY"),
    fetchOptionsChain("BANKNIFTY"),
  ]);

  fetchSignalHistory();

  // If data still not loaded (cold start), schedule a quick retry
  if (!_dataLoaded.NIFTY || !_dataLoaded.BANKNIFTY) {
    clearTimeout(_retryTimer);
    _retryTimer = setTimeout(() => fetchAllData(), RETRY_INTERVAL * 1000);
  }
}

// ─── Fetch AI Signal for one index ───────────────────────────
async function fetchSignal(sym, force = false) {
  const url = `${API}/api/signal/${sym}${force ? "?refresh=true" : ""}`;

  // Show warm-up message if not yet loaded
  if (!_dataLoaded[sym]) {
    const prefix = sym === "NIFTY" ? "nifty" : "bankNifty";
    const subEl = document.getElementById(`${prefix}StrikeText`);
    if (subEl && subEl.textContent === "Fetching signal...")
      subEl.textContent = "⏳ Server warming up... (~30s)";
  }

  try {
    const r = await fetch(url);
    if (r.status === 503) return; // Not ready yet, retry loop will handle it
    if (!r.ok) return;
    const json = await r.json();
    if (json.status === "ok") {
      _dataLoaded[sym] = true; // Mark as loaded
      clearTimeout(_retryTimer);  // Cancel retry once data arrives
    }
    renderSignal(sym, json);
    renderIndicators(sym, json.analysis_summary);
  } catch (e) {
    console.warn(`Signal fetch failed for ${sym}:`, e);
  }
}

// ─── Render Signal Box ────────────────────────────────────────
function renderSignal(sym, json) {
  const s = json.signal;
  const a = json.analysis_summary || {};
  const prefix = sym === "NIFTY" ? "nifty" : "bankNifty";
  const bn = sym === "NIFTY" ? "nifty" : "bn";

  // Box class
  const box = document.getElementById(`${prefix}SignalBox`);
  box.className = "signal-box " + signalBoxClass(s.signal);

  // LTP & change
  document.getElementById(`${prefix}LTP`).textContent = `₹${fmt(a.ltp)}`;
  const chgEl = document.getElementById(`${prefix}Change`);
  const chgPct = a.change_pct || 0;
  chgEl.textContent = `${chgPct >= 0 ? "+" : ""}${chgPct.toFixed(2)}%`;
  chgEl.className = "signal-change " + (chgPct >= 0 ? "pos" : "neg");

  // Badge
  const badge = document.getElementById(`${prefix}Badge`);
  const { cls, emoji, label, subLabel } = signalMeta(s, sym);
  badge.className = `signal-badge ${cls}`;
  document.getElementById(`${prefix}Emoji`).textContent = emoji;
  document.getElementById(`${prefix}SignalText`).textContent = label;
  document.getElementById(`${prefix}StrikeText`).textContent = subLabel;

  // Confidence bar
  const conf = s.confidence || 0;
  const confBar = document.getElementById(`${bn}ConfBar`) || document.getElementById(`${prefix}ConfBar`) || document.getElementById("niftyConfBar");
  const confPct = document.getElementById(`${bn}ConfPct`) || document.getElementById(`${prefix}ConfPct`) || document.getElementById("niftyConfPct");

  // Use correct ids
  const confBarId = sym === "NIFTY" ? "niftyConfBar" : "bnConfBar";
  const confPctId = sym === "NIFTY" ? "niftyConfPct" : "bnConfPct";
  const confBarEl = document.getElementById(confBarId);
  const confPctEl = document.getElementById(confPctId);
  if (confBarEl) {
    confBarEl.style.width = conf + "%";
    confBarEl.className = `confidence-fill ${cls}-fill`;
  }
  if (confPctEl) {
    confPctEl.textContent = conf + "%";
    confPctEl.style.color = cls === "call" ? "var(--call)" : cls === "put" ? "var(--put)" : "var(--wait)";
  }

  // Trade detail cells
  const isCall = s.option_type === "CE";
  const isPut  = s.option_type === "PE";
  const valCls = isCall ? "call-val" : isPut ? "put-val" : "";

  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}StrikeVal`, s.strike ? `${s.strike} ${s.option_type || ""}` : "—", valCls);
  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}Expiry`, s.nearest_expiry || "—");
  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}Entry`,  s.entry_premium ? `₹${s.entry_premium}` : "—", valCls);
  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}Target`, s.target_premium ? `₹${s.target_premium}` : "—", "call-val");
  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}SL`,     s.stop_loss_premium ? `₹${s.stop_loss_premium}` : "—", "put-val");
  setVal(`${sym === "NIFTY" ? "nifty" : "bn"}Lots`,   s.lots_recommended ? `${s.lots_recommended} lot(s)` : "—");

  // Reasoning
  document.getElementById(`${sym === "NIFTY" ? "nifty" : "bn"}Reasoning`).textContent =
    s.reasoning || "—";

  // Trade Execution & Holding Guide Tab
  const pfx = sym === "NIFTY" ? "nifty" : "bn";
  const actionEl = document.getElementById(`${pfx}ActionSummary`);
  const holdEl   = document.getElementById(`${pfx}HoldTime`);
  const exitEl   = document.getElementById(`${pfx}ExitRule`);

  if (actionEl) {
    actionEl.textContent = s.action_summary || "Wait for technical setup...";
    actionEl.className = "exec-val " + (isCall ? "pos" : isPut ? "neg" : "");
  }
  if (holdEl)   holdEl.textContent   = s.holding_time || "0 Mins — Stay on Sidelines";
  if (exitEl)   exitEl.textContent   = s.exit_rule || "Preserve capital";

  // Bias Score
  const bias = s.bias_score || a.bias_score || 0;
  const biasScoreId = sym === "NIFTY" ? "niftyBiasScore" : "bnBiasScore";
  const biasMarkerId = sym === "NIFTY" ? "niftyBiasMarker" : "bnBiasMarker";
  document.getElementById(biasScoreId).textContent = `${bias > 0 ? "+" : ""}${bias}`;
  const pct = ((bias + 10) / 20) * 100; // map -10..+10 → 0..100%
  document.getElementById(biasMarkerId).style.left = `${Math.max(5, Math.min(95, pct))}%`;
}

function signalBoxClass(sig) {
  if (sig === "BUY_CALL") return "call-box";
  if (sig === "BUY_PUT")  return "put-box";
  return "wait-box";
}

function signalMeta(s, sym) {
  if (s.source === "market_closed") {
    return {
      cls: "wait", emoji: "🌙", label: "MARKET CLOSED",
      subLabel: "NSE opens at 9:15 AM IST (Mon–Fri)"
    };
  }
  if (s.signal === "BUY_CALL") return {
    cls: "call", emoji: "📈", label: "BUY CALL ✅",
    subLabel: `${s.strike} CE | Lot: ${s.lots_recommended || "?"} | Cost: ₹${s.estimated_cost_inr || "?"}`
  };
  if (s.signal === "BUY_PUT") return {
    cls: "put", emoji: "📉", label: "BUY PUT 🔴",
    subLabel: `${s.strike} PE | Lot: ${s.lots_recommended || "?"} | Cost: ₹${s.estimated_cost_inr || "?"}`
  };
  return {
    cls: "wait", emoji: "⏸️", label: "WAIT / AVOID",
    subLabel: "No clear signal — stay on sidelines"
  };
}

// ─── Render Technical Indicators Panel ───────────────────────
function renderIndicators(sym, analysis) {
  if (!analysis || !analysis.ta) return;
  const ta = analysis.ta;
  const id = sym === "NIFTY" ? "niftyIndicators" : "bnIndicators";
  const pcrId   = sym === "NIFTY" ? "niftyPCR"       : "bnPCR";
  const pcrSigId= sym === "NIFTY" ? "niftyPCRSignal" : "bnPCRSignal";
  const pcrFillId=sym === "NIFTY" ? "niftyPCRFill"   : "bnPCRFill";
  const mpId    = sym === "NIFTY" ? "niftyMaxPain"   : "bnMaxPain";
  const oirId   = sym === "NIFTY" ? "niftyOIRange"   : "bnOIRange";
  const srcId   = sym === "NIFTY" ? "niftyDataSrc"   : "bnDataSrc";

  // PCR (needs full signal data — will be updated by chain fetch)
  if (analysis.pcr !== undefined) {
    const pcr = analysis.pcr;
    document.getElementById(pcrId).textContent = pcr.toFixed(2);
    const pcrSig = analysis.pcr_signal || "";
    document.getElementById(pcrSigId).textContent = pcrSig.replace(/_/g, " ");
    const pct = Math.min(100, (pcr / 2) * 100);
    const fill = document.getElementById(pcrFillId);
    fill.style.width = pct + "%";
    fill.style.background = pcr >= 1.2 ? "var(--call)" : pcr <= 0.8 ? "var(--put)" : "var(--wait)";
  }

  // Indicator grid
  const indicators = [
    {
      name: "RSI (14)",
      val: ta.rsi?.toFixed(1) || "—",
      sig: ta.rsi_signal || "—",
      bull: ["OVERSOLD","BULLISH_MOMENTUM"].includes(ta.rsi_signal),
      bear: ["OVERBOUGHT","BEARISH_MOMENTUM"].includes(ta.rsi_signal),
    },
    {
      name: "MACD",
      val: ta.macd?.toFixed(2) || "—",
      sig: ta.macd_bias || "—",
      bull: ta.macd_bias === "BULLISH",
      bear: ta.macd_bias === "BEARISH",
    },
    {
      name: "EMA Cross",
      val: ta.ema_crossover || "—",
      sig: ta.ema_crossover === "GOLDEN" ? "Bullish" : "Bearish",
      bull: ta.ema_crossover === "GOLDEN",
      bear: ta.ema_crossover === "DEATH",
    },
    {
      name: "Supertrend",
      val: ta.supertrend_trend || "—",
      sig: ta.supertrend_signal || "—",
      bull: ta.supertrend_signal === "BUY",
      bear: ta.supertrend_signal === "SELL",
    },
  ];

  const container = document.getElementById(id);
  container.innerHTML = indicators.map(ind => `
    <div class="ind-box">
      <div class="ind-name">${ind.name}</div>
      <div class="ind-value">${ind.val}</div>
      <div class="ind-sig ${ind.bull ? 'bull' : ind.bear ? 'bear' : 'neut'}">${ind.sig.replace(/_/g," ")}</div>
    </div>
  `).join("");
}

// ─── Fetch & Render Options Chain ────────────────────────────
async function fetchOptionsChain(sym) {
  const url = `${API}/api/options-chain/${sym}`;
  try {
    const r = await fetch(url);
    if (!r.ok) return;
    const json = await r.json();
    renderChain(sym, json);
  } catch (e) {
    console.warn(`Chain fetch failed for ${sym}:`, e);
  }
}

function renderChain(sym, data) {
  const bodyId = sym === "NIFTY" ? "niftyChainBody" : "bnChainBody";
  const expId  = sym === "NIFTY" ? "niftyExpLabel"  : "bnExpLabel";
  const mpId   = sym === "NIFTY" ? "niftyMaxPain"   : "bnMaxPain";
  const oirId  = sym === "NIFTY" ? "niftyOIRange"   : "bnOIRange";
  const pcrId  = sym === "NIFTY" ? "niftyPCR"       : "bnPCR";
  const pcrSigId=sym === "NIFTY" ? "niftyPCRSignal" : "bnPCRSignal";
  const pcrFillId=sym==="NIFTY"  ? "niftyPCRFill"   : "bnPCRFill";
  const srcId  = sym === "NIFTY" ? "niftyDataSrc"   : "bnDataSrc";

  document.getElementById(expId).textContent = `Expiry: ${data.nearest_expiry || "—"}`;
  document.getElementById(mpId).textContent = data.max_pain ? `${data.max_pain}` : "—";
  const oi = data.oi_analysis || {};
  document.getElementById(oirId).textContent = oi.oi_range || "—";
  document.getElementById(srcId).textContent = `Source: ${data.data_source || "—"} • ${data.timestamp || ""}`;

  // PCR
  if (data.pcr) {
    document.getElementById(pcrId).textContent = data.pcr.toFixed(2);
    document.getElementById(pcrSigId).textContent = (data.pcr_signal || "").replace(/_/g," ");
    const pct = Math.min(100, (data.pcr / 2) * 100);
    const fill = document.getElementById(pcrFillId);
    fill.style.width = pct + "%";
    fill.style.background = data.pcr >= 1.2 ? "var(--call)" : data.pcr <= 0.8 ? "var(--put)" : "var(--wait)";
  }

  const chain = data.chain || [];
  const tbody = document.getElementById(bodyId);
  if (!chain.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-3);padding:20px;">No chain data</td></tr>`;
    return;
  }

  tbody.innerHTML = chain.map(row => {
    const isATM = row.is_atm;
    const atmClass = isATM ? "atm-row" : "";
    const callOI = fmtOI(row.call_oi);
    const putOI  = fmtOI(row.put_oi);
    const callBar = `<div class="oi-bar-wrap" style="justify-content:flex-end"><span style="font-size:.72rem;color:rgba(255,255,255,.5)">${callOI}</span><div class="oi-bar call-bar" style="width:${row.call_oi_pct || 0}px;max-width:50px"></div></div>`;
    const putBar  = `<div class="oi-bar-wrap"><div class="oi-bar put-bar" style="width:${row.put_oi_pct || 0}px;max-width:50px"></div><span style="font-size:.72rem;color:rgba(255,255,255,.5)">${putOI}</span></div>`;
    const chgCallOI = row.call_chg_oi || 0;
    const chgPutOI  = row.put_chg_oi  || 0;

    return `<tr class="${atmClass}">
      <td class="call-td">${callBar}</td>
      <td class="call-ltp-td">${row.call_ltp ? "₹" + row.call_ltp : "—"}</td>
      <td class="call-td" style="color:var(--text-3)">${row.call_iv ? row.call_iv + "%" : "—"}</td>
      <td class="strike-cell" style="font-weight:${isATM?700:500};color:${isATM?"#fff":"var(--text-2)"};text-align:center">
        ${row.strike}${isATM ? " <span style='font-size:.6rem;color:var(--accent)'>ATM</span>" : ""}
      </td>
      <td class="put-td" style="color:var(--text-3)">${row.put_iv ? row.put_iv + "%" : "—"}</td>
      <td class="put-ltp-td">${row.put_ltp ? "₹" + row.put_ltp : "—"}</td>
      <td class="put-td">${putBar}</td>
    </tr>`;
  }).join("");
}

// ─── Signal History ───────────────────────────────────────────
async function fetchSignalHistory() {
  try {
    const r = await fetch(`${API}/api/signal-history?limit=15`);
    const json = await r.json();
    renderHistory(json.data || []);
  } catch (e) {
    console.warn("History fetch failed:", e);
  }
}

function renderHistory(rows) {
  const container = document.getElementById("historyList");

  // Backend already dedupes to 1 per hour and filters BUY signals only
  const trades = (rows || []).filter(r => r.signal === "BUY_CALL" || r.signal === "BUY_PUT");

  if (!trades.length) {
    container.innerHTML = `<div style="text-align:center;padding:20px;color:var(--text-3);font-size:0.82rem">
      📊 <strong>No Trades Executed Yet</strong><br>
      <span style="font-size:0.75rem;color:var(--text-3)">Signal History shows <strong>BUY CALL</strong> and <strong>BUY PUT</strong> trades — one per 30 min — closed at <strong>13% profit cap</strong>, SL hit, or after 30 min hold.</span>
    </div>`;
    return;
  }

  container.innerHTML = trades.map(row => {
    const sig   = row.signal || "BUY_CALL";
    const cls   = sig === "BUY_CALL" ? "call" : "put";
    const label = sig === "BUY_CALL" ? "📈 BUY CALL" : "📉 BUY PUT";

    // Date + time of signal entry
    const entryDt = row.created_at ? row.created_at.replace("T", " ").slice(0, 16) : "—";

    const entryP  = row.entry_premium != null ? (+row.entry_premium).toFixed(2) : "—";
    const exitP   = row.exit_premium  != null ? (+row.exit_premium).toFixed(2)  : entryP;
    const pnlPct  = row.pnl_pct  != null ? +row.pnl_pct  : 0;
    const pnlAmt  = row.pnl_amount != null ? +row.pnl_amount : 0;

    // Outcome badge from backend
    const outcome      = row.outcome      || (row.status?.includes("ACTIVE") ? "⏳ ACTIVE" : `${pnlPct >= 0 ? "✅" : "❌"} ${pnlPct >= 0 ? "PROFIT" : "LOSS"} ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%`);
    const outcomeClass = row.outcome_class || (pnlPct > 0 ? "profit" : pnlPct < 0 ? "loss" : "active");

    const outcomeColors = {
      profit:  { bg: "rgba(16,185,129,0.15)", border: "rgba(16,185,129,0.4)", text: "#10b981" },
      loss:    { bg: "rgba(239,68,68,0.15)",  border: "rgba(239,68,68,0.4)",  text: "#ef4444" },
      active:  { bg: "rgba(56,189,248,0.10)", border: "rgba(56,189,248,0.3)", text: "#38bdf8" },
      neutral: { bg: "rgba(148,163,184,0.12)",border: "rgba(148,163,184,0.3)",text: "#94a3b8" },
    };
    const oc = outcomeColors[outcomeClass] || outcomeColors.neutral;

    const statusStr = row.status || "ACTIVE";

    return `<div class="history-item" style="display:flex;align-items:flex-start;gap:12px;padding:10px 14px;border-bottom:1px solid rgba(255,255,255,0.05)">
      <!-- Signal badge -->
      <span class="history-badge ${cls}" style="flex-shrink:0;margin-top:2px">${label}</span>

      <!-- Trade details -->
      <div style="flex:1;min-width:0">
        <div style="font-weight:700;font-size:.85rem;color:var(--text-1)">${row.symbol} &nbsp;${row.strike} ${row.option_type || ""}</div>
        <div style="font-size:.73rem;color:var(--text-3);margin-top:2px">
          📅 ${entryDt} &nbsp;|&nbsp; Entry ₹${entryP} &nbsp;→&nbsp; Exit ₹${exitP}
          ${pnlAmt !== 0 ? `&nbsp;|&nbsp; <span style="color:${oc.text};font-weight:700">${pnlAmt >= 0 ? "+" : ""}₹${pnlAmt.toFixed(0)} PnL</span>` : ""}
        </div>
        <div style="font-size:0.71rem;color:var(--text-3);margin-top:2px;opacity:0.7">${statusStr}</div>
      </div>

      <!-- 1-hour hold outcome pill -->
      <div style="flex-shrink:0;text-align:right">
        <div style="background:${oc.bg};border:1px solid ${oc.border};color:${oc.text};
                    padding:5px 12px;border-radius:8px;font-size:0.78rem;font-weight:700;white-space:nowrap">
          ${outcome}
        </div>
        <div style="font-size:0.67rem;color:var(--text-3);margin-top:3px;opacity:0.7">30min result (% P&amp;L)</div>
      </div>
    </div>`;
  }).join("");
}

// ─── Auto-Refresh Countdown ───────────────────────────────────
function startCountdown() {
  clearInterval(countdownTimer);
  countdown = POLL_INTERVAL;

  countdownTimer = setInterval(() => {
    countdown--;
    const el = document.getElementById("countdownText");
    if (el) el.textContent = countdown + "s";
    if (countdown <= 0) {
      countdown = POLL_INTERVAL;
      fetchAllData();
    }
  }, 1000);
}

function resetCountdown() {
  countdown = POLL_INTERVAL;
  const el = document.getElementById("countdownText");
  if (el) el.textContent = countdown + "s";
}

// ─── Helpers ──────────────────────────────────────────────────
function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function fmtOI(n) {
  if (!n) return "—";
  if (n >= 1e7) return (n / 1e7).toFixed(1) + "Cr";
  if (n >= 1e5) return (n / 1e5).toFixed(1) + "L";
  if (n >= 1000) return (n / 1000).toFixed(0) + "K";
  return n.toString();
}

function setVal(id, val, cls = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className = "trade-stat-val" + (cls ? " " + cls : "");
}
