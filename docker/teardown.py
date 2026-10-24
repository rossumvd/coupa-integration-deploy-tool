"""Remove exactly what one CIB deploy created — nothing else in the organisation.

Reads a manifest (see manifest.py), checks every object against the live org, prints
the plan, and deletes only with --apply. An object is deleted only if it still exists
under the same id AND the same name; a renamed object makes the whole run refuse.

Usage:
  python teardown.py <manifest.json> --profile <rossum-auth profile id> [--apply] [--keep-datasets]
  (or ROSSUM_USERNAME / ROSSUM_PASSWORD in the environment instead of --profile)

The login must be a user whose home organisation is the manifest's org: Data Storage
takes the organisation from the token, and this is the guard against the wrong org.
"""
import argparse
import io
import json
import os
import re
import sys
import tarfile
import time
import tomllib
from urllib.parse import urlparse

import requests

# Delete order follows the API's dependency rules (Rossum OpenAPI spec):
# rules/hooks reference queues; a queue delete is async; schemas can go only once no
# queue uses them; engines only without queues; workspaces only when empty.
ORDER = ["rules", "hooks", "queues", "inboxes", "email_templates", "schemas", "engines", "engine_fields",
         "labels", "workspaces"]
MEMORIZATION = ["_supplier_memorization_test", "_customer_memorization_test", "_tax_code_memorization"]
MANIFEST_COLLECTION = "_cib_deploy_manifest"  # written by docker/record.py
POLL_S, QUIET_S = 30, 180  # late-import watch after the dataset drop
# *.rossum.ai also hosts third-party sites (status page, docs); allow only the API clusters there.
LEGACY_HOSTS = {"elis.rossum.ai", "us.app.rossum.ai", "shared-jp.app.rossum.ai"}


def check_api_base(api_base, profile_base):
    """The password is sent to this host, so it must be a Rossum API and the profile's own host."""
    u = urlparse(str(api_base))
    if u.scheme != "https" or u.username or u.port or u.path.rstrip("/") != "/api/v1" or not (
            re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.rossum\.app", u.hostname or "") or u.hostname in LEGACY_HOSTS):
        return f"{api_base} is not a Rossum API URL (https://<name>.rossum.app/api/v1)"
    if profile_base and urlparse(profile_base).hostname != u.hostname:
        return f"{api_base} does not match the profile's host {profile_base}"
    return None


def check_org_manifest(doc, org_id, api_base):
    """Anyone in the org can write to the manifest collection: accept only a manifest for this org and host."""
    if doc.get("org_id") != org_id or str(doc.get("api_base", "")).rstrip("/") != api_base.rstrip("/"):
        return f"manifest is for org {doc.get('org_id')} at {doc.get('api_base')}, not org {org_id} at {api_base}"
    return None


def release_names(version):
    """Object names per type in the CIB release: a deploy gives its objects these names."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", str(version)):
        sys.exit(f"invalid CIB version in manifest: {version!r}")
    names = {}

    def add(f):
        try:
            d = json.load(f)
        except ValueError:
            return
        if isinstance(d, dict) and isinstance(d.get("name"), str) and "/api/v1/" in str(d.get("url", "")):
            names.setdefault(d["url"].split("/api/v1/")[1].split("/")[0], set()).add(d["name"])

    # The Docker image carries the releases (the deploy tool's cache): no GitHub at run time.
    local = os.path.join(os.path.expanduser(os.environ.get("CIB_RELEASES_DIR", "~/.cib_releases")), version)
    if os.path.isdir(local):
        for root, _, files in os.walk(os.path.join(local, "cib-org", "default")):
            for fn in files:
                if fn.endswith(".json"):
                    with open(os.path.join(root, fn)) as f:
                        add(f)
        return names
    r = requests.get(f"https://api.github.com/repos/rossumai/rossum-coupa-integration/tarball/{version}", timeout=120)
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
        for member in tar.getmembers():
            if member.isfile() and "/cib-org/default/" in member.name and member.name.endswith(".json"):
                add(tar.extractfile(member))
    return names


def foreign_names(manifest, release):
    """Manifest objects whose name is not a CIB release name. Schemas and inboxes may carry their queue's name."""
    queues = release.get("queues", set())
    out = []
    for typ, objs in manifest["created"].items():
        if typ == "email_templates":  # taken live from the CIB queues, not from the manifest
            continue
        allowed = release.get(typ, set()) | (queues if typ in ("schemas", "inboxes") else set())
        out += [f"{typ} {o['id']} '{o['name']}'" for o in objs if o.get("name") not in allowed]
    return out


def newest(docs):
    """The manifest of the latest deploy run (GitHub run ids grow)."""
    return max(docs, key=lambda d: int(d["run"]), default=None)


def plan(manifest, live, collections, import_names, keep_datasets):
    """live: {(type, id): current name} for objects that still exist."""
    steps, refuse, gone = [], [], []
    for typ in ORDER:
        ids = []
        for obj in manifest["created"].get(typ, []):
            key = (typ, obj["id"])
            if key not in live:
                gone.append(f"{typ} {obj['id']} '{obj['name']}'")
            elif obj["name"] is not None and live[key] != obj["name"]:
                refuse.append(f"{typ} {obj['id']}: name is now '{live[key]}', manifest says '{obj['name']}'")
            else:
                ids.append(obj["id"])
        if ids:
            steps.append({"type": typ, "ids": ids})
    datasets = []
    if not keep_datasets:
        pre = set(manifest.get("preexisting_datasets") or [])
        names = set(MEMORIZATION) | set(import_names)
        datasets = sorted(c for c in collections if c not in pre and
                          (c in names or any(c.startswith(f"__tmp_{n}") for n in import_names)))
    return {"steps": steps, "refuse": refuse, "gone": gone, "datasets": datasets}


def leftover(collections, planned, import_names, preexisting):
    """CIB collections still present after the drop: planned ones, memorization ones, and
    __tmp_<import> collections an import job may have written after the plan was made."""
    pre = set(preexisting or [])
    keep = set(planned) | set(MEMORIZATION)
    return sorted(c for c in collections if c not in pre and
                  (c in keep or any(c.startswith(f"__tmp_{n}") for n in import_names)))


class Api:
    def __init__(self, api_base, token):
        self.base, self.s = api_base.rstrip("/"), requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.ds = re.sub(r"/api/v1$", "", self.base) + "/svc/data-storage/api/v1"

    def _req(self, method, url, **kw):
        # Rossum answers 429 after a few hundred quick calls; wait as told, else back off.
        for attempt in range(8):
            r = self.s.request(method, url, timeout=60, **kw)
            if r.status_code not in (429, 502, 503, 504):  # 502-504: temporary gateway errors (seen 2026-10-13)
                return r
            ra = r.headers.get("Retry-After")
            time.sleep(float(ra) if ra else min(2 ** attempt, 30))
        return r

    def get(self, path, **params):
        r = self._req("GET", f"{self.base}/{path}", params=params)
        return None if r.status_code == 404 else r.raise_for_status() or r.json()

    def delete(self, path, **params):
        r = self._req("DELETE", f"{self.base}/{path}", params=params)
        if r.status_code not in (202, 204, 404):
            raise RuntimeError(f"DELETE {path}: HTTP {r.status_code} {r.text[:200]}")

    def collections(self):
        r = self._req("POST", f"{self.ds}/collections/list", json={})
        r.raise_for_status()
        return set(r.json().get("result", []))

    def find(self, collection):
        r = self._req("POST", f"{self.ds}/data/find", json={"collectionName": collection, "query": {}})
        return r.json().get("result", []) if r.ok else []

    def open_imports(self, names):
        """MDH import operations of these datasets that have not ended yet."""
        mdh = re.sub(r"/api/v1$", "", self.base) + "/svc/master-data-hub/api/v1/operation/"
        out = []
        for n in sorted(names):
            r = self._req("GET", mdh, params={"dataset_name": n, "limit": 3})
            ops = r.json().get("operations", []) if r.ok else []
            out += [f"{n}:{o.get('status')}" for o in ops if o.get("status") not in ("finished", "failed")]
        return out

    def drop(self, name):
        r = self._req("POST", f"{self.ds}/collections/drop", json={"collectionName": name})
        if r.status_code not in (200, 202, 404):
            raise RuntimeError(f"drop {name}: HTTP {r.status_code} {r.text[:200]}")


def profile_base(profile):
    with open(os.path.expanduser("~/.rossum/profiles.toml"), "rb") as f:
        return tomllib.load(f)["profiles"][profile]["base_url"]


def login(api_base, profile):
    if profile:
        env = open(os.path.expanduser("~/.rossum/credentials.env")).read()
        val = lambda k: re.search(rf"^ROSSUM_{k}_{re.escape(profile)}=(.*)$", env, re.M).group(1).strip().strip("'\"")
        user, pwd = val("USERNAME"), val("PASSWORD")
    else:
        user, pwd = os.environ["ROSSUM_USERNAME"], os.environ["ROSSUM_PASSWORD"]
    r = requests.post(f"{api_base}/auth/login", json={"username": user, "password": pwd, "max_token_lifetime_s": 3600},
                      timeout=60, allow_redirects=False)
    if r.status_code != 200:
        sys.exit(f"login failed: HTTP {r.status_code}")
    return user, r.json()["key"]


def read_live(api, manifest):
    live = {}
    for typ, objs in manifest["created"].items():
        for o in objs:
            cur = api.get(f"{typ}/{o['id']}")
            if cur is not None:
                live[(typ, o["id"])] = cur.get("name")
    return live


def run(manifest, api, apply, keep_datasets, release):
    foreign = foreign_names(manifest, release)
    if foreign:
        print(f"REFUSED — {len(foreign)} manifest object(s) do not carry a CIB {manifest.get('cib_version')} name:")
        print("\n".join(f"  {f}" for f in foreign[:20]))
        return 2
    live = read_live(api, manifest)
    # Rossum creates email templates for each new queue itself; they belong to our queues.
    templates = []
    for (typ, qid) in [k for k in live if k[0] == "queues"]:
        for t in (api.get("email_templates", queue=qid, page_size=100) or {}).get("results", []):
            templates.append({"id": t["id"], "name": t["name"]})
            live[("email_templates", t["id"])] = t["name"]
    manifest = {**manifest, "created": {**manifest["created"], "email_templates": templates}}
    import_names = set()
    for (typ, hid) in [k for k in live if k[0] == "hooks"]:
        name = ((api.get(f"hooks/{hid}") or {}).get("settings") or {}).get("import_config", {}).get("dataset_name")
        if name:
            import_names.add(name)
    p = plan(manifest, live, api.collections(), import_names, keep_datasets)

    docs = {qid: (api.get("annotations", queue=qid, page_size=1) or {}).get("pagination", {}).get("total")
            for s in p["steps"] if s["type"] == "queues" for qid in s["ids"]}
    print(f"\nOrg {manifest['org_id']} — CIB {manifest.get('cib_version')} from run {manifest.get('run')}")
    for s in p["steps"]:
        print(f"  delete {len(s['ids']):4} {s['type']}")
    print(f"  drop   {len(p['datasets']):4} datasets: {', '.join(p['datasets']) or '-'}"
          + ("  (--keep-datasets)" if keep_datasets else ""))
    if manifest.get("preexisting_datasets") is None and p["datasets"]:
        print("  NOTE: this manifest has no record of datasets that existed before the deploy.")
    print(f"  documents in CIB queues (go with the queues): {sum(v or 0 for v in docs.values())}")
    print(f"  already gone: {len(p['gone'])}")
    if p["refuse"]:
        print("\nREFUSED — these objects changed since the deploy, nothing will be deleted:")
        print("\n".join(f"  {r}" for r in p["refuse"]))
        return 2
    if not apply:
        print("\nDry run. Re-run with --apply to delete.")
        return 0

    for s in p["steps"]:
        typ = s["type"]
        for oid in s["ids"]:
            if typ == "queues":
                api.delete(f"queues/{oid}", delete_after="00:00:00")  # default is a 24 h delay
            elif api.get(f"{typ}/{oid}") is not None:  # may already be gone with its queue/engine
                api.delete(f"{typ}/{oid}")
        if typ == "queues":
            deadline = time.time() + 900
            while any(api.get(f"queues/{q}") is not None for q in s["ids"]):
                if time.time() > deadline:
                    raise RuntimeError("queues still exist after 15 min; re-run later to finish")
                time.sleep(15)
        print(f"  deleted {typ}")
    for name in p["datasets"]:
        api.drop(name)
    if p["datasets"]:
        # Drops are async, and import jobs queued before the hooks were deleted can still start and
        # recreate their collections (seen 2026-10-16 on 480638 and 279768, minutes later). So: drop
        # again until no import runs and the collections stayed away for QUIET_S.
        # ponytail: fixed quiet window; a job queued longer than that still slips through (preflight warns).
        print(f"  dropped {len(p['datasets'])} datasets; waiting until no late import recreates them …")
        quiet, deadline, left_ds = 0, time.time() + 900, []
        while quiet < QUIET_S and time.time() < deadline:
            time.sleep(POLL_S)
            left_ds = leftover(api.collections(), p["datasets"], import_names, manifest.get("preexisting_datasets"))
            busy = api.open_imports(import_names)
            for name in left_ds:
                api.drop(name)
            quiet = 0 if left_ds or busy else quiet + POLL_S
        print("  Data Storage quiet" if quiet >= QUIET_S else f"  WARNING: still not quiet after 15 min: {left_ds}")
        print("  NOTE: the MDH operation log of these datasets stays. A redeploy from the website runs a full\n"
              "  first import; a deploy by other means resumes from this deploy's watermark and imports\n"
              "  almost nothing (backfill from a literal 1970-01-01T00:00:00Z anchor).")

    left = read_live(api, manifest)
    print(f"\nVerification: {len(left)} manifest object(s) still exist" + (f": {sorted(left)[:10]}" if left else ""))
    if left:
        return 1
    if MANIFEST_COLLECTION in api.collections():
        api.drop(MANIFEST_COLLECTION)  # last: only once nothing it describes is left
        print(f"  dropped {MANIFEST_COLLECTION}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", help="manifest file; omit with --from-org")
    ap.add_argument("--from-org", action="store_true", help=f"read the manifest from the org's {MANIFEST_COLLECTION}")
    ap.add_argument("--api-base", help="with --from-org, e.g. https://acme.rossum.app/api/v1")
    ap.add_argument("--org-id", type=int, help="with --from-org")
    ap.add_argument("--profile", help="rossum-auth profile id (reads ~/.rossum/credentials.env)")
    ap.add_argument("--apply", action="store_true", help="delete; without it only the plan is printed")
    ap.add_argument("--keep-datasets", action="store_true", help="do not drop Data Storage collections")
    a = ap.parse_args(argv)
    if a.from_org == bool(a.manifest) or (a.from_org and not (a.api_base and a.org_id)):
        ap.error("give a manifest file, or --from-org with --api-base and --org-id")
    manifest = json.load(open(a.manifest)) if a.manifest else {"api_base": a.api_base, "org_id": a.org_id}
    err = check_api_base(manifest["api_base"], profile_base(a.profile) if a.profile else None)
    if err:
        sys.exit(err)
    user, token = login(manifest["api_base"], a.profile)
    api = Api(manifest["api_base"], token)
    try:
        if a.from_org:
            manifest = newest(api.find(MANIFEST_COLLECTION))
            if manifest is None:
                sys.exit(f"no manifest in {MANIFEST_COLLECTION} of org {a.org_id}")
            err = check_org_manifest(manifest, a.org_id, a.api_base)
            if err:
                sys.exit(err)
        me = api.get("auth/user")
        if not me["organization"].rstrip("/").endswith(f"/{manifest['org_id']}"):
            sys.exit(f"{user} belongs to {me['organization']}, not org {manifest['org_id']} — refusing")
        return run(manifest, api, a.apply, a.keep_datasets, release_names(manifest.get("cib_version")))
    finally:
        api.s.post(f"{api.base}/auth/logout", timeout=30)


if __name__ == "__main__":
    sys.exit(main())
