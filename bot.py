import time
import os
import datetime
import asyncio
from urllib.parse import urlparse
from typing import Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from store import (
    init_db,
    add_watch,
    remove_watch,
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
    base = (
        f"--- ZARA ALERT --- ({status})\n"
        f"{details.get('name')}\n"
        f"Size: {details.get('size')} | Color: {details.get('color')}\n"
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
        "/watch <BEDEN> <URL>\n"
        "/unwatch <BEDEN> <URL>\n"
        "/list\n"
        "/check (hemen kontrol)\n\n"
        "Not: POSITIVE yakalanınca mesaj gönderilir ve ilgili watch otomatik silinir (one-shot).\n"
        "Fiyat değişimi olursa da bildirim gönderilir (watch silinmez).\n\n"
        "Örn:\n"
        "/watch XL https://www.zara.com/tr/tr/....html\n"
    )
    await update.message.reply_text(msg)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Kullanım: /watch <BEDEN> <URL>")
        return

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
    if len(args) < 2:
        await update.message.reply_text("Kullanım: /unwatch <BEDEN> <URL>")
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


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    watches = list_watches(chat_id)
    log(f"/list received | chat_id={chat_id} | count={len(watches)}")

    if not watches:
        await update.message.reply_text("İzleme listesi boş.")
        return

    lines = ["İzlediklerin:"]
    for w in watches:
        price_info = f" | {w.last_price}" if w.last_price else ""
        lines.append(f"- id={w.id} | {w.size} | {w.url} | last={w.last_status or '-'} ({w.last_availability or '-'}{price_info})")
    await update.message.reply_text("\n".join(lines))


async def process_watch(w, source: str):
    async with SEM:
        try:
            log(f"{source} -> checking watch id={w.id} size={w.size}")

            # check_size_status_async is now async, so await it directly
            status, av_norm, details = await check_size_status_async(w.url, w.size)

            log(f"{source} -> result id={w.id}: status={status}, availability={av_norm}")
            
            current_price = str(details.get("price")) if details.get("price") is not None else None
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

            update_watch_status(w.id, status, av_norm, last_price=current_price)

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


def main():
    init_db()
    log("Bot starting...")
    log(f"Check interval set to {CHECK_INTERVAL_SECONDS} seconds")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("start", help_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("check", check_cmd))

    app.job_queue.run_repeating(periodic_job, interval=CHECK_INTERVAL_SECONDS, first=10)
    log("Periodic job scheduled (first run in 10s)")

    app.run_polling()


if __name__ == "__main__":
    main()
