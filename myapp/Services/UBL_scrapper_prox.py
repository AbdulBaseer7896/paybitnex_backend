"""
UBL Digital Business Portal — full automated flow:
login -> E-mail OTP channel -> Select -> read NEW OTP from Gmail -> enter OTP
-> Submit -> Proceed -> open period filter -> Select Range -> set From/To dates
-> Done -> Export -> CSV -> save the CSV into Bank_statments with a date-time name
-> logout.

Browser is visible. Every step prints its status.
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pathlib import Path
from datetime import datetime
import imaplib
import email
import time
import re
import base64
import socket
import threading
import socketserver

# ─────────────────────────────────────────────
#  CHANGE THESE
# ─────────────────────────────────────────────
# Portal
USER_ID  = "huzair@bitnex"
PASSWORD = "Huzair@0055017"
URL      = "https://corporate.ubldigital.com/"     # <-- set the real login page URL

# Statement date range (format exactly as the portal shows it, e.g. dd/mm/yyyy)
FROM_DATE = "01/09/2026"
TO_DATE   = "09/09/2026"

# Gmail (IMAP) — use a 16-char Google App Password, NOT your normal password
IMAP_HOST  = "imap.gmail.com"
IMAP_PORT  = 993
EMAIL_USER = "bitnextech@gmail.com"
EMAIL_PASS = "zvdq vwwt tzue tdnb"   # app password, NOT your normal password
OTP_SENDER = "ubl_digital@ubl.com.pk"

# Proxy — format: host:port:username:password   (set to "" to run without a proxy)
PROXY = "isp.decodo.com:10001:user-sphczwl7x3-ip-9.249.116.13:Y~qhspw6Yo6D0bgrQ0"
VERIFY_PROXY_IP = True   # open an IP-check page first and print the IP Chrome is using

# Timings (seconds)
WAIT_AFTER_SUBMIT_BEFORE_PROCEED = 30
WAIT_AFTER_PROCEED               = 45
OTP_CHECK_WAITS                  = [20, 20, 40]   # check after 20s, 20s, 40s

# ── Locators ─────────────────────────────────
# Confirmed:
PERIOD_DROPDOWN_BUTTON   = (By.ID, "movementsSelectCont-button")
PERIOD_MENU_SELECT_RANGE = (By.XPATH, "//ul[@id='movementsSelectCont-menu']//a[normalize-space()='Select Range']")
DATE_POPUP               = (By.ID, "casaModalDatePicker")

# Best-guess (send exact SelectorsHub lines if any of these fail):
FROM_DATE_LOCATOR = (By.XPATH, "//div[@id='casaModalDatePicker']//*[normalize-space()='From']/following::input[1]")
TO_DATE_LOCATOR   = (By.XPATH, "//div[@id='casaModalDatePicker']//*[normalize-space()='To']/following::input[1]")
DONE_BTN_LOCATOR  = (By.XPATH, "//div[@id='casaModalDatePicker']//input[@value='Done'] | //div[@id='casaModalDatePicker']//button[normalize-space()='Done'] | //div[@id='casaModalDatePicker']//a[normalize-space()='Done']")
PROCEED_LOCATOR   = (By.XPATH, "//input[@value='Proceed'] | //button[normalize-space()='Proceed'] | //a[normalize-space()='Proceed']")
EXPORT_DROPDOWN   = (By.XPATH, "//*[contains(@class,'ui-selectmenu-status') and normalize-space()='Please Select']/ancestor::a[1]")
CSV_OPTION        = (By.XPATH, "//ul[contains(@id,'menu')]//a[normalize-space()='CSV'] | //li//a[normalize-space()='CSV']")
# Logout is TWO buttons on TWO screens:
#   1) dashboard Logout (HTML not provided — best-guess text/title locator)
#   2) button on the logged-out screen — CONFIRMED: <a id="logOut" href="index.html">
LOGOUT_DASHBOARD  = (By.XPATH, "//a[normalize-space()='Logout'] | //a[contains(.,'Logout')] | //*[@title='Logout']")
LOGOUT_SECOND     = (By.ID, "logOut")
# ─────────────────────────────────────────────

OTP_PATTERNS = [
    r"one-time-password\)\s*is\s*(\d{4,8})",
    r"OTP[^\d]{0,40}?(\d{4,8})",
]

# ── Download folder: Bank_statments next to this script ──
SCRIPT_DIR   = Path(__file__).resolve().parent
DOWNLOAD_DIR = SCRIPT_DIR / "Bank_statments"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
print(f"[INIT] Download folder: {DOWNLOAD_DIR}")


# ── Gmail / OTP helpers (UID-based, no timestamps) ───────────────────
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
        print(f"    ⚠️  Could not snapshot inbox ({e}).")
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
        print(f"    ⚠️  Gmail check failed ({e}).")
    return None


# ── Selenium helpers ─────────────────────────────────────────────────
def click_el(driver, el):
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def type_into(driver, wait, element_id, text):
    el = wait.until(EC.presence_of_element_located((By.ID, element_id)))
    driver.execute_script("arguments[0].removeAttribute('readonly');", el)
    el.click(); el.clear(); el.send_keys(text)
    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('keyup',  {bubbles:true}));
        arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
    """, el)
    return el


def fill_date(driver, wait, locator, value):
    """Set the date via JS (avoids triggering the calendar popup)."""
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
        text = driver.execute_script(
            "var e=document.getElementById('loginErrorTxt');return e?e.textContent.trim():'';")
        if text:
            return text
    except Exception:
        pass
    return None


def enter_otp(driver, wait, otp):
    field = wait.until(EC.element_to_be_clickable((By.ID, "otpInput")))
    field.click(); field.clear(); field.send_keys(otp)
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
    click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "otpSubmitBtn"))))


def wait_for_new_download(folder, before, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        current = {f for f in folder.iterdir() if not f.name.endswith(".crdownload")}
        new = [f for f in (current - before) if f.is_file()]
        if new:
            time.sleep(1)
            return max(new, key=lambda f: f.stat().st_mtime)
        time.sleep(1)
    return None


def shutdown(driver, message=None):
    if message:
        print(message)
    print("\n[DONE] Closing browser...")
    try:
        driver.quit()
    except Exception:
        pass
    print("[DONE] Browser closed. Script stopped.")
    raise SystemExit(0)



# ── Proxy helpers ────────────────────────────────────────────────────
# Chrome cannot take a username/password on --proxy-server (and headless has no
# auth popup), so we run a tiny local forwarder: Chrome -> 127.0.0.1:<port> ->
# upstream proxy, and the forwarder adds the Proxy-Authorization header.
def parse_proxy(proxy_str):
    """'host:port:user:pass' -> (host, port, user, pass). Returns None if empty."""
    proxy_str = (proxy_str or "").strip()
    if not proxy_str:
        return None
    parts = proxy_str.split(":", 3)
    if len(parts) != 4:
        raise ValueError("PROXY must be in the form host:port:username:password")
    host, port, user, pwd = parts
    return host, int(port), user, pwd


class _ProxyForwarder(socketserver.BaseRequestHandler):
    upstream = None   # (host, port, auth_header_value) — set by start_local_proxy

    def handle(self):
        client = self.request
        client.settimeout(60)
        up_host, up_port, auth = self.upstream
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
                if len(head) > 1 << 20:
                    return
            head, rest = head.split(b"\r\n\r\n", 1)
            lines = [l for l in head.split(b"\r\n")
                     if not l.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))]
            is_connect = lines[0].upper().startswith(b"CONNECT ")
            lines.append(b"Proxy-Authorization: " + auth)
            if not is_connect:
                # Plain HTTP: force one request per connection so every request is authed
                lines = [l for l in lines if not l.lower().startswith(b"connection:")]
                lines.append(b"Connection: close")
            new_head = b"\r\n".join(lines) + b"\r\n\r\n"

            upstream = socket.create_connection((up_host, up_port), timeout=30)
            upstream.settimeout(60)
            upstream.sendall(new_head + rest)
            self._pipe(client, upstream)
        except Exception:
            pass

    @staticmethod
    def _pipe(a, b):
        def one_way(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        t = threading.Thread(target=one_way, args=(b, a), daemon=True)
        t.start()
        one_way(a, b)
        t.join()
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_local_proxy(proxy_str):
    """Start the local auth-injecting forwarder. Returns (server, '127.0.0.1:port') or (None, None)."""
    parsed = parse_proxy(proxy_str)
    if not parsed:
        return None, None
    host, port, user, pwd = parsed
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()

    class Handler(_ProxyForwarder):
        upstream = (host, port, b"Basic " + token.encode())

    server = _ThreadedServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    local = f"127.0.0.1:{server.server_address[1]}"
    print(f"[PROXY] Local forwarder {local} -> {host}:{port} (user: {user})")
    return server, local


# ── Browser setup (VISIBLE, downloads to Bank_statments) ─────────────
print("[1] Setting up Chrome options...")
options = Options()
options.add_argument("--headless=new")            # run WITHOUT a visible window
options.add_argument("--window-size=1920,1080")   # headless needs an explicit viewport
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--disable-extensions")
proxy_server, local_proxy = start_local_proxy(PROXY)
if local_proxy:
    options.add_argument(f"--proxy-server=http://{local_proxy}")
    print(f"[1] Chrome will use proxy: {PROXY.split(':')[0]}:{PROXY.split(':')[1]}")
else:
    print("[1] No proxy configured (PROXY is empty).")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option("useAutomationExtension", False)
options.add_experimental_option("prefs", {
    "download.default_directory": str(DOWNLOAD_DIR),
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": True,
})
print("[1] Done.")

print("[2] Launching Chrome (headless)...")
driver = webdriver.Chrome(options=options)
driver.set_page_load_timeout(30)
try:
    driver.execute_cdp_cmd("Page.setDownloadBehavior",
                           {"behavior": "allow", "downloadPath": str(DOWNLOAD_DIR)})
except Exception:
    pass
wait = WebDriverWait(driver, 20)
print("[2] Done.")

try:
    if local_proxy and VERIFY_PROXY_IP:
        print("[2a] Checking which IP Chrome is using through the proxy...")
        try:
            driver.get("https://api.ipify.org?format=text")
            ip_seen = driver.find_element(By.TAG_NAME, "body").text.strip()
            print(f"[2a] Proxy IP: {ip_seen}")
        except Exception as e:
            print(f"[2a] ⚠️  Could not verify proxy IP ({e}). Continuing anyway...")

    print("[3] Navigating to login URL...")
    driver.get(URL)
    print("[3] Page loaded.")
    time.sleep(3)

    print("[4] Entering Login ID...")
    type_into(driver, wait, "userNameText", USER_ID)
    print("[4] Login ID entered.")
    time.sleep(2)

    print("[5] Entering password...")
    type_into(driver, wait, "passwordText", PASSWORD)
    print("[5] Password entered.")
    time.sleep(2)

    print("[6] Clicking LOGIN...")
    click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "loginButton"))))
    print("[6] Login button clicked.")
    time.sleep(5)

    print("[7] Checking for login errors...")
    err = check_login_error(driver)
    if err:
        shutdown(driver, f"[LOGIN FAILED] {err}")
    print("[7] No login error.")

    print("[8] Waiting for the OTP channel dialog...")
    email_radio = wait.until(EC.element_to_be_clickable(
        (By.CSS_SELECTOR, "input[type='radio'][value='EMAIL']")))
    print("[8] OTP dialog appeared.")
    time.sleep(3)

    print("[9] Selecting E-mail channel...")
    click_el(driver, email_radio)
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", email_radio)
    print("[9] E-mail channel selected.")
    time.sleep(2)

    print("[10a] Snapshotting existing UBL emails...")
    known_otp_uids = snapshot_otp_uids()
    print(f"[10a] {len(known_otp_uids)} existing UBL email(s) recorded.")

    print("[10] Pressing Select...")
    click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "btn_select"))))
    print("[10] Select button pressed.")

    otp = None
    for i, w in enumerate(OTP_CHECK_WAITS, 1):
        print(f"[11.{i}] Waiting {w}s, then checking Gmail (attempt {i}/{len(OTP_CHECK_WAITS)})...")
        time.sleep(w)
        otp = fetch_new_otp(known_otp_uids)
        if otp:
            print(f"[11.{i}] New OTP found: {otp}")
            break
        print(f"[11.{i}] No new OTP yet.")
    if not otp:
        shutdown(driver, "[12] No OTP in the email after all checks. Stopping.")

    print("[13] Entering OTP...")
    enter_otp(driver, wait, otp)
    print("[13] OTP entered.")
    time.sleep(2)

    print("[14] Pressing Submit...")
    submit_otp(driver, wait)
    print("[14] Submit pressed.")
    time.sleep(6)

    print("[15] Checking OTP result...")
    otp_err = read_otp_error(driver)
    if otp_err:
        print(f"[15] OTP error: {otp_err}")
        print("[16] Re-checking Gmail for a newer OTP...")
        otp2 = fetch_new_otp(known_otp_uids)
        if not otp2 or otp2 == otp:
            shutdown(driver, "[16] The new OTP has not reached the inbox. Not retrying. Stopping.")
        print(f"[16] A newer OTP arrived: {otp2}. Trying once more...")
        enter_otp(driver, wait, otp2)
        time.sleep(2)
        submit_otp(driver, wait)
        print("[16] Submit pressed (2nd attempt).")
        time.sleep(6)
        if read_otp_error(driver):
            shutdown(driver, "[16] Second attempt also failed. Stopping.")
        print("[16] Second attempt succeeded.")
    else:
        print("[15] OTP accepted.")

    # ── Statement export flow ──
    print(f"[17] Waiting {WAIT_AFTER_SUBMIT_BEFORE_PROCEED}s before Proceed...")
    time.sleep(WAIT_AFTER_SUBMIT_BEFORE_PROCEED)

    print("[18] Clicking Proceed...")
    click_el(driver, wait.until(EC.element_to_be_clickable(PROCEED_LOCATOR)))
    print("[18] Proceed clicked.")

    print(f"[19] Waiting {WAIT_AFTER_PROCEED}s for the accounts page...")
    time.sleep(WAIT_AFTER_PROCEED)

    print("[20] Opening the period filter dropdown...")
    click_el(driver, wait.until(EC.element_to_be_clickable(PERIOD_DROPDOWN_BUTTON)))
    time.sleep(1)
    print("[20] Selecting 'Select Range'...")
    click_el(driver, wait.until(EC.element_to_be_clickable(PERIOD_MENU_SELECT_RANGE)))
    print("[20] 'Select Range' selected.")

    print("[20a] Waiting for the date popup...")
    wait.until(EC.visibility_of_element_located(DATE_POPUP))
    print("[20a] Date popup open.")
    time.sleep(2)

    print(f"[21] Entering From date: {FROM_DATE}")
    fill_date(driver, wait, FROM_DATE_LOCATOR, FROM_DATE)
    print(f"[22] Entering To date: {TO_DATE}")
    fill_date(driver, wait, TO_DATE_LOCATOR, TO_DATE)
    time.sleep(1)

    print("[23] Clicking Done...")
    click_el(driver, wait.until(EC.element_to_be_clickable(DONE_BTN_LOCATOR)))
    print("[23] Done clicked.")
    time.sleep(5)

    print("[24] Opening Export dropdown ('Please Select')...")
    click_el(driver, wait.until(EC.element_to_be_clickable(EXPORT_DROPDOWN)))
    time.sleep(1)

    print("[25] Choosing CSV...")
    before_files = {f for f in DOWNLOAD_DIR.iterdir() if not f.name.endswith(".crdownload")}
    click_el(driver, wait.until(EC.element_to_be_clickable(CSV_OPTION)))
    print("[25] CSV clicked. Waiting for download...")

    downloaded = wait_for_new_download(DOWNLOAD_DIR, before_files, timeout=60)
    if downloaded:
        stamp = datetime.now().strftime("%Y-%m-%d_%I-%M-%S-%p")
        new_path = DOWNLOAD_DIR / f"UBL_Statement_{stamp}{downloaded.suffix.lower()}"
        downloaded.rename(new_path)
        print(f"[26] Saved: {new_path}")
    else:
        print("[26] ⚠️  No file downloaded within the timeout.")

    print("[27] Logging out — step 1: dashboard Logout button...")
    try:
        driver.execute_script("window.onbeforeunload = null;")
        click_el(driver, wait.until(EC.element_to_be_clickable(LOGOUT_DASHBOARD)))
        print("[27] Dashboard Logout clicked.")
    except Exception as e:
        print(f"[27] WARNING — could not click dashboard Logout: {e}")
    time.sleep(4)

    print("[28] Logging out — step 2: button on the logged-out screen (#logOut)...")
    try:
        click_el(driver, wait.until(EC.element_to_be_clickable(LOGOUT_SECOND)))
        print("[28] Second logout (#logOut) clicked.")
    except Exception as e:
        print(f"[28] Second logout not found / not needed: {e}")
    time.sleep(3)
    print("[28] Logout complete.")

except SystemExit:
    raise
except Exception as e:
    print(f"\n[ERROR] Something went wrong: {e}")

finally:
    print("\n[DONE] Closing browser...")
    try:
        driver.quit()
    except Exception:
        pass
    if proxy_server:
        try:
            proxy_server.shutdown()
            proxy_server.server_close()
        except Exception:
            pass
    print("[DONE] Browser closed. Script finished.")