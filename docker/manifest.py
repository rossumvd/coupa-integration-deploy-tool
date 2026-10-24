"""Build a CIB deploy manifest: every object a deploy created in the target org.

Two sources, same output:
  from-state  prd2's deploy state file (deploy_states/cib_target.json) — used by the
              workflow right after a deploy, on a fresh runner, so it holds this run only.
  from-log    the "CREATE <type>: ..." lines of a deploy log — recovery for runs that
              finished before the workflow saved manifests (rich wraps long lines).

Usage:
  python manifest.py from-state <state.json> --org-id N --api-base URL --cib-version V --run ID
                     [--preexisting-datasets collections.json] [-o manifest.json]
  python manifest.py from-log <run.log> --org-id N --api-base URL --cib-version V --run ID [-o manifest.json]
"""
import argparse
import json
import re
import sys

# prd2 state keys / CREATE-line types -> manifest keys
TYPES = {"workspace": "workspaces", "queue": "queues", "schema": "schemas", "inbox": "inboxes",
         "engine": "engines", "engine_field": "engine_fields", "hook": "hooks", "rule": "rules",
         "label": "labels", "email_template": "email_templates"}


def _manifest(org_id, api_base, cib_version, run, created, preexisting_datasets):
    return {"org_id": org_id, "api_base": api_base, "cib_version": cib_version, "run": run,
            "created": created, "preexisting_datasets": preexisting_datasets}


def from_state(state, org_id, api_base, cib_version, run, preexisting_datasets):
    created = {}
    for key, objs in state.items():
        if key not in TYPES.values() or not isinstance(objs, dict):
            continue
        for source in objs.values():
            for target_id, dep in (source.get("deployments") or {}).items():
                if target_id.isdigit():  # skip "<NEW COPY>" placeholders of failed creates
                    name = ((dep.get("last_applied") or {}).get("forward") or {}).get("name")
                    created.setdefault(key, []).append({"id": int(target_id), "name": name})
    return _manifest(org_id, api_base, cib_version, run, created, preexisting_datasets)


def from_log(text, org_id, api_base, cib_version, run):
    # GitHub log lines start with "<job>\t<step>\t<timestamp> "; keep only the message.
    lines = [re.sub(r"^\S+Z ", "", l.split("\t")[-1]) for l in text.splitlines()]
    created, buf = {}, None
    for line in lines:
        if re.match(r"^CREATE \w+: ", line):
            buf = line
        elif buf is not None:
            buf += " " + line.strip()
        if buf is not None and re.search(r'\(\d+\)"\.\s*$', buf):
            m = re.match(r'CREATE (\w+): ".*" -> "(.*) \((\d+)\)"\.$', re.sub(r"\s+", " ", buf).strip())
            if m and m.group(1) in TYPES:
                created.setdefault(TYPES[m.group(1)], []).append({"id": int(m.group(3)), "name": m.group(2)})
            buf = None
    return _manifest(org_id, api_base, cib_version, run, created, None)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["from-state", "from-log"])
    ap.add_argument("path")
    ap.add_argument("--org-id", type=int, required=True)
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--cib-version", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--preexisting-datasets", help="JSON list of collection names that existed before the deploy")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    with open(a.path, encoding="utf-8") as f:
        if a.source == "from-state":
            pre = json.load(open(a.preexisting_datasets)) if a.preexisting_datasets else None
            m = from_state(json.load(f), a.org_id, a.api_base, a.cib_version, a.run, pre)
        else:
            m = from_log(f.read(), a.org_id, a.api_base, a.cib_version, a.run)
    out = json.dumps(m, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(out + "\n")
    else:
        print(out)
    print(f"manifest: {sum(len(v) for v in m['created'].values())} objects "
          f"({', '.join(f'{k} {len(v)}' for k, v in m['created'].items())})", file=sys.stderr)


if __name__ == "__main__":
    main()
