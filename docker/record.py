"""Record what a deploy created, so docker/teardown.py can remove exactly that.

  python record.py snapshot <config.json> <out.json>   before the deploy: Data Storage collections that already exist
  python record.py record <config.json> <pre.json> <run_id>
        after the deploy: build the manifest from prd2's deploy state, print it between
        markers (the website offers it for download) and store it in the org's
        Data Storage collection _cib_deploy_manifest.
"""
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest as manifest_mod  # noqa: E402

BEGIN, END = "===CIB-MANIFEST-BEGIN===", "===CIB-MANIFEST-END==="
COLLECTION = "_cib_deploy_manifest"


def render(m):
    return f"{BEGIN}\n{json.dumps(m, separators=(',', ':'))}\n{END}"


def _ds(cfg):
    base = re.sub(r"/api/v1/?$", "", cfg["rossum"]["api_base_url"]) + "/svc/data-storage/api/v1"
    return base, {"Authorization": f"Bearer {cfg['rossum']['target_org_token']}"}


def stable_listing(list_once, quiet=30, every=10, limit=300, sleep=time.sleep):
    """The listing once it has not changed for `quiet` s. A teardown just before drops
    collections asynchronously; recording one mid-drop as pre-existing makes the next teardown keep it."""
    cur, same, waited = list_once(), 0, 0
    while same < quiet and waited < limit:
        sleep(every)
        waited += every
        nxt = list_once()
        same = same + every if nxt == cur else 0
        cur = nxt
    if same < quiet:
        print(f"WARNING: Data Storage collections still changing after {limit} s; recording the last listing.")
    return cur


def snapshot(cfg, out):
    base, h = _ds(cfg)

    def list_once():
        r = requests.post(f"{base}/collections/list", headers=h, json={}, timeout=60)
        r.raise_for_status()
        return sorted(r.json().get("result", []))

    with open(out, "w") as f:
        json.dump(stable_listing(list_once), f)


def record(cfg, pre_path, run_id):
    r = cfg["rossum"]
    state_path = os.path.expanduser(f"~/.cib_releases/{r['cib_version']}/deploy_states/cib_target.json")
    if not os.path.exists(state_path):
        print(f"No deploy state at {state_path}: prd2 created nothing, so there is no manifest.")
        return
    pre = json.load(open(pre_path)) if os.path.exists(pre_path) else None
    m = manifest_mod.from_state(json.load(open(state_path)), r["org_id"], r["api_base_url"], r["cib_version"],
                                run_id, pre)
    print(render(m), flush=True)
    base, h = _ds(cfg)
    resp = requests.post(f"{base}/data/insert_one", headers=h, timeout=60,
                         json={"collectionName": COLLECTION, "document": m, "waitForFullWrite": True})
    print(f"Manifest stored in Data Storage collection {COLLECTION}: HTTP {resp.status_code}")


if __name__ == "__main__":
    cmd, cfg_path, *rest = sys.argv[1:]
    cfg = json.load(open(cfg_path))
    snapshot(cfg, *rest) if cmd == "snapshot" else record(cfg, *rest)
