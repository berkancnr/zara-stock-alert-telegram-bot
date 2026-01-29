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
from zara_ldjson import get_zara_products_async, evaluate_size_status, send_telegram

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHECK_INTERVAL_SECONDS = 60

JOB_LOCK = asyncio.Lock()
# Limit concurrency to 5 unique URLs
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
        "/watch <BEDEN1,BEDEN2> <URL> (Çoklu beden)\n"
        "/watch <URL>          (Herhangi bir beden - ANY)\n"
        "/unwatch <BEDEN1,BEDEN2> <URL>\n"
        "/list\n"
        "/check (hemen kontrol)\n"
        "/clear (tüm listeyi sil)\n"
        "/del <ID> (ID ile listeden sil)\n\n"
        "Örn:\n"
        "/watch S,M,L https://www.zara.com/tr/tr/....html\n"
        "/watch https://www.zara.com/tr/tr/....html\n"
    )
    await update.message.reply_text(msg)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Kullanım: /watch [BEDEN1,BEDEN2] <URL>")
        return

    if len(args) == 1:
        size_input = "ANY"
        url = args[0].strip()
    else:
        size_input = args[0].upper().strip()
        url = args[1].strip()

    if not _valid_url(url):
        await update.message.reply_text("URL geçersiz görünüyor.")
        return

    sizes = [s.strip() for s in size_input.split(",") if s.strip()]
    
    added, existed = [], []
    for s in sizes:
        if add_watch(str(update.effective_chat.id), url, s, int(time.time())):
            added.append(s)
        else:
            existed.append(s)
    
    res = []
    if added: res.append(f"✅ Takibe alındı: {', '.join(added)}")
    if existed: res.append(f"ℹ️ Zaten listede: {', '.join(existed)}")
    
    await update.message.reply_text(f"{'\n'.join(res)}\n🔗 {url}")
    log(f"Watch add | chat_id={update.effective_chat.id} | sizes={sizes}")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Kullanım: /unwatch <BEDEN1,BEDEN2> <URL>")
        return

    size_input = args[0].upper().strip()
    url = args[1].strip()
    sizes = [s.strip() for s in size_input.split(",") if s.strip()]
    
    removed, not_found = [], []
    for s in sizes:
        if remove_watch(str(update.effective_chat.id), url, s):
            removed.append(s)
        else:
            not_found.append(s)
    
    res = []
    if removed: res.append(f"🗑️ Silindi: {', '.join(removed)}")
    if not_found: res.append(f"❌ Kayıt bulunamadı: {', '.join(not_found)}")
    
    await update.message.reply_text(f"{'\n'.join(res)}\n🔗 {url}")


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


async def process_url_group(url: str, watches: list, source: str):
    """Aynı URL'ye sahip tüm watch'ları tek bir sayfa yüklemesiyle işler."""
    async with SEM:
        try:
            log(f"{source} -> processing group for {url} ({len(watches)} sizes)")
            products = await get_zara_products_async(url)
            
            for w in watches:
                status, av_norm, details = evaluate_size_status(products, w.size, url)
                
                current_price = str(details.get("price")) if details.get("price") is not None else None
                product_name = details.get("name")
                price_changed = w.last_price and current_price and w.last_price != current_price
                price_msg = f"💰 FİYAT DEĞİŞTİ: {w.last_price} -> {current_price}" if price_changed else None

                if status == "POSITIVE":
                    await asyncio.to_thread(send_telegram, BOT_TOKEN, w.chat_id, _format_alert(details, status, price_msg))
                    remove_watch(w.chat_id, w.url, w.size)
                    log(f"{source} -> ALERT SENT and removed id={w.id}")
                else:
                    if price_changed:
                        await asyncio.to_thread(send_telegram, BOT_TOKEN, w.chat_id, _format_alert(details, status, price_msg))
                    update_watch_status(w.id, status, av_norm, last_price=current_price, product_name=product_name)

        except Exception as e:
            log(f"{source} ERROR for {url}: {repr(e)}")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    watches = list_watches(chat_id)

    if not watches:
        await update.message.reply_text("İzleme listesi boş.")
        return

    await update.message.reply_text("Kontrol ediliyor... (URL gruplama devrede)")
    
    url_groups = {}
    for w in watches:
        url_groups.setdefault(w.url, []).append(w)
    
    tasks = [process_url_group(url, group, "Manual") for url, group in url_groups.items()]
    await asyncio.gather(*tasks)
    await update.message.reply_text("Kontrol tamamlandı.")


async def periodic_job(context: ContextTypes.DEFAULT_TYPE):
    if JOB_LOCK.locked(): return
    async with JOB_LOCK:
        watches = list_all_watches()
        if not watches: return

        url_groups = {}
        for w in watches:
            url_groups.setdefault(w.url, []).append(w)
        
        log(f"Periodic -> processing {len(url_groups)} unique URLs")
        tasks = [process_url_group(url, group, "Periodic") for url, group in url_groups.items()]
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
