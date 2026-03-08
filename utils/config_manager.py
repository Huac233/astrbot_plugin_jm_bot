import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import yaml

from astrbot.api import logger
from astrbot.api.star import StarTools

INT_FIELDS = {
    "request_timeout": ["request", "timeout"],
    "request_max_retries": ["request", "max_retries"],
    "output_pdf_max_pages": ["output", "pdf_max_pages"],
    "output_jpeg_quality": ["output", "jpeg_quality"],
    "output_max_local_albums": ["output", "max_local_albums"],
    "chapter_fold_threshold": ["interaction", "chapter_fold_threshold"],
    "download_image_threads": ["download", "image_threads"],
    "download_photo_threads": ["download", "photo_threads"],
    "interaction_search_page_count_threads": ["interaction", "search_page_count_threads"],
    "interaction_search_cover_threads": ["interaction", "search_cover_threads"],
    "interaction_max_download_images": ["interaction", "max_download_images"],
    "interaction_max_download_chapters": ["interaction", "max_download_chapters"],
    "interaction_auto_recall_seconds": ["interaction", "auto_recall_seconds"],
    "output_max_local_chapters": ["output", "max_local_chapters"],
    "output_cover_cache_max_files": ["output", "cover_cache_max_files"],
}

BOOL_FIELDS = {
    "request_enabled": ["request", "enabled"],
    "features_open_random_search": ["features", "open_random_search"],
    "features_auto_find_jm": ["features", "auto_find_jm"],
}

STR_FIELDS = {
    "request_proxies": ["request", "proxies"],
    "output_base_dir": ["output", "base_dir"],
    "output_pdf_password": ["output", "pdf_password"],
    "output_cover_cache_dir": ["output", "cover_cache_dir"],
}


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


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _read_yaml_config(yaml_path: Path) -> tuple[dict[str, Any], bool]:
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}, False
    logger.warning("配置文件不存在，准备创建默认配置")
    return {}, True


def _apply_cli_overrides(base_config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    config = dict(base_config or {})
    for key, val in (overrides or {}).items():
        if val in (None, ""):
            continue
        if key in INT_FIELDS:
            try:
                _set_nested(config, INT_FIELDS[key], int(val))
            except Exception:
                logger.warning(f"配置项 {key}={val} 不是有效整数，跳过")
        elif key in BOOL_FIELDS:
            _set_nested(config, BOOL_FIELDS[key], _to_bool(val))
        elif key in STR_FIELDS:
            _set_nested(config, STR_FIELDS[key], str(val))
    return config


def _normalize_request(config: dict[str, Any], schema_defaults: dict[str, Any]):
    request = config.setdefault("request", {})
    request.setdefault("enabled", bool(_schema_default(schema_defaults, "request_enabled", False)))
    request.setdefault("proxies", str(_schema_default(schema_defaults, "request_proxies", "")))
    request.setdefault("timeout", int(_schema_default(schema_defaults, "request_timeout", 15)))
    request.setdefault("max_retries", int(_schema_default(schema_defaults, "request_max_retries", 3)))
    request["timeout"] = _clamp_int(request.get("timeout"), 15, 1, 300)
    request["max_retries"] = _clamp_int(request.get("max_retries"), 3, 1, 10)

    if request.get("enabled", True):
        try:
            request["proxy"] = parse_proxy_config(str(request.get("proxies", "") or ""))
        except Exception as e:
            logger.warning(f"代理配置无效，已回退为无代理: {e}")
            request["proxy"] = {}
            request["enabled"] = False
    else:
        request["proxy"] = {}


def _normalize_output(config: dict[str, Any], schema_defaults: dict[str, Any], data_dir: Path) -> Path:
    output = config.setdefault("output", {})
    base_dir = Path(
        output.get("base_dir")
        or _schema_default(schema_defaults, "output_base_dir", str(data_dir / "download"))
    )
    output["base_dir"] = str(base_dir)
    output.pop("image_dir", None)
    output.pop("pdf_dir", None)
    output.setdefault("pdf_max_pages", int(_schema_default(schema_defaults, "output_pdf_max_pages", 150)))
    output.setdefault("jpeg_quality", int(_schema_default(schema_defaults, "output_jpeg_quality", 85)))
    output.setdefault("pdf_password", str(_schema_default(schema_defaults, "output_pdf_password", "")))
    output.setdefault("max_local_albums", int(_schema_default(schema_defaults, "output_max_local_albums", 5)))
    output.setdefault("max_local_chapters", int(_schema_default(schema_defaults, "output_max_local_chapters", 0)))
    output.setdefault("cover_cache_dir", str(_schema_default(schema_defaults, "output_cover_cache_dir", str(data_dir / "cover_cache"))))
    output.setdefault("cover_cache_max_files", int(_schema_default(schema_defaults, "output_cover_cache_max_files", 100)))

    output["pdf_max_pages"] = _clamp_int(output.get("pdf_max_pages"), 150, 1, 5000)
    output["jpeg_quality"] = _clamp_int(output.get("jpeg_quality"), 85, 1, 100)
    output["max_local_albums"] = _clamp_int(output.get("max_local_albums"), 5, 0, 1000)
    output["max_local_chapters"] = _clamp_int(output.get("max_local_chapters"), 0, 0, 1000)
    output["cover_cache_max_files"] = _clamp_int(output.get("cover_cache_max_files"), 100, 0, 5000)
    return base_dir


def _normalize_cache(config: dict[str, Any], base_dir: Path):
    cache = config.setdefault("cache", {})
    cache_root = base_dir.parent
    cache.setdefault("search_cache_file", str(cache_root / "search_cache.json"))
    cache.setdefault("random_cache_file", str(cache_root / "jm_max_page.json"))
    cache.setdefault("chapter_selection_cache_file", str(cache_root / "chapter_selection_cache.json"))
    cache.setdefault("cache_root_dir", str(cache_root))


def _normalize_features(config: dict[str, Any], schema_defaults: dict[str, Any]):
    features = config.setdefault("features", {})
    features.setdefault("open_random_search", bool(_schema_default(schema_defaults, "features_open_random_search", True)))
    features.setdefault("auto_find_jm", bool(_schema_default(schema_defaults, "features_auto_find_jm", False)))


def _normalize_download(config: dict[str, Any], schema_defaults: dict[str, Any]):
    download = config.setdefault("download", {})
    download.setdefault("image_threads", int(_schema_default(schema_defaults, "download_image_threads", 8)))
    download.setdefault("photo_threads", int(_schema_default(schema_defaults, "download_photo_threads", 8)))
    download["image_threads"] = _clamp_int(download.get("image_threads"), 8, 1, 16)
    download["photo_threads"] = _clamp_int(download.get("photo_threads"), 8, 1, 8)


def _normalize_interaction(config: dict[str, Any], schema_defaults: dict[str, Any]):
    interaction = config.setdefault("interaction", {})
    interaction.setdefault("chapter_fold_threshold", int(_schema_default(schema_defaults, "chapter_fold_threshold", 20)))
    interaction.setdefault("max_download_images", int(_schema_default(schema_defaults, "interaction_max_download_images", 400)))
    interaction.setdefault("max_download_chapters", int(_schema_default(schema_defaults, "interaction_max_download_chapters", 3)))
    interaction.setdefault("search_page_count_threads", int(_schema_default(schema_defaults, "interaction_search_page_count_threads", 10)))
    interaction.setdefault("search_cover_threads", int(_schema_default(schema_defaults, "interaction_search_cover_threads", 10)))
    interaction.setdefault("chapter_detail_threads", int(_schema_default(schema_defaults, "interaction_chapter_detail_threads", 10)))
    interaction.setdefault("chapter_selection_ttl", 86400)
    interaction.setdefault("auto_recall_seconds", int(_schema_default(schema_defaults, "interaction_auto_recall_seconds", 60)))

    interaction["chapter_fold_threshold"] = _clamp_int(interaction.get("chapter_fold_threshold"), 20, 0, 100)
    interaction["max_download_images"] = _clamp_int(interaction.get("max_download_images"), 400, 0, 5000)
    interaction["max_download_chapters"] = _clamp_int(interaction.get("max_download_chapters"), 3, 0, 100)
    interaction["search_page_count_threads"] = _clamp_int(interaction.get("search_page_count_threads"), 10, 1, 16)
    interaction["search_cover_threads"] = _clamp_int(interaction.get("search_cover_threads"), 10, 1, 16)
    interaction["chapter_detail_threads"] = _clamp_int(interaction.get("chapter_detail_threads"), 10, 1, 16)
    interaction["chapter_selection_ttl"] = _clamp_int(interaction.get("chapter_selection_ttl"), 86400, 300, 604800)
    interaction["auto_recall_seconds"] = _clamp_int(interaction.get("auto_recall_seconds"), 60, 0, 86400)


def _normalize_commands(config: dict[str, Any]):
    commands = config.setdefault("commands", {})
    commands.setdefault("search", "搜jm")
    commands.setdefault("view", "看jm")
    commands.setdefault("random", "随机jm")
    commands.setdefault("update_domain", "jm更新域名")
    commands.setdefault("clear_domain", "jm清空域名")


def _ensure_runtime_dirs(config: dict[str, Any]):
    output = config.get("output", {}) or {}
    cache = config.get("cache", {}) or {}
    Path(output["base_dir"]).mkdir(parents=True, exist_ok=True)
    Path(output["cover_cache_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cache["search_cache_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cache["chapter_selection_cache_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(cache["random_cache_file"]).parent.mkdir(parents=True, exist_ok=True)


def _write_yaml_config(yaml_path: Path, config: dict[str, Any]):
    try:
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        logger.warning(f"写入配置文件失败: {e}")


def load_config(
    config: dict, config_path: str | Path | None = None, persist: bool = False
) -> dict[str, Any]:
    data_dir = StarTools.get_data_dir("astrbot_plugin_jm_bot")
    yaml_path = Path(config_path) if config_path else data_dir / "config.yaml"
    schema_defaults = _load_schema_defaults()

    yaml_config, created_default_file = _read_yaml_config(yaml_path)
    merged_config = _apply_cli_overrides(yaml_config, config)

    _normalize_request(merged_config, schema_defaults)
    base_dir = _normalize_output(merged_config, schema_defaults, data_dir)
    _normalize_cache(merged_config, base_dir)
    _normalize_features(merged_config, schema_defaults)
    _normalize_download(merged_config, schema_defaults)
    _normalize_interaction(merged_config, schema_defaults)
    _normalize_commands(merged_config)
    _ensure_runtime_dirs(merged_config)

    if persist or created_default_file:
        _write_yaml_config(yaml_path, merged_config)

    return merged_config
