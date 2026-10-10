import asyncio
import logging
import os
import re
import sqlite3
from urllib.parse import urlparse, urljoin

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "120")))
DB_PATH = os.getenv("DB_PATH", "kufar_bot.sqlite3")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kufar-bot")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(user_id, url)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_ads (
        user_id INTEGER NOT NULL,
        search_id INTEGER NOT NULL,
        ad_url TEXT NOT NULL,
        first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, search_id, ad_url)
    )""")
    conn.commit()
    return conn

def valid_kufar_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and (host == "kufar.by" or host.endswith(".kufar.by"))
    except Exception:
        return False

def extract_ads(html: str, page_url: str):
    """Best-effort parser for public search results. Kufar page structure may change."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    # First try structured metadata where available.
    for node in soup.select('a[href]'):
        href = (node.get("href") or "").strip()
        full_url = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(full_url)
        if parsed.hostname not in ("kufar.by", "www.kufar.by"):
            continue
        path = parsed.path.lower()
        if not path or path == "/" or "/item/" not in path:
            continue
        title = " ".join(node.get_text(" ", strip=True).split())
        if not title:
            title = node.get("aria-label") or node.get("title") or "Объявление на Kufar"
        # Remove tracking query params, preserve URL path.
        clean_url = f"https://www.kufar.by{parsed.path}"
        found[clean_url] = title[:180]
    # Fallback: links sometimes use another item path shape.
    if not found:
        for node in soup.select('a[href]'):
            href = (node.get("href") or "").strip()
            full_url = urljoin(page_url, href).split("#")[0]
            parsed = urlparse(full_url)
            if parsed.hostname not in ("kufar.by", "www.kufar.by"):
                continue
            if re.search(r"/(item|ad)/[^/]+", parsed.path, re.I):
                title = " ".join(node.get_text(" ", strip=True).split()) or "Объявление на Kufar"
                found[f"https://www.kufar.by{parsed.path}"] = title[:180]
    return found

async def fetch_ads(session: aiohttp.ClientSession, url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KufarNewListingNotifier/1.0)",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as resp:
        if resp.status in (403, 429):
            raise RuntimeError(f"Kufar вернул HTTP {resp.status}; возможно, запросы ограничены.")
        resp.raise_for_status()
        html = await resp.text()
    return extract_ads(html, url)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я отслеживаю новые объявления в поиске Kufar Беларусь.\n\n"
        "1) Открой kufar.by и настрой поиск с нужными фильтрами.\n"
        "2) Скопируй ссылку именно на страницу результатов поиска.\n"
        "3) Отправь её сюда.\n\n"
        "Команды: /list — мои поиски, /delete — удалить поиск, /help — помощь.\n"
        f"Проверка примерно раз в {CHECK_INTERVAL} секунд. Стабильность зависит от того, как Kufar отдаёт страницу."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def add_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    url = (update.message.text or "").strip()
    if not valid_kufar_url(url):
        await update.message.reply_text(
            "Пришли полную ссылку на страницу поиска с домена kufar.by. "
            "Не отправляй логины, пароли или приватные ссылки."
        )
        return
    conn = db()
    try:
        cur = conn.execute("INSERT OR IGNORE INTO searches(user_id, url) VALUES (?, ?)", (user_id, url))
        conn.commit()
        row = conn.execute("SELECT id FROM searches WHERE user_id=? AND url=?", (user_id, url)).fetchone()
        search_id = row[0]
    finally:
        conn.close()

    # Seed current results so the bot doesn't alert for every old listing on first add.
    try:
        async with aiohttp.ClientSession() as session:
            ads = await fetch_ads(session, url)
        conn = db()
        for ad_url in ads:
            conn.execute("INSERT OR IGNORE INTO seen_ads(user_id, search_id, ad_url) VALUES (?, ?, ?)",
                         (user_id, search_id, ad_url))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"Поиск сохранён ✅\nСейчас удалось прочитать объявлений: {len(ads)}.\n"
            "Текущие объявления запомнил; буду присылать только новые.\n"
            "Если число 0, возможно, Kufar изменил страницу — пришли мне ссылку поиска, и парсер можно будет подстроить."
        )
    except Exception as e:
        log.warning("Initial fetch failed: %s", e)
        await update.message.reply_text(
            "Ссылку сохранил, но пока не смог прочитать выдачу Kufar.\n"
            f"Причина: {str(e)[:250]}\n"
            "Проверь, открывается ли ссылка в браузере. Если Kufar блокирует автоматические запросы, нужно будет использовать другой разрешённый способ доступа."
        )

async def list_searches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute("SELECT id, url FROM searches WHERE user_id=? AND active=1 ORDER BY id", (update.effective_user.id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Пока нет отслеживаемых ссылок. Отправь ссылку на поиск Kufar.")
        return
    text = "\n\n".join(f"{i}. {url}" for i, url in rows)
    await update.message.reply_text("Твои поиски:\n\n" + text + "\n\nЧтобы удалить, напиши /delete НОМЕР")

async def delete_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши номер поиска, например: /delete 2\nПосмотреть номера: /list")
        return
    try:
        search_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Нужен номер из команды /list.")
        return
    conn = db()
    cur = conn.execute("DELETE FROM searches WHERE id=? AND user_id=?", (search_id, update.effective_user.id))
    conn.execute("DELETE FROM seen_ads WHERE search_id=? AND user_id=?", (search_id, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text("Поиск удалён." if cur.rowcount else "Не нашёл такой поиск. Проверь /list.")

async def monitor(app: Application):
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        while True:
            conn = db()
            searches = conn.execute("SELECT id, user_id, url FROM searches WHERE active=1").fetchall()
            conn.close()
            for search_id, user_id, url in searches:
                try:
                    ads = await fetch_ads(session, url)
                    if not ads:
                        continue
                    conn = db()
                    new_ads = []
                    for ad_url, title in ads.items():
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO seen_ads(user_id, search_id, ad_url) VALUES (?, ?, ?)",
                            (user_id, search_id, ad_url)
                        )
                        if cur.rowcount:
                            new_ads.append((ad_url, title))
                    conn.commit()
                    conn.close()
                    # Notify only newly found ads. On initial add, existing ads were seeded.
                    for ad_url, title in new_ads[:20]:
                        try:
                            await app.bot.send_message(
                                chat_id=user_id,
                                text=f"🆕 Новое объявление на Kufar\n\n{title}\n{ad_url}",
                                disable_web_page_preview=False,
                            )
                        except Exception:
                            log.exception("Could not notify user %s", user_id)
                except Exception as e:
                    log.warning("Search %s failed: %s", search_id, e)
                await asyncio.sleep(2)  # Be gentle with the site.
            await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app: Application):
    app.create_task(monitor(app))

def main():
    if not TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в файле .env")
    db().close()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_searches))
    app.add_handler(CommandHandler("delete", delete_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_search))
    app.run_polling()

if __name__ == "__main__":
    main()
