#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CNN 공포·탐욕 지수 -> 텔레그램. 미국장 장중/마감 하루 2회.

릴레이(wscn_relay.py)와 완전히 독립이다. 여기서 실패해도 속보 중계에는
영향이 없고, 반대도 마찬가지다.
"""

import json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# 워크플로가 넘겨주는 cron 문자열. 어느 발송인지 구분하는 데만 쓴다.
SCHEDULE = os.environ.get("FNG_SCHEDULE", "").strip()
DRY_RUN = os.environ.get("FNG_DRY_RUN", "0") == "1"

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
KST = timezone(timedelta(hours=9))
TRIES = 3
BACKOFF = (1.0, 3.0)

# cron -> 라벨. 워크플로의 schedule 과 반드시 같이 고쳐야 한다.
LABELS = {
    "0 16 * * 1-5": "미국장 장중",
    "30 20 * * 1-5": "미국장 마감",
}

KO = {"extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
      "greed": "탐욕", "extreme greed": "극단적 탐욕"}
EMO = {"extreme fear": "😱", "fear": "😨", "neutral": "😐",
       "greed": "🤑", "extreme greed": "🔥"}


def log(m):
    print("[%s] %s" % (datetime.now(KST).strftime("%H:%M:%S"), m), flush=True)


def fetch():
    """CNN 비공식 dataviz 엔드포인트. 5xx/타임아웃은 재시도한다."""
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(URL, headers={
                "User-Agent": UA,
                "Referer": "https://edition.cnn.com/",
                "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if 400 <= e.code < 500 and e.code != 429:
                break   # 요청 자체 문제 -> 재시도 무의미
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRIES:
            time.sleep(BACKOFF[att - 1])
    raise RuntimeError("CNN 조회 실패 (%d회 시도, %s)" % (att, last))


def bar(v, n=22):
    p = max(0, min(n - 1, int(v / 100.0 * n)))
    return "".join("●" if i == p else "─" for i in range(n))


def render(j):
    fg = j["fear_and_greed"]
    s = float(fg["score"])
    rating = fg["rating"]
    ts = datetime.fromisoformat(fg["timestamp"]).astimezone(KST)
    age = (datetime.now(KST) - ts).total_seconds() / 3600.0
    label = LABELS.get(SCHEDULE, "현재")

    def cmp(name, v):
        return "  %-6s %4.0f  (%+.1f)" % (name, float(v), s - float(v))

    return "\n".join([
        "%s CNN 공포·탐욕 지수 — %s" % (EMO.get(rating, "📊"), label),
        "",
        "   %.0f  ·  %s" % (s, KO.get(rating, rating)),
        "   공포 %s 탐욕" % bar(s),
        "        0    25   45 55   75   100",
        "",
        cmp("전일", fg["previous_close"]),
        cmp("1주 전", fg["previous_1_week"]),
        cmp("1달 전", fg["previous_1_month"]),
        cmp("1년 전", fg["previous_1_year"]),
        "",
        # CNN 은 미국 장중에만 갱신한다. 휴장이면 값이 그대로이므로
        # 기준 시각을 항상 붙여서 신선도를 눈으로 확인할 수 있게 한다.
        "기준: %s KST (%.0f시간 전)" % (ts.strftime("%m-%d %H:%M"), age),
        "cnn.com/markets/fear-and-greed",
    ])


def tg_send(text):
    if DRY_RUN:
        log("DRY_RUN — 전송 생략")
        print("-" * 46)
        print(text)
        print("-" * 46)
        return True
    url = "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN
    body = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text,
        "disable_web_page_preview": "true"}).encode()
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code == 429:
                try:
                    j = json.loads(e.read().decode("utf-8", "replace"))
                    w = int(j.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    w = 5
                log("429 -> %d초 대기" % w)
                time.sleep(min(w + 1, 60))
                continue
            if 400 <= e.code < 500:
                break   # 설정 오류. 재시도해도 같다.
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRIES:
            time.sleep(BACKOFF[att - 1])
    log("전송 실패 (%d회 시도, %s)" % (att, last))
    return False


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        log("FATAL: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
        return 1
    try:
        j = fetch()
    except Exception as e:
        log("FATAL: %s" % e)
        return 1
    msg = render(j)
    log("발송: %s / %s" % (LABELS.get(SCHEDULE, "수동"),
                          msg.split("\n")[2].strip()))
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
