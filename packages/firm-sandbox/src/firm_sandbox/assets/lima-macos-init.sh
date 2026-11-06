#!/bin/sh
set -eux
/bin/date
remaining=60
while [ ! -x /Volumes/cidata/lima-guestagent ]; do
  if [ "$remaining" -eq 0 ]; then
    echo "/Volumes/cidata/lima-guestagent is not executable" >&2
    exit 1
  fi
  /bin/sleep 1
  remaining=$((remaining - 1))
done
exec /Volumes/cidata/lima-guestagent fake-cloud-init
