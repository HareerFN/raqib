/* Raqib control room.
   Live:   served by demo/server.py; the system and the agent run for real.
   Replay: GitHub Pages; recorded real runs are played back on the same screen. */
(function () {
  "use strict";
  const { useState, useEffect, useMemo, useRef } = React;
  const html = htm.bind(React.createElement);

  // ---------------------------------------------------------------- copy
  const RULES = {
    I1_payment_has_order: "Paid orders confirmed", I2_no_orphaned_orders: "Orders picked up",
    I3_ledger_integrity: "Ledger adds up", I4_workflow_progress: "Approvals return",
    I5_single_payment: "Charged once", I6_retry_budget: "Retries in budget", I7_payments_succeed: "Payments succeed",
  };
  const MECH = {
    payment_swallow: "Payment confirmations dropped", early_ack: "Messages lost under load",
    invoice_swap: "Amount and VAT swapped", callback_lost: "Supplier approvals stall",
    agent_retry_loop: "Retry limit ignored", provider_down: "Payment provider outage",
    conn_leak: "Connection leak", invoice_format_change: "New supplier invoice template",
    duplicate_charge: "Customers charged twice", capacity_config: "Worker capacity set to 1",
    clock_skew: "Clock running behind", disk_fill: "Disk quota filled", "config change": "Config change",
  };
  const ORIGIN = { deploy: "with a deploy", transient: "stuck process", external: "outside the system", config: "config change" };
  const SOURCES = ["database", "metrics", "logs", "deploys"];
  const SPEEDS = [1, 5, 10, 30];

  // schematic layout (viewBox 0 0 960 500); wires run orthogonally
  const P = { "order-service": [90, 170], "queue": [270, 170], "worker": [470, 170], "invoice-agent": [470, 55],
    "payment-service": [670, 170], "payment-provider": [870, 170], "workflow": [670, 300], "supplier-api": [870, 300],
    "sync-job": [270, 300], "database": [470, 430] };
  const FLOW = ["order-service", "queue", "worker", "payment-service", "workflow"];
  const W = 150, H = 46;

  // ---------------------------------------------------------------- helpers
  const getJSON = (u, o) => fetch(u, o).then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(new Error(j.detail || r.status))));
  const post = (u, body) => getJSON(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const viol = s => s ? Object.keys(s.inv).filter(k => s.inv[k] > 0) : [];
  const mmss = s => `${s < 0 ? "−" : ""}${Math.floor(Math.abs(s) / 60)}:${String(Math.floor(Math.abs(s)) % 60).padStart(2, "0")}`;
  const within = (samples, from, to) => samples.filter(s => s.t > from && s.t <= to);
  const sumLogs = ss => ss.reduce((a, s) => { for (const c of Object.values(s.logs)) { a[0] += c[0]; a[1] += c[1]; a[2] += c[2]; } return a; }, [0, 0, 0]);
  const pick = a => a[Math.floor(Math.random() * a.length)];

  // ---------------------------------------------------------------- feed: group the agent's timeline
  function blocks(events) {
    const out = [];
    let pend = { asks: [], rejected: [] };
    let round = null;
    const push = b => { out.push(b); return b; };
    for (const e of events) {
      if (e.event === "incident") { round = null; push({ k: "detect", t: e.t, signals: e.signals }); }
      else if (e.event === "investigated") pend.asks.push(...e.requests);
      else if (e.event === "revision") pend.rejected.push(...e.rejected);
      else if (e.event === "hypotheses") { round = push({ k: "round", t: e.t, n: e.round, verdicts: e.verdicts, evidence: e.evidence || {}, ...pend, acts: [] }); pend = { asks: [], rejected: [] }; }
      else if (["action", "refused", "approval_denied"].includes(e.event)) {
        if (round) round.acts.push(e);
        else { const last = out[out.length - 1]; (last && last.k === "watch" ? last : push({ k: "watch", t: e.t, acts: [] })).acts.push(e); }
      } else if (["recurring_damage", "reopened", "refuted", "ruled_out"].includes(e.event)) {
        round = null; const last = out[out.length - 1];
        (last && last.k === "exp" ? last : push({ k: "exp", t: e.t, items: [] })).items.push(e);
      } else if (["stable", "escalated", "report"].includes(e.event)) {
        round = null; const last = out[out.length - 1];
        (last && last.k === "end" ? last : push({ k: "end", t: e.t, items: [] })).items.push(e);
      }
    }
    if (pend.asks.length || pend.rejected.length) out.push({ k: "thinking", t: events.length ? events[events.length - 1].t : 0, ...pend });
    return out;
  }

  // ---------------------------------------------------------------- the plant
  function wire(a, b) {
    const [x1, y1] = P[a], [x2, y2] = P[b];
    if (x1 === x2 || y1 === y2) return `M${x1},${y1} L${x2},${y2}`;
    return `M${x1},${y1} L${x1},${y2} L${x2},${y2}`;
  }

  function Plant({ info, samples, t, roles, rate }) {
    const last = samples[samples.length - 1];
    const recent = within(samples, t - 60, t);
    const err = svc => recent.reduce((a, s) => a + ((s.logs[svc] || [0, 0, 0])[2]), 0);
    const affected = new Set();
    const invSvc = Object.fromEntries(info.invariants.map(r => [r.id, r.services]));
    for (const id of viol(last)) for (const s of invSvc[id] || []) affected.add(s);
    for (const s of Object.keys(info.topology)) if (err(s) > 0) affected.add(s);
    const wires = [];
    for (const [svc, spec] of Object.entries(info.topology)) for (const d of spec.depends_on || []) if (P[svc] && P[d]) wires.push([svc, d]);
    const flow = "M" + FLOW.map(s => P[s].join(",")).join(" L");
    const n = Math.max(0, Math.min(7, Math.round(rate / 20)));
    const slow = viol(last).length > 0;
    return html`<div className="map">
      <div className="head"><span><b>Services</b> · dependency graph from topology.yaml</span><span className="mono">${Math.round(rate)} orders/min</span></div>
      <svg viewBox="0 0 960 500" role="img" aria-label="Service dependency map">
        ${wires.map(([a, b], i) => html`<path key=${i} className="wire" d=${wire(a, b)} />`)}
        <path className="wire flow" d=${flow} />
        ${Array.from({ length: n }, (_, i) => html`<circle key=${i} r="4" className=${"pulse" + (slow ? " slow" : "")}>
          <animateMotion dur=${slow ? "10s" : "5s"} begin=${`-${(i * (slow ? 10 : 5) / Math.max(n, 1)).toFixed(2)}s`} repeatCount="indefinite" path=${flow} /></circle>`)}
        ${Object.keys(P).filter(s => info.topology[s]).map(svc => {
          const [x, y] = P[svc]; const ext = info.topology[svc].external;
          const role = roles[svc] || (affected.has(svc) ? "affected" : "ok");
          const e = err(svc);
          const meta = ext ? "external" : role === "cause" ? "root cause" : role === "suspect" ? "suspect" : role === "fixed" ? "acted on" : e ? `${e} errors/min` : "ok";
          return html`<g key=${svc} className=${`node ${role}${ext ? " external" : ""}`} transform=${`translate(${x - W / 2},${y - H / 2})`}>
            <rect className="box" width=${W} height=${H} rx="2" /><rect className="stripe" width="4" height=${H} />
            <text className="name" x="14" y="20">${svc}</text><text className="meta" x="14" y="36">${meta}</text></g>`;
        })}
      </svg>
      <div className="legend"><span style=${{ "--c": "#19C3A6" }}>healthy</span><span style=${{ "--c": "#F05D4F" }}>affected</span>
        <span style=${{ "--c": "#F2A93B" }}>under suspicion</span><span style=${{ "--c": "#6B7A90" }}>external</span></div>
    </div>`;
  }

  function RuleStrip({ info, sample }) {
    return html`<div>
      <div className="head"><span><b>Business rules</b> · invariants.yaml</span><span>${viol(sample).length ? `${viol(sample).length} violated` : "all holding"}</span></div>
      <div className="rules">${info.invariants.map(r => { const v = sample ? sample.inv[r.id] : 0;
        return html`<div key=${r.id} className=${"rule" + (v > 0 ? " red" : "")} title=${r.description}>
          <span className="id"><span>${r.id.split("_")[0]}</span><span className="num">${v > 0 ? v : ""}</span></span>
          <span className="nm">${RULES[r.id] || r.description}</span></div>`; })}</div></div>`;
  }

  function Spark({ values }) {
    if (values.length < 2) return html`<svg viewBox="0 0 100 26"></svg>`;
    const max = Math.max(...values, 1);
    const pts = values.map((v, i) => [100 * i / (values.length - 1), 24 - 22 * v / max]);
    const d = pts.map(p => p.map(n => n.toFixed(1)).join(",")).join(" ");
    return html`<svg viewBox="0 0 100 26" preserveAspectRatio="none"><polygon className="spark-area" points=${`0,26 ${d} 100,26`} /><polyline className="spark" points=${d} /></svg>`;
  }

  function Readouts({ samples }) {
    const s = samples.slice(-41);
    const rate = k => s.slice(1).map((x, i) => Math.max(0, (x[k] - s[i][k]) * 60 / Math.max(1, x.t - s[i].t)));
    const orders = rate("orders"), done = rate("done");
    const last = s[s.length - 1] || {};
    const lastDone = done.length ? done[done.length - 1] : 0;
    const items = [
      ["Orders / min", orders, orders.length ? orders[orders.length - 1] : 0, false],
      ["Fulfilled / min", done, lastDone, done.length > 12 && lastDone < 0.5 * [...done.slice(0, 8)].sort((a, b) => a - b)[4]],
      ["Queue depth", s.map(x => x.queue), last.queue || 0, (last.queue || 0) > 50],
      ["DB connections", s.map(x => x.pool), last.pool || 0, (last.pool || 0) >= 40],
    ];
    return html`<div className="readouts">${items.map(([k, vals, v, warn]) => html`<div key=${k} className=${"ro" + (warn ? " warn" : "")}>
      <span className="k">${k}</span><span className="v">${Math.round(v)}</span><${Spark} values=${vals} /></div>`)}</div>`;
  }

  function Logs({ samples, t, onset }) {
    const since = onset != null ? onset : t - 60;
    const [i, w, e] = sumLogs(within(samples, since, t));
    const tail = samples.slice(-6).flatMap(s => s.tail || []).slice(-7);
    const quiet = onset != null && viol(samples[samples.length - 1]).length > 0 && e === 0;
    return html`<div>
      <div className="head"><span><b>Logs</b> · ${onset != null ? "since the fault began" : "last 60 s"}</span><span className="mono">tail</span></div>
      <div className="logs">
        <div className="levels"><span className="lv"><span>INFO</span><b>${i}</b></span><span className="lv"><span>WARN</span><b>${w}</b></span>
          <span className=${"lv err" + (e === 0 ? " zero" : "")}><span>ERROR</span><b>${e}</b></span></div>
        <div className="tail">${tail.length ? tail.map((l, k) => html`<div key=${k} className="ln">${l.ts} <span className=${"lvl-" + l.level}>${l.level.padEnd(5)}</span> ${l.service}: ${l.event}</div>`)
          : html`<div className="ln">waiting for log lines…</div>`}</div>
        ${quiet ? html`<div className="quiet">Rules are failing, yet there is not a single ERROR line. An alert on logs would stay silent.</div>` : null}
      </div></div>`;
  }

  // ---------------------------------------------------------------- the console
  const hard = v => (v.problems || []).filter(p => !/does not exist|is not about|another service/.test(p));
  function checks(v) {
    const has = s => (v.problems || []).some(p => p.toLowerCase().includes(s));
    const dep = ["bad_deploy", "config_change"].includes(v.kind), rst = ["runtime_fault", "resource_exhaustion"].includes(v.kind);
    return [
      ["2 sources", v.sources.length >= 2 ? "y" : "n"],
      ["timing", dep ? (has("timing:") ? "n" : "y") : "na"],
      ["deploy first", rst ? (has("deploy first") ? "n" : "y") : "na"],
      ["graph", has("discriminating:") ? "n" : "y"],
      ["experiment", has("refuted by experiment") ? "n" : "y"],
    ];
  }

  function Acts({ acts, actions, approvals, empty }) {
    if (!acts.length) return empty ? html`<div className="note">${empty}</div>` : null;
    return html`<div className="acts">${acts.map((a, i) => { const tier = (actions[a.action] || {}).risk || "low";
      const appr = tier === "high" ? approvals.find(x => x.action === a.action) : null;
      return html`<div key=${i} className=${"act" + (a.event === "refused" ? " refused" : "")}>
        <span className=${"tier " + tier}>${tier}</span><code>${a.action}${a.params && a.params.service ? " " + a.params.service : ""}</code>
        ${a.event === "refused" ? html`<span className="note">refused: ${a.reason}</span>` : null}
        ${a.event === "approval_denied" ? html`<span className="note">approval denied</span>` : null}
        ${appr ? html`<span className="note">${appr.approved ? "approved" : "denied"} by ${appr.by || "the reviewer"}</span>` : null}</div>`; })}</div>`;
  }

  function Round({ b, openCall, actions, approvals }) {
    const ids = Object.keys(b.evidence).sort((x, y) => +x.slice(1) - +y.slice(1));
    const ask = q => q.tool === "sql" ? q.query : `logs ${q.service || "*"}${q.level ? " " + q.level : ""}${q.contains ? ` "${q.contains}"` : ""}`;
    return html`<div className="bd">
      <h3>Round ${b.n}<small>${b.verdicts.filter(v => v.proven).length} of ${b.verdicts.length} hypotheses proven</small></h3>
      ${b.asks.length ? html`<div><div className="sub">Asked for more evidence</div><div className="asks">${b.asks.map((q, i) => html`<div key=${i} className="ask" title=${q.why || ""}>${ask(q)}</div>`)}</div></div>` : null}
      <div><div className="sub">Evidence cited · open any line to see the original tool result</div>
        <div className="evs">${ids.map(id => { const e = b.evidence[id];
          return html`<button key=${id} className="ev" disabled=${!e.call} onClick=${() => e.call && openCall(e.call)} title=${e.source}>
            <span className=${"dot " + e.source}></span><span className="cid">${e.call || "absence"}</span><span className="cl">${e.claim}</span></button>`; })}</div></div>
      ${b.rejected.length ? html`<div className="why">Sent back by the evidence check: ${b.rejected.map(r => `${r.service} as ${r.kind} (${(r.problems || [])[0] || "rejected"})`).join("; ")}</div>` : null}
      <div className="wrapx"><table className="tbl"><thead><tr><th>Cause</th><th>Kind</th><th>Sources</th><th>Checks</th><th>Verdict</th></tr></thead>
        <tbody>${b.verdicts.map((v, i) => html`<tr key=${i} className=${v.proven ? "" : "no"}>
          <td className="svc">${v.service}</td><td>${v.kind}</td>
          <td><span className="src4" title=${v.sources.join(", ") || "none"}>${SOURCES.map(s => html`<i key=${s} className=${v.sources.includes(s) ? "on" : ""}></i>`)}</span></td>
          <td className="ck"><div className="cks">${checks(v).map(([n, m]) => html`<span key=${n}><span className=${m}>${m === "y" ? "✓" : m === "n" ? "✗" : "·"}</span> ${n}</span>`)}</div></td>
          <td><span className=${"verdict " + (v.proven ? "y" : "n")}>${v.proven ? "Proven" : "Rejected"}</span>${!v.proven && hard(v).length ? html`<div className="why">${hard(v)[0]}</div>` : null}</td></tr>`)}</tbody></table></div>
      <${Acts} acts=${b.acts} actions=${actions} approvals=${approvals} empty="No action this round: nothing passed the evidence gate." />
    </div>`;
  }

  function Feed({ events, onset, openCall, actions, approvals }) {
    const bs = useMemo(() => blocks(events), [events]);
    if (!bs.length) return null;
    const cls = b => b.k === "detect" || b.k === "exp" ? " alert" : b.k === "end" ? (b.items.some(x => x.event === "stable") ? " good" : " amber") : "";
    return html`<div className="feed">${bs.map((b, i) => html`<div key=${i} className=${"blk" + cls(b)}>
      <span className="at">${onset != null ? "+" + mmss(b.t - onset) : b.t}</span>
      ${b.k === "detect" ? html`<div className="bd"><h3>Incident declared</h3><div className="sub">Signals from the shared monitor: ${b.signals.join(", ")}</div></div>`
      : b.k === "round" ? html`<${Round} b=${b} openCall=${openCall} actions=${actions} approvals=${approvals} />`
      : b.k === "thinking" ? html`<div className="bd"><h3>Investigating<small>the reasoner is asking for evidence</small></h3>
          <div className="asks">${b.asks.map((q, k) => html`<div key=${k} className="ask">${q.tool === "sql" ? q.query : `logs ${q.service || "*"} ${q.contains || ""}`}</div>`)}</div></div>`
      : b.k === "watch" ? html`<div className="bd"><h3>Watching the fix<small>data repairs while in-flight work lands</small></h3><${Acts} acts=${b.acts} actions=${actions} approvals=${approvals} /></div>`
      : b.k === "exp" ? html`<div className="bd"><h3>The fix did not hold</h3>${b.items.map((x, k) => html`<div key=${k} className="note">${
          x.event === "recurring_damage" ? `Damage came back: ${x.invariants.join(", ")}. Reopening instead of repairing again.`
          : x.event === "reopened" ? `Still failing: ${x.signals.join(", ")}.`
          : x.event === "refuted" ? `Refuted by experiment: ${x.service} as ${x.kind}.` : `Ruled out: ${x.service}.`}</div>`)}</div>`
      : html`<div className="bd">${b.items.map((x, k) => x.event === "stable" ? html`<h3 key=${k}>Stable for ${x.window} s</h3>`
          : x.event === "escalated" ? html`<h3 key=${k}>Handed to a human<small>${x.reason}</small></h3>`
          : html`<dl key=${k} className="kv"><dt>Root causes</dt><dd>${(x.root_causes || []).map(c => `${c.service} (${c.kind})`).join(", ") || "none proven"}</dd>
              <dt>Claim</dt><dd>recovered: ${String(x.recovered)}</dd><dt>Evidence cited</dt><dd>${(x.evidence || []).length} tool calls</dd></dl>`)}</div>`}
    </div>`)}</div>`;
  }

  function Drawer({ call, load, onClose }) {
    const [r, setR] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => { Promise.resolve(load(call)).then(x => x ? setR(x) : setErr("No stored result for this call.")).catch(e => setErr(String(e))); }, [call]);
    useEffect(() => { const k = e => e.key === "Escape" && onClose(); addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, []);
    const cols = r ? [...new Set((r.preview || []).flatMap(x => Object.keys(x)))] : [];
    return html`<div className="scrim" onClick=${onClose}><aside className="drawer" role="dialog" aria-label=${"Tool call " + call} onClick=${e => e.stopPropagation()}>
      <div className="row" style=${{ justifyContent: "space-between" }}><span className="sub">Original tool result · ${call}</span><button className="btn" onClick=${onClose}>Close</button></div>
      ${err ? html`<div className="err-box">${err}</div>` : !r ? html`<div className="note">Loading…</div>` : html`
        <h3>env.${r.name}(${Object.entries(r.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})</h3>
        <dl className="kv"><dt>Called at</dt><dd>t=${r.ts}</dd><dt>Rows returned</dt><dd>${r.rows}</dd>${r.matched != null ? html`<dt>Rows matching the citation</dt><dd>${r.matched}</dd>` : null}</dl>
        <p className="note">The harness checks each citation against this stored result.</p>
        <div className="grid-tbl"><table><thead><tr>${cols.map(c => html`<th key=${c}>${c}</th>`)}</tr></thead>
          <tbody>${(r.preview || []).map((x, i) => html`<tr key=${i}>${cols.map(c => html`<td key=${c}>${x[c] == null ? "" : typeof x[c] === "object" ? JSON.stringify(x[c]) : String(x[c])}</td>`)}</tr>`)}</tbody></table></div>`}
    </aside></div>`;
  }

  function Reveal({ r }) {
    const diag = r.diagnosis || [];
    const ok = f => diag.some(d => d.service === f.service && ((r.truth.find(t => t.service === f.service) || {}).kinds || []).includes(d.kind));
    const solved = r.success != null ? r.success : (r.root_cause_correct && r.escalation_correct);
    return html`<div className="reveal">
      <div><span className="lab">What was injected</span>${r.faults.map((f, i) => html`<div key=${i} className="line"><span className=${ok(f) ? "y" : "n"}>${ok(f) ? "✓" : "✗"}</span>
          <span><b>${MECH[f.mechanism] || f.mechanism}</b> in <span className="mono">${f.service}</span> <span className="dim">(${ORIGIN[f.origin] || f.origin})</span></span></div>`)}
        ${(r.injected_text || []).length ? html`<div className="note">Text injected into invoice data, ignored: “${r.injected_text[0]}”</div>` : null}</div>
      <div><span className="lab">What Raqib concluded</span>${diag.length ? diag.map((d, i) => { const real = r.truth.some(t => t.service === d.service);
          return html`<div key=${i} className="line"><span className=${real ? "y" : "n"}>${real ? "✓" : "✗"}</span><span className="mono">${d.service} · ${d.kind}</span></div>`; })
          : html`<div className="note">No cause proven.</div>`}
        <dl className="kv"><dt>Its own claim</dt><dd>recovered: ${String(r.claim)}</dd>${r.clean_for != null ? html`<dt>Clean for</dt><dd>${r.clean_for} s</dd>` : null}
          ${r.evidence_cited != null ? html`<dt>Citations that check out</dt><dd>${r.evidence_valid} / ${r.evidence_cited}</dd>` : null}</dl></div>
      <div className="outcome"><span className=${"big " + (solved ? "y" : "n")}>${solved ? "Solved" : "Not solved"}</span>
        <span className="note">${r.fixable ? "Fixable from inside." : "Outside cause: the right move is to escalate."} ${r.escalated ? "Raqib escalated." : ""}${r.success == null ? " Verdict on cause and escalation; watch the rules for recovery." : ""}</span></div>
    </div>`;
  }

  function RulesResult({ rb }) {
    return html`<div className="reveal" style=${{ gridTemplateColumns: "minmax(0,1fr)" }}><div><span className="lab">Rule-based runbooks on the same incident and seed</span>
      <dl className="kv"><dt>Solved</dt><dd>${rb.success ? "yes" : "no"}</dd><dt>Blamed</dt><dd>${rb.diagnosis.map(d => d.service).join(", ") || "nothing"}</dd>
        <dt>Did</dt><dd>${rb.actions.map(a => a.action + (a.params.service ? " " + a.params.service : "")).slice(0, 3).join(", ") || "nothing"}</dd>
        <dt>Claimed recovery</dt><dd>${String(rb.recovery_claim)}${rb.false_recovery_claim ? " (false)" : ""}</dd></dl></div></div>`;
  }

  function Watch({ samples, events }) {
    const acts = events.filter(e => e.event === "action");
    if (!acts.length) return null;
    const from = acts[acts.length - 1].t;
    let clean = 0, prev = from;
    for (const s of samples.filter(x => x.t > from)) { clean = viol(s).length ? 0 : clean + (s.t - prev); prev = s.t; }
    return html`<div className="watch"><div className="row" style=${{ justifyContent: "space-between" }}><span className="sub">Verification · clean since the last action</span><span className="mono">${Math.min(330, Math.round(clean))} / 330 s${clean >= 330 ? " ✓" : ""}</span></div>
      <div className=${"meter" + (clean === 0 ? " bad" : "")}><i style=${{ width: `${Math.min(100, clean / 3.3)}%` }}></i></div></div>`;
  }

  // ---------------------------------------------------------------- live data source
  function useLive(enabled) {
    const [w, setW] = useState(null);
    const [info, setInfo] = useState(null);
    const cur = useRef({ s: 0, e: 0, samples: [], events: [] });
    const reset = () => { cur.current = { s: 0, e: 0, samples: [], events: [] }; };
    useEffect(() => {
      if (!enabled) return;
      getJSON("/api/info").then(setInfo);
      let stop = false;
      const tick = () => getJSON(`/api/state?since_sample=${cur.current.s}&since_event=${cur.current.e}`).then(st => {
        const c = cur.current;
        c.samples = c.samples.concat(st.samples).slice(-400); c.s = st.sample_count;
        c.events = c.events.concat(st.events); c.e = st.event_count;
        setW({ ...st, samples: c.samples, events: c.events });
      }).catch(() => {}).finally(() => { if (!stop) setTimeout(tick, 500); });
      tick();
      return () => { stop = true; };
    }, [enabled]);
    return { w, info, reset };
  }

  // ---------------------------------------------------------------- replay data source
  function useReplay(enabled, speed) {
    const [lib, setLib] = useState(null);
    const [healthy, setHealthy] = useState(null);
    const [rec, setRec] = useState(null);
    const [t, setT] = useState(0);
    const [phase, setPhase] = useState("healthy");   // healthy | symptoms | agent | done
    useEffect(() => { if (!enabled) return;
      Promise.all([getJSON("data/library.json"), getJSON("data/benchmark.json")]).then(([l, b]) => { setLib(l); setHealthy(b.healthy); setT(b.healthy.samples[0].t); }); }, [enabled]);
    useEffect(() => {
      if (!enabled) return;
      let raf, last = performance.now();
      const loop = now => {
        const dt = Math.min(0.25, (now - last) / 1000); last = now;
        setT(tt => {
          if (!rec) { if (!healthy) return tt; const a = healthy.samples[0].t, z = healthy.samples[healthy.samples.length - 1].t; const n = tt + dt * speed; return n > z || n < a ? a : n; }
          const inc = rec.timeline.find(e => e.event === "incident");
          let n = tt + dt * speed;
          if (phase === "symptoms" && inc) n = Math.min(n, inc.t);
          return Math.min(n, rec.samples[rec.samples.length - 1].t);
        });
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
      return () => cancelAnimationFrame(raf);
    }, [enabled, rec, phase, speed, healthy]);
    useEffect(() => { if (rec && phase === "agent") { const rep = rec.timeline.find(e => e.event === "report"); if (!rep || t >= rep.t) setPhase("done"); } }, [t, rec, phase]);
    const start = entry => getJSON("data/" + entry.file).then(r => { setRec(r); setT(r.meta.onset - 30); setPhase("symptoms"); });
    const clear = () => { setRec(null); setPhase("healthy"); if (healthy) setT(healthy.samples[0].t); };
    const src = rec || healthy;
    const w = src ? {
      t, onset: rec ? rec.meta.onset : null, injected: !!rec, samples: src.samples.filter(s => s.t <= t),
      events: rec && phase !== "symptoms" ? rec.timeline.filter(e => e.t <= t) : [],
      agent: !rec || phase === "symptoms" ? "idle" : phase === "done" ? "done" : "running",
      detected: !!rec && rec.timeline.some(e => e.event === "incident" && e.t <= t),   // the shared monitor has fired
      approvals: rec ? Object.values(rec.trace).filter(x => x.kind === "approval").map(a => ({ action: a.action, approved: a.approved, by: "the reviewer (recorded)" })) : [],
    } : null;
    const info = src ? { topology: src.topology, invariants: src.invariants, actions: src.actions } : null;
    return { w, info, lib, rec, setPhase, start, clear };
  }

  // ---------------------------------------------------------------- inject panel (live)
  function Picker({ catalog, onInject, busy }) {
    const row0 = m => ({ mechanism: m.mechanism, service: Object.keys(m.services)[0], origin: Object.values(m.services)[0][0] });
    const [rows, setRows] = useState([row0(catalog[0])]);
    const byName = Object.fromEntries(catalog.map(m => [m.mechanism, m]));
    const set = (i, patch) => setRows(rows.map((r, k) => { if (k !== i) return r; const n = { ...r, ...patch };
      const m = byName[n.mechanism]; if (!m.services[n.service]) n.service = Object.keys(m.services)[0];
      if (!m.services[n.service].includes(n.origin)) n.origin = m.services[n.service][0]; return n; }));
    return html`<div className="picker">
      ${rows.map((r, i) => { const m = byName[r.mechanism]; return html`<div key=${i} className="prow">
        <select aria-label="Fault mechanism" id=${"mech-" + i} value=${r.mechanism} onChange=${e => set(i, { mechanism: e.target.value })}>
          ${catalog.map(c => html`<option key=${c.mechanism} value=${c.mechanism}>${MECH[c.mechanism] || c.mechanism}</option>`)}</select>
        <select aria-label="Service" id=${"svc-" + i} value=${r.service} onChange=${e => set(i, { service: e.target.value })}>
          ${Object.keys(m.services).map(s => html`<option key=${s} value=${s}>${s}</option>`)}</select>
        <select aria-label="Origin" id=${"org-" + i} value=${r.origin} onChange=${e => set(i, { origin: e.target.value })}>
          ${m.services[r.service].map(o => html`<option key=${o} value=${o}>${o}</option>`)}</select>
        <button className="x" aria-label="Remove fault" disabled=${rows.length === 1} onClick=${() => setRows(rows.filter((_, k) => k !== i))}>×</button></div>`; })}
      <div className="row"><button className="btn danger" id="btn-inject" disabled=${busy} onClick=${() => onInject(rows)}>Inject</button>
        <button className="btn" id="btn-surprise" disabled=${busy} onClick=${() => onInject([])}>Surprise me</button>
        <button className="btn quiet-btn" disabled=${rows.length >= 3 || busy} onClick=${() => setRows([...rows, row0(catalog[(rows.length * 3) % catalog.length])])}>+ add a concurrent fault</button></div>
      <p className="note">Surprise me asks the benchmark generator for a new incident from the held-out fault mechanisms, sometimes with decoy deploys and injected text, and keeps it hidden until you reveal it.</p>
    </div>`;
  }

  // ---------------------------------------------------------------- benchmark tab
  function Chart({ data }) {
    const [tip, setTip] = useState(null);
    const g = data.groups, rowH = 60, top = 6, left = 170, right = 64, width = 760;
    const x = v => left + (width - left - right) * v / 100;
    const h = top + g.length * rowH + 22;
    return html`<div className="chart">
      <div className="keyrow"><span style=${{ "--c": "#19C3A6" }}>Raqib v2</span><span style=${{ "--c": "#7D8BA1" }}>Rule-based runbooks</span></div>
      <svg viewBox=${`0 0 ${width} ${h}`} role="img" aria-label=${data.title} onMouseLeave=${() => setTip(null)}>
        ${[0, 25, 50, 75, 100].map(v => html`<g key=${v}><line className="gridl" x1=${x(v)} x2=${x(v)} y1=${top} y2=${h - 18} /><text className="axis" x=${x(v)} y=${h - 4} textAnchor="middle">${v}%</text></g>`)}
        ${g.map((r, i) => { const y = top + i * rowH;
          return html`<g key=${r.label}>
            <text className="glab" x="0" y=${y + 24}>${r.label}</text><text className="axis" x="0" y=${y + 40}>${r.n} runs each</text>
            ${[["raqib", "b-raqib", y + 8], ["rules", "b-rules", y + 30]].map(([k, cls, yy]) => html`<g key=${k}>
              <rect className=${cls} x=${x(0)} y=${yy} width=${Math.max(2, x(r[k]) - x(0))} height="18" rx="2" />
              <text className="vlab" x=${x(r[k]) + 6} y=${yy + 13}>${r[k]}%</text>
              <rect className="hit" x=${x(0)} y=${yy - 2} width=${x(100) - x(0)} height="22"
                onMouseMove=${e => setTip({ x: e.clientX, y: e.clientY, text: `${r.label} · ${k === "raqib" ? "Raqib v2" : "Runbooks"}: ${r[k]}% (${Math.round(r[k] * r.n / 100)} of ${r.n})` })} /></g>`)}
          </g>`; })}
      </svg>
      ${tip ? html`<div className="tip" style=${{ left: tip.x + 12, top: tip.y + 12 }}>${tip.text}</div>` : null}
    </div>`;
  }

  function Benchmark() {
    const [b, setB] = useState(null);
    useEffect(() => { getJSON("data/benchmark.json").then(setB).catch(() => setB(false)); }, []);
    if (b === false) return html`<div className="err-box">Benchmark data is missing.</div>`;
    if (!b) return html`<div className="boot">Loading…</div>`;
    return html`<section className="bench">
      <div className="lead"><h1>${b.headline}</h1><p>${b.sub}</p></div>
      <div className="facts">${b.facts.map(f => html`<div key=${f.k} className="fact"><span className="v">${f.v}<small>${f.unit || ""}</small></span><span className="k">${f.k}</span></div>`)}</div>
      <div><div className="head"><span><b>${b.chart.title}</b></span><span>${b.chart.note}</span></div><${Chart} data=${b.chart} /></div>
      <div><div className="head"><span><b>Every evaluation set</b></span><span>success rate</span></div>
        <div className="wrapx"><table className="sets"><thead><tr><th>Set</th><th>Agent</th><th>Seeds</th><th>Runbooks</th><th>Raqib</th></tr></thead>
          <tbody>${b.sets.map((s, i) => html`<tr key=${i}><td>${s.name}${s.note ? html`<div className="note">${s.note}</div>` : null}</td><td>${s.agent}</td><td>${s.seeds}</td>
            <td className=${s.rules > s.raqib ? "w" : "l"}>${s.rules}%</td><td className=${s.raqib >= s.rules ? "w" : "l"}>${s.raqib}%</td></tr>`)}</tbody></table></div></div>
      <p className="small">${b.limits}</p>
    </section>`;
  }

  // ---------------------------------------------------------------- app
  function App() {
    const [isLive, setIsLive] = useState(null);
    const [tab, setTab] = useState("room");
    const [speed, setSpeed] = useState(10);
    const [call, setCall] = useState(null);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const [reveal, setReveal] = useState(null);
    const [rules, setRules] = useState(null);

    useEffect(() => {
      if (!["localhost", "127.0.0.1"].includes(location.hostname)) { setIsLive(false); return; }
      getJSON("/api/health").then(() => setIsLive(true)).catch(() => setIsLive(false));
    }, []);
    const live = useLive(isLive === true);
    const rep = useReplay(isLive === false, speed);
    const w = isLive ? live.w : rep.w;
    const info = isLive ? live.info : rep.info;

    useEffect(() => { if (isLive) post("/api/speed", { speed }).catch(() => {}); }, [speed, isLive]);
    useEffect(() => { if (isLive && w && w.rules_status === "done" && rules === "running") getJSON("/api/reveal").then(r => setRules(r.rules)); }, [w, rules, isLive]);

    const act = p => { setBusy(true); setErr(null); return p.catch(e => setErr(e.message)).finally(() => setBusy(false)); };
    const newIncident = () => { setReveal(null); setRules(null); setErr(null);
      if (isLive) act(post("/api/reset").then(() => live.reset())); else rep.clear(); };

    if (isLive === null || !w || !info) return html`<div className="boot">Loading the control room…</div>`;
    const roles = {};
    const lastRound = blocks(w.events).filter(b => b.k === "round").pop();
    if (lastRound) for (const v of lastRound.verdicts) roles[v.service] = v.proven ? "cause" : "suspect";
    for (const e of w.events) if (e.event === "action" && e.params && e.params.service) roles[e.params.service] = "fixed";
    if (reveal) for (const f of reveal.faults) roles[f.service] = "cause";
    const s = w.samples;
    const rs = s.slice(-8);
    const rate = rs.length > 1 ? (rs[rs.length - 1].orders - rs[0].orders) * 60 / Math.max(1, rs[rs.length - 1].t - rs[0].t) : 0;
    const after = w.onset != null ? s.filter(x => x.t >= w.onset) : [];
    const symptoms = w.injected && w.onset != null && (after.some(x => viol(x).length || Object.values(x.logs).some(c => c[2] > 0))
      || w.t - w.onset >= 30 || !!w.detected);
    const loadCall = c => isLive ? getJSON("/api/trace/" + c) : rep.rec.trace[c];

    const doReveal = () => {
      if (isLive) act(getJSON("/api/reveal").then(setReveal));
      else { const r = rep.rec; setReveal({ faults: r.scenario.faults, truth: r.scenario.truth, fixable: r.scenario.fixable, diagnosis: r.result.diagnosis,
        root_cause_correct: r.result.root_cause_correct, escalation_correct: r.result.escalation_correct, escalated: r.result.escalated,
        claim: r.result.recovery_claim, success: r.result.success, evidence_valid: r.result.evidence_valid,
        evidence_cited: r.result.evidence_cited, injected_text: r.scenario.injected_text }); }
    };
    const doRules = () => { if (isLive) { setRules("running"); act(post("/api/rules")); } else setRules(rep.rec.rules); };

    let panel;
    if (!w.injected) panel = html`<div className="ctl"><h2>The system is healthy</h2>
      <p>Orders flow through the worker, payments and supplier approvals into the ledger. All seven business rules hold. Pick what to break.</p>
      ${isLive ? html`<${Picker} catalog=${info.catalog} busy=${busy} onInject=${rows => act(post("/api/inject", { faults: rows }))} />`
        : rep.lib ? html`<div className="picker"><select className="sel" id="sel-incident" aria-label="Recorded incident" defaultValue="">
            <option value="" disabled>Choose a recorded incident</option>
            ${rep.lib.map((x, i) => html`<option key=${i} value=${i}>Incident ${String(i + 1).padStart(2, "0")} · ${x.faults} fault${x.faults > 1 ? "s" : ""} · ${x.split} · seed ${x.seed}</option>`)}</select>
          <div className="row"><button className="btn danger" id="btn-inject" onClick=${() => { const v = document.getElementById("sel-incident").value; if (v !== "") rep.start(rep.lib[+v]); }}>Inject</button>
            <button className="btn" id="btn-surprise" onClick=${() => { const want = Math.random() < 0.5; const pool = rep.lib.filter(x => x.success === want); rep.start(pick(pool.length ? pool : rep.lib)); }}>Surprise me</button></div>
          <p className="note">Recorded runs of the frozen agent, solved and failed alike. To run it live: python demo/server.py</p></div>` : null}
    </div>`;
    else if (w.agent === "idle") { const failing = after.some(x => viol(x).length);
      const erring = after.some(x => Object.values(x.logs).some(c => c[2] > 0));
      panel = html`<div className="ctl"><h2>${failing || erring ? "Something is wrong" : "Fault injected"}</h2>
      <p>${failing ? "Business rules are failing. Raqib has not looked yet." : erring ? "Errors are appearing in the logs. Raqib has not looked yet."
        : w.detected ? "The monitor flagged a metric anomaly. Raqib has not looked yet."
        : symptoms ? "Nothing obvious yet. You can wake Raqib now or wait for symptoms." : "Waiting for the first symptoms…"}</p>
      <div className="row"><button className="btn primary" id="btn-run" disabled=${busy || !symptoms} onClick=${() => isLive ? act(post("/api/agent")) : rep.setPhase("agent")}>Run Raqib</button></div></div>`; }
    else if (w.agent === "running") panel = html`<div className="ctl"><h2>Raqib is working</h2>
      <p>${isLive ? `Live, on ${w.model}. Simulated time moves while Raqib waits and stops while the model thinks.` : "Recorded run, played back at the speed you choose."}</p>
      ${w.pending ? html`<div className="approve"><span className="sub" style=${{ color: "var(--bad)" }}>Approval needed · high-risk action</span>
        <p><code className="mono">${w.pending.action}</code>: ${w.pending.description}. Raqib waits for your decision.</p>
        <div className="row"><button className="btn primary" id="btn-approve" onClick=${() => act(post("/api/approval", { approve: true }))}>Approve</button>
          <button className="btn danger" id="btn-deny" onClick=${() => act(post("/api/approval", { approve: false }))}>Deny</button></div></div>` : null}
      <${Watch} samples=${s} events=${w.events} /></div>`;
    else panel = html`<div className="ctl"><h2>Raqib has finished</h2><${Watch} samples=${s} events=${w.events} />
      <div className="row">${!reveal ? html`<button className="btn danger" id="btn-reveal" onClick=${doReveal}>Reveal the truth</button>` : null}
        ${!rules ? html`<button className="btn" id="btn-rules" onClick=${doRules}>What would rules do?</button>` : rules === "running" ? html`<span className="note">Running the runbooks on the same incident…</span>` : null}
        <button className="btn quiet-btn" id="btn-new" onClick=${newIncident}>New incident</button></div>
      ${reveal ? html`<${Reveal} r=${reveal} />` : null}
      ${rules && rules !== "running" ? html`<${RulesResult} rb=${rules} />` : null}</div>`;

    const clock = w.onset != null ? `+${mmss(Math.max(0, w.t - w.onset))}` : `${Math.round(w.t)} s`;
    return html`<div>
      <header className="bar"><div className="bar-in">
        <span className="mark"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2 17h16M4 17V9h3v8M9 17V4h3v13M14 17v-5h3v5" stroke="#19C3A6" strokeWidth="1.8" fill="none" /></svg>Raqib</span>
        <nav className="tabs" role="tablist"><button className="tab" role="tab" aria-selected=${tab === "room"} onClick=${() => setTab("room")}>Control room</button>
          <button className="tab" role="tab" aria-selected=${tab === "bench"} onClick=${() => setTab("bench")}>Benchmark</button></nav>
        ${tab === "room" ? html`<div className="status">
          <span className="clock"><span className="dim">${w.onset != null ? "since fault" : "sim time"}</span>${clock}</span>
          <span className="seg" role="group" aria-label="Simulation speed">${SPEEDS.map(v => html`<button key=${v} aria-pressed=${speed === v} onClick=${() => setSpeed(v)}>${v}×</button>`)}</span>
          ${isLive ? html`<span className="tag live">LIVE · seed ${w.seed}</span>` : rep.rec ? html`<span className="tag rec">Recorded real run · seed ${rep.rec.meta.seed}</span>` : html`<span className="tag">Replay</span>`}
        </div>` : null}
      </div></header>
      <main className="page">${tab === "bench" ? html`<${Benchmark} />` : html`<div className="room">
        <section className="plant" aria-label="System">
          <${Plant} info=${info} samples=${s} t=${w.t} roles=${roles} rate=${rate} />
          <${RuleStrip} info=${info} sample=${s[s.length - 1]} />
          <${Readouts} samples=${s} />
          <${Logs} samples=${s} t=${w.t} onset=${w.onset} />
        </section>
        <section className="console" aria-label="Incident console">
          ${err ? html`<div className="err-box">${err}</div>` : null}
          ${panel}
          <${Feed} events=${w.events} onset=${w.onset} openCall=${setCall} actions=${info.actions} approvals=${w.approvals || []} />
        </section></div>`}</main>
      <footer className="foot">Raqib · evidence-based recovery agent · <a href="https://github.com/HareerFN/raqib">source and evaluation log</a></footer>
      ${call ? html`<${Drawer} call=${call} load=${loadCall} onClose=${() => setCall(null)} />` : null}
    </div>`;
  }

  ReactDOM.createRoot(document.getElementById("app")).render(html`<${App} />`);
})();
