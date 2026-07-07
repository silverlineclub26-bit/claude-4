# -*- coding: utf-8 -*-
"""
台指期 月線操作系統（單一商品 · 期貨專用）

判斷邏輯（前後自洽）：
- 月線 MA20 = 多空主方向：站上做多、跌破多單出場
- 季線 MA60 = 大趨勢濾網：只有「價在季線下且季線下彎」才允許做空；
  多頭 / 中性大趨勢一律不做空，跌破月線就空手（避免逆勢、避免隔日大翻轉）
- 5日 / 10日 = 短線強弱與加碼階梯（進取型：確認後 1→2→3 口）

資料：FinMind TaiwanFuturesDaily（微台 MTX，近月主力）。
每日由 GitHub Actions 執行，index.html 推回 repo 後由 GitHub Pages 發布。
"""

import os
import datetime
import json

import requests

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

PV = 10          # 微台每點價值（元）
MARGIN = 31800   # 微台原始保證金（元）


# ---------------------------------------------------------------------------
# 資料抓取
# ---------------------------------------------------------------------------

def fetch_futures_daily(futures_id, start_date, end_date):
    """呼叫 FinMind TaiwanFuturesDaily，回傳一天一筆日 K（舊到新，取當日成交量最大合約）。"""
    params = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": futures_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    headers = {}
    if FINMIND_TOKEN:
        headers["Authorization"] = "Bearer " + FINMIND_TOKEN

    try:
        resp = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError("呼叫 FinMind API 失敗：%s" % e) from e

    if resp.status_code != 200:
        raise RuntimeError("FinMind API 回傳 HTTP %s：%s" % (resp.status_code, resp.text[:300]))

    payload = resp.json()
    if payload.get("status") != 200 and payload.get("msg", "").lower() not in ("success", ""):
        raise RuntimeError("FinMind API 回傳非成功狀態：%s" % str(payload)[:300])

    rows = payload.get("data", []) or []
    if not rows:
        raise RuntimeError("FinMind 沒有回傳任何資料（data_id=%s, %s ~ %s）。" % (futures_id, start_date, end_date))

    if any("trading_session" in r for r in rows):
        day_rows = [r for r in rows if str(r.get("trading_session", "")).lower() == "position"]
        if day_rows:
            rows = day_rows

    def _vol(r):
        v = r.get("trading_volume", r.get("volume", 0))
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    best_by_date = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        if d not in best_by_date or _vol(r) > _vol(best_by_date[d]):
            best_by_date[d] = r

    bars = []
    for d in sorted(best_by_date):
        r = best_by_date[d]
        try:
            bar = {"date": d, "open": float(r["open"]), "max": float(r["max"]),
                   "min": float(r["min"]), "close": float(r["close"])}
        except (KeyError, TypeError, ValueError):
            continue
        if bar["max"] <= 0 and bar["min"] <= 0 and bar["close"] <= 0:
            continue
        bars.append(bar)

    if not bars:
        raise RuntimeError("FinMind 資料整理後為空，無有效日 K。")
    return bars


# ---------------------------------------------------------------------------
# 指標與狀態機
# ---------------------------------------------------------------------------

def sma(values, period):
    out = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


# 狀態鍵 → (標題, 說明, 建議動作, 主色)  漲紅跌綠
RED, GREEN, GREY = "#E5484D", "#3DAE73", "#8B919B"
STATE_META = {
    "up_strong": ("多方強勢", "站上月線，且站上 5 日 / 10 日，短線最強。",
                  "滿倉續抱 · 站穩就加碼到 3 口", RED),
    "up_r5":     ("多方（回檔 5 日）", "站上月線與 10 日、跌破 5 日，短線小回。",
                  "持有 2 口 · 站回 5 日再加碼", RED),
    "up_r10":    ("多方轉弱", "站上月線但跌破 10 日，短線走弱。",
                  "基本 1 口 · 跌破月線就出場", RED),
    "exit_flat": ("跌破月線 · 空手", "跌破月線、多單出場；大趨勢非空頭，不做空。",
                  "空手觀望 · 站回月線再進多", GREY),
    "down_strong": ("空方強勢", "季線空頭確認，且跌破 5 日 / 10 日，短線最弱。",
                    "滿倉空單續抱 · 續弱加碼到 3 口", GREEN),
    "down_r5":   ("空方（回彈 10 日）", "季線空頭、站回 10 日仍在月線下，小反彈。",
                  "持有 2 口空 · 再破 5 日加碼", GREEN),
    "down_r10":  ("空方轉弱", "季線空頭但接近月線，空方轉弱。",
                  "基本 1 口空 · 站上月線就回補", GREEN),
    "bounce_flat": ("空頭反彈 · 空手", "季線空頭大趨勢下站回月線，視為反彈。",
                    "空手觀望 · 不追多（等季線轉強）", GREY),
    "hold_below": ("多方 · 跌破月線待確認", "收盤跌破月線但未連兩日確認，續抱多單不砍。",
                   "續抱 · 若明日仍收月線下才出場", RED),
    "hold_above": ("空方 · 站回月線待確認", "收盤站回月線但未連兩日確認，續抱空單不補。",
                   "續抱空單 · 若明日仍收月線上才回補", GREEN),
    "none":      ("資料不足", "均線尚未齊備。", "觀望", GREY),
}


PHASE_META = {   # 發散-收斂週期階段 → (標籤, css, 目標口數)
    "expand": ("發散中 · 大幅加碼", "md-trend", 3),
    "fade":   ("發散尾聲/開始收斂 · 逐步減碼", "md-normal", 2),
    "coil":   ("收斂進行中 · 不動作(底倉)", "md-chop", 1),
}


def build_history(bars, max_days=180):
    closes = [b["close"] for b in bars]
    ma5, ma10, ma20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    ma60, ma240 = sma(closes, 60), sma(closes, 240)
    n = len(bars)
    ranges = [b["max"] - b["min"] for b in bars]

    # 日報酬與波動(標準差),供「波動警示」自適應停損
    rets = [0.0] * n
    for i in range(1, n):
        if closes[i - 1]:
            rets[i] = (closes[i] - closes[i - 1]) / closes[i - 1]

    def _std(lst):
        if len(lst) < 2:
            return 0.0
        mu = sum(lst) / len(lst)
        return (sum((x - mu) ** 2 for x in lst) / len(lst)) ** 0.5

    # 三線(5/10/20)相對收盤的糾結度(%)，供三線糾結/突破判定
    sp3 = [None] * n
    for i in range(n):
        a, b_, c_ = ma5[i], ma10[i], ma20[i]
        if None not in (a, b_, c_) and closes[i]:
            sp3[i] = (max(a, b_, c_) - min(a, b_, c_)) / closes[i] * 100.0

    def _r(x):
        return None if x is None else round(x, 1)

    recs = []
    ml_side = None    # 月線側別(遲滯):進場即時、跌破月線需連兩日才確認出場
    for i in range(n):
        c = closes[i]
        m5, m10, m20, m60 = ma5[i], ma10[i], ma20[i], ma60[i]
        if m20 is None or m5 is None or m10 is None:
            continue

        # ── 沿用舊系統：波動洗盤 / 5-10日收斂(發散·盤整·震盪) / 三線糾結 ──
        r5 = ranges[max(0, i - 4):i + 1]; r20 = ranges[max(0, i - 19):i + 1]
        vr = sum(r5) / len(r5); vb = sum(r20) / len(r20)
        choppy = vb > 0 and vr > 1.2 * vb                       # 近5振幅 > 1.2×近20 = 洗盤

        # 發散(進取版)= 5日與10日距離比 3 天前變大;變小=收斂(高波動→震盪,低波動→盤整)
        conv, conv_label = "flat", "—"
        if i >= 3 and None not in (ma5[i], ma10[i], ma5[i - 3], ma10[i - 3]):
            dn = abs(ma5[i] - ma10[i]); dr = abs(ma5[i - 3] - ma10[i - 3])
            if dn < dr:
                conv, conv_label = ("chop", "震盪") if choppy else ("range", "盤整")
            else:
                conv, conv_label = "diverge", "發散"

        triband, triband_label = "mix", "中性"
        win = [v for v in sp3[max(0, i - 19):i + 1] if v is not None]
        base = sum(win) / len(win) if win else None
        if base and base > 0 and sp3[i] is not None:
            recent = [v for v in sp3[max(0, i - 4):i + 1] if v is not None]
            recent_tight = any(v < 0.6 * base for v in recent)
            cur_tight = sp3[i] < 0.6 * base
            above = c > m5 and c > m10 and c > m20
            below = c < m5 and c < m10 and c < m20
            if above and recent_tight:
                triband, triband_label = "break_up", "糾結突破↑"
            elif below and recent_tight:
                triband, triband_label = "break_dn", "糾結跌破↓"
            elif above:
                triband, triband_label = "above", "站上三線"
            elif below:
                triband, triband_label = "below", "跌破三線"
            elif cur_tight:
                triband, triband_label = "coil", "均線糾結"
            else:
                triband, triband_label = "mix", "中性"

        # ── 發散-收斂週期(核心加碼邏輯) ──
        # 用舊系統驗證過的訊號對應週期階段(比純斜率穩):
        #   發散/糾結突破 → 發散中(大幅加碼);三線糾結/震盪 → 收斂中(不動作底倉);其餘 → 尾聲/趨緩(逐步)
        if conv == "diverge":
            phase, plab = "expand", "均線發散 · 大幅加碼"
        elif triband in ("break_up", "break_dn"):
            phase, plab = "expand", "糾結突破 · 大幅加碼"
        elif triband == "coil" or conv == "chop":
            phase, plab = "coil", "收斂盤整 · 不動作底倉"
        else:
            phase, plab = "fade", "趨緩/尾聲 · 逐步減碼"

        # 季線大趨勢（做空濾網 / 標籤）：需 60 日均線與 20 根前的斜率
        m60p = ma60[i - 20] if (m60 is not None and i >= 20) else None
        bear = (m60 is not None and m60p is not None and c < m60 and m60 < m60p)
        bull = (m60 is not None and m60p is not None and c > m60 and m60 > m60p)
        regime = "bear" if bear else ("bull" if bull else "neutral")

        # 月線側別遲滯：站上月線當日即進;跌破月線需「連兩日」才翻(隔日確認出場)
        ab = c >= m20
        if ml_side is None:
            ml_side = "above" if ab else "below"
        elif ml_side == "above":
            prev_below = (i >= 1 and ma20[i - 1] is not None and closes[i - 1] < ma20[i - 1])
            if (not ab) and prev_below:            # 今、昨連兩日破月線 → 確認出場
                ml_side = "below"
        else:  # below
            if ab:                                 # 站回月線當日即翻
                ml_side = "above"

        # 自洽狀態機：空頭大趨勢才做空；否則只做多；跌破月線隔日確認才出場
        if bear:
            if ml_side == "below":
                d = -1
                if c > m20:                        # 站回月線但未連兩日確認 → 續抱空
                    raw, state = 1, "hold_above"
                else:
                    raw = 3 if c <= m5 else (2 if c <= m10 else 1)
                    state = "down_strong" if c <= m5 else ("down_r5" if c <= m10 else "down_r10")
            else:
                d, raw, state = 0, 0, "bounce_flat"
        else:
            if ml_side == "above":
                d = 1
                if c < m20:                        # 跌破月線但未連兩日確認 → 續抱多
                    raw, state = 1, "hold_below"
                else:
                    raw = 3 if c >= m5 else (2 if c >= m10 else 1)
                    state = "up_strong" if c >= m5 else ("up_r5" if c >= m10 else "up_r10")
            else:
                d, raw, state = 0, 0, "exit_flat"

        # ── 動態加碼(大賺小賠)：口數跟著發散-收斂週期走 ──
        _, p_cls, p_tier = PHASE_META[phase]
        p_label = plab               # 顯示實際觸發原因(發散 or 糾結突破)
        lots = 0 if d == 0 else p_tier
        # 回檔不破加碼:趨勢中「收黑(回檔)但守住10日不破」= 好加碼點 → 補到滿倉
        # (29年回測:疊在發散之上、報酬升、回檔持平;不取代發散,只在好點位補滿)
        pullback_add = False
        if d != 0 and i >= 1 and m10 is not None and closes[i - 1] is not None:
            if d > 0:
                pullback_add = (c < closes[i - 1]) and (c > m10)   # 多頭:收黑但守10日上
            else:
                pullback_add = (c > closes[i - 1]) and (c < m10)   # 空頭:收紅但守10日下
        if pullback_add:
            lots = 3
        # 波動放大加碼:當日振幅 > 近20日均的 VOLK 倍、且守月線(趨勢仍在)→ 恐慌/爆量常是轉折,買不砍。
        # (29年回測:VK=1.75 年化+2.4%、回檔不惡化、斷頭0;破月線仍出場不加。與「拿波動大去砍」相反,砍會賣在底。)
        VOLK = 1.75
        vol_ratio = 0.0
        if i >= 20:
            _aw = ranges[i - 20:i]
            _av = sum(_aw) / len(_aw) if _aw else 0.0
            if _av > 0:
                vol_ratio = ranges[i] / _av
        on_side = (c > m20) if d > 0 else (c < m20)               # 守月線(趨勢仍在)
        vol_add = bool(d != 0 and vol_ratio >= VOLK and on_side)
        if vol_add:
            lots = 3
        # 部位比例(滿倉的幾成):平時發散最多「八成」,只有回檔不破 / 波動放大守月線才准「滿倉」
        # (回測驗證:斷頭歸零、報酬幾乎不減——最後兩成只加在回檔守線的好點位)
        if d == 0:
            fill_base = 0.0       # 階段基準比例(減碼前)
        elif phase == "expand":
            fill_base = 1.0       # 發散中 → 可滿倉(絕對口數由斷頭緩衝上限控管)
        elif phase == "fade":
            fill_base = 0.5       # 開始轉收斂 → 五成(統一「中間級距」都五成,好記)
        else:
            fill_base = 0.333     # 收斂 → 底倉三成
        fill = 1.0 if ((pullback_add or vol_add) and d != 0) else fill_base   # 回檔不破 / 波動放大守月線 → 滿倉
        # 減碼規則:趨勢中跌破短均線就先縮部位(不等月線破)→ 反轉少受傷。回檔不破 / 波動放大守月線 豁免。
        # (29年回測:回檔大降~13%,報酬幾乎不變)raw: 3=站上5日 2=破5日守10日 1=破10日/守月線
        reduce_label = ""
        if d != 0 and not pullback_add and not vol_add:
            if raw <= 1:
                fill = min(fill, 0.333); reduce_label = "破10日·減碼底倉"
            elif raw == 2:
                fill = min(fill, 0.5);   reduce_label = "破5日·減碼五成"

        # 波動→停損上移改由 live 15分即時負責;網站停損統一掛月線
        warn = False
        stop_label = "月線"
        stop_val = m20

        headline, desc, action, accent = STATE_META[state]
        # 依週期階段覆寫建議動作(待確認狀態保留自己的續抱說明)
        if d != 0 and state not in ("hold_below", "hold_above"):
            dw = "多" if d > 0 else "空"
            if vol_add:
                action = "波動放大+守月線 · 恐慌爆量常是轉折 · 加碼往滿倉（" + dw + "方）"
            elif pullback_add:
                action = "回檔不破守住10日 · 好加碼點、補到滿倉（" + dw + "方）"
            elif phase == "expand":
                action = "發散中 · 大幅加碼、往滿倉（" + dw + "方）"
            elif phase == "fade":
                action = "發散尾聲/開始收斂 · 逐步減碼、收獲利（" + dw + "方）"
            else:
                action = "收斂進行中 · 不加碼、只留底倉（" + dw + "方）"

        recs.append({
            "date": bars[i]["date"], "close": round(c, 0),
            "up": 1 if (i >= 1 and closes[i] >= closes[i - 1]) else 0,   # 當天收紅(漲)
            "ma5": _r(m5), "ma10": _r(m10), "ma20": _r(m20),
            "ma60": _r(m60), "ma240": _r(ma240[i]),
            "regime": regime, "dir": d, "lots": lots, "raw": raw, "state": state,
            "vol_add": 1 if vol_add else 0, "vol_ratio": round(vol_ratio, 2),
            "pullback_add": 1 if pullback_add else 0, "fill": round(fill, 3),
            "fill_base": round(fill_base, 3), "reduce_label": reduce_label,
            "mode": phase, "mode_label": p_label, "mode_cls": p_cls,
            "warn": 1 if warn else 0, "stop": _r(stop_val), "stop_label": stop_label,
            "conv_label": conv_label, "triband_label": triband_label,
            "headline": headline, "desc": desc, "action": action, "accent": accent,
        })
    return recs[-max_days:]


# ---------------------------------------------------------------------------
# HTML 產出
# ---------------------------------------------------------------------------

def generate_html(records, gen_time):
    data_json = json.dumps(records, ensure_ascii=False)
    html = _TEMPLATE.replace("__DATA__", data_json).replace("__GEN__", gen_time)
    return html


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>台指期 月線操作系統</title>
<style>
  :root { --bg:#0B0D10; --card:#12151A; --line:#242A31; --ink:#E6E8EB; --muted:#8B919B;
    --accent:#E5484D; --red:#E5484D; --green:#3DAE73; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif;
    line-height:1.5; padding:0 0 40px; }
  .wrap { max-width:520px; margin:0 auto; padding:16px 14px; }
  h1 { font-size:19px; font-weight:800; margin:6px 0 2px; letter-spacing:.5px; }
  .eyebrow { font-size:12px; color:var(--muted); font-weight:700; letter-spacing:1px; }
  .sub-note { font-size:11.5px; color:var(--muted); margin-top:4px; line-height:1.6; }

  .picker { display:flex; align-items:center; gap:8px; margin:14px 0 10px; }
  .picker label { font-size:12px; color:var(--muted); font-weight:700; }
  .picker input[type=date] { flex:1; padding:9px 11px; border-radius:9px; border:1px solid var(--line);
    background:var(--card); color:var(--ink); font-size:14px; }
  .nav { display:flex; gap:6px; }
  .nav button { width:38px; height:38px; border-radius:9px; border:1px solid var(--line);
    background:var(--card); color:var(--ink); font-size:18px; cursor:pointer; }
  .nav button:disabled { opacity:.35; }
  .ct-sel { padding:9px 10px; border-radius:9px; border:1px solid var(--line);
    background:var(--card); color:var(--ink); font-size:14px; font-weight:800; cursor:pointer; }

  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:16px; margin-bottom:12px; }
  .card-title { font-size:12.5px; font-weight:800; color:var(--muted); letter-spacing:.6px;
    display:flex; align-items:center; gap:8px; margin-bottom:12px; cursor:pointer; user-select:none; }
  .tag { font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:999px; border:1px solid var(--line); }
  .caret { margin-left:auto; font-size:13px; color:var(--muted); transition:transform .2s; padding:0 2px; }
  .card.collapsed .card-title { margin-bottom:0; }
  .card.collapsed .card-body { display:none; }
  .card.collapsed .caret { transform:rotate(-90deg); }

  /* 盤勢建議 hero */
  .regime { display:inline-block; font-size:12px; font-weight:800; padding:4px 11px; border-radius:999px;
    border:1px solid; margin-bottom:10px; }
  .rg-bull { color:var(--red); border-color:rgba(229,72,77,.5); background:rgba(229,72,77,.08); }
  .rg-bear { color:var(--green); border-color:rgba(61,174,115,.5); background:rgba(61,174,115,.08); }
  .rg-neutral { color:var(--muted); border-color:var(--line); }
  .mode { display:inline-block; font-size:12px; font-weight:800; padding:4px 11px; border-radius:999px;
    border:1px solid; margin:0 0 10px 6px; }
  .md-trend  { color:#3DA9FC; border-color:rgba(61,169,252,.5); background:rgba(61,169,252,.08); }
  .md-normal { color:var(--muted); border-color:var(--line); }
  .md-chop   { color:#D4A73C; border-color:rgba(212,167,60,.5); background:rgba(212,167,60,.1); }
  .warn-badge { display:inline-block; font-size:12px; font-weight:800; padding:4px 11px; border-radius:999px;
    margin:0 0 10px 6px; color:#fff; background:#E5484D; border:1px solid #E5484D; }
  .maline { font-size:11.5px; color:var(--muted); margin-top:8px; }
  .maline b { color:#C2C7CE; }
  .headline { font-size:27px; font-weight:900; letter-spacing:.5px; display:flex; align-items:center; gap:10px; }
  .arrow { font-size:22px; font-weight:900; }
  .metaline { font-size:12.5px; color:var(--muted); margin-top:6px; }
  .metaline b { color:var(--ink); font-variant-numeric:tabular-nums; }
  .desc { font-size:13.5px; color:#C2C7CE; margin-top:12px; }
  .action { margin-top:12px; padding:13px 14px; border-radius:10px; font-weight:800; font-size:15.5px; text-align:center;
    border:1px solid; }
  .act-long { color:var(--red); border-color:rgba(229,72,77,.45); background:rgba(229,72,77,.1); }
  .act-short { color:var(--green); border-color:rgba(61,174,115,.45); background:rgba(61,174,115,.1); }
  .act-flat { color:var(--muted); border-color:var(--line); background:rgba(139,145,155,.08); }
  .baselots { margin-top:10px; font-size:15px; font-weight:800; text-align:center; }

  .levels { display:grid; grid-template-columns:repeat(5,1fr); gap:6px; margin-top:14px; }
  .lv { background:#0E1116; border:1px solid var(--line); border-radius:9px; padding:8px 4px; text-align:center; }
  .lv-k { font-size:10.5px; color:var(--muted); font-weight:700; }
  .lv-v { font-size:13px; font-weight:800; font-variant-numeric:tabular-nums; margin-top:2px; }
  .lv.up .lv-v { color:var(--red); } .lv.down .lv-v { color:var(--green); }

  /* 輸入卡共用 */
  .field, .cap-field { display:flex; align-items:center; gap:8px; background:#0E1116; border:1px solid var(--line);
    border-radius:10px; padding:11px 13px; }
  .field:focus-within, .cap-field:focus-within { border-color:var(--accent); }
  .field span, .cap-field span { font-size:12.5px; color:var(--muted); font-weight:700; white-space:nowrap; }
  .field input, .cap-field input { flex:1; min-width:0; background:transparent; border:none; outline:none;
    color:var(--ink); font-size:20px; font-weight:800; text-align:right; }
  input[type=number]::-webkit-outer-spin-button, input[type=number]::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
  input[type=number] { -moz-appearance:textfield; }

  .dir-toggle { display:flex; gap:6px; margin-bottom:10px; }
  .dir-toggle button { flex:1; padding:9px 0; border-radius:8px; border:1px solid var(--line);
    background:#0E1116; color:var(--muted); font-size:14px; font-weight:800; cursor:pointer; transition:.15s; }
  .dir-toggle button.on[data-d="1"] { background:var(--red); color:#fff; border-color:var(--red); }
  .dir-toggle button.on[data-d="-1"] { background:var(--green); color:#fff; border-color:var(--green); }
  .row2 { display:flex; gap:8px; }
  .row2 .field { flex:1; min-width:0; }

  .result { margin-top:12px; padding:14px; border-radius:10px; font-weight:800; font-size:16px;
    text-align:center; border:1px solid var(--line); background:#0E1116; line-height:1.5; }
  .result b { font-size:25px; }
  .result .sub { font-weight:600; font-size:12.5px; color:var(--muted); margin-top:4px; }
  .result .sub.warn { color:#D4A73C; font-weight:700; }
  .result.long { color:var(--red); } .result.short { color:var(--green); } .result.flat { color:var(--muted); }

  .risk-row { display:flex; align-items:center; gap:10px; margin-top:10px; }
  .risk-lab { font-size:12.5px; font-weight:800; color:var(--muted); }
  .chips { display:flex; gap:6px; flex:1; }
  .chips button { flex:1; padding:8px 0; border-radius:8px; border:1px solid var(--line);
    background:#0E1116; color:var(--muted); font-size:13.5px; font-weight:800; cursor:pointer; transition:.15s; }
  .chips button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  .chips .custom-wrap { flex:1; min-width:0; position:relative; display:flex; }
  .chips .custom { width:100%; min-width:0; padding:8px 16px 8px 4px; border-radius:8px; border:1px solid var(--line);
    background:#0E1116; color:var(--ink); font-size:13px; font-weight:800; text-align:center; }
  .chips .custom:focus { outline:none; border-color:var(--accent); }
  .chips .custom-wrap.filled::after { content:"%"; position:absolute; right:7px; top:50%; transform:translateY(-50%);
    color:var(--muted); font-size:11.5px; font-weight:800; pointer-events:none; }
  .chips .custom-wrap.on .custom { background:var(--accent); color:#fff; border-color:var(--accent); }
  .chips .custom-wrap.on.filled::after { color:#fff; }

  .hint { margin-top:10px; padding:12px 14px; border-radius:10px; font-size:14.5px; font-weight:800;
    text-align:center; border:1px solid; line-height:1.5; }
  .hint b { font-size:17px; }
  .hint.add { color:#3DA9FC; border-color:rgba(61,169,252,.5); background:rgba(61,169,252,.1); }
  .hint.cut { color:#D4A73C; border-color:rgba(212,167,60,.5); background:rgba(212,167,60,.1); }
  .hint.out { color:var(--green); border-color:rgba(61,174,115,.5); background:rgba(61,174,115,.1); }
  .hint.ok  { color:var(--red); border-color:rgba(229,72,77,.5); background:rgba(229,72,77,.1); }

  footer { font-size:10.5px; color:var(--muted); text-align:center; margin-top:14px; line-height:1.7; }
</style>
</head>
<body>
<div class="wrap">
  <h1>PO的期貨無腦照作系統 V1.0</h1>

  <div class="picker">
    <input type="date" id="dateInput">
    <div class="nav">
      <button id="prevBtn" title="前一交易日">‹</button>
      <button id="nextBtn" title="後一交易日">›</button>
    </div>
    <select id="ctSel" class="ct-sel" title="商品">
      <option value="TX">大台</option>
      <option value="MTX">小台</option>
      <option value="TMF">微台</option>
    </select>
  </div>

  <!-- 1. 盤勢建議 -->
  <div class="card" id="cardStance">
    <div class="card-title">📈 每日盤勢<span class="tag">收盤定調 · 隔日確認</span><span class="caret">▾</span></div>
    <div class="card-body">
      <span class="regime" id="regimeTag"></span><span class="mode" id="modeTag"></span>
      <div class="headline"><span id="headEl">—</span><span class="arrow" id="arrowEl"></span></div>
      <div class="metaline">收盤日 <b id="dateOut">—</b> · 收盤 <b id="closeOut">—</b> <span id="latestBadge"></span></div>
      <div class="maline" id="maLine"></div>
      <div class="desc" id="descEl"></div>
      <div class="baselots" id="baseLots"></div>
    </div>
  </div>

  <!-- 2. 我的現有部位 -->
  <div class="card" id="cardPos">
    <div class="card-title">📌 我的現有部位<span class="tag">自動記憶 · 算損益</span><span class="caret">▾</span></div>
    <div class="card-body">
      <div class="dir-toggle" id="posDir">
        <button data-d="1">多單</button><button data-d="-1">空單</button>
      </div>
      <div class="row2">
        <div class="field"><input type="number" id="posLots" min="0" step="1" placeholder="0" inputmode="numeric"><span>口</span></div>
        <div class="field"><input type="number" id="posCost" min="0" step="1" placeholder="進場價" inputmode="numeric"><span>成本</span></div>
      </div>
      <div class="result" id="posBox">尚未輸入部位</div>
      <div class="hint" id="hintBox" style="display:none"></div>
    </div>
  </div>

  <!-- 3. 風險策略 -->
  <div class="card" id="cardRisk">
    <div class="card-title">🧮 風險策略調整<span class="tag">控管單筆風險 · 自動算口數</span><span class="caret">▾</span></div>
    <div class="card-body">
      <div class="cap-field">
        <span>本金</span>
        <input type="number" id="capInput" min="1" step="1" value="10" inputmode="numeric" placeholder="10">
        <span>萬元</span>
      </div>
      <div class="risk-row">
        <span class="risk-lab">風險</span>
        <div class="chips" id="riskChips">
          <button data-r="5">5%</button><button data-r="10">10%</button><button data-r="20">20%</button><button data-r="30">30%</button>
          <span class="custom-wrap" id="riskCustomWrap"><input type="number" id="riskCustom" class="custom" min="1" max="100" placeholder="自訂" inputmode="numeric"></span>
        </div>
      </div>
      <div class="result" id="riskBox"></div>
    </div>
  </div>

  <footer>
    資料來源 FinMind（台指微台 MTX 近月主力）· 本頁產生 __GEN__<br>
    歷史為當日(含)之前資料回推 · 僅供研究參考，非投資建議
  </footer>
</div>

<script>
const HIST = __DATA__;
// 三種台指期(同一指數、不同乘數):每點價值與原始保證金(約)
const CONTRACTS = {
  TX:  { name: "大台", pv: 200, mg: 190000 },
  MTX: { name: "小台", pv: 50,  mg: 93500  },
  TMF: { name: "微台", pv: 10,  mg: 31800  }
};
window.__ct = "TMF";
function CT() { return CONTRACTS[window.__ct] || CONTRACTS.TMF; }
const REGIME = {
  bull:    { label: "多頭大趨勢 · 季線上揚", cls: "rg-bull" },
  bear:    { label: "空頭大趨勢 · 季線下彎", cls: "rg-bear" },
  neutral: { label: "趨勢未明 · 季線走平", cls: "rg-neutral" }
};

const dateInput = document.getElementById("dateInput");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");
const capInput = document.getElementById("capInput");
const riskChips = document.getElementById("riskChips");
const riskCustom = document.getElementById("riskCustom");
const posDir = document.getElementById("posDir");
const posLots = document.getElementById("posLots");
const posCost = document.getElementById("posCost");
const riskBox = document.getElementById("riskBox");
const posBox = document.getElementById("posBox");
const hintBox = document.getElementById("hintBox");

let curIdx = HIST.length - 1;

function fmt(v, d) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
}

function render(idx) {
  const r = HIST[idx]; if (!r) return;
  curIdx = idx;
  const isLatest = idx === HIST.length - 1;
  dateInput.value = r.date;
  document.documentElement.style.setProperty("--accent", r.accent);

  const rg = REGIME[r.regime] || REGIME.neutral;
  const rt = document.getElementById("regimeTag");
  rt.textContent = rg.label; rt.className = "regime " + rg.cls;
  const mt = document.getElementById("modeTag");
  mt.textContent = r.mode_label; mt.className = "mode " + r.mode_cls;
  document.getElementById("maLine").innerHTML = "均線：發散度 <b>" + r.conv_label + "</b> · 三線 <b>" + r.triband_label + "</b>";

  const head = document.getElementById("headEl");
  head.textContent = r.headline; head.style.color = r.accent;
  // 均線排列天氣(5/10/20):5>10>20 多頭排列=☀️太陽;5<10<20 空頭排列=🌧️下雨;其餘 糾結=⛅多雲
  const arrow = document.getElementById("arrowEl");
  var a5 = r.ma5, a10 = r.ma10, a20 = r.ma20, wx = "", wl = "";
  if (a5 != null && a10 != null && a20 != null) {
    if (a5 > a10 && a10 > a20) { wx = "☀️"; wl = "多頭排列"; }
    else if (a5 < a10 && a10 < a20) { wx = "🌧️"; wl = "空頭排列"; }
    else { wx = "⛅"; wl = "糾結"; }
  }
  arrow.innerHTML = wx ? (wx + '<span style="font-size:13px;font-weight:700;margin-left:5px;color:var(--muted)">' + wl + '</span>') : "";

  document.getElementById("dateOut").textContent = r.date;
  var ce = document.getElementById("closeOut");
  ce.textContent = fmt(r.close, 0);
  ce.style.color = r.up ? "var(--red)" : "var(--green)";   // 當天漲紅跌綠
  document.getElementById("latestBadge").textContent = isLatest ? "· ● 最新" : "· ○ 歷史回推";

  document.getElementById("descEl").textContent = r.desc;

  window.__curRec = r;
  renderRisk();
  renderPos();

  prevBtn.disabled = idx === 0;
  nextBtn.disabled = isLatest;
}

function renderRisk() {
  var r = window.__curRec; if (!r) return;
  var bl = document.getElementById("baseLots");
  var PVV = CT().pv, MG = CT().mg;
  var baseCap = (parseFloat(capInput.value) || 0) * 10000;   // 輸入本金(萬元)
  // 即時權益 = 本金 + 未實現損益:價漲離月線變遠時,獲利同步墊高可下口數(與回測一致)
  var _pl = parseFloat(posLots.value) || 0, _pc = parseFloat(posCost.value) || 0, _pd = window.__posDir || 1;
  var upnl = (_pl > 0 && _pc > 0) ? (r.close - _pc) * _pd * _pl * PVV : 0;
  var cap = baseCap + upnl;
  var RISK = (window.__risk || 10) / 100;
  var dir = r.dir, m20 = r.ma20, c = r.close;
  if (dir === 0) {
    riskBox.textContent = "系統空手 · 無建議口數"; riskBox.className = "result flat";
    bl.innerHTML = '<span style="color:var(--muted)">系統空手觀望（0 口）</span>';
    window.__targetLots = 0; renderHint(); return;
  }
  if (cap <= 0 || m20 == null) {
    riskBox.textContent = "請於下方輸入本金"; riskBox.className = "result flat";
    bl.innerHTML = '<span style="color:var(--muted)">請輸入本金以計算建議口數</span>';
    window.__targetLots = 0; renderHint(); return;
  }
  var dist = dir > 0 ? (c - m20) : (m20 - c);
  var stop = Math.max(dist, c * 0.01);        // 到月線停損（最小 1%）
  var oneLot = stop * PVV;
  var buf = 0.75 * MG + 0.05 * c * PVV;        // 斷頭緩衝5%:主動管理(停損/減碼)是主防線,這是「跳空開盤來不及反應」的安全氣囊
  var capMax = Math.floor(cap / buf);          // 永遠開著的斷頭防護上限
  var full = Math.floor(cap * RISK / oneLot);  // 滿倉(風險式)
  var forced = false;
  if (full < 1 && capMax >= 1) { full = 1; forced = true; }
  var capped = false;
  if (full > capMax) { full = capMax; capped = true; }
  if (full < 1) {
    riskBox.textContent = "建議 0 口 · 本金不足一口保證金 NT$" + fmt(MG, 0);
    riskBox.className = "result flat";
    bl.innerHTML = '<span style="color:var(--muted)">本金不足 1 口</span>';
    window.__targetLots = 0; renderHint(); return;
  }
  var fill = (r.fill != null) ? r.fill : (r.lots / 3);   // 部位比例:發散上限八成、回檔不破才滿倉
  var N = Math.max(1, Math.round(full * fill));
  var fillLab = r.vol_add ? "波動放大·守月線加碼" : (r.pullback_add ? "回檔不破·滿倉" : (r.reduce_label ? r.reduce_label : (fill >= 1 ? "發散·滿倉" : (fill >= 0.5 ? "轉收斂·五成" : "收斂·底倉三成"))));
  var word = dir > 0 ? "口多單" : "口空單";
  var col = dir > 0 ? "var(--red)" : "var(--green)";
  // Section 1:現在建議持有(統一口數)
  bl.innerHTML = '現在建議持有 <b style="color:' + col + '">' + N + '</b> ' + word;
  // 試算卡:現在 N + 滿倉 full
  var warn = "";
  if (forced) warn = '<div class="sub warn">⚠️ 本金小，滿倉這 1 口風險約 ' + (oneLot / cap * 100).toFixed(0) + '%，已超過所選 ' + (RISK * 100) + '%</div>';
  else if (capped) warn = '<div class="sub warn">⚠️ 已達斷頭緩衝上限 ' + capMax + ' 口（撐得過跳空5%,其餘靠主動停損）</div>';
  var eqNote = upnl ? ' · 權益 NT$' + fmt(cap, 0) + '（本金+浮盈 ' + (upnl >= 0 ? '+' : '') + fmt(upnl, 0) + '）' : '';
  riskBox.innerHTML = "現在建議 <b>" + N + "</b> " + word +
    '<div class="sub">滿倉上限 ' + full + ' 口 · 部位 ' + Math.round(fill * 100) + '%（' + fillLab + '） · 到月線 ' + Math.round(dist) + ' 點 · 一口風險 NT$' + fmt(oneLot, 0) + eqNote + '</div>' + warn;
  riskBox.className = "result " + (dir > 0 ? "long" : "short");
  window.__targetLots = N; renderHint();
}

function renderPos() {
  var r = window.__curRec; if (!r) return;
  var lots = parseFloat(posLots.value) || 0, cost = parseFloat(posCost.value) || 0, dir = window.__posDir || 1;
  if (lots <= 0 || cost <= 0) { posBox.textContent = "尚未輸入部位"; posBox.className = "result flat"; renderHint(); return; }
  var PVV = CT().pv, MG = CT().mg;
  var c = r.close;
  var pts = (c - cost) * dir, pnl = pts * lots * PVV, pctM = pnl / (lots * MG) * 100;
  var sign = pnl >= 0 ? "+" : "";
  var sv = r.stop, stopLine = "";
  if (sv != null) {
    var toS = dir > 0 ? (c - sv) : (sv - c);
    stopLine = " · " + (r.warn ? "⚠️停損上移" : "停損") + r.stop_label + " " + Math.round(sv) + "（距 " + Math.round(toS) + " 點）";
  }
  posBox.innerHTML = "浮動損益 <b>" + sign + "NT$" + fmt(pnl, 0) + "</b>" +
    '<div class="sub">' + sign + fmt(pts, 0) + " 點 · 對保證金 " + sign + pctM.toFixed(0) + "%" + stopLine + "</div>";
  posBox.className = "result " + (pnl >= 0 ? "long" : "short");   // 賺紅賠綠
  renderHint();
}

function renderHint() {
  var r = window.__curRec; if (!r) return;
  var cur = parseFloat(posLots.value) || 0;
  var curDir = window.__posDir || 1, sysDir = r.dir, target = window.__targetLots || 0;
  if (cur <= 0) {
    if (sysDir !== 0 && target > 0) {
      hintBox.innerHTML = "🚦 系統為" + (sysDir > 0 ? "多方" : "空方") + " · 可進場 <b>" + target + "</b> 口" + (sysDir > 0 ? "多單" : "空單");
      hintBox.className = "hint add"; hintBox.style.display = "block";
    } else { hintBox.style.display = "none"; }
    return;
  }
  hintBox.style.display = "block";
  if (sysDir === 0) { hintBox.innerHTML = "🚦 系統轉<b>空手</b> · 建議出場全部 " + cur + " 口"; hintBox.className = "hint out"; return; }
  if (sysDir !== curDir) { hintBox.innerHTML = "🔄 方向與系統<b>相反</b> · 建議平倉 " + cur + " 口" + (target > 0 ? "、反手 " + target + " 口" : ""); hintBox.className = "hint out"; return; }
  var diff = target - cur;
  if (diff > 0) { hintBox.innerHTML = "🔵 建議<b>加碼 " + diff + "</b> 口（目前 " + cur + " → 建議 " + target + "）"; hintBox.className = "hint add"; }
  else if (diff < 0) { hintBox.innerHTML = "🟠 建議<b>減碼 " + (-diff) + "</b> 口（目前 " + cur + " → 建議 " + target + "）"; hintBox.className = "hint cut"; }
  else { hintBox.innerHTML = "✅ 部位<b>符合建議</b>（" + target + " 口）· 續抱"; hintBox.className = "hint ok"; }
}

function nearestIdx(d) {
  var best = 0;
  for (var i = 0; i < HIST.length; i++) { if (HIST[i].date <= d) best = i; }
  return best;
}
dateInput.addEventListener("change", function () { if (dateInput.value) render(nearestIdx(dateInput.value)); });
prevBtn.addEventListener("click", function () { if (curIdx > 0) render(curIdx - 1); });
nextBtn.addEventListener("click", function () { if (curIdx < HIST.length - 1) render(curIdx + 1); });

// 本金（萬元）
var _sc = null; try { _sc = localStorage.getItem("ml_cap_wan"); } catch (e) {}
if (_sc) capInput.value = _sc;
capInput.addEventListener("input", function () { try { localStorage.setItem("ml_cap_wan", capInput.value); } catch (e) {} renderRisk(); });

// 風險%
var _sr = null; try { _sr = localStorage.getItem("ml_risk"); } catch (e) {}
window.__risk = _sr ? parseFloat(_sr) : 10;
var _cb = riskChips.querySelectorAll("button");
function _syncRisk() {
  var m = false;
  Array.prototype.forEach.call(_cb, function (x) { var on = parseFloat(x.getAttribute("data-r")) === window.__risk; x.classList.toggle("on", on); if (on) m = true; });
  riskCustom.value = m ? "" : window.__risk;
  var w = document.getElementById("riskCustomWrap");
  w.classList.toggle("on", !m);                       // 自訂被選 → 紅底定格
  w.classList.toggle("filled", !m && riskCustom.value !== "");
}
Array.prototype.forEach.call(_cb, function (b) {
  b.addEventListener("click", function () { window.__risk = parseFloat(b.getAttribute("data-r")); try { localStorage.setItem("ml_risk", window.__risk); } catch (e) {} _syncRisk(); renderRisk(); });
});
riskCustom.addEventListener("input", function () {
  var w = document.getElementById("riskCustomWrap");
  w.classList.toggle("filled", riskCustom.value !== "");
  var v = parseFloat(riskCustom.value); if (!isFinite(v) || v <= 0) { w.classList.remove("on"); return; }
  window.__risk = v; try { localStorage.setItem("ml_risk", window.__risk); } catch (e) {}
  Array.prototype.forEach.call(_cb, function (x) { x.classList.remove("on"); });
  w.classList.add("on"); renderRisk();
});
_syncRisk();

// 現有部位
try {
  var _pd = localStorage.getItem("ml_pos_dir"), _pl = localStorage.getItem("ml_pos_lots"), _pc = localStorage.getItem("ml_pos_cost");
  window.__posDir = _pd ? parseFloat(_pd) : 1;
  if (_pl) posLots.value = _pl;
  if (_pc) posCost.value = _pc;
} catch (e) { window.__posDir = 1; }
var _db = posDir.querySelectorAll("button");
function _syncPosDir() { Array.prototype.forEach.call(_db, function (x) { x.classList.toggle("on", parseFloat(x.getAttribute("data-d")) === window.__posDir); }); }
Array.prototype.forEach.call(_db, function (b) {
  b.addEventListener("click", function () { window.__posDir = parseFloat(b.getAttribute("data-d")); try { localStorage.setItem("ml_pos_dir", window.__posDir); } catch (e) {} _syncPosDir(); renderPos(); });
});
posLots.addEventListener("input", function () { try { localStorage.setItem("ml_pos_lots", posLots.value); } catch (e) {} renderPos(); });
posCost.addEventListener("input", function () { try { localStorage.setItem("ml_pos_cost", posCost.value); } catch (e) {} renderPos(); });
_syncPosDir();

// 商品選擇(下拉:大台/小台/微台):切換後 PV、保證金、損益、口數全部跟著調整
var ctSel = document.getElementById("ctSel");
try { var _sct = localStorage.getItem("ml_ct"); if (_sct && CONTRACTS[_sct]) window.__ct = _sct; } catch (e) {}
ctSel.value = window.__ct;
ctSel.addEventListener("change", function () {
  window.__ct = ctSel.value;
  try { localStorage.setItem("ml_ct", window.__ct); } catch (e) {}
  renderRisk(); renderPos();
});

// 區塊收放(點標題列切換,記憶狀態)
Array.prototype.forEach.call(document.querySelectorAll(".card"), function (card) {
  var title = card.querySelector(".card-title");
  var key = "ml_col_" + card.id;
  try { if (localStorage.getItem(key) === "1") card.classList.add("collapsed"); } catch (e) {}
  if (title) title.addEventListener("click", function () {
    card.classList.toggle("collapsed");
    try { localStorage.setItem(key, card.classList.contains("collapsed") ? "1" : "0"); } catch (e) {}
  });
});

render(curIdx);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

def main(output_path="index.html"):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=800)   # 足夠算季線斜率與年線
    print("抓取台指期（微台 MTX）%s ~ %s ..." % (start.isoformat(), end.isoformat()))
    bars = fetch_futures_daily("MTX", start.isoformat(), end.isoformat())
    print("  取得 %d 根日 K，最新 %s" % (len(bars), bars[-1]["date"]))

    history = build_history(bars, max_days=180)
    if not history:
        raise RuntimeError("資料不足，無法建立任何一天的判定。")

    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = generate_html(history, gen_time)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    latest = history[-1]
    print("完成：寫入 %s（%d 天）。最新 %s → %s（%d 口，方向 %d）"
          % (output_path, len(history), latest["date"], latest["headline"], latest["lots"], latest["dir"]))


if __name__ == "__main__":
    main()
