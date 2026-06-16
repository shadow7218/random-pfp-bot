import os
import logging
import asyncio
import json
import base64
import hashlib
from datetime import datetime
import pytz
from dotenv import load_dotenv
from PIL import Image
import io
import httpx
import random
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT2_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
CHANNEL_ID = os.getenv("CHANNEL_ID", "@Anime_wallpapers_EXT")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8063827544"))
TIMEZONE = "Asia/Kolkata"
POST_HOUR = 14
POST_MINUTE = 0
MIN_PHOTOS = 4
MAX_PHOTOS = 9

QUEUE_FILE = "./queue2.json"
SEEN_FILE = "./seen2.json"
USERS_FILE = "./users2.json"
STATS_FILE = "./stats2.json"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── WEEKLY THEME ─────────────────────────────────────────────────────────────
WEEKLY_THEME = {
    0: "dark",      # Monday
    1: "random",    # Tuesday
    2: "random",    # Wednesday
    3: "random",    # Thursday
    4: "cute",      # Friday
    5: "random",    # Saturday
    6: "random",    # Sunday
}

# ─── API CATEGORIES ───────────────────────────────────────────────────────────
WAIFU_CATEGORIES = {
    "dark": ["megumin", "shinobu", "maid"],
    "cute": ["neko", "waifu", "shinobu"],
    "random": ["megumin", "neko", "waifu", "shinobu", "maid", "uniform"]
}

NEKOS_CATEGORIES = {
    "dark": ["kamisato_ayaka", "raiden_shogun", "mona"],
    "cute": ["sakura_miyashiro", "izumi_konata", "shinobu_kocho"],
    "random": ["kamisato_ayaka", "raiden_shogun", "sakura_miyashiro", "izumi_konata", "shinobu_kocho", "mona"]
}

# ─── STORAGE ──────────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_queue(): return load_json(QUEUE_FILE, [])
def save_queue(q): save_json(QUEUE_FILE, q)
def load_seen(): return set(load_json(SEEN_FILE, []))
def save_seen(s): save_json(SEEN_FILE, list(s))

def load_users():
    users = load_json(USERS_FILE, [ADMIN_ID])
    if ADMIN_ID not in users:
        users.append(ADMIN_ID)
    return users

def save_users(u): save_json(USERS_FILE, u)
def is_allowed(uid): return uid in load_users()
def load_stats(): return load_json(STATS_FILE, {"total_posts": 0, "total_images": 0})
def save_stats(s): save_json(STATS_FILE, s)

# ─── IMAGE HELPERS ────────────────────────────────────────────────────────────
def crop_to_square(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if w == h:
        return image_bytes
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

def image_hash(image_bytes: bytes) -> str:
    return hashlib.md5(image_bytes).hexdigest()

# ─── FETCH FROM APIS ──────────────────────────────────────────────────────────
async def fetch_from_waifu(theme: str) -> bytes | None:
    try:
        categories = WAIFU_CATEGORIES.get(theme, WAIFU_CATEGORIES["random"])
        category = random.choice(categories)
        async with httpx.AsyncClient() as client:
            res = await client.get(f"https://api.waifu.pics/sfw/{category}", timeout=10)
            res.raise_for_status()
            url = res.json().get("url")
            if not url: return None
            img_res = await client.get(url, timeout=15)
            img_res.raise_for_status()
            return img_res.content
    except Exception as e:
        logger.warning(f"Waifu.pics error: {e}")
        return None

async def fetch_from_nekos(theme: str) -> bytes | None:
    try:
        categories = NEKOS_CATEGORIES.get(theme, NEKOS_CATEGORIES["random"])
        category = random.choice(categories)
        async with httpx.AsyncClient() as client:
            res = await client.get(f"https://nekos.best/api/v2/{category}", timeout=10)
            res.raise_for_status()
            results = res.json().get("results", [])
            if not results: return None
            url = random.choice(results).get("url")
            if not url: return None
            img_res = await client.get(url, timeout=15)
            img_res.raise_for_status()
            return img_res.content
    except Exception as e:
        logger.warning(f"Nekos.best error: {e}")
        return None

async def fetch_from_animepictures(theme: str) -> bytes | None:
    try:
        tags = {"dark": "dark+anime", "cute": "cute+anime+girl", "random": "anime+girl"}.get(theme, "anime")
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"https://anime-pictures.net/api/v3/posts?search_tag={tags}&limit=20",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            res.raise_for_status()
            posts = res.json().get("posts", [])
            if not posts: return None
            post = random.choice(posts)
            url = post.get("small_preview") or post.get("preview_url")
            if not url: return None
            if not url.startswith("http"):
                url = "https://anime-pictures.net" + url
            img_res = await client.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            img_res.raise_for_status()
            return img_res.content
    except Exception as e:
        logger.warning(f"Anime-pictures error: {e}")
        return None

async def fetch_image(theme: str, seen: set) -> bytes | None:
    fetchers = [fetch_from_waifu, fetch_from_nekos, fetch_from_animepictures]
    random.shuffle(fetchers)
    for fetcher in fetchers:
        for _ in range(3):
            img_bytes = await fetcher(theme)
            if img_bytes:
                h = image_hash(img_bytes)
                if h not in seen:
                    return img_bytes
    return None

# ─── GEMINI HASHTAGS ──────────────────────────────────────────────────────────
async def generate_hashtags(image_bytes: bytes) -> str:
    if not GEMINI_API_KEY:
        return "#random #malepfp #male #solopfp #mangapfp #pfp"
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((512, 512))
        buffered = io.BytesIO()
        img.convert("RGB").save(buffered, format="JPEG", quality=70)
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

        payload = {
            "contents": [{"parts": [
                {"text": (
                    "Look at this anime/manga profile picture. "
                    "Return ONLY hashtags separated by spaces. "
                    "Always include #pfp #solopfp. "
                    "If male add #malepfp #male, if female add #femalepfp #female. "
                    "If from manga add #mangapfp, if from anime add #animepfp. "
                    "Add character name if known, anime/manga name if known. "
                    "Add #random if character unknown. "
                    "No explanation, just hashtags."
                )},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}}
            ]}],
            "generationConfig": {"maxOutputTokens": 100}
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json=payload, timeout=30.0
            )
            response.raise_for_status()
            result = response.json()

        if "candidates" not in result or not result["candidates"]:
            return "#random #malepfp #male #solopfp #mangapfp #pfp"
        return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"Gemini error: {e}")
        return "#random #malepfp #male #solopfp #mangapfp #pfp"

# ─── CAPTION ──────────────────────────────────────────────────────────────────
def build_caption(hashtags: str) -> str:
    return (
        "⛩✧🦋･ 𝐀𝖓𝖎𝖒𝖊 𝐏𝖋𝐏 & 𝐖𝖆𝖑𝖑𝖕𝖆𝖕𝖊𝖗 ･🦋✧⛩\n"
        "┈──────────┈🔻┈──────────┈\n\n"
        "𝐓𝐚𝐠𝐬  ~ " + hashtags + "\n\n"
        "𝐉ᴏɪɴ 𝐅ᴏʀ 𝐌ᴏʀᴇ....! ✨\n"
        "    ➥ " + CHANNEL_ID
    )

# ─── CORE POST ────────────────────────────────────────────────────────────────
async def do_post(app, notify=True):
    now = datetime.now(pytz.timezone(TIMEZONE))
    theme = WEEKLY_THEME.get(now.weekday(), "random")
    logger.info(f"Posting with theme: {theme}")

    seen = load_seen()
    queue = load_queue()
    images = []

    # Use manual queue images first
    while queue and len(images) < MAX_PHOTOS:
        item = queue.pop(0)
        img_bytes = bytes(item["bytes"])
        h = image_hash(img_bytes)
        if h not in seen:
            images.append(img_bytes)
            seen.add(h)

    # Fetch remaining from APIs
    count_needed = random.randint(MIN_PHOTOS, MAX_PHOTOS)
    while len(images) < count_needed:
        img_bytes = await fetch_image(theme, seen)
        if img_bytes:
            img_bytes = crop_to_square(img_bytes)
            h = image_hash(img_bytes)
            seen.add(h)
            images.append(img_bytes)
        else:
            logger.warning("Could not fetch enough images")
            break

    if not images:
        await app.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Could not fetch any images! APIs may be down.")
        return False

    try:
        hashtags = await generate_hashtags(images[0])
        caption = build_caption(hashtags)

        media = [InputMediaPhoto(media=images[0], caption=caption, parse_mode=None)]
        for img in images[1:]:
            media.append(InputMediaPhoto(media=img))

        await app.bot.send_media_group(chat_id=CHANNEL_ID, media=media)

        stats = load_stats()
        stats["total_posts"] += 1
        stats["total_images"] += len(images)
        save_stats(stats)
        save_queue(queue)
        save_seen(seen)

        if notify:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"✅ Post sent!\n"
                    f"📸 {len(images)} images posted\n"
                    f"🎨 Theme: {theme.capitalize()}\n"
                    f"⏰ Time: {now.strftime('%I:%M %p')}\n"
                    f"📢 Channel: {CHANNEL_ID}\n"
                    f"📊 Total posts: {stats['total_posts']}"
                )
            )
        return True
    except Exception as e:
        logger.error(f"Post error: {e}")
        await app.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Failed to post!\nError: {e}")
        return False

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
async def scheduler(app):
    logger.info(f"Scheduler started. Posting daily at {POST_HOUR:02d}:{POST_MINUTE:02d} IST")
    posted_today = False
    while True:
        now = datetime.now(pytz.timezone(TIMEZONE))
        if now.hour == POST_HOUR and now.minute == POST_MINUTE:
            if not posted_today:
                logger.info("⏰ Time to post!")
                await do_post(app)
                posted_today = True
        else:
            posted_today = False
        await asyncio.sleep(30)

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text(
        "👋 Random PfP Bot running!\n\n"
        "Send me images to add to queue.\n\n"
        "/queue - Queue status\n"
        "/post - Post instantly to channel\n"
        "/stats - Total posts stats\n"
        "/nextpost - Next post time\n"
        "/theme - Today's theme\n"
        "/adduser [id] - Add user\n"
        "/removeuser [id] - Remove user\n"
        "/users - List users"
    )

async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    queue = load_queue()
    await update.message.reply_text(
        f"📦 Queue:\n🖼️ Manual images: {len(queue)}\n🌐 Auto-fetch: ON"
    )

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text("📤 Fetching and posting now...")
    success = await do_post(ctx.application)
    if success:
        await update.message.reply_text("✅ Posted successfully!")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    stats = load_stats()
    await update.message.reply_text(
        f"📊 Stats:\n📬 Total posts: {stats['total_posts']}\n🖼️ Total images: {stats['total_images']}"
    )

async def cmd_nextpost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    from datetime import timedelta
    now = datetime.now(pytz.timezone(TIMEZONE))
    next_post = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    if now >= next_post:
        next_post += timedelta(days=1)
    diff = next_post - now
    hours, rem = divmod(int(diff.total_seconds()), 3600)
    minutes = rem // 60
    await update.message.reply_text(
        f"⏰ Next post: {next_post.strftime('%I:%M %p')} IST\n🕐 In: {hours}h {minutes}m"
    )

async def cmd_theme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    now = datetime.now(pytz.timezone(TIMEZONE))
    today_theme = WEEKLY_THEME.get(now.weekday(), "random")
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    schedule = "\n".join([f"{days[d]}: {t.capitalize()}" for d, t in WEEKLY_THEME.items()])
    await update.message.reply_text(f"🎨 Today: {today_theme.capitalize()}\n\n📅 Schedule:\n{schedule}")

async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        uid = int(ctx.args[0])
        users = load_users()
        if uid not in users:
            users.append(uid)
            save_users(users)
            await update.message.reply_text(f"✅ User {uid} added!")
        else:
            await update.message.reply_text("ℹ️ Already exists!")
    except:
        await update.message.reply_text("Usage: /adduser 123456789")

async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        uid = int(ctx.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Cannot remove admin!")
            return
        users = load_users()
        if uid in users:
            users.remove(uid)
            save_users(users)
            await update.message.reply_text(f"✅ User {uid} removed!")
        else:
            await update.message.reply_text("ℹ️ Not found!")
    except:
        await update.message.reply_text("Usage: /removeuser 123456789")

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    users = load_users()
    await update.message.reply_text("👥 Allowed users:\n" + "\n".join(str(u) for u in users))

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = bytes(await file.download_as_bytearray())

    seen = load_seen()
    h = image_hash(img_bytes)
    if h in seen:
        await update.message.reply_text("⚠️ Duplicate! Skipping.")
        return

    img_bytes = crop_to_square(img_bytes)
    h = image_hash(img_bytes)
    seen.add(h)
    save_seen(seen)

    queue = load_queue()
    queue.append({"bytes": list(img_bytes)})
    save_queue(queue)
    await update.message.reply_text(f"✅ Added!\n📦 Manual queue: {len(queue)} images")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("nextpost", cmd_nextpost))
    app.add_handler(CommandHandler("theme", cmd_theme))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("🤖 Random PfP Bot started!")
    await scheduler(app)

if __name__ == "__main__":
    asyncio.run(main())
