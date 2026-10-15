"""Build the stimulus set: perfect Zelda levels plus one-defect variants of each.

Usage:
    python -m critic.make_stimuli --n-levels 48 --seed 0

Writes:
    data/stimuli.json         every level variant with its ground truth
    data/stimuli_preview.md   all levels as text, for a visual check
"""
import argparse
import json
from pathlib import Path

import numpy as np

from critic.levels import CHECKS, DEFECTS, PROBLEM, evaluate, hill_climb, make_env, to_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-levels", type=int, default=48, help="number of perfect base levels")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="data/stimuli.json")
    args = parser.parse_args()

    env = make_env()
    env.seed(args.seed)
    rng = np.random.default_rng(args.seed)

    stimuli = []
    base_id = 0
    attempts = 0
    while base_id < args.n_levels:
        attempts += 1
        base = hill_climb(env, rng)
        if base is None:
            continue
        variants = {"none": base}
        for name, fn in DEFECTS.items():
            variants[name] = fn(base, rng)
        if any(v is None for v in variants.values()):
            continue  # a defect could not be applied cleanly; try another base level

        rows = []
        for variant, level in variants.items():
            quality, facts = evaluate(env, level)
            if not CHECKS[variant](facts, quality):
                print(f"  skipping a base level: {variant} has side effects {facts}")
                rows = None
                break
            rows.append({
                "level_id": f"L{base_id:02d}",
                "variant": variant,
                "defective": variant != "none",
                "quality": round(quality, 4),
                "facts": facts,
                "text": to_text(level),
            })
        if rows is None:
            continue
        stimuli.extend(rows)
        print(f"L{base_id:02d}: " + ", ".join(f"{r['variant']}={r['quality']:.2f}" for r in rows))
        base_id += 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"problem": PROBLEM, "seed": args.seed, "n_levels": args.n_levels,
            "variants": ["none", *DEFECTS], "base_attempts": attempts}
    out.write_text(json.dumps({"meta": meta, "stimuli": stimuli}, indent=1))

    preview = ["# Stimulus preview", "", f"{len(stimuli)} levels from `{PROBLEM}` (seed {args.seed}).", ""]
    for r in stimuli:
        preview += [f"## {r['level_id']} / {r['variant']} (quality {r['quality']:.2f})",
                    "", "```", r["text"], "```", "", f"`{r['facts']}`", ""]
    out.with_name("stimuli_preview.md").write_text("\n".join(preview))
    print(f"Wrote {len(stimuli)} stimuli to {out}")


if __name__ == "__main__":
    main()
