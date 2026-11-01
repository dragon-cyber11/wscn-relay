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
# (표시명, 이벤트 slug)
RACES = [
    ("하원", "which-party-will-win-the-house-in-2026"),
    ("상원", "which-party-will-win-the-senate-in-2026"),
]
PARTY = {"Democratic Party": "민주당", "Republican Party": "공화당"}
UP, DOWN = "📈", "📉"


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


def race_lines(ev):
    """활성 마켓 중 민주·공화만, 'Yes' 가격 = 승리 확률."""
    out = []
    for m in ev.get("markets") or []:
        name = PARTY.get(m.get("groupItemTitle"))
        if not name or not m.get("active") or m.get("closed"):
            continue
        try:
            p = float(json.loads(m["outcomePrices"])[0]) * 100.0
        except Exception:
            continue
        line = "%s %.1f%%" % (name, p)
        ch = m.get("oneDayPriceChange")
        if ch:
            d = round(float(ch) * 100.0, 1)
            if d:
                line += "  %s%+.1f%%p" % (UP if d > 0 else DOWN, d)
        out.append((p, line))
    out.sort(reverse=True)            # 확률 높은 쪽 먼저
    return [l for _, l in out]


def render():
    now = datetime.now(KST)
    body, vols = [], []
    for label, slug in RACES:
        ev = fetch(slug)
        if not ev:
            continue
        lines = race_lines(ev)
        if not lines:
            continue
        body += ["[%s]" % label] + lines + [""]
        vols.append("%s %s" % (label, money(ev.get("volume"))))
    if not body:
        return None
    while body and body[-1] == "":
        body.pop()
    return "\n".join(
        ["🗳 폴리마켓 미국 중간선거 확률", ""] + body + [
            "",
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
