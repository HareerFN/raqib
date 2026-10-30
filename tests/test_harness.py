"""The harness must tell a good agent from a useless one."""

import yaml

from harness.reference_agents import NullAgent, OracleAgent
from harness.runner import run_one

DEV = yaml.safe_load(open("scenarios/dev.yaml", encoding="utf-8"))


def test_null_agent_fails_everything():
    for sc in DEV:
        assert not run_one(sc, "dev", NullAgent, seed=1).success


def test_oracle_agent_solves_everything():
    for sc in DEV:
        r = run_one(sc, "dev", OracleAgent, seed=1)
        assert r.success, (sc["id"], r.final_violations)
