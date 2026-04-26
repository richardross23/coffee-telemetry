"""Subscribe to coffee/shot/end and write one JSON file per shot."""

import datetime
import json
import os
import sys

import paho.mqtt.client as mqtt

OUT = os.environ.get("OUT_DIR", "/data/shots")
BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC = "coffee/shot/end"


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"connected to {BROKER}:{PORT}, subscribing to {TOPIC}", flush=True)
        client.subscribe(TOPIC)
    else:
        print(f"connect failed rc={rc}", flush=True)


def on_message(client, userdata, msg):
    if msg.topic != TOPIC:
        return
    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError as e:
        print(f"bad json on {msg.topic}: {e}", flush=True)
        return

    shot_id = payload.get("shot_id", "unknown")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(OUT, f"{ts}_{shot_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"saved {path}", flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="shot-logger")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
