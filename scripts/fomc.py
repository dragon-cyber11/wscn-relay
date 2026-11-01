#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""폴리마켓 다음 FOMC 금리 결정 확률 -> 텔레그램. 평일 미국 장 마감 직후 한 번.

폴리마켓 'fomc' 시리즈에서 아직 안 끝난 "Fed Decision in <월>?" 이벤트 중
가장 가까운 회의를 자동으로 고른다. 회의가 끝나면 다음 회의로 넘어가므로
slug 를 손으로 바꿀 필요가 없다. 발송 시각은 wscn 릴레이 루프가 정한다.
전송은 fng.py 의 tg_send 를 그대로 쓴다(응답 타임아웃 중복 방지 포함).
"""

import json, sys, time
import urllib.error, urllib.request
from datetime import datetime, timezone

from fng import KST, TRIES, BACKOFF, UA, log, tg_send

SERIES = ("https://gamma-api.polymarket.com/events"
          "?series_id=35&closed=false&limit=50")   # 35 = fomc 시리즈
UP, DOWN = "📈", "📉"
BAR_N = 15

# 폴리마켓 결과 이름 -> (정렬 순서, 표시명). 인하 큰 쪽부터 인상 큰 쪽까지.
OUTCOMES = {
    "50+ bps decrease": (0, "인하 50bp+"),
    "25 bps decrease":  (1, "인하 25bp"),
    "No change":        (2, "동결"),
    "25 bps increase":  (3, "인상 25bp"),
    "50+ bps increase": (4, "인상 50bp+"),
}
MONTH_KO = {m: "%d월" % i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _get(url):
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRIES:
            time.sleep(BACKOFF[att - 1])
    log("폴리마켓 FOMC 조회 실패 (%s)" % last)
    return None


def next_meeting(events, now=None):
    """마감이 아직 안 지난 금리 결정 이벤트 중 가장 가까운 것."""
    now = now or datetime.now(timezone.utc)
    best = None
    for e in events or []:
        if not (e.get("title") or "").startswith("Fed Decision in"):
            continue
        try:
            end = datetime.fromisoformat(e["endDate"].replace("Z", "+00:00"))
        except Exception:
            continue
        if end > now and (best is None or end < best[0]):
            best = (end, e)
    return best[1] if best else None


def money(v):
    v = float(v or 0)
    return "$%.1fM" % (v / 1e6) if v >= 1e6 else "$%.0fK" % (v / 1e3)


def rows(ev):
    """[(순서, 표시명, 확률%, 24h변화%p|None)] — 활성 마켓의 'Yes' 가격."""
    out = []
    for m in ev.get("markets") or []:
        hit = OUTCOMES.get(m.get("groupItemTitle"))
        if not hit or not m.get("active") or m.get("closed"):
            continue
        try:
            p = float(json.loads(m["outcomePrices"])[0]) * 100.0
        except Exception:
            continue
        ch = m.get("oneDayPriceChange")
        out.append((hit[0], hit[1], p,
                    round(float(ch) * 100.0, 1) if ch else None))
    return sorted(out)


def bar(p, total):
    # 막대를 맨 앞에 둬서 줄마다 같은 폭으로 정렬되게 한다(블록 문자는 폭 동일).
    share = p / total if total else 0.0
    k = max(0, min(BAR_N, int(round(share * BAR_N))))
    return "▓" * k + "░" * (BAR_N - k)


def render(now=None):
    ev = next_meeting(_get(SERIES), now)
    if not ev:
        return None
    rs = rows(ev)
    if not rs:
        return None
    total = sum(p for _o, _n, p, _c in rs)   # 합이 100 이 아닐 수 있어 정규화
    month = (ev.get("title") or "").replace("Fed Decision in", "").strip(" ?")
    body = ["%s %.1f%% %s" % (bar(p, total), p, name) for _o, name, p, _c in rs]
    moves = ["%s %s%+.1f%%p" % (name, UP if c > 0 else DOWN, c)
             for _o, name, _p, c in rs if c and abs(c) >= 0.1]
    lines = ["#FOMC 🏦 폴리마켓", "",
             "[%s 회의 금리 결정]" % MONTH_KO.get(month, month)] + body
    if moves:
        lines.append("24h " + " · ".join(moves))
    lines += ["", "거래량 %s" % money(ev.get("volume")),
              "기준 %s KST" % datetime.now(KST).strftime("%m-%d %H:%M")]
    return "\n".join(lines)


def main():
    msg = render()
    if msg is None:
        log("FATAL: 폴리마켓 FOMC 확률 없음")
        return 1
    log("발송: 폴리마켓 FOMC")
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
