"""Deploy the Coupa Integration Baseline (CIB) into a Rossum organisation from a container.

The settings come only from environment variables (CIB_*); nothing is stored in the image or in
a volume. Normally the host script `cib` passes them from a named credential set
(docker run --rm cib-deploy --print-wrapper > cib); without it:

  docker run --rm -it --env-file ~/.cib/NAME.env cib-deploy [--yes] [--preflight-only]

Default: preflight, then you confirm by typing the organisation ID. --yes: deploy without asking
when the preflight passed with no warning (--accept-warnings: also with warnings). --teardown:
remove exactly what the last deploy created (its manifest), after the same confirmation. Credentials go only to the Rossum API (login, deploy) and the Coupa host
(the deploy tool's credential check); the token is logged out at the end. Every secret is masked
in the output.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

import requests

TOOL = "/opt/tool"  # coupa-integration-deploy-tool, cloned at image build
HERE = os.path.dirname(os.path.abspath(__file__))
REGIONS = ("prod-eu", "prod-eu2", "prod-us2", "prod-jp")
# environment variable -> setting; CIB_REGION and CIB_VERSION may be empty (detected / newest)
ENV = {"CIB_ROSSUM_URL": "api_base_url", "CIB_ORG_ID": "org_id", "CIB_REGION": "target_rossum_instance",
       "CIB_USERNAME": "username", "CIB_PASSWORD": "password", "CIB_VERSION": "cib_version",
       "CIB_COUPA_URL": "coupa_base_api_url", "CIB_COUPA_CLIENT_ID": "client_id",
       "CIB_COUPA_CLIENT_SECRET": "client_secret"}
OPTIONAL = ("CIB_REGION", "CIB_VERSION")


def die(msg):
    print(f"\nERROR: {msg}", flush=True)
    sys.exit(1)


def api_base(url):
    url = url.strip().rstrip("/")
    return url if url.endswith("/api/v1") else url + "/api/v1"


def from_env(environ):
    """Settings from CIB_* variables, or die listing the missing ones."""
    missing = [k for k in ENV if k not in OPTIONAL and not environ.get(k, "").strip()]
    if missing:
        die(f"missing settings: {', '.join(missing)}. Run: cib configure NAME (or docker run --env-file FILE)")
    p = {ENV[k]: environ.get(k, "").strip() for k in ENV}
    p["api_base_url"] = api_base(p["api_base_url"])
    return p


def validate(p):
    """Problems with the settings, as messages ([] = usable)."""
    out = []
    for k in ("api_base_url", "coupa_base_api_url"):
        if urllib.parse.urlparse(p[k]).scheme != "https":
            out.append(f"{k} must start with https://")
    if not p["org_id"].isdigit():
        out.append("CIB_ORG_ID must be a number")
    if p["target_rossum_instance"] not in REGIONS:
        out.append(f"CIB_REGION must be one of {', '.join(REGIONS)}")
    if p["cib_version"] not in baked_versions():
        out.append(f"CIB_VERSION must be one of {', '.join(baked_versions())} (the versions in this image)")
    return out


def baked_versions():
    d = os.path.expanduser("~/.cib_releases")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def detect_region(url):
    sys.path.insert(0, TOOL)
    from helpers import detect_region as d  # the deploy tool's own DNS lookup
    return d(url)


def secrets_of(p, token):
    out = []
    for v in (token, p["password"], p["client_id"], p["client_secret"]):
        out += [v, urllib.parse.quote(v, safe=""), json.dumps(v)[1:-1]]
    out.append(base64.b64encode(f"{p['client_id']}:{p['client_secret']}".encode()).decode())
    return sorted({s for s in out if s}, key=len, reverse=True)


class Out:
    """Prints every line with each secret replaced by ***."""

    def __init__(self):
        self.secrets = []

    def __call__(self, line=""):
        for s in self.secrets:
            line = line.replace(s, "***")
        print(line, flush=True)

    def run(self, cmd, env=None):
        """Run cmd in the tool's directory with no stdin (its y/N prompts read 'no'); returns (rc, output)."""
        proc = subprocess.Popen(cmd, cwd=TOOL, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})})
        lines = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            self(line)
        return proc.wait(), "\n".join(lines)


def login(p):
    api = p["api_base_url"]
    r = requests.post(f"{api}/auth/login", json={"username": p["username"], "password": p["password"]}, timeout=60)
    if r.status_code != 200:
        die(f"Rossum login failed: HTTP {r.status_code} {r.text[:200]}")
    token = r.json()["key"]
    me = requests.get(f"{api}/auth/user", headers={"Authorization": f"Bearer {token}"}, timeout=60).json()
    org = str(me.get("organization", "")).rstrip("/").rsplit("/", 1)[-1]
    if org != p["org_id"]:
        logout(api, token)
        die(f"{p['username']} belongs to organisation {org}, not {p['org_id']}. "
            f"Log in with an admin of organisation {p['org_id']}.")
    return token


def logout(api, token):
    try:
        requests.post(f"{api}/auth/logout", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except requests.RequestException:
        pass


def tool_config(p, token):
    return {"rossum": {"org_id": int(p["org_id"]), "api_base_url": p["api_base_url"],
                       "target_rossum_instance": p["target_rossum_instance"],
                       "token_owner_username": p["username"], "target_org_token": token,
                       "cib_version": p["cib_version"]},
            "coupa": {"coupa_base_api_url": p["coupa_base_api_url"], "client_id": p["client_id"],
                      "client_secret": p["client_secret"]}}


def confirm(what, p, host):
    answer = input(f"\n{what} organisation {p['org_id']} at {host}? Type the organisation ID to confirm: ").strip()
    return answer == p["org_id"]


def teardown(p, a, out, host):
    """teardown.py --from-org: dry run, confirmation, then --apply. It logs in by itself."""
    out.secrets = secrets_of(p, "")
    cmd = [sys.executable, os.path.join(HERE, "teardown.py"), "--from-org",
           "--api-base", p["api_base_url"], "--org-id", p["org_id"]]
    env = {"ROSSUM_USERNAME": p["username"], "ROSSUM_PASSWORD": p["password"]}
    out("\n=== Teardown plan (dry run) ===")
    rc, text = out.run(cmd, env)
    if "no manifest in" in text:  # nothing deployed, or already torn down
        out(f"\nNothing to tear down: no CIB deploy is recorded in organisation {p['org_id']}.")
        return 0
    if rc != 0 or "Dry run." not in text:
        die(f"teardown refused or failed (exit {rc}); nothing was deleted.")
    if a.dry_run:
        return 0
    if not a.yes and not confirm("Delete the CIB deploy listed above from", p, host):
        out("Not confirmed; nothing was deleted.")
        return 1
    out("\n=== Teardown ===")
    rc, _ = out.run(cmd + ["--apply"], env)
    out(f"\n{'TEARDOWN FINISHED' if rc == 0 else f'TEARDOWN FAILED (exit {rc})'}.")
    return 0 if rc == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cib-deploy", description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="deploy without asking when the preflight passed")
    ap.add_argument("--accept-warnings", action="store_true", help="with --yes: deploy also when the preflight warns")
    ap.add_argument("--preflight-only", action="store_true", help="stop after the preflight")
    ap.add_argument("--teardown", action="store_true", help="remove the last CIB deploy (from its manifest)")
    ap.add_argument("--dry-run", action="store_true", help="with --teardown: only show what would be removed")
    ap.add_argument("--print-wrapper", action="store_true", help="print the host script `cib` and exit")
    a = ap.parse_args(argv)
    if a.print_wrapper:
        with open(os.path.join(HERE, "cib")) as f:
            print(f.read(), end="")
        return 0

    p = from_env(os.environ)
    if not p["target_rossum_instance"]:
        p["target_rossum_instance"] = detect_region(p["api_base_url"]) or ""
    if not p["cib_version"] and baked_versions():
        p["cib_version"] = baked_versions()[-1]
    problems = validate(p)
    if problems:
        die("; ".join(problems))
    if not a.yes and not a.preflight_only and not a.dry_run and not sys.stdin.isatty():
        die("the confirmation needs a terminal: run with docker run -it, or pass --yes")

    out = Out()
    host = urllib.parse.urlparse(p["api_base_url"]).hostname
    if a.teardown:
        out(f"Teardown of the CIB deploy in organisation {p['org_id']} at {host}, user {p['username']}")
        return teardown(p, a, out, host)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out(f"CIB {p['cib_version']} -> organisation {p['org_id']} at {host} ({p['target_rossum_instance']}), "
        f"user {p['username']}, Coupa {p['coupa_base_api_url']}")
    token = login(p)
    out.secrets = secrets_of(p, token)
    tool_cfg = os.path.join(TOOL, "config.json")
    try:
        with open(tool_cfg, "w") as f:
            json.dump(tool_config(p, token), f)

        out("\n=== Preflight ===")
        rc, text = out.run([sys.executable, os.path.join(HERE, "preflight.py")])
        if rc != 0 or "PREFLIGHT OK" not in text:
            die(f"preflight failed (exit {rc}); nothing was deployed.")
        notes = [l.strip() for l in text.splitlines() if re.search(r"\b(WARNING|NOTE)\b", l)]
        if notes:
            out("\nPreflight warnings (read them before you deploy):")
            for n in notes:
                out(f"  - {n}")
        if a.preflight_only:
            out("\nPreflight passed.")
            return 0
        if a.yes and notes and not a.accept_warnings:
            die("the preflight has warnings and --yes deploys only without them; nothing was deployed. "
                "Fix them, or add --accept-warnings.")
        if not a.yes and not confirm(f"Deploy CIB {p['cib_version']} into", p, host):
            out("Not confirmed; nothing was deployed.")
            return 1

        out("\n=== Deploy ===")
        pre = "/tmp/pre_datasets.json"
        out.run([sys.executable, os.path.join(HERE, "record.py"), "snapshot", tool_cfg, pre])
        rc, _ = out.run([sys.executable, "cib_init_script.py"])
        # Also after a failed deploy: a partial deploy needs the teardown most.
        out.run([sys.executable, os.path.join(HERE, "record.py"), "record", tool_cfg, pre,
                 re.sub(r"\D", "", stamp)])  # numeric: teardown sorts manifests by int(run)
        out(f"\n{'DEPLOY FINISHED' if rc == 0 else f'DEPLOY FAILED (exit {rc})'}.")
        return 0 if rc == 0 else 1
    finally:
        logout(p["api_base_url"], token)
        if os.path.exists(tool_cfg):
            os.remove(tool_cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C; main's finally has logged the token out
        print("\nCancelled.")
        sys.exit(130)
