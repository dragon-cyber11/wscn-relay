#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CNN 공포·탐욕 지수 -> 텔레그램. 미국장 장중/마감 하루 2회.

릴레이(wscn_relay.py)와 완전히 독립이다. 여기서 실패해도 속보 중계에는
영향이 없고, 반대도 마찬가지다.
"""

import html, json, os, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("FNG_DRY_RUN", "0") == "1"
# 수동 실행(workflow_dispatch)은 시간대와 무관하게 무조건 보낸다.
FORCE = os.environ.get("FNG_FORCE", "0") == "1"

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
TRIES = 3
BACKOFF = (1.0, 3.0)

# 뉴욕 현지시각 기준 발송 시점. 서머타임은 zoneinfo 가 알아서 처리하므로
# cron 을 계절마다 고칠 필요가 없다.
#   장중 13:00 ET — 개장 3시간 30분 뒤, 오전 변동성이 가라앉은 시점
#   마감 17:30 ET — 마감 1시간 30분 뒤, CNN 종가 반영이 끝난 뒤
PHASES = ((13, 0, "미국장 장중"), (17, 30, "미국장 마감"))
# cron 지연을 흡수하는 창. 두 후보 cron 이 1시간 간격이라 55분이면
# 계절에 관계없이 정확히 하나만 창 안에 들어온다.
WINDOW_MIN = 55

KO = {"extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
      "greed": "탐욕", "extreme greed": "극단적 탐욕"}
EMO = {"extreme fear": "😱", "fear": "😨", "neutral": "😐",
       "greed": "🤑", "extreme greed": "🔥"}


# 커스텀(프리미엄) 이모지. {일반 이모지: custom_emoji_id} 로 적으면
# 그 문자를 <tg-emoji> 로 감싸 보낸다. 비어 있으면 일반 이모지로 나간다.
# 여기 키는 market.py 의 UP/DOWN/FLAT 글자와 반드시 같아야 치환된다.
#
# ID 는 봇 토큰으로 확인한다(팩 이름은 t.me/addemoji/<이름> 의 <이름>).
# 저장소 Actions 의 emoji-ids 워크플로를 돌리면 아래 형식으로 찍힌다:
#   curl -s "https://api.telegram.org/bot<TOKEN>/getStickerSet?name=NewsEmoji" \
#     | python3 -m json.tool | grep -E '"emoji"|custom_emoji_id'
#
# 주의: 커스텀 이모지는 봇이 Fragment 에서 유저네임을 샀거나, 봇 소유자가
# 프리미엄이고 private/group/supergroup 으로 직접 보낼 때만 쓸 수 있다
# (공식 문서). 허용 목록에 채널이 없어 될지는 보내봐야 안다. 실패하면
# 일반 이모지로 자동 재시도하므로, 안 되면 그냥 평범한 🔼🔽 로 나간다.
CUSTOM_EMOJI = {
    "🔼": "5449683594425410231",   # 상승
    "🔽": "5447183459602669338",   # 하락
    "➡️": "5416117059207572332",   # 보합
}


def to_html(text):
    """평문을 HTML 모드용으로 바꾸고, 설정된 이모지만 커스텀으로 치환한다."""
    out = html.escape(text)
    for ch, eid in CUSTOM_EMOJI.items():
        esc = html.escape(ch)
        out = out.replace(
            esc, '<tg-emoji emoji-id="%s">%s</tg-emoji>' % (eid, esc))
    return out


def log(m):
    print("[%s] %s" % (datetime.now(KST).strftime("%H:%M:%S"), m), flush=True)


def phase(now=None):
    """
    지금이 어느 발송 시점인지 뉴욕 현지시각으로 판정한다.
    해당 없으면 None — 반대 계절용 cron 이 깨운 경우다.

    워크플로는 서머타임/표준시 후보 시각 양쪽에 cron 을 걸어두고,
    실제로 보낼지는 여기서 정한다. 목표 시각 이후 WINDOW_MIN 분까지만
    받아들이므로 cron 이 조금 늦게 떠도 발송되고, 1시간 어긋난 반대
    계절 cron 은 창 밖이라 조용히 건너뛴다.
    """
    et = (now or datetime.now(timezone.utc)).astimezone(NY)
    for h, m, label in PHASES:
        target = et.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (et - target).total_seconds() / 60.0
        if 0 <= delta < WINDOW_MIN:
            return label, et
    return None, et


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


BAR_N = 20


def bar(v, n=BAR_N):
    """
    공백으로 위치를 맞추지 않는다. 텔레그램 기본 글꼴은 가변폭이고
    글자 크기도 사용자마다 달라서, 공백 정렬은 보는 사람에 따라 깨진다.
    같은 폭이 보장되는 블록 문자만 이어 붙여 채움 비율로 위치를 나타낸다.
    """
    p = max(1, min(n, int(round(v / 100.0 * n))))
    return "▓" * p + "░" * (n - p)


def render(j, label="현재"):
    fg = j["fear_and_greed"]
    s = float(fg["score"])
    rating = fg["rating"]
    ts = datetime.fromisoformat(fg["timestamp"]).astimezone(KST)
    age = (datetime.now(KST) - ts).total_seconds() / 3600.0

    def cmp(name, v):
        d = s - float(v)
        mark = "▲%.1f" % d if d > 0 else "▼%.1f" % abs(d) if d < 0 else "―"
        # 라벨은 전부 한글 3음절로 폭이 같다(아래 호출부 참고). 한글 음절은
        # 어떤 글꼴에서도 폭이 균일하므로 뒤따르는 숫자가 같은 위치에서
        # 시작한다. 공백 패딩은 쓰지 않는다.
        return "%s %.0f  %s" % (name, float(v), mark)

    return "\n".join([
        "%s CNN 공포·탐욕 지수 — %s" % (EMO.get(rating, "📊"), label),
        "",
        "%.0f · %s" % (s, KO.get(rating, rating)),
        # 라벨을 막대와 같은 줄에 둔다. 별도 눈금 줄을 쓰면 가변폭에서
        # 숫자와 막대가 어긋난다.
        "공포 %s 탐욕" % bar(s),
        "",
        # 라벨 폭을 맞추려고 전일/1주전 대신 하루전/일주전을 쓴다.
        # 넷 다 한글 3음절이라 렌더링 폭이 동일하다.
        cmp("하루전", fg["previous_close"]),
        cmp("일주전", fg["previous_1_week"]),
        cmp("한달전", fg["previous_1_month"]),
        cmp("일년전", fg["previous_1_year"]),
        "",
        # CNN 은 미국 장중에만 갱신한다. 휴장이면 값이 그대로이므로
        # 기준 시각을 항상 붙여서 신선도를 눈으로 확인할 수 있게 한다.
        "기준 %s KST (%.0f시간 전)" % (ts.strftime("%m-%d %H:%M"), age),
    ])


def tg_send(text):
    if DRY_RUN:
        log("DRY_RUN — 전송 생략")
        print("-" * 46)
        print(to_html(text) if CUSTOM_EMOJI else text)
        print("-" * 46)
        return True
    if CUSTOM_EMOJI:
        if _post(to_html(text), "HTML"):
            return True
        # 커스텀 이모지는 봇/채팅 종류에 따라 거부된다. 메시지를 통째로
        # 잃는 것보다 일반 이모지로라도 보내는 편이 낫다.
        log("커스텀 이모지 발송 실패 -> 일반 이모지로 재시도")
    return _post(text, None)


def _post(text, parse_mode):
    payload = {"chat_id": CHAT_ID, "text": text,
               "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    url = "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN
    body = urllib.parse.urlencode(payload).encode()
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
    label, et = phase()
    if label is None:
        if not FORCE:
            # 반대 계절용 cron 이 깨운 것이다. 실패가 아니므로 0 으로 끝낸다.
            log("발송 시점 아님 (뉴욕 %s) -> 건너뜀" % et.strftime("%m-%d %H:%M %Z"))
            return 0
        label = "현재"
    try:
        j = fetch()
    except Exception as e:
        log("FATAL: %s" % e)
        return 1
    msg = render(j, label)
    log("발송: %s (뉴욕 %s) / %s"
        % (label, et.strftime("%H:%M %Z"), msg.split("\n")[2].strip()))
    return 0 if tg_send(msg) else 1


if __name__ == "__main__":
    sys.exit(main())
