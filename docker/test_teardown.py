import json
import os
from pathlib import Path

import pytest

import manifest
import teardown

# Run logs hold raw API output, so they live outside the repo (CIB_RUNS_DIR, default ~/.cib/runs).
RUN7 = Path(os.environ.get("CIB_RUNS_DIR", "~/.cib/runs")).expanduser() / "run-38267147595-org480638.log"


@pytest.mark.skipif(not RUN7.exists(), reason=f"run log {RUN7} is not on this machine")
def test_manifest_from_run7_log_has_every_created_object():
    m = manifest.from_log(RUN7.read_text(), org_id=480638, api_base="https://vasek.rossum.app/api/v1",
                          cib_version="v2.0.1", run="38267147595")
    counts = {k: len(v) for k, v in m["created"].items()}
    assert counts == {"workspaces": 6, "queues": 7, "schemas": 7, "inboxes": 7, "engines": 2,
                      "engine_fields": 104, "hooks": 33, "rules": 53}
    assert {"id": 1399158, "name": "MDH - Main (CIB)"} in m["created"]["hooks"]
    assert m["preexisting_datasets"] is None  # not recorded for this run


def test_manifest_from_state_reads_target_ids_and_names():
    state = {"queues": {"100": {"deployments": {"9001": {"last_applied": {"forward": {"name": "Q1"}}}}}},
             "workspaces": {"200": {"deployments": {"9002": {"last_applied": {"forward": {"name": "W1"}}}}}},
             "labels": {}}
    m = manifest.from_state(state, org_id=1, api_base="https://x.rossum.app/api/v1", cib_version="v2.0.1",
                            run="r", preexisting_datasets=["keep_me"])
    assert m["created"] == {"queues": [{"id": 9001, "name": "Q1"}], "workspaces": [{"id": 9002, "name": "W1"}]}
    assert m["preexisting_datasets"] == ["keep_me"]


def _plan(objects, collections=(), keep_datasets=False, preexisting=None, import_names=()):
    m = {"org_id": 1, "created": objects, "preexisting_datasets": preexisting}
    live = {(t, o["id"]): o["name"] for t, objs in objects.items() for o in objs}
    return teardown.plan(m, live, set(collections), set(import_names), keep_datasets)


def test_plan_orders_deletes_by_api_dependencies():
    objs = {"workspaces": [{"id": 1, "name": "W"}], "queues": [{"id": 2, "name": "Q"}],
            "schemas": [{"id": 3, "name": "S"}], "hooks": [{"id": 4, "name": "H"}], "rules": [{"id": 5, "name": "R"}],
            "engines": [{"id": 6, "name": "E"}], "inboxes": [{"id": 7, "name": "I"}],
            "engine_fields": [{"id": 8, "name": "F"}]}
    order = [s["type"] for s in _plan(objs)["steps"]]
    assert order.index("rules") < order.index("queues") < order.index("schemas")
    assert order.index("hooks") < order.index("queues")
    assert order.index("queues") < order.index("workspaces") and order.index("queues") < order.index("engines")


def test_plan_refuses_renamed_object_and_skips_gone_one():
    objs = {"hooks": [{"id": 4, "name": "H"}, {"id": 5, "name": "Gone"}]}
    live = {("hooks", 4): "Somebody renamed it"}  # 5 is gone
    p = teardown.plan({"org_id": 1, "created": objs, "preexisting_datasets": []}, live, set(), set(), False)
    assert p["refuse"] == ["hooks 4: name is now 'Somebody renamed it', manifest says 'H'"]
    assert p["gone"] == ["hooks 5 'Gone'"]


def test_plan_datasets_only_cib_names_present_and_not_preexisting():
    cols = {"_supplier_memorization_test", "suppliers_test", "__tmp_suppliers_test_x", "customer_own", "_tax_code_memorization"}
    p = _plan({}, cols, preexisting=["_tax_code_memorization"], import_names={"suppliers_test"})
    assert sorted(p["datasets"]) == ["__tmp_suppliers_test_x", "_supplier_memorization_test", "suppliers_test"]
    assert _plan({}, cols, keep_datasets=True, preexisting=[], import_names={"suppliers_test"})["datasets"] == []


def test_manifest_collection_is_never_in_the_dataset_plan():
    # it is dropped only after the verification, see teardown.run
    p = _plan({}, {"_cib_deploy_manifest", "_supplier_memorization_test"}, preexisting=[])
    assert p["datasets"] == ["_supplier_memorization_test"]


def test_newest_manifest_picked_from_org_documents():
    docs = [{"run": "1", "created": {}}, {"run": "3", "created": {}}, {"run": "2", "created": {}}]
    assert teardown.newest(docs)["run"] == "3"
    assert teardown.newest([]) is None


def test_api_retries_on_429_and_honours_retry_after(monkeypatch):
    class R:
        def __init__(self, code, headers=None):
            self.status_code, self.headers = code, headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)
        def json(self):
            return {"name": "ok"}
    replies = [R(429, {"Retry-After": "2"}), R(429), R(200)]
    sleeps = []
    api = teardown.Api("https://x.rossum.app/api/v1", "t")
    monkeypatch.setattr(api.s, "request", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(teardown.time, "sleep", sleeps.append)
    assert api.get("hooks/1") == {"name": "ok"}
    assert sleeps == [2.0, 2]


def test_leftover_datasets_include_late_tmp_collections_only():
    cols = {"suppliers_test", "__tmp_purchase_orders_test_2026-10-12 15:28", "customer_own", "_tax_code_memorization"}
    left = teardown.leftover(cols, planned=["suppliers_test"], import_names={"purchase_orders_test"}, preexisting=[])
    assert sorted(left) == ["__tmp_purchase_orders_test_2026-10-12 15:28", "_tax_code_memorization", "suppliers_test"]
    assert teardown.leftover(cols, [], {"purchase_orders_test"}, ["_tax_code_memorization"]) == \
        ["__tmp_purchase_orders_test_2026-10-12 15:28"]


def test_security_api_base_must_be_rossum_and_match_profile():
    assert teardown.check_api_base("https://vasek.rossum.app/api/v1", "https://vasek.rossum.app") is None
    assert "not a Rossum API" in teardown.check_api_base("https://evil.com/api/v1", None)
    assert "not a Rossum API" in teardown.check_api_base("https://status.rossum.ai/api/v1", None)
    assert "profile" in teardown.check_api_base("https://other.rossum.app/api/v1", "https://vasek.rossum.app")
    assert teardown.check_api_base("https://elis.rossum.ai/api/v1", None) is None


def test_security_objects_must_carry_release_names():
    release = {"hooks": {"MDH - Main (CIB)"}, "queues": {"AP Documents - DE"}, "schemas": {"Base"}}
    m = {"created": {"hooks": [{"id": 1, "name": "MDH - Main (CIB)"}, {"id": 2, "name": "Customer own hook"}],
                     "schemas": [{"id": 3, "name": "AP Documents - DE"}, {"id": 4, "name": "Base"}],
                     "workspaces": [{"id": 5, "name": "Anything"}]}}
    assert teardown.foreign_names(m, release) == ["hooks 2 'Customer own hook'", "workspaces 5 'Anything'"]


def test_security_org_manifest_must_match_requested_org_and_host():
    doc = {"org_id": 480638, "api_base": "https://vasek.rossum.app/api/v1", "run": "1", "created": {}}
    assert teardown.check_org_manifest(doc, 480638, "https://vasek.rossum.app/api/v1") is None
    assert teardown.check_org_manifest({**doc, "org_id": 1}, 480638, "https://vasek.rossum.app/api/v1")
    assert teardown.check_org_manifest({**doc, "api_base": "https://evil.com/api/v1"}, 480638, "https://vasek.rossum.app/api/v1")


def test_api_retries_temporary_gateway_errors(monkeypatch):
    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)
        def json(self):
            return {"name": "ok"}
    replies = [R(502), R(503), R(504), R(200)]
    api = teardown.Api("https://x.rossum.app/api/v1", "t")
    monkeypatch.setattr(api.s, "request", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(teardown.time, "sleep", lambda s: None)
    assert api.get("hooks/1") == {"name": "ok"}


def test_release_names_from_the_local_release_cache(tmp_path, monkeypatch):
    d = tmp_path / "v2.0.1" / "cib-org" / "default" / "hooks"
    d.mkdir(parents=True)
    (d / "h.json").write_text(json.dumps({"name": "MDH - Main (CIB)", "url": "https://cib.rossum.app/api/v1/hooks/1"}))
    (d / "bad.json").write_text("{")
    monkeypatch.setenv("CIB_RELEASES_DIR", str(tmp_path))
    monkeypatch.setattr(teardown.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no GitHub")))
    assert teardown.release_names("v2.0.1") == {"hooks": {"MDH - Main (CIB)"}}
