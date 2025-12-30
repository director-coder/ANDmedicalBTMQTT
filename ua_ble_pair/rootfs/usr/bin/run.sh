#!/usr/bin/env sh
set -eu

CONFIG=/data/options.json

ADDRESS=$(python -c "import json;print(json.load(open('$CONFIG')).get('address',''))")
NAME_HINT=$(python -c "import json;print(json.load(open('$CONFIG')).get('name_hint','UA-651'))")
ADAPTER=$(python -c "import json;print(json.load(open('$CONFIG')).get('adapter','hci0'))")
DISCOVER_SECONDS=$(python -c "import json;print(int(json.load(open('$CONFIG')).get('discover_seconds',30)))")

exec python /usr/src/app/pair.py \
  --address "$ADDRESS" \
  --name-hint "$NAME_HINT" \
  --adapter "$ADAPTER" \
  --discover-seconds "$DISCOVER_SECONDS"
