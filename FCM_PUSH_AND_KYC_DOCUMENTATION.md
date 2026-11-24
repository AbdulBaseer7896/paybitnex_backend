# PaidiX: FCM Push Notifications (Killed-App) & KYC Real-Time Alerts Documentation

**Date:** November 24, 2026  
**Branch:** `feature/fcm-and-kyc-notifications`  
**Repository:** [AbdulBaseer7896/paybitnex_backend](https://github.com/AbdulBaseer7896/paybitnex_backend)  
**Pull Request:** [Create Pull Request](https://github.com/AbdulBaseer7896/paybitnex_backend/pull/new/feature/fcm-and-kyc-notifications)  
**Dependencies Added:** `firebase-admin==6.6.0`, `channels==4.3.2`, `redis>=6.1.0`  
**Migration:** `myapp/migrations/0073_alter_notification_type_devicetoken.py`  
**Automated Tests:** 16/16 Unit & Integration Tests Passing (100% OK)  

---

## 1. Problem Statement & Architecture Solution

### 1.1 The Killed-App / Zero-Launch Problem
When a mobile user swipes the PaidiX app away from "Recent Apps" or the OS terminates the background process:
1. The Android Linux kernel terminates the process PID.
2. All TCP sockets—including Django Channels WebSockets—are immediately closed by the OS kernel with TCP FIN/RST packets.
3. The Daphne ASGI server receives the socket disconnect and unbinds the user from the Redis channel group.
4. If an admin then updates a payment status or approves a KYC submission, a pure WebSocket broadcast **cannot reach the phone**.

### 1.2 The Solution: Dual-Channel Delivery (WebSocket + FCM)
We have implemented a dual-delivery architecture:
- **Foreground / Active App:** Instant, sub-millisecond real-time event via **Django Channels WebSockets** (`/ws/notifications/`) for live UI refresh and in-app banners.
- **Killed App / Lock Screen / Background:** High-priority OS-level push notification via **Firebase Cloud Messaging (FCM)** using `firebase-admin` (v6.6.0). Google Play Services (Android) and APNs (iOS) display native notification banners on the lock screen without requiring the app to be open.

```
                          Event Triggered (Payment / KYC)
                                        │
                                        ▼
                          Notification.objects.create(...)
                                        │
                         dbtx.on_commit(lambda: ...)
                                        │
                     ┌──────────────────┴──────────────────┐
                     │                                     │
                     ▼                                     ▼
        Django Channels (WebSocket)               Firebase Admin (FCM)
                     │                                     │
                     ▼                                     ▼
             Active Socket                         Google Play Services / APNs
     (In-App Real-Time UI Refresh)                 (Lock Screen OS Push Banner)
```

---

## 2. Mobile Device Token Management API

Mobile clients must register their FCM device token with the backend upon app launch or user login.

### 2.1 Register Device Token
- **Method & URL:** `POST /api/v1/notifications/devices/`
- **Headers:** `Authorization: Bearer <JWT_ACCESS_TOKEN>`
- **Request Body:**
  ```json
  {
    "token": "dK1_fcm_device_registration_token_from_firebase_sdk...",
    "device_type": "android"
  }
  ```
  *(Supported `device_type` values: `"android"`, `"ios"`, `"web"`)*

- **Response (HTTP 200 / 201):**
  ```json
  {
    "message": "Device token registered successfully.",
    "created": true,
    "device_type": "android"
  }
  ```

### 2.2 Unregister Device Token (Logout)
When a user logs out, the app should deactivate the device token to prevent receiving another user's push notifications on that phone:

- **Method & URL:** `POST /api/v1/notifications/devices/unregister/`
- **Headers:** `Authorization: Bearer <JWT_ACCESS_TOKEN>`
- **Request Body:**
  ```json
  {
    "token": "dK1_fcm_device_registration_token_from_firebase_sdk..."
  }
  ```
- **Response (HTTP 200):**
  ```json
  {
    "message": "Device token unregistered successfully.",
    "updated": 1
  }
  ```

---

## 3. Server-Side FCM Push Engine (`myapp/notifications/fcm.py`)

### 3.1 Multicast Dispatch & Payload Specifications
Whenever a notification is created, `send_fcm_push_to_user` or `send_fcm_push_to_users` executes:
- **Android High-Priority Configuration:**
  - `priority="high"`
  - `channel_id="paidix_alerts"`
  - `sound="default"`
  - `click_action="FLUTTER_NOTIFICATION_CLICK"`
- **iOS APNs Configuration:**
  - `headers={"apns-priority": "10"}`
  - `sound="default"`, `content_available=True`, `badge=1`
- **FCM Data Block:** Flat string-string dictionary containing all event metadata (`notification_id`, `type`, `payment_id`, `status`).

### 3.2 Automatic Stale Token Pruning
If Google Play Services returns `UNREGISTERED` or `registration-token-not-registered` (meaning the user uninstalled or reinstalled the app), the engine automatically updates `DeviceToken.is_active = False` in the database so dead tokens are never messaged again.

### 3.3 Server Configuration (`.env`)
In production, place the Firebase service account key on the server and point to it:
```ini
FIREBASE_CREDENTIALS_PATH=/root/paybitnex_backend/firebase-service-account.json
# OR pass raw JSON:
# FIREBASE_CREDENTIALS_JSON={"type": "service_account", "project_id": "paidix", ...}
```
*(If credentials are not yet set, the engine logs a debug warning and gracefully continues without crashing).*

---

## 4. KYC Real-Time & Push Notification Specifications

KYC status changes and customer submissions now trigger both WebSocket and FCM lock-screen notifications.

### 4.1 Event: KYC Approved (`kyc_status_updated`)
Dispatched to the Customer when an admin approves their verification documents:

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

### 4.2 Event: KYC Objections Raised (`kyc_status_updated`)
Dispatched to the Customer when compliance requires updated or clearer documents:

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
  "message": "Action required: Objections raised on your KYC documents. Please review and resubmit. Notes: Selfie is blurry.",
  "notes": "Selfie is blurry.",
  "created_at": "2026-11-24T10:40:00.000000+00:00"
}
```

### 4.3 Event: KYC Rejected (`kyc_status_updated`)
Dispatched to the Customer when verification is rejected:

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

### 4.4 Event: Customer Submitted KYC (`kyc_submitted`)
Broadcasted to all online Admins & Accountants:

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

## 5. Payment Events Summary (Dual WebSocket + FCM)

Whenever payment status updates (`under_review`, `verified`, `pkr_sent`, `completed`, `on_hold`, `rejected`), the backend pushes:
- **WebSocket frame:** To group `user_<customer_id>`
- **FCM Push banner:** To all active registered devices of that customer
- **Example Payload:**
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

---

## 6. Mobile Client Implementation Example (Flutter / React Native)

```typescript
// 1. Register Token on Login:
async function registerDeviceToken(fcmToken: string) {
  await api.post('/api/v1/notifications/devices/', {
    token: fcmToken,
    device_type: Platform.OS === 'ios' ? 'ios' : 'android',
  });
}

// 2. Background / Killed-App Handler (Runs when app is swiped away):
FirebaseMessaging.onBackgroundMessage(async (remoteMessage) => {
  console.log('Background lock-screen push received:', remoteMessage.notification);
  // OS automatically renders notification from the 'notification' block
});

// 3. Foreground Active Socket Handler:
socket.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'payment_status_updated') {
    showInAppBanner(data.title, data.message);
    updatePaymentList();
  } else if (data.type === 'kyc_status_updated') {
    showInAppBanner(data.title, data.message);
    updateKYCProfileState(data.status);
  }
};
```

---

## 7. Automated Testing Results

Suite: `myapp/notifications/tests.py`

```
Creating test database for alias 'default'...
Ran 16 tests in 1.861s

OK
Destroying test database for alias 'default'...
```

- `test_device_token_registration_and_unregistration`: PASSED
- `test_notify_kyc_status_updated_approved`: PASSED
- `test_notify_kyc_status_updated_objections`: PASSED
- `test_notify_kyc_submitted`: PASSED
- `test_auth_success_query_token`: PASSED
- `test_auth_success_bearer_header`: PASSED
- `test_auth_failure_no_token`: PASSED
- `test_auth_failure_invalid_token`: PASSED
- `test_ping_pong`: PASSED
- `test_customer_isolated_group`: PASSED
- `test_notify_payment_status_updated_customer`: PASSED
- `test_notify_payment_received_confirmed_admin_broadcast`: PASSED
- `test_list_own_notifications`: PASSED
- `test_unread_count`: PASSED
- `test_mark_read`: PASSED
- `test_read_all`: PASSED
