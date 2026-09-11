import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import json
import re
import datetime
import uuid
import asyncio
from typing import Union, Optional

# database stuff (sqlite)
DB_FILE = "bot_data.db"

async def get_db():
    conn = await aiosqlite.connect(DB_FILE)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    conn = await get_db()
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY,
            prefix TEXT DEFAULT '?',
            mute_role INTEGER,
            modlogs_channel INTEGER,
            joinlogs_channel INTEGER
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS mod_roles (
            guild_id TEXT,
            role_id INTEGER,
            PRIMARY KEY (guild_id, role_id)
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS warnings (
            warn_id TEXT PRIMARY KEY,
            guild_id TEXT,
            user_id INTEGER,
            reason TEXT,
            timestamp TEXT
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS autopunish_rules (
            guild_id TEXT,
            warn_count INTEGER,
            punishment_type TEXT,
            duration TEXT,
            PRIMARY KEY (guild_id, warn_count)
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS active_mutes (
            guild_id TEXT,
            user_id INTEGER,
            roles TEXT,
            expires TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS active_bans (
            guild_id TEXT,
            user_id INTEGER,
            expires TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    await conn.commit()

async def get_guild_data(guild_id: str):
    conn = await get_db()
    
    async with conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
        guild = await cursor.fetchone()
    if not guild:
        async with conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,)):
            await conn.commit()
        async with conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
            guild = await cursor.fetchone()
        
    async with conn.execute("SELECT role_id FROM mod_roles WHERE guild_id = ?", (guild_id,)) as cursor:
        mod_roles = [row["role_id"] for row in await cursor.fetchall()]
    
    async with conn.execute("SELECT warn_id, user_id, reason, timestamp FROM warnings WHERE guild_id = ?", (guild_id,)) as cursor:
        warnings = [
            {"id": row["warn_id"], "user_id": row["user_id"], "reason": row["reason"], "timestamp": row["timestamp"]}
            for row in await cursor.fetchall()
        ]
    
    async with conn.execute("SELECT warn_count, punishment_type, duration FROM autopunish_rules WHERE guild_id = ?", (guild_id,)) as cursor:
        autopunish = {
            str(row["warn_count"]): {"type": row["punishment_type"], "duration": row["duration"]}
            for row in await cursor.fetchall()
        }
    
    async with conn.execute("SELECT user_id, roles, expires FROM active_mutes WHERE guild_id = ?", (guild_id,)) as cursor:
        active_mutes = {
            str(row["user_id"]): {"roles": json.loads(row["roles"]), "expires": row["expires"]}
            for row in await cursor.fetchall()
        }
    
    async with conn.execute("SELECT user_id, expires FROM active_bans WHERE guild_id = ?", (guild_id,)) as cursor:
        active_bans = {
            str(row["user_id"]): row["expires"]
            for row in await cursor.fetchall()
        }
    
    await conn.close()
    
    return {
        "prefix": guild["prefix"] if guild["prefix"] else "?",
        "mod_roles": mod_roles,
        "mute_role": guild["mute_role"],
        "modlogs_channel": guild["modlogs_channel"],
        "joinlogs_channel": guild["joinlogs_channel"],
        "warnings": warnings,
        "autopunish": autopunish,
        "active_mutes": active_mutes,
        "active_bans": active_bans
    }

async def update_guild_data(guild_id: str, key: str, value):
    conn = await get_db()
    
    await conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
    
    if key == "prefix":
        await conn.execute("UPDATE guild_settings SET prefix = ? WHERE guild_id = ?", (value, guild_id))
    elif key == "mute_role":
        await conn.execute("UPDATE guild_settings SET mute_role = ? WHERE guild_id = ?", (value, guild_id))
    elif key == "modlogs_channel":
        await conn.execute("UPDATE guild_settings SET modlogs_channel = ? WHERE guild_id = ?", (value, guild_id))
    elif key == "joinlogs_channel":
        await conn.execute("UPDATE guild_settings SET joinlogs_channel = ? WHERE guild_id = ?", (value, guild_id))
    elif key == "mod_roles":
        await conn.execute("DELETE FROM mod_roles WHERE guild_id = ?", (guild_id,))
        for role_id in value:
            await conn.execute("INSERT INTO mod_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    elif key == "warnings":
        await conn.execute("DELETE FROM warnings WHERE guild_id = ?", (guild_id,))
        for w in value:
            await conn.execute(
                "INSERT INTO warnings (warn_id, guild_id, user_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
                (w["id"], guild_id, w["user_id"], w["reason"], w["timestamp"])
            )
    elif key == "autopunish":
        await conn.execute("DELETE FROM autopunish_rules WHERE guild_id = ?", (guild_id,))
        for count_str, rule in value.items():
            await conn.execute(
                "INSERT INTO autopunish_rules (guild_id, warn_count, punishment_type, duration) VALUES (?, ?, ?, ?)",
                (guild_id, int(count_str), rule["type"], rule.get("duration"))
            )
    elif key == "active_mutes":
        await conn.execute("DELETE FROM active_mutes WHERE guild_id = ?", (guild_id,))
        for user_id_str, mute_info in value.items():
            await conn.execute(
                "INSERT INTO active_mutes (guild_id, user_id, roles, expires) VALUES (?, ?, ?, ?)",
                (guild_id, int(user_id_str), json.dumps(mute_info["roles"]), mute_info.get("expires"))
            )
    elif key == "active_bans":
        await conn.execute("DELETE FROM active_bans WHERE guild_id = ?", (guild_id,))
        for user_id_str, expiry in value.items():
            await conn.execute(
                "INSERT INTO active_bans (guild_id, user_id, expires) VALUES (?, ?, ?)",
                (guild_id, int(user_id_str), expiry)
            )
            
    await conn.commit()
    await conn.close()

# prefix stuff
async def get_prefix(bot, message):
    if not message.guild:
        return "?"
    guild_data = await get_guild_data(str(message.guild.id))
    custom_prefix = guild_data.get("prefix", "?")
    return commands.when_mentioned_or(custom_prefix)(bot, message)

# bot initialize
class MiraBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=get_prefix, intents=intents, help_command=None)
    async def setup_hook(self):
        await init_db()
        print("DB initialized.")
        try:
            await self.tree.sync()
            print("Successfully synced global commands.")
        except Exception as e:
            print(f"Failed to sync global commands: {e}")
        expiration_loop.start()
intents = discord.Intents.all()
bot = MiraBot()

# time stuff
def parse_duration(duration_str: Optional[str]) -> Optional[datetime.timedelta]:
    if not duration_str or duration_str.lower() == "infinite":
        return None
    regex = re.compile(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?')
    matches = regex.match(duration_str)
    if not matches or not any(matches.groups()):
        return None
    
    days, hours, minutes, seconds = matches.groups()
    return datetime.timedelta(
        days=int(days) if days else 0,
        hours=int(hours) if hours else 0,
        minutes=int(minutes) if minutes else 0,
        seconds=int(seconds) if seconds else 0
    )

def format_timedelta(td: Optional[datetime.timedelta]) -> str:
    if not td:
        return "Infinite"
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    if seconds > 0: parts.append(f"{seconds}s")
    return "".join(parts) if parts else "0s"

# perms check
def is_mod_or_admin():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        guild_data = await get_guild_data(str(ctx.guild.id))
        mod_roles = guild_data.get("mod_roles", [])
        user_role_ids = [role.id for role in ctx.author.roles]
        if any(role_id in mod_roles for role_id in user_role_ids):
            return True
        raise commands.CheckFailure("You do not have permission to run this command.")
    return commands.check(predicate)

def is_admin():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        raise commands.CheckFailure("You do not have permission to run this command.")
    return commands.check(predicate)

# logging
async def send_mod_log(guild: discord.Guild, action: str, target: Union[discord.Member, discord.User], moderator: discord.Member, duration: str, reason: str):
    guild_data = await get_guild_data(str(guild.id))
    channel_id = guild_data.get("modlogs_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return

    embed = discord.Embed(title="Moderation Action Logged", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="Punishment", value=action, inline=True)
    embed.add_field(name="Target", value=f"{target.mention} ({target.id})", inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass

async def send_target_dm(target: Union[discord.Member, discord.User], action: str, duration: str, reason: str):
    embed = discord.Embed(title="Notification", color=discord.Color.orange())
    embed.add_field(name="Action Taken", value=action, inline=True)
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    try:
        await target.send(embed=embed)
    except Exception:
        pass

# autopunish check
async def check_autopunish(ctx, user: discord.Member):
    guild = ctx.guild
    ctx_mod = ctx.author
    guild_data = await get_guild_data(str(guild.id))
    user_warns = [w for w in guild_data.get("warnings", []) if w["user_id"] == user.id]
    warn_count = len(user_warns)
    
    autopunish_rules = guild_data.get("autopunish", {})
    if str(warn_count) in autopunish_rules:
        rule = autopunish_rules[str(warn_count)]
        punishment = rule["type"].lower()
        duration_str = rule.get("duration")
        
        past_tense_punishment = "muted" if punishment == "mute" else f"{punishment}ed"
        reason = f"Automatically {past_tense_punishment} due to receiving {warn_count} warnings."
        
        if punishment == "kick":
            try:
                await send_target_dm(user, "Kick", "N/A", reason)
                await guild.kick(user, reason=reason)
                await send_mod_log(guild, "Kick", user, ctx_mod, "N/A", reason)
                await ctx.send(f"{user.name} has been auto-kicked.")
            except discord.Forbidden:
                pass
        elif punishment == "ban":
            try:
                await send_target_dm(user, "Ban", duration_str or "Infinite", reason)
                await guild.ban(user, reason=reason, delete_message_days=0)
                await send_mod_log(guild, "Ban", user, ctx_mod, duration_str or "Infinite", reason)
                await ctx.send(f"{user.name} has been auto-banned.")
                
                if duration_str and duration_str.lower() != "infinite":
                    delta = parse_duration(duration_str)
                    if delta:
                        expiry = (datetime.datetime.utcnow() + delta).isoformat()
                        active_bans = guild_data.get("active_bans", {})
                        active_bans[str(user.id)] = expiry
                        await update_guild_data(str(guild.id), "active_bans", active_bans)
            except discord.Forbidden:
                pass
        elif punishment == "mute":
            mute_role_id = guild_data.get("mute_role")
            if not mute_role_id:
                return
            mute_role = guild.get_role(int(mute_role_id))
            if not mute_role:
                return
            
            try:
                saved_roles = [role.id for role in user.roles if role != guild.default_role]
                active_mutes = guild_data.get("active_mutes", {})
                
                expiry = None
                if duration_str and duration_str.lower() != "infinite":
                    delta = parse_duration(duration_str)
                    if delta:
                        expiry = (datetime.datetime.utcnow() + delta).isoformat()
                
                active_mutes[str(user.id)] = {"roles": saved_roles, "expires": expiry}
                await update_guild_data(str(guild.id), "active_mutes", active_mutes)
                
                await user.edit(roles=[mute_role], reason=reason)
                await send_target_dm(user, "Mute", duration_str or "Infinite", reason)
                await send_mod_log(guild, "Mute", user, ctx_mod, duration_str or "Infinite", reason)
                
                await ctx.send(f"{user.name} has been auto-muted.")
            except discord.Forbidden:
                pass

# bot events and expirations
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Game(name="Among Us"))        
    await process_expirations()


async def process_expirations():
    conn = await get_db()
    async with conn.execute("SELECT guild_id FROM guild_settings") as cursor:
        guild_ids = [row["guild_id"] for row in await cursor.fetchall()]
    await conn.close()
    
    now = datetime.datetime.utcnow()
    
    for guild_id in guild_ids:
        gdata = await get_guild_data(guild_id)
        guild = bot.get_guild(int(guild_id))
        if not guild:
            continue
            
        active_mutes = gdata.get("active_mutes", {})
        for user_id, mute_info in list(active_mutes.items()):
            if mute_info.get("expires"):
                expiry = datetime.datetime.fromisoformat(mute_info["expires"])
                if now >= expiry:
                    member = guild.get_member(int(user_id))
                    if member:
                        mute_role_id = gdata.get("mute_role")
                        mute_role = guild.get_role(int(mute_role_id)) if mute_role_id else None
                        roles_to_restore = [guild.get_role(rid) for rid in mute_info["roles"] if guild.get_role(rid)]
                        
                        try:
                            if mute_role in member.roles:
                                await member.remove_roles(mute_role)
                            await member.add_roles(*roles_to_restore)
                            await send_mod_log(guild, "Unmute (Expired)", member, bot.user, "N/A", "Mute duration expired.")
                        except discord.Forbidden:
                            pass
                    del active_mutes[user_id]
        await update_guild_data(guild_id, "active_mutes", active_mutes)
        
        active_bans = gdata.get("active_bans", {})
        for user_id, ban_expiry_str in list(active_bans.items()):
            expiry = datetime.datetime.fromisoformat(ban_expiry_str)
            if now >= expiry:
                try:
                    user = await bot.fetch_user(int(user_id))
                    await guild.unban(user, reason="Ban duration expired.")
                    await send_mod_log(guild, "Unban (Expired)", user, bot.user, "N/A", "Ban duration expired.")
                except Exception:
                    pass
                del active_bans[user_id]
        await update_guild_data(guild_id, "active_bans", active_bans)

@tasks.loop(seconds=30)
async def expiration_loop():
    await process_expirations()

# joinlogs
@bot.event
async def on_member_join(member: discord.Member):
    guild_data = await get_guild_data(str(member.guild.id))
    channel_id = guild_data.get("joinlogs_channel")
    if channel_id:
        channel = member.guild.get_channel(int(channel_id))
        if channel:
            await channel.send(f"{member.mention} has joined {member.guild.name}! We now have {member.guild.member_count} members.")

@bot.event
async def on_member_remove(member: discord.Member):
    guild_data = await get_guild_data(str(member.guild.id))
    channel_id = guild_data.get("joinlogs_channel")
    if channel_id:
        channel = member.guild.get_channel(int(channel_id))
        if channel:
            await channel.send(f"{member.name} has left {member.guild.name} :sob:. We now have {member.guild.member_count} members.")

# error handler
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        guild_prefix = (await get_guild_data(str(ctx.guild.id))).get("prefix", "?") if ctx.guild else "?"
        await ctx.send(f"Error: Missing required parameters. Type `{guild_prefix}help` for formatting.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Error: {error}")
    else:
        await ctx.send(f"An unexpected error occurred: {error}")

# commands

@bot.command(
    name="kick",
    brief="Command for kicking users from the server",
    help="""**Format: {prefix}kick (user) [reason]**
    **Perms required: Moderator**
    This command can be used to kick users from the server. Kicked members can rejoin the server with a valid invite."""
)
@is_mod_or_admin()
async def kick_cmd(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    if not ctx.guild.me.guild_permissions.kick_members:
        return await ctx.send("Bot is missing required permissions.")
    try:
        await send_target_dm(member, "Kick", "N/A", reason)
        await member.kick(reason=reason)
        await ctx.send(f"{member.name} has been kicked!")
        await send_mod_log(ctx.guild, "Kick", member, ctx.author, "N/A", reason)
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="ban",
    brief="Command used for banning members from the server.",
    help="""**Format: {prefix}ban (user) [duration] [reason]**
    **Perms required: Moderator**
    This command can be used to ban users from the server. Banned users cannot rejoin until the ban expires."""
)
@is_mod_or_admin()
async def ban_cmd(ctx, member: Union[discord.Member, discord.User], duration_or_reason: Optional[str] = None, *, reason: Optional[str] = None):
    if not ctx.guild.me.guild_permissions.ban_members:
        return await ctx.send("Bot is missing required permissions.")
    
    is_infinite = duration_or_reason and duration_or_reason.lower() == "infinite"
    td = parse_duration(duration_or_reason)
    
    if td or is_infinite:
        duration_str = duration_or_reason
        final_reason = reason or "No reason provided."
    else:
        duration_str = None
        final_reason = f"{duration_or_reason or ''} {reason or ''}".strip() or "No reason provided."

    try:
        await send_target_dm(member, "Ban", duration_str or "Infinite", final_reason)
        await ctx.guild.ban(member, reason=final_reason, delete_message_days=0)
        await ctx.send(f"{member.name} has been banned!")
        await send_mod_log(ctx.guild, "Ban", member, ctx.author, duration_str or "Infinite", final_reason)
        
        if td:
            expiry = (datetime.datetime.utcnow() + td).isoformat()
            guild_data = await get_guild_data(str(ctx.guild.id))
            active_bans = guild_data.get("active_bans", {})
            active_bans[str(member.id)] = expiry
            await update_guild_data(str(ctx.guild.id), "active_bans", active_bans)
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="unban",
    brief="Command to unban a banned user",
    help="""**Format: {prefix}unban (user) [reason]**
    **Perms required: Moderator**
    This command can be used to rovoke a user's ban before it expires."""
)
@is_mod_or_admin()
async def unban_cmd(ctx, user: discord.User, *, reason: str = "No reason provided."):
    if not ctx.guild.me.guild_permissions.ban_members:
        return await ctx.send("Bot is missing required permissions.")
    try:
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"{user.name} has been unbanned!")
        await send_mod_log(ctx.guild, "Unban", user, ctx.author, "N/A", reason)
        
        guild_data = await get_guild_data(str(ctx.guild.id))
        active_bans = guild_data.get("active_bans", {})
        if str(user.id) in active_bans:
            del active_bans[str(user.id)]
            await update_guild_data(str(ctx.guild.id), "active_bans", active_bans)
    except discord.NotFound:
        await ctx.send("User is not banned.")
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

# modrole cmds
@bot.group(
    name="modrole",
    invoke_without_command=True,
    brief="Command to view moderator roles",
    help="""**Format:**
    {prefix}modrole (to view moderator roles)
    {prefix}modrole add (role)
    {prefix}modrole remove (role)""",
    aliases=["modroles", "modrolelist", "listmodrole"]
)
async def modrole_group(ctx):
    guild_data = await get_guild_data(str(ctx.guild.id))
    mod_roles = guild_data.get("mod_roles", [])
    if not mod_roles:
        return await ctx.send("No moderator roles configured.")
    
    mentions = [ctx.guild.get_role(rid).mention for rid in mod_roles if ctx.guild.get_role(rid)]
    await ctx.send(f"Moderator roles: {', '.join(mentions)}")

@modrole_group.command(name="add")
@is_admin()
async def modrole_add(ctx, role: discord.Role):
    guild_data = await get_guild_data(str(ctx.guild.id))
    mod_roles = guild_data.get("mod_roles", [])
    if role.id not in mod_roles:
        mod_roles.append(role.id)
        await update_guild_data(str(ctx.guild.id), "mod_roles", mod_roles)
        await ctx.send(f"Added {role.name} to the mod roles list.")
    else:
        await ctx.send("Role is already in the mod roles list.")

@modrole_group.command(name="remove")
@is_admin()
async def modrole_remove(ctx, role: discord.Role):
    guild_data = await get_guild_data(str(ctx.guild.id))
    mod_roles = guild_data.get("mod_roles", [])
    if role.id in mod_roles:
        mod_roles.remove(role.id)
        await update_guild_data(str(ctx.guild.id), "mod_roles", mod_roles)
        await ctx.send(f"Removed {role.name} from the mod roles list.")
    else:
        await ctx.send("Role is not in the mod roles list.")

@modrole_group.command(name="list")
async def modrole_list(ctx):
    await modrole_group(ctx)

@bot.command(
    name="mute",
    brief="Command to mute a member",
    help="""**Format: {prefix}mute (user) [duration] [reason]**
    **Perms required: Moderator**
    This command is used to mute a member. Muted members cannot send messages."""
)
@is_mod_or_admin()
async def mute_cmd(ctx, member: discord.Member, duration_or_reason: Optional[str] = None, *, reason: Optional[str] = None):
    guild_data = await get_guild_data(str(ctx.guild.id))
    mute_role_id = guild_data.get("mute_role")
    if not mute_role_id:
        return await ctx.send("Muted role is not defined.")
    
    mute_role = ctx.guild.get_role(int(mute_role_id))
    if not mute_role:
        return await ctx.send("Muted role is not defined.")
        
    is_infinite = duration_or_reason and duration_or_reason.lower() == "infinite"
    td = parse_duration(duration_or_reason)
    
    if td or is_infinite:
        duration_str = duration_or_reason
        final_reason = reason or "No reason provided."
    else:
        duration_str = None
        final_reason = f"{duration_or_reason or ''} {reason or ''}".strip() or "No reason provided."
        
    try:
        saved_roles = [role.id for role in member.roles if role != ctx.guild.default_role]
        active_mutes = guild_data.get("active_mutes", {})
        
        expiry = (datetime.datetime.utcnow() + td).isoformat() if td else None
        active_mutes[str(member.id)] = {"roles": saved_roles, "expires": expiry}
        await update_guild_data(str(ctx.guild.id), "active_mutes", active_mutes)
        
        await member.edit(roles=[mute_role], reason=final_reason)
        await ctx.send(f"{member.name} has been muted!")
        await send_target_dm(member, "Mute", duration_str or "Infinite", final_reason)
        await send_mod_log(ctx.guild, "Mute", member, ctx.author, duration_str or "Infinite", final_reason)
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="unmute",
    brief="Command to unmute a muted member",
    help="""**Format: {prefix}unmute (user) [reason]**
    **Perms required: Moderator**
    This command is used for unmuting a muted member."""
    )
@is_mod_or_admin()
async def unmute_cmd(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    guild_data = await get_guild_data(str(ctx.guild.id))
    active_mutes = guild_data.get("active_mutes", {})
    
    mute_role_id = guild_data.get("mute_role")
    mute_role = ctx.guild.get_role(int(mute_role_id)) if mute_role_id else None
    
    if str(member.id) not in active_mutes and (not mute_role or mute_role not in member.roles):
        return await ctx.send("This user is not muted.")
        
    try:
        roles_info = active_mutes.get(str(member.id), {"roles": []})
        roles_to_restore = [ctx.guild.get_role(rid) for rid in roles_info["roles"] if ctx.guild.get_role(rid)]
        
        if mute_role and mute_role in member.roles:
            await member.remove_roles(mute_role)
        if roles_to_restore:
            await member.add_roles(*roles_to_restore)
            
        if str(member.id) in active_mutes:
            del active_mutes[str(member.id)]
            await update_guild_data(str(ctx.guild.id), "active_mutes", active_mutes)
            
        await ctx.send(f"{member.name} has been unmuted!")
        await send_target_dm(member, "Unmute", "N/A", reason)
        await send_mod_log(ctx.guild, "Unmute", member, ctx.author, "N/A", reason)
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="muterole",
    brief="Command to define Muted role",
    help="""**Format: {prefix}muterole (role)**
    **Perms required: Administrator**
    This command is used for defining the role used for mute."""
)
@is_admin()
async def muterole_cmd(ctx, role: discord.Role):
    await update_guild_data(str(ctx.guild.id), "mute_role", role.id)
    await ctx.send(f"Defined the Muted role to {role.name}.")

# prefix
@bot.hybrid_group(
    name="prefix",
    fallback="view",
    brief="Command for checking and changing prefix",
    help="""**Format:**
    {prefix}prefix (for viewing current prefix)
    {prefix}prefix set (prefix) (for changing prefix)
    This command can be used to view and change the prefix.
    -# Tip: Type `@MiraBot#7374 prefix` or `/prefix view` in case you forget the prefix."""
)
async def prefix_group(ctx):
    guild_data = await get_guild_data(str(ctx.guild.id))
    current = guild_data.get("prefix", "?")
    await ctx.send(f"The current prefix is `{current}`")

@prefix_group.command(name="set")
@is_admin()
@app_commands.describe(new_prefix="The prefix to use for textual commands")
async def prefix_set(ctx, new_prefix: str):
    if ctx.interaction:
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send("You do not have permission to run this command.", ephemeral=True)
            
    await update_guild_data(str(ctx.guild.id), "prefix", new_prefix)
    await ctx.send(f"Changed the set prefix to `{new_prefix}`")

@bot.command(
    name="modlogs",
    brief="Command for setting modlogs channel",
    help="""**Format: {prefix}modlogs (channel/disable)**
    Command for setting up moderation logs channel."""
)
@is_admin()
async def modlogs_cmd(ctx, channel: Union[discord.TextChannel, str]):
    if isinstance(channel, str) and channel.lower() == "disable":
        await update_guild_data(str(ctx.guild.id), "modlogs_channel", None)
        await ctx.send("Disabled modlogs.")
    elif isinstance(channel, discord.TextChannel):
        await update_guild_data(str(ctx.guild.id), "modlogs_channel", channel.id)
        await ctx.send(f"Defined the modlogs channel to {channel.mention}.")
    else:
        await ctx.send("Invalid setup input. Provide a channel mention or 'disable'.")

@bot.command(
    name="joinlogs",
    brief="Command to set joinlogs channel",
    help="""**Format: {prefix}joinlogs (channel/disable)**
    **Perms required: Administrator**
    This command is used for setting up join/leave messages."""
)
@is_admin()
async def joinlogs_cmd(ctx, channel: Union[discord.TextChannel, str]):
    if isinstance(channel, str) and channel.lower() == "disable":
        await update_guild_data(str(ctx.guild.id), "joinlogs_channel", None)
        await ctx.send("Disabled joinlogs.")
    elif isinstance(channel, discord.TextChannel):
        await update_guild_data(str(ctx.guild.id), "joinlogs_channel", channel.id)
        await ctx.send(f"Defined the joinlogs channel to {channel.mention}.")
    else:
        await ctx.send("Invalid setup input. Provide a channel mention or 'disable'.")

@bot.command(
    name="warn",
    brief="Command to warn a member",
    help="""**Format: {prefix}warn (user) [reason]**
    **Perms required: Moderator**
    COmmand to warn a member."""
)
@is_mod_or_admin()
async def warn_cmd(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    warn_id = str(uuid.uuid4())[:8]
    guild_data = await get_guild_data(str(ctx.guild.id))
    warnings = guild_data.get("warnings", [])
    
    warnings.append({
        "id": warn_id,
        "user_id": member.id,
        "reason": reason,
        "timestamp": datetime.datetime.utcnow().isoformat()
    })
    await update_guild_data(str(ctx.guild.id), "warnings", warnings)
    
    await ctx.send(f"{member.name} has been warned!")
    await send_target_dm(member, f"Warn (ID: {warn_id})", "N/A", reason)
    await send_mod_log(ctx.guild, f"Warn (ID: {warn_id})", member, ctx.author, "N/A", reason)
    
    await check_autopunish(ctx, member)

@bot.command(
    name="warnings",
    breif="Check a user's warnings",
    help="""**Format: {prefix}warnings [user]**
    Check warnings list for any member. Defaults to self if no user is specified.""",
    aliases=["warns"]
)
async def warnings_cmd(ctx, user: Optional[discord.Member] = None):
    target = user or ctx.author
    guild_data = await get_guild_data(str(ctx.guild.id))
    user_warns = [w for w in guild_data.get("warnings", []) if w["user_id"] == target.id]
    
    if not user_warns:
        return await ctx.send("This user has no warnings.")
        
    embed = discord.Embed(title=f"Warnings for {target.name}", color=discord.Color.yellow())
    for w in user_warns:
        embed.add_field(name=f"ID: {w['id']}", value=f"Reason: {w['reason']}", inline=False)
    await ctx.send(embed=embed)

@bot.command(
    name="removewarn", 
    brief="Command to delete a warning",
    help="""**Format: {prefix}removewarn (warn id) [reason]**
    Perms required: Moderator
    This command is used to remove an existing warning.""",
    aliases=["unwarn"]
)
@is_mod_or_admin()
async def removewarn_cmd(ctx, warnid: str, *, reason: str = "No reason provided."):
    guild_data = await get_guild_data(str(ctx.guild.id))
    warnings = guild_data.get("warnings", [])
    
    found = None
    for w in warnings:
        if w["id"] == warnid:
            found = w
            break
            
    if not found:
        return await ctx.send("Warning ID not found.")
        
    warnings.remove(found)
    await update_guild_data(str(ctx.guild.id), "warnings", warnings)
    
    target = await bot.fetch_user(found["user_id"])
    await ctx.send(f"Warning {warnid} has been removed.")
    await send_mod_log(ctx.guild, f"Remove Warn ({warnid})", target, ctx.author, "N/A", reason)

@bot.command(
    name="slowmode",
    brief="Command to set slowmode",
    help="""**Format: {prefix}slowmode (time) [channel]**
    **Perms required: Moderator**
    This command is used to set slowmode.""",
    aliases=["cooldown"]
)
@is_mod_or_admin()
async def slowmode_cmd(ctx, time_str: str, channel: Optional[discord.TextChannel] = None):
    target_channel = channel or ctx.channel
    td = parse_duration(time_str)
    if not td:
        return await ctx.send("Invalid duration format. Use e.g. 5s, 2m.")
        
    seconds = int(td.total_seconds())
    if seconds > 21600:
        return await ctx.send("Slowmode duration cannot exceed 6 hours.")
        
    try:
        await target_channel.edit(slowmode_delay=seconds)
        await ctx.send(f"Set slowmode to {time_str} in {target_channel.mention}.")
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="lockdown", 
    brief="Command to lock a channel",
    help="""**Format: {prefix}lockdown [channel] [duration]**
    This command is used to lock channels making non-moderators unable to send messages.""",
    aliases=["lock"]
)
@is_mod_or_admin()
async def lockdown_cmd(ctx, channel: Optional[discord.TextChannel] = None, duration_str: Optional[str] = None):
    target_channel = channel or ctx.channel
    overwrite = target_channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    
    try:
        await target_channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"Locked down {target_channel.mention}.")
        
        td = parse_duration(duration_str)
        if td:
            await asyncio.sleep(td.total_seconds())
            overwrite.send_messages = None
            await target_channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            await ctx.send(f"Unlocked down {target_channel.mention} automatically.")
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

@bot.command(
    name="unlockdown",
    brief="Command to unlock a locked channel",
    help="""**Format: {prefix}unlockdown [channel] [duration]**
    This command is used to unlock a previously locked channel to allow members to send messages again.""",
    aliases=["unlock"]
)
@is_mod_or_admin()
async def unlockdown_cmd(ctx, channel: Optional[discord.TextChannel] = None):
    target_channel = channel or ctx.channel
    overwrite = target_channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    try:
        await target_channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"Unlocked down {target_channel.mention}.")
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

class HelpPaginationView(discord.ui.View):
    def __init__(self, cmds, prefix, ctx):
        super().__init__(timeout=600)
        self.cmds = cmds
        self.prefix = prefix
        self.ctx = ctx
        self.current_page = 0
        self.per_page = 5
        self.max_pages = max(1, (len(cmds) - 1) // self.per_page + 1)
        self.update_buttons()

    def update_buttons(self):
        self.first_page.disabled = self.current_page == 0
        self.prev_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= self.max_pages - 1
        self.last_page.disabled = self.current_page >= self.max_pages - 1

    def build_embed(self):
        embed = discord.Embed(
            title="Help Menu",
            description="You can view a full list of all commands here.",
            color=discord.Color.blue()
        )
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_cmds = self.cmds[start:end]

        for cmd in page_cmds:
            short_desc = (cmd.brief or "it seems Mira hasnt written a description here yet.").replace("{prefix}", self.prefix)
            embed.add_field(name=f"{self.prefix}{cmd.name}", value=short_desc, inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.max_pages}")
        return embed

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="⏪")
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your help menu!", ephemeral=True)
        self.current_page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="◀️")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your help menu!", ephemeral=True)
        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="▶️")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your help menu!", ephemeral=True)
        self.current_page = min(self.max_pages - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="⏩")
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("This isn't your help menu!", ephemeral=True)
        self.current_page = self.max_pages - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        
# help
@bot.hybrid_command(
    name="help",
    description="Displays the bot commands help menu.",
    brief="Show the help menu",
    help="""**Format: {prefix}help [command]**
    This command can be used to view the list of commands. You can specify a command to view further details."""
)
@app_commands.describe(command_name="The specific command you would like to view help for")
async def help_cmd(ctx, command_name: Optional[str] = None):
    prefix = "?"
    if ctx.guild:
        guild_data = await get_guild_data(str(ctx.guild.id))
        prefix = guild_data.get("prefix", "?")

    if command_name:
        cmd = bot.get_command(command_name)
        if not cmd or cmd.hidden:
            return await ctx.send(f"Command `{command_name}` not found.")
            
        embed = discord.Embed(title=f"Command Help for {prefix}{cmd.name}", color=discord.Color.purple())
        long_desc = (cmd.help or cmd.brief or "it seems <@841248604629762049> hasnt written a description here yet :(").replace("{prefix}", prefix)
        embed.description = long_desc
        return await ctx.send(embed=embed)

    cmds = sorted([c for c in bot.commands if not c.hidden], key=lambda c: c.name)
    if not cmds:
        return await ctx.send("There are no commands available.")

    view = HelpPaginationView(cmds, prefix, ctx)
    await ctx.send(embed=view.build_embed(), view=view)

# autopunish cmds
@bot.group(
    name="autopunish",
    invoke_without_command=True,
    brief="Command to view or manage autopunish rules",
    help="""**Format:**
    {prefix}autopunish (to list autopunish rules)
    {prefix}autopunish add (count) (type) [duration]
    {prefix}autopunish remove (count)
    {prefix}autopunish list"""
)
async def autopunish_group(ctx):
    await ctx.invoke(autopunish_list)

@autopunish_group.command(name="add")
@is_admin()
async def autopunish_add(ctx, count: int, ptype: str, duration: Optional[str] = None):
    ptype = ptype.lower()
    if ptype not in ["mute", "kick", "ban"]:
        return await ctx.send("Invalid punishment type. Choose between: mute, kick, ban.")
    if ptype == "kick" and duration:
        return await ctx.send("Kick punishments cannot have a duration attached.")
        
    guild_data = await get_guild_data(str(ctx.guild.id))
    autopunish = guild_data.get("autopunish", {})
    
    autopunish[str(count)] = {
        "type": ptype,
        "duration": duration
    }
    await update_guild_data(str(ctx.guild.id), "autopunish", autopunish)
    await ctx.send(f"Added autopunish rule for {count} warnings -> {ptype} {duration or ''}")

@autopunish_group.command(name="remove")
@is_admin()
async def autopunish_remove(ctx, count: int):
    guild_data = await get_guild_data(str(ctx.guild.id))
    autopunish = guild_data.get("autopunish", {})
    
    if str(count) in autopunish:
        del autopunish[str(count)]
        await update_guild_data(str(ctx.guild.id), "autopunish", autopunish)
        await ctx.send(f"Removed autopunish rule for {count} warnings.")
    else:
        await ctx.send("No autopunish rule exists for that warning count.")

@autopunish_group.command(name="list")
async def autopunish_list(ctx):
    guild_data = await get_guild_data(str(ctx.guild.id))
    autopunish = guild_data.get("autopunish", {})
    
    if not autopunish:
        return await ctx.send("No autopunish rules set up.")
        
    embed = discord.Embed(title="Autopunish Regulations", color=discord.Color.dark_magenta())
    for count, rule in sorted(autopunish.items(), key=lambda x: int(x[0])):
        ptype = rule['type'].capitalize()
        duration = "N/A" if rule['type'].lower() == "kick" else (rule.get('duration') or 'Infinite')
        embed.add_field(name=f"{count} Warnings", value=f"Punishment: {ptype} | Duration: {duration}", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(
    name="echo",
    brief="Command to echo a message to a channel",
    help="""**Format: {prefix}echo (message) [channel]**
    This command is used to send a message to a specified channel."""
)
@app_commands.describe(msg="The message you want to echo", channel="The channel to send the message to")
@is_admin()
async def echo_cmd(ctx, msg: str, channel: Optional[discord.TextChannel] = None):
    target_channel = channel or ctx.channel
    try:
        await target_channel.send(msg)
        if ctx.interaction:
            await ctx.send("Successfully echoed message!", ephemeral=True)
        else:
            await ctx.send("Successfully echoed message!")
    except discord.Forbidden:
        if ctx.interaction:
            await ctx.send("Bot is missing required permissions to send messages in that channel.", ephemeral=True)
        else:
            await ctx.send("Bot is missing required permissions to send messages in that channel.")

@bot.command(
    name="close",
    brief="Close a lobby thread.",
    help="""Please type {prefix}close to close lobby threads for closed lobbies.""",
    aliases=["threadclose","lobbyclose"]
)
async def close_cmd(ctx):
    if not isinstance(ctx.channel, (discord.Thread, discord.ForumChannel)):
        if hasattr(ctx.channel, 'parent') and ctx.channel.parent and isinstance(ctx.channel.parent, discord.ForumChannel):
            pass 
        else:
            return await ctx.send("This command can only be used inside threads or forum posts.")

    is_moderator = False
    if ctx.author.guild_permissions.administrator:
        is_moderator = True
    else:
        guild_data = await get_guild_data(str(ctx.guild.id))
        mod_roles = guild_data.get("mod_roles", [])
        if any(r.id in mod_roles for r in ctx.author.roles):
            is_moderator = True

    is_op = (ctx.channel.owner_id == ctx.author.id)

    if not (is_op or is_moderator):
        return await ctx.send("You do not have permission to run this command.")

    try:
        await ctx.send("Closed and locked the thread.")
        await ctx.channel.edit(name="CLOSED", locked=True, archived=True)
    except discord.Forbidden:
        await ctx.send("Bot is missing required permissions.")

bot.run("bot token")