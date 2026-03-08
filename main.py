import asyncio
import io
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.permission import PermissionType

from .utils.config_manager import load_config
from .utils.jm_ops import (
    cache_cover_image,
    clear_domains,
    clear_plugin_runtime_files,
    download_album_or_photos,
    download_single_image,
    generate_album_pdf,
    get_album_detail,
    get_album_page_stats,
    get_album_total_pages_fallback,
    get_random_album,
    get_search_page,
    search_album,
    update_domains,
)

_GLOBAL_FILE_LOCK = threading.RLock()


@register(
    "astrbot_plugin_jm_bot", "chatgpt", "适配 AstrBot 的 JM 漫画下载插件", "v0.1.1"
)
class JMBot(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = load_config(config)
        self.search_cache_file = Path(self.config["cache"]["search_cache_file"])
        self.chapter_selection_cache_file = Path(
            self.config["cache"]["chapter_selection_cache_file"]
        )
        self.search_cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.chapter_selection_cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_locks = {}
        self._page_count_cache = {}
        self._search_cache_ttl_seconds = 1800
        self._page_count_cache_ttl_seconds = 21600
        self._page_count_cache_max_entries = 512
        self._album_locks = {}
        self._lock_guard = threading.RLock()
        self._active_tasks = {}
        self._pdf_build_lock = asyncio.Lock()
        self._apply_configured_command_aliases()

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if group_id:
            return f"group:{group_id}:user:{sender_id}"
        return f"private:{sender_id}"

    def _get_named_lock(self, bucket: dict, key: str) -> threading.RLock:
        with self._lock_guard:
            if key not in bucket:
                bucket[key] = threading.RLock()
            return bucket[key]

    def _acquire_session(self, session_key: str):
        lock = self._get_named_lock(self._session_locks, session_key)
        ok = lock.acquire(blocking=False)
        if not ok:
            raise RuntimeError("当前会话已有 JM 任务在运行，请稍候再试")
        return lock

    def _acquire_album(self, album_id: str):
        lock = self._get_named_lock(self._album_locks, f"album:{album_id}")
        ok = lock.acquire(blocking=False)
        if not ok:
            raise RuntimeError(f"漫画 {album_id} 正在被其他任务处理，请稍候再试")
        return lock

    def _set_active_task(self, session_key: str, action: str, target: str = ""):
        with self._lock_guard:
            self._active_tasks[session_key] = {
                "action": action,
                "target": target,
                "time": datetime.now().isoformat(),
            }

    def _clear_active_task(self, session_key: str):
        with self._lock_guard:
            self._active_tasks.pop(session_key, None)

    def _runtime_hint(self) -> str:
        with self._lock_guard:
            running = len(self._active_tasks)
        image_threads = int(
            (self.config.get("download", {}) or {}).get("image_threads", 1) or 1
        )
        photo_threads = int(
            (self.config.get("download", {}) or {}).get("photo_threads", 1) or 1
        )
        return f"当前运行任务 {running} 个，线程配置 图={image_threads} / 章={photo_threads}"

    def _auto_recall_seconds(self) -> int:
        return int(
            (self.config.get("interaction", {}) or {}).get("auto_recall_seconds", 60)
            or 0
        )

    async def _schedule_recall(self, event: AstrMessageEvent, message_id):
        seconds = self._auto_recall_seconds()
        if seconds <= 0 or not message_id:
            return
        try:
            await asyncio.sleep(seconds)
            await event.bot.delete_msg(message_id=message_id)
        except Exception:
            pass

    async def _send_forward_nodes(
        self, event: AstrMessageEvent, nodes_list: list[Node]
    ):
        if not nodes_list:
            return
        if isinstance(event, AiocqhttpMessageEvent) and self._auto_recall_seconds() > 0:
            is_group = bool(event.get_group_id())
            payload = await Nodes(nodes_list).to_dict()
            if is_group:
                payload["group_id"] = event.get_group_id()
                ret = await event.bot.call_action("send_group_forward_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                ret = await event.bot.call_action("send_private_forward_msg", **payload)
            message_id = ret.get("message_id") if isinstance(ret, dict) else None
            if message_id:
                asyncio.create_task(self._schedule_recall(event, message_id))
            return
        await event.send(event.chain_result([Nodes(nodes_list)]))

    async def _send_plain(self, event: AstrMessageEvent, text: str):
        if isinstance(event, AiocqhttpMessageEvent) and self._auto_recall_seconds() > 0:
            payloads = {"message": [{"type": "text", "data": {"text": text}}]}
            is_group = bool(event.get_group_id())
            if is_group:
                payloads["group_id"] = event.get_group_id()
                ret = await event.bot.call_action("send_group_msg", **payloads)
            else:
                payloads["user_id"] = event.get_sender_id()
                ret = await event.bot.call_action("send_private_msg", **payloads)
            message_id = ret.get("message_id") if isinstance(ret, dict) else None
            if message_id:
                asyncio.create_task(self._schedule_recall(event, message_id))
            return
        await event.send(event.plain_result(text))

    def _command_name(self, key: str, default: str) -> str:
        commands = self.config.get("commands", {}) or {}
        value = str(commands.get(key, default) or default).strip()
        return value or default

    def _command_aliases(self, key: str, default: str) -> set[str]:
        commands = self.config.get("commands", {}) or {}
        aliases = {default}
        configured = str(commands.get(key, default) or default).strip()
        if configured:
            aliases.add(configured)
        aliases.discard(default)
        return aliases

    def _apply_command_binding(self, handler_name: str, key: str, default: str):
        handler = getattr(self, handler_name, None)
        if handler is None:
            return
        for event_filter in getattr(handler, "_event_filters", []) or []:
            if isinstance(event_filter, CommandFilter):
                event_filter.command_name = self._command_name(key, default)
                event_filter.alias = self._command_aliases(key, default)
                if hasattr(event_filter, "_cmpl_cmd_names"):
                    event_filter._cmpl_cmd_names = None
                break

    def _apply_configured_command_aliases(self):
        self._apply_command_binding("jm_unified", "view", "看jm")
        self._apply_command_binding("search_jm", "search", "搜jm")
        self._apply_command_binding("random_jm", "random", "随机jm")
        self._apply_command_binding("jm_update_domain", "update_domain", "jm更新域名")
        self._apply_command_binding("jm_clear_domain", "clear_domain", "jm清空域名")

    @staticmethod
    def parse_command(message: str) -> list[str]:
        cleaned_text = re.sub(r"@\S+\s*", "", message).strip()
        return [p for p in cleaned_text.split(" ") if p][1:]

    def _purge_search_cache(self, data: dict) -> dict:
        now = datetime.now()
        cleaned = {}
        for key, item in (data or {}).items():
            if not isinstance(item, dict):
                continue
            created_at = item.get("created_at")
            if not created_at:
                continue
            try:
                if (
                    now - datetime.fromisoformat(created_at)
                ).total_seconds() <= self._search_cache_ttl_seconds:
                    cleaned[key] = item
            except Exception:
                continue
        return cleaned

    def _load_search_cache(self) -> dict:
        with _GLOBAL_FILE_LOCK:
            if not self.search_cache_file.exists():
                return {}
            try:
                data = json.loads(
                    self.search_cache_file.read_text(encoding="utf-8") or "{}"
                )
            except Exception:
                return {}
        cleaned = self._purge_search_cache(data)
        if cleaned != data:
            self._save_search_cache(cleaned)
        return cleaned

    def _save_search_cache(self, data: dict):
        with _GLOBAL_FILE_LOCK:
            self.search_cache_file.write_text(
                json.dumps(
                    self._purge_search_cache(data), ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )

    def _resolve_album_id(self, session_key: str, token: str) -> str | None:
        cache = self._load_search_cache()
        user_cache = cache.get(str(session_key), {}) or {}
        items = user_cache.get("items", {}) if isinstance(user_cache, dict) else {}

        if token in items:
            return str(items[token])

        if token.isdigit():
            return token

        return None

    def _load_chapter_selection_cache(self) -> dict:
        with _GLOBAL_FILE_LOCK:
            if not self.chapter_selection_cache_file.exists():
                return {}
            try:
                return json.loads(
                    self.chapter_selection_cache_file.read_text(encoding="utf-8")
                    or "{}"
                )
            except Exception:
                return {}

    def _save_chapter_selection_cache(self, data: dict):
        with _GLOBAL_FILE_LOCK:
            self.chapter_selection_cache_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _get_selection_key(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if group_id:
            return f"group:{group_id}:user:{sender_id}"
        return f"private:{sender_id}"

    def _purge_expired_selections(self, cache: dict) -> dict:
        now = datetime.now()
        cleaned = {}
        for key, item in cache.items():
            expires_at = item.get("expires_at")
            if not expires_at:
                continue
            try:
                if datetime.fromisoformat(expires_at) > now:
                    cleaned[key] = item
            except Exception:
                continue
        return cleaned

    def _format_chapter_lines(self, chapters: list[dict[str, str]]) -> list[str]:
        lines = []
        for chapter in chapters:
            idx = chapter["selection_index"]
            cidx = chapter.get("chapter_index", str(idx))
            title = chapter.get("chapter_title", "") or "未命名章节"
            pid = chapter["photo_id"]
            pcount = chapter.get("page_count", 0)
            ptext = f" / {pcount}P" if pcount else ""
            lines.append(f"[{idx}] 第{cidx}话 / {pid}{ptext} - {title}")
        return lines

    def _build_chapter_selection_message(self, album: dict[str, object]) -> str:
        threshold = int(
            self.config.get("interaction", {}).get("chapter_fold_threshold", 20)
        )
        chapters = album["chapters"]
        lines = [
            f"这本有 {album['chapter_count']} 个章节，不能直接乱冲哦～",
            f"漫画: [{album['album_id']}] {album['title']}",
            f"共 {album.get('chapter_count', 0)} 章 / {album.get('total_pages', 0)} P",
            "请输入章节编号",
            "支持单个、逗号、范围，比如: 2 或 1,3,5-7",
        ]
        chapter_lines = self._format_chapter_lines(chapters)
        if threshold > 0 and len(chapter_lines) > threshold:
            lines.append(f"章节列表较长，下面发折叠内容，共 {len(chapter_lines)} 条。")
        else:
            lines.append("章节列表:")
            lines.extend(chapter_lines)
        return "\n".join(lines)

    def _parse_chapter_selection_input(self, raw: str, max_index: int) -> list[int]:
        text = raw.strip()
        if text.startswith("选jm"):
            text = text[3:].strip()
        if not text:
            return []

        numbers = set()
        normalized = text.replace("，", ",").replace("、", ",").replace("~", "-")
        for part in [p.strip() for p in normalized.split(",") if p.strip()]:
            if "-" in part:
                left, right = part.split("-", 1)
                if not left.isdigit() or not right.isdigit():
                    raise ValueError("章节范围格式不对")
                start, end = int(left), int(right)
                if start > end:
                    start, end = end, start
                for value in range(start, end + 1):
                    numbers.add(value)
            else:
                if not part.isdigit():
                    raise ValueError("章节编号只能是数字、逗号或范围")
                numbers.add(int(part))

        result = sorted(n for n in numbers if 1 <= n <= max_index)
        if not result:
            raise ValueError("没有有效的章节编号")
        return result

    def _store_pending_selection(
        self, event: AstrMessageEvent, album: dict[str, object]
    ):
        cache = self._purge_expired_selections(self._load_chapter_selection_cache())
        key = self._get_selection_key(event)
        ttl = int(
            self.config.get("interaction", {}).get("chapter_selection_ttl", 86400)
        )
        expires_at = datetime.now() + timedelta(seconds=ttl)
        cache[key] = {
            "album_id": album["album_id"],
            "title": album["title"],
            "chapters": album["chapters"],
            "expires_at": expires_at.isoformat(),
        }
        self._save_chapter_selection_cache(cache)

    def _pop_pending_selection(self, event: AstrMessageEvent) -> dict | None:
        cache = self._purge_expired_selections(self._load_chapter_selection_cache())
        key = self._get_selection_key(event)
        item = cache.pop(key, None)
        self._save_chapter_selection_cache(cache)
        return item

    async def _send_chapter_selection_prompt(
        self, event: AstrMessageEvent, album: dict[str, object]
    ):
        threshold = int(
            self.config.get("interaction", {}).get("chapter_fold_threshold", 20)
        )
        base_message = self._build_chapter_selection_message(album)
        chapter_lines = self._format_chapter_lines(album["chapters"])
        if threshold > 0 and len(chapter_lines) > threshold:
            await self._send_plain(event, base_message)
            sender_name = "JM章节列表"
            sender_id = event.get_self_id()
            try:
                sender_id = int(sender_id)
            except Exception:
                pass
            nodes_list = []
            chunk = []
            for line in chapter_lines:
                chunk.append(line)
                if len(chunk) >= 40:
                    content_text = "章节列表:" + "\n" + "\n".join(chunk)
                    nodes_list.append(
                        Node(
                            name=sender_name,
                            uin=sender_id,
                            content=[Plain(content_text)],
                        )
                    )
                    chunk = []
            if chunk:
                content_text = "章节列表:" + "\n" + "\n".join(chunk)
                nodes_list.append(
                    Node(name=sender_name, uin=sender_id, content=[Plain(content_text)])
                )
            if nodes_list:
                await self._send_forward_nodes(event, nodes_list)
            return
        await self._send_plain(event, base_message)

    def _parse_image_request(self, args: list[str]) -> dict[str, int | str] | None:
        if len(args) < 3:
            return None

        token = args[0]
        chapter_text = str(args[1]).strip()
        page_text = str(args[2]).strip().upper()

        if page_text.startswith("P"):
            page_text = page_text[1:]
        if not chapter_text.isdigit() or not page_text.isdigit():
            return None

        return {
            "token": token,
            "chapter": chapter_text,
            "page": int(page_text),
        }

    async def _search(self, query: str, page: int):
        return await asyncio.to_thread(search_album, self.config, query, page)

    async def _search_with_covers(self, query: str, page: int):
        return await asyncio.to_thread(get_search_page, self.config, query, page)

    def _add_number_to_image(
        self, image: PILImage.Image, number: int
    ) -> PILImage.Image:
        image = image.convert("RGBA")
        txt_layer = PILImage.new("RGBA", image.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)
        try:
            font = ImageFont.truetype("msyh.ttc", size=48)
        except Exception:
            font = ImageFont.load_default()
        text = str(number)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        rect_height = text_height + 18
        draw.rectangle(
            (0, image.height - rect_height, image.width, image.height),
            fill=(0, 0, 0, 160),
        )
        draw.text(
            ((image.width - text_width) / 2, image.height - rect_height + 8),
            text,
            font=font,
            fill=(255, 255, 255, 255),
        )
        return PILImage.alpha_composite(image, txt_layer).convert("RGB")

    def _create_combined_image(self, images: list[PILImage.Image]):
        valid_images = [img for img in images if img is not None]
        if not valid_images:
            return None
        numbered_images = [
            self._add_number_to_image(img, i) for i, img in enumerate(valid_images, 1)
        ]
        target_height = 420
        padding = 8
        images_per_row = 5
        scaled_widths = [
            int((img.size[0] * target_height) / img.size[1]) for img in numbered_images
        ]
        rows, cur, total = [], [], 0
        for width in scaled_widths:
            if len(cur) < images_per_row:
                cur.append(width)
                total += width
            else:
                rows.append((cur, total))
                cur, total = [width], width
        if cur:
            rows.append((cur, total))
        total_width = (
            max(row_total for _, row_total in rows) + (images_per_row - 1) * padding
        )
        total_height = len(rows) * target_height + (len(rows) - 1) * padding
        combined = PILImage.new("RGB", (total_width, total_height), (255, 255, 255))
        y_offset = 0
        image_index = 0
        for row_widths, row_total in rows:
            row_start_x = (
                total_width - (row_total + (len(row_widths) - 1) * padding)
            ) // 2
            x_offset = row_start_x
            for scaled_width in row_widths:
                img = numbered_images[image_index].resize(
                    (scaled_width, target_height), PILImage.Resampling.LANCZOS
                )
                combined.paste(img, (x_offset, y_offset))
                x_offset += scaled_width + padding
                image_index += 1
            y_offset += target_height + padding
        return combined

    async def _prefetch_selected_chapter_stats(
        self, album_id: str, selected: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        if not selected:
            return selected
        interaction = self.config.get("interaction", {}) or {}
        concurrency = int(interaction.get("chapter_detail_threads", 4) or 4)
        selected_photo_ids = [
            str(item.get("photo_id", ""))
            for item in selected
            if str(item.get("photo_id", "")).strip()
        ]
        stats = await asyncio.to_thread(
            get_album_page_stats,
            self.config,
            album_id,
            selected_photo_ids,
            concurrency,
        )
        chapter_map = {
            str(item.get("photo_id", "")): item
            for item in (stats.get("chapters", []) or [])
        }
        merged = []
        for item in selected:
            extra = chapter_map.get(str(item.get("photo_id", "")), {})
            row = dict(item)
            if extra.get("chapter_title"):
                row["chapter_title"] = extra.get("chapter_title")
            if extra.get("page_count") is not None:
                row["page_count"] = int(extra.get("page_count", 0) or 0)
            merged.append(row)
        return merged

    async def _download_search_covers(
        self, items: list[dict[str, object]]
    ) -> list[PILImage.Image]:
        semaphore = asyncio.Semaphore(
            int(
                (self.config.get("interaction", {}) or {}).get(
                    "search_cover_threads", 5
                )
                or 5
            )
        )

        async def one(item):
            aid = str(item.get("id", ""))
            async with semaphore:
                last_error = None
                for _ in range(3):
                    try:
                        path = await cache_cover_image(self.config, aid, "_3x4")
                        if not path:
                            await asyncio.sleep(0.15)
                            continue
                        return await asyncio.to_thread(
                            lambda: PILImage.open(path).convert("RGB")
                        )
                    except Exception as e:
                        last_error = e
                        await asyncio.sleep(0.15)
                logger.warning(f"cover open failed for {aid}: {last_error}")
                return None

        return await asyncio.gather(*[one(item) for item in items[:10]])

    def _purge_page_count_cache(self):
        now = datetime.now()
        cleaned = {}
        for aid, item in list((self._page_count_cache or {}).items()):
            try:
                age = (now - datetime.fromisoformat(str(item.get("time")))).total_seconds()
                if age <= self._page_count_cache_ttl_seconds:
                    cleaned[aid] = item
            except Exception:
                continue

        if len(cleaned) > self._page_count_cache_max_entries:
            sorted_items = sorted(
                cleaned.items(),
                key=lambda kv: str((kv[1] or {}).get("time", "")),
                reverse=True,
            )[: self._page_count_cache_max_entries]
            cleaned = dict(sorted_items)

        self._page_count_cache = cleaned

    async def _fill_page_counts(self, items: list[dict[str, object]], limit: int = 10):
        self._purge_page_count_cache()
        semaphore = asyncio.Semaphore(
            int(
                (self.config.get("interaction", {}) or {}).get(
                    "search_page_count_threads", 3
                )
                or 3
            )
        )

        async def one(item):
            aid = str(item.get("id", ""))
            cached = self._page_count_cache.get(aid)
            if isinstance(cached, dict):
                try:
                    age = (
                        datetime.now() - datetime.fromisoformat(str(cached.get("time")))
                    ).total_seconds()
                    if age <= self._page_count_cache_ttl_seconds:
                        item["page_count"] = int(cached.get("pages", 0) or 0)
                        return
                except Exception:
                    pass
            async with semaphore:
                try:
                    stats = await asyncio.to_thread(
                        get_album_page_stats, self.config, aid
                    )
                    pages = int((stats or {}).get("total_pages", 0) or 0)
                    if pages <= 0:
                        pages = int(
                            await asyncio.to_thread(
                                get_album_total_pages_fallback, self.config, aid
                            )
                            or 0
                        )
                    self._page_count_cache[aid] = {
                        "pages": pages,
                        "time": datetime.now().isoformat(),
                    }
                    self._purge_page_count_cache()
                    item["page_count"] = pages
                except Exception as e:
                    logger.warning(f"fill page count failed for {aid}: {e}")
                    try:
                        pages = int(
                            await asyncio.to_thread(
                                get_album_total_pages_fallback, self.config, aid
                            )
                            or 0
                        )
                    except Exception as e2:
                        logger.warning(
                            f"fill page count fallback failed for {aid}: {e2}"
                        )
                        pages = 0
                    self._page_count_cache[aid] = {
                        "pages": pages,
                        "time": datetime.now().isoformat(),
                    }
                    self._purge_page_count_cache()
                    item["page_count"] = pages

        await asyncio.gather(*[one(item) for item in items[:limit]])
        return items

    async def _send_search_forward(
        self, event: AstrMessageEvent, result_text: str, combined_image_obj=None
    ):
        sender_name = "JM搜索"
        sender_id = event.get_self_id()
        try:
            sender_id = int(sender_id)
        except Exception:
            pass
        nodes_list = []
        temp_file_path = ""
        try:
            if combined_image_obj is not None:
                import os
                import tempfile

                img_byte_arr = io.BytesIO()
                combined_image_obj.save(img_byte_arr, "JPEG", quality=85)
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=".jpg"
                ) as temp_file:
                    temp_file.write(img_byte_arr.getvalue())
                    temp_file_path = temp_file.name
                nodes_list.append(
                    Node(
                        name=sender_name, uin=sender_id, content=[Image(temp_file_path)]
                    )
                )
            nodes_list.append(
                Node(name=sender_name, uin=sender_id, content=[Plain(result_text)])
            )
            if nodes_list:
                await self._send_forward_nodes(event, nodes_list)
        finally:
            if temp_file_path:
                import os

                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)

    async def _send_search_preview(
        self,
        event: AstrMessageEvent,
        query: str,
        page: int,
        search_page: dict[str, object],
    ):
        items = list(search_page.get("items", []) or [])
        cache = self._load_search_cache()
        user_key = self._get_session_key(event)
        cache[user_key] = {
            "query": query,
            "page": page,
            "items": {},
            "created_at": datetime.now().isoformat(),
        }
        for idx, item in enumerate(items[:10], 1):
            cache[user_key]["items"][str(idx)] = str(item.get("id", ""))
        self._save_search_cache(cache)

        await self._fill_page_counts(items, limit=min(10, len(items)))
        covers = await self._download_search_covers(items)
        combined = self._create_combined_image(covers)

        lines = [
            f"当前第 {page}/{search_page.get('page_count', 1)} 页，共 {search_page.get('total', len(items))} 条"
        ]
        for idx, item in enumerate(items[:10], 1):
            aid = str(item.get("id", ""))
            title = str(item.get("title", ""))
            tags = list(item.get("tags", []) or [])
            pcount = int(item.get("page_count", 0) or 0)
            ptext = f" [{pcount}P]" if pcount > 0 else ""
            tag_text = f" | 标签: {' '.join(tags[:6])}" if tags else ""
            lines.append(f"[{idx}] {aid} - {title}{ptext}{tag_text}")
        lines.append("")
        lines.append("发送 看jm [序号] 或 看jm [id]")
        total_pages = int(search_page.get("page_count", 1) or 1)
        nav = []
        if page > 1:
            nav.append(f"上一页: 搜jm {query} {page - 1}")
        if total_pages > page:
            nav.append(f"下一页: 搜jm {query} {page + 1}")
        if nav:
            lines.append(" | ".join(nav))
        await self._send_search_forward(event, "\n".join(lines), combined)

    async def _handle_view_album(self, event: AstrMessageEvent, token: str):
        album_id = self._resolve_album_id(self._get_session_key(event), token)
        if not album_id:
            await self._send_plain(event, "未找到对应编号，请先查jm，或直接填漫画id")
            return

        try:
            album = await asyncio.to_thread(get_album_detail, self.config, album_id)
            if int(album.get("chapter_count", 0)) <= 1:
                await self._send_plain(
                    event,
                    f"这本只有 1 个章节，直接开始下载: {album_id}\n"
                    + self._runtime_hint(),
                )
                album_lock = self._acquire_album(album_id)
                try:
                    ret = await self._download(album_id)
                finally:
                    album_lock.release()
                await self._send_pdf_files(event, ret, album_id)
                return

            self._store_pending_selection(event, album)
            await self._send_chapter_selection_prompt(event, album)
        except Exception as e:
            logger.exception("获取 JM 章节信息失败")
            await self._send_plain(event, f"读取章节失败: {e}")

    def _get_download_limits(self) -> tuple[int, int]:
        interaction = self.config.get("interaction", {}) or {}
        max_images = int(interaction.get("max_download_images", 120) or 120)
        max_chapters = int(interaction.get("max_download_chapters", 3) or 3)
        return max_images, max_chapters

    async def _handle_select_chapters(
        self, event: AstrMessageEvent, token: str, raw_selection: str
    ):
        album_id = self._resolve_album_id(self._get_session_key(event), token)
        if not album_id:
            await self._send_plain(event, "未找到对应编号，请先查jm，或直接填漫画id")
            return

        try:
            album = await asyncio.to_thread(get_album_detail, self.config, album_id)
            picked = self._parse_chapter_selection_input(
                raw_selection, len(album["chapters"])
            )
            chapters = album["chapters"]
            selected = [chapters[idx - 1] for idx in picked]
            selected = await self._prefetch_selected_chapter_stats(album_id, selected)

            max_images, max_chapters = self._get_download_limits()
            if max_chapters > 0 and len(selected) > max_chapters:
                raise RuntimeError(
                    f"本次最多允许下载 {max_chapters} 个章节，请分批发送"
                )

            selected_photo_ids = [item["photo_id"] for item in selected]
            summary = "、".join([f"{item['selection_index']}" for item in selected])
            total_selected_pages = sum(
                int(item.get("page_count", 0) or 0) for item in selected
            )
            detail_lines = []
            for item in selected:
                pcount = int(item.get("page_count", 0) or 0)
                ptext = f" / {pcount}P" if pcount else ""
                detail_lines.append(
                    f"第{item['selection_index']}话 / {item['photo_id']}{ptext} - {item.get('chapter_title', '') or '未命名章节'}"
                )
            preview_text = "\n".join(detail_lines[:8])
            if len(detail_lines) > 8:
                preview_text = preview_text + "\n..."

            message = (
                f"已选择章节: {summary}\n"
                f"共 {len(selected)} 章 / {total_selected_pages}P\n"
                f"开始下载 [{album['album_id']}] {album['title']}\n"
            )
            if preview_text:
                message += preview_text + "\n"
            message += self._runtime_hint()
            await self._send_plain(event, message)
            album_lock = self._acquire_album(album["album_id"])
            try:
                ret = await self._download(album["album_id"], selected_photo_ids)
            finally:
                album_lock.release()
            stats = ret.get("stats", {}) or {}
            total_images = int(stats.get("total_images", 0))
            if max_images > 0 and total_images > max_images:
                raise RuntimeError(
                    f"本次共 {total_images} 张，超过上限 {max_images}，请减少章节后重试"
                )
            await self._send_pdf_files(event, ret, album["album_id"])
        except Exception as e:
            logger.exception("下载指定 JM 章节失败")
            await self._send_plain(event, f"下载失败: {e}")

    async def _handle_single_image(
        self, event: AstrMessageEvent, token: str, chapter: str, page: int
    ):
        album_id = self._resolve_album_id(self._get_session_key(event), token)
        if not album_id:
            await self._send_plain(event, "未找到对应编号，请先查jm，或直接填漫画id")
            return

        await self._send_plain(
            event,
            f"正在提取单张图片: [{album_id}] 第{chapter}章 P{page}\n"
            + self._runtime_hint(),
        )
        temp_dir = None
        album_lock = None
        try:
            album_lock = self._acquire_album(album_id)
            ret = await asyncio.to_thread(download_single_image, self.config, album_id, chapter, page)
            temp_dir = ret.get("temp_dir")
            caption = f"[{ret['album_id']}] 第{ret['chapter_index']}章 第 {ret['page']}P / 共 {ret['total_pages']}P"
            await self._send_plain(event, caption)
            await event.send(event.chain_result([Image(ret["image_file"])]))
        except Exception as e:
            logger.exception("下载 JM 单图失败")
            await self._send_plain(event, f"单图下载失败: {e}")
        finally:
            if album_lock:
                album_lock.release()
            if temp_dir:
                import shutil

                shutil.rmtree(temp_dir, ignore_errors=True)

    async def _download(self, album_id: str, photo_ids: list[str] | None = None):
        return await asyncio.to_thread(download_album_or_photos, self.config, album_id, photo_ids)

    async def _build_pdf(self, album_id: str, title: str, image_paths: list):
        async with self._pdf_build_lock:
            logger.info(f"start pdf build for {album_id}, images={len(image_paths)}")
            result = await asyncio.to_thread(
                generate_album_pdf, self.config, album_id, title, image_paths
            )
            logger.info(f"finish pdf build for {album_id}, pdfs={len(result)}")
            return result

    async def _random(self, query: str):
        return await asyncio.to_thread(get_random_album, self.config, query)

    @filter.command("看jm", alias={"jm_view"})
    async def jm_unified(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        session_key = self._get_session_key(event)
        session_lock = None
        try:
            session_lock = self._acquire_session(session_key)
            self._set_active_task(session_key, "看jm")
            args = self.parse_command(event.message_str)
            if not args:
                await self._send_plain(
                    event,
                    "用法: 看jm [编号或id] / 看jm [编号或id] [章节] / 看jm [编号或id] [章节] P[页码]",
                )
                return

            if len(args) >= 3:
                parsed = self._parse_image_request(args[:3])
                if parsed:
                    await self._handle_single_image(
                        event,
                        str(parsed["token"]),
                        str(parsed["chapter"]),
                        int(parsed["page"]),
                    )
                    return

            if len(args) == 2:
                token, arg2 = args[0], args[1].strip()
                try:
                    self._parse_chapter_selection_input(arg2, 999999)
                    await self._handle_select_chapters(event, token, arg2)
                    return
                except Exception:
                    pass

            if len(args) == 1:
                await self._handle_view_album(event, args[0])
                return

            await self._send_plain(
                event, "看jm 只负责查看和下载，搜索请用 搜jm [关键词]"
            )
            return
        except Exception as e:
            logger.exception("统一 JM 命令失败")
            await self._send_plain(event, f"JM 处理失败: {e}")
        finally:
            self._clear_active_task(session_key)
            if session_lock:
                session_lock.release()

    async def _send_pdf_files(
        self, event: AstrMessageEvent, ret: dict, fallback_album_id: str
    ):
        if ret.get("cached"):
            pdf_files = ret.get("pdf_files", [])
            if not pdf_files:
                await self._send_plain(event, "本地命中缓存但未找到 PDF 文件")
                return
            await self._send_plain(
                event, f"已找到本地缓存，共 {len(pdf_files)} 个 PDF，正在发送..."
            )
        else:
            stats = ret.get("stats", {}) or {}
            total_images = int(stats.get("total_images", 0))
            success_images = int(stats.get("success_images", 0))
            failed_images = int(stats.get("failed_images", 0))

            await self._send_plain(
                event,
                f"下载统计：总计 {total_images} 张，成功 {success_images} 张，失败 {failed_images} 张",
            )
            await self._send_plain(event, "正在排队生成 PDF，请稍候...")

            pdf_files = await self._build_pdf(
                ret["album_id"], ret["title"], ret["image_paths"]
            )
            if not pdf_files:
                await self._send_plain(event, "下载完成但未生成 PDF")
                return
            await self._send_plain(
                event, f"已生成 {len(pdf_files)} 个 PDF，正在发送..."
            )

        for i, p in enumerate(pdf_files, 1):
            orig_name = Path(p).name
            safe_name = f"jm_{ret.get('album_id', fallback_album_id)}_{i}.pdf"

            try:
                await event.send(event.chain_result([File(name=orig_name, file=p)]))
                continue
            except Exception as e1:
                err1 = str(e1)[:220]
                logger.warning(f"send file failed with original name: {e1}")
                await self._send_plain(
                    event,
                    f"文件发送失败（原文件名）：{orig_name}\n原因：{err1}\n正在尝试回退重发...",
                )

            try:
                await event.send(event.chain_result([File(name=safe_name, file=p)]))
            except Exception as e2:
                err2 = str(e2)[:220]
                logger.exception(f"send file failed with safe name too: {e2}")
                await self._send_plain(
                    event,
                    f"文件重发仍失败：{orig_name}\n回退名：{safe_name}\n原因：{err2}",
                )

        await self._send_plain(event, "发送完成～")

    @filter.command("jm清理缓存", alias={"jm_clear_cache"})
    async def clear_jm_runtime(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        try:
            result = await asyncio.to_thread(clear_plugin_runtime_files, self.config)
            await self._send_plain(
                event,
                f"清理完成：下载目录 {result.get('download_dirs', 0)} 项，下载散文件 {result.get('download_files', 0)} 项，封面缓存 {result.get('cover_files', 0)} 项，缓存文件 {result.get('cache_files', 0)} 项，配置文件 {result.get('option_files', 0)} 项，临时文件 {result.get('temp_files', 0)} 项",
            )
        except Exception as e:
            logger.exception("清理 JM 运行缓存失败")
            await self._send_plain(event, f"清理失败: {e}")

    @filter.command("搜jm", alias={"jm_search"})
    async def search_jm(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        session_key = self._get_session_key(event)
        session_lock = None
        try:
            session_lock = self._acquire_session(session_key)
            self._set_active_task(session_key, "搜jm")
            args = self.parse_command(event.message_str)
            if not args:
                await self._send_plain(event, "用法: 搜jm [关键词] [页码(默认1)]")
                return
            page = 1
            q_args = args
            if len(args) >= 2 and args[-1].isdigit() and int(args[-1]) > 0:
                page = int(args[-1])
                q_args = args[:-1]
            query = " ".join(q_args).strip()
            if not query:
                await self._send_plain(event, "用法: 搜jm [关键词] [页码(默认1)]")
                return
            await self._send_plain(
                event, "正在搜索 JM 并抓取封面，请稍候...\n" + self._runtime_hint()
            )
            results = await self._search_with_covers(query, page)
            if not results or not results.get("items"):
                await self._send_plain(event, "没有搜索到结果")
                return
            await self._send_search_preview(event, query, page, results)
        except Exception as e:
            logger.exception("搜 JM 失败")
            await self._send_plain(event, f"搜索失败: {e}")
        finally:
            self._clear_active_task(session_key)
            if session_lock:
                session_lock.release()

    @filter.command("随机jm", alias={"jm_random"})
    async def random_jm(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        if not self.config.get("features", {}).get("open_random_search", False):
            await self._send_plain(
                event, "随机功能未开启，请在配置中打开 features.open_random_search"
            )
            return

        args = self.parse_command(event.message_str)
        query = args[0] if args else ""
        await self._send_plain(event, "正在抽取随机本子...")

        try:
            ret = await self._random(query)
            if not ret:
                await self._send_plain(event, "未找到结果，换个关键词试试")
                return
            aid = ret["id"]
            title = ret["title"]
            await self._send_plain(
                event, f"你今天的随机本子是: [{aid}] {title}\n发送 看jm {aid} 开始下载"
            )
        except Exception as e:
            logger.exception("随机 JM 失败")
            await self._send_plain(event, f"随机失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("jm更新域名", alias={"jm_update_domain"})
    async def jm_update_domain(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        await self._send_plain(event, "正在检测可用域名，请稍候...")
        try:
            ret = await asyncio.to_thread(update_domains, self.config)
            ok_items = [x for x in ret if x.get("status") == "ok"]
            fail_items = [x for x in ret if x.get("status") != "ok"]
            if not ret:
                await self._send_plain(
                    event, "未获取到候选域名，请检查代理或网络连通性"
                )
                return

            ok_items = sorted(
                ok_items,
                key=lambda x: (
                    0 if x.get("verify") == "deep" else 1,
                    int(x.get("latency_ms", 10**9)),
                    x.get("domain", ""),
                ),
            )
            msg = [f"检测完成: 可用 {len(ok_items)} / 不可用 {len(fail_items)}"]
            if ok_items:
                top = []
                for x in ok_items[:20]:
                    d = x.get("domain", "")
                    latency_ms = x.get("latency_ms", "?")
                    a = x.get("attempts", 1)
                    v = x.get("verify", "?")
                    top.append(f"{d} ({latency_ms}ms, 第{a}次成功, {v})")
                msg.append("可用(已按延迟排序):\n" + "\n".join(top))
            await self._send_plain(event, "\n\n".join(msg))
        except Exception as e:
            logger.exception("更新域名失败")
            await self._send_plain(event, f"更新域名失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("jm清空域名", alias={"jm_clear_domain"})
    async def jm_clear_domain(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        event.stop_event()
        try:
            await asyncio.to_thread(clear_domains, self.config)
            await self._send_plain(
                event, "已清空配置中的 domain，后续将自动解析可用域名"
            )
        except Exception as e:
            logger.exception("清空域名失败")
            await self._send_plain(event, f"清空失败: {e}")
