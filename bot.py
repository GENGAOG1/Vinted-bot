import os
import json
import asyncio
import logging
import threading
from pathlib import Path
import discord
from discord.ext import commands
from flask import Flask, jsonify
from vinted import Vinted
# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("vinted-bot")
# ============================================================
# ENVIRONMENT
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN wurde nicht gefunden."
    )
PORT = int(os.getenv("PORT", "10000"))
# ============================================================
# DATA
# ============================================================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE = DATA_DIR / "seen.json"
DEFAULT_CONFIG = {
    "brands": [
        "Nike",
        "Ralph Lauren",
        "Adidas",
        "Tommy Hilfiger",
        "Lacoste",
        "Carhartt"
    ],
    "channel_id": None,
    # Standard: 5 Minuten
    "interval": 300,
    # Kein Preislimit
    "max_price": None,
    # Anzahl der Vinted-Treffer pro Marke
    "results_per_brand": 20
}
# ============================================================
# JSON
# ============================================================
def load_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as file:
            return json.load(file)
    except Exception as error:
        logger.error(
            "Fehler beim Laden von %s: %s",
            path,
            error
        )
        return default
def save_json(path, data):
    try:
        with path.open(
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                data,
                file,
                indent=4,
                ensure_ascii=False
            )
    except Exception as error:
        logger.error(
            "Fehler beim Speichern von %s: %s",
            path,
            error
        )
config = load_json(
    CONFIG_FILE,
    DEFAULT_CONFIG.copy()
)
# Fehlende Einstellungen ergänzen
for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value
save_json(
    CONFIG_FILE,
    config
)
# ============================================================
# SEEN ITEMS
# ============================================================
seen_ids = set(
    load_json(
        SEEN_FILE,
        []
    )
)
def save_seen():
    """
    Speichert maximal die letzten 5000 IDs.
    """
    global seen_ids
    seen_ids = set(
        list(seen_ids)[-5000:]
    )
    save_json(
        SEEN_FILE,
        list(seen_ids)
    )
# ============================================================
# VINTED
# ============================================================
vinted = Vinted(
    domain="de"
)
# ============================================================
# DISCORD
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)
# ============================================================
# FLASK / RENDER
# ============================================================
app = Flask(__name__)
@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "Vinted Discord Bot"
    })
@app.route("/health")
def health():
    return jsonify({
        "status": "healthy"
    })
def run_web_server():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
# ============================================================
# OBJECT HELPERS
# ============================================================
def get_value(
    obj,
    name,
    default=None
):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(
            name,
            default
        )
    return getattr(
        obj,
        name,
        default
    )
def get_item_id(item):
    value = get_value(
        item,
        "id"
    )
    if value is None:
        return None
    return str(value)
def get_item_title(item):
    return str(
        get_value(
            item,
            "title",
            "Unbekannter Artikel"
        )
    )
def get_item_brand(item):
    return str(
        get_value(
            item,
            "brand_title",
            "Unbekannte Marke"
        )
    )
def get_item_price(item):
    value = get_value(
        item,
        "price"
    )
    if value is None:
        return None
    try:
        return float(
            str(value).replace(
                ",",
                "."
            )
        )
    except (
        ValueError,
        TypeError
    ):
        return None
def get_item_url(item):
    value = get_value(
        item,
        "url"
    )
    if value:
        return str(value)
    item_id = get_item_id(
        item
    )
    if item_id:
        return (
            "https://www.vinted.de/items/"
            + item_id
        )
    return None
def get_photo_url(item):
    photo = get_value(
        item,
        "photo"
    )
    if not photo:
        return None
    if isinstance(
        photo,
        str
    ):
        return photo
    for field in (
        "url",
        "full_size_url",
        "full_size",
        "image_url"
    ):
        value = get_value(
            photo,
            field
        )
        if value:
            return str(value)
    return None
# ============================================================
# VINTED SEARCH
# ============================================================
def search_brand_sync(brand):
    max_price = config.get(
        "max_price"
    )
    results_per_brand = int(
        config.get(
            "results_per_brand",
            20
        )
    )
    search_options = {
        "query": brand,
        "order": "newest_first",
        "per_page": results_per_brand
    }
    if max_price is not None:
        search_options["price_to"] = max_price
    logger.info(
        "Suche nach: %s",
        brand
    )
    response = vinted.search(
        **search_options
    )
    items = get_value(
        response,
        "items",
        []
    )
    return items or []
async def search_brand(brand):
    return await asyncio.to_thread(
        search_brand_sync,
        brand
    )
# ============================================================
# EMBED
# ============================================================
def create_item_embed(item):
    title = get_item_title(
        item
    )
    brand = get_item_brand(
        item
    )
    price = get_item_price(
        item
    )
    url = get_item_url(
        item
    )
    photo = get_photo_url(
        item
    )
    if price is not None:
        price_text = (
            f"{price:.2f} €"
        )
    else:
        price_text = (
            "Preis unbekannt"
        )
    embed = discord.Embed(
        title=title[:256],
        description="🛍️ Neuer Vinted-Artikel!",
        color=discord.Color.blurple()
    )
    if url:
        embed.url = url
    embed.add_field(
        name="🏷️ Marke",
        value=brand[:1024],
        inline=True
    )
    embed.add_field(
        name="💰 Preis",
        value=price_text,
        inline=True
    )
    if url:
        embed.add_field(
            name="🔗 Artikel",
            value=(
                f"[Auf Vinted öffnen]"
                f"({url})"
            ),
            inline=False
        )
    if photo:
        try:
            embed.set_image(
                url=photo
            )
        except Exception:
            pass
    embed.set_footer(
        text="Vinted Monitor"
    )
    return embed
# ============================================================
# SEND ITEM
# ============================================================
async def send_new_item(
    item,
    channel
):
    item_id = get_item_id(
        item
    )
    if not item_id:
        return False
    if item_id in seen_ids:
        return False
    embed = create_item_embed(
        item
    )
    try:
        await channel.send(
            embed=embed
        )
        seen_ids.add(
            item_id
        )
        return True
    except discord.Forbidden:
        logger.error(
            "Keine Berechtigung für #%s.",
            channel.name
        )
    except discord.HTTPException as error:
        logger.error(
            "Discord-Fehler: %s",
            error
        )
    return False
# ============================================================
# AUTOMATIC SEARCH LOOP
# ============================================================
async def automatic_finder():
    await bot.wait_until_ready()
    logger.info(
        "Automatische Vinted-Suche gestartet."
    )
    while not bot.is_closed():
        try:
            channel_id = config.get(
                "channel_id"
            )
            if not channel_id:
                logger.info(
                    "Noch kein Discord-Kanal konfiguriert."
                )
            else:
                channel = bot.get_channel(
                    int(channel_id)
                )
                if channel is None:
                    logger.warning(
                        "Discord-Kanal nicht gefunden."
                    )
                else:
                    brands = config.get(
                        "brands",
                        []
                    )
                    logger.info(
                        "Starte automatische Suche: %s",
                        ", ".join(brands)
                    )
                    total_new = 0
                    for brand in brands:
                        try:
                            items = await search_brand(
                                brand
                            )
                            logger.info(
                                "%s: %s Treffer",
                                brand,
                                len(items)
                            )
                            # Die Ergebnisse kommen bereits
                            # nach newest_first.
                            for item in reversed(items):
                                if await send_new_item(
                                    item,
                                    channel
                                ):
                                    total_new += 1
                                    # Kleine Discord-Pause
                                    await asyncio.sleep(
                                        1
                                    )
                        except Exception as error:
                            logger.error(
                                "Fehler bei Marke %s: %s",
                                brand,
                                error
                            )
                    save_seen()
                    logger.info(
                        "Suche beendet. Neue Artikel: %s",
                        total_new
                    )
        except Exception as error:
            logger.exception(
                "Fehler im Finder: %s",
                error
            )
        # ====================================================
        # WICHTIG:
        # Das Intervall wird HIER jedes Mal neu gelesen.
        # Dadurch funktioniert !config interval wirklich.
        # ====================================================
        interval = int(
            config.get(
                "interval",
                300
            )
        )
        # Sicherheitsminimum
        interval = max(
            60,
            interval
        )
        logger.info(
            "Nächste Suche in %s Sekunden.",
            interval
        )
        await asyncio.sleep(
            interval
        )
# ============================================================
# INITIAL BASELINE
# ============================================================
async def create_initial_baseline():
    global seen_ids
    # Wenn bereits IDs vorhanden sind,
    # brauchen wir keine neue Baseline.
    if seen_ids:
        return
    logger.info(
        "Erster Start - erstelle Baseline..."
    )
    brands = config.get(
        "brands",
        []
    )
    for brand in brands:
        try:
            items = await search_brand(
                brand
            )
            for item in items:
                item_id = get_item_id(
                    item
                )
                if item_id:
                    seen_ids.add(
                        item_id
                    )
        except Exception as error:
            logger.error(
                "Baseline-Fehler bei %s: %s",
                brand,
                error
            )
    save_seen()
    logger.info(
        "Baseline gespeichert: %s Artikel",
        len(seen_ids)
    )
# ============================================================
# READY
# ============================================================
@bot.event
async def on_ready():
    logger.info(
        "Discord verbunden als %s",
        bot.user
    )
    await create_initial_baseline()
    if not hasattr(
        bot,
        "finder_task"
    ):
        bot.finder_task = asyncio.create_task(
            automatic_finder()
        )
    logger.info(
        "Vinted Monitor ist aktiv."
    )
# ============================================================
# HELP
# ============================================================
@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🤖 Vinted Monitor",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="⚙️ Konfiguration",
        value=(
            "`!config show`\n"
            "`!config brand add Nike`\n"
            "`!config brand remove Nike`\n"
            "`!config brand list`\n"
            "`!config channel #kanal`\n"
            "`!config interval 300`\n"
            "`!config maxprice 50`\n"
            "`!config maxprice off`"
        ),
        inline=False
    )
    embed.add_field(
        name="🔎 Suche",
        value=(
            "`!search Nike`\n"
            "`!search all`"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Status",
        value="`!status`",
        inline=False
    )
    await ctx.send(
        embed=embed
    )
# ============================================================
# CONFIG
# ============================================================
@bot.group(
    name="config",
    invoke_without_command=True
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_command(ctx):
    await ctx.send(
        "Nutze `!config show`."
    )
# ============================================================
# CONFIG SHOW
# ============================================================
@config_command.command(
    name="show"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_show(ctx):
    brands = config.get(
        "brands",
        []
    )
    channel_id = config.get(
        "channel_id"
    )
    if channel_id:
        channel = bot.get_channel(
            int(channel_id)
        )
        if channel:
            channel_text = channel.mention
        else:
            channel_text = str(
                channel_id
            )
    else:
        channel_text = "Nicht gesetzt"
    max_price = config.get(
        "max_price"
    )
    if max_price is None:
        price_text = "Kein Limit"
    else:
        price_text = (
            f"{max_price:.2f} €"
        )
    embed = discord.Embed(
        title="⚙️ Vinted Konfiguration",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🏷️ Marken",
        value=(
            "\n".join(
                f"• {brand}"
                for brand in brands
            )
            or "Keine"
        ),
        inline=False
    )
    embed.add_field(
        name="📢 Kanal",
        value=channel_text,
        inline=False
    )
    embed.add_field(
        name="⏱️ Intervall",
        value=(
            f"{config.get('interval')} Sekunden"
        ),
        inline=True
    )
    embed.add_field(
        name="💰 Maximalpreis",
        value=price_text,
        inline=True
    )
    embed.add_field(
        name="📦 Bereits gesehen",
        value=str(
            len(seen_ids)
        ),
        inline=True
    )
    await ctx.send(
        embed=embed
    )
# ============================================================
# BRAND ADD
# ============================================================
@config_command.command(
    name="brand"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_brand(
    ctx,
    action: str,
    *,
    brand: str
):
    action = action.lower()
    brands = config.setdefault(
        "brands",
        []
    )
    if action == "add":
        if any(
            x.lower() == brand.lower()
            for x in brands
        ):
            await ctx.send(
                f"⚠️ **{brand}** ist bereits aktiviert."
            )
            return
        brands.append(
            brand
        )
        save_json(
            CONFIG_FILE,
            config
        )
        await ctx.send(
            f"✅ **{brand}** wurde hinzugefügt."
        )
    elif action == "remove":
        found = None
        for existing in brands:
            if existing.lower() == brand.lower():
                found = existing
                break
        if not found:
            await ctx.send(
                f"❌ **{brand}** wurde nicht gefunden."
            )
            return
        brands.remove(
            found
        )
        save_json(
            CONFIG_FILE,
            config
        )
        await ctx.send(
            f"✅ **{found}** wurde entfernt."
        )
    else:
        await ctx.send(
            "❌ Nutze `add` oder `remove`."
        )
# ============================================================
# BRAND LIST
# ============================================================
@config_command.command(
    name="brands"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_brands(ctx):
    brands = config.get(
        "brands",
        []
    )
    if not brands:
        await ctx.send(
            "📭 Keine Marken aktiviert."
        )
        return
    await ctx.send(
        "🏷️ **Aktive Marken:**\n"
        +
        "\n".join(
            f"• `{brand}`"
            for brand in brands
        )
    )
# ============================================================
# CHANNEL
# ============================================================
@config_command.command(
    name="channel"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_channel(
    ctx,
    channel: discord.TextChannel
):
    config["channel_id"] = channel.id
    save_json(
        CONFIG_FILE,
        config
    )
    await ctx.send(
        f"✅ Zielkanal: {channel.mention}"
    )
# ============================================================
# INTERVAL
# ============================================================
@config_command.command(
    name="interval"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_interval(
    ctx,
    seconds: int
):
    # Nicht zu aggressiv gegenüber Vinted.
    MIN_INTERVAL = 60
    MAX_INTERVAL = 86400
    if seconds < MIN_INTERVAL:
        await ctx.send(
            "❌ Das Minimum beträgt "
            f"**{MIN_INTERVAL} Sekunden**."
        )
        return
    if seconds > MAX_INTERVAL:
        await ctx.send(
            "❌ Das Maximum beträgt "
            f"**{MAX_INTERVAL} Sekunden**."
        )
        return
    config["interval"] = seconds
    save_json(
        CONFIG_FILE,
        config
    )
    await ctx.send(
        f"✅ Intervall: **{seconds} Sekunden**."
    )
# ============================================================
# MAX PRICE
# ============================================================
@config_command.command(
    name="maxprice"
)
@commands.has_guild_permissions(
    manage_guild=True
)
async def config_maxprice(
    ctx,
    value: str
):
    if value.lower() == "off":
        config["max_price"] = None
        save_json(
            CONFIG_FILE,
            config
        )
        await ctx.send(
            "✅ Preislimit deaktiviert."
        )
        return
    try:
        price = float(
            value.replace(
                ",",
                "."
            )
        )
        if price <= 0:
            raise ValueError
    except ValueError:
        await ctx.send(
            "❌ Beispiel: "
            "`!config maxprice 50`"
        )
        return
    config["max_price"] = price
    save_json(
        CONFIG_FILE,
        config
    )
    await ctx.send(
        f"✅ Maximalpreis: **{price:.2f} €**."
    )
# ============================================================
# MANUAL SEARCH
# ============================================================
@bot.command()
async def search(
    ctx,
    *,
    brand: str
):
    if brand.lower() == "all":
        brands = config.get(
            "brands",
            []
        )
    else:
        brands = [
            brand
        ]
    await ctx.send(
        "🔎 Suche auf Vinted..."
    )
    total = 0
    for current_brand in brands:
        try:
            items = await search_brand(
                current_brand
            )
            # Manuelle Suche:
            # maximal 5 Treffer pro Marke.
            for item in items[:5]:
                embed = create_item_embed(
                    item
                )
                await ctx.send(
                    embed=embed
                )
                total += 1
                await asyncio.sleep(
                    1
                )
        except Exception as error:
            logger.error(
                "Manuelle Suche %s: %s",
                current_brand,
                error
            )
            await ctx.send(
                f"❌ Fehler bei **{current_brand}**."
            )
    await ctx.send(
        f"✅ Suche beendet. "
        f"**{total} Artikel** gefunden."
    )
# ============================================================
# STATUS
# ============================================================
@bot.command()
async def status(ctx):
    channel_id = config.get(
        "channel_id"
    )
    if channel_id:
        channel_text = (
            f"<#{channel_id}>"
        )
    else:
        channel_text = "Nicht gesetzt"
    max_price = config.get(
        "max_price"
    )
    if max_price is None:
        max_price_text = "Kein Limit"
    else:
        max_price_text = (
            f"{max_price:.2f} €"
        )
    await ctx.send(
        "🟢 **Vinted Bot läuft**\n\n"
        f"🏷️ Marken: "
        f"`{len(config.get('brands', []))}`\n"
        f"📢 Kanal: {channel_text}\n"
        f"⏱️ Intervall: "
        f"`{config.get('interval')} Sekunden`\n"
        f"💰 Maxpreis: "
        f"`{max_price_text}`\n"
        f"📦 Bekannte Artikel: "
        f"`{len(seen_ids)}`"
    )
# ============================================================
# COMMAND ERRORS
# ============================================================
@bot.event
async def on_command_error(
    ctx,
    error
):
    if isinstance(
        error,
        commands.MissingPermissions
    ):
        await ctx.send(
            "❌ Du brauchst "
            "**Server verwalten**."
        )
        return
    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):
        await ctx.send(
            "❌ Ein Argument fehlt. "
            "Nutze `!help`."
        )
        return
    if isinstance(
        error,
        commands.BadArgument
    ):
        await ctx.send(
            "❌ Das Argument ist ungültig."
        )
        return
    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return
    logger.error(
        "Command-Fehler: %s",
        error
    )
# ============================================================
# START
# ============================================================
def main():
    # Render Webserver
    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )
    web_thread.start()
    logger.info(
        "Render-Webserver läuft auf Port %s",
        PORT
    )
    # Discord
    bot.run(
        DISCORD_TOKEN
    )
if __name__ == "__main__":
    main()
