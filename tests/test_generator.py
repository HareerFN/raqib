"""Scenario generator: determinism, split separation, truth derivation, quality filters, sealing."""

import copy
import hashlib
import re

import yaml

from harness.generator import LIB, SPLITS, _conflict, compose, check, pairs, scenario_file

HELDOUT_PAIRS = {(p["mechanism"], svc) for p, svc, _ in pairs("heldout")}
BY_NAME = {p["mechanism"]: p for p in LIB["primitives"]}
SEEDS = range(300)


def _pairs_of(sc):
    return {(f["mechanism"], f["service"]) for f in sc["generated"]["faults"]}


def test_same_seed_same_scenario():
    for split in SPLITS:
        assert compose(SPLITS[split][1] + 7, split) == compose(SPLITS[split][1] + 7, split)


def test_heldout_mechanisms_never_appear_in_dev_or_validation():
    for split in ("gen-dev", "gen-validation"):
        for s in SEEDS:
            sc = compose(SPLITS[split][1] + s, split)
            assert not _pairs_of(sc) & HELDOUT_PAIRS, sc["id"]
            assert not any(f["name"] == "invoice_note" for e in sc["events"] for f in e.get("faults", []))


def test_every_heldout_scenario_has_something_unseen():
    assert len({m for m, _ in HELDOUT_PAIRS} - {p["mechanism"] for p, _, _ in pairs("dev")}) >= 2
    for s in SEEDS:
        sc = compose(SPLITS["gen-heldout"][1] + s, "gen-heldout")
        assert _pairs_of(sc) & HELDOUT_PAIRS, sc["id"]


def test_faults_in_one_scenario_are_distinct_and_compatible():
    for split in SPLITS:
        for s in SEEDS:
            faults = compose(SPLITS[split][1] + s, split)["generated"]["faults"]
            assert len({f["service"] for f in faults}) == len(faults)
            for i, a in enumerate(faults):
                for b in faults[i + 1:]:
                    assert not _conflict(BY_NAME[a["mechanism"]], BY_NAME[b["mechanism"]])


def test_truth_and_oracle_fix_follow_the_origin():
    for split in SPLITS:
        for s in SEEDS:
            sc = compose(SPLITS[split][1] + s, split)
            truth, fix = sc["truth"], sc["truth"]["fix"]
            assert truth["fixable"] == all(f["origin"] != "external" for f in sc["generated"]["faults"])
            for f, rc in zip(sc["generated"]["faults"], truth["root_causes"]):
                svc, origin = f["service"], f["origin"]
                assert rc["service"] == svc
                if origin == "deploy":
                    assert "bad_deploy" in rc["kinds"] and f"rollback_service:{svc}" in fix
                elif origin == "config":
                    assert "config_change" in rc["kinds"] and f"rollback_service:{svc}" in fix
                elif origin == "transient":
                    assert f"restart_service:{svc}" in fix and f"rollback_service:{svc}" not in fix
                else:
                    assert not any(step.endswith(f":{svc}") for step in fix)
                for repair in BY_NAME[f["mechanism"]].get("repairs", []):
                    if origin != "external":
                        assert repair in fix
            assert truth["needs_approval"] == sorted({x for x in fix if x == "refund_duplicate_payments"})


def test_filters_reject_broken_scenarios():
    base = next(sc for sc in (compose(SPLITS["gen-dev"][1] + s, "gen-dev") for s in SEEDS)
                if sc["family"] == "callback_lost" and sc["generated"]["faults"][0]["origin"] == "deploy"
                and sc["generated"]["faults"][0]["service"] == "workflow")
    assert check(base, seeds=(1,)) == []
    silent = copy.deepcopy(base)
    for e in silent["events"]:
        for f in e.get("faults", []):
            f["rate"] = 0.0
    assert any("does not detect" in r for r in check(silent, seeds=(1,)))
    wrong = copy.deepcopy(base)
    wrong["truth"]["fix"] = ["restart_service:workflow", "rerequest_supplier_approvals"]
    assert any("oracle cannot solve" in r for r in check(wrong, seeds=(1,)))


def test_generated_files_match_the_generator():
    for split in SPLITS:
        path = scenario_file(split)
        if not path.exists():
            continue
        for sc in yaml.safe_load(path.read_text(encoding="utf-8"))[:5]:
            assert compose(sc["generated"]["seed"], split) == sc


def test_sealed_files_are_unchanged():
    sealed = (scenario_file("gen-heldout").parent / "SEALED.md").read_text(encoding="utf-8")
    for name, digest in re.findall(r"SHA-256 of (\S+\.yaml)[^`]*`([0-9a-f]{64})`", sealed):
        data = (scenario_file("gen-heldout").parent / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name
