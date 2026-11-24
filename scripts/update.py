#!/usr/bin/env python3
"""Fetch CCTV and satellite streams from the four IPTV source libraries and apply IP-level delta updates."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCES_FILE = ROOT / "sources.json"
CHANNELS_M3U = ROOT / "channels.m3u"
DATA_DIR = ROOT / "data"
CHANNELS_JSON = DATA_DIR / "channels.json"
LAST_UPDATE = DATA_DIR / "last-update.json"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
)
ALLOWED_CATEGORIES = {"央视", "卫视"}
MAX_PER_CHANNEL = 12
GENERIC_SUFFIX = [
    "中文国际", "国防军事", "军事农业", "农业农村", "社会与法", "体育赛事",
    "纪录片", "电视剧", "奥林匹克", "综合", "财经", "综艺", "体育", "电影",
    "军事", "纪录", "科教", "戏曲", "新闻", "少儿", "音乐", "农业", "频道",
]
CHINESE_NUMBERS = [
    ("十七", 17), ("十六", 16), ("十五", 15), ("十四", 14), ("十三", 13),
    ("十二", 12), ("十一", 11), ("十", 10), ("九", 9), ("八", 8), ("七", 7),
    ("六", 6), ("五", 5), ("四", 4), ("三", 3), ("二", 2), ("一", 1),
]
CCTV_RE = re.compile(r"(?i)CCTV\s*[-_ ]?\s*(\d+)\s*(K)?\s*(\+|PLUS)?\s*(.*)")
ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
QUALITY_STRIP_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(8K|4K|2160P|1080P|720P|576P|480P|2160|1080|720|FHD|UHD|HDR|HEVC)(?![A-Z0-9])"
)
BRACKET_RE = re.compile(r"[（(\[【][^）)\]】]{0,16}[）)\]】]")
CCTV_KEY_RE = re.compile(r"cctv(\d+)(plus|asia|europe|america|k)?")


def normalize_width(value: str) -> str:
    out = []
    for char in value:
        if "０" <= char <= "９":
            out.append(chr(ord("0") + (ord(char) - ord("０"))))
        elif char == "＋":
            out.append("+")
        elif char in "－—":
            out.append("-")
        elif char == "　":
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def strip_symbols(value: str) -> str:
    return re.sub(r"[\u2000-\u206F\uFE00-\uFE0F]+", "", value).strip()


def explicit_quality(text: str) -> tuple[int, str] | None:
    if not text:
        return None
    upper = text.upper()
    if "4320" in upper or re.search(r"(?i)(?<![A-Z0-9])8K(?![A-Z0-9])", text):
        return 500, "8K"
    if "2160" in upper or "UHD" in upper or re.search(r"(?i)(?<![A-Z0-9])4K(?![A-Z0-9])", text):
        return 400, "4K"
    if "1080" in upper or "FHD" in upper:
        return 300, "1080P"
    if "超清" in text or "蓝光" in text:
        return 300, "超清"
    if "720" in upper:
        return 200, "720P"
    if "高清" in text or re.search(r"(?i)(?<![A-Z0-9])HD(?![A-Z0-9])", text):
        return 200, "高清"
    if "576" in upper or "480" in upper or "标清" in text or re.search(r"(?i)(?<![A-Z0-9])SD(?![A-Z0-9])", text):
        return 100, "标清"
    return None


def strip_channel_k(name: str) -> str:
    return re.sub(r"(?i)CCTV\s*[-_ ]?\s*\d+\s*K\b", " ", name)


def stream_quality(name: str, hints: list[str]) -> tuple[int, str]:
    found = explicit_quality(strip_channel_k(name))
    if found:
        return found
    for hint in hints:
        found = explicit_quality(hint)
        if found:
            return found
    return 80, "自动"


def strip_quality_words(name: str) -> str:
    def keep_or_drop(match: re.Match[str]) -> str:
        inner = match.group(0)
        return "" if explicit_quality(inner) else inner

    text = BRACKET_RE.sub(keep_or_drop, name)
    text = QUALITY_STRIP_RE.sub(" ", text)
    text = re.sub(r"(超清|高清版|高清|标清版|标清|流畅|蓝光|原画)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def cleanup_name(name: str) -> str:
    return strip_quality_words(name).replace("  ", " ").strip(" -|·_")


def region_of(rest: str) -> str:
    text = strip_quality_words(rest)
    for token in sorted(GENERIC_SUFFIX, key=len, reverse=True):
        text = text.replace(token, "")
    text = re.sub(r"[\s\-_|·,，。/]+", "", text)
    if "欧洲" in text or re.search(r"Europe", text, re.I):
        return "europe"
    if "美洲" in text or re.search(r"America", text, re.I):
        return "america"
    if "亚洲" in text or re.search(r"Asia", text, re.I):
        return "asia"
    return "" if not text else text.lower()


def region_label(region: str) -> str:
    return {"europe": "欧洲", "america": "美洲", "asia": "亚洲"}.get(region, region)


def parse_cctv(name: str) -> tuple[str, str] | None:
    match = CCTV_RE.search(name)
    if not match:
        return None
    try:
        number = int(match.group(1))
    except ValueError:
        return None
    if number not in range(1, 18):
        return None
    k = bool(match.group(2))
    plus = bool(match.group(3))
    region = region_of(match.group(4) or "")
    key = f"cctv{number}{'k' if k else ''}{'plus' if plus else ''}{region}"
    display = f"CCTV-{number}{'K' if k else ''}{'+' if plus else ''}"
    if region:
        display += f" {region_label(region)}"
    return key, display


def chinese_cctv_alias(cleaned: str) -> tuple[str, str] | None:
    compact = cleaned.replace(" ", "")
    for zh, number in CHINESE_NUMBERS:
        if f"央视{zh}套" in compact or f"中央{zh}套" in compact:
            return f"cctv{number}", f"CCTV-{number}"
    match = re.search(r"(?:央视|中央)(\d{1,2})套", compact)
    if match:
        number = int(match.group(1))
        if number in range(1, 18):
            return f"cctv{number}", f"CCTV-{number}"
    if compact.replace("频道", "") == "央视新闻":
        return "cctv13", "CCTV-13"
    return None


def category_of(name: str, group: str | None) -> str:
    grouped = strip_symbols(group or "")
    blob = name + grouped
    if any(token.lower() in blob.lower() for token in ("凤凰", "翡翠", "香港", "澳门", "台湾", "TVB")):
        return "港澳台"
    if re.search(r"(?i)CCTV|CGTN", name) or "央视" in name:
        return "央视"
    if "卫视" in name:
        return "卫视"
    if "央视" in grouped:
        return "央视"
    if "卫视" in grouped:
        return "卫视"
    if any(token in grouped for token in ("港澳", "港台", "台湾", "香港", "澳门")):
        return "港澳台"
    if any(token in grouped for token in ("国际", "海外", "国外")):
        return "国际"
    if any(token in grouped for token in ("地方", "省级", "城市")):
        return "地方"
    if any("\u4e00" <= ch <= "\u9fff" for ch in name):
        return "地方"
    if any(ch.isalpha() for ch in name):
        return "国际"
    return "其他"


def accept(name: str, group: str | None) -> bool:
    trimmed = name.strip()
    if len(trimmed) < 2:
        return False
    letters = sum(ch.isalpha() for ch in trimmed)
    cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in trimmed)
    if letters < 2 and cjk < 2:
        return False
    if re.search(r"^\d{4}-\d{2}-\d{2}", trimmed):
        return False
    blob = (trimmed + " " + (group or "")).lower()
    if any(token in blob for token in ("成人", "色情", "情色", "adult", "xxx")):
        return False
    if "更新" in (group or "") and re.search(r"\d{4}", trimmed) and cjk < 2:
        return False
    return True


def identify(raw_name: str, group_title: str | None, hints: list[str] | None = None) -> dict[str, Any] | None:
    original = normalize_width(raw_name).strip()
    if not accept(original, group_title):
        return None
    rank, label = stream_quality(original, hints or [])
    cctv = parse_cctv(original)
    if cctv:
        return {"key": cctv[0], "display": cctv[1], "category": "央视", "quality_rank": rank, "quality_label": label}
    cleaned = cleanup_name(original)
    alias = chinese_cctv_alias(cleaned)
    if alias:
        return {"key": alias[0], "display": alias[1], "category": "央视", "quality_rank": rank, "quality_label": label}
    satellite = re.search(r"([\u4e00-\u9fff]{2,8})卫视", cleaned)
    if satellite:
        name = satellite.group(0)
        return {
            "key": name,
            "display": name,
            "category": category_of(name, group_title),
            "quality_rank": rank,
            "quality_label": label,
        }
    if not cleaned:
        return None
    key = re.sub(r"[\s\W]+", "", cleaned, flags=re.UNICODE).lower()
    if len(key) < 2:
        return None
    return {
        "key": key,
        "display": cleaned,
        "category": category_of(cleaned, group_title),
        "quality_rank": rank,
        "quality_label": label,
    }


def split_extinf(body: str) -> tuple[str, str]:
    index = 0
    while index < len(body) and body[index] not in " ,":
        index += 1
    rest = body[index:].strip() if index < len(body) else ""
    if not rest:
        return "", ""
    if rest.startswith(","):
        return "", rest[1:].strip()
    quote = None
    for position, char in enumerate(rest):
        if quote is not None and char == quote:
            quote = None
        elif quote is None and char in "\"'":
            quote = char
        elif quote is None and char == ",":
            return rest[:position], rest[position + 1 :].strip()
    return rest, ""


def parse_attributes(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not source.strip():
        return values
    for match in ATTR_RE.finditer(source):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        values[key] = value
    return values


def split_pipe(url_line: str) -> tuple[str, str | None, str | None]:
    decoded = url_line.replace("%7C", "|").replace("%7c", "|")
    parts = decoded.split("|", 1)
    url = parts[0].strip()
    if len(parts) == 1:
        return url, None, None
    agent = None
    referer = None
    for token in parts[1].split("&"):
        key = token.split("=", 1)[0].strip().lower()
        value = token.split("=", 1)[1].strip() if "=" in token else ""
        if key in {"user-agent", "http-user-agent"}:
            agent = value
        elif key in {"referer", "referrer", "http-referer", "http-referrer"}:
            referer = value
    return url, agent, referer


def is_http_stream(url: str) -> bool:
    lower = url.lower()
    return lower.startswith("http://") or lower.startswith("https://")


def parse_playlist(text: str) -> list[dict[str, Any]]:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if "#extinf" in normalized.lower() or "#extm3u" in normalized.lower():
        return parse_m3u(normalized)
    return parse_txt(normalized)


def to_stream(raw_name: str, group: str | None, url: str, logo: str | None, agent: str | None, referer: str | None, hints: list[str], source: str) -> dict[str, Any] | None:
    if not is_http_stream(url):
        return None
    identity = identify(raw_name, group, hints)
    if identity is None or identity["category"] not in ALLOWED_CATEGORIES:
        return None
    return {
        "channel_key": identity["key"],
        "display_name": identity["display"],
        "category": identity["category"],
        "logo": logo,
        "stream_url": url,
        "user_agent": agent,
        "referer": referer,
        "quality_rank": identity["quality_rank"],
        "quality_label": identity["quality_label"],
        "raw_name": raw_name,
        "source": source,
    }


def parse_m3u(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    attributes = ""
    title = ""
    has_info = False
    user_agent = None
    referer = None
    source = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("#extinf"):
            body = line.split(":", 1)[1] if ":" in line else ""
            attributes, title = split_extinf(body)
            has_info = True
        elif lower.startswith("#extvlcopt:") or lower.startswith("#kodiprop:"):
            payload = line.split(":", 1)[1] if ":" in line else ""
            key = payload.split("=", 1)[0].strip().lower()
            value = payload.split("=", 1)[1].strip() if "=" in payload else ""
            if key in {"http-user-agent", "user-agent"}:
                user_agent = value
            elif key in {"http-referrer", "http-referer", "referer", "referrer"}:
                referer = value
        elif line.startswith("#"):
            continue
        elif has_info:
            url, pipe_agent, pipe_referer = split_pipe(line)
            attrs = parse_attributes(attributes)
            raw_name = title or attrs.get("tvg-name", "")
            item = to_stream(
                raw_name=raw_name,
                group=attrs.get("group-title"),
                url=url,
                logo=(attrs.get("tvg-logo") or "").strip() or None,
                agent=pipe_agent or user_agent,
                referer=pipe_referer or referer,
                hints=[value for value in (attrs.get("zz-definition"), attrs.get("tvg-id")) if value],
                source=source,
            )
            if item:
                result.append(item)
            attributes = ""
            title = ""
            has_info = False
            user_agent = None
            referer = None
    return result


def parse_txt(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    group = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.upper().startswith("#EXT"):
            continue
        if "#genre#" in line:
            group = line.split(",", 1)[0].split("#", 1)[0].strip() or None
            continue
        comma = line.find(",")
        if comma <= 0:
            continue
        name = line[:comma].strip()
        url, agent, referer = split_pipe(line[comma + 1 :].strip())
        item = to_stream(name, group, url, None, agent, referer, [], "")
        if item:
            result.append(item)
    return result


def looks_like_playlist(body: str) -> bool:
    head = body[:800].lower()
    if "<html" in head or "<!doctype" in head:
        return False
    return "#extinf" in body.lower() or "#genre#" in body.lower()


def fetch_url(url: str, timeout: int = 40) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_feed(urls: list[str]) -> str:
    errors: list[str] = []
    for url in urls:
        if not url:
            continue
        try:
            body = fetch_url(url)
            if not looks_like_playlist(body):
                raise ValueError("不是播放列表")
            return body
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            errors.append(f"{url}: {error}")
    raise RuntimeError("；".join(errors) or "没有可用地址")


def stream_path(url: str) -> str | None:
    uri = urlsplit(url.strip())
    scheme = (uri.scheme or "").lower()
    if scheme in {"udp", "rtp", "rtsp"}:
        return None
    path = uri.path or ""
    if not path or path == "/":
        return None
    query = f"?{uri.query}" if uri.query else ""
    return path + query


def same_stream(left: str, right: str) -> bool:
    left_path = stream_path(left)
    right_path = stream_path(right)
    if left_path and right_path:
        return left_path == right_path
    return left == right


def cctv_order(key: str) -> int | None:
    match = CCTV_KEY_RE.fullmatch(key)
    if not match:
        return None
    number = int(match.group(1))
    suffix = {"": 0, "plus": 1, "k": 2, "asia": 3, "europe": 4, "america": 5}.get(match.group(2) or "", 6)
    return number * 10 + suffix


def channel_sort_key(channel: dict[str, Any]) -> tuple:
    category = 0 if channel["category"] == "央视" else 1
    number = cctv_order(channel["key"])
    return category, number if number is not None else 10_000, channel["name"]


def load_previous() -> list[dict[str, Any]]:
    if not CHANNELS_JSON.exists():
        return []
    payload = json.loads(CHANNELS_JSON.read_text(encoding="utf-8"))
    return payload.get("channels", [])


def flatten(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for channel in channels:
        for stream in channel.get("streams", []):
            url = stream.get("stream_url") or stream.get("url") or ""
            rows.append(
                {
                    "channel_key": channel["key"],
                    "display_name": channel["name"],
                    "category": channel["category"],
                    "logo": channel.get("logo"),
                    "stream_url": url,
                    "quality_rank": stream.get("quality_rank", 80),
                    "quality_label": stream.get("quality") or stream.get("quality_label") or "自动",
                    "source": stream.get("source") or "",
                    "user_agent": stream.get("user_agent"),
                    "referer": stream.get("referer"),
                    "raw_name": stream.get("raw_name") or channel["name"],
                }
            )
    return rows


def merge_streams(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}
    for item in existing + incoming:
        grouped[item["channel_key"]].append(item)
        current = meta.get(item["channel_key"])
        if current is None or item["quality_rank"] >= current["quality_rank"]:
            meta[item["channel_key"]] = item

    added = updated = removed = kept = 0
    channels: list[dict[str, Any]] = []
    incoming_by_key = defaultdict(list)
    for item in incoming:
        incoming_by_key[item["channel_key"]].append(item)
    existing_by_key = defaultdict(list)
    for item in existing:
        existing_by_key[item["channel_key"]].append(item)

    for key, info in meta.items():
        old_sources = existing_by_key.get(key, [])
        fresh = incoming_by_key.get(key, [])
        fresh = sorted(
            {item["stream_url"]: item for item in fresh}.values(),
            key=lambda item: item["quality_rank"],
            reverse=True,
        )[:MAX_PER_CHANNEL]
        adopted: list[dict[str, Any]] = []
        pool = list(old_sources)
        for item in fresh:
            match_index = next((index for index, old in enumerate(pool) if same_stream(old["stream_url"], item["stream_url"])), -1)
            if match_index >= 0:
                previous = pool.pop(match_index)
                if previous["stream_url"] != item["stream_url"]:
                    updated += 1
                else:
                    kept += 1
                adopted.append(item)
            else:
                added += 1
                adopted.append(item)
        removed += len(pool)
        if not adopted:
            continue
        channels.append(
            {
                "key": key,
                "name": info["display_name"],
                "category": info["category"],
                "logo": next((item.get("logo") for item in adopted if item.get("logo")), info.get("logo")),
                "streams": [
                    {
                        "url": item["stream_url"],
                        "quality": item["quality_label"],
                        "quality_rank": item["quality_rank"],
                        "source": item.get("source") or "",
                        "user_agent": item.get("user_agent"),
                        "referer": item.get("referer"),
                        "raw_name": item.get("raw_name") or item["display_name"],
                    }
                    for item in adopted
                ],
            }
        )
    channels.sort(key=channel_sort_key)
    return channels, {"added": added, "updated": updated, "removed": removed, "kept": kept}


def write_m3u(channels: list[dict[str, Any]]) -> None:
    lines = ["#EXTM3U"]
    for channel in channels:
        for stream in channel["streams"]:
            attrs = [
                f'tvg-id="{channel["key"]}"',
                f'tvg-name="{channel["name"]}"',
                f'group-title="{channel["category"]}"',
                f'zz-definition="{stream["quality"]}"',
            ]
            if channel.get("logo"):
                attrs.insert(2, f'tvg-logo="{channel["logo"]}"')
            lines.append(f'#EXTINF:-1 {" ".join(attrs)},{channel["name"]}')
            if stream.get("user_agent"):
                lines.append(f'#EXTVLCOPT:http-user-agent={stream["user_agent"]}')
            if stream.get("referer"):
                lines.append(f'#EXTVLCOPT:http-referrer={stream["referer"]}')
            lines.append(stream["url"])
    CHANNELS_M3U.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_incoming() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    incoming: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for project in config["projects"]:
        for feed in project["feeds"]:
            report = {
                "project": project["id"],
                "feed": feed["id"],
                "ok": False,
                "channels": 0,
                "error": None,
            }
            try:
                body = fetch_feed(feed["urls"])
                parsed = parse_playlist(body)
                for item in parsed:
                    item["source"] = project["id"]
                incoming.extend(parsed)
                report["ok"] = True
                report["channels"] = len({item["channel_key"] for item in parsed})
                print(f"[ok] {project['id']}/{feed['id']}: {len(parsed)} streams / {report['channels']} channels", flush=True)
            except Exception as error:  # noqa: BLE001
                report["error"] = str(error)
                print(f"[fail] {project['id']}/{feed['id']}: {error}", flush=True)
            reports.append(report)
    return incoming, reports


def run_update() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    incoming, reports = collect_incoming()
    if not incoming:
        print("no usable 央视/卫视 streams fetched", file=sys.stderr)
        return 1
    previous = flatten(load_previous())
    channels, stats = merge_streams(previous, incoming)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "updated_at": now,
        "channel_count": len(channels),
        "stream_count": sum(len(channel["streams"]) for channel in channels),
        "channels": channels,
    }
    CHANNELS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_m3u(channels)
    message = (
        f"chore: 差量更新频道地址 (added {stats['added']}, "
        f"updated {stats['updated']}, removed {stats['removed']})"
    )
    LAST_UPDATE.write_text(
        json.dumps(
            {
                "updated_at": now,
                "stats": stats,
                "feeds": reports,
                "channel_count": payload["channel_count"],
                "stream_count": payload["stream_count"],
                "commit_message": message,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(message)
    print(f"channels={payload['channel_count']} streams={payload['stream_count']}")
    return 0


def self_test() -> int:
    cases = [
        ("CCTV-1综合", None, "cctv1", "CCTV-1", "央视"),
        ("CCTV1", None, "cctv1", "CCTV-1", "央视"),
        ("CCTV-5+体育赛事", None, "cctv5plus", "CCTV-5+", "央视"),
        ("湖南卫视高清", None, "湖南卫视", "湖南卫视", "卫视"),
        ("BRTV 北京卫视 (1080p)", None, "北京卫视", "北京卫视", "卫视"),
        ("央视一套", None, "cctv1", "CCTV-1", "央视"),
        ("凤凰卫视", None, "凤凰卫视", "凤凰卫视", "港澳台"),
        ("CCTV-4 Asia (720p)", None, "cctv4asia", "CCTV-4 亚洲", "央视"),
    ]
    for raw, group, key, display, category in cases:
        got = identify(raw, group)
        assert got is not None, raw
        assert got["key"] == key, (raw, got["key"], key)
        assert got["display"] == display, (raw, got["display"], display)
        assert got["category"] == category, (raw, got["category"], category)
    assert identify("2026-08-27 14:06:22", "更新时间") is None
    assert same_stream("http://1.1.1.1:8080/live/cctv1.m3u8", "http://2.2.2.2:9000/live/cctv1.m3u8")
    assert not same_stream("http://1.1.1.1/live/a.m3u8", "http://1.1.1.1/live/b.m3u8")
    playlist = """#EXTM3U
#EXTINF:-1 tvg-name="CCTV1" group-title="央视频道",CCTV-1综合
http://example.com/cctv1.m3u8
#EXTINF:-1 group-title="卫视",湖南卫视高清
http://1.1.1.1:8080/live/hunan.m3u8
#EXTINF:-1 group-title="地方",山西卫视
udp://@239.1.1.1:8000
"""
    parsed = parse_playlist(playlist)
    assert [item["channel_key"] for item in parsed] == ["cctv1", "湖南卫视"]
    print("self-test ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return run_update()


if __name__ == "__main__":
    raise SystemExit(main())
