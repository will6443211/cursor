#!/bin/bash
set -euo pipefail
cd /opt/zixuan-fenxi
export TZ=Asia/Shanghai
mkdir -p /opt/zixuan-fenxi/logs
exec /usr/bin/flock -n /opt/zixuan-fenxi/logs/run.lock \
  /usr/bin/python3 /opt/zixuan-fenxi/run_snapshot.py \
  >> /opt/zixuan-fenxi/logs/cron.log 2>&1
