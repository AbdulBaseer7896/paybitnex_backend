"""
Helper script to update payment status and test real-time WebSocket notifications.

Usage:
    python scripts/trigger_payment_status.py
    python scripts/trigger_payment_status.py --status on_hold --note "Reviewing documents"
"""
import argparse
import os
import sys

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paybitnex.settings")

import django
django.setup()

import urllib.request
import json
from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Transaction_models import IncomingPayment
from rest_framework_simplejwt.tokens import AccessToken


def main():
    parser = argparse.ArgumentParser(description="Trigger payment status update via API")
    parser.add_argument("--payment-id", help="UUID of payment to update (defaults to first available)")
    parser.add_argument("--status", default="on_hold", help="New status (e.g. on_hold, manual_verification, rejected)")
    parser.add_argument("--note", default="Testing real-time notification", help="Audit / status note")
    parser.add_argument("--port", default=8000, type=int, help="Server port (default: 8000)")
    args = parser.parse_args()

    # Find admin or accountant user
    admin = User.objects.filter(role__in=[UserRole.ADMIN, UserRole.ACCOUNTANT]).first()
    if not admin:
        print("[!] No admin/accountant user found in database.")
        sys.exit(1)

    # Find payment
    if args.payment_id:
        payment = IncomingPayment.objects.filter(id=args.payment_id).first()
    else:
        payment = IncomingPayment.objects.select_related("customer").first()

    if not payment:
        print("[!] No payment found in database.")
        sys.exit(1)

    token = str(AccessToken.for_user(admin))
    url = f"http://127.0.0.1:{args.port}/api/v1/transactions/payments/{payment.id}/status/"

    payload = json.dumps({
        "status": args.status,
        "note": args.note
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST"
    )

    print(f"[*] Updating payment {payment.id} (customer: {payment.customer.email})")
    print(f"[*] Request: POST {url}")
    print(f"[*] Admin actor: {admin.email}")
    print(f"[*] Payload: status={args.status}, note={args.note}")

    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode("utf-8")
            print(f"[+] SUCCESS! Server response HTTP {resp.status}:")
            print(json.dumps(json.loads(data), indent=2))
            print(f"\n[✓] Check Terminal 2 (WebSocket client) — you should see payment_status_updated!")
    except urllib.error.HTTPError as e:
        err_content = e.read().decode("utf-8")
        print(f"[-] HTTP Error {e.code}: {e.reason}")
        print(err_content)
    except Exception as e:
        print(f"[-] Connection failed: {e}")
        print("    Make sure Daphne / runserver is running in Terminal 1.")


if __name__ == "__main__":
    main()
