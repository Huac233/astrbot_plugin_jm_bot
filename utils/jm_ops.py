import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jmcomic
import pikepdf
import yaml
from PIL import Image

from astrbot.api import logger

_COVER_CACHE_LOCK = threading.RLock()
_COVER_DOWNLOAD_LOCKS = {}
_COVER_LOCK_GUARD = threading.RLock()
_COVER_CACHE_LAST_CLEAN_TS = 0.0
_OPTION_FILE_LOCK = threading.RLock()


def _plugin_data_root() -> Path:
    return Path("/AstrBot/data/plugin_data/astrbot_plugin_jm_bot").resolve()


def _is_safe_runtime_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(_plugin_data_root())
        return True
    except Exception:
        return False


def _purge_cover_download_locks(max_entries: int = 512):
    with _COVER_LOCK_GUARD:
        if len(_COVER_DOWNLOAD_LOCKS) <= max_entries:
            return
        for key in list(_COVER_DOWNLOAD_LOCKS.keys())[:-max_entries]:
            _COVER_DOWNLOAD_LOCKS.pop(key, None)


_ALLOWED_OPTION_ROOT_KEYS = {
    "version",
    "debug",
    "dir_rule",
    "client",
    "plugin",
    "download",
    "log",
}


def _sanitize_option_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _ALLOWED_OPTION_ROOT_KEYS}


def _get_cover_download_lock(key: str):
    with _COVER_LOCK_GUARD:
        if key not in _COVER_DOWNLOAD_LOCKS:
            _COVER_DOWNLOAD_LOCKS[key] = threading.RLock()
            _purge_cover_download_locks()
        return _COVER_DOWNLOAD_LOCKS[key]


def _is_valid_image_file(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            return False
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _future_timeout_seconds(config: dict[str, Any], minimum: int = 15) -> int:
    request = config.get("request", {}) or {}
    try:
        timeout = int(request.get("timeout", 15) or 15)
    except Exception:
        timeout = 15
    try:
        retries = int(request.get("max_retries", 3) or 3)
    except Exception:
        retries = 3
    retries = max(1, retries)
    timeout = max(1, timeout)
    return max(minimum, timeout * retries + 5)


def _proxy_map(config: dict[str, Any]) -> dict[str, str]:
    req = config.get("request", {}) or {}
    enabled = bool(req.get("enabled", True))
    if not enabled:
        return {}
    px = str(req.get("proxies", "") or "").strip()
    if not px:
        return {}
    return {"http": px, "https": px}


def _build_option(config: dict[str, Any]) -> jmcomic.JmOption:
    option_path = _jm_option_path(config)
    if not option_path.exists():
        ensure_jm_option_file(config)
    return jmcomic.JmOption.construct(read_jm_option_data(config))


def _new_client(
    config: dict[str, Any],
    impl: str = "api",
    domain_list: list[str] | None = None,
) -> Any:
    option = _build_option(config)
    kwargs: dict[str, Any] = {"impl": impl}
    if domain_list:
        kwargs["domain_list"] = domain_list
    return option.new_jm_client(**kwargs)


def _jm_option_path(config: dict[str, Any]) -> Path:
    base_dir = Path(config["output"]["base_dir"])
    root_dir = base_dir.parent
    root_dir.mkdir(parents=True, exist_ok=True)
    return root_dir / "jm_option.yml"


def _default_jm_option_data(config: dict[str, Any]) -> dict[str, Any]:
    base_dir = Path(config["output"]["base_dir"])
    option_data: dict[str, Any] = {
        "version": "2.0",
        "dir_rule": {
            "base_dir": str(base_dir),
            "rule": "Bd_Aid_Pindex",
        },
        "client": {
            "impl": "api",
        },
        "download": {
            "cache": True,
            "image": {
                "decode": True,
                "suffix": ".jpg",
            },
            "threading": {
                "image": int((config.get("download", {}) or {}).get("image_threads", 1) or 1),
                "photo": int((config.get("download", {}) or {}).get("photo_threads", 1) or 1),
            },
        },
        "log": True,
    }

    req = config.get("request", {}) or {}
    enabled = bool(req.get("enabled", True))
    px = str(req.get("proxies", "") or "").strip() if enabled else ""
    if px:
        option_data["client"]["postman"] = {
            "meta_data": {"proxies": {"http": px, "https": px}}
        }
    return option_data


def read_jm_option_data(config: dict[str, Any]) -> dict[str, Any]:
    option_path = _jm_option_path(config)
    with _OPTION_FILE_LOCK:
        if option_path.exists():
            try:
                data = _sanitize_option_data(yaml.safe_load(option_path.read_text(encoding="utf-8")) or {})
            except Exception:
                data = {}
        else:
            data = {}

    merged = _default_jm_option_data(config)
    for key, value in data.items():
        if key == "client" and isinstance(value, dict):
            merged.setdefault("client", {}).update(value)
        elif key == "download" and isinstance(value, dict):
            merged.setdefault("download", {}).update(value)
            if isinstance(value.get("image"), dict):
                merged["download"].setdefault("image", {}).update(value["image"])
            if isinstance(value.get("threading"), dict):
                merged["download"].setdefault("threading", {}).update(value["threading"])
        elif key == "dir_rule" and isinstance(value, dict):
            merged.setdefault("dir_rule", {}).update(value)
        else:
            merged[key] = value

    merged["dir_rule"]["base_dir"] = str(Path(config["output"]["base_dir"]))
    merged["dir_rule"]["rule"] = "Bd_Aid_Pindex"
    merged.setdefault("client", {})
    merged["client"].setdefault("impl", "api")
    merged.setdefault("download", {})
    merged["download"].setdefault("cache", True)
    merged["download"].setdefault("image", {})
    merged["download"]["image"].setdefault("decode", True)
    merged["download"]["image"].setdefault("suffix", ".jpg")
    merged["download"].setdefault("threading", {})
    merged["download"]["threading"]["image"] = int((config.get("download", {}) or {}).get("image_threads", 1) or 1)
    merged["download"]["threading"]["photo"] = int((config.get("download", {}) or {}).get("photo_threads", 1) or 1)

    req = config.get("request", {}) or {}
    enabled = bool(req.get("enabled", True))
    px = str(req.get("proxies", "") or "").strip() if enabled else ""
    if px:
        merged["client"]["postman"] = {
            "meta_data": {"proxies": {"http": px, "https": px}}
        }
    else:
        merged["client"].pop("postman", None)

    return _sanitize_option_data(merged)


def write_jm_option_data(config: dict[str, Any], data: dict[str, Any]) -> str:
    option_path = _jm_option_path(config)
    cleaned = _sanitize_option_data(data)
    with _OPTION_FILE_LOCK:
        with open(option_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cleaned, f, allow_unicode=True, sort_keys=False)
    return str(option_path)


def ensure_jm_option_file(config: dict[str, Any]) -> str:
    data = read_jm_option_data(config)
    return write_jm_option_data(config, data)


def _enforce_max_local_albums(config: dict[str, Any]):
    global _COVER_CACHE_LAST_CLEAN_TS
    output = config.get("output", {}) or {}
    max_local = int(output.get("max_local_albums", 1) or 0)
    if max_local <= 0:
        return

    base = Path(config["output"]["base_dir"])
    base.mkdir(parents=True, exist_ok=True)

    dirs = [d for d in base.iterdir() if d.is_dir()]
    # 仅统计像 album 目录的文件夹（纯数字目录）
    dirs = [d for d in dirs if d.name.isdigit()]
    if len(dirs) < max_local:
        return

    dirs.sort(key=lambda d: d.stat().st_mtime)
    oldest = dirs[0]
    import shutil

    shutil.rmtree(oldest, ignore_errors=True)
    logger.info(
        f"max_local_albums reached({max_local}), removed oldest album dir: {oldest}"
    )


def _enforce_cover_cache_max_files(config: dict[str, Any], force: bool = False):
    global _COVER_CACHE_LAST_CLEAN_TS
    output = config.get("output", {}) or {}
    max_files = int(output.get("cover_cache_max_files", 200) or 0)
    if max_files <= 0:
        return

    now = time.time()
    if not force and now - _COVER_CACHE_LAST_CLEAN_TS < 30:
        return

    cover_dir = Path(output.get("cover_cache_dir", ""))
    cover_dir.mkdir(parents=True, exist_ok=True)
    files = [p for p in cover_dir.iterdir() if p.is_file()]
    if len(files) < max_files:
        _COVER_CACHE_LAST_CLEAN_TS = now
        return

    files.sort(key=lambda p: p.stat().st_mtime)
    remove_count = len(files) - max_files + 1
    for old in files[:remove_count]:
        old.unlink(missing_ok=True)
        logger.info(
            f"cover_cache_max_files reached({max_files}), removed cover cache: {old}"
        )
    _COVER_CACHE_LAST_CLEAN_TS = now


def _enforce_max_local_chapters(config: dict[str, Any], album_id: str):
    global _COVER_CACHE_LAST_CLEAN_TS
    output = config.get("output", {}) or {}
    max_local = int(output.get("max_local_chapters", 0) or 0)
    if max_local <= 0:
        return

    album_dir = Path(config["output"]["base_dir"]) / str(album_id)
    if not album_dir.exists() or not album_dir.is_dir():
        return

    chapter_dirs = [d for d in album_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    if len(chapter_dirs) <= max_local:
        return

    chapter_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for old in chapter_dirs[max_local:]:
        shutil.rmtree(old, ignore_errors=True)
        logger.info(
            f"max_local_chapters reached({max_local}), removed chapter dir: {old}"
        )


def _get_photo_detail_by_chapter(config: dict[str, Any], album_id: str, chapter: str):
    client = _new_client(config, impl="api")
    album = client.get_album_detail(album_id)

    photo = None
    chapter_text = str(chapter).strip()
    episodes = list(getattr(album, "episode_list", []) or [])

    if episodes:
        for idx, episode in enumerate(episodes, 1):
            photo_id = str(episode[0])
            chapter_index = str(episode[1]) if len(episode) >= 2 else str(idx)
            if chapter_text in {str(idx), chapter_index, photo_id}:
                photo = client.get_photo_detail(
                    photo_id, fetch_album=False, fetch_scramble_id=True
                )
                photo.from_album = album
                break
    else:
        photo = client.get_photo_detail(
            str(getattr(album, "album_id", album_id)),
            fetch_album=False,
            fetch_scramble_id=True,
        )
        photo.from_album = album

    if photo is None:
        raise RuntimeError("未找到对应章节")

    return album, photo


def get_album_page_stats(
    config: dict[str, Any],
    album_id: str,
    selected_photo_ids: list[str] | None = None,
    concurrency: int = 4,
) -> dict[str, Any]:
    client = _new_client(config, impl="api")
    album = client.get_album_detail(album_id)

    selected_set = {str(pid) for pid in (selected_photo_ids or []) if str(pid).strip()}
    chapters = []
    total_pages = 0
    episodes = list(getattr(album, "episode_list", []) or [])
    if episodes:
        filtered = []
        for idx, episode in enumerate(episodes, 1):
            photo_id = str(episode[0])
            if selected_set and photo_id not in selected_set:
                continue
            chapter_index = str(episode[1]) if len(episode) >= 2 else str(idx)
            chapter_title = str(episode[2]) if len(episode) >= 3 else ""
            filtered.append((idx, photo_id, chapter_index, chapter_title))

        max_workers = max(1, min(16, int(concurrency or 4), len(filtered) or 1))

        def fetch_one(row):
            idx, photo_id, chapter_index, chapter_title = row
            local_client = _new_client(config, impl="api")
            photo = local_client.get_photo_detail(
                photo_id, fetch_album=False, fetch_scramble_id=True
            )
            pages = len(photo)
            return {
                "selection_index": idx,
                "photo_id": photo_id,
                "chapter_index": chapter_index,
                "chapter_title": chapter_title.strip(),
                "page_count": pages,
            }

        if max_workers <= 1 or len(filtered) <= 1:
            chapters = [fetch_one(row) for row in filtered]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(fetch_one, row) for row in filtered]
                chapters = [future.result(timeout=_future_timeout_seconds(config, minimum=20)) for future in futures]
        chapters.sort(key=lambda item: int(item.get("selection_index", 10**9)))
        total_pages = sum(int(item.get("page_count", 0) or 0) for item in chapters)
    else:
        photo = client.get_photo_detail(
            str(getattr(album, "album_id", album_id)),
            fetch_album=False,
            fetch_scramble_id=True,
        )
        pages = len(photo)
        total_pages = pages
        chapters.append(
            {
                "selection_index": 1,
                "photo_id": str(getattr(photo, "photo_id", album_id)),
                "chapter_index": "1",
                "chapter_title": str(getattr(album, "name", album_id)).strip(),
                "page_count": pages,
            }
        )

    return {
        "album_id": str(getattr(album, "album_id", album_id)),
        "title": sanitize_filename(str(getattr(album, "name", album_id))),
        "chapter_count": len(chapters),
        "total_pages": total_pages,
        "chapters": chapters,
    }


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip()
    return name[:max_len] if len(name) > max_len else name


def encrypt_pdf(input_pdf: str, output_pdf: str, password: str):
    with pikepdf.open(input_pdf) as pdf:
        pdf.save(
            output_pdf, encryption=pikepdf.Encryption(owner=password, user=password)
        )


def images_to_pdf_chunks(
    image_paths: list[str],
    output_dir: Path,
    pdf_name: str,
    pdf_max_pages: int,
    pdf_password: str,
    jpeg_quality: int,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    total_pages = len(image_paths)
    if total_pages == 0:
        return []

    chunk_size = pdf_max_pages if pdf_max_pages > 0 else total_pages
    pdf_files: list[str] = []

    for idx, start in enumerate(range(0, total_pages, chunk_size), 1):
        chunk = image_paths[start : start + chunk_size]
        temp_pdf = output_dir / f".__temp_{pdf_name}_{idx}.pdf"
        final_pdf = output_dir / (
            f"{pdf_name}-{idx}.pdf" if total_pages > chunk_size else f"{pdf_name}.pdf"
        )

        temp_page_files: list[Path] = []
        try:
            for page_no, image_path in enumerate(chunk, 1):
                page_pdf = output_dir / f".__page_{idx}_{page_no}.pdf"
                with Image.open(image_path) as im:
                    rgb = im.convert("RGB")
                    try:
                        rgb.save(str(page_pdf), format="PDF", quality=jpeg_quality)
                    finally:
                        rgb.close()
                temp_page_files.append(page_pdf)

            merged_pdf = pikepdf.Pdf.new()
            try:
                for page_pdf in temp_page_files:
                    with pikepdf.open(str(page_pdf)) as one_pdf:
                        merged_pdf.pages.extend(one_pdf.pages)
                merged_pdf.save(str(temp_pdf))
            finally:
                merged_pdf.close()

            if pdf_password:
                encrypt_pdf(str(temp_pdf), str(final_pdf), pdf_password)
                if temp_pdf.exists():
                    temp_pdf.unlink()
            else:
                os.replace(str(temp_pdf), str(final_pdf))

            pdf_files.append(str(final_pdf))
        finally:
            if temp_pdf.exists():
                temp_pdf.unlink(missing_ok=True)
            for page_pdf in temp_page_files:
                page_pdf.unlink(missing_ok=True)

    return pdf_files


def build_album_image_list(base_dir: Path, album_id: str) -> list[str]:
    album_dir = base_dir / str(album_id)
    if not album_dir.exists():
        return []

    image_paths: list[str] = []

    for chapter in sorted(
        album_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 10**9
    ):
        if not chapter.is_dir():
            continue
        files = sorted(
            chapter.iterdir(),
            key=lambda p: (
                int(re.search(r"\d+", p.name).group())
                if re.search(r"\d+", p.name)
                else 10**9
            ),
        )
        for f in files:
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                image_paths.append(str(f))
    return image_paths


def find_existing_pdfs_in_album(album_dir: Path) -> list[str]:
    if not album_dir.exists() or not album_dir.is_dir():
        return []
    return sorted(
        [
            str(p)
            for p in album_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        ]
    )


def _collect_download_stats(dler, image_paths: list[str]) -> dict[str, int]:
    success_images = len(image_paths)
    failed_images = len(getattr(dler, "download_failed_image", []) or [])

    failed_photo_images = 0
    for item in getattr(dler, "download_failed_photo", []) or []:
        photo = item[0] if isinstance(item, tuple) and item else item
        try:
            failed_photo_images += int(len(photo))
        except Exception:
            failed_photo_images += 1

    failed_images += failed_photo_images
    total_images = success_images + failed_images

    return {
        "total_images": int(total_images),
        "success_images": int(success_images),
        "failed_images": int(failed_images),
        "failed_image_tasks": int(
            len(getattr(dler, "download_failed_image", []) or [])
        ),
        "failed_photo_tasks": int(
            len(getattr(dler, "download_failed_photo", []) or [])
        ),
    }


def download_album_images(config: dict[str, Any], album_id: str) -> dict[str, Any]:
    out_base = Path(config["output"]["base_dir"])
    existing = find_existing_pdfs_in_album(out_base / str(album_id))
    if existing:
        return {
            "album_id": album_id,
            "title": str(album_id),
            "cached": True,
            "pdf_files": existing,
        }

    _enforce_max_local_albums(config)

    option = jmcomic.JmOption.construct(read_jm_option_data(config))
    album, dler = jmcomic.download_album(album_id, option, check_exception=False)

    real_id = str(album.album_id)
    title = sanitize_filename(getattr(album, "name", real_id))

    image_paths = build_album_image_list(out_base, real_id)
    stats = _collect_download_stats(dler, image_paths)

    if not image_paths:
        raise RuntimeError("下载完成但未找到图片文件")

    return {
        "album_id": real_id,
        "title": title,
        "cached": False,
        "image_paths": image_paths,
        "stats": stats,
    }


def generate_album_pdf(
    config: dict[str, Any], album_id: str, title: str, image_paths: list[str]
) -> list[str]:
    out_base = Path(config["output"]["base_dir"]) / str(album_id)
    return images_to_pdf_chunks(
        image_paths=image_paths,
        output_dir=out_base,
        pdf_name=title,
        pdf_max_pages=int(config["output"].get("pdf_max_pages", 200)),
        pdf_password=str(config["output"].get("pdf_password", "")),
        jpeg_quality=int(config["output"].get("jpeg_quality", 85)),
    )


def download_album_to_pdf(config: dict[str, Any], album_id: str) -> dict[str, Any]:
    prepared = download_album_images(config, album_id)
    if prepared.get("cached"):
        return prepared

    pdf_files = generate_album_pdf(
        config, prepared["album_id"], prepared["title"], prepared["image_paths"]
    )
    prepared["pdf_files"] = pdf_files
    return prepared


def search_album(
    config: dict[str, Any], query: str, page: int = 1
) -> list[dict[str, Any]]:
    client = _new_client(config, impl="api")

    tags = re.sub(r"[，,]+", " ", query).strip()
    result = client.search_site(search_query=tags, page=page)
    arr = []
    idx = 1
    for aid, title in result:
        arr.append({"index": idx, "id": str(aid), "title": str(title)})
        idx += 1
    return arr


def get_random_album(
    config: dict[str, Any], query: str = ""
) -> dict[str, Any] | None:
    import random

    client = _new_client(config, impl="api")

    tags = re.sub(r"[，,]+", " ", query).strip() if query else ""

    cache_file = Path(config["cache"]["random_cache_file"])
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    cache_data: dict[str, Any] = {}
    if cache_file.exists():
        try:
            cache_data = json.loads(cache_file.read_text(encoding="utf-8") or "{}")
        except Exception:
            cache_data = {}

    max_page = 0
    if tags in cache_data:
        info = cache_data[tags]
        ts = info.get("timestamp")
        if ts:
            dt = datetime.fromisoformat(ts)
            if datetime.now() - dt <= timedelta(hours=24):
                max_page = int(info.get("max_page", 0))

    if max_page <= 0:
        result = client.search_site(search_query=tags, page=1)
        lst = list(result.iter_id_title())
        if not lst:
            return None
        last_id = lst[-1][0]

        low, high = 1, 6000
        while low < high:
            mid = (low + high) // 2
            res = client.search_site(search_query=tags, page=mid)
            ls = list(res.iter_id_title()) if res else []
            if not ls:
                high = mid
                continue
            if ls[-1][0] == last_id:
                high = mid
            else:
                low = mid + 1
        max_page = low
        cache_data[tags] = {
            "max_page": max_page,
            "timestamp": datetime.now().isoformat(),
        }
        cache_file.write_text(
            json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if max_page <= 0:
        return None

    p = random.randint(1, max_page)
    res = client.search_site(search_query=tags, page=p)
    arr = list(res.iter_id_title())
    if not arr:
        return None
    aid, title = random.choice(arr)
    return {"id": str(aid), "title": str(title), "page": p, "max_page": max_page}


def update_domains(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_jm_option_file(config)

    req = config.get("request", {}) or {}
    timeout = int(req.get("timeout", 15) or 15)
    max_retries = int(req.get("max_retries", 2) or 2)
    probe_attempts = max(1, max_retries + 1)

    pm = _proxy_map(config)
    postman = jmcomic.JmModuleConfig.new_postman(
        session=True,
        proxies=pm or None,
        timeout=timeout,
    )

    domain_set = set()

    # 1) 官方域名接口
    try:
        lst = jmcomic.JmModuleConfig.get_html_domain_all(postman=postman)
        for d in lst or []:
            try:
                d = jmcomic.JmcomicText.parse_to_jm_domain(str(d))
            except Exception:
                d = str(d).strip()
            if d and not d.startswith("jm365") and "." in d:
                domain_set.add(d)
    except Exception as e:
        logger.warning(f"get_html_domain_all failed: {e}")

    # 2) github 兜底域名
    if not domain_set:
        try:
            s2 = jmcomic.JmModuleConfig.get_html_domain_all_via_github(postman=postman)
            for d in s2 or []:
                try:
                    d = jmcomic.JmcomicText.parse_to_jm_domain(str(d))
                except Exception:
                    d = str(d).strip()
                if d and not d.startswith("jm365") and "." in d:
                    domain_set.add(d)
        except Exception as e:
            logger.warning(f"get_html_domain_all_via_github failed: {e}")

    cleaned: list[str] = []
    for d in sorted(domain_set):
        d = d.strip().split("/")[0]
        if d.startswith("t.me"):
            continue
        if d and "." in d:
            cleaned.append(d)

    def probe_domain(domain: str) -> dict[str, Any]:
        best_latency_ms = None
        last_error = ""

        # 第1层：轻量连通性检测（兼容历史策略，403 视为可用）
        precheck_ok = False
        precheck_latency = None
        for i in range(probe_attempts):
            t0 = time.perf_counter()
            try:
                r = postman.get(
                    f"https://{domain}", allow_redirects=False, timeout=timeout
                )
                code = int(getattr(r, "status_code", 0) or 0)
                precheck_latency = int((time.perf_counter() - t0) * 1000)
                if code in (200, 301, 302, 307, 308, 403):
                    precheck_ok = True
                    best_latency_ms = precheck_latency
                    break
                last_error = f"precheck_status={code}"
            except Exception as e:
                last_error = str(e)

        if not precheck_ok:
            return {
                "domain": domain,
                "status": "fail",
                "latency_ms": -1,
                "attempts": probe_attempts,
                "error": last_error[:200],
            }

        # 第2层：深度可用性检测（不通过也不直接判死，避免误杀）
        for i in range(probe_attempts):
            t0 = time.perf_counter()
            try:
                client = _new_client(config, impl="html", domain_list=[domain])
                client.get_album_detail("123456")
                latency_ms = int((time.perf_counter() - t0) * 1000)
                best_latency_ms = (
                    min(best_latency_ms, latency_ms)
                    if best_latency_ms is not None
                    else latency_ms
                )
                return {
                    "domain": domain,
                    "status": "ok",
                    "latency_ms": best_latency_ms,
                    "attempts": i + 1,
                    "verify": "deep",
                }
            except Exception as e:
                last_error = str(e)

        return {
            "domain": domain,
            "status": "ok",
            "latency_ms": int(best_latency_ms if best_latency_ms is not None else 9999),
            "attempts": probe_attempts,
            "verify": "precheck",
            "error": last_error[:200],
        }

    results: list[dict[str, Any]] = []
    if cleaned:
        workers = min(8, max(1, len(cleaned)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(probe_domain, d): d for d in cleaned}
            for fut in as_completed(fut_map):
                try:
                    results.append(fut.result())
                except Exception as e:
                    d = fut_map[fut]
                    results.append(
                        {
                            "domain": d,
                            "status": "fail",
                            "latency_ms": -1,
                            "attempts": probe_attempts,
                            "error": str(e)[:200],
                        }
                    )

    ok_results = [r for r in results if r.get("status") == "ok"]
    ok_results.sort(
        key=lambda r: (
            0 if r.get("verify") == "deep" else 1,
            int(r.get("latency_ms", 10**9)),
            r.get("domain", ""),
        )
    )
    ok_domains = [r["domain"] for r in ok_results]

    data = read_jm_option_data(config)
    data.setdefault("client", {})
    data.setdefault("client", {}).setdefault("domain", {})

    # 仅当本次探测有可用域名时才覆盖；否则保留旧配置，避免“更新后全空”
    if ok_domains:
        data["client"]["domain"]["html"] = ok_domains
        write_jm_option_data(config, data)

    return results


def clear_domains(config: dict[str, Any]):
    data = read_jm_option_data(config)
    client = data.setdefault("client", {})
    client.pop("domain", None)
    client["impl"] = "api"
    write_jm_option_data(config, data)


def get_album_detail(config: dict[str, Any], album_id: str) -> dict[str, Any]:
    stats = get_album_page_stats(config, album_id)
    chapters = []
    for item in stats.get("chapters", []) or []:
        chapters.append(
            {
                "selection_index": int(item.get("selection_index", 1) or 1),
                "photo_id": str(item.get("photo_id", "")),
                "chapter_index": str(item.get("chapter_index", "")),
                "chapter_title": str(item.get("chapter_title", "") or "").strip(),
                "page_count": int(item.get("page_count", 0) or 0),
            }
        )

    return {
        "album_id": str(stats.get("album_id", album_id)),
        "title": sanitize_filename(str(stats.get("title", album_id))),
        "author": "",
        "chapter_count": int(
            stats.get("chapter_count", len(chapters)) or len(chapters)
        ),
        "total_pages": int(stats.get("total_pages", 0) or 0),
        "chapters": chapters,
    }


def _collect_downloaded_image_paths(
    base_dir: Path, album_id: str, downloaders: list[Any]
) -> list[str]:
    album_dir = base_dir / str(album_id)
    image_paths: list[str] = []
    seen = set()

    for dler in downloaders or []:
        for attr in ("all_downloaded", "download_success_dict", "downloaded_dict"):
            value = getattr(dler, attr, None)
            if isinstance(value, dict):
                for path in value.values():
                    try:
                        p = Path(str(path))
                        if (
                            p.exists()
                            and p.is_file()
                            and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                        ):
                            sp = str(p)
                            if sp not in seen:
                                seen.add(sp)
                                image_paths.append(sp)
                    except Exception:
                        pass

    if image_paths:
        return sorted(image_paths, key=lambda s: [int(x) if x.isdigit() else x.lower() for x in re.findall(r"\d+|\D+", Path(s).name)])

    if not album_dir.exists():
        return []

    chapter_dirs = [d for d in album_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    chapter_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for chapter in chapter_dirs:
        files = sorted(
            chapter.iterdir(),
            key=lambda p: (
                int(re.search(r"\d+", p.name).group())
                if re.search(r"\d+", p.name)
                else 10**9
            ),
        )
        current = [
            str(f)
            for f in files
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        ]
        if current:
            image_paths.extend(current)
            break
    return image_paths


def build_selected_image_list(
    base_dir: Path, album_id: str, selected_chapter_dirs: list[str]
) -> list[str]:
    album_dir = base_dir / str(album_id)
    if not album_dir.exists():
        return []

    selected = {str(pid) for pid in selected_chapter_dirs}
    image_paths: list[str] = []
    for chapter in sorted(
        album_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 10**9
    ):
        if not chapter.is_dir() or chapter.name not in selected:
            continue
        files = sorted(
            chapter.iterdir(),
            key=lambda p: (
                int(re.search(r"\d+", p.name).group())
                if re.search(r"\d+", p.name)
                else 10**9
            ),
        )
        for f in files:
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                image_paths.append(str(f))
    return image_paths


def download_album_or_photos(
    config: dict[str, Any], album_id: str, photo_ids: list[str] | None = None
) -> dict[str, Any]:
    out_base = Path(config["output"]["base_dir"])
    selected_photo_ids = [str(pid) for pid in (photo_ids or []) if str(pid).strip()]

    _enforce_max_local_albums(config)

    option = _build_option(config)

    if selected_photo_ids:
        photos = []
        downloaders = []
        for photo_id in selected_photo_ids:
            photo, dler = jmcomic.download_photo(
                photo_id, option, check_exception=False
            )
            photos.append(photo)
            downloaders.append(dler)

        if not photos:
            raise RuntimeError("章节下载完成但未返回章节信息")

        first_photo = photos[0]
        real_album_id = str(getattr(first_photo, "album_id", album_id))
        title = sanitize_filename(
            str(
                getattr(getattr(first_photo, "from_album", None), "name", real_album_id)
            )
        )
        selected_chapter_dirs = [
            str(getattr(photo, "album_index", "")) for photo in photos
        ]
        image_paths = build_selected_image_list(
            out_base, real_album_id, selected_chapter_dirs
        )
        if not image_paths:
            image_paths = _collect_downloaded_image_paths(
                out_base, real_album_id, downloaders
            )

        total_images = success_images = failed_images = failed_image_tasks = (
            failed_photo_tasks
        ) = 0
        for dler in downloaders:
            stats = _collect_download_stats(dler, [])
            failed_images += int(stats.get("failed_images", 0))
            failed_image_tasks += int(stats.get("failed_image_tasks", 0))
            failed_photo_tasks += int(stats.get("failed_photo_tasks", 0))
        success_images = len(image_paths)
        total_images = success_images + failed_images

        if not image_paths:
            raise RuntimeError("章节下载完成但未找到图片文件")

        _enforce_max_local_chapters(config, real_album_id)

        return {
            "album_id": real_album_id,
            "title": title,
            "cached": False,
            "image_paths": image_paths,
            "stats": {
                "total_images": total_images,
                "success_images": success_images,
                "failed_images": failed_images,
                "failed_image_tasks": failed_image_tasks,
                "failed_photo_tasks": failed_photo_tasks,
            },
            "selected_photo_ids": selected_photo_ids,
            "selected_chapter_dirs": selected_chapter_dirs,
        }

    ret = download_album_images(config, album_id)
    _enforce_max_local_chapters(config, ret.get("album_id", album_id))
    return ret


def download_single_image(
    config: dict[str, Any], album_id: str, chapter: str, page: int
) -> dict[str, Any]:
    option = _build_option(config)
    album, photo = _get_photo_detail_by_chapter(config, album_id, chapter)

    total_pages = len(photo)
    if page < 1 or page > total_pages:
        raise RuntimeError(f"页码超出范围，当前章节共有 {total_pages} 张")

    image = photo[page - 1]
    save_path = Path(option.decide_image_filepath(image))
    save_path.parent.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="jm_single_", dir=str(Path(config["output"]["base_dir"]).parent)
        )
    )
    logger.info(
        f"single_image target: album={album_id}, chapter={chapter}, page={page}, save_path={save_path}"
    )

    try:
        client = option.build_jm_client()
        decode_image = option.decide_download_image_decode(image)
        client.download_by_image_detail(
            image, str(save_path), decode_image=decode_image
        )

        if not save_path.exists():
            raise RuntimeError(f"图片下载失败或文件未落盘: {save_path}")

        ext = save_path.suffix or ".jpg"
        safe_title = sanitize_filename(str(getattr(album, "name", album_id)))
        out = temp_dir / f"jm_{album_id}_c{photo.album_index}_p{page}{ext}"
        shutil.copy2(save_path, out)
        return {
            "album_id": str(getattr(album, "album_id", album_id)),
            "title": safe_title,
            "photo_id": str(getattr(photo, "photo_id", "")),
            "chapter_index": int(getattr(photo, "album_index", 1)),
            "page": int(page),
            "total_pages": int(total_pages),
            "image_file": str(out),
            "temp_dir": str(temp_dir),
            "save_path": str(save_path),
        }
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def get_search_page(
    config: dict[str, Any], query: str, page: int = 1
) -> dict[str, Any]:
    client = _new_client(config, impl="api")

    tags = re.sub(r"[，,]+", " ", query).strip()
    result = client.search_site(search_query=tags, page=page)
    items = []
    for idx, (aid, title, tag_list) in enumerate(result.iter_id_title_tag(), 1):
        items.append(
            {
                "index": idx,
                "id": str(aid),
                "title": str(title),
                "tags": list(tag_list or []),
            }
        )
    return {
        "items": items,
        "page": page,
        "page_count": int(getattr(result, "page_count", 1) or 1),
        "total": int(getattr(result, "total", len(items)) or len(items)),
    }


async def cache_cover_image(
    config: dict[str, Any], album_id: str, size: str = "_3x4"
) -> str:
    cover_dir = Path((config.get("output", {}) or {}).get("cover_cache_dir", ""))
    cover_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".jpg"
    safe_size = size or "raw"
    target = cover_dir / f"{album_id}{safe_size}{suffix}"

    with _COVER_CACHE_LOCK:
        _enforce_cover_cache_max_files(config)
        if _is_valid_image_file(target):
            return str(target)
        if target.exists():
            target.unlink(missing_ok=True)

    lock = _get_cover_download_lock(str(target))

    def _download_cover_sync() -> str:
        with lock:
            if _is_valid_image_file(target):
                logger.info(
                    f"cover cache hit after lock: album={album_id}, path={target}"
                )
                return str(target)
            if target.exists():
                target.unlink(missing_ok=True)

            client = _new_client(config, impl="api")

            temp_target = target.with_name(
                f"{target.stem}.part.{os.getpid()}.{threading.get_ident()}{target.suffix}"
            )
            temp_target.unlink(missing_ok=True)
            logger.info(
                f"cover download start: album={album_id}, path={target}, temp={temp_target}, size={size}"
            )
            try:
                client.download_album_cover(album_id, str(temp_target), size=size)
                if not _is_valid_image_file(temp_target):
                    raise RuntimeError(f"cover download incomplete: {temp_target}")
                os.replace(temp_target, target)
            except Exception as e:
                logger.warning(
                    f"cover download failed: album={album_id}, path={target}, error={e}"
                )
                raise
            finally:
                if temp_target.exists():
                    temp_target.unlink(missing_ok=True)

            return str(target) if _is_valid_image_file(target) else ""

    return await asyncio.to_thread(_download_cover_sync)


def clear_plugin_runtime_files(config: dict[str, Any]) -> dict[str, int]:
    removed = {
        "download_dirs": 0,
        "download_files": 0,
        "cover_files": 0,
        "cache_files": 0,
        "option_files": 0,
        "temp_files": 0,
    }

    output = config.get("output", {}) or {}
    cache = config.get("cache", {}) or {}
    base_dir = Path(output.get("base_dir", ""))
    cover_dir = Path(output.get("cover_cache_dir", ""))

    if not base_dir or not str(base_dir).strip() or not _is_safe_runtime_path(base_dir):
        raise ValueError(f"拒绝清理非插件数据目录: {base_dir}")
    if cover_dir and str(cover_dir).strip() and not _is_safe_runtime_path(cover_dir):
        raise ValueError(f"拒绝清理非插件数据目录: {cover_dir}")

    if base_dir.exists():
        for item in list(base_dir.iterdir()):
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                    removed["download_dirs"] += 1
                elif item.is_file():
                    item.unlink(missing_ok=True)
                    removed["download_files"] += 1
            except Exception:
                pass

    if cover_dir.exists():
        for item in list(cover_dir.iterdir()):
            try:
                if item.is_file():
                    item.unlink(missing_ok=True)
                    removed["cover_files"] += 1
            except Exception:
                pass

    for key in (
        "search_cache_file",
        "random_cache_file",
        "chapter_selection_cache_file",
    ):
        path = Path(cache.get(key, ""))
        try:
            if path.exists() and path.is_file():
                path.unlink(missing_ok=True)
                removed["cache_files"] += 1
        except Exception:
            pass

    root_dir = base_dir.parent if str(base_dir) else None

    if root_dir and root_dir.exists():
        for item in list(root_dir.iterdir()):
            name = item.name
            try:
                if item.is_dir() and name.startswith("jm_single_"):
                    shutil.rmtree(item, ignore_errors=True)
                    removed["temp_files"] += 1
                elif item.is_file() and (
                    name.startswith(".__temp_")
                    or name.startswith(".__page_")
                    or ".part." in name
                ):
                    item.unlink(missing_ok=True)
                    removed["temp_files"] += 1
            except Exception:
                pass

    return removed


def get_album_brief_pages(config: dict[str, Any], album_id: str) -> int:
    client = _new_client(config, impl="api")
    album = client.get_album_detail(album_id)
    return int(getattr(album, "page_count", 0) or 0)


def get_album_total_pages_fallback(config: dict[str, Any], album_id: str) -> int:
    client = _new_client(config, impl="api")
    album = client.get_album_detail(album_id)
    direct_page_count = int(getattr(album, "page_count", 0) or 0)
    episodes = list(getattr(album, "episode_list", []) or [])
    if not episodes:
        return direct_page_count
    if len(episodes) == 1:
        pid = str(episodes[0][0])
        photo = client.get_photo_detail(pid, fetch_album=False, fetch_scramble_id=True)
        return int(len(photo) or 0)
    total = 0
    for ep in episodes:
        pid = str(ep[0])
        photo = client.get_photo_detail(pid, fetch_album=False, fetch_scramble_id=True)
        total += int(len(photo) or 0)
    return total
