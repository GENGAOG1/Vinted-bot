import os
import json
import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional
import discord
from discord.ext import commands, tasks
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
    raise RuntimeError("DISCORD_TOKEN fehlt in den Render Environment Variables.")
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE = DATA_DIR / "seen.json"
# ============================================================
# DEFAULT CONFIG
# ============================================================
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
    # 300 Sekunden = 5 Minuten
    # Absichtlich nicht 20 Sekunden, damit die Suche nicht
    # unnötig aggressiv gegen Vinted läuft.
    "interval": 300,
    "max_price": None,
    # Anzahl der Treffer pro Markensuche
    "results_per_brand": 20
}
# ============================================================
# CONFIG HELPERS
# ============================================================
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Fehler beim Laden von %s: %s", path, e)
        return default
def save_json(path: Path, data):
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error("Fehler beim Speichern von %s: %s", path, e)
config = load_json(CONFIG_FILE, DEFAULT_CONFIG.copy())
# Fehlende Config-Werte nachtragen
for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value
save_json(CONFIG_FILE, config)
seen_ids = set(load_json(SEEN_FILE, []))
def save_seen():
    # Nicht unendlich wachsen lassen
    limited = list(seen_ids)[-5000:]
    save_json(SEEN_FILE, limited)
# ============================================================
# VINTED
# ============================================================
vinted = Vinted(
    domain="de",
    language="de-DE"
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
# FLASK / RENDER WEB SERVICE
# ============================================================
app = Flask(__name__)
@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Vinted Discord Bot"
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
# VINTED HELPERS
# ============================================================
def get_value(obj: Any, name: str, default=None):
    """
    Holt Attribute oder Dictionary-Wert.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
def get_photo_url(item: Any) -> Optional[str]:
    photo = get_value(item, "photo")
    if not photo:
        return None
    if isinstance(photo, str):
        return photo
    # Verschiedene mögliche Wrapper-Felder
    for field in (
        "url",
        "full_size_url",
        "full_size",
        "image_url"
    ):
        value = get_value(photo, field)
        if value:
            return str(value)
    return None
def get_item_id(item: Any) -> Optional[str]:
    value = get_value(item, "id")
    if value is None:
        return None
    return str(value)
def get_item_title(item: Any) -> str:
    return str(
        get_value(item, "title", "Unbekannter Artikel")
    )
def get_item_brand(item: Any) -> str:
    return str(
        get_value(item, "brand_title", "Unbekannte Marke")
    )
def get_item_price(item: Any) -> Optional[float]:
    raw = get_value(item, "price")
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (ValueError, TypeError):
        return None
def get_item_url(item: Any) -> Optional[str]:
    value = get_value(item, "url")
    if value:
        return str(value)
    item_id = get_item_id(item)
    if item_id:
        return f"https://www.vinted.de/items/{item_id}"
    return None
# ============================================================
# VINTED SEARCH
# ============================================================
def search_brand_sync(brand: str):
    """
    Synchrone Vinted-Suche.
    Wird später mit asyncio.to_thread() aufgerufen,
    damit Discord nicht blockiert.
    """
    max_price = config.get("max_price")
    results_per_brand = int(
        config.get("results_per_brand", 20)
    )
    kwargs = {
        "query": brand,
        "order": "newest_first",
        "per_page": results_per_brand
    }
    if max_price is not None:
        kwargs["price_to"] = max_price
    response = vinted.search(**kwargs)
    return get_value(response, "items", []) or []
async def search_brand(brand: str):
    return await asyncio.to_thread(
        search_brand_sync,
        brand
    )
# ============================================================
# DISCORD EMBED
# ============================================================
def create_item_embed(item: Any) -> discord.Embed:
    title = get_item_title(item)
    brand = get_item_brand(item)
    price = get_item_price(item)
    url = get_item_url(item)
    photo = get_photo_url(item)
    if price is not None:
        price_text = f"{price:.2f} €"
    else:
        price_text = "Preis unbekannt"
    embed = discord.Embed(
        title=title[:256],
        url=url,
        description="🛍️ Neuer Vinted-Artikel gefunden!",
        color=discord.Color.blurple()
    )
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
            value=f"[Auf Vinted öffnen]({url})",
            inline=False
        )
    if photo:
        try:
            embed.set_thumbnail(url=photo)
        except Exception:
            pass
    embed.set_footer(
        text="Vinted Monitor"
    )
    return embed
# ============================================================
# SEND NEW ITEM
# ============================================================
async def send_item(item: Any, channel: discord.TextChannel):
    item_id = get_item_id(item)
    if not item_id:
        return False
    if item_id in seen_ids:
        return False
    embed = create_item_embed(item)
    try:
        await channel.send(embed=embed)
        seen_ids.add(item_id)
        return True
    except discord.Forbidden:
        logger.error(
            "Keine Berechtigung, in #%s zu schreiben.",
            channel.name
        )
    except discord.HTTPException as e:
        logger.error(
            "Discord HTTP Fehler: %s",
            e
        )
    return False
# ============================================================
# AUTOMATIC FINDER
# ============================================================
@tasks.loop(seconds=300)
async def finder_loop():
    """
    Automatische Vinted-Suche.
    """
    interval = int(config.get("interval", 300))
    # tasks.loop kann nicht dynamisch mit config.interval
    # gesteuert werden, deshalb warten wir hier zusätzlich.
    #
    # Der Loop läuft nur als Scheduler.
    await asyncio.sleep(0)
    channel_id = config.get("channel_id")
    if not channel_id:
        logger.info(
            "Kein Discord-Kanal konfiguriert."
        )
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        logger.warning(
            "Konfigurierter Discord-Kanal wurde nicht gefunden."
        )
        return
    brands = config.get("brands", [])
    if not brands:
        logger.info(
            "Keine Marken konfiguriert."
        )
        return
    logger.info(
        "Starte Vinted-Suche für: %s",
        ", ".join(brands)
    )
    new_items = 0
    for brand in brands:
        try:
            items = await search_brand(brand)
            logger.info(
                "%s: %s Treffer",
                brand,
                len(items)
            )
            # Neue Artikel zuerst
            for item in reversed(items):
                sent = await send_item(
                    item,
                    channel
                )
                if sent:
                    new_items += 1
                # Kleine Pause zwischen Discord-Nachrichten
                if sent:
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(
                "Vinted-Suche für %s fehlgeschlagen: %s",
                brand,
                e
            )
            # Bei Fehler mit nächster Marke weitermachen
            continue
    save_seen()
    logger.info(
        "Suche abgeschlossen. Neue Artikel: %s",
        new_items
    )
# ============================================================
# INITIAL BASELINE
# ============================================================
async def initialize_seen_items():
    """
    Beim ersten Start werden die aktuell vorhandenen Treffer
    als bereits bekannt gespeichert.
    Dadurch werden nicht sofort 100 alte Artikel in Discord
    gespammt.
    """
    global seen_ids
    if seen_ids:
        return
    logger.info(
        "Erster Start: erstelle Vinted-Baseline..."
    )
    brands = config.get("brands", [])
    for brand in brands:
        try:
            items = await search_brand(brand)
            for item in items:
                item_id = get_item_id(item)
                if item_id:
                    seen_ids.add(item_id)
        except Exception as e:
            logger.error(
                "Baseline für %s fehlgeschlagen: %s",
                brand,
                e
            )
    save_seen()
    logger.info(
        "Baseline gespeichert: %s Artikel",
        len(seen_ids)
    )
# ============================================================
# BOT EVENTS
# ============================================================
@bot.event
async def on_ready():
    logger.info(
        "Discord verbunden als %s",
        bot.user
    )
    logger.info(
        "Invite: %s",
        f"https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=2147483648&scope=bot"
    )
    await initialize_seen_items()
    if not finder_loop.is_running():
        finder_loop.start()
    logger.info(
        "Vinted Finder gestartet."
    )
# ============================================================
# HELP
# ============================================================
@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🤖 Vinted Bot",
        description="Verfügbare Befehle:",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="⚙️ Konfiguration",
        value=(
            "`!config show`\n"
            "`!config brand add <Marke>`\n"
            "`!config brand remove <Marke>`\n"
            "`!config brand list`\n"
            "`!config channel #kanal`\n"
            "`!config interval <Sekunden>`\n"
            "`!config maxprice <Preis>`\n"
            "`!config maxprice off`"
        ),
        inline=False
    )
    embed.add_field(
        name="🔎 Suche",
        value=(
            "`!search <Marke>`\n"
            "`!search all`"
        ),
        inline=False
    )
    await ctx.send(embed=embed)
# ============================================================
# CONFIG GROUP
# ============================================================
@bot.group(
    name="config",
    invoke_without_command=True
)
@commands.has_guild_permissions(manage_guild=True)
async def config_command(ctx):
    await ctx.send(
        "Nutze `!config show`, um die aktuelle Konfiguration zu sehen."
    )
# ============================================================
# CONFIG SHOW
# ============================================================
@config_command.command(name="show")
@commands.has_guild_permissions(manage_guild=True)
async def config_show(ctx):
    brands = config.get("brands", [])
    channel_id = config.get("channel_id")
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        channel_text = channel.mention if channel else f"`{channel_id}`"
    else:
        channel_text = "Nicht gesetzt"
    max_price = config.get("max_price")
    if max_price is None:
        price_text = "Kein Limit"
    else:
        price_text = f"{max_price:.2f} €"
    embed = discord.Embed(
        title="⚙️ Vinted Bot Konfiguration",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🏷️ Marken",
        value="\n".join(
            f"• {brand}" for brand in brands
        ) or "Keine",
        inline=False
    )
    embed.add_field(
        name="📢 Kanal",
        value=channel_text,
        inline=False
    )
    embed.add_field(
        name="⏱️ Intervall",
        value=f"{config.get('interval')} Sekunden",
        inline=True
    )
    embed.add_field(
        name="💰 Maximalpreis",
        value=price_text,
        inline=True
    )
    embed.add_field(
        name="🔎 Treffer pro Marke",
        value=str(config.get("results_per_brand", 20)),
        inline=True
    )
    await ctx.send(embed=embed)
# ============================================================
# BRAND ADD
# ============================================================
@config_command.command(name="brand_add")
@commands.has_guild_permissions(manage_guild=True)
async def brand_add(ctx, *, brand: str):
    brand = brand.strip()
    if not brand:
        await ctx.send("❌ Bitte eine Marke angeben.")
        return
    brands = config.setdefault("brands", [])
    if any(x.lower() == brand.lower() for x in brands):
        await ctx.send(
            f"⚠️ **{brand}** ist bereits aktiviert."
        )
        return
    brands.append(brand)
    save_json(CONFIG_FILE, config)
    await ctx.send(
        f"✅ **{brand}** wurde hinzugefügt."
    )
# ============================================================
# BRAND REMOVE
# ============================================================
@config_command.command(name="brand_remove")
@commands.has_guild_permissions(manage_guild=True)
async def brand_remove(ctx, *, brand: str):
    brands = config.get("brands", [])
    found = None
    for existing in brands:
        if existing.lower() == brand.lower():
            found = existing
            break
    if not found:
        await ctx.send(
            f"❌ **{brand}** ist nicht aktiviert."
        )
        return
    brands.remove(found)
    save_json(CONFIG_FILE, config)
    await ctx.send(
        f"✅ **{found}** wurde entfernt."
    )
# ============================================================
# BRAND LIST
# ============================================================
@config_command.command(name="brand_list")
@commands.has_guild_permissions(manage_guild=True)
async def brand_list(ctx):
    brands = config.get("brands", [])
    if not brands:
        await ctx.send(
            "📭 Keine Marken aktiviert."
        )
        return
    await ctx.send(
        "🏷️ **Aktive Marken:**\n" +
        "\n".join(
            f"• `{brand}`"
            for brand in brands
        )
    )
# ============================================================
# CHANNEL
# ============================================================
@config_command.command(name="channel")
@commands.has_guild_permissions(manage_guild=True)
async def config_channel(
    ctx,
    channel: discord.TextChannel
):
    config["channel_id"] = channel.id
    save_json(CONFIG_FILE, config)
    await ctx.send(
        f"✅ Vinted-Artikel werden ab jetzt in {channel.mention} gepostet."
    )
# ============================================================
# INTERVAL
# ============================================================
@config_command.command(name="interval")
@commands.has_guild_permissions(manage_guild=True)
async def config_interval(
    ctx,
    seconds: int
):
    # Nicht aggressiv gegen Vinted abfragen.
    MIN_INTERVAL = 60
    MAX_INTERVAL = 86400
    if seconds < MIN_INTERVAL:
        await ctx.send(
            f"❌ Das Minimum beträgt **{MIN_INTERVAL} Sekunden**."
        )
        return
    if seconds > MAX_INTERVAL:
        await ctx.send(
            f"❌ Das Maximum beträgt **{MAX_INTERVAL} Sekunden**."
        )
        return
    config["interval"] = seconds
    save_json(CONFIG_FILE, config)
    await ctx.send(
        f"✅ Suchintervall auf **{seconds} Sekunden** gesetzt."
    )
# ============================================================
# MAX PRICE
# ============================================================
@config_command.command(name="maxprice")
@commands.has_guild_permissions(manage_guild=True)
async def config_maxprice(
    ctx,
    value: str
):
    if value.lower() == "off":
        config["max_price"] = None
        save_json(CONFIG_FILE, config)
        await ctx.send(
            "✅ Preislimit deaktiviert."
        )
        return
    try:
        price = float(
            value.replace(",", ".")
        )
        if price <= 0:
            raise ValueError
    except ValueError:
        await ctx.send(
            "❌ Beispiel: `!config maxprice 50`"
        )
        return
    config["max_price"] = price
    save_json(CONFIG_FILE, config)
    await ctx.send(
        f"✅ Maximalpreis auf **{price:.2f} €** gesetzt."
    )
# ============================================================
# MANUAL SEARCH
# ============================================================
@bot.command()
async def search(ctx, *, brand: str):
    brand = brand.strip()
    if brand.lower() == "all":
        brands = config.get("brands", [])
    else:
        brands = [brand]
    await ctx.send(
        "🔎 Ich suche gerade auf Vinted..."
    )
    total = 0
    for current_brand in brands:
        try:
            items = await search_brand(
                current_brand
            )
            # Bei manueller Suche maximal 5 Treffer
            items = items[:5]
            if not items:
                continue
            for item in items:
                embed = create_item_embed(item)
                await ctx.send(
                    embed=embed
                )
                total += 1
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(
                "Manuelle Suche fehlgeschlagen: %s",
                e
            )
            await ctx.send(
                f"❌ Suche für **{current_brand}** fehlgeschlagen."
            )
    await ctx.send(
        f"✅ Suche beendet. **{total} Artikel** gefunden."
    )
# ============================================================
# STATUS
# ============================================================
@bot.command()
async def status(ctx):
    channel_id = config.get("channel_id")
    channel_text = (
        f"<#{channel_id}>"
        if channel_id
        else "Nicht gesetzt"
    )
    await ctx.send(
        "🟢 **Vinted Bot läuft**\n\n"
        f"🏷️ Marken: `{len(config.get('brands', []))}`\n"
        f"📢 Kanal: {channel_text}\n"
        f"⏱️ Intervall: `{config.get('interval')}s`\n"
        f"💰 Maxpreis: `{config.get('max_price') or 'kein Limit'}`\n"
        f"📦 Bereits bekannte Artikel: `{len(seen_ids)}`"
    )
# ============================================================
# ERROR HANDLER
# ============================================================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(
        error,
        commands.MissingPermissions
    ):
        await ctx.send(
            "❌ Du brauchst die Berechtigung **Server verwalten**."
        )
        return
    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):
        await ctx.send(
            "❌ Es fehlt ein Argument. Nutze `!help`."
        )
        return
    if isinstance(
        error,
        commands.BadArgument
    ):
        await ctx.send(
            "❌ Das Argument konnte nicht verarbeitet werden."
        )
        return
    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return
    logger.error(
        "Command error: %s",
        error
    )
# ============================================================
# START
# ============================================================
def main():
    # Render-Webserver starten
    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )
    web_thread.start()
    logger.info(
        "Webserver läuft auf Port %s",
        PORT
    )
    # Discord starten
    bot.run(DISCORD_TOKEN)
if __name__ == "__main__":
    main()
