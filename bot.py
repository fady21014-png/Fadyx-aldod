#!/usr/bin/env python3
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

# ================= الإعدادات الأساسية =================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "8853934310:AAHQ0FEd7oECal5f3sdd61MeD_gvRyBhsZ4").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
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

# ================= قاعدة البيانات والهيكل =================
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
    """CREATE TABLE IF NOT EXISTS pending_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, number TEXT, package_idx INTEGER,
        created_at TEXT DEFAULT '')"""
)
run(
    """CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, amount INTEGER,
        cycle TEXT, paid_at TEXT,
        FOREIGN KEY(client_id) REFERENCES clients(id))"""
)
run("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")

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

# ================= الأدوات المساعدة =================
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

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

def pkg_label(i):
    n, p = PACKAGES[i]
    short = n.split(" /")[0]
    return f"{short} • {p} جنيه"

def kb_rows(items, per_row=2):
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]

CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True, is_persistent=True)
PKG_KB = ReplyKeyboardMarkup(
    kb_rows([pkg_label(i) for i in range(len(PACKAGES))]) + [[BTN_CANCEL]],
    resize_keyboard=True, is_persistent=True,
)

# ================= أوامر البوت الأساسية والعملاء =================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owner = owner_id()
    
    if owner and uid == owner:
        await update.message.reply_text(
            "أهلاً بيك يا فادي يا هندسة 👋\nأنت في لوحة التحكم الرئيسية:",
            reply_markup=MENU,
        )
        return

    ctx.user_data.clear()
    ctx.user_data["step"] = "client_number"
    await update.message.reply_text(
        "🚀 *أهلاً بك في خدمة حجز الباقات والاشتراكات!*\n\n"
        "من فضلك، أدخل رقم الموبايل الخاص بك للبدء في طلب الاشتراك:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KB,
    )

async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    cyc = cycle(now_local())
    rows = run("SELECT * FROM clients ORDER BY bill_day, number")
    if not rows:
        await update.message.reply_text("مفيش عملاء مضافين لسه.")
        return
    lines = []
    for day in sorted({c["bill_day"] for c in rows}):
        group = [c for c in rows if c["bill_day"] == day]
        if group:
            lines.append(f"━━ *يوم {day}* ━━")
            for c in group:
                mark = "✅" if c["paid_cycle"] == cyc else "⏳"
                lines.append(f"{mark} `{c['number']}` | {c['package']} | {c['price']} ج")
    await update.message.reply_text(
        stamp_line() + "\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

async def show_unpaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    rows = run("SELECT * FROM clients WHERE paid_cycle!=? ORDER BY bill_day", (cycle(now_local()),))
    if not rows:
        await update.message.reply_text("كله دافع ومظبوط ✅")
        return
    await update.message.reply_text(f"⏳ لسه ما دفعوش: *{len(rows)}* عميل", parse_mode=ParseMode.MARKDOWN)

async def delete_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    if not ctx.args:
        await update.message.reply_text("اكتب: `/del رقم_العميل`", parse_mode=ParseMode.MARKDOWN)
        return
    run("DELETE FROM clients WHERE number=?", (ctx.args[0],))
    await update.message.reply_text("تم حذف العميل بنجاح ✅")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owner = owner_id()
    ctx.user_data.clear()
    if owner and uid == owner:
        await update.message.reply_text("تم الإلغاء.", reply_markup=MENU)
    else:
        await update.message.reply_text("تم إلغاء العملية. يمكنك إرسال `/start` للبدء من جديد.")

# ================= معالجة خطوات الزبون (محدثة لتعمل بدقة) =================
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owner = owner_id()
    text = update.message.text.strip().translate(AR_DIGITS)
    
    if "إلغاء" in text:
        await cancel(update, ctx)
        return

    step = ctx.user_data.get("step")

    if step == "client_number":
        ctx.user_data["client_number"] = text
        ctx.user_data["step"] = "client_package"
        await update.message.reply_text(
            "تمام جداً! الآن اختر الباقة التي تريد الاشتراك بها من الأزرار أدناه 👇",
            reply_markup=PKG_KB,
        )
        return

    elif step == "client_package":
        # مطابقة ذكية للباقة بناءً على الأرقام الموجودة في النص (الجيجابايت أو السعر)
        pkg_idx = None
        nums = re.findall(r"\d+", text)
        
        for i, (p_name, p_price) in enumerate(PACKAGES):
            # مطابقة عبر اسم الباقة أو السعر أو رقم الجيجا المستخرج
            if any(n in p_name or str(p_price) == n for n in nums):
                pkg_idx = i
                break
        
        if pkg_idx is None:
            await update.message.reply_text("من فضلك اختر باقة صحيحة من الأزرار الموجودة أدناه.")
            return

        number = ctx.user_data["client_number"]
        username = f"@{update.effective_user.username}" if update.effective_user.username else f"ID: {uid}"
        
        # حفظ الطلب في جدول المعلقة (Pending)
        run(
            "INSERT INTO pending_requests(user_id, username, number, package_idx, created_at) VALUES(?,?,?,?,?)",
            (uid, username, number, pkg_idx, naive_iso(now_local()))
        )
        req_id = run("SELECT last_insert_rowid() as id")[0]["id"]
        
        ctx.user_data.clear()
        
        # إرسال رسالة للزبون أن طلبه قيد المراجعة
        await update.message.reply_text(
            "⏳ *تم إرسال طلب اشتراكك بنجاح!*\n\n"
            "جاري مراجعة الطلب من الإدارة، سيتم إعلامك فور قبول اشتراكك وتفعيله.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=types.ReplyKeyboardRemove()
        )

        # إرسال إشعار فوري لك (الأونر) في شات البوت مع أزرار الموافقة والرفض
        if owner:
            p_name, p_price = PACKAGES[pkg_idx]
            approval_markup = Markup([
                [
                    Btn("✅ موافقة وتفعيل", callback_data=f"approve:{req_id}"),
                    Btn("❌ رفض", callback_data=f"reject:{req_id}")
                ]
            ])
            await ctx.bot.send_message(
                owner,
                f"🚨 *طلب اشتراك جديد قيد الانتظار!*\n\n"
                f"👤 المستخدم: {username} (ID: `{uid}`)\n"
                f"📱 الرقم: `{number}`\n"
                f"📦 الباقة: {p_name} ({p_price} جنيه)\n"
                f"{stamp_line()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=approval_markup
            )
        return

    # إدارة الأونر اليدوية لو أضاف عميل من لوحة التحكم
    if owner and uid == owner:
        admin_step = ctx.user_data.get("admin_step")
        if admin_step == "number":
            ctx.user_data["number"] = text
            ctx.user_data["admin_step"] = "package"
            await update.message.reply_text("اختر الباقة:", reply_markup=PKG_KB)

# ================= زر إضافة عميل يدوي للأونر =================
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    ctx.user_data["admin_step"] = "number"
    await update.message.reply_text("أدخل رقم العميل:", reply_markup=CANCEL_KB)

# ================= معالجة أزرار الموافقة والرفض من الأونر (Callback) =================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[0]
    req_id = int(parts[1])

    reqs = run("SELECT * FROM pending_requests WHERE id=?", (req_id,))
    if not reqs:
        await q.edit_message_text("❌ هذا الطلب غير موجود أو تم معالجته مسبقاً.")
        return
    
    req = reqs[0]
    user_id = req["user_id"]
    number = req["number"]
    pkg_idx = req["package_idx"]
    p_name, p_price = PACKAGES[pkg_idx]

    if action == "approve":
        bill_day = 4  # يوم المحاسبة الافتراضي
        
        run(
            "INSERT INTO clients(number, package, price, bill_day, chat_id, created_at) VALUES(?,?,?,?,?,?)",
            (number, p_name, p_price, bill_day, user_id, naive_iso(now_local()))
        )
        run("DELETE FROM pending_requests WHERE id=?", (req_id,))
        
        await q.edit_message_text(
            f"✅ *تمت الموافقة وتفعيل العميل بنجاح!*\n\n"
            f"📱 الرقم: `{number}`\n"
            f"📦 الباقة: {p_name} ({p_price} جنيه)\n"
            f"{stamp_line()}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await ctx.bot.send_message(
                user_id,
                "🎉 *مبارك! تم قبول اشتراكك وتفعيله بنجاح.*\n\n"
                f"📦 باقتك: {p_name}\n"
                f"💰 السعر: {p_price} جنيه\n"
                "سيتواصل معك النظام لمتابعة المواعيد والتجديد.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            log.warning(f"Could not notify user {user_id}: {e}")

    elif action == "reject":
        run("DELETE FROM pending_requests WHERE id=?", (req_id,))
        
        await q.edit_message_text(
            f"❌ *تم رفض الطلب الخاص بالرقم:* `{number}`\n{stamp_line()}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            await ctx.bot.send_message(
                user_id,
                "عذراً، تم رفض طلب الاشتراك الحالي من الإدارة.",
            )
        except Exception as e:
            log.warning(f"Could not notify user {user_id}: {e}")

# ================= الإحصائيات والتصدير =================
async def show_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    cyc = cycle(now_local())
    all_rows = run("SELECT * FROM clients")
    total = len(all_rows)
    paid = [c for c in all_rows if c["paid_cycle"] == cyc]
    msg = f"📊 إجمالي العملاء: {total} | دفعوا: {len(paid)}"
    await update.message.reply_text(msg)

async def export_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    rows = run("SELECT * FROM clients")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Number", "Package", "Price", "BillDay"])
    for c in rows:
        writer.writerow([c["id"], c["number"], c["package"], c["price"], c["bill_day"]])
    await update.message.reply_document(document=io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename="clients.csv")

# ================= التشغيل الرئيسي =================
def main():
    if not TOKEN:
        print("❌ ضع BOT_TOKEN صحيحاً.")
        return
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("del", delete_client))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("export", export_csv))

    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_ADD}$"), add_start))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"), show_list))
    app.add_handler(MessageHandler(filters.Regex(f^{BTN_UNPAID}$"), show_unpaid))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_STATS}$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_EXPORT}$"), export_csv))

    app.add_handler(CallbackQueryHandler(on_button, pattern="^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("🤖 بوت استقبال الطلبات يعمل بكفاءة تامة الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
