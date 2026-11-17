#!/usr/bin/env python3
"""
بوت إدارة اشتراكات العملاء (تليجرام)
- الأونر: لوحة تحكم كاملة (موافقة/رفض، تسجيل دفع، تأجيل، إحصائيات، تصدير).
- العميل: /start ثم الرقم ثم الباقة، وبعد الموافقة يصله تذكير تلقائي بالدفع.

المتطلبات:
    pip install "python-telegram-bot[job-queue]>=21" python-dotenv

ملف .env:
    BOT_TOKEN=توكن_البوت
    OWNER_ID=رقم_الآيدي_بتاعك
    DB_FILE=clients.db   (اختياري)
"""
import asyncio
import csv
import html
import io
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= الإعدادات =================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())
except ValueError:
    OWNER_ID = 0

TZ = ZoneInfo("Africa/Cairo")
DB_FILE = os.getenv("DB_FILE") or ("/data/clients.db" if os.path.isdir("/data") else "clients.db")
DEFAULT_HOUR = 9
BILL_DAYS = (4, 16)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("client-bot")

PACKAGES = [
    ("30 جيجا / 4000 دقيقة", 450),
    ("40 جيجا / 4000 دقيقة", 530),
    ("50 جيجا / 5000 دقيقة", 580),
    ("60 جيجا / 6000 دقيقة", 630),
    ("70 جيجا / 7000 دقيقة", 680),
    ("85 جيجا / 8500 دقيقة", 730),
    ("نت مفتوح طول الشهر", 2000),
]

BTN_ADD = "➕ إضافة عميل"
BTN_LIST = "📋 العملاء"
BTN_UNPAID = "⏳ غير المدفوعين"
BTN_PENDING = "📩 الطلبات المعلقة"
BTN_STATS = "📊 الإحصائيات"
BTN_EXPORT = "📤 تصدير Excel"
BTN_HOUR = "🕘 وقت التذكير"
BTN_CANCEL = "❌ إلغاء"
BTN_PAID_YES = "✅ دفع"
BTN_PAID_NO = "⏳ لسه ما دفعش"

OWNER_BUTTONS = {BTN_ADD, BTN_LIST, BTN_UNPAID, BTN_PENDING, BTN_STATS, BTN_EXPORT, BTN_HOUR}

# ================= قاعدة البيانات =================
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row


def run(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.fetchall()


def insert(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def ensure_column(table, col, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        run(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


run(
    """CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT, package TEXT, price INTEGER, bill_day INTEGER,
        chat_id INTEGER DEFAULT 0, username TEXT DEFAULT '',
        paid_cycle TEXT DEFAULT '', snooze_until TEXT DEFAULT '',
        created_at TEXT DEFAULT '', paid_at TEXT DEFAULT '')"""
)
run(
    """CREATE TABLE IF NOT EXISTS pending_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, number TEXT, package_idx INTEGER,
        created_at TEXT DEFAULT '')"""
)
run(
    """CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, amount INTEGER, cycle TEXT, paid_at TEXT)"""
)
run("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
ensure_column("clients", "username", "TEXT DEFAULT ''")
ensure_column("clients", "snooze_until", "TEXT DEFAULT ''")
ensure_column("clients", "paid_at", "TEXT DEFAULT ''")


def get_setting(key, default=""):
    r = run("SELECT value FROM settings WHERE key=?", (key,))
    return r[0]["value"] if r else default


def set_setting(key, value):
    run(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def reminder_hour():
    try:
        h = int(get_setting("reminder_hour", str(DEFAULT_HOUR)))
        return h if 0 <= h <= 23 else DEFAULT_HOUR
    except ValueError:
        return DEFAULT_HOUR


# ================= أدوات مساعدة =================
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
PHONE_RE = re.compile(r"^01\d{9}$")


def is_owner(uid):
    return OWNER_ID != 0 and uid == OWNER_ID


def esc(x):
    return html.escape(str(x))


def now_local():
    return datetime.now(TZ)


def naive_iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def fmt_time(h, m):
    return f"{h % 12 or 12}:{m:02d} {'ص' if h < 12 else 'م'}"


def stamp_line():
    n = now_local()
    return f"🕒 {n:%Y-%m-%d} - {fmt_time(n.hour, n.minute)}"


def pkg_label(i):
    name, price = PACKAGES[i]
    return f"{name.split(' /')[0]} • {price} جنيه"


LABELS = {pkg_label(i): i for i in range(len(PACKAGES))}


def kb_rows(items, per_row=2):
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


MENU = ReplyKeyboardMarkup(
    [[BTN_ADD], [BTN_LIST, BTN_UNPAID], [BTN_PENDING, BTN_STATS], [BTN_EXPORT, BTN_HOUR]],
    resize_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
PKG_KB = ReplyKeyboardMarkup(
    kb_rows([pkg_label(i) for i in range(len(PACKAGES))]) + [[BTN_CANCEL]],
    resize_keyboard=True,
)
DAY_KB = ReplyKeyboardMarkup([[str(d) for d in BILL_DAYS], [BTN_CANCEL]], resize_keyboard=True)
PAID_KB = ReplyKeyboardMarkup([[BTN_PAID_YES, BTN_PAID_NO], [BTN_CANCEL]], resize_keyboard=True)


# ---- منطق الدورات (cycle) ----
def cycle_key(d, bill_day):
    """الدورة الحالية: شهر آخر يوم فاتورة عدّى."""
    if d.day >= bill_day:
        return d.strftime("%Y-%m")
    prev = d.replace(day=1) - timedelta(days=1)
    return prev.strftime("%Y-%m")


def due_date_of(key, bill_day):
    y, m = map(int, key.split("-"))
    return date(y, m, bill_day)


def next_due(d, bill_day):
    if d.day < bill_day:
        return date(d.year, d.month, bill_day)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, bill_day)


def is_unpaid(c, today):
    return (c["paid_cycle"] or "") != cycle_key(today, c["bill_day"])


def is_snoozed(c, today):
    s = c["snooze_until"] or ""
    if not s:
        return False
    try:
        return today < date.fromisoformat(s)
    except ValueError:
        return False


def get_client(cid):
    r = run("SELECT * FROM clients WHERE id=?", (cid,))
    return r[0] if r else None


def mark_paid(cid):
    c = get_client(cid)
    if not c:
        return None
    now = now_local()
    key = cycle_key(now.date(), c["bill_day"])
    run(
        "UPDATE clients SET paid_cycle=?, paid_at=?, snooze_until='' WHERE id=?",
        (key, naive_iso(now), cid),
    )
    insert(
        "INSERT INTO payments(client_id, amount, cycle, paid_at) VALUES(?,?,?,?)",
        (cid, c["price"], key, naive_iso(now)),
    )
    return get_client(cid)


def create_client(number, pkg_idx, bill_day, chat_id=0, username="", paid=False):
    name, price = PACKAGES[pkg_idx]
    cid = insert(
        "INSERT INTO clients(number, package, price, bill_day, chat_id, username, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (number, name, price, bill_day, chat_id, username, naive_iso(now_local())),
    )
    if paid:
        mark_paid(cid)
    return get_client(cid)


def client_text(c):
    today = now_local().date()
    unpaid = is_unpaid(c, today)
    due = next_due(today, c["bill_day"])
    cur_due = due_date_of(cycle_key(today, c["bill_day"]), c["bill_day"])
    lines = [
        f"📱 الرقم: <code>{esc(c['number'])}</code>",
        f"📦 الباقة: {esc(c['package'])}",
        f"💰 السعر: {c['price']} جنيه",
        f"📅 يوم الفاتورة: {c['bill_day']} من كل شهر",
    ]
    if c["username"]:
        lines.append(f"👤 {esc(c['username'])}")
    if unpaid:
        late = (today - cur_due).days
        lines.append("⏳ الحالة: <b>غير مدفوع</b>" + (f" (متأخر {late} يوم)" if late > 0 else ""))
    else:
        lines.append(f"✅ الحالة: <b>مدفوع</b> — الفاتورة الجاية {due:%Y-%m-%d}")
    if is_snoozed(c, today):
        lines.append(f"⏰ التذكير مؤجل لحد {c['snooze_until']}")
    if not c["chat_id"]:
        lines.append("⚠️ العميل ده مضاف يدوي (مش هيوصله تذكير على تليجرام)")
    return "\n".join(lines)


def client_markup(c):
    today = now_local().date()
    rows = []
    if is_unpaid(c, today):
        rows.append([Btn("✅ تسجيل دفع", callback_data=f"paid:{c['id']}")])
        rows.append([
            Btn("⏰ تأجيل يوم", callback_data=f"snz:{c['id']}"),
            Btn("📩 ذكّره الآن", callback_data=f"rem:{c['id']}"),
        ])
    rows.append([Btn("🗑 حذف العميل", callback_data=f"del:{c['id']}")])
    rows.append([Btn("⬅️ رجوع للقائمة", callback_data="pg:all:0")])
    return Markup(rows)


# ---- القوائم ----
PER_PAGE = 15


def fetch_rows(kind):
    rows = run("SELECT * FROM clients ORDER BY id DESC")
    if kind == "unpaid":
        today = now_local().date()
        rows = [c for c in rows if is_unpaid(c, today)]
    return rows


def list_view(kind, page):
    rows = fetch_rows(kind)
    total = len(rows)
    if total == 0:
        return ("لا يوجد عملاء حالياً." if kind == "all" else "🎉 كل العملاء دافعين."), None
    pages = (total + PER_PAGE - 1) // PER_PAGE
    page = max(0, min(page, pages - 1))
    today = now_local().date()
    btns = [
        [Btn(f"{'⏳' if is_unpaid(c, today) else '✅'} {c['number']} • {c['price']} جنيه",
             callback_data=f"cl:{c['id']}")]
        for c in rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    ]
    nav = []
    if page > 0:
        nav.append(Btn("◀️ السابق", callback_data=f"pg:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("التالي ▶️", callback_data=f"pg:{kind}:{page + 1}"))
    if nav:
        btns.append(nav)
    title = "📋 كل العملاء" if kind == "all" else "⏳ غير المدفوعين"
    return f"{title} ({total}) — صفحة {page + 1}/{pages}\nاضغط على عميل لإدارته:", Markup(btns)


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


async def notify(ctx, chat_id, text, markup=None):
    """إرسال آمن (لو العميل عامل بلوك مايوقفش البوت)."""
    if not chat_id:
        return False
    try:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except TelegramError as e:
        log.warning("could not message %s: %s", chat_id, e)
        return False


def reminder_text(c, today, heads_up=False):
    if heads_up:
        return (
            "🔔 <b>تنبيه بموعد التجديد</b>\n\n"
            f"📦 باقتك: {esc(c['package'])}\n"
            f"💰 المطلوب: {c['price']} جنيه\n"
            "📅 موعد التجديد <b>بكرة</b>. جهّز المبلغ من فضلك."
        )
    due = due_date_of(cycle_key(today, c["bill_day"]), c["bill_day"])
    late = (today - due).days
    status = "اليوم موعد السداد" if late <= 0 else f"متأخر {late} يوم"
    return (
        "🔔 <b>تذكير بدفع الاشتراك</b>\n\n"
        f"📱 الرقم: <code>{esc(c['number'])}</code>\n"
        f"📦 الباقة: {esc(c['package'])}\n"
        f"💰 المبلغ المطلوب: <b>{c['price']} جنيه</b>\n"
        f"📅 الاستحقاق: {due:%Y-%m-%d} ({status})\n\n"
        "بعد ما تدفع اضغط الزر تحت علشان الإدارة تأكد الاستلام 👇"
    )


def pay_markup(cid):
    return Markup([[Btn("💳 أنا دفعت", callback_data=f"cpaid:{cid}")]])


# ================= الأوامر =================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.user_data.clear()
    if is_owner(uid):
        await update.message.reply_text(
            f"أهلاً بيك يا فادي يا هندسة 👋\nأنت في لوحة التحكم الرئيسية.\n{stamp_line()}",
            reply_markup=MENU,
        )
        return
    mine = run("SELECT * FROM clients WHERE chat_id=?", (uid,))
    if mine:
        today = now_local().date()
        lines = ["👋 أهلاً بيك تاني! اشتراكاتك:\n"]
        for c in mine:
            st = "⏳ مطلوب دفعها" if is_unpaid(c, today) else "✅ مدفوعة"
            lines.append(f"📱 <code>{esc(c['number'])}</code> — {esc(c['package'])} — {c['price']} جنيه — {st}")
        lines.append("\nلطلب اشتراك جديد ابعت رقم الموبايل الآن:")
        ctx.user_data["step"] = "client_number"
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=CANCEL_KB
        )
        return
    ctx.user_data["step"] = "client_number"
    await update.message.reply_text(
        "🚀 <b>أهلاً بك في خدمة حجز الباقات والاشتراكات!</b>\n\n"
        "من فضلك اكتب رقم الموبايل (مثال: 01012345678):",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KB,
    )


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if is_owner(update.effective_user.id):
        await update.message.reply_text("تم الإلغاء.", reply_markup=MENU)
    else:
        await update.message.reply_text(
            "تم إلغاء العملية. ابعت /start للبدء من جديد.", reply_markup=ReplyKeyboardRemove()
        )


# ================= الرسائل النصية =================
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    text = update.message.text.strip().translate(AR_DIGITS)
    if text == BTN_CANCEL:
        await cancel(update, ctx)
        return
    if is_owner(uid):
        await owner_text(update, ctx, text)
    else:
        await customer_text(update, ctx, text)


# ---------- الأونر ----------
async def owner_text(update, ctx, text):
    msg = update.message
    ud = ctx.user_data

    if text in OWNER_BUTTONS:
        ud.clear()
        if text == BTN_ADD:
            ud["admin_step"] = "number"
            await msg.reply_text("أدخل رقم العميل:", reply_markup=CANCEL_KB)
        elif text in (BTN_LIST, BTN_UNPAID):
            body, markup = list_view("all" if text == BTN_LIST else "unpaid", 0)
            await msg.reply_text(body, reply_markup=markup)
        elif text == BTN_PENDING:
            await show_pending(msg)
        elif text == BTN_STATS:
            await msg.reply_text(stats_text(), parse_mode=ParseMode.HTML)
        elif text == BTN_EXPORT:
            await export_csv(msg)
        elif text == BTN_HOUR:
            await show_hours(msg)
        return

    step = ud.get("admin_step")
    if step == "number":
        if not PHONE_RE.match(text):
            await msg.reply_text("الرقم لازم يبدأ بـ 01 ويتكون من 11 رقم. جرّب تاني:")
            return
        ud["number"] = text
        ud["admin_step"] = "package"
        await msg.reply_text("اختر الباقة:", reply_markup=PKG_KB)
    elif step == "package":
        if text not in LABELS:
            await msg.reply_text("اختر باقة من الأزرار تحت.")
            return
        ud["pkg_idx"] = LABELS[text]
        ud["admin_step"] = "day"
        await msg.reply_text("اختر يوم الفاتورة:", reply_markup=DAY_KB)
    elif step == "day":
        if not text.isdigit() or int(text) not in BILL_DAYS:
            await msg.reply_text("اختر 4 أو 16 من الأزرار.")
            return
        ud["bill_day"] = int(text)
        ud["admin_step"] = "paid"
        await msg.reply_text("العميل دفع الفاتورة الحالية؟", reply_markup=PAID_KB)
    elif step == "paid":
        if text not in (BTN_PAID_YES, BTN_PAID_NO):
            await msg.reply_text("اختر من الأزرار.")
            return
        c = create_client(ud["number"], ud["pkg_idx"], ud["bill_day"], paid=(text == BTN_PAID_YES))
        ud.clear()
        await msg.reply_text("✅ تمت إضافة العميل\n\n" + client_text(c) + f"\n{stamp_line()}",
                             parse_mode=ParseMode.HTML, reply_markup=MENU)
    else:
        await msg.reply_text("اختر من القائمة 👇", reply_markup=MENU)


async def show_pending(msg):
    reqs = run("SELECT * FROM pending_requests ORDER BY id")
    if not reqs:
        await msg.reply_text("لا توجد طلبات معلقة ✅")
        return
    for r in reqs:
        name, price = PACKAGES[r["package_idx"]]
        await msg.reply_text(
            f"🚨 <b>طلب اشتراك</b>\n👤 {esc(r['username'])} (ID: <code>{r['user_id']}</code>)\n"
            f"📱 <code>{esc(r['number'])}</code>\n📦 {esc(name)} ({price} جنيه)",
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([[
                Btn("✅ موافقة", callback_data=f"apr:{r['id']}"),
                Btn("❌ رفض", callback_data=f"rej:{r['id']}"),
            ]]),
        )


def stats_text():
    today = now_local().date()
    rows = run("SELECT * FROM clients")
    unpaid = [c for c in rows if is_unpaid(c, today)]
    owed = sum(c["price"] for c in unpaid)
    expected = sum(c["price"] for c in rows)
    month = today.strftime("%Y-%m")
    got = run("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE substr(paid_at,1,7)=?", (month,))[0]["s"]
    pend = run("SELECT COUNT(*) n FROM pending_requests")[0]["n"]
    return (
        "📊 <b>الإحصائيات</b>\n\n"
        f"👥 العملاء: {len(rows)}\n"
        f"✅ مدفوع: {len(rows) - len(unpaid)}\n"
        f"⏳ غير مدفوع: {len(unpaid)}\n"
        f"📩 طلبات معلقة: {pend}\n\n"
        f"💵 مستحق تحصيله: {owed} جنيه\n"
        f"💰 اتحصّل هذا الشهر: {got} جنيه\n"
        f"📈 إجمالي الاشتراكات الشهرية: {expected} جنيه\n"
        f"🕘 وقت التذكير: {fmt_time(reminder_hour(), 0)}\n{stamp_line()}"
    )


async def export_csv(msg):
    rows = run("SELECT * FROM clients ORDER BY id")
    today = now_local().date()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "الرقم", "الباقة", "السعر", "يوم الفاتورة", "الحالة", "آخر دفع", "حساب تليجرام"])
    for c in rows:
        w.writerow([
            c["id"], c["number"], c["package"], c["price"], c["bill_day"],
            "غير مدفوع" if is_unpaid(c, today) else "مدفوع",
            c["paid_at"] or "", c["username"] or "",
        ])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # utf-8-sig علشان Excel يقرأ العربي
    data.name = f"clients_{today:%Y-%m-%d}.csv"
    await msg.reply_document(data, caption=f"📤 {len(rows)} عميل\n{stamp_line()}")


async def show_hours(msg):
    cur = reminder_hour()
    hours = list(range(6, 24))
    btns = [
        [Btn(("✅ " if h == cur else "") + fmt_time(h, 0), callback_data=f"hr:{h}") for h in hours[i:i + 3]]
        for i in range(0, len(hours), 3)
    ]
    await msg.reply_text(
        f"🕘 وقت التذكير الحالي: {fmt_time(cur, 0)} (بتوقيت القاهرة)\nاختر الوقت الجديد:",
        reply_markup=Markup(btns),
    )


# ---------- العميل ----------
async def customer_text(update, ctx, text):
    msg = update.message
    ud = ctx.user_data
    uid = update.effective_user.id
    step = ud.get("step")

    if step == "client_number":
        if not PHONE_RE.match(text):
            await msg.reply_text("الرقم لازم يبدأ بـ 01 ويتكون من 11 رقم. جرّب تاني:")
            return
        ud["client_number"] = text
        ud["step"] = "client_package"
        await msg.reply_text("تمام! اختر الباقة من الأزرار 👇", reply_markup=PKG_KB)

    elif step == "client_package":
        if text not in LABELS:
            await msg.reply_text("من فضلك اختر باقة من الأزرار الموجودة تحت.")
            return
        idx = LABELS[text]
        number = ud["client_number"]
        user = update.effective_user
        username = f"@{user.username}" if user.username else (user.full_name or f"ID:{uid}")

        dup = run("SELECT id FROM pending_requests WHERE user_id=? AND number=?", (uid, number))
        if dup:
            ud.clear()
            await msg.reply_text("طلبك لسه قيد المراجعة، استنى رد الإدارة 🙏", reply_markup=ReplyKeyboardRemove())
            return

        req_id = insert(
            "INSERT INTO pending_requests(user_id, username, number, package_idx, created_at) VALUES(?,?,?,?,?)",
            (uid, username, number, idx, naive_iso(now_local())),
        )
        ud.clear()
        await msg.reply_text(
            "⏳ <b>تم إرسال طلب اشتراكك!</b>\nجاري مراجعته من الإدارة وهيوصلك إشعار فور الرد.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        name, price = PACKAGES[idx]
        await notify(
            ctx, OWNER_ID,
            f"🚨 <b>طلب اشتراك جديد!</b>\n\n"
            f"👤 {esc(username)} (ID: <code>{uid}</code>)\n"
            f"📱 الرقم: <code>{esc(number)}</code>\n"
            f"📦 الباقة: {esc(name)} ({price} جنيه)\n{stamp_line()}",
            Markup([[
                Btn("✅ موافقة", callback_data=f"apr:{req_id}"),
                Btn("❌ رفض", callback_data=f"rej:{req_id}"),
            ]]),
        )
    else:
        await msg.reply_text("ابعت /start لبدء طلب اشتراك جديد.")


# ================= الأزرار =================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    parts = (q.data or "").split(":")
    act = parts[0]

    try:
        # ---- زر العميل: "أنا دفعت" ----
        if act == "cpaid":
            c = get_client(int(parts[1]))
            if not c or c["chat_id"] != uid:
                await q.answer("غير متاح", show_alert=True)
                return
            await q.answer("تم إبلاغ الإدارة ✅", show_alert=True)
            await notify(
                ctx, OWNER_ID,
                f"💳 <b>العميل بيقول إنه دفع</b>\n\n{client_text(c)}\n{stamp_line()}",
                Markup([[Btn("✅ تأكيد الاستلام", callback_data=f"paid:{c['id']}")]]),
            )
            return

        # ---- كل ما بعد كده للأونر فقط ----
        if not is_owner(uid):
            await q.answer("غير مسموح", show_alert=True)
            return
        await q.answer()

        if act == "apr":
            req = run("SELECT * FROM pending_requests WHERE id=?", (int(parts[1]),))
            if not req:
                await safe_edit(q, "❌ الطلب غير موجود أو تم معالجته.")
                return
            rid = req[0]["id"]
            await safe_edit(
                q,
                f"📱 <code>{esc(req[0]['number'])}</code>\nاختر يوم الفاتورة:",
                Markup([[Btn(f"يوم {d}", callback_data=f"day:{rid}:{d}") for d in BILL_DAYS]]),
            )

        elif act == "day":
            rid, d = int(parts[1]), int(parts[2])
            if d not in BILL_DAYS:
                return
            await safe_edit(
                q,
                f"يوم الفاتورة: {d}\nالعميل دفع الفاتورة الحالية؟",
                Markup([[
                    Btn(BTN_PAID_YES, callback_data=f"fin:{rid}:{d}:1"),
                    Btn(BTN_PAID_NO, callback_data=f"fin:{rid}:{d}:0"),
                ]]),
            )

        elif act == "fin":
            rid, d, paid = int(parts[1]), int(parts[2]), parts[3] == "1"
            req = run("SELECT * FROM pending_requests WHERE id=?", (rid,))
            if not req or d not in BILL_DAYS:
                await safe_edit(q, "❌ الطلب غير موجود أو تم معالجته.")
                return
            r = req[0]
            c = create_client(r["number"], r["package_idx"], d, r["user_id"], r["username"], paid)
            run("DELETE FROM pending_requests WHERE id=?", (rid,))
            await safe_edit(q, "✅ <b>تم تفعيل العميل</b>\n\n" + client_text(c) + f"\n{stamp_line()}")
            tail = "✅ تم تسجيل دفعك." if paid else "هيوصلك تذكير بالمبلغ المطلوب في الموعد."
            await notify(
                ctx, r["user_id"],
                "🎉 <b>تم قبول اشتراكك وتفعيله!</b>\n\n"
                f"📦 الباقة: {esc(c['package'])}\n💰 السعر: {c['price']} جنيه\n"
                f"📅 يوم الفاتورة: {d} من كل شهر\n{tail}",
                None if paid else pay_markup(c["id"]),
            )

        elif act == "rej":
            req = run("SELECT * FROM pending_requests WHERE id=?", (int(parts[1]),))
            if not req:
                await safe_edit(q, "❌ الطلب غير موجود أو تم معالجته.")
                return
            r = req[0]
            run("DELETE FROM pending_requests WHERE id=?", (r["id"],))
            await safe_edit(q, f"❌ تم رفض الطلب: <code>{esc(r['number'])}</code>\n{stamp_line()}")
            await notify(ctx, r["user_id"], "عذراً، تم رفض طلب الاشتراك من الإدارة.")

        elif act == "cl":
            c = get_client(int(parts[1]))
            if not c:
                await safe_edit(q, "العميل غير موجود.")
                return
            await safe_edit(q, client_text(c), client_markup(c))

        elif act == "pg":
            body, markup = list_view(parts[1], int(parts[2]))
            try:
                await q.edit_message_text(body, reply_markup=markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise

        elif act == "paid":
            c = get_client(int(parts[1]))
            if not c:
                await safe_edit(q, "العميل غير موجود.")
                return
            if not is_unpaid(c, now_local().date()):
                await safe_edit(q, "✅ العميل مدفوع بالفعل.\n\n" + client_text(c), client_markup(c))
                return
            c = mark_paid(c["id"])
            await safe_edit(q, f"✅ <b>تم تسجيل الدفع</b>\n\n{client_text(c)}\n{stamp_line()}", client_markup(c))
            await notify(
                ctx, c["chat_id"],
                f"✅ <b>تم استلام دفعك</b>\n💰 {c['price']} جنيه — {esc(c['package'])}\nشكراً لك 🌹",
            )

        elif act == "snz":
            c = get_client(int(parts[1]))
            if not c:
                return
            until = (now_local().date() + timedelta(days=1)).isoformat()
            run("UPDATE clients SET snooze_until=? WHERE id=?", (until, c["id"]))
            c = get_client(c["id"])
            await safe_edit(q, f"⏰ تم تأجيل التذكير ليوم {until}\n\n{client_text(c)}", client_markup(c))

        elif act == "rem":
            c = get_client(int(parts[1]))
            if not c:
                return
            ok = await notify(ctx, c["chat_id"], reminder_text(c, now_local().date()), pay_markup(c["id"]))
            await q.answer("تم إرسال التذكير ✅" if ok else "تعذر الإرسال (العميل مش مسجل أو عامل بلوك)", show_alert=True)

        elif act == "del":
            c = get_client(int(parts[1]))
            if not c:
                return
            await safe_edit(
                q, f"⚠️ متأكد إنك عايز تحذف العميل <code>{esc(c['number'])}</code>؟",
                Markup([[Btn("🗑 نعم احذف", callback_data=f"delok:{c['id']}"),
                         Btn("↩️ لا", callback_data=f"cl:{c['id']}")]]),
            )

        elif act == "delok":
            run("DELETE FROM clients WHERE id=?", (int(parts[1]),))
            await safe_edit(q, f"🗑 تم حذف العميل.\n{stamp_line()}")

        elif act == "hr":
            h = int(parts[1])
            if 0 <= h <= 23:
                set_setting("reminder_hour", h)
                set_setting("last_run", "")  # يسمح بتشغيل جديد اليوم لو الوقت لسه ماجاش
                await safe_edit(q, f"✅ تم ضبط وقت التذكير اليومي على {fmt_time(h, 0)} (القاهرة)\n{stamp_line()}")

    except (ValueError, IndexError):
        log.warning("bad callback data: %s", q.data)
    except Exception:
        log.exception("callback error")
        try:
            await q.answer("حصل خطأ، حاول تاني", show_alert=True)
        except TelegramError:
            pass


# ================= التذكيرات التلقائية =================
async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    today = now_local().date()
    rows = run("SELECT * FROM clients ORDER BY id")
    unpaid_list = []
    sent = 0
    for c in rows:
        try:
            if is_unpaid(c, today):
                unpaid_list.append(c)
                if is_snoozed(c, today):
                    continue
                if await notify(ctx, c["chat_id"], reminder_text(c, today), pay_markup(c["id"])):
                    sent += 1
            elif next_due(today, c["bill_day"]) == today + timedelta(days=1):
                if await notify(ctx, c["chat_id"], reminder_text(c, today, heads_up=True)):
                    sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            log.exception("reminder failed for client %s", c["id"])

    if OWNER_ID:
        if unpaid_list:
            total = sum(c["price"] for c in unpaid_list)
            btns = [
                [Btn(f"✅ {c['number']} • {c['price']}", callback_data=f"paid:{c['id']}")]
                for c in unpaid_list[:40]
            ]
            await notify(
                ctx, OWNER_ID,
                f"🔔 <b>ملخص اليوم</b>\n⏳ غير مدفوع: {len(unpaid_list)} عميل — {total} جنيه\n"
                f"📨 تذكيرات اتبعتت: {sent}\n\nاضغط على عميل لتسجيل دفعه:\n{stamp_line()}",
                Markup(btns),
            )
        else:
            await notify(ctx, OWNER_ID, f"🎉 كل العملاء دافعين.\n{stamp_line()}")


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    """بيشتغل كل دقيقة ويطلق التذكير مرة واحدة يومياً عند وقت التذكير."""
    now = now_local()
    today = now.date().isoformat()
    if now.hour >= reminder_hour() and get_setting("last_run") != today:
        set_setting("last_run", today)
        log.info("running daily reminders")
        await send_reminders(ctx)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


async def post_init(app: Application):
    if OWNER_ID:
        try:
            await app.bot.send_message(OWNER_ID, f"🤖 البوت اشتغل.\n{stamp_line()}", reply_markup=MENU)
        except TelegramError as e:
            log.warning("Owner has not started the bot yet: %s", e)


# ================= التشغيل =================
def main():
    if not TOKEN:
        raise SystemExit("❌ ضع BOT_TOKEN في ملف .env")
    if OWNER_ID == 0:
        raise SystemExit("❌ ضع OWNER_ID (رقم الآيدي بتاعك) في ملف .env")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if app.job_queue is None:
        raise SystemExit('❌ ثبّت: pip install "python-telegram-bot[job-queue]"')
    app.job_queue.run_repeating(tick, interval=60, first=10)

    print("🤖 البوت شغال...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
