#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华尔街见闻 7x24 快讯 -> 텔레그램. 속보(lives)만 중계."""

import html, json, os, re, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HOSTS = ["https://api-one.wallstcn.com", "https://api.wallstcn.com"]
CHANNEL = os.environ.get("WSCN_CHANNEL", "global")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "600"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", "600"))
INIT_ONLY = os.environ.get("INIT_ONLY", "0") == "1"

STATE_DIR = ".state"
F_LAST_ID = STATE_DIR + "/wscn_last_id.txt"
F_HEARTBEAT = STATE_DIR + "/wscn_heartbeat.txt"
F_SENT = STATE_DIR + "/wscn_sent_titles.json"
F_BLOCK_ALERT = STATE_DIR + "/wscn_block_alert.txt"

KST = timezone(timedelta(hours=9))
CST = timezone(timedelta(hours=8))   # API가 문자열 시각을 주면 베이징 시간이다
SEND_GAP = 3.5
MAX_PER_CYCLE = 18
TITLE_MAX = 1200
BACKFILL_MAX = 200
STALL_ALERT_SEC = 300
SEND_BACKOFF = 30           # 전송 실패 후 다음 시도까지 (핫루프 방지)
MAX_TIMEOUT_RETRY = 3       # 거절 직후 타임아웃 재시도 상한 (중복 폭탄 방지)
REJECT_WINDOW = 60          # "방금 거절당했다"로 볼 시간 창
TRANSLATE_TRIES = 3         # gtx 가 간헐적으로 500 을 뱉는다 -> 재시도
TRANSLATE_BACKOFF = (0.5, 1.5)   # 시도 사이 대기 (길이 = TRANSLATE_TRIES-1)
BLOCK_ALERT_COOLDOWN = 3600  # 403 차단 경고 재발송 간격
COMMIT_TAIL_GUARD = 30      # 종료 직전이면 주기 커밋을 건너뛴다 (중복 커밋 방지)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 400 중 "설정이 틀렸다"는 것들 — 절대 뉴스를 버리면 안 되고 무한 재시도
CONFIG_400 = ("chat not found", "not a member", "not enough rights",
              "chat_id is empty", "bot was blocked", "bot was kicked",
              "chat is deactivated", "have no rights", "user is deactivated")
_last_reject = 0.0   # 마지막으로 명시적 거절을 받은 시각
_reject_count = 0    # 이번 실행에서 받은 명시적 거절 총 횟수


class Blocked(Exception):
    """모든 경로가 403 — 러너 IP가 차단됨. 잡을 끝내고 새 러너를 받아야 함."""


class RateLimited(Exception):
    """모든 경로가 429 — 잠시 쉬면 풀린다. 러너를 버릴 이유는 없음."""


HDRS = [
    {"Accept": "application/json, text/plain, */*",
     "Referer": "https://wallstreetcn.com/live/global",
     "Origin": "https://wallstreetcn.com"},
    {"Accept": "application/json, text/plain, */*",
     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
     "Referer": "https://wallstreetcn.com/"},
    {"Accept": "application/json, text/plain, */*",
     "Accept-Language": "zh-CN,zh;q=0.9",
     "Referer": "https://wallstreetcn.com/",
     "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
     "Sec-Fetch-Site": "same-site"},
]

# ── 개별종목 공시 걸러내기 ──
# 원칙: 절대 진짜 뉴스를 버리지 않는다. 애매하면 통과시킨다.
FILTER_NOISE = os.environ.get("FILTER_NOISE", "1") == "1"
# 회사 공시에서만 쓰이는 표현 -> 무조건 차단
NOISE_STRONG = re.compile(
    r"中签|回购(公司)?股份|股份回购|全资子公司|控股股东|股权激励|"
    r"限售股|解禁|质押|停牌|复牌|定向增发|配股|业绩(预告|快报)|扭亏为盈|"
    r"量化私募|私募.{0,8}(回血|净值|涨超)")
# 거시 뉴스에도 쓰일 수 있는 표현 -> "회사명：" 으로 시작할 때만 차단
NOISE_WEAK = re.compile(
    r"净利润|营业收入|营收|拟投资|拟收购|中标|订单|产能|澄清|传闻|"
    r"价格上调|上调.{0,6}价格|额度|全面升级|重置|用户数|增持|减持|减仓")
# 콜론 앞이 이것들이면 회사가 아니라 인물/기관 -> 차단 대상 아님
NOT_COMPANY = ("特朗普", "拜登", "普京", "报道", "知情人士", "消息人士",
               "分析师", "防长", "外长", "总统", "总理", "部长", "主席",
               "行长", "央行", "美联储", "白宫", "欧盟", "商务部", "外交部",
               "财政部", "国防部", "统计局", "发言人", "国务院", "日本央行",
               "欧洲央行", "传媒", "媒体")
COLON_RE = re.compile(r"^([^\s：:]{2,14})[：:]")

# ── 기업 뉴스 광역 차단 ──
# "회사가 주어인 뉴스"는 종목이 아니라 시장을 보는 용도에 불필요하다.
# 콜론 접두를 곧바로 회사로 간주하면 "伊朗官员：", "北京：", "万斯：" 같은
# 지정학·정책 뉴스가 같이 죽는다(실측 43건 중 6건 오탐). 그래서 접두만으로
# 판단하지 않고, 회사라는 적극적 증거(종목코드 / 회사 접미사)를 요구한다.
TICKER_RE = re.compile(r"[（(]\s*\d{4,6}\.(SH|SZ|BJ|HK)\s*[）)]")
# 주의: 能源·银行 등 일반 업종어가 들어 있어 기관명도 걸린다.
#   예) 美国能源信息署（EIA）：원유 생산량 전망, 中小银行 금리인상
#   운영상 감수하기로 한 오탐이다. 되살리려면 해당 단어를 빼면 된다.
CORP_SFX = re.compile(
    r"集团|股份|控股|有限公司|科技|半导体|电子|汽车|生物|制药|医药|能源|电力|"
    r"地产|置业|航空|重工|钢铁|化工|通信|传媒|证券|保险|基金|银行|资本|实业|"
    r"材料|新材|光电|智能|网络|软件|锂业|矿业|时代|电池|股价")
# 회사명 추출용. 기존 COLON_RE 보다 넓게 잡는다(따옴표·괄호 포함 사명 대응).
COLON_WIDE = re.compile(r"^([^\s：:]{2,24})[：:]")


def noise_reason(t):
    """잡음이면 (True, 사유). 아니면 (False, '')"""
    if not FILTER_NOISE:
        return False, ""
    if NOISE_STRONG.search(t):
        return True, "disclosure"
    m = COLON_RE.match(t)
    if m and not any(x in m.group(1) for x in NOT_COMPANY) \
           and NOISE_WEAK.search(t):
        return True, "company"
    if TICKER_RE.search(t):
        return True, "ticker"
    m = COLON_WIDE.match(t)
    head = m.group(1) if m else t[:14]
    if not any(x in head for x in NOT_COMPANY) and CORP_SFX.search(head):
        return True, "corp"
    return False, ""


# 🚨 판정: 제목 맨 앞의 【라벨】 이 속보성이면 사이렌
BREAKING_LABELS = ["突发", "快讯", "重磅", "独家", "紧急", "刚刚", "爆"]
LABEL_RE = re.compile(r"^\s*[【\[]\s*([^】\]]{1,12})\s*[】\]]")

# #속보 판정: 미국 물가·고용 핵심 지표 발표. WSCN 원문(중국어) 제목이
# '美国' 과 아래 지표어를 함께 담으면 지표 발표로 보고 시간 옆에 #속보 를 붙인다.
# 미국으로 한정하려 '美国' 을 함께 요구한다 — CPI·PPI·PCE 는 중국·유로존·
# 일본에도 있어 지표어만으로는 나라를 가를 수 없기 때문이다.
#   失业金  = 初请/续请/申请失业金(신규·연속 실업수당) 전부 포함
#   非农    = 비농업 고용,  失业率 = 실업률,  ADP = ADP 민간고용
KEY_INDICATOR = re.compile(r"CPI|PPI|PCE|非农|失业率|失业金|ADP")


def is_indicator(head):
    """미국 물가·고용 핵심 지표 발표면 True."""
    h = head or ""
    return "美国" in h and bool(KEY_INDICATOR.search(h))


def log(m):
    print("[%s] %s" % (datetime.now(KST).strftime("%H:%M:%S"), m), flush=True)


def http_json(path, timeout=10):
    """
    호스트 2개 x 헤더 3종을 전부 시도한다.
    5xx 같은 일시적 오류에서 폴백 호스트를 못 쓰면 안 되므로 어떤 실패든 다음
    조합으로 넘어가고, 모든 조합이 실패했을 때만 원인을 분류해서 올린다.
      전부 403 -> Blocked   (러너 IP 차단, 러너 교체 필요)
      전부 429 -> RateLimited (레이트리밋, 잠시 쉬면 됨)
    """
    saw_403 = False
    saw_429 = False
    last_err = "unknown"
    for host in HOSTS:
        for h in HDRS:
            hh = {"User-Agent": UA}
            hh.update(h)
            try:
                req = urllib.request.Request(host + path, headers=hh)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                last_err = "HTTP %d" % e.code
                if e.code == 403:
                    saw_403 = True
                elif e.code == 429:
                    saw_429 = True
                continue
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                continue
    if saw_403:
        raise Blocked("all routes 403 (runner IP blocked)")
    if saw_429:
        raise RateLimited("all routes 429 (rate limited)")
    raise RuntimeError("all routes failed (%s)" % last_err)


def read_text(p, d=""):
    try:
        return open(p, encoding="utf-8").read().strip()
    except Exception:
        return d


def write_text(p, s):
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def load_sent():
    """항목 하나가 깨졌다고 중복 방지 맵 전체를 버리지 않는다."""
    try:
        d = json.load(open(F_SENT, encoding="utf-8"))
    except Exception as e:
        log("sent 로드 실패(%s) -> 빈 맵으로 시작" % type(e).__name__)
        return {}
    if not isinstance(d, dict):
        return {}
    c = time.time() - 86400
    return {k: v for k, v in d.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > c}


def save_sent(d):
    c = time.time() - 86400
    write_text(F_SENT, json.dumps(
        {k: v for k, v in d.items() if v > c}, ensure_ascii=False))


_tr = {}
_tr_fail = 0   # 재시도를 다 쓰고도 실패해서 원문으로 나간 건수


def translate(t):
    """
    비공식 구글 번역. 실패하면 원문 반환(봇은 안 멈춤).

    gtx 는 간헐적으로 500 을 뱉는데 한 번 실패했다고 원문을 그대로 내보내면
    중국어가 그대로 나간다. 일시적 오류는 재시도하고, 요청 자체가 잘못된
    4xx 는 재시도해봐야 같은 결과이므로 즉시 포기한다.
    """
    global _tr_fail
    if not t:
        return ""
    k = t[:500]
    if k in _tr:
        return _tr[k]
    url = ("https://translate.googleapis.com/translate_a/single"
           "?client=gtx&sl=zh-CN&tl=ko&dt=t&q="
           + urllib.parse.quote(t[:4500]))
    last = "unknown"
    for att in range(1, TRANSLATE_TRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            out = "".join(s[0] for s in d[0] if s and s[0]).strip()
            if out:
                if att > 1:
                    log("translate ok (%d회째 시도)" % att)
                _tr[k] = out
                return out
            last = "empty response"
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if 400 <= e.code < 500 and e.code != 429:
                break   # 요청 자체 문제 -> 재시도 무의미
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if att < TRANSLATE_TRIES:
            time.sleep(TRANSLATE_BACKOFF[att - 1])
    _tr_fail += 1
    log("translate fail (%d회 시도, %s) -> 원문 사용" % (att, last))
    return t


def tg_send(text):
    """
    전송 판정 규칙
      200            -> 성공
      429            -> 서버가 명시적 거절, 대기 후 재시도
      400/401        -> 명시적 거절 = 확실히 미전송. 원인을 분류해서 반환
      응답 없음(타임아웃) -> 전달 여부 불명
         - 최근 REJECT_WINDOW 초 안에 명시적 거절이 없었다면:
           중복 폭탄을 피하려고 '보낸 것'으로 간주
         - 방금 거절당하던 중이었다면: 실패로 간주 (뉴스 유실 방지)
    """
    global _last_reject, _reject_count
    if not BOT_TOKEN or not CHAT_ID:
        return False, "no_credentials"
    url = "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN
    body = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text,
        "disable_web_page_preview": "true"}).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
                _last_reject = 0.0
                return True, "ok"
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                j = {}
            desc = (j.get("description") or "").lower()
            if code == 429:
                w = int(j.get("parameters", {}).get("retry_after", 5))
                log("429 -> sleep %ds" % w)
                time.sleep(min(w + 1, 60))
                continue
            _last_reject = time.time()
            _reject_count += 1
            if code == 401:
                return False, "auth_401"
            if code == 400:
                kind = "cfg" if any(x in desc for x in CONFIG_400) else "msg"
                log("400/%s: %s" % (kind, desc[:80]))
                return False, "bad400_%s" % kind
            return False, "http_%d" % code
        except Exception as e:
            if time.time() - _last_reject < REJECT_WINDOW:
                # 방금까지 거절당하던 중의 타임아웃 -> 갔을 리 없다
                log("timeout (최근 %d초 내 거절 있었음) -> 실패로 처리, 뉴스 보존"
                    % REJECT_WINDOW)
                return False, "timeout_after_reject"
            log("unknown-state (%s) -> treat as sent" % type(e).__name__)
            return True, "unknown_treated_sent"
    return False, "retry_exhausted"


def alert_once(state_file, cooldown, text):
    """같은 경고가 짧은 간격으로 반복 발송되는 것을 막는다."""
    try:
        last = float(read_text(state_file, "0") or "0")
    except ValueError:
        last = 0.0
    now = time.time()
    if now - last < cooldown:
        log("경고 억제(쿨다운 %ds 남음): %s"
            % (int(cooldown - (now - last)), text[:40]))
        return False
    write_text(state_file, str(now))
    tg_send(text)
    return True


def to_kst(dt_val):
    """실패하면 None. '봇 시각'으로 조용히 대체되지 않게 함."""
    try:
        if isinstance(dt_val, (int, float)):
            ts = float(dt_val)
        else:
            s = str(dt_val).strip()
            if re.fullmatch(r"\d+(\.\d+)?", s):
                ts = float(s)
            else:
                m = re.match(
                    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
                if not m:
                    return None
                # 문자열 시각은 UTC가 아니라 베이징 시간으로 온다
                d = datetime(*[int(x) for x in m.groups()], tzinfo=CST)
                return d.astimezone(KST).strftime("%H:%M")
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
            KST).strftime("%H:%M")
    except Exception:
        return None


def clean(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def headline(it):
    """快讯은 title이 비어있는 경우가 많음 -> content_text 첫 줄로 대체"""
    t = clean(it.get("title") or "")
    if not t:
        t = clean(it.get("highlight_title") or "")
    if not t:
        t = clean(it.get("content_text") or it.get("content") or "")
        t = t.split("\n")[0].strip()
    return t[:TITLE_MAX]


def label_of(head):
    m = LABEL_RE.match(head or "")
    return m.group(1) if m else None


def is_breaking(head, it):
    lab = label_of(head)
    if lab and any(k in lab for k in BREAKING_LABELS):
        return True
    if (it.get("highlight_title") or "").strip():
        return True
    return False


def fetch(limit=20, cursor=None):
    p = ("/apiv1/content/lives?channel=%s&accept=live&limit=%d"
         % (CHANNEL, limit))
    if cursor:
        p += "&cursor=%s" % cursor
    d = http_json(p)
    if d.get("code") != 20000:
        raise RuntimeError("api code=%s" % d.get("code"))
    dd = d.get("data") or {}
    raw = dd.get("items") or []
    items = [x for x in raw if x.get("type") == "live"]
    # 응답 스키마가 바뀌면 전부 조용히 버려지므로 눈에 보이게 남긴다
    if raw and not items:
        log("경고: 응답 %d건이 전부 type!=live (types=%s) -> 스키마 확인 필요"
            % (len(raw), sorted({str(x.get("type")) for x in raw})))
    elif len(items) < len(raw):
        log("fetch raw=%d live=%d" % (len(raw), len(items)))
    return items, dd.get("next_cursor")


def fetch_since(last_id):
    """API가 한 번에 20건만 줌 -> 마지막 id 만날 때까지 되짚기(최대 200건)"""
    out, cursor, seen = [], None, set()
    while len(out) < BACKFILL_MAX:
        items, nxt = fetch(20, cursor)
        if not items:
            break
        hit = False
        for it in items:
            i = int(it.get("id") or 0)
            if i in seen:
                continue
            seen.add(i)
            if last_id and i <= last_id:
                hit = True
                break
            out.append(it)
        if hit or not nxt or not last_id:
            break
        cursor = nxt
    out.sort(key=lambda x: int(x.get("id") or 0))
    return out


def git(*a):
    try:
        return subprocess.run(["git"] + list(a), capture_output=True,
                              text=True, timeout=60)
    except Exception as e:
        log("git error: %s" % e)
        return None


def _push():
    """타임아웃 등으로 결과를 모르면 실패로 본다(성공으로 넘기면 상태가 유실됨)."""
    p = git("push", "origin", "HEAD:main")
    if p is None:
        log("push 결과 불명(타임아웃) -> 실패로 처리")
        return False
    if p.returncode != 0:
        log("push rc=%d: %s" % (p.returncode, (p.stderr or "")[:200]))
        return False
    return True


def commit_state():
    """rebase 쓰면 충돌로 커밋이 영원히 안 올라감 -> reset --mixed 방식"""
    git("add", "-A", STATE_DIR)
    r = git("diff", "--cached", "--quiet")
    if r is not None and r.returncode == 0:
        return
    stamp = datetime.now(KST).strftime("%m-%d %H:%M")
    cid = ["-c", "user.name=wscn-relay", "-c", "user.email=relay@local"]
    git(*(cid + ["commit", "-m", "state: wscn %s" % stamp]))
    if _push():
        return
    log("push conflict -> fetch + reset --mixed")
    git("fetch", "origin", "main")
    git("reset", "--mixed", "origin/main")
    git("add", "-A", STATE_DIR)
    git(*(cid + ["commit", "-m", "state: wscn resync %s" % stamp]))
    if not _push():
        log("재동기화 후에도 push 실패 -> 다음 주기에 다시 시도")


def heartbeat(**kw):
    p = ["at=%s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")]
    p += ["%s=%s" % (k, v) for k, v in kw.items()]
    write_text(F_HEARTBEAT, " ".join(p) + "\n")


def _counts(d, empty="0"):
    return ",".join("%s:%s" % kv for kv in sorted(d.items())) or empty


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("FATAL: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
        return 1
    os.makedirs(STATE_DIR, exist_ok=True)
    last_id = int(read_text(F_LAST_ID, "0") or "0")
    sent = load_sent()
    log("start ch=%s last_id=%s run=%ds init=%s filter=%s"
        % (CHANNEL, last_id, RUN_SECONDS, INIT_ONLY, FILTER_NOISE))

    if last_id == 0 or INIT_ONLY:
        for att in range(1, 6):
            try:
                items, _ = fetch(20)
                if items:
                    newest = max(int(x.get("id") or 0) for x in items)
                    write_text(F_LAST_ID, str(newest))
                    heartbeat(mode="init", last_id=newest, send="0(init)")
                    commit_state()
                    log("INIT done last_id=%d (전송 안 함)" % newest)
                    return 0
                log("INIT empty items (%d/5)" % att)
            except Blocked as e:
                log("INIT blocked: %s -> 러너 교체 필요" % e)
                return 2
            except RateLimited as e:
                log("INIT rate limited (%d/5): %s" % (att, e))
                time.sleep(30)
                continue
            except Exception as e:
                log("INIT failed (%d/5): %s" % (att, e))
            time.sleep(3)
        return 1

    t0 = tc = tn = time.time()
    alerted = False
    total = 0
    feed_reason = "ok"      # 피드(fetch) 상태
    send_reason = "-"       # 마지막 전송 결과 (절대 feed_reason과 섞지 않는다)
    lag = -1
    labels = {}
    blocked = 0
    limited = 0
    fails = {}      # id -> 메시지 자체 문제로 거부된 횟수
    tmo = {}        # id -> 거절 직후 타임아웃 횟수
    dropped = 0
    unconfirmed = 0  # 전달 여부 불명인 채로 보낸 것으로 처리한 건수
    filt = {}       # 필터 사유별 건수

    while time.time() - t0 < RUN_SECONDS:
        cyc = time.time()
        send_failed = False
        try:
            fresh = fetch_since(last_id)
            feed_reason = "ok"
            blocked = 0
            limited = 0
        except Blocked as e:
            blocked += 1
            feed_reason = "blocked_403"
            log("blocked %d/10: %s" % (blocked, e))
            if blocked >= 10:
                # 경고를 먼저 기록해야 쿨다운 상태가 커밋에 함께 올라간다
                alert_once(F_BLOCK_ALERT, BLOCK_ALERT_COOLDOWN,
                           "⚠️ 华尔街见闻 API 403 차단 — 러너를 교체합니다")
                heartbeat(last_id=last_id, send="%d건" % total,
                          reason="blocked_403", exit="reroll_runner")
                save_sent(sent)
                commit_state()
                return 2
            time.sleep(10)
            continue
        except RateLimited as e:
            # 레이트리밋은 러너를 버릴 이유가 없다. 점점 길게 쉬었다 다시 시도.
            limited += 1
            feed_reason = "rate_limited_429"
            w = min(30 * limited, 300)
            log("rate limited %d회: %s -> %d초 대기" % (limited, e, w))
            time.sleep(w)
            continue
        except Exception as e:
            feed_reason = "fetch_error:%s" % type(e).__name__
            log("fetch error: %s" % e)
            time.sleep(min(POLL_INTERVAL * 5, 10))
            continue

        if fresh:
            tn = time.time()
            if alerted:
                tg_send("✅ 华尔街见闻 속보 피드 복구됨")
                alerted = False
            n = 0
            for it in fresh:
                if n >= MAX_PER_CYCLE:
                    break
                iid = int(it.get("id") or 0)
                head = headline(it)
                if not head:
                    last_id = max(last_id, iid)
                    write_text(F_LAST_ID, str(last_id))
                    continue
                key = head[:120]
                if key in sent:
                    last_id = max(last_id, iid)
                    write_text(F_LAST_ID, str(last_id))
                    continue

                nz, why = noise_reason(head)
                if nz:
                    filt[why] = filt.get(why, 0) + 1
                    log("[필터/%s] %s" % (why, head[:44]))
                    last_id = max(last_id, iid)
                    write_text(F_LAST_ID, str(last_id))
                    continue

                lab = label_of(head)
                if lab:
                    labels[lab] = labels.get(lab, 0) + 1

                ko = translate(head)
                hhmm = to_kst(it.get("display_time"))
                mark = "🚨 " if is_breaking(head, it) else ""
                # 미국 물가·고용 지표 발표는 시간 옆에 #속보 를 붙인다.
                tag = " #속보" if is_indicator(head) else ""

                lines = ["%s%s" % (mark, ko)]
                if ko.strip() != head.strip():
                    lines.append("(원문: %s)" % head)
                if hhmm:
                    lines.append("(%s)%s" % (hhmm, tag))
                elif tag:
                    lines.append(tag.strip())
                msg = "\n".join(lines)
                if len(msg) > 4000:
                    msg = msg[:3990] + "…"

                ok, send_reason = tg_send(msg)
                if ok:
                    if send_reason == "unknown_treated_sent":
                        # 실제 도달 여부 불명. 눈에 보이게 세어둔다.
                        unconfirmed += 1
                    sent[key] = time.time()
                    total += 1
                    n += 1
                    last_id = max(last_id, iid)
                    write_text(F_LAST_ID, str(last_id))
                else:
                    if send_reason == "bad400_msg":
                        # 이 메시지 하나만의 문제 -> 5회 후 이 건만 버림
                        fails[iid] = fails.get(iid, 0) + 1
                        if fails[iid] >= 5:
                            log("id=%d 메시지 거부 5회 -> 이 건만 건너뜀" % iid)
                            dropped += 1
                            last_id = max(last_id, iid)
                            write_text(F_LAST_ID, str(last_id))
                            continue
                    elif send_reason == "timeout_after_reject":
                        # 전달 여부 불명이 반복 -> 무한 재시도하면 중복 폭탄
                        tmo[iid] = tmo.get(iid, 0) + 1
                        if tmo[iid] >= MAX_TIMEOUT_RETRY:
                            log("id=%d 불명 타임아웃 %d회 -> 중복 방지 위해 "
                                "보낸 것으로 간주" % (iid, tmo[iid]))
                            dropped += 1
                            unconfirmed += 1
                            last_id = max(last_id, iid)
                            write_text(F_LAST_ID, str(last_id))
                            continue
                    # 설정 오류(bad400_cfg/auth_401)는 절대 건너뛰지 않는다
                    log("send failed (%s) -> last_id 유지, 재시도" % send_reason)
                    send_failed = True
                    break
                try:
                    lag = int(time.time() - float(it.get("display_time") or 0))
                except Exception:
                    lag = -1
                time.sleep(SEND_GAP)

        # 피드가 이상할 때만 알린다. 전송 실패는 여기서 판단하지 않는다.
        if (not alerted) and (time.time() - tn > STALL_ALERT_SEC) and (
                feed_reason.startswith("fetch_error")
                or feed_reason.startswith("blocked")
                or feed_reason.startswith("rate_limited")):
            tg_send("⚠️ 华尔街见闻 속보 피드 이상 (%s) — %d분째 신규 없음"
                    % (feed_reason, int((time.time() - tn) / 60)))
            alerted = True

        # 종료 직전이면 건너뛴다. 안 그러면 루프 커밋과 최종 커밋이 1초 간격으로
        # 겹쳐 매 사이클 커밋/푸시가 두 번씩 발생한다.
        if (time.time() - tc > COMMIT_EVERY
                and time.time() - t0 < RUN_SECONDS - COMMIT_TAIL_GUARD):
            save_sent(sent)
            heartbeat(last_id=last_id, send="%d건" % total, reason=feed_reason,
                      send_reason=send_reason, lag=lag, rejects=_reject_count,
                      dropped=dropped, unconfirmed=unconfirmed,
                      tr_fail=_tr_fail,
                      filtered=_counts(filt), labels=_counts(labels, "-"))
            commit_state()
            tc = time.time()

        if send_failed:
            # 1초 간격으로 계속 두드리면 텔레그램에 수천 건 실패요청이 감
            log("전송 실패 -> %d초 대기 후 재시도" % SEND_BACKOFF)
            time.sleep(SEND_BACKOFF)
            continue

        el = time.time() - cyc
        if el < POLL_INTERVAL:
            time.sleep(POLL_INTERVAL - el)

    save_sent(sent)
    heartbeat(last_id=last_id, send="%d건" % total, reason=feed_reason,
              send_reason=send_reason, lag=lag, rejects=_reject_count,
              dropped=dropped, unconfirmed=unconfirmed,
              tr_fail=_tr_fail,
              filtered=_counts(filt), labels=_counts(labels, "-"),
              exit="normal")
    commit_state()
    log("done sent=%d last_id=%d unconfirmed=%d" % (total, last_id, unconfirmed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
