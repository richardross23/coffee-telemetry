"""ESPHome Native API subscriber — alternative to the MQTT logger.

Connects directly to the espresso device over its Native API (port 6053,
the same protocol HA uses) and watches state changes. Replaces the
firmware-fragile MQTT publish path with a pull-style subscription that
the device speaks natively.

Phase 1 (this file): connect, list entities, log every state change.
Verifies reachability and reveals the entity surface we'll build shot
detection on.

Phase 2 (next): track shot_state transitions, build per-shot records,
write the same JSON file format the MQTT logger produces so downstream
(history, replay, channeling detector) is unchanged.

Phase 3: serve a websocket so the browser can drop mqtt.js entirely.
"""

import asyncio
import logging
import os
import sys

from aioesphomeapi import APIClient, ReconnectLogic

log = logging.getLogger("native-subscriber")

DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.1.172")
DEVICE_PORT = int(os.environ.get("DEVICE_PORT", "6053"))
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "coffee-tank")

# Map entity key (int, assigned by device) → human name for log lines.
_entity_names: dict[int, str] = {}


def _on_state(state) -> None:
    """Fires for every state change. `state` is an EntityState subclass —
    SensorState, BinarySensorState, TextSensorState, etc."""
    name = _entity_names.get(state.key, f"key={state.key}")
    value = getattr(state, "state", None)
    # SensorState has .state (float). BinarySensorState has .state (bool).
    # TextSensorState has .state (str). NumberState has .state (float).
    log.info("%s = %r", name, value)


async def _run() -> None:
    client = APIClient(DEVICE_HOST, DEVICE_PORT, DEVICE_PASSWORD,
                       client_info="coffee-telemetry-subscriber")

    async def on_connect() -> None:
        log.info("connected to %s:%s", DEVICE_HOST, DEVICE_PORT)
        # list_entities_services returns (entities, services). We only want entities.
        entities, _ = await client.list_entities_services()
        _entity_names.clear()
        for e in entities:
            _entity_names[e.key] = f"{type(e).__name__.replace('Info', '').lower()}.{e.object_id}"
        log.info("subscribed to %d entities", len(_entity_names))
        await client.subscribe_states(_on_state)

    async def on_disconnect(expected_disconnect: bool) -> None:
        log.info("disconnected (expected=%s)", expected_disconnect)

    reconnect = ReconnectLogic(
        client=client,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
        zeroconf_instance=None,
        name=DEVICE_NAME,
    )
    await reconnect.start()

    # Run forever; reconnect logic handles drops transparently.
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await reconnect.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
