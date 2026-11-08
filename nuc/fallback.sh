#!/usr/bin/env bash
# nuc fallback (owner 8 Nov 2026). GitHub Actions is the primary runner; the nuc ticks only when the cloud
# has not committed state for > STALE_MIN (GitHub cron skipped or delayed). It uses the same state branch
# (lights-state) and pushes back, so a tick by either runner is seen by the other: no duplicate messages,
# except if both tick in the same minute (rare, accepted). When the nuc has no power it is simply absent.
# Cloud commits are authored 'lights-bot', nuc commits 'lights-nuc'; freshness counts cloud commits only,
# so the nuc keeps covering every 15 min while the cloud is silent.
set -euo pipefail
STALE_MIN=${STALE_MIN:-50}   # cloud period 30 min + GitHub cron lag up to ~20 min
R=$(cd "$(dirname "$0")/.." && pwd)
S="$R/.state"
CRED=(-c credential.useHttpPath=true -c credential.helper=/home/i/.claude/secrets/git-credential-github.sh)

git -C "$R" pull -q --ff-only origin main || echo "[lights-fallback] code pull failed, using local copy"
[ -d "$S/.git" ] || git clone -q -b lights-state https://github.com/Philgr8/lights.git "$S"
git -C "$S" fetch -q origin lights-state
git -C "$S" reset -q --hard origin/lights-state

last=$(git -C "$S" log -1 --author=lights-bot --format=%ct || echo 0)
age=$(( ($(date +%s) - ${last:-0}) / 60 ))
if (( age <= STALE_MIN )); then
  echo "[lights-fallback] cloud fresh (${age} min ago), nothing to do"
  exit 0
fi
echo "[lights-fallback] cloud silent ${age} min: ticking on the nuc"
rc=0
LIGHTS_STATE="$S" python3 -I "$R/lights.py" tick || rc=$?
git -C "$S" add state.json
if ! git -C "$S" diff --cached --quiet; then
  git -C "$S" -c user.name=lights-nuc -c user.email=lights-nuc@users.noreply.github.com commit -qm "state (nuc) $(date -u +%FT%TZ)"
  git -C "$S" "${CRED[@]}" push -q origin HEAD:lights-state || echo "[lights-fallback] push rejected (cloud ticked meanwhile)"
fi
exit $rc
