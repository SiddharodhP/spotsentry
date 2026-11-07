#!/bin/bash
# Runs once on every new instance. Variables come from the launch template's user data.
# Logs go to /var/log/spotsentry.log. A custom AMI (docs/AWS_STEPS.md) skips the installs.
set -euxo pipefail
export HOME=/root
APP=/opt/spotsentry

# Swap, so a memory spike on a small type (t3.small has 2 GiB) slows down rather than kills the job.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile || true
fi

if [ ! -x "$APP/.venv/bin/spotsentry" ]; then
  apt-get update -y
  apt-get install -y git curl
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  rm -rf "$APP"
  git clone --depth 1 --branch "$SPOTSENTRY_REPO_BRANCH" "$SPOTSENTRY_REPO_URL" "$APP"
  cd "$APP"
  uv venv --python 3.11 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
  uv pip install --python .venv/bin/python -r requirements-ml.txt \
    --index-url https://download.pytorch.org/whl/cpu
  uv pip install --python .venv/bin/python --no-deps -e .
fi

cd "$APP"
set +e
# CIFAR-10 from the job's bucket: seconds, against 30 minutes from its home server.
.venv/bin/python infra/dataset_cache.py fetch
.venv/bin/python infra/dataset_cache.py seed --wait &
.venv/bin/spotsentry run --job "$SPOTSENTRY_JOB_CONFIG"
code=$?
echo "spotsentry exited with code $code"
# 0 done (the job already scaled the group to zero), 75/76 interrupted (AWS terminates us),
# anything else is an error. Keep the instance up for debugging rather than looping.
exit "$code"
