import os
import re
import random
import asyncio
from enum import Enum
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# ما نستخدم بريفكس، بنعتمد على on_message
bot = commands.Bot(command_prefix=commands.when_mentioned_or(""), intents=intents)
bot.remove_command("help")


# --------------------------- إعدادات يوتيوب/صوت ---------------------------
YTDL_OPTS = {
    "format": "bestaudio[ext=webm][acodec=opus]/bestaudio",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# سنحقن الفوليوم والـ seek في options كل مرة ننشئ السورس


# --------------------------- كائنات المساعدة ---------------------------
class LoopMode(Enum):
    OFF = 0
    ONE = 1
    ALL = 2


class Track:
    def __init__(self, info: Dict[str, Any], requested_by: discord.Member):
        self.info = info  # معلومات yt-dlp
        self.title = info.get("title", "بدون عنوان")
        self.webpage_url = info.get("webpage_url") or info.get("url")
        self.stream_url = info.get("url")
        self.duration = info.get("duration")  # بالثواني أو None
        self.requested_by = requested_by

    def __str__(self):
        return self.title


def is_url(q: str) -> bool:
    return q.startswith("http://") or q.startswith("https://")


# --------------------------- مشغل لكل سيرفر ---------------------------
class GuildPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.vc: Optional[discord.VoiceClient] = None

        self.queue: List[Track] = []
        self.history: List[Track] = []

        self.current: Optional[Track] = None
        self.loop_mode: LoopMode = LoopMode.OFF
        self.autoplay: bool = False
        self.volume: float = 1.0  # 100%

        self._start_mono_time: Optional[float] = None  # لحساب المكان الحالي
        self._start_seek_offset: float = 0.0  # ثواني

        # رسالة البانل وعرض الأزرار
        self.panel_message: Optional[discord.Message] = None
        self.panel_view: Optional["ControlView"] = None

        # قفل لتسلسل التشغيل
        self.lock = asyncio.Lock()

    # -------- أدوات السورس/الصوت --------
    def _build_ffmpeg_options(self, seek_seconds: float = 0.0) -> str:
        afilters = []
        # فوليوم
        afilters.append(f"volume={self.volume}")
        # استريو افتراضي — لا حاجة لفلاتر ثانية
        filters_str = ",".join(afilters)
        opts = f"-vn -af {filters_str}"
        if seek_seconds > 0:
            # سنستخدم before_options للseek المبكر
            pass
        return opts

    def _make_source(self, url: str, seek_seconds: float = 0.0) -> discord.FFmpegPCMAudio:
        before = FFMPEG_BEFORE
        if seek_seconds > 0:
            # -ss في before_options يحسن سرعة الـ seek للستريم
            before = f"{FFMPEG_BEFORE} -ss {int(seek_seconds)}"
        options = self._build_ffmpeg_options(seek_seconds)
        return discord.FFmpegPCMAudio(url, before_options=before, options=options)

    def _elapsed(self) -> float:
        if self._start_mono_time is None:
            return 0.0
        return (asyncio.get_running_loop().time() - self._start_mono_time)

    # -------- تشغيل/تنقل --------
    async def enqueue_and_maybe_play(self, track: Track, text_channel: discord.TextChannel):
        self.queue.append(track)
        # إذا لا يوجد شيء يشغل الآن، ابدأ فورًا
        if not self.is_playing():
            await self._play_next(text_channel)
        else:
            await self.update_panel(text_channel)

    async def _play_next(self, text_channel: discord.TextChannel):
        async with self.lock:
            next_track: Optional[Track] = None

            if self.loop_mode == LoopMode.ONE and self.current:
                next_track = self.current
            else:
                # إذا انتهت الحالية، أرسلها للهستوري
                if self.current and (not self.history or self.history[-1] != self.current):
                    self.history.append(self.current)

                if self.queue:
                    # Loop ALL: بعد سحب أول عنصر، نضيفه نهاية الطابور لاحقًا
                    next_track = self.queue.pop(0)
                    if self.loop_mode == LoopMode.ALL:
                        self.queue.append(next_track)
                elif self.autoplay and self.current:
                    # أوتو بلاي بسيط: ابحث عن أغنية مشابهة بالعنوان
                    query = f"{self.current.title}"
                    try:
                        info = await fetch_yt_info(query)
                        next_track = Track(info, self.current.requested_by)
                    except Exception:
                        next_track = None

            if next_track is None:
                # لا يوجد شيء -> نظف
                self.current = None
                await self.stop_and_cleanup_panel()
                return

            self.current = next_track
            self._start_seek_offset = 0.0
            self._start_mono_time = asyncio.get_running_loop().time()

            # تأكد من وجود اتصال صوتي
            if not self.vc or not self.vc.is_connected():
                # سيتم حضور/الاتصال من الخارج قبل نداء هذه الدالة عادةً
                return

            source = self._make_source(self.current.stream_url, seek_seconds=0.0)

            def _after_playing(error):
                # هذا الكولباك يعمل في ثريد مختلف، لازم نعيده لللوب
                fut = asyncio.run_coroutine_threadsafe(self._on_track_end(text_channel, error), bot.loop)
                try:
                    fut.result()
                except Exception:
                    pass

            self.vc.play(source, after=_after_playing)
            await self.show_or_update_panel(text_channel)

    async def _on_track_end(self, text_channel: discord.TextChannel, error: Optional[Exception]):
        # عند انتهاء المقطع
        self._start_mono_time = None
        if error:
            try:
                await text_channel.send(f"حدث خطأ أثناء التشغيل: `{error}`")
            except Exception:
                pass
        # إذا لا يوجد شيء يشغل بعده -> سيحذف البانل داخل _play_next
        await self._play_next(text_channel)

    def is_playing(self) -> bool:
        return self.vc and self.vc.is_connected() and self.vc.is_playing()

    async def ensure_connected(self, member: discord.Member):
        # اتصل بنفس روم العضو
        if member.voice and member.voice.channel:
            if not self.vc or not self.vc.is_connected():
                self.vc = await member.voice.channel.connect()
            elif self.vc.channel != member.voice.channel:
                # شرطك: فقط من نفس الروم
                raise RuntimeError("يلزم تكون مع البوت في نفس الروم الصوتي.")
        else:
            raise RuntimeError("أدخل روم صوتي أولاً.")

    async def pause_resume(self):
        if self.vc:
            if self.vc.is_paused():
                self.vc.resume()
            elif self.vc.is_playing():
                self.vc.pause()

    async def stop_and_cleanup_panel(self):
        # وقف ومسح البانل إذا مافي طابور
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        if not self.queue and not self.current:
            # امسح البانل
            await self.delete_panel()

    async def skip(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()

    async def previous(self, text_channel: discord.TextChannel):
        if not self.history:
            return
        prev_track = self.history.pop()
        if self.current:
            # رجّع الحالية لأول الطابور
            self.queue.insert(0, self.current)
        self.current = prev_track
        self._start_seek_offset = 0.0
        self._start_mono_time = asyncio.get_running_loop().time()

        # شغّلها
        source = self._make_source(self.current.stream_url, 0.0)

        def _after(error):
            fut = asyncio.run_coroutine_threadsafe(self._on_track_end(text_channel, error), bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        self.vc.play(source, after=_after)
        await self.show_or_update_panel(text_channel)

    async def seek(self, seconds: int, text_channel: discord.TextChannel):
        # تقديم/ترجيع
        if not self.current or not self.vc:
            return
        elapsed = self._start_seek_offset + self._elapsed()
        new_pos = max(0, int(elapsed) + seconds)
        self._start_seek_offset = float(new_pos)
        self._start_mono_time = asyncio.get_running_loop().time()
        src = self._make_source(self.current.stream_url, seek_seconds=new_pos)

        def _after(error):
            fut = asyncio.run_coroutine_threadsafe(self._on_track_end(text_channel, error), bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        self.vc.play(src, after=_after)
        await self.update_panel(text_channel)

    async def set_volume(self, delta: float, text_channel: discord.TextChannel):
        # delta +0.1/-0.1
        self.volume = float(min(2.0, max(0.0, self.volume + delta)))
        # أعد إنشاء السورس للحجم الجديد مع المحافظة على الموضع
        if self.current and self.vc:
            pos = self._start_seek_offset + self._elapsed()
            await self.seek(0, text_channel)  # سيُعاد بناؤه بنفس الموضع عبر _make_source
            self._start_seek_offset = pos
            await self.seek(0, text_channel)

    # -------- البانل --------
    def build_embed(self) -> discord.Embed:
        e = discord.Embed(color=0x2B6CB0, title="🎵 الأغنية الحالية")
        if self.current:
            e.description = f"**{self.current.title}**"
            if self.current.webpage_url:
                e.url = self.current.webpage_url
            
            # إضافة صورة المقطع
            if self.current.info.get("thumbnail"):
                e.set_thumbnail(url=self.current.info["thumbnail"])
            
            # إضافة معلومات إضافية
            if self.current.duration:
                duration_str = f"{self.current.duration // 60}:{self.current.duration % 60:02d}"
                e.add_field(name="⏱️ المدة", value=duration_str, inline=True)
            
            if self.current.requested_by:
                e.add_field(name="👤 طلب بواسطة", value=self.current.requested_by.display_name, inline=True)
                
            # إضافة خط وكت المقطع
            if self.current.info.get("uploader"):
                e.add_field(name="📺 القناة", value=self.current.info["uploader"], inline=True)
                
            # إضافة عدد المشاهدات إذا كان متوفر
            if self.current.info.get("view_count"):
                view_count = self.current.info["view_count"]
                if view_count > 1000000:
                    view_str = f"{view_count/1000000:.1f}M"
                elif view_count > 1000:
                    view_str = f"{view_count/1000:.1f}K"
                else:
                    view_str = str(view_count)
                e.add_field(name="👁️ المشاهدات", value=view_str, inline=True)
        else:
            e.description = "لا يوجد تشغيل حالياً."
            
        vol_pct = int(self.volume * 100)
        q_len = len(self.queue)
        loop_map = {LoopMode.OFF: "إيقاف", LoopMode.ONE: "واحد", LoopMode.ALL: "الكل"}
        e.add_field(name="🔊 الصوت", value=f"[{vol_pct}%]", inline=True)
        e.add_field(name="🎵 الطابور", value=f"[{q_len} ♫]", inline=True)
        e.add_field(name="🔁 التكرار", value=f"[{loop_map[self.loop_mode]}]", inline=True)
        return e

    async def show_or_update_panel(self, text_channel: discord.TextChannel):
        if self.panel_message and self.panel_view:
            await self.update_panel(text_channel)
            return
        self.panel_view = ControlView(self, text_channel)
        self.panel_message = await text_channel.send(embed=self.build_embed(), view=self.panel_view)

    async def update_panel(self, text_channel: discord.TextChannel):
        if self.panel_message:
            try:
                await self.panel_message.edit(embed=self.build_embed(), view=self.panel_view)
            except discord.NotFound:
                # لو انمسحت الرسالة بالغلط، أعد إنشاءها
                self.panel_view = ControlView(self, text_channel)
                self.panel_message = await text_channel.send(embed=self.build_embed(), view=self.panel_view)

    async def delete_panel(self):
        if self.panel_message:
            try:
                await self.panel_message.delete()
            except Exception:
                pass
        self.panel_message = None
        self.panel_view = None


# --------------------------- View (الأزرار) ---------------------------
class ControlView(discord.ui.View):
    def __init__(self, player: GuildPlayer, text_channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.player = player
        self.text_channel = text_channel

    # الصف 1: خفض الصوت | السابق | إيقاف | تخطي | رفع الصوت
    @discord.ui.button(label="خفض الصوت", style=discord.ButtonStyle.secondary, emoji="🔉", row=0)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.set_volume(-0.1, self.text_channel)
        await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="الأغنية السابقة", style=discord.ButtonStyle.secondary, emoji="⏮️", row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.previous(self.text_channel)

    @discord.ui.button(label="إيقاف التشغيل", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        self.player.queue.clear()
        self.player.current = None
        await self.player.stop_and_cleanup_panel()

    @discord.ui.button(label="تخطي الأغنية", style=discord.ButtonStyle.primary, emoji="⏭️", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.skip()

    @discord.ui.button(label="رفع الصوت", style=discord.ButtonStyle.secondary, emoji="🔊", row=0)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.set_volume(+0.1, self.text_channel)
        await self.player.update_panel(self.text_channel)

    # الصف 2: ترجيع 10s | أوتو بلاي | إيقاف/استئناف | تكرار | تقديم 10s
    @discord.ui.button(label="ترجيع 10 ثانية", style=discord.ButtonStyle.secondary, emoji="⏪", row=1)
    async def backward_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.seek(-10, self.text_channel)

    @discord.ui.button(label="تشغيل تلقائي", style=discord.ButtonStyle.secondary, emoji="🎼", row=1)
    async def autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        self.player.autoplay = not self.player.autoplay
        await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="إيقاف/استئناف", style=discord.ButtonStyle.primary, emoji="⏯️", row=1)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.pause_resume()
        await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="وضع التكرار", style=discord.ButtonStyle.secondary, emoji="🔁", row=1)
    async def repeat_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        # OFF -> ONE -> ALL -> OFF
        order = [LoopMode.OFF, LoopMode.ONE, LoopMode.ALL]
        idx = order.index(self.player.loop_mode)
        self.player.loop_mode = order[(idx + 1) % len(order)]
        await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="تقديم 10 ثانية", style=discord.ButtonStyle.secondary, emoji="⏩", row=1)
    async def forward_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.seek(+10, self.text_channel)

    # الصف 3: عرض الطابور | أعد السابقة | تشغيل (نفس زر الإيقاف/استئناف) | اختيار أغنية | خلط الطابور
    @discord.ui.button(label="عرض الطابور", style=discord.ButtonStyle.secondary, emoji="🧾", row=2)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not self.player.queue:
            await interaction.followup.send("الطابور فارغ.", ephemeral=True)
            return
        text = "\n".join([f"{i+1}. {t.title}" for i, t in enumerate(self.player.queue[:20])])
        await interaction.followup.send(f"**الطابور:**\n{text}", ephemeral=True)

    @discord.ui.button(label="إضافة السابقة", style=discord.ButtonStyle.secondary, emoji="🔂", row=2)
    async def add_previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        if self.player.history:
            self.player.queue.insert(0, self.player.history[-1])
            await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="تشغيل الموسيقى", style=discord.ButtonStyle.success, emoji="▶️", row=2)
    async def play_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        await self.player.pause_resume()
        await self.player.update_panel(self.text_channel)

    @discord.ui.button(label="القفز لأغنية", style=discord.ButtonStyle.secondary, emoji="🎯", row=2)
    async def jump_to_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        # فتح قائمة اختيار من الطابور
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not self.player.queue:
            await interaction.followup.send("الطابور فارغ.", ephemeral=True)
            return

        # صنع Select ديناميكي
        options = []
        for i, t in enumerate(self.player.queue[:25]):
            options.append(discord.SelectOption(label=f"{i+1}. {t.title[:90]}", value=str(i)))

        select = discord.ui.Select(placeholder="اختر أغنية للقفز إليها", options=options)

        async def select_callback(interact: discord.Interaction):
            idx = int(select.values[0])
            # انقل المختارة لبداية الطابور و Skip الحالي
            chosen = self.player.queue.pop(idx)
            self.player.queue.insert(0, chosen)
            await interact.response.send_message(f"تم القفز إلى: **{chosen.title}**", ephemeral=True)
            await self.player.skip()

        select.callback = select_callback
        view = discord.ui.View(timeout=30)
        view.add_item(select)
        await interaction.followup.send("اختر أغنية:", view=view, ephemeral=True)

    @discord.ui.button(label="خلط الطابور", style=discord.ButtonStyle.secondary, emoji="🔀", row=2)
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=False)
        random.shuffle(self.player.queue)
        await self.player.update_panel(self.text_channel)


# --------------------------- إدارة اللاعبين لكل سيرفر ---------------------------
players: Dict[int, GuildPlayer] = {}


def get_player(guild: discord.Guild) -> GuildPlayer:
    if guild.id not in players:
        players[guild.id] = GuildPlayer(guild)
    return players[guild.id]


# --------------------------- يوتيوب DL (async wrapper) ---------------------------
async def fetch_yt_info(query: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    def _dl():
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            if is_url(query):
                return ydl.extract_info(query, download=False)
            else:
                return ydl.extract_info(f"ytsearch1:{query}", download=False)

    info = await loop.run_in_executor(None, _dl)
    if "entries" in info:
        info = info["entries"][0]
    return info


# --------------------------- الأحداث/الأوامر النصية ---------------------------
PLAY_PATTERNS = [
    r"^\s*شغل\s+(.+)$",
    r"^\s*تشغيل\s+(.+)$",
]

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # تجاهل البوتات
    if message.author.bot or not message.guild:
        return

    content = message.content.strip()

    query = None
    for pat in PLAY_PATTERNS:
        m = re.match(pat, content, flags=re.IGNORECASE)
        if m:
            query = m.group(1).strip()
            break

    if query is None:
        return  # مش أمر تشغيل

    # حاول حذف رسالة المستخدم
    try:
        await message.delete()
    except Exception:
        pass

    text_channel = message.channel
    member = message.author
    player = get_player(message.guild)

    # تأكد من الاتصال/التواجد بنفس الروم
    try:
        await player.ensure_connected(member)
    except Exception as e:
        warn = await text_channel.send(str(e))
        await asyncio.sleep(4)
        try:
            await warn.delete()
        except Exception:
            pass
        return

    # إذا البوت متصل في روم آخر (حالة نادرة)
    if player.vc and player.vc.channel != (member.voice.channel if member.voice else None):
        msg = await text_channel.send("يلزم تكون مع البوت في نفس الروم الصوتي.")
        await asyncio.sleep(4)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # جيب معلومات المقطع
    try:
        info = await fetch_yt_info(query)
    except Exception as e:
        await text_channel.send(f"تعذر جلب المقطع: `{e}`")
        return

    track = Track(info, member)
    await player.enqueue_and_maybe_play(track, text_channel)


# تنظيف عند خروج/سحب البوت من الروم الصوتي: احذف البانل
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.id != bot.user.id:
        return
    guild = member.guild
    player = players.get(guild.id)
    if not player:
        return
    # إذا انفصل البوت كليًا
    if before.channel and after.channel is None:
        await player.delete_panel()


# --------------------------- تشغيل ---------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN غير موجود! ضع التوكن في ملف .env")
    bot.run("MTQ2MDAwNzIyNTQwMDc1NDI5OQ.GAs-Dg.2asN3nMhfeZ83ErRko9blx6-gJ99sNCUDIio3M")
