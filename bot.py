import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional
import discord
from discord.ext import commands
from flask import Flask, jsonify
from vinted import Vinted
# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("vinted-bot")
# ============================================================
# ENV
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN fehlt in den Render Environment Variables."
    )
# ============================================================
# FILES
# ============================================================
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
            logger.exception(
                "Konnte %s nicht lesen. Verwende Standardwert.",
                path,
            )
            return default
def save_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with file_lock:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )
        temp.replace(path)
config = load_json(
    CONFIG_FILE,
    DEFAULT_CONFIG.copy(),
)
seen_ids = set(
    str(x)
    for x in load_json(SEEN_FILE, [])
)
# ============================================================
# CONFIG REPAIR
# ============================================================
for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value
if not isinstance(config.get("brands"), list):
    config["brands"] = DEFAULT_CONFIG["brands"].copy()
config["brands"] = [
    str(x).strip()
    for x in config["brands"]
    if str(x).strip()
]
try:
    config["interval"] = max(
        60,
        int(config.get("interval", 300)),
    )
except (TypeError, ValueError):
    config["interval"] = 300
if config.get("max_price") is not None:
    try:
        config["max_price"] = float(
            config["max_price"]
        )
    except (TypeError, ValueError):
        config["max_price"] = None
save_json(CONFIG_FILE, config)
save_json(SEEN_FILE, sorted(seen_ids))
# ============================================================
# VINTED
# ============================================================
try:
    vinted = Vinted(domain="de")
    logger.info(
        "Vinted-Client wurde erfolgreich initialisiert."
    )
except Exception:
    vinted = None
    logger.exception(
        "Vinted-Client konnte nicht initialisiert werden. "
        "Der Bot bleibt online und versucht später erneut."
    )
vinted_blocked_until = 0.0
vinted_block_logged = False
class VintedBlockedError(Exception):
    """Vinted antwortet aktuell mit HTTP 403 oder 429."""
def exception_status_code(
    exc: BaseException,
) -> Optional[int]:
    response = getattr(
        exc,
        "response",
        None,
    )
    status = getattr(
        response,
        "status_code",
        None,
    )
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    text = str(exc)
    if "403" in text:
        return 403
    if "429" in text:
        return 429
    return None
def mark_vinted_blocked(
    seconds: int = 1800,
) -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = (
        time.time() + seconds
    )
    if not vinted_block_logged:
        logger.warning(
            "Vinted antwortet mit HTTP 403. "
            "Vinted-Abfragen werden für %d Minuten pausiert.",
            seconds // 60,
        )
        vinted_block_logged = True
def clear_vinted_block() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = 0.0
    vinted_block_logged = False
def vinted_is_blocked() -> bool:
    return (
        time.time()
        < vinted_blocked_until
    )
def vinted_search_sync(
    brand: str,
):
    global vinted
    if vinted_is_blocked():
        raise VintedBlockedError(
            "Vinted ist wegen HTTP 403 temporär pausiert."
        )
    if vinted is None:
        try:
            vinted = Vinted(
                domain="de"
            )
        except Exception as exc:
            status = exception_status_code(
                exc
            )
            if status == 403:
                mark_vinted_blocked()
                raise VintedBlockedError(
                    "Vinted liefert HTTP 403."
                ) from exc
            raise
    try:
        result = vinted.search(
            query=brand,
            order="newest_first",
            per_page=int(
                config.get(
                    "results_per_brand",
                    20,
                )
            ),
            price_to=config.get(
                "max_price"
            ),
        )
        clear_vinted_block()
        return result
    except Exception as exc:
        status = exception_status_code(
            exc
        )
        if status == 403:
            mark_vinted_blocked()
            raise VintedBlockedError(
                "Vinted liefert HTTP 403."
            ) from exc
        if status == 429:
            mark_vinted_blocked(
                900
            )
            raise VintedBlockedError(
                "Vinted liefert HTTP 429. "
                "Abfrage wird pausiert."
            ) from exc
        raise
# ============================================================
# VINTED ITEM HELPERS
# ============================================================
def get_value(
    obj: Any,
    *names: str,
    default: Any = None,
) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(
                obj,
                name,
            )
            if value is not None:
                return value
    return default
def get_items(
    result: Any,
) -> list:
    if result is None:
        return []
    direct = get_value(
        result,
        "items",
        default=None,
    )
    if isinstance(
        direct,
        list,
    ):
        return direct
    dtos = get_value(
        result,
        "dtos",
        default=None,
    )
    if dtos is not None:
        items = get_value(
            dtos,
            "items",
            default=None,
        )
        if isinstance(
            items,
            list,
        ):
            return items
    return []
def item_id(
    item: Any,
) -> Optional[str]:
    value = get_value(
        item,
        "id",
        default=None,
    )
    if value is None:
        return None
    return str(value)
def item_title(
    item: Any,
) -> str:
    return str(
        get_value(
            item,
            "title",
            "name",
            default="Vinted-Angebot",
        )
    )
def item_price(
    item: Any,
) -> str:
    price = get_value(
        item,
        "price",
        default=None,
    )
    if isinstance(
        price,
        dict,
    ):
        amount = get_value(
            price,
            "amount",
            "value",
            default=None,
        )
        currency = get_value(
            price,
            "currency_code",
            "currency",
            default="EUR",
        )
    else:
        amount = price
        currency = get_value(
            item,
            "currency_code",
            "currency",
            default="EUR",
        )
    if amount is None:
        return "Preis unbekannt"
    return f"{amount} {currency}"
def item_url(
    item: Any,
) -> Optional[str]:
    return get_value(
        item,
        "url",
        "item_url",
        "web_url",
        default=None,
    )
def item_photo(
    item: Any,
) -> Optional[str]:
    photo = get_value(
        item,
        "photo",
        "photo_url",
        "image_url",
        default=None,
    )
    if isinstance(
        photo,
        dict,
    ):
        return get_value(
            photo,
            "url",
            "full_size_url",
            "full_size",
            default=None,
        )
    return photo
def item_brand(
    item: Any,
) -> Optional[str]:
    return get_value(
        item,
        "brand_title",
        "brand",
        default=None,
    )
# ============================================================
# DISCORD
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)
def get_target_channel():
    channel_id = config.get(
        "channel_id"
    )
    if not channel_id:
        return None
    try:
        channel_id = int(channel_id)
    except (
        TypeError,
        ValueError,
    ):
        return None
    return bot.get_channel(
        channel_id
    )
def build_item_embed(
    item: Any,
    brand: str,
) -> discord.Embed:
    title = item_title(item)
    price = item_price(item)
    url = item_url(item)
    photo = item_photo(item)
    actual_brand = (
        item_brand(item)
        or brand
    )
    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**Marke:** {actual_brand}\n"
            f"**Preis:** {price}"
        ),
    )
    if (
        isinstance(url, str)
        and url.startswith("http")
    ):
        embed.url = url
    if (
        isinstance(photo, str)
        and photo.startswith("http")
    ):
        embed.set_thumbnail(
            url=photo
        )
    embed.set_footer(
        text="Vinted Monitor"
    )
    return embed
# ============================================================
# SEARCH
# ============================================================
def search_brand_sync(
    brand: str,
) -> list:
    result = vinted_search_sync(
        brand
    )
    return get_items(
        result
    )
async def search_brand(
    brand: str,
) -> list:
    return await asyncio.to_thread(
        search_brand_sync,
        brand,
    )
async def create_initial_baseline():
    if seen_ids:
        return
    logger.info(
        "Erstelle initiale Vinted-Baseline..."
    )
    found_any = False
    for brand in list(
        config["brands"]
    ):
        try:
            items = await search_brand(
                brand
            )
            for item in items:
                iid = item_id(
                    item
                )
                if iid:
                    seen_ids.add(
                        iid
                    )
                    found_any = True
            logger.info(
                "Baseline für %s: %d Angebote.",
                brand,
                len(items),
            )
        except VintedBlockedError as exc:
            logger.warning(
                "Baseline für %s pausiert: %s",
                brand,
                exc,
            )
            break
        except Exception:
            logger.exception(
                "Baseline-Fehler bei %s.",
                brand,
            )
        await asyncio.sleep(1)
    save_json(
        SEEN_FILE,
        sorted(seen_ids),
    )
    if found_any:
        logger.info(
            "Initiale Baseline gespeichert."
        )
    else:
        logger.info(
            "Keine Baseline gespeichert. "
            "Falls Vinted aktuell blockiert, "
            "wird später erneut versucht."
        )
async def check_brand(
    brand: str,
):
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
        items = await search_brand(
            brand
        )
    except VintedBlockedError as exc:
        logger.warning(
            "Vinted-Abfrage für %s pausiert: %s",
            brand,
            exc,
        )
        return
    except Exception:
        logger.exception(
            "Fehler bei Vinted-Suche für %s.",
            brand,
        )
        return
    new_items = []
    for item in items:
        iid = item_id(
            item
        )
        if iid is None:
            continue
        if iid not in seen_ids:
            seen_ids.add(
                iid
            )
            new_items.append(
                item
            )
    if new_items:
        save_json(
            SEEN_FILE,
            sorted(seen_ids),
        )
    for item in reversed(
        new_items
    ):
        try:
            await channel.send(
                embed=build_item_embed(
                    item,
                    brand,
                )
            )
        except discord.Forbidden:
            logger.error(
                "Keine Berechtigung, "
                "in den Zielkanal zu schreiben."
            )
            break
        except discord.HTTPException:
            logger.exception(
                "Discord-Fehler beim Senden."
            )
        await asyncio.sleep(
            0.5
        )
    if new_items:
        logger.info(
            "%d neue Angebote für %s gefunden.",
            len(new_items),
            brand,
        )
# ============================================================
# AUTOMATIC FINDER
# ============================================================
automatic_task = None
async def automatic_finder():
    await bot.wait_until_ready()
    logger.info(
        "Automatischer Vinted-Finder gestartet."
    )
    await create_initial_baseline()
    while not bot.is_closed():
        interval = max(
            60,
            int(
                config.get(
                    "interval",
                    300,
                )
            ),
        )
        if not config.get(
            "brands"
        ):
            logger.warning(
                "Keine Marken konfiguriert."
            )
        elif vinted_is_blocked():
            remaining = max(
                1,
                int(
                    vinted_blocked_until
                    - time.time()
                ),
            )
            logger.warning(
                "Vinted pausiert noch %d Sekunden.",
                remaining,
            )
        else:
            for brand in list(
                config.get(
                    "brands",
                    [],
                )
            ):
                if bot.is_closed():
                    return
                if vinted_is_blocked():
                    break
                await check_brand(
                    brand
                )
                await asyncio.sleep(
                    1
                )
        await asyncio.sleep(
            interval
        )
# ============================================================
# DISCORD EVENTS
# ============================================================
@bot.event
async def on_ready():
    global automatic_task
    logger.info(
        "Discord verbunden als %s (%s).",
        bot.user,
        bot.user.id
        if bot.user
        else "?",
    )
    if (
        automatic_task is None
        or automatic_task.done()
    ):
        automatic_task = asyncio.create_task(
            automatic_finder()
        )
# ============================================================
# ADMIN CHECK
# ============================================================
def admin_only():
    async def predicate(
        ctx: commands.Context,
    ) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send(
            "❌ Du brauchst die Berechtigung "
            "**Server verwalten**."
        )
        return False
    return commands.check(
        predicate
    )
# ============================================================
# HELP
# ============================================================
@bot.command(
    name="help"
)
async def help_command(
    ctx: commands.Context,
):
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
    await ctx.send(
        embed=embed
    )
# ============================================================
# CONFIG
# ============================================================
@bot.group(
    name="config",
    invoke_without_command=True,
)
@admin_only()
async def config_group(
    ctx: commands.Context,
):
    await ctx.send(
        "Nutze `!config show`, "
        "`!config brands` oder `!help`."
    )
@config_group.command(
    name="show"
)
@admin_only()
async def config_show(
    ctx: commands.Context,
):
    channel_id = config.get(
        "channel_id"
    )
    channel_text = "nicht gesetzt"
    if channel_id:
        try:
            channel = bot.get_channel(
                int(channel_id)
            )
            channel_text = (
                channel.mention
                if channel
                else str(channel_id)
            )
        except (
            TypeError,
            ValueError,
        ):
            pass
    max_price = config.get(
        "max_price"
    )
    max_price_text = (
        "aus"
        if max_price is None
        else f"{max_price:.2f} €"
    )
    embed = discord.Embed(
        title="Vinted-Konfiguration",
        description=(
            f"**Marken:** "
            f"{', '.join(config['brands']) or 'keine'}\n"
            f"**Kanal:** {channel_text}\n"
            f"**Intervall:** "
            f"{config['interval']} Sekunden\n"
            f"**Max. Preis:** "
            f"{max_price_text}\n"
            f"**Angebote/Marke:** "
            f"{config['results_per_brand']}\n"
        ),
    )
    await ctx.send(
        embed=embed
    )
@config_group.command(
    name="brands"
)
@admin_only()
async def config_brands(
    ctx: commands.Context,
):
    brands = config.get(
        "brands",
        [],
    )
    if not brands:
        await ctx.send(
            "Keine Marken konfiguriert."
        )
        return
    await ctx.send(
        "**Aktive Marken:**\n"
        + "\n".join(
            f"• {brand}"
            for brand in brands
        )
    )
@config_group.group(
    name="brand",
    invoke_without_command=True,
)
@admin_only()
async def config_brand(
    ctx: commands.Context,
):
    await ctx.send(
        "Nutze `!config brand add <Marke>` "
        "oder `!config brand remove <Marke>`."
    )
@config_brand.command(
    name="add"
)
@admin_only()
async def config_brand_add(
    ctx: commands.Context,
    *,
    brand: str,
):
    brand = brand.strip()
    if not brand:
        await ctx.send(
            "❌ Keine Marke angegeben."
        )
        return
    if brand.lower() in {
        x.lower()
        for x in config["brands"]
    }:
        await ctx.send(
            "❌ Diese Marke ist bereits vorhanden."
        )
        return
    config["brands"].append(
        brand
    )
    save_json(
        CONFIG_FILE,
        config,
    )
    await ctx.send(
        f"✅ **{brand}** wurde hinzugefügt."
    )
@config_brand.command(
    name="remove"
)
@admin_only()
async def config_brand_remove(
    ctx: commands.Context,
    *,
    brand: str,
):
    brand = brand.strip()
    old = config["brands"]
    new = [
        x
        for x in old
        if x.lower() != brand.lower()
    ]
    if len(new) == len(old):
        await ctx.send(
            "❌ Diese Marke ist nicht konfiguriert."
        )
        return
    config["brands"] = new
    save_json(
        CONFIG_FILE,
        config,
    )
    await ctx.send(
        f"✅ **{brand}** wurde entfernt."
    )
@config_group.command(
    name="channel"
)
@admin_only()
async def config_channel(
    ctx: commands.Context,
    channel: discord.TextChannel,
):
    config["channel_id"] = channel.id
    save_json(
        CONFIG_FILE,
        config,
    )
    await ctx.send(
        f"✅ Zielkanal ist jetzt "
        f"{channel.mention}."
    )
@config_group.command(
    name="interval"
)
@admin_only()
async def config_interval(
    ctx: commands.Context,
    seconds: int,
):
    if seconds < 60:
        await ctx.send(
            "❌ Das Minimum beträgt "
            "**60 Sekunden**."
        )
        return
    if seconds > 86400:
        await ctx.send(
            "❌ Das Maximum beträgt "
            "**86400 Sekunden**."
        )
        return
    config["interval"] = seconds
    save_json(
        CONFIG_FILE,
        config,
    )
    await ctx.send(
        f"✅ Prüfintervall auf "
        f"**{seconds} Sekunden** gesetzt."
    )
@config_group.command(
    name="maxprice"
)
@admin_only()
async def config_maxprice(
    ctx: commands.Context,
    value: str,
):
    if value.lower() == "off":
        config["max_price"] = None
        save_json(
            CONFIG_FILE,
            config,
        )
        await ctx.send(
            "✅ Preislimit deaktiviert."
        )
        return
    try:
        price = float(
            value.replace(
                ",",
                ".",
            )
        )
    except ValueError:
        await ctx.send(
            "❌ Ungültiger Preis. "
            "Beispiel: `!config maxprice 50`"
        )
        return
    if price <= 0:
        await ctx.send(
            "❌ Der Preis muss größer "
            "als 0 sein."
        )
        return
    config["max_price"] = price
    save_json(
        CONFIG_FILE,
        config,
    )
    await ctx.send(
        f"✅ Maximalpreis auf "
        f"**{price:.2f} €** gesetzt."
    )
# ============================================================
# MANUAL SEARCH
# ============================================================
@bot.command(
    name="search"
)
@admin_only()
async def manual_search(
    ctx: commands.Context,
    *,
    brand: str,
):
    brand = brand.strip()
    if not brand:
        await ctx.send(
            "❌ Beispiel: `!search Nike`"
        )
        return
    if brand.lower() == "all":
        brands = list(
            config.get(
                "brands",
                [],
            )
        )
    else:
        brands = [brand]
    await ctx.send(
        "🔎 Suche nach aktuellen "
        "Vinted-Angeboten..."
    )
    total = 0
    for current_brand in brands:
        try:
            items = await search_brand(
                current_brand
            )
        except VintedBlockedError as exc:
            await ctx.send(
                f"⚠️ Vinted ist aktuell "
                f"nicht verfügbar: `{exc}`"
            )
            return
        except Exception:
            logger.exception(
                "Manuelle Suche fehlgeschlagen: %s",
                current_brand,
            )
            await ctx.send(
                f"❌ Fehler bei "
                f"**{current_brand}**."
            )
            continue
        if not items:
            await ctx.send(
                f"Keine Angebote für "
                f"**{current_brand}** gefunden."
            )
            continue
        for item in items[:10]:
            try:
                await ctx.send(
                    embed=build_item_embed(
                        item,
                        current_brand,
                    )
                )
                total += 1
            except discord.HTTPException:
                logger.exception(
                    "Discord-Fehler bei manueller Suche."
                )
                break
            await asyncio.sleep(
                0.4
            )
    await ctx.send(
        f"✅ Manuelle Suche beendet. "
        f"Angezeigt: **{total}**."
    )
# ============================================================
# STATUS
# ============================================================
@bot.command(
    name="status"
)
async def status_command(
    ctx: commands.Context,
):
    if vinted_is_blocked():
        remaining = max(
            0,
            int(
                vinted_blocked_until
                - time.time()
            ),
        )
        vinted_status = (
            f"⏸️ pausiert "
            f"({remaining}s verbleibend)"
        )
    elif vinted is None:
        vinted_status = (
            "⚠️ nicht initialisiert"
        )
    else:
        vinted_status = (
            "🟢 bereit"
        )
    channel_id = config.get(
        "channel_id"
    )
    channel = None
    if channel_id:
        try:
            channel = bot.get_channel(
                int(channel_id)
            )
        except (
            TypeError,
            ValueError,
        ):
            channel = None
    embed = discord.Embed(
        title="Bot-Status",
        description=(
            f"**Discord:** 🟢 online\n"
            f"**Vinted:** {vinted_status}\n"
            f"**Marken:** "
            f"{len(config.get('brands', []))}\n"
            f"**Gesehene Angebote:** "
            f"{len(seen_ids)}\n"
            f"**Intervall:** "
            f"{config.get('interval', 300)}s\n"
            f"**Zielkanal:** "
            f"{channel.mention if channel else 'nicht gesetzt'}"
        ),
    )
    await ctx.send(
        embed=embed
    )
# ============================================================
# COMMAND ERRORS
# ============================================================
@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
):
    if isinstance(
        error,
        commands.CommandNotFound,
    ):
        return
    if isinstance(
        error,
        commands.MissingRequiredArgument,
    ):
        await ctx.send(
            "❌ Es fehlt ein Argument. "
            "Nutze `!help`."
        )
        return
    if isinstance(
        error,
        commands.BadArgument,
    ):
        await ctx.send(
            "❌ Ungültiges Argument. "
            "Nutze `!help`."
        )
        return
    if isinstance(
        error,
        commands.CheckFailure,
    ):
        return
    logger.exception(
        "Unhandled command error: %s",
        error,
    )
    await ctx.send(
        "❌ Bei dem Befehl ist "
        "ein Fehler aufgetreten."
    )
# ============================================================
# FLASK / RENDER
# ============================================================
app = Flask(__name__)
@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "vinted-discord-bot",
            "discord": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
            "brands": config.get(
                "brands",
                [],
            ),
        }
    )
@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "discord_ready": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
        }
    )
def run_flask():
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
# ============================================================
# MAIN
# ============================================================
def main():
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )
    flask_thread.start()
    logger.info(
        "Flask-Healthserver gestartet."
    )
    logger.info(
        "Starte Discord-Bot..."
    )
    bot.run(
        DISCORD_TOKEN
    )
if __name__ == "__main__":
    main()
