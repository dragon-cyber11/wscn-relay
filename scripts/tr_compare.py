#!/usr/bin/env python3
"""일회성: 같은 헤드라인을 Gemini 모델별로 번역해 품질·속도 비교. 텔레그램 발송 없음."""
import json, sys, time
import wscn_relay as w

MODELS = ["models/gemini-3.5-flash-lite", "models/gemini-3.6-flash",
          "models/gemini-3.8-flash", "models/gemini-4-hydrogen"]
S = json.load(open("scripts/tr_samples.json", encoding="utf-8"))
avail = [m["name"] for m in w._gem_req("/models?pageSize=200").get("models", [])]
print("사용 가능:", [m for m in MODELS if m in avail])
stats = {}
for src, items in (("English", S["en"]), ("Chinese", S["zh"])):
    for text in items:
        print("\n■", text)
        for m in MODELS:
            w._gem_model = m
            w._gem_off_until = 0
            t0 = time.time()
            out = w.gemini_translate(text, src=src, timeout=20)
            dt = time.time() - t0
            st = stats.setdefault(m, [0, 0, 0.0])
            st[0] += 1; st[1] += out is not None; st[2] += dt
            print("  %-24s %4.1fs  %s" % (m.split("/")[1], dt, out))
            time.sleep(1)
print("\n== 요약 (성공/시도, 평균초)")
for m, (n, ok, t) in stats.items():
    print("  %-24s %d/%d  %.2fs" % (m.split("/")[1], ok, n, t / n))
