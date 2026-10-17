# AgentCore Runtime image for the Lachesis agent (A6). ARM64: AgentCore
# Runtime V2 runs arm64 microVMs. The repo's src/ ships inside so the
# hosted agent runs the same handler code the tests pin.
FROM python:3.12-slim

RUN pip install --no-cache-dir "strands-agents==1.61.1" boto3 "pytest==8.3.4"

COPY src/lachesis /opt/app/lachesis
COPY agent/entrypoint.py agent/decider_sagemaker.py /opt/app/

WORKDIR /opt/app
EXPOSE 8080
ENTRYPOINT ["python", "/opt/app/entrypoint.py"]
