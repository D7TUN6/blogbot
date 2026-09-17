#!/usr/bin/env python3
"""blogbot — превращает новые сообщения Telegram-канала в записи /blog.

Строго работает через ЛОКАЛЬНЫЙ telegram-bot-api (http://127.0.0.1:8082),
никаких обращений к api.telegram.org. Для каждого нового сообщения канала
(и медиа-альбомов) пишет .mdx в content/mdx/{ru,en}/blog/ через общие
хелперы blog_common.py — сайт подхватывает записи на лету без пересборки
(server/lib/mdx-utils.ts читает файлы с диска).

Достаёт отставшие сообщения за время простоя: telegram-bot-api хранит
непотверждённые апдейты ~24ч, offset после рестартов не форсируется, а
дедупликация держится на message_id в state-файле.

Переменные окружения:
  BLOGBOT_TOKEN       — токен бота (из sops, НЕ в файлы)
  BLOGBOT_CHANNEL_ID  — канал для чтения (default -1001667272666)
  BLOGBOT_SITE_ROOT   — корень сайта (default ~/files/mounts/TS480SSD/services/site/d7tun6)
  BLOGBOT_BOT_API_URL — локальный API (default http://127.0.0.1:8082)
  BLOGBOT_STATE       — файл состояния (default ./blogbot-state.json)
  BLOGBOT_TMP         — каталог загрузок временных файлов (default ./tmp)
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent

SITE_ROOT = Path(os.environ.get("BLOGBOT_SITE_ROOT", "/home/d7tun6/files/mounts/TS480SSD/services/site/d7tun6")).resolve()
sys.path.insert(0, str(SITE_ROOT / "scripts"))
import blog_common as bc  # noqa: E402

CHANNEL_ID = int(os.environ.get("BLOGBOT_CHANNEL_ID", "-1001667272666"))
API_URL = os.environ.get("BLOGBOT_BOT_API_URL", "http://127.0.0.1:8082").rstrip("/")
STATE_FILE = Path(os.environ.get("BLOGBOT_STATE", str(BOT_DIR / "blogbot-state.json")))
TMP_DIR = Path(os.environ.get("BLOGBOT_TMP", str(BOT_DIR / "tmp")))

from telegram import Update  # noqa: E402
from telegram.ext import Application, ContextTypes, MessageHandler  # noqa: E402
from telegram.ext.filters import Chat  # noqa: E402
from telegram.request import HTTPXRequest  # noqa: E402


# ── внутреннее состояние ─────────────────────────────────────────
class BotState:
    def __init__(self) -> None:
        self.seen: dict[int, str] = {}
        self.slugs: list[str] = []
        self.stats: dict[str, int] = {}
        self.seq = 0
        self.load()

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.seen = {int(k): v for k, v in (data.get("seen") or {}).items()}
            self.slugs = list(data.get("slugs") or [])
        except Exception as exc:
            print(f"[blogbot] state load failed ({exc}) — starting fresh", flush=True)

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "seen": self.seen,
            "slugs": self.slugs,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


ST = BotState()


# ── entity/текст (Bot API → export-style) ────────────────────────
def _entities(text: str, entities) -> list[dict]:
    if not text:
        return []
    if not entities:
        return [{"type": "plain", "text": text}]
    out: list[dict] = []
    pos = 0
    for ent in sorted(entities, key=lambda e: (e.offset, e.length)):
        off, ln = ent.offset, ent.length
        if off > pos:
            out.append({"type": "plain", "text": text[pos:off]})
        piece = text[off:off + ln]
        t = ent.type
        if t == "text_link":
            out.append({"type": "text_link", "text": piece, "href": ent.url or ""})
        elif t in ("url", "hashtag"):
            out.append({"type": "link", "text": piece, "href": piece})
        elif t in ("bold", "italic", "underline", "strikethrough", "code"):
            out.append({"type": t, "text": piece})
        elif t == "pre":
            out.append({"type": "pre", "text": piece})
        elif t == "blockquote":
            out.append({"type": "blockquote", "text": piece})
        else:
            out.append({"type": "plain", "text": piece})
        pos = off + ln
    if pos < len(text):
        out.append({"type": "plain", "text": text[pos:]})
    return out


# ── медиа (Bot API → упрощённый dict) ────────────────────────────
def pick_medium(message) -> dict | None:
    if message.photo:
        size = max(message.photo, key=lambda s: s.file_size or 0)
        return {"photo": size}
    if message.audio:
        a = message.audio
        return {"file": a, "file_name": a.file_name or "audio.mp3", "mime_type": a.mime_type or "", "media_type": "audio"}
    if message.video:
        v = message.video
        return {"file": v, "file_name": v.file_name or "video.mp4", "mime_type": v.mime_type or "", "media_type": "video"}
    if message.voice:
        return {"file": message.voice, "file_name": "voice.ogg", "mime_type": "audio/ogg", "media_type": "voice"}
    if message.video_note:
        return {"file": message.video_note, "file_name": "video_note.mp4", "mime_type": "video/mp4", "media_type": "video"}
    if message.animation:
        a = message.animation
        return {"file": a, "file_name": a.file_name or "anim.gif", "mime_type": a.mime_type or "", "media_type": "animation"}
    if message.sticker:
        return {"file": message.sticker, "file_name": "sticker.webp", "mime_type": "", "media_type": "sticker"}
    if message.document:
        d = message.document
        return {"file": d, "file_name": d.file_name or "file", "mime_type": d.mime_type or "", "media_type": "document"}
    return None


async def download_to_tmp(bot, fobj, tag: str) -> Path | None:
    try:
        f = await bot.get_file(fobj.file_id)
    except Exception as exc:
        print(f"[blogbot] getFile {tag} fail: {exc}", flush=True)
        return None
    if not f.file_path:
        print(f"[blogbot] getFile {tag}: no file_path", flush=True)
        return None

    ST.seq += 1
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # telegram-bot-api запущен в --local моде: getFile возвращает
    # абсолютный путь на диске, HTTP-эндпоинта /file/... нет вовсе.
    if f.file_path.startswith("/"):
        src = Path(f.file_path)
        if not src.exists():
            print(f"[blogbot] local media {f.file_path} не найден", flush=True)
            return None
        dest = TMP_DIR / f"{ST.seq:05d}_{bc.safe_name(src.name)}"
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            print(f"[blogbot] local copy fail: {exc}", flush=True)
            return None
        return dest

    dest = TMP_DIR / f"{ST.seq:05d}_{bc.safe_name(Path(f.file_path).name)}"
    try:
        await f.download_to_drive(dest)
    except Exception as exc:
        print(f"[blogbot] download {tag} fail: {exc}", flush=True)
        return None
    return dest


def _kind_of(m: dict) -> str:
    ext = Path(m.get("file_name", "")).suffix.lower()
    mime = (m.get("mime_type") or "").lower()
    mtype = m.get("media_type", "")
    if mtype == "sticker":
        return "file" if ext not in bc.IMAGE_EXTS else "img"
    if mtype == "animation" or ext in bc.IMAGE_EXTS or mime.startswith("image/"):
        return "img"
    if (mtype == "voice" or mtype == "audio"
            or ext in bc.AUDIO_EXTS or mime.startswith(("audio/", "application/ogg", "application/x-ogg"))):
        return "audio"
    if ext in bc.VIDEO_EXTS or mime.startswith("video/"):
        return "video"
    return "file"


async def render_media(bot, media: list[dict], unix: int, photos_only: bool) -> list[str]:
    """media — список medium-dict'ов сообщения; возвращает html-блоки медиа."""
    out: list[str] = []
    if not media:
        return out
    if photos_only:
        for m in media:
            if "photo" not in m:
                continue
            src = await download_to_tmp(bot, m["photo"], "photo")
            url = bc.commit_media(src, unix, bc.safe_name(f"photo-{ST.seq:05d}")) if src else None
            if url:
                out.extend(bc.media_block("photo", url, "фото из канала"))
        return out

    for m in media:
        if "photo" in m:
            src = await download_to_tmp(bot, m["photo"], "photo")
            url = bc.commit_media(src, unix, bc.safe_name(f"photo-{ST.seq:05d}")) if src else None
            if url:
                out.extend(bc.media_block("photo", url, "фото из канала"))
            continue
        fobj = m.get("file")
        if not fobj:
            continue
        fname = m.get("file_name") or "file"
        kind = _kind_of(m)
        src = await download_to_tmp(bot, fobj, kind)
        if not src:
            continue
        base = bc.safe_name(f"{kind}-{ST.seq:05d}")
        url = bc.commit_media(src, unix, base)
        if not url:
            continue
        poster_url = None
        thumb = getattr(fobj, "thumbnail", None)
        if kind == "video" and thumb:
            tsrc = await download_to_tmp(bot, thumb, "thumb")
            poster_url = bc.commit_media(tsrc, unix, bc.safe_name(f"thumb-{ST.seq:05d}")) if tsrc else None
        if kind == "img":
            out.extend(bc.media_block("photo", url, fname))
        elif kind == "audio":
            out.extend(bc.media_block("audio", url, fname))
        elif kind == "video":
            out.extend(bc.media_block("video", url, fname, poster_url))
        else:
            out.extend(bc.media_block("file", url, fname))
    return out


# ── фильтры (зеркалят scripts/tg-blog-import.py) ─────────────────
def skip_reason(plain: str, has_media: bool) -> str | None:
    strip_links = re.sub(r"https?://\S+|www\.\S+", "", plain).strip()
    if has_media and len(plain) < 8 and strip_links == "":
        return "медиа без текста/контекста"
    if strip_links == "" and re.search(r"https?://\S+", plain) and len(plain) < 60:
        return "изолированная ссылка"
    if not has_media and not plain:
        return "пустое сообщение"
    if not has_media and len(plain) < 50:
        return "короткий флуд (<50 без медиа)"
    return None


def _make_slug(date_iso: str, title: str, used: set[str]) -> str:
    slug_base = bc.slugify_ru(title) or "post"
    slug = f"{date_iso[:10]}-{slug_base[:48]}"
    slug = re.sub(r"-+", "-", slug).strip("-")[:64]
    if not slug:
        slug = f"{date_iso[:10]}-post"
    if slug in used:
        n = 2
        while f"{slug}-{n}" in used:
            n += 1
        slug = f"{slug}-{n}"
    return slug


def _existing_slugs() -> set[str]:
    out = set(ST.slugs)
    if bc.BLOG_RU.exists():
        out.update(p.stem for p in bc.BLOG_RU.glob("*.mdx"))
    return out


async def publish_messages(bot, msgs: list, media: list[dict]) -> None:
    """msgs — 1+ Message канала (одна запись; альбом = одна запись)."""
    first = msgs[0]
    if first.message_id in ST.seen:
        return

    ents_total: list[dict] = []
    for i, msg in enumerate(msgs):
        if i:
            ents_total.append({"type": "plain", "text": "\n"})
        text = msg.text or msg.caption or ""
        ents_total += _entities(text, msg.entities or msg.caption_entities or [])

    plain = "".join(e.get("text", "") for e in ents_total).strip()
    has_media = bool(media)
    reason = skip_reason(plain, has_media)
    if reason:
        ST.stats[reason] = ST.stats.get(reason, 0) + 1
        ct = f"{first.message_id}" + (f" (альбом {first.media_group_id})" if len(msgs) > 1 else "")
        print(f"[blogbot] skip {ct}: {reason}", flush=True)
        return

    paras = bc.build_paragraphs(ents_total)
    if not paras and not has_media:
        ST.stats["нет контента для блога"] = ST.stats.get("нет контента для блога", 0) + 1
        print(f"[blogbot] skip {first.message_id}: нет контента для блога", flush=True)
        return

    album = bc.album_match(plain)
    attachments = await render_media(bot, media, int(first.date.timestamp()), photos_only=bool(album))

    title_line = bc.first_line_plain(paras) or "Пост"
    title = re.sub(r"\s+", " ", title_line)[:64].strip().strip(".,;:! _-") or "пост"
    local_dt = datetime.fromtimestamp(first.date.timestamp())
    date_iso = local_dt.strftime("%Y-%m-%d %H:%M:%S")
    used = _existing_slugs()
    slug = _make_slug(date_iso, title, used)
    used.add(slug)

    excerpt = html.unescape(re.sub(r"\s+", " ", title_line))[:140]
    tags = bc.classify(plain)
    content = bc.make_content(paras, attachments, album, [], slug, title, date_iso, tags, excerpt)
    bc.write_post(slug, content)

    for msg in msgs:
        ST.seen[msg.message_id] = slug
    ST.slugs.append(slug)
    ST.save()

    kind = f"альбом из {len(msgs)} сообщений" if len(msgs) > 1 else "сообщение"
    print(f"[blogbot] post {slug} <- {first.message_id} ({kind})", flush=True)


async def on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    chat = update.effective_chat
    if not chat or chat.id != CHANNEL_ID:
        return
    s_chat = getattr(msg, "sender_chat", None)
    if s_chat and getattr(s_chat, "id", None) != CHANNEL_ID:
        return
    if msg.message_id in ST.seen:
        return

    if msg.media_group_id:
        context.application.bot_data.setdefault("media_groups", {})
        groups = context.application.bot_data["media_groups"]
        # последовательные сообщения одного альбома копятся и флашатся
        # в on_post_poll (после каждого цикла getUpdates)
        groups.setdefault(msg.media_group_id, []).append(msg)
        print(f"[blogbot] buffered {msg.message_id} in album {msg.media_group_id} (total {len(groups[msg.media_group_id])})", flush=True)
        return

    media = []
    medium = pick_medium(msg)
    if medium:
        media = [medium]
    await publish_messages(context.bot, [msg], media)


async def flush_albums(app: Application, stale_after: float = 8.0) -> None:
    now = time.time()
    groups = app.bot_data.get("media_groups", {})
    for gid, msgs in list(groups.items()):
        if not msgs:
            continue
        last = max((m.date.timestamp() for m in msgs), default=0)
        if now - last < stale_after:
            continue
        media = [m for m in (pick_medium(m) for m in msgs) if m]
        await publish_messages(app.bot, msgs, media)
        groups.pop(gid, None)


async def album_flusher(app: Application) -> None:
    while True:
        await asyncio.sleep(2.0)
        try:
            await flush_albums(app)
        except Exception as exc:
            print(f"[blogbot] flush error: {exc}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-sleep", type=float, default=1.0,
                    help="пауза между циклами getUpdates")
    args = ap.parse_args()

    token = os.environ.get("BLOGBOT_TOKEN", "")
    if not token:
        print("ERROR: BLOGBOT_TOKEN не задан", file=sys.stderr)
        return 2
    if API_URL != "http://127.0.0.1:8082":
        print(f"ERROR: BLOGBOT_BOT_API_URL должен указывать на локальный telegram-bot-api, а не {API_URL}",
              file=sys.stderr)
        return 2

    async def post_init(application: Application) -> None:
        # asyncio.create_task (не application.create_task): флашер работает
        # сразу после запуска event loop'а без PTB-прочих предупреждений
        asyncio.get_running_loop().create_task(album_flusher(application))

    app = (
        Application.builder()
        .token(token)
        .base_url(f"{API_URL}/bot")
        .request(HTTPXRequest(read_timeout=180, write_timeout=180, connect_timeout=30))
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(Chat(CHANNEL_ID), on_update))

    print(f"[blogbot] start: poller={API_URL}, channel={CHANNEL_ID}, site={SITE_ROOT}", flush=True)
    app.run_polling(allowed_updates=[Update.CHANNEL_POST, Update.MESSAGE],
                    drop_pending_updates=False,
                    poll_interval=args.poll_sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())