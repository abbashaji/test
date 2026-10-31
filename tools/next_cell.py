#!/usr/bin/env python3
"""Pick (and optionally lock) the next cell. Reference implementation of the connector's next_cell.

Run it in a fresh clone of the repo:
  python tools/next_cell.py --fetch                       # show what is ready
  python tools/next_cell.py --fetch --ci --claim          # also claim; --ci reads running workflows via `gh`
  python tools/next_cell.py --fetch --takeover C-007      # human override: take over a running cell now
  python tools/next_cell.py --fetch --takeover C-007 --force   # ...even while its CI is still running

Locking. A claim creates branch cell/<ID> on the remote with --force-with-lease=<ref>: (the ref must not
exist yet), so two agents racing for the same cell cannot both win. Each claim pushes its OWN unique empty
commit (a bare push of the same sha would be a harmless no-op for the loser). The same trick, with the lease
set to the old tip, makes a RESUME / takeover atomic: only one taker wins, and the new commit restarts the lease.

A cell is considered alive while (a) its branch has a commit younger than its lease, or (b) with --ci, a
workflow run for it is queued or running (at most max_ci_minutes). So a chat that is only waiting for CI is
not mistaken for a chat whose tokens ran out.
"""
import argparse
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprintlib import (  # noqa: E402
    ci_from_gh, claims_from_git, compute_state, git, lease_of, load_cells, load_config, merged_from_git,
    render, validate,
)


def cell_text(cell) -> str:
    c = {k: v for k, v in cell.items() if not k.startswith("_")}
    return yaml.safe_dump(c, sort_keys=False, allow_unicode=True).rstrip()


def _lock_commit(root, parent, message) -> str:
    tree = git(root, "rev-parse", f"{parent}^{{tree}}").strip()
    r = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=blueprint", "-c", "user.email=blueprint@localhost",
         "commit-tree", tree, "-p", parent, "-m", message],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("could not create lock commit: " + r.stderr.strip())
    return r.stdout.strip()


def try_claim(root, remote, cid, sha) -> bool:
    """Create the cell branch on the remote only if it does not exist yet (atomic lock)."""
    ref = f"refs/heads/cell/{cid}"
    lock = _lock_commit(root, sha, f"claim({cid}): lock {uuid.uuid4().hex}")
    r = subprocess.run(["git", "-C", str(root), "push", remote, f"{lock}:{ref}", f"--force-with-lease={ref}:"],
                       capture_output=True, text=True)
    return r.returncode == 0


def try_takeover(root, remote, cid) -> str | None:
    """Atomically move an existing cell branch forward by one lock commit. Returns the new tip, or None if
    someone else touched the branch first (they own it now) or it does not exist."""
    ref = f"refs/heads/cell/{cid}"
    tip = git(root, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/cell/{cid}", check=False).strip()
    if not tip:
        return None
    lock = _lock_commit(root, tip, f"takeover({cid}): lock {uuid.uuid4().hex}")
    r = subprocess.run(["git", "-C", str(root), "push", remote, f"{lock}:{ref}", f"--force-with-lease={ref}:{tip}"],
                       capture_output=True, text=True)
    return lock if r.returncode == 0 else None


def resume_text(root, remote, cid, cells) -> str:
    last = git(root, "log", "-1", "--format=%B", f"{remote}/cell/{cid}").strip()
    return f"RESUME {cid} on branch cell/{cid}. Last commit:\n{last}\n\n{cell_text(cells[cid])}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--ref", help="ref holding merged work (default: <remote>/<main_branch>)")
    ap.add_argument("--repo", help="owner/name for gh (default: the repo of the current directory)")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--claim", action="store_true")
    ap.add_argument("--ci", action="store_true", help="count running workflows as a sign of life (needs gh)")
    ap.add_argument("--takeover", metavar="ID", help="take over a running cell now, ignoring its lease")
    ap.add_argument("--force", action="store_true", help="with --takeover: also when its CI is still running")
    a = ap.parse_args(argv)

    cfg = load_config(a.root)
    ref = a.ref or f"{a.remote}/{cfg['main_branch']}"
    if a.fetch:
        git(a.root, "fetch", "--prune", a.remote)

    cells, load_errors = load_cells(Path(a.root) / "cells")
    errors, _ = validate(cells, cfg)
    errors = load_errors + errors
    if errors:
        print("INVALID_PLAN: fix the plan before running cells")
        for e in errors[:5]:
            print(" - " + e)
        return 2

    merged = merged_from_git(a.root, ref)
    claims = claims_from_git(a.root, a.remote)
    ci = None
    if a.ci or a.takeover:
        ci = ci_from_gh(a.repo)
        if ci is None and a.ci:
            print("WARN: could not read workflow runs (gh missing or not logged in); using commit lease only")

    if a.takeover:
        cid = a.takeover
        if cid not in cells:
            print(f"UNKNOWN_CELL {cid}")
            return 2
        if cid in merged:
            print(f"ALREADY_DONE {cid}")
            return 0
        if cid not in claims:
            print(f"NOT_RUNNING {cid}: no branch cell/{cid}; use --claim")
            return 0
        if ci and cid in ci and ci[cid] < cfg["max_ci_minutes"] and not a.force:
            print(f"CI_RUNNING {cid}: a workflow started {round(ci[cid])}m ago is still active. "
                  "The chat is probably just waiting for it. Use --force to take over anyway.")
            return 4
        if not try_takeover(a.root, a.remote, cid):
            print(f"TAKEOVER_FAILED {cid}: someone else just updated the branch")
            return 3
        print(resume_text(a.root, a.remote, cid, cells))
        return 0

    state = compute_state(cells, merged, claims, cfg["lease_minutes"], ci or {}, cfg["max_ci_minutes"],
                          cfg["bottleneck_threshold"],
                          critical_chain_threshold=cfg["critical_chain_threshold"],
                          critical_transitive_threshold=cfg["critical_transitive_threshold"],
                          critical_lease_minutes=cfg["critical_lease_minutes"],
                          park_max_minutes=cfg["park_max_minutes"])
    print(render(state))
    if not (a.claim and state["status"] == "ready"):
        return 0

    sha = git(a.root, "rev-parse", ref).strip()
    for r in state["ready"]:
        cid = r["id"]
        if r["reclaim"]:
            if try_takeover(a.root, a.remote, cid):
                print("\n" + resume_text(a.root, a.remote, cid, cells))
                return 0
            continue
        if try_claim(a.root, a.remote, cid, sha):
            lease = lease_of(cells[cid], cfg["lease_minutes"])
            print(f"\nCLAIMED {cid} on branch cell/{cid} (lease {lease} min, renewed by every commit)\n\n"
                  f"{cell_text(cells[cid])}")
            return 0
    print("\nCLAIM_FAILED: every ready cell was taken by someone else just now. Ask again.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
