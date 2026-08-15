#!/usr/bin/env python3
"""
Gate.io USD1 公告云端监控（GitHub Actions 版）
- WebSocket 实时订阅 Gate 公告频道（秒级推送）
- 每隔数分钟轮询公告页面兜底（覆盖持币生息等非 WS 频道公告）
- 命中关键词 -> 创建 GitHub Issue（自动 assign 仓库主人，触发邮件通知）
- 可选 SMTP 邮件直发（配置 repo secrets: SMTP_USER / SMTP_PASS / MAIL_TO）
"""
import json
import os
import re
import smtplib
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText

import websocket

# ---------- 配置 ----------
KEYWORDS = ["USD1", "USD-1", "World Liberty"]
WS_URL = "wss://api.gateio.ws/ws/v4/ann"
CHANNELS = [
    "announcement.summary_listing",
    "announcement.summary_delisting",
    "announcement.summary_fee",
    "announcement.summary_trade",
    "announcement.summary_activity",
    "announcement.summary_announcement",
    "copy_mix_summary_listing",
    "copy_mix_summary_delisting",
    "copy_mix_summary_trade",
]
PAGE_URL = "https://www.gate.com/zh/announcements/lastest"
ARTICLE_URL = "https://www.gate.com/zh/announcements/article/{}"

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "4"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "150"))  # 秒
TEST_NOTIFY = os.environ.get("TEST_NOTIFY", "") == "1"

REPO = os.environ.get("GITHUB_REPOSITORY", "")  # owner/repo
GH_TOKEN = os.environ.get("GH_TOKEN", "")
OWNER = REPO.split("/")[0] if REPO else ""

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
MAIL_TO = os.environ.get("MAIL_TO", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

seen_lock = threading.Lock()
seen = set()
terminate = threading.Event()


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def hit_keyword(text):
    return any(k.lower() in text.lower() for k in KEYWORDS)


# ---------- GitHub API ----------
def gh_api(method, path, payload=None):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "gate-usd1-monitor",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": str(e)}


def create_issue(title, body):
    if not (REPO and GH_TOKEN):
        log(f"[notify] GH_TOKEN 未配置，仅打印: {title}")
        return
    status, rsp = gh_api("POST", "issues", {
        "title": title,
        "body": body,
        "assignees": [OWNER],
    })
    if status in (200, 201):
        log(f"[notify] Issue 创建成功: {rsp.get('html_url', '')}")
    else:
        log(f"[notify] Issue 创建失败 {status}: {json.dumps(rsp, ensure_ascii=False)[:300]}")


def send_mail(subject, body):
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = MAIL_TO
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [MAIL_TO], msg.as_string())
        log(f"[mail] 邮件已发送至 {MAIL_TO}: {subject}")
    except Exception as e:
        log(f"[mail] 邮件发送失败: {e}")


def notify(ann_id, title, detail):
    body = (
        f"**{title}**\n\n{detail}\n\n"
        f"---\n检测时间: {datetime.now(timezone.utc).isoformat()}\n"
        f"来源: Gate.io 公告监控（自动检测）"
    )
    log(f"*** 新 USD1 公告 *** id={ann_id} title={title}")
    create_issue(f"[Gate USD1] {title}", body)
    send_mail(f"[Gate USD1 监控] {title}", body)


# ---------- 状态持久化（提交到仓库，[skip ci] 避免触发工作流） ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state_to_repo():
    if not (REPO and GH_TOKEN) or not seen:
        return
    import base64
    state = {"seen": sorted(seen)[-500:], "updated_at": datetime.now(timezone.utc).isoformat()}
    content = base64.b64encode(
        json.dumps(state, ensure_ascii=False, indent=2).encode()).decode()
    status, cur = gh_api("GET", "contents/state.json")
    payload = {
        "message": "update state [skip ci]",
        "content": content,
        "branch": "main",
    }
    if status == 200:
        payload["sha"] = cur.get("sha")
    st, rsp = gh_api("PUT", "contents/state.json", payload)
    log(f"[state] 保存状态到仓库: {st}")


# ---------- 公告页面轮询（curl_cffi 绕过 Akamai） ----------
def fetch_announcements():
    from curl_cffi import requests as cffi_requests
    resp = cffi_requests.get(
        PAGE_URL,
        impersonate="chrome120",
        timeout=25,
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    if resp.status_code != 200:
        log(f"[poll] 页面请求失败: HTTP {resp.status_code}")
        return []
    m = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", resp.text, re.DOTALL)
    if not m:
        log("[poll] 未找到 __NEXT_DATA__")
        return []
    data = json.loads(m.group(1))
    items = (
        data.get("props", {}).get("pageProps", {})
        .get("listData", {}).get("list", [])
    )
    result = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ann_id = str(it.get("id") or it.get("ann_id") or "")
        title = str(it.get("title") or "")
        if ann_id and title:
            result.append({
                "id": ann_id,
                "title": title,
                "brief": str(it.get("brief") or ""),
                "url": ARTICLE_URL.format(ann_id),
            })
    return result


def poll_once(is_init):
    try:
        anns = fetch_announcements()
    except Exception as e:
        log(f"[poll] 异常: {e}")
        return
    log(f"[poll] 获取到 {len(anns)} 条公告")
    for a in anns:
        with seen_lock:
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            is_new = not is_init
        if is_new and hit_keyword(a["title"] + " " + a["brief"]):
            notify(a["id"], a["title"], f"{a['brief']}\n\n链接: {a['url']}")


# ---------- WebSocket 实时订阅 ----------
def on_ws_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        return
    event = data.get("event")
    if event == "subscribe":
        log(f"[ws] 订阅成功: {data.get('channel')}")
        return
    result = data.get("result")
    if result is None:
        return
    text = json.dumps(result, ensure_ascii=False)
    if not hit_keyword(text):
        return
    strings = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9 ，。、！？：；%().\-_/]{6,}", text)
    key_part = next((s for s in strings if any(k.lower() in s.lower() for k in KEYWORDS)), "")
    ann_id = str(result.get("id") or result.get("ann_id") or f"ws-{int(time.time())}")
    with seen_lock:
        if ann_id in seen:
            return
        seen.add(ann_id)
    title = (result.get("title") or key_part or "USD1 相关公告")[:80]
    notify(ann_id, title, f"频道: {data.get('channel')}\n\n{text[:1000]}")


def on_ws_error(ws, error):
    log(f"[ws] 错误: {error}")


def on_ws_close(ws, status, msg):
    log(f"[ws] 连接关闭: {status}")


def on_ws_open(ws):
    log("[ws] 已连接，开始订阅频道...")
    for ch in CHANNELS:
        ws.send(json.dumps({
            "time": int(time.time()),
            "channel": ch,
            "event": "subscribe",
            "payload": ["cn", "en"],
        }))


def ws_loop():
    while not terminate.is_set():
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
                on_open=on_ws_open,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log(f"[ws] 异常: {e}")
        if terminate.is_set():
            break
        log("[ws] 5 秒后重连...")
        terminate.wait(5)


# ---------- 主流程 ----------
def main():
    log(f"启动云端监控: RUN_MINUTES={RUN_MINUTES} POLL_INTERVAL={POLL_INTERVAL}s repo={REPO}")

    state = load_state()
    is_init = state is None
    if state:
        with seen_lock:
            seen.update(state.get("seen", []))
        log(f"[state] 从本地恢复 {len(seen)} 条已见记录")
    else:
        if REPO and GH_TOKEN:
            st, cur = gh_api("GET", "contents/state.json")
            if st == 200:
                import base64
                remote = json.loads(base64.b64decode(cur["content"]))
                with seen_lock:
                    seen.update(remote.get("seen", []))
                is_init = False
                log(f"[state] 从仓库恢复 {len(seen)} 条已见记录")

    poll_once(is_init)
    if is_init:
        log("[init] 首次运行：已记录当前公告基线，不发送通知")

    threading.Thread(target=ws_loop, daemon=True).start()

    if TEST_NOTIFY:
        notify("test-000", "测试通知 - 云端监控链路验证",
               "这是一条测试通知。如果你收到此 Issue 指派通知或邮件，说明云端监控通知链路正常。")

    deadline = time.time() + RUN_MINUTES * 60
    next_poll = time.time() + POLL_INTERVAL
    while time.time() < deadline:
        if time.time() >= next_poll:
            poll_once(False)
            next_poll = time.time() + POLL_INTERVAL
        time.sleep(1)

    log("本轮监控结束")
    if seen:
        save_state_to_repo()


if __name__ == "__main__":
    main()
