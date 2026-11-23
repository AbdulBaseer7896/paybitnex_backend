# PaidiX: Real-Time Notifications System — Complete Technical & Event Specification

**Branch:** `feature/realtime-notifications`  
**Repository:** [AbdulBaseer7896/paybitnex_backend](https://github.com/AbdulBaseer7896/paybitnex_backend)  
**Pull Request:** [Create Pull Request](https://github.com/AbdulBaseer7896/paybitnex_backend/pull/new/feature/realtime-notifications)  
**Core Stack:** Django 6.1.3, Channels 4.3.2, Daphne 4.1.2, Redis 5.2.1 / channels_redis 4.2.1, SimpleJWT 5.3.1  
**Verification:** 12/12 Automated Tests Passing (100%), Verified Live on Daphne ASGI  

---

## 1. System Architecture Overview

```
                               Mobile Client / Web Frontend
                                             │
                             ┌───────────────┴───────────────┐
                             │ WebSocket / HTTPS (Port 443)  │
                             └───────────────┬───────────────┘
                                             ▼
                                    Nginx Reverse Proxy
                                             │
                     ┌───────────────────────┴───────────────────────┐
                     │                                               │
           /api/* (HTTP REST)                               /ws/* (WebSocket)
                     ▼                                               ▼
             Gunicorn / WSGI                                  Daphne ASGI (Port 8001)
                     │                                               │
                     ├───────────────────────┬───────────────────────┤
                     ▼                       ▼                       ▼
               SQLite / Postgres        Redis (Pub/Sub)         Celery Worker
               (Database WAL)         (Channel Layer 6379)    (Background Sweeps)
```

---

## 2. Connection, Authentication & Channels

### 2.1 WebSocket Endpoints
- **Production:** `wss://api.paidix.com/ws/notifications/`
- **Development:** `ws://localhost:8000/ws/notifications/`

### 2.2 JWT Authentication Flow
Authentication uses the standard PaidiX SimpleJWT access token (`/api/v1/auth/login/`):
- **Query Parameter (Primary / Mobile Recommended):**
  ```text
  wss://api.paidix.com/ws/notifications/?token=<ACCESS_TOKEN>
  ```
- **Handshake Header:**
  ```http
  Authorization: Bearer <ACCESS_TOKEN>
  ```
- **Security & Rejection Code `4401`:** If the token is missing, expired, or invalid, the socket immediately closes with code `4401` (mirroring HTTP 401). The mobile client must catch code `4401`, refresh the access token, and reconnect.
- **Zero Token Leakage:** Authentication tokens are stripped and never printed in logs or exceptions.

### 2.3 Channel Layer & Role-Based Group Routing
- **Customer:** Joins strictly isolated personal group `user_<customer_id>`. Customers **never** receive notifications belonging to other customers or internal administrative channels.
- **Admin & Accountant:** Join personal group `user_<admin_id>` **AND** the broadcast group `admins`.
- **Dual Mode (Dev / Prod):**
  - **Local Dev:** Set `USE_INMEMORY_CHANNEL_LAYER="1"` to run without Redis.
  - **Production:** Redis channel layer bridges separate worker processes (Daphne, Gunicorn, Celery workers) via Redis Pub/Sub.

---

## 3. Defined WebSocket Events Specification

This section defines every single event payload emitted by the WebSocket server across all payment lifecycle transitions.

### 3.1 Field-by-Field Payload Schema
Every notification pushed over the socket contains the following standard contract:

| Field | Type | Description |
|---|---|---|
| `type` | `string` | Primary event identifier (e.g. `"payment_status_updated"` or `"payment_received_confirmed"`). |
| `event` | `string` | Event alias for frontend/mobile routers expecting `data.event`. Matches `type`. |
| `notification_id` | `integer` | Unique ID of the persisted database notification (`Notification.id`). |
| `payment_id` | `string (UUID)` | UUID of the associated `IncomingPayment`. |
| `reference` | `string` | Human-readable payment reference (e.g. `"PBX-2026-000123"`). |
| `old_status` | `string` | Previous status code before this transition (e.g. `"under_review"`). |
| `new_status` | `string` | New current status code (e.g. `"verified"`). |
| `status` | `string` | Current status code alias. Matches `new_status`. |
| `title` | `string` | Human-readable alert title suitable for in-app banners / OS push headers. |
| `message` | `string` | Detailed message describing the status update (includes note if provided). |
| `created_at` | `string (ISO 8601)` | Timestamp in UTC format (`"2026-11-23T17:45:19.629308+00:00"`). |
| `customer_id` | `string (UUID)` | *(Admin events only)* UUID of the customer confirming payment. |
| `customer_name` | `string` | *(Admin events only)* Full name or email of the customer. |

---

### 3.2 Event: `payment_status_updated` (Customer Channel)
Dispatched to `user_<customer_id>` whenever an admin, accountant, or system sweep changes payment status.

Below are the exact payloads received by the mobile client for each status transition:

#### Transition 1: Status changed to `under_review`
Triggered when compliance / accountant starts reviewing submitted proof documents:
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

#### Transition 2: Status changed to `verified`
Triggered when accountant verifies sender proof and locks the exchange rate & fee:
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

#### Transition 3: Status changed to `pkr_sent`
Triggered when accountant dispatches the outgoing PKR transfer to the customer's bank account:
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

#### Transition 4: Status changed to `completed` (Manual Customer Confirm)
Triggered when customer confirms receipt of PKR in their local account:
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

#### Transition 5: Status changed to `completed` (Auto-Confirmed Stale Payment)
Triggered by Celery beat sweep when customer hasn't confirmed within the 48-hour deadline:
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

#### Transition 6: Status changed to `on_hold`
Triggered when an admin places payment on hold (e.g. requesting additional KYC / transaction documents):
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
  "message": "Status for payment PBX-2026-000101 updated from under_review to on_hold. (Awaiting clear selfie / sender verification)",
  "created_at": "2026-11-23T18:15:00.000000+00:00"
}
```

#### Transition 7: Status changed to `rejected`
Triggered when payment is rejected:
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
  "message": "Status for payment PBX-2026-000101 updated from under_review to rejected. (Third-party sender transfer rejected)",
  "created_at": "2026-11-23T18:30:00.000000+00:00"
}
```

---

### 3.3 Event: `payment_received_confirmed` (Admin & Accountant Broadcast)
Dispatched to the shared Channels group `admins` whenever a customer confirms receipt:

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

### 3.4 Keep-Alive Ping / Pong
The client sends a ping frame every 25 seconds:
- **Client Frame:** `{"type": "ping"}`
- **Server Response:** `{"type": "pong"}`

If pong is not received within 5 seconds, the client should trigger reconnect logic.

---

## 4. Mobile Client Integration Pattern (React Native / Flutter / iOS / Android)

When the mobile app connects to `wss://api.paidix.com/ws/notifications/?token=<TOKEN>`:

```typescript
// Example WebSocket message handler in mobile app:
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === "pong") {
    // Keep-alive acknowledgment
    return;
  }

  if (data.type === "payment_status_updated") {
    // 1. Update UI badge / unread counter
    badgeCountStore.increment();

    // 2. Update active transaction in local store (Redux / Zustand / Riverpod)
    paymentStore.updatePaymentStatus(data.payment_id, data.new_status);

    // 3. Display user-friendly in-app notification banner
    showInAppBanner({
      title: data.title,
      body: data.message,
      paymentId: data.payment_id,
    });
  }

  if (data.type === "payment_received_confirmed") {
    // Admin dashboard real-time table refresh
    adminDashboard.refreshLiveFeed(data);
  }
};
```

---

## 5. Offline Synchronization via REST API

Whenever the mobile app launches, wakes from background, or recovers from network drop, it calls the REST API to synchronize missed notifications:

1. **Get Unread Count (for Tab Badges):**
   ```http
   GET /api/v1/notifications/unread-count/
   Authorization: Bearer <TOKEN>
   ```
   **Response:** `{"unread_count": 3}`

2. **Fetch Notifications List (Paginated):**
   ```http
   GET /api/v1/notifications/?unread=true
   Authorization: Bearer <TOKEN>
   ```

3. **Mark Single Notification as Read:**
   ```http
   POST /api/v1/notifications/{id}/read/
   Authorization: Bearer <TOKEN>
   ```

4. **Mark All Notifications as Read:**
   ```http
   POST /api/v1/notifications/read-all/
   Authorization: Bearer <TOKEN>
   ```

---

## 6. Dependencies & Requirements (`requirements.txt`)

The following packages are installed and pinned in `requirements.txt`:
```ini
daphne==4.1.2
channels==4.3.2
channels_redis==4.2.1
```

---

## 7. Production Deployment (Daphne & Nginx)

### Daphne Systemd Unit (`/etc/systemd/system/paybitnex-daphne.service`)
```ini
[Unit]
Description=PaidiX Daphne ASGI Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/paybitnex_backend
EnvironmentFile=/root/paybitnex_backend/.env
ExecStart=/root/paybitnex_backend/venv/bin/daphne -b 127.0.0.1 -p 8001 paybitnex.asgi:application
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

### Nginx WebSocket Proxy (`/etc/nginx/sites-available/paybitnex`)
```nginx
upstream daphne_backend {
    server 127.0.0.1:8001;
}

server {
    server_name api.paidix.com;

    # Standard REST API
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket Notifications
    location /ws/ {
        proxy_pass http://daphne_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_buffering off;
    }
}
```
