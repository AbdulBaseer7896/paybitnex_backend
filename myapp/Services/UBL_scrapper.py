"""
UBL Digital Business Portal — Automated Statement Scraper.

Workflow:
  1. Login -> E-mail OTP channel -> Select.
  2. Reads NEW OTP from Gmail via IMAP.
  3. Enters OTP -> Submit -> Proceed.
  4. Opens period filter -> Select Range -> sets From/To dates.
  5. Done -> Export -> CSV -> saves to Bank_statments folder.
  6. Logout -> quits browser.
  7. Returns the Path to the downloaded CSV file.
"""

import os
import sys
import time
import re
import imaplib
import email
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Set, Dict, Any

# ─────────────────────────────────────────────
#  DEFAULT CREDENTIALS & PORTAL CONFIG
# ─────────────────────────────────────────────
USER_ID  = os.environ.get("UBL_USER_ID", "huzair@bitnex")
PASSWORD = os.environ.get("UBL_PASSWORD", "Huzair@0055017")
URL      = os.environ.get("UBL_URL", "https://corporate.ubldigital.com/")

# Gmail (IMAP) — 16-char Google App Password
IMAP_HOST  = os.environ.get("UBL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT  = int(os.environ.get("UBL_IMAP_PORT", "993"))
EMAIL_USER = os.environ.get("UBL_EMAIL_USER", "bitnextech@gmail.com")
EMAIL_PASS = os.environ.get("UBL_EMAIL_PASS", "zvdq vwwt tzue tdnb")
OTP_SENDER = os.environ.get("UBL_OTP_SENDER", "ubl_digital@ubl.com.pk")

# Timings (seconds)
WAIT_AFTER_SUBMIT_BEFORE_PROCEED = int(os.environ.get("UBL_WAIT_AFTER_SUBMIT", "30"))
WAIT_AFTER_PROCEED               = int(os.environ.get("UBL_WAIT_AFTER_PROCEED", "75")) 
OTP_CHECK_WAITS                  = [20, 20, 40]

OTP_PATTERNS = [
    r"one-time-password\)\s*is\s*(\d{4,8})",
    r"OTP[^\d]{0,40}?(\d{4,8})",
]

import socket
import threading
import base64

# Default download folder: Bank_statments in paybitnex_backend root
DEFAULT_DOWNLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "Bank_statments"

# Proxy configuration (Decodo Pakistan residential proxy)
# Note: asn-XXXXX and sessionduration pins are intentionally omitted.
# Pinning to a specific ASN or omitting country caused Decodo to route to Singapore / foreign IPs,
# which triggers UBL/bank security blocks.
# Adding -country-PK guarantees Decodo assigns a residential IP located inside Pakistan.
DEFAULT_PROXY = os.environ.get(
    "UBL_PROXY",
    "gate.decodo.com:10001:user-spduoaryo1-country-PK:5gsB8b3LSlx~4sysRp"
)
PROXY_STRING = DEFAULT_PROXY


class LocalProxyTunnel:
    """Lightweight in-process HTTP/HTTPS proxy tunnel forwarder that injects
    Proxy-Authorization headers for authenticated upstream proxies (like Decodo).

    Properly handles both:
    - HTTP requests: injects Proxy-Authorization header directly
    - HTTPS CONNECT tunnels: reads upstream 200 response before piping data
      so Chrome can complete the SSL handshake correctly without browser extensions.
    """
    def __init__(self, upstream_host: str, upstream_port: int, user: str, password: str):
        try:
            self.upstream_host = socket.gethostbyname(upstream_host)
        except Exception:
            self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        auth_bytes = f"{user}:{password}".encode("utf-8")
        self.auth_b64 = base64.b64encode(auth_bytes).decode("ascii")

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.server.listen(200)
        self.last_proxy_error = None

        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server.accept()
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except Exception:
                break

    @staticmethod
    def _recv_until_headers_end(sock: socket.socket, timeout: float = 15.0) -> bytes:
        """Reads from sock until we see the end of HTTP headers (\\r\\n\\r\\n)."""
        buf = b""
        sock.settimeout(timeout)
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket):
        """One-directional copy loop."""
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass

    def _handle_client(self, client: socket.socket):
        upstream = None
        try:
            raw = self._recv_until_headers_end(client)
            if not raw:
                return

            first_line_end = raw.find(b"\r\n")
            first_line = raw[:first_line_end].decode("latin-1", errors="replace")
            method = first_line.split(" ", 1)[0].upper()

            # Connect to upstream proxy (gate.decodo.com or IP)
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(30)
            upstream.connect((self.upstream_host, self.upstream_port))

            if method == "CONNECT":
                # HTTPS tunnel: forward CONNECT with Proxy-Authorization
                target = first_line.split(" ")[1]
                connect_req = (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                    f"Proxy-Authorization: Basic {self.auth_b64}\r\n"
                    f"Proxy-Connection: keep-alive\r\n"
                    f"\r\n"
                ).encode("latin-1")
                upstream.sendall(connect_req)

                upstream_resp = self._recv_until_headers_end(upstream, timeout=20)
                if not upstream_resp:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return

                status_line = upstream_resp.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
                status_code = status_line.split(" ")[1] if " " in status_line else "000"

                if not status_code.startswith("2"):
                    err_text = upstream_resp.decode("latin-1", errors="replace")
                    m = re.search(r"x-error-message:\s*(.+)", err_text, re.IGNORECASE)
                    if m:
                        target_str = str(first_line).lower()
                        if not any(bg in target_str for bg in ("googleapis.com", "google.com", "gstatic.com", "gvt1.com")):
                            proxy_msg = m.group(1).strip()
                            self.last_proxy_error = f"{status_code}: {proxy_msg}"
                            print(f"\n[PROXY ERROR] Upstream gateway rejected connection ({status_code}): {proxy_msg}\n")
                    client.sendall(upstream_resp)
                    return

                client.sendall(upstream_resp if upstream_resp.endswith(b"\r\n\r\n")
                               else upstream_resp + b"\r\n")

                t1 = threading.Thread(target=self._pipe, args=(client, upstream), daemon=True)
                t2 = threading.Thread(target=self._pipe, args=(upstream, client), daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

            else:
                # HTTP request: inject Proxy-Authorization
                header_end = raw.find(b"\r\n")
                injected = raw[:header_end + 2]
                injected += b"Proxy-Authorization: Basic " + self.auth_b64.encode("ascii") + b"\r\n"
                injected += raw[header_end + 2:]
                upstream.sendall(injected)

                t1 = threading.Thread(target=self._pipe, args=(client, upstream), daemon=True)
                t2 = threading.Thread(target=self._pipe, args=(upstream, client), daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()

        except Exception:
            pass
        finally:
            for s in (client, upstream):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    def stop(self):
        self.running = False
        try:
            self.server.close()
        except Exception:
            pass


class LocalSocks5Tunnel:
    """Lightweight in-process SOCKS5 proxy forwarder with RFC-1929 username/password auth.

    Listens on 127.0.0.1 without authentication so Chrome can connect with
    --proxy-server=socks5://127.0.0.1:<port> (no credentials needed locally).
    Authenticates with the upstream SOCKS5 server using user/password on each
    connection. Pipes bidirectional traffic after the SOCKS5 handshake.

    Required because Chrome headless on Linux cannot handle SOCKS5 auth prompts.
    """

    def __init__(self, upstream_host: str, upstream_port: int, user: str, password: str):
        try:
            self.upstream_host = socket.gethostbyname(upstream_host)
        except Exception:
            self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        self.user     = user.encode("utf-8")
        self.password = password.encode("utf-8")
        self.last_proxy_error = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.server.listen(200)
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server.accept()
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except Exception:
                break

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed unexpectedly")
            buf += chunk
        return buf

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass

    def _handle_client(self, client: socket.socket):
        upstream = None
        try:
            client.settimeout(60)

            # ── Step 1: greet Chrome — offer no-auth ──────────────────────────
            ver_nmeth = self._recv_exact(client, 2)
            if ver_nmeth[0] != 5:
                return
            nmethods = ver_nmeth[1]
            self._recv_exact(client, nmethods)    # consume method list
            client.sendall(b"\x05\x00")            # VER=5, METHOD=0 (no auth)

            # ── Step 2: read CONNECT request from Chrome ───────────────────────
            header = self._recv_exact(client, 4)
            if header[0] != 5 or header[1] != 1:   # must be VER=5, CMD=CONNECT
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            atyp = header[3]
            if atyp == 0x01:        # IPv4
                raw_addr = self._recv_exact(client, 4)
                dst_addr = ".".join(str(b) for b in raw_addr)
            elif atyp == 0x03:      # domain name
                dlen = self._recv_exact(client, 1)[0]
                dst_addr = self._recv_exact(client, dlen).decode("utf-8", errors="replace")
            elif atyp == 0x04:      # IPv6
                import ipaddress
                raw_addr = self._recv_exact(client, 16)
                dst_addr = str(ipaddress.IPv6Address(raw_addr))
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port_bytes = self._recv_exact(client, 2)
            dst_port   = (port_bytes[0] << 8) | port_bytes[1]  # noqa: F841

            # ── Step 3: connect to upstream SOCKS5 server ─────────────────────
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(30)
            upstream.connect((self.upstream_host, self.upstream_port))
            upstream.settimeout(60)

            # ── Step 4: RFC-1929 user/pass auth with upstream ─────────────────
            upstream.sendall(b"\x05\x01\x02")   # request user/pass auth
            srv_choice = self._recv_exact(upstream, 2)
            if srv_choice[1] == 0x02:
                auth_pkt = (
                    b"\x01"
                    + bytes([len(self.user)])     + self.user
                    + bytes([len(self.password)]) + self.password
                )
                upstream.sendall(auth_pkt)
                auth_resp = self._recv_exact(upstream, 2)
                if auth_resp[1] != 0x00:
                    self.last_proxy_error = "SOCKS5 upstream auth rejected (wrong credentials?)"
                    client.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
            elif srv_choice[1] == 0xFF:
                self.last_proxy_error = "SOCKS5 upstream rejected all auth methods"
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            # else 0x00 = no-auth accepted, continue

            # ── Step 5: forward CONNECT to upstream ───────────────────────────
            if atyp == 0x03:
                addr_enc = dst_addr.encode("utf-8")
                connect_req = b"\x05\x01\x00\x03" + bytes([len(addr_enc)]) + addr_enc + port_bytes
            elif atyp == 0x01:
                connect_req = b"\x05\x01\x00\x01" + bytes(int(p) for p in dst_addr.split(".")) + port_bytes
            else:
                addr_enc = dst_addr.encode("utf-8")
                connect_req = b"\x05\x01\x00\x03" + bytes([len(addr_enc)]) + addr_enc + port_bytes
            upstream.sendall(connect_req)

            # ── Step 6: read upstream connection reply ────────────────────────
            rep_hdr = self._recv_exact(upstream, 4)
            if rep_hdr[1] != 0x00:
                err_codes = {1: "General SOCKS failure", 2: "Connection not allowed",
                             3: "Network unreachable", 4: "Host unreachable",
                             5: "Connection refused", 6: "TTL expired"}
                self.last_proxy_error = err_codes.get(rep_hdr[1], f"SOCKS5 error {rep_hdr[1]:#x}")
                client.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            # consume bound address
            rep_atyp = rep_hdr[3]
            if rep_atyp == 0x01:
                self._recv_exact(upstream, 6)
            elif rep_atyp == 0x03:
                self._recv_exact(upstream, self._recv_exact(upstream, 1)[0] + 2)
            elif rep_atyp == 0x04:
                self._recv_exact(upstream, 18)

            # ── Step 7: tell Chrome we're connected ───────────────────────────
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

            # ── Step 8: pipe traffic bidirectionally ──────────────────────────
            # Remove timeouts for the pipe phase so long-lived AJAX requests
            # don't get killed if they take longer than 60s to return data.
            client.settimeout(None)
            upstream.settimeout(None)
            
            t1 = threading.Thread(target=self._pipe, args=(client, upstream), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(upstream, client), daemon=True)
            t1.start(); t2.start()
            t1.join();  t2.join()

        except Exception:
            pass
        finally:
            for s in (client, upstream):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    def stop(self):
        self.running = False
        try:
            self.server.close()
        except Exception:
            pass


def parse_proxy_string(proxy_str: str) -> Optional[Dict[str, Any]]:
    """Parses proxy string into host, port, user, pass, type dictionary.
    Supports formats:
      host:port:user:pass             (HTTP proxy, default)
      user:pass@host:port             (HTTP proxy, @-style)
      socks5://host:port:user:pass    (SOCKS5 with auth)
      socks5://user:pass@host:port    (SOCKS5 with auth, @-style)
      socks5://host:port              (SOCKS5 no auth)
    """
    if not proxy_str or not str(proxy_str).strip():
        return None

    raw = str(proxy_str).strip()

    # Detect proxy type from scheme prefix
    proxy_type = "http"
    for prefix in ("socks5h://", "socks5://", "socks4a://", "socks4://",
                   "https://", "http://"):
        if raw.lower().startswith(prefix):
            if "socks5" in prefix:
                proxy_type = "socks5"
            elif "socks4" in prefix:
                proxy_type = "socks4"
            raw = raw[len(prefix):]
            break

    # Parse user:pass@host:port
    if "@" in raw:
        auth_part, host_part = raw.split("@", 1)
        user, pwd = auth_part.split(":", 1) if ":" in auth_part else (auth_part, "")
        host, port = host_part.split(":", 1) if ":" in host_part else (host_part, "1080" if proxy_type == "socks5" else "80")
        try:
            return {"host": host, "port": int(port), "user": user, "pass": pwd, "type": proxy_type}
        except ValueError:
            return {"host": host, "port": 1080 if proxy_type == "socks5" else 80, "user": user, "pass": pwd, "type": proxy_type}

    # Parse host:port:user:pass  or  host:port
    parts = raw.split(":")
    if len(parts) == 4:
        try:
            return {"host": parts[0], "port": int(parts[1]), "user": parts[2], "pass": parts[3], "type": proxy_type}
        except ValueError:
            return None
    elif len(parts) == 2:
        try:
            return {"host": parts[0], "port": int(parts[1]), "user": None, "pass": None, "type": proxy_type}
        except ValueError:
            return None

    return None


def strip_proxy_session_pins(proxy_str: str) -> str:
    """Remove Decodo session-pinning parameters (sessionduration, session, asn) from proxy username.
    Preserves static IP pins (-ip-x.x.x.x) and country pins unchanged.
    Only appends -country-PK as a fallback when no IP pin AND no country pin is present.
    SOCKS5 proxies are returned unchanged (session-pin logic only applies to Decodo HTTP proxies).

    Note: -ip-<addr> and -country-<code> are mutually exclusive in Decodo ISP proxies.
    A static IP-pinned username must NOT have -country- appended or Decodo will reject it.
    """
    if not proxy_str:
        return proxy_str
    # SOCKS5 proxies have nothing to do with Decodo session pinning — leave them alone.
    raw = str(proxy_str).strip().lower()
    if raw.startswith(("socks5://", "socks5h://", "socks4://", "socks4a://")):
        return proxy_str
    info = parse_proxy_string(proxy_str)
    if not info or not info.get("user"):
        return proxy_str
    if info.get("type") == "socks5":
        return proxy_str

    clean_user = re.sub(
        r"-(sessionduration|session|asn)-[^-]+",
        "",
        info["user"],
        flags=re.IGNORECASE
    ).rstrip("-")

    # Only add -country-PK when there is no static IP pin and no existing country pin.
    # IP-pinned proxies (e.g. user-xxx-ip-1.2.3.4) are already locked to a specific
    # exit node — appending -country-PK would produce a conflicting/invalid username.
    has_ip_pin      = re.search(r"-ip-\d+\.\d+\.\d+\.\d+", clean_user, re.IGNORECASE)
    has_country_pin = "country-" in clean_user.lower()
    if not has_ip_pin and not has_country_pin:
        clean_user += "-country-PK"

    if "@" in str(proxy_str):
        return f"{clean_user}:{info['pass']}@{info['host']}:{info['port']}"
    return f"{info['host']}:{info['port']}:{clean_user}:{info['pass']}"


def normalize_date_for_ubl(d_str: str, default_val: str) -> str:
    """Normalizes any date string (DD.MM.YYYY, YYYY-MM-DD, DD-MM-YYYY) to DD/MM/YYYY."""
    if not d_str:
        return default_val
    d_str = str(d_str).strip().replace(".", "/").replace("-", "/")
    p = d_str.split("/")
    if len(p) == 3 and len(p[0]) == 4:
        return f"{p[2].zfill(2)}/{p[1].zfill(2)}/{p[0]}"
    elif len(p) == 3:
        return f"{p[0].zfill(2)}/{p[1].zfill(2)}/{p[2]}"
    return d_str


# ─────────────────────────────────────────────
#  Gmail / OTP Helpers
# ─────────────────────────────────────────────
def _body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                p = part.get_payload(decode=True)
                if p:
                    return p.decode(part.get_content_charset() or "utf-8", "ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                p = part.get_payload(decode=True)
                if p:
                    return re.sub(r"<[^>]+>", " ",
                                  p.decode(part.get_content_charset() or "utf-8", "ignore"))
    else:
        p = msg.get_payload(decode=True)
        if p:
            return p.decode(msg.get_content_charset() or "utf-8", "ignore")
    return ""


def _extract_otp(text):
    for pat in OTP_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _uid_search_ubl(imap):
    status, data = imap.uid("search", None, "FROM", OTP_SENDER)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def snapshot_otp_uids():
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(EMAIL_USER, EMAIL_PASS)
        imap.select("INBOX")
        uids = set(_uid_search_ubl(imap))
        imap.logout()
        return uids
    except Exception as e:
        print(f"[OTP] Could not snapshot inbox ({e}).")
        return set()


def fetch_new_otp(known_uids):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(EMAIL_USER, EMAIL_PASS)
        imap.select("INBOX")
        uids = _uid_search_ubl(imap)
        new_uids = [u for u in uids if u not in known_uids]
        for uid in reversed(new_uids):
            status, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            otp = _extract_otp(_body_text(msg))
            if otp:
                imap.logout()
                return otp
        imap.logout()
    except Exception as e:
        print(f"    Gmail check failed ({e}).")
    return None


# ─────────────────────────────────────────────
#  Selenium Helpers
# ─────────────────────────────────────────────
def click_el(driver, el):
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def type_into(driver, wait, element_id, text):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    el = wait.until(EC.presence_of_element_located((By.ID, element_id)))
    driver.execute_script("arguments[0].removeAttribute('readonly');", el)
    el.click()
    el.clear()
    el.send_keys(text)
    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('keyup',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
    """, el)
    return el


def fill_date(driver, wait, locator, value):
    """Set the date via JS (avoids triggering the calendar popup)."""
    from selenium.webdriver.support import expected_conditions as EC
    el = wait.until(EC.presence_of_element_located(locator))
    driver.execute_script("""
        arguments[0].removeAttribute('readonly');
        arguments[0].value = arguments[1];
        arguments[0].dispatchEvent(new Event('input',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
        if (arguments[0].blur) arguments[0].blur();
    """, el, value)


def check_login_error(driver):
    try:
        text = driver.execute_script("""
            var ids = ['loginErrorTxt', 'errorMessage', 'lblMsg', 'errorMsg', 'lblError', 'spnError'];
            for (var i = 0; i < ids.length; i++) {
                var e = document.getElementById(ids[i]);
                if (e && e.textContent.trim()) return e.textContent.trim();
            }
            var errs = document.querySelectorAll('.error-message, .alert-danger, [data-bind*="errorMessage"]');
            for (var i = 0; i < errs.length; i++) {
                if (errs[i].textContent.trim() && errs[i].offsetWidth > 0) return errs[i].textContent.trim();
            }
            return '';
        """)
        if text:
            return text
    except Exception:
        pass
    return None


def enter_otp(driver, wait, otp):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    field = wait.until(EC.element_to_be_clickable((By.ID, "otpInput")))
    field.click()
    field.clear()
    field.send_keys(otp)
    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
    """, field)


def read_otp_error(driver):
    try:
        return driver.execute_script("""
            var ps = document.querySelectorAll('p[data-bind*="errorMessage"]');
            for (var i=0;i<ps.length;i++){
                var p = ps[i];
                if (window.getComputedStyle(p).display !== 'none'){
                    var s = p.querySelector('span[data-bind*="errorMessage"]');
                    if (s && s.textContent.trim()) return s.textContent.trim();
                }
            }
            return '';
        """) or ""
    except Exception:
        return ""


def submit_otp(driver, wait):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "otpSubmitBtn"))))


def wait_for_new_download(folder: Path, before: Set[Path], timeout=60) -> Optional[Path]:
    end = time.time() + timeout
    while time.time() < end:
        current = {f for f in folder.iterdir() if not f.name.endswith(".crdownload")}
        new = [f for f in (current - before) if f.is_file()]
        if new:
            time.sleep(1)
            return max(new, key=lambda f: f.stat().st_mtime)
        time.sleep(1)
    return None


def get_fallback_ubl_proxies() -> List[Dict[str, str]]:
    """Retrieve up to 3 fallback proxies configured in SystemSetting or environment.
    Strictly filters out empty strings or unset values.
    """
    proxies = []
    try:
        from myapp.Models.Core_models import SystemSetting
        for i in range(1, 4):
            val = (SystemSetting.get(f"ubl_fallback_proxy_{i}", "") or "").strip()
            if val and val.lower() not in ("none", "false", "0", "off", "null", "undefined", '""', "''"):
                proxies.append({
                    "key": f"ubl_fallback_proxy_{i}",
                    "name": f"Admin Fallback Proxy #{i}",
                    "proxy": val,
                })
    except Exception:
        pass

    if not proxies:
        for i in range(1, 4):
            val = (os.environ.get(f"UBL_FALLBACK_PROXY_{i}", "") or "").strip()
            if val and val.lower() not in ("none", "false", "0", "off", "null", "undefined", '""', "''"):
                proxies.append({
                    "key": f"UBL_FALLBACK_PROXY_{i}",
                    "name": f"Env Fallback Proxy #{i}",
                    "proxy": val,
                })
    return proxies


# ─────────────────────────────────────────────
#  Core Scraper Single Attempt
# ─────────────────────────────────────────────
def _scrape_ubl_single_attempt(
    from_date: str = "01/09/2026",
    to_date: str = "11/09/2026",
    download_dir: Optional[Path] = None,
    headless: bool = True,
    log_callback=None,
    proxy: Optional[str] = None,
) -> Path:
    """Automates UBL Corporate Portal login, OTP validation via Gmail,
    date-range export of CSV statement, and returns the Path to the saved file.
    Routes traffic through proxy (default: Pakistan residential proxy) if configured.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def log(msg: str):
        if log_callback:
            try:
                log_callback(str(msg))
            except UnicodeEncodeError:
                safe = str(msg).encode("ascii", errors="replace").decode("ascii")
                log_callback(safe)
        else:
            try:
                print(str(msg))
            except UnicodeEncodeError:
                safe = str(msg).encode("ascii", errors="replace").decode("ascii")
                print(safe)

    target_from = normalize_date_for_ubl(from_date, "01/09/2026")
    target_to   = normalize_date_for_ubl(to_date, "11/09/2026")

    target_dir = Path(download_dir).resolve() if download_dir else DEFAULT_DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    log(f"[INIT] Download directory: {target_dir}")
    log(f"[INIT] Statement date range: {target_from} to {target_to}")

    # Locators
    PERIOD_DROPDOWN_BUTTON   = (By.ID, "movementsSelectCont-button")
    PERIOD_MENU_SELECT_RANGE = (By.XPATH, "//ul[@id='movementsSelectCont-menu']//a[normalize-space()='Select Range']")
    DATE_POPUP               = (By.ID, "casaModalDatePicker")
    FROM_DATE_LOCATOR        = (By.XPATH, "//div[@id='casaModalDatePicker']//*[normalize-space()='From']/following::input[1]")
    TO_DATE_LOCATOR          = (By.XPATH, "//div[@id='casaModalDatePicker']//*[normalize-space()='To']/following::input[1]")
    DONE_BTN_LOCATOR         = (By.XPATH, "//div[@id='casaModalDatePicker']//input[@value='Done'] | //div[@id='casaModalDatePicker']//button[normalize-space()='Done'] | //div[@id='casaModalDatePicker']//a[normalize-space()='Done']")
    PROCEED_LOCATOR          = (By.XPATH, "//input[@value='Proceed'] | //button[normalize-space()='Proceed'] | //a[normalize-space()='Proceed']")
    EXPORT_DROPDOWN          = (By.XPATH, "//*[contains(@class,'ui-selectmenu-status') and normalize-space()='Please Select']/ancestor::a[1]")
    CSV_OPTION               = (By.XPATH, "//ul[contains(@id,'menu')]//a[normalize-space()='CSV'] | //li//a[normalize-space()='CSV']")
    LOGOUT_DASHBOARD         = (By.XPATH, "//a[normalize-space()='Logout'] | //a[contains(.,'Logout')] | //*[@title='Logout']")
    LOGOUT_SECOND            = (By.ID, "logOut")

    log("[1] Setting up Chrome options...")
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-sync")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--no-pings")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-insecure-localhost")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    prefs = {
        "download.default_directory": str(target_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    options.add_experimental_option("prefs", prefs)

    # ── Proxy Setup via Local Tunnel (Lightweight in-process HTTP/HTTPS forwarder) ──
    proxy_tunnel = None
    if proxy is False or (isinstance(proxy, str) and proxy.strip().lower() in ("none", "false", "0", "off")):
        log("[1.1] No proxy configured — using direct connection.")
    else:
        active_proxy = proxy if proxy else PROXY_STRING
        active_proxy = strip_proxy_session_pins(active_proxy)
        proxy_info = parse_proxy_string(active_proxy)
        if proxy_info:
            is_socks5 = proxy_info.get("type") == "socks5"
            if proxy_info.get("user") and proxy_info.get("pass"):
                if is_socks5:
                    log(f"[1.1] Initializing local SOCKS5 tunnel to {proxy_info['host']}:{proxy_info['port']}...")
                    try:
                        proxy_tunnel = LocalSocks5Tunnel(
                            upstream_host=proxy_info["host"],
                            upstream_port=proxy_info["port"],
                            user=proxy_info["user"],
                            password=proxy_info["pass"]
                        )
                        log(f"[1.1] Local SOCKS5 tunnel active on 127.0.0.1:{proxy_tunnel.port}")
                        options.add_argument(f"--proxy-server=socks5://127.0.0.1:{proxy_tunnel.port}")
                    except Exception as pe:
                        log(f"[PROXY ERROR] Could not start local SOCKS5 tunnel: {pe}")
                else:
                    log(f"[1.1] Initializing local HTTP proxy tunnel to {proxy_info['host']}:{proxy_info['port']}...")
                    try:
                        proxy_tunnel = LocalProxyTunnel(
                            upstream_host=proxy_info["host"],
                            upstream_port=proxy_info["port"],
                            user=proxy_info["user"],
                            password=proxy_info["pass"]
                        )
                        log(f"[1.1] Local HTTP proxy tunnel active on 127.0.0.1:{proxy_tunnel.port}")
                        options.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_tunnel.port}")
                    except Exception as pe:
                        log(f"[PROXY ERROR] Could not start local HTTP proxy tunnel: {pe}")
            else:
                scheme = "socks5" if is_socks5 else "http"
                log(f"[1.1] Configuring direct {scheme.upper()} proxy: {proxy_info['host']}:{proxy_info['port']}...")
                options.add_argument(f"--proxy-server={scheme}://{proxy_info['host']}:{proxy_info['port']}")

    log(f"[2] Launching Chrome ({'headless' if headless else 'visible'})...")
    driver = None
    try:
        import undetected_chromedriver as uc_mod
        uc_options = uc_mod.ChromeOptions()
        uc_options.page_load_strategy = "eager"
        if headless:
            uc_options.add_argument("--headless=new")
        uc_options.add_argument("--no-sandbox")
        uc_options.add_argument("--disable-setuid-sandbox")
        uc_options.add_argument("--disable-dev-shm-usage")
        uc_options.add_argument("--disable-gpu")
        uc_options.add_argument("--no-zygote")
        uc_options.add_argument("--disable-software-rasterizer")
        uc_options.add_argument("--disable-background-networking")
        uc_options.add_argument("--ignore-certificate-errors")
        uc_options.add_argument("--allow-insecure-localhost")
        uc_options.add_argument("--window-size=1920,1080")
        uc_options.add_experimental_option("prefs", prefs)
        if proxy_tunnel:
            tunnel_scheme = "socks5" if isinstance(proxy_tunnel, LocalSocks5Tunnel) else "http"
            uc_options.add_argument(f"--proxy-server={tunnel_scheme}://127.0.0.1:{proxy_tunnel.port}")
        driver = uc_mod.Chrome(options=uc_options, use_subprocess=False)
        log("[2] Chrome launched via undetected-chromedriver [OK]")
    except ImportError:
        pass
    except Exception as uc_err:
        log(f"[2] undetected-chromedriver launch failed ({uc_err}) - falling back to standard selenium.")
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        driver = None

    if driver is None:
        try:
            driver = webdriver.Chrome(options=options)
            log("[2] Chrome launched via standard selenium [OK]")
        except Exception as e1:
            log(f"[SCRAPER] Primary selenium launch failed: {e1}. Retrying with minimal fallback options...")
            fallback_opts = Options()
            fallback_opts.page_load_strategy = "eager"
            if headless:
                fallback_opts.add_argument("--headless=new")
            fallback_opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
            fallback_opts.add_argument("--no-sandbox")
            fallback_opts.add_argument("--disable-setuid-sandbox")
            fallback_opts.add_argument("--disable-dev-shm-usage")
            fallback_opts.add_argument("--disable-gpu")
            fallback_opts.add_argument("--ignore-certificate-errors")
            fallback_opts.add_argument("--window-size=1920,1080")
            fallback_opts.add_argument("--disable-blink-features=AutomationControlled")
            fallback_opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            fallback_opts.add_experimental_option("useAutomationExtension", False)
            if proxy_tunnel:
                tunnel_scheme = "socks5" if isinstance(proxy_tunnel, LocalSocks5Tunnel) else "http"
                fallback_opts.add_argument(f"--proxy-server={tunnel_scheme}://127.0.0.1:{proxy_tunnel.port}")
            fallback_opts.add_experimental_option("prefs", prefs)
            driver = webdriver.Chrome(options=fallback_opts)
            log("[2] Chrome launched via fallback selenium [OK]")

    driver.set_page_load_timeout(60)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(target_dir)},
        )
    except Exception:
        pass
    wait = WebDriverWait(driver, 30)

    # Verify Outbound IP via lightweight request if proxy is used
    if proxy_tunnel:
        try:
            import urllib.request
            local_url = f"127.0.0.1:{proxy_tunnel.port}"
            if isinstance(proxy_tunnel, LocalSocks5Tunnel):
                # urllib doesn't speak SOCKS5 natively; use the socks module if available,
                # otherwise skip the IP check (Chrome will still route through the tunnel).
                try:
                    import socks as _socks
                    import socket as _sock_mod
                    s = _socks.socksocket()
                    # Use rdns=True so DNS resolution for ipify happens via proxy,
                    # preventing ERR_NAME_NOT_RESOLVED on prod.
                    s.set_proxy(_socks.SOCKS5, "127.0.0.1", proxy_tunnel.port, rdns=True)
                    s.settimeout(15)
                    s.connect(("api.ipify.org", 80))
                    s.sendall(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\n\r\n")
                    resp_data = b""
                    while True:
                        chunk = s.recv(1024)
                        if not chunk:
                            break
                        resp_data += chunk
                    s.close()
                    out_ip = resp_data.decode("utf-8", errors="ignore").split("\r\n\r\n", 1)[-1].strip()
                    log(f"[2.1] Verified Outbound IP (SOCKS5): {out_ip}")
                except ImportError:
                    log("[2.1] SOCKS5 IP check skipped (PySocks not installed — pip install PySocks).")
                except Exception as socks_err:
                    log(f"[2.1] SOCKS5 IP check notice: {socks_err}")
                    if getattr(proxy_tunnel, "last_proxy_error", None):
                        raise RuntimeError(f"SOCKS5 Proxy Error ({proxy_tunnel.last_proxy_error})")
            else:
                proxy_handler = urllib.request.ProxyHandler({
                    'http':  f'http://{local_url}',
                    'https': f'http://{local_url}',
                })
                opener = urllib.request.build_opener(proxy_handler)
                req = urllib.request.Request('https://api.ipify.org', headers={'User-Agent': 'curl/7.68.0'})
                with opener.open(req, timeout=8) as resp:
                    out_ip = resp.read().decode('utf-8').strip()
                    log(f"[2.1] Verified Outbound IP: {out_ip}")
        except Exception as ip_err:
            err_str = str(ip_err)
            log(f"[2.1] IP check notice: {err_str}")
            if getattr(proxy_tunnel, "last_proxy_error", None):
                raise RuntimeError(f"Proxy Authentication / Quota Exceeded ({proxy_tunnel.last_proxy_error})")
            if "407" in err_str or "Tunnel connection failed" in err_str:
                raise RuntimeError(f"Proxy Authentication / Quota Exceeded: {err_str}")

    try:
        # Resilient navigation loop to ensure UBL portal loads
        login_ready = False
        for nav_attempt in range(1, 4):
            log(f"[3.{nav_attempt}] Navigating to UBL login URL (attempt {nav_attempt}/3)...")
            try:
                driver.get(URL)
            except Exception as load_err:
                log(f"[WARN] Page load exception ({load_err}); stopping load...")
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass

            # Fast-fail if proxy is dead / rejected connection
            if proxy_tunnel and getattr(proxy_tunnel, "last_proxy_error", None):
                raise RuntimeError(f"Proxy Gateway Error ({proxy_tunnel.last_proxy_error})")

            time.sleep(3)

            page_title = driver.title or ""
            curr_url = driver.current_url or ""
            log(f"[3.{nav_attempt}] Page title: '{page_title}' | URL: '{curr_url}'")
            if any(term in page_title.lower() for term in ("access denied", "cloudflare", "403 forbidden", "attention required")):
                raise RuntimeError(
                    f"WAF / Portal Blocked: '{page_title}'. The UBL portal is restricting access from this IP/Proxy."
                )

            try:
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "userNameText")))
                login_ready = True
                break
            except Exception:
                # Fast fail if proxy error occurred
                if proxy_tunnel and getattr(proxy_tunnel, "last_proxy_error", None):
                    raise RuntimeError(f"Proxy Gateway Error ({proxy_tunnel.last_proxy_error})")
                log(f"[WARN] Login field not ready on attempt {nav_attempt}; retrying...")
                time.sleep(2)

        if not login_ready:
            # Final check before failing
            wait.until(EC.presence_of_element_located((By.ID, "userNameText")))

        log("[4] Entering Login ID...")
        type_into(driver, wait, "userNameText", USER_ID)
        time.sleep(1.5)

        log("[5] Entering password...")
        type_into(driver, wait, "passwordText", PASSWORD)
        time.sleep(1.5)

        log("[6] Clicking LOGIN...")
        login_btn = wait.until(EC.element_to_be_clickable((By.ID, "loginButton")))
        click_el(driver, login_btn)
        time.sleep(10)   # SOCKS5 proxy adds latency on the redirect

        log("[7] Checking for login errors...")
        err = check_login_error(driver)
        if err:
            raise RuntimeError(f"Login failed: {err}")

        log("[8] Waiting for the OTP channel dialog...")
        otp_wait = WebDriverWait(driver, 90)   # slow SOCKS5/proxy connections need more time
        email_radio = otp_wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[type='radio'][value='EMAIL']")))
        time.sleep(2)

        log("[9] Selecting E-mail channel...")
        click_el(driver, email_radio)
        driver.execute_script(
            "arguments[0].checked = true; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", email_radio)
        time.sleep(2)

        log("[10a] Snapshotting existing UBL emails...")
        known_otp_uids = snapshot_otp_uids()

        log("[10] Pressing Select button...")
        select_btn = wait.until(EC.presence_of_element_located((By.ID, "btn_select")))
        click_el(driver, select_btn)

        otp = None
        for i, w in enumerate(OTP_CHECK_WAITS, 1):
            log(f"[11.{i}] Waiting {w}s, then checking Gmail for OTP (attempt {i}/{len(OTP_CHECK_WAITS)})...")
            time.sleep(w)
            otp = fetch_new_otp(known_otp_uids)
            if otp:
                log(f"[11.{i}] New OTP found: {otp}")
                break
        if not otp:
            raise TimeoutError("No OTP received from UBL via email.")

        log("[13] Entering OTP...")
        enter_otp(driver, wait, otp)
        time.sleep(2)

        log("[14] Pressing Submit...")
        submit_otp(driver, wait)
        time.sleep(6)

        otp_err = read_otp_error(driver)
        if otp_err:
            log(f"[15] OTP error: {otp_err}. Checking for newer OTP...")
            otp2 = fetch_new_otp(known_otp_uids)
            if not otp2 or otp2 == otp:
                raise RuntimeError("OTP verification failed on first attempt and no newer OTP found.")
            enter_otp(driver, wait, otp2)
            time.sleep(2)
            submit_otp(driver, wait)
            time.sleep(6)
            if read_otp_error(driver):
                raise RuntimeError("Second OTP verification attempt also failed.")

        log("[15] OTP successfully accepted.")

        # Statement export flow: Exact proven delays for proxy / high-latency connections
        log(f"[17] Waiting {WAIT_AFTER_SUBMIT_BEFORE_PROCEED}s before Proceed...")
        time.sleep(WAIT_AFTER_SUBMIT_BEFORE_PROCEED)

        log("[18] Clicking Proceed...")
        click_el(driver, wait.until(EC.element_to_be_clickable(PROCEED_LOCATOR)))
        log("[18] Proceed clicked.")

        log(f"[19] Waiting {WAIT_AFTER_PROCEED}s for the accounts page...")
        time.sleep(WAIT_AFTER_PROCEED)

        # The accounts page loads account data and the period filter dropdown
        # via AJAX after Proceed. On slow proxy connections (India -> proxy -> UBL)
        # these AJAX calls can take 90-120s. We must NOT call window.stop() here —
        # doing so kills the pending AJAX requests and the dropdown never renders.
        # Just use a generous wait and let the browser finish naturally.
        slow_wait = WebDriverWait(driver, 120)

        # Wait for the period-filter dropdown to become interactive.
        # On high-latency proxy connections (India -> proxy -> UBL) the jQuery UI
        # selectmenu is created in the DOM early but its .click() handler isn't
        # bound until the details-panel AJAX completes. A single long wait isn't
        # reliable — instead we click, confirm the menu opened, and retry if not.
        log("[20] Opening the period filter dropdown (with retry loop)...")
        dropdown_opened = False
        MAX_DROPDOWN_ATTEMPTS = 10           # retry up to 10x (≈ 10 × 25s = ~250s max)
        DROPDOWN_RETRY_GAP    = 25           # seconds to wait between attempts

        for attempt in range(1, MAX_DROPDOWN_ATTEMPTS + 1):
            log(f"[20] Dropdown click attempt {attempt}/{MAX_DROPDOWN_ATTEMPTS}...")
            # Try Selenium click first (respects aria-disabled / CSS display)
            try:
                short_w = WebDriverWait(driver, 15)
                btn = short_w.until(EC.element_to_be_clickable(PERIOD_DROPDOWN_BUTTON))
                click_el(driver, btn)
            except Exception:
                # Fall back to raw JS click (fires the event even through overlays)
                try:
                    driver.execute_script(
                        "var b=document.getElementById('movementsSelectCont-button');"
                        "if(b) b.click();"
                    )
                except Exception:
                    pass

            # Verify the dropdown menu actually opened (quick check — 5s)
            try:
                WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable(PERIOD_MENU_SELECT_RANGE)
                )
                dropdown_opened = True
                log(f"[20] Dropdown opened successfully on attempt {attempt}.")
                break
            except Exception:
                if attempt < MAX_DROPDOWN_ATTEMPTS:
                    log(f"[20] Menu did not open — waiting {DROPDOWN_RETRY_GAP}s before retry...")
                    time.sleep(DROPDOWN_RETRY_GAP)

        if not dropdown_opened:
            raise RuntimeError(
                f"Period filter dropdown menu never opened after {MAX_DROPDOWN_ATTEMPTS} attempts "
                f"(~{MAX_DROPDOWN_ATTEMPTS * (15 + 5 + DROPDOWN_RETRY_GAP)}s total). "
                "The jQuery UI click handler may not be binding — the details-panel AJAX is "
                "likely still in flight. Try increasing WAIT_AFTER_PROCEED or switching to a "
                "lower-latency proxy (e.g. a Pakistani residential IP)."
            )
        time.sleep(1)

        log("[20] Selecting 'Select Range'...")
        click_el(driver, slow_wait.until(EC.element_to_be_clickable(PERIOD_MENU_SELECT_RANGE)))
        log("[20] 'Select Range' selected.")

        log("[20a] Waiting for the date popup...")
        slow_wait.until(EC.visibility_of_element_located(DATE_POPUP))
        log("[20a] Date popup open.")
        time.sleep(2)

        log(f"[21] Entering From date: {target_from}")
        fill_date(driver, slow_wait, FROM_DATE_LOCATOR, target_from)
        log(f"[22] Entering To date: {target_to}")
        fill_date(driver, slow_wait, TO_DATE_LOCATOR, target_to)
        time.sleep(1)

        log("[23] Clicking Done...")
        click_el(driver, slow_wait.until(EC.element_to_be_clickable(DONE_BTN_LOCATOR)))
        log("[23] Done clicked.")
        time.sleep(5)

        log("[24] Opening Export dropdown ('Please Select')...")
        click_el(driver, slow_wait.until(EC.element_to_be_clickable(EXPORT_DROPDOWN)))
        time.sleep(1)

        log("[25] Choosing CSV...")
        before_files = {f for f in target_dir.iterdir() if not f.name.endswith(".crdownload")}
        click_el(driver, wait.until(EC.element_to_be_clickable(CSV_OPTION)))
        log("[25] CSV clicked. Waiting for download...")

        downloaded = wait_for_new_download(target_dir, before_files, timeout=60)
        if not downloaded:
            raise FileNotFoundError("CSV statement was not downloaded within the 60s timeout.")

        stamp = datetime.now().strftime("%Y-%m-%d_%I-%M-%S-%p")
        new_path = target_dir / f"UBL_Statement_{stamp}{downloaded.suffix.lower()}"
        downloaded.rename(new_path)
        log(f"[26] Statement downloaded and saved: {new_path.name}")
        log(f"[SAVED_STATEMENT_CSV]: {new_path.resolve()}")

        # Logout
        try:
            driver.execute_script("window.onbeforeunload = null;")
            click_el(driver, wait.until(EC.element_to_be_clickable(LOGOUT_DASHBOARD)))
            time.sleep(3)
            click_el(driver, wait.until(EC.element_to_be_clickable(LOGOUT_SECOND)))
            time.sleep(2)
            log("[28] Logged out successfully.")
        except Exception:
            pass

        return new_path

    except Exception as exc:
        try:
            err_shot = target_dir / "ubl_error_snapshot.png"
            driver.save_screenshot(str(err_shot))
            log(f"[DEBUG] Saved failure screenshot to: {err_shot}")
        except Exception:
            pass
        if proxy_tunnel and getattr(proxy_tunnel, "last_proxy_error", None):
            raise RuntimeError(f"Proxy Gateway Error ({proxy_tunnel.last_proxy_error}) during scraping: {exc}") from exc
        raise exc

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        if proxy_tunnel:
            proxy_tunnel.stop()


# ─────────────────────────────────────────────
#  Main Entrypoint with Fallback Rotation
# ─────────────────────────────────────────────
def scrape_ubl_statement(
    from_date: str = "01/09/2026",
    to_date: str = "11/09/2026",
    download_dir: Optional[Path] = None,
    headless: bool = True,
    log_callback=None,
    proxy: Optional[str] = None,
) -> Path:
    """Automates UBL Corporate Portal login, OTP validation via Gmail,
    date-range export of CSV statement, and returns the Path to the saved file.
    
    If proxy fails or encounters quota limits (407), automatically rotates
    through up to 3 fallback proxies configured in Admin Settings (SystemSetting).
    """
    def log(msg: str):
        if log_callback:
            try:
                log_callback(str(msg))
            except UnicodeEncodeError:
                safe = str(msg).encode("ascii", errors="replace").decode("ascii")
                log_callback(safe)
        else:
            try:
                print(str(msg))
            except UnicodeEncodeError:
                safe = str(msg).encode("ascii", errors="replace").decode("ascii")
                print(safe)

    # Case 1: Direct connection (--no-proxy) or specific custom proxy string passed
    if proxy is False or (isinstance(proxy, str) and proxy.strip().lower() in ("none", "false", "0", "off")):
        return _scrape_ubl_single_attempt(
            from_date=from_date,
            to_date=to_date,
            download_dir=download_dir,
            headless=headless,
            log_callback=log_callback,
            proxy=False,
        )

    if proxy and proxy != DEFAULT_PROXY:
        return _scrape_ubl_single_attempt(
            from_date=from_date,
            to_date=to_date,
            download_dir=download_dir,
            headless=headless,
            log_callback=log_callback,
            proxy=proxy,
        )

    # Case 2: Attempt with DEFAULT_PROXY
    log("\n[PROXY STRATEGY] Attempting UBL statement scraping with DEFAULT_PROXY...")
    try:
        return _scrape_ubl_single_attempt(
            from_date=from_date,
            to_date=to_date,
            download_dir=download_dir,
            headless=headless,
            log_callback=log_callback,
            proxy=DEFAULT_PROXY,
        )
    except Exception as exc:
        log(f"\n[PROXY FALLBACK] Primary DEFAULT_PROXY attempt failed: {exc}")
        log("[PROXY FALLBACK] Checking for fallback proxies configured in Admin Settings...")

        fallback_proxies = get_fallback_ubl_proxies()
        if not fallback_proxies:
            log("[PROXY FALLBACK] No fallback proxies configured in Admin Settings (SystemSetting ubl_fallback_proxy_1..3 are currently blank).")
            log("[PROXY FALLBACK] Tip: Configure working fallback proxies in Paidix Admin Settings (Settings > Fallback Proxies) to enable automatic failover.")
            raise exc

        log(f"[PROXY FALLBACK] Found {len(fallback_proxies)} fallback proxy configuration(s). Starting fallback attempts...")
        attempt_errors = [f"DEFAULT_PROXY: {exc}"]

        for idx, item in enumerate(fallback_proxies, 1):
            cand_name = item["name"]
            cand_proxy = item["proxy"]
            log(f"\n{'='*70}")
            log(f"  [PROXY FALLBACK {idx}/{len(fallback_proxies)}] Trying {cand_name}...")
            log(f"{'='*70}")

            try:
                result_path = _scrape_ubl_single_attempt(
                    from_date=from_date,
                    to_date=to_date,
                    download_dir=download_dir,
                    headless=headless,
                    log_callback=log_callback,
                    proxy=cand_proxy,
                )
                log(f"\n{'='*70}")
                log(f"  [PROXY FALLBACK SUCCESS] UBL scraper succeeded using {cand_name}!")
                log(f"{'='*70}\n")
                return result_path
            except Exception as cand_err:
                log(f"[PROXY FALLBACK NOTICE] {cand_name} failed: {cand_err}")
                attempt_errors.append(f"{cand_name}: {cand_err}")

        final_err = f"All proxies failed ({len(fallback_proxies) + 1} attempts):\n" + "\n".join(f" - {e}" for e in attempt_errors)
        raise RuntimeError(final_err) from exc


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="UBL Statement Scraper")
    parser.add_argument("--from-date", type=str, default="01/09/2026", help="From date (DD/MM/YYYY)")
    parser.add_argument("--to-date", type=str, default="11/09/2026", help="To date (DD/MM/YYYY)")
    parser.add_argument("--download-dir", type=str, default=None, help="Folder to save statement CSV")
    parser.add_argument("--visible", action="store_true", default=False, help="Run browser visibly")
    parser.add_argument("--no-proxy", action="store_true", default=False, help="Run without any proxies (direct connection)")
    args = parser.parse_args()

    saved_file = scrape_ubl_statement(
        from_date=args.from_date,
        to_date=args.to_date,
        download_dir=Path(args.download_dir) if args.download_dir else None,
        headless=not args.visible,
        proxy="none" if args.no_proxy else None,
    )
    print(f"\nCompleted! Downloaded file: {saved_file}")