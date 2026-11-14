import asyncio
import calendar
import csv
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
    Update,
)
from telegram.constants import ParseMode
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
TOKEN = "9065174452:AAHROsaMzGQ5ZPHjwBUiO10mgA5T3HGSuTY"
OWNER_ID = "8949099540"
TZ = ZoneInfo("Africa/Cairo")
DB_FILE = os.getenv("DB_FILE", "clients.db")
REMINDER_HOUR_DEFAULT = 9

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
BTN_HOUR = "🕘 وقت التذكير"
BTN_STATS = "📊 الإحصائيات"
BTN_EXPORT = "📤 تصدير Excel"
BTN_CANCEL = "❌ إلغاء"

MENU = ReplyKeyboardMarkup(
    [[BTN_ADD], [BTN_LIST, BTN_UNPAID], [BTN_STATS, BTN_EXPORT], [BTN_HOUR]],
    resize_keyboard=True,
)

# ================= قاعدة البيانات =================
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row

def run(sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.fetchall()

run(
    """CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT, package TEXT, price INTEGER, bill_day INTEGER,
        chat_id INTEGER DEFAULT 0,
        paid_cycle TEXT DEFAULT '', snooze_until TEXT DEFAULT '',
        created_at TEXT DEFAULT '', paid_at TEXT DEFAULT '')"""
)
run(
    """CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, amount INTEGER,
        cycle TEXT, paid_at TEXT,
        FOREIGN KEY(client_id) REFERENCES clients(id))"""
)
run("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")

for _col, _type in [
    ("created_at", "TEXT DEFAULT ''"),
    ("paid_at", "TEXT DEFAULT ''"),
    ("chat_id", "INTEGER DEFAULT 0"),
]:
    try:
        run(f"ALTER TABLE clients ADD COLUMN {_col} {_type}")
    except sqlite3.OperationalError:
        pass

def get_setting(k):
    r = run("SELECT v FROM settings WHERE k=?", (k,))
    return r[0]["v"] if r else None

def set_setting(k, v):
    run("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, v))

def owner_id():
    if OWNER_ID:
        return OWNER_ID
    v = get_setting("owner")
    return int(v) if v else None

async def allowed(update: Update) -> bool:
    uid = update.effective_user.id
    owner = owner_id()
    if owner is None:
        set_setting("owner", str(uid))
        return True
    return uid == owner

# ================= أدوات =================
def now_local():
    return datetime.now(TZ)

def cycle(now):
    return now.strftime("%Y-%m")

def naive_iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")

def fmt_time(h, m):
    ampm = "ص" if h < 12 else "م"
    return f"{h % 12 or 12}:{m:02d} {ampm}"

def stamp_line():
    n = now_local()
    return f"🕒 {n:%Y-%m-%d} - {fmt_time(n.hour, n.minute)}"

def parse_time(text):
    t = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).lower()
    nums = re.findall(r"\d+", t)
    if not nums:
        return None
    h = int(nums[0])
    m = int(nums[1]) if len(nums) > 1 else 0
    if "م" in t or "pm" in t:
        if h < 12:
            h += 12
    if ("ص" in t or "am" in t) and h == 12:
        h = 0
    if h > 23 or m > 59:
        return None
    return h, m

def date_for_day(n, today):
    y, mth = today.year, today.month
    for _ in range(14):
        last = calendar.monthrange(y, mth)[1]
        if n <= last:
            d = today.replace(year=y, month=mth, day=n)
            if d >= today:
                return d
        mth += 1
        if mth > 12:
            mth, y = 1, y + 1
    return None

def card(c):
    return (
        f"👤 *العميل:* {c['number']}\n"
        f"📦 *الباقة:* {c['package']}\n"
        f"💰 *السعر:* {c['price']} جنيه\n"
        f"📅 *يوم المحاسبة:* {c['bill_day']}"
    )

def reminder_kb(cid):
    return Markup(
        [
            [Btn("✅ تم الدفع", callback_data=f"paid:{cid}")],
            [
                Btn("⏰ بعد ساعة", callback_data=f"snz:{cid}:h"),
                Btn("📅 بعد يوم", callback_data=f"snz:{cid}:d"),
            ],
        ]
    )

def card_cmds(c):
    return (
        card(c)
        + f"\n\n✅ تم الدفع: `/paid_{c['id']}`"
        + f"\n⏰ بعد ساعة: `/h_{c['id']}`"
        + f"\n📅 بعد يوم: `/d_{c['id']}`"
        + f"\n🕒 يوم وساعة تانية: `/r_{c['id']}`"
    )

async def send_cards(bot, chat_id, rows):
    for c in rows:
        try:
            await bot.send_message(
                chat_id, card_cmds(c),
                reply_markup=reminder_kb(c["id"]),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning(f"send_cards failed for client {c['id']}: {e}")

# ================= الأوامر الأساسية =================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    await update.message.reply_text(
        "أهلاً بيك يا صاحبي 👋\nاختار من القايمة تحت:",
        reply_markup=MENU,
    )

async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    cyc = cycle(now_local())
    rows = run("SELECT * FROM clients ORDER BY bill_day, number")
    if not rows:
        await update.message.reply_text("مفيش عملاء لسه.")
        return
    lines = []
    for day in sorted({c["bill_day"] for c in rows}):
        group = [c for c in rows if c["bill_day"] == day]
        if group:
            lines.append(f"━━ *يوم {day}* ━━")
            for c in group:
                mark = "✅" if c["paid_cycle"] == cyc else "⏳"
                lines.append(f"{mark} {c['number']} | {c['package']} | {c['price']} ج")
    lines.append("\nلحذف عميل: `/del رقم_العميل`")
    await update.message.reply_text(
        stamp_line() + "\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

async def show_unpaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    rows = run(
        "SELECT * FROM clients WHERE paid_cycle!=? ORDER BY bill_day",
        (cycle(now_local()),),
    )
    if not rows:
        await update.message.reply_text("كله دافع ✅")
        return
    await update.message.reply_text(
        f"⏳ لسه ما دفعوش: *{len(rows)}* عميل\n{stamp_line()}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await send_cards(ctx.bot, update.effective_chat.id, rows)

async def delete_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    if not ctx.args:
        await update.message.reply_text("اكتب: `/del رقم_العميل`", parse_mode=ParseMode.MARKDOWN)
        return
    run("DELETE FROM clients WHERE number=?", (ctx.args[0],))
    await update.message.reply_text("اتحذف ✅")

# ================= إضافة عميل =================
MENU_RE = f"^({BTN_ADD}|{BTN_LIST}|{BTN_UNPAID}|{BTN_HOUR}|{BTN_STATS}|{BTN_EXPORT})$"
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

def pkg_label(i):
    n, p = PACKAGES[i]
    short = n.split(" /")[0]
    return f"{short} • {p} جنيه"

def kb_rows(items, per_row=2):
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]

CANCEL_KB = ReplyKeyboardMarkup(
    [[BTN_CANCEL]], resize_keyboard=True, is_persistent=True,
    input_field_placeholder="اكتب رقم العميل أو @username",
)
PKG_KB = ReplyKeyboardMarkup(
    kb_rows([pkg_label(i) for i in range(len(PACKAGES))]) + [[BTN_CANCEL]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="اختار الباقة",
)
DAY_KB = ReplyKeyboardMarkup(
    [["يوم 4", "يوم 16"], [BTN_CANCEL]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="اختار اليوم",
)

def find_pkg(text):
    t = text.translate(AR_DIGITS).strip()
    for i in range(len(PACKAGES)):
        if pkg_label(i) in t or PACKAGES[i][0] in t:
            return i
    nums = re.findall(r"\d+", t)
    if nums:
        n = int(nums[0])
        for i, (_, price) in enumerate(PACKAGES):
            if price == n:
                return i
        if 1 <= n <= len(PACKAGES):
            return n - 1
    return None

def find_day(text):
    for n in re.findall(r"\d+", text.translate(AR_DIGITS)):
        if 1 <= int(n) <= 31:
            return int(n)
    return None

def packages_text():
    return "اختار الباقة من الأزرار اللي تحت 👇"

async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    ctx.user_data["step"] = "number"
    await update.message.reply_text(
        "ابعت رقم العميل (أو @username لو تعرفه):",
        reply_markup=CANCEL_KB,
    )

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("اتلغى.", reply_markup=MENU)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    step = ctx.user_data.get("step")
    if not step:
        return
    text = update.message.text.strip()
    if "إلغاء" in text:
        await cancel(update, ctx)
        return

    if step in ("rem_day", "rem_time", "set_time"):
        await extra_steps(update, ctx, step, text)
        return

    if step == "number":
        ctx.user_data["number"] = text.translate(AR_DIGITS)
        ctx.user_data["step"] = "package"
        await update.message.reply_text(packages_text(), reply_markup=PKG_KB)

    elif step == "package":
        i = find_pkg(text)
        if i is None:
            await update.message.reply_text("مش فاهم الباقة. اضغط على واحدة من الأزرار 👇")
            return
        ctx.user_data["pkg"] = i
        ctx.user_data["step"] = "day"
        name, price = PACKAGES[i]
        await update.message.reply_text(
            f"📦 {name} - {price} جنيه\nاختار يوم المحاسبة:",
            reply_markup=DAY_KB,
        )

    elif step == "day":
        day = find_day(text)
        if day is None:
            await update.message.reply_text("اختار يوم 4 أو 16، أو رقم من 1 لـ 31.")
            return
        name, price = PACKAGES[ctx.user_data["pkg"]]
        number = ctx.user_data["number"]
        run(
            "INSERT INTO clients(number, package, price, bill_day, created_at) VALUES(?,?,?,?,?)",
            (number, name, price, day, naive_iso(now_local())),
        )
        ctx.user_data.clear()
        await update.message.reply_text(
            f"اتضاف ✅\n👤 {number}\n📦 {name}\n💰 {price} جنيه\n📅 يوم {day}\n{stamp_line()}",
            reply_markup=MENU,
        )

# ================= تذكير =================
TIME_KB = ReplyKeyboardMarkup(
    [["9:00 ص", "12:00 م"], ["3:00 م", "6:00 م"], ["9:00 م"], [BTN_CANCEL]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="أو اكتب الساعة زي 7:30 م",
)
REM_DAY_KB = ReplyKeyboardMarkup(
    [["النهاردة", "بكرة"], ["بعد يومين", "بعد 3 أيام"], [BTN_CANCEL]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="أو اكتب رقم اليوم",
)
DAY_OFFSETS = {"النهاردة": 0, "بكرة": 1, "بعد يومين": 2, "بعد 3 أيام": 3}

def reminder_minutes():
    v = get_setting("reminder_min")
    return int(v) if v else REMINDER_HOUR_DEFAULT * 60

async def hour_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    ctx.user_data["step"] = "set_time"
    m = reminder_minutes()
    await update.message.reply_text(
        f"🕘 التذكير الشهري دلوقتي {fmt_time(m // 60, m % 60)}\n"
        f"{stamp_line()}\nاختار الساعة الجديدة:",
        reply_markup=TIME_KB,
    )

async def extra_steps(update: Update, ctx: ContextTypes.DEFAULT_TYPE, step, text):
    now = now_local()
    t = text.translate(AR_DIGITS).strip()

    if step == "rem_day":
        day = None
        if t in DAY_OFFSETS:
            day = now.date() + timedelta(days=DAY_OFFSETS[t])
        else:
            nums = re.findall(r"\d+", t)
            if nums and 1 <= int(nums[0]) <= 31:
                day = date_for_day(int(nums[0]), now.date())
        if day is None:
            await update.message.reply_text("اختار من الأزرار أو رقم (1 لـ 31).")
            return
        ctx.user_data["rem_date"] = day.isoformat()
        ctx.user_data["step"] = "rem_time"
        await update.message.reply_text(
            f"📅 {day.isoformat()}\nالساعة كام؟",
            reply_markup=TIME_KB,
        )

    elif step == "rem_time":
        hm = parse_time(text)
        if hm is None:
            await update.message.reply_text("اكتب الساعة زي 7:30 م.")
            return
        d = date.fromisoformat(ctx.user_data["rem_date"])
        target = datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=TZ)
        if target <= now:
            await update.message.reply_text("الوقت عدّى، اختار ساعة جاية.")
            return
        cid = ctx.user_data["rem_cid"]
        run("UPDATE clients SET snooze_until=? WHERE id=?", (naive_iso(target), cid))
        ctx.user_data.clear()
        await update.message.reply_text(
            f"⏰ هفكرك يوم {d.isoformat()} الساعة {fmt_time(*hm)}\n{stamp_line()}",
            reply_markup=MENU,
        )

    elif step == "set_time":
        hm = parse_time(text)
        if hm is None:
            await update.message.reply_text("اكتب الساعة زي 9:00 ص.")
            return
        set_setting("reminder_min", str(hm[0] * 60 + hm[1]))
        ctx.user_data.clear()
        await update.message.reply_text(
            f"تمام ✅ التذكير الشهري هيوصل الساعة {fmt_time(*hm)} (بتوقيت القاهرة)\n{stamp_line()}",
            reply_markup=MENU,
        )

async def on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    m = re.match(r"^/(paid|h|d|r)_(\d+)", update.message.text)
    action, cid = m.group(1), int(m.group(2))
    rows = run("SELECT * FROM clients WHERE id=?", (cid,))
    if not rows:
        await update.message.reply_text("العميل ده اتحذف.")
        return
    c = rows[0]
    if action == "paid":
        mark_paid(cid, c)
        await update.message.reply_text(f"✅ تم الدفع: {c['number']}\n{stamp_line()}")
    elif action == "r":
        ctx.user_data.clear()
        ctx.user_data["step"] = "rem_day"
        ctx.user_data["rem_cid"] = cid
        await update.message.reply_text(
            f"🕒 العميل {c['number']}\n{stamp_line()}\nيوم كام تفكرك؟",
            reply_markup=REM_DAY_KB,
        )
    else:
        delta = timedelta(hours=1) if action == "h" else timedelta(days=1)
        run(
            "UPDATE clients SET snooze_until=? WHERE id=?",
            (naive_iso(now_local() + delta), cid),
        )
        label = "بعد ساعة" if action == "h" else "بعد يوم"
        await update.message.reply_text(f"⏰ هفكرك {label}: {c['number']}\n{stamp_line()}")

def mark_paid(cid, c):
    now = now_local()
    cyc = cycle(now)
    run(
        "UPDATE clients SET paid_cycle=?, paid_at=?, snooze_until='' WHERE id=?",
        (cyc, naive_iso(now), cid),
    )
    run(
        "INSERT INTO payments(client_id, amount, cycle, paid_at) VALUES(?,?,?,?)",
        (cid, c["price"], cyc, naive_iso(now)),
    )

# ================= إحصائيات =================
async def show_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    cyc = cycle(now_local())
    all_rows = run("SELECT * FROM clients")
    total = len(all_rows)
    paid = [c for c in all_rows if c["paid_cycle"] == cyc]
    unpaid = total - len(paid)
    expected = sum(c["price"] for c in all_rows)
    collected = sum(c["price"] for c in paid)
    pending = expected - collected
    total_paid = run("SELECT COALESCE(SUM(amount),0) AS s FROM payments")[0]["s"]

    msg = (
        f"📊 *إحصائيات الشهر {cyc}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"👥 إجمالي العملاء: *{total}*\n"
        f"✅ دفعوا: *{len(paid)}*\n"
        f"⏳ لم يدفعوا: *{unpaid}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 المتوقع: *{expected:,}* جنيه\n"
        f"💵 المحصّل: *{collected:,}* جنيه\n"
        f"🔴 المتأخر: *{pending:,}* جنيه\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 إجمالي المدفوعات: *{total_paid:,}* جنيه\n"
        f"\n{stamp_line()}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ================= تصدير =================
async def export_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    rows = run("SELECT * FROM clients ORDER BY bill_day, number")
    if not rows:
        await update.message.reply_text("مفيش بيانات.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Number", "Package", "Price", "BillDay", "PaidCycle", "Created", "PaidAt"])
    for c in rows:
        writer.writerow([c["id"], c["number"], c["package"], c["price"], c["bill_day"], c["paid_cycle"], c["created_at"], c["paid_at"]])
    data = buf.getvalue().encode("utf-8-sig")
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"clients_{now_local():%Y%m%d}.csv",
        caption=f"📤 تصدير {len(rows)} عميل\n{stamp_line()}",
    )

# ================= أزرار التذكير =================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    cid = int(parts[1])
    rows = run("SELECT * FROM clients WHERE id=?", (cid,))
    if not rows:
        await q.edit_message_text("العميل ده اتحذف.")
        return
    c = rows[0]
    if parts[0] == "paid":
        mark_paid(cid, c)
        await q.edit_message_text(
            card(c) + f"\n\n✅ *تم الدفع*\n{stamp_line()}",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif parts[0] == "snz":
        delta = timedelta(hours=1) if parts[2] == "h" else timedelta(days=1)
        until = now_local() + delta
        run("UPDATE clients SET snooze_until=? WHERE id=?", (naive_iso(until), cid))
        label = "بعد ساعة" if parts[2] == "h" else "بعد يوم"
        await q.edit_message_text(
            card(c) + f"\n\n⏰ *هفكرك {label}*\n{stamp_line()}",
            parse_mode=ParseMode.MARKDOWN,
        )

# ================= التذكير التلقائي =================
async def tick(bot):
    owner = owner_id()
    if not owner:
        return
    now = now_local()
    cyc = cycle(now)
    today = now.date()
    last_day = calendar.monthrange(now.year, now.month)[1]
    minutes_now = now.hour * 60 + now.minute

    # قبل يوم
    tomorrow = today + timedelta(days=1)
    if minutes_now >= reminder_minutes() and get_setting("last_pre") != str(today):
        set_setting("last_pre", str(today))
        due_tomorrow = run("SELECT * FROM clients WHERE bill_day=?", (tomorrow.day,))
        unpaid_pre = [c for c in due_tomorrow if c["paid_cycle"] != cyc]
        if unpaid_pre:
            await bot.send_message(owner, f"🔔 *تذكير مسبق*: بكرا محاسبة {len(unpaid_pre)} عميل\n{stamp_line()}", parse_mode=ParseMode.MARKDOWN)
            await send_cards(bot, owner, unpaid_pre)

    # يوم المحاسبة
    if minutes_now >= reminder_minutes() and get_setting("last_run") != str(today):
        set_setting("last_run", str(today))
        due = run("SELECT * FROM clients WHERE bill_day=? OR (bill_day>? AND ?=?)", (today.day, last_day, today.day, last_day))
        unpaid = [c for c in due if c["paid_cycle"] != cyc]
        if unpaid:
            await bot.send_message(owner, f"🔔 *محاسبة يوم {today.day}*: {len(unpaid)} عميل لسه مدفعوش\n{stamp_line()}", parse_mode=ParseMode.MARKDOWN)
            await send_cards(bot, owner, unpaid)
        elif due:
            await bot.send_message(owner, f"🔔 *محاسبة يوم {today.day}*: كله دافع ✅\n{stamp_line()}", parse_mode=ParseMode.MARKDOWN)

    # بعد يوم
    yesterday = today - timedelta(days=1)
    if minutes_now >= reminder_minutes() and get_setting("last_follow") != str(today):
        set_setting("last_follow", str(today))
        due_yday = run("SELECT * FROM clients WHERE bill_day=?", (yesterday.day,))
        still_unpaid = [c for c in due_yday if c["paid_cycle"] != cyc]
        if still_unpaid:
            await bot.send_message(owner, f"⚠️ *متابعة*: {len(still_unpaid)} عميل مدفعوش\n{stamp_line()}", parse_mode=ParseMode.MARKDOWN)
            await send_cards(bot, owner, still_unpaid)

    # مؤجلة
    rows = run("SELECT * FROM clients WHERE snooze_until!='' AND snooze_until<=? AND paid_cycle!=?", (naive_iso(now), cyc))
    for c in rows:
        run("UPDATE clients SET snooze_until='' WHERE id=?", (c["id"],))
    if rows:
        await bot.send_message(owner, f"⏰ *تذكير مؤجل:*\n{stamp_line()}", parse_mode=ParseMode.MARKDOWN)
        await send_cards(bot, owner, rows)

async def post_init(app: Application):
    async def runner():
        while True:
            try:
                await tick(app.bot)
            except Exception as e:
                log.exception(f"tick error: {e}")
            await asyncio.sleep(60)
    app.create_task(runner())
    log.info("Bot started ✅")

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception(f"Update error: {ctx.error}")

# ================= الإطلاق =================
def main():
    if not TOKEN:
        print("❌ حط BOT_TOKEN في Variables أو ملف .env")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("del", delete_client))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("export", export_csv))

    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_ADD}$"), add_start))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"), show_list))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_UNPAID}$"), show_unpaid))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_HOUR}$"), hour_start))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_STATS}$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_EXPORT}$"), export_csv))

    app.add_handler(MessageHandler(filters.Regex(r"^/(paid|h|d|r)_\d+"), on_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^(paid|snz):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_RE), on_text))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
