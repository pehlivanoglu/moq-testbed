#!/bin/sh
set -eu

mode=headless
for arg in "$@"; do
  case "$arg" in
    --browser-mode=*) mode=${arg#*=} ;;
  esac
done

pids=""
cleanup() {
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

node /opt/moqlab/static-server.mjs &
pids="$pids $!"

display_args=""
if [ "$mode" = x11 ]; then
  if [ -z "${DISPLAY:-}" ] || [ ! -d /tmp/.X11-unix ]; then
    echo "x11 mode requires DISPLAY and /tmp/.X11-unix" >&2
    exit 1
  fi
  display_args="--ozone-platform=x11"
else
  display_args="--headless=new"
fi

chromium $display_args --no-sandbox --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 --user-data-dir=/tmp/chromium-profile \
  about:blank >/tmp/chromium.log 2>&1 &
pids="$pids $!"

node /opt/moqlab/runner.mjs "$@"
