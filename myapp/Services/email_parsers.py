"""
email_parsers.py — Per-bank email parsers + auto-detection.

detect_parser(soup) inspects the HTML and returns (bank_name, parser_fn).
Each parser returns a dict:
    {
        "sender_name":    str,
        "amount":         str (raw, e.g. "+$169.00" or "1,500.00 USD"),
        "currency":       str (e.g. "USD"; optional, defaults "USD"),
        "transaction_id": str,
        "note":           str,
    }
or None if the email does not look like a payment receipt.

To add a new bank:
  1. Add a fingerprint entry to FINGERPRINTS
  2. Write a @register("yourbank") function below
"""
from __future__ import annotations
import re
from bs4 import BeautifulSoup


_PARSERS: dict = {}


def register(name: str):
    def decorator(fn):
        _PARSERS[name] = fn
        return fn
    return decorator


# ── Fingerprints ──────────────────────────────────────────────────────
# Checked in order — first match wins. `match: "any"` OR's the checks.
FINGERPRINTS = [
    {
        "bank": "cashapp",
        "checks": [
            {"type": "img_src_contains", "value": "squarecdn.com"},
            {"type": "text_contains",    "value": "cash.app"},
        ],
        "match": "any",
    },
    {
        "bank": "airwallex",
        "checks": [
            {"type": "img_src_contains", "value": "ctfassets.net/o9ep2zekvg13"},
            {"type": "domain_in_link",   "value": "airwallex.com"},
        ],
        "match": "any",
    },
    {
        "bank": "zelle_amex",
        "checks": [
            {"type": "img_src_contains", "value": "americanexpress.com"},
            {"type": "domain_in_link",   "value": "americanexpress.com"},
        ],
        "match": "any",
    },
    {
        "bank": "venmo",
        "checks": [
            {"type": "img_src_contains", "value": "venmo.com"},
            {"type": "domain_in_link",   "value": "venmo.com"},
        ],
        "match": "any",
    },
    {
        "bank": "paypal",
        "checks": [
            {"type": "img_src_contains", "value": "paypalobjects.com"},
            {"type": "domain_in_link",   "value": "paypal.com"},
            {"type": "text_contains",    "value": "paypal.com"},
        ],
        "match": "any",
    },
    {
        "bank": "zelle",
        "checks": [
            {"type": "img_src_contains", "value": "zellepay.com"},
            {"type": "text_contains",    "value": "zellepay.com"},
        ],
        "match": "any",
    },
    # ── Generic US banks ──────────────────────────────────────────────
    {
        "bank": "us_bank",
        "checks": [
            {"type": "text_contains", "value": "usbank.com"},
            {"type": "text_contains", "value": "u.s. bank"},
            {"type": "domain_in_link", "value": "usbank.com"},
        ],
        "match": "any",
    },
    {
        "bank": "chase",
        "checks": [
            {"type": "text_contains", "value": "chase.com"},
            {"type": "domain_in_link", "value": "chase.com"},
            {"type": "text_contains", "value": "jpmorgan chase"},
        ],
        "match": "any",
    },
    {
        "bank": "bofa",
        "checks": [
            {"type": "text_contains", "value": "bankofamerica.com"},
            {"type": "domain_in_link", "value": "bankofamerica.com"},
        ],
        "match": "any",
    },
]


def _run_check(soup: BeautifulSoup, check: dict) -> bool:
    t = check["type"]
    v = check["value"].lower()
    if t == "img_src_contains":
        return any(v in (img.get("src") or "").lower() for img in soup.find_all("img"))
    if t == "text_contains":
        return v in soup.get_text(" ", strip=True).lower()
    if t == "domain_in_link":
        return any(v in (a.get("href") or "").lower() for a in soup.find_all("a"))
    if t == "meta_contains":
        return any(v in (m.get("content") or "").lower() for m in soup.find_all("meta"))
    return False


def detect_parser(soup: BeautifulSoup):
    """Return (bank_name, parser_fn) or (None, None)."""
    for fp in FINGERPRINTS:
        results = [_run_check(soup, c) for c in fp["checks"]]
        matched = any(results) if fp.get("match", "any") == "any" else all(results)
        if matched:
            bank = fp["bank"]
            return bank, _PARSERS.get(bank)
    return None, None


# ── Cash App ──────────────────────────────────────────────────────────
@register("cashapp")
def parse_cashapp(soup: BeautifulSoup, msg) -> dict | None:
    result = {"sender_name": "", "amount": "", "currency": "USD",
              "transaction_id": "", "note": ""}
    name_td = soup.find("td", style=lambda s: s and "font-size:32px" in s)
    if name_td:
        result["sender_name"] = name_td.get_text(strip=True)
    amount_td = soup.find("td", style=lambda s: s and "font-size:48px" in s)
    if amount_td:
        result["amount"] = amount_td.get_text(strip=True).lstrip("+")
    grey_tds = soup.find_all(
        "td",
        style=lambda s: s and "color:#666666" in s and "font-size:16px" in s,
    )
    notes = [td.get_text(strip=True) for td in grey_tds if td.get_text(strip=True)]
    if len(notes) >= 2:
        result["note"] = notes[1]
    elif len(notes) == 1:
        result["note"] = notes[0]
    txn_re = re.compile(r"#[A-Z0-9]{1,2}-[A-Z0-9]+")
    for td in soup.find_all("td"):
        m = txn_re.search(td.get_text(strip=True))
        if m:
            result["transaction_id"] = m.group()
            break
    if not result["sender_name"] or not result["amount"]:
        return None
    return result


# ── Zelle via American Express ────────────────────────────────────────
@register("zelle_amex")
def parse_zelle_amex(soup: BeautifulSoup, msg) -> dict | None:
    result = {"sender_name": "", "amount": "", "currency": "USD",
              "transaction_id": "", "note": ""}
    label_map = {}
    for row in soup.find_all("td", style=lambda s: s and "border-bottom:1px solid #c8c9c7" in s):
        divs = row.find_all("div", style=lambda s: s and "display:inline-block" in s)
        if len(divs) >= 2:
            label = divs[0].get_text(strip=True).rstrip(":").strip().lower()
            value = divs[1].get_text(strip=True).lstrip("- ").strip()
            label_map[label] = value
    result["amount"] = label_map.get("amount", "")
    result["sender_name"] = label_map.get("from", "")
    result["transaction_id"] = label_map.get("transaction id", "")
    if not result["amount"] or not result["sender_name"]:
        return None
    return result


# ── Airwallex ─────────────────────────────────────────────────────────
@register("airwallex")
def parse_airwallex(soup: BeautifulSoup, msg) -> dict | None:
    result = {"sender_name": "", "amount": "", "currency": "USD",
              "transaction_id": "", "note": ""}
    label_map = {}
    summary_box = soup.find("div", style=lambda s: s and "border:1px solid #e8eaed" in s)
    if summary_box:
        grey_divs = summary_box.find_all(
            "div", style=lambda s: s and "color:#68707a" in s and "font-size:14px" in s,
        )
        for label_div in grey_divs:
            parent_td = label_div.find_parent("td")
            if not parent_td:
                continue
            parent_tr = parent_td.find_parent("tr")
            if not parent_tr:
                continue
            next_tr = parent_tr.find_next_sibling("tr")
            if next_tr:
                label_map[label_div.get_text(strip=True).lower()] = next_tr.get_text(strip=True)
    result["amount"] = label_map.get("deposit amount", "")
    result["sender_name"] = label_map.get("from", "")
    result["transaction_id"] = label_map.get("deposit id", "")
    result["note"] = label_map.get("reference", "")
    if not result["amount"]:
        headline = soup.find("div", style=lambda s: s and "font-size:24px" in s)
        if headline:
            m = re.search(r"([\d,]+\.?\d*)\s+([A-Z]{3})", headline.get_text())
            if m:
                result["amount"] = f"{m.group(1)} {m.group(2)}"
                result["currency"] = m.group(2)
    if not result["sender_name"] or not result["amount"]:
        return None
    return result


# ── Generic US bank (Chase / US Bank / BofA / Zelle fallback) ─────────
# Works for the common "Amount: $X.XX ... Reference: ABC123" pattern.
# This is best-effort — replace with per-bank parsers as you collect
# real receipt samples. The matcher does not need 100% accuracy on every
# field: amount + date is enough to auto-verify in most cases.
@register("us_bank")
@register("chase")
@register("bofa")
@register("zelle")
def parse_generic_bank(soup: BeautifulSoup, msg) -> dict | None:
    result = {"sender_name": "", "amount": "", "currency": "USD",
              "transaction_id": "", "note": ""}
    text = soup.get_text(" ", strip=True)

    # Amount
    amt = re.search(
        r"(?:amount|deposit(?:ed)?|you(?:'ve)?\s+(?:received|got))[^\d$]{0,30}"
        r"(?:\$\s*)?([\d,]+(?:\.\d{2})?)",
        text, re.IGNORECASE,
    )
    if amt:
        result["amount"] = amt.group(1)
    else:
        m = re.search(r"\$\s*([\d,]+\.\d{2})", text)
        if m:
            result["amount"] = m.group(1)

    # Reference / confirmation
    ref = re.search(
        r"(?:confirmation|reference|transaction|trace|ref)"
        r"(?:\s+(?:number|id|code))?[:\s#]+([A-Za-z0-9\-]{6,40})",
        text, re.IGNORECASE,
    )
    if ref:
        result["transaction_id"] = ref.group(1)

    # Sender name
    sender = re.search(
        r"(?:from|sender|sent by)[:\s]+([A-Z][A-Za-z\.\-\' ]{2,60})",
        text,
    )
    if sender:
        result["sender_name"] = sender.group(1).strip()

    if not result["amount"]:
        return None
    return result


# ── Stubs for future banks ────────────────────────────────────────────
# When you add fingerprints for these banks, replace the stub with a
# real parser (copy the style of parse_cashapp above).
@register("venmo")
def parse_venmo(soup, msg): return parse_generic_bank(soup, msg)

@register("paypal")
def parse_paypal(soup, msg): return parse_generic_bank(soup, msg)