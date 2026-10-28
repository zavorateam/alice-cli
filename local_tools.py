#!/usr/bin/env python3
"""
Local Tool Registry: shell, файлы (expanduser, root-jail), MCP stdio, WriteFile.
"""
import asyncio
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("local_tools")
MAX_OUTPUT_BYTES = 8192
SHELL_META = [";", "|", "&", "`", "$(", ">", "<", "\n"]


@dataclass
class ToolResult:
    success: bool
    output: str
    error: Optional[str] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)


class LocalTool:
    name: str = ""
    description: str = ""
    arg_schema: Dict[str, Any] = {}

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    def describe(self) -> str:
        props = self.arg_schema.get("properties", {})
        required = set(self.arg_schema.get("required", []))
        parts = [f"{n}{'*' if n in required else ''}:{d.get('type', 'any')}"
                 for n, d in props.items()]
        args_str = ", ".join(parts) if parts else "—"
        return f"- {self.name}({args_str}): {self.description}"


class ShellTool(LocalTool):
    """
    strict=True  → shell=False, exec, whitelist + запрет метасимволов.
    strict=False → shell=True, пайпы разрешены, опасное блокируется blocklist'ом
                   (первый токен каждой команды в цепочке ;|&&||).
    """
    name = "shell"
    description = "Выполнить shell-команду на машине пользователя. Возвращает stdout+stderr."
    arg_schema = {
        "type": "object",
        "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"],
    }

    def __init__(
        self,
        allowed: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 15.0,
        strict: bool = True,
        blocklist: Optional[List[str]] = None,
    ):
        self.allowed = allowed
        self.cwd = os.path.expanduser(cwd) if cwd else os.getcwd()
        self.timeout = timeout
        self.strict = strict
        self.blocklist = blocklist or []
        self._forbidden_chars = SHELL_META

    @staticmethod
    def _extract_command_heads(cmd: str) -> List[str]:
        parts = re.split(r"(?:;|\||&&|\|\|)", cmd)
        heads: List[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            try:
                tokens = shlex.split(part)
            except ValueError:
                tokens = part.split()
            if tokens:
                head = os.path.basename(tokens[0].strip())
                heads.append(head)
        return heads

    def _check_blocklist(self, cmd: str) -> Optional[str]:
        if not self.blocklist:
            return None
        heads = self._extract_command_heads(cmd)
        blocked = set(self.blocklist)
        for head in heads:
            if head in blocked:
                return f"Команда {head!r} заблокирована (blocklist)"
        return None

    def _check_strict(self, cmd: str) -> Optional[str]:
        if not self.strict:
            return None
        for ch in self._forbidden_chars:
            if ch in cmd:
                return f"Запрещённый символ {ch!r} (strict mode)"
        return None

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        cmd = (args.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(False, "", "Пустая команда")

        block_err = self._check_blocklist(cmd)
        if block_err:
            return ToolResult(False, "", block_err)

        strict_err = self._check_strict(cmd)
        if strict_err:
            return ToolResult(False, "", strict_err)

        tokens: Optional[List[str]] = None
        if self.strict and self.allowed:
            try:
                tokens = shlex.split(cmd)
            except ValueError as e:
                return ToolResult(False, "", f"Ошибка разбора: {e}")
            if not tokens:
                return ToolResult(False, "", "Пустая команда")
            if tokens[0] not in self.allowed:
                return ToolResult(False, "", f"Команда {tokens[0]!r} не в whitelist")

        if self.strict:
            if tokens is None:
                tokens = shlex.split(cmd)
            tokens = [os.path.expanduser(t) if t.startswith("~") else t for t in tokens]
            proc = await asyncio.create_subprocess_exec(
                *tokens, cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(False, "", f"Таймаут {self.timeout}s")
        except asyncio.CancelledError:
            proc.kill()
            raise

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        combined = out + (f"\n[stderr]\n{err}" if err else "")
        if len(combined) > MAX_OUTPUT_BYTES:
            combined = combined[:MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(combined)}B]"
        return ToolResult(proc.returncode == 0, combined,
                          None if proc.returncode == 0 else f"exit {proc.returncode}")


class ReadFileTool(LocalTool):
    name = "read_file"
    description = "Прочитать текстовый файл (UTF-8)."
    arg_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def __init__(self, root: str = "/", max_bytes: int = MAX_OUTPUT_BYTES):
        self.root = os.path.realpath(os.path.expanduser(root))
        self.max_bytes = max_bytes

    async def execute(self, args):
        path = (args.get("path") or "").strip()
        if not path:
            return ToolResult(False, "", "Пустой path")
        real = os.path.realpath(os.path.expanduser(path))
        if not (real == self.root or real.startswith(self.root + os.sep)):
            return ToolResult(False, "", f"Путь вне {self.root} (задайте files.root в конфиге)")
        try:
            with open(real, "rb") as f:
                data = f.read(self.max_bytes + 1)
            truncated = len(data) > self.max_bytes
            text = data[:self.max_bytes].decode("utf-8", errors="replace")
            if truncated:
                text += "\n... [truncated]"
            return ToolResult(True, text)
        except Exception as e:
            return ToolResult(False, "", str(e))


class ListDirTool(LocalTool):
    name = "list_dir"
    description = "Список файлов в директории."
    arg_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def __init__(self, root: str = "/"):
        self.root = os.path.realpath(os.path.expanduser(root))

    async def execute(self, args):
        path = (args.get("path") or "").strip()
        if not path:
            return ToolResult(False, "", "Пустой path")
        real = os.path.realpath(os.path.expanduser(path))
        if not (real == self.root or real.startswith(self.root + os.sep)):
            return ToolResult(
                False, "",
                f"Путь вне {self.root}. Текущий root — задайте files.root в alice_tools.json"
            )
        try:
            entries = []
            for name in sorted(os.listdir(real)):
                full = os.path.join(real, name)
                kind = "DIR " if os.path.isdir(full) else "FILE"
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                entries.append(f"{kind} {size:>8} {name}")
            return ToolResult(True, "\n".join(entries) or "(пусто)")
        except Exception as e:
            return ToolResult(False, "", str(e))


class AttachFileTool(LocalTool):
    name = "attach_file"
    description = (
        "Прикрепить локальный файл к чату — Алиса сможет его увидеть "
        "(OCR, изображения, документы, PDF, DOCX). Файл прикрепляется к следующему сообщению."
    )
    arg_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["path"],
    }

    def __init__(self, root: str = "/", max_bytes: int = 20 * 1024 * 1024):
        self.root = os.path.realpath(os.path.expanduser(root))
        self.max_bytes = max_bytes

    async def execute(self, args):
        path = (args.get("path") or "").strip()
        if not path:
            return ToolResult(False, "", "Пустой path")
        real = os.path.realpath(os.path.expanduser(path))
        if not (real == self.root or real.startswith(self.root + os.sep)):
            return ToolResult(False, "", f"Путь вне {self.root}")
        if not os.path.isfile(real):
            return ToolResult(False, "", f"Файл не найден: {real}")
        size = os.path.getsize(real)
        if size > self.max_bytes:
            return ToolResult(False, "", f"Файл слишком большой: {size} > {self.max_bytes}")

        title = args.get("title") or os.path.basename(real)
        return ToolResult(
            success=True,
            output=f"Файл {title} ({size} байт) подготовлен к прикреплению.",
            attachments=[{"path": real, "title": title}],
        )


class WriteFileTool(LocalTool):
    name = "write_file"
    description = (
        "Сохранить текст в файл на диске пользователя. "
        "Создаёт директории. Используй для .txt, .csv, .md, .py, .json, .html, .yaml."
    )
    arg_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Абсолютный путь или относительно write_root"},
            "content": {"type": "string", "description": "Содержимое файла"},
            "mode": {"type": "string", "enum": ["w", "a"], "default": "w",
                     "description": "w = перезаписать, a = дозаписать"},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, root: str = "~/Downloads", max_bytes: int = 10 * 1024 * 1024):
        self.root = os.path.realpath(os.path.expanduser(root))
        self.max_bytes = max_bytes

    async def execute(self, args):
        path = (args.get("path") or "").strip()
        content = args.get("content", "")
        mode = args.get("mode", "w")
        encoding = args.get("encoding", "utf-8")

        if not path:
            return ToolResult(False, "", "Пустой path")
        if mode not in ("w", "a"):
            return ToolResult(False, "", f"Неверный mode: {mode}")

        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.root, expanded)
        real = os.path.realpath(expanded)

        if not (real == self.root or real.startswith(self.root + os.sep)):
            return ToolResult(
                False, "",
                f"Путь вне write_root ({self.root}). Используй путь внутри {self.root}"
            )

        size = len(content.encode(encoding, errors="replace"))
        if size > self.max_bytes:
            return ToolResult(False, "", f"Содержимое слишком большое: {size} > {self.max_bytes}")

        try:
            os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
            with open(real, mode, encoding=encoding) as f:
                f.write(content)
            verb = "дописан" if mode == "a" else "сохранён"
            return ToolResult(True, f"Файл {verb}: {real} ({size} байт)")
        except Exception as e:
            return ToolResult(False, "", f"Ошибка записи: {e}")


class McpStdioClient:
    def __init__(self, name: str, command: List[str], env: Optional[Dict[str, str]] = None):
        self.name = name
        self.command = command
        self.env = {**os.environ, **(env or {})}
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._tasks: List[asyncio.Task] = []
        self.tools: List[Dict[str, Any]] = []

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env,
        )
        self._tasks.append(asyncio.create_task(self._reader_loop()))
        self._tasks.append(asyncio.create_task(self._stderr_loop()))
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "alice-tools-bridge", "version": "1.2.0"},
        })
        await self._notify("notifications/initialized", {})
        resp = await self._request("tools/list", {})
        self.tools = resp.get("tools", [])

    async def _reader_loop(self):
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result", {}))

    async def _stderr_loop(self):
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            logger.debug(f"[{self.name}] {line.decode(errors='replace').rstrip()}")

    async def _request(self, method: str, params: Dict) -> Dict:
        self._req_id += 1
        rid = self._req_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        ) + "\n").encode())
        await self.proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=30.0)

    async def _notify(self, method: str, params: Dict):
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params}
        ) + "\n").encode())
        await self.proc.stdin.drain()

    async def call_tool(self, name: str, args: Dict) -> Dict:
        return await self._request("tools/call", {"name": name, "arguments": args})

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except Exception:
                self.proc.kill()


class McpProxyTool(LocalTool):
    def __init__(self, server: str, tool_def: Dict, client: McpStdioClient):
        self.server = server
        self.client = client
        self.remote_name = tool_def["name"]
        self.name = f"mcp__{server}__{self.remote_name}"
        self.description = tool_def.get("description", "") or f"MCP {server}/{self.remote_name}"
        self.arg_schema = tool_def.get("inputSchema", {"type": "object", "properties": {}})

    async def execute(self, args):
        try:
            result = await self.client.call_tool(self.remote_name, args)
        except Exception as e:
            return ToolResult(False, "", str(e))
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        combined = "\n".join(parts)
        if len(combined) > MAX_OUTPUT_BYTES:
            combined = combined[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
        is_err = bool(result.get("isError"))
        return ToolResult(not is_err, combined, combined if is_err else None)


class LocalToolRegistry:
    def __init__(self):
        self.tools: Dict[str, LocalTool] = {}
        self._clients: List[McpStdioClient] = []

    def register(self, tool: LocalTool):
        self.tools[tool.name] = tool

    async def add_mcp_server(self, name: str, command: List[str],
                             env: Optional[Dict[str, str]] = None) -> int:
        client = McpStdioClient(name, command, env)
        await client.start()
        self._clients.append(client)
        for td in client.tools:
            self.register(McpProxyTool(name, td, client))
        logger.info(f"MCP '{name}': {len(client.tools)} инструментов")
        return len(client.tools)

    def describe_for_prompt(self, max_chars: int = 4000) -> str:
        parts, total = [], 0
        for tool in self.tools.values():
            line = tool.describe()
            if total + len(line) > max_chars:
                parts.append(f"- ... (ещё {len(self.tools) - len(parts)} опущено)")
                break
            parts.append(line)
            total += len(line)
        return "\n".join(parts) if parts else "(нет инструментов)"

    async def execute(self, name: str, args: Dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(False, "", f"Неизвестный инструмент: {name}")
        try:
            return await tool.execute(args or {})
        except Exception as e:
            return ToolResult(False, "", f"Ошибка: {e}")

    async def close(self):
        for c in self._clients:
            await c.close()


async def load_tools_from_config_async(config_path: str) -> LocalToolRegistry:
    reg = LocalToolRegistry()
    if not config_path or not os.path.exists(config_path):
        return reg

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    sc = cfg.get("shell") or {}
    if sc.get("enabled", True):
        if not sc.get("strict", True):
            logger.warning(
                "shell.strict=false: пайпы и && разрешены. blocklist по первым "
                "токенам не является security boundary — команда с обфускацией "
                "или подобранным путём может обойти его. Используйте только "
                "для доверенных агентов/пользователей."
            )
        reg.register(ShellTool(
            allowed=sc.get("allowed_commands"),
            cwd=sc.get("working_directory"),
            timeout=float(sc.get("timeout_sec", 15.0)),
            strict=bool(sc.get("strict", True)),
            blocklist=sc.get("blocked_commands"),
        ))

    fc = cfg.get("files") or {}
    if fc:
        # Безопасные дефолты: НЕ "/" (открывает всю ФС). Если пользователь
        # явно хочет "/", пусть пишет "root": "/" и получает warning в логе.
        read_root = fc.get("root", "~")
        write_root = fc.get("write_root", "~/Downloads")
        # Защита от случайного root="/" (или пустой строки, которая в
        # os.path.expanduser → "" → realpath("") → cwd, что ещё хуже)
        if read_root in ("", "/"):
            logger.warning(
                f"files.root={read_root!r} открывает широкий доступ к ФС; "
                f"используем '~' как безопасный дефолт. Если нужен именно "
                f"'/' — задайте явно в конфиге и понимайте риск."
            )
            read_root = "~"
        if write_root in ("", "/"):
            logger.warning(f"files.write_root={write_root!r} заменён на '~/Downloads'")
            write_root = "~/Downloads"
        if fc.get("read_enabled", True):
            reg.register(ReadFileTool(root=read_root))
        if fc.get("list_enabled", True):
            reg.register(ListDirTool(root=read_root))
        if fc.get("attach_enabled", True):
            reg.register(AttachFileTool(
                root=read_root,
                max_bytes=int(fc.get("attach_max_bytes", 20 * 1024 * 1024)),
            ))
        if fc.get("write_enabled", True):
            reg.register(WriteFileTool(root=write_root, max_bytes=int(fc.get("write_max_bytes", 10 * 1024 * 1024))))

    for name, server in (cfg.get("mcpServers") or {}).items():
        cmd = [server["command"]] + list(server.get("args", []))
        try:
            await reg.add_mcp_server(name, cmd, server.get("env"))
        except Exception as e:
            logger.warning(f"MCP '{name}' не запустился: {e}")

    return reg

class DownloadAttachmentTool(LocalTool):
    """
    Скачать файл, созданный агентом в чате (xlsx, pdf, png, ...),
    на диск пользователя. В отличие от blind-scan — только по явному
    вызову агента: {"tool": "download_attachment", "args": {...}}.

    Регистрируется ExpertBridge с downloader'ом, привязанным к _download()
    (уже умеет RPC-headers + Playwright fallback для gproxy).
    """
    name = "download_attachment"
    description = (
        "Скачать файл, созданный агентом в чате, на диск пользователя. "
        "Передай file_guid (uuid из ссылки get_source_file) или полный url. "
        "Файл сохранится в ~/Downloads (или download_dir ExpertBridge). "
        "После вызова вернётся путь и размер — можешь сказать пользователю, что файл сохранён."
    )
    arg_schema = {
        "type": "object",
        "properties": {
            "file_guid": {
                "type": "string",
                "description": "uuid из https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid=<uuid>",
            },
            "url": {
                "type": "string",
                "description": "Полный URL файла (альтернатива file_guid)",
            },
            "filename": {
                "type": "string",
                "description": "Имя файла на диске (опционально; иначе из Content-Disposition)",
            },
        },
    }

    def __init__(self, downloader):
        """
        downloader: async callable(url: str, filename: Optional[str]) -> Optional[str]
        Возвращает путь к сохранённому файлу или None при ошибке.
        """
        self._downloader = downloader

    async def execute(self, args):
        file_guid = (args.get("file_guid") or "").strip()
        url = (args.get("url") or "").strip()
        filename = args.get("filename")

        if file_guid:
            url = f"https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid={file_guid}"
        if not url:
            return ToolResult(False, "", "Нужен file_guid или url")

        try:
            path = await self._downloader(url, filename)
        except Exception as e:
            return ToolResult(False, "", f"download error: {e}")

        if not path:
            return ToolResult(False, "", "Не удалось скачать (HTTP + Playwright оба вернули None)")

        size = 0
        try:
            size = os.path.getsize(path)
        except Exception:
            pass
        return ToolResult(True, f"Сохранено: {path} ({size} байт)")