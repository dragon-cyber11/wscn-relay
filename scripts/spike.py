#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""급변동 알림: 주요 선물·환율이 15분 사이 극단적으로 움직이면 숫자만 보낸다.

wscn 릴레이 루프가 매 주기 tick() 을 부른다. 종목 하나씩 차례로(SPACING 초
간격) 야후 1분봉을 읽으므로 각 종목은 약 1분마다 갱신되고, 한 번에 요청은
하나뿐이라 속보 중계를 거의 막지 않는다. 해석은 붙이지 않는다(방향·숫자만).

기준(TH)은 '진짜 극단값'만 잡도록 실측으로 정했다: 2026-09~10 60일치 5분봉에서
15분 등락 상위 0.02% 수준 — 이 기준과 90분 쿨다운으로 60일간 전체 4회
(ES·NQ 09-14, CL 09-18, GC 09-04). 엔·원은 60일간 0회(개입급 변동만 걸림).
"""

import json, os, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

from fng import KST, UA, log, tg_send

# (야후 티커, 표시명, 15분 등락 기준 %, 소수 자릿수)
SYMBOLS = [
    ("ES=F",  "S&P500 선물",   1.0, 2),
    ("NQ=F",  "나스닥100 선물", 1.3, 2),
    ("CL=F",  "WTI",           3.5, 2),
    ("GC=F",  "금",            1.5, 1),
    ("JPY=X", "엔/달러",        0.6, 2),
    ("KRW=X", "원/달러",        0.7, 1),
]
WINDOW = 15 * 60      # 비교 구간(초)
SLACK = 90            # 기준 봉을 찾을 때 허용 오차(초). 장 재개 갭은 이걸로 걸러진다.
STALE = 180           # 마지막 체결이 이보다 오래되면(휴장) 판정하지 않는다
COOLDOWN = 90 * 60    # 같은 종목 재알림 간격
SPACING = 10          # 종목 간 조회 간격(초) -> 종목당 약 60초마다
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d"
STATE = os.path.join(".state", "spike_last.json")

_i = 0
_next = 0.0
_last = None


def _load():
    global _last
    if _last is None:
        try:
            _last = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            _last = {}
    return _last


def _save():
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_last, f)
        os.replace(tmp, STATE)
    except Exception as e:
        log("급변동 상태 저장 실패: %s" % e)


def bars(sym):
    """[(epoch, 종가)] 1분봉. 실패하면 []."""
    req = urllib.request.Request(CHART % urllib.parse.quote(sym),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    res = d["chart"]["result"][0]
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    return [(t, c) for t, c in zip(res.get("timestamp") or [], closes) if c]


def check(pts, th, now=None):
    """
    (등락%, 기준가, 현재가) 또는 None.

    오류 틱 하나에 울리지 않도록 마지막 두 봉이 모두 같은 방향으로 기준을
    넘어야 한다(실제 급변이면 1분 늦게 울릴 뿐이다).
    """
    now = now or time.time()
    if len(pts) < 3 or now - pts[-1][0] > STALE:
        return None
    t_ref = pts[-1][0] - WINDOW
    ref = None
    for t, c in reversed(pts[:-2]):
        if t <= t_ref:
            if t_ref - t <= SLACK:
                ref = c
            break
    if not ref:
        return None
    a = (pts[-1][1] / ref - 1) * 100.0
    b = (pts[-2][1] / ref - 1) * 100.0
    if abs(a) >= th and abs(b) >= th and (a > 0) == (b > 0):
        return a, ref, pts[-1][1]
    return None


def render(name, ch, ref, now_p, nd):
    mark = "📈" if ch > 0 else "📉"
    f = lambda v: format(round(v, nd), ",.%df" % nd)
    return "\n".join([
        "#급변동 ⚡ %s" % name,
        "15분 %s%+.2f%% (%s → %s)" % (mark, ch, f(ref), f(now_p)),
        "기준 %s KST" % datetime.now(KST).strftime("%H:%M"),
    ])


def tick():
    """릴레이 루프가 매 주기 부른다. 차례가 아니면 즉시 돌아간다."""
    global _i, _next
    now = time.time()
    if now < _next:
        return
    _next = now + SPACING
    sym, name, th, nd = SYMBOLS[_i % len(SYMBOLS)]
    _i += 1
    last = _load()
    if now - float(last.get(sym, 0)) < COOLDOWN:
        return
    try:
        hit = check(bars(sym), th, now)
    except Exception as e:
        log("급변동 %s 조회 실패(무시): %s" % (sym, str(e)[:60]))
        return
    if not hit:
        return
    ch, ref, p = hit
    last[sym] = now        # 전송 결과와 무관하게 쿨다운(실패 시 매분 재시도 방지)
    _save()
    log("급변동 발송: %s %+.2f%%" % (name, ch))
    tg_send(render(name, ch, ref, p, nd))
