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
import json
import logging
import os
import threading
import time
import uuid
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg
import discord

from dave_voice.discord_protocol import DAVEVoiceProtocol
from dave_voice.voice_client import DAVEVoiceClient

# libdave (via dave.py) logs per-frame decrypt failures through the "dave" logger.
# A chunk of frames legitimately fail at stream start / silence / epoch edges; the
# successful frames are plenty for accurate transcripts, so silence the noise.
logging.getLogger("dave").setLevel(logging.CRITICAL)

TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql://postgres:pw@postgres:5432/vexa
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "http://transcription-worker:8000/v1/audio/transcriptions")
VEXA_USER_ID = int(os.environ["VEXA_USER_ID"])  # the user id from your create-user curl
LANGUAGE = os.environ.get("LANGUAGE", "").strip()  # "" = autodetect
SILENCE_MS = int(os.environ.get("SILENCE_MS", "800"))  # gap that ends an utterance
MIN_MS = int(os.environ.get("MIN_UTTERANCE_MS", "400"))  # drop blips shorter than this

# The transcription worker is single-concurrency and CPU-slow on the NAS (~minutes per
# short clip), so a 503 or a bot restart used to mean lost audio. Segments now spill to
# disk first and a single background drainer replays them; the queue survives crashes.
TRANSCRIBE_TIMEOUT = float(os.environ.get("TRANSCRIBE_TIMEOUT", "600"))  # the worker holds a connection for minutes
PENDING_DIR = Path(os.environ.get("PENDING_DIR", "/data/pending"))
DRAIN_IDLE_SLEEP = float(os.environ.get("DRAIN_IDLE_SLEEP", "5"))  # scan interval when queue is empty
DRAIN_BACKOFF = float(os.environ.get("DRAIN_BACKOFF", "10"))  # pause after a failed transcribe attempt

# Pycord decodes Opus to 48 kHz, 16-bit, stereo PCM in the sink.
SR, CH, SW = 48_000, 2, 2
BYTES_PER_SEC = SR * CH * SW
# The transcription worker feeds WAV samples straight to Whisper as 16 kHz, so we
# must downsample to 16 kHz before sending (sending 48 kHz makes speech play 3x slow).
OUT_RATE = 16_000

intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Bot(intents=intents)
pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def ensure_pool() -> asyncpg.Pool:
    """Lazily create the asyncpg pool on first use.

    Don't rely on on_ready alone: the Discord gateway can become ready before
    Postgres accepts connections (compose `depends_on` doesn't wait for DB
    readiness), which leaves `pool` None and makes `/join` fail with a confusing
    AttributeError. This creates the pool once, on demand, and surfaces a clear
    asyncpg error here if the DB is genuinely unreachable/misconfigured.
    """
    global pool
    if pool is not None:
        return pool
    async with _pool_lock:
        if pool is None:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    return pool


class PcmBuffer:
    """Accumulates per-user PCM emitted by DAVEVoiceClient; silence-gap segmented."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf: dict[int, bytearray] = defaultdict(bytearray)
        self.last: dict[int, float] = {}
        self.start: dict[int, float] = {}

    def write(self, user: int, data: bytes):
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

    def drain_ready(self, silence_s):
        return self._pop(only_silent=True, silence_s=silence_s)

    def drain_all(self):
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


# active: guild_id -> (voice_protocol, client, sink, meeting, flusher_task)
active: dict[int, tuple] = {}


def to_mono_wav(pcm_stereo: bytes) -> bytes:
    """Downmix 48 kHz stereo PCM to mono, resample to 16 kHz, and wrap as WAV.

    The worker hands WAV samples to Whisper assuming 16 kHz, so we must downsample
    here — sending 48 kHz stretches speech 3x and the VAD discards it as non-speech.
    """
    mono = audioop.tomono(pcm_stereo, SW, 0.5, 0.5)  # 48 kHz stereo -> 48 kHz mono
    mono16k, _ = audioop.ratecv(mono, SW, 1, SR, OUT_RATE, None)  # 48 kHz -> 16 kHz
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SW)
        w.setframerate(OUT_RATE)
        w.writeframes(mono16k)
    return buf.getvalue()


async def transcribe(wav: bytes) -> str | None:
    # Retry only on connection errors (worker restarting / still loading its model);
    # do NOT retry timeouts — those mean the worker is overloaded and retrying would
    # only deepen the backlog. FormData is single-use, so rebuild it per attempt.
    #
    # Returns None when the worker is unavailable (non-200 / connection error / other
    # exception) — the caller retries. Returns "" when the worker responded 200 OK but
    # found no speech (VAD stripped the clip) — that is a legitimate empty result, not a
    # failure; the caller must advance the queue, not retry the same silence forever.
    attempts = 3
    for attempt in range(attempts):
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", "whisper-1")  # OpenAI-compatible field; worker uses its own MODEL_SIZE
        if LANGUAGE:
            form.add_field("language", LANGUAGE)
        try:
            async with aiohttp.ClientSession() as s:
                timeout = aiohttp.ClientTimeout(total=TRANSCRIBE_TIMEOUT)
                async with s.post(TRANSCRIBE_URL, data=form, timeout=timeout) as r:
                    if r.status != 200:
                        print(f"transcribe HTTP {r.status}", flush=True)
                        return None
                    return ((await r.json()).get("text") or "").strip()
        except aiohttp.ClientConnectorError as e:
            if attempt < attempts - 1:
                await asyncio.sleep(2 * (attempt + 1))  # 2s, 4s backoff
                continue
            print(f"transcribe worker unreachable after {attempts} tries: {e}", flush=True)
            return None
        except Exception as e:
            print(f"transcribe error: {e}", flush=True)
            return None
    return None


def spill_segment(
    meeting_id: int, session_uid: str, speaker: str, t0_off: float, t1_off: float, language: str | None, wav: bytes
) -> Path:
    """Durably stage a segment for the background drainer.

    The transcription worker is single-concurrency and CPU-slow (~minutes per short clip
    on the NAS). Spilling to disk means a 503 or a bot restart never loses audio — the
    drainer replays it when the worker has capacity. Files survive crashes; on boot the
    drainer picks up anything left behind and inserts it against the stored meeting_id,
    so a crash mid-call just delays those rows, it doesn't drop them.
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    base = PENDING_DIR / f"{meeting_id}_{uuid.uuid4().hex}"
    base.with_suffix(".json").write_text(
        json.dumps(
            {
                "meeting_id": meeting_id,
                "session_uid": session_uid,
                "speaker": speaker,
                "t0": t0_off,
                "t1": t1_off,
                "language": language,
            }
        )
    )
    base.with_suffix(".wav").write_bytes(wav)
    return base


def scan_pending() -> list[Path]:
    """Pending sidecars, oldest first (FIFO replay so transcripts land in order)."""
    if not PENDING_DIR.exists():
        return []
    return sorted(PENDING_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)


def load_segment(json_path: Path) -> tuple[bytes, dict[str, Any]]:
    meta = json.loads(json_path.read_text())
    wav = json_path.with_suffix(".wav").read_bytes()
    return wav, meta


def mark_finalizing(meeting_id: int) -> None:
    """Record that /leave was called — the drainer flips this meeting to 'completed' once
    its pending queue is empty. On disk (not in-memory) so it survives a restart."""
    (PENDING_DIR / "finalizing").mkdir(parents=True, exist_ok=True)
    (PENDING_DIR / "finalizing" / str(meeting_id)).touch()


def finalizing_meetings() -> list[int]:
    d = PENDING_DIR / "finalizing"
    if not d.exists():
        return []
    return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit())


async def insert_transcription(meta: dict[str, Any], text: str) -> None:
    async with (await ensure_pool()).acquire() as c:
        await c.execute(
            "INSERT INTO transcriptions"
            " (meeting_id, start_time, end_time, text, speaker, language, session_uid, segment_id, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())",
            int(meta["meeting_id"]),
            float(meta["t0"]),
            float(meta["t1"]),
            text,
            meta["speaker"],
            meta.get("language"),
            meta["session_uid"],
            str(uuid.uuid4()),
        )


async def _finalize_completed() -> None:
    """Mark any finalizing meetings whose pending queue is empty as 'completed'.

    This is why /leave no longer sets 'completed' inline: a summary/webhook fired at
    'completed' would otherwise see only the segments the slow worker had gotten to.
    """
    fms = finalizing_meetings()
    if not fms:
        return
    pending_prefixes = {p.name.split("_", 1)[0] for p in scan_pending()}
    for meeting_id in fms:
        if str(meeting_id) in pending_prefixes:
            continue
        async with (await ensure_pool()).acquire() as c:
            await c.execute(
                "UPDATE meetings SET status='completed', end_time=now(), updated_at=now() WHERE id=$1",
                meeting_id,
            )
        (PENDING_DIR / "finalizing" / str(meeting_id)).unlink(missing_ok=True)
        print(f"meeting {meeting_id} completed (pending queue drained)", flush=True)


async def drain_once() -> str:
    """Process one pending segment.

    Returns "done" (transcribed + inserted), "failed" (worker busy/down — leave the file,
    back off), or "idle" (queue empty). Single-concurrency by construction: the worker
    accepts one job at a time, so the loop driving this never has >1 in flight.
    """
    items = scan_pending()
    if not items:
        await _finalize_completed()
        return "idle"
    json_path = items[0]
    try:
        wav, meta = load_segment(json_path)
    except (OSError, ValueError) as e:
        print(f"pending {json_path.name} unreadable, dropping: {e}", flush=True)
        json_path.with_suffix(".wav").unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        return "done"
    text = await transcribe(wav)
    if text is None:
        return "failed"  # worker busy/down — back off, retry this one next pass
    # Worker responded (200 OK). Empty text = silence/VAD-stripped — a legitimate no-speech
    # result, NOT a failure. Delete the clip and advance so a single silence segment can't
    # block the FIFO queue behind it forever (the stuck-queue bug).
    if text:
        await insert_transcription(meta, text)
        print(f"[{meta['speaker']}] {text}", flush=True)
    json_path.with_suffix(".wav").unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    await _finalize_completed()
    return "done"


_drain_task: asyncio.Task[None] | None = None


async def drain_loop() -> None:
    """Background replay of spilled segments. The disk directory is the queue, so this
    survives restarts and recovers crash-left segments on boot."""
    try:
        while True:
            try:
                status = await drain_once()
            except Exception as e:  # DB/worker blip — must not kill the drainer
                print(f"drain error (will retry): {e}", flush=True)
                await asyncio.sleep(DRAIN_BACKOFF)
                continue
            if status == "done":
                await asyncio.sleep(0)  # yield, then immediately pull the next one
            elif status == "failed":
                await asyncio.sleep(DRAIN_BACKOFF)
            else:
                await asyncio.sleep(DRAIN_IDLE_SLEEP)
    except asyncio.CancelledError:
        pass


def ensure_drain_loop() -> None:
    global _drain_task
    if _drain_task is None or _drain_task.done():
        _drain_task = asyncio.create_task(drain_loop())


async def store(meeting: Meeting, uid: int, pcm: bytes, t0: float, t1: float):
    """Spill a segment to the durable queue. The drainer transcribes + inserts it.

    Doing the downmix here (not in the drainer) keeps the drainer a pure read-POST-insert
    loop and means the disk holds a worker-ready 16 kHz WAV, not raw 48 kHz PCM.
    """
    if (len(pcm) / BYTES_PER_SEC) * 1000 < MIN_MS:
        return
    speaker = await meeting.name_for(uid)
    spill_segment(
        meeting.id,
        meeting.session_uid,
        speaker,
        t0 - meeting.t0,
        t1 - meeting.t0,
        (LANGUAGE or None),
        to_mono_wav(pcm),
    )


async def flusher(guild_id: int):
    try:
        while True:
            await asyncio.sleep(0.2)
            entry = active.get(guild_id)
            if not entry:
                return
            _, _, sink, meeting, _ = entry
            for uid, pcm, t0, t1 in sink.drain_ready(SILENCE_MS / 1000):
                asyncio.create_task(store(meeting, uid, pcm, t0, t1))
    except asyncio.CancelledError:
        pass


@bot.slash_command(description="Join your voice channel and start transcribing")
async def join(ctx: discord.ApplicationContext):
    guild = ctx.guild
    author = ctx.author
    if guild is None or not isinstance(author, discord.Member):
        await ctx.respond("Use this command in a server.", ephemeral=True)
        return
    voice = author.voice
    if voice is None or voice.channel is None:
        await ctx.respond("Join a voice channel first.", ephemeral=True)
        return
    if guild.id in active:
        await ctx.respond("Already recording in this server.", ephemeral=True)
        return

    channel = voice.channel
    await ctx.defer()

    # Register a VoiceProtocol so py-cord routes VOICE_SERVER_UPDATE / VOICE_STATE_UPDATE
    # to it (it does NOT dispatch them as client events). This sends gateway op 4 and
    # captures token/endpoint/session_id; DAVEVoiceClient then owns the voice WS/UDP.
    try:
        vc: DAVEVoiceProtocol = await channel.connect(cls=DAVEVoiceProtocol, timeout=20)
    except TimeoutError:
        await ctx.respond("Timed out connecting to voice. Try again.", ephemeral=True)
        return
    except discord.ClientException:
        await ctx.respond("Already connected to voice in this server.", ephemeral=True)
        return

    if not (vc.token and vc.endpoint and vc.session_id):
        await vc.disconnect(force=True)
        await ctx.respond("Didn't receive full voice credentials; try again.", ephemeral=True)
        return

    t0 = time.monotonic()
    session_uid = str(uuid.uuid4())
    async with (await ensure_pool()).acquire() as c:
        meeting_id = await c.fetchval(
            "INSERT INTO meetings"
            " (user_id, platform, platform_specific_id, status, start_time, data, created_at, updated_at)"
            " VALUES ($1,'discord',$2,'active', now(), '{}'::jsonb, now(), now()) RETURNING id",
            VEXA_USER_ID,
            str(channel.id),
        )
        await c.execute(
            "INSERT INTO meeting_sessions (meeting_id, session_uid, session_start_time) VALUES ($1,$2, now())",
            meeting_id,
            session_uid,
        )

    meeting = Meeting(guild, channel, meeting_id, session_uid, t0)
    sink = PcmBuffer()

    assert bot.user is not None  # the gateway is connected before any command runs
    client = DAVEVoiceClient(
        server_id=guild.id,
        channel_id=channel.id,
        user_id=bot.user.id,
        session_id=vc.session_id,
        token=vc.token,
        endpoint=vc.endpoint,
        on_pcm=lambda uid, pcm: sink.write(uid, pcm),
    )
    try:
        await client.start()
    except Exception as e:
        await vc.disconnect(force=True)
        async with (await ensure_pool()).acquire() as c:
            await c.execute(
                "UPDATE meetings SET status='failed', end_time=now(), updated_at=now() WHERE id=$1",
                meeting_id,
            )
        await ctx.respond(f"Failed to start voice receive: {e}", ephemeral=True)
        return
    task = asyncio.create_task(flusher(guild.id))
    ensure_drain_loop()  # in case on_ready hasn't run / the task died
    active[guild.id] = (vc, client, sink, meeting, task)
    await ctx.respond(f"Recording **{channel.name}** → meeting `{meeting_id}`. Speakers tagged by name.")


@bot.slash_command(description="Stop transcribing and leave the channel")
async def leave(ctx: discord.ApplicationContext):
    guild = ctx.guild
    if guild is None:
        await ctx.respond("Use this command in a server.", ephemeral=True)
        return
    entry = active.pop(guild.id, None)
    if not entry:
        await ctx.respond("Not recording here.", ephemeral=True)
        return
    vc, client, sink, meeting, task = entry
    task.cancel()
    await ctx.defer()
    await client.stop()
    await vc.disconnect(force=True)  # change_voice_state(None) + cleanup
    for uid, pcm, t0, t1 in sink.drain_all():
        await store(meeting, uid, pcm, t0, t1)
    # Don't mark 'completed' here: the slow worker may still be draining spilled segments,
    # and a summary/webhook fired at 'completed' needs every utterance. The drainer flips
    # the status once this meeting's pending queue is empty (see _finalize_completed).
    mark_finalizing(meeting.id)
    ensure_drain_loop()
    queued = len(scan_pending())
    await ctx.respond(f"Stopped. {queued} segment(s) queued → meeting `{meeting.id}` transcribes in the background.")


@bot.event
async def on_ready():
    try:
        await ensure_pool()  # best-effort warm-up; ensure_pool retries on first use
    except Exception as e:
        print(f"pool init deferred (will retry on first /join): {e}", flush=True)
    if not discord.opus.is_loaded():
        for lib in ("libopus.so.0", "libopus.so", "opus"):
            try:
                discord.opus.load_opus(lib)
                break
            except Exception:
                continue
    ensure_drain_loop()  # pick up any segments spilled before a restart
    print(f"discord-adapter ready as {bot.user}", flush=True)


if __name__ == "__main__":
    bot.run(TOKEN)
