#!/usr/bin/env python
"""
Manual WebSocket test client for PaidiX real-time notifications.

Usage:
    pip install websockets           # one-time
    python scripts/ws_test_client.py --token <YOUR_JWT_ACCESS_TOKEN>

What it does:
    1. Connects to ws://localhost:8000/ws/notifications/ using the JWT.
    2. Sends a {"type":"ping"} and expects a {"type":"pong"} back.
    3. Sits and prints every incoming notification JSON until you Ctrl-C.

Getting a token:
    curl -X POST http://localhost:8000/api/v1/auth/login/ \
         -H "Content-Type: application/json" \
         -d '{"email":"customer@example.com","password":"yourpass"}'
    # → grab the "access" field from the response JSON.

Expected output when you change a payment status from admin:
    Connected!
    ← pong: {'type': 'pong'}
    ← notification: {
        'type': 'payment_status_updated',
        'notification_id': 1,
        'payment_id': '...',
        'old_status': 'verified',
        'new_status': 'pkr_sent',
        'title': 'Payment PKR Sent',
        'message': 'PKR has been sent for payment PBX-2026-000123',
        'created_at': '2026-11-23T15:40:00Z'
    }
"""
import argparse
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("Install websockets first:  pip install websockets")
    sys.exit(1)


async def listen(url: str, token: str):
    # Token can go via header or query param.  We demonstrate both;
    # mobile clients typically use query param.
    ws_url = f"{url}?token={token}"
    # NOTE: We do NOT print ws_url because it contains the token.
    print(f"Connecting to {url} ...")

    try:
        async with websockets.connect(ws_url) as ws:
            print("Connected!\n")

            # Send a ping to verify the round-trip.
            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await ws.recv())
            print(f"← pong: {pong}\n")

            # Sit and print incoming events.
            print("Waiting for notifications (Ctrl-C to quit) ...\n")
            async for raw in ws:
                data = json.loads(raw)
                print(f"← notification: {json.dumps(data, indent=2)}\n")

    except websockets.exceptions.ConnectionClosedError as e:
        if e.code == 4401:
            print("ERROR: Connection rejected — invalid or missing token (close code 4401).")
        else:
            print(f"ERROR: Connection closed with code {e.code}: {e.reason}")
    except ConnectionRefusedError:
        print("ERROR: Could not connect. Is the dev server running on that URL?")


def main():
    parser = argparse.ArgumentParser(description="PaidiX WebSocket test client")
    parser.add_argument("--url", default="ws://localhost:8000/ws/notifications/",
                        help="WebSocket URL (default: ws://localhost:8000/ws/notifications/)")
    parser.add_argument("--token", required=True,
                        help="JWT access token (Bearer token from /api/v1/auth/login/)")
    args = parser.parse_args()
    asyncio.run(listen(args.url, args.token))


if __name__ == "__main__":
    main()
