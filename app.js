/* Raqib control room. Two modes on one interface:
   Replay: recorded real benchmark runs (site/data), for GitHub Pages.
   Live:   demo/server.py runs the agent for real on a fresh incident. */
(function () {
  "use strict";
  const { useState, useEffect, useMemo, useRef, useCallback } = React;
  const html = htm.bind(React.createElement);

  // ------------------------------------------------------------------ vocabulary (UI copy only)
  const STAGES = [
    ["healthy", "Healthy", "سليم"], ["choose", "Choose fault", "اختر العطل"], ["symptoms", "Symptoms", "الأعراض"],
    ["investigate", "Investigation", "التحقيق"], ["verify", "Verify + reveal", "التحقق والكشف"], ["score", "Scoreboard", "النتائج"],
  ];
  const RULE_NAMES = {
    I1_payment_has_order: "Paid orders get confirmed", I2_no_orphaned_orders: "New orders get picked up",
    I3_ledger_integrity: "Ledger VAT and totals add up", I4_workflow_progress: "Supplier approvals come back",
    I5_single_payment: "Each order charged once", I6_retry_budget: "Retries stay within budget",
    I7_payments_succeed: "Payments succeed",
  };
  const MECHANISMS = {
    payment_swallow: "Payment confirmations silently dropped", early_ack: "Messages lost when the worker crashes under load",
    invoice_swap: "Amount and VAT silently swapped", callback_lost: "Supplier approvals never come back",
    agent_retry_loop: "Retry limit ignored for one supplier", provider_down: "Payment provider outage",
    conn_leak: "Database connections never returned", invoice_format_change: "Supplier switched invoice template",
    duplicate_charge: "Retries charge customers twice", capacity_config: "Worker concurrency set to 1",
    clock_skew: "Service clock running behind", disk_fill: "Disk quota filled by one service",
    "config change": "Configuration change shipped with a deploy",
  };
  const ORIGINS = { deploy: "arrived with a deploy", transient: "process stuck, no deploy", external: "outside the system",
    config: "config shipped with a deploy" };
  const SRC = ["database", "metrics", "logs", "deploys"];
  const POS = {   // service map layout (viewBox 0 0 960 520)
    "order-service": [90, 150], "queue": [270, 150], "worker": [460, 150], "invoice-agent": [460, 40],
    "payment-service": [660, 150], "payment-provider": [860, 150], "workflow": [660, 320], "supplier-api": [860, 320],
    "sync-job": [270, 320], "database": [460, 460],
  };
  const FLOW = ["order-service", "queue", "worker", "payment-service", "workflow"];

  // ------------------------------------------------------------------ helpers
  const fetchJSON = (url, opts) => fetch(url, opts).then(r => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
  const sampleAt = (samples, t) => { let s = samples[0]; for (const x of samples) { if (x.t > t) break; s = x; } return s; };
  const violated = s => s ? Object.entries(s.inv).filter(([, n]) => n > 0).map(([k]) => k) : [];
  const logSum = (samples, from, to) => {
    const tot = [0, 0, 0];
    for (const s of samples) if (s.t > from && s.t <= to) for (const c of Object.values(s.logs)) { tot[0] += c[0]; tot[1] += c[1]; tot[2] += c[2]; }
    return tot;
  };
  const errServices = (samples, from, to) => {
    const out = new Set();
    for (const s of samples) if (s.t > from && s.t <= to) for (const [svc, c] of Object.entries(s.logs)) if (c[2] > 0) out.add(svc);
    return out;
  };
  const fmtT = s => `${s >= 0 ? "+" : "−"}${Math.floor(Math.abs(s) / 60)}:${String(Math.abs(s) % 60).padStart(2, "0")}`;
  const pick = a => a[Math.floor(Math.random() * a.length)];
  const shuffle = a => { const b = [...a]; for (let i = b.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [b[i], b[j]] = [b[j], b[i]]; } return b; };

  // ------------------------------------------------------------------ steps from the agent's timeline
  function buildSteps(rec) {
    const steps = [];
    const tl = rec.timeline;
    let i = 0;
    while (i < tl.length) {
      const e = tl[i];
      if (e.event === "incident") steps.push({ type: "detect", t: e.t, e });
      else if (e.event === "investigated") steps.push({ type: "investigate", t: e.t, e });
      else if (e.event === "revision") steps.push({ type: "revision", t: e.t, e });
      else if (e.event === "hypotheses") {
        steps.push({ type: "evidence", t: e.t, e }, { type: "hypotheses", t: e.t, e },
          { type: "ledger", t: e.t, e }, { type: "discriminating", t: e.t, e });
        const acts = [];
        let j = i + 1;
        while (j < tl.length && ["action", "refused", "approval_denied"].includes(tl[j].event)) { acts.push(tl[j]); j++; }
        steps.push({ type: "risk", t: e.t, e, acts });
        i = j; continue;
      } else if (["recurring_damage", "reopened", "refuted", "ruled_out"].includes(e.event)) {
        const group = [e];
        let j = i + 1;
        while (j < tl.length && ["recurring_damage", "reopened", "refuted", "ruled_out"].includes(tl[j].event)) { group.push(tl[j]); j++; }
        steps.push({ type: "experiment", t: e.t, group });
        i = j; continue;
      } else if (e.event === "action") steps.push({ type: "repair", t: e.t, acts: [e] });
      else if (e.event === "stable") steps.push({ type: "stable", t: e.t, e });
      else if (e.event === "escalated") steps.push({ type: "escalate", t: e.t, e });
      else if (e.event === "report") steps.push({ type: "report", t: e.t, e });
      i++;
    }
    // merge consecutive repair-only steps
    const out = [];
    for (const s of steps) {
      const last = out[out.length - 1];
      if (s.type === "repair" && last && last.type === "repair") last.acts.push(...s.acts); else out.push(s);
    }
    return out;
  }

  function mapState(rec, t, step, revealed) {
    // what the wall shows at time t: affected (red), suspects (amber), causes and fixes
    const s = sampleAt(rec.samples, t);
    const state = {};
    const inv = rec.invariants.reduce((m, x) => (m[x.id] = x.services, m), {});
    for (const id of violated(s)) for (const svc of inv[id] || []) state[svc] = "affected";
    for (const svc of errServices(rec.samples, t - 60, t)) state[svc] = "affected";
    if (step && step.e && step.e.verdicts) for (const v of step.e.verdicts) state[v.service] = v.proven ? "cause" : "suspect";
    if (step && step.acts) for (const a of step.acts) if (a.event === "action" && a.params && a.params.service) state[a.params.service] = "fixed";
    if (revealed) for (const f of rec.scenario.faults) state[f.service] = "cause";
    return state;
  }

  // ------------------------------------------------------------------ the wall
  function ServiceMap({ topology, state, flowing, slow }) {
    const nodes = Object.keys(POS).filter(k => topology[k]);
    const edges = [];
    for (const [svc, spec] of Object.entries(topology)) for (const d of spec.depends_on || []) if (POS[svc] && POS[d]) edges.push([svc, d]);
    const flowPath = "M" + FLOW.map(s => POS[s].join(",")).join(" L");
    const dots = flowing ? [0, 1, 2, 3, 4, 5] : [];
    return html`<div className="pane map">
      <div className="pane-h"><span className="label">Service map <b>· from topology.yaml</b></span>
        <span className="legend"><span><i style=${{ borderColor: "#19C3A6" }}></i>healthy</span><span><i style=${{ borderColor: "#F05D4F" }}></i>affected</span>
          <span><i style=${{ borderColor: "#F2A93B" }}></i>suspect</span><span><i style=${{ borderColor: "#6B7A90", borderStyle: "dashed" }}></i>external</span></span></div>
      <svg viewBox="0 0 960 520" role="img" aria-label="Service dependency map">
        ${edges.map(([a, b], k) => html`<line key=${k} className=${"edge" + (FLOW.includes(a) && FLOW.includes(b) ? " flow" : "")}
            x1=${POS[a][0]} y1=${POS[a][1]} x2=${POS[b][0]} y2=${POS[b][1]} />`)}
        <path d=${flowPath} className="edge flow" style=${{ strokeWidth: 3, opacity: 0.6 }} />
        ${dots.map(k => html`<circle key=${k} r="4.5" className=${"dot" + (slow ? " slow" : "")}>
            <animateMotion dur=${slow ? "9s" : "4.5s"} begin=${`-${(k * (slow ? 1.5 : 0.75)).toFixed(2)}s`} repeatCount="indefinite" path=${flowPath} /></circle>`)}
        ${nodes.map(svc => { const [x, y] = POS[svc]; const ext = topology[svc].external; const st = state[svc] || "ok";
          return html`<g key=${svc} className=${`node ${st}${ext ? " external" : ""}`} transform=${`translate(${x - 74},${y - 24})`}>
            <rect className="halo" width="148" height="48" rx="8" style=${{ transformOrigin: "74px 24px" }} />
            <rect width="148" height="48" />
            <text x="74" y="22" textAnchor="middle">${svc}</text>
            <text x="74" y="38" textAnchor="middle" className="sub">${ext ? "EXTERNAL" : st === "ok" ? "OK" : st.toUpperCase()}</text></g>`; })}
      </svg></div>`;
  }

  function Annunciator({ invariants, sample, prev }) {
    return html`<div className="pane"><div className="pane-h"><span className="label">Business rules <b>· invariants.yaml</b></span>
        <span className="label">${violated(sample).length ? html`<b style=${{ color: "#F05D4F" }}>${violated(sample).length} violated</b>` : "all holding"}</span></div>
      <div className="annun">${invariants.map(r => { const n = sample ? sample.inv[r.id] : 0; const was = prev ? prev.inv[r.id] : 0;
        return html`<div key=${r.id} className=${"lamp" + (n > 0 ? " red" : "") + (n > 0 && !was ? " flash" : "")} title=${r.description}>
          <span className="code"><span>${r.id.split("_")[0]}</span><span className="num">${n > 0 ? `${n} rows` : "OK"}</span></span>
          <span className="name">${RULE_NAMES[r.id] || r.description}</span></div>`; })}</div></div>`;
  }

  function LogMeter({ totals, window, showSilent }) {
    const [info, warn, err] = totals;
    return html`<div className="pane"><div className="pane-h"><span className="label">Logs <b>· ${window}</b></span></div>
      <div className="logs">
        <div className="meter"><span className="k">INFO</span><span className="v">${info}</span></div>
        <div className="meter"><span className="k">WARN</span><span className="v">${warn}</span></div>
        <div className=${"meter err" + (err === 0 ? " zero" : "")}><span className="k">ERROR</span><span className="v">${err}</span></div>
      </div>
      ${showSilent && err === 0 ? html`<div className="silent"><b>0 ERROR lines.</b> This failure is silent: no alert would fire on logs. Only the business rules see it.</div>` : null}
    </div>`;
  }

  function Wall({ rec, t, step, revealed, from, flowing, showSilent, label }) {
    if (!rec) return null;
    const s = sampleAt(rec.samples, t);
    const idx = rec.samples.indexOf(s);
    const prev = idx > 0 ? rec.samples[idx - 1] : null;
    const st = mapState(rec, t, step, revealed);
    const slow = violated(s).length > 0 || (s && s.queue > 50);
    return html`<section className="wall" aria-label="Situation wall">
      <${ServiceMap} topology=${rec.topology} state=${st} flowing=${flowing} slow=${slow} />
      <${Annunciator} invariants=${rec.invariants} sample=${s} prev=${prev} />
      <${LogMeter} totals=${logSum(rec.samples, from, t)} window=${label} showSilent=${showSilent} />
    </section>`;
  }

  // ------------------------------------------------------------------ console pieces
  function EvidenceCard({ id, ev, onOpen }) {
    const absent = !ev.call || !onOpen;
    return html`<button className=${"ev" + (ev.call ? "" : " absent")} onClick=${() => ev.call && onOpen(ev.call)} disabled=${!ev.call}>
      <span className="top"><span className="src">${ev.source}</span>${ev.call ? html`<span className="cid">${ev.call}</span>` : null}
        <span className="eid">${id}</span>${ev.call ? html`<span className="open">open result →</span>` : html`<span className="open">an absence: nothing to point at</span>`}</span>
      <span className="claim">${ev.claim}</span></button>`;
  }

  function Hyp({ v, evidence, onOpen }) {
    return html`<div className=${"hyp " + (v.proven ? "ok" : "no")}>
      <div className="h"><span className="svc">${v.service}</span><span className="chip amber">${v.kind}</span></div>
      ${v.mechanism ? html`<p className="mech">${v.mechanism}</p>` : null}
      <div className="cols2">
        <div className="side"><span className="label">Supporting</span>${v.supporting.map(id => evidence[id] ? html`<${EvidenceCard} key=${id} id=${id} ev=${evidence[id]} onOpen=${onOpen} />` : null)}</div>
        <div className="side"><span className="label">Refuting</span>${v.refuting.length ? v.refuting.map(id => evidence[id] ? html`<${EvidenceCard} key=${id} id=${id} ev=${evidence[id]} onOpen=${onOpen} />` : null) : html`<span className="hint">none cited</span>`}</div>
      </div></div>`;
  }

  const has = (v, s) => v.problems.some(p => p.toLowerCase().includes(s));
  function LedgerCheck({ v }) {
    const kindBad = has(v, "needs a recent deploy") || has(v, "is not an external") || has(v, "is external");
    const two = v.sources.length >= 2;
    const outweigh = has(v, "refuting evidence outweighs");
    const off = v.problems.filter(p => p.includes("is not about") || p.includes("another service") || p.includes("does not exist"));
    return html`<div className=${"hyp " + (v.proven ? "ok" : "no")}>
      <div className="h"><span className="svc">${v.service}</span><span className="chip amber">${v.kind}</span>
        <span className=${"big-pill " + (v.proven ? "ok" : "no")}>${v.proven ? "PROVEN" : "REJECTED"}</span></div>
      <div className="checks">
        <div className="check"><span className=${"mark " + (two ? "y" : "n")}>${two ? "✓" : "✗"}</span>
          <span>Evidence from 2 independent sources <span className="sources">${SRC.map(s => html`<span key=${s} className=${"srcbox" + (v.sources.includes(s) ? " on" : "")}>${s}</span>`)}</span></span></div>
        <div className="check"><span className=${"mark " + (kindBad ? "n" : "y")}>${kindBad ? "✗" : "✓"}</span>
          <span>The kind of cause is possible <span className="d">${kindBad ? v.problems.find(p => p.includes("deploy") || p.includes("external")) : "a deploy exists for bad_deploy, external only for external services"}</span></span></div>
        <div className="check"><span className=${"mark " + (outweigh ? "n" : "y")}>${outweigh ? "✗" : "✓"}</span><span>Counter-evidence does not outweigh the support</span></div>
        ${off.length ? html`<div className="check"><span className="mark na">−</span><span>Dropped citations <span className="d">${off.join("; ")}</span></span></div>` : null}
      </div></div>`;
  }

  function DiscCheck({ v }) {
    const deployKind = ["bad_deploy", "config_change"].includes(v.kind);
    const restartKind = ["runtime_fault", "resource_exhaustion"].includes(v.kind);
    const rows = [
      ["Deploy timing", "a deploy that lands after the symptoms began cannot be their cause", deployKind ? (has(v, "timing:") ? "n" : "y") : "na"],
      ["Deploy first", "if the service was deployed before its symptoms, test rollback before restart", restartKind ? (has(v, "deploy first") ? "n" : "y") : "na"],
      ["Dependency graph", "symptoms already explained by an upstream cause are not a second cause", has(v, "discriminating:") ? "n" : "y"],
      ["Fix as experiment", "a fix that ran while the symptoms stayed refutes its cause", has(v, "refuted by experiment") ? "n" : "y"],
    ];
    return html`<div className="hyp"><div className="h"><span className="svc">${v.service}</span><span className="chip amber">${v.kind}</span></div>
      <div className="checks">${rows.map(([n, d, m]) => html`<div key=${n} className="check"><span className=${"mark " + m}>${m === "y" ? "✓" : m === "n" ? "✗" : "n/a"}</span>
        <span>${n} <span className="d">${m === "n" ? v.problems.find(p => p.toLowerCase().includes(n.split(" ")[0].toLowerCase()) || p.includes("discriminating") || p.includes("refuted")) : d}</span></span></div>`)}</div></div>`;
  }

  function RiskLadder({ rec, acts, mode, onDecision, decision }) {
    const tiers = { low: [], medium: [], high: [] };
    for (const [name, a] of Object.entries(rec.actions)) tiers[a.risk].push(name);
    const chosen = new Set(acts.filter(a => a.event === "action").map(a => a.action));
    const refused = acts.filter(a => a.event === "refused");
    const highChosen = acts.filter(a => a.event === "action" && rec.actions[a.action] && rec.actions[a.action].risk === "high");
    const approvals = Object.values(rec.trace).filter(x => x.kind === "approval");
    return html`<div className="stepcard">
      <div className="ladder">${["low", "medium", "high"].map(tier => html`<div key=${tier} className=${"rung " + tier}>
        <span className="tier">${tier}${tier === "high" ? " · approval" : ""}</span>
        <span className="acts">${tiers[tier].map(n => html`<span key=${n} className=${"act" + (chosen.has(n) ? " chosen" : "") + (refused.some(r => r.action === n) ? " refused" : "")}>${n}</span>`)}</span></div>`)}</div>
      ${acts.filter(a => a.event === "action").map((a, k) => html`<div key=${k} className="check"><span className="mark y">→</span>
        <span><span className="mono">${a.action}${a.params && a.params.service ? " " + a.params.service : ""}</span> <span className="d">${a.result && a.result.ok ? "ok" : "failed"}</span></span></div>`)}
      ${refused.map((a, k) => html`<div key=${"r" + k} className="check"><span className="mark n">✗</span><span><span className="mono">${a.action}</span> refused: <span className="d">${a.reason}</span></span></div>`)}
      ${!acts.length ? html`<p className="hint">No action this round: nothing passed the evidence gate.</p>` : null}
      ${highChosen.length ? html`<div className="approve"><span className="kicker red">Human approval required</span>
        <p><span className="mono">${highChosen[0].action}</span> touches money. Raqib asks before it runs.</p>
        ${decision == null ? html`<div className="actions"><button className="btn go" onClick=${() => onDecision(true)}>Approve</button><button className="btn danger" onClick=${() => onDecision(false)}>Deny</button></div>`
          : html`<p className="hint">You chose <b>${decision ? "approve" : "deny"}</b>. ${mode === "replay"
            ? `In this recorded run the reviewer ${approvals.length && approvals[0].approved ? "approved" : "denied"} it, so the run continues that way. Use Live mode to make the call for real.`
            : `Your decision was sent to the agent during the run.`}</p>`}</div>` : null}
    </div>`;
  }

  function Drawer({ call, rec, onClose }) {
    const c = rec.trace[call];
    useEffect(() => { const k = e => e.key === "Escape" && onClose(); window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, []);
    if (!c) return null;
    const cols = [...new Set((c.preview || []).flatMap(r => Object.keys(r)))];
    return html`<div className="drawer-bg" onClick=${onClose}><aside className="drawer" role="dialog" aria-label=${`Tool call ${call}`} onClick=${e => e.stopPropagation()}>
      <div className="pane-h"><span className="kicker amber">Original tool result · ${call}</span><button className="btn" onClick=${onClose}>Close</button></div>
      <h3 className="mono">env.${c.name}(${Object.entries(c.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})</h3>
      <dl className="kv"><dt>Called at</dt><dd>t=${c.ts}s</dd><dt>Rows returned</dt><dd>${c.rows}</dd><dt>Rows matching the citation</dt><dd>${c.matched}</dd></dl>
      <p className="hint">The harness checks every citation against this stored result. Shown: ${c.matched ? "the matching rows" : "the first rows"}.</p>
      <div className="tablewrap"><table><thead><tr>${cols.map(k => html`<th key=${k}>${k}</th>`)}</tr></thead>
        <tbody>${(c.preview || []).map((r, i) => html`<tr key=${i}>${cols.map(k => html`<td key=${k}>${r[k] == null ? "" : typeof r[k] === "object" ? JSON.stringify(r[k]) : String(r[k])}</td>`)}</tr>`)}</tbody></table></div>
    </aside></div>`;
  }

  function StepView({ step, rec, mode, onOpen, decisions, setDecision }) {
    const e = step.e;
    switch (step.type) {
      case "detect": return html`<div className="stepcard"><span className="kicker red">Detect</span>
        <p>The shared Monitoring Engine fired. Raqib declares an incident and starts collecting evidence.</p>
        <div className="chips">${e.signals.map(s => html`<span key=${s} className="chip red">${s}</span>`)}</div></div>`;
      case "investigate": return html`<div className="stepcard"><span className="kicker amber">Reasoner asks for evidence</span>
        ${e.requests.map((q, k) => html`<div key=${k} className="stepcard"><div className="query">${q.tool === "sql" ? q.query : `logs(service=${q.service || "any"}${q.level ? ", level=" + q.level : ""}${q.contains ? `, contains="${q.contains}"` : ""})`}</div>
          ${q.why ? html`<p className="why">${q.why}</p>` : null}</div>`)}
        <p className="hint">Read-only. Code runs the query and writes the result into the Evidence Ledger.</p></div>`;
      case "revision": return html`<div className="stepcard"><span className="kicker red">Evidence check rejected a hypothesis</span>
        ${e.rejected.map((v, k) => html`<div key=${k} className="hyp no"><div className="h"><span className="svc">${v.service}</span><span className="chip amber">${v.kind}</span></div>
          <div className="checks">${v.problems.map((p, j) => html`<div key=${j} className="check"><span className="mark n">✗</span><span>${p}</span></div>`)}</div></div>`)}
        <p className="hint">The reasons go back to the model once. It may cite more evidence, change the service or kind, or drop it.</p></div>`;
      case "evidence": return html`<div className="stepcard"><span className="kicker amber">Evidence · every item points to a real tool call</span>
        <div className="evgrid">${Object.entries(e.evidence).map(([id, ev]) => html`<${EvidenceCard} key=${id} id=${id} ev=${ev} onOpen=${onOpen} />`)}</div></div>`;
      case "hypotheses": return html`<div className="stepcard"><span className="kicker amber">Hypotheses · round ${e.round}</span>
        ${e.summary ? html`<p className="lede">${e.summary}</p>` : null}
        ${e.verdicts.map((v, k) => html`<${Hyp} key=${k} v=${v} evidence=${e.evidence} onOpen=${onOpen} />`)}</div>`;
      case "ledger": return html`<div className="stepcard"><span className="kicker">Evidence Ledger gate</span>
        <p className="hint">No fix unless the cause has evidence from at least two independent sources.</p>
        ${e.verdicts.map((v, k) => html`<${LedgerCheck} key=${k} v=${v} />`)}</div>`;
      case "discriminating": return html`<div className="stepcard"><span className="kicker">Discriminating tests</span>
        <p className="hint">Checks in code that tell competing causes apart. They hold even when the model is wrong.</p>
        ${e.verdicts.map((v, k) => html`<${DiscCheck} key=${k} v=${v} />`)}</div>`;
      case "risk": return html`<div><span className="kicker">Least-risk action</span>
        <${RiskLadder} rec=${rec} acts=${step.acts} mode=${mode} decision=${decisions[step.t]} onDecision=${d => setDecision(step.t, d)} /></div>`;
      case "repair": return html`<div className="stepcard"><span className="kicker">Repair the damage</span>
        ${step.acts.map((a, k) => html`<div key=${k} className="check"><span className="mark y">→</span><span className="mono">${a.action}${a.params && a.params.service ? " " + a.params.service : ""}</span></div>`)}
        <p className="hint">Idempotent data repairs, tied to the business rule they restore.</p></div>`;
      case "experiment": return html`<div className="stepcard"><span className="kicker red">The fix was an experiment</span>
        ${step.group.map((g, k) => html`<div key=${k} className="check"><span className="mark n">!</span><span>${
          g.event === "recurring_damage" ? `Damage came back after the fix: ${g.invariants.join(", ")}. Reopen instead of masking it.`
          : g.event === "reopened" ? `Symptoms persisted: ${g.signals.join(", ")}. Incident reopened.`
          : g.event === "refuted" ? `Refuted: ${g.service} (${g.kind}). ${g.reason}`
          : `Ruled out: ${g.service}. ${(g.persisting || []).join(", ")} persisted.`}</span></div>`)}</div>`;
      case "stable": return html`<div className="stepcard"><span className="kicker">Stable</span><p>Every rule and metric check stayed clean for ${e.window} s.</p></div>`;
      case "escalate": return html`<div className="stepcard"><span className="kicker amber">Handed over to a human</span><p>${e.reason}</p></div>`;
      case "report": return html`<div className="stepcard"><span className="kicker">Final report</span>
        <dl className="kv"><dt>Root causes</dt><dd>${e.root_causes.map(c => `${c.service} (${c.kind})`).join(", ") || "none proven"}</dd>
          <dt>Cited evidence</dt><dd>${e.evidence.slice(0, 8).join(" ")}${e.evidence.length > 8 ? " …" : ""}</dd>
          <dt>Claim</dt><dd style=${{ color: e.recovered ? "#19C3A6" : "#F2A93B" }}>recovered: ${String(e.recovered)}</dd></dl>
        <p className="hint">${e.reason}</p></div>`;
      default: return null;
    }
  }
  const STEP_TITLE = { detect: "Detect", investigate: "Investigate", revision: "Revise", evidence: "Evidence", hypotheses: "Hypotheses",
    ledger: "Ledger check", discriminating: "Discriminating test", risk: "Risk ladder", repair: "Repair", experiment: "Experiment",
    stable: "Stable", escalate: "Escalate", report: "Report" };

  // ------------------------------------------------------------------ stages
  function Healthy({ onBreak }) {
    return html`<div className="pane"><span className="kicker">Stage 1 · Healthy</span>
      <h2 className="title">A procure-to-pay system, running<span className="ar">نظام يعمل بشكل سليم</span></h2>
      <p className="lede">Orders come in, a worker extracts invoices and charges payments, suppliers approve, and a sync job copies everything to the ledger. Seven business rules watch that the system is doing its job. Right now every rule holds.</p>
      <div className="actions"><button className="btn danger big" id="btn-break" onClick=${onBreak}>Break it</button></div>
      <p className="hint">The fault you pick stays hidden, from Raqib and from you, until the end.</p></div>`;
  }

  function Choose({ cards, onPick, onSurprise, mode, busy }) {
    return html`<div className="pane"><span className="kicker red">Stage 2 · Choose a fault</span>
      <h2 className="title">Pick a card<span className="ar">اختر بطاقة</span></h2>
      <p className="lede">${mode === "live" ? "Each card is a new incident composed from the held-out fault mechanisms. Raqib runs on it for real." :
        "Each card is a recorded real run of the frozen agent. One to three faults can hide behind a card, with decoy deploys and injected text."}</p>
      <div className="deck">${cards.map((c, k) => html`<button key=${k} className="fcard" disabled=${busy} onClick=${() => onPick(k)} aria-label=${`Fault card ${k + 1}`}>
        <span className="q">?</span><span className="l">FAULT ${String.fromCharCode(65 + k)}</span></button>`)}</div>
      <div className="actions"><button className="btn amber" id="btn-surprise" disabled=${busy} onClick=${onSurprise}>Surprise me</button>
        ${mode === "replay" ? html`<span className="hint">Surprise me picks a solved or a failed run at random.</span>` : null}</div></div>`;
  }

  function Symptoms({ rec, t, done, onWake }) {
    const since = t - rec.meta.onset;
    const tot = logSum(rec.samples, rec.meta.onset, t);
    return html`<div className="pane"><span className="kicker red">Stage 3 · Symptoms</span>
      <h2 className="title">Something is wrong<span className="ar">شيء ما خطأ</span></h2>
      <div className="pane-h"><span className="label">Time since the hidden fault began</span><span className="clock">${fmtT(since)}</span></div>
      <p className="lede">${violated(sampleAt(rec.samples, t)).length ? "Business rules are turning red." : "No rule has fired yet."} ${tot[2] === 0 ? "The logs show no ERROR line." : `${tot[2]} ERROR lines so far.`}</p>
      <div className="actions"><button className="btn go big" id="btn-wake" disabled=${!done} onClick=${onWake}>Wake Raqib</button>
        ${!done ? html`<span className="hint">watching the system break…</span>` : null}</div></div>`;
  }

  function Investigate({ rec, steps, at, setAt, mode, onOpen, decisions, setDecision, onVerify }) {
    const [auto, setAuto] = useState(false);
    useEffect(() => {
      if (!auto) return;
      if (at >= steps.length - 1) { setAuto(false); return; }
      const step = steps[at];
      const needs = step.type === "risk" && step.acts.some(a => a.event === "action" && rec.actions[a.action] && rec.actions[a.action].risk === "high") && decisions[step.t] == null;
      if (needs) { setAuto(false); return; }
      const id = setTimeout(() => setAt(at + 1), 2600);
      return () => clearTimeout(id);
    }, [auto, at]);
    const step = steps[at];
    const last = at >= steps.length - 1;
    return html`<div className="pane"><span className="kicker">Stage 4 · Investigation</span>
      <div className="pane-h"><h2 className="title">${STEP_TITLE[step.type]}<span className="ar mono">step ${at + 1} / ${steps.length}</span></h2>
        <span className="clock">${fmtT(step.t - rec.meta.onset)}</span></div>
      <div className="stepbar"><button className="btn" disabled=${at === 0} onClick=${() => { setAuto(false); setAt(at - 1); }}>Back</button>
        <button className="btn go" id="btn-next" disabled=${last} onClick=${() => { setAuto(false); setAt(at + 1); }}>Next step</button>
        <button className="btn" id="btn-auto" disabled=${last} onClick=${() => setAuto(!auto)}>${auto ? "Pause" : "Auto-play"}</button>
        <button className="btn" id="btn-end" disabled=${last} onClick=${() => { setAuto(false); setAt(steps.length - 1); }}>Skip to end</button>
        <div className="progress" aria-hidden="true"><i style=${{ width: `${100 * (at + 1) / steps.length}%` }}></i></div></div>
      <${StepView} key=${at} step=${step} rec=${rec} mode=${mode} onOpen=${onOpen} decisions=${decisions} setDecision=${setDecision} />
      ${last ? html`<div className="actions"><button className="btn go big" id="btn-verify" onClick=${onVerify}>Verify recovery</button></div>` : null}</div>`;
  }

  function Verify({ rec, onScore }) {
    const r = rec.result;
    const acts = rec.timeline.filter(e => e.event === "action");
    const fromT = acts.length ? acts[acts.length - 1].t : rec.meta.onset;
    const after = rec.samples.filter(s => s.t > fromT);
    let clean = 0;
    for (const s of after) { if (violated(s).length === 0) clean += 10; else clean = 0; }
    const goal = 330;
    const [shown, setShown] = useState(0);
    const [revealed, setRevealed] = useState(false);
    const [rules, setRules] = useState(false);
    useEffect(() => { let v = 0; const id = setInterval(() => { v = Math.min(clean, v + 10); setShown(v); if (v >= clean) clearInterval(id); }, 40); return () => clearInterval(id); }, [rec]);
    const diag = r.diagnosis;
    const truth = rec.scenario.truth;
    const rb = rec.rules;
    return html`<div className="pane"><span className="kicker">Stage 5 · Verify and reveal</span>
      <h2 className="title">Did it really recover?<span className="ar">هل تعافى فعلاً؟</span></h2>
      <div className="stab"><span className="label">Consecutive clean time after the last action</span>
        <span className="big">${shown}<small> s</small></span>
        <div className=${"bar" + (clean < goal ? " bad" : "")}><i style=${{ width: `${Math.min(100, 100 * shown / goal)}%` }}></i></div>
        <span className="hint">Raqib's own watch window is ${goal} s (the slowest rule's horizon + 30 s). The harness then checks 300 s more on its own.</span></div>
      <dl className="kv"><dt>Raqib's claim</dt><dd>recovered: ${String(r.recovery_claim)}</dd>
        <dt>Harness stability check</dt><dd style=${{ color: r.stable ? "#19C3A6" : "#F05D4F" }}>${r.stable ? "stable" : "not stable"}</dd>
        <dt>Claim matches the harness</dt><dd>${r.false_recovery_claim ? "no: false recovery claim" : "yes"}</dd>
        <dt>Evidence that checks out</dt><dd>${r.evidence_valid} / ${r.evidence_cited}</dd>
        <dt>Harmful actions</dt><dd>${r.harmful_actions}</dd><dt>Model cost</dt><dd>$${(r.cost_usd || 0).toFixed(4)}</dd></dl>
      <div className=${"flip" + (revealed ? " on" : "")}><div className="flip-inner">
        <div className="face front"><span className="q mono" style=${{ fontSize: "2.8rem", color: "#F05D4F" }}>?</span><span className="label">The hidden fault</span></div>
        <div className="face back"><span className="kicker red">The truth</span>
          <div className="truth">${rec.scenario.faults.map((f, k) => { const hit = diag.some(d => d.service === f.service && (truth.find(t => t.service === f.service) || { kinds: [] }).kinds.includes(d.kind));
            return html`<div key=${k} className="trow"><span className=${"mark " + (hit ? "y" : "n")}>${hit ? "✓" : "✗"}</span>
              <span><b>${MECHANISMS[f.mechanism] || f.mechanism}</b> in <span className="mono">${f.service}</span> <span className="d hint">(${ORIGINS[f.origin] || f.origin})</span></span></div>`; })}
            ${diag.filter(d => !truth.some(t => t.service === d.service)).map((d, k) => html`<div key=${"w" + k} className="trow"><span className="mark n">✗</span><span>Blamed <span className="mono">${d.service}</span>, which was not a cause</span></div>`)}
            ${rec.scenario.deploys.filter(d => !rec.scenario.faults.some(f => f.service === d.service && f.origin !== "external")).length ? html`<p className="hint">Decoy deploys: ${rec.scenario.deploys.filter(d => !rec.scenario.faults.some(f => f.service === d.service)).map(d => d.service).join(", ")}</p>` : null}
            ${rec.scenario.injected_text && rec.scenario.injected_text.length ? html`<p className="hint">Injected into invoice data and ignored: “${rec.scenario.injected_text[0]}”</p>` : null}
            <p className="hint">Raqib's diagnosis: ${diag.map(d => `${d.service} (${d.kind})`).join(", ") || "none"}</p></div>
          <div className="verdict"><span className=${"big-pill " + (r.success ? "ok" : "no")}>${r.success ? "SOLVED" : "NOT SOLVED"}</span>
            <span className="hint">${rec.scenario.fixable ? "fixable from inside" : "outside cause: the right move is to escalate"}</span></div></div>
      </div></div>
      <div className="actions">${!revealed ? html`<button className="btn danger big" id="btn-reveal" onClick=${() => setRevealed(true)}>Reveal the truth</button>`
        : html`<button className="btn amber" id="btn-rules" onClick=${() => setRules(true)} disabled=${rules || !rb}>What would rules do?</button>
               <button className="btn go" id="btn-score" onClick=${onScore}>Scoreboard</button>`}</div>
      ${rules && rb ? html`<div className=${"hyp " + (rb.success ? "ok" : "no")}><span className="kicker amber">Rule-based runbooks, same incident, same seed</span>
        <dl className="kv"><dt>Solved</dt><dd>${rb.success ? "yes" : "no"}</dd><dt>Diagnosis</dt><dd>${rb.diagnosis.map(d => d.service).join(", ") || "none"}</dd>
          <dt>Actions</dt><dd>${rb.actions.map(a => a.action + (a.params.service ? " " + a.params.service : "")).slice(0, 4).join(", ") || "none"}</dd>
          <dt>Claimed recovery</dt><dd>${String(rb.recovery_claim)}</dd><dt>That claim was false</dt><dd style=${{ color: rb.false_recovery_claim ? "#F05D4F" : "inherit" }}>${rb.false_recovery_claim ? "yes" : "no"}</dd></dl></div>` : null}
    </div>`;
  }

  function Duel({ q, a, b, unit, note, invert }) {
    const max = Math.max(a.v, b.v, 1);
    return html`<div className="tile"><span className="q">${q}</span>
      <div className="duel"><span className="who">Raqib</span><span className="track"><i className="r" style=${{ width: `${100 * a.v / max}%` }}></i></span><span className="val r">${a.v}${unit}</span>
        <span className="who">${b.who}</span><span className="track"><i style=${{ width: `${100 * b.v / max}%` }}></i></span><span className="val">${b.v}${unit}</span></div>
      ${note ? html`<span className="note">${note}</span>` : null}</div>`;
  }

  function Score({ board, onAgain }) {
    return html`<div className="pane"><span className="kicker">Stage 6 · Scoreboard</span>
      <h2 className="title">The whole benchmark<span className="ar">النتائج الكاملة</span></h2>
      <p className="lede">Sealed held-out sets were hashed and committed before each agent version existed, and run once with --final. Success means right cause, no harm, the right escalation call, and a passed stability check.</p>
      <div className="score">${board.tiles.map((t, k) => html`<${Duel} key=${k} ...${t} />`)}</div>
      <div className="actions"><button className="btn go" id="btn-again" onClick=${onAgain}>Break it again</button></div></div>`;
  }

  // ------------------------------------------------------------------ app
  function App() {
    const [mode, setMode] = useState("replay");
    const [liveOk, setLiveOk] = useState(false);
    const [lib, setLib] = useState(null);
    const [board, setBoard] = useState(null);
    const [stage, setStage] = useState("healthy");
    const [cards, setCards] = useState([]);
    const [rec, setRec] = useState(null);
    const [t, setT] = useState(0);
    const [symDone, setSymDone] = useState(false);
    const [at, setAt] = useState(0);
    const [decisions, setDecisions] = useState({});
    const [drawer, setDrawer] = useState(null);
    const [busy, setBusy] = useState(null);
    const [error, setError] = useState(null);
    const [pending, setPending] = useState(null);

    useEffect(() => {
      Promise.all([fetchJSON("data/library.json"), fetchJSON("data/scoreboard.json")])
        .then(([l, b]) => { setLib(l); setBoard(b); }).catch(e => setError(String(e)));
      if (!["localhost", "127.0.0.1"].includes(location.hostname)) return;   // Live mode needs the local server
      const ctl = new AbortController(); setTimeout(() => ctl.abort(), 1500);
      fetch("/api/health", { signal: ctl.signal }).then(r => r.ok && r.json()).then(j => { if (j && j.live) setLiveOk(true); }).catch(() => {});
    }, []);

    const steps = useMemo(() => rec ? buildSteps(rec) : [], [rec]);
    const healthyRec = board ? board.healthy : null;

    // healthy wall: loop the recorded healthy period
    useEffect(() => {
      if (stage !== "healthy" && stage !== "choose") return;
      if (!healthyRec) return;
      const s = healthyRec.samples; let k = 0;
      const id = setInterval(() => { k = (k + 1) % s.length; setT(s[k].t); }, 700);
      return () => clearInterval(id);
    }, [stage, healthyRec]);

    // symptoms: replay samples from onset to detection
    useEffect(() => {
      if (stage !== "symptoms" || !rec) return;
      const inc = rec.timeline.find(e => e.event === "incident");
      const end = inc ? inc.t : rec.meta.onset + 120;
      let cur = rec.meta.onset - 20; setT(cur); setSymDone(false);
      const stepT = Math.max(10, Math.round((end - cur) / 40));
      const id = setInterval(() => { cur = Math.min(end, cur + stepT); setT(cur); if (cur >= end) { clearInterval(id); setSymDone(true); } }, 180);
      return () => clearInterval(id);
    }, [stage, rec]);

    useEffect(() => { if (stage === "investigate" && steps[at]) setT(steps[at].t); }, [stage, at, steps]);
    useEffect(() => { if (stage === "verify" && rec) setT(rec.samples[rec.samples.length - 1].t); }, [stage, rec]);

    const dealCards = () => setCards(mode === "live" ? [0, 1, 2, 3, 4, 5] : shuffle(lib).slice(0, 6));
    const begin = r => { setRec(r); setAt(0); setDecisions({}); setStage("symptoms"); };

    const loadReplay = entry => { setBusy("loading"); fetchJSON(`data/${entry.file}`).then(r => { setBusy(null); begin(r); }).catch(e => { setBusy(null); setError(String(e)); }); };
    const runLive = card => {
      setBusy("Composing a new incident and running Raqib for real. This takes a few minutes…");
      fetchJSON("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ card }) }).then(({ id }) => {
        const poll = () => fetchJSON(`/api/runs/${id}`).then(s => {
          setPending(s.pending ? { id, ...s.pending } : null);
          if (s.status === "done") { setBusy(null); begin(s.record); }
          else if (s.status === "error") { setBusy(null); setError(s.error); }
          else { setBusy(`Raqib is working · simulated t=${s.progress.t || 0}s · ${s.progress.events || 0} tool calls so far`); setTimeout(poll, 1500); }
        }).catch(e => { setBusy(null); setError(String(e)); });
        poll();
      }).catch(e => { setBusy(null); setError(String(e)); });
    };
    const decideLive = ok => fetchJSON(`/api/runs/${pending.id}/approval`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approve: ok }) }).then(() => setPending(null)).catch(e => setError(String(e)));

    const onPick = k => mode === "live" ? runLive(k) : loadReplay(cards[k]);
    const onSurprise = () => {
      if (mode === "live") return runLive(null);
      const want = Math.random() < 0.5;
      const pool = lib.filter(x => x.success === want);
      loadReplay(pick(pool.length ? pool : lib));
    };

    if (error) return html`<div className="room"><div className="error">Could not load the demo: ${error}</div></div>`;
    if (!lib || !board) return html`<div className="boot"><span className="wordmark-ar">رقيب</span><span className="boot-line">Loading the control room…</span></div>`;

    const wallRec = rec && !["healthy", "choose"].includes(stage) ? rec : healthyRec;
    const stageIdx = STAGES.findIndex(s => s[0] === stage);
    const step = stage === "investigate" ? steps[at] : null;
    const from = stage === "symptoms" ? rec.meta.onset : t - 60;

    return html`<div className="room">
      <header className="topbar">
        <div className="brand"><span className="wordmark-ar" lang="ar">رقيب</span>
          <span className="brand-en"><b>RAQIB</b><span>EVIDENCE-BASED RECOVERY · HUMAIN CHALLENGE 1</span></span></div>
        <div className="badges">
          ${rec && !["healthy", "choose"].includes(stage) ? (rec.meta.mode === "live"
            ? html`<span className="badge live">Live run · ${rec.meta.model}</span>`
            : html`<span className="badge rec">Recorded real run · seed ${rec.meta.seed} · ${rec.meta.split}</span>`) : null}
          <span className="modes" role="group" aria-label="Mode">
            <button aria-pressed=${mode === "replay"} onClick=${() => { setMode("replay"); setStage("healthy"); }}>Replay</button>
            <button aria-pressed=${mode === "live"} disabled=${!liveOk} title=${liveOk ? "Run the agent for real" : "Live mode needs the local server: python demo/server.py"}
              onClick=${() => { setMode("live"); setStage("healthy"); }}>Live</button></span>
        </div>
      </header>
      <ol className="rail">${STAGES.map(([id, en, ar], k) => html`<li key=${id} className=${k < stageIdx ? "done" : k === stageIdx ? "now" : ""}>
        <span className="n">0${k + 1}</span><span className="t">${en}</span><span className="ar" lang="ar">${ar}</span></li>`)}</ol>
      <main className="floor">
        <${Wall} rec=${wallRec} t=${t} step=${step} revealed=${stage === "verify" || stage === "score"} from=${from}
          flowing=${true} showSilent=${stage === "symptoms" || stage === "investigate"}
          label=${stage === "symptoms" ? "since the fault began" : "last 60 s"} />
        <section className="console" aria-label="Operator console">
          ${stage === "healthy" ? html`<${Healthy} onBreak=${() => { dealCards(); setStage("choose"); }} />` : null}
          ${stage === "choose" ? html`<${Choose} cards=${cards} mode=${mode} busy=${!!busy} onPick=${onPick} onSurprise=${onSurprise} />` : null}
          ${busy && stage === "choose" ? html`<div className="pane"><span className="kicker amber">Working</span><p className="lede">${busy === "loading" ? "Loading the recorded run…" : busy}</p>
            ${pending ? html`<div className="approve"><span className="kicker red">Raqib asks for approval</span>
              <p><span className="mono">${pending.action}</span> is a high-risk action. Your decision goes to the agent now.</p>
              <div className="actions"><button className="btn go" onClick=${() => decideLive(true)}>Approve</button><button className="btn danger" onClick=${() => decideLive(false)}>Deny</button></div></div>` : null}</div>` : null}
          ${stage === "symptoms" ? html`<${Symptoms} rec=${rec} t=${t} done=${symDone} onWake=${() => setStage("investigate")} />` : null}
          ${stage === "investigate" ? html`<${Investigate} rec=${rec} steps=${steps} at=${at} setAt=${setAt} mode=${rec.meta.mode}
              onOpen=${setDrawer} decisions=${decisions} setDecision=${(k, d) => setDecisions({ ...decisions, [k]: d })} onVerify=${() => setStage("verify")} />` : null}
          ${stage === "verify" ? html`<${Verify} rec=${rec} onScore=${() => setStage("score")} />` : null}
          ${stage === "score" ? html`<${Score} board=${board} onAgain=${() => { setRec(null); setStage("healthy"); }} />` : null}
        </section>
      </main>
      <footer className="foot"><span>Replay runs are real benchmark runs of the frozen agent, replayed from the committed model cache.</span>
        <a href="https://github.com/HareerFN/raqib">github.com/HareerFN/raqib</a></footer>
      ${drawer && rec ? html`<${Drawer} call=${drawer} rec=${rec} onClose=${() => setDrawer(null)} />` : null}
    </div>`;
  }

  ReactDOM.createRoot(document.getElementById("app")).render(html`<${App} />`);
})();
