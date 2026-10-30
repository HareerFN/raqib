"""Rule engine sanity checks: clean runs pass, each development fault trips its rule."""

import pytest

from raqib.invariants import RuleEngine
from target.system import TargetSystem

ENGINE = RuleEngine()


def broken(system):
    return {r.id for r in ENGINE.violations(system.db, system.clock.now)}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_clean_system_has_no_violations(seed):
    s = TargetSystem(seed=seed)
    s.run(3600)  # one simulated hour
    assert broken(s) == set()


DEV_FAULTS = [
    ("payment_swallow", {"rate": 0.2}, None, "I1_payment_has_order"),
    ("early_ack", {"load_threshold": 20, "crash_rate": 0.3}, {"traffic_rate": "10.0"}, "I2_no_orphaned_orders"),
    ("invoice_swap", {"rate": 0.05}, None, "I3_ledger_integrity"),
    ("callback_lost", {"rate": 0.3}, None, "I4_workflow_progress"),
    ("agent_retry_loop", {"supplier": "Najd Logistics"}, None, "I6_retry_budget"),
    ("provider_down", {}, None, "I7_payments_succeed"),
]


@pytest.mark.parametrize("fault,params,cfg,expected", DEV_FAULTS, ids=[f[0] for f in DEV_FAULTS])
def test_each_dev_fault_trips_its_rule(fault, params, cfg, expected):
    s = TargetSystem(seed=7)
    s.run(300)
    assert broken(s) == set()
    for k, v in (cfg or {}).items():
        s.set_cfg(k, v)
    s.faults.on(fault, **params)
    s.run(900)
    assert expected in broken(s)


def test_heavy_load_alone_is_not_a_violation():
    """Busy is not broken: high traffic without a fault must not raise false alarms."""
    s = TargetSystem(seed=11)
    s.set_cfg("traffic_rate", "10.0")
    s.run(1200)
    assert broken(s) == set()
