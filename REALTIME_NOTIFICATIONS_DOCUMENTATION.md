# PaidiX: Real-Time Notifications System — Architecture, API & Deployment Documentation

**Author / Contributor:** Backend Team  
**Branch:** `feature/realtime-notifications`  
**Repository:** [AbdulBaseer7896/paybitnex_backend](https://github.com/AbdulBaseer7896/paybitnex_backend)  
**Pull Request:** [Create PR](https://github.com/AbdulBaseer7896/paybitnex_backend/pull/new/feature/realtime-notifications)  
**Status:** Complete, Tested (12/12 Unit Tests Passed), Verified Live on ASGI/Daphne  

---

## 1. Executive Summary

This feature implements an enterprise-grade, real-time notification engine for the PaidiX platform using **Django Channels (ASGI)**, **WebSockets**, and a supporting **REST API**. 

### Core Capabilities:
1. **Customer Real-Time Alerts:** When an Admin, Accountant, or system task updates payment status (e.g., from `under_review` to `verified`, `pkr_sent`, `on_hold`, `completed`, or `rejected`), the customer receives an instant WebSocket push on mobile.
2. **Admin Real-Time Alerts:** When a customer marks "I received my PKR" (`customer_confirm`), all online admins and accountants receive an instant WebSocket alert.
3. **Dual Persistence & Sync:** Every event is persisted in the database *before* dispatching over WebSockets via `transaction.on_commit()`. If a user is offline or the mobile app is closed, they can fetch missed notifications via the REST API or sync badge counters.
4. **Resilient Local & Production Dual-Mode:** Works out-of-the-box locally with `USE_INMEMORY_CHANNEL_LAYER="1"` and automatically bridges multi-process workers (Celery, Daphne, Gunicorn) via Redis in production.

---

## 2. System Architecture

```
                               Mobile Client / Web Client
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

## 3. WebSocket Specifications

- **WebSocket URL (Production):** `wss://api.paidix.com/ws/notifications/`
- **WebSocket URL (Development):** `ws://localhost:8000/ws/notifications/`

### 3.1 Authentication
Uses the exact same SimpleJWT access tokens as the REST API:
1. **Query parameter (Standard for mobile/Postman):**
   ```text
   ws://localhost:8000/ws/notifications/?token=<ACCESS_TOKEN>
   ```
2. **Handshake Header:**
   ```http
   Authorization: Bearer <ACCESS_TOKEN>
   ```
- **Rejection Code `4401`:** If the token is missing, expired, or invalid, the socket immediately closes with code `4401`. The client must refresh its token and reconnect.
- **Zero Token Leakage:** Tokens are never logged to console or logging backends.

### 3.2 Channel Groups & Permissions
- **Customer:** Automatically placed into their isolated personal group: `user_<customer_id>`. Customers **never** receive notifications belonging to other users or internal staff.
- **Admin / Accountant:** Placed into their personal group `user_<admin_id>` **AND** the broadcast group `admins`.

### 3.3 Heartbeat (Ping / Pong)
To maintain TCP keep-alive and detect mobile network transitions:
- **Client sends:** `{"type": "ping"}`
- **Server replies:** `{"type": "pong"}`

---

## 4. Real-Time Event Contracts (Payloads)

All event payloads are flat, stable, and backwards-compatible JSON objects:

### Event A: `payment_status_updated` (Dispatched to Customer)
```json
{
  "type": "payment_status_updated",
  "notification_id": 13,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "old_status": "on_hold",
  "new_status": "under_review",
  "title": "Payment Under Review",
  "message": "Status for payment PBX-TEST-001 updated from on_hold to under_review. (Testing real-time notification)",
  "created_at": "2026-11-23T17:45:19.629308+00:00"
}
```

### Event B: `payment_received_confirmed` (Dispatched to Admins)
```json
{
  "type": "payment_received_confirmed",
  "notification_id": 14,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "customer_id": "f6e0cf82-1ca8-4a20-9ebc-57825d17d293",
  "customer_name": "Test Customer",
  "title": "Customer Confirmed Receipt",
  "message": "Test Customer confirmed PKR receipt for payment PBX-TEST-001.",
  "created_at": "2026-11-23T17:46:00.123456+00:00"
}
```

---

## 5. Database Schema & Migration

### Model: `Notification` (`myapp/notifications/models.py`)
- `recipient`: ForeignKey (`AUTH_USER_MODEL`, on_delete=CASCADE, related_name="notifications")
- `type`: CharField (`payment_status_updated` | `payment_received_confirmed` | `generic`)
- `payment`: ForeignKey (`IncomingPayment`, null=True, blank=True)
- `title`: CharField(255)
- `message`: TextField()
- `payload`: JSONField(default=dict)
- `is_read`: BooleanField(default=False)
- `read_at`: DateTimeField(null=True, blank=True)
- `created_at`: DateTimeField(auto_now_add=True)

### Composite Index
```python
indexes = [
    models.Index(fields=["recipient", "is_read", "-created_at"]),
]
```
Optimized for the high-frequency query: *"Fetch user's unread notifications ordered by newest first"*.

**Migration:** `myapp/migrations/0072_notification.py` (applied and included in branch).

---

## 6. Integration Trigger Points in Codebase

Dispatches are executed in `myapp/notifications/services.py` wrapped inside `dbtx.on_commit(...)` to prevent events on failed/rolled back database transactions:

1. **`myapp/Views/Transaction_views.py` (`_record_status_change`)**:
   - Triggers `notify_payment_status_updated` whenever status changes.
   - Skips self-notification if the actor is the customer.
2. **`myapp/Views/Transaction_views.py` (`customer_confirm`)**:
   - Triggers `notify_payment_received_confirmed` to notify online admins and accountants.
3. **`myapp/Utils/stale_payment_tasks.py` (`auto_confirm_stale_payments`)**:
   - Dispatches auto-completion notifications to customer.

---

## 7. REST API Endpoints

All endpoints require `Authorization: Bearer <JWT_ACCESS_TOKEN>`:

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/notifications/` | Paginated list of notifications for the authenticated user (supports `?unread=true`). |
| `GET` | `/api/v1/notifications/unread-count/` | Returns unread badge count: `{"unread_count": 3}`. |
| `POST` | `/api/v1/notifications/<id>/read/` | Marks a single notification as read. |
| `POST` | `/api/v1/notifications/read-all/` | Marks all notifications of the user as read: `{"marked_read": 5}`. |

---

## 8. Automated Testing & Verification

Unit & Integration test suite located in `myapp/notifications/tests.py`:
- `NotificationConsumerTestCase`: Tests successful JWT query param auth, bearer header auth, invalid token code `4401`, ping/pong, and isolated group dispatch.
- `NotificationServiceTestCase`: Tests customer status notification creation, skip-on-same-status, admin broadcast creation, and safe exception isolation.
- `NotificationAPITestCase`: Tests listing own notifications, tenant isolation (user B cannot see user A's notifications), unread counts, single mark-as-read, and mark-all-as-read.

**Result:**
```
Ran 12 tests in 1.384s
OK (100% Passing)
```

---

## 9. Production Deployment Setup

### 9.1 Daphne Systemd Unit (`/etc/systemd/system/paybitnex-daphne.service`)
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

### 9.2 Nginx WebSocket Upstream Block (`/etc/nginx/sites-available/paybitnex`)
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

### 9.3 Environment Configuration (`.env`)
```ini
# Production Redis layer connecting Celery & Daphne
REDIS_URL=redis://127.0.0.1:6379/0
USE_INMEMORY_CHANNEL_LAYER=0
```
