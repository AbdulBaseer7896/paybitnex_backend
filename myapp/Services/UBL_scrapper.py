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
from typing import Optional, List, Set

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
WAIT_AFTER_SUBMIT_BEFORE_PROCEED = 30
WAIT_AFTER_PROCEED               = 45
OTP_CHECK_WAITS                  = [20, 20, 40]

OTP_PATTERNS = [
    r"one-time-password\)\s*is\s*(\d{4,8})",
    r"OTP[^\d]{0,40}?(\d{4,8})",
]

# Default download folder: Bank_statments in paybitnex_backend root
DEFAULT_DOWNLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "Bank_statments"

# Proxy configuration (e.g. Decodo Pakistan residential proxy)
DEFAULT_PROXY = "gate.decodo.com:10001:user-spduoaryo1-sessionduration-360-asn-136969:5gsB8b3LSlx~4sysRp"
PROXY_STRING = os.environ.get("UBL_PROXY", DEFAULT_PROXY)


def create_proxy_auth_extension(proxy_str: str) -> Optional[str]:
    """Creates a temporary Chrome extension .zip for authenticated HTTP proxy.
    Returns path to the temporary zip file, or None if disabled.
    """
    if not proxy_str or proxy_str.strip().lower() in ("none", "false", "0", "off", ""):
        return None

    proxy_str = proxy_str.strip()
    host = ""
    port = 80
    user = ""
    password = ""

    if "@" in proxy_str or "://" in proxy_str:
        from urllib.parse import urlparse
        raw_url = proxy_str if "://" in proxy_str else f"http://{proxy_str}"
        p = urlparse(raw_url)
        host = p.hostname or ""
        port = p.port or 80
        user = p.username or ""
        password = p.password or ""
    else:
        parts = proxy_str.split(":")
        if len(parts) == 4:
            host, port_s, user, password = parts[0], parts[1], parts[2], parts[3]
            try:
                port = int(port_s)
            except ValueError:
                port = 80
        elif len(parts) == 2:
            host, port_s = parts[0], parts[1]
            try:
                port = int(port_s)
            except ValueError:
                port = 80
        else:
            return None

    manifest = """{
    "version": "1.0.0",
    "manifest_version": 2,
    "name": "Chrome Proxy Auth",
    "permissions": [
        "proxy",
        "tabs",
        "unlimitedStorage",
        "storage",
        "<all_urls>",
        "webRequest",
        "webRequestBlocking"
    ],
    "background": {
        "scripts": ["background.js"]
    }
}"""

    bg_script = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "http",
            host: "{host}",
            port: parseInt({port})
        }},
        bypassList: ["localhost", "127.0.0.1"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
"""
    if user and password:
        safe_user = user.replace('\\', '\\\\').replace('"', '\\"')
        safe_pass = password.replace('\\', '\\\\').replace('"', '\\"')
        bg_script += f"""
chrome.webRequest.onAuthRequired.addListener(
    function(details) {{
        return {{
            authCredentials: {{
                username: "{safe_user}",
                password: "{safe_pass}"
            }}
        }};
    }},
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""

    import tempfile, zipfile
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="ubl_proxy_")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as zp:
        zp.writestr("manifest.json", manifest)
        zp.writestr("background.js", bg_script)
    return path


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
        text = driver.execute_script(
            "var e=document.getElementById('loginErrorTxt');return e?e.textContent.trim():'';")
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


# ─────────────────────────────────────────────
#  Core Scraper Function
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
    Routes traffic through proxy (default: Pakistan residential proxy) if configured.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

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
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")

    proxy_ext_path = None
    target_proxy = proxy if proxy is not None else PROXY_STRING
    if target_proxy:
        proxy_ext_path = create_proxy_auth_extension(target_proxy)
        if proxy_ext_path:
            p_parts = target_proxy.split(":")
            host_label = f"{p_parts[0]}:{p_parts[1]}" if len(p_parts) >= 2 else "configured proxy"
            log(f"[1.1] Routing through proxy: {host_label}")
            options.add_extension(proxy_ext_path)

    if not proxy_ext_path:
        options.add_argument("--disable-extensions")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {
        "download.default_directory": str(target_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })

    log(f"[2] Launching Chrome ({'headless' if headless else 'visible'})...")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(target_dir)},
        )
    except Exception:
        pass
    wait = WebDriverWait(driver, 30)

    try:
        log("[3] Navigating to UBL login URL...")
        try:
            driver.get(URL)
        except Exception as load_err:
            log(f"[WARN] Initial page load wait exceeded ({load_err}); stopping page load and proceeding...")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(3)

        log("[4] Entering Login ID...")
        type_into(driver, wait, "userNameText", USER_ID)
        time.sleep(2)

        log("[5] Entering password...")
        type_into(driver, wait, "passwordText", PASSWORD)
        time.sleep(2)

        log("[6] Clicking LOGIN...")
        click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "loginButton"))))
        time.sleep(5)

        log("[7] Checking for login errors...")
        err = check_login_error(driver)
        if err:
            raise RuntimeError(f"Login failed: {err}")

        log("[8] Waiting for the OTP channel dialog...")
        email_radio = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[type='radio'][value='EMAIL']")))
        time.sleep(2)

        log("[9] Selecting E-mail channel...")
        click_el(driver, email_radio)
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", email_radio)
        time.sleep(2)

        log("[10a] Snapshotting existing UBL emails...")
        known_otp_uids = snapshot_otp_uids()

        log("[10] Pressing Select button...")
        click_el(driver, wait.until(EC.element_to_be_clickable((By.ID, "btn_select"))))

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

        # Statement export flow
        log(f"[17] Waiting {WAIT_AFTER_SUBMIT_BEFORE_PROCEED}s before Proceed...")
        time.sleep(WAIT_AFTER_SUBMIT_BEFORE_PROCEED)

        log("[18] Clicking Proceed...")
        click_el(driver, wait.until(EC.element_to_be_clickable(PROCEED_LOCATOR)))
        time.sleep(WAIT_AFTER_PROCEED)

        log("[20] Opening period filter dropdown...")
        click_el(driver, wait.until(EC.element_to_be_clickable(PERIOD_DROPDOWN_BUTTON)))
        time.sleep(1)

        log("[20] Selecting 'Select Range'...")
        click_el(driver, wait.until(EC.element_to_be_clickable(PERIOD_MENU_SELECT_RANGE)))
        wait.until(EC.visibility_of_element_located(DATE_POPUP))
        time.sleep(2)

        log(f"[21] Entering From date: {target_from}")
        fill_date(driver, wait, FROM_DATE_LOCATOR, target_from)
        log(f"[22] Entering To date: {target_to}")
        fill_date(driver, wait, TO_DATE_LOCATOR, target_to)
        time.sleep(1)

        log("[23] Clicking Done...")
        click_el(driver, wait.until(EC.element_to_be_clickable(DONE_BTN_LOCATOR)))
        time.sleep(5)

        log("[24] Opening Export dropdown...")
        click_el(driver, wait.until(EC.element_to_be_clickable(EXPORT_DROPDOWN)))
        time.sleep(1)

        log("[25] Choosing CSV...")
        before_files = {f for f in target_dir.iterdir() if not f.name.endswith(".crdownload")}
        click_el(driver, wait.until(EC.element_to_be_clickable(CSV_OPTION)))
        log("[25] CSV export triggered. Waiting for download...")

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

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if proxy_ext_path and os.path.exists(proxy_ext_path):
            try:
                os.remove(proxy_ext_path)
            except Exception:
                pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="UBL Statement Scraper")
    parser.add_argument("--from-date", type=str, default="01/09/2026", help="From date (DD/MM/YYYY)")
    parser.add_argument("--to-date", type=str, default="11/09/2026", help="To date (DD/MM/YYYY)")
    parser.add_argument("--download-dir", type=str, default=None, help="Folder to save statement CSV")
    parser.add_argument("--visible", action="store_true", default=False, help="Run browser visibly")
    args = parser.parse_args()

    saved_file = scrape_ubl_statement(
        from_date=args.from_date,
        to_date=args.to_date,
        download_dir=Path(args.download_dir) if args.download_dir else None,
        headless=not args.visible,
    )
    print(f"\nCompleted! Downloaded file: {saved_file}")