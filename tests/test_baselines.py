"""Baselines and monitor: plumbing and false-alarm checks (no network needed)."""

import json

import yaml

from baselines.llm_single_shot import LLMSingleShotAgent
from baselines.rule_based import RuleBasedAgent
from harness.env import Env
from harness.runner import run_one
from raqib.llm import FakeLLM
from raqib.monitor import Monitor
from target.system import TargetSystem

DEV = {s["id"]: s for s in yaml.safe_load(open("scenarios/dev.yaml", encoding="utf-8"))}


def test_monitor_raises_no_false_alarms_on_a_clean_busy_system():
    s = TargetSystem(seed=21)
    s.run(300)
    s.set_cfg("traffic_rate", "10.0")
    env = Env(s, lambda _: None, 1300, lambda _: False)
    monitor = Monitor(env)
    for _ in range(120):
        env.wait(10)
        assert monitor.check() == []


def test_rule_based_solves_a_known_family():
    assert run_one(DEV["dev_payment_swallow"], "dev", RuleBasedAgent, seed=1).success


def test_llm_single_shot_plumbing_with_fake_model():
    reply = json.dumps({"root_causes": [{"service": "payment-service", "kind": "bad_deploy"}],
                        "actions": [{"action": "rollback_service", "params": {"service": "payment-service"}},
                                    {"action": "replay_payment_confirmations", "params": {}}],
                        "escalate": False, "summary": "bad payment deploy"})
    fake = FakeLLM(lambda messages: reply)
    r = run_one(DEV["dev_payment_swallow"], "dev",
                lambda **kw: LLMSingleShotAgent(llm=fake, **kw), seed=1)
    assert fake.calls == 1 and r.llm_calls == 1
    assert [a["action"] for a in r.actions] == ["rollback_service", "replay_payment_confirmations"]
