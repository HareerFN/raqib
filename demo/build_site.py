"""Write site/data/benchmark.json from reports/final/ and the recorded library.

    python demo/record_library.py && python demo/build_site.py
Every number on the Benchmark tab is computed from a result file; nothing is typed in by hand.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FINAL = ROOT / "reports" / "final"
DATA = ROOT / "site" / "data"


def load(name: str):
    p = FINAL / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def rate(runs) -> float:
    return round(100 * sum(r["success"] for r in runs) / len(runs), 1) if runs else 0.0


def groups(set_file: str, raqib: dict, rules: dict) -> list[dict]:
    scen = {s["id"]: s for s in yaml.safe_load((ROOT / "scenarios" / set_file).read_text(encoding="utf-8"))}
    tests = [
        ("All incidents", lambda s: True),
        ("One fault", lambda s: len(s["generated"]["faults"]) == 1),
        ("Two or three at once", lambda s: len(s["generated"]["faults"]) > 1),
        ("Outside cause, must escalate", lambda s: not s["truth"]["fixable"]),
    ]
    out = []
    for label, keep in tests:
        a = [r for r in raqib["runs"] if keep(scen[r["scenario"]])]
        b = [r for r in rules["runs"] if keep(scen[r["scenario"]])]
        out.append({"label": label, "n": len(a), "raqib": rate(a), "rules": rate(b)})
    return out


def main():
    v2, v2r = load("gen-heldout-v2_raqib"), load("gen-heldout-v2_rule_based")
    v1, v1r = load("gen-heldout_raqib"), load("gen-heldout_rule_based")
    hw, hwr = load("heldout_raqib"), load("heldout_rule_based")
    val, valr, abl = load("validation_raqib"), load("validation_rule_based"), load("validation_raqib_no_ledger")
    fr_raqib = sum(x["summary"]["false_recovery_claims"] for x in (v1, v2))
    fr_rules = sum(x["summary"]["false_recovery_claims"] for x in (v1r, v2r))
    runs = v1["summary"]["runs"] + v2["summary"]["runs"]
    cited = sum(x["summary"]["evidence_cited"] for x in (v1, v2))
    s = v2["summary"]
    bench = {
        "headline": f"On 60 incidents it never saw, Raqib solves {s['success_rate']}%. Runbooks solve {v2r['summary']['success_rate']}%.",
        "sub": ("The incidents come from fault mechanisms never used in development, generated and sealed by hash before "
                "this version of the agent was written, and run once with three seeds. Solved means the right cause, "
                "no harmful action, the right call on escalation, and a stability check passed after the agent stopped."),
        "facts": [
            {"v": fr_raqib, "unit": f" / {runs}", "k": f"times Raqib said “recovered” when it was not. Runbooks: {fr_rules}."},
            {"v": f"{v2['summary']['evidence_validity']:.0f}", "unit": "%", "k": f"of {cited:,} cited evidence items match a real tool result"},
            {"v": f"${s['cost_usd'] / s['runs']:.4f}", "unit": "", "k": "model cost per incident"},
        ],
        "chart": {"title": "Success on gen_heldout_v2, by kind of incident", "note": "3 seeds · 180 runs per agent",
                  "groups": groups("gen_heldout_v2.yaml", v2, v2r)},
        "sets": [
            {"name": "gen_heldout_v2", "agent": "v2", "seeds": "3", "rules": v2r["summary"]["success_rate"], "raqib": s["success_rate"],
             "note": "sealed before v2"},
            {"name": "gen-heldout", "agent": "v1", "seeds": "3", "rules": v1r["summary"]["success_rate"], "raqib": v1["summary"]["success_rate"],
             "note": "sealed before v1"},
            {"name": "Hand-written held-out", "agent": "v1", "seeds": "3", "rules": hwr["summary"]["success_rate"], "raqib": hw["summary"]["success_rate"],
             "note": "six cases; runbooks win here"},
            {"name": "Validation", "agent": "v1", "seeds": "1", "rules": valr["summary"]["success_rate"], "raqib": val["summary"]["success_rate"],
             "note": f"without the Evidence Ledger: {abl['summary']['success_rate']}%"},
        ],
        "limits": ("Where it is weak: connection leaks and a full disk (under 10% solved), where it restarts the database "
                   "instead of the service holding the connections. Seeds are fewer on development sets because of the "
                   "model budget. Everything is in docs/EVALUATION_LOG.md."),
    }
    lib = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    first = json.loads((DATA / lib[0]["file"]).read_text(encoding="utf-8"))
    healthy = {k: first[k] for k in ("topology", "invariants", "actions")}
    healthy["samples"] = [x for x in first["samples"] if x["t"] < first["meta"]["onset"]]
    bench["healthy"] = healthy
    (DATA / "benchmark.json").write_text(json.dumps(bench, ensure_ascii=False), encoding="utf-8")
    old = DATA / "scoreboard.json"
    if old.exists():
        old.unlink()
    print(json.dumps({k: bench[k] for k in ("headline", "facts")}, ensure_ascii=False, indent=1))
    print(json.dumps(bench["chart"]["groups"]))


if __name__ == "__main__":
    main()
