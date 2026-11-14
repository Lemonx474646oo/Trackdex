"""
بوت تليجرام لمتابعة عملة على TON (DexScreener) - مجاني
- لوحة تحكم بأزرار + جدول سعر 2500 و 10000 بالدولار والجنيه
- تحليل كل دقيقة + تقرير كل 15 دقيقة للمشتركين والقنوات
المتغيرات: TG_TOKEN (إجباري) / ADMIN_ID / TOKEN_ADDR / CHAIN
"""
import os, sys, time, math, json, threading, html, requests
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
from collections import deque

TG_TOKEN = os.environ["TG_TOKEN"]
ADMIN = int(os.getenv("ADMIN_ID") or 1430392777)
CHAIN = os.getenv("CHAIN", "ton")
TOKEN_ADDR = os.getenv("TOKEN_ADDR", "EQBvpKspmZ6Wk6Hc9VZptfHdh2HPoWFVTQi9oZEIcXl3_5kr")
HOLD = float(os.getenv("HOLD_AMOUNT", "2500"))
WITHDRAW = float(os.getenv("WITHDRAW_AMOUNT", "10000"))
EGP_FALLBACK = float(os.getenv("EGP_FALLBACK", "50"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "21000"))
STATE_FILE = "subs.json"
TG = f"https://api.telegram.org/bot{TG_TOKEN}"

lock = threading.Lock()
history = deque(maxlen=60)
pending, results = [], deque(maxlen=300)
last = {}
_cache = {"pair": (0, None), "rate": (0, None)}
START = time.time()


# ---------- الحالة ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("users", [ADMIN]); s.setdefault("channels", []); s.setdefault("muted", [])
    s.setdefault("token", TOKEN_ADDR); s.setdefault("chain", CHAIN)
    s.setdefault("hold", HOLD); s.setdefault("withdraw", WITHDRAW)
    s.setdefault("ch_every", 120); s.setdefault("ch_edit", False); s.setdefault("ch_msgs", {})
    return s

state = load_state()

def save_state():
    with lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)


# ---------- تليجرام ----------
def api(method, **params):
    try:
        return requests.post(f"{TG}/{method}", json=params, timeout=20).json()
    except Exception as e:
        print(method, "error", e)
        return {}

def kb(chat_id):
    muted = chat_id in state["muted"]
    rows = [
        [{"text": "💰 السعر الآن", "callback_data": "now"}],
        [{"text": "📊 التحليل", "callback_data": "an"}, {"text": "📈 الدقة", "callback_data": "acc"}],
        [{"text": "🔔 تشغيل التنبيهات" if muted else "🔕 إيقاف التنبيهات", "callback_data": "tog"}],
    ]
    if chat_id == ADMIN:
        rows.append([{"text": "👥 المشتركين", "callback_data": "users"},
                     {"text": "🪙 تغيير العملة", "callback_data": "tok"}])
    return {"inline_keyboard": rows}

def send(chat, text, keyboard=True):
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if keyboard and isinstance(chat, int) and chat > 0:
        p["reply_markup"] = kb(chat)
    d = api("sendMessage", **p)
    if d.get("error_code") == 403 and chat in state["users"]:
        state["users"].remove(chat); save_state()
    return d

def broadcast(text, channels=True):
    for uid in list(state["users"]):
        if uid not in state["muted"]:
            send(uid, text); time.sleep(0.05)
    if channels:
        for ch in state["channels"]:
            send(ch, text, keyboard=False); time.sleep(0.05)

def post_channels():
    """سعر دقيق لكل القنوات (رسالة جديدة أو تعديل نفس الرسالة)"""
    if not state["channels"]:
        return
    text = price_text()
    if last:
        text += f"\n\n{last['sig']} (قوة {last['score']:+.2f}) | شراء/بيع 5د: {last['buys5']}/{last['sells5']}"
    for ch in list(state["channels"]):
        key = str(ch)
        mid = state["ch_msgs"].get(key)
        if state["ch_edit"] and mid:
            d = api("editMessageText", chat_id=ch, message_id=mid, text=text, parse_mode="HTML",
                    disable_web_page_preview=True)
            if d.get("ok") or "not modified" in str(d.get("description", "")):
                continue
        d = send(ch, text, keyboard=False)
        if d.get("ok") and state["ch_edit"]:
            state["ch_msgs"][key] = d["result"]["message_id"]
        time.sleep(0.1)


# ---------- البيانات ----------
def get_pair(max_age=3):
    t, p = _cache["pair"]
    if p and time.time() - t < max_age:
        return p
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{state['token']}", timeout=10).json()
    allp = r.get("pairs") or []
    pairs = [x for x in allp if x.get("chainId") == state.get("chain")] if state.get("chain") else allp
    if not pairs:
        raise ValueError("pair not found")
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd", 0) or 0)
    _cache["pair"] = (time.time(), p)
    return p

def usd_egp():
    t, v = _cache["rate"]
    if v and time.time() - t < 600:
        return v
    try:
        v = float(requests.get("https://open.er-api.com/v6/latest/USD", timeout=10).json()["rates"]["EGP"])
        _cache["rate"] = (time.time(), v)
    except Exception:
        v = v or EGP_FALLBACK
    return v

def ratio(t):
    b, s = t.get("buys", 0), t.get("sells", 0)
    return (b - s) / (b + s) if (b + s) else 0.0

def analyze(p):
    tx, pc = p.get("txns", {}), p.get("priceChange", {})
    price = float(p["priceUsd"])
    history.append((time.time(), price))
    r5, r1h = ratio(tx.get("m5", {})), ratio(tx.get("h1", {}))
    pc5, pc1 = pc.get("m5", 0) or 0, pc.get("h1", 0) or 0
    own = 0.0
    if len(history) >= 5:
        base = history[max(0, len(history) - 10)][1]
        own = (price - base) / base * 100
    score = (0.35 * r5 + 0.15 * r1h + 0.2 * math.tanh(pc5 / 2)
             + 0.1 * math.tanh(pc1 / 5) + 0.2 * math.tanh(own / 1))
    sig = "📈 صعود" if score > 0.15 else "📉 هبوط" if score < -0.15 else "➖ عرضي"
    return dict(price=price, score=score, sig=sig, pc5=pc5, pc1=pc1,
                buys5=tx.get("m5", {}).get("buys", 0), sells5=tx.get("m5", {}).get("sells", 0))

def evaluate():
    now, price = time.time(), history[-1][1]
    for item in pending[:]:
        t, p0, sig = item
        if now - t >= 900:
            pending.remove(item)
            if "عرضي" in sig:
                continue
            results.append(price > p0 if "صعود" in sig else price < p0)

def accuracy():
    if not results:
        return "لسه مفيش توقعات متقيّمة"
    return f"{sum(results)}/{len(results)} = {sum(results) / len(results) * 100:.0f}%"


# ---------- الرسائل ----------
def fmt(x):
    return f"{x:,.2f}" if x >= 1 else f"{x:.6f}"

def price_text():
    p = get_pair(max_age=3)
    price = float(p["priceUsd"])
    rate = usd_egp()
    sym = html.escape((p.get("baseToken") or {}).get("symbol", "TOKEN"))
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    rows = ["Qty       USD          EGP",
            "-" * 31]
    for q in (state["hold"], state["withdraw"]):
        rows.append(f"{q:<9,.0f} ${fmt(q * price):<11} {fmt(q * price * rate)}")
    table = "\n".join(rows)
    return (f"💎 <b>{sym}</b>\n"
            f"السعر: <b>${price:.8g}</b>  ≈ {price * rate:.6g} ج.م\n\n"
            f"<pre>{table}</pre>\n"
            f"🔒 {state['hold']:,.0f} = الاحتفاظ المطلوب\n"
            f"💸 {state['withdraw']:,.0f} = أقل سحب\n"
            f"💵 الدولار = {rate:.2f} ج.م | سيولة: ${liq:,.0f}\n"
            f"🕒 {time.strftime('%H:%M:%S', time.gmtime())} UTC")

def analysis_text():
    if not last:
        return "⏳ لسه بجمع بيانات، جرّب بعد دقيقة."
    a = last
    return (f"{a['sig']}  (قوة {a['score']:+.2f})\n"
            f"شراء/بيع (5د): {a['buys5']}/{a['sells5']}\n"
            f"تغير 5د: {a['pc5']}% | ساعة: {a['pc1']}%\n"
            f"دقة التوقعات: {accuracy()}\n"
            f"⚠️ مش نصيحة مالية")

def full_report():
    return "📊 <b>تقرير كل 15 دقيقة</b>\n\n" + price_text() + "\n\n" + analysis_text()


# ---------- تغيير العملة ----------
def set_token(addr, chain=None):
    old = (state["token"], state.get("chain"))
    state["token"], state["chain"] = addr, chain
    _cache["pair"] = (0, None)
    try:
        p = get_pair(max_age=0)
    except Exception:
        state["token"], state["chain"] = old
        _cache["pair"] = (0, None)
        return None
    state["chain"] = chain or p.get("chainId")
    history.clear(); pending.clear(); results.clear(); last.clear()
    save_state()
    return p

def handle_token(cid, addr, chain=None):
    p = set_token(addr, chain)
    if not p:
        send(cid, "❌ ما لقيتش العملة دي على DexScreener. اتأكد من العنوان (والشبكة لو حددتها).")
        return
    bt = p.get("baseToken") or {}
    send(cid, f"✅ بقيت أتابع <b>{html.escape(bt.get('name', ''))}</b> "
              f"({html.escape(bt.get('symbol', ''))}) على {html.escape(p.get('chainId', ''))}\n\n" + price_text())


# ---------- الأوامر ----------
def link_channel(ch, title=""):
    if ch not in state["channels"]:
        state["channels"].append(ch)
        save_state()
    send(ch, "✅ تم ربط القناة. هتوصلها الأسعار تلقائياً.", keyboard=False)
    send(ADMIN, f"✅ اتربطت قناة: {html.escape(str(title))} <code>{ch}</code>")

def handle(u):
    cp = u.get("channel_post")
    if cp and (cp.get("text") or "").strip().lower().startswith("/link"):
        link_channel(cp["chat"]["id"], cp["chat"].get("title") or cp["chat"].get("username", ""))
        return
    if "callback_query" in u:
        cq = u["callback_query"]
        cid, mid, data = cq["message"]["chat"]["id"], cq["message"]["message_id"], cq["data"]
        api("answerCallbackQuery", callback_query_id=cq["id"])
        try:
            if data == "now":
                text = price_text()
            elif data == "an":
                text = analysis_text()
            elif data == "acc":
                text = f"📈 دقة التوقعات: {accuracy()}"
            elif data == "tog":
                if cid in state["muted"]:
                    state["muted"].remove(cid)
                else:
                    state["muted"].append(cid)
                save_state()
                text = "🔕 اتوقفت التنبيهات" if cid in state["muted"] else "🔔 اشتغلت التنبيهات"
            elif data == "tok" and cid == ADMIN:
                text = (f"🪙 العملة الحالية:\n<code>{html.escape(state['token'])}</code> ({state.get('chain')})\n\n"
                        "لتغييرها ابعتلي عنوان العملة مباشرة، أو:\n"
                        "<code>/settoken العنوان</code>\n<code>/settoken العنوان ton</code> (لتحديد الشبكة)")
            elif data == "users" and cid == ADMIN:
                text = f"👥 المشتركين: {len(state['users'])}\n📣 القنوات: {len(state['channels'])}"
            else:
                return
        except Exception as e:
            text = f"❌ خطأ في جلب السعر: {html.escape(str(e))}"
        api("editMessageText", chat_id=cid, message_id=mid, text=text, parse_mode="HTML",
            reply_markup=kb(cid))
        return

    m = u.get("message") or {}
    cid, txt = m.get("chat", {}).get("id"), (m.get("text") or "").strip()
    if not cid:
        return
    if m.get("chat", {}).get("type") in ("group", "supergroup"):
        first = txt.split()[0].split("@")[0].lower() if txt else ""
        if first == "/link" and (m.get("from") or {}).get("id") == ADMIN:
            link_channel(cid, m["chat"].get("title", ""))
        return
    if cid == ADMIN and txt and not txt.startswith("/") and " " not in txt and len(txt) >= 32:
        handle_token(cid, txt)
        return
    if not txt.startswith("/"):
        return
    cmd, _, arg = txt.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd == "/start":
        if cid not in state["users"]:
            state["users"].append(cid)
        if cid in state["muted"]:
            state["muted"].remove(cid)
        save_state()
        try:
            send(cid, "✅ أهلاً! هتوصلك تقارير كل 15 دقيقة.\n\n" + price_text())
        except Exception as e:
            send(cid, f"✅ اشتركت. (تعذّر جلب السعر الآن: {html.escape(str(e))})")
    elif cmd in ("/panel", "/price"):
        try:
            send(cid, price_text())
        except Exception as e:
            send(cid, f"❌ {html.escape(str(e))}")
    elif cmd == "/stop":
        if cid not in state["muted"]:
            state["muted"].append(cid); save_state()
        send(cid, "🔕 اتوقفت التنبيهات. /start لتشغيلها.")
    elif cid == ADMIN and cmd == "/settoken" and arg:
        parts = arg.split()
        handle_token(cid, parts[0], parts[1].lower() if len(parts) > 1 else None)
    elif cid == ADMIN and cmd == "/setamounts" and arg:
        try:
            h, w = [float(x) for x in arg.split()[:2]]
            state["hold"], state["withdraw"] = h, w
            save_state()
            send(cid, "✅ اتحدّثت الكميات\n\n" + price_text())
        except Exception:
            send(cid, "الصيغة: /setamounts 2500 10000")
    elif cid == ADMIN and cmd == "/broadcast" and arg:
        broadcast(html.escape(arg))
    elif cid == ADMIN and cmd == "/addchannel" and arg:
        ch = arg.strip()
        if ch.lstrip("-").isdigit():
            ch = int(ch)
        d = send(ch, "✅ تم ربط القناة. هتوصلها الأسعار تلقائياً.", keyboard=False)
        if d.get("ok"):
            if ch not in state["channels"]:
                state["channels"].append(ch); save_state()
            send(cid, f"✅ اتضافت القناة {html.escape(str(ch))}")
        else:
            send(cid, "❌ ما قدرتش أبعت للقناة: " + html.escape(str(d.get("description"))) +
                      "\nتأكد إن البوت أدمن وعنده صلاحية نشر الرسائل.")
    elif cid == ADMIN and cmd == "/interval" and arg:
        try:
            n = int(arg.split()[0])
            assert 60 <= n <= 3600
            state["ch_every"] = n; save_state()
            send(cid, f"✅ هيتبعت للقنوات كل {n} ثانية")
        except Exception:
            send(cid, "الصيغة: /interval 120 (بين 60 و 3600 ثانية)")
    elif cid == ADMIN and cmd == "/chmode" and arg.strip().lower() in ("edit", "new"):
        state["ch_edit"] = arg.strip().lower() == "edit"; state["ch_msgs"] = {}; save_state()
        send(cid, "✏️ هيعدّل نفس الرسالة كل مرة" if state["ch_edit"] else "🆕 هيبعت رسالة جديدة كل مرة")
    elif cid == ADMIN and cmd == "/delchannel" and arg:
        if arg.strip() in state["channels"]:
            state["channels"].remove(arg.strip()); save_state()
        send(cid, "🗑 اتحذفت")
    elif cid == ADMIN and cmd == "/users":
        send(cid, f"👥 {len(state['users'])} مشترك | 📣 {len(state['channels'])} قناة")

def updates_loop():
    offset = None
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35).json()
            if not r.get("ok"):
                print("getUpdates failed:", r)
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle(u)
                except Exception as e:
                    print("handle error", e)
        except Exception as e:
            print("updates error", e)
            time.sleep(3)


def main():
    print("bot started", flush=True)
    d = send(ADMIN, "🟢 البوت اشتغل (تشغيلة جديدة)", keyboard=False)
    print("start msg:", d.get("ok"), d.get("description"), flush=True)
    threading.Thread(target=updates_loop, daemon=True).start()
    last_an = last_rep = last_ch = 0
    while time.time() - START < RUN_SECONDS:
        now = time.time()
        try:
            if now - last_an >= 60:
                last_an = now
                a = analyze(get_pair(max_age=0))
                last.clear(); last.update(a)
                pending.append((now, a["price"], a["sig"]))
                evaluate()
            if now - last_rep >= 900 and last:
                last_rep = now
                broadcast(full_report(), channels=False)
            if now - last_ch >= state["ch_every"] and last:
                last_ch = now
                get_pair(max_age=0)
                post_channels()
        except Exception as e:
            print("loop error", e)
        time.sleep(5)
    save_state()


if __name__ == "__main__":
    main()
