#!/usr/bin/env python3
"""
Alice Tools CLI. Tools + Meeseeks + Expert.
Expert идёт через ExpertBridge (chat-based tool_call), Pro/Base — через LocalToolBridge.
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

from agents import registry
from agents.base import ThinkingStep
from alice_auth import load_or_request_credentials, verify_credentials
from alice_cli import (
    AliceCliSession, AliceCompleter,
    RESET, BOLD, DIM, ALICE_RED, ACCENT_CYAN, ACCENT_GREEN, ACCENT_YELLOW, TEXT_MUTED,
    readline,
)
from alice_server import AliceAgentManager
from local_tools import LocalToolRegistry, load_tools_from_config_async
from tool_bridge import LocalToolBridge
from expert_bridge import ExpertBridge
from meeseeks import MeeseeksPool, MeeseeksTool, MeeseeksBatchTool

DEFAULT_CONFIG = "alice_tools.json"


class AliceToolsSession(AliceCliSession):
    def __init__(
        self,
        manager: AliceAgentManager,
        default_agent: str = "pro",
        ephemeral: bool = False,
        tools: Optional[LocalToolRegistry] = None,
        bridge: Optional[LocalToolBridge] = None,
        meeseeks_pool: Optional[MeeseeksPool] = None,
        yolo: bool = False,
        show_thinking: bool = False,
    ):
        super().__init__(manager=manager, default_agent=default_agent, ephemeral=ephemeral)
        self.tools = tools
        self.bridge = bridge
        self.meeseeks_pool = meeseeks_pool
        self.yolo = yolo
        self.show_thinking = show_thinking
        self._pending_attach: Optional[List[Dict[str, Any]]] = None

        self.expert_bridge: Optional[ExpertBridge] = None
        if tools:
            self.expert_bridge = ExpertBridge(
                manager=manager,
                tools=tools,
                agent_id="expert",
                download_dir="~/Downloads",
                auto_download=True,
                max_iterations=8,
                approve_callback=(None if yolo else self._approve_tool),
                show_thinking=show_thinking,
            )

        self._setup_extended_autocomplete()

    def _setup_extended_autocomplete(self):
        if not readline:
            return
        commands = [
            "/help", "help", "/?", "/agent", "agent", "/agents", "agents", "/list", "list",
            "/new", "new", "/dialogs", "dialogs", "/ephemeral", "ephemeral",
            "/tools", "tools", "/exec", "exec", "/attach", "attach", "/yolo", "yolo",
            "/expert", "expert",
            "/meeseeks", "meeseeks", "/meeseeks-batch", "meeseeks-batch",
            "/clear", "clear", "/exit", "exit", "/bye", "bye",
        ]
        agents = list(registry.all_aliases().keys())
        readline.set_completer(AliceCompleter(commands, agents).complete)
        readline.parse_and_bind("tab: complete")

    def _expert_badge(self) -> str:
        left = getattr(self.manager.ws, "expert_trial_left", None)
        limit = getattr(self.manager.ws, "expert_trial_limit", None)
        if left is None:
            return ""
        color = ACCENT_GREEN if left > 1 else ACCENT_YELLOW
        return f" {color}[Эксперт: {left}/{limit}]{RESET}"

    def print_banner(self):
        agent = registry.get(self.current_agent)
        eph = f"{ACCENT_YELLOW}[Временный: вкл]{RESET}" if self.is_ephemeral else f"{DIM}[Временный: выкл]{RESET}"
        tools_info = f"{ACCENT_GREEN}[Инструменты: {len(self.tools.tools)}]{RESET}" if self.tools else f"{DIM}[Инструменты: выкл]{RESET}"
        msk = f" {ACCENT_GREEN}[Мисиксы: {self.meeseeks_pool.max_depth} ур.]{RESET}" if self.meeseeks_pool else ""
        yolo_info = f" {ACCENT_YELLOW}[YOLO]{RESET}" if self.yolo else ""
        think_info = f" {ACCENT_CYAN}[THINKING]{RESET}" if self.show_thinking else ""
        expert_info = f" {ACCENT_YELLOW}[Expert: chat tool-call]{RESET}" if agent.id == "expert" and self.expert_bridge else ""
        print(f"""
{ALICE_RED}{BOLD}    ▲   {RESET} {BOLD}Yandex Alice AI CLI{RESET} {DIM}+ tools + meeseeks + expert{RESET}
{ALICE_RED}{BOLD}    █   {RESET} {TEXT_MUTED}Режим:{RESET} {BOLD}{ACCENT_CYAN}[{agent.name}]{RESET} {DIM}({agent.type.upper()}){RESET} {eph} {tools_info}{msk}{yolo_info}{think_info}{self._expert_badge()}{expert_info}
{ALICE_RED}{BOLD}    ▼   {RESET} {TEXT_MUTED}help / tools{RESET}
""")

    def print_help(self):
        tools_block = ""
        if self.tools and self.tools.tools:
            tools_block = f"\n{BOLD}Инструменты:{RESET}\n" + "\n".join(
                f"  {ACCENT_YELLOW}{name}{RESET} — {tool.description}"
                for name, tool in self.tools.tools.items()
            ) + "\n"
        print(f"""
{BOLD}Команды:{RESET}
  {ACCENT_CYAN}list{RESET} (agents)            Список агентов
  {ACCENT_CYAN}agent <имя>{RESET}            Переключить (pro / base / expert / deep_research)
  {ACCENT_CYAN}expert{RESET}                 Переключить в Эксперт
  {ACCENT_CYAN}new{RESET}                    Новый диалог
  {ACCENT_CYAN}ephemeral on|off{RESET}       Авто-удаление
  {ACCENT_CYAN}dialogs{RESET}                Активные диалоги
  {ACCENT_CYAN}tools{RESET}                  Список инструментов
  {ACCENT_CYAN}exec <cmd>{RESET}             Shell-команда напрямую
  {ACCENT_CYAN}attach <path>{RESET}          Прикрепить файл к следующему сообщению
  {ACCENT_CYAN}meeseeks [@agent] <task>{RESET}     Один мисикс
  {ACCENT_CYAN}meeseeks-batch <t1> | <t2>{RESET}   Параллельные мисиксы (agent:task)
  {ACCENT_CYAN}yolo on|off{RESET}            Авто-одобрение
  {ACCENT_CYAN}clear{RESET} / {ACCENT_CYAN}help{RESET} / {ACCENT_CYAN}exit{RESET}
{tools_block}""")

    def _render_thinking(self, steps: List[ThinkingStep]):
        if not steps:
            return
        print(f"\n{ACCENT_CYAN}{'─' * 20} Размышления {'─' * 20}{RESET}")
        for step in steps:
            if step.kind == "reasoning_title" and step.title:
                print(f"{ACCENT_CYAN}▸ {step.title}{RESET}")
            elif step.kind == "reasoning_content" and step.content:
                for line in step.content.splitlines():
                    print(f"{TEXT_MUTED}  {line}{RESET}")
            elif step.kind == "tool":
                print(f"{ACCENT_YELLOW}  ⚙ [{step.tool}] {step.title or ''}{RESET}")
        print(f"{ACCENT_CYAN}{'─' * 54}{RESET}\n")

    def _render_sources(self, sources: List[Dict[str, Any]]):
        if not sources:
            return
        print(f"{TEXT_MUTED}Источники:{RESET}")
        for s in sources[:10]:
            title = s.get("DocumentTitle") or s.get("ForcedShortUrl") or "source"
            url = s.get("FullUrl") or ""
            print(f"  {TEXT_MUTED}• {title[:70]}{RESET}" + (f" — {url[:60]}..." if url else ""))
        print()

    def _render_thinking_logs(self, logs: List[str]):
        for log in logs:
            if log.startswith("🔧"):
                print(f"{ACCENT_YELLOW}{log}{RESET}")
            elif log.startswith("→"):
                head, _, tail = log.partition("\n")
                print(f"{ACCENT_GREEN}{head}{RESET}")
                if tail:
                    print(f"{TEXT_MUTED}{tail}{RESET}")
            elif log.startswith("✗") or log.startswith("⚠") or log.startswith("⛔"):
                print(f"\033[31m{log}{RESET}")
            elif log.startswith("📎") or log.startswith("📥"):
                print(f"{ACCENT_GREEN}{log}{RESET}")
            elif log.startswith("💻"):
                print(f"{ACCENT_YELLOW}{log}{RESET}")
            else:
                print(f"{TEXT_MUTED}{log}{RESET}")

    async def _approve_tool(self, name: str, call_args: Dict[str, Any]) -> bool:
        if self.yolo:
            return True
        try:
            ans = await asyncio.get_event_loop().run_in_executor(
                None, input,
                f"{ACCENT_YELLOW}Разрешить {BOLD}{name}{RESET}{ACCENT_YELLOW}? [y/N] >>> {RESET}",
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() in ("y", "yes", "д", "да", "1")

    async def _on_meeseeks_event(self, kind: str, handle: str, agent_id: str, info: str):
        prefix = {
            "start": f"{ACCENT_CYAN}🧞 ▶{RESET}",
            "done":  f"{ACCENT_GREEN}🧞 ✓{RESET}",
            "fail":  "\033[31m🧞 ✗\033[0m",
            "deny":  "\033[31m🧞 ⛔\033[0m",
            "warn":  f"{ACCENT_YELLOW}🧞 ⚠{RESET}",
        }.get(kind, f"🧞 {kind}")
        print(f"{prefix} [{handle}] {DIM}{agent_id}{RESET}: {info}")

    async def _execute_turn_expert(self, user_text: str):
        assert self.expert_bridge is not None
        try:
            async for turn in self.expert_bridge.send_with_tools(
                user_text, dialog_id=self.dialog_id,
            ):
                self.dialog_id = turn.dialog_id or self.dialog_id
                if turn.error:
                    print(f"\n\033[31m[Ошибка]: {turn.error}{RESET}\n")
                    return
                if turn.thinking_logs and not turn.text:
                    self._render_thinking_logs(turn.thinking_logs)
                    continue
                if turn.text:
                    print(turn.text, end="", flush=True)
                if turn.is_finished:
                    print()
                    if self.show_thinking and turn.thinking_steps:
                        self._render_thinking(turn.thinking_steps)
                    if turn.sources:
                        self._render_sources(turn.sources)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{TEXT_MUTED}[Прервано]{RESET}\n")
        except Exception as e:
            print(f"\n\033[31m[Ошибка Expert]: {e}{RESET}")

    async def _execute_turn_bridge(self, user_text: str, is_suggest: bool = False):
        assert self.bridge is not None
        initial = self._pending_attach
        self._pending_attach = None
        try:
            async for turn in self.bridge.send_with_tools(
                user_text, dialog_id=self.dialog_id, agent_id=self.current_agent,
                initial_attachments=initial,
            ):
                self.dialog_id = turn.dialog_id
                if turn.error:
                    print(f"\n\033[31m[Ошибка]: {turn.error}{RESET}\n")
                    return
                if turn.thinking_logs and not turn.text:
                    self._render_thinking_logs(turn.thinking_logs)
                    continue
                if turn.text:
                    print(turn.text, end="", flush=True)
                if turn.is_finished:
                    print()
                    if self.show_thinking and turn.thinking_steps:
                        self._render_thinking(turn.thinking_steps)
                    if turn.sources:
                        self._render_sources(turn.sources)
                    if turn.widgets:
                        seen = set()
                        for w in turn.widgets:
                            if w.url and w.url in seen:
                                continue
                            if w.url:
                                seen.add(w.url)
                            self.render_widget(w)
                    if turn.suggestions:
                        self.render_suggestions(turn.suggestions)
                    else:
                        self.active_suggestions = []
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{TEXT_MUTED}[Прервано]{RESET}\n")
        except Exception as e:
            print(f"\n\033[31m[Ошибка]: {e}{RESET}")

    async def execute_turn(self, user_text: str, is_suggest: bool = False):
        if self.current_agent == "expert" and self.expert_bridge and not is_suggest:
            return await self._execute_turn_expert(user_text)
        if not self.bridge or is_suggest:
            return await super().execute_turn(user_text, is_suggest=is_suggest)
        return await self._execute_turn_bridge(user_text, is_suggest=is_suggest)

    async def run_loop(self):
        if self.is_ephemeral:
            self.dialog_id = await self.manager.create_new_dialog(
                agent_id=self.current_agent, is_ephemeral=True,
            )
            if self.expert_bridge:
                self.expert_bridge.forget_hint(self.dialog_id)

        self.print_banner()

        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, input, f"{BOLD}{ALICE_RED}>>> {RESET}"
                )
                text = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{TEXT_MUTED}Выход.{RESET}")
                break
            if not text:
                continue

            clean_cmd = text.lstrip("/").lower()
            parts = clean_cmd.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("exit", "bye", "quit", "q"):
                break
            elif cmd in ("help", "?"):
                self.print_help()
                continue
            elif cmd in ("list", "agents"):
                self.print_agents_table()
                continue
            elif cmd in ("agent", "model"):
                if not arg:
                    a = registry.get(self.current_agent)
                    print(f"{TEXT_MUTED}Текущий: {BOLD}{a.name}{RESET} (id={a.id})")
                else:
                    a = registry.get(arg)
                    if a.id != arg.lower() and a.id != arg:
                        print(f"{ACCENT_YELLOW}⚠ '{arg}' не найден, fallback на '{a.id}'{RESET}")
                    self.current_agent = a.id
                    print(f"{ACCENT_GREEN}✓{RESET} → {BOLD}{a.name}{RESET} (id={a.id})")
                continue
            elif cmd == "expert":
                a = registry.get("expert")
                self.current_agent = a.id
                print(f"{ACCENT_GREEN}✓{RESET} → {BOLD}{a.name}{RESET} (preset={a.preset})")
                continue
            elif cmd == "ephemeral":
                if arg in ("on", "1"):
                    self.is_ephemeral = True
                    self.dialog_id = await self.manager.create_new_dialog(
                        agent_id=self.current_agent, is_ephemeral=True,
                    )
                    if self.expert_bridge:
                        self.expert_bridge.forget_hint(self.dialog_id)
                    print(f"{ACCENT_GREEN}✓{RESET} ephemeral ON")
                elif arg in ("off", "0"):
                    self.is_ephemeral = False
                    print(f"{TEXT_MUTED}ephemeral OFF{RESET}")
                else:
                    print(f"{TEXT_MUTED}ephemeral: {self.is_ephemeral}{RESET}")
                continue
            elif cmd == "new":
                self.dialog_id = await self.manager.create_new_dialog(
                    agent_id=self.current_agent, is_ephemeral=self.is_ephemeral,
                )
                if self.expert_bridge:
                    self.expert_bridge.forget_hint(self.dialog_id)
                self.active_suggestions = []
                print(f"{ACCENT_GREEN}✓{RESET} Новый диалог: {self.dialog_id[:12]}...")
                continue
            elif cmd == "clear":
                os.system("cls" if os.name == "nt" else "clear")
                self.print_banner()
                continue
            elif cmd == "dialogs":
                print(f"\n{BOLD}Диалоги:{RESET}")
                for did in self.manager.known_dialogs:
                    active = f" {ALICE_RED}●{RESET}" if did == self.dialog_id else ""
                    eph = f" {ACCENT_YELLOW}[temp]{RESET}" if did in self.manager.ephemeral_dialogs else ""
                    print(f"  {did}{eph}{active}")
                print()
                continue
            elif cmd == "tools":
                if not self.tools or not self.tools.tools:
                    print(f"{TEXT_MUTED}Инструменты не загружены{RESET}")
                    continue
                print(f"\n{BOLD}Инструменты:{RESET}")
                for name, tool in self.tools.tools.items():
                    print(f"  {ACCENT_YELLOW}{name}{RESET}: {tool.description}")
                print()
                continue
            elif cmd == "exec":
                if not self.tools or "shell" not in self.tools.tools:
                    print(f"{TEXT_MUTED}shell не сконфигурирован{RESET}")
                    continue
                if not arg:
                    print(f"{TEXT_MUTED}/exec <cmd>{RESET}")
                    continue
                print(f"{DIM}$ {arg}{RESET}")
                res = await self.tools.execute("shell", {"cmd": arg})
                if res.success:
                    print(res.output)
                else:
                    print(f"\033[31m{res.error or res.output}{RESET}")
                continue
            elif cmd == "attach":
                if not arg:
                    print(f"{TEXT_MUTED}/attach <path>{RESET}")
                    continue
                print(f"{DIM}Загружаю {arg}...{RESET}")
                try:
                    result = await self.manager.ws.upload_file(arg)
                except Exception as e:
                    print(f"\033[31m[Ошибка]: {e}{RESET}")
                    continue
                if not result:
                    print(f"\033[31m[Не удалось загрузить]{RESET}")
                    continue
                if self._pending_attach is None:
                    self._pending_attach = []
                self._pending_attach.append({
                    "file_guid": result["file_guid"],
                    "title": result.get("title"),
                    "mime_type": result.get("mime_type"),
                })
                size_kb = (result.get("size_bytes") or 0) / 1024
                print(f"{ACCENT_GREEN}✓{RESET} {BOLD}{result.get('title')}{RESET} "
                      f"({size_kb:.1f} KB) — прикрепится к следующему сообщению")
                continue
            elif cmd == "yolo":
                if arg in ("on", "1"):
                    self.yolo = True
                    print(f"{ACCENT_YELLOW}⚠ YOLO ON — авто-одобрение всех вызовов{RESET}")
                elif arg in ("off", "0"):
                    self.yolo = False
                    print(f"{TEXT_MUTED}YOLO OFF{RESET}")
                else:
                    print(f"{TEXT_MUTED}yolo: {self.yolo}{RESET}")
                continue
            elif cmd == "meeseeks":
                if not self.meeseeks_pool:
                    print(f"{TEXT_MUTED}Мисиксы отключены{RESET}")
                    continue
                if not arg:
                    print(f"{TEXT_MUTED}/meeseeks [@agent] <task>{RESET}")
                    continue
                m_agent = self.current_agent
                task_text = arg
                if arg.startswith("@"):
                    sp = arg[1:].split(maxsplit=1)
                    if len(sp) == 2:
                        m_agent, task_text = sp[0], sp[1]
                print(f"{DIM}Мисикс (режим {m_agent})...{RESET}")
                r = await self.meeseeks_pool.spawn(task=task_text, agent_id=m_agent)
                st = ACCENT_GREEN if r.success else "\033[31m"
                print(f"{st}{'✓' if r.success else '✗'}{RESET} "
                      f"[{r.handle}, {r.elapsed:.1f}s, {r.agent_id}]")
                if self.show_thinking and r.thinking_steps:
                    self._render_thinking(r.thinking_steps)
                print(r.text or r.error or "(пусто)")
                continue
            elif cmd in ("meeseeks-batch", "meeseeks_batch"):
                if not self.meeseeks_pool:
                    print(f"{TEXT_MUTED}Мисиксы отключены{RESET}")
                    continue
                if not arg:
                    print(f"{TEXT_MUTED}/meeseeks-batch <t1> | <t2>{RESET}")
                    continue
                tasks = [t.strip() for t in arg.split("|") if t.strip()]
                if not tasks:
                    print(f"{TEXT_MUTED}Нет задач{RESET}")
                    continue
                print(f"{DIM}{len(tasks)} мисиксов...{RESET}")
                known_ids = {a.id for a in registry.list_all()}
                coros = []
                for t in tasks:
                    if ":" in t and t.split(":", 1)[0].strip() in known_ids:
                        ag, tk = t.split(":", 1)
                        coros.append(self.meeseeks_pool.spawn(task=tk.strip(), agent_id=ag.strip()))
                    else:
                        coros.append(self.meeseeks_pool.spawn(task=t, agent_id=self.current_agent))
                results = await asyncio.gather(*coros)
                print()
                for i, r in enumerate(results, 1):
                    st = f"{ACCENT_GREEN}✅{RESET}" if r.success else "\033[31m❌{RESET}"
                    print(f"{st} [{i}] ({r.elapsed:.1f}s, {r.agent_id}) {r.task[:60]}")
                    body = r.text or r.error or "(пусто)"
                    for ln in body.splitlines():
                        print(f"    {ln}")
                    print()
                continue

            is_suggest = False
            if text.isdigit() and self.active_suggestions:
                idx = int(text) - 1
                if 0 <= idx < len(self.active_suggestions):
                    sel = self.active_suggestions[idx]
                    text = sel.payload or sel.title
                    is_suggest = True
                    print(f"{DIM}Выбран вариант [{idx+1}]: {sel.title}{RESET}")

            await self.execute_turn(text, is_suggest=is_suggest)


async def async_main():
    parser = argparse.ArgumentParser(description="Alice CLI + Tools + Meeseeks + Expert")
    parser.add_argument("--auth", help="Токен (y0_...) или путь к файлу токена/кук")
    parser.add_argument("--token", help="OAuth токен")
    parser.add_argument("--cookies", help="Путь к файлу кук")
    parser.add_argument("--agent", default="pro", help="Начальный агент")
    parser.add_argument("-e", "--ephemeral", action="store_true", help="Временная сессия")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный лог")
    parser.add_argument("--thinking", action="store_true",
                        help="Показывать структурированный процесс размышлений")
    parser.add_argument("--tools-config", default=None,
                        help=f"JSON-конфиг инструментов (default: ./{DEFAULT_CONFIG})")
    parser.add_argument("--yolo", action="store_true",
                        help="Авто-одобрение всех вызовов")
    parser.add_argument("--no-tools", action="store_true",
                        help="Отключить инструменты")
    parser.add_argument("--no-meeseeks", action="store_true",
                        help="Отключить мисиксы")
    # NEW: продолжить существующий диалог (ID с сайта — /chat/<uuid>)
    parser.add_argument("--dialog-id", default=None,
                        help="Продолжить существующий диалог (ID с сайта)")
    args = parser.parse_args()

    creds = load_or_request_credentials(
        explicit_source=args.auth or args.token or args.cookies,
        allow_interactive=True,
    )
    if not creds:
        print("\033[31m[Ошибка]: Авторизация не настроена.\033[0m")
        sys.exit(1)

    print(f"{TEXT_MUTED}Healthcheck...{RESET}")
    ok, msg = await verify_credentials(creds)
    if not ok:
        print(f"\033[31m[Healthcheck]: {msg}{RESET}")
        sys.exit(1)
    auth_str = f"OAuth ({creds.token[:10]}...)" if creds.auth_type == "token" \
        else f"Cookies ({len(creds.cookie_dict)})"
    print(f"{ACCENT_GREEN}✓{RESET} {msg} | {BOLD}{auth_str}{RESET}")

    manager = AliceAgentManager(creds, verbose=args.verbose, show_thinking=args.thinking)
    try:
        await manager.initialize(initial_dialog_id=args.dialog_id)
    except Exception as e:
        print(f"\n\033[31m[Ошибка подключения]: {e}{RESET}")
        sys.exit(1)

    config_path = args.tools_config or DEFAULT_CONFIG
    raw_cfg: Dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
        except Exception as e:
            print(f"\033[31m[Ошибка чтения {config_path}]: {e}{RESET}")

    tools: Optional[LocalToolRegistry] = None
    if not args.no_tools:
        if os.path.exists(config_path):
            print(f"{TEXT_MUTED}Загрузка инструментов: {config_path}...{RESET}")
            try:
                tools = await load_tools_from_config_async(config_path)
                if tools.tools:
                    print(f"{ACCENT_GREEN}✓{RESET} Инструментов: {BOLD}{len(tools.tools)}{RESET}")
                else:
                    print(f"{TEXT_MUTED}[warn] Конфиг пуст{RESET}")
                    tools = None
            except Exception as e:
                print(f"\033[31m[Ошибка загрузки инструментов]: {e}{RESET}")
                tools = None
        elif args.tools_config:
            print(f"\033[31m[Ошибка]: {config_path} не найден{RESET}")
            sys.exit(1)
        else:
            print(f"{TEXT_MUTED}[info] {config_path} не найден — режим без инструментов{RESET}")

    session = AliceToolsSession(
        manager=manager,
        default_agent=args.agent,
        ephemeral=args.ephemeral,
        tools=tools,
        bridge=None,
        meeseeks_pool=None,
        yolo=args.yolo,
        show_thinking=args.thinking,
    )
    if args.dialog_id:
        session.dialog_id = args.dialog_id
        session.active_suggestions = []

    if tools and not args.no_meeseeks:
        mk_cfg = raw_cfg.get("meeseeks") or {}
        if mk_cfg.get("enabled", True):
            def bridge_factory(sub_mgr, reg):
                return LocalToolBridge(
                    sub_mgr, reg, agent_id="pro",
                    max_iterations=int(mk_cfg.get("bridge_max_iterations", 8)),
                    approve_callback=(None if session.yolo else session._approve_tool),
                    show_thinking=args.thinking,
                )
            pool = MeeseeksPool(
                manager=manager, tools=tools, bridge_factory=bridge_factory,
                max_depth=int(mk_cfg.get("max_depth", 3)),
                max_concurrent=int(mk_cfg.get("max_concurrent", 5)),
                on_event=session._on_meeseeks_event,
            )
            tools.register(MeeseeksTool(pool))
            tools.register(MeeseeksBatchTool(pool))
            session.meeseeks_pool = pool
            print(f"{ACCENT_GREEN}✓{RESET} Мисиксы: {BOLD}{len(tools.tools)}{RESET} инструментов, "
                  f"глубина {pool.max_depth}, параллельно {pool.max_concurrent}")

    if tools:
        session.bridge = LocalToolBridge(
            manager, tools, agent_id=args.agent,
            approve_callback=(None if args.yolo else session._approve_tool),
            show_thinking=args.thinking,
        )

    try:
        await session.run_loop()
    finally:
        if tools:
            await tools.close()
        await manager.shutdown()


def main():
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()