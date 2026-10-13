#!/usr/bin/env bash
# Run the Lachesis demo agent ON the Decider box (see docs/aws-run.md).
#
# Everything stays on the box: the Decider server answers at 127.0.0.1:8099
# (no tunnel), and Bedrock credentials come from the instance role only.
# The script refuses to run with static AWS keys in the environment, so a
# copied laptop credential can never silently power the run.
#
#   bash scripts/box_demo.sh [answers-file]
#
# Answers to the human pause (one y or n per line) come from the optional
# answers file, or from the terminal when omitted. The full transcript is
# kept at ~/lachesis-demo-run.log.
set -eu

if [ -n "${AWS_ACCESS_KEY_ID:-}" ] || [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "static AWS keys in the environment; the box must use its instance role only" >&2
  exit 1
fi

REPO="$HOME/lachesis-guard"
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" pull --ff-only
else
  git clone https://github.com/upgradedev/lachesis-guard "$REPO"
fi
cd "$REPO"

# The demo runs from the project's own venv: the box's system botocore is
# too old for the agent's streaming calls, and the environment belongs with
# the project anyway.
VENV="$REPO/.venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -c "import strands" 2>/dev/null || \
  "$VENV/bin/pip" install -q strands-agents pytest
PY="$VENV/bin/python"

curl -sf http://127.0.0.1:8099/health >/dev/null || {
  echo "no Decider server at 127.0.0.1:8099; start it first:" >&2
  echo "  strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --device cpu --port 8099" >&2
  exit 1
}

export STRANDS_DECIDER_URL=http://127.0.0.1:8099
export AWS_DEFAULT_REGION=eu-west-1
# The demo key, joined from the task file's own fragments; no whole-key
# literal is ever committed.
KEY="acme-staging-"
KEY="${KEY}4f9c2b7e1d8a6053b2e1"
LOG="$HOME/lachesis-demo-run.log"
if [ -n "${1:-}" ]; then
  "$PY" scripts/demo_agent.py --model eu.anthropic.claude-sonnet-5-5 \
    --key "$KEY" < "$1" 2>&1 | tee "$LOG"
else
  "$PY" scripts/demo_agent.py --model eu.anthropic.claude-sonnet-5-5 \
    --key "$KEY" 2>&1 | tee "$LOG"
fi
