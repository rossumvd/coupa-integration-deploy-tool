# CIB deploy in one container. Everything is downloaded at build time, so a run needs only
# Docker, the credentials and internet access to the Rossum and Coupa hosts.
#   docker build -t cib-deploy -f docker/Dockerfile .      (from the repository root)
# Settings come only from CIB_* environment variables (see docker/README.md); nothing is stored in the image.
FROM python:3.12-slim

ARG PRD2_VERSION=v2.21.1
ARG CIB_VERSIONS="v1.1.0 v2.0.0 v2.0.1"

ENV PYTHONUNBUFFERED=1

COPY . /opt/tool
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --global user.email noreply@rossum.ai && git config --global user.name cib-deploy \
 && pip install --no-cache-dir -q -r /opt/tool/requirements.txt \
 && python -m venv /opt/prd2 \
 && /opt/prd2/bin/pip install --no-cache-dir -q "git+https://github.com/rossumai/deployment-manager.git@$PRD2_VERSION" \
 && ln -s /opt/prd2/bin/prd2 /usr/local/bin/prd2 \
 && mv /opt/tool/docker /opt/cib \
 && cd /opt/tool && python -c "import sys, helpers; [helpers.download_cib_release(v) for v in sys.argv[1:]]" $CIB_VERSIONS

ENTRYPOINT ["python", "/opt/cib/cib.py"]
