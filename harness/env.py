"""The environment an agent works in.

Agents get this object and nothing else: read-only evidence tools (invariants,
metrics, logs, deploy history, topology, read-only SQL) and the repair actions.
They never see the fault switches or the scenario's ground truth.

Every call is recorded in `trace` with a stable id (T1, T2, ...). Evidence tools also
store their result, so a diagnosis that cites "T7" can be checked against what T7
really returned. `call(tool, **args)` returns {"id", "result"} for agents that cite.
"""

from pathlib import Path

import yaml

from raqib.invariants import RuleEngine

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = yaml.safe_load((ROOT / "config" / "actions.yaml").read_text(encoding="utf-8"))
TOPOLOGY = yaml.safe_load((ROOT / "config" / "topology.yaml").read_text(encoding="utf-8"))["services"]


class BudgetExhausted(Exception):
    pass


class Env:
    def __init__(self, system, scheduler, budget_seconds: int, approver):
        self._sys = system
        self._scheduler = scheduler          # fires scenario events while time passes
        self._approver = approver            # simulated human reviewer
        self._rules = RuleEngine()
        self.start = system.clock.now
        self.deadline = self.start + budget_seconds
        self.trace: list[dict] = []          # every tool call, for scoring and the demo timeline
        self.incident_at: int | None = None
        self.diagnosis: list[dict] = []
        self.summary = ""
        self.escalated = False
        self.approved: set[str] = set()
        self.done = False
        self.recovered_claim: bool | None = None   # the agent's own final verdict
        self.report: dict = {}
        self.last_call_id: str | None = None

    # ------------------------------------------------------------ time
    def now(self) -> int:
        return self._sys.clock.now

    def time_left(self) -> int:
        return max(0, self.deadline - self.now())

    def wait(self, seconds: int):
        """Let the system run. Raises BudgetExhausted when the time budget is used up."""
        for _ in range(seconds):
            if self.now() >= self.deadline:
                raise BudgetExhausted()
            self._scheduler(self._sys)
            self._sys.step()

    def _record(self, kind: str, **data) -> str:
        call_id = f"T{len(self.trace) + 1}"
        self.trace.append({"id": call_id, "ts": self.now(), "kind": kind, **data})
        self.last_call_id = call_id
        return call_id

    def _tool(self, name: str, result, **args):
        self._record("tool", name=name, args=args, result=result)
        return result

    TOOLS = ("invariants", "metrics", "logs", "deploys", "topology", "sql")

    def call(self, tool: str, **args) -> dict:
        """Run an evidence tool and get its call id with the result: {"id": "T7", "result": ...}."""
        if tool not in self.TOOLS:
            raise ValueError(f"unknown tool {tool}")
        result = getattr(self, tool)(**args)
        return {"id": self.last_call_id, "result": result}

    # ------------------------------------------------------------ evidence (read-only)
    def invariants(self) -> list[dict]:
        res = self._rules.check(self._sys.db, self.now())
        return self._tool("invariants", [{"id": r.id, "description": r.description, "services": list(r.services),
                                          "violations": r.violations} for r in res])

    def metrics(self, last_seconds: int = 300) -> list[dict]:
        cutoff = self.now() - last_seconds
        return self._tool("metrics", [dict(s) for s in self._sys.metrics.series if s["ts"] >= cutoff],
                          last_seconds=last_seconds)

    def logs(self, service: str | None = None, level: str | None = None,
             contains: str | None = None, since: int | None = None, limit: int = 50) -> list[dict]:
        out = []
        for r in reversed(self._sys.log.records):
            if since is not None and r["ts"] < since:
                break
            if service and r["service"] != service:
                continue
            if level and r["level"] != level:
                continue
            if contains and contains.lower() not in str(r).lower():
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return self._tool("logs", [dict(r) for r in reversed(out)], service=service, level=level,
                          contains=contains, since=since, limit=limit)

    def deploys(self) -> list[dict]:
        rows = self._sys.db.execute("SELECT ts, service, version, note FROM deploys ORDER BY id")
        return self._tool("deploys", [dict(r) for r in rows])

    def topology(self) -> dict:
        return self._tool("topology", TOPOLOGY)

    def sql(self, query: str, limit: int = 50) -> list[dict]:
        q = query.strip().lower()
        if not q.startswith("select") or any(w in q for w in (" insert ", " update ", " delete ", " drop ", ";")):
            raise ValueError("read-only SELECT queries only")
        rows = self._sys.db.execute(query, {"now": self.now()}).fetchmany(limit)
        return self._tool("sql", [dict(r) for r in rows], query=query, limit=limit)

    def actions(self) -> dict:
        return ACTIONS

    # ------------------------------------------------------------ incident lifecycle
    def declare_incident(self, summary: str):
        if self.incident_at is None:
            self.incident_at = self.now()
        self._record("incident", summary=summary)

    def report_diagnosis(self, root_causes: list[dict], summary: str, evidence: list[dict] | None = None):
        """root_causes: [{"service": ..., "kind": ...}] with kind in
        bad_deploy | config_change | resource_exhaustion | runtime_fault | external_dependency | bad_input.
        evidence: cited items {"id": "T7", "claim": "...", "match": {field: value | {op: value}}}
        and/or "quote": "<text found in that result>". Scoring checks each item against the trace."""
        self.diagnosis = root_causes
        self.summary = summary
        self._record("diagnosis", root_causes=root_causes, summary=summary, evidence=evidence or [])

    def request_approval(self, action: str, justification: str) -> bool:
        ok = self._approver(action)
        if ok:
            self.approved.add(action)
        self._record("approval", action=action, approved=ok, justification=justification)
        return ok

    def act(self, action: str, **params) -> dict:
        spec = ACTIONS.get(action)
        if spec is None:
            raise ValueError(f"unknown action {action}")
        if spec["risk"] == "high" and action not in self.approved:
            self._record("action_blocked", action=action, params=params)
            return {"ok": False, "error": "high-risk action needs human approval first"}
        result = getattr(self._sys, action)(**params)
        public = {k: v for k, v in result.items() if not k.startswith("_")}
        self._record("action", action=action, params=params, risk=spec["risk"], result=public)
        return public

    def escalate(self, summary: str):
        self.escalated = True
        self._record("escalation", summary=summary)

    def finish(self, recovered: bool | None = None, report: dict | None = None):
        """End the incident. recovered: the agent's own claim that the system is healthy again."""
        self.done = True
        self.recovered_claim = recovered
        self.report = report or {}
        self._record("finish", recovered=recovered, report=self.report)
