#!/usr/bin/env python3
"""
JustLend USDT Borrow APY monitor for GitHub Actions.
Checks jUSDT borrow APY on JustLend via openapi.just.network.
Sends email alert when APY exceeds 5% threshold.
Uses same secrets as psm_monitor: SMTP_USER / SMTP_PASS / MAIL_TO
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

API_URL = "https://openapi.just.network/lend/jtoken"
JUSDT_ADDRESS = "TXJgMdjVX5dKiQaUi9QobwNxtSQaFqccvd"
MARKET_PAGE = "https://app.justlend.org/marketDetailNew?jtokenAddress=TXJgMdjVX5dKiQaUi9QobwNxtSQaFqccvd&_from=/homeV1&lang=zh-TC"

THRESHOLD = float(os.environ.get("APY_THRESHOLD", "0.05"))  # 5%

COOLDOWN_SECONDS = 1800  # 30 min
STATE_FILE = ".usdt_apy_alert_state"

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = 465

# ======================== Data ========================

def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "JustLend-APY-Monitor/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_usdt_borrow_apy():
    """Query JustLend API for jUSDT borrow APY and market data."""
    data = fetch_json(API_URL)
    if data.get("code") != 0:
        raise RuntimeError("API error: %s" % data.get("message", "unknown"))
    token_list = data.get("data", {}).get("tokenList", [])
    for token in token_list:
        if token.get("symbol") == "jUSDT" or token.get("address") == JUSDT_ADDRESS:
            return {
                "borrow_apy": float(token.get("borrowRate", "0")),
                "supply_apy": float(token.get("supplyRate", "0")),
                "cash": float(token.get("cash", "0")),
                "total_borrows": float(token.get("totalBorrows", "0")),
                "symbol": token.get("symbol"),
            }
    raise RuntimeError("jUSDT market not found")


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
    msg["From"] = formataddr(("JustLend APY Monitor", SMTP_USER))
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

    print("[%s] JustLend USDT APY monitor started" % ts)
    print("  Threshold: %.0f%%" % (THRESHOLD * 100))
    print("  Cooldown: %d min" % (COOLDOWN_SECONDS // 60))

    try:
        market = get_usdt_borrow_apy()
    except Exception as e:
        print("[ERROR] Query failed: %s" % e)
        if os.environ.get("GITHUB_ACTIONS"):
            with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
                f.write("### USDT APY Monitor - Query Failed\n\n%s\n" % e)
        sys.exit(1)

    borrow_apy = market["borrow_apy"]
    apy_pct = borrow_apy * 100
    threshold_pct = THRESHOLD * 100

    print("\n  USDT Borrow APY: %.4f%%" % apy_pct)
    print("  Supply APY:      %.4f%%" % (market["supply_apy"] * 100))
    print("  Available Cash:  {:,.2f} USDT".format(market["cash"]))
    print("  Total Borrows:   {:,.2f} USDT".format(market["total_borrows"]))

    alert = borrow_apy > THRESHOLD

    if alert:
        print("\n  ALERT: %.4f%% > %.0f%%" % (apy_pct, threshold_pct))
        last_alert = load_last_alert_time()
        elapsed = time.time() - last_alert
        if elapsed < COOLDOWN_SECONDS:
            remaining = int((COOLDOWN_SECONDS - elapsed) / 60)
            print("  Cooldown: %d min since last alert, %d min remaining" % (int(elapsed/60), remaining))
            print("  Skip email")
        else:
            print("  Sending alert email to %s..." % MAIL_TO)
            subject = "JustLend USDT Borrow APY Alert: %.2f%% > %.0f%%" % (apy_pct, threshold_pct)
            body = (
                "JustLend USDT Borrow APY Alert\n\n"
                "USDT Borrow APY exceeds threshold!\n\n"
                "----------------------------------------\n"
                "Current Borrow APY: %.4f%%\n"
                "Threshold:          %.0f%%\n"
                "Time:               %s\n"
                "----------------------------------------\n\n"
                "Market page: %s\n"
                "jUSDT Contract: %s\n\n"
                "Other market data:\n"
                "  Supply APY:     %.4f%%\n"
                "  Available Cash: {:,.2f} USDT\n"
                "  Total Borrows:  {:,.2f} USDT\n"
            ).format(market["cash"], market["total_borrows"]) % (
                apy_pct, threshold_pct, ts, MARKET_PAGE, JUSDT_ADDRESS,
                market["supply_apy"] * 100
            )
            try:
                if send_email(subject, body):
                    save_alert_time()
                    print("  OK: Alert email sent")
                else:
                    print("  FAIL: Missing config")
            except Exception as e:
                print("  FAIL: %s" % e)
    else:
        print("\n  OK: %.4f%% <= %.0f%%" % (apy_pct, threshold_pct))
        clear_alert_state()

    # GitHub Actions summary
    if os.environ.get("GITHUB_ACTIONS"):
        status = "ALERT" if alert else "OK"
        try:
            with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
                f.write(
                    "### %s - USDT APY Monitor\n\n"
                    "| Metric | Value |\n|------|------|\n"
                    "| Borrow APY | %.4f%% |\n"
                    "| Supply APY | %.4f%% |\n"
                    "| Threshold | %.0f%% |\n"
                    "| Cash | {:,.2f} USDT |\n"
                    "| Total Borrows | {:,.2f} USDT |\n"
                    "| Time | %s |\n"
                    "| Page | [JustLend](%s) |\n"
                ).format(market["cash"], market["total_borrows"]) % (
                    status, apy_pct, market["supply_apy"] * 100,
                    threshold_pct, ts, MARKET_PAGE
                )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
