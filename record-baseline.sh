#!/usr/bin/env bash
# Record benchmark baselines on a quiet machine, detached from any session.
#
# ADR-0005: a baseline is only worth recording on a machine that is not busy,
# so this waits for you to quit everything before it measures. It writes both
# the baseline file and a full log, so nothing needs to be watching it.
#
#   ./record-baseline.sh [delay-seconds]     # default 120
#
# Detach it from the terminal that starts it:
#   nohup ./record-baseline.sh 120 > /dev/null 2>&1 &
set -uo pipefail
cd "$(dirname "$0")"

DELAY="${1:-120}"
LOG="benchmarks/recording-$(date +%Y%m%d-%H%M%S).log"
mkdir -p benchmarks

{
  echo "=== baseline recording run"
  echo "started:  $(date)"
  echo "waiting:  ${DELAY}s for the machine to go quiet"
  echo
} > "$LOG"

sleep "$DELAY"

{
  echo "=== machine at measurement time"
  uptime
  echo
  pmset -g batt 2>/dev/null | head -2
  echo
  echo "=== top CPU consumers"
  ps aux | sort -k3 -rn | head -6 | awk '{printf "%-7s %6s%%cpu %s\n", $2, $3, substr($0, index($0,$11), 60)}'
  echo
} >> "$LOG" 2>&1

# A dry run first: it reports the numbers and the calibration spread without
# writing anything, so the log records what the machine looked like even if the
# quiet-machine gate then refuses the recording.
{
  echo "=== dry run (no baseline written)"
  uv run python -m PyLOB.bench --repeats 5
  echo "dry-run exit: $?"
  echo
} >> "$LOG" 2>&1

{
  echo "=== recording"
  uv run python -m PyLOB.bench --repeats 5 --rebaseline
  echo "rebaseline exit: $?"
  echo
  echo "finished: $(date)"
} >> "$LOG" 2>&1

# Leave a stable name pointing at this run, so it is findable without knowing
# the timestamp.
ln -sf "$(basename "$LOG")" benchmarks/recording-latest.log
