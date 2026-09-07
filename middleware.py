from __future__ import annotations

import json
import re
import shlex
import shutil
import unicodedata
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

import uvicorn
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter

import app


HOST = str(app.CONFIG["agent"].get("host", "127.0.0.1"))
PORT = int(app.CONFIG["agent"].get("port", 8001))
CONNECT_HOST = "127.0.0.1" if HOST in {"0.0.0.0", "::", "[::]"} else HOST
LOCAL_URL = f"http://{CONNECT_HOST}:{PORT}"
LANGUAGE = "chinese"


HELP_ZH = """支持的命令（命令名不带短横线，选项参数带短横线）：
  help                          显示帮助
  version                       显示中间件版本
  status                        显示服务、llama.cpp 和缓存状态
  config                        显示启动时读取的配置（只读）
  list                          列出全部本地 KV 缓存（仅 KV ID、状态、创建时间）
  list -f <值> [-f <值>]        按名称、Agent ID、缓存 ID、文件名或日期筛选
                                命中一条时显示 KV 详情（模板内容请查看模板 JSON）
  del -f <值> [-f <值>]         删除匹配的 KV 元数据和磁盘快照
  del all                       删除全部 KV（必须明确写 all）
  messages [-n 数量]            查看运行期间的请求预览
  messages -f <序号>            查看单条消息完整信息
  messages -f <序号> <列标题>   只查看该消息的指定列
  messages clear                清空运行期消息
  template                      查看模板参考文件预览
  template -f <文件名或ID>       查看单个模板的完整内容
  template del <文件名或ID>     删除指定模板信息
  template del all              删除全部模板信息
  logs [-n 数量]                查看运行期日志预览
  logs -f <序号>                查看单条日志完整信息
  logs -f <序号> <列标题>       只查看该日志的指定列
  language chinese              切换为中文
  language english              Switch to English
  stop                          停止中间件功能，仅保留透明路由转发
  start                         启动中间件功能
  show                          查看启动、路由、错误和功能状态
  exit                          优雅停止服务并退出

筛选示例：
  list -f agent-a
  list -f 2026-09-04
  list -f "2026,09,01-2026,09,04"
  list -f 2026-09-04..2026-09-05 -f agent-a
日期精度可到年、月、日、时、分；省略部分表示覆盖整个对应时间段。
旧的 -list、-messages 等写法仍兼容，但不再作为推荐格式。
"""

HELP_EN = """Supported commands (command names have no dash; options keep a dash):
  help                          Show this help
  version                       Show middleware version
  status                        Show service, llama.cpp and cache status
  config                        Show read-only startup configuration
  list                          List KV IDs, status, and creation time
  list -f <value> [-f <value>]  Filter by name, Agent ID, cache ID, file, or date
                                One match shows KV details; template content stays in its JSON file
  del -f <value> [-f <value>]   Delete matching metadata and snapshots
  del all                       Delete every KV cache (explicit all required)
  messages [-n count]           Show a readable request preview
  messages -f <number>         Show one complete message
  messages -f <number> <column> Show one column from that message
  messages clear               Clear runtime messages
  template                      Show a preview of persisted templates
  template -f <file-or-ID>     Show one complete template
  template del <file-or-ID>    Delete one template
  template del all              Delete all templates
  logs [-n count]               Show a readable runtime log preview
  logs -f <number>              Show one complete log
  logs -f <number> <column>     Show one column from that log
  language chinese              切换为中文
  language english              Switch to English
  stop                          Stop middleware features; keep transparent routing
  start                         Start middleware features
  show                          Show startup, routing, error, and feature status
  exit                          Stop the service cleanly

Filter examples:
  list -f agent-a
  list -f 2026-09-04
  list -f "2026,09,01-2026,09,04"
  list -f 2026-09-04..2026-09-05 -f agent-a
Date precision may be year, month, day, hour, or minute.
Legacy -list, -messages, etc. remain accepted but are no longer recommended.
"""

COMPLETER = NestedCompleter.from_nested_dict({
    "help": None, "version": None, "status": None, "stats": None,
    "config": None, "list": {"-f": None}, "del": {"-f": None, "all": None},
    "messages": {"-n": None, "-f": None, "clear": None},
    "template": {"-f": None, "del": {"all": None}}, "logs": {"-n": None, "-f": None},
    "language": {"chinese": None, "english": None},
    "stop": None, "start": None, "show": None, "exit": None,
})


def tr(chinese: str, english: str) -> str:
    return chinese if LANGUAGE == "chinese" else english


def request_json(path: str, method: str = "GET") -> Any:
    request = urllib.request.Request(LOCAL_URL + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"middleware API is unavailable: {exc.reason}") from exc


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def fit(value: Any, width: int) -> str:
    text = str(value if value is not None and value != "" else "-").replace("\r", " ").replace("\n", " ")
    result = ""
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            suffix = "..."
            while display_width(result) + len(suffix) > width:
                result = result[:-1]
            return result + suffix
        result += char
        used += char_width
    return result + " " * (width - used)


def print_table(headers: list[str], rows: list[list[Any]], max_widths: list[int], no_truncate: bool = False) -> None:
    widths = []
    for index, header in enumerate(headers):
        content_width = max([display_width(header)] + [display_width(str(row[index] if row[index] is not None and row[index] != "" else "-")) for row in rows])
        widths.append(min(max_widths[index], content_width))
    if not no_truncate:
        available = max(40, shutil.get_terminal_size(fallback=(120, 30)).columns - 1)
        minimums = [display_width(header) for header in headers]
        while sum(widths) + 3 * len(widths) + 1 > available:
            reducible = [index for index, width in enumerate(widths) if width > minimums[index]]
            if not reducible:
                break
            widest = max(reducible, key=lambda index: widths[index] - minimums[index])
            widths[widest] -= 1
    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print(separator)
    print("| " + " | ".join(fit(header, widths[index]) for index, header in enumerate(headers)) + " |")
    print(separator)
    for row in rows:
        print("| " + " | ".join(fit(value, widths[index]) for index, value in enumerate(row)) + " |")
    print(separator)
    print(tr(f"共 {len(rows)} 条。", f"Total: {len(rows)}"))


def format_time(timestamp: Any) -> str:
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "-"


def format_size(size: Any) -> str:
    value = int(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return "0 B"


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if not isinstance(value, str) else value


def clean_user_question(value: Any) -> str:
    """Remove platform wrapper blocks from the human-readable preview."""
    if value is None:
        return "-"
    text = str(value)
    text = re.sub(r"(?is)<reasoning-language>.*?</reasoning-language>\s*", "", text)
    text = re.sub(r"(?is)<interrupted-turn-recovery>.*?</interrupted-turn-recovery>\s*", "", text)
    return text.strip() or "-"


def preview_field(label: str, value: Any) -> None:
    """Print one preview field without horizontal truncation."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value if value is not None and value != "" else "-")
    if "\n" in text:
        print(f"{label}:")
        print("\n".join(f"  {line}" for line in text.splitlines()))
    else:
        print(f"{label}: {text}")


def preview_separator() -> None:
    print("-" * min(88, max(40, shutil.get_terminal_size(fallback=(100, 30)).columns - 1)))


def detail_block(title: str, value: Any) -> None:
    """Render complete details vertically so long JSON never breaks a table."""
    print(f"\n=== {title} ===")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(str(value if value is not None else "-"))


def date_bound(value: str, upper: bool) -> float | None:
    parts = value.replace("/", "-").replace(",", "-").split("-")
    if not parts or not parts[0].isdigit() or len(parts[0]) != 4 or len(parts) > 5:
        return None
    try:
        nums = [int(part) for part in parts]
        while len(nums) < 5:
            nums.append(1 if len(nums) in {1, 2} else 0)
        point = datetime(nums[0], nums[1], nums[2], nums[3], nums[4])
        if upper:
            precision = len(parts)
            if precision == 1:
                point = datetime(nums[0] + 1, 1, 1)
            elif precision == 2:
                point = datetime(nums[0] + (nums[1] == 12), nums[1] % 12 + 1, 1)
            elif precision == 3:
                point += timedelta(days=1)
            elif precision == 4:
                point += timedelta(hours=1)
            else:
                point += timedelta(minutes=1)
        return point.timestamp()
    except ValueError:
        return None


def date_range(value: str) -> tuple[float, float] | None:
    for separator in ("..", "~", "至"):
        if separator in value:
            left, right = value.split(separator, 1)
            start, end = date_bound(left.strip(), False), date_bound(right.strip(), True)
            return (start, end) if start is not None and end is not None else None
    # A hyphenated range with comma/slash date components, e.g. 2026,09,01-2026,09,04.
    marker = value.find("-", 5)
    if ("," in value or "/" in value) and marker > 0:
        start, end = date_bound(value[:marker], False), date_bound(value[marker + 1:], True)
        return (start, end) if start is not None and end is not None else None
    start, end = date_bound(value, False), date_bound(value, True)
    return (start, end) if start is not None and end is not None else None


def values_after(tokens: list[str], flag: str) -> list[str]:
    return [tokens[index + 1] for index, token in enumerate(tokens[:-1]) if token == flag]


def matching_caches(filters: list[str]) -> list[dict[str, Any]]:
    caches = request_json("/admin/api/caches")
    for value in filters:
        span = date_range(value)
        if span:
            caches = [item for item in caches if span[0] <= float(item.get("created_at", 0)) < span[1]]
            continue
        needle = value.casefold()
        fields = ("id", "name", "agent_id", "snapshot_filename", "status")
        caches = [item for item in caches if any(needle in str(item.get(field, "")).casefold() for field in fields)]
    return caches


def cache_rows(caches: list[dict[str, Any]]) -> None:
    if not caches:
        print(tr("没有匹配的 KV 缓存。", "No matching KV cache."))
        return
    for index, item in enumerate(caches, 1):
        print(f"\n[{index}]")
        if LANGUAGE == "chinese":
            preview_field("KV ID", item.get("id"))
            preview_field("名称", item.get("name"))
            preview_field("Agent ID", item.get("agent_id"))
            preview_field("状态", item.get("status"))
            preview_field("创建时间", format_time(item.get("created_at")))
            preview_field("Token 数量", item.get("token_count"))
            preview_field("快照大小", format_size(item.get("snapshot_size")) if item.get("snapshot_exists") else item.get("size_status"))
            preview_field("快照文件", item.get("snapshot_file"))
            preview_field("模板文件", item.get("template_file"))
        else:
            preview_field("KV ID", item.get("id"))
            preview_field("Name", item.get("name"))
            preview_field("Agent ID", item.get("agent_id"))
            preview_field("Status", item.get("status"))
            preview_field("Created", format_time(item.get("created_at")))
            preview_field("Tokens", item.get("token_count"))
            preview_field("Snapshot size", format_size(item.get("snapshot_size")) if item.get("snapshot_exists") else item.get("size_status"))
            preview_field("Snapshot file", item.get("snapshot_file"))
            preview_field("Template file", item.get("template_file"))
        preview_separator()
    print(tr(f"共 {len(caches)} 条。", f"Total: {len(caches)}"))


def cache_summary_rows(caches: list[dict[str, Any]]) -> None:
    rows = [[item.get("id"), item.get("status"), format_time(item.get("created_at"))] for item in caches]
    headers = ["KV ID", "状态", "创建时间"] if LANGUAGE == "chinese" else ["KV ID", "Status", "Created"]
    print_table(headers, rows, [64, 16, 19])


def cache_detail(item: dict[str, Any]) -> None:
    """Print one cache without embedding its large template payload."""
    labels_zh = {
        "id": "KV ID", "name": "名称", "agent_id": "Agent ID", "status": "状态",
        "slot_id": "Slot", "created_at": "创建时间", "last_used_at": "最后使用时间",
        "ready_at": "就绪时间", "snapshot_filename": "快照文件名", "snapshot_file": "快照文件位置",
        "snapshot_exists": "快照存在", "snapshot_size": "快照大小", "size_status": "大小状态",
        "metadata_file": "元数据文件", "model_fingerprint": "模型指纹", "tokenizer_fingerprint": "Tokenizer 指纹",
        "llama_cpp_version": "llama.cpp 版本", "token_count": "Token 数量", "prefix_hash": "模板 Hash",
        "prefix": "模板内容", "template_file": "模板参考文件", "template_exists": "模板文件存在",
        "template_size": "模板文件大小", "snapshot_result": "保存结果", "warning": "警告", "save_error": "保存错误",
    }
    print(tr("\n=== KV 详情 ===", "\n=== KV details ==="))
    for key, value in item.items():
        if key in {"prefix", "rendered_prefix"}:
            continue
        if key.endswith("_at") and isinstance(value, (int, float)):
            value = format_time(value)
        elif key == "snapshot_size":
            value = f"{value:,} bytes ({format_size(value)})"
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, indent=2)
        label = labels_zh.get(key, key) if LANGUAGE == "chinese" else key
        detail_block(label, value)


def message_rows(messages: list[dict[str, Any]]) -> None:
    if not messages:
        print(tr("没有运行期消息。", "No runtime messages."))
        return
    labels = ("序号", "时间", "Agent ID", "会话 ID", "用户问题", "KV ID", "命中", "缓存 Token", "HTTP") if LANGUAGE == "chinese" else ("No.", "Time", "Agent ID", "Session ID", "User question", "KV ID", "Hit", "Cached tokens", "HTTP")
    for index, item in enumerate(messages, 1):
        print(f"\n[{labels[0]} {index}]")
        preview_field(labels[1], format_time(item.get("time")))
        preview_field(labels[2], item.get("agent_id"))
        preview_field(labels[3], item.get("session_id"))
        preview_field(labels[4], clean_user_question(item.get("user_question")))
        preview_field(labels[5], item.get("prefix_id"))
        preview_field(labels[6], item.get("cache_hit", "未知"))
        preview_field(labels[7], item.get("cached_tokens", "-"))
        preview_field(labels[8], item.get("status_code"))
        preview_separator()
    print(tr(f"共 {len(messages)} 条。", f"Total: {len(messages)}"))


def message_detail(item: dict[str, Any]) -> None:
    print(tr("\n=== 消息详情 ===", "\n=== Message details ==="))
    for key, value in item.items():
        label = {"agent_id": "Agent ID", "session_id": "会话 ID", "user_question": "用户问题", "request": "请求", "response": "响应"}.get(key, key)
        detail_block(label, value)


MESSAGE_FIELD_ALIASES = {
    "序号": "__index__", "no.": "__index__", "number": "__index__", "index": "__index__",
    "时间": "time", "time": "time", "agent id": "agent_id", "agent_id": "agent_id",
    "会话": "session_id", "会话id": "session_id", "会话 id": "session_id", "session": "session_id", "session id": "session_id", "session_id": "session_id",
    "用户问题": "user_question", "user question": "user_question", "user_question": "user_question",
    "kv id": "prefix_id", "kv_id": "prefix_id", "命中": "cache_hit", "hit": "cache_hit",
    "缓存token": "cached_tokens", "缓存 token": "cached_tokens", "cached": "cached_tokens", "cached tokens": "cached_tokens", "cached_tokens": "cached_tokens",
    "http": "status_code", "状态码": "status_code", "status": "status_code", "status_code": "status_code",
}

LOG_FIELD_ALIASES = {
    "序号": "__index__", "no.": "__index__", "number": "__index__", "index": "__index__",
    "时间": "time", "time": "time", "级别": "level", "level": "level", "事件": "event", "event": "event",
    "详细信息": "__details__", "details": "__details__",
}


def selected_field(item: dict[str, Any], index: int, selector: str, aliases: dict[str, str]) -> tuple[str, Any] | None:
    """Resolve a displayed column title to one value without dumping the whole record."""
    key = aliases.get(selector.casefold(), selector)
    if key == "__index__":
        return selector, index
    if key == "__details__":
        value = {name: value for name, value in item.items() if name not in {"time", "level", "event"}}
    elif key in item:
        value = item[key]
    else:
        return None
    if key == "time" and isinstance(value, (int, float)):
        value = format_time(value)
    elif key == "user_question":
        value = clean_user_question(value)
    return selector, value


def print_selected_field(item: dict[str, Any], index: int, selector: str, aliases: dict[str, str]) -> bool:
    selected = selected_field(item, index, selector, aliases)
    if selected is None:
        return False
    label, value = selected
    detail_block(label, value)
    return True


def template_rows(templates: list[dict[str, Any]]) -> None:
    if not templates:
        print(tr("没有已保存的模板。", "No persisted templates."))
        return
    for index, item in enumerate(templates, 1):
        print(f"\n[{index}]")
        if LANGUAGE == "chinese":
            preview_field("模板文件", item.get("filename"))
            preview_field("模板文件位置", str(app.DATA_DIR / "templates" / str(item.get("filename") or "")))
            preview_field("KV ID", item.get("prefix_id"))
            preview_field("Agent ID", item.get("agent_id"))
            preview_field("名称", item.get("name"))
            preview_field("创建时间", format_time(item.get("created_at")))
            preview_field("Token 数量", item.get("token_count"))
            preview_field("系统消息数量", len(item.get("messages") or []))
            preview_field("工具数量", len(item.get("tools") or []))
        else:
            preview_field("Template file", item.get("filename"))
            preview_field("Template path", str(app.DATA_DIR / "templates" / str(item.get("filename") or "")))
            preview_field("KV ID", item.get("prefix_id"))
            preview_field("Agent ID", item.get("agent_id"))
            preview_field("Name", item.get("name"))
            preview_field("Created", format_time(item.get("created_at")))
            preview_field("Tokens", item.get("token_count"))
            preview_field("Messages", len(item.get("messages") or []))
            preview_field("Tools", len(item.get("tools") or []))
        preview_separator()
    print(tr(f"共 {len(templates)} 条。", f"Total: {len(templates)}"))


def template_detail(item: dict[str, Any]) -> None:
    print(tr("\n=== 模板详情 ===", "\n=== Template details ==="))
    labels = {
        "filename": "模板文件名", "prefix_id": "KV ID", "agent_id": "Agent ID", "prefix_hash": "模板 Hash",
        "snapshot_scope": "快照范围", "created_at": "创建时间", "ready_at": "就绪时间",
        "token_count": "Token 数量", "messages": "系统/开发者消息", "tools": "工具列表",
        "rendered_prefix": "llama.cpp 渲染前缀",
    }
    for key, value in item.items():
        if key.endswith("_at") and isinstance(value, (int, float)):
            value = format_time(value)
        label = labels.get(key, key) if LANGUAGE == "chinese" else key
        detail_block(label, value)


def log_rows(logs: list[dict[str, Any]]) -> None:
    if not logs:
        print(tr("没有运行期日志。", "No runtime logs."))
        return
    important_keys = {
        "agent_id", "session_id", "prefix_id", "slot_id", "filename", "restored",
        "cache_hit", "cached_tokens", "prompt_tokens", "status_code", "size",
        "error", "previous_prefix_id", "expected_cached_tokens",
    }
    for index, item in enumerate(logs, 1):
        print(f"\n[{'序号' if LANGUAGE == 'chinese' else 'No.'} {index}]")
        preview_field("时间" if LANGUAGE == "chinese" else "Time", format_time(item.get("time")))
        preview_field("级别" if LANGUAGE == "chinese" else "Level", item.get("level"))
        preview_field("事件" if LANGUAGE == "chinese" else "Event", item.get("event"))
        details = {}
        for key, value in item.items():
            if key in important_keys:
                details[key] = value
            elif key == "user_question":
                details["user_question"] = clean_user_question(value)
            elif key == "restore_result" and isinstance(value, dict):
                details[key] = {
                    name: value.get(name)
                    for name in ("id_slot", "filename", "n_restored", "n_read", "timings")
                    if name in value
                }
        if details:
            preview_field("详细信息" if LANGUAGE == "chinese" else "Details", details)
        preview_separator()
    print(tr(f"共 {len(logs)} 条。", f"Total: {len(logs)}"))


def log_detail(item: dict[str, Any]) -> None:
    print(tr("\n=== 日志详情 ===", "\n=== Log details ==="))
    for key, value in item.items():
        detail_block(key, format_time(value) if key == "time" else value)


def run_command(line: str) -> bool:
    global LANGUAGE
    tokens = shlex.split(line)
    if not tokens:
        return True
    # Commands are words (help/list/messages); only options use a leading dash.
    # Strip a leading dash for backwards compatibility with the old command form.
    command = tokens[0].lower().lstrip("-")
    if command in {"help", "?"}:
        print(HELP_ZH if LANGUAGE == "chinese" else HELP_EN)
    elif command == "version":
        status = request_json("/admin/api/status")
        print(f"llama.cpp KV Middleware {status['service']['version']} (PID {status['service']['pid']})")
    elif command in {"status", "stats"}:
        print_json(request_json("/admin/api/status"))
    elif command == "show":
        status = request_json("/admin/api/status")
        service = status.get("service", {})
        routing = status.get("routing", {})
        rows = [["功能状态", "启动" if service.get("middleware_enabled") else "停止"], ["路由转发", "启用" if routing.get("enabled") else "停用"], ["路由请求数", routing.get("requests", 0)], ["路由错误数", routing.get("errors", 0)], ["llama.cpp", "已连接" if status.get("upstream", {}).get("connected") else "未连接"]]
        if LANGUAGE == "english":
            rows = [["Feature", "Started" if service.get("middleware_enabled") else "Stopped"], ["Routing", "Enabled" if routing.get("enabled") else "Disabled"], ["Routed Requests", routing.get("requests", 0)], ["Routing Errors", routing.get("errors", 0)], ["llama.cpp", "Connected" if status.get("upstream", {}).get("connected") else "Disconnected"]]
        print_table(["项目", "状态"] if LANGUAGE == "chinese" else ["Item", "Status"], rows, [24, 24])
    elif command == "config":
        print_json(request_json("/admin/api/config"))
    elif command == "list":
        filters = values_after(tokens, "-f")
        selected = matching_caches(filters)
        if filters and len(selected) == 1:
            cache_detail(selected[0])
        elif not filters:
            cache_summary_rows(selected)
        else:
            cache_rows(selected)
    elif command == "del":
        filters = values_after(tokens, "-f")
        delete_all = len(tokens) == 2 and tokens[1].lower() == "all"
        if not delete_all and not filters:
            print(tr("拒绝删除：请使用 del -f <筛选值>，或明确使用 del all。", "Deletion refused: use del -f <filter>, or explicitly use del all."))
        else:
            targets = matching_caches([] if delete_all else filters)
            for item in targets:
                request_json(f"/prefixes/{item['id']}", "DELETE")
            print(tr(f"已删除 {len(targets)} 条 KV 缓存。", f"Deleted {len(targets)} KV cache(s)."))
    elif command == "messages":
        if len(tokens) > 1 and tokens[1].lower() == "clear":
            print_json(request_json("/admin/api/messages", "DELETE"))
        else:
            count = int(tokens[tokens.index("-n") + 1]) if "-n" in tokens else 50
            messages = request_json(f"/admin/api/messages?limit={count}")
            if "-f" in tokens:
                try:
                    field_position = tokens.index("-f") + 2
                    index = int(tokens[field_position - 1])
                    if 1 <= index <= len(messages):
                        if len(tokens) > field_position:
                            selector = " ".join(tokens[field_position:])
                            if not print_selected_field(messages[index - 1], index, selector, MESSAGE_FIELD_ALIASES):
                                print(tr(f"未找到消息列：{selector}。", f"Message column not found: {selector}."))
                        else:
                            message_detail(messages[index - 1])
                    else:
                        print(tr("消息序号不存在。", "Message number not found."))
                except (ValueError, IndexError):
                    print(tr("用法：messages -f <序号> [列标题]", "Usage: messages -f <number> [column]"))
            else:
                message_rows(messages)
    elif command == "template":
        templates = request_json("/admin/api/templates")
        if len(tokens) >= 2 and tokens[1].lower() == "del":
            if len(tokens) != 3:
                print(tr("用法：template del <模板文件名或ID|all>", "Usage: template del <template-file-or-ID|all>"))
            else:
                target = tokens[2]
                selected = templates if target.lower() == "all" else [item for item in templates if target in {item["filename"], item["prefix_id"], item.get("name")}]
                for item in selected:
                    request_json(f"/admin/api/templates/{item['prefix_id']}", "DELETE")
                print(tr(f"已删除 {len(selected)} 个模板。", f"Deleted {len(selected)} template(s)."))
        elif "-f" in tokens:
            try:
                target = tokens[tokens.index("-f") + 1]
                selected = [item for item in templates if target in {item["filename"], item["prefix_id"], item.get("name")}]
                if len(selected) != 1:
                    print(tr("请使用唯一的模板文件名、KV ID 或名称。", "Use one unique template filename, KV ID, or name."))
                else:
                    template_detail(request_json(f"/admin/api/templates/{selected[0]['prefix_id']}"))
            except IndexError:
                print(tr("用法：template -f <模板文件名或ID>", "Usage: template -f <template-file-or-ID>"))
        else:
            template_rows(templates)
    elif command == "logs":
        count = int(tokens[tokens.index("-n") + 1]) if "-n" in tokens else 100
        logs = request_json(f"/admin/api/logs?limit={count}")
        if "-f" in tokens:
            try:
                field_position = tokens.index("-f") + 2
                index = int(tokens[field_position - 1])
                if 1 <= index <= len(logs):
                    if len(tokens) > field_position:
                        selector = " ".join(tokens[field_position:])
                        if not print_selected_field(logs[index - 1], index, selector, LOG_FIELD_ALIASES):
                            print(tr(f"未找到日志列：{selector}。", f"Log column not found: {selector}."))
                    else:
                        log_detail(logs[index - 1])
                else:
                    print(tr("日志序号不存在。", "Log number not found."))
            except (ValueError, IndexError):
                print(tr("用法：logs -f <序号> [列标题]", "Usage: logs -f <number> [column]"))
        else:
            log_rows(logs)
    elif command == "language":
        if len(tokens) != 2 or tokens[1].lower() not in {"chinese", "english"}:
            print(tr("用法：language chinese|english", "Usage: language chinese|english"))
        else:
            LANGUAGE = tokens[1].lower()
            print(tr("显示语言已切换为中文。", "Display language changed to English."))
    elif command == "stop":
        app.middleware_enabled = False
        app.runtime_event("info", "middleware_stopped")
        print(tr("中间件功能已停止，仅保留路由转发。", "Middleware features stopped; transparent routing remains enabled."))
    elif command == "start":
        app.middleware_enabled = True
        app.runtime_event("info", "middleware_started")
        print(tr("中间件功能已启动。", "Middleware features started."))
    elif command in {"exit", "quit"}:
        return False
    else:
        print(tr(f"未知命令：{tokens[0]}。输入 help 查看帮助。", f"Unknown command: {tokens[0]}. Enter help for help."))
    return True


def main() -> None:
    config = uvicorn.Config(app.app, host=HOST, port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="middleware-http", daemon=True)
    thread.start()
    while thread.is_alive() and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit(tr(f"中间件启动失败：无法监听 {HOST}:{PORT}", f"Middleware failed to bind {HOST}:{PORT}"))
    app.runtime_event("info", "service_started", host=HOST, port=PORT)
    print(f"llama.cpp KV Middleware {app.app.version}")
    print(f"Agent API: {app.CONFIG['agent'].get('api_base', LOCAL_URL + '/v1')}")
    print(tr("输入 help 查看管理命令，输入 exit 停止服务。", "Enter help for commands, or exit to stop the service."))
    session: PromptSession[str] = PromptSession(completer=COMPLETER, complete_while_typing=False)
    try:
        while True:
            try:
                keep_running = run_command(session.prompt("kv> ").strip())
            except (RuntimeError, ValueError, IndexError) as exc:
                print(tr(f"命令执行失败：{exc}", f"Command failed: {exc}"))
                keep_running = True
            if not keep_running:
                break
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        print(tr("中间件已停止。", "Middleware stopped."))


if __name__ == "__main__":
    main()
