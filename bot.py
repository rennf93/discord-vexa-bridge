"""Discord -> Vexa transcript adapter.

Joins a voice channel, receives PER-USER audio (Discord gives each speaker as a
separate stream — native, perfect diarization, no clustering), transcribes each
utterance on the Vexa CPU Whisper service that's already running, and writes rows
into the same Postgres tables the Vexa bots use:
    meetings (platform='discord') -> meeting_sessions -> transcriptions(speaker=...)
The gateway merges Postgres + Redis on read, so Discord calls appear in the
dashboard / MCP / Claude exactly like Meet and Zoom.

Slash commands: /join (joins your current voice channel), /leave.
Utterances are delimited by silence: Discord only sends voice packets while a user
is transmitting, so a gap with no packets ends an utterance.
"""

import asyncio
import audioop  # stdlib on py3.11 (removed in 3.13 — keep the base image at 3.11)
import io
import os
import threading
import time
import uuid
import wave
from collections import defaultdict

import aiohttp
import asyncpg
import discord
from discord.sinks import Sink

TOKEN          = os.environ["DISCORD_TOKEN"]
DATABASE_URL   = os.environ["DATABASE_URL"]           # postgresql://postgres:pw@postgres:5432/vexa
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "http://transcription-worker:8000/v1/audio/transcriptions")
VEXA_USER_ID   = int(os.environ["VEXA_USER_ID"])      # the user id from your create-user curl
LANGUAGE       = os.environ.get("LANGUAGE", "").strip()        # "" = autodetect
SILENCE_MS     = int(os.environ.get("SILENCE_MS", "800"))      # gap that ends an utterance
MIN_MS         = int(os.environ.get("MIN_UTTERANCE_MS", "400"))# drop blips shorter than this

# Pycord decodes Opus to 48 kHz, 16-bit, stereo PCM in the sink.
SR, CH, SW = 48_000, 2, 2
BYTES_PER_SEC = SR * CH * SW

intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Bot(intents=intents)
pool: asyncpg.Pool | None = None


class LiveSink(Sink):
    """Receives decoded PCM per user on pycord's voice-recv thread."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.buf: dict[int, bytearray] = defaultdict(bytearray)
        self.last: dict[int, float] = {}
        self.start: dict[int, float] = {}

    def write(self, data, user):  # called from a non-async audio thread
        now = time.monotonic()
        with self.lock:
            if not self.buf[user]:
                self.start[user] = now
            self.buf[user].extend(data)
            self.last[user] = now

    def _pop(self, only_silent: bool, silence_s: float = 0.0):
        now = time.monotonic()
        out = []
        with self.lock:
            for user in list(self.buf.keys()):
                b = self.buf[user]
                if not b:
                    continue
                if only_silent and (now - self.last.get(user, now)) < silence_s:
                    continue
                out.append((user, bytes(b), self.start.get(user, now), self.last.get(user, now)))
                self.buf[user] = bytearray()
        return out

    def drain_ready(self, silence_s):  # users who've gone quiet
        return self._pop(only_silent=True, silence_s=silence_s)

    def drain_all(self):               # everything (on stop)
        return self._pop(only_silent=False)


class Meeting:
    def __init__(self, guild, channel, meeting_id, session_uid, t0):
        self.guild = guild
        self.channel = channel
        self.id = meeting_id
        self.session_uid = session_uid
        self.t0 = t0  # monotonic clock at session start; transcript times are relative to this
        self.names: dict[int, str] = {m.id: m.display_name for m in channel.members}

    async def name_for(self, uid: int) -> str:
        if uid in self.names:
            return self.names[uid]
        member = self.guild.get_member(uid)
        if member is None:
            try:
                member = await self.guild.fetch_member(uid)
            except Exception:
                member = None
        name = member.display_name if member else str(uid)
        self.names[uid] = name
        return name


# guild_id -> (voice_client, sink, meeting, flusher_task)
active: dict[int, tuple[discord.VoiceClient, LiveSink, Meeting, asyncio.Task]] = {}


def to_mono_wav(pcm_stereo: bytes) -> bytes:
    """Downmix 48 kHz stereo PCM to mono and wrap as WAV. The worker resamples to 16 kHz."""
    mono = audioop.tomono(pcm_stereo, SW, 0.5, 0.5)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SW)
        w.setframerate(SR)
        w.writeframes(mono)
    return buf.getvalue()


async def transcribe(wav: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("file", wav, filename="utterance.wav", content_type="audio/wav")
    form.add_field("model", "whisper-1")  # OpenAI-compatible field; worker uses its own MODEL_SIZE
    if LANGUAGE:
        form.add_field("language", LANGUAGE)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(TRANSCRIBE_URL, data=form,
                              timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    print(f"transcribe HTTP {r.status}", flush=True)
                    return ""
                return ((await r.json()).get("text") or "").strip()
    except Exception as e:
        print(f"transcribe error: {e}", flush=True)
        return ""


async def store(meeting: Meeting, uid: int, pcm: bytes, t0: float, t1: float):
    if (len(pcm) / BYTES_PER_SEC) * 1000 < MIN_MS:
        return
    text = await transcribe(to_mono_wav(pcm))
    if not text:
        return
    speaker = await meeting.name_for(uid)
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO transcriptions"
            " (meeting_id, start_time, end_time, text, speaker, language, session_uid, segment_id, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())",
            meeting.id, t0 - meeting.t0, t1 - meeting.t0, text, speaker,
            (LANGUAGE or None), meeting.session_uid, str(uuid.uuid4()),
        )
    print(f"[{speaker}] {text}", flush=True)


async def flusher(guild_id: int):
    try:
        while True:
            await asyncio.sleep(0.2)
            entry = active.get(guild_id)
            if not entry:
                return
            _, sink, meeting, _ = entry
            for uid, pcm, t0, t1 in sink.drain_ready(SILENCE_MS / 1000):
                asyncio.create_task(store(meeting, uid, pcm, t0, t1))
    except asyncio.CancelledError:
        pass


@bot.slash_command(description="Join your voice channel and start transcribing")
async def join(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        await ctx.respond("Join a voice channel first.", ephemeral=True)
        return
    if ctx.guild.id in active:
        await ctx.respond("Already recording in this server.", ephemeral=True)
        return

    channel = ctx.author.voice.channel
    await ctx.defer()
    vc = await channel.connect()
    t0 = time.monotonic()
    session_uid = str(uuid.uuid4())

    async with pool.acquire() as c:
        meeting_id = await c.fetchval(
            "INSERT INTO meetings"
            " (user_id, platform, platform_specific_id, status, start_time, data, created_at, updated_at)"
            " VALUES ($1,'discord',$2,'active', now(), '{}'::jsonb, now(), now()) RETURNING id",
            VEXA_USER_ID, str(channel.id),
        )
        await c.execute(
            "INSERT INTO meeting_sessions (meeting_id, session_uid, session_start_time)"
            " VALUES ($1,$2, now())",
            meeting_id, session_uid,
        )

    meeting = Meeting(ctx.guild, channel, meeting_id, session_uid, t0)
    sink = LiveSink()
    vc.start_recording(sink, lambda s, *a: None)  # live capture via sink.write; callback unused
    task = asyncio.create_task(flusher(ctx.guild.id))
    active[ctx.guild.id] = (vc, sink, meeting, task)
    await ctx.respond(f"Recording **{channel.name}** → meeting `{meeting_id}`. Speakers tagged by name.")


@bot.slash_command(description="Stop transcribing and leave the channel")
async def leave(ctx: discord.ApplicationContext):
    entry = active.pop(ctx.guild.id, None)
    if not entry:
        await ctx.respond("Not recording here.", ephemeral=True)
        return
    vc, sink, meeting, task = entry
    task.cancel()
    await ctx.defer()
    try:
        vc.stop_recording()
    except Exception:
        pass
    for uid, pcm, t0, t1 in sink.drain_all():
        await store(meeting, uid, pcm, t0, t1)
    await vc.disconnect()
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE meetings SET status='completed', end_time=now(), updated_at=now() WHERE id=$1",
            meeting.id,
        )
    await ctx.respond(f"Stopped. meeting `{meeting.id}` saved.")


@bot.event
async def on_ready():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    if not discord.opus.is_loaded():
        for lib in ("libopus.so.0", "libopus.so", "opus"):
            try:
                discord.opus.load_opus(lib)
                break
            except Exception:
                continue
    print(f"discord-adapter ready as {bot.user}", flush=True)


bot.run(TOKEN)