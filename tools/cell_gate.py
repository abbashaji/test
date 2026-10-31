#!/usr/bin/env python3
"""Merge gate for a cell PR. Exit 0 = safe to auto-merge, 1 = reject.

The plan (cells/, blueprint.yaml, this script) is read from a TRUSTED checkout of the base branch,
so an agent PR can never loosen its own rules. Only the code under test comes from the PR head.

Modes (--mode):
  all        everything in one process (local use and tests): static checks, then acceptance commands in order
  check      static checks only (branch, title, dependencies, paths, outputs). Writes check.json and, with
             --github-output, the job matrix {"index":[0,1,2]} so every acceptance command can run as its own job
  run-one    run acceptance command number --index inside the PR checkout, write a result json (no secrets needed)
  aggregate  read check.json and all result jsons, write ONE short summary listing EVERY failure

Splitting only changes WHERE commands run (in parallel), never WHICH commands run: the verdict is green only
if the static checks pass and every acceptance command exited 0.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprintlib import (  # noqa: E402
    BRANCH_RE, acceptance_cmds, git, hard_deps, load_cells, load_config, matches, merged_from_git,
    slist, validate,
)

MAX_SUMMARY_LINES = 30


def tail(text: str, n: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ---------------------------------------------------------------- static checks
def static_check(trusted, work, branch, title, base_ref, preflight=False):
    """Return (cell id or None, cell or None, problems, config).

    preflight=True is for a mid-work dry run (no PR yet): the title, "no files changed" and "missing output"
    checks are skipped because the work is not finished. Paths, protected files and dependencies still apply.
    """
    trusted, work = Path(trusted), Path(work)
    cfg = load_config(trusted)
    cells, problems = load_cells(trusted / "cells")
    problems = list(problems)
    cid = None

    m = BRANCH_RE.match(branch)
    if not m:
        problems.append(f"branch {branch!r} must be cell/C-NNN")
    else:
        cid = m.group(1)
        if cid not in cells:
            problems.append(f"{cid} is not in the plan")
            cid = None
    cell = cells.get(cid) if cid else None
    if not cell:
        return cid, None, problems, cfg

    if not preflight and not title.startswith(f"cell({cid}):"):
        problems.append(f"PR title must start with 'cell({cid}):'")
    errs, _ = validate(cells, cfg)
    problems += [f"plan invalid: {e}" for e in errs[:3]]
    merged = merged_from_git(work, base_ref)
    if cid in merged:
        problems.append(f"{cid} is already merged")
    unmerged = [d for d in hard_deps(cell) if d not in merged]
    if unmerged:
        problems.append("dependencies not merged yet: " + ", ".join(unmerged))

    changed = git(work, "diff", "--name-only", "--no-renames", f"{base_ref}...HEAD").splitlines()
    if not changed and not preflight:
        problems.append("PR changes no files")
    allowed = slist(cell, "allowed_paths")
    for f in changed:
        if matches(f, cfg["protected_paths"]):
            problems.append(f"protected file changed: {f}")
        elif not matches(f, allowed):
            problems.append(f"outside allowed_paths: {f}")
    for o in [] if preflight else slist(cell, "outputs"):
        if subprocess.run(["git", "-C", str(work), "cat-file", "-e", f"HEAD:{o}"],
                          capture_output=True).returncode != 0:
            problems.append(f"missing output: {o}")
    return cid, cell, problems, cfg


# ---------------------------------------------------------------- acceptance
def run_cmd(cmd, cwd, timeout):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return r.returncode, tail(r.stdout + "\n" + r.stderr, 40) if r.returncode else ""


def summarize(label, problems, results, n_cmds, acceptance_ran):
    """results: list of {index, cmd, rc, tail}. One short text with the verdict and ALL failures."""
    failed = [r for r in results if r["rc"] != 0]
    ran = {r["index"] for r in results}
    missing = [i for i in range(n_cmds) if i not in ran] if acceptance_ran and not problems else []
    if problems or failed or missing:
        out = [f"FAIL {label}"] + [f"- {p}" for p in problems[:10]]
        per = 20 if len(failed) <= 1 else max(5, (MAX_SUMMARY_LINES - 8) // len(failed))
        for r in failed:
            out.append(f"- acceptance failed (exit {r['rc']}): {r['cmd']}")
            if r.get("tail"):
                out.append(tail(r["tail"], per))
        for i in missing:
            out.append(f"- acceptance command #{i} produced no result (job crashed or was cancelled)")
        return "\n".join(out).splitlines()[:MAX_SUMMARY_LINES + 5], False
    extra = f" (acceptance {len(results)}/{n_cmds})" if acceptance_ran else " (acceptance not run)"
    return [f"PASS {label}{extra}"], True


def emit(lines, path):
    text = "\n".join(lines)
    print(text)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all", choices=["all", "check", "run-one", "aggregate"])
    ap.add_argument("--trusted", help="checkout of the base branch (plan + tools)")
    ap.add_argument("--work", help="checkout of the PR head (code under test)")
    ap.add_argument("--branch")
    ap.add_argument("--title")
    ap.add_argument("--base-ref", default="origin/main")
    ap.add_argument("--run-acceptance", action="store_true", help="mode all: also run acceptance commands")
    ap.add_argument("--preflight", action="store_true", help="mid-work dry run: skip title/outputs/empty-diff checks")
    ap.add_argument("--summary", help="write the summary to this file as well")
    ap.add_argument("--check-file", help="check.json path (modes check / run-one / aggregate)")
    ap.add_argument("--github-output", help="mode check: append matrix/ok to this file ($GITHUB_OUTPUT)")
    ap.add_argument("--index", type=int, help="mode run-one: acceptance command number")
    ap.add_argument("--result", help="mode run-one: result json path to write")
    ap.add_argument("--results-dir", help="mode aggregate: directory searched for result jsons")
    a = ap.parse_args(argv)

    if a.mode == "run-one":
        data = json.loads(Path(a.check_file).read_text())
        cmds = data["cmds"]
        if not 0 <= a.index < len(cmds):
            print(f"no acceptance command #{a.index}")
            return 1
        rc, out = run_cmd(cmds[a.index], a.work, data["timeout"])
        res = {"index": a.index, "cmd": cmds[a.index], "rc": rc, "tail": out}
        if a.result:
            Path(a.result).parent.mkdir(parents=True, exist_ok=True)
            Path(a.result).write_text(json.dumps(res), encoding="utf-8")
        print(f"#{a.index} exit {rc}: {cmds[a.index]}")
        return 1 if rc else 0

    if a.mode == "aggregate":
        data = json.loads(Path(a.check_file).read_text())
        results = []
        for f in sorted(Path(a.results_dir).rglob("*.json")) if a.results_dir else []:
            try:
                r = json.loads(f.read_text())
            except ValueError:
                continue
            if isinstance(r, dict) and "index" in r and "rc" in r:
                results.append(r)
        lines, ok = summarize(data["label"], data["problems"], results, len(data["cmds"]), True)
        emit(lines, a.summary)
        return 0 if ok else 1

    for req in ("trusted", "work", "branch", "title"):
        if getattr(a, req) is None:
            ap.error(f"--{req} is required for mode {a.mode}")
    cid, cell, problems, cfg = static_check(a.trusted, a.work, a.branch, a.title, a.base_ref, a.preflight)
    cmds = acceptance_cmds(cell) if cell else []
    label = cid or a.branch

    if a.mode == "check":
        data = {"label": label, "problems": problems, "cmds": cmds, "timeout": cfg["acceptance_timeout_s"]}
        if a.check_file:
            Path(a.check_file).parent.mkdir(parents=True, exist_ok=True)
            Path(a.check_file).write_text(json.dumps(data), encoding="utf-8")
        if a.github_output:
            runnable = not problems and bool(cmds)
            with open(a.github_output, "a", encoding="utf-8") as f:
                f.write(f"matrix={json.dumps({'index': list(range(len(cmds))) if runnable else []})}\n")
                f.write(f"runnable={'true' if runnable else 'false'}\n")
        print(f"check {label}: {len(problems)} problem(s), {len(cmds)} acceptance command(s)")
        for p in problems[:10]:
            print(f"- {p}")
        return 0  # the verdict job decides; a failed plan job would hide the other jobs' results

    # mode all
    results = []
    if not problems and a.run_acceptance:
        for i, cmd in enumerate(cmds):
            rc, out = run_cmd(cmd, a.work, cfg["acceptance_timeout_s"])
            results.append({"index": i, "cmd": cmd, "rc": rc, "tail": out})
            if rc:
                break
    lines, ok = summarize(label, problems, results, len(cmds), a.run_acceptance and not problems)
    emit(lines, a.summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
