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
