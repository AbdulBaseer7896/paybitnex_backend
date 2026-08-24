# Celery worker + beat under systemd

> **You already have these services.** `User_guide.txt` shows
> `paybitnex`, `paybitnex-celery` and `paybitnex-beat` running on the
> server. These files match those names and the `/root/paybitnex_backend`
> layout, so they are a **reference and repair kit**, not something to
> install alongside what is already there. Installing a second beat under a
> different name would double every scheduled task.
>
> Check what you have before touching anything:
>
> ```bash
> systemctl status paybitnex-celery paybitnex-beat
> systemctl is-enabled paybitnex-beat     # must say "enabled" to survive reboot
> ```

## The two processes

| Service | What it does | How many |
|---|---|---|
| `paybitnex-celery` | Executes tasks off the Redis queue | 1 or more |
| `paybitnex-beat`   | Publishes tasks on a schedule | **exactly one, fleet-wide** |

### Why beat is a separate service and not `celery worker -B`

`celery worker -B` embeds the scheduler inside a worker, which saves a unit
file. Celery's own docs mark it development-only, and for good reason:

- **Scaling breaks it.** Two workers with `-B` means two schedulers. Every
  entry fires twice per tick — duplicate confirmation-reminder emails to
  customers.
- **Restarts are coupled.** Restarting the worker to change concurrency, or
  a worker OOM, silently takes the scheduler down with it.
- **You lose independent control** — no restarting beat alone after a
  schedule change, no separate `journalctl -u paybitnex-beat`.

Your server already separates them, so this is just confirming the setup you
have is the right one. `-B` would be a reasonable simplification only if you
were certain you'd never run more than one worker — not worth the downgrade.

### Why not `django-celery-beat`

That package stores the schedule in the database with an admin UI to edit
it. It pins Django below 6 and downgrades this project's Django 6.0.7 to
5.1.15, so it is not an option here. The schedule lives in
`CELERY_BEAT_SCHEDULE` in `paybitnex/settings.py` instead — version
controlled, deploys with the code, and no DB copy that can drift out of sync
with what the code expects.

## What the payment queue depends on

Two entries drive Awaiting Customer Confirmation, both every 30 minutes:

- `flag_stale_payments` — moves PKR-sent payments into the queue once they
  pass `stale_payment_minutes`, stamps `stale_at`, emails the customer once.
- `auto_confirm_stale_payments` — approves anything still in the queue
  `auto_confirm_payment_minutes` (default 1440 = one day) after `stale_at`.

The queue *display* is computed on the fly, so the page looks correct even
with beat down. Auto-approval is not — with beat down, payments accumulate
and nothing closes them. See "Safety net" below.

## Install / repair

Only if a unit is missing or broken:

```bash
sudo cp deploy/systemd/paybitnex-celery.service /etc/systemd/system/
sudo cp deploy/systemd/paybitnex-beat.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paybitnex-celery paybitnex-beat
```

`EnvironmentFile` points at the same `.env` `manage.py` reads. systemd's
parser is **not** a shell — no `export`, no `${VAR}` interpolation, no
trailing comments after a value. If the unit fails with a parse error, that
is almost always why.

Note there is no `ProtectHome=` in either unit. The deploy lives under
`/root`, and `ProtectHome=true` would make it unreadable and the service
would fail to start.

## Verify

```bash
systemctl status paybitnex-celery paybitnex-beat
sudo journalctl -u paybitnex-beat -f        # logs each entry as it fires
sudo journalctl -u paybitnex-celery -f      # logs the tasks running
```

Within 30 minutes the worker log should show:

```
flag_stale_payments: flagged 2 PKR-sent payments (threshold=4320 min)
auto_confirm_stale_payments: confirmed 1 of 1 due (window=1440 min)
```

Force a run without waiting for the next tick:

```bash
cd /root/paybitnex_backend && ./venv/bin/python manage.py run_payment_sweeps --dry-run
```

## Safety net (recommended)

A dead beat is silent — no error surfaces, payments just stop closing. An
hourly cron backstop turns a beat outage into a coarser schedule instead of
a stall:

```cron
0 * * * * cd /root/paybitnex_backend && ./venv/bin/python manage.py run_payment_sweeps >> /var/log/paybitnex-sweeps.log 2>&1
```

Running this alongside beat is harmless. Both sweeps select by cutoff and
re-check status under a row lock before writing, so a concurrent beat tick
and cron run cannot double-approve a payment or distribute fees twice.

## After changing the schedule

```bash
sudo systemctl restart paybitnex-beat
```

If beat seems stuck on the old cadence, delete its bookkeeping file and
restart — it is regenerated, and nothing but "when each entry last fired"
is lost:

```bash
sudo rm -f /var/lib/paybitnex/celerybeat-schedule
sudo systemctl restart paybitnex-beat
```

## Local development on Windows

Celery's prefork pool does not work on Windows. Use the solo pool, in two
terminals:

```powershell
.\venv\Scripts\celery.exe -A paybitnex worker --loglevel=info --pool=solo
.\venv\Scripts\celery.exe -A paybitnex beat --loglevel=info
```

Or skip Celery entirely and drive the sweeps by hand — needs no Redis and no
worker:

```powershell
.\venv\Scripts\python.exe manage.py run_payment_sweeps
```
