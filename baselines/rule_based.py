"""Baseline 1: classic runbook automation, no LLM.

How most teams automate recovery today:
  - a signal fires -> run the runbook written for that signal
  - "if something broke right after a deploy, roll that deploy back"
  - retry the runbook a couple of times, then page a human
It knows every development failure family, so it is a strong baseline on known
failures. The question is how it does on failures nobody wrote a runbook for.
"""

from harness.env import BudgetExhausted
from raqib.monitor import Monitor

DATA_REPAIR = {
    "I1_payment_has_order": "replay_payment_confirmations",
    "I2_no_orphaned_orders": "requeue_orphaned_orders",
    "I3_ledger_integrity": "resync_ledger",
    "I4_workflow_progress": "rerequest_supplier_approvals",
    "I5_single_payment": "refund_duplicate_payments",
}
RECENT_DEPLOY_WINDOW = 900


class RuleBasedAgent:
    name = "rule_based"

    def __init__(self, **_):
        pass

    def run(self, env):
        monitor = Monitor(env)
        rounds = 0
        try:
            while True:
                env.wait(10)
                signals = monitor.check()
                if not signals:
                    continue
                env.declare_incident("; ".join(s.id for s in signals))
                ids = {s.id for s in signals}

                # runbook: payment provider failing -> external, page a human
                if "I7_payments_succeed" in ids:
                    env.report_diagnosis([{"service": "payment-provider", "kind": "external_dependency"}],
                                         "payment failures: provider outage runbook")
                    env.escalate("payment provider failing")
                    env.finish()
                    return

                # runbook: roll back the most recent deploy of an implicated service
                implicated = {svc for s in signals for svc in s.services}
                recent = [d for d in env.deploys()
                          if d["ts"] >= env.now() - RECENT_DEPLOY_WINDOW and d["service"] in implicated
                          and not d["note"].startswith("rollback")]
                diagnosis = []
                if recent:
                    target = recent[-1]["service"]
                    env.act("rollback_service", service=target)
                    diagnosis = [{"service": target, "kind": "bad_deploy"}]
                elif "M4_db_pool_pressure" in ids:
                    env.act("restart_service", service="database")
                    diagnosis = [{"service": "database", "kind": "resource_exhaustion"}]
                elif ids & {"I6_retry_budget", "M1_throughput_drop", "M3_dead_letters"}:
                    env.act("restart_service", service="worker")
                    diagnosis = [{"service": "worker", "kind": "resource_exhaustion"}]
                env.report_diagnosis(diagnosis, "runbook diagnosis")

                for sig_id, action in DATA_REPAIR.items():
                    if sig_id in ids:
                        if env.actions()[action]["risk"] == "high":
                            env.request_approval(action, f"runbook for {sig_id}")
                        env.act(action)

                rounds += 1
                env.wait(90)
                if not monitor.check():
                    env.finish()
                    return
                if rounds >= 2:
                    env.escalate("runbooks did not resolve: " + ", ".join(sorted(ids)))
                    env.finish()
                    return
        except BudgetExhausted:
            return
