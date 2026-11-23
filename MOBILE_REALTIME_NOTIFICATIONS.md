# Mobile Real-Time Notifications Integration Guide

This document describes the real-time notification system implemented in PaidiX using **Django Channels (WebSocket)** and the supporting **REST API**.

---

## 1. WebSocket Endpoint & Connection

- **URL (Production):** `wss://api.paidix.com/ws/notifications/`
- **URL (Development):** `ws://localhost:8000/ws/notifications/`

### Authentication
Authentication uses the exact same JWT access token as the REST API (`/api/v1/auth/login/`).

Pass the token via:
1. **Query parameter (Recommended for mobile):**
   ```text
   wss://api.paidix.com/ws/notifications/?token=<ACCESS_TOKEN>
   ```
2. **Handshake Header (If supported by client):**
   ```http
   Authorization: Bearer <ACCESS_TOKEN>
   ```

> **Security Note:** If the token is missing, expired, or invalid, the server immediately closes the socket with close code **`4401`** (mirroring HTTP 401). When your mobile app receives code `4401`, refresh your token or prompt the user to re-authenticate.

---

## 2. Groups & Security

- **Customer Connections:** Automatically join group `user_<user_id>`. A customer will **never** receive notifications belonging to another customer or internal admin channels.
- **Admin Connections:** Users with role `admin` or `accountant` join group `user_<user_id>` AND the shared `admins` broadcast group.

---

## 3. Event JSON Payload Specifications

All WebSocket events are delivered as flat, stable JSON objects.

### Event 1: Payment Status Updated (`payment_status_updated`)
Sent to the **Customer** whenever an admin/accountant or background process changes the payment status.

```json
{
  "type": "payment_status_updated",
  "notification_id": 142,
  "payment_id": "4b68c2aa-d450-482a-bc74-1293c66f54c2",
  "old_status": "verified",
  "new_status": "pkr_sent",
  "title": "PKR Sent to Your Account",
  "message": "PKR has been sent for payment PBX-2026-000123. Please verify in your bank account.",
  "created_at": "2026-11-23T10:15:30.123456+00:00"
}
```

#### Status Values:
- `submitted`
- `under_review`
- `verified`
- `pkr_sent`
- `completed`
- `on_hold`
- `rejected`

---

### Event 2: Payment Received Confirmed (`payment_received_confirmed`)
Sent to **Admins and Accountants** whenever a customer marks "I received my PKR".

```json
{
  "type": "payment_received_confirmed",
  "notification_id": 143,
  "payment_id": "4b68c2aa-d450-482a-bc74-1293c66f54c2",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "customer_id": "b3e94208-8b5e-4a69-897b-1cb81036811f",
  "customer_name": "Muhammad Ali",
  "title": "Customer Confirmed Receipt",
  "message": "Muhammad Ali confirmed receipt for payment PBX-2026-000123. Note: Received in HBL",
  "created_at": "2026-11-23T10:18:45.654321+00:00"
}
```

---

## 4. Heartbeat (Ping / Pong)

To detect broken network sockets (e.g. WiFi-to-cellular handover, app backgrounding), the mobile client should send a heartbeat ping every 25–30 seconds:

**Client Send:**
```json
{"type": "ping"}
```

**Server Reply:**
```json
{"type": "pong"}
```

If the client does not receive `{"type": "pong"}` within 5 seconds of sending ping, it should terminate the socket and trigger reconnect logic.

---

## 5. Reconnection & Offline Synchronization

### Auto-Reconnect Strategy
- Implement exponential backoff with jitter (e.g., retry after 1s, 2s, 4s, 8s, up to 30s max).
- If connection closes with **`4401`**, do not auto-reconnect with the old token. Refresh the access token first, then reconnect.

### Syncing Missed Notifications via REST API
WebSocket events are **always persisted in the database** before being pushed over the wire. If the app was closed or disconnected, sync unread notifications upon reconnect using the REST API:

1. **Get Unread Count (For Tab / App Icon Badges):**
   ```http
   GET /api/v1/notifications/unread-count/
   Authorization: Bearer <TOKEN>
   ```
   **Response:**
   ```json
   {
     "unread_count": 3
   }
   ```

2. **Fetch Notifications List:**
   ```http
   GET /api/v1/notifications/?unread=true
   Authorization: Bearer <TOKEN>
   ```
   *(Supports standard pagination with `page` and `page_size` parameters).*

3. **Mark Notification As Read:**
   ```http
   PATCH /api/v1/notifications/<notification_id>/read/
   Authorization: Bearer <TOKEN>
   ```

4. **Mark All Notifications As Read:**
   ```http
   POST /api/v1/notifications/read-all/
   Authorization: Bearer <TOKEN>
   ```

---

## 6. Background Push Notification (FCM / APNs) Integration

WebSockets disconnect when mobile apps go into background or device sleeps.

- The backend service `myapp/notifications/services.py` contains explicit commented integration points (`[FCM Integration Point]`).
- Background notifications should wake up the app when closed, prompting the user to open it, at which point the WebSocket reconnects and synchronizes via `/api/v1/notifications/`.
