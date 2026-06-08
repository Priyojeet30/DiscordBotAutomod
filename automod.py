import discord
from discord import app_commands
from discord.ext import commands
import re
import unicodedata
import datetime
from database import (
    get_automod, set_automod_flag,
    add_blacklist_word, remove_blacklist_word, get_blacklist,
    get_guild_settings, set_guild_setting,
    add_warning, get_warnings, clear_warnings,
    add_strike, get_strikes, reset_strikes,
)


# ════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════

BAD_WORDS = [
    "fuck", "shit", "ass", "bitch", "bastard",
    "dick", "pussy", "cunt", "nigga", "nigger",
    "fag", "faggot", "whore", "slut", "retard",
    
]

# Maps lookalike characters to their plain equivalents
# Prevents evasion like @ss, sh!t, fück, etc.
CHAR_MAP = {
    '@': 'a', '4': 'a', 'á': 'a', 'à': 'a', 'â': 'a', 'ä': 'a', 'ã': 'a', 'å': 'a',
    '3': 'e', 'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
    '1': 'i', '!': 'i', 'í': 'i', 'ì': 'i', 'î': 'i', 'ï': 'i',
    '0': 'o', 'ó': 'o', 'ò': 'o', 'ô': 'o', 'ö': 'o', 'õ': 'o', 'ø': 'o',
    '5': 's', 'ß': 'ss',
    '7': 't', '+': 't',
    'ú': 'u', 'ù': 'u', 'û': 'u', 'ü': 'u',
    'ý': 'y', 'ÿ': 'y',
    'ñ': 'n',
    'ç': 'c',
}

SCAM_PATTERNS = [
    r'free\s*nitro',
    r'discord\s*(gift|free)',
    r'claim\s*your\s*(prize|reward|gift)',
    r'you\s*(have\s*been|were)\s*selected',
    r'click\s*here\s*to\s*claim',
    r'steam\s*gift\s*card',
    r'bit\.ly',
    r'tinyurl\.com',
    r'discord\.gift/',
    r'@everyone.{0,30}(free|claim|gift|nitro)',
]




class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # In-memory trackers (reset on bot restart)
        self._spam_tracker:      dict[int, list[float]] = {}  # uid → [timestamps]
        self._flood_tracker:     dict[int, list[float]] = {}  # uid → [timestamps]
        self._duplicate_tracker: dict[int, str]         = {}  # uid → last_message



    async def get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        settings = await get_guild_settings(guild.id)
        ch_id    = settings.get("log_channel")

        if ch_id:
            ch = guild.get_channel(int(ch_id))
            if ch:
                return ch

        # Auto-create if not configured
        try:
            ch = await guild.create_text_channel(
                "automod-logs",
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        embed_links=True
                    ),
                },
                topic="🛡️ AutoMod violation logs — admins only",
                reason="Auto created — AutoMod logging system"
            )
            await set_guild_setting(guild.id, "log_channel", str(ch.id))
            return ch
        except Exception as e:
            print(f"[AutoMod] Could not create log channel in {guild.name}: {e}")
            return None

    async def send_log(
        self,
        guild:        discord.Guild,
        filter_name:  str,
        member:       discord.Member,
        content:      str,
        action_taken: str
    ):
        ch = await self.get_log_channel(guild)
        if not ch:
            return

        embed = discord.Embed(
            title=f"🛡️ AutoMod — {filter_name}",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="👤 Member",
            value=f"{member.mention} (`{member.id}`)",
            inline=False
        )
        embed.add_field(
            name="📝 Content",
            value=f"||{content[:500]}||" if content else "N/A",
            inline=False
        )
        embed.add_field(name="📍 Filter",  value=filter_name,  inline=True)
        embed.add_field(name="⚡ Action",  value=action_taken, inline=True)
        embed.set_footer(text="AutoMod System")

        try:
            await ch.send(embed=embed)
        except Exception as e:
            print(f"[AutoMod] Could not send log: {e}")


    async def punish(self, message: discord.Message, filter_name: str, reason: str):
        member  = message.author
        guild   = message.guild
        content = message.content or "[attachment / embed]"

        # 1 ── Delete the offending message
        try:
            await message.delete()
        except Exception:
            pass

        # 2 ── Notify in channel (auto-deletes after 5s)
        try:
            await message.channel.send(
                f"🛡️ {member.mention} **{filter_name}** — {reason}",
                delete_after=5
            )
        except Exception:
            pass

        # 3 ── Record warning and increment strike
        warn_count = await add_warning(guild.id, member.id, f"{filter_name}: {reason}")
        strike     = await add_strike(guild.id, member.id)

        # 4 ── Determine punishment (config or strike-based escalation)
        settings    = await get_automod(guild.id)
        punishment  = settings.get("punishment", "warn")   # warn | mute | kick | ban
        action_taken = f"Warning #{warn_count} (Strike {strike})"

        # Ban — configured or 7+ strikes
        if punishment == "ban" or strike >= 7:
            try:
                await member.send(
                    f"🔨 You have been **permanently banned** from **{guild.name}**.\n"
                    f"Reason: {filter_name} — {reason}"
                )
            except Exception:
                pass
            try:
                await member.ban(reason=f"AutoMod: {filter_name}", delete_message_days=1)
                action_taken = "Permanently banned"
            except Exception:
                pass

        # Kick — configured or 5+ strikes
        elif punishment == "kick" or strike >= 5:
            try:
                await member.send(
                    f"👢 You have been **kicked** from **{guild.name}**.\n"
                    f"Reason: {filter_name} — {reason}"
                )
            except Exception:
                pass
            try:
                await member.kick(reason=f"AutoMod: {filter_name}")
                action_taken = "Kicked from server"
            except Exception:
                pass

        # Mute — configured or 3+ strikes (scales with strike count)
        elif punishment == "mute" or strike >= 3:
            mute_minutes = 10 * strike  # 30m at 3 strikes, 40m at 4, etc.
            try:
                await member.timeout(
                    datetime.timedelta(minutes=mute_minutes),
                    reason=f"AutoMod: {filter_name}"
                )
                action_taken = f"Timed out for {mute_minutes} minutes (Strike {strike})"
                try:
                    await member.send(
                        f"⏱️ You have been **timed out** in **{guild.name}** for {mute_minutes} minutes.\n"
                        f"Reason: {filter_name} — {reason}\n"
                        f"Continued violations will result in a kick or ban."
                    )
                except Exception:
                    pass
            except Exception:
                pass

        # Warn only (default)
        else:
            try:
                await member.send(
                    f"⚠️ **Warning #{warn_count}** in **{guild.name}**\n"
                    f"Filter triggered: **{filter_name}**\n"
                    f"Reason: {reason}\n"
                    f"Repeated violations will result in stricter punishment!"
                )
            except Exception:
                pass
            action_taken = f"DM warning sent (Warning #{warn_count}, Strike {strike})"

        # 5 ── Send to log channel
        await self.send_log(guild, filter_name, member, content, action_taken)



    def _normalize(self, text: str) -> tuple[str, str, str]:
        """
        Returns (stripped, spaced, normalized_raw):
        - stripped     : letters/digits only — catches condensed words
        - spaced       : letters/digits/spaces only — used for word boundary checks
        - normalized_raw: char-map applied, before stripping — catches evasion patterns
        """
        # Remove invisible / zero-width chars
        text = re.sub(r'[\u200b\u200c\u200d\u2060\u00ad\ufeff\u180e]', '', text)
        # Unicode normalization (full-width chars, etc.)
        text = unicodedata.normalize('NFKC', text)
        text = text.lower()
        # Apply lookalike substitution
        text = ''.join(CHAR_MAP.get(ch, ch) for ch in text)

        normalized_raw = text
        spaced         = re.sub(r'[^a-z0-9\s]', ' ', text)
        spaced         = re.sub(r'\s+', ' ', spaced).strip()
        stripped       = re.sub(r'[^a-z0-9]', '', text)

        return stripped, spaced, normalized_raw

    def _build_skip_vowel_pattern(self, word: str) -> str:
        """
        Regex that detects f*ck, f.u.c.k, f-ck style evasion.
        First/last chars required; inner vowels optional; 0-2 separators between letters.
        """
        VOWELS = set('aeiou')
        parts  = []
        for i, ch in enumerate(word):
            if i == 0 or i == len(word) - 1:
                parts.append(re.escape(ch))
            else:
                parts.append(
                    f'(?:{re.escape(ch)})?' if ch in VOWELS else re.escape(ch)
                )
            if i < len(word) - 1:
                parts.append(r'[^a-z0-9]{0,2}')
        return r'(?<![a-z0-9])' + ''.join(parts) + r'(?![a-z0-9])'

    def _word_matches(self, word: str, stripped: str, spaced: str, normalized_raw: str) -> bool:
        """
        Four-layer detection:
        1. Word boundary on spaced text          — normal text, UPPERCASE, accented chars
        2. Condensed spaced letters              — 'a s s', 'f u c k'
        3. Skip-vowel evasion pattern            — 'f*ck', 'a.s.s', 'f-ck'
        4. Strict substring (len>3 words only)   — catches words embedded in no-space text
        """
        boundary = r'(?<![a-z0-9])' + re.escape(word) + r'(?![a-z0-9])'

        # Layer 1
        if re.search(boundary, spaced):
            return True

        # Layer 2 — collapse spaced-out letters
        condensed = spaced
        for _ in range(10):
            new = re.sub(
                r'(?<![a-z0-9])([a-z0-9]) ([a-z0-9])(?![a-z0-9])',
                r'\1\2',
                condensed
            )
            if new == condensed:
                break
            condensed = new
        if re.search(boundary, condensed):
            return True

        # Layer 3 — evasion pattern
        if re.search(self._build_skip_vowel_pattern(word), normalized_raw):
            return True

        # Layer 4 — substring (longer words only to avoid false positives)
        if len(word) > 3 and word in stripped:
            return True

        return False

    def contains_bad_word(self, text: str) -> bool:
        """Check against the static BAD_WORDS list."""
        stripped, spaced, raw = self._normalize(text)
        return any(self._word_matches(w, stripped, spaced, raw) for w in BAD_WORDS)

    async def contains_blacklisted_word(self, guild_id: int, text: str) -> bool:
        """Check against the per-guild custom blacklist."""
        words = await get_blacklist(guild_id)
        if not words:
            return False
        stripped, spaced, raw = self._normalize(text)
        return any(self._word_matches(w.lower(), stripped, spaced, raw) for w in words)



    def _check_caps(self, content: str) -> bool:
        """True if >70% of alphabetic characters are uppercase. Min 10 chars."""
        if len(content) < 10:
            return False
        letters = [c for c in content if c.isalpha()]
        if not letters:
            return False
        return sum(1 for c in letters if c.isupper()) / len(letters) > 0.70

    def _check_emoji(self, content: str) -> bool:
        """True if the message contains more than 5 emojis (unicode + custom)."""
        unicode_emoji_re = re.compile(
            r'[\U0001F600-\U0001F64F'
            r'\U0001F300-\U0001F5FF'
            r'\U0001F680-\U0001F6FF'
            r'\U0001F1E0-\U0001F1FF'
            r'\U00002702-\U000027B0'
            r'\U000024C2-\U0001F251'
            r'\U0001f926-\U0001f937'
            r'\U00010000-\U0010ffff'
            r'\u2640-\u2642'
            r'\u2600-\u2B55]+'
        )
        custom_emoji_re = re.compile(r'<a?:\w+:\d+>')
        total = len(unicode_emoji_re.findall(content)) + len(custom_emoji_re.findall(content))
        return total > 5

    def _check_mention(self, message: discord.Message) -> bool:
        """True if message has more than 3 user + role mentions combined."""
        return (len(message.mentions) + len(message.role_mentions)) > 3

    def _check_zalgo(self, content: str) -> bool:
        """True if message contains zalgo/glitch combining character sequences."""
        return bool(re.search(r'[\u0300-\u036f\u0489]{3,}', content))

    def _check_newlines(self, content: str) -> bool:
        """True if message has more than 10 line breaks."""
        return content.count('\n') > 10

    def _check_repeated_char(self, content: str) -> bool:
        """True if any single character repeats 10+ times in a row."""
        return bool(re.search(r'(.)\1{10,}', content))

    def _check_invite(self, content: str) -> bool:
        """True if message contains a Discord invite link."""
        return bool(re.search(r'discord\.(gg|com/invite)/\S+', content, re.IGNORECASE))

    def _check_link(self, content: str) -> bool:
        """True if message contains any URL."""
        return bool(re.search(r'(https?://|www\.)\S+', content, re.IGNORECASE))

    def _check_scam(self, content: str) -> bool:
        """True if message matches any known scam pattern."""
        return any(re.search(p, content, re.IGNORECASE) for p in SCAM_PATTERNS)

    def _check_everyone(self, content: str) -> bool:
        """True if message contains @everyone or @here."""
        return '@everyone' in content or '@here' in content

    def _check_external_emoji(self, message: discord.Message) -> bool:
        """True if message uses custom emojis that don't belong to this server."""
        guild_emoji_ids = {str(e.id) for e in message.guild.emojis}
        found_ids = re.findall(r'<a?:\w+:(\d+)>', message.content)
        return any(eid not in guild_emoji_ids for eid in found_ids)



    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and DMs
        if message.author.bot:
            return
        if not message.guild:
            return
        # Administrators are exempt from all filters
        if message.author.guild_permissions.administrator:
            return

        content  = message.content or ""
        settings = await get_automod(message.guild.id)
        uid      = message.author.id
        now      = discord.utils.utcnow().timestamp()

        # ── Anti-Spam (5 messages in 5 seconds) ────
        if settings.get("antispam"):
            self._spam_tracker.setdefault(uid, [])
            self._spam_tracker[uid] = [t for t in self._spam_tracker[uid] if now - t < 5]
            self._spam_tracker[uid].append(now)
            if len(self._spam_tracker[uid]) >= 5:
                self._spam_tracker[uid] = []
                await self.punish(message, "Anti-Spam", "Sending messages too fast (5 in 5 seconds)")
                return

        # ── Anti-Flood (8 messages in 10 seconds) ──
        if settings.get("antiflood"):
            self._flood_tracker.setdefault(uid, [])
            self._flood_tracker[uid] = [t for t in self._flood_tracker[uid] if now - t < 10]
            self._flood_tracker[uid].append(now)
            if len(self._flood_tracker[uid]) >= 8:
                self._flood_tracker[uid] = []
                await self.punish(message, "Anti-Flood", "Message flooding detected (8 in 10 seconds)")
                return

        # ── Anti-Scam ───────────────────────────────
        if settings.get("antiscam") and content and self._check_scam(content):
            await self.punish(message, "Anti-Scam", "Potential scam message detected")
            return

        # ── Anti-Invite ─────────────────────────────
        if settings.get("antiinvite") and content and self._check_invite(content):
            await self.punish(message, "Anti-Invite", "Discord invite links are not allowed")
            return

        # ── Anti-Link ───────────────────────────────
        if settings.get("antilink") and content and self._check_link(content):
            await self.punish(message, "Anti-Link", "Links are not allowed in this server")
            return

        # ── Anti-Everyone ───────────────────────────
        if settings.get("antieveryone") and content and self._check_everyone(content):
            await self.punish(message, "Anti-Everyone", "@everyone / @here is not allowed for regular members")
            return

        # ── Anti-Caps ───────────────────────────────
        if settings.get("anticaps") and content and self._check_caps(content):
            await self.punish(message, "Anti-Caps", "Excessive use of capital letters (>70%)")
            return

        # ── Anti-Emoji ──────────────────────────────
        if settings.get("antiemoji") and content and self._check_emoji(content):
            await self.punish(message, "Anti-Emoji", "Too many emojis in one message (max 5)")
            return

        # ── Anti-Mention ────────────────────────────
        if settings.get("antimention") and self._check_mention(message):
            await self.punish(message, "Anti-Mention", "Mass mentions are not allowed (max 3)")
            return

        # ── Anti-Zalgo ──────────────────────────────
        if settings.get("antizalgo") and content and self._check_zalgo(content):
            await self.punish(message, "Anti-Zalgo", "Zalgo/glitch text is not allowed")
            return

        # ── Anti-Newline ────────────────────────────
        if settings.get("antinewline") and content and self._check_newlines(content):
            await self.punish(message, "Anti-Newline", "Excessive line breaks (max 10 allowed)")
            return

        # ── Repeated Characters ─────────────────────
        if settings.get("repeatedchar") and content and self._check_repeated_char(content):
            await self.punish(message, "Repeated Characters", "Repeated character spam detected")
            return

        # ── Anti-Attachment ─────────────────────────
        if settings.get("antiattachment") and message.attachments:
            await self.punish(message, "Anti-Attachment", "File attachments are not allowed")
            return

        # ── Anti-External-Emoji ─────────────────────
        if settings.get("antiexternalemoji") and content and self._check_external_emoji(message):
            await self.punish(message, "Anti-External-Emoji", "Emojis from other servers are not allowed")
            return

        # ── Anti-Duplicate ──────────────────────────
        if settings.get("antiduplicate") and content:
            last_msg = self._duplicate_tracker.get(uid)
            if last_msg and last_msg == content.strip():
                await self.punish(message, "Anti-Duplicate", "Sending the same message repeatedly")
                return
            self._duplicate_tracker[uid] = content.strip()

        # ── Bad Word Filter (always active) ─────────
        if content and self.contains_bad_word(content):
            await self.punish(message, "Bad Word Filter", "Inappropriate language detected")
            return

        # ── Custom Blacklist (always active) ────────
        if content and await self.contains_blacklisted_word(message.guild.id, content):
            await self.punish(message, "Word Blacklist", "Blacklisted word detected")
            return



    async def _toggle(
        self,
        interaction: discord.Interaction,
        key:   str,
        label: str,
        hint:  str = ""
    ):
        settings = await get_automod(interaction.guild.id)
        new_val  = not settings.get(key, False)
        await set_automod_flag(interaction.guild.id, key, new_val)
        status = "✅ **Enabled**" if new_val else "❌ **Disabled**"
        extra  = f"\n`{hint}`" if new_val and hint else ""
        await interaction.response.send_message(
            f"🛡️ **{label}** is now {status}!{extra}", ephemeral=True
        )



    @app_commands.command(name="antispam", description="Toggle anti-spam (5 messages in 5 seconds)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antispam(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antispam", "Anti-Spam", "Triggers on 5+ messages in 5 seconds")

    @antispam.error
    async def antispam_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiflood", description="Toggle anti-flood (8 messages in 10 seconds)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiflood(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiflood", "Anti-Flood", "Triggers on 8+ messages in 10 seconds")

    @antiflood.error
    async def antiflood_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antilink", description="Toggle anti-link (blocks all URLs)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antilink(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antilink", "Anti-Link", "Blocks http://, https://, www. links")

    @antilink.error
    async def antilink_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiinvite", description="Toggle anti-invite (blocks Discord invites)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiinvite(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiinvite", "Anti-Invite", "Blocks discord.gg and discord.com/invite links")

    @antiinvite.error
    async def antiinvite_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiscam", description="Toggle anti-scam filter")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiscam(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiscam", "Anti-Scam", "Detects free nitro, phishing links, suspicious patterns")

    @antiscam.error
    async def antiscam_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="anticaps", description="Toggle anti-caps (blocks >70% caps messages)")
    @app_commands.checks.has_permissions(administrator=True)
    async def anticaps(self, interaction: discord.Interaction):
        await self._toggle(interaction, "anticaps", "Anti-Caps", "Blocks messages with >70% capital letters (min 10 chars)")

    @anticaps.error
    async def anticaps_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiemoji", description="Toggle anti-emoji (max 5 emojis per message)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiemoji(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiemoji", "Anti-Emoji", "Blocks messages with more than 5 emojis")

    @antiemoji.error
    async def antiemoji_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antimention", description="Toggle anti-mention (max 3 mentions per message)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antimention(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antimention", "Anti-Mention", "Blocks messages with more than 3 user/role mentions")

    @antimention.error
    async def antimention_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antizalgo", description="Toggle anti-zalgo (blocks glitch/cursed text)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antizalgo(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antizalgo", "Anti-Zalgo", "Blocks heavily decorated zalgo/glitch text")

    @antizalgo.error
    async def antizalgo_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antinewline", description="Toggle anti-newline (max 10 line breaks)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antinewline(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antinewline", "Anti-Newline", "Blocks messages with more than 10 line breaks")

    @antinewline.error
    async def antinewline_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="repeatedchar", description="Toggle repeated-character filter (10+ same chars in a row)")
    @app_commands.checks.has_permissions(administrator=True)
    async def repeatedchar(self, interaction: discord.Interaction):
        await self._toggle(interaction, "repeatedchar", "Repeated Characters", "Blocks 10+ identical characters in a row")

    @repeatedchar.error
    async def repeatedchar_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antieveryone", description="Toggle anti-everyone (@everyone/@here for non-admins)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antieveryone(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antieveryone", "Anti-Everyone", "Prevents non-admins from using @everyone or @here")

    @antieveryone.error
    async def antieveryone_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiattachment", description="Toggle anti-attachment (blocks file uploads)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiattachment(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiattachment", "Anti-Attachment", "Blocks all file and image uploads")

    @antiattachment.error
    async def antiattachment_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiexternalemoji", description="Toggle external emoji filter")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiexternalemoji(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiexternalemoji", "Anti-External-Emoji", "Blocks custom emojis from other servers")

    @antiexternalemoji.error
    async def antiexternalemoji_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="antiduplicate", description="Toggle anti-duplicate (blocks repeated consecutive messages)")
    @app_commands.checks.has_permissions(administrator=True)
    async def antiduplicate(self, interaction: discord.Interaction):
        await self._toggle(interaction, "antiduplicate", "Anti-Duplicate", "Blocks consecutive identical messages from the same user")

    @antiduplicate.error
    async def antiduplicate_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @app_commands.command(name="blacklistword", description="Add a word to this server's custom word filter")
    @app_commands.describe(word="Word to blacklist")
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklistword(self, interaction: discord.Interaction, word: str):
        added = await add_blacklist_word(interaction.guild.id, word.lower())
        if not added:
            await interaction.response.send_message(f"❌ `{word}` is already blacklisted!", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ `{word.lower()}` added to the word filter!", ephemeral=True)

    @blacklistword.error
    async def blacklistword_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="whitelistword", description="Remove a word from this server's custom word filter")
    @app_commands.describe(word="Word to remove from the filter")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelistword(self, interaction: discord.Interaction, word: str):
        removed = await remove_blacklist_word(interaction.guild.id, word.lower())
        if not removed:
            await interaction.response.send_message(f"❌ `{word}` is not in the blacklist!", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ `{word.lower()}` removed from the word filter!", ephemeral=True)

    @whitelistword.error
    async def whitelistword_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)

    @app_commands.command(name="blacklist", description="View all custom blacklisted words for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklist_view(self, interaction: discord.Interaction):
        words = await get_blacklist(interaction.guild.id)
        if not words:
            await interaction.response.send_message("📭 No custom blacklisted words set.", ephemeral=True)
            return
        word_list = ", ".join(f"`{w}`" for w in words)
        embed = discord.Embed(
            title=f"🚫 Blacklisted Words — {len(words)} total",
            description=word_list[:4000],
            color=discord.Color.red()
        )
        embed.set_footer(text="Use /whitelistword to remove a word")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist_view.error
    async def blacklist_view_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @app_commands.command(name="setpunishment", description="Set the default punishment for AutoMod violations")
    @app_commands.describe(level="Punishment level to apply")
    @app_commands.choices(level=[
        app_commands.Choice(name="⚠️  Warn  — DM warning only (default)",        value="warn"),
        app_commands.Choice(name="⏱️  Mute  — Timeout (scales with strikes)",     value="mute"),
        app_commands.Choice(name="👢  Kick  — Remove from server",                value="kick"),
        app_commands.Choice(name="🔨  Ban   — Permanently ban from server",       value="ban"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def setpunishment(self, interaction: discord.Interaction, level: str):
        await set_automod_flag(interaction.guild.id, "punishment", level)
        descriptions = {
            "warn": "Users will receive a DM warning only.",
            "mute": "Users will be timed out (duration scales: 10m × strike count).",
            "kick": "Users will be kicked from the server.",
            "ban":  "Users will be permanently banned.",
        }
        embed = discord.Embed(
            title="⚡ Punishment Level Updated",
            description=(
                f"AutoMod punishment set to **{level.upper()}**.\n"
                f"{descriptions[level]}\n\n"
                f"**Automatic escalation regardless of setting:**\n"
                f"Strike 3+ → Timeout\n"
                f"Strike 5+ → Kick\n"
                f"Strike 7+ → Ban"
            ),
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @setpunishment.error
    async def setpunishment_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)


    @app_commands.command(name="setlogchannel", description="Set the channel for AutoMod violation logs")
    @app_commands.describe(channel="Channel to send AutoMod logs to")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await set_guild_setting(interaction.guild.id, "log_channel", str(channel.id))
        await interaction.response.send_message(
            f"✅ AutoMod logs will now be sent to {channel.mention}!", ephemeral=True
        )

    @setlogchannel.error
    async def setlogchannel_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @app_commands.command(name="automodstatus", description="View all AutoMod filter statuses for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def automodstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        settings  = await get_automod(interaction.guild.id)
        guild_cfg = await get_guild_settings(interaction.guild.id)
        blacklist = await get_blacklist(interaction.guild.id)

        def s(key: str) -> str:
            return "✅" if settings.get(key) else "❌"

        log_ch_id  = guild_cfg.get("log_channel")
        log_ch     = interaction.guild.get_channel(int(log_ch_id)) if log_ch_id else None
        punishment = settings.get("punishment", "warn").upper()

        embed = discord.Embed(
            title=f"🛡️ AutoMod Status — {interaction.guild.name}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="⚡ Rate / Spam Filters",
            value=(
                f"{s('antispam')}  Anti-Spam (5 msgs / 5s)\n"
                f"{s('antiflood')}  Anti-Flood (8 msgs / 10s)\n"
                f"{s('antiduplicate')}  Anti-Duplicate"
            ),
            inline=True
        )
        embed.add_field(
            name="📝 Text Filters",
            value=(
                f"{s('anticaps')}  Anti-Caps (>70%)\n"
                f"{s('antiemoji')}  Anti-Emoji (>5)\n"
                f"{s('antimention')}  Anti-Mention (>3)\n"
                f"{s('antizalgo')}  Anti-Zalgo\n"
                f"{s('antinewline')}  Anti-Newline (>10)\n"
                f"{s('repeatedchar')}  Repeated Chars"
            ),
            inline=True
        )
        embed.add_field(
            name="🔗 Link & Content Filters",
            value=(
                f"{s('antilink')}  Anti-Link\n"
                f"{s('antiinvite')}  Anti-Invite\n"
                f"{s('antiscam')}  Anti-Scam\n"
                f"{s('antieveryone')}  Anti-Everyone\n"
                f"{s('antiattachment')}  Anti-Attachment\n"
                f"{s('antiexternalemoji')}  Anti-External-Emoji"
            ),
            inline=True
        )
        embed.add_field(
            name="🔤 Word Filters (always active)",
            value=(
                f"✅  Bad Word Filter (built-in list)\n"
                f"✅  Custom Blacklist ({len(blacklist)} word{'s' if len(blacklist) != 1 else ''})"
            ),
            inline=False
        )
        embed.add_field(
            name="⚙️ Configuration",
            value=(
                f"📋 Log Channel : {log_ch.mention if log_ch else '❌ Not set (auto-creates on first violation)'}\n"
                f"⚡ Punishment  : **{punishment}**\n\n"
                f"**Auto-escalation:** Strike 3→Mute | Strike 5→Kick | Strike 7→Ban"
            ),
            inline=False
        )
        embed.set_footer(text="Toggle filters with /antispam, /antilink etc. | Change punishment with /setpunishment")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @automodstatus.error
    async def automodstatus_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @app_commands.command(name="warnings", description="View AutoMod warnings for a member")
    @app_commands.describe(member="The member to check")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warnings_cmd(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        warns = await get_warnings(interaction.guild.id, member.id)
        if not warns:
            await interaction.followup.send(
                f"✅ **{member.name}** has no AutoMod warnings on record.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"⚠️ AutoMod Warnings — {member.name}",
            description=f"**{len(warns)}** warning(s) on record",
            color=discord.Color.yellow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        for i, w in enumerate(warns[:10], 1):
            ts = (
                discord.utils.format_dt(w["timestamp_utc"], style="R")
                if w.get("timestamp_utc") else "Unknown"
            )
            embed.add_field(
                name=f"Warning #{i}",
                value=f"📝 {w['reason']}\n🕐 {ts}",
                inline=False
            )

        if len(warns) > 10:
            embed.set_footer(text=f"Showing 10/{len(warns)} — use /clearwarnings to reset")
        else:
            embed.set_footer(text=f"ID: {member.id}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @warnings_cmd.error
    async def warnings_cmd_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Manage Messages permission required!", ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear all AutoMod warnings and reset strikes for a member")
    @app_commands.describe(member="The member to clear")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        count = await clear_warnings(interaction.guild.id, member.id)
        await reset_strikes(interaction.guild.id, member.id)
        if count == 0:
            await interaction.response.send_message(
                f"ℹ️ **{member.name}** had no warnings to clear.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Cleared **{count}** warning(s) and reset strikes for **{member.name}**!",
            ephemeral=True
        )

    @clearwarnings.error
    async def clearwarnings_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @app_commands.command(name="strikes", description="Check a member's AutoMod strike count")
    @app_commands.describe(member="The member to check")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def strikes_cmd(self, interaction: discord.Interaction, member: discord.Member):
        count = await get_strikes(interaction.guild.id, member.id)
        thresholds = {
            0: "✅ Clean record",
            1: "⚠️ 1 strike",
            2: "⚠️ 2 strikes",
            3: "🚨 3 strikes — mute threshold reached",
            4: "🚨 4 strikes",
            5: "🔴 5 strikes — kick threshold reached",
            6: "🔴 6 strikes",
        }
        status = thresholds.get(count, f"🔴 {count} strikes — ban threshold reached (7+)")
        await interaction.response.send_message(
            f"📊 **{member.name}** has **{count}** AutoMod strike(s)\n{status}",
            ephemeral=True
        )

    @strikes_cmd.error
    async def strikes_cmd_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Manage Messages permission required!", ephemeral=True)

    @app_commands.command(name="resetstrikes", description="Reset AutoMod strikes for a member")
    @app_commands.describe(member="The member to reset")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetstrikes(self, interaction: discord.Interaction, member: discord.Member):
        await reset_strikes(interaction.guild.id, member.id)
        await interaction.response.send_message(
            f"✅ Strikes reset for **{member.name}**!", ephemeral=True
        )

    @resetstrikes.error
    async def resetstrikes_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)



    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Auto-create the log channel when the bot joins a new server."""
        await self.get_log_channel(guild)


    @app_commands.command(name="help", description="Show all AutoMod bot commands")
    async def help_cmd(self, interaction: discord.Interaction):
        view  = HelpView(interaction.user.id)
        embed = help_home_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)




def help_home_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛡️ AutoMod Bot — Help",
        description=(
            "Welcome! Use the buttons below to explore all commands.\n\n"
            "🏠 **Home** — You are here\n"
            "⚡ **Filters** — Toggle automod filters on/off\n"
            "🔤 **Words** — Blacklist / whitelist words\n"
            "⚙️ **Config** — Setup log channel & punishment\n"
            "⚠️ **Moderation** — Warnings & strikes management\n"
            "📊 **Status** — View current settings dashboard"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="All commands are admin-only unless stated otherwise")
    return embed


def help_filters_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚡ Filter Toggle Commands",
        description="Each command **toggles** the filter ON or OFF.\nAll require **Administrator** permission.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="⚡ Rate / Spam",
        value=(
            "`/antispam` — Block 5+ messages in 5 seconds\n"
            "`/antiflood` — Block 8+ messages in 10 seconds\n"
            "`/antiduplicate` — Block repeated consecutive messages"
        ),
        inline=False
    )
    embed.add_field(
        name="📝 Text Content",
        value=(
            "`/anticaps` — Block >70% capital letters (min 10 chars)\n"
            "`/antiemoji` — Block more than 5 emojis per message\n"
            "`/antimention` — Block more than 3 mentions per message\n"
            "`/antizalgo` — Block zalgo / glitch text\n"
            "`/antinewline` — Block more than 10 line breaks\n"
            "`/repeatedchar` — Block 10+ repeated identical characters"
        ),
        inline=False
    )
    embed.add_field(
        name="🔗 Links & Content",
        value=(
            "`/antilink` — Block all URLs (http, https, www)\n"
            "`/antiinvite` — Block Discord invite links\n"
            "`/antiscam` — Block known scam patterns & phishing\n"
            "`/antieveryone` — Block @everyone / @here for non-admins\n"
            "`/antiattachment` — Block all file & image uploads\n"
            "`/antiexternalemoji` — Block emojis from other servers"
        ),
        inline=False
    )
    embed.set_footer(text="✅ Bad word filter & custom blacklist are always active — no toggle needed")
    return embed


def help_words_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔤 Word Filter Commands",
        description=(
            "The **bad word filter** is always active and catches evasion like `f*ck`, `@ss`, `fück` etc.\n"
            "You can also add your own custom words per server."
        ),
        color=discord.Color.red()
    )
    embed.add_field(
        name="📋 Commands",
        value=(
            "`/blacklistword <word>` — Add a word to this server's filter\n"
            "`/whitelistword <word>` — Remove a word from this server's filter\n"
            "`/blacklist` — View all custom blacklisted words"
        ),
        inline=False
    )
    embed.add_field(
        name="ℹ️ How evasion detection works",
        value=(
            "• `f*ck` → caught via skip-vowel pattern\n"
            "• `@ss` → caught via character map (@=a)\n"
            "• `f u c k` → caught via space-collapse\n"
            "• `fück` → caught via unicode normalization"
        ),
        inline=False
    )
    embed.set_footer(text="All word commands require Administrator permission")
    return embed


def help_config_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚙️ Configuration Commands",
        description="Setup your AutoMod bot for this server.",
        color=discord.Color.green()
    )
    embed.add_field(
        name="📋 Log Channel",
        value=(
            "`/setlogchannel <channel>` — Set where violation logs are sent\n"
            "ℹ️ If not set, bot auto-creates `#automod-logs` on first violation"
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ Punishment Level",
        value=(
            "`/setpunishment <level>` — Set what happens on violation\n\n"
            "`warn` — DM warning only *(default)*\n"
            "`mute` — Timeout (10min × strike count)\n"
            "`kick` — Remove from server\n"
            "`ban` — Permanently ban\n\n"
            "**Auto-escalation:**\n"
            "Strike 3+ → Timeout | Strike 5+ → Kick | Strike 7+ → Ban"
        ),
        inline=False
    )
    embed.set_footer(text="All config commands require Administrator permission")
    return embed


def help_moderation_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Warnings & Strikes Commands",
        description="Manage AutoMod violation records for members.",
        color=discord.Color.orange()
    )
    embed.add_field(
        name="⚠️ Warnings",
        value=(
            "`/warnings <member>` — View all warnings *(Manage Messages)*\n"
            "`/clearwarnings <member>` — Clear warnings + reset strikes *(Admin)*"
        ),
        inline=False
    )
    embed.add_field(
        name="🔢 Strikes",
        value=(
            "`/strikes <member>` — Check strike count *(Manage Messages)*\n"
            "`/resetstrikes <member>` — Reset strikes to 0 *(Admin)*"
        ),
        inline=False
    )
    embed.add_field(
        name="📋 Strike Thresholds",
        value=(
            "Strike 1-2 → Warning only\n"
            "Strike 3+ → Timeout (10min × strikes)\n"
            "Strike 5+ → Kicked from server\n"
            "Strike 7+ → Permanently banned"
        ),
        inline=False
    )
    embed.set_footer(text="Strikes reset automatically when cleared")
    return embed


def help_status_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📊 Status & Quick Setup",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="📊 Commands",
        value=(
            "`/automodstatus` — Full overview of all filters, punishment, log channel\n"
            "`/blacklist` — View custom blacklisted words\n"
            "`/warnings <member>` — View violation history\n"
            "`/strikes <member>` — View strike count"
        ),
        inline=False
    )
    embed.add_field(
        name="🚀 Quick Setup Guide",
        value=(
            "1️⃣ `/setlogchannel #channel` — set log destination\n"
            "2️⃣ `/setpunishment mute` — choose punishment level\n"
            "3️⃣ `/antispam` `/antilink` `/antiscam` — enable filters\n"
            "4️⃣ `/blacklistword <word>` — add custom words\n"
            "5️⃣ `/automodstatus` — confirm everything is set"
        ),
        inline=False
    )
    embed.set_footer(text="Use /help anytime to come back here")
    return embed

class HelpView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ These buttons are not for you!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🏠 Home",       style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_home_embed(), view=self)

    @discord.ui.button(label="⚡ Filters",    style=discord.ButtonStyle.secondary)
    async def filters(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_filters_embed(), view=self)

    @discord.ui.button(label="🔤 Words",      style=discord.ButtonStyle.secondary)
    async def words(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_words_embed(), view=self)

    @discord.ui.button(label="⚙️ Config",     style=discord.ButtonStyle.success)
    async def config(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_config_embed(), view=self)

    @discord.ui.button(label="⚠️ Moderation", style=discord.ButtonStyle.danger)
    async def moderation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_moderation_embed(), view=self)

    @discord.ui.button(label="📊 Status",     style=discord.ButtonStyle.primary)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=help_status_embed(), view=self)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
