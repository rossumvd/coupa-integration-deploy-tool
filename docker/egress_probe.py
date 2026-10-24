"""Check outbound HTTPS from serverless functions in the target org.

Rossum keeps this in the organisation's Django setting "Serverless function security context"
(Allow Rossum Api / Allow Https); the public API does not expose it, so we test it: create a
temporary function hook, let it call the Coupa host once, delete the hook. A function takes the
org's current setting when it is created ("Change is not applied immediately, but only after
function is updated through the API" — Django help text).

probe(api_base, token, coupa_url) -> (ok, detail): True = reached the host, False = the function
saw a network error or no answer within 10 s, None = the probe itself could not run.
"""
import time
from urllib.parse import urlparse

import requests

NAME = "deploy-web egress probe (temporary)"  # no "(CIB)": must not look like a CIB object
LIMIT_S = 10
# The HTTPS call runs in a thread with a hard limit: urlopen's timeout does not cover the DNS
# lookup, which can hang when outbound traffic is blocked.
CODE = f'''
import threading, time, urllib.request, urllib.error

def rossum_hook_request_handler(payload):
    url, out, t0 = payload.get("probe_url", ""), {{}}, time.time()
    def call():
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout={LIMIT_S - 2})
            out.update(egress="ok", http_status=r.status)
        except urllib.error.HTTPError as e:  # the host answered: outbound HTTPS works
            out.update(egress="ok", http_status=e.code)
        except Exception as e:
            out.update(egress="blocked", error=f"{{type(e).__name__}}: {{e}}"[:300])
    th = threading.Thread(target=call, daemon=True)
    th.start()
    th.join({LIMIT_S})
    if not out:
        out.update(egress="blocked", error="no answer within {LIMIT_S} s")
    out["ms"] = int((time.time() - t0) * 1000)
    return out
'''


def interpret(status_code, body, host):
    """Map the /invoke answer to (ok, detail). Anything Rossum-side is 'could not run', never 'blocked'."""
    if status_code != 200 or not isinstance(body, dict):
        return None, f"the probe function could not run: HTTP {status_code} {str(body)[:200]}"
    if body.get("egress") == "ok":
        return True, f"a serverless function reached https://{host}/ (HTTP {body.get('http_status')}, {body.get('ms')} ms)"
    if body.get("egress") == "blocked":
        return False, f"a serverless function could not reach https://{host}/: {body.get('error')}"
    return None, f"unexpected probe response: {str(body)[:200]}"


def probe(api_base, token, coupa_url, wait_s=120):
    host = urlparse(coupa_url).hostname
    if not host:
        return None, f"invalid Coupa URL {coupa_url!r}"
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    r = s.post(f"{api_base.rstrip('/')}/hooks", timeout=60, json={
        "name": NAME, "type": "function", "events": ["invocation.manual"], "queues": [], "active": True,
        "config": {"runtime": "python3.12", "code": CODE, "third_party_library_pack": None, "timeout_s": 30}})
    if r.status_code != 201:
        return None, f"could not create the probe function: HTTP {r.status_code} {r.text[:200]}"
    hook = r.json()
    try:
        deadline = time.time() + wait_s
        while (status := (s.get(hook["url"], timeout=60).json().get("config") or {}).get("status")) == "pending":
            if time.time() > deadline:
                return None, f"the probe function was still pending after {wait_s} s"
            time.sleep(3)
        if status == "failed":
            return None, "the probe function could not be built (status failed)"
        # After "pending", the function can still answer 400 "FunctionNotReadyException:
        # Initializing extension" for a while (seen 2026-10-13); retry until the deadline.
        while True:
            r = s.post(f"{hook['url']}/invoke", timeout=60, json={"probe_url": f"https://{host}/"})
            if not (r.status_code == 400 and "FunctionNotReady" in r.text) or time.time() > deadline:
                break
            time.sleep(5)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return interpret(r.status_code, body if r.status_code == 200 else str(body), host)
    finally:
        s.delete(hook["url"], timeout=60)
