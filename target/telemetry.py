"""Structured logs and metrics, the evidence the repair agent will read."""

import json
from collections import defaultdict


class Logger:
    def __init__(self, clock):
        self.clock = clock
        self.records: list[dict] = []

    def log(self, service: str, level: str, event: str, **fields):
        self.records.append(
            {"ts": self.clock.now, "service": service, "level": level, "event": event, **fields}
        )

    def info(self, service, event, **f):
        self.log(service, "INFO", event, **f)

    def warn(self, service, event, **f):
        self.log(service, "WARN", event, **f)

    def error(self, service, event, **f):
        self.log(service, "ERROR", event, **f)

    def dump(self, path: str):
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


class Metrics:
    """Counters plus gauges, snapshotted every `interval` seconds."""

    def __init__(self, clock, interval: int = 10):
        self.clock = clock
        self.interval = interval
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}
        self.series: list[dict] = []

    def inc(self, name: str, value: float = 1):
        self.counters[name] += value

    def gauge(self, name: str, value: float):
        self.gauges[name] = value

    def maybe_snapshot(self):
        if self.clock.now % self.interval == 0:
            self.series.append(
                {"ts": self.clock.now, **dict(self.counters), **self.gauges}
            )
