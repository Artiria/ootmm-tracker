#!/bin/bash
# build-one.sh - the part of forge that runs INSIDE OoTMM's toolchain container.
#
# Clones one ref of OoTMM/OoTMM, builds it the way its own CI does, generates
# every seed listed in /jobs/jobs.tsv and leaves, under /out/$LABEL/:
#
#   <job>/OoTMM-<hash>.z64 (+ -PlayerN for multiworld), the .ootmm patch(es),
#          the spoiler log and job.json
#   oot.sym, mm.sym   the linker's symbol table of each payload (nm -n -S):
#                     the truth the tracker's heuristics are checked against
#   data/             that version's own gi.yml, pool CSVs, scenes/npc/entrances
#   build.json, setup.log, build.log
#
# Environment: REF (tag, branch or full sha), LABEL (the out folder), and
# VERSION_LABEL (what the spoiler's "Version:" line should say). Driven by
# forge.py; nothing here is meant to be run by hand.
set -euo pipefail

t0=$(date +%s)
say() { echo "== $1 ($(( $(date +%s) - t0 ))s)"; }
fail() { echo "!! $1"; exit 1; }

OUT="/out/$LABEL"
mkdir -p "$OUT" /work
cd /work

# --- source -----------------------------------------------------------------
if [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  git init -q .
  git remote add origin https://github.com/OoTMM/OoTMM.git
  git fetch -q --depth 1 origin "$REF"
  git checkout -q FETCH_HEAD
else
  opts=()
  [ "$REF" = "master" ] || opts=(--branch "$REF")
  git -c advice.detachedHead=false clone -q --depth 1 "${opts[@]}" https://github.com/OoTMM/OoTMM.git .
fi
SHA=$(git rev-parse HEAD)
say "$REF = ${SHA:0:7}"

cp /roms/oot.z64 /roms/mm.z64 roms/

# Where the generator lives moved in v30: packages/core before, packages/generator since.
GEN=packages/generator; PROJ=@ootmm/generator
[ -d "$GEN" ] || { GEN=packages/core; PROJ=@ootmm/core; }
[ -f "$GEN/lib/cli.ts" ] || fail "no generator CLI at $GEN/lib/cli.ts: this ref is older than forge knows how to build"

# --- install ----------------------------------------------------------------
# NODE_ENV=production must NOT be set yet: pnpm would skip devDependencies.
say "install"
if   [ -x scripts/setup.sh ];        then ./scripts/setup.sh        > "$OUT/setup.log" 2>&1 || { tail -30 "$OUT/setup.log"; fail "setup.sh failed"; }
elif [ -x setup.sh ];                then ./setup.sh                > "$OUT/setup.log" 2>&1 || { tail -30 "$OUT/setup.log"; fail "setup.sh failed"; }
elif [ -x scripts/install-deps.sh ]; then ./scripts/install-deps.sh > "$OUT/setup.log" 2>&1 || { tail -30 "$OUT/setup.log"; fail "install-deps.sh failed"; }
fi
if [ -f pnpm-lock.yaml ]; then
  PM="pnpm"; RUN="pnpm exec"
  grep -q "pnpm install\|pnpm i\b" scripts/setup.sh setup.sh 2>/dev/null || pnpm install >> "$OUT/setup.log" 2>&1 || { tail -30 "$OUT/setup.log"; fail "pnpm install failed"; }
else
  PM="npx"; RUN="npx"
  npm ci >> "$OUT/setup.log" 2>&1 || { tail -30 "$OUT/setup.log"; fail "npm ci failed"; }
fi

# --- build ------------------------------------------------------------------
# Production, like the website's seeds: a Debug payload is different MIPS code
# and the tracker's signatures would be tested against code nobody plays.
say "build"
export NODE_ENV=production VERSION="$VERSION_LABEL" ENV_KEYS="$VERSION_LABEL,stable"
$PM nx run "$PROJ:build" > "$OUT/build.log" 2>&1 || { tail -60 "$OUT/build.log"; fail "build failed"; }

# The payload's symbol table, with sizes. Absolute symbols (gSaveContext) have no size.
for g in oot mm; do
  elf=$(ls "$GEN"/build/tree/Release/$g 2>/dev/null || true)
  [ -n "$elf" ] || fail "no ELF for $g under $GEN/build/tree/Release"
  (mips64-ultra-elf-nm -n -S "$elf" 2>/dev/null || mips64-elf-nm -n -S "$elf") > "$OUT/$g.sym"
done

# That version's own data, kept next to the seed for reference and for
# comparing with the tracker's own copies of the same files.
mkdir -p "$OUT/data"
find . -path '*/node_modules' -prune -o \
  \( -name gi.yml -o -name pool_oot.csv -o -name pool_mm.csv -o -name scenes.yml -o -name npc.yml -o -name entrances.yml \) -print 2>/dev/null \
  | while read -r f; do cp "$f" "$OUT/data/"; done

# --- generate ---------------------------------------------------------------
cd "$GEN"
has_preset=0
grep -q -- "'--preset'" lib/cli.ts && has_preset=1

# Older CLIs write out/ inside the package; newer ones at the repo root.
# (No `ls | head` here: under set -e a failing substitution in an assignment
# ends the script, and one of the two paths always fails.)
outdir() { for d in /work/out "$PWD/out"; do [ -d "$d" ] && { echo "$d"; return 0; }; done; echo "!! the generator wrote nothing under /work/out or $PWD/out" >&2; return 1; }
clear_out() { rm -rf /work/out "$PWD/out"; }

n_ok=0; n_skip=0; n_fail=0
while IFS=$'\t' read -r job kind arg; do
  [ -n "$job" ] || continue
  jdir="$OUT/$job"
  rm -rf "$jdir"; mkdir -p "$jdir"
  tj=$(date +%s)
  clear_out
  case "$kind" in
    preset)
      if [ "$has_preset" = 1 ]; then
        args=(--seed "forge-$LABEL-$job" --preset "$arg")
      elif [ "$arg" = "Default" ]; then
        # No --preset before v30; Default is what an empty settings map gives.
        printf 'seed: forge-%s-%s\nsettings: {}\n' "$LABEL" "$job" > "$jdir/config.yml"
        args=(--config "$jdir/config.yml")
      else
        echo "-- $job: skipped, this CLI has no --preset and '$arg' is not Default"
        printf '{"job": "%s", "kind": "%s", "arg": "%s", "status": "skipped"}\n' "$job" "$kind" "$arg" > "$jdir/job.json"
        n_skip=$((n_skip + 1)); continue
      fi ;;
    config)
      cp "/jobs/$arg" "$jdir/config.yml"
      args=(--config "$jdir/config.yml") ;;
    *) fail "unknown job kind '$kind'" ;;
  esac
  say "generate $job"
  if ! $RUN tsx ./lib/cli.ts "${args[@]}" > "$jdir/generate.log" 2>&1; then
    grep -v "^\s*at " "$jdir/generate.log" | tail -5
    printf '{"job": "%s", "kind": "%s", "arg": "%s", "status": "failed"}\n' "$job" "$kind" "$arg" > "$jdir/job.json"
    n_fail=$((n_fail + 1)); continue
  fi
  od=$(outdir)
  cp "$od"/* "$jdir/"
  # Multiworld: the CLI emits one .ootmm per world and no ROM. Each ROM comes
  # out of --patch, and they all get the same name, hence the rename.
  if ! ls "$jdir"/*.z64 >/dev/null 2>&1; then
    for p in "$jdir"/OoTMM-Patch-*.ootmm; do
      clear_out
      $RUN tsx ./lib/cli.ts --patch "$p" > "$jdir/patch.log" 2>&1 || { tail -5 "$jdir/patch.log"; fail "--patch failed for $p"; }
      base=$(basename "$p" .ootmm); base=${base/OoTMM-Patch-/OoTMM-}
      mv "$(outdir)"/*.z64 "$jdir/$base.z64"
    done
  fi
  printf '{"job": "%s", "kind": "%s", "arg": "%s", "status": "ok", "seconds": %d, "files": [' "$job" "$kind" "$arg" $(( $(date +%s) - tj )) > "$jdir/job.json"
  { ls "$jdir" | grep -E '\.(z64|ootmm|txt)$' || true; } | sed 's/.*/"&"/' | paste -sd, >> "$jdir/job.json"
  printf ']}\n' >> "$jdir/job.json"
  n_ok=$((n_ok + 1))
done < /jobs/jobs.tsv

node_v=$(node -v)
printf '{"ref": "%s", "sha": "%s", "version": "%s", "generator": "%s", "node": "%s", "seconds": %d, "jobs": {"ok": %d, "skipped": %d, "failed": %d}}\n' \
  "$REF" "$SHA" "$VERSION_LABEL" "$GEN" "$node_v" $(( $(date +%s) - t0 )) $n_ok $n_skip $n_fail > "$OUT/build.json"
say "done: $n_ok ok, $n_skip skipped, $n_fail failed"
[ "$n_fail" = 0 ]
