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

# ⚠️ هام جداً: ضع رقم الـ ID الخاص بك هنا مباشرة (بدلاً من 0) لضمان وصول الإشعارات لك
OWNER_ID ="8949099540"

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

def is_owner(uid: int) -> bool:
    if OWNER_ID != 0 and uid == OWNER_ID:
        return True
    return False

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

# ================= الأوامر الأساسية =================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    # لو المستخدم هو الأอนر (أنت)
    if is_owner(uid):
        ctx.user_data.clear()
        await update.message.reply_text(
            "أهلاً بيك يا فادي يا هندسة 👋\nأنت في لوحة التحكم الرئيسية:",
            reply_markup=MENU,
        )
        return

    # لو زبون جديد يفتح البوت
    ctx.user_data.clear()
    ctx.user_data["step"] = "client_number"
    await update.message.reply_text(
        "🚀 *أهلاً بك في خدمة حجز الباقات والاشتراكات!*\n\n"
        "من فضلك، أدخل رقم الموبايل الخاص بك للبدء في طلب الاشتراك:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=CANCEL_KB,
    )

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.user_data.clear()
    if is_owner(uid):
        await update.message.reply_text("تم الإلغاء.", reply_markup=MENU)
    else:
        await update.message.reply_text("تم إلغاء العملية. يمكنك إرسال `/start` للبدء من جديد.", reply_markup=types.ReplyKeyboardRemove())

# ================= معالجة رسائل النصوص (الزبائن والأونر) =================
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip().translate(AR_DIGITS)
    
    if "إلغاء" in text:
        await cancel(update, ctx)
        return

    # لو الأอนر بيكتب أوامر لوحة التحكم
    if is_owner(uid):
        admin_step = ctx.user_data.get("admin_step")
        if text == BTN_ADD:
            ctx.user_data.clear()
            ctx.user_data["admin_step"] = "number"
            await update.message.reply_text("أدخل رقم العميل:", reply_markup=CANCEL_KB)
            return
        elif admin_step == "number":
            ctx.user_data["number"] = text
            ctx.user_data["admin_step"] = "package"
            await update.message.reply_text("اختر الباقة:", reply_markup=PKG_KB)
            return
        elif admin_step == "package":
            # إضافة يدوية من الأونر لو حابب
            pass
        return

    # رحلة العميل (الزبون)
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
        pkg_idx = None
        # مطابقة دقيقة بناءً على النص أو الأرقام في زر الباقة
        for i, (p_name, p_price) in enumerate(PACKAGES):
            label = pkg_label(i).translate(AR_DIGITS)
            if label in text or str(p_price) in text or any(n in text for n in re.findall(r"\d+", label)):
                pkg_idx = i
                break

        if pkg_idx is None:
            await update.message.reply_text("من فضلك اختر باقة صحيحة من الأزرار الموجودة أدناه.")
            return

        number = ctx.user_data["client_number"]
        username = f"@{update.effective_user.username}" if update.effective_user.username else f"ID: {uid}"
        
        # حفظ الطلب في جدول الطلبات المعلقة
        run(
            "INSERT INTO pending_requests(user_id, username, number, package_idx, created_at) VALUES(?,?,?,?,?)",
            (uid, username, number, pkg_idx, naive_iso(now_local()))
        )
        req_id = run("SELECT last_insert_rowid() as id")[0]["id"]
        
        ctx.user_data.clear()
        
        # إشعار للزبون
        await update.message.reply_text(
            "⏳ *تم إرسال طلب اشتراكك بنجاح!*\n\n"
            "جاري مراجعة الطلب من الإدارة، سيتم إعلامك فور قبول اشتراكك وتفعيله.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=types.ReplyKeyboardRemove()
        )

        # إرسال إشعار فوري للأونر (أنت)
        if OWNER_ID != 0:
            p_name, p_price = PACKAGES[pkg_idx]
            approval_markup = Markup([
                [
                    Btn("✅ موافقة وتفعيل", callback_data=f"approve:{req_id}"),
                    Btn("❌ رفض", callback_data=f"reject:{req_id}")
                ]
            ])
            try:
                await ctx.bot.send_message(
                    OWNER_ID,
                    f"🚨 *طلب اشتراك جديد قيد الانتظار!*\n\n"
                    f"👤 المستخدم: {username} (ID: `{uid}`)\n"
                    f"📱 الرقم: `{number}`\n"
                    f"📦 الباقة: {p_name} ({p_price} جنيه)\n"
                    f"{stamp_line()}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=approval_markup
                )
            except Exception as e:
                log.error(f"Failed to send notification to owner: {e}")
        return

# ================= معالجة أزرار الموافقة والرفض من الأونر =================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        bill_day = 4
        
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

# ================= التشغيل الرئيسي =================
def main():
    if not TOKEN:
        print("❌ ضع BOT_TOKEN صحيحاً.")
        return
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(on_button, pattern="^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("🤖 بوت استقبال الطلبات يعمل بكفاءة تامة الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
