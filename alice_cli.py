#!/usr/bin/env python3
"""Alice AI Terminal Client (unified).

Объединяет alice_cli.py + alice_tools_cli.py:
  - чат, агенты, диалоги, временные сессии, /switch
  - структурированные размышления (--thinking), sources, widgets, suggestions
  - вложения: /attach + upload_file
  - локальные инструменты: alice_tools.json, подтверждение (по умолчанию НЕ yolo)
  - ExpertBridge, одиночные/пакетные Meeseeks
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

try:
    import readline
except ImportError:
    try:
        import pyreadline3 as readline
    except ImportError:
        readline = None

from agents import SuggestionAction, registry
from agents.base import ThinkingStep
from alice_server import AliceAgentManager, AliceTurnResponse
from alice_auth import load_credentials_with_cookies, verify_credentials

logger = logging.getLogger("alice_cli")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ALICE_RED = "\033[38;2;252;63;29m"
ACCENT_CYAN = "\033[38;2;0;210;255m"
ACCENT_GREEN = "\033[38;2;0;220;130m"
ACCENT_YELLOW = "\033[38;2;255;190;0m"
TEXT_MUTED = "\033[38;2;130;135;145m"

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
DEFAULT_CONFIG = "alice_tools.json"


class AliceCompleter:
    def __init__(self, commands: List[str], agents: List[str]):
        self.commands = commands
        self.agents = agents

    def complete(self, text: str, state: int):
        options = []
        if text.startswith("agent ") or text.startswith("/agent "):
            prefix = text.split(" ", 1)[1]
            cmd = text.split(" ")[0]
            options = [f"{cmd} {a}" for a in self.agents if a.startswith(prefix)]
        else:
            options = [c for c in self.commands if c.startswith(text)]
        if state < len(options):
            return options[state]
        return None


class AliceCliSession:
    def __init__(
        self,
        manager: AliceAgentManager,
        default_agent: str = "pro",
        ephemeral: bool = False,
        tools=None,
        bridge=None,
        expert_bridge=None,
        meeseeks_pool=None,
        yolo: bool = False,
        show_thinking: bool = False,
    ):
        self.manager = manager
        self.current_agent = default_agent
        self.is_ephemeral = ephemeral
        self.dialog_id = manager.active_dialog_id
        self.active_suggestions: List[SuggestionAction] = []
        self.tools = tools
        self.bridge = bridge
        self.expert_bridge = expert_bridge
        self.meeseeks_pool = meeseeks_pool
        self.yolo = yolo
        self.show_thinking = show_thinking
        self._pending_attach: Optional[List[Dict[str, Any]]] = None
        self._setup_autocomplete()

    # ---------- autocomplete ----------
    def _setup_autocomplete(self):
        if not readline:
            return
        commands = [
            "/help", "help", "/?",
            "/agent", "agent", "/agents", "agents", "/list", "list",
            "/new", "new", "/dialogs", "dialogs", "/switch", "switch",
            "/dialog", "dialog", "/ephemeral", "ephemeral",
            "/tools", "tools", "/exec", "exec", "/attach", "attach",
            "/yolo", "yolo", "/expert", "expert",
            "/meeseeks", "meeseeks", "/meeseeks-batch", "meeseeks-batch",
            "/clear", "clear", "/exit", "exit", "/bye", "bye",
        ]
        agents = list(registry.all_aliases().keys())
        readline.set_completer(AliceCompleter(commands, agents).complete)
        readline.parse_and_bind("tab: complete")

    # ---------- badges / banner ----------
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
{ALICE_RED}{BOLD}    ▼   {RESET} {TEXT_MUTED}help / tools / switch{RESET}
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
  {ACCENT_CYAN}list{RESET} (agents)              Список агентов
  {ACCENT_CYAN}agent <имя>{RESET}              Переключить (pro / base / expert / deep_research)
  {ACCENT_CYAN}expert{RESET}                   Переключить в Эксперт
  {ACCENT_CYAN}new{RESET}                      Новый диалог
  {ACCENT_CYAN}ephemeral on|off{RESET}         Авто-удаление
  {ACCENT_CYAN}dialogs{RESET}                  Активные диалоги (с индексами)
  {ACCENT_CYAN}switch <uuid|N>{RESET}          Переключиться на другой диалог
  {ACCENT_CYAN}switch{RESET}                   Показать текущий диалог
  {ACCENT_CYAN}tools{RESET}                    Список инструментов
  {ACCENT_CYAN}exec <cmd>{RESET}               Shell-команда напрямую
  {ACCENT_CYAN}attach <path>{RESET}            Прикрепить файл
  {ACCENT_CYAN}meeseeks [@agent] <task>{RESET}       Один мисикс
  {ACCENT_CYAN}meeseeks-batch <t1> | <t2>{RESET}     Пакет (agent:task)
  {ACCENT_CYAN}yolo on|off{RESET}              Авто-одобрение
  {ACCENT_CYAN}clear{RESET} / {ACCENT_CYAN}help{RESET} / {ACCENT_CYAN}exit{RESET}
{tools_block}""")

    def print_agents_table(self):
        print(f"\n{BOLD}{'ID / АЛИАС':<16} {'ТИП':<10} {'ИМЯ / СЕРВИС':<30} {'ОПИСАНИЕ':<35}{RESET}")
        print(f"{TEXT_MUTED}{'─'*85}{RESET}")
        for agent in registry.list_all():
            atype = agent.type.upper()
            color = ACCENT_GREEN if atype == "APP" else (ACCENT_CYAN if atype == "AGENT" else BOLD)
            marker = f" {ALICE_RED}●{RESET}" if agent.id == self.current_agent else "  "
            print(f"{marker}{color}{agent.id:<14}{RESET} {DIM}[{atype:<5}]{RESET} {agent.name:<30} {agent.description}")
        print()

    # ---------- sync commands ----------
    def _print_dialogs(self):
        dialogs = sorted(self.manager.known_dialogs)
        print(f"\n{BOLD}Диалоги:{RESET}")
        for idx, did in enumerate(dialogs, 1):
            active = f" {ALICE_RED}● активен{RESET}" if did == self.dialog_id else ""
            eph = f" {ACCENT_YELLOW}[temp]{RESET}" if did in self.manager.ephemeral_dialogs else ""
            print(f"  [{idx}] {did}{eph}{active}")
        print()

    def _cmd_switch(self, arg: str):
        if not arg:
            print(f"{TEXT_MUTED}Текущий диалог: {BOLD}{self.dialog_id}{RESET}")
            return
        if arg.isdigit():
            dialogs = sorted(self.manager.known_dialogs)
            idx = int(arg) - 1
            if 0 <= idx < len(dialogs):
                target = dialogs[idx]
            else:
                print(f"\033[31m[Ошибка]: индекс {arg} вне диапазона{RESET}")
                return
        else:
            target = arg
            if target not in self.manager.known_dialogs:
                print(f"\033[31m[Ошибка]: диалог {target} не найден{RESET}")
                return
        self.dialog_id = target
        self.manager.active_dialog_id = target
        self.active_suggestions = []
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.manager.ws.update_subscriptions(target, self.manager.known_dialogs))
        except RuntimeError:
            pass
        print(f"{ACCENT_GREEN}✓{RESET} Переключено на {BOLD}{target}{RESET}")

    def _handle_command(self, text: str) -> str:
        clean = text.lstrip("/").lower()
        parts = clean.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("exit", "bye", "quit", "q"):
            print(f"{TEXT_MUTED}До свидания!{RESET}")
            return "exit"
        if cmd in ("help", "?"):
            self.print_help()
            return "handled"
        if cmd in ("list", "agents"):
            self.print_agents_table()
            return "handled"
        if cmd in ("agent", "model"):
            if not arg:
                a = registry.get(self.current_agent)
                print(f"{TEXT_MUTED}Текущий: {BOLD}{a.name}{RESET} (id={a.id})")
            else:
                a = registry.get(arg)
                if a.id != arg.lower() and a.id != arg:
                    print(f"{ACCENT_YELLOW}⚠ '{arg}' не найден, fallback на '{a.id}'{RESET}")
                self.current_agent = a.id
                self.active_suggestions = []
                print(f"{ACCENT_GREEN}✓{RESET} → {BOLD}{a.name}{RESET} (id={a.id})")
            return "handled"
        if cmd == "expert":
            a = registry.get("expert")
            self.current_agent = a.id
            self.active_suggestions = []
            print(f"{ACCENT_GREEN}✓{RESET} → {BOLD}{a.name}{RESET}")
            return "handled"
        if cmd == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            self.print_banner()
            return "handled"
        if cmd == "dialogs":
            self._print_dialogs()
            return "handled"
        if cmd in ("switch", "dialog"):
            self._cmd_switch(arg)
            return "handled"
        if cmd == "tools":
            if not self.tools:
                print(f"{TEXT_MUTED}Инструменты не загружены{RESET}")
            else:
                print(f"\n{BOLD}Инструменты:{RESET}")
                for name, tool in self.tools.tools.items():
                    print(f"  {ACCENT_YELLOW}{name}{RESET}: {tool.description}")
                print()
            return "handled"
        return "chat"

    # ---------- async commands ----------
    async def _cmd_new(self):
        self.dialog_id = await self.manager.create_new_dialog(
            agent_id=self.current_agent, is_ephemeral=self.is_ephemeral,
        )
        if self.expert_bridge:
            self.expert_bridge.forget_hint(self.dialog_id)
        self.active_suggestions = []
        print(f"{ACCENT_GREEN}✓{RESET} Новый диалог: {self.dialog_id[:12]}...")

    async def _cmd_ephemeral(self, arg: str):
        if arg in ("on", "1", "true"):
            self.is_ephemeral = True
            self.dialog_id = await self.manager.create_new_dialog(
                agent_id=self.current_agent, is_ephemeral=True,
            )
            if self.expert_bridge:
                self.expert_bridge.forget_hint(self.dialog_id)
            print(f"{ACCENT_GREEN}✓{RESET} ephemeral ON, диалог {self.dialog_id[:10]}...")
        elif arg in ("off", "0", "false"):
            self.is_ephemeral = False
            print(f"{TEXT_MUTED}ephemeral OFF{RESET}")
        else:
            print(f"{TEXT_MUTED}ephemeral: {self.is_ephemeral}{RESET}")

    async def _cmd_exec(self, arg: str):
        if not self.tools or "shell" not in self.tools.tools:
            print(f"{TEXT_MUTED}shell не сконфигурирован{RESET}")
            return
        if not arg:
            print(f"{TEXT_MUTED}/exec <cmd>{RESET}")
            return
        print(f"{DIM}$ {arg}{RESET}")
        res = await self.tools.execute("shell", {"cmd": arg})
        if res.success:
            print(res.output)
        else:
            print(f"\033[31m{res.error or res.output}{RESET}")

    async def _cmd_attach(self, arg: str):
        if not arg:
            print(f"{TEXT_MUTED}/attach <path>{RESET}")
            return
        print(f"{DIM}Загружаю {arg}...{RESET}")
        try:
            result = await self.manager.ws.upload_file(arg)
        except Exception as e:
            print(f"\033[31m[Ошибка]: {e}{RESET}")
            return
        if not result:
            print(f"\033[31m[Не удалось загрузить]{RESET}")
            return
        if self._pending_attach is None:
            self._pending_attach = []
        self._pending_attach.append({
            "file_guid": result["file_guid"],
            "title": result.get("title"),
            "mime_type": result.get("mime_type"),
        })
        size_kb = (result.get("size_bytes") or 0) / 1024
        print(f"{ACCENT_GREEN}✓{RESET} {BOLD}{result.get('title')}{RESET} ({size_kb:.1f} KB)")

    def _cmd_yolo(self, arg: str):
        if arg in ("on", "1", "true"):
            self.yolo = True
            print(f"{ACCENT_YELLOW}⚠ YOLO ON — авто-одобрение{RESET}")
        elif arg in ("off", "0", "false"):
            self.yolo = False
            print(f"{TEXT_MUTED}YOLO OFF{RESET}")
        else:
            print(f"{TEXT_MUTED}yolo: {self.yolo}{RESET}")

    async def _cmd_meeseeks(self, arg: str):
        if not self.meeseeks_pool:
            print(f"{TEXT_MUTED}Мисиксы отключены{RESET}")
            return
        if not arg:
            print(f"{TEXT_MUTED}/meeseeks [@agent] <task>{RESET}")
            return
        m_agent = self.current_agent
        task_text = arg
        if arg.startswith("@"):
            sp = arg[1:].split(maxsplit=1)
            if len(sp) == 2:
                m_agent, task_text = sp[0], sp[1]
        print(f"{DIM}Мисикс (режим {m_agent})...{RESET}")
        r = await self.meeseeks_pool.spawn(task=task_text, agent_id=m_agent)
        st = ACCENT_GREEN if r.success else "\033[31m"
        print(f"{st}{'✓' if r.success else '✗'}{RESET} [{r.handle}, {r.elapsed:.1f}s, {r.agent_id}]")
        if self.show_thinking and r.thinking_steps:
            self._render_thinking(r.thinking_steps)
        print(r.text or r.error or "(пусто)")

    async def _cmd_meeseeks_batch(self, arg: str):
        if not self.meeseeks_pool:
            print(f"{TEXT_MUTED}Мисиксы отключены{RESET}")
            return
        if not arg:
            print(f"{TEXT_MUTED}/meeseeks-batch <t1> | <t2>{RESET}")
            return
        tasks = [t.strip() for t in arg.split("|") if t.strip()]
        if not tasks:
            return
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
            for ln in (r.text or r.error or "(пусто)").splitlines():
                print(f"    {ln}")
            print()

    # ---------- rendering ----------
    def render_widget(self, widget):
        title = widget.title or "Виджет"
        print(f"\n{ACCENT_YELLOW}┌── 📦 {title} {'─' * max(5, 55 - len(title))}┐{RESET}")
        if widget.url:
            print(f"{ACCENT_YELLOW}│{RESET}  {TEXT_MUTED}Ссылка / Действие:{RESET} {BOLD}{widget.url}{RESET}")
        if widget.component_name == "deep_research_sources":
            for s in widget.raw_data.get("sources", []):
                dt = s.get("DocumentTitle", "Источник")
                fu = s.get("FullUrl", "")
                print(f"{ACCENT_YELLOW}│{RESET}  • {dt[:45]}: {DIM}{fu[:45]}...{RESET}")
        print(f"{ACCENT_YELLOW}└──{'─' * 58}┘{RESET}")

    def render_suggestions(self, suggestions: List[SuggestionAction]):
        self.active_suggestions = suggestions
        if not suggestions:
            return
        print(f"\n{TEXT_MUTED}Варианты ответов (введите номер):{RESET}")
        for i, sugg in enumerate(suggestions, start=1):
            print(f"  {ACCENT_YELLOW}[{i}]{RESET} {BOLD}{sugg.title}{RESET}")
        print()

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

    def _render_sources(self, sources):
        if not sources:
            return
        print(f"{TEXT_MUTED}Источники:{RESET}")
        for s in sources[:10]:
            title = s.get("DocumentTitle") or s.get("ForcedShortUrl") or "source"
            url = s.get("FullUrl") or ""
            print(f"  {TEXT_MUTED}• {title[:70]}{RESET}" + (f" — {url[:60]}..." if url else ""))
        print()

    def _render_thinking_logs(self, logs):
        for log in logs:
            if log.startswith("▸"):
                print(f"{ACCENT_CYAN}{log}{RESET}")
            elif log.startswith("🔧"):
                print(f"{ACCENT_YELLOW}{log}{RESET}")
            elif log.startswith("→"):
                head, _, tail = log.partition("\n")
                print(f"{ACCENT_GREEN}{head}{RESET}")
                if tail:
                    print(f"{TEXT_MUTED}{tail}{RESET}")
            elif log.startswith(("✗", "⚠", "⛔")):
                print(f"\033[31m{log}{RESET}")
            elif log.startswith(("📎", "📥")):
                print(f"{ACCENT_GREEN}{log}{RESET}")
            elif log.startswith("💻"):
                print(f"{ACCENT_YELLOW}{log}{RESET}")
            else:
                print(f"{TEXT_MUTED}{log}{RESET}")

    # ---------- approvals ----------
    async def _approve_tool(self, name: str, args: Dict[str, Any]) -> bool:
        if self.yolo:
            return True
        try:
            ans = await asyncio.to_thread(
                input,
                f"{ACCENT_YELLOW}Разрешить {BOLD}{name}{RESET}{ACCENT_YELLOW}? [y/N] >>> {RESET}",
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() in ("y", "yes", "д", "да", "1")

    async def _on_meeseeks_event(self, kind, handle, agent_id, info):
        prefix = {
            "start": f"{ACCENT_CYAN}🧞 ▶{RESET}",
            "done": f"{ACCENT_GREEN}🧞 ✓{RESET}",
            "fail": "\033[31m🧞 ✗\033[0m",
            "deny": "\033[31m🧞 ⛔\033[0m",
            "warn": f"{ACCENT_YELLOW}🧞 ⚠{RESET}",
        }.get(kind, f"🧞 {kind}")
        print(f"{prefix} [{handle}] {DIM}{agent_id}{RESET}: {info}")

    # ---------- turn execution ----------
    async def execute_turn(self, user_text: str, is_suggest: bool = False):
        if self.current_agent == "expert" and self.expert_bridge and not is_suggest:
            return await self._execute_turn_expert(user_text)
        if self.bridge and not is_suggest:
            return await self._execute_turn_bridge(user_text, is_suggest=is_suggest)
        return await self._execute_turn_plain(user_text, is_suggest=is_suggest)

    async def _execute_turn_plain(self, user_text: str, is_suggest: bool = False):
        printed_len = 0
        spinner_idx = 0
        is_thinking = False
        try:
            async for turn in self.manager.send_message(
                text=user_text, agent_id=self.current_agent,
                dialog_id=self.dialog_id, is_suggest=is_suggest,
            ):
                self.dialog_id = turn.dialog_id
                if turn.error:
                    if is_thinking:
                        sys.stdout.write("\r\033[2K")
                    print(f"\n\033[31m[Ошибка]: {turn.error}{RESET}\n")
                    return
                if turn.thinking_logs and not turn.text:
                    is_thinking = True
                    curr = turn.thinking_logs[-1]
                    frame = SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]
                    spinner_idx += 1
                    sys.stdout.write(f"\r\033[2K{TEXT_MUTED}{frame} [Алиса]:{RESET} {DIM}{curr}{RESET}")
                    sys.stdout.flush()
                if turn.text:
                    if is_thinking:
                        sys.stdout.write("\r\033[2K")
                        sys.stdout.flush()
                        is_thinking = False
                    new_chunk = turn.text[printed_len:]
                    if new_chunk:
                        sys.stdout.write(new_chunk)
                        sys.stdout.flush()
                        printed_len = len(turn.text)
                if turn.is_finished:
                    if is_thinking:
                        sys.stdout.write("\r\033[2K")
                        sys.stdout.flush()
                    print()
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
            sys.stdout.write("\r\033[2K")
            print(f"\n{TEXT_MUTED}[Запрос прерван по Ctrl+C]{RESET}\n")
        except Exception as e:
            print(f"\n\033[31m[Ошибка]: {e}{RESET}")

    async def _execute_turn_expert(self, user_text: str):
        assert self.expert_bridge is not None
        try:
            async for turn in self.expert_bridge.send_with_tools(user_text, dialog_id=self.dialog_id):
                self.dialog_id = turn.dialog_id or self.dialog_id
                if turn.error:
                    print(f"\n\033[31m[Ошибка]: {turn.error}{RESET}\n")
                    return
                if self.show_thinking and turn.thinking_steps:
                    self._render_thinking(turn.thinking_steps)
                if turn.thinking_logs:
                    self._render_thinking_logs(turn.thinking_logs)
                if turn.text:
                    print(turn.text, end="", flush=True)
                if turn.is_finished:
                    print()
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
                if self.show_thinking and turn.thinking_steps:
                    self._render_thinking(turn.thinking_steps)
                if turn.thinking_logs:
                    self._render_thinking_logs(turn.thinking_logs)
                if turn.text:
                    print(turn.text, end="", flush=True)
                if turn.is_finished:
                    print()
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

    # ---------- main loop ----------
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
                user_input = await asyncio.to_thread(
                    input, f"{BOLD}{ALICE_RED}>>> {RESET}"
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

            if cmd == "new":
                await self._cmd_new()
                continue
            if cmd == "ephemeral":
                await self._cmd_ephemeral(arg)
                continue
            if cmd == "exec":
                await self._cmd_exec(arg)
                continue
            if cmd == "attach":
                await self._cmd_attach(arg)
                continue
            if cmd == "yolo":
                self._cmd_yolo(arg)
                continue
            if cmd == "meeseeks":
                await self._cmd_meeseeks(arg)
                continue
            if cmd in ("meeseeks-batch", "meeseeks_batch"):
                await self._cmd_meeseeks_batch(arg)
                continue

            result = self._handle_command(text)
            if result == "exit":
                break
            if result == "handled":
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


# ---------- bootstrap helpers ----------
async def _load_tools_or_none(config_path: str):
    if not os.path.exists(config_path):
        return None, {}
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"\033[31m[Ошибка чтения {config_path}]: {e}{RESET}")
        return None, {}
    print(f"{TEXT_MUTED}Загрузка инструментов: {config_path}...{RESET}")
    try:
        from local_tools import load_tools_from_config_async
        tools = await load_tools_from_config_async(config_path)
        if tools.tools:
            print(f"{ACCENT_GREEN}✓{RESET} Инструментов: {BOLD}{len(tools.tools)}{RESET}")
        else:
            print(f"{TEXT_MUTED}[warn] Конфиг пуст{RESET}")
            tools = None
        return tools, raw
    except Exception as e:
        print(f"\033[31m[Ошибка загрузки инструментов]: {e}{RESET}")
        return None, raw


def _build_bridge(manager, tools, agent_id, session, show_thinking):
    if not tools:
        return None
    from tool_bridge import LocalToolBridge
    return LocalToolBridge(
        manager, tools, agent_id=agent_id,
        approve_callback=(None if session.yolo else session._approve_tool),
        show_thinking=show_thinking,
    )


def _build_meeseeks(manager, tools, raw_cfg, session, show_thinking):
    if not tools:
        return None
    mk = (raw_cfg or {}).get("meeseeks") or {}
    if not mk.get("enabled", True):
        return None
    try:
        from tool_bridge import LocalToolBridge
        from meeseeks import MeeseeksPool, MeeseeksTool, MeeseeksBatchTool

        def factory(sub_mgr, reg):
            return LocalToolBridge(
                sub_mgr, reg, agent_id="pro",
                max_iterations=int(mk.get("bridge_max_iterations", 8)),
                approve_callback=(None if session.yolo else session._approve_tool),
                show_thinking=show_thinking,
            )

        pool = MeeseeksPool(
            manager=manager, tools=tools, bridge_factory=factory,
            max_depth=int(mk.get("max_depth", 3)),
            max_concurrent=int(mk.get("max_concurrent", 5)),
            on_event=session._on_meeseeks_event,
        )
        tools.register(MeeseeksTool(pool))
        tools.register(MeeseeksBatchTool(pool))
        return pool
    except Exception as e:
        print(f"\033[31m[Ошибка Meeseeks]: {e}{RESET}")
        return None


def _build_expert_bridge(manager, tools, session, show_thinking):
    if not tools:
        return None
    try:
        from expert_bridge import ExpertBridge
        return ExpertBridge(
            manager=manager, tools=tools, agent_id="expert",
            download_dir="~/Downloads", auto_download=False,
            max_iterations=8,
            approve_callback=(None if session.yolo else session._approve_tool),
            show_thinking=show_thinking,
        )
    except Exception as e:
        print(f"\033[31m[Ошибка ExpertBridge]: {e}{RESET}")
        return None


# ---------- main ----------
async def async_main():
    parser = argparse.ArgumentParser(description="Yandex Alice AI CLI")
    parser.add_argument("--auth", help="Токен (y0_...) или путь к файлу")
    parser.add_argument("--token", help="OAuth токен")
    parser.add_argument("--cookies", help="Путь к файлу cookies")
    parser.add_argument("--agent", default="pro")
    parser.add_argument("-e", "--ephemeral", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--tools-config", default=None)
    parser.add_argument("--yolo", action="store_true")
    parser.add_argument("--no-tools", action="store_true")
    parser.add_argument("--no-meeseeks", action="store_true")
    parser.add_argument("--dialog-id", default=None)
    args = parser.parse_args()

    creds = load_credentials_with_cookies(
        token_source=args.auth or args.token,
        cookies_source=args.cookies,
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

    tools, raw_cfg = None, {}
    if not args.no_tools:
        config_path = args.tools_config or DEFAULT_CONFIG
        tools, raw_cfg = await _load_tools_or_none(config_path)

    session = AliceCliSession(
        manager=manager, default_agent=args.agent, ephemeral=args.ephemeral,
        yolo=args.yolo, show_thinking=args.thinking,
    )
    if args.dialog_id:
        session.dialog_id = args.dialog_id
        session.active_suggestions = []

    if tools:
        session.tools = tools
        session.bridge = _build_bridge(manager, tools, args.agent, session, args.thinking)
        if not args.no_meeseeks:
            session.meeseeks_pool = _build_meeseeks(manager, tools, raw_cfg, session, args.thinking)
        session.expert_bridge = _build_expert_bridge(manager, tools, session, args.thinking)

    try:
        await session.run_loop()
    finally:
        if tools:
            try:
                await tools.close()
            except Exception:
                pass
        await manager.shutdown()


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
