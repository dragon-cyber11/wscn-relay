#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""미국장 마감 시황 -> 텔레그램. 주요 지수·원자재·금리·환율 한 장.

발송 시점 판정(서머타임 포함)과 텔레그램 전송은 fng.py 것을 그대로 쓴다.
같은 워크플로에서 fng.py 다음에 실행되며, 마감 슬롯에서만 보낸다.
"""

import json, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

from fng import FORCE, KST, TRIES, BACKOFF, UA, log, phase, tg_send

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=5d"

# (야후 티커, 표시명, 소수 자릿수)
GROUPS = [
    ("지수", [
        ("^GSPC", "S&P500", 2), ("^IXIC", "나스닥", 2), ("^DJI", "다우", 2),
        ("^RUT", "러셀2000", 2), ("^VIX", "VIX", 2),
    ]),
    ("원자재", [
        ("CL=F", "WTI", 2), ("BZ=F", "브렌트", 2), ("GC=F", "금", 2),
        ("SI=F", "은", 2), ("HG=F", "구리", 3), ("NG=F", "천연가스", 3),
    ]),
    ("환율", [
        ("DX-Y.NYB", "달러지수", 2), ("KRW=X", "원/달러", 2),
        ("JPY=X", "엔/달러", 2), ("EURUSD=X", "유로/달러", 4),
    ]),
]
# 수익률은 등락률(%)이 아니라 bp 변화가 의미 있으므로 따로 다룬다.
YIELDS = [("^TNX", "미10년물", 2), ("^FVX", "미5년물", 2)]


def quote(sym):
    """현재가와 전일 종가. 실패하면 None (그 항목만 빠지고 나머지는 나간다)."""
    url = CHART % urllib.parse.quote(sym)
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            m = d["chart"]["result"][0]["meta"]
            p = m.get("regularMarketPrice")
            pc = m.get("chartPreviousClose") or m.get("previousClose")
            if p is None or not pc:
                last = "필드 없음"
            else:
                return float(p), float(pc)
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRIES:
            time.sleep(BACKOFF[att - 1])
    log("%s 조회 실패 (%s)" % (sym, last))
    return None


# 등락 표시. 다른 조합으로 바꾸려면 이 세 줄만 고치면 된다.
#   🔴/🔵 한국식(상승 빨강·하락 파랑) · 🟢/🔴 미국식 · ⬆️/⬇️ 화살표
UP, DOWN, FLAT = "🔺", "🔻", "▪️"


def num(v, nd):
    return format(round(v, nd), ",.%df" % nd)


def line(name, v, pc, nd):
    d = (v - pc) / pc * 100.0 if pc else 0.0
    mark = UP if d > 0 else DOWN if d < 0 else FLAT
    return "%s %s  %s%.2f%%" % (name, num(v, nd), mark, abs(d))


def yline(name, v, pc, nd):
    bp = (v - pc) * 100.0
    mark = UP if bp > 0 else DOWN if bp < 0 else FLAT
    return "%s %s%%  %s%.1fbp" % (name, num(v, nd), mark, abs(bp))


def render():
    out, got, miss = [], 0, 0
    for title, rows in GROUPS:
        body = []
        for sym, name, nd in rows:
            q = quote(sym)
            if q is None:
                miss += 1
                continue
            body.append(line(name, q[0], q[1], nd))
            got += 1
            time.sleep(0.2)
        if body:
            out.append("[%s]" % title)
            out += body
            out.append("")
    body = []
    for sym, name, nd in YIELDS:
        q = quote(sym)
        if q is None:
            miss += 1
            continue
        body.append(yline(name, q[0], q[1], nd))
        got += 1
        time.sleep(0.2)
    if body:
        out.append("[금리]")
        out += body
        out.append("")

    if not got:
        return None, got, miss
    head = "📊 미국장 마감 시황 — %s" % datetime.now(KST).strftime("%m-%d")
    tail = "기준 %s KST" % datetime.now(KST).strftime("%H:%M")
    if miss:
        tail += " · %d개 항목 조회 실패" % miss
    return "\n".join([head, ""] + out + [tail]), got, miss


def main():
    label, et = phase()
    # 마감 슬롯에서만 보낸다. 장중에는 CNN 지수만 나간다.
    if label != "미국장 마감" and not FORCE:
        log("마감 시점 아님 (뉴욕 %s) -> 건너뜀" % et.strftime("%m-%d %H:%M %Z"))
        return 0
    msg, got, miss = render()
    if msg is None:
        log("FATAL: 전 종목 조회 실패")
        return 1
    log("발송: 시황 %d종목 (실패 %d)" % (got, miss))
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
