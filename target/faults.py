"""Fault switches inside the target system.

The fault injector turns these on. Every fault has an origin: a bad deploy of a
service (fixed by rolling that service back) or an external cause (cannot be
fixed from inside, so the right move is to escalate).

The repair agent never reads this module: it only sees logs, metrics, the
database and the deploy history, like an engineer on call.
"""


class FaultSwitches:
    def __init__(self):
        self._active: dict[str, dict] = {}

    def on(self, name: str, origin: str | None = None, **params):
        """origin examples: 'deploy:payment-service', 'external:payment-provider'."""
        self._active[name] = {"origin": origin, **params}

    def off(self, name: str):
        self._active.pop(name, None)

    def active(self, name: str) -> bool:
        return name in self._active

    def params(self, name: str) -> dict:
        return self._active.get(name, {})

    def names(self) -> list[str]:
        return list(self._active)

    def clear_origin(self, origin: str) -> list[str]:
        removed = [n for n, p in self._active.items() if p.get("origin") == origin]
        for n in removed:
            self.off(n)
        return removed
