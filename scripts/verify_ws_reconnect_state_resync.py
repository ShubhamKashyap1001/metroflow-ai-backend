"""WebSocket reconnect state-resync verification: reconnect must get
the LAST KNOWN state
immediately, with no new broadcast needed, then keep receiving live
ticks - real manager.py + real /ws/monitor route body, real
fastapi/starlette/websockets stack."""
import asyncio
import json
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.websocket.manager import manager
from app.websocket import events

app = FastAPI()

@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    await manager.connect(websocket, user_id=None)
    try:
        while True:
            raw = await websocket.receive_text()
            manager.record_activity(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


results = []
def check(label, cond):
    results.append((label, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} - {label}")


async def broadcast_crowd(updates, ts="t"):
    await manager.broadcast(events.CROWD_UPDATE, {"updates": updates, "timestamp": ts})

async def broadcast_train(updates, ts="t"):
    await manager.broadcast(events.TRAIN_POSITION, {"updates": updates, "timestamp": ts})


def main():
    manager.bind_loop(asyncio.get_event_loop())
    client = TestClient(app)

    print("=== Step 1: connect, seed real state via normal simulator-style broadcasts ===")
    with client.websocket_connect("/ws/monitor") as ws1:
        asyncio.run(broadcast_crowd([
            {"station_id": 1, "crowd_level": "high", "current_count": 500},
            {"station_id": 2, "crowd_level": "low", "current_count": 40},
        ], ts="tick-A"))
        m1 = json.loads(ws1.receive_text())
        asyncio.run(broadcast_train([
            {"train_id": 10, "progress_ratio": 0.4, "status": "Running"},
        ], ts="tick-A"))
        m2 = json.loads(ws1.receive_text())
        check("client got crowd tick", m1["data"]["timestamp"] == "tick-A")
        check("client got train tick", m2["data"]["timestamp"] == "tick-A")

        # A single check-in style PARTIAL update for just station 1
        # (mirrors crowd_service's request-triggered coalesced notify()).
        asyncio.run(broadcast_crowd([
            {"station_id": 1, "crowd_level": "critical", "current_count": 610},
        ], ts="tick-B-partial"))
        m3 = json.loads(ws1.receive_text())
        check("client got the partial check-in update", m3["data"]["timestamp"] == "tick-B-partial")

    print("\n=== Step 2: disconnect - simulate network drop, NOT a page refresh ===")
    check("manager has zero active connections while offline", len(manager.active_connections) == 0)

    print("\n=== Step 3: reconnect - must get latest state IMMEDIATELY, with NO new broadcast ===")
    with client.websocket_connect("/ws/monitor") as ws2:
        t0 = time.monotonic()
        first = json.loads(ws2.receive_text())
        elapsed = time.monotonic() - t0

        check("first message after reconnect (before any new tick) is a crowd_update",
              first["event"] == events.CROWD_UPDATE)
        by_station = {u["station_id"]: u for u in first["data"]["updates"]}
        check("resync includes station 1 with the LATEST value (critical, from the partial update), not the stale full-tick value",
              by_station.get(1, {}).get("crowd_level") == "critical")
        check("resync includes station 2 which was never touched by the partial update (merge, not overwrite)",
              by_station.get(2, {}).get("crowd_level") == "low")
        check(f"resync arrived immediately, no new broadcast needed ({elapsed:.3f}s)", elapsed < 1.0)

        second = json.loads(ws2.receive_text())
        check("second message after reconnect is the train_position resync",
              second["event"] == events.TRAIN_POSITION and second["data"]["updates"][0]["train_id"] == 10)

        print("\n=== Step 4: continued live updates after reconnect (simulated 5-10s cadence) ===")
        for i in range(3):
            time.sleep(0.2)
            asyncio.run(broadcast_crowd([{"station_id": 1, "crowd_level": f"level-{i}"}], ts=f"cadence-{i}"))
            msg = json.loads(ws2.receive_text())
            check(f"cadence tick {i} delivered post-reconnect", msg["data"]["timestamp"] == f"cadence-{i}")

    print("\n=== SUMMARY ===")
    failed = [l for l, ok in results if not ok]
    for l, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {l}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED")
        sys.exit(1)
    print(f"\nAll {len(results)} checks PASSED")


if __name__ == "__main__":
    main()
