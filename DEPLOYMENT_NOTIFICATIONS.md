# Production Deployment Guide: Django Channels & WebSocket

This guide covers deploying the PaidiX real-time notification system using **Daphne (ASGI)**, **Redis**, and **Nginx**.

---

## 1. Architecture Overview

```
                      +------------------+
                      |   Mobile App     |
                      +--------+---------+
                               |
                   WebSocket / HTTPS (443)
                               |
                      +--------v---------+
                      |     Nginx        |
                      +--------+---------+
                               |
             +-----------------+-----------------+
             | (HTTP / REST)                     | (WebSocket /ws/*)
      +------v------+                     +------v------+
      |   Gunicorn  | (or Daphne)         |    Daphne   | (ASGI port 8001)
      +------+------+                     +------+------+
             |                                   |
             +-----------------+-----------------+
                               |
                      +--------v---------+
                      |   Redis 6379     | (Channels Layer)
                      +--------^---------+
                               |
                      +--------+---------+
                      |  Celery Worker   | (Pushes status events)
                      +------------------+
```

---

## 2. Daphne Systemd Service

Create `/etc/systemd/system/paybitnex-daphne.service`:

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

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paybitnex-daphne
```

---

## 3. Nginx Configuration for WebSocket Proxy

Add the WebSocket proxy block inside your existing server configuration (`/etc/nginx/sites-available/paybitnex`):

```nginx
upstream daphne_backend {
    server 127.0.0.1:8001;
}

server {
    server_name api.paidix.com;

    # Standard REST API endpoints (proxied to gunicorn or daphne)
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket notifications endpoint
    location /ws/ {
        proxy_pass http://daphne_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Keep alive settings for long-lived socket connections
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_connect_timeout 60s;
        proxy_buffering off;
    }
}
```

Verify and reload Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 4. Environment Variables (`.env`)

Ensure the following variables are configured in `.env`:

| Variable | Recommended Production Value | Description |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Channels Redis channel layer and Celery broker. |
| `USE_INMEMORY_CHANNEL_LAYER` | `0` or unset | **CRITICAL:** Must be `0` or omitted in production. If set to `1`, events from Celery or different processes will fail to reach WebSocket consumers! |

---

## 5. Why Redis Is Required in Production

1. **Multi-Process Architecture:**
   - Daphne handles WebSocket connections in one process.
   - Celery workers (handling auto-confirmations, stale tasks) run in separate processes.
   - Web workers (Gunicorn/Daphne handling REST status updates) run in their own worker processes.
2. **Channel Layer Bridge:**
   - `channels_redis` connects all processes via Redis Pub/Sub. When a Celery worker or REST view triggers `notify_payment_status_updated()`, Redis broadcasts the event to the Daphne worker holding the live WebSocket connection.
