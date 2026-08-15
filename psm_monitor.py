#!/usr/bin/env python3
# Auto-triggered via push
"""
USDD PSM monitor for GitHub Actions.
Checks PSM contract USDT balance (USDD->USDT Available) every 5 min.
Sends email alert when below threshold.
Uses same secrets as gate-usd1-monitor: SMTP_USER / SMTP_PASS / MAIL_TO
"""

import json
import os
import sys
import time
import smtplib
import urllib.request
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timezone, timedelta

# ======================== Config ========================

PSM_CONTRACT = "TSUYvQ5tdd3DijCD1uGunGLpftHuSZ12sQ"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID_URL = f"https://api.trongrid.io/v1/accounts/{PSM_CONTRACT}"
USDD_API_URL = "https://openapi.usdd.io/api/v1/data-platform/latest-collateral?chain=tron"

THRESHOLD = int(os.environ.get("THRESHOLD", "20000000"))
USDT_DECIMALS = 6

COOLDOWN_SECONDS = 1800  # 30 min
STATE_FILE = ".psm_alert_state"

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = 465

# ======================== Data ========================

def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "USDD-PSM-Monitor/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_psm_usdt_balance():
    """Query PSM contract USDT balance (USDD->USDT Available)."""
    data = fetch_json(TRONGRID_URL)
    trc20_list = data.get("data", [{}])[0].get("trc20", [])
    for token in trc20_list:
        if USDT_CONTRACT in token:
            raw = int(token[USDT_CONTRACT])
            return raw / (10 ** USDT_DECIMALS)
    raise RuntimeError("USDT balance not found in PSM contract")


def get_psm_vault_info():
    """Query PSM-USDT-A vault line and debt."""
    data = fetch_json(USDD_API_URL)
    items = data.get("data", {}).get("items", [])
    for item in items:
        if item.get("vaultType") == "PSM-USDT-A":
            line = float(item.get("line", 0))
            debt = float(item.get("debt", 0))
            return {"line": line, "debt": debt, "available_usdt_to_usdd": line - debt}
    return {"line": 0, "debt": 0, "available_usdt_to_usdd": 0}


# ======================== Cooldown ========================

def load_last_alert_time():
    try:
        with open(STATE_FILE, "r") as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0

def save_alert_time():
    with open(STATE_FILE, "w") as f:
        f.write(str(time.time()))

def clear_alert_state():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


# ======================== Email ========================

def send_email(subject, body):
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        print("[WARN] SMTP_USER/SMTP_PASS/MAIL_TO not set, skip email")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("USDD PSM Monitor", SMTP_USER))
    msg["To"] = MAIL_TO
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [MAIL_TO], msg.as_string())
    return True


# ======================== Main ========================

def main():
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    ts = now.strftime("%Y-%m-%d %H:%M:%S CST")

    print(f"[{ts}] USDD PSM monitor started")
    print(f"  Threshold: {THRESHOLD:,} USDT")
    print(f"  Cooldown: {COOLDOWN_SECONDS // 60} min")

    try:
        usdt_balance = get_psm_usdt_balance()
        vault_info = get_psm_vault_info()
    except Exception as e:
        print(f"[ERROR] Query failed: {e}")
        if os.environ.get("GITHUB_ACTIONS"):
            with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
                f.write(f"### PSM Monitor - Query Failed\n\n{e}\n")
        sys.exit(1)

    print(f"\n  USDD -> USDT  Available: {usdt_balance:,.4f} USDT")
    print(f"  USDT -> USDD  Available: {vault_info['available_usdt_to_usdd']:,.4f} USDD")
    print(f"               (line: {vault_info['line']:,.0f}, debt: {vault_info['debt']:,.2f})")

    alert = usdt_balance < THRESHOLD

    if alert:
        print(f"\n  ALERT: {usdt_balance:,.4f} < {THRESHOLD:,}")
        last_alert = load_last_alert_time()
        elapsed = time.time() - last_alert
        if elapsed < COOLDOWN_SECONDS:
            remaining = int((COOLDOWN_SECONDS - elapsed) / 60)
            print(f"  Cooldown: {int(elapsed/60)} min since last alert, {remaining} min remaining")
            print(f"  Skip email")
        else:
            print(f"  Sending alert email to {MAIL_TO}...")
            subject = f"USDD PSM Alert: Available {usdt_balance:,.0f} USDT < {THRESHOLD:,}"
            body = f"""USDD PSM Available Alert

USDD -> USDT Available below threshold!

----------------------------------------
Current Available: {usdt_balance:,.4f} USDT
Threshold: {THRESHOLD:,} USDT
Time: {ts}
----------------------------------------

PSM page: https://app.usdd.io/tron/psm
Contract: {PSM_CONTRACT}

Other data:
  USDT -> USDD Available: {vault_info['available_usdt_to_usdd']:,.4f} USDD
  PSM line: {vault_info['line']:,.0f}
  PSM debt: {vault_info['debt']:,.2f}
"""
            try:
                if send_email(subject, body):
                    save_alert_time()
                    print(f"  OK: Alert email sent")
                else:
                    print(f"  FAIL: Missing config")
            except Exception as e:
                print(f"  FAIL: {e}")
    else:
        print(f"\n  OK: {usdt_balance:,.4f} >= {THRESHOLD:,}")
        clear_alert_state()

    # GitHub Actions summary
    if os.environ.get("GITHUB_ACTIONS"):
        status = "ALERT" if alert else "OK"
        try:
            with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
                f.write(f"### {status} - USDD PSM Monitor\n\n| Metric | Value |\n|------|------|\n| USDD->USDT Available | {usdt_balance:,.4f} USDT |\n| USDT->USDD Available | {vault_info['available_usdt_to_usdd']:,.4f} USDD |\n| Threshold | {THRESHOLD:,} USDT |\n| Time | {ts} |\n| Page | [PSM](https://app.usdd.io/tron/psm) |\n")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
