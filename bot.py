import time
import os
import datetime
import asyncio
import html
import re
from urllib.parse import urlparse
from typing import Optional

from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

from store import (
    init_db,
    add_watch,
    remove_watch,
    remove_all_watches,
    remove_watch_by_id,
    list_watches,
    list_all_watches,
    update_watch_status,
)
from zara_ldjson import check_size_status_async, send_telegram

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHECK_INTERVAL_SECONDS = 60

JOB_LOCK = asyncio.Lock()
# Limit concurrency to 5 (increased from 3 due to resource blocking optimization)
SEM = asyncio.Semaphore(5)


def log(msg: str) -> None:
    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def _valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _format_alert(details: dict, status: str, price_change_msg: Optional[str] = None) -> str:
    found_sizes = details.get("found_sizes")
    
    if found_sizes:
        size_info = f"⚡ FOUND SIZES: {', '.join(found_sizes)}"
    else:
        size_info = f"Size: {details.get('size')}"

    base = (
        f"--- ZARA ALERT --- ({status})\n"
        f"{details.get('name')}\n"
        f"{size_info} | Color: {details.get('color')}\n"
        f"Price: {details.get('price')} {details.get('currency')}\n"
        f"Availability: {details.get('availability_norm')}\n"
        f"SKU: {details.get('sku')}\n"
        f"URL: {details.get('url')}"
    )
    if price_change_msg:
        base += f"\n\n{price_change_msg}"
    return base


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Komutlar:\n"
        "/watch <BEDEN> <URL>  (Belirli bedeni izle)\n"
        "/watch <URL>          (Herhangi bir bedeni izle - ANY)\n"
        "/unwatch <BEDEN> <URL>\n"
        "/list\n"
        "/check (hemen kontrol)\n"
        "/clear (tüm listeyi sil)\n"
        "/del <ID> (ID ile listeden sil)\n\n"
        "Not: POSITIVE yakalanınca mesaj gönderilir ve ilgili watch otomatik silinir (one-shot).\n"
        "Fiyat değişimi olursa da bildirim gönderilir (watch silinmez).\n\n"
        "Örn:\n"
        "/watch XL https://www.zara.com/tr/tr/....html\n"
        "/watch https://www.zara.com/tr/tr/....html (Tüm bedenler)\n"
    )
    await update.message.reply_text(msg)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Kullanım: /watch [BEDEN] <URL>")
        return

    # Eğer 1 argüman varsa sadece URL verilmiştir -> size="ANY"
    if len(args) == 1:
        size = "ANY"
        url = args[0].strip()
    else:
        # 2 veya daha fazla varsa ilki size, ikincisi URL kabul edelim
        size = args[0].upper().strip()
        url = args[1].strip()

    log(f"/watch received | chat_id={update.effective_chat.id} | size={size} | url={url}")

    if not _valid_url(url):
        await update.message.reply_text("URL geçersiz görünüyor.")
        return

    ok = add_watch(str(update.effective_chat.id), url, size, int(time.time()))
    if ok:
        await update.message.reply_text(f"Kaydedildi: {size} -> {url}")
        log(f"Watch saved | chat_id={update.effective_chat.id} | size={size}")
    else:
        await update.message.reply_text("Zaten izleniyor.")
        log(f"Watch already exists | chat_id={update.effective_chat.id} | size={size}")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    # unwatch için hala size ve url bekleyelim ki yanlışlıkla silinmesin
    # ama kullanıcı /watch <URL> yaptıysa silmek için /unwatch ANY <URL> demeli
    if len(args) < 2:
        await update.message.reply_text("Kullanım: /unwatch <BEDEN> <URL>\n(Tüm bedenler için: /unwatch ANY <URL>)")
        return

    size = args[0].upper().strip()
    url = args[1].strip()

    log(f"/unwatch received | chat_id={update.effective_chat.id} | size={size} | url={url}")

    ok = remove_watch(str(update.effective_chat.id), url, size)
    if ok:
        await update.message.reply_text(f"Silindi: {size} -> {url}")
        log(f"Watch removed | chat_id={update.effective_chat.id} | size={size}")
    else:
        await update.message.reply_text("Kayıt bulunamadı.")
        log(f"Watch not found | chat_id={update.effective_chat.id} | size={size}")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a watch by its ID. Supports /del 5 and clicking /del_5."""
    watch_id = None
    if context.args:
        try:
            watch_id = int(context.args[0])
        except ValueError:
            pass
    
    # Check if it was a /del_ID style call (from clicking the link)
    if not watch_id and update.message.text:
        match = re.search(r"/del_(\d+)", update.message.text)
        if match:
            watch_id = int(match.group(1))

    if not watch_id:
        await update.message.reply_text("Kullanım: /del <ID> veya listedeki /del_ID linkine tıklayın.")
        return
    
    ok = remove_watch_by_id(watch_id, str(update.effective_chat.id))
    if ok:
        await update.message.reply_text(f"✅ ID:{watch_id} listeden silindi.")
        log(f"/del executed | chat_id={update.effective_chat.id} | watch_id={watch_id}")
    else:
        await update.message.reply_text("❌ Kayıt bulunamadı.")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before clearing all watches."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Evet, Hepsini Sil", callback_data="clear_yes"),
            InlineKeyboardButton("❌ İptal", callback_data="clear_no"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚠️ Tüm izleme listenizi silmek üzeresiniz. Emin misiniz?", reply_markup=reply_markup
    )


async def confirm_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from the clear confirmation buttons."""
    query = update.callback_query
    await query.answer()

    if query.data == "clear_yes":
        count = remove_all_watches(str(update.effective_chat.id))
        await query.edit_message_text(f"🗑️ Listeniz temizlendi. ({count} kayıt silindi)")
        log(f"/clear confirmed | chat_id={update.effective_chat.id} | removed_count={count}")
    else:
        await query.edit_message_text("❌ İşlem iptal edildi.")
        log(f"/clear cancelled | chat_id={update.effective_chat.id}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    watches = list_watches(chat_id)
    log(f"/list received | chat_id={chat_id} | count={len(watches)}")

    if not watches:
        await update.message.reply_text("📭 İzleme listeniz şu an boş.")
        return

    header = "📋 <b>İzleme Listeniz</b>\n\n"
    messages = []
    current_chunk = header

    for w in watches:
        status_emoji = "🟢" if w.last_status == "POSITIVE" else "🔴" if w.last_status == "NEGATIVE" else "⚪"
        price_str = f"<b>{w.last_price}</b> TL" if w.last_price else "???"
        product_display = html.escape(w.product_name or "İsimsiz Ürün")

        # Extremely compact format
        entry = (
            f"{status_emoji} <a href='{w.url}'>{product_display}</a>\n"
            f"└ {w.size} | {price_str} | Sil: /del_{w.id}\n\n"
        )

        if len(current_chunk) + len(entry) > 4000:
            messages.append(current_chunk)
            current_chunk = ""
        
        current_chunk += entry

    if current_chunk:
        messages.append(current_chunk)

    for msg in messages:
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def process_watch(w, source: str):
    async with SEM:
        try:
            log(f"{source} -> checking watch id={w.id} size={w.size}")

            # check_size_status_async is now async, so await it directly
            status, av_norm, details = await check_size_status_async(w.url, w.size)

            log(f"{source} -> result id={w.id}: status={status}, availability={av_norm}")
            
            current_price = str(details.get("price")) if details.get("price") is not None else None
            product_name = details.get("name")
            last_price = w.last_price
            price_changed = False
            price_msg = None

            if last_price and current_price and last_price != current_price:
                price_changed = True
                price_msg = f"💰 FİYAT DEĞİŞTİ: {last_price} -> {current_price}"
                log(f"{source} -> Price change detected id={w.id}: {price_msg}")

            if status == "POSITIVE":
                log(f"{source} -> sending alert then removing watch id={w.id}")
                await asyncio.to_thread(
                    send_telegram, BOT_TOKEN, w.chat_id, _format_alert(details, status, price_msg)
                )
                removed = remove_watch(w.chat_id, w.url, w.size)
                log(f"{source} -> removed={removed} (chat_id={w.chat_id}, size={w.size})")
                return
            
            if price_changed:
                 log(f"{source} -> sending price alert id={w.id}")
                 await asyncio.to_thread(
                    send_telegram, BOT_TOKEN, w.chat_id, _format_alert(details, status, price_msg)
                )

            update_watch_status(w.id, status, av_norm, last_price=current_price, product_name=product_name)

        except Exception as e:
            log(f"{source} ERROR id={w.id}: {repr(e)}")
            update_watch_status(w.id, "ERROR", "error", last_price=w.last_price)


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Kullanıcının kendi watch'larını hemen kontrol eder.
    Concurrency limited by SEM.
    """
    chat_id = str(update.effective_chat.id)
    watches = list_watches(chat_id)

    log(f"/check received | chat_id={chat_id} | watch_count={len(watches)}")

    if not watches:
        await update.message.reply_text("İzleme listesi boş.")
        return

    await update.message.reply_text("Kontrol ediyorum... (tarayıcı arka planda çalışır)")
    log("Manual check started")

    # Run tasks concurrently
    tasks = [process_watch(w, "Manual") for w in watches]
    await asyncio.gather(*tasks)

    await update.message.reply_text("Kontrol tamamlandı.")
    log("Manual check finished")


async def periodic_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tüm watch'ları periyodik kontrol eder.
    Concurrency limited by SEM.
    """
    if JOB_LOCK.locked():
        log("⏱️ periodic_job skipped (previous run still in progress)")
        return

    async with JOB_LOCK:
        log("⏱️ periodic_job tick")

        watches = list_all_watches()
        log(f"Periodic -> found {len(watches)} watch(es)")

        if not watches:
            return

        # Run tasks concurrently
        tasks = [process_watch(w, "Periodic") for w in watches]
        await asyncio.gather(*tasks)


async def post_init(application):
    commands = [
        BotCommand("watch", "[BEDEN] [URL] Takip başlat"),
        BotCommand("unwatch", "[BEDEN] [URL] Takibi durdur"),
        BotCommand("list", "İzleme listeni göster"),
        BotCommand("check", "Hemen kontrol et"),
        BotCommand("clear", "Tüm listeyi temizle"),
        BotCommand("del", "<ID> Listeden sil"),
        BotCommand("help", "Yardım mesajı"),
    ]
    await application.bot.set_my_commands(commands)
    log("Bot commands menu set.")

def main():
    init_db()
    log("Bot starting...")
    log(f"Check interval set to {CHECK_INTERVAL_SECONDS} seconds")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("start", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"^/del_\d+$"), del_cmd))
    app.add_handler(CallbackQueryHandler(confirm_clear, pattern="^clear_"))

    app.job_queue.run_repeating(periodic_job, interval=CHECK_INTERVAL_SECONDS, first=10)
    log("Periodic job scheduled (first run in 10s)")

    app.run_polling()


if __name__ == "__main__":
    main()
