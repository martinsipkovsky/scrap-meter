#!/bin/sh
# The end-to-end check on a throwaway stack (docs/TEST_REPORT.md): builds the
# app, seeds a week of data, starts the simulated devices and fake messengers,
# runs tests/e2e_check.py and tests/e2e_browser.py, and removes it all again.
# Run from the repository root with Docker (Git Bash on Windows works).
set -e
export COMPOSE_PROJECT_NAME=cmtest WEB_PORT=8400 LISTEN_PORTS=5600-5619
export DEFAULT_ADMIN_USER=admin DEFAULT_ADMIN_PASSWORD=e2e-admin-$$
export MSYS_NO_PATHCONV=1
HERE=$(pwd -W 2>/dev/null || pwd)
COMPOSE="docker compose -f docker-compose.yml -f tests/e2e-compose.yml"

POLL_ENABLED=false $COMPOSE up -d --build
sleep 12
docker cp tests/perf_seed.py cmtest-web-1:/srv/perf_seed.py
docker exec -w /srv cmtest-web-1 python perf_seed.py --stations 4 --days 7
docker run -d --rm --name e2e-fake --network cmtest_default -v "$HERE/tests:/t" cognex-monitor:test \
  python /t/fake_messengers.py 8099
docker run -d --rm --name e2e-dev --network cmtest_default -v "$HERE/tests:/t" cognex-monitor:test \
  sh -c "pip install -q pyftpdlib >/dev/null 2>&1; python -u /t/e2e_devices.py"
POLL_ENABLED=true $COMPOSE up -d
sleep 25

docker run --rm --network cmtest_default -v "$HERE/tests:/srv/tests" -e PYTHONPATH=/srv -e POLL_ENABLED=false \
  cognex-monitor:test python /srv/tests/e2e_check.py --base http://web:8000 --dev e2e-dev \
  --fake http://e2e-fake:8099 --user admin --password "$DEFAULT_ADMIN_PASSWORD" --out /srv/tests/e2e.json || true
docker run --rm --network cmtest_default -v "$HERE:/repo" -w /repo mcr.microsoft.com/playwright/python:v1.48.0-jammy \
  sh -c "pip install -q playwright==1.48.0 2>/dev/null; python tests/e2e_browser.py --base http://web:8000 \
  --user admin --password $DEFAULT_ADMIN_PASSWORD --sid 5 --out tests/e2e-browser.json" || true

if [ "$KEEP" != "1" ]; then
  docker stop e2e-fake e2e-dev >/dev/null 2>&1 || true
  $COMPOSE down -v
fi
