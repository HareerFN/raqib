"""Compact text snapshot of the system state, for LLM prompts.

Kept short on purpose: every line costs tokens, so only what helps a diagnosis goes in.
"""

import json

KINDS = "bad_deploy | config_change | resource_exhaustion | external_dependency | bad_input"


def _fmt_log(r: dict) -> str:
    extra = {k: v for k, v in r.items() if k not in ("ts", "service", "level", "event")}
    return f"[{r['ts']}] {r['level']:<5} {r['service']}: {r['event']} {json.dumps(extra, ensure_ascii=False) if extra else ''}"


def build(env, signals) -> str:
    now = env.now()
    lines = [f"NOW t={now}s", "", "SIGNALS"]
    lines += [f"- {s.id} ({', '.join(s.services)}): {s.detail}" for s in signals] or ["- none"]

    series = env.metrics(last_seconds=600)
    if series:
        keys = sorted(k for k in series[-1] if k != "ts")
        old = next((s for s in series if s["ts"] >= now - 300), series[0])
        lines += ["", "METRICS (5 min ago -> now)"]
        lines += [f"- {k}: {old.get(k, 0):g} -> {series[-1].get(k, 0):g}" for k in keys]

    lines += ["", "DEPLOYS (last 20 min)"]
    lines += [f"- t={d['ts']} {d['service']} {d['version']}: {d['note']}"
              for d in env.deploys() if d["ts"] >= now - 1200] or ["- none"]

    problems = env.logs(level="ERROR", since=now - 300, limit=12) + env.logs(level="WARN", since=now - 300, limit=8)
    lines += ["", "ERROR/WARN LOGS (last 5 min, newest last)"]
    lines += [_fmt_log(r) for r in sorted(problems, key=lambda r: r["ts"])] or ["- none"]

    lines += ["", "RECENT INFO LOGS"]
    lines += [_fmt_log(r) for r in env.logs(level="INFO", since=now - 60, limit=12)]

    lines += ["", "TOPOLOGY (service: depends_on)"]
    lines += [f"- {k}: {', '.join(v.get('depends_on', [])) or '-'}{' (external)' if v.get('external') else ''}"
              for k, v in env.topology().items()]

    lines += ["", "ACTIONS (name [risk]: description)"]
    lines += [f"- {k} [{v['risk']}]{' params: ' + ', '.join(v['params']) if v['params'] else ''}: {v['description']}"
              for k, v in env.actions().items()]
    return "\n".join(lines)
