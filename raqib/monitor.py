"""Monitoring engine: decides *that* something is wrong. Plain code, no LLM.

Two kinds of signals:
  - invariant: a business rule is violated (catches silent failures)
  - metric:    a health metric moved badly (catches failures no rule describes)

Every agent in the benchmark shares this engine, so comparisons isolate what
differs between them: diagnosis and repair.
"""

from dataclasses import dataclass


@dataclass
class Signal:
    kind: str            # "invariant" | "metric"
    id: str
    services: list[str]
    detail: str


def _delta(series: list[dict], key: str, seconds: int) -> float:
    if not series:
        return 0.0
    end = series[-1]
    start = next((s for s in series if s["ts"] >= end["ts"] - seconds), series[0])
    return end.get(key, 0) - start.get(key, 0)


def _rate_windows(series: list[dict], key: str, window: int = 60) -> list[float]:
    """Rate of a counter over sliding windows (per second)."""
    rates = []
    for i, s in enumerate(series):
        start = next((p for p in series[: i + 1] if p["ts"] >= s["ts"] - window), series[0])
        span = s["ts"] - start["ts"]
        if span >= window * 0.8:
            rates.append((s.get(key, 0) - start.get(key, 0)) / span)
    return rates


class Monitor:
    def __init__(self, env, use_invariants: bool = True, use_metrics: bool = True):
        self.env = env
        self.use_invariants = use_invariants
        self.use_metrics = use_metrics

    def check(self) -> list[Signal]:
        signals: list[Signal] = []
        if self.use_invariants:
            for inv in self.env.invariants():
                if inv["violations"]:
                    signals.append(Signal("invariant", inv["id"], inv["services"],
                                          f"{inv['description']}: {inv['violations']} violations"))
        if self.use_metrics:
            signals += self._metric_signals(self.env.metrics(last_seconds=600))
        return signals

    def _metric_signals(self, series: list[dict]) -> list[Signal]:
        out: list[Signal] = []
        if len(series) < 7:
            return out
        latest = series[-1]

        # backlog growing while throughput fell: busy is fine, stuck is not
        rates = _rate_windows(series, "messages_processed")
        depth_now = latest.get("queue_depth", 0)
        grew = depth_now - next((s.get("queue_depth", 0) for s in series if s["ts"] >= latest["ts"] - 120), 0)
        if rates and depth_now > 50 and grew > 30 and rates[-1] < 0.7 * max(rates):
            out.append(Signal("metric", "M1_throughput_drop", ["worker", "queue"],
                              f"queue depth {depth_now:.0f} (+{grew:.0f} in 2 min), "
                              f"processing {rates[-1]:.1f}/s vs peak {max(rates):.1f}/s"))

        # bursts of errors, by service
        err_services = sorted({k.split(".", 1)[1] for k in latest if k.startswith("errors.")
                               and _delta(series, k, 60) >= 5})
        if err_services:
            out.append(Signal("metric", "M2_error_burst", err_services,
                              "error burst in the last minute: " + ", ".join(err_services)))

        # work being dropped into the dead-letter queue
        dead = _delta(series, "messages_dead", 60)
        if dead > 0:
            out.append(Signal("metric", "M3_dead_letters", ["worker"], f"{dead:.0f} messages dead-lettered in 1 min"))

        # connection pool pressure
        pool = latest.get("db_pool_in_use", 0)
        if pool >= 40:
            out.append(Signal("metric", "M4_db_pool_pressure", ["database"], f"db pool in use {pool:.0f}/50"))
        return out
