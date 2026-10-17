"""Unit tests for the AgentCore entrypoint and SageMaker Decider client."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import BotoCoreError, ClientError

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import decider_sagemaker  # noqa: E402
import entrypoint  # noqa: E402

from lachesis.client import DeciderUnavailable  # noqa: E402


def test_workdir_seeds_once_and_run_tests_passes_after_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(entrypoint, "WORK_ROOT", tmp_path)
    session_id = "s" * 40
    workdir = entrypoint._workdir(session_id)
    assert (workdir / "refunds_prod_config.py").is_file()
    assert (workdir / "test_workspace.py").is_file()

    read_file, write_file, delete_file, run_tests = entrypoint._make_tools(workdir)
    assert "LEGACY_REFUND_CAP" in read_file(path="refunds_prod_config.py")
    assert write_file(path="cleanup_notes.txt", content="note") == "wrote cleanup_notes.txt"
    assert delete_file(path="refunds_prod_config.py") == "deleted refunds_prod_config.py"

    # Re-entering _workdir on a resumed session must not recreate the deleted file
    workdir_again = entrypoint._workdir(session_id)
    assert workdir_again == workdir
    assert not (workdir / "refunds_prod_config.py").exists()

    out = run_tests()
    assert out.startswith("exit 0\n")
    assert "1 passed" in out


def test_confine_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="outside the workspace"):
        entrypoint._confine(tmp_path, "../escape.txt")


def test_scrubbed_env_strips_aws_variables(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "token")
    env = entrypoint._scrubbed_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_handle_invocation_validates_session_id_and_prompt(monkeypatch):
    assert entrypoint.handle_invocation({"session_id": "short"})["status"] == "error"

    def fake_build(sid):
        def fake_agent(arg):
            return SimpleNamespace(stop_reason="end_turn", __str__=lambda self: "ok")

        return fake_agent, SimpleNamespace(log=[{"tool": "write_file", "verdict": "proceed"}])

    monkeypatch.setattr(entrypoint, "_build_agent", fake_build)
    assert entrypoint.handle_invocation({"session_id": "s" * 40})["status"] == "error"
    done = entrypoint.handle_invocation({"session_id": "s" * 40, "prompt": "do it"})
    assert done["status"] == "completed"
    assert done["triage"] == [{"tool": "write_file", "verdict": "proceed"}]


def test_sagemaker_client_maps_errors_to_decider_unavailable(monkeypatch):
    class FakeRuntime:
        def __init__(self, mode):
            self.mode = mode

        def invoke_endpoint(self, **kwargs):
            if self.mode == "client_error":
                raise ClientError({"Error": {"Code": "ModelError"}}, "InvokeEndpoint")
            if self.mode == "botocore":
                raise BotoCoreError()
            if self.mode == "bad_json":
                return {"Body": io.BytesIO(b"not-json")}
            if self.mode == "missing_key":
                return {"Body": io.BytesIO(b"{}")}
            return {
                "Body": io.BytesIO(
                    json.dumps(
                        {
                            "answers": {"risk_class": {"choice": "benign", "confidence": 0.9}},
                            "latency_ms": 718.0,
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ).encode()
                )
            }

    for err_mode in ("client_error", "botocore", "bad_json", "missing_key"):
        monkeypatch.setattr(
            decider_sagemaker.boto3, "client", lambda *a, m=err_mode, **k: FakeRuntime(m)
        )
        client = decider_sagemaker.SageMakerDeciderClient()
        with pytest.raises(DeciderUnavailable):
            client.ask("state", {"risk_class": {}})

    monkeypatch.setattr(decider_sagemaker.boto3, "client", lambda *a, **k: FakeRuntime("ok"))
    client = decider_sagemaker.SageMakerDeciderClient()
    res = client.ask("state", {"risk_class": {}})
    assert res.latency_ms == 718.0
    assert client.calls == 1
    assert client.health() == {}
