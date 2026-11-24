# PaidiX: Real-Time Notifications & OS-Level Push Specification

**Branch:** `feature/realtime-notifications`  
**Repository:** [AbdulBaseer7896/paybitnex_backend](https://github.com/AbdulBaseer7896/paybitnex_backend)  
**Pull Request:** [Create Pull Request](https://github.com/AbdulBaseer7896/paybitnex_backend/pull/new/feature/realtime-notifications)  
**Core Technologies:** Django 6.1.3, Channels 4.3.2, Daphne 4.1.2, Firebase Cloud Messaging (`firebase-admin` 6.6.0), Redis 5.2.1, SimpleJWT 5.3.1  
**Verification:** 16/16 Automated Tests Passing (100% OK)  

---

## 1. Unified Notification Architecture (Foreground vs. Killed App)

Mobile applications operate in two distinct OS process states that require complementary notification mechanisms:

```
                                Mobile Client
                                      │
            ┌─────────────────────────┴─────────────────────────┐
            │                                                   │
  App Open / Foreground                                App Swiped Away / Killed
  (WebSocket Connection)                               (OS Push Daemon)
            │                                                   │
            ▼                                                   ▼
    Django Channels (Daphne)                           Google Play Services (FCM)
   /ws/notifications/?token=...                       & Apple Push Notification (APNs)
            │                                                   │
            ├─────────────────────────┬─────────────────────────┤
            │                         │                         │
            ▼                         ▼                         ▼
   WebSocket Consumer            Channel Layer              FCM Dispatch
 (In-app live snackbar)       (Redis Pub/Sub 6379)     (OS Lock-Screen Banner)
```

1. **Foreground / Active App (Django Channels WebSockets):**
   - When the user has the app open, notifications are delivered in sub-millisecond real time over the active WebSocket socket (`/ws/notifications/`).
   - Instant UI feedback, sound, live table reload, badge increment without server polling.
2. **Killed App / Background / Device Locked (Firebase Cloud Messaging):**
   - When the app is swiped away from Recents or the device is locked, the Linux kernel terminates the app process and closes the TCP socket.
   - The backend automatically issues an **OS-level FCM push payload** (`firebase-admin`) to Google Play Services (Android) and APNs (iOS).
   - The operating system displays the notification banner on the lock screen. When the user taps the banner, the app launches, reconnects to WebSockets, and synchronizes via REST API.

---

## 2. Firebase Cloud Messaging (FCM) Integration

### 2.1 Mobile Device Registration Endpoint
When the mobile app launches or logs in, it registers its FCM token with the backend:

- **Endpoint:** `POST /api/v1/notifications/devices/`
- **Headers:** `Authorization: Bearer <JWT_ACCESS_TOKEN>`
- **Request Payload:**
  ```json
  {
    "token": "dK1_fcm_device_token_from_google_play_services...",
    "device_type": "android"
  }
  ```
  *(Supported `device_type`: `"android"`, `"ios"`, `"web"`)*

- **Success Response (HTTP 200 / 201):**
  ```json
  {
    "message": "Device token registered successfully.",
    "created": true,
    "device_type": "android"
  }
  ```

### 2.2 Unregistering on Logout
When the user logs out, the app should unregister the token so that the device no longer receives push alerts for that account:

- **Endpoint:** `POST /api/v1/notifications/devices/unregister/`
- **Headers:** `Authorization: Bearer <JWT_ACCESS_TOKEN>`
- **Request Payload:**
  ```json
  {
    "token": "dK1_fcm_device_token_from_google_play_services..."
  }
  ```

### 2.3 Server-Side FCM Dispatch & Stale Token Pruning
Whenever `Notification.objects.create(...)` is executed:
- The backend dispatches to the active Channels WebSocket group (`transaction.on_commit`).
- The backend sends a high-priority FCM payload to all active tokens of the user.
- **Stale Token Pruning:** If Google returns `UNREGISTERED` (e.g. app uninstalled or token regenerated), the backend automatically flags `DeviceToken.is_active = False` to prevent dead payload dispatches.

### 2.4 Server Configuration (`.env`)
To activate FCM push in production, set either of the following environment variables:
```ini
# Path to service account JSON downloaded from Firebase Console:
FIREBASE_CREDENTIALS_PATH=/root/paybitnex_backend/firebase-service-account.json

# OR raw JSON content:
# FIREBASE_CREDENTIALS_JSON={"type": "service_account", "project_id": "paidix-prod", ...}
```
*(If unset, the server logs a debug message and gracefully continues without crashing).*

---

## 3. Defined WebSocket & Push Event Specifications

Every event pushed to WebSockets or FCM contains identical standard field keys for zero-mismatch parsing on mobile clients.

### 3.1 Field Schema Reference

| Field | Type | Description |
|---|---|---|
| `type` / `event` | `string` | Event identifier (e.g. `"payment_status_updated"`, `"kyc_status_updated"`). |
| `notification_id` | `integer` | Database notification ID (`Notification.id`). |
| `payment_id` | `string (UUID)` | UUID of payment (if payment-related, otherwise `null`). |
| `reference` | `string` | Payment reference (e.g. `"PBX-2026-000101"`). |
| `old_status` | `string` | Previous status code. |
| `new_status` / `status` | `string` | Current updated status code. |
| `kyc_status` | `string` | KYC status code (if KYC-related). |
| `notes` | `string` | Admin notes or objection details. |
| `title` | `string` | Notification title (lock-screen header). |
| `message` | `string` | Notification body text. |
| `created_at` | `string (ISO 8601)` | Timestamp in UTC format. |

---

### 3.2 Payment Events (`payment_status_updated`)
Delivered to the customer when their payment status transitions:

#### 1. Payment Under Review (`under_review`)
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
  "created_at": "2026-11-24T10:00:00.000000+00:00"
}
```

#### 2. Payment Verified (`verified`)
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
  "created_at": "2026-11-24T10:05:00.000000+00:00"
}
```

#### 3. PKR Sent (`pkr_sent`)
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
  "created_at": "2026-11-24T10:10:00.000000+00:00"
}
```

#### 4. Payment Completed (`completed`)
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
  "created_at": "2026-11-24T10:15:00.000000+00:00"
}
```

#### 5. Payment On Hold (`on_hold`)
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 105,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "on_hold",
  "status": "on_hold",
  "title": "Payment On Hold",
  "message": "Status for payment PBX-2026-000101 updated from under_review to on_hold. (Awaiting sender verification)",
  "created_at": "2026-11-24T10:20:00.000000+00:00"
}
```

#### 6. Payment Rejected (`rejected`)
```json
{
  "type": "payment_status_updated",
  "event": "payment_status_updated",
  "notification_id": 106,
  "payment_id": "4df0d5a6-13a2-4df3-877d-1c5e517d48a5",
  "reference": "PBX-2026-000101",
  "old_status": "under_review",
  "new_status": "rejected",
  "status": "rejected",
  "title": "Payment Rejected",
  "message": "Status for payment PBX-2026-000101 updated from under_review to rejected. (Invalid transfer slip)",
  "created_at": "2026-11-24T10:25:00.000000+00:00"
}
```

---

### 3.3 Customer Receipt Confirmation Event (`payment_received_confirmed`)
Broadcasted to all online Admins & Accountants:

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

### 3.4 KYC Approval & Review Events (`kyc_status_updated`)
Delivered to the Customer when compliance reviews their profile:

#### 1. KYC Approved (`approved`)
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

#### 2. KYC Objections Raised (`objections`)
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
  "message": "Action required: Objections raised on your KYC documents. Please review and resubmit. Notes: Selfie is blurry, please provide a clear selfie.",
  "notes": "Selfie is blurry, please provide a clear selfie.",
  "created_at": "2026-11-24T10:40:00.000000+00:00"
}
```

#### 3. KYC Rejected (`rejected`)
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
  "message": "Your PaidiX KYC verification was rejected. Reason: Provided identity document expired.",
  "notes": "Provided identity document expired.",
  "created_at": "2026-11-24T10:45:00.000000+00:00"
}
```

---

### 3.5 KYC Submission Alert for Staff (`kyc_submitted`)
Broadcasted to all online Admins & Accountants when a customer submits / resubmits KYC:

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

## 4. Mobile Client Push Handling (Flutter / React Native / Native)

```typescript
// Background / Killed App Notification Handler (FCM):
FirebaseMessaging.onBackgroundMessage(async (remoteMessage) => {
  console.log("FCM Lock-Screen Payload Received:", remoteMessage.data);
  // Android & iOS display the notification automatically from the 'notification' block.
});

// Foreground Active Socket Handler (WebSocket):
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === "payment_status_updated") {
    // 1. In-app UI update
    showInAppAlert(data.title, data.message);
    // 2. Refresh local payment state
    refreshPayment(data.payment_id);
  } else if (data.type === "kyc_status_updated") {
    // Update KYC verification badge
    updateKYCStatus(data.status);
    showInAppAlert(data.title, data.message);
  }
};
```

---

## 5. Offline Sync REST APIs

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/notifications/devices/` | `POST` | Register FCM token on app launch / login |
| `/api/v1/notifications/devices/unregister/` | `POST` | Deactivate FCM token on logout |
| `/api/v1/notifications/unread-count/` | `GET` | Get badge count: `{"unread_count": 2}` |
| `/api/v1/notifications/` | `GET` | List notifications (supports `?unread=true`) |
| `/api/v1/notifications/{id}/read/` | `PATCH` | Mark single notification as read |
| `/api/v1/notifications/read-all/` | `POST` | Mark all notifications as read |
