"""Write site/data/scoreboard.json from reports/final/ and the recorded library.

    python demo/record_library.py && python demo/build_site.py
Every number on the scoreboard is read from a result file; nothing is typed in by hand.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINAL = ROOT / "reports" / "final"
DATA = ROOT / "site" / "data"


def summary(name: str):
    p = FINAL / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))["summary"] if p.exists() else None


def seeds(name: str):
    p = FINAL / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))["seeds"] if p.exists() else []


def pct(s):
    return round(s["success_rate"], 1)


def tile(q, raqib, other, who, note=None, key="success", unit="%"):
    if not raqib or not other:
        return None
    if key == "success":
        a, b = pct(raqib), pct(other)
    else:
        a, b = raqib[key], other[key]
    return {"q": q, "a": {"v": a}, "b": {"v": b, "who": who}, "unit": unit, "note": note}


def main():
    tiles = [
        tile("Unseen failures · gen_heldout_v2 (agent v2, 3 seeds)", summary("gen-heldout-v2_raqib"),
             summary("gen-heldout-v2_rule_based"), "Runbooks",
             "Sealed before v2 was written; 60 generated incidents from fault mechanisms never seen in development."),
        tile("Unseen failures · gen-heldout (agent v1, 3 seeds)", summary("gen-heldout_raqib"),
             summary("gen-heldout_rule_based"), "Runbooks"),
        tile("Hand-written held-out (agent v1, 3 seeds)", summary("heldout_raqib"), summary("heldout_rule_based"),
             "Runbooks", "Six hand-written incidents. Runbooks do better here, and Raqib varies a lot by seed."),
        tile("False recovery claims · gen-heldout + gen_heldout_v2", None, None, "Runbooks"),
        tile("Validation (agent v1, seed 1)", summary("validation_raqib"), summary("validation_rule_based"), "Runbooks"),
        tile("Ablation · without the Evidence Ledger (validation, seed 1)", summary("validation_raqib"),
             summary("validation_raqib_no_ledger"), "No ledger",
             "Same agent with the two-source gate switched off."),
    ]
    # false recovery claims, summed over the generated held-out sets that exist
    fr = {"raqib": 0, "rule": 0, "runs": 0}
    for name in ("gen-heldout", "gen-heldout-v2"):
        a, b = summary(f"{name}_raqib"), summary(f"{name}_rule_based")
        if a and b:
            fr["raqib"] += a["false_recovery_claims"]
            fr["rule"] += b["false_recovery_claims"]
            fr["runs"] += a["runs"]
    tiles[3] = {"q": "False recovery claims · generated held-out sets (lower is better)",
                "a": {"v": fr["raqib"]}, "b": {"v": fr["rule"], "who": "Runbooks"}, "unit": "",
                "note": f"Out of {fr['runs']} runs each: the agent said “recovered” but the harness found the system still broken."}
    tiles = [t for t in tiles if t]

    lib = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    first = json.loads((DATA / lib[0]["file"]).read_text(encoding="utf-8"))
    healthy = {k: first[k] for k in ("topology", "invariants", "actions")}
    healthy["samples"] = [s for s in first["samples"] if s["t"] < first["meta"]["onset"]]
    healthy["meta"] = {"onset": first["meta"]["onset"], "mode": "replay", "seed": first["meta"]["seed"]}
    healthy["timeline"], healthy["scenario"] = [], {"faults": []}
    (DATA / "scoreboard.json").write_text(json.dumps({"tiles": tiles, "healthy": healthy}, ensure_ascii=False),
                                          encoding="utf-8")
    print(f"scoreboard: {len(tiles)} tiles; healthy samples: {len(healthy['samples'])}")


if __name__ == "__main__":
    main()
