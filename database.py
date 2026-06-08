import os
import motor.motor_asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()


def now_ist():
    utc = datetime.now(timezone.utc)
    ist = utc + timedelta(hours=5, minutes=30)
    return utc, ist


# ── Connection ──────────────────────────────────────────
client = motor.motor_asyncio.AsyncIOMotorClient(os.getenv("MONGO_URL"))
db     = client["automod_bot"]

# ── Collections ─────────────────────────────────────────
guild_settings_col   = db["guild_settings"]
automod_settings_col = db["automod_settings"]
warnings_col         = db["warnings"]
strikes_col          = db["strikes"]


# ════════════════════════════════════════════════════════
# INIT
# ════════════════════════════════════════════════════════

async def init_db():
    await guild_settings_col.create_index("guild_id",   unique=True)
    await automod_settings_col.create_index("guild_id", unique=True)
    await warnings_col.create_index([("guild_id", 1), ("user_id", 1)])
    await strikes_col.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
    print("✅ Database indexes created successfully.")


# ════════════════════════════════════════════════════════
# GUILD SETTINGS
# Stores: log_channel ID
# ════════════════════════════════════════════════════════

async def get_guild_settings(guild_id: int) -> dict:
    doc = await guild_settings_col.find_one({"guild_id": str(guild_id)})
    return doc or {"guild_id": str(guild_id), "log_channel": None}


async def set_guild_setting(guild_id: int, key: str, value) -> None:
    await guild_settings_col.update_one(
        {"guild_id": str(guild_id)},
        {"$set": {key: value}},
        upsert=True
    )


# ════════════════════════════════════════════════════════
# AUTOMOD SETTINGS
# Stores all filter toggles + punishment level + blacklist
# ════════════════════════════════════════════════════════

async def get_automod(guild_id: int) -> dict:
    doc = await automod_settings_col.find_one(
        {"guild_id": str(guild_id)}, {"_id": 0}
    )
    return doc or {"guild_id": str(guild_id)}


async def set_automod_flag(guild_id: int, key: str, value) -> None:
    await automod_settings_col.update_one(
        {"guild_id": str(guild_id)},
        {"$set": {key: value}},
        upsert=True
    )


async def add_blacklist_word(guild_id: int, word: str) -> bool:
    doc = await get_automod(guild_id)
    if word.lower() in [w.lower() for w in doc.get("blacklist", [])]:
        return False
    await automod_settings_col.update_one(
        {"guild_id": str(guild_id)},
        {"$push": {"blacklist": word.lower()}},
        upsert=True
    )
    return True


async def remove_blacklist_word(guild_id: int, word: str) -> bool:
    doc = await get_automod(guild_id)
    if word.lower() not in [w.lower() for w in doc.get("blacklist", [])]:
        return False
    await automod_settings_col.update_one(
        {"guild_id": str(guild_id)},
        {"$pull": {"blacklist": word.lower()}}
    )
    return True


async def get_blacklist(guild_id: int) -> list[str]:
    doc = await get_automod(guild_id)
    return doc.get("blacklist", [])


# ════════════════════════════════════════════════════════
# WARNINGS
# One document per violation — used for /warnings command
# ════════════════════════════════════════════════════════

async def add_warning(guild_id: int, user_id: int, reason: str) -> int:
    """Insert a warning and return the new total count for this user."""
    utc, ist = now_ist()
    await warnings_col.insert_one({
        "guild_id":      str(guild_id),
        "user_id":       str(user_id),
        "reason":        reason,
        "timestamp_utc": utc,
        "timestamp_ist": ist,
    })
    return await warnings_col.count_documents(
        {"guild_id": str(guild_id), "user_id": str(user_id)}
    )


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    cursor = warnings_col.find(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"_id": 0}
    ).sort("timestamp_utc", -1)
    return await cursor.to_list(length=50)


async def clear_warnings(guild_id: int, user_id: int) -> int:
    result = await warnings_col.delete_many(
        {"guild_id": str(guild_id), "user_id": str(user_id)}
    )
    return result.deleted_count


# ════════════════════════════════════════════════════════
# STRIKES
# Increments per violation — drives punishment escalation
# ════════════════════════════════════════════════════════

async def get_strikes(guild_id: int, user_id: int) -> int:
    doc = await strikes_col.find_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"strikes": 1}
    )
    return doc["strikes"] if doc else 0


async def add_strike(guild_id: int, user_id: int) -> int:
    """Increment and return the new strike count."""
    doc = await strikes_col.find_one_and_update(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$inc": {"strikes": 1}},
        upsert=True,
        return_document=True
    )
    return doc["strikes"] if doc else 1


async def reset_strikes(guild_id: int, user_id: int) -> None:
    await strikes_col.update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)},
        {"$set": {"strikes": 0}}
    )
