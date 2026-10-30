"""Proof that agents cannot see the answer.

1. Static scan (AST) of agent code: no private Env state, no fault switches, no ground
   truth, no scenario object, no imports of the simulator, runner, reference agents or
   generator, no reflection tricks to get around these rules.
2. Runtime check: agents run against a guarded Env that fails on any non-public access.
3. Leak test: agent code, prompt templates and the config text agents see contain no
   scenario ids, fault mechanism names or per-scenario repair steps.
Each check is also run on a planted cheat, to prove it is not vacuous.
"""

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from baselines.llm_single_shot import LLMSingleShotAgent
from baselines.rule_based import RuleBasedAgent
from harness.env import Env
from harness.runner import run_one
from raqib.llm import FakeLLM

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIRS = ["raqib", "baselines"]            # everything an agent is built from
SEEN_CONFIG = ["config/actions.yaml", "config/topology.yaml", "config/invariants.yaml"]  # text agents see via Env

BANNED_MODULES = ("target", "harness.runner", "harness.reference_agents", "harness.generator", "harness.citations")
BANNED_KEYS = {"truth", "fault_names", "fix", "generated", "fault_switches"}
BANNED_ATTRS = {"faults", "__dict__", "_sys", "trace"}
BANNED_CALLS = {"vars", "globals", "locals", "__import__", "eval", "exec", "import_module", "_getframe"}
BANNED_PATH_TEXT = ("scenarios", "fault_primitives", "sealed", "results/")
REFLECTION_MODULES = {"inspect", "gc", "ctypes", "importlib"}


# ---------------------------------------------------------------- 1. static scan
def violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []

    def bad(node, why):
        out.append(f"{filename}:{getattr(node, 'lineno', '?')}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(BANNED_MODULES) or a.name.split(".")[0] in REFLECTION_MODULES:
                    bad(node, f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            full = [f"{mod}.{a.name}" for a in node.names]
            if mod.startswith(BANNED_MODULES) or any(f.startswith(BANNED_MODULES) for f in full) \
                    or mod.split(".")[0] in REFLECTION_MODULES:
                bad(node, f"from {mod} import ...")
        elif isinstance(node, ast.Attribute):
            private = node.attr.startswith("_") and not node.attr.startswith("__")
            on_self = isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            if node.attr in BANNED_ATTRS or (private and not on_self):
                bad(node, f"attribute .{node.attr}")
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in BANNED_KEYS:
                bad(node, f"subscript [{key.value!r}]")
            if isinstance(node.value, ast.Name) and node.value.id == "scenario":
                bad(node, "scenario[...]")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "scenario":
            bad(node, "uses the scenario object")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in BANNED_CALLS:
                bad(node, f"call {name}()")
            if name in ("getattr", "setattr", "hasattr", "delattr") and len(node.args) >= 2:
                arg = node.args[1]
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("_")):
                    bad(node, f"{name}() with a computed or private name")
            if name == "get" and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value in BANNED_KEYS:
                bad(node, f".get({node.args[0].value!r})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            low = node.value.lower()
            for text in BANNED_PATH_TEXT:
                if text in low:
                    bad(node, f"string mentions {text!r}")
    return out


def agent_files() -> list[Path]:
    return sorted(p for d in AGENT_DIRS for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


def test_agent_code_never_touches_hidden_state():
    found = [v for p in agent_files() for v in violations(p.read_text(encoding="utf-8"), str(p.relative_to(ROOT)))]
    assert found == [], "\n".join(found)


@pytest.mark.parametrize("cheat", [
    "env._sys.faults.active('x')",
    "env._scheduler",
    "x = system.faults",
    "from target.system import TargetSystem",
    "import target",
    "from harness import runner",
    "from harness.reference_agents import OracleAgent",
    "from harness.generator import compose",
    "t = scenario['truth']",
    "fix = sc['fix']",
    "names = s.get('fault_names')",
    "x = scenario",
    "getattr(env, '_sys')",
    "getattr(env, name)",
    "vars(env)",
    "env.__dict__",
    "import inspect",
    "open('scenarios/heldout.yaml')",
    "self.env._rules",
    "env.trace",
])
def test_scanner_catches_planted_cheats(cheat):
    assert violations(cheat), cheat


def test_scanner_allows_normal_agent_code():
    ok = """
from harness.env import BudgetExhausted
from raqib.monitor import Monitor
class A:
    def run(self, env):
        self._helper()
        r = env.call("logs", service="worker")
        rows = env.sql("SELECT * FROM orders")
        x = getattr(self, "name")
"""
    assert violations(ok) == []


# ---------------------------------------------------------------- 2. runtime check
PUBLIC = {name for name in dir(Env) if not name.startswith("_")} | {"last_call_id", "start", "deadline"}
PUBLIC -= {"trace", "diagnosis", "summary", "escalated", "approved", "done", "incident_at", "recovered_claim", "report"}


class GuardedEnv:
    """Wraps the real Env; any access outside the public API fails the run."""

    def __init__(self, env):
        object.__setattr__(self, "_env", env)
        object.__setattr__(self, "accessed", set())

    def __getattr__(self, name):
        if name not in PUBLIC:
            raise AssertionError(f"agent touched non-public Env attribute {name!r}")
        self.accessed.add(name)
        return getattr(self._env, name)

    def __setattr__(self, name, value):
        raise AssertionError(f"agent tried to set Env attribute {name!r}")


def guarded(factory, seen: list):
    def make(**kw):
        agent = factory(**kw)
        real_run = agent.run

        def run(env):
            g = GuardedEnv(env)
            seen.append(g)
            return real_run(g)
        agent.run = run
        return agent
    return make


DEV = yaml.safe_load((ROOT / "scenarios" / "dev.yaml").read_text(encoding="utf-8"))
FAKE_REPLY = json.dumps({"root_causes": [{"service": "worker", "kind": "runtime_fault"}],
                         "actions": [{"action": "restart_service", "params": {"service": "worker"}}],
                         "escalate": False, "summary": "x"})
AGENT_FACTORIES = {
    "rule_based": RuleBasedAgent,
    "llm_single_shot": lambda **kw: LLMSingleShotAgent(llm=FakeLLM(lambda m: FAKE_REPLY), **kw),
}


@pytest.mark.parametrize("name", sorted(AGENT_FACTORIES))
def test_agents_only_use_the_public_env_api(name):
    for sc in DEV[:3]:
        seen = []
        run_one(sc, "dev", guarded(AGENT_FACTORIES[name], seen), seed=1)
        assert seen and seen[0].accessed <= PUBLIC


def test_runtime_guard_catches_a_cheating_agent():
    class Cheater:
        def __init__(self, **_):
            pass

        def run(self, env):
            env.wait(10)
            return env._sys.faults
    with pytest.raises(AssertionError, match="non-public"):
        run_one(DEV[0], "dev", guarded(Cheater, []), seed=1)


# ---------------------------------------------------------------- 3. leak test
def forbidden_terms() -> set[str]:
    terms = set()
    for path in (ROOT / "scenarios").glob("*.yaml"):
        for sc in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            terms.add(sc["id"])
            terms |= {f["name"] for e in sc["events"] for f in e.get("faults", [])}
            terms |= {step for step in sc["truth"].get("fix", []) if ":" in step}
    lib = yaml.safe_load((ROOT / "config" / "fault_primitives.yaml").read_text(encoding="utf-8"))
    terms |= {p["mechanism"] for p in lib["primitives"]}
    terms |= {p["fault"] for p in lib["primitives"] if p.get("fault")}
    terms |= set(lib["injected_text"]["texts"])
    return {t.lower() for t in terms}


def _variants(term: str) -> set[str]:
    return {term} if ":" in term or " " in term else {term, term.replace("_", " "), term.replace("_", "-")}


def leaks(text: str, terms: set[str]) -> list[str]:
    low = text.lower()
    return sorted(t for t in terms for v in _variants(t)
                  if re.search(rf"(?<![a-z0-9_]){re.escape(v)}(?![a-z0-9_])", low))


def leak_files() -> list[Path]:
    files = [p for d in AGENT_DIRS for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    return sorted(files) + [ROOT / c for c in SEEN_CONFIG]


def test_agent_code_and_prompts_leak_no_scenario_knowledge():
    terms = forbidden_terms()
    assert len(terms) > 50
    found = {str(p.relative_to(ROOT)): hits for p in leak_files()
             if (hits := leaks(p.read_text(encoding="utf-8"), terms))}
    assert found == {}, found


def test_leak_check_catches_planted_leaks():
    terms = forbidden_terms()
    assert leaks("if 'conn_leak' in hint:", terms)
    assert leaks("Watch for a payment swallow pattern", terms)
    assert leaks("steps = ['rollback_service:payment-service']", terms)
    assert not leaks("check the connection pool and the payment confirmations", terms)
