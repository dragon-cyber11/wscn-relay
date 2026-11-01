#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""폴리마켓 미국 중간선거(하원·상원) 확률 -> 텔레그램. 한시 운영.

DEADLINE 이 지나면 아무것도 보내지 않고 조용히 끝난다(워크플로를 지우기
전까지 크론이 계속 깨워도 무해). 전송은 fng.py 의 tg_send 를 그대로 쓴다
(응답 타임아웃 중복 방지 포함).
"""

import json, sys, time
import urllib.error, urllib.request
from datetime import datetime

from fng import FORCE, KST, TRIES, BACKOFF, UA, log, tg_send

# 이 시각까지만 보낸다. 마지막 정규 슬롯(11/3 00:00 KST)이 크론 지연으로
# 조금 늦게 떠도 나가도록 1시간 여유를 둔다.
DEADLINE = datetime(2026, 11, 3, 1, 0, tzinfo=KST)

GAMMA = "https://gamma-api.polymarket.com/events?slug=%s"
# (표시명, 이벤트 slug) — 상원 먼저
RACES = [
    ("상원", "which-party-will-win-the-senate-in-2026"),
    ("하원", "which-party-will-win-the-house-in-2026"),
]
UP, DOWN = "📈", "📉"
BAR_N = 15


def fetch(slug):
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(GAMMA % slug, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d:
                return d[0]
            last = "빈 응답"
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRIES:
            time.sleep(BACKOFF[att - 1])
    log("%s 조회 실패 (%s)" % (slug, last))
    return None


def money(v):
    v = float(v or 0)
    return "$%.1fM" % (v / 1e6) if v >= 1e6 else "$%.0fK" % (v / 1e3)


def odds(ev):
    """{'D': (확률%, 24h변화%p|None), 'R': ...}. 활성 마켓의 'Yes' 가격 = 승리 확률."""
    key = {"Democratic Party": "D", "Republican Party": "R"}
    out = {}
    for m in ev.get("markets") or []:
        k = key.get(m.get("groupItemTitle"))
        if not k or not m.get("active") or m.get("closed"):
            continue
        try:
            p = float(json.loads(m["outcomePrices"])[0]) * 100.0
        except Exception:
            continue
        ch = m.get("oneDayPriceChange")
        out[k] = (p, round(float(ch) * 100.0, 1) if ch else None)
    return out


def gauge(d, r, n=BAR_N):
    """
    민주 쪽이 채워지는 게이지. 공백 정렬은 가변폭 글꼴에서 깨지므로 쓰지
    않고, 폭이 같은 블록 문자만 이어 붙인다(공탐지수와 같은 방식).
    두 정당 가격 합이 100 이 아닐 수 있어(스프레드) 비율로 정규화한다.
    """
    share = d / (d + r) if d + r else 0.5
    k = max(0, min(n, int(round(share * n))))
    return "▓" * k + "░" * (n - k)


def race_block(label, o):
    d, dch = o.get("D", (0.0, None))
    r, rch = o.get("R", (0.0, None))
    lines = ["[%s]" % label,
             "민주 %.1f%% %s %.1f%% 공화" % (d, gauge(d, r), r)]
    moves = []
    for name, ch in (("민주", dch), ("공화", rch)):
        if ch:
            moves.append("%s %s%+.1f%%p" % (name, UP if ch > 0 else DOWN, ch))
    if moves:
        lines.append("24h " + " · ".join(moves))
    return lines


def render():
    now = datetime.now(KST)
    body, vols = [], []
    for label, slug in RACES:
        ev = fetch(slug)
        if not ev:
            continue
        o = odds(ev)
        if "D" not in o and "R" not in o:
            continue
        body += race_block(label, o) + [""]
        vols.append("%s %s" % (label, money(ev.get("volume"))))
    if not body:
        return None
    return "\n".join(
        ["#중간선거 🗳 폴리마켓", ""] + body + [
            "거래량 " + " · ".join(vols),
            "기준 %s KST · 선거 11/3(현지)" % now.strftime("%m-%d %H:%M"),
        ])


def main():
    if datetime.now(KST) > DEADLINE and not FORCE:
        log("운영 기간 종료 (%s 까지) -> 건너뜀" % DEADLINE.strftime("%m-%d %H:%M KST"))
        return 0
    msg = render()
    if msg is None:
        log("FATAL: 폴리마켓 조회 전부 실패")
        return 1
    log("발송: 폴리마켓 중간선거")
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
