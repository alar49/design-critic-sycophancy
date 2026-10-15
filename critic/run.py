"""Run the rating experiment: every model rates every level variant under every framing.

Usage:
    python -m critic.run --config config.yaml --dry-run --limit 20   # offline test, no API calls
    python -m critic.run --config config.yaml                         # real run (resumable)

Results are appended to the JSONL file named in the config, one line per API call.
Re-running the same command skips trials that already succeeded, so an interrupted
run can simply be restarted.
"""
import argparse
import json
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from critic.llm import parse_answer
from critic.prompts import FRAMING_NAMES, build_messages, opening
from critic.providers import ModelPaused, Router, load_models, provider_kind, run_by_model, token_counts


def load_dotenv(path=".env"):
    """Read KEY=VALUE lines from .env into the environment (without overriding existing values)."""
    import os
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def select_levels(stimuli, n_levels=None):
    """Keep the first n_levels base levels (L00, L01, ...) of the stimulus file, or all if None."""
    if not n_levels:
        return stimuli
    keep = sorted({s["level_id"] for s in stimuli})[:n_levels]
    if len(keep) < n_levels:
        raise ValueError(f"n_levels={n_levels} but the stimulus file has only {len(keep)} base levels")
    return [s for s in stimuli if s["level_id"] in keep]


def read_rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def row_cost(row, spec):
    """Cost recorded for a row; for rows written before costs were recorded, recompute it."""
    if isinstance(row.get("cost"), (int, float)):
        return row["cost"]
    return spec.cost(row.get("usage")) if spec and row.get("usage") else None


def cost_report(rows, specs, planned):
    """Per model: trials done, average tokens per call, cost so far and projected cost of the full run."""
    lines = ["", "Tokens and cost so far (projection = average cost per completed trial x trials planned):",
             f"  {'model':24s} {'done':>11s} {'in/call':>8s} {'out/call':>9s} {'reason/call':>12s} {'cost':>9s} {'projected':>10s}"]
    total = proj_total = 0.0
    for spec in specs:
        mine = [r for r in rows if r.get("model") == spec.name and r.get("usage")]
        done = {r["trial_id"] for r in mine if r.get("api_error") is None and r.get("parse_error") is None}
        counts = [token_counts(r["usage"]) for r in mine]
        avg = lambda i: (sum(c[i] or 0 for c in counts) / len(counts)) if counts else 0
        costs = [row_cost(r, spec) for r in mine]
        known = [c for c in costs if c is not None]
        cost = sum(known) if known else None
        proj = cost / len(done) * planned if (cost is not None and done) else None
        total += cost or 0.0
        proj_total += proj or 0.0
        fmt = lambda x: "unknown" if x is None else f"${x:.2f}"
        lines.append(f"  {spec.name:24s} {len(done):>5d}/{planned:<5d} {avg(0):8.0f} {avg(1):9.0f} {avg(2):12.0f} "
                     f"{fmt(cost):>9s} {fmt(proj):>10s}")
    lines.append(f"  {'total':24s} {'':11s} {'':8s} {'':9s} {'':12s} {f'${total:.2f}':>9s} {f'${proj_total:.2f}':>10s}")
    lines.append("  (cost is 'unknown' for a model whose provider reports no cost and that has no price_in/price_out)")
    return "\n".join(lines)


def load_done(path):
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("parse_error") is None and row.get("api_error") is None:
                    done.add(row["trial_id"])
    return done


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="use a fake model; no API calls")
    parser.add_argument("--limit", type=int, default=None, help="only run the first N pending trials")
    args = parser.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(Path(args.config).read_text())
    exp = cfg["experiment"]
    stimuli = select_levels(json.loads(Path(cfg["stimuli"]).read_text())["stimuli"], exp.get("n_levels"))
    framings = exp["framings"]
    unknown = set(framings) - set(FRAMING_NAMES)
    if unknown:
        raise ValueError(f"unknown framings in config: {unknown}")
    wording = cfg.get("wording") or {}
    print("Opening sentences:")
    for f in framings:
        print(f"  {f:14s} {opening(f, wording)}")

    out_path = Path(cfg["output"] if not args.dry_run else cfg["output"].replace(".jsonl", "_dryrun.jsonl"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out_path)

    specs = load_models(cfg)
    trials = []
    for spec in specs:
        for s in stimuli:
            for framing in framings:
                for rep in range(exp["repeats"]):
                    tid = f"{spec.name}|{s['level_id']}|{s['variant']}|{framing}|{rep}"
                    if tid not in done:
                        trials.append((spec, tid, s, framing, rep))
    random.Random(exp.get("seed", 0)).shuffle(trials)  # interleave conditions over time
    if args.limit:
        trials = trials[: args.limit]
    print(f"{len(done)} trials already done, {len(trials)} to run -> {out_path}")
    planned = len(stimuli) * len(framings) * exp["repeats"]
    by_name = {s.name: s for s in specs}
    if not trials:
        print(cost_report(read_rows(out_path), specs, planned))
        return

    spent = {}  # what earlier runs into the same file already cost, so caps hold across re-runs
    for r in read_rows(out_path):
        c = row_cost(r, by_name.get(r.get("model")))
        if c:
            spent[r["model"]] = spent.get(r["model"], 0.0) + c
    router = Router(cfg, specs, dry_run=args.dry_run, spent=spent)  # fails here if an API key is missing
    for s in specs:
        if (router.max_cost is not None or s.max_cost is not None) and s.price_in is None and s.price_out is None \
                and provider_kind(router.providers[s.provider]) != "openrouter" and not args.dry_run:
            print(f"  note: {s.name} has no price_in/price_out, so the budget cannot count its cost "
                  f"unless its provider reports it.")
    lock = threading.Lock()

    def run_one(trial):
        spec, tid, s, framing, rep = trial
        messages = build_messages(s["text"], framing, wording)
        row = {"trial_id": tid, "model": spec.name, "provider": spec.provider, "provider_model": spec.model,
               "reasoning_setting": spec.reasoning,
               "level_id": s["level_id"], "variant": s["variant"], "framing": framing,
               "opening": opening(framing, wording), "repeat": rep,
               "time": datetime.now(timezone.utc).isoformat(), "api_error": None, "parse_error": None}
        start = time.time()
        try:
            reply, info = router.complete(spec, messages, exp)
            reasoning, served_by = info.pop("reasoning", None), info.pop("served_by", None)
            parsed, err = parse_answer(reply)  # the final answer only; reasoning is kept apart
            if err and info.get("finish_reason") == "length":
                err = f"cut off at max_tokens ({err}); raise max_tokens for this model"
            tin, tout, treason = token_counts(info)
            row.update({"reply": reply, "reasoning": reasoning, "served_by": served_by,
                        "tokens_in": tin, "tokens_out": tout, "tokens_reasoning": treason,
                        "cost": info.get("cost"), "usage": info, "parse_error": err})
            if parsed:
                row.update(parsed)
        except ModelPaused:
            return None  # not recorded: the trial stays pending for the next run
        except Exception as e:  # keep going; failed trials are retried on the next run
            row["api_error"] = str(e)
        row["latency_s"] = round(time.time() - start, 2)
        with lock:
            with out_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        return row

    n_ok = n_fail = n_skip = 0
    for i, row in enumerate(run_by_model(specs, trials, run_one, exp.get("concurrency", 4)), 1):
        if row is None:
            n_skip += 1
        elif row["api_error"] or row["parse_error"]:
            n_fail += 1
            print(f"  ! {row['trial_id']}: {(row['api_error'] or row['parse_error'])[:200]}")
        else:
            n_ok += 1
        if i % 25 == 0 or i == len(trials):
            print(f"{i}/{len(trials)} done ({n_ok} ok, {n_fail} failed, {n_skip} skipped), "
                  f"${router.total_spent():.2f} spent in total")
    if router.paused:
        print(f"Paused (quota?): {', '.join(sorted(router.paused))}. Their remaining trials run when you re-run.")
    print(cost_report(read_rows(out_path), specs, planned))
    print("Finished. Re-run the same command to retry failed or skipped trials.")


if __name__ == "__main__":
    main()
