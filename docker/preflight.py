"""Run the deploy tool's pre-deploy checks, plus an egress probe (docker/egress_probe.py).

The only write: the probe creates one temporary function hook and deletes it again.

Mirrors deploy_cib() in coupa-integration-deploy-tool v1.3.4 up to init_prd_release.
Run from the tool's directory: python /opt/cib/preflight.py
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from cib_assets import AssetResolutionError, resolve_assets  # noqa: E402
from helpers import (check_hook_templates, check_org_features, check_prd2_available, check_region,  # noqa: E402
                     check_rossum_api_version, check_target_org_empty, download_cib_release, json_to_dict,
                     make_client, normalize_base_url, resolve_token_owner_id, restore_release_to_pristine,
                     verify_credentials)

from egress_probe import probe  # noqa: E402  (same directory as this script)

cfg = json_to_dict("config.json")
rossum, coupa = cfg["rossum"], cfg["coupa"]
coupa["coupa_base_api_url"] = normalize_base_url(coupa["coupa_base_api_url"])

check_prd2_available()
check_rossum_api_version()
version = rossum["cib_version"]
prd_path = download_cib_release(version)
restore_release_to_pristine(prd_path)
try:
    assets = resolve_assets(prd_path, version, os.getcwd())
except AssetResolutionError as e:
    print(e.message())
    sys.exit(1)
verify_credentials(coupa, assets)
check_region(rossum)
client = make_client(rossum)
check_target_org_empty(client)
check_org_features(client, rossum, prd_path)
check_hook_templates(client, prd_path)
# The deploy resolves the hook token owner only inside init_prd_release; check it here too.
resolve_token_owner_id(client, rossum["token_owner_username"])
# Outbound HTTPS from functions (Django org setting) is not in the API: test it.
ok, detail = probe(rossum["api_base_url"], rossum["target_org_token"], coupa["coupa_base_api_url"])
if ok:
    print(f"  Egress check OK: {detail}")
elif ok is False:
    print(f"  WARNING: egress check: outbound HTTPS from serverless functions is blocked: {detail}")
else:
    print(f"  WARNING: egress check could not run: {detail}")
# Import jobs queued before a teardown can recreate their collections after it (seen 2026-10-16
# on 480638 and 279768). The tool does not look at Data Storage, so check the release's datasets.
import glob, json, re, requests  # noqa: E401,E402
names = set()
for path in glob.glob(os.path.join(prd_path, "cib-org", "**", "hooks", "*.json"), recursive=True):
    with open(path) as f:
        name = (((json.load(f).get("settings") or {}).get("import_config") or {}).get("dataset_name"))
    if name:
        names.add(name)
ds = re.sub(r"/api/v1/?$", "", rossum["api_base_url"]) + "/svc/data-storage/api/v1/collections/list"
existing = requests.post(ds, headers={"Authorization": f"Bearer {rossum['target_org_token']}"}, json={}, timeout=60).json().get("result", [])
left = sorted(n for n in existing if n in names or n.startswith("__tmp_"))
if left:
    print(f"  WARNING: leftover collections: Data Storage already holds {len(left)} CIB import collection(s) "
          f"from an earlier deploy ({', '.join(left)}). The deploy's import check can count them as imported; "
          f"if an import job of the old deploy still runs, the new imports can be rejected. Wait until old "
          f"imports end, drop these collections, then deploy.")
else:
    print(f"  Data Storage check OK: none of the release's {len(names)} import collections exists yet.")
print("\nPREFLIGHT OK: this organisation is ready for CIB", version)
