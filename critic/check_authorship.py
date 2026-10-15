"""Authorship pre-test: does each model understand who the opening sentence says made the level?

Run this BEFORE the main experiment. It sends the same prompt as the main run, except that the
request for a review is replaced by a multiple-choice question (version 3, see critic/prompts.py):
"According to this message, who made this level? (We are asking what the message says, not whether
it can be verified.) A) you, the assistant ... B) me, the person writing ... C) a third party
(someone other than you and me, named or not) D) the message mentions no author".

Usage:
    python -m critic.check_authorship [--dry-run] [--fresh]

Re-running keeps the answers already collected for the same version of the question and only asks
again what is missing or failed (e.g. a model paused by a rate limit); --fresh starts over. Answers
are matched by model NAME, sentence and level, not by settings: to test other settings of a model,
give that entry a new name. Answers to an earlier version of the question are moved to
results/authorship_check_<version>.jsonl.

Writes results/authorship_check.jsonl (every reply) and results/authorship_check.md (summary).

Decision rules (fixed before running version 3; see README). Version 3 is the last version.
  * Expected answers: user B, self A, other C, llm C. `neutral` (expected D) is for information only:
    it shows whether option D is understood; no hypothesis depends on it.
  * Failed calls (API errors, empty replies) are not answers: they are excluded and asked again on
    a re-run. A reply in which no letter can be read counts as a wrong answer.
  * A framing passes for a model if at least 80% of its answers are the expected letter and at least
    `authorship_check.min_answered` (default 16) of its 20 calls were answered.
  * `other` / `llm`: the wording set in config.yaml (`wording`; the design uses "another") is kept,
    unless it fails for some model while the other wording passes for every model; then switch.
    The same wording is used for all models.
  * A framing that fails for a model makes the hypotheses that rely on it uninterpretable for that
    model (reported as exploratory): user -> H1-H3 and H5; other -> H1-H3; self or llm -> H4.
"""
import argparse
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml

from critic.llm import THINK_TAG
from critic.prompts import AUTHORSHIP_QUESTION, DEFAULT_WORDING, QUESTION_VERSION, build_messages, opening
from critic.providers import ModelPaused, Router, load_models, run_by_model
from critic.run import load_dotenv

THRESHOLD = 0.8
LETTERS = "ABCD"
# hypotheses that rely on each framing being understood
RELIES_ON = {"user": ["H1-H3", "H5"], "other": ["H1-H3"], "self": ["H4"], "llm": ["H4"]}
# (framing, wording of the outside authors, expected answer)
PROBES = [
    ("neutral", None, "D"),  # information only
    ("user", None, "B"),
    ("self", None, "A"),
    ("other", "plain", "C"),
    ("other", "another", "C"),
    ("llm", "plain", "C"),
    ("llm", "another", "C"),
]


def parse_letter(text):
    """The answer letter, or None. Accepts 'D', 'D)', '(D)', 'Answer: D', '**D**', and ignores
    reasoning written inline (<think>, <thinking>, <thought>, <reasoning>)."""
    text = THINK_TAG.sub("", text or "").strip().strip("*").strip()
    m = (re.match(r"^\(?([A-D])(?:\)|\.|:|\*|\s*$|\s*[-–—]|\s*\n)", text)
         or re.search(r"(?i:answer|option)(?:\s+is)?\W*\(?([A-D])(?![A-Za-z])", text))
    return m.group(1) if m else None


def free_name(path):
    """`path`, or path_2, path_3, ... if it already exists."""
    k, p = 2, path
    while p.exists():
        p = path.with_name(f"{path.stem}_{k}{path.suffix}")
        k += 1
    return p


def archive_old_versions(out):
    """Move answers to an earlier version of the question (rows without a version are v1) out of `out`."""
    if not out.exists():
        return
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    old = {}
    for r in rows:
        v = r.get("question_version", "v1")
        if v != QUESTION_VERSION:
            old.setdefault(v, []).append(r)
    if not old:
        return
    for v, rs in old.items():
        dest = free_name(out.with_name(f"{out.stem}_{v}{out.suffix}"))
        dest.write_text("".join(json.dumps(r) + "\n" for r in rs))
        print(f"{len(rs)} answers to question {v} moved to {dest}")
    current = [r for r in rows if r.get("question_version", "v1") == QUESTION_VERSION]
    out.write_text("".join(json.dumps(r) + "\n" for r in current))
    md = out.with_suffix(".md")
    if md.exists() and not current:  # the summary belongs to the old version too
        v = Counter(r.get("question_version", "v1") for r in rows).most_common(1)[0][0]
        dest = free_name(md.with_name(f"{md.stem}_{v}{md.suffix}"))
        md.rename(dest)
        print(f"Its summary moved to {dest}")


def summarize(df, min_answered):
    """One row per model x framing x wording: answered and failed calls, letter shares among the
    answered calls, the share of the expected letter and the result (pass / fail / too few answers)."""
    keys = ["model", "framing", "wording", "expected"]
    cols = list(LETTERS + "?")
    df = df.assign(answered=df["error"].isna(), letter=df["letter"].fillna("?"))
    table = df.groupby(keys).agg(n_answered=("answered", "sum"), n_calls=("answered", "size"))
    table["n_answered"] = table["n_answered"].astype(int)
    table["n_failed"] = table.pop("n_calls") - table["n_answered"]
    ans = df[df["answered"]]
    counts = (pd.crosstab([ans[k] for k in keys], ans["letter"]) if len(ans)
              else pd.DataFrame(index=table.index, columns=cols))
    counts = counts.reindex(index=table.index, columns=cols).fillna(0)
    table = table.join(counts.div(table["n_answered"].where(table["n_answered"] > 0), axis=0))
    table["expected_share"] = [table.loc[i, i[3]] for i in table.index]
    table["result"] = ["too few answers" if n < min_answered else ("pass" if s >= THRESHOLD else "fail")
                       for n, s in zip(table["n_answered"], table["expected_share"])]
    order = {(f, w or "-"): k for k, (f, w, _) in enumerate(PROBES)}
    table = table.reindex(sorted(table.index, key=lambda i: (i[0], order[(i[1], i[2])])))
    return table.round(2)


def decide(table, configured):
    """Decisions following the rules in the module docstring. `configured` maps other/llm to a wording."""
    models = sorted(table.index.get_level_values("model").unique())

    def results(framing, wording):
        sub = table.xs((framing, wording), level=("framing", "wording"))
        return {m: (sub.xs(m, level="model")["result"].iloc[0] if m in sub.index.get_level_values("model")
                    else "too few answers") for m in models}

    decisions, flags = [], {m: set() for m in models}
    for framing in ("other", "llm"):
        mine = configured[framing]
        alt = "another" if mine == "plain" else "plain"
        res, alt_res = results(framing, mine), results(framing, alt)
        fails = [m for m, r in res.items() if r == "fail"]
        short = [m for m, r in res.items() if r == "too few answers"]
        alt_info = (f"(for information, the \"{alt}\" wording passes for "
                    f"{sum(r == 'pass' for r in alt_res.values())} of {len(models)} models)")
        sentence = opening(framing, {framing: mine})
        if not fails and not short:
            decisions.append(f"{framing}: the configured wording ({mine}: \"{sentence}\") passes for every "
                             f"model; keep it {alt_info}")
        elif fails and all(r == "pass" for r in alt_res.values()):
            decisions.append(f"{framing}: the configured wording ({mine}) FAILS for {', '.join(fails)}; the \"{alt}\" "
                             f"wording passes for every model -> set wording.{framing}: {alt}")
        elif fails:
            decisions.append(f"{framing}: the configured wording ({mine}: \"{sentence}\") FAILS for "
                             f"{', '.join(fails)}; keep it {alt_info}")
            for m in fails:
                flags[m].update(RELIES_ON[framing])
        if short:
            decisions.append(f"{framing}: not enough answers yet for {', '.join(short)} (configured wording "
                             f"{mine}); re-run to finish the failed calls")
    for framing in ("self", "user"):
        res = results(framing, "-")
        bad = [m for m, r in res.items() if r == "fail"]
        short = [m for m, r in res.items() if r == "too few answers"]
        if bad:
            decisions.append(f"{framing}: FAILS for {', '.join(bad)}")
            for m in bad:
                flags[m].update(RELIES_ON[framing])
        if short:
            decisions.append(f"{framing}: not enough answers yet for {', '.join(short)}; re-run to finish them")
        if not bad and not short:
            decisions.append(f"{framing}: passes for every model")
    neutral = results("neutral", "-")
    decisions.append("neutral (information only, expected D): " + ", ".join(f"{m} {r}" for m, r in neutral.items()))
    for m in models:
        if flags[m]:
            keep = [h for h in ("H1-H3", "H4", "H5") if h not in flags[m]]
            decisions.append(f"{m}: report {', '.join(sorted(flags[m]))} as uninterpretable (exploratory)"
                             + (f"; {', '.join(keep)} {'stays' if len(keep) == 1 else 'stay'} confirmatory" if keep else ""))
    return decisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="use a fake model; no API calls")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore answers saved by an earlier run and ask everything again")
    args = parser.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(Path(args.config).read_text())
    exp, chk = cfg["experiment"], cfg.get("authorship_check") or {}
    min_answered = chk.get("min_answered", 16)
    configured = {**DEFAULT_WORDING, **(cfg.get("wording") or {})}
    stimuli = json.loads(Path(cfg["stimuli"]).read_text())["stimuli"]
    levels = [s for s in stimuli if s["variant"] == "none"][: chk.get("n_levels", 20)]
    out = Path(chk.get("output", "results/authorship_check.jsonl"))
    if args.dry_run:
        out = out.with_name(out.stem + "_dryrun" + out.suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    archive_old_versions(out)

    specs = load_models(cfg)
    # Answers from an earlier run are kept (unless --fresh) when the question version, the model and the
    # exact sentence match; failed calls are asked again. So a model that hit a rate limit can be re-run.
    kept = {}
    if out.exists() and not args.fresh:
        names = {s.name for s in specs}
        for line in out.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("model") in names and not r.get("error"):
                    kept[(r["model"], r["opening"], r["level_id"])] = r
    jobs, reused = [], []
    for spec in specs:
        for framing, wording, expected in PROBES:
            w = {framing: wording} if wording else {}
            for s in levels:
                sentence = opening(framing, w)
                old = kept.get((spec.name, sentence, s["level_id"]))
                if old:
                    reused.append(old)
                else:
                    jobs.append((spec, framing, wording or "-", expected, sentence,
                                 s, build_messages(s["text"], framing, w, final=AUTHORSHIP_QUESTION)))
    total = len(specs) * len(PROBES) * len(levels)
    print(f"Question {QUESTION_VERSION}. {total} answers needed ({len(specs)} models x {len(PROBES)} wordings x "
          f"{len(levels)} levels): {len(reused)} kept from an earlier run, {len(jobs)} calls to make")

    router = Router(cfg, specs, dry_run=args.dry_run)  # fails here if an API key is missing
    rows, lock = list(reused), threading.Lock()
    # rewrite the file with the kept answers; each new reply is appended as soon as it arrives
    out.write_text("".join(json.dumps(r) + "\n" for r in reused))
    per_model = {s.name: {"done": sum(r["model"] == s.name for r in reused), "errors": 0, "unparsed": 0}
                 for s in specs}
    total_per_model = len(PROBES) * len(levels)
    start = time.time()

    def run_one(job):
        spec, framing, wording, expected, sentence, s, messages = job
        row = {"model": spec.name, "question_version": QUESTION_VERSION, "framing": framing, "wording": wording,
               "opening": sentence, "level_id": s["level_id"], "expected": expected,
               "reply": None, "letter": None, "error": None}
        try:
            for _ in range(2):  # an empty reply is asked once more right away
                reply, info = router.complete(spec, messages, exp)
                if reply.strip():
                    break
            row.update(reply=reply, reasoning=info.get("reasoning"), served_by=info.get("served_by"),
                       finish_reason=info.get("finish_reason"))
            if not reply.strip():
                row["error"] = "empty reply" + (" (cut off at max_tokens: raise max_tokens)"
                                                if info.get("finish_reason") == "length" else "")
            else:
                row["letter"] = parse_letter(reply)
        except ModelPaused as e:
            row["error"] = f"paused: {e}"
        except Exception as e:
            row["error"] = str(e)
        with lock:
            rows.append(row)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
            c = per_model[spec.name]
            c["done"] += 1
            if row["error"]:
                c["errors"] += 1
                if c["errors"] <= 3:  # show the first few errors of each model right away
                    print(f"  ! {spec.name}: {row['error'][:200]}")
                    if c["errors"] == 3:
                        print(f"  ! {spec.name}: further errors are counted but not printed")
            elif row["letter"] is None:
                c["unparsed"] += 1
                if c["unparsed"] <= 2:
                    print(f"  ? {spec.name}: could not read a letter in: {(row['reply'] or '')[:120]!r}")
        return row

    last_print = [time.time()]

    def print_status():
        with lock:
            done = sum(c["done"] for c in per_model.values())
            status = "  ".join(f"{name} {c['done']}/{total_per_model}"
                               + (f" ({c['errors']} err)" if c["errors"] else "")
                               + (f" ({c['unparsed']} ?)" if c["unparsed"] else "")
                               for name, c in per_model.items())
            last_print[0] = time.time()
        print(f"[{time.time() - start:5.0f}s] {done}/{total}  {status}", flush=True)

    finished = threading.Event()

    def heartbeat():  # also report while every call is waiting (rate limits, slow providers)
        while not finished.wait(5):
            if time.time() - last_print[0] >= 30:
                print_status()

    print("Progress (done/total per model; err = failed calls, ? = no letter found in the answer):")
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        for i, _ in enumerate(run_by_model(specs, jobs, run_one, exp.get("concurrency", 4)), 1):
            if i % 25 == 0 or i == len(jobs):
                print_status()
    finally:
        finished.set()

    df = pd.DataFrame(rows)
    table = summarize(df, min_answered)
    decisions = decide(table, configured)
    failed = int(df["error"].notna().sum())

    text = "\n".join([
        f"# Authorship pre-test (question {QUESTION_VERSION})", "",
        "Share of answers per letter among the answered calls (A = you, the assistant; B = me, the user; "
        "C = a third party; D = the message mentions no author; ? = no letter found, counted as wrong; "
        "`neutral` is for information only). "
        "Failed calls (API errors, empty replies) are not answers and are not in the shares. A framing "
        f"passes if the expected letter reaches {THRESHOLD:.0%} with at least {min_answered} answered calls. "
        f"{len(levels)} perfect levels per wording; {failed} failed call{'' if failed == 1 else 's'}.", "",
        f"Configured wording: other = {configured['other']}, llm = {configured['llm']}.", "",
        table.astype(object).where(table.notna(), "-").to_markdown(), "", "## Decisions", "", *[f"- {d}" for d in decisions], ""])
    out.with_suffix(".md").write_text(text)
    print(text)
    if failed:
        print(f"{failed} call{' has' if failed == 1 else 's have'} failed; re-run the same command to ask "
              "only those again before deciding.")
    if args.dry_run:
        print("(Dry run: the fake model answers at random, so every wording fails. This only tests the pipeline.)")


if __name__ == "__main__":
    main()
