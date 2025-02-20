from __future__ import annotations
import asyncio

import datetime
import pandas as pd

from typing import List

import nextcord
from nextcord.ext.commands.bot import Bot
from nextcord.ext import tasks, commands

import pie.database.config
from pie import check, i18n, logger, utils

from .database import UserChannel, UserChannelConfig
from sqlalchemy.orm.attributes import flag_modified


_ = i18n.Translator("modules/boards").translate
bot_log = logger.Bot.logger()
guild_log = logger.Guild.logger()
config = pie.database.config.Config.get()

df_columns = {
    "guild_id": pd.Series(dtype="int64"),
    "guild_name": pd.Series(dtype="str"),
    "channel_id": pd.Series(dtype="int64"),
    "channel_name": pd.Series(dtype="str"),
    "user_id": pd.Series(dtype="int64"),
    "user_name": pd.Series(dtype="str"),
    "webhook_id": pd.Series(dtype="int64"),
    "last_msg_at": pd.Series(dtype="datetime64[ns, UTC]"),
}


class Messages(commands.Cog):
    """Get message count leaderboards."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.positive_cache: pd.DataFrame = pd.DataFrame(df_columns)
        self.negative_cache: pd.DataFrame = pd.DataFrame(df_columns)
        self.lock = asyncio.Lock()
        self.bulker.start()

    def cog_unload(self):
        self.bulker.cancel()

    # LOOP

    @tasks.loop(seconds=5.0)
    async def bulker(self) -> None:
        async with self.lock:
            self._save_cache()

    @bulker.before_loop
    async def before_bulker(self):
        """Wait until the bot is ready."""
        await self.bot.wait_until_ready()
        await self._sync()

    @bulker.after_loop
    async def after_bulker(self):
        if self.bulker.is_being_cancelled():
            async with self.lock:
                self._save_cache()

    def _save_cache(self, channel=None):
        pd.set_option("display.max_columns", None)
        if not self.positive_cache.empty:
            if channel is None:
                df = self.positive_cache
                self.positive_cache = pd.DataFrame(df_columns)
            else:
                df = pd.DataFrame(columns=self.positive_cache.columns)
                minus_rows = self.positive_cache.loc[
                    self.positive_cache.channel_id == channel.id, :
                ]
                df = df.append(minus_rows, ignore_index=True)
                self.positive_cache.drop(minus_rows.index, inplace=True)

            df["count"] = df.groupby(["guild_id", "channel_id", "user_id"])[
                "user_id"
            ].transform("size")
            sorted_df = df.sort_values(
                ["guild_id", "channel_id", "user_id", "last_msg_at"],
                ascending=False,
            )
            df = sorted_df.drop_duplicates(
                subset=["guild_id", "channel_id", "user_id"], keep="first"
            ).reset_index(drop=True)
            df["last_msg_at"] = pd.Series(
                df["last_msg_at"].dt.to_pydatetime(), dtype=object
            )
            items = df.to_dict("records")
            for item in items:
                UserChannel.bulk_increment(item)

        if not self.negative_cache.empty:
            if channel is None:
                df2 = self.negative_cache
                self.negative_cache = pd.DataFrame(df_columns)
            else:
                df2 = pd.DataFrame(columns=self.negative_cache.columns)
                minus_rows = self.negative_cache.loc[
                    self.negative_cache.channel_id == channel.id, :
                ]
                df2 = df2.append(minus_rows, ignore_index=True)
                self.negative_cache.drop(minus_rows.index, inplace=True)

            df2["count"] = df2.groupby(["guild_id", "channel_id", "user_id"])[
                "user_id"
            ].transform("size")
            sorted_df2 = df2.sort_values(
                ["guild_id", "channel_id", "user_id", "last_msg_at"],
                ascending=False,
            )
            df2 = sorted_df2.drop_duplicates(
                subset=["guild_id", "channel_id", "user_id"], keep="first"
            ).reset_index(drop=True)
            df2["last_msg_at"] = pd.Series(
                df2["last_msg_at"].dt.to_pydatetime(), dtype=object
            )
            items = df2.to_dict("records")
            for item in items:
                UserChannel.bulk_decrement(item)

    async def _sync(
        self, gld: nextcord.Guild = None, chnnl: nextcord.abc.GuildChannel = None
    ):
        """Synchronizes new messages that were sent during the bot was offline to the database."""
        total_count = 0
        async with self.lock:
            for guild in self.bot.guilds:
                if gld is not None and guild.id != gld.id:
                    continue
                channel_counts = UserChannel.get_channel_counts(
                    guild=guild, webhooks=True, include_filtered=True
                )
                guild_count = 0
                channels_and_threads = guild.channels + guild.threads
                for channel in channels_and_threads:
                    if chnnl is not None and channel.id != chnnl.id:
                        continue
                    elif isinstance(channel, (nextcord.TextChannel, nextcord.Thread)):
                        msgs = []
                        if channel_counts is None:
                            try:
                                msgs = await channel.history(
                                    limit=None, oldest_first=True
                                ).flatten()
                            except nextcord.errors.Forbidden:
                                await self.log(
                                    level="warning",
                                    message=f"Forbidden getting history for channel {channel} in guild {guild.name}",
                                )
                        else:
                            count = next(
                                (
                                    x
                                    for x in channel_counts
                                    if channel.id == x.channel_id
                                ),
                                None,
                            )
                            if count is not None:
                                try:
                                    msgs = await channel.history(
                                        limit=None,
                                        after=count.last_msg_at.replace(
                                            tzinfo=datetime.timezone.utc
                                        ),
                                        oldest_first=True,
                                    ).flatten()
                                except nextcord.errors.Forbidden:
                                    await self.log(
                                        level="warning",
                                        message="Forbidden getting history for channel {channel} in guild {guild}".format(
                                            channel=channel, guild=guild.name
                                        ),
                                    )
                            else:
                                try:
                                    msgs = await channel.history(
                                        limit=None, oldest_first=True
                                    ).flatten()
                                except nextcord.errors.Forbidden:
                                    await self.log(
                                        level="warning",
                                        message="Forbidden getting history for channel {channel} in guild {guild}".format(
                                            channel=channel, guild=guild.name
                                        ),
                                    )

                        if len(msgs) > 0:
                            guild_count += len(msgs)
                            if isinstance(channel, nextcord.Thread):
                                channel_name = f"{channel.parent.name}: 🧵{channel.name}"
                            else:
                                channel_name = channel.name

                            msgs_dicts = [
                                {
                                    "guild_id": x.guild.id,
                                    "guild_name": x.guild.name,
                                    "channel_id": x.channel.id,
                                    "channel_name": channel_name,
                                    "user_id": x.author.id,
                                    "user_name": x.author.display_name,
                                    "webhook_id": x.webhook_id,
                                    "last_msg_at": x.created_at,
                                }
                                for x in msgs
                                if not x.type
                                == nextcord.MessageType.thread_starter_message
                            ]
                            self.positive_cache = self.positive_cache.append(
                                msgs_dicts, ignore_index=True, sort=False
                            )
                            self._save_cache(channel=channel)
                            await guild_log.debug(
                                None,
                                guild,
                                f"Channel {channel.name} was synced. \n Synchronized {len(msgs)} new messages.",
                            )

                total_count += guild_count
                await guild_log.info(
                    None,
                    guild,
                    f"Message count database was successfully synced. \n Synchronized {guild_count} new messages.",
                )
            await bot_log.info(
                None,
                None,
                f"Message count database was successfully synced. \n Synchronized {total_count} new messages.",
            )

    # COMMANDS
    @commands.guild_only()
    @commands.check(check.acl)
    @commands.group(name="messages")
    async def messages_(self, ctx: commands.Context):
        """Messageboards configuration"""
        await utils.discord.send_help(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @messages_.group(name="config")
    async def messages_config_(self, ctx: commands.Context):
        """Messageboards configuration"""
        await utils.discord.send_help(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @messages_config_.command(name="get")
    async def messages_config_get(self, ctx: commands.Context):
        """Get Messageboards configuration for current guild."""
        config = UserChannelConfig.get(ctx.guild)
        if config is None:
            await ctx.reply(_(ctx, "Messageboard config was not found for this guild."))
            return
        embed = utils.discord.create_embed(
            author=ctx.message.author, title=_(ctx, "Messageboard config")
        )

        ignored_channels = []
        for id in config.ignored_channels:
            channel = ctx.guild.get_channel(id)
            if channel is not None:
                ignored_channels.append(channel.mention)
            else:
                ignored_channels.append(f"{id} (NotFound)")
        ignored_members = []
        for id in config.ignored_members:
            member = ctx.guild.get_member(id)
            if member is not None:
                ignored_members.append(member.display_name)
            else:
                ignored_members.append(f"{id} (NotFound)")
        if ignored_channels == []:
            ignored_channels = ["None"]
        if ignored_members == []:
            ignored_members = ["None"]

        embed.add_field(
            name=_(ctx, "Guild"),
            value=ctx.guild.name,
            inline=False,
        )
        embed.add_field(
            name=_(ctx, "Ignored channels"),
            value=", ".join(channel for channel in ignored_channels),
            inline=False,
        )
        embed.add_field(
            name=_(ctx, "Ignored members"),
            value=", ".join(member for member in ignored_members),
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.check(check.acl)
    @messages_config_.command(name="ignore")
    async def messages_config_ignore(
        self,
        ctx: commands.Context,
        channels: commands.Greedy[nextcord.TextChannel],
        members: commands.Greedy[nextcord.Member],
    ):
        """Set channels or members as ignored so they won't be shown in the boards.

        Args:
            channels: Channels to ignore
            members: Members to ignore
        """
        if channels == [] and members == []:
            await utils.discord.send_help(ctx)
            return
        UserChannelConfig.add(
            guild=ctx.guild, ignored_channels=channels, ignored_members=members
        )
        gtx = i18n.TranslationContext(ctx.guild.id, None)
        await guild_log.info(
            ctx.author,
            ctx.channel,
            _(
                gtx,
                "Messageboards config changed. Added: ignored_channels: {channels} ignored_members: {members}",
            ).format(channels=channels, members=members),
        )
        await self.messages_config_get(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @messages_config_.command(name="reset")
    async def messages_config_reset(self, ctx: commands.Context):
        """Reset the configuration. Deletes all ignored items."""
        config = UserChannelConfig.get(ctx.guild)
        if config is None:
            await ctx.reply(_(ctx, "Messageboard config was not found for this guild."))
            return
        if config is not None:
            config.ignored_channels = []
            config.ignored_members = []
            config.save()

        gtx = i18n.TranslationContext(ctx.guild.id, None)
        await guild_log.info(
            ctx.author,
            ctx.channel,
            _(
                gtx,
                "Messageboards config was reset.",
            ),
        )
        await self.messages_config_get(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @messages_config_.command(name="remove")
    async def messages_config_remove(
        self,
        ctx: commands.Context,
        channels: commands.Greedy[nextcord.TextChannel],
        members: commands.Greedy[nextcord.Member],
    ):
        """Remove members or channels from ignored list.

        Args:
            channels: Channels to stop ignoring
            members:  Members to stop ignoring
        """
        if members == [] and channels == []:
            await utils.discord.send_help(ctx)
            return

        config = UserChannelConfig.get(ctx.guild)
        if config is None:
            await ctx.reply(_(ctx, "Messageboard config was not found for this guild."))
            return
        if config is not None:
            if channels != []:
                channels_set = set([x.id for x in channels])
                config.ignored_channels = [
                    x for x in config.ignored_channels if x not in channels_set
                ]
                flag_modified(config, "ignored_channels")
            if members != []:
                members_set = set([x.id for x in members])
                config.ignored_members = [
                    x for x in config.ignored_members if x not in members_set
                ]
                flag_modified(config, "ignored_channels")
            config.save()

        gtx = i18n.TranslationContext(ctx.guild.id, None)
        await guild_log.info(
            ctx.author,
            ctx.channel,
            _(
                gtx,
                "Messageboards config changed. Removed: ignored_channels: {channels} ignored_members: {members}",
            ).format(channels=channels, members=members),
        )
        await self.messages_config_get(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @commands.group(name="channel")
    async def channel_(self, ctx: commands.Context):
        """Channel boards"""
        await utils.discord.send_help(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @channel_.command(name="board")
    async def channel_board(self, ctx: commands.Context):
        """Channel message leaderboard"""
        channel_counts = UserChannel.get_channel_counts(guild=ctx.guild)

        if not channel_counts:
            await ctx.reply(
                _(
                    ctx,
                    "I couldn't find any results in the database.",
                )
            )
            return

        embeds = self._create_channel_embeds(
            ctx=ctx,
            channel_counts=channel_counts,
            title=_(ctx, "Channel board"),
            description=_(ctx, "Total count of messages in channels"),
        )
        scrollable = utils.ScrollableEmbed(ctx, embeds)
        await scrollable.scroll()

    @commands.guild_only()
    @commands.check(check.acl)
    @channel_.command(name="info")
    async def channel_info(
        self, ctx: commands.Context, channel: nextcord.TextChannel = None
    ):
        """Channel information with user leaderboard for the channel

        Args:
            channel: Channel to view information of. Defaults to channel the message was sent from.
        """
        if channel is None:
            channel = ctx.channel

        user_counts = UserChannel.get_user_counts(guild=ctx.guild, channel=channel)
        ranked_channel = UserChannel.get_channel_ranked(
            guild=ctx.guild, channel=channel
        )
        channel_sum = UserChannel.get_channel_sum(guild=ctx.guild)
        last_userchannel = UserChannel.get_last(guild=ctx.guild, channel=channel)

        if not user_counts:
            await ctx.reply(
                _(
                    ctx,
                    "I couldn't find any results in the database.",
                )
            )
            return

        last_msg_at = last_userchannel.last_msg_at.replace(
            tzinfo=datetime.timezone.utc
        ).astimezone(tz=None)
        last_msg_at = last_msg_at.strftime("%d.%m.%Y %H:%M:%S")

        embed = utils.discord.create_embed(
            author=ctx.message.author, title=_(ctx, "Channel information")
        )
        embed.add_field(name=_(ctx, "Name"), value=str(channel.name), inline=True)
        embed.add_field(name=_(ctx, "ID"), value=str(channel.id), inline=True)
        embed.add_field(
            name=_(ctx, "Guild"), value=str(channel.guild.name), inline=True
        )
        try:
            embed.add_field(
                name=_(ctx, "Category"), value=str(channel.category.name), inline=True
            )
        except AttributeError:
            pass
        embed.add_field(
            name=_(ctx, "Last message"),
            value=f"{last_userchannel.user_name}\n{last_msg_at}",
            inline=True,
        )
        embed.add_field(
            name=_(ctx, "Total message count"),
            value=str(ranked_channel.total),
            inline=True,
        )
        embed.add_field(
            name=_(ctx, "Channel rank"),
            value=f"{ranked_channel.rank}/{channel_sum}",
            inline=True,
        )
        embeds = []
        embeds.append(embed)

        embeds += self._create_user_embeds(
            ctx=ctx,
            user_counts=user_counts,
            title=_(ctx, "Channel information"),
            description=_(ctx, "Total count of messages by users"),
        )
        scrollable = utils.ScrollableEmbed(ctx, embeds)
        await scrollable.scroll()

    @commands.guild_only()
    @commands.check(check.acl)
    @commands.group(name="user")
    async def user_(self, ctx: commands.Context):
        """User boards"""
        await utils.discord.send_help(ctx)

    @commands.guild_only()
    @commands.check(check.acl)
    @user_.command(name="board")
    async def user_board(self, ctx: commands.Context):
        """User message leaderboard"""
        user_counts = UserChannel.get_user_counts(guild=ctx.guild)

        if not user_counts:
            await ctx.reply(
                _(
                    ctx,
                    "I couldn't find any results in the database.",
                )
            )
            return

        embeds = self._create_user_embeds(
            ctx=ctx,
            user_counts=user_counts,
            title=_(ctx, "User board"),
            description=_(ctx, "Total count of messages by users"),
        )
        scrollable = utils.ScrollableEmbed(ctx, embeds)
        await scrollable.scroll()

    @commands.guild_only()
    @commands.check(check.acl)
    @user_.command(name="info")
    async def user_info(self, ctx: commands.Context, member: nextcord.Member = None):
        """User information with channel leaderboard for the user

        Args:
            member: User to view information of. Defaults to user that sent the command.
        """
        if member is None:
            member = ctx.message.author

        channel_counts = UserChannel.get_channel_counts(guild=ctx.guild, member=member)
        ranked_member = UserChannel.get_user_ranked(guild=ctx.guild, member=member)
        channel_sum = UserChannel.get_user_sum(guild=ctx.guild)
        last_userchannel = UserChannel.get_last(guild=ctx.guild, member=member)

        if not channel_counts:
            await ctx.reply(
                _(
                    ctx,
                    "I couldn't find any results in the database.",
                )
            )
            return

        last_channel = ctx.guild.get_channel(last_userchannel.channel_id)
        role_list = []
        for role in member.roles:
            if role.name != "@everyone":
                role_list.append(role.mention)
        role_list.reverse()

        last_msg_at = last_userchannel.last_msg_at.replace(
            tzinfo=datetime.timezone.utc
        ).astimezone(tz=None)
        last_msg_at = last_msg_at.strftime("%d.%m.%Y %H:%M:%S")

        joined_guild = member.joined_at.replace(
            tzinfo=datetime.timezone.utc
        ).astimezone(tz=None)
        joined_guild = joined_guild.strftime("%d.%m.%Y\n%H:%M:%S")

        joined_dc = member.created_at.replace(tzinfo=datetime.timezone.utc).astimezone(
            tz=None
        )
        joined_dc = joined_dc.strftime("%d.%m.%Y\n%H:%M:%S")

        if member.colour != nextcord.Colour.default():
            embed = utils.discord.create_embed(
                author=ctx.message.author,
                title=_(ctx, "User information"),
                color=member.colour,
            )
        else:
            embed = utils.discord.create_embed(
                author=ctx.message.author, title=_(ctx, "User information")
            )

        status = (
            "Do not diturb"
            if str(member.status) == "dnd"
            else str(member.status).title()
        )

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(
            name=_(ctx, "Name"),
            value=f"{member.display_name}\n({member.name}#{member.discriminator})",
            inline=True,
        )
        embed.add_field(name=_(ctx, "ID"), value=str(member.id), inline=True)
        embed.add_field(name=_(ctx, "Status"), value=status, inline=True)
        embed.add_field(
            name=_(ctx, "Joined discord"), value=str(joined_dc), inline=True
        )
        embed.add_field(
            name=_(ctx, "Joined guild"), value=str(joined_guild), inline=True
        )
        embed.add_field(name=_(ctx, "Guild"), value=str(member.guild.name), inline=True)
        embed.add_field(
            name=_(ctx, "Total message count"),
            value=str(ranked_member.total),
            inline=True,
        )
        embed.add_field(
            name=_(ctx, "User rank"),
            value=f"{ranked_member.rank}/{channel_sum}",
            inline=True,
        )
        embed.add_field(
            name=_(ctx, "Last message"),
            value=f"#{last_channel.name}\n{last_msg_at}",
            inline=True,
        )
        embed.add_field(
            name=_(ctx, "Roles"),
            value=", ".join(str(r) for r in role_list),
            inline=False,
        )
        embeds = []
        embeds.append(embed)

        embeds += self._create_channel_embeds(
            ctx=ctx,
            channel_counts=channel_counts,
            title=_(ctx, "Channel board"),
            description=_(ctx, "Total count of messages in channels"),
        )
        scrollable = utils.ScrollableEmbed(ctx, embeds)
        await scrollable.scroll()

    # HELPER FUNCTIONS

    @staticmethod
    def _create_channel_embeds(
        ctx: commands.Context,
        channel_counts: List[UserChannel],
        title: str,
        description: str,
        item_count: int = 10,
    ) -> List[nextcord.Embed]:
        """Creates the embed pages for channel boards

        Args:
            ctx: Message context
            channel_counts: Database items to create boards from
            title: Title of the embeds
            description: Description of the embeds
            item_count: How many items per page. Defaults to 10.

        Returns:
            List of embeds
        """
        pages: List[nextcord.Embed] = []
        chunks = [
            channel_counts[i : i + item_count]
            for i in range(0, len(channel_counts), item_count)
        ]

        for idx, chunk in enumerate(chunks, start=1):
            embed = utils.discord.create_embed(
                author=ctx.message.author,
                title=title,
                description=description,
            )
            lines = []
            rank_len = len(str(chunk[0].rank))
            count_len = len(str(chunk[0].total))
            for item in chunk:
                rank = f"{str(item.rank).rjust(rank_len)}"
                count = f"{str(item.total).rjust(count_len)}"
                name = f"#{item.channel_name}"
                lines.append(f"`{rank}` ... `{count}` {name}")

            embed.add_field(
                name=_(ctx, "Top {offset}").format(offset=(idx * item_count)),
                value="\n".join(lines),
                inline=False,
            )
            pages.append(embed)

        return pages

    @staticmethod
    def _create_user_embeds(
        ctx: commands.Context,
        user_counts: List[UserChannel],
        title: str,
        description: str,
        item_count: int = 10,
    ) -> List[nextcord.Embed]:
        """Creates the embed pages for user boards

        Args:
            ctx: Message context
            user_counts: Database items to create boards from
            title: Title of the embeds
            description: Description of the embeds
            item_count: How many items per page. Defaults to 10.

        Returns:
            List of embeds
        """
        pages: List[nextcord.Embed] = []
        chunks = [
            user_counts[i : i + item_count]
            for i in range(0, len(user_counts), item_count)
        ]

        author_position = -1
        author_item = None
        for idx, chunk in enumerate(chunks, start=1):
            embed = utils.discord.create_embed(
                author=ctx.message.author,
                title=title,
                description=description,
            )
            lines = []
            rank_len = len(str(chunk[0].rank))
            count_len = len(str(chunk[0].total))

            for item in chunk:
                rank = f"{str(item.rank).rjust(rank_len)}"
                count = f"{str(item.total).rjust(count_len)}"
                if item.user_id == ctx.message.author.id:
                    name = f"**{item.user_name}**"
                    author_position = idx
                    author_item = item
                else:
                    name = f"{item.user_name}"
                lines.append(f"`{rank}` ... `{count}` {name}")

            embed.add_field(
                name=_(ctx, "Top {offset}").format(offset=(idx * item_count)),
                value="\n".join(lines),
                inline=False,
            )

        pages.append(embed)

        if author_position != -1:
            for idx, embed in enumerate(pages, start=1):
                if author_position != idx:
                    embed.add_field(
                        name=_(ctx, "Your position"),
                        value=f"`{author_item.rank}` ... `{author_item.total}` {author_item.user_name}",
                        inline=False,
                    )

        return pages

    # LISTENERS

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message):
        """Adds message to positive_cache if it came from a guild channel"""
        if isinstance(message.channel, nextcord.TextChannel):
            self.positive_cache = self.positive_cache.append(
                {
                    "guild_id": message.guild.id,
                    "guild_name": message.guild.name,
                    "channel_id": message.channel.id,
                    "channel_name": message.channel.name,
                    "user_id": message.author.id,
                    "user_name": message.author.display_name,
                    "webhook_id": message.webhook_id,
                    "last_msg_at": message.created_at,
                },
                ignore_index=True,
            )
        if (
            isinstance(message.channel, nextcord.Thread)
            and not message.type == nextcord.MessageType.thread_starter_message
        ):
            self.positive_cache = self.positive_cache.append(
                {
                    "guild_id": message.guild.id,
                    "guild_name": message.guild.name,
                    "channel_id": message.channel.id,
                    "channel_name": f"{message.channel.parent.name}: 🧵{message.channel.name}",
                    "user_id": message.author.id,
                    "user_name": message.author.display_name,
                    "webhook_id": message.webhook_id,
                    "last_msg_at": message.created_at,
                },
                ignore_index=True,
            )

    @commands.Cog.listener()
    async def on_message_delete(self, message: nextcord.Message):
        """Adds message to negative_cache if it was deleted in a guild channel."""
        if isinstance(message.channel, nextcord.TextChannel):
            self.negative_cache = self.positive_cache.append(
                {
                    "guild_id": message.guild.id,
                    "guild_name": message.guild.name,
                    "channel_id": message.channel.id,
                    "channel_name": message.channel.name,
                    "user_id": message.author.id,
                    "user_name": message.author.display_name,
                    "webhook_id": message.webhook_id,
                    "last_msg_at": message.created_at,
                },
                ignore_index=True,
            )
        if (
            isinstance(message.channel, nextcord.Thread)
            and not message.type == nextcord.MessageType.thread_starter_message
        ):
            self.positive_cache = self.positive_cache.append(
                {
                    "guild_id": message.guild.id,
                    "guild_name": message.guild.name,
                    "channel_id": message.channel.id,
                    "channel_name": f"{message.channel.parent.name}: 🧵{message.channel.name}",
                    "user_id": message.author.id,
                    "user_name": message.author.display_name,
                    "webhook_id": message.webhook_id,
                    "last_msg_at": message.created_at,
                },
                ignore_index=True,
            )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: List[nextcord.Message]):
        """Adds messages to negative_cache if they were deleted in a guild channel."""
        for message in messages:
            if isinstance(message.channel, nextcord.TextChannel):
                self.negative_cache = self.positive_cache.append(
                    {
                        "guild_id": message.guild.id,
                        "guild_name": message.guild.name,
                        "channel_id": message.channel.id,
                        "channel_name": message.channel.name,
                        "user_id": message.author.id,
                        "user_name": message.author.display_name,
                        "webhook_id": message.webhook_id,
                        "last_msg_at": message.created_at,
                    },
                    ignore_index=True,
                )
            if (
                isinstance(message.channel, nextcord.Thread)
                and not message.type == nextcord.MessageType.thread_starter_message
            ):
                self.positive_cache = self.positive_cache.append(
                    {
                        "guild_id": message.guild.id,
                        "guild_name": message.guild.name,
                        "channel_id": message.channel.id,
                        "channel_name": f"{message.channel.parent.name}: 🧵{message.channel.name}",
                        "user_id": message.author.id,
                        "user_name": message.author.display_name,
                        "webhook_id": message.webhook_id,
                        "last_msg_at": message.created_at,
                    },
                    ignore_index=True,
                )

    @commands.Cog.listener()
    async def on_ready(self):
        await self._sync()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self._sync(gld=guild)


def setup(bot) -> None:
    bot.add_cog(Messages(bot))
