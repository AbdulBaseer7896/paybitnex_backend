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

## 3. Defined WebSocket Event Payloads

All WebSocket events are delivered as flat, stable JSON objects. Every event contains both `type` and `event` keys so your mobile routers can match either.

### 3.1 Schema & Field Reference

| Key | Type | Description |
|---|---|---|
| `type` / `event` | `string` | `"payment_status_updated"` or `"payment_received_confirmed"` |
| `notification_id` | `integer` | Persistent database notification ID |
| `payment_id` | `string (UUID)` | ID of the payment |
| `reference` | `string` | Human-readable payment reference (e.g. `"PBX-2026-000101"`) |
| `old_status` | `string` | Previous status |
| `new_status` / `status` | `string` | Current updated status |
| `title` | `string` | Title for OS notifications / in-app snackbars |
| `message` | `string` | Detailed message with note if attached |
| `created_at` | `string (ISO 8601)` | Timestamp in UTC |

---

### 3.2 Event: `payment_status_updated` (Dispatched to Customer)

Sent to the **Customer** whenever an admin/accountant or background process changes payment status:

#### 1. Under Review (`under_review`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 101,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "submitted",
  "new_status": "under_review",
  "status": "under_review",
  "title": "Payment Under Review",
  "message": "Status for payment PBX-2026-000101 updated from submitted to under_review.",
  "created_at": "2026-11-23T17:40:00.000000+00:00"
}
```

#### 2. Verified (`verified`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 102,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "verified",
  "status": "verified",
  "title": "Payment Verified",
  "message": "Status for payment PBX-2026-000101 updated from under_review to verified. (Sender bank document verified)",
  "created_at": "2026-11-23T17:42:00.000000+00:00"
}
```

#### 3. PKR Sent (`pkr_sent`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 103,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "verified",
  "new_status": "pkr_sent",
  "status": "pkr_sent",
  "title": "PKR Sent to Your Account",
  "message": "PKR has been sent for payment PBX-2026-000101. Please verify in your bank account.",
  "created_at": "2026-11-23T17:45:00.000000+00:00"
}
```

#### 4. Completed (`completed`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 104,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "status": "completed",
  "title": "Payment Completed",
  "message": "Payment PBX-2026-000101 is now completed. Thank you!",
  "created_at": "2026-11-23T17:48:00.000000+00:00"
}
```

#### 5. Auto-Confirmed Stale Payment (`completed`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 105,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "status": "completed",
  "title": "Payment Auto-Confirmed",
  "message": "Payment PBX-2026-000101 was automatically confirmed and marked completed.",
  "created_at": "2026-11-23T18:00:00.000000+00:00"
}
```

#### 6. On Hold (`on_hold`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 106,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "on_hold",
  "status": "on_hold",
  "title": "Payment On Hold",
  "message": "Status for payment PBX-2026-000101 updated from under_review to on_hold. (Awaiting proof)",
  "created_at": "2026-11-23T18:15:00.000000+00:00"
}
```

#### 7. Rejected (`rejected`):
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 107,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "rejected",
  "status": "rejected",
  "title": "Payment Rejected",
  "message": "Status for payment PBX-2026-000101 updated from under_review to rejected.",
  "created_at": "2026-11-23T18:30:00.000000+00:00"
}
```

---

### 3.3 Event: `payment_received_confirmed` (Admin Broadcast)
Sent to **Admins and Accountants** whenever a customer marks "I received my PKR":

```json
{
  "type": "payment_received_confirmed",
  "event": "payment_received_confirmed",
  "notification_id": 108,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "status": "completed",
  "customer_id": "f6e0cf82-1ca8-4a20-9ebc-57825d17d293",
  "customer_name": "Inza Sandhu",
  "title": "Customer Confirmed Receipt",
  "message": "Inza Sandhu confirmed receipt for payment PBX-2026-000101. Note: Received in Meezan Bank",
  "created_at": "2026-11-23T18:45:00.000000+00:00"
}
```

---

## 4. Heartbeat (Ping / Pong)

To detect broken network sockets, send a heartbeat ping every 25–30 seconds:

**Client Send:** `{"type": "ping"}`  
**Server Reply:** `{"type": "pong"}`

---

## 5. Offline Synchronization via REST API

Whenever the mobile app launches or recovers from offline state:

1. **Unread Badge Count:**
   `GET /api/v1/notifications/unread-count/` -> `{"unread_count": 3}`
2. **Notifications List:**
   `GET /api/v1/notifications/?unread=true`
3. **Mark As Read:**
   `POST /api/v1/notifications/{id}/read/`
4. **Mark All Read:**
   `POST /api/v1/notifications/read-all/`
