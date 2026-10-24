"""Checks for cib.py's pure parts. Run: uv run --with requests --with pytest pytest docker -q"""
import base64
import io
import contextlib

import pytest

import cib

ENV = {"CIB_ROSSUM_URL": "https://acme.rossum.app/", "CIB_ORG_ID": "123", "CIB_REGION": "prod-eu2",
       "CIB_USERNAME": "a@b.c", "CIB_PASSWORD": "pw-1234567890", "CIB_VERSION": "v2.0.1",
       "CIB_COUPA_URL": "https://acme-test.coupahost.com", "CIB_COUPA_CLIENT_ID": "cid-abc",
       "CIB_COUPA_CLIENT_SECRET": "sec/+=x"}


def test_api_base():
    assert cib.api_base("https://acme.rossum.app/") == "https://acme.rossum.app/api/v1"
    assert cib.api_base("https://acme.rossum.app/api/v1/") == "https://acme.rossum.app/api/v1"


def test_from_env_and_validate(monkeypatch):
    monkeypatch.setattr(cib, "baked_versions", lambda: ["v1.1.0", "v2.0.1"])
    p = cib.from_env(ENV)
    assert p["api_base_url"] == "https://acme.rossum.app/api/v1" and cib.validate(p) == []
    bad = cib.from_env({**ENV, "CIB_ROSSUM_URL": "http://x", "CIB_ORG_ID": "12a", "CIB_REGION": "eu",
                        "CIB_VERSION": "v9"})
    assert len(cib.validate(bad)) == 4
    # optional ones may be empty; required ones may not
    assert cib.from_env({**ENV, "CIB_REGION": "", "CIB_VERSION": ""})["cib_version"] == ""
    with pytest.raises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
        cib.from_env({**ENV, "CIB_PASSWORD": " "})


def test_out_masks_every_encoding(capsys):
    out = cib.Out()
    out.secrets = cib.secrets_of(cib.from_env(ENV), "tok-999")
    basic = base64.b64encode(b"cid-abc:sec/+=x").decode()
    out(f"token tok-999 pw pw-1234567890 secret sec/+=x quoted sec%2F%2B%3Dx basic {basic}")
    text = capsys.readouterr().out
    for leak in ("tok-999", "pw-1234567890", "sec/+=x", "sec%2F%2B%3Dx", basic):
        assert leak not in text, (leak, text)


def _fake_run(monkeypatch, tmp_path, preflight):
    """main() with login, logout and every subprocess faked; returns the commands it ran."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cib, "TOOL", str(tmp_path))
    monkeypatch.setattr(cib, "baked_versions", lambda: ["v2.0.1"])
    monkeypatch.setattr(cib, "login", lambda p: "tok-1")
    monkeypatch.setattr(cib, "logout", lambda *a: None)
    ran = []

    def run(self, cmd, env=None):
        ran.append(cmd)
        return (0, preflight) if cmd[-1].endswith("preflight.py") or "teardown.py" in cmd[1] else (0, "")
    monkeypatch.setattr(cib.Out, "run", run)
    return ran


def test_yes_stops_on_warnings_unless_accepted(monkeypatch, tmp_path, capsys):
    ran = _fake_run(monkeypatch, tmp_path, "  WARNING: egress blocked\nPREFLIGHT OK")
    with pytest.raises(SystemExit):
        cib.main(["--yes"])
    assert "--accept-warnings" in capsys.readouterr().out and not any("cib_init_script.py" in c for c in ran)
    ran.clear()
    assert cib.main(["--yes", "--accept-warnings"]) == 0 and any("cib_init_script.py" in c for c in ran)
    assert not (tmp_path / "config.json").exists()  # the token file is removed again


def test_teardown_dry_run_then_apply(monkeypatch, tmp_path):
    ran = _fake_run(monkeypatch, tmp_path, "Dry run. Re-run with --apply to delete.")
    assert cib.main(["--teardown", "--dry-run"]) == 0 and len(ran) == 1 and "--apply" not in ran[0]
    ran.clear()
    assert cib.main(["--teardown", "--yes"]) == 0 and ran[1][-1] == "--apply" and "--org-id" in ran[0]


def test_teardown_of_an_empty_org_is_not_an_error(monkeypatch, tmp_path, capsys):
    ran = _fake_run(monkeypatch, tmp_path, "")
    monkeypatch.setattr(cib.Out, "run", lambda self, cmd, env=None: ran.append(cmd) or (1, "no manifest in _cib_deploy_manifest of org 1"))
    assert cib.main(["--teardown", "--yes"]) == 0 and len(ran) == 1
    assert "Nothing to tear down" in capsys.readouterr().out
