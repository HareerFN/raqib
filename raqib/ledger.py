"""Evidence Ledger: the agent's record of facts, and the gate every fix must pass.

Code writes every evidence item; the LLM can only cite items by id. So the model
can reason about evidence but cannot invent it. Before any fix, the ledger checks
the hypothesis behind it:

  - every cited item exists and is about the suspected service (or a service that
    depends on it, where its symptoms would show up)
  - the supporting items come from at least 2 independent sources
  - the refuting items do not outweigh the supporting ones
  - the kind of cause is possible at all (a bad deploy needs a deploy, an external
    cause needs an external service)

A hypothesis that fails is never acted on.
"""

from dataclasses import asdict, dataclass, field

# Independent evidence sources. Topology is a fact about the design, not an
# observation of the incident, so it gives context but does not count.
SOURCES = ("database", "metrics", "logs", "deploys")
KINDS = ("bad_deploy", "config_change", "resource_exhaustion", "runtime_fault",
         "external_dependency", "bad_input")
MIN_SOURCES = 2


@dataclass
class Evidence:
    id: str
    source: str              # database | metrics | logs | deploys | topology
    kind: str                # invariant | invariants_ok | metric | log_signature | log_search | sql | deploy | no_deploy | topology
    services: list[str]
    claim: str
    ts: int | None = None    # when the observed change began, when known
    data: dict = field(default_factory=dict)
    call: str | None = None  # id of the Env tool call it was read from
    support: dict | None = None  # {"match": {...}} or {"quote": "..."}, checkable against that result

    def citation(self) -> dict | None:
        """This item as a citation the harness can check; None for an absence (nothing to point at)."""
        if not self.call or not self.support:
            return None
        return {"id": self.call, "claim": self.claim, **self.support}

    def line(self) -> str:
        return f"{self.id} [{self.source}] ({', '.join(self.services) or '-'}) {self.claim}"


@dataclass
class Verdict:
    service: str
    kind: str
    proven: bool
    sources: list[str]
    supporting: list[str]
    refuting: list[str]
    problems: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def dependents(service: str, topology: dict) -> set[str]:
    """Every service that depends on `service`, directly or through others."""
    out, frontier = set(), {service}
    while frontier:
        nxt = {s for s, spec in topology.items() if set(spec.get("depends_on", [])) & frontier} - out
        out |= nxt
        frontier = nxt
    return out


DEPLOY_TOLERANCE = 30     # seconds: a deploy may be blamed for symptoms that start at most this long before it


class Ledger:
    """gate=False and discriminate=False exist only for ablations."""

    def __init__(self, topology: dict, gate: bool = True, discriminate: bool = True, use_topology: bool = True):
        self.topology = topology
        self.gate = gate
        self.discriminate = discriminate
        self.use_topology = use_topology
        self.items: dict[str, Evidence] = {}

    def add(self, source: str, kind: str, services: list[str], claim: str,
            ts: int | None = None, call: str | None = None, support: dict | None = None, **data) -> Evidence:
        e = Evidence(f"E{len(self.items) + 1}", source, kind, sorted(set(services)), claim, ts, data, call, support)
        self.items[e.id] = e
        return e

    def render(self) -> list[str]:
        return [e.line() for e in self.items.values()]

    def of_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self.items.values() if e.kind == kind]

    # ------------------------------------------------------------ the gate
    def assess(self, h: dict) -> Verdict:
        svc, kind = h.get("service"), h.get("kind")
        problems: list[str] = []
        if svc not in self.topology:
            return Verdict(str(svc), str(kind), False, [], [], [], [f"unknown service {svc}"])
        if kind not in KINDS:
            return Verdict(svc, str(kind), False, [], [], [], [f"unknown kind {kind}"])

        related = {svc} | (dependents(svc, self.topology) if self.use_topology else set())
        supporting, sources = [], set()
        for eid in h.get("supporting") or []:
            e = self.items.get(eid)
            if e is None:
                problems.append(f"{eid} does not exist")
            elif e.kind == "deploy" and e.services != [svc]:
                problems.append(f"{eid} is a deploy of another service")
            elif not set(e.services) & related:
                problems.append(f"{eid} is not about {svc} or a service that depends on it")
            else:
                supporting.append(eid)
                if e.source in SOURCES:
                    sources.add(e.source)
        refuting = [eid for eid in h.get("refuting") or [] if eid in self.items]
        refuting_sources = {self.items[i].source for i in refuting} & set(SOURCES)

        problems += self._kind_check(svc, kind)
        if self.discriminate:
            problems += self._timing_check(svc, kind, supporting)
        if len(sources) < MIN_SOURCES:
            problems.append(f"supporting evidence from {len(sources)} source(s), need {MIN_SOURCES}")
        # counter-evidence blocks a cause when it is at least as broad and as plentiful as the support
        if refuting and len(refuting_sources) >= len(sources) and len(refuting) >= len(supporting):
            problems.append("refuting evidence outweighs supporting evidence")
        hard = [p for p in problems if not p.endswith("does not exist")
                and "is not about" not in p and "another service" not in p]
        proven = not hard or not self.gate
        return Verdict(svc, kind, proven, sorted(sources), supporting, refuting, problems)

    def explained_upstream(self, verdicts: list[Verdict]) -> list[Verdict]:
        """Discriminating test with the dependency graph: when a proven cause sits upstream of
        another proven cause, and every symptom cited for the downstream one is also cited for
        the upstream one, the downstream service is showing symptoms, not a cause of its own.
        Returns the verdicts with those downstream causes rejected."""
        if not self.discriminate or not self.use_topology:
            return verdicts
        symptom = lambda v: {i for i in v.supporting  # noqa: E731
                             if self.items[i].kind not in ("deploy", "no_deploy", "topology")}
        proven = [v for v in verdicts if v.proven]
        for down in proven:
            for up in proven:
                if up is down or down.service not in dependents(up.service, self.topology):
                    continue
                if symptom(down) <= set(up.supporting):
                    down.proven = False
                    down.problems.append(f"discriminating: its symptoms are all explained by {up.service} "
                                         f"upstream ({up.kind})")
                    break
        return verdicts

    def _timing_check(self, svc: str, kind: str, supporting: list[str]) -> list[str]:
        """Discriminating test for a deploy: did the symptoms of this service start after it?
        A deploy that lands after the service was already misbehaving cannot be the cause."""
        if kind not in ("bad_deploy", "config_change"):
            return []
        starts = [self.items[i].ts for i in supporting if self.items[i].ts is not None
                  and self.items[i].kind not in ("deploy", "no_deploy", "topology") and svc in self.items[i].services]
        deploys = [e.ts for e in self.of_kind("deploy") if e.services == [svc] and not e.data.get("rollback")
                   and e.ts is not None]
        if not starts or not deploys:
            return []
        first = min(starts)
        if any(t <= first + DEPLOY_TOLERANCE for t in deploys):
            return []
        return [f"timing: {svc} symptoms began at t={first}, before its deploy at t={min(deploys)}"]

    def _kind_check(self, svc: str, kind: str) -> list[str]:
        external = bool(self.topology[svc].get("external"))
        if kind in ("bad_deploy", "config_change"):
            deploys = [e for e in self.of_kind("deploy") if e.services == [svc] and not e.data.get("rollback")]
            return [] if deploys else [f"{kind} needs a recent deploy of {svc}, and there is none"]
        if kind == "external_dependency":
            return [] if external else [f"{svc} is not an external service"]
        if kind in ("runtime_fault", "resource_exhaustion") and external:
            return [f"{svc} is external: it cannot be restarted from here"]
        return []
