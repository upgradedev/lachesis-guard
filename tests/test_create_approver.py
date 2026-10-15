"""The approver script creates a SUPPRESSed user in the approvers
group and never handles a password (F6)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import boto3

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "create_approver.py"


def _load():
    spec = importlib.util.spec_from_file_location("create_approver", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeIdp:
    def __init__(self):
        self.calls = []

    def admin_create_user(self, **kwargs):
        self.calls.append(("create", kwargs))

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("group", kwargs))


class _FakeCfn:
    def describe_stacks(self, StackName):
        return {"Stacks": [{"Outputs": [{"OutputKey": "UserPoolId", "OutputValue": "pool-1"}]}]}


class _FakeSession:
    def __init__(self, idp):
        self._idp = idp

    def client(self, name):
        return self._idp if name == "cognito-idp" else _FakeCfn()


def test_create_approver_suppresses_and_groups(monkeypatch, capsys):
    module = _load()
    idp = _FakeIdp()
    monkeypatch.setattr(boto3.session, "Session", lambda region_name=None: _FakeSession(idp))
    monkeypatch.setattr(sys, "argv", ["create_approver.py", "--username", "owner-approver"])
    assert module.main() == 0
    assert idp.calls[0] == (
        "create",
        {"UserPoolId": "pool-1", "Username": "owner-approver", "MessageAction": "SUPPRESS"},
    )
    assert idp.calls[1][1]["GroupName"] == "approvers"
    out = capsys.readouterr().out
    assert "admin-set-user-password" in out and "--permanent" in out
