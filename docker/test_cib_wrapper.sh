#!/bin/bash
# Checks for the host script docker/cib with a fake `docker` and a fake `bw` on PATH (fake values only).
# Run: bash docker/test_cib_wrapper.sh           (KEYCHAIN=1 also tests the real macOS Keychain)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CIB_HOME="$T/home" PATH="$T/bin:$PATH"
mkdir -p "$T/bin"
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }

# fake docker: records its arguments and the CIB_* values it would hand to the container
cat >"$T/bin/docker" <<'EOF'
#!/bin/bash
echo "ARGS $*" >"$CIB_HOME/../docker.args"
env | grep '^CIB_' | grep -v '^CIB_HOME=' | sort >"$CIB_HOME/../docker.env"
echo "container output"
exit 3
EOF
# fake bw: the Secure Note of cib-deploy/bwset
cat >"$T/bin/bw" <<'EOF'
#!/bin/bash
[ "$1 $2 $3" = "get notes cib-deploy/bwset" ] || { echo "Not found." >&2; exit 1; }
printf 'CIB_ROSSUM_URL=https://bw.rossum.app\nCIB_ORG_ID=7\nCIB_PASSWORD=bw-pass word=#x\n'
EOF
chmod +x "$T/bin/docker" "$T/bin/bw"
CIB="$HERE/cib"
ANSWERS=$'https://acme.rossum.app\n123\n\na@b.c\nsecret pass=1\n\nhttps://acme-test.coupahost.com\ncid-1234567890\ncsec-abcdefghij\n'

# configure (file): every prompt answered, then file content and mode
printf '%s' "$ANSWERS" | "$CIB" configure acme >/dev/null 2>&1
check "configure writes ~/.cib/acme.env" '[ -f "$CIB_HOME/acme.env" ]'
check "file mode 600" '[ "$(stat -f %Lp "$CIB_HOME/acme.env" 2>/dev/null || stat -c %a "$CIB_HOME/acme.env")" = 600 ]'
check "value with spaces and = kept" 'grep -qx "CIB_PASSWORD=secret pass=1" "$CIB_HOME/acme.env"'
check "empty optional stays empty" 'grep -qx "CIB_REGION=" "$CIB_HOME/acme.env"'

# configure again, Enter everywhere except the org ID: other values kept, secrets hinted not shown
# under a pseudo-terminal (script), since read -p shows its prompt only on a terminal
out="$( (sleep 1; printf '\n456\n\n\n\n\n\n\n\n') | script -q /dev/null "$CIB" configure acme 2>&1)"
check "re-configure changes only what was typed" 'grep -qx CIB_ORG_ID=456 "$CIB_HOME/acme.env" && grep -qx "CIB_PASSWORD=secret pass=1" "$CIB_HOME/acme.env"'
check "password never hinted" '! grep -q "pass=1\|ass=1" <<<"$out"'
check "client id hint is last 4 only" 'grep -q "\*\*\*\*7890" <<<"$out" && ! grep -q "cid-123456" <<<"$out"'

# deploy (file): values reach docker as -e NAME (never as argument), exit code and log kept
"$CIB" deploy acme --yes >/dev/null 2>&1; rc=$?
check "deploy passes the container's exit code" '[ $rc = 3 ]'
check "secrets are not docker arguments" '! grep -q "secret pass\|csec-" "$T/docker.args"'
check "-e NAME for every key, --yes passed" 'grep -q -- "-e CIB_PASSWORD" "$T/docker.args" && grep -q -- "cib-deploy --yes" "$T/docker.args"'
check "container receives the values" 'grep -qx "CIB_PASSWORD=secret pass=1" "$T/docker.env" && grep -qx CIB_ORG_ID=456 "$T/docker.env"'
check "log written" 'grep -q "container output" "$CIB_HOME"/logs/*-acme.log'
check "log mode 600" '[ "$(stat -f %Lp "$CIB_HOME"/logs/*-acme.log 2>/dev/null || stat -c %a "$CIB_HOME"/logs/*-acme.log)" = 600 ]'

# names, unknown keys, missing sets
check "no name is refused" '! "$CIB" deploy --yes >/dev/null 2>&1'
check "path-like name is refused" '! "$CIB" deploy ../x >/dev/null 2>&1'
check "missing set is refused" '! "$CIB" deploy nope >/dev/null 2>&1'
printf 'CIB_ORGID=1\n' >"$CIB_HOME/typo.env"
check "unknown key is refused" '"$CIB" deploy typo 2>&1 | grep -q "unknown key .CIB_ORGID."'
check "list shows names" '[ "$("$CIB" list | tr "\n" " ")" = "acme typo " ]'

# bitwarden (fake bw): notes become the values; configure is refused with instructions
"$CIB" deploy bwset --source bitwarden >/dev/null 2>&1
check "bitwarden values reach docker" 'grep -qx "CIB_PASSWORD=bw-pass word=#x" "$T/docker.env" && grep -qx CIB_ORG_ID=7 "$T/docker.env"'
check "bitwarden configure is read-only" '"$CIB" configure bwset --source bitwarden 2>&1 | grep -q "Secure Note named .cib-deploy/bwset."'

# macOS Keychain (real): only with KEYCHAIN=1; uses account cib-selftest-$$ and deletes it again
if [ "${KEYCHAIN:-}" = 1 ]; then
  K="cib-selftest-$$"
  printf '%s' "$ANSWERS" | "$CIB" configure "$K" --source keychain >/dev/null 2>&1
  "$CIB" deploy "$K" --source keychain --preflight-only >/dev/null 2>&1
  check "keychain round trip" 'grep -qx "CIB_PASSWORD=secret pass=1" "$T/docker.env" && grep -qx "CIB_COUPA_CLIENT_SECRET=csec-abcdefghij" "$T/docker.env"'
  "$CIB" delete "$K" --source keychain >/dev/null 2>&1
  check "keychain delete" '! security find-generic-password -s cib-deploy -a "$K" >/dev/null 2>&1'
fi

echo "$fails failure(s)"
[ $fails = 0 ]
