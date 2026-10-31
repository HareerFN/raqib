"""Collect recorded benchmark runs into demo/demo_data.json for the demo page.

    python demo/export.py

Uses the newest results/<split>_<agent>_*.json per (split, agent). For the run library it
keeps one seed of Raqib per scenario, with the injected faults (shown to the judge after the
run, never to the agent), the agent's timeline and the harness verdict.
"""

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness.generator import SPLITS, scenario_file  # noqa: E402

SPLIT_ORDER = ["dev", "gen-dev", "validation", "gen-validation", "heldout", "gen-heldout"]
AGENT_ORDER = ["null", "rule_based", "llm_single_shot", "raqib", "raqib_no_ledger", "oracle"]
LIBRARY_SPLITS = ["gen-heldout", "heldout", "validation", "gen-dev"]   # final held-out runs first
MAX_TEXT = 260


def newest(split: str, agent: str):
    files = sorted((ROOT / "results").glob(f"{split}_{agent}_*.json"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    files = [f for f in files if re.fullmatch(rf"{re.escape(split)}_{re.escape(agent)}_\d+", f.stem)]
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def scenarios(split: str) -> dict:
    path = scenario_file(split) if split in SPLITS else ROOT / "scenarios" / f"{split}.yaml"
    return {s["id"]: s for s in yaml.safe_load(path.read_text(encoding="utf-8"))}


def faults_of(sc: dict) -> list[dict]:
    if "generated" in sc:
        return [{"mechanism": f["mechanism"], "service": f["service"], "origin": f["origin"]}
                for f in sc["generated"]["faults"]]
    out = []
    for e in sc["events"]:
        for f in e.get("faults", []):
            origin, _, svc = f["origin"].partition(":")
            out.append({"mechanism": f["name"], "service": svc, "origin": origin})
        if (e.get("deploy") or {}).get("config"):
            out.append({"mechanism": "config change", "service": e["deploy"]["service"], "origin": "config"})
    return out


def clip(s, n=MAX_TEXT):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def slim(event: dict) -> dict:
    e = dict(event)
    if e["event"] == "investigated":
        e["requests"] = [{k: clip(v, 200) for k, v in r.items() if v} for r in e["requests"]]
        e["evidence"] = [clip(x) for x in e["evidence"]]
    if e["event"] == "hypotheses":
        e["evidence"] = {k: {**v, "claim": clip(v["claim"])} for k, v in e["evidence"].items()}
        e["verdicts"] = [{**v, "mechanism": clip(v["mechanism"], 220)} for v in e["verdicts"]]
        e["summary"] = clip(e["summary"], 300)
    if e["event"] == "report":
        e["actions"] = [clip(a, 160) for a in e["actions"]]
    return e


def main():
    summaries = []
    for split in SPLIT_ORDER:
        for agent in AGENT_ORDER:
            data = newest(split, agent)
            if data:
                summaries.append({"split": split, "agent": agent, "seeds": data["seeds"], **data["summary"]})

    library = []
    for split in LIBRARY_SPLITS:
        data = newest(split, "raqib")
        if not data:
            continue
        rb = newest(split, "rule_based")
        rb_by = {(r["scenario"], r["seed"]): r for r in rb["runs"]} if rb else {}
        sc_by = scenarios(split)
        seen = set()
        for r in data["runs"]:
            if r["scenario"] in seen or r["seed"] != data["seeds"][0]:
                continue
            seen.add(r["scenario"])
            sc = sc_by[r["scenario"]]
            other = rb_by.get((r["scenario"], r["seed"]))
            library.append({
                "id": r["scenario"], "split": split, "seed": r["seed"], "family": r["family"],
                "faults": faults_of(sc), "truth": sc["truth"]["root_causes"], "fixable": sc["truth"]["fixable"],
                "onset": r["onset"],
                "result": {k: r[k] for k in ("success", "detected", "mttd", "mttr", "root_cause_correct", "recovered",
                                             "escalated", "escalation_correct", "harmful_actions",
                                             "unnecessary_actions", "stable", "recovery_claim",
                                             "false_recovery_claim", "evidence_cited", "evidence_valid",
                                             "cost_usd", "llm_calls", "diagnosis")},
                "rule_based": {k: other[k] for k in ("success", "diagnosis", "recovery_claim", "false_recovery_claim")}
                if other else None,
                "timeline": [slim(e) for e in r["timeline"]],
            })
    out = ROOT / "demo" / "demo_data.json"
    out.write_text(json.dumps({"summaries": summaries, "runs": library}, ensure_ascii=False, default=str),
                   encoding="utf-8")
    print(f"{len(summaries)} summaries, {len(library)} runs -> {out.relative_to(ROOT)} "
          f"({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
