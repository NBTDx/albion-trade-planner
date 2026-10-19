#!/bin/bash
# Start the local price collector and the Albion Data Client.
# The data client reads the game's network traffic, so it needs your password (sudo).
# Market pages you open in-game go only to market.db here: '-i' points the client's "public"
# uploader at the local collector instead of the Albion Online Data Project. ('-i noop' crashes
# client 0.1.61, and '-d' turns off every upload, the local one included.)
# The client lives in tools/albiondata-client/ (unzip the release there), or set ALBION_CLIENT.
cd -- "$(dirname "$0")"

PORT=8766
CLIENT=${ALBION_CLIENT:-./tools/albiondata-client/albiondata-client/albiondata-client-executable}
if [ ! -x "$CLIENT" ]; then
  echo "Albion Data Client not found at $CLIENT"
  echo "Download it from https://github.com/ao-data/albiondata-client/releases and unzip it into"
  echo "tools/albiondata-client/, or run: ALBION_CLIENT=/path/to/client ./start-collector.sh"
  exit 1
fi
LISTENER=$(lsof -nP -t -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | head -1)
if [ -n "$LISTENER" ] && ! ps -o command= -p "$LISTENER" | grep -q collector.py; then
  echo "Port $PORT is taken by another program (PID $LISTENER): $(ps -o command= -p "$LISTENER")"
  echo "Stop it or change PORT in collector.py and this script."
  exit 1
fi
if [ -z "$LISTENER" ]; then
  python3 collector.py >> collector.log 2>&1 &
  COLLECTOR=$!
  trap 'kill $COLLECTOR 2>/dev/null' EXIT
  sleep 1
fi
echo "Collector log: $(pwd)/collector.log   (python3 collector.py status shows what's captured)"
echo "Starting the Albion Data Client; enter your password if asked."
echo "Change zones once (walk through a door) if it says the location isn't set."
sudo "$CLIENT" -i http://127.0.0.1:$PORT
