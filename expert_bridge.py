#!/usr/bin/env python3
"""
ExpertBridge — Expert через JSON-контракт (v5.3).
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote

import aiohttp
import yarl

from agents.base import AliceTurnResponse
from alice_server import AliceAgentManager
from local_tools import LocalToolRegistry, DownloadAttachmentTool

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger("expert_bridge")

RE_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)
RE_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
RE_KV = re.compile(
    r"^\s*(?:\d+\.\s*|[-*]\s*)?([a-z][a-z0-9_]*(?:__[a-z0-9_]+)*)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
RE_DOLLAR = re.compile(r"^\s*\$\s+(.+?)\s*$", re.MULTILINE)
RE_INLINE_CODE = re.compile(r"`[^`\n]+`")

RE_FILEGUID = re.compile(
    r'https://rpc\.alice\.yandex\.ru/gproxy/get_source_file\?FileGuid=([a-f0-9-]+)',
    re.IGNORECASE,
)
RE_S3_URL = re.compile(
    r'https://file-service-raw-files\.s3-private\.mds\.yandex\.net/[^\s\'"<>\)\]]+',
    re.IGNORECASE,
)
RE_MD_LINK = re.compile(
    r'\[([^\]]+)\]\((https://(?:rpc\.alice\.yandex\.ru|file-service-raw-files\.s3-private\.mds\.yandex\.net)[^\)]+)\)',
    re.IGNORECASE,
)

DEFAULT_DOWNLOAD_DIR = "~/Downloads"


class ExpertBridge:
    def __init__(
        self,
        manager: AliceAgentManager,
        tools: LocalToolRegistry,
        agent_id: str = "expert",
        download_dir: str = DEFAULT_DOWNLOAD_DIR,
        auto_download: bool = False,
        max_iterations: int = 8,
        approve_callback=None,
        show_thinking: bool = False,
    ):
        self.manager = manager
        self.tools = tools
        self.agent_id = agent_id
        self.download_dir = os.path.expanduser(download_dir)
        self.auto_download = auto_download
        self.max_iterations = max_iterations
        self.approve_callback = approve_callback
        self.show_thinking = show_thinking

        os.makedirs(self.download_dir, exist_ok=True)
        self._contract_established: set = set()
        self._download_context: Dict[str, Optional[str]] = {"dialog_id": None}

        if "download_attachment" not in self.tools.tools:
            async def _dl(url: str, filename: Optional[str]):
                return await self._download(
                    url, filename,
                    dialog_id=self._download_context.get("dialog_id"),
                )
            self.tools.register(DownloadAttachmentTool(downloader=_dl))

    # ---------- contract ----------

    def _build_contract(self) -> str:
        lines = [
            "Давай сразу договоримся о формате взаимодействия. Я пишу CLI-обёртку для нашего диалога, которая парсит твои ответы.",
            "",
            "ФОРМАТ — один из двух:",
            "",
            "1. Нужна моя машина (локальный shell, запись файлов на мой диск, скачивание файлов) → верни ТОЛЬКО JSON, без пояснений до и после, без markdown:",
            '{"tool": "shell", "args": {"cmd": "uname -a"}}',
            "",
            "2. Не нужна моя машина (интернет, Python, файлы в песочнице, OCR, обычный разговор) → отвечай как всегда.",
            "",
            "ФОРМАТ ОТВЕТА С ИНСТРУМЕНТОМ (важно):",
            "1. Одна короткая строка — что собираешься делать (5-15 слов). Без markdown.",
            "2. На следующей строке — JSON-вызов.",
            "3. Несколько инструментов подряд — по строке-описанию на каждый.",
            "4. Финальный ответ (без инструментов) — обычный текст, без JSON.",
            "",
            "Пример:",
            "Проверю систему и количество ядер.",
            '{"tool": "shell", "args": {"cmd": "uname -a; nproc"}}',
            "",
            "Соберу данные по всем препаратам параллельно.",
            '{"tool": "meeseeks_batch", "args": {"tasks": ["expert: ...", "expert: ..."]}}',
            "",
            "По результатам: <обычный финальный ответ>",
            "",
            "Без ```json-блоков, без комментариев после JSON, без пояснений в той же строке.",
            "",
            "Если в сообщении есть JSON — в нём НЕТ НИЧЕГО другого, кроме строки-описания над ним.",
            "",
            "ПРАВИЛА:",
            "1. Read-only по умолчанию. ls, cat, uname, df, pwd, head, tail, wc, file, stat, which, id, date, uptime, ps, du, nproc — без объяснений.",
            "2. Write/delete/modify — только с пометкой «это изменит» в следующем сообщении.",
            "3. Секреты — никогда. Пароли, токены, ключи, .env, .ssh, cookies.",
            "4. Сомневаешься — спроси словами, не стреляй JSON вслепую.",
            "",
            "Опасное (rm, mv, sudo) заблокировано на моей стороне — даже если ты пришлёшь, оно не выполнится.",
            "",
            "ПАЙПЫ — ЯВНО РАЗРЕШЕНЫ. И даже приветствуются.",
            "head, tail, grep, wc, sort, awk, sed, cut, uniq, xargs — используй смело.",
            "",
            "ПРО write_file — ОСОБО ВАЖНО:",
            "Это НЕ shell-команда. Это «сохрани мой же текст в файл».",
            "Не `echo > file`, не `tee`, не `printf >`.",
            '{"tool": "write_file", "args": {"path": "~/Downloads/test.json", "content": "{\\"hello\\":\\"world\\"}"}}',
            "",
            "ПРО download_attachment — ОСОБО ВАЖНО:",
            "Если создала файл у себя (xlsx, csv, pdf, docx, png, zip...) и хочешь отдать на мой диск —",
            "НЕ вставляй просто markdown-ссылку (я её вслепую не скачиваю). Верни JSON-вызов:",
            '{"tool": "download_attachment", "args": {"file_guid": "34a2270-...", "filename": "cbr_rates.xlsx"}}',
            "",
            "FileGuid — uuid из https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid=<uuid>.",
            'Можно также передать "url" вместо file_guid.',
            "Файл сохранится в ~/Downloads. Получишь ответ «Сохранено: /путь (N байт)».",
            "",
            "ДОСТУПНЫЕ TOOLS:",
        ]

        for name, tool in self.tools.tools.items():
            desc = tool.description.strip().split("\n")[0]
            lines.append(f"  • {name} — {desc}")

        lines.extend([
            "",
            "Согласна работать в таком режиме?",
        ])
        return "\n".join(lines)

    def forget_hint(self, dialog_id: str):
        self._contract_established.discard(dialog_id)

    # ---------- parser ----------

    def _parse_json_objects(self, text: str) -> List[Dict]:
        calls: List[Dict] = []
        decoder = json.JSONDecoder()
        i, n = 0, len(text)
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            try:
                obj, end = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(obj, dict):
                name = obj.get("tool") or obj.get("name") or obj.get("action")
                args = obj.get("args") or obj.get("arguments") or obj.get("params") or {}

                if name == "shell" and isinstance(args, dict):
                    if "command" in args and "cmd" not in args:
                        args["cmd"] = args["command"]

                if name and isinstance(name, str):
                    calls.append({"tool": name, "args": args})
            i += end
        return calls

    def _parse_calls(self, text: str) -> List[Dict]:
        if not text:
            return []

        calls = self._parse_json_objects(text)
        if calls:
            return calls

        for raw in RE_TAG.findall(text):
            try:
                obj = json.loads(raw)
                name = obj.get("tool") or obj.get("name")
                args = obj.get("args") or obj.get("arguments") or {}
                if name == "shell" and "command" in args and "cmd" not in args:
                    args["cmd"] = args["command"]
                if name:
                    return [{"tool": name, "args": args}]
            except json.JSONDecodeError:
                continue

        for raw in RE_FENCED_JSON.findall(text):
            try:
                obj = json.loads(raw)
                name = obj.get("tool") or obj.get("name")
                args = obj.get("args") or obj.get("arguments") or {}
                if name == "shell" and "command" in args and "cmd" not in args:
                    args["cmd"] = args["command"]
                if name:
                    return [{"tool": name, "args": args}]
            except json.JSONDecodeError:
                continue

        clean = RE_INLINE_CODE.sub("", text)
        dollar_cmds: List[Dict] = []
        for cmd in RE_DOLLAR.findall(clean):
            cmd = cmd.strip().rstrip(".,;")
            if cmd:
                dollar_cmds.append({"tool": "shell", "args": {"cmd": cmd}})
        if dollar_cmds:
            return dollar_cmds

        known = {n.lower() for n in self.tools.tools.keys()}
        kv_calls: List[Dict] = []
        for name, arg in RE_KV.findall(clean):
            if name.lower() not in known:
                continue
            arg = arg.strip().rstrip(".,;:")
            if arg.startswith("{") or arg.startswith("["):
                try:
                    parsed: Any = json.loads(arg)
                except json.JSONDecodeError:
                    parsed = arg
            else:
                parsed = arg
            if name == "shell" and isinstance(parsed, str):
                parsed = {"cmd": parsed}
            elif name in ("read_file", "list_dir", "attach_file", "download_attachment") and isinstance(parsed, str):
                if name == "download_attachment":
                    parsed = {"file_guid": parsed}
                else:
                    parsed = {"path": parsed}
            elif name == "meeseeks" and isinstance(parsed, str):
                if ":" in parsed:
                    ag, task = parsed.split(":", 1)
                    parsed = {"agent_id": ag.strip(), "task": task.strip()}
                else:
                    parsed = {"task": parsed}
            elif name == "meeseeks_batch" and isinstance(parsed, str):
                parsed = {"tasks": [t.strip() for t in parsed.split("|") if t.strip()]}
            kv_calls.append({"tool": name, "args": parsed})
        return kv_calls

    def _extract_label_and_calls(self, text: str) -> Tuple[str, List[Dict]]:
        """
        Разделяет ответ на (label, calls).
        label = последняя непустая строка до первого JSON-вызова
                (то, что Expert написал как «сейчас сделаю X»).
        """
        if not text:
            return "", []

        decoder = json.JSONDecoder()
        calls: List[Dict] = []
        first_json_start: Optional[int] = None
        i, n = 0, len(text)

        while i < n:
            if text[i] == '{':
                try:
                    obj, end = decoder.raw_decode(text[i:])
                    if isinstance(obj, dict):
                        name = obj.get("tool") or obj.get("name") or obj.get("action")
                        args = obj.get("args") or obj.get("arguments") or obj.get("params") or {}
                        if name and isinstance(name, str):
                            if name == "shell" and isinstance(args, dict) \
                                    and "command" in args and "cmd" not in args:
                                args["cmd"] = args["command"]
                            if first_json_start is None:
                                first_json_start = i
                            calls.append({"tool": name, "args": args})
                            i += end
                            continue
                except json.JSONDecodeError:
                    pass
            i += 1

        if calls and first_json_start is not None:
            prefix = text[:first_json_start].strip()
            lines = [ln.rstrip() for ln in prefix.splitlines() if ln.strip()]
            label = lines[-1] if lines else ""
            return label, calls

        return "", self._parse_calls(text)

    # ---------- downloads ----------

    @staticmethod
    def _ext_from_ct(ct: str) -> str:
        ct_map = {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/pdf": ".pdf",
            "text/csv": ".csv",
            "text/markdown": ".md",
            "application/json": ".json",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "application/zip": ".zip",
        }
        return ct_map.get(ct.split(";")[0].strip(), "")

    async def _download_via_browser(self, url: str, filename: Optional[str] = None, dialog_id: Optional[str] = None) -> Optional[str]:
        if not PLAYWRIGHT_AVAILABLE:
            logger.warning("Playwright не установлен (pip install playwright && playwright install chromium).")
            return None

        logger.info(f"Запускаю Playwright для скачивания (UI-клик): {url[:80]}...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(ignore_https_errors=True)

            pw_cookies = []
            for k, v in self.manager.creds.cookie_dict.items():
                if not k or not v:
                    continue
                k_clean = re.sub(r'[^\x20-\x7E]', '', str(k)).strip()
                v_clean = re.sub(r'[^\x20-\x7E]', '', str(v)).strip()
                if not k_clean or not v_clean:
                    continue
                pw_cookies.append({
                    "name": k_clean,
                    "value": v_clean,
                    "url": "https://yandex.ru"
                })

            try:
                await context.add_cookies(pw_cookies)
            except Exception as e:
                logger.warning(f"Ошибка при массовой установке cookies: {e}.")

            page = await context.new_page()
            try:
                chat_url = f"https://alice.yandex.ru/chat/{dialog_id}" if dialog_id else "https://alice.yandex.ru/chat/"
                await page.goto(chat_url, wait_until="domcontentloaded")

                file_guid = url.split("FileGuid=")[1].split("&")[0]
                link_selector = f'a[href*="{file_guid}"]'

                await page.wait_for_selector(link_selector, timeout=15000)

                async with page.expect_download(timeout=15000) as download_info:
                    await page.click(link_selector)

                download = await download_info.value
                suggested_name = filename or download.suggested_filename
                safe_name = re.sub(r'[^\w.\-]', '_', suggested_name)
                final_path = os.path.join(self.download_dir, safe_name)

                await download.save_as(final_path)
                logger.info(f"✅ Скачано через браузер (UI-клик): {final_path}")
                return final_path
            except Exception as e:
                logger.error(f"❌ Playwright UI-click download failed: {e}")
                return None
            finally:
                await browser.close()

    async def _download(self, url: str, filename: Optional[str], dialog_id: Optional[str] = None) -> Optional[str]:
        is_gproxy = "rpc.alice.yandex.ru/gproxy" in url

        if is_gproxy:
            headers = self.manager.creds.build_rpc_headers()
            request_url: Any = url
        else:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
            request_url = yarl.URL(url, encoded=True)

        name = filename or "file"
        safe = re.sub(r'[^\w.\-]', '_', name)
        dest = os.path.join(self.download_dir, safe)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    request_url, headers=headers, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"download {url[:120]}: HTTP {resp.status}. Пробую Playwright...")
                        if is_gproxy:
                            return await self._download_via_browser(url, filename, dialog_id)
                        return None

                    data = await resp.read()
                    ct = resp.headers.get("Content-Type", "")
                    cd = resp.headers.get("Content-Disposition", "")

            better: Optional[str] = None
            if cd:
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                if m:
                    better = unquote(m.group(1))
            if not better and "response-content-disposition=" in url:
                qs = parse_qs(url.split("?", 1)[1])
                rcd = qs.get("response-content-disposition", [""])[0]
                m = re.search(r'filename="?([^";]+)"?', rcd)
                if m:
                    better = m.group(1)
            if better:
                safe = re.sub(r'[^\w.\-]', '_', better)
                dest = os.path.join(self.download_dir, safe)

            if not os.path.splitext(dest)[1]:
                dest += self._ext_from_ct(ct)

            with open(dest, "wb") as f:
                f.write(data)
            logger.info(f"downloaded → {dest} ({len(data)} B)")
            return dest
        except Exception as e:
            logger.error(f"download {url[:120]}: {e}")
            if is_gproxy:
                return await self._download_via_browser(url, filename, dialog_id)
            return None

    def _extract_files(self, turn: AliceTurnResponse, full_text: str) -> List[Dict[str, str]]:
        found: Dict[str, Dict[str, str]] = {}

        def add(raw_url: str, title: str = ""):
            if not raw_url:
                return
            if "FileGuid=" in raw_url:
                guid = raw_url.split("FileGuid=")[1].split("&")[0].strip()
                url = f"https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid={guid}"
                key = f"fg:{guid}"
                default_title = title or f"expert_{guid[:8]}"
            elif "file-service-raw-files.s3-private.mds.yandex.net" in raw_url:
                url = raw_url.rstrip(".,;:")
                key = url.split("?")[0]
                default_title = title or "file"
            else:
                return
            if key in found:
                return
            found[key] = {"url": url, "title": default_title}

        for src in (turn.sources or []):
            url = src.get("FullUrl", "")
            title = src.get("DocumentTitle") or ""
            add(url, title)
            pv = src.get("preview_url") or (src.get("InputFileItemInfo") or {}).get("FileIcon")
            if pv and "s3-private" in pv:
                add(pv, title or "preview")

        for title, url in RE_MD_LINK.findall(full_text or ""):
            add(url, title)

        for guid in RE_FILEGUID.findall(full_text or ""):
            add(f"https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid={guid}")

        for url in RE_S3_URL.findall(full_text or ""):
            add(url)

        return list(found.values())

    # ---------- execute ----------

    async def _execute(self, name: str, args: Dict, dialog_id: Optional[str] = None) -> str:
        if not name:
            return "[ошибка]: пустое имя инструмента"

        if dialog_id:
            self._download_context["dialog_id"] = dialog_id

        if name == "write_file" and isinstance(args, dict) and args.get("_expect_content_from_text"):
            return ('[нужно содержимое]: используй JSON-формат — '
                    '{"tool": "write_file", "args": {"path": "...", "content": "..."}}')

        if self.approve_callback:
            try:
                approved = await self.approve_callback(name, args)
            except Exception:
                approved = False
            if not approved:
                return f"[отклонено пользователем]: {name}"

        res = await self.tools.execute(name, args)
        if res.success:
            out = res.output or "(пусто)"
            if len(out) > 4000:
                out = out[:4000] + f"\n... [обрезано {len(out)} B]"
            return out
        return f"[ошибка {name}]: {res.error or res.output or 'unknown'}"

    # ---------- main ----------

    async def _establish_contract(self, dialog_id: str) -> Tuple[str, Optional[str], List[Dict]]:
        if dialog_id in self._contract_established:
            return dialog_id, None, []

        self._download_context["dialog_id"] = dialog_id or None
        contract = self._build_contract()

        await asyncio.sleep(1.5)

        ack_text: Optional[str] = None

        async for turn in self.manager.send_message(
            text=contract, agent_id=self.agent_id, dialog_id=dialog_id,
        ):
            if turn.dialog_id:
                dialog_id = turn.dialog_id
                self._download_context["dialog_id"] = dialog_id
            if turn.text and turn.is_finished:
                ack_text = turn.text

        refuse_markers = ["не могу", "нет инструмент", "притормозим", "не сработает", "нет канала", "так не выйдет"]
        if ack_text and any(m in ack_text.lower() for m in refuse_markers):
            retry = (
                "Уточню, чтобы не было недопонимания: JSON исполняю Я, не ты. "
                "Ты просто пишешь строку — я нажимаю Enter. "
                "Ты не получаешь доступ к моей машине — ты даёшь мне команду текстом. "
                "Если готова — просто ответь JSON-вызовом, без предисловий."
            )
            async for turn in self.manager.send_message(
                text=retry, agent_id=self.agent_id, dialog_id=dialog_id,
            ):
                if turn.dialog_id:
                    dialog_id = turn.dialog_id
                    self._download_context["dialog_id"] = dialog_id
                if turn.text and turn.is_finished:
                    ack_text = turn.text

        self._contract_established.add(dialog_id)
        ack_calls = self._parse_calls(ack_text) if ack_text else []
        return dialog_id, ack_text, ack_calls

    async def send_with_tools(
        self,
        user_text: str,
        dialog_id: Optional[str] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        final_dialog_id = dialog_id or ""
        self._download_context["dialog_id"] = final_dialog_id or None

        ack_results: List[str] = []

        if final_dialog_id not in self._contract_established:
            yield AliceTurnResponse(
                dialog_id=final_dialog_id,
                request_id="exp_contract",
                thinking_logs=["🤝 Устанавливаю контракт с Expert (JSON)..."],
                is_finished=False,
            )

            final_dialog_id, ack, ack_calls = await self._establish_contract(final_dialog_id)
            self._download_context["dialog_id"] = final_dialog_id or None

            if ack:
                if ack_calls:
                    for call in ack_calls:
                        name = call["tool"]
                        args = call["args"]
                        yield AliceTurnResponse(
                            dialog_id=final_dialog_id,
                            request_id=f"exp_ack_tool_{name}",
                            thinking_logs=[
                                f"🤝 Expert (ack): {json.dumps(call, ensure_ascii=False)[:160]}"
                            ],
                            is_finished=False,
                        )
                        result = await self._execute(name, args, dialog_id=final_dialog_id)
                        preview = result if len(result) <= 800 else result[:800] + f"… ({len(result)} B)"
                        yield AliceTurnResponse(
                            dialog_id=final_dialog_id,
                            request_id=f"exp_ack_res_{name}",
                            thinking_logs=[f"→ {name}:\n{preview}"],
                            is_finished=False,
                        )
                        ack_results.append(f"Результат `{name}`:\n{result}")
                else:
                    short = ack if len(ack) <= 300 else ack[:300] + "…"
                    yield AliceTurnResponse(
                        dialog_id=final_dialog_id,
                        request_id="exp_contract_ack",
                        thinking_logs=[f"🤝 Expert: {short}"],
                        is_finished=False,
                    )

        if ack_results:
            current = (
                "Я выполнил команду, которую ты прислала в ответ на контракт:\n\n"
                + "\n\n".join(ack_results)
                + f"\n\nТеперь моя задача: {user_text}"
            )
        else:
            current = user_text

        for iteration in range(self.max_iterations):
            yield AliceTurnResponse(
                dialog_id=final_dialog_id,
                request_id=f"exp_wait_{iteration}",
                thinking_logs=[f"⏳ Итерация {iteration + 1}"],
                is_finished=False,
            )

            final_turn: Optional[AliceTurnResponse] = None
            prev_thinking_count = 0

            async for turn in self.manager.send_message(
                text=current, agent_id=self.agent_id, dialog_id=final_dialog_id,
            ):
                if turn.dialog_id:
                    final_dialog_id = turn.dialog_id
                    self._download_context["dialog_id"] = final_dialog_id or None

                # STREAM: каждая новая порция thinking_steps сразу в CLI.
                if len(turn.thinking_steps) > prev_thinking_count:
                    new_steps = turn.thinking_steps[prev_thinking_count:]
                    prev_thinking_count = len(turn.thinking_steps)
                    yield AliceTurnResponse(
                        dialog_id=final_dialog_id,
                        request_id=f"exp_think_{iteration}_{prev_thinking_count}",
                        thinking_steps=new_steps,
                        is_finished=False,
                    )

                final_turn = turn

            if not final_turn:
                yield AliceTurnResponse(
                    dialog_id=final_dialog_id, request_id="exp_empty",
                    text="", is_finished=True, error="Пустой ответ от Expert",
                )
                return

            if final_turn.error:
                yield final_turn
                return

            raw_text = final_turn.text or ""
            label, calls = self._extract_label_and_calls(raw_text)

            if calls:
                for f in self._extract_files(final_turn, raw_text):
                    logger.info(f"[intermediate file] {f['url'][:140]}")

            if not calls:
                yield AliceTurnResponse(
                    dialog_id=final_turn.dialog_id,
                    request_id=final_turn.request_id,
                    thinking_logs=list(final_turn.thinking_logs),
                    thinking_steps=[],  # уже стримились выше
                    text=raw_text,
                    widgets=list(final_turn.widgets),
                    suggestions=list(final_turn.suggestions),
                    sources=list(final_turn.sources),
                    is_finished=True,
                    error=None,
                )

                files = self._extract_files(final_turn, raw_text)
                if files and self.auto_download:
                    dl_logs = []
                    for f in files:
                        local = await self._download(f["url"], f["title"], dialog_id=final_dialog_id)
                        if local:
                            dl_logs.append(f"📥 {f['title']} → {local}")
                        else:
                            dl_logs.append(f"⚠ {f['title']} — не скачалось")
                    if dl_logs:
                        yield AliceTurnResponse(
                            dialog_id=final_dialog_id,
                            request_id=f"exp_dl_{iteration}",
                            thinking_logs=dl_logs,
                            is_finished=False,
                        )
                elif files:
                    yield AliceTurnResponse(
                        dialog_id=final_dialog_id,
                        request_id=f"exp_files_hint_{iteration}",
                        thinking_logs=[
                            f"📎 В ответе {len(files)} ссылок на файлы. "
                            f"Авто-скачивание выключено. Скажи «скачай» — "
                            f"Expert вызовет download_attachment явно."
                        ],
                        is_finished=False,
                    )
                return

            if label:
                yield AliceTurnResponse(
                    dialog_id=final_dialog_id,
                    request_id=f"exp_label_{iteration}",
                    thinking_logs=[f"▸ {label}"],
                    is_finished=False,
                )

            results_for_expert: List[str] = []
            for call in calls:
                name = call["tool"]
                args = call["args"]

                yield AliceTurnResponse(
                    dialog_id=final_dialog_id,
                    request_id=f"exp_tool_{iteration}_{name}",
                    thinking_logs=[f"🔧 {name}: {json.dumps(args, ensure_ascii=False)[:200]}"],
                    is_finished=False,
                )

                result = await self._execute(name, args, dialog_id=final_dialog_id)

                preview = result if len(result) <= 800 else result[:800] + f"… ({len(result)} B)"
                yield AliceTurnResponse(
                    dialog_id=final_dialog_id,
                    request_id=f"exp_res_{iteration}_{name}",
                    thinking_logs=[f"→ {name}:\n{preview}"],
                    is_finished=False,
                )

                results_for_expert.append(f"Результат `{name}`:\n{result}")

            current = (
                "\n\n".join(results_for_expert)
                + "\n\nЕсли нужен следующий инструмент — сначала строка-описание, "
                  "потом JSON. Если готов финальный ответ — обычный текст."
            )

        yield AliceTurnResponse(
            dialog_id=final_dialog_id,
            request_id="exp_max_iter",
            text=f"\n[Лимит итераций инструментов: {self.max_iterations}]",
            is_finished=True,
        )