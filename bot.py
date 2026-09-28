import os
import json
import asyncio
from pathlib import Path
from urllib.parse import quote_plus

import discord
from discord.ext import commands, tasks


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Auf Render kannst du DATA_DIR=/data setzen und dort einen
# Persistent Disk mounten.
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "brands": [
        "Nike",
        "Ralph Lauren"
    ],
    "channel_id": None,
    "interval": 300,
    "max_price": None
}


# ============================================================
# STORAGE
# ============================================================

def ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    ensure_storage()

    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        config = DEFAULT_CONFIG.copy()
        config.update(data)

        if not isinstance(config.get("brands"), list):
            config["brands"] = []

        return config

    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config):
    ensure_storage()

    temporary = CONFIG_FILE.with_suffix(".tmp")

    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    temporary.replace(CONFIG_FILE)


config = load_config()


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)


# ============================================================
# HELP
# ============================================================

@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🛍️ Vinted Finder",
        description="Verfügbare Befehle:",
        color=0x7B2CBF
    )

    embed.add_field(
        name="⚙️ Konfiguration",
        value=(
            "`!config` – aktuelle Konfiguration\n"
            "`!config show` – Konfiguration anzeigen\n"
            "`!config brand add Nike`\n"
            "`!config brand remove Nike`\n"
            "`!config brands` – Marken anzeigen\n"
            "`!config channel #channel`\n"
            "`!config interval 300`\n"
            "`!config maxprice 100`\n"
            "`!config maxprice off`"
        ),
        inline=False
    )

    embed.add_field(
        name="🔎 Suche",
        value=(
            "`!search Nike`\n"
            "`!search Ralph Lauren`\n"
            "`!search all`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# ============================================================
# CONFIG
# ============================================================

@bot.group(name="config", invoke_without_command=True)
async def config_command(ctx):
    await send_config(ctx)


async def send_config(ctx):
    brands = config.get("brands", [])

    if brands:
        brand_text = "\n".join(
            f"• {brand}" for brand in brands
        )
    else:
        brand_text = "Keine Marken konfiguriert."

    channel_id = config.get("channel_id")

    if channel_id:
        channel = bot.get_channel(channel_id)

        if channel:
            channel_text = channel.mention
        else:
            channel_text = f"<#{channel_id}>"
    else:
        channel_text = "Nicht gesetzt"

    max_price = config.get("max_price")

    if max_price is None:
        price_text = "Kein Limit"
    else:
        price_text = f"{max_price:.2f} €"

    embed = discord.Embed(
        title="⚙️ Vinted Finder – Config",
        color=0x7B2CBF
    )

    embed.add_field(
        name="🏷️ Marken",
        value=brand_text,
        inline=False
    )

    embed.add_field(
        name="📢 Ausgabe-Channel",
        value=channel_text,
        inline=True
    )

    embed.add_field(
        name="⏱️ Intervall",
        value=f"{config.get('interval', 300)} Sekunden",
        inline=True
    )

    embed.add_field(
        name="💰 Preislimit",
        value=price_text,
        inline=True
    )

    await ctx.send(embed=embed)


@config_command.command(name="show")
async def config_show(ctx):
    await send_config(ctx)


# ============================================================
# BRAND ADD
# ============================================================

@config_command.group(name="brand", invoke_without_command=True)
async def config_brand(ctx):
    await ctx.send(
        "Benutzung:\n"
        "`!config brand add Nike`\n"
        "`!config brand remove Nike`\n"
        "`!config brand list`"
    )


@config_brand.command(name="add")
async def config_brand_add(ctx, *, brand: str):
    brand = brand.strip()

    if not brand:
        await ctx.send("❌ Bitte gib eine Marke an.")
        return

    existing = config["brands"]

    if any(x.lower() == brand.lower() for x in existing):
        await ctx.send(f"⚠️ **{brand}** ist bereits aktiviert.")
        return

    existing.append(brand)

    save_config(config)

    await ctx.send(
        f"✅ **{brand}** wurde zur Suche hinzugefügt."
    )


# ============================================================
# BRAND REMOVE
# ============================================================

@config_brand.command(name="remove")
async def config_brand_remove(ctx, *, brand: str):
    brand = brand.strip()

    found = None

    for existing in config["brands"]:
        if existing.lower() == brand.lower():
            found = existing
            break

    if found is None:
        await ctx.send(
            f"❌ **{brand}** ist nicht konfiguriert."
        )
        return

    config["brands"].remove(found)

    save_config(config)

    await ctx.send(
        f"✅ **{found}** wurde entfernt."
    )


# ============================================================
# BRAND LIST
# ============================================================

@config_brand.command(name="list")
async def config_brand_list(ctx):
    if not config["brands"]:
        await ctx.send("🏷️ Keine Marken konfiguriert.")
        return

    text = "\n".join(
        f"• {brand}"
        for brand in config["brands"]
    )

    embed = discord.Embed(
        title="🏷️ Aktive Marken",
        description=text,
        color=0x7B2CBF
    )

    await ctx.send(embed=embed)


# ============================================================
# CHANNEL
# ============================================================

@config_command.command(name="channel")
async def config_channel(ctx, channel: discord.TextChannel):
    config["channel_id"] = channel.id

    save_config(config)

    await ctx.send(
        f"✅ Vinted-Meldungen werden jetzt in {channel.mention} gesendet."
    )


# ============================================================
# INTERVAL
# ============================================================

@config_command.command(name="interval")
async def config_interval(ctx, seconds: int):
    if seconds < 60:
        await ctx.send(
            "❌ Das Intervall muss mindestens 60 Sekunden betragen."
        )
        return

    if seconds > 86400:
        await ctx.send(
            "❌ Das Intervall darf maximal 86400 Sekunden betragen."
        )
        return

    config["interval"] = seconds

    save_config(config)

    await ctx.send(
        f"✅ Suchintervall auf **{seconds} Sekunden** gesetzt."
    )


# ============================================================
# MAX PRICE
# ============================================================

@config_command.command(name="maxprice")
async def config_maxprice(ctx, value: str):
    if value.lower() in ("off", "none", "aus"):
        config["max_price"] = None

        save_config(config)

        await ctx.send(
            "✅ Preislimit deaktiviert."
        )
        return

    try:
        price = float(value.replace(",", "."))

        if price < 0:
            raise ValueError

    except ValueError:
        await ctx.send(
            "❌ Beispiel: `!config maxprice 100`"
        )
        return

    config["max_price"] = price

    save_config(config)

    await ctx.send(
        f"✅ Preislimit auf **{price:.2f} €** gesetzt."
    )


# ============================================================
# VINTED SEARCH URL
# ============================================================

def create_vinted_search_url(brand):
    """
    Erstellt einen normalen Vinted-Suchlink.

    Dieser Bot ruft die Vinted-Seite nicht automatisch ab.
    """

    query = quote_plus(brand)

    return f"https://www.vinted.de/catalog?search_text={query}"


# ============================================================
# SEARCH COMMAND
# ============================================================

@bot.command()
async def search(ctx, *, brand: str = None):

    if not brand:
        await ctx.send(
            "❌ Beispiel:\n"
            "`!search Nike`\n"
            "`!search Ralph Lauren`\n"
            "`!search all`"
        )
        return

    if brand.lower() == "all":
        brands = config["brands"]

        if not brands:
            await ctx.send(
                "❌ Es sind keine Marken konfiguriert."
            )
            return

        links = []

        for item in brands:
            url = create_vinted_search_url(item)
            links.append(
                f"**{item}**\n{url}"
            )

        embed = discord.Embed(
            title="🔎 Vinted-Suchen",
            description="\n\n".join(links),
            color=0x7B2CBF
        )

        await ctx.send(embed=embed)
        return

    url = create_vinted_search_url(brand)

    embed = discord.Embed(
        title=f"🔎 Vinted – {brand}",
        description=(
            f"Suche nach **{brand}**:\n\n"
            f"{url}"
        ),
        color=0x7B2CBF
    )

    embed.add_field(
        name="💰 Preislimit",
        value=(
            "Kein Limit"
            if config["max_price"] is None
            else f"{config['max_price']:.2f} €"
        )
    )

    await ctx.send(embed=embed)


# ============================================================
# AUTOMATIC TASK
# ============================================================

@tasks.loop(seconds=60)
async def finder_loop():
    """
    Platzhalter für eine autorisierte Vinted-Datenquelle.

    Der Loop läuft bereits auf Render.
    Ein autorisierter Provider kann hier später neue
    Angebote liefern.
    """

    # Aktuelles Intervall berücksichtigen
    finder_loop.change_interval(
        seconds=config.get("interval", 300)
    )

    # Ohne konfigurierten Channel nichts machen
    channel_id = config.get("channel_id")

    if not channel_id:
        return

    channel = bot.get_channel(channel_id)

    if not channel:
        return

    # --------------------------------------------------------
    # Hier wird absichtlich KEIN Vinted-Scraping durchgeführt.
    #
    # Sobald eine von Vinted autorisierte API / Datenquelle
    # vorhanden ist, kann hier beispielsweise stehen:
    #
    # listings = await provider.search(
    #     brands=config["brands"],
    #     max_price=config["max_price"]
    # )
    #
    # Danach werden nur neue listings gepostet.
    # --------------------------------------------------------

    return


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():

    print("=" * 50)
    print("Vinted Finder gestartet")
    print(f"Bot: {bot.user}")
    print(f"Guilds: {len(bot.guilds)}")
    print(f"Marken: {config['brands']}")
    print("=" * 50)

    if not finder_loop.is_running():
        finder_loop.start()


@bot.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "❌ Fehlender Parameter.\n"
            "Benutze `!help` für eine Übersicht."
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            "❌ Ungültiger Parameter.\n"
            "Benutze `!help` für eine Übersicht."
        )
        return

    if isinstance(error, commands.CommandNotFound):
        return

    print(
        f"Command error in {ctx.command}: {error}"
    )


# ============================================================
# START
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN wurde nicht gesetzt."
    )

bot.run(TOKEN)
