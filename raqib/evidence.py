"""Evidence collectors: turn raw telemetry into short factual claims in the ledger.

Each collector compares the incident window with the healthy baseline the agent
recorded when it came on watch, and writes down only what changed. Nothing here
knows about specific failures: it is the same reading an on-call engineer does
of logs, metrics, deploy history and the database.

Every item records the Env tool call it was read from and a checkable form of its
claim (`support`), so the final report's citations can be verified by the harness.
"""

import json
import re

from .ledger import Ledger

BASELINE = 300            # seconds of healthy history before the agent came on watch
RATE_WINDOW = 60

# Metrics catalog: which service each metric describes, and which are gauges.
METRICS = {
    "orders_created": ["order-service"],
    "messages_processed": ["worker"], "messages_retried": ["worker"], "messages_dead": ["worker"],
    "worker_restarts": ["worker"], "dead_letters": ["worker"],
    "queue_depth": ["queue", "worker"],
    "invoices_extracted": ["invoice-agent"],
    "payments_requested": ["payment-service"], "payments_captured": ["payment-service"],
    "payments_failed": ["payment-service"], "pending_webhooks": ["payment-service"],
    "orders_fulfilled": ["workflow"],
    "ledger_rows_synced": ["sync-job"],
    "db_pool_in_use": ["database"],
}
GAUGES = {"queue_depth", "dead_letters", "db_pool_in_use", "pending_webhooks"}
SIGNATURE_FIELDS = ("error", "reason", "status")
IGNORED_LOG_SERVICES = {"ops", "deployer"}   # our own actions; deploys are read from the deploy table
MAX_TEXT = 140


def metric_services(key: str) -> list[str]:
    if key.startswith("errors."):
        return [key.split(".", 1)[1]]
    return METRICS.get(key, [])


def clip(value, n: int = MAX_TEXT) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _signature(r: dict) -> tuple:
    parts = [r["service"], r["level"], r["event"]]
    for f in SIGNATURE_FIELDS:
        if f in r:
            value = re.sub(r"\d{4,}", "N", str(r[f]))   # ids vary, the message does not
            parts.append(f"{f}={value}")
    return tuple(parts)


def _value_at(series: list[dict], key: str, t: int) -> float:
    v = 0.0
    for s in series:
        if s["ts"] > t:
            break
        v = s.get(key, v)
    return v


def _match(record: dict, keys) -> dict:
    return {"match": {k: record[k] for k in keys if k in record}}


class Collector:
    def __init__(self, env, watch_start: int, use_topology: bool = True, use_invariants: bool = True):
        self.env = env
        self.watch_start = watch_start      # everything before this is the healthy baseline
        self.use_topology = use_topology
        self.use_invariants = use_invariants
        self.topology = env.topology()
        self.first_anomaly: int | None = None

    # ------------------------------------------------------------ all collectors
    def collect(self, ledger: Ledger, signals, detected_at: int):
        self.first_anomaly = None
        if self.use_invariants:
            self._invariants(ledger)
        self._metrics(ledger)
        self._logs(ledger)
        first = min(t for t in (self.first_anomaly, detected_at) if t is not None)
        implicated = {svc for s in signals for svc in s.services}
        implicated |= {svc for e in ledger.items.values() if e.kind != "invariants_ok" for svc in e.services}
        self._deploys(ledger, first, implicated)
        if self.use_topology:
            self._topology(ledger, implicated)

    def _anomaly(self, ts: int | None):
        if ts is not None and (self.first_anomaly is None or ts < self.first_anomaly):
            self.first_anomaly = ts

    # ------------------------------------------------------------ database: business invariants
    def _invariants(self, ledger: Ledger):
        res = self.env.call("invariants")
        healthy = []
        for inv in res["result"]:
            if inv["violations"]:
                ledger.add("database", "invariant", inv["services"],
                           f"invariant {inv['id']} violated by {inv['violations']} rows: {inv['description']}",
                           call=res["id"], support={"match": {"id": inv["id"], "violations": {">": 0}}},
                           invariant=inv["id"])
            else:
                healthy.append(inv)
        if healthy:
            ledger.add("database", "invariants_ok", [s for i in healthy for s in i["services"]],
                       "these invariants hold: " + ", ".join(i["id"] for i in healthy), call=res["id"])

    # ------------------------------------------------------------ metrics vs baseline
    def _metrics(self, ledger: Ledger):
        now, w0 = self.env.now(), self.watch_start
        res = self.env.call("metrics", last_seconds=now - (w0 - BASELINE))
        series, call = res["result"], res["id"]
        if len(series) < 3:
            return
        keys = sorted({k for s in series for k in s if k != "ts"})
        incident = [s for s in series if s["ts"] > w0]
        for key in keys:
            services = metric_services(key)
            if key in GAUGES:
                self._gauge(ledger, call, key, services, series, incident, w0)
            else:
                self._counter(ledger, call, key, services, series, incident, w0)

    def _counter(self, ledger, call, key, services, series, incident, w0):
        base = (_value_at(series, key, w0) - _value_at(series, key, w0 - BASELINE)) / BASELINE
        rates = []
        for s in incident:
            if s["ts"] - RATE_WINDOW < w0 - BASELINE:
                continue
            rate = (s.get(key, 0) - _value_at(series, key, s["ts"] - RATE_WINDOW)) / RATE_WINDOW
            rates.append((s["ts"], rate, s))
        if not rates:
            return
        now_rate = rates[-1][1]
        up = [x for x in rates if x[1] >= 3 * base + 0.05]
        if up:
            peak_t, peak, rec = max(up, key=lambda x: x[1])
            ledger.add("metrics", "metric", services,
                       f"{key} rate rose from {base:.2f}/s (baseline) starting t={up[0][0]}, "
                       f"peak {peak:.2f}/s at t={peak_t}, now {now_rate:.2f}/s", ts=up[0][0], metric=key,
                       call=call, support=_match(rec, ("ts", key)))
            self._anomaly(up[0][0])
        down = [x for x in rates if base >= 0.1 and x[1] <= 0.6 * base]
        if down:
            low_t, low, rec = min(down, key=lambda x: x[1])
            ledger.add("metrics", "metric", services,
                       f"{key} rate fell from {base:.2f}/s (baseline) starting t={down[0][0]}, "
                       f"lowest {low:.2f}/s at t={low_t}, now {now_rate:.2f}/s", ts=down[0][0], metric=key,
                       call=call, support=_match(rec, ("ts", key)))
            self._anomaly(down[0][0])

    def _gauge(self, ledger, call, key, services, series, incident, w0):
        base = [s.get(key, 0) for s in series if w0 - BASELINE <= s["ts"] <= w0]
        if not base or not incident:
            return
        bmax, bmean = max(base), sum(base) / len(base)
        now = incident[-1].get(key, 0)
        high = [s for s in incident if s.get(key, 0) >= max(2 * bmax, bmax + 15)]
        if high:
            peak = max(high, key=lambda s: s.get(key, 0))
            ledger.add("metrics", "metric", services,
                       f"{key} rose above its baseline (max {bmax:g}) at t={high[0]['ts']}, "
                       f"peak {peak.get(key, 0):g} at t={peak['ts']}, now {now:g}", ts=high[0]["ts"], metric=key,
                       call=call, support=_match(peak, ("ts", key)))
            self._anomaly(high[0]["ts"])
        if bmean >= 5 and now <= 0.3 * bmean:
            ledger.add("metrics", "metric", services,
                       f"{key} fell from a baseline mean of {bmean:.1f} to {now:g}", metric=key,
                       call=call, support=_match(incident[-1], ("ts", key)))

    # ------------------------------------------------------------ logs: new or surging signatures
    def _logs(self, ledger: Ledger):
        now, w0 = self.env.now(), self.watch_start
        res = self.env.call("logs", since=w0 - BASELINE, limit=1_000_000)
        base_counts: dict[tuple, int] = {}
        seen: dict[tuple, dict] = {}
        for r in res["result"]:
            if r["service"] in IGNORED_LOG_SERVICES:
                continue
            sig = _signature(r)
            if r["ts"] <= w0:
                base_counts[sig] = base_counts.get(sig, 0) + 1
            else:
                s = seen.setdefault(sig, {"n": 0, "first": r["ts"], "last": r["ts"], "rec": r})
                s["n"] += 1
                s["last"] = r["ts"]
        span = max(1, now - w0)
        for sig, s in sorted(seen.items(), key=lambda kv: kv[1]["first"]):
            base_n = base_counts.get(sig, 0)
            new = base_n == 0 and s["n"] >= 2
            surge = base_n > 0 and s["n"] >= 10 and s["n"] / span >= 3 * base_n / BASELINE
            if not (new or surge):
                continue
            service, level, event, *fields = sig
            detail = f" ({', '.join(fields)})" if fields else ""
            baseline = "never seen in the baseline" if new else f"baseline {base_n / BASELINE:.3f}/s"
            ledger.add("logs", "log_signature", [service],
                       f'{level} {service} "{clip(event, 60)}"{clip(detail)}: {s["n"]} times, '
                       f'first t={s["first"]}, last t={s["last"]}, {baseline}', ts=s["first"],
                       call=res["id"], support=_match(s["rec"], ("ts", "service", "level", "event")))
            if level in ("ERROR", "WARN") or new:
                self._anomaly(s["first"])

    # ------------------------------------------------------------ deploy history
    def _deploys(self, ledger: Ledger, first_anomaly: int, implicated: set[str]):
        # suspects: deploys during the baseline or after it. Older ones ran healthy for the whole baseline.
        since = self.watch_start - BASELINE
        res = self.env.call("deploys")
        deployed = set()
        for d in res["result"]:
            if d["ts"] <= since:
                continue
            rollback = d["note"].startswith("rollback from")
            gap = first_anomaly - d["ts"]
            when = f"{gap} s before the first anomaly" if gap >= 0 else f"{-gap} s after the first anomaly"
            label = "rollback (by Raqib)" if rollback else "deploy"
            ledger.add("deploys", "deploy", [d["service"]],
                       f"{label} {d['service']} {d['version']} at t={d['ts']} ({when}, t={first_anomaly}): "
                       f"\"{clip(d['note'], 80)}\"", ts=d["ts"], rollback=rollback,
                       call=res["id"], support=_match(d, ("ts", "service", "version")))
            if not rollback:
                deployed.add(d["service"])
        for svc in sorted(implicated):
            spec = self.topology.get(svc)
            if spec is None or spec.get("external") or svc in deployed:
                continue
            # an absence: real evidence for the ledger, but nothing in a result to point at
            ledger.add("deploys", "no_deploy", [svc], f"no deploy of {svc} since t={since}", call=res["id"])

    # ------------------------------------------------------------ topology context
    def _topology(self, ledger: Ledger, implicated: set[str]):
        call = self.env.call("topology")["id"]
        deps = {d for svc in implicated for d in self.topology.get(svc, {}).get("depends_on", [])}
        for svc in sorted(implicated | deps):
            spec = self.topology.get(svc)
            if spec is None:
                continue
            uses = [f"{d} (external)" if self.topology.get(d, {}).get("external") else d
                    for d in spec.get("depends_on", [])]
            users = sorted(s for s, sp in self.topology.items() if svc in sp.get("depends_on", []))
            ledger.add("topology", "topology", [svc],
                       f"{svc} depends on: {', '.join(uses) or 'nothing'}; used by: {', '.join(users) or 'nothing'}",
                       call=call, support={"match": {"service": svc}})

    # ------------------------------------------------------------ investigations the reasoner asks for
    def owners(self, query: str) -> list[str]:
        q = query.lower()
        return sorted(svc for svc, spec in self.topology.items()
                      if any(re.search(rf"\b{t}\b", q) for t in spec.get("owns", [])))

    def investigate(self, ledger: Ledger, req: dict) -> str | None:
        """Runs one evidence request. Returns an error note instead of evidence when it fails."""
        tool = req.get("tool")
        if tool == "sql":
            query = str(req.get("query", "")).strip().rstrip(";").strip()
            try:
                res = self.env.call("sql", query=query, limit=20)
            except Exception as exc:  # noqa: BLE001 (bad SQL from the model is expected sometimes)
                return f"SQL failed: {clip(query, 100)} -> {clip(str(exc), 100)}"
            rows = res["result"]
            shown = [{k: clip(v, 60) for k, v in r.items()} for r in rows[:8]]
            # rows that name a service (e.g. a per-service usage table) are about that service
            named = {str(r[k]) for r in rows for k in ("service", "target") if k in r} & set(self.topology)
            first = {k: v for k, v in (rows[0].items() if rows else []) if isinstance(v, (int, float, str))}
            ledger.add("database", "sql", sorted(set(self.owners(query)) | named),
                       f"SQL `{clip(query, 220)}` returned {len(rows)}{'+' if len(rows) == 20 else ''} rows: "
                       f"{clip(shown, 600)}", query=query,
                       call=res["id"], support={"match": dict(list(first.items())[:4])} if first else None)
            return None
        if tool == "logs":
            service = req.get("service") or None
            level = req.get("level") or None
            contains = req.get("contains") or None
            if service and service not in self.topology and service not in ("ops", "deployer"):
                return f"unknown service {service}"
            res = self.env.call("logs", service=service, level=level, contains=contains,
                                since=self.watch_start - BASELINE, limit=200)
            rows = res["result"]
            services = [service] if service else sorted({r["service"] for r in rows})
            samples = [clip(dict(r), 160) for r in rows[-3:]]
            ledger.add("logs", "log_search", [s for s in services if s in self.topology],
                       f"log search service={service} level={level} contains={clip(contains or '', 40)!r}: "
                       f"{len(rows)}{'+' if len(rows) == 200 else ''} records; latest: {samples}",
                       call=res["id"], support=_match(rows[-1], ("ts", "service", "event")) if rows else None)
            return None
        return f"unknown tool {tool}"
