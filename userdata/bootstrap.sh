#!/bin/bash
# EC2 user data template rendered by gacha/ec2_gacha.py.
# Placeholders: __REPO_URL__ __GIT_REF__ __TTL_MINUTES__
set -euo pipefail
exec >>/var/log/aws_ec2_select_bootstrap.log 2>&1

REPO_URL="__REPO_URL__"
GIT_REF="__GIT_REF__"
TTL_MINUTES="__TTL_MINUTES__"
SRC_DIR=/opt/aws_ec2_select

echo "bootstrap start $(date -u +%FT%TZ) ref=${GIT_REF} ttl=${TTL_MINUTES}m"

# Self-destruct first so that any later failure still ends in termination
# (the controller launches with InstanceInitiatedShutdownBehavior=terminate).
shutdown -h "+${TTL_MINUTES}" "aws_ec2_select TTL reached"

dnf install -y -q git cmake gcc-c++ make openssl-devel chrony
systemctl enable --now chronyd

rm -rf "${SRC_DIR}"
git init -q "${SRC_DIR}"
git -C "${SRC_DIR}" remote add origin "${REPO_URL}"
git -C "${SRC_DIR}" fetch -q --depth 1 origin "${GIT_REF}"
git -C "${SRC_DIR}" checkout -q FETCH_HEAD
echo "source commit $(git -C "${SRC_DIR}" rev-parse HEAD)"

cmake -S "${SRC_DIR}/probe" -B "${SRC_DIR}/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "${SRC_DIR}/build" -j "$(nproc)" --target latency_probe

for _ in $(seq 1 30); do
  if chronyc waitsync 1 0.001 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
chronyc tracking || true
echo "bootstrap done $(date -u +%FT%TZ)"
