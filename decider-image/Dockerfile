# Decider 2B BYOC image for SageMaker (A4).
#
# Base carries CUDA torch; strands-decider 0.1.0 is the version every
# measurement in this repo used (0.1.0 plus the adapter lock is the
# ADDENDUM fallback for the package risk, taken from the start). The
# pinned checkpoint is baked in at build time and served offline.
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

ARG CHECKPOINT=StrandsAgents/strands-decider-2B-qwen3.5-v1-2610

RUN pip install --no-cache-dir strands-decider==0.1.0 \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download('${CHECKPOINT}')"

ENV HF_HUB_OFFLINE=1 \
    STRANDS_DECIDER_MODEL=${CHECKPOINT} \
    DECIDER_DEVICE=cuda

COPY adapter.py /opt/program/adapter.py
WORKDIR /opt/program

EXPOSE 8080
ENTRYPOINT ["python", "/opt/program/adapter.py"]
