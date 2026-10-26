# CIB deploy

Deploys the **Coupa Integration Baseline (CIB)** into a Rossum organization from one container.

You need only **Docker**, your **credentials** and an **internet connection** to your Rossum and Coupa hosts. No other account, website or install.

## 1. Write a credentials file

One file for each target organization, for example `~/.cib/acme-test.env`. Make it readable only by you (`chmod 600 ~/.cib/acme-test.env`).

```
CIB_ROSSUM_URL=https://acme.rossum.app
CIB_ORG_ID=123456
CIB_REGION=
CIB_USERNAME=admin@acme.com
CIB_PASSWORD=...
CIB_VERSION=v2.0.1
CIB_COUPA_URL=https://acme-test.coupahost.com
CIB_COUPA_CLIENT_ID=...
CIB_COUPA_CLIENT_SECRET=...
```

- `CIB_REGION` may stay empty: the region is found from the URL.
- `CIB_VERSION` may stay empty: the newest CIB release in the image is used.
- The Rossum user must be an admin of the target organization. It becomes the owner of the CIB hook tokens.

## 2. Check, then deploy

```bash
# check only (no deploy)
docker run --rm --env-file ~/.cib/acme-test.env rossumvd/cib-deploy --preflight-only

# check, then type the organization ID to deploy
docker run --rm -it --env-file ~/.cib/acme-test.env rossumvd/cib-deploy
```

A deploy takes about 8 minutes and ends with `DEPLOY FINISHED`.

## All commands

| Do this | Add to `docker run --rm --env-file FILE rossumvd/cib-deploy` |
|---|---|
| Check the organization only | `--preflight-only` |
| Deploy after you confirm | `-it` (before the image name) |
| Deploy with no question, only when the check has no warnings | `--yes` |
| Deploy with no question, also with warnings | `--yes --accept-warnings` |
| Show what a teardown would remove | `--teardown --dry-run` |
| Remove the last deploy, after you confirm | `-it` and `--teardown` |

## What the check looks at

- The Coupa OAuth client is valid and has the scopes for all imports and the export.
- The region matches the organization.
- The organization has no CIB objects yet (fresh installs only).
- The organization allows hook timeouts up to 360 s, and the required Store hook templates are visible.
- Rossum serverless functions can reach your Coupa host (warning only). The check creates one temporary function hook and deletes it again.
- No CIB import collections are left in Data Storage from an earlier deploy (warning only).

## Credentials

- They stay in your file. They never go into the image or a Docker volume.
- They go only to the Rossum API in the file and to your Coupa host.
- The container is removed after the run (`--rm`), the Rossum token is logged out, and the output masks secret values.

## Tags

| Tag | Meaning |
|---|---|
| `latest` | the newest build |
| `1.3.4` | deploy tool v1.3.4 (newest build of that version) |
| `1.3.4-<commit>` | one exact build |

Each image works on Intel/AMD and Apple silicon. It contains the deploy tool, prd2 v2.21.1 and the CIB releases v1.1.0, v2.0.0 and v2.0.1.

## Teardown (test organizations)

`--teardown` removes exactly what the last deploy of this image created. It reads the list from the `_cib_deploy_manifest` collection in the organization, shows it, and asks you to type the organization ID. Objects that someone renamed stop the teardown. Then it drops the CIB import collections and waits until Data Storage is quiet.

## Source

https://github.com/rossumvd/coupa-integration-deploy-tool (folder `docker/`)
