# Mobile Real-Time Notifications & Push Guide (WebSocket + FCM)

This guide provides the complete mobile integration contract for **Django Channels WebSockets** (active in-app delivery) and **Firebase Cloud Messaging (FCM)** (killed-app / lock-screen delivery).

---

## 1. Process States: WebSocket vs. FCM Push

| Process State | Delivery Channel | Behavior |
|---|---|---|
| **App Open (Foreground)** | **WebSocket (`/ws/notifications/`)** | Instant sub-second event over persistent socket. Update UI live, refresh tables, show snackbar. |
| **App Closed (Killed / Swiped away)** | **Firebase Cloud Messaging (FCM)** | Google Play Services / APNs displays notification banner on device lock screen. |
| **App Re-launched** | **REST API (`/api/v1/notifications/`)** | Fetches any unread notifications and badge counters missed while offline. |

---

## 2. Registering for FCM Push Notifications

When your mobile app launches and obtains an FCM device token:

### 2.1 Register Device Token
- **POST** `/api/v1/notifications/devices/`
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Request Body:**
  ```json
  {
    "token": "<FCM_REGISTRATION_TOKEN>",
    "device_type": "android"
  }
  ```
  *(device_type: `"android"`, `"ios"`, or `"web"`)*

- **Response (200 / 201):**
  ```json
  {
    "message": "Device token registered successfully.",
    "created": true,
    "device_type": "android"
  }
  ```

### 2.2 Unregister on Logout
When the user logs out:
- **POST** `/api/v1/notifications/devices/unregister/`
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Request Body:**
  ```json
  {
    "token": "<FCM_REGISTRATION_TOKEN>"
  }
  ```

---

## 3. WebSocket Endpoint & Connection

- **URL:** `wss://api.paidix.com/ws/notifications/?token=<ACCESS_TOKEN>`
- **Heartbeat:** Send `{"type": "ping"}` every 25s, receive `{"type": "pong"}`.
- **Rejection Code `4401`:** If token is invalid or expired, socket closes with code `4401`. Refresh token and reconnect.

---

## 4. Defined Event Specifications

### 4.1 Payment Status Updated (`payment_status_updated`)
Delivered to Customer when payment changes state:

```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 101,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "verified",
  "status": "verified",
  "title": "Payment Verified",
  "message": "Status for payment PBX-2026-000101 updated from under_review to verified. (Sender bank document verified)",
  "created_at": "2026-11-24T10:05:00.000000+00:00"
}
```

**Status codes:** `submitted`, `under_review`, `verified`, `pkr_sent`, `completed`, `on_hold`, `rejected`.

---

### 4.2 Customer PKR Receipt Confirmed (`payment_received_confirmed`)
Delivered to Admins & Accountants:

```json
{
  "type": "payment_received_confirmed",
  "event": "payment_received_confirmed",
  "notification_id": 107,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "pkr_sent",
  "new_status": "completed",
  "status": "completed",
  "customer_id": "f6e0cf82-1ca8-4a20-9ebc-57825d17d293",
  "customer_name": "Inza Sandhu",
  "title": "Customer Confirmed Receipt",
  "message": "Inza Sandhu confirmed receipt for payment PBX-2026-000101. Note: Received in Meezan Bank",
  "created_at": "2026-11-24T10:30:00.000000+00:00"
}
```

---

### 4.3 KYC Verification Updated (`kyc_status_updated`)
Delivered to Customer when their KYC is reviewed:

#### Approved:
```json
{
  "type": "kyc_status_updated",
  "event": "kyc_status_updated",
  "notification_id": 108,
  "kyc_status": "approved",
  "status": "approved",
  "old_status": "pending",
  "profile_id": "89b3f021-3a05-4b08-9df5-60bca9a128e4",
  "title": "KYC Approved",
  "message": "Your PaidiX account has been verified! You can now send and receive payments.",
  "notes": "",
  "created_at": "2026-11-24T10:35:00.000000+00:00"
}
```

#### Objections Raised:
```json
{
  "type": "kyc_status_updated",
  "event": "kyc_status_updated",
  "notification_id": 109,
  "kyc_status": "objections",
  "status": "objections",
  "old_status": "pending",
  "profile_id": "89b3f021-3a05-4b08-9df5-60bca9a128e4",
  "title": "KYC Objections Raised",
  "message": "Action required: Objections raised on your KYC documents. Notes: Selfie is blurry.",
  "notes": "Selfie is blurry.",
  "created_at": "2026-11-24T10:40:00.000000+00:00"
}
```

#### Rejected:
```json
{
  "type": "kyc_status_updated",
  "event": "kyc_status_updated",
  "notification_id": 110,
  "kyc_status": "rejected",
  "status": "rejected",
  "old_status": "pending",
  "profile_id": "89b3f021-3a05-4b08-9df5-60bca9a128e4",
  "title": "KYC Verification Rejected",
  "message": "Your PaidiX KYC verification was rejected. Reason: Invalid document.",
  "notes": "Invalid document.",
  "created_at": "2026-11-24T10:45:00.000000+00:00"
}
```

---

### 4.4 KYC Submitted Alert (`kyc_submitted`)
Delivered to Admins & Accountants:

```json
{
  "type": "kyc_submitted",
  "event": "kyc_submitted",
  "notification_id": 111,
  "kyc_status": "pending",
  "status": "pending",
  "profile_id": "89b3f021-3a05-4b08-9df5-60bca9a128e4",
  "customer_id": "f6e0cf82-1ca8-4a20-9ebc-57825d17d293",
  "customer_name": "Inza Sandhu",
  "title": "KYC Submitted",
  "message": "Customer Inza Sandhu submitted KYC documents for review.",
  "created_at": "2026-11-24T10:50:00.000000+00:00"
}
```

---

## 5. REST Sync Endpoints

- `GET /api/v1/notifications/unread-count/` → `{"unread_count": 3}`
- `GET /api/v1/notifications/?unread=true` → List of unread notifications
- `PATCH /api/v1/notifications/{id}/read/` → Mark single notification as read
- `POST /api/v1/notifications/read-all/` → Mark all notifications as read
