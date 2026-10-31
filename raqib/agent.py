"""Raqib: the evidence-based repair agent.

One incident, handled in rounds:
  detect (shared Monitor)
  -> collect evidence into the ledger (code)
  -> reason: the LLM may ask for more evidence, then proposes hypotheses citing evidence ids
  -> verify: the ledger accepts a hypothesis only with evidence from >= 2 independent sources
  -> act on proven causes only: remove the cause, then repair the damage
  -> watch: re-run data repairs while in-flight work lands, and wait out a stability window
  -> stable: finish. New or lasting symptoms: next round. Weak evidence or external cause: escalate.
Every incident ends with a structured report: root causes, cited evidence (Env call ids),
actions taken, and an explicit recovered: true|false claim.

The LLM reasons; code decides what is allowed to happen.
"""

import re

from harness.env import BudgetExhausted

from .evidence import Collector
from .invariants import RuleEngine
from .ledger import Ledger, dependents
from .llm import LLMClient
from .monitor import Monitor
from .reasoner import Reasoner

CHECK_EVERY = 30          # seconds between checks while watching a fix
MAX_ROUNDS = 5            # attempt budget: diagnosis rounds per incident
MAX_UNPROVEN = 2          # rounds without a proven cause before handing over to a human
MAX_INVESTIGATIONS = 2    # times the reasoner may ask for more evidence per round
MAX_REPAIRS = 4           # runs of one data-repair action per incident
REPAIRS_PER_WATCH = 1     # data repairs per invariant while watching one fix, before damage counts as recurring
MAX_REVISIONS = 1         # times per round the reasoner may revise hypotheses the evidence check rejected
ESCALATE_KINDS = ("external_dependency", "bad_input")
CAUSE_ACTIONS = {"rollback_service": ("bad_deploy", "config_change"),
                 "restart_service": ("runtime_fault", "resource_exhaustion")}


def stability_window() -> int:
    """Watch a fix for as long as the slowest invariant needs to show a problem, plus one check."""
    horizons = [int(n) for r in RuleEngine().rules for n in re.findall(r":now\s*-\s*(\d+)", r["query"])]
    return max(horizons, default=120) + CHECK_EVERY


class RaqibAgent:
    """The flags exist for ablations; the full agent uses the defaults."""

    def __init__(self, llm=None, gate: bool = True, discriminate: bool = True, use_topology: bool = True,
                 use_invariants: bool = True, name: str = "raqib", **_):
        self.name = name
        self.llm = llm or LLMClient("strong")
        self.reasoner = Reasoner(self.llm, use_topology=use_topology)
        self.gate, self.discriminate = gate, discriminate
        self.use_topology, self.use_invariants = use_topology, use_invariants
        self.investigations = MAX_INVESTIGATIONS if discriminate else 0
        self.timeline: list[dict] = []     # every decision, for the report and the demo

    @property
    def cost_usd(self):
        return self.llm.cost_usd

    @property
    def llm_calls(self):
        return self.llm.calls

    # ================================================================ main loop
    def run(self, env):
        self.env = env
        self.watch_start = env.now()
        self.monitor = Monitor(env, use_invariants=self.use_invariants)
        self.collector = Collector(env, self.watch_start, use_topology=self.use_topology,
                                   use_invariants=self.use_invariants)
        self.approved: set[str] = set()
        self.cited: dict[str, dict] = {}           # citations for the report, keyed by claim
        self.stable_for = stability_window()
        self.causes: dict[str, dict] = {}          # service -> {"service", "kind"}: the diagnosis
        self.history: list[str] = []               # what Raqib did, fed back to the reasoner
        self.cause_actions: dict[tuple, int] = {}  # (action, service) -> when it ran
        self.explains: dict[str, set] = {}         # cause service -> signal ids it was meant to explain
        self.refuted: dict[tuple, str] = {}        # (service, kind) -> why an experiment ruled it out
        self.external: dict[str, set] = {}         # proven outside cause -> signal ids it explains
        self.last_fixed: list[tuple] = []          # (service, kind) whose cause action ran in the last round
        self.repairs: dict[str, list[str]] = {}    # data-repair action -> invariants it restores
        self.repair_runs: dict[str, int] = {}
        self.best: dict | None = None              # strongest hypothesis seen, for escalation reports
        self.schema = self._schema()
        try:
            signals = self._wait_for_incident()
            detected_at = env.now()
            env.declare_incident("; ".join(s.id for s in signals))
            self._note("incident", signals=[s.id for s in signals])
            unproven = 0
            for round_no in range(1, MAX_ROUNDS + 1):
                outcome = self._round(round_no, signals, detected_at)
                if outcome == "escalated":
                    return
                if outcome in ("unproven", "external_pending"):
                    unproven += 1
                    if unproven >= MAX_UNPROVEN:
                        return self._escalate("no cause could be proven with evidence from two sources"
                                              if not self.external else "remaining symptoms have no proven cause")
                    env.wait(60)                   # let more evidence accumulate, then look again
                    signals = self._unexplained(self.monitor.check()) or signals
                    if self.external and not self._unexplained(signals):
                        return self._escalate_external()
                    continue
                signals = self._watch()
                if signals is None:
                    self._note("stable", window=self.stable_for)
                    self._finish(True, f"stable for {self.stable_for} s: every check clean")
                    return
                self._note("reopened", signals=[s.id for s in signals])
                self._learn_from_fix(signals)
                if self.external:
                    signals = self._unexplained(signals)
                    if not signals:                # what is left is the outside cause: hand over now
                        return self._escalate_external()
            self._escalate(f"symptoms remain after {MAX_ROUNDS} rounds")
        except BudgetExhausted:
            if not env.done:
                if self.external:
                    self._escalate_external()
                else:
                    self._finish(False, "time budget used up before recovery was verified")

    def _wait_for_incident(self):
        while True:
            self.env.wait(10)
            signals = self.monitor.check()
            if signals:
                return signals

    # ================================================================ one diagnosis round
    def _round(self, round_no: int, signals, detected_at: int) -> str:
        env = self.env
        ledger = Ledger(env.topology(), gate=self.gate, discriminate=self.discriminate,
                        use_topology=self.use_topology)
        self.collector.collect(ledger, signals, detected_at)
        messages = self.reasoner.start(self._context(ledger, signals, round_no))
        for i in range(self.investigations + 1):
            out = self.reasoner.ask(messages)
            if i == self.investigations or not out["investigate"]:
                break
            before = len(ledger.items)
            notes = [n for n in (self.collector.investigate(ledger, r) for r in out["investigate"]) if n]
            new = list(ledger.items.values())[before:]
            self._note("investigated", requests=out["investigate"], evidence=[e.line() for e in new], notes=notes)
            left = self.investigations - i - 1
            messages.append({"role": "user", "content": "\n".join(
                ["NEW EVIDENCE"] + [e.line() for e in new] + [f"- {n}" for n in notes]
                + ["", f"Investigation allowed: {'yes, ' + str(left) + ' more time(s)' if left else 'no'}. "
                   + ("Investigate further or give your hypotheses." if left else "Give your final hypotheses.")])})

        verdicts = self._assess(ledger, out["hypotheses"])
        # verifier feedback: tell the reasoner why code rejected a hypothesis, let it revise once
        for _ in range(MAX_REVISIONS if self.gate else 0):
            rejected = [(h, v) for h, v in verdicts if not v.proven]
            if not rejected:
                break
            self._note("revision", rejected=[{"service": v.service, "kind": v.kind, "problems": v.problems}
                                             for _, v in rejected])
            messages.append({"role": "user", "content": "\n".join(
                ["EVIDENCE CHECK (code)"]
                + [f"- {v.service} / {v.kind}: {'PASSED' if v.proven else 'REJECTED: ' + '; '.join(v.problems)}"
                   for _, v in verdicts]
                + ["", "Revise. Fix what the check found: cite evidence from another independent source, or "
                   "change the service or kind if the evidence points elsewhere, or drop the hypothesis. "
                   "Keep the hypotheses that passed. Investigation allowed: no. Give your final hypotheses."])})
            revised = self.reasoner.ask(messages)
            if revised.get("parse_error"):
                break
            out = {**revised, "summary": revised.get("summary") or out.get("summary", "")}
            verdicts = self._assess(ledger, out["hypotheses"])
        cited = {i for _, v in verdicts for i in v.supporting + v.refuting}
        self._note("hypotheses", round=round_no, summary=out.get("summary", ""),
                   verdicts=[{**v.as_dict(), "mechanism": h.get("mechanism", "")} for h, v in verdicts],
                   evidence={i: {"source": ledger.items[i].source, "claim": ledger.items[i].claim,
                                 "call": ledger.items[i].call} for i in sorted(cited, key=lambda x: int(x[1:]))})
        for h, v in verdicts:
            if not any("needs a recent deploy" in p or "is not an external" in p or "is external" in p
                       for p in v.problems):
                if self.best is None or len(v.sources) > len(self.best["sources"]):
                    self.best = {"service": v.service, "kind": v.kind, "sources": v.sources,
                                 "mechanism": h.get("mechanism", ""),
                                 "evidence": [ledger.items[i] for i in v.supporting]}

        proven = [(h, v) for h, v in verdicts if v.proven]
        internal = [(h, v) for h, v in proven if v.kind not in ESCALATE_KINDS]
        external = [(h, v) for h, v in proven if v.kind in ESCALATE_KINDS]
        if not proven:
            self.history.append(f"round {round_no}: no hypothesis passed the evidence check")
            return "unproven"

        self._rule_out(signals, {v.service for _, v in proven})
        acted = False
        self.last_fixed = []
        for h, v in internal:
            self.causes[v.service] = {"service": v.service, "kind": v.kind}
            self.explains[v.service] = set(h.get("explains") or [])
            if self._execute(h, v):
                acted = True
                self.last_fixed.append((v.service, v.kind))
        for h, v in external:
            self.causes[v.service] = {"service": v.service, "kind": v.kind}
        self._report(out.get("summary", ""), ledger, [v for _, v in proven])
        acted |= bool(self._repair_damage(self._violated()))
        for h, v in external:
            explains = set(h.get("explains") or [])
            if not explains:                       # fall back to the dependency graph
                reach = {v.service} | dependents(v.service, env.topology())
                explains = {s.id for s in signals if set(s.services) & reach}
            self.external[v.service] = explains
        if external:
            # escalation governor: hand over the outside cause only after everything it does not
            # explain has been handled; other faults can be happening at the same time
            explained = set().union(*self.external.values(), *(self.explains.get(v.service, set())
                                                                for _, v in internal))
            if acted:
                return "acted"
            if {s.id for s in signals} - explained and not internal:
                self.history.append(f"t={env.now()}: outside cause found ({', '.join(self.external)}); "
                                    "other symptoms are still unexplained")
                return "external_pending"
            return self._escalate_external()
        return "acted" if acted else "no_change"

    def _unexplained(self, signals):
        explained = set().union(*self.external.values()) if self.external else set()
        return [s for s in signals or [] if s.id not in explained]

    def _escalate_external(self) -> str:
        return self._escalate("cause is outside the system: "
                              + ", ".join(f"{svc} ({self.causes[svc]['kind']})" for svc in self.external
                                          if svc in self.causes))

    def _assess(self, ledger: Ledger, hypotheses: list[dict]) -> list[tuple]:
        verdicts = [(h, ledger.assess(h)) for h in hypotheses]
        for _, v in verdicts:
            why = self.refuted.get((v.service, v.kind))
            if why and v.proven:
                v.proven = False
                v.problems.append(f"refuted by experiment: {why}")
        ledger.deploy_first([v for _, v in verdicts], set(self.refuted))
        ledger.explained_upstream([v for _, v in verdicts])
        return verdicts

    def _learn_from_fix(self, signals):
        """The fix was an experiment. If the cause it removed was meant to explain symptoms that
        are still there, that (service, kind) is refuted for the rest of the incident."""
        current = {s.id for s in signals}
        fixed_explain = set().union(*(self.explains.get(svc, set()) for svc, _ in self.last_fixed)) \
            if self.last_fixed else set()
        unexplained = current - fixed_explain
        if self.last_fixed and unexplained:
            self.history.append(f"t={self.env.now()}: after fixing {', '.join(s for s, _ in self.last_fixed)}, "
                                f"{', '.join(sorted(unexplained))} remain and are not explained by that fix: "
                                "look for another cause")
        for svc, kind in self.last_fixed:
            persisting = self.explains.get(svc, set()) & current
            if persisting:
                why = (f"{svc} was treated as {kind} at t={self.cause_actions_at(svc)}, "
                       f"but {', '.join(sorted(persisting))} persisted")
                self.refuted[(svc, kind)] = why
                self._note("refuted", service=svc, kind=kind, reason=why)
                self.history.append(f"t={self.env.now()}: experiment refuted {svc} ({kind}): {why}")
        self.last_fixed = []

    def cause_actions_at(self, svc: str):
        return max((t for (_, s), t in self.cause_actions.items() if s == svc), default=None)

    def _rule_out(self, signals, reproven: set[str]):
        """A fix is also an experiment. If the cause was removed, the symptoms it was meant to
        explain are still there, and the new round no longer supports it, it was not the cause."""
        current = {s.id for s in signals}
        fixed = {svc for _, svc in self.cause_actions}
        for svc in list(self.causes):
            persisting = self.explains.get(svc, set()) & current
            if svc in fixed and svc not in reproven and persisting:
                del self.causes[svc]
                self._note("ruled_out", service=svc, persisting=sorted(persisting))
                self.history.append(f"t={self.env.now()}: {svc} ruled out: its fix ran but "
                                    f"{', '.join(sorted(persisting))} persisted")

    # ================================================================ acting
    def _execute(self, h: dict, v) -> bool:
        acted = False
        steps = list(h.get("repair_plan") or [])
        # repair the damage of every invariant this cause explains, even if the model left it out
        for inv in h.get("explains") or []:
            steps += [{"action": a, "params": {}} for a, spec in self.env.actions().items()
                      if inv in (spec.get("repairs") or [])]
        for step in steps:
            name, params = step.get("action"), step.get("params") or {}
            why = self._refuse(v, name, params)
            if why:
                self._note("refused", action=name, params=params, reason=why)
                continue
            spec = self.env.actions()[name]
            if spec.get("repairs"):
                self.repairs[name] = spec["repairs"]   # runs after the cause is removed
                continue
            key = (name, params.get("service"))
            if key in self.cause_actions and not self._deployed_since(params["service"], self.cause_actions[key]):
                continue                                # idempotent: already done, nothing changed since
            if self._do(name, params, f"{v.service} {v.kind}: {h.get('mechanism', '')} "
                                      f"(evidence {', '.join(v.supporting)})"):
                self.cause_actions[key] = self.env.now()
                acted = True
        return acted

    def _refuse(self, v, name, params) -> str | None:
        spec = self.env.actions().get(name)
        if spec is None:
            return "unknown action"
        if set(params) != set(spec["params"]):
            return f"wrong parameters {sorted(params)}, expected {spec['params']}"
        if spec.get("repairs"):
            return None
        if name in CAUSE_ACTIONS:
            if params["service"] != v.service:
                return f"targets {params['service']}, but the proven cause is {v.service}"
            if v.kind not in CAUSE_ACTIONS[name]:
                return f"{name} does not remove a {v.kind}"
            return None
        return "not tied to the proven cause or to a violated invariant"

    def _do(self, name: str, params: dict, justification: str) -> bool:
        env = self.env
        if env.actions()[name]["risk"] == "high" and name not in self.approved:
            if not env.request_approval(name, justification):
                self._note("approval_denied", action=name)
                self.history.append(f"t={env.now()}: approval for {name} was denied")
                return False
            self.approved.add(name)
        result = env.act(name, **params)
        self._note("action", action=name, params=params, result=result)
        self.history.append(f"t={env.now()}: {name} {params or ''} -> {result}")
        return bool(result.get("ok"))

    def _violated(self) -> set[str]:
        return {i["id"] for i in self.env.invariants() if i["violations"]}

    def _repair_damage(self, violated: set[str]) -> set[str]:
        """Run the data repair for each violated invariant whose cause was proven."""
        repaired = set()
        for action, invariants in self.repairs.items():
            hit = violated & set(invariants)
            if hit and self.repair_runs.get(action, 0) < MAX_REPAIRS:
                self.repair_runs[action] = self.repair_runs.get(action, 0) + 1
                if self._do(action, {}, f"repairs {', '.join(sorted(hit))} after the cause was removed"):
                    repaired |= hit
        return repaired

    def _deployed_since(self, service: str, ts: int) -> bool:
        return any(d["service"] == service and d["ts"] > ts and not d["note"].startswith("rollback")
                   for d in self.env.deploys())

    # ================================================================ verifying
    def _watch(self):
        """Watch the fix. Returns lasting symptoms that reopen the incident, or None once stable."""
        quiet, previous = 0, set()
        repaired_here: dict[str, int] = {}       # invariant -> repairs during this watch
        while quiet < self.stable_for:
            self.env.wait(CHECK_EVERY)
            signals = self.monitor.check()
            if not signals:
                quiet += CHECK_EVERY
                previous = set()
                continue
            quiet = 0
            violated = {s.id for s in signals if s.kind == "invariant"}
            # one more repair is fine (work already in flight when the cause was removed); damage
            # that comes back after that means a cause is still active: reopen, do not mask it
            recurring = {i for i in violated if repaired_here.get(i, 0) >= REPAIRS_PER_WATCH}
            if recurring:
                self._note("recurring_damage", invariants=sorted(recurring))
                return signals
            repaired = self._repair_damage(violated - recurring)
            for i in repaired:
                repaired_here[i] = repaired_here.get(i, 0) + 1
            rest = [s for s in signals if s.id not in repaired]
            ids = {s.id for s in rest}
            if ids & previous:                   # the same symptom on two checks in a row
                return rest
            previous = ids
        return None

    # ================================================================ reporting
    def _cite(self, items):
        for e in items:
            c = e.citation()
            if c:
                self.cited.setdefault(c["claim"], c)

    def _report(self, summary: str, ledger: Ledger, verdicts):
        self._cite(ledger.items[eid] for v in verdicts for eid in v.supporting)
        self.env.report_diagnosis(list(self.causes.values()), summary, evidence=list(self.cited.values()))

    def _finish(self, recovered: bool, reason: str):
        report = {"root_causes": list(self.causes.values()),
                  "evidence": [c["id"] for c in self.cited.values()],
                  "actions": list(self.history),
                  "escalated": any(e["event"] == "escalated" for e in self.timeline),
                  "recovered": recovered, "reason": reason}
        self._note("report", **report)
        self.env.finish(recovered=recovered, report=report)

    def _escalate(self, reason: str) -> str:
        if not self.causes and self.best:
            self.causes[self.best["service"]] = {"service": self.best["service"], "kind": self.best["kind"]}
            self._cite(self.best["evidence"])
            self.env.report_diagnosis(list(self.causes.values()), f"suspected: {self.best['mechanism']}",
                                      evidence=list(self.cited.values()))
        lines = [reason, "diagnosis: " + (", ".join(f"{c['service']} ({c['kind']})" for c in self.causes.values())
                                          or "unknown")]
        lines += ["actions taken:"] + (self.history or ["none"])
        self.env.escalate("\n".join(lines))
        self._note("escalated", reason=reason)
        self._finish(False, reason)
        return "escalated"

    def _note(self, event: str, **data):
        self.timeline.append({"t": self.env.now(), "event": event, **data})

    # ================================================================ prompt context
    def _schema(self) -> list[str]:
        writers: dict[str, list[str]] = {}
        for svc, spec in self.env.topology().items():
            for t in spec.get("owns", []):
                writers.setdefault(t, []).append(svc)
        out = []
        for row in self.env.sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
            cols = [c.strip().split()[0] for c in row["sql"].split("(", 1)[1].rsplit(")", 1)[0].split(",")]
            who = ", ".join(writers.get(row["name"], [])) or "system"
            out.append(f"- {row['name']}({', '.join(cols)}) written by {who}")
        return out

    def _context(self, ledger: Ledger, signals, round_no: int) -> str:
        env = self.env
        lines = [f"NOW t={env.now()}s. Incident round {round_no}. Raqib came on watch at t={self.watch_start}; "
                 f"the {300} s before that are the healthy baseline.", "", "CURRENT SIGNALS"]
        lines += [f"- {s.id} ({', '.join(s.services)}): {s.detail}" for s in signals] or ["- none"]
        if self.history:
            lines += ["", "WHAT RAQIB ALREADY DID (earlier rounds)"] + [f"- {h}" for h in self.history]
            lines += ["Do not repeat a fix that already ran unless new evidence shows it did not take effect."]
        if self.refuted:
            lines += ["", "RULED OUT BY EXPERIMENT (code rejects these service/kind pairs)"]
            lines += [f"- {svc} / {kind}: {why}" for (svc, kind), why in self.refuted.items()]
        lines += ["", "EVIDENCE (written by code; cite by id)"] + ledger.render()
        lines += ["", "DATABASE TABLES"] + self.schema
        lines += ["", "ACTIONS (name [risk] params: description)"]
        lines += [f"- {k} [{v['risk']}] {v['params']}: {v['description']}" for k, v in env.actions().items()]
        lines += ["", f"Investigation allowed: yes, up to {MAX_INVESTIGATIONS} time(s). "
                      "Ask for evidence that separates competing explanations, or give your hypotheses now."]
        return "\n".join(lines)
