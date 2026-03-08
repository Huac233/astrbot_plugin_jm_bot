import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import yaml

from astrbot.api import logger
from astrbot.api.star import StarTools


def parse_proxy_config(proxy_str: str) -> dict[str, Any]:
    if not proxy_str:
        return {}

    parsed = urlparse(proxy_str)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError("仅支持 HTTP/HTTPS/SOCKS5 代理协议")

    auth = None
    if parsed.username and parsed.password:
        auth = aiohttp.BasicAuth(parsed.username, parsed.password)

    proxy_url = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        proxy_url += f":{parsed.port}"

    return {"url": proxy_url, "auth": auth}




def _load_schema_defaults() -> dict[str, Any]:
    schema_path = Path(__file__).parent.parent / "_conf_schema.json"
    if not schema_path.exists():
        return {}
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取 _conf_schema.json 失败: {e}")
        return {}

    defaults: dict[str, Any] = {}
    for key, meta in data.items():
        if isinstance(meta, dict) and "default" in meta:
            defaults[key] = meta["default"]
    return defaults


def _schema_default(schema_defaults: dict[str, Any], key: str, fallback: Any) -> Any:
    return schema_defaults.get(key, fallback)

def _set_nested(d: dict[str, Any], path: list[str], value: Any):
    cur = d
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = value


def load_config(
    config: dict, config_path: str | Path | None = None, persist: bool = False
) -> dict[str, Any]:
    data_dir = StarTools.get_data_dir("astrbot_plugin_jm_bot")
    yaml_path = Path(config_path) if config_path else data_dir / "config.yaml"
    schema_defaults = _load_schema_defaults()

    yaml_config: dict[str, Any] = {}
    created_default_file = False
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}
    else:
        logger.warning("配置文件不存在，准备创建默认配置")
        created_default_file = True

    int_fields = {
        "request_timeout": ["request", "timeout"],
        "request_max_retries": ["request", "max_retries"],
        "output_pdf_max_pages": ["output", "pdf_max_pages"],
        "output_jpeg_quality": ["output", "jpeg_quality"],
        "output_max_local_albums": ["output", "max_local_albums"],
        "chapter_fold_threshold": ["interaction", "chapter_fold_threshold"],
        "download_image_threads": ["download", "image_threads"],
        "download_photo_threads": ["download", "photo_threads"],
        "interaction_search_page_count_threads": [
            "interaction",
            "search_page_count_threads",
        ],
        "interaction_search_cover_threads": ["interaction", "search_cover_threads"],
        "interaction_max_download_images": ["interaction", "max_download_images"],
        "interaction_max_download_chapters": ["interaction", "max_download_chapters"],
        "interaction_auto_recall_seconds": ["interaction", "auto_recall_seconds"],
        "output_max_local_chapters": ["output", "max_local_chapters"],
        "output_cover_cache_max_files": ["output", "cover_cache_max_files"],
    }
    bool_fields = {
        "request_enabled": ["request", "enabled"],
        "features_open_random_search": ["features", "open_random_search"],
        "features_auto_find_jm": ["features", "auto_find_jm"],
    }
    str_fields = {
        "request_proxies": ["request", "proxies"],
        "output_base_dir": ["output", "base_dir"],
        "output_pdf_password": ["output", "pdf_password"],
        "output_cover_cache_dir": ["output", "cover_cache_dir"],
    }

    for key, val in (config or {}).items():
        if val in (None, ""):
            continue
        if key in int_fields:
            try:
                _set_nested(yaml_config, int_fields[key], int(val))
            except Exception:
                logger.warning(f"配置项 {key}={val} 不是有效整数，跳过")
        elif key in bool_fields:
            if isinstance(val, str):
                b = val.lower() in ("1", "true", "yes", "on")
            else:
                b = bool(val)
            _set_nested(yaml_config, bool_fields[key], b)
        elif key in str_fields:
            _set_nested(yaml_config, str_fields[key], str(val))

    request = yaml_config.setdefault("request", {})
    request.setdefault("enabled", bool(_schema_default(schema_defaults, "request_enabled", False)))
    if request.get("enabled", True):
        request["proxy"] = parse_proxy_config(request.get("proxies", ""))
    else:
        request["proxy"] = {}

    output = yaml_config.setdefault("output", {})
    base = Path(output.get("base_dir") or _schema_default(schema_defaults, "output_base_dir", str(data_dir / "download")))
    output["base_dir"] = str(base)
    # 清理旧字段，避免误导
    output.pop("image_dir", None)
    output.pop("pdf_dir", None)
    output.setdefault("pdf_max_pages", int(_schema_default(schema_defaults, "output_pdf_max_pages", "150")))
    output.setdefault("jpeg_quality", int(_schema_default(schema_defaults, "output_jpeg_quality", "85")))
    output.setdefault("pdf_password", str(_schema_default(schema_defaults, "output_pdf_password", "")))
    output.setdefault("max_local_albums", int(_schema_default(schema_defaults, "output_max_local_albums", "5")))
    output.setdefault("max_local_chapters", int(_schema_default(schema_defaults, "output_max_local_chapters", "0")))
    output.setdefault("cover_cache_dir", str(_schema_default(schema_defaults, "output_cover_cache_dir", str(data_dir / "cover_cache"))))
    output.setdefault("cover_cache_max_files", int(_schema_default(schema_defaults, "output_cover_cache_max_files", "100")))

    try:
        jpeg_quality = int(output.get("jpeg_quality", 85))
    except Exception:
        jpeg_quality = 85
    output["jpeg_quality"] = max(1, min(100, jpeg_quality))

    try:
        mla = int(output.get("max_local_albums", 1))
    except Exception:
        mla = 1
    output["max_local_albums"] = max(0, mla)

    try:
        mlc = int(output.get("max_local_chapters", 0))
    except Exception:
        mlc = 0
    output["max_local_chapters"] = max(0, mlc)

    try:
        mccf = int(output.get("cover_cache_max_files", 200))
    except Exception:
        mccf = 200
    output["cover_cache_max_files"] = max(0, mccf)

    cache = yaml_config.setdefault("cache", {})
    cache_root = base.parent
    cache.setdefault("search_cache_file", str(cache_root / "search_cache.json"))
    cache.setdefault("random_cache_file", str(cache_root / "jm_max_page.json"))
    cache.setdefault(
        "chapter_selection_cache_file", str(cache_root / "chapter_selection_cache.json")
    )
    cache.setdefault("cache_root_dir", str(cache_root))

    features = yaml_config.setdefault("features", {})
    features.setdefault("open_random_search", bool(_schema_default(schema_defaults, "features_open_random_search", True)))
    features.setdefault("auto_find_jm", False)

    download = yaml_config.setdefault("download", {})
    download.setdefault("image_threads", int(_schema_default(schema_defaults, "download_image_threads", "8")))
    download.setdefault("photo_threads", int(_schema_default(schema_defaults, "download_photo_threads", "8")))

    try:
        image_threads = int(download.get("image_threads", 1))
    except Exception:
        image_threads = 1
    download["image_threads"] = max(1, min(16, image_threads))

    try:
        photo_threads = int(download.get("photo_threads", 1))
    except Exception:
        photo_threads = 1
    download["photo_threads"] = max(1, min(8, photo_threads))

    interaction = yaml_config.setdefault("interaction", {})
    interaction.setdefault("chapter_fold_threshold", int(_schema_default(schema_defaults, "chapter_fold_threshold", "20")))
    interaction.setdefault("max_download_images", int(_schema_default(schema_defaults, "interaction_max_download_images", "400")))
    interaction.setdefault("max_download_chapters", int(_schema_default(schema_defaults, "interaction_max_download_chapters", "3")))
    interaction.setdefault("search_page_count_threads", int(_schema_default(schema_defaults, "interaction_search_page_count_threads", "10")))
    interaction.setdefault("search_cover_threads", int(_schema_default(schema_defaults, "interaction_search_cover_threads", "10")))
    interaction.setdefault("chapter_detail_threads", int(_schema_default(schema_defaults, "interaction_chapter_detail_threads", "10")))
    interaction.setdefault("chapter_selection_ttl", 86400)
    interaction.setdefault("auto_recall_seconds", int(_schema_default(schema_defaults, "interaction_auto_recall_seconds", "60")))

    try:
        fold_threshold = int(interaction.get("chapter_fold_threshold", 20))
    except Exception:
        fold_threshold = 20
    interaction["chapter_fold_threshold"] = max(0, min(100, fold_threshold))

    try:
        max_download_images = int(interaction.get("max_download_images", 120))
    except Exception:
        max_download_images = 120
    interaction["max_download_images"] = max(0, min(5000, max_download_images))

    try:
        max_download_chapters = int(interaction.get("max_download_chapters", 3))
    except Exception:
        max_download_chapters = 3
    interaction["max_download_chapters"] = max(0, min(100, max_download_chapters))

    try:
        search_page_count_threads = int(interaction.get("search_page_count_threads", 3))
    except Exception:
        search_page_count_threads = 3
    interaction["search_page_count_threads"] = max(
        1, min(16, search_page_count_threads)
    )

    try:
        search_cover_threads = int(interaction.get("search_cover_threads", 5))
    except Exception:
        search_cover_threads = 5
    interaction["search_cover_threads"] = max(1, min(16, search_cover_threads))

    try:
        chapter_detail_threads = int(interaction.get("chapter_detail_threads", 4))
    except Exception:
        chapter_detail_threads = 4
    interaction["chapter_detail_threads"] = max(1, min(16, chapter_detail_threads))

    try:
        chapter_selection_ttl = int(interaction.get("chapter_selection_ttl", 86400))
    except Exception:
        chapter_selection_ttl = 86400
    interaction["chapter_selection_ttl"] = max(300, min(604800, chapter_selection_ttl))

    try:
        auto_recall_seconds = int(interaction.get("auto_recall_seconds", 60))
    except Exception:
        auto_recall_seconds = 60
    interaction["auto_recall_seconds"] = max(0, min(86400, auto_recall_seconds))

    commands = yaml_config.setdefault("commands", {})
    commands.setdefault("search", "查jm")
    commands.setdefault("view", "看jm")
    commands.setdefault("random", "随机jm")
    commands.setdefault("update_domain", "jm更新域名")
    commands.setdefault("clear_domain", "jm清空域名")

    base.mkdir(parents=True, exist_ok=True)
    Path(cache["search_cache_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cache["chapter_selection_cache_file"]).parent.mkdir(
        parents=True, exist_ok=True
    )
    Path(output["cover_cache_dir"]).mkdir(parents=True, exist_ok=True)

    if persist or created_default_file:
        try:
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(yaml_config, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.warning(f"写入配置文件失败: {e}")

    return yaml_config
