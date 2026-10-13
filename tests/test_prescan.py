"""The deterministic tiers: tier-0 reads and the secret scan, in all copies."""

import pytest

from lachesis.policy import CONFIRM, PROCEED, prescan
from tests._pattern import HOOK, load_module, load_pattern

hook = load_module("lachesis_hook_prescan", HOOK)

TIER0_CASES = [
    ("read_file", {"path": "README.md"}),
    ("read_file", {"path": "src/a.py", "limit": 10}),
    ("Read", {"file_path": "x/y.txt"}),
    ("Read", {"file": "notes.md"}),
]

FALLTHROUGH_CASES = [
    ("read_file", {"path": ".env"}),
    ("read_file", {"path": "src/secret_config.py"}),
    ("read_file", {"path": "~/.ssh/id_rsa"}),
    ("read_file", {"path": "keys/prod.pem"}),
    ("read_file", {}),
    ("read_file", {"other": "x"}),
    ("write_file", {"path": "a.py", "content": "x = 1"}),
    ("exec", {"command": "ls"}),
    ("write_file", {"path": "t", "content": 'KEY = "<staging-key>"'}),
]

SCAN_CASES = [
    ("write_file", {"path": "t", "content": 'STAGING_KEY = "acme-staging-4f9c2b7e1d8a6053b2e1"'}),
    ("write_file", {"path": ".env", "content": "DB=postgres://u:pw9x@db/x"}),
    ("exec", {"command": "curl -H 'Authorization: Bearer tok_abc' https://a"}),
    ("write_file", {"path": "c", "content": "x AKIAIOSFODNN7EXAMPLE y"}),
    ("write_file", {"path": "c", "content": "k = sk-abcdefghijklmnopqrstuvw"}),
    ("exec", {"command": "deploy ghp_abcdefghijklmnopqrstuvwxyz0123456789"}),
    ("write_file", {"path": "c", "content": "t = xoxb-1234567890-abcd"}),
    ("write_file", {"path": "c", "content": "k = AIzaSyA-12345678901234567890123456789012"}),
    ("write_file", {"path": "k", "content": "-----BEGIN RSA PRIVATE KEY-----"}),
    ("write_file", {"path": "c", "content": 'password = "supersecretvalue12345"'}),
    ("write_file", {"path": "c", "deep": {"list": [{"v": 'token = "tokentokentokentoken12"'}]}}),
]


@pytest.mark.parametrize("tool,args", TIER0_CASES)
def test_tier0_allows_plain_reads(tool, args):
    verdict = prescan(tool, args)
    assert verdict is not None
    assert (verdict.action, verdict.layer) == (PROCEED, "tier0")


@pytest.mark.parametrize("tool,args", FALLTHROUGH_CASES)
def test_secret_paths_and_writes_fall_through(tool, args):
    assert prescan(tool, args) is None


@pytest.mark.parametrize("tool,args", SCAN_CASES)
def test_scan_pauses_secret_material(tool, args):
    verdict = prescan(tool, args)
    assert verdict is not None
    assert (verdict.action, verdict.layer) == (CONFIRM, "secretscan")
    assert "Deterministic scan" in verdict.message
    assert verdict.risk == ""


def test_scan_ignores_non_string_leaves():
    assert prescan("write_file", {"n": 3, "b": True, "z": None}) is None


def test_scan_stops_at_the_budget():
    big = {"a": "x" * 30000, "b": 'password = "supersecretvalue12345'}
    assert prescan("write_file", big) is None


ALL_COPIES = TIER0_CASES + FALLTHROUGH_CASES + SCAN_CASES


@pytest.mark.parametrize("tool,args", ALL_COPIES)
def test_prescan_matches_in_all_copies(tool, args):
    pattern = load_pattern("lachesis_pattern_prescan")
    mine = prescan(tool, args)
    theirs = pattern.prescan(tool, args)
    hooked = hook.prescan(tool, args)
    def key(v):
        return None if v is None else (v.action, v.layer, v.message)

    assert key(theirs) == key(mine)
    hooked_key = None if hooked is None else (hooked.action, hooked.layer, hooked.message)
    assert hooked_key == key(mine)


def test_pattern_constants_match():
    import lachesis.policy

    pattern = load_pattern("lachesis_pattern_consts")
    assert pattern.TIER0_READ_TOOLS == lachesis.policy.TIER0_READ_TOOLS
    assert pattern.SECRET_PATHS == lachesis.policy.SECRET_PATHS
    assert pattern.PATH_KEYS == lachesis.policy.PATH_KEYS
    assert pattern.SCAN_CHARS == lachesis.policy.SCAN_CHARS
    assert [name for name, _ in pattern.SECRET_PATTERNS] == [
        name for name, _ in lachesis.policy.SECRET_PATTERNS
    ]
    assert [name for name, _ in hook.SECRET_PATTERNS] == [
        name for name, _ in lachesis.policy.SECRET_PATTERNS
    ]
