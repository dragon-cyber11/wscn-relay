#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""지역별 마감 시황 -> 텔레그램. 아시아·유럽·미국을 각 장 마감 뒤 따로 보낸다.

각 지역 장이 다 끝난 시각에 그 지역 주요 지수를 정리한다. 원자재·환율·금리는
미국 마감 메시지에만 붙인다. 발송 시각 판정(서머타임 포함)은 아래 REGIONS 와
region_phase() 가, 텔레그램 전송은 fng.py 것을 그대로 쓴다.
"""

import csv, io, json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fng import FORCE, KST, TRIES, BACKOFF, UA, log, tg_send

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=5d"

# 지역별 주요 지수 (야후 티커, 표시명, 소수 자릿수)
IDX = {
    "asia": [
        ("000001.SS", "상하이", 2), ("^TWII", "대만가권", 2),
        ("^N225", "닛케이", 2), ("^KS11", "코스피", 2), ("^HSI", "항셍", 2),
    ],
    "europe": [
        ("^GDAXI", "독일DAX", 2), ("^STOXX50E", "유로스톡스50", 2),
        ("^FTSE", "영국FTSE", 2), ("^FCHI", "프랑스CAC", 2),
        ("^IBEX", "스페인IBEX", 2),
    ],
    "us": [
        ("^GSPC", "S&P500", 2), ("^IXIC", "나스닥", 2), ("^DJI", "다우", 2),
        ("^RUT", "러셀2000", 2), ("^VIX", "VIX", 2),
    ],
}

# 원자재·환율은 미국 마감 메시지에만 붙는다(금리는 재무부 CSV 로 따로 처리).
US_EXTRA = [
    ("원자재", [
        ("CL=F", "WTI", 2), ("BZ=F", "브렌트", 2), ("GC=F", "금", 2),
        ("SI=F", "은", 2), ("HG=F", "구리", 3), ("NG=F", "천연가스", 3),
    ]),
    ("환율", [
        ("DX-Y.NYB", "달러지수", 2), ("KRW=X", "원/달러", 2),
        ("JPY=X", "엔/달러", 2), ("EURUSD=X", "유로/달러", 4),
    ]),
]

# 지역별 발송 시점: 키 -> (표준시간대, 시, 분, 표시명). 각 지역 '장이 다 끝난
# 뒤'를 노린다. GitHub cron 은 UTC 고정이라 서머타임을 못 따라가므로,
# 워크플로엔 계절별 후보 cron 을 걸어두고 실제 발송 여부는 region_phase()
# 가 현지시각으로 판정한다.
#   아시아: 홍콩 16:00(HKT) 마감 뒤 = 서울 17:15. 한·일·중·대·홍 DST 없음(후보 1개).
#   유럽:   프랑크푸르트/파리 17:30(CET/CEST) 마감 뒤 17:45 현지.
#   미국:   16:00(ET) 마감 뒤, 종가 반영이 끝난 17:30 현지.
REGIONS = {
    "asia":   ("Asia/Seoul",       17, 15, "아시아"),
    "europe": ("Europe/Berlin",    17, 45, "유럽"),
    "us":     ("America/New_York",  17, 30, "미국"),
}
# cron 지연을 흡수하는 창. 각 지역 발송창이 서로 겹치지 않을 만큼만.
WINDOW_MIN = 55

# 국채 수익률은 미 재무부 공식 일일 곡선을 쓴다.
# 야후에는 2년물 현물 지수가 없고, 대체 후보인 2YY=F 는 유동성이 없어
# 며칠씩 같은 값에 머무른다(실측 08-07~08-13 내내 4.170 고정).
# 재무부 CSV 는 2/10/30년을 모두 담고 미국 동부시간 오후에 갱신되므로
# 미국 마감 발송 시각(ET 17:30)과도 맞는다.
TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/daily-treasury-rates.csv/%d/all"
            "?type=daily_treasury_yield_curve&field_tdr_date_value=%d"
            "&page&_format=csv")
YIELD_COLS = [("2 Yr", "미2년물"), ("10 Yr", "미10년물"), ("30 Yr", "미30년물")]


def region_phase(now=None):
    """
    지금이 어느 지역 마감 발송창인지 현지시각으로 판정한다.
    해당 없으면 (None, ...) — 반대 계절용 cron 이 깨운 경우다.

    각 지역의 목표 시각 이후 WINDOW_MIN 분까지만 받아들이므로 cron 이 조금
    늦게 떠도 발송되고, 1시간 어긋난 반대 계절 cron 은 창 밖이라 조용히 넘어간다.
    """
    utc = now or datetime.now(timezone.utc)
    for key, (zone, h, m, _label) in REGIONS.items():
        loc = utc.astimezone(ZoneInfo(zone))
        target = loc.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (loc - target).total_seconds() / 60.0
        if 0 <= delta < WINDOW_MIN:
            return key, loc
    return None, utc.astimezone(KST)


def prev_close(p, closes):
    """
    일간 등락용 '직전 세션 종가'를 일봉 종가 배열에서 고른다.

    meta.chartPreviousClose 는 range(5d) 시작 이전 종가(≈5~6일 전)라
    하루 등락 계산에는 못 쓴다. 배열의 마지막이 현재가와 가장 가까우면 그게
    현재 세션 종가이므로 직전은 [-2], 장중이라 마지막이 어제 종가면 그게 곧
    직전 종가이므로 [-1] 을 쓴다.
    """
    if len(closes) < 2:
        return None
    if p is not None and abs(closes[-1] - p) <= abs(closes[-2] - p):
        return closes[-2]
    return closes[-1]


def quote(sym):
    """(현재가, 직전 세션 종가, 마지막 체결 epoch). 실패하면 None.

    세 번째 값은 휴장 판정에 쓴다(오늘 거래가 있었는지). 없으면 None.
    """
    url = CHART % urllib.parse.quote(sym)
    last = "unknown"
    for att in range(1, TRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            res = d["chart"]["result"][0]
            m = res["meta"]
            p = m.get("regularMarketPrice")
            ts = m.get("regularMarketTime")
            closes = [c for c in
                      ((res.get("indicators", {}).get("quote") or [{}])[0]
                       .get("close") or [])
                      if c is not None]
            if p is None and closes:
                p = closes[-1]
            pc = prev_close(p, closes)
            if p is None or not pc:
                last = "필드 없음"
            else:
                return float(p), float(pc), ts
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


# 등락 표시. 바꾸려면 이 줄만 고치면 된다.
# 커스텀(프리미엄) 이모지를 쓰려면 여기 글자를 fng.py 의 CUSTOM_EMOJI
# 키와 똑같이 맞춰야 <tg-emoji> 로 치환된다. 지금은 팩에 있는 📈📉➡️.
UP, DOWN, FLAT = "📈", "📉", "➡️"


def treasury():
    """
    최근 두 영업일의 국채 수익률 행을 (최신, 직전) 순서로 돌려준다.
    파일이 연도별이라 연초에는 전년도 것까지 봐야 두 행이 채워진다.
    """
    rows = []
    year = datetime.now(KST).year
    for y in (year, year - 1):
        url = TREASURY % (y, y)
        for att in range(1, TRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=25) as r:
                    txt = r.read().decode("utf-8", "replace")
                rows += list(csv.DictReader(io.StringIO(txt)))
                break
            except Exception as e:
                if att == TRIES:
                    log("재무부 %d년 조회 실패 (%s)" % (y, str(e)[:40]))
                else:
                    time.sleep(BACKOFF[att - 1])
        if len(rows) >= 2:
            break
    return rows[:2]


def num(v, nd):
    return format(round(v, nd), ",.%df" % nd)


def signed(d, nd, unit):
    """0 에는 부호를 붙이지 않는다. +0.00% 는 어색하다."""
    return ("%+.*f%s" if d else "%.*f%s") % (nd, d, unit)


def line(name, v, pc, nd):
    # 화살표로 방향이 보이지만 부호도 같이 적는다. 숫자만 눈으로 훑을 때
    # 부호가 없으면 상승·하락이 구분되지 않는다.
    # 표시할 자리수로 먼저 반올림해야 화살표와 숫자가 어긋나지 않는다
    # (예: +0.004% 를 그대로 두면 📈0.00% 가 된다).
    d = round((v - pc) / pc * 100.0, 2) if pc else 0.0
    mark = UP if d > 0 else DOWN if d < 0 else FLAT
    return "%s %s  %s%s" % (name, num(v, nd), mark, signed(d, 2, "%"))


def yline(name, v, pc):
    """수익률은 등락률(%)이 아니라 bp 변화가 의미 있다."""
    bp = round((v - pc) * 100.0, 1)
    mark = UP if bp > 0 else DOWN if bp < 0 else FLAT
    return "%s %.2f%%  %s%s" % (name, v, mark, signed(bp, 1, "bp"))


def _today_in(ts, zone):
    """야후 마지막 체결 epoch 이 zone 기준 오늘이면 True(휴장 판정용)."""
    if not ts:
        return False
    z = ZoneInfo(zone)
    return datetime.fromtimestamp(ts, z).date() == datetime.now(z).date()


def _fill(rows):
    """티커 목록을 조회해 (표시줄들, 성공수, 실패수) 로 돌려준다."""
    body, got, miss = [], 0, 0
    for sym, name, nd in rows:
        q = quote(sym)
        if q is None:
            miss += 1
            continue
        body.append(line(name, q[0], q[1], nd))
        got += 1
        time.sleep(0.2)
    return body, got, miss


def _fill_idx(rows, zone):
    """지수 채우기. (표시줄, 성공, 실패, 오늘거래수) — 마지막은 휴장 판정용."""
    body, got, miss, traded = [], 0, 0, 0
    for sym, name, nd in rows:
        q = quote(sym)
        if q is None:
            miss += 1
            continue
        p, pc, ts = q
        body.append(line(name, p, pc, nd))
        got += 1
        if _today_in(ts, zone):
            traded += 1
        time.sleep(0.2)
    return body, got, miss, traded


def render(region):
    """
    지역 마감 메시지를 (msg, 성공, 실패, 사유) 로 돌려준다.
    아시아·유럽은 지수만, 미국은 원자재·환율·금리까지.
    사유: ok / empty(전부 조회 실패) / holiday(오늘 거래 없음).
    """
    zone, _h, _m, label = REGIONS[region]

    # 지수를 먼저 채우면서 '오늘 거래된' 종목 수를 센다.
    idx_body, got, miss, traded = _fill_idx(IDX[region], zone)
    if not idx_body:
        return None, got, miss, "empty"
    # 지수를 다 불렀는데 이 지역에서 오늘 아무도 거래 안 했으면 휴장이다.
    # 메시지는 그대로 만들되 사유로 표시만 한다 — 정규 발송은 main 에서
    # 건너뛰고, 수동 강제 발송은 (전날 종가라도) 보낸다.
    reason = "holiday" if traded == 0 else "ok"

    out = []
    if region == "us":
        # 미국: 지수 + 원자재·환율 + 금리. 구획이 여럿이라 소제목을 붙인다.
        out += ["[지수]"] + idx_body + [""]
        for title, rows in US_EXTRA:
            body, g, m = _fill(rows)
            got += g
            miss += m
            if body:
                out += ["[%s]" % title] + body + [""]
        tr = treasury()
        body = []
        if len(tr) >= 2:
            for col, name in YIELD_COLS:
                try:
                    v, pc = float(tr[0][col]), float(tr[1][col])
                except (KeyError, TypeError, ValueError):
                    miss += 1
                    continue
                body.append(yline(name, v, pc))
                got += 1
        else:
            miss += len(YIELD_COLS)
        if body:
            out += ["[금리]"] + body + [""]
    else:
        # 아시아·유럽: 지수 한 덩어리라 소제목 없이 나열한다.
        out += idx_body + [""]

    head = "📊 %s 마감 시황 — %s" % (label, datetime.now(KST).strftime("%m-%d"))
    tail = "기준 %s KST" % datetime.now(KST).strftime("%H:%M")
    if miss:
        tail += " · %d개 항목 조회 실패" % miss
    # 기준 시각 줄은 바로 위 블록에 붙인다(마지막 빈 줄만 걷어낸다).
    while out and out[-1] == "":
        out.pop()
    return "\n".join([head, ""] + out + [tail]), got, miss, reason


def main():
    # 지역은 (1) 수동 지정 MARKET_REGION, (2) 현재 시각 판정 순으로 정한다.
    region = os.environ.get("MARKET_REGION", "").strip().lower()
    if region and region not in REGIONS:
        log("알 수 없는 MARKET_REGION=%r -> 무시" % region)
        region = ""
    if not region:
        region, loc = region_phase()
        if region is None:
            if not FORCE:
                # 반대 계절용 cron 이 깨운 것이다. 실패가 아니므로 0.
                log("마감 발송창 아님 (%s) -> 건너뜀"
                    % loc.strftime("%m-%d %H:%M %Z"))
                return 0
            region = "us"   # 수동 강제인데 지정이 없으면 미국으로 본다
            log("강제 발송: 지역 지정 없음 -> 미국 마감")

    label = REGIONS[region][3]
    msg, got, miss, reason = render(region)
    if msg is None:
        log("FATAL: %s 마감 전 종목 조회 실패" % label)
        return 1
    if reason == "holiday" and not FORCE:
        # 휴장일: 조용히 넘긴다(실패가 아니므로 0). 강제 발송은 그대로 보낸다.
        log("%s 휴장(오늘 거래 없음) -> 건너뜀" % label)
        return 0
    log("발송: %s 마감 %d종목 (실패 %d)%s"
        % (label, got, miss, " [휴장·강제]" if reason == "holiday" else ""))
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
