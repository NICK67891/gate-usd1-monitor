#!/usr/bin/env python3
"""
JustLend USDT Borrow APY monitor for GitHub Actions.
Checks jUSDT borrow APY on JustLend via openapi.just.network.

openapi.just.network sits behind Cloudflare and rejects GitHub Actions
runner IPs (403). Direct fetch is tried first (works locally / from
WorkBuddy sandbox); if it fails, the request is routed through the
r.jina.ai reader proxy as fallback.

Alert policy: sends one email per check while APY exceeds the threshold.
The workflow runs a 5-minute check loop (self-chaining sessions), so this
means a reminder every 5 minutes.

Secrets required: SMTP_USER / SMTP_PASS / MAIL_TO (same as psm_monitor).
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

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = 465

# Data source chain: direct first, proxy fallbacks after.
SOURCES = [
    (
        "direct",
        API_URL,
        {
            "User-Agent": "JustLend-APY-Monitor/1.0",
            "Accept": "application/json",
        },
    ),
    (
        "jina-proxy",
        "https://r.jina.ai/" + API_URL,
        {
            "User-Agent": "JustLend-APY-Monitor/1.0",
            "Accept": "application/json",
            "x-return-format": "text",
        },
    ),
    (
        "jina-proxy-plain",
        "https://r.jina.ai/" + API_URL,
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "*/*",
        },
    ),
]

# ======================== Data ========================

def fetch_text(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_json_text(text):
    text = text.strip()
    # r.jina.ai may wrap content in markdown fences; strip them.
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    data = json.loads(text)
    # r.jina.ai may return its own JSON envelope:
    # {"code":200,"data":{"text":"<inner json>",...}}
    if (isinstance(data, dict) and data.get("code") == 200
            and isinstance(data.get("data"), dict)):
        inner = data["data"].get("text") or data["data"].get("content")
        if isinstance(inner, str) and inner.strip():
            try:
                data = json.loads(inner)
            except ValueError:
                pass
    return data


def parse_market(data):
    if not isinstance(data, dict):
        raise RuntimeError("unexpected response type: %s" % type(data).__name__)
    if data.get("code") != 0:
        raise RuntimeError("API error: %s" % data.get("message", "unknown"))
    token_list = data.get("data", {}).get("tokenList", [])
    if not token_list:
        raise RuntimeError("empty tokenList in response")
    for token in token_list:
        if token.get("symbol") == "jUSDT" or token.get("address") == JUSDT_ADDRESS:
            return {
                "borrow_apy": float(token.get("borrowRate", "0")),
                "supply_apy": float(token.get("supplyRate", "0")),
                "cash": float(token.get("cash", "0")),
                "total_borrows": float(token.get("totalBorrows", "0")),
                "symbol": token.get("symbol"),
            }
    raise RuntimeError("jUSDT market not found in response")


def get_usdt_borrow_apy():
    """Try each data source (direct first, proxy fallback) with retries."""
    errors = []
    for name, url, headers in SOURCES:
        for attempt in (1, 2):
            try:
                market = parse_market(parse_json_text(fetch_text(url, headers)))
                market["source"] = name
                return market
            except Exception as e:
                errors.append("%s#%d: %s" % (name, attempt, e))
                if attempt == 1:
                    time.sleep(2)
    raise RuntimeError("all data sources failed -> " + " | ".join(errors))


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

    try:
        market = get_usdt_borrow_apy()
    except Exception as e:
        print("[ERROR] Query failed: %s" % e)
        if os.environ.get("GITHUB_ACTIONS"):
            try:
                with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
                    f.write("### USDT APY Monitor - Query Failed\n\n%s\n" % e)
            except Exception:
                pass
        sys.exit(1)

    borrow_apy = market["borrow_apy"]
    apy_pct = borrow_apy * 100
    threshold_pct = THRESHOLD * 100

    print("  Data source:     %s" % market["source"])
    print("  USDT Borrow APY: %.4f%%" % apy_pct)
    print("  Supply APY:      %.4f%%" % (market["supply_apy"] * 100))
    print("  Available Cash:  {:,.2f} USDT".format(market["cash"]))
    print("  Total Borrows:   {:,.2f} USDT".format(market["total_borrows"]))

    alert = borrow_apy > THRESHOLD

    if alert:
        print("\n  ALERT: %.4f%% > %.0f%%" % (apy_pct, threshold_pct))
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
                print("  OK: Alert email sent")
            else:
                print("  FAIL: Missing SMTP config")
        except Exception as e:
            print("  FAIL: %s" % e)
    else:
        print("\n  OK: %.4f%% <= %.0f%%" % (apy_pct, threshold_pct))

    # GitHub Actions step summary
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
                    "| Data Source | %s |\n"
                    "| Time | %s |\n"
                    "| Page | [JustLend](%s) |\n"
                ).format(market["cash"], market["total_borrows"]) % (
                    status, apy_pct, market["supply_apy"] * 100,
                    threshold_pct, market["source"], ts, MARKET_PAGE
                )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
