"""Evidence validity: does each piece of evidence an agent cites really exist?

An agent cites evidence in its diagnosis as items like
    {"id": "T7", "claim": "payment-service errors after its deploy",
     "match": {"service": "payment-service", "level": "ERROR"}}
    {"id": "T3", "match": {"id": "I1_payment_has_order", "violations": {">": 0}}}
    {"id": "T9", "quote": "disk quota exceeded"}

An item is valid only when
  1. its id is a real evidence-tool call in the Env trace, made before the diagnosis, and
  2. that call's stored result supports it: some record in the result has every
     `match` field (equal, or passing the {op: value} test), and/or the `quote` text
     appears in the result.
A bare id, or an item with neither `match` nor `quote`, says nothing checkable and is invalid.
"""

import json
import operator

OPS = {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge,
       "<": operator.lt, "<=": operator.le}


def _records(result) -> list[dict]:
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        # topology: {service: spec} -> [{"service": name, **spec}]
        if all(isinstance(v, dict) for v in result.values()):
            return [{"service": k, **v} for k, v in result.items()]
        return [result]
    return []


def _test(actual, expected) -> bool:
    if isinstance(expected, dict) and expected and set(expected) <= {*OPS, "contains"}:
        for op, value in expected.items():
            try:
                if op == "contains":
                    if isinstance(actual, (list, tuple)):
                        ok = value in actual
                    else:
                        ok = str(value).lower() in str(actual).lower()
                else:
                    ok = OPS[op](actual, value)
            except TypeError:
                return False
            if not ok:
                return False
        return True
    return actual == expected


def supports(result, item: dict) -> bool:
    match, quote = item.get("match"), item.get("quote")
    if not match and not quote:
        return False
    if match:
        if not isinstance(match, dict):
            return False
        if not any(all(k in r and _test(r[k], v) for k, v in match.items()) for r in _records(result)):
            return False
    if quote:
        text = json.dumps(result, ensure_ascii=False, default=str).lower()
        if str(quote).lower() not in text:
            return False
    return True


def check(trace: list[dict]) -> dict:
    """Check the citations of the last diagnosis in a trace."""
    diag_at = max((i for i, e in enumerate(trace) if e["kind"] == "diagnosis"), default=None)
    if diag_at is None:
        return {"cited": 0, "valid": 0, "invalid": []}
    tools = {e["id"]: e for e in trace[:diag_at] if e["kind"] == "tool"}
    items = trace[diag_at].get("evidence") or []
    invalid = []
    for item in items:
        if not isinstance(item, dict):
            item = {"id": item}
        call = tools.get(item.get("id"))
        if call is None:
            invalid.append({**item, "why": "no such tool call before the diagnosis"})
        elif not supports(call["result"], item):
            invalid.append({**item, "why": f"not supported by the result of {call['name']}"})
    return {"cited": len(items), "valid": len(items) - len(invalid), "invalid": invalid}
