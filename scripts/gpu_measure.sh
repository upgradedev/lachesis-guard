#!/usr/bin/env bash
# S10 GPU measurement: serve the pinned Decider on CUDA and measure it.
#
# Runs ON a fresh g5.xlarge (Deep Learning Base OSS Nvidia Driver GPU AMI,
# Ubuntu 22.04) as user-data or by hand. Everything is logged to the file
# named by $1 (default /var/log/gpu-measure.log). The box bills by the hour,
# so the run is bounded: install, serve, measure, tour, done marker.
#
#   sudo bash gpu_measure.sh /var/log/gpu-measure.log
#
# Outputs on the box:
#   /tmp/gpu-threshold.jsonl  one row per labeled case (verdicts + latency)
#   /tmp/gpu-tour.log         the pattern tour (four verdicts or exit 1)
#   DONE marker line at the end of the log.
set -u
exec > >(tee -a "${1:-/var/log/gpu-measure.log}") 2>&1
echo "=== gpu-measure start $(date -u +%FT%TZ) on $(hostname) ==="
nvidia-smi || true

python3 -m pip --version
python3 -m pip install --break-system-packages "strands-decider==0.1.0" "transformers==5.19.0" "strands-agents" || \
  python3 -m pip install "strands-decider==0.1.0" "transformers==5.19.0" "strands-agents"

python3 - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0))
PY

git clone --depth 1 https://github.com/upgradedev/lachesis-guard /opt/lachesis

nohup strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
  --device cuda --port 8099 >/tmp/decider-serve.log 2>&1 &
echo "server pid $!"

for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8099/health; then echo; break; fi
  sleep 10
done
curl -s http://127.0.0.1:8099/health || { echo "SERVER NEVER CAME UP"; tail -50 /tmp/decider-serve.log; exit 1; }

cd /opt/lachesis
export STRANDS_DECIDER_URL=http://127.0.0.1:8099
python3 scripts/measure_threshold.py --out /tmp/gpu-threshold.jsonl || true
python3 patterns/guardrails/example.py >/tmp/gpu-tour.log 2>&1
echo "tour exit: $?"
grep -E "proceed|guide|confirm|deny" /tmp/gpu-tour.log | tail -6 || true
echo "=== DONE $(date -u +%FT%TZ) ==="
