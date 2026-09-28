import asyncio
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands
from flask import Flask, jsonify
import cloudscraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("vinted-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN fehlt in den Render Environment Variables."
    )

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE = DATA_DIR / "seen.json"

DEFAULT_CONFIG = {
    "brands": [
        "Nike",
        "Ralph Lauren",
        "Adidas",
        "Tommy Hilfiger",
        "Lacoste",
        "Carhartt",
    ],
    "channel_id": None,
    "interval": 300,
    "max_price": None,
    "results_per_brand": 20,
}

file_lock = threading.Lock()


def load_json(path: Path, default: Any) -> Any:
    with file_lock:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Konnte %s nicht lesen.", path)
            return default


def save_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with file_lock:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(path)


config = load_json(CONFIG_FILE, DEFAULT_CONFIG.copy())
seen_ids = set(str(x) for x in load_json(SEEN_FILE, []))

for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value

if not isinstance(config.get("brands"), list):
    config["brands"] = DEFAULT_CONFIG["brands"].copy()

config["brands"] = [str(x).strip() for x in config["brands"] if str(x).strip()]

try:
    config["interval"] = max(60, int(config.get("interval", 300)))
except (TypeError, ValueError):
    config["interval"] = 300

if config.get("max_price") is not None:
    try:
        config["max_price"] = float(config["max_price"])
    except (TypeError, ValueError):
        config["max_price"] = None

save_json(CONFIG_FILE, config)
save_json(SEEN_FILE, sorted(seen_ids))


VINTED_DOMAIN = "de"
VINTED_BASE_URL = f"https://www.vinted.{VINTED_DOMAIN}"
VINTED_403_COOLDOWN = 5 * 60
VINTED_429_COOLDOWN = 15 * 60
VINTED_REQUEST_TIMEOUT = 30
VINTED_MAX_RETRIES_PER_REQUEST = 2


class VintedBlockedError(Exception):
    pass


class VintedSession:
    def __init__(self) -> None:
        self.scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
            },
            interpreter="native",
            delay=random.uniform(2, 5),
        )
        self.scraper.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
        )

    def warmup(self) -> None:
        try:
            self.scraper.get(
                f"{VINTED_BASE_URL}/",
                timeout=VINTED_REQUEST_TIMEOUT,
            )
        except Exception:
            pass

    def search(
        self,
        query: str,
        order: str = "newest_first",
        per_page: int = 20,
        price_to: Optional[float] = None,
    ) -> dict:
        params = {
            "search_text": query,
            "order": order,
            "per_page": int(per_page),
        }
        if price_to is not None:
            params["price_to"] = price_to

        url = f"{VINTED_BASE_URL}/api/v2/catalog/items"
        last_status: Optional[int] = None

        for attempt in range(1, VINTED_MAX_RETRIES_PER_REQUEST + 1):
            try:
                resp = self.scraper.get(
                    url,
                    params=params,
                    timeout=VINTED_REQUEST_TIMEOUT,
                )
            except Exception as exc:
                if attempt >= VINTED_MAX_RETRIES_PER_REQUEST:
                    raise
                time.sleep(random.uniform(2, 5))
                continue

            last_status = resp.status_code

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise RuntimeError(
                        "Vinted-Antwort war kein gültiges JSON."
                    )

            if resp.status_code in (403, 429):
                err = RuntimeError(
                    f"Vinted HTTP {resp.status_code}"
                )
                setattr(err, "response", resp)
                raise err

            if resp.status_code >= 500:
                if attempt >= VINTED_MAX_RETRIES_PER_REQUEST:
                    err = RuntimeError(
                        f"Vinted HTTP {resp.status_code}"
                    )
                    setattr(err, "response", resp)
                    raise err
                time.sleep(random.uniform(3, 8))
                continue

            err = RuntimeError(
                f"Vinted HTTP {resp.status_code}"
            )
            setattr(err, "response", resp)
            raise err

        err = RuntimeError(
            f"Vinted HTTP {last_status} ohne Erfolg"
        )
        raise err


vinted: Optional[VintedSession] = None
vinted_blocked_until = 0.0
vinted_block_logged = False
vinted_lock = threading.Lock()


def exception_status_code(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            try:
                return int(status_code)
            except (TypeError, ValueError):
                pass
    message = str(exc)
    if "403" in message:
        return 403
    if "429" in message:
        return 429
    return None


def create_vinted_session() -> VintedSession:
    logger.info("Erstelle neue Vinted-Session...")
    session = VintedSession()
    session.warmup()
    logger.info("Neue Vinted-Session erstellt.")
    return session


def reset_vinted_session() -> None:
    global vinted
    with vinted_lock:
        vinted = None
    logger.info("Vinted-Session wurde verworfen.")


def mark_vinted_403() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = time.time() + VINTED_403_COOLDOWN
    if not vinted_block_logged:
        logger.warning(
            "Vinted HTTP 403. Session verworfen. "
            "Nächster Versuch in %d Minuten.",
            VINTED_403_COOLDOWN // 60,
        )
        vinted_block_logged = True


def mark_vinted_429() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = time.time() + VINTED_429_COOLDOWN
    if not vinted_block_logged:
        logger.warning(
            "Vinted HTTP 429. Nächster Versuch in %d Minuten.",
            VINTED_429_COOLDOWN // 60,
        )
        vinted_block_logged = True


def clear_vinted_block() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = 0.0
    vinted_block_logged = False


def vinted_is_blocked() -> bool:
    return time.time() < vinted_blocked_until


def vinted_block_remaining() -> int:
    return max(0, int(vinted_blocked_until - time.time()))


def vinted_search_sync(brand: str) -> dict:
    global vinted

    if vinted_is_blocked():
        raise VintedBlockedError(
            f"Vinted pausiert noch {vinted_block_remaining()}s."
        )

    with vinted_lock:
        if vinted is None:
            try:
                vinted = create_vinted_session()
            except Exception as exc:
                status = exception_status_code(exc)
                if status == 403:
                    mark_vinted_403()
                    raise VintedBlockedError(
                        "Vinted verweigert die neue Session "
                        "mit HTTP 403."
                    ) from exc
                raise
        session = vinted

    try:
        result = session.search(
            query=brand,
            order="newest_first",
            per_page=int(config.get("results_per_brand", 20)),
            price_to=config.get("max_price"),
        )
        clear_vinted_block()
        return result
    except Exception as exc:
        status = exception_status_code(exc)
        if status == 403:
            reset_vinted_session()
            mark_vinted_403()
            raise VintedBlockedError("Vinted HTTP 403.") from exc
        if status == 429:
            reset_vinted_session()
            mark_vinted_429()
            raise VintedBlockedError("Vinted HTTP 429.") from exc
        raise


async def search_brand(brand: str) -> list:
    result = await asyncio.to_thread(vinted_search_sync, brand)
    return get_items(result)


def get_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def get_items(result: Any) -> list:
    if result is None:
        return []
    direct = get_value(result, "items", default=None)
    if isinstance(direct, list):
        return direct
    dtos = get_value(result, "dtos", default=None)
    if dtos is not None:
        items = get_value(dtos, "items", default=None)
        if isinstance(items, list):
            return items
    return []


def item_id(item: Any) -> Optional[str]:
    value = get_value(item, "id", default=None)
    if value is None:
        return None
    return str(value)


def item_title(item: Any) -> str:
    return str(
        get_value(item, "title", "name", default="Vinted-Angebot")
    )


def item_price(item: Any) -> str:
    price = get_value(item, "price", default=None)
    if isinstance(price, dict):
        amount = get_value(price, "amount", "value", default=None)
        currency = get_value(
            price, "currency_code", "currency", default="EUR"
        )
    else:
        amount = price
        currency = get_value(
            item, "currency_code", "currency", default="EUR"
        )
    if amount is None:
        return "Preis unbekannt"
    return f"{amount} {currency}"


def item_url(item: Any) -> Optional[str]:
    return get_value(item, "url", "item_url", "web_url", default=None)


def item_photo(item: Any) -> Optional[str]:
    photo = get_value(
        item, "photo", "photo_url", "image_url", default=None
    )
    if isinstance(photo, dict):
        return get_value(
            photo, "url", "full_size_url", "full_size", default=None
        )
    return photo


def item_brand(item: Any) -> Optional[str]:
    return get_value(item, "brand_title", "brand", default=None)


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


def get_target_channel():
    channel_id = config.get("channel_id")
    if not channel_id:
        return None
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return None
    return bot.get_channel(channel_id)


def build_item_embed(item: Any, brand: str) -> discord.Embed:
    title = item_title(item)
    price = item_price(item)
    url = item_url(item)
    photo = item_photo(item)
    actual_brand = item_brand(item) or brand

    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**Marke:** {actual_brand}\n" f"**Preis:** {price}"
        ),
    )
    if isinstance(url, str) and url.startswith("http"):
        embed.url = url
    if isinstance(photo, str) and photo.startswith("http"):
        embed.set_thumbnail(url=photo)
    embed.set_footer(text="Vinted Monitor")
    return embed


async def check_brand(brand: str) -> None:
    if vinted_is_blocked():
        return

    channel = get_target_channel()
    if channel is None:
        logger.warning(
            "Kein Discord-Zielkanal gesetzt. "
            "Nutze !config channel #kanal."
        )
        return

    try:
        items = await search_brand(brand)
    except VintedBlockedError as exc:
        logger.warning(
            "Vinted-Suche für %s pausiert: %s", brand, exc
        )
        return
    except Exception:
        logger.exception("Fehler bei Vinted-Suche für %s.", brand)
        return

    new_items = []
    for item in items:
        iid = item_id(item)
        if iid is None:
            continue
        if iid not in seen_ids:
            seen_ids.add(iid)
            new_items.append(item)

    if new_items:
        save_json(SEEN_FILE, sorted(seen_ids))

    for item in reversed(new_items):
        try:
            await channel.send(embed=build_item_embed(item, brand))
        except discord.Forbidden:
            logger.error(
                "Keine Berechtigung, in den Zielkanal zu schreiben."
            )
            break
        except discord.HTTPException:
            logger.exception("Discord-Fehler beim Senden.")
        await asyncio.sleep(0.5)

    if new_items:
        logger.info(
            "%d neue Angebote für %s gefunden.", len(new_items), brand
        )


automatic_task: Optional[asyncio.Task] = None


async def automatic_finder() -> None:
    await bot.wait_until_ready()
    logger.info("Automatischer Vinted-Finder gestartet.")

    while not bot.is_closed():
        interval = max(60, int(config.get("interval", 300)))

        if vinted_is_blocked():
            remaining = vinted_block_remaining()
            logger.info(
                "Vinted pausiert noch %d Sekunden. "
                "Überspringe kompletten Scan.",
                remaining,
            )
            await asyncio.sleep(min(60, max(1, remaining)))
            if not vinted_is_blocked():
                logger.info(
                    "Vinted-Cooldown beendet. "
                    "Beim nächsten Scan wird eine neue Session erstellt."
                )
            continue

        brands = list(config.get("brands", []))
        if not brands:
            logger.warning("Keine Marken konfiguriert.")
        else:
            for brand in brands:
                if bot.is_closed():
                    return
                if vinted_is_blocked():
                    logger.warning(
                        "Scan abgebrochen wegen Vinted-Sperre. "
                        "Restliche Marken werden übersprungen."
                    )
                    break
                await check_brand(brand)
                await asyncio.sleep(1)

        logger.info(
            "Scan beendet. Nächster Scan in %d Sekunden.", interval
        )
        await asyncio.sleep(interval)


@bot.event
async def on_ready() -> None:
    global automatic_task
    logger.info(
        "Discord verbunden als %s (%s).",
        bot.user,
        bot.user.id if bot.user else "?",
    )
    if automatic_task is None or automatic_task.done():
        automatic_task = asyncio.create_task(automatic_finder())


def admin_only():
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send(
            "❌ Du brauchst die Berechtigung **Server verwalten**."
        )
        return False

    return commands.check(predicate)


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="Vinted Monitor",
        description=(
            "`!config show`\n"
            "`!config brands`\n"
            "`!config brand add <Marke>`\n"
            "`!config brand remove <Marke>`\n"
            "`!config channel #kanal`\n"
            "`!config interval <Sekunden>`\n"
            "`!config maxprice <Preis>`\n"
            "`!config maxprice off`\n"
            "`!search <Marke>`\n"
            "`!search all`\n"
            "`!status`"
        ),
    )
    await ctx.send(embed=embed)


@bot.group(name="config", invoke_without_command=True)
@admin_only()
async def config_group(ctx: commands.Context) -> None:
    await ctx.send(
        "Nutze `!config show`, `!config brands` oder `!help`."
    )


@config_group.command(name="show")
@admin_only()
async def config_show(ctx: commands.Context) -> None:
    channel_id = config.get("channel_id")
    channel_text = "nicht gesetzt"
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
            channel_text = (
                channel.mention if channel else str(channel_id)
            )
        except (TypeError, ValueError):
            pass

    max_price = config.get("max_price")
    max_price_text = (
        "aus" if max_price is None else f"{max_price:.2f} €"
    )

    embed = discord.Embed(
        title="Vinted-Konfiguration",
        description=(
            f"**Marken:** "
            f"{', '.join(config['brands']) or 'keine'}\n"
            f"**Kanal:** {channel_text}\n"
            f"**Intervall:** {config['interval']} Sekunden\n"
            f"**Max. Preis:** {max_price_text}\n"
            f"**Angebote/Marke:** {config['results_per_brand']}"
        ),
    )
    await ctx.send(embed=embed)


@config_group.command(name="brands")
@admin_only()
async def config_brands(ctx: commands.Context) -> None:
    brands = config.get("brands", [])
    if not brands:
        await ctx.send("Keine Marken konfiguriert.")
        return
    await ctx.send(
        "**Aktive Marken:**\n"
        + "\n".join(f"• {brand}" for brand in brands)
    )


@config_group.group(name="brand", invoke_without_command=True)
@admin_only()
async def config_brand(ctx: commands.Context) -> None:
    await ctx.send(
        "Nutze `!config brand add <Marke>` "
        "oder `!config brand remove <Marke>`."
    )


@config_brand.command(name="add")
@admin_only()
async def config_brand_add(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    if not brand:
        await ctx.send("❌ Keine Marke angegeben.")
        return

    existing = {x.lower() for x in config["brands"]}
    if brand.lower() in existing:
        await ctx.send("❌ Diese Marke ist bereits vorhanden.")
        return

    config["brands"].append(brand)
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ **{brand}** wurde hinzugefügt.")


@config_brand.command(name="remove")
@admin_only()
async def config_brand_remove(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    old = config["brands"]
    new = [x for x in old if x.lower() != brand.lower()]
    if len(new) == len(old):
        await ctx.send("❌ Diese Marke ist nicht konfiguriert.")
        return

    config["brands"] = new
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ **{brand}** wurde entfernt.")


@config_group.command(name="channel")
@admin_only()
async def config_channel(
    ctx: commands.Context, channel: discord.TextChannel
) -> None:
    config["channel_id"] = channel.id
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Zielkanal ist jetzt {channel.mention}.")


@config_group.command(name="interval")
@admin_only()
async def config_interval(
    ctx: commands.Context, seconds: int
) -> None:
    if seconds < 60:
        await ctx.send("❌ Minimum: **60 Sekunden**.")
        return
    if seconds > 86400:
        await ctx.send("❌ Maximum: **86400 Sekunden**.")
        return

    config["interval"] = seconds
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Prüfintervall: **{seconds} Sekunden**.")


@config_group.command(name="maxprice")
@admin_only()
async def config_maxprice(
    ctx: commands.Context, value: str
) -> None:
    if value.lower() == "off":
        config["max_price"] = None
        save_json(CONFIG_FILE, config)
        await ctx.send("✅ Preislimit deaktiviert.")
        return

    try:
        price = float(value.replace(",", "."))
    except ValueError:
        await ctx.send("❌ Ungültiger Preis.")
        return

    if price <= 0:
        await ctx.send("❌ Der Preis muss größer als 0 sein.")
        return

    config["max_price"] = price
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Maximalpreis: **{price:.2f} €**.")


@bot.command(name="search")
@admin_only()
async def manual_search(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    if not brand:
        await ctx.send("❌ Beispiel: `!search Nike`")
        return

    if vinted_is_blocked():
        remaining = vinted_block_remaining()
        await ctx.send(
            "⚠️ Vinted ist momentan pausiert. "
            f"Neuer Versuch in **{remaining // 60}m "
            f"{remaining % 60}s**."
        )
        return

    if brand.lower() == "all":
        brands = list(config.get("brands", []))
    else:
        brands = [brand]

    await ctx.send("🔎 Suche nach aktuellen Vinted-Angeboten...")

    total = 0
    for current_brand in brands:
        if vinted_is_blocked():
            await ctx.send(
                "⚠️ Vinted hat die Session gesperrt. "
                "Suche abgebrochen, Bot pausiert für "
                f"**{vinted_block_remaining() // 60} Minuten**."
            )
            return
        try:
            items = await search_brand(current_brand)
        except VintedBlockedError as exc:
            await ctx.send(
                "⚠️ Vinted ist momentan nicht verfügbar.\n"
                f"`{exc}`"
            )
            return
        except Exception:
            logger.exception("Manuelle Suche fehlgeschlagen.")
            await ctx.send(f"❌ Fehler bei **{current_brand}**.")
            continue

        if not items:
            await ctx.send(
                f"Keine Angebote für **{current_brand}** gefunden."
            )
            continue

        for item in items[:10]:
            try:
                await ctx.send(
                    embed=build_item_embed(item, current_brand)
                )
                total += 1
            except discord.HTTPException:
                logger.exception("Discord-Fehler.")
                break
            await asyncio.sleep(0.4)

    await ctx.send(f"✅ Suche beendet. Angezeigt: **{total}**.")


@bot.command(name="status")
async def status_command(ctx: commands.Context) -> None:
    if vinted_is_blocked():
        remaining = vinted_block_remaining()
        minutes = remaining // 60
        seconds = remaining % 60
        vinted_status = (
            f"⏸️ pausiert – neue Session in "
            f"{minutes}m {seconds}s"
        )
    elif vinted is None:
        vinted_status = "🟡 neue Session beim nächsten Scan"
    else:
        vinted_status = "🟢 Session aktiv"

    channel_id = config.get("channel_id")
    channel = None
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
        except (TypeError, ValueError):
            pass

    embed = discord.Embed(
        title="Bot-Status",
        description=(
            f"**Discord:** 🟢 online\n"
            f"**Vinted:** {vinted_status}\n"
            f"**Marken:** {len(config.get('brands', []))}\n"
            f"**Gesehene Angebote:** {len(seen_ids)}\n"
            f"**Intervall:** {config.get('interval', 300)}s\n"
            f"**Zielkanal:** "
            f"{channel.mention if channel else 'nicht gesetzt'}"
        ),
    )
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Argument fehlt. Nutze `!help`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Ungültiges Argument. Nutze `!help`.")
        return
    if isinstance(error, commands.CheckFailure):
        return

    logger.exception("Unhandled command error: %s", error)
    await ctx.send("❌ Bei dem Befehl ist ein Fehler aufgetreten.")


app = Flask(__name__)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "vinted-discord-bot",
            "discord": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
            "vinted_pause_remaining": vinted_block_remaining(),
            "brands": config.get("brands", []),
        }
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "discord_ready": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
            "vinted_pause_remaining": vinted_block_remaining(),
        }
    )


def run_flask() -> None:
    port = int(os.getenv("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


def main() -> None:
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Render Healthserver gestartet.")
    logger.info("Starte Discord-Bot...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
