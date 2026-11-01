# BLUEPRINT — agent protocol

You execute exactly ONE cell of a pre-planned build. You have no memory: the repo is the only source of truth. Be terse; tokens are your scarcest resource.

## Start
1. Call `next_cell(repo)`. You only use connector tools; you never run scripts or git commands.
2. `ALL_DONE`: reply with that text only, then stop. `NONE_READY ... retry_after`: call `next_cell(wait_seconds=45)` again, at most 8 times, then end the turn. Never pick a blocked cell. Never guess.
3. Otherwise you hold the lock on branch `cell/<ID>`. Every commit, `cell_signal heartbeat` and a running CI workflow for your cell renew it. `RESUME` = continue from that branch and follow the `next:` line of its last commit.
4. A cell marked `[critical]` is on the critical path (short lease): commit after every finished file group, never batch up work. Before one generation you expect to exceed ~5 minutes, call `cell_signal heartbeat`.
5. After `next_cell` returns `CLAIMED` or `RESUME`, begin your next message with that reply's first line, cut right after `repo=<owner/name>`, verbatim.
6. If a tool reply ends with `LIMIT_WARNING`: commit the current file group, then `submit_cell` if the cell is complete, else `cell_signal release` with `next:`, and end your turn. Never call `next_cell` again.

## Work
- Read only what the cell names: `read:` files, plus files you must edit or import. Use line ranges. Do NOT read other cells or the plan.
- `inputs[].contract` is the truth about other cells. `kind: contract` = code against it with a mock; never wait for or read the provider.
- Write only inside `allowed_paths`. The gate rejects any change to `cells/**`, `blueprint.yaml`, `BLUEPRINT.md`, `tools/**`, `.github/**`, `acceptance/**`, `docs/decisions.md`.
- Do not redesign. If a contract is wrong or impossible: make no workaround; call `cell_signal feedback` (what, why, proposed fix), then stop.
- Commit early and often with `apply_changes` (one call = one commit; batch several files per call). A file over ~150 lines is written in several `append` ops/calls with a commit between. Reply `already applied` = it went through; continue with the returned base. Pass `repo` (shown as `repo=...` in your CLAIMED/RESUME reply) in every tool call. Pass `base` (the 7-char sha from your last CLAIMED/RESUME/ok reply: it proves you still own the cell) and `next` (the exact next step). A CONFLICT reply means someone took the cell over: stop at once.

## Token budget
- New file: send full content once. Existing file: send small `old -> new` edits, never whole files.
- Never paste file contents or diffs into chat.
- If about 60% of your context is used and the cell is not nearly done: call `cell_signal release` with a `next:` line, then stop (do not only reply PARTIAL). A fresh chat adopts it at once.

## CI: use it in parallel, never poll it
- `preflight(id)` runs the full acceptance pipeline on your branch right now, without a PR (all commands in parallel, all failures in one result). Start it as soon as the main files exist, keep working, read `ci_result(id)` once later.
- `submit_cell` starts the real gate. Once submitted, CI merges the PR by itself: even if your chat ends, the work is not lost.
- `ci_result(id)` waits on the server for about 90 s. If it says `PENDING <run>`: do not call it again in a loop. Do other useful work; if there is none, end your turn with `PENDING <ID> run <n>` and let the user ask again. Your cell stays locked while the run is active.

## Finish
1. Check your work against `outputs` and `acceptance` (CI runs acceptance; you cannot).
2. `submit_cell(id, summary)`; the PR title is `cell(<ID>): <title>`.
3. `ci_result(id)`: PASS means CI merges it automatically and you are done. FAIL lists every failure (at most about 30 lines in total). Every push to the open PR re-runs the gate, so `preflight`, then fix ALL of them in ONE `apply_changes`, resubmit (max 2 attempts), otherwise `cell_signal feedback`.
4. Final reply, at most 5 lines: cell id, PASS/FAIL/PENDING, assumptions or deviations, feedback if any.
