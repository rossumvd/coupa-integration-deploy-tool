# CIB deploy with Docker

Deploys the Coupa Integration Baseline (CIB) into a Rossum organisation from one container. A run needs only Docker, your credentials and internet access to your Rossum and Coupa hosts. No account anywhere else, no website, no CI. The credentials never go into the image or a Docker volume: they stay in a source on your machine and reach the container as environment variables for one run.

## Quick start (Docker only)

1. Install Docker. macOS: `brew install colima docker && colima start` (`brew install docker` alone gives only the client). Windows / Linux: Docker Desktop or Docker Engine.
2. Write the credentials into a file, for example `~/.cib/acme-test.env` (keys below), and make it readable only by you (`chmod 600`).
3. Run:

```bash
docker run --rm --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4 --preflight-only   # checks only
docker run --rm -it --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4                  # preflight, then type the organisation ID to deploy
docker run --rm --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4 --yes                # unattended: deploys only if the preflight has no warnings
docker run --rm --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4 --yes --accept-warnings
docker run --rm --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4 --teardown --dry-run  # what a teardown would remove
docker run --rm -it --env-file ~/.cib/acme-test.env rossumvd/cib-deploy:1.3.4 --teardown        # remove the last deploy (type the organisation ID)
```

The first `docker run` downloads the image from Docker Hub (https://hub.docker.com/r/rossumvd/cib-deploy/tags). Tags: `<version>` of this tool, `<version>-<commit>`, `latest`.

Without access to Docker Hub, use the file you received: `docker load -i cib-deploy.tar`, then write `cib-deploy` in place of `rossumvd/cib-deploy:1.3.4`.

## Optional: the host script `cib`

Named credential sets in a file, the macOS Keychain or Bitwarden, and logs in `~/.cib/logs/`. The script comes out of the image (macOS with Homebrew; on Linux use any directory on your PATH). Set `CIB_IMAGE` to the image it runs:

```bash
docker run --rm rossumvd/cib-deploy:1.3.4 --print-wrapper > "$(brew --prefix)/bin/cib" && chmod +x "$(brew --prefix)/bin/cib"
export CIB_IMAGE=rossumvd/cib-deploy:1.3.4
```

### Credential sets

Every set has its own name (no default), e.g. `acme-test`. One set = one target organisation + its Coupa connection.

```bash
cib configure acme-test                      # asks for each value; Enter keeps the stored one
cib deploy acme-test                         # preflight, then type the organisation ID to deploy
cib deploy acme-test --yes                   # unattended: deploy if the preflight passed without warnings
cib deploy acme-test --yes --accept-warnings # unattended, also with preflight warnings
cib deploy acme-test --preflight-only
cib teardown acme-test --dry-run             # what the last deploy created and a teardown would remove
cib teardown acme-test                       # remove it (type the organisation ID to confirm; --yes skips)
cib delete acme-test                         # delete the credential set, not the deploy
cib list
```

Sources (`--source`, default `file`):

| Source | Where | Edit / delete |
|---|---|---|
| `file` | `~/.cib/NAME.env`, plain `KEY=VALUE` lines, mode 0600 | any text editor / `rm` (or `cib configure` / `cib delete`) |
| `keychain` | macOS Keychain, generic password, service `cib-deploy`, account `NAME` | `cib configure NAME --source keychain` / `cib delete NAME --source keychain`, or Keychain Access |
| `bitwarden` | Bitwarden Secure Note named `cib-deploy/NAME`, notes = the `KEY=VALUE` lines (read-only here; needs the `bw` CLI logged in; when the vault is locked `bw` asks for the master password itself, or unlock once per shell with `export BW_SESSION="$(bw unlock --raw)"`) | in Bitwarden |

The keys (`CIB_REGION` and `CIB_VERSION` may stay empty: detected / newest CIB in the image):

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

## Where things are

- Logs: on the screen (secrets masked by the container); with the `cib` script also in `~/.cib/logs/<UTC time>-<NAME>.log`.
- Credentials go only to the Rossum API you entered and to the Coupa host (OAuth check). The Rossum token is logged out at the end of every run.
- The Rossum user must be an admin of the target organisation (the login's home organisation). It becomes the owner of the CIB hook tokens.
- The values are environment variables of the running container, so `docker inspect` shows them while it runs (to anyone who can use your Docker).

## What runs inside

| Part | Version | Source |
|---|---|---|
| Deploy tool | this repository at the image's version (`VERSION`) | the repository root, unchanged |
| prd2 | `v2.21.1` (`PRD2_VERSION`) | https://github.com/rossumai/deployment-manager |
| CIB releases | `v1.1.0 v2.0.0 v2.0.1` (`CIB_VERSIONS`) | https://github.com/rossumai/rossum-coupa-integration, downloaded at build time |
| Preflight | `docker/preflight.py` | the tool's own checks up to the prd2 run, the egress probe (`docker/egress_probe.py`: one temporary function hook, deleted again), and a warning when the release's import collections already exist in Data Storage (left by an earlier deploy) |
| Manifest | `docker/record.py`, `docker/manifest.py` | stored in the org's `_cib_deploy_manifest` (run ID = UTC time `YYYYMMDDHHMMSS`), so `docker/teardown.py --from-org` can remove exactly this deploy |

The tool asks two y/N questions (newer tool, newer CIB). The container answers them with "no", so the image's pinned versions are what gets deployed. `--yes` stops on any preflight warning unless `--accept-warnings` is given.

Teardown (`--teardown`, or `cib teardown`) runs `docker/teardown.py --from-org`: it removes only the objects in the last deploy's manifest (refusing if any was renamed), drops the CIB import collections, then keeps watching for about 3 minutes and drops them again if a late import job recreates them. It reads the CIB release names from the image, so it needs no GitHub either.

## Build

A push to `main` or a version tag pushes the image: `.github/workflows/docker.yml` builds `linux/amd64` + `linux/arm64` and pushes `<DOCKERHUB_USERNAME>/cib-deploy:<VERSION without v>`, `:<VERSION without v>-<commit>` and `:latest`. The tag must match `VERSION`. The workflow can also run by hand (Actions → docker → Run workflow). Repository settings: secret `DOCKERHUB_TOKEN` (Docker Hub access token, Read & Write); variable `DOCKERHUB_USERNAME` if the Docker Hub account is not the repository owner.

Local build, and a file to send to a partner:

```bash
docker buildx build --platform linux/arm64,linux/amd64 -t cib-deploy:latest -f docker/Dockerfile --load .
docker save --platform linux/arm64 --platform linux/amd64 cib-deploy:latest -o cib-deploy.tar
```

Needs buildx and an engine with the containerd image store (colima's Docker 27 has it). The image holds no credentials, only public code and the CIB releases.

## Tests

```bash
uv run --with requests --with pytest pytest docker -q
bash docker/test_cib_wrapper.sh               # KEYCHAIN=1 also round-trips a fake set through the real Keychain
```
