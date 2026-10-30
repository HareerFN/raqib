"""Two reference agents that validate the harness itself (they are not baselines).

NullAgent:   does nothing. Should score ~0. Proves failures do not heal on their own.
OracleAgent: reads the ground truth and applies the known fix. Should recover every
             fixable scenario. Proves every fixable scenario really can be fixed
             with the available actions.
"""

from .env import BudgetExhausted


class NullAgent:
    name = "null"

    def __init__(self, **_):
        pass

    def run(self, env):
        while True:
            env.wait(60)


class OracleAgent:
    name = "oracle"

    def __init__(self, scenario, onset, **_):
        self.truth = scenario["truth"]
        self.onset = onset

    def run(self, env):
        try:
            while env.now() < self.onset + 60:
                env.wait(10)
        except BudgetExhausted:
            return
        env.declare_incident("oracle: known fault")
        env.report_diagnosis([{"service": rc["service"], "kind": rc["kinds"][0]}
                              for rc in self.truth["root_causes"]], "ground truth")
        steps = [s.partition(":")[::2] for s in self.truth["fix"]]
        for action, service in steps:
            if env.actions()[action]["risk"] == "high":
                env.request_approval(action, "known fix")
            env.act(action, **({"service": service} if service else {}))
        # in-flight work started before the fix can land afterwards (e.g. a pending
        # duplicate webhook), so settle and run the data repairs once more
        try:
            env.wait(30)
        except BudgetExhausted:
            return
        for action, service in steps:
            if action not in ("rollback_service", "restart_service"):
                env.act(action, **({"service": service} if service else {}))
        if not self.truth["fixable"]:
            env.escalate("oracle: cannot be fixed from inside the system")
        env.finish()
