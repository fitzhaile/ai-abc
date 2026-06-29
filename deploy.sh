#!/usr/bin/env bash
# Deploy the pre-built static dashboard to data.fitzhaile.com/d/americas-boating/
# on Bluehost via FTP. There is no build step: index.html + data.js + assets/
# are committed and uploaded as-is.
#
# Bluehost's FTPS encrypts the login fine but its *data* channel TLS is broken, so
# we use --ftp-ssl-control: the login (your password) is sent encrypted, while the
# file bytes go over a plain data channel (the files are public, so that's fine).
# We also force classic PASV (--disable-epsv) because Bluehost mishandles EPSV.
#
# Credentials are read from .env (which is git-ignored) and never printed:
#   FTP_HOST, FTP_USER, FTP_PASS, FTP_DIR
#
# Usage:  ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

# Load FTP_* from .env without echoing the values.
eval "$(python3 -c '
import shlex
d = {}
for line in open(".env"):
    if "=" in line:
        k, v = line.split("=", 1)
        if k in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_DIR"):
            d[k] = v.strip().strip(chr(34)).strip(chr(39))
for k in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_DIR"):
    print(k + "=" + shlex.quote(d.get(k, "")))
')"
: "${FTP_HOST:?missing FTP_HOST in .env}"
: "${FTP_USER:?missing FTP_USER in .env}"
: "${FTP_PASS:?missing FTP_PASS in .env}"
: "${FTP_DIR:?missing FTP_DIR in .env}"

# This site is pre-built static — no build step. Make sure the source is present.
SRC=dashboard
[ -f "${SRC}/index.html" ] && [ -f "${SRC}/data.js" ] || {
  echo "ERROR: ${SRC}/index.html or ${SRC}/data.js is missing — nothing to deploy." >&2
  echo "       If the data is stale, regenerate it first, then re-run this script:" >&2
  echo "       python3 derive/extract.py && python3 derive/build_dashboard_data.py" >&2
  exit 1
}
echo "==> deploying pre-built static site from ${SRC}/ (no build step)"

# Upload everything the live page actually loads. data.json is skipped on purpose:
# index.html loads data.js, never data.json, so shipping it would just waste bandwidth.
echo "==> uploading ${SRC}/ to ${FTP_DIR} (skipping data.json — the page never loads it)"
count=0
for f in $(cd "${SRC}" && find . -type f -not -name data.json -not -name '.DS_Store'); do
  rel="${f#./}"
  curl -sS --ftp-ssl-control -k --disable-epsv --ftp-skip-pasv-ip --ftp-create-dirs \
       --connect-timeout 25 --max-time 300 \
       -T "${SRC}/${rel}" --user "${FTP_USER}:${FTP_PASS}" \
       "ftp://${FTP_HOST}${FTP_DIR}/${rel}" \
    && { echo "    OK   ${rel}"; count=$((count + 1)); } \
    || { echo "    FAIL ${rel}"; exit 1; }
done

echo "==> done: ${count} files uploaded"
echo "    live at https://data.fitzhaile.com/d/americas-boating/"
