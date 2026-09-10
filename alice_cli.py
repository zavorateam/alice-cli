#!/usr/bin/env python3
"""
Alice AI Terminal Client (Ollama Style).
Поддерживает временные сессии (--ephemeral) с авто-удалением чата при выходе и чистый Ctrl+C.
"""

import argparse
import asyncio
import os
import sys
from typing import List, Optional

try:
    import readline
except ImportError:
    try:
        import pyreadline3 as readline
    except ImportError:
        readline = None

from agents import SuggestionAction, registry
from alice_server import AliceAgentManager, AliceTurnResponse, load_cookies

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ALICE_RED = "\033[38;2;252;63;29m"
ACCENT_CYAN = "\033[38;2;0;210;255m"
ACCENT_GREEN = "\033[38;2;0;220;130m"
ACCENT_YELLOW = "\033[38;2;255;190;0m"
TEXT_MUTED = "\033[38;2;130;135;145m"

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


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
    def __init__(self, manager: AliceAgentManager, default_agent: str = "pro", ephemeral: bool = False):
        self.manager = manager
        self.current_agent = default_agent
        self.is_ephemeral = ephemeral
        self.dialog_id = manager.active_dialog_id
        self.active_suggestions: List[SuggestionAction] = []
        self._setup_autocomplete()

    def _setup_autocomplete(self):
        if readline:
            commands = [
                "/help", "help", "/?",
                "/agent", "agent",
                "/agents", "agents",
                "/list", "list",
                "/new", "new",
                "/dialogs", "dialogs",
                "/ephemeral", "ephemeral",
                "/clear", "clear",
                "/exit", "exit", "/bye", "bye",
            ]
            agents = list(registry.all_aliases().keys())
            completer = AliceCompleter(commands, agents)
            readline.set_completer(completer.complete)
            readline.parse_and_bind("tab: complete")

    def print_banner(self):
        agent = registry.get(self.current_agent)
        eph_status = f"{ACCENT_YELLOW}[Временный чат: вкл]{RESET}" if self.is_ephemeral else f"{DIM}[Временный чат: выкл]{RESET}"
        print(
            f"""
{ALICE_RED}{BOLD}    ▲   {RESET} {BOLD}Yandex Alice AI CLI{RESET}
{ALICE_RED}{BOLD}    █   {RESET} {TEXT_MUTED}Режим:{RESET} {BOLD}{ACCENT_CYAN}[{agent.name}]{RESET} {DIM}({agent.type.upper()}){RESET} {eph_status}
{ALICE_RED}{BOLD}    ▼   {RESET} {TEXT_MUTED}Введите {BOLD}help{RESET} для команд или {BOLD}list{RESET} для списка приложений.{RESET}
"""
        )

    def print_help(self):
        print(
            f"""
{BOLD}Команды терминала:{RESET}
  {ACCENT_CYAN}list{RESET} (или {ACCENT_CYAN}agents{RESET})       Показать список приложений (Такси, Лавка, Маркет, АЗС, Юрист, Дешевле...)
  {ACCENT_CYAN}agent <имя>{RESET}           Переключить приложение/модель ({DIM}agent taxi{RESET}, {DIM}agent market{RESET}...)
  {ACCENT_CYAN}new{RESET}                    Начать новый диалог
  {ACCENT_CYAN}ephemeral on/off{RESET}       Включить/выключить авто-удаление чата при выходе
  {ACCENT_CYAN}dialogs{RESET}                Показать активные диалоги
  {ACCENT_CYAN}clear{RESET}                  Очистить экран
  {ACCENT_CYAN}help{RESET}                   Показать эту справку
  {ACCENT_CYAN}exit{RESET} (или {ACCENT_CYAN}bye{RESET})        Выйти (при включенном ephemeral чат удалится)
"""
        )

    def print_agents_table(self):
        print(f"\n{BOLD}{'ID / АЛИАС':<16} {'ТИП':<10} {'ИМЯ / СЕРВИС':<30} {'ОПИСАНИЕ':<35}{RESET}")
        print(f"{TEXT_MUTED}{'─'*85}{RESET}")

        for agent in registry.list_all():
            atype = agent.type.upper()
            color = ACCENT_GREEN if atype == "APP" else (ACCENT_CYAN if atype == "AGENT" else BOLD)
            marker = f" {ALICE_RED}●{RESET}" if agent.id == self.current_agent else "  "
            print(f"{marker}{color}{agent.id:<14}{RESET} {DIM}[{atype:<5}]{RESET} {agent.name:<30} {agent.description}")
        print()

    def render_widget(self, widget):
        title = widget.title or "Виджет"
        print(f"\n{ACCENT_YELLOW}┌── 📦 {title} {'─' * max(5, 55 - len(title))}┐{RESET}")
        if widget.url:
            print(f"{ACCENT_YELLOW}│{RESET}  {TEXT_MUTED}Ссылка / Действие:{RESET} {BOLD}{widget.url}{RESET}")
        if widget.component_name == "deep_research_sources":
            sources = widget.raw_data.get("sources", [])
            for s in sources:
                dt = s.get("DocumentTitle", "Источник")
                fu = s.get("FullUrl", "")
                print(f"{ACCENT_YELLOW}│{RESET}  • {dt[:45]}: {DIM}{fu[:45]}...{RESET}")
        print(f"{ACCENT_YELLOW}└──{'─' * 58}┘{RESET}")

    def render_suggestions(self, suggestions: List[SuggestionAction]):
        self.active_suggestions = suggestions
        if not suggestions:
            return

        print(f"\n{TEXT_MUTED}Варианты ответов / Адреса / Тарифы (введите номер):{RESET}")
        for i, sugg in enumerate(suggestions, start=1):
            print(f"  {ACCENT_YELLOW}[{i}]{RESET} {BOLD}{sugg.title}{RESET}")
        print()

    async def execute_turn(self, user_text: str, is_suggest: bool = False):
        printed_len = 0
        spinner_idx = 0
        is_thinking = False

        try:
            async for turn in self.manager.send_message(
                text=user_text,
                agent_id=self.current_agent,
                dialog_id=self.dialog_id,
                is_suggest=is_suggest,
            ):
                self.dialog_id = turn.dialog_id

                if turn.error:
                    if is_thinking:
                        sys.stdout.write("\r\033[2K")
                    print(f"\n\033[31m[Ошибка]: {turn.error}{RESET}\n")
                    return

                # Индикация размышлений
                if turn.thinking_logs and not turn.text:
                    is_thinking = True
                    curr_thought = turn.thinking_logs[-1]
                    frame = SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]
                    spinner_idx += 1
                    sys.stdout.write(f"\r\033[2K{TEXT_MUTED}{frame} [Алиса]:{RESET} {DIM}{curr_thought}{RESET}")
                    sys.stdout.flush()

                # Стриминг дельты без дублей
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
                        seen_urls = set()
                        for w in turn.widgets:
                            if w.url and w.url in seen_urls:
                                continue
                            if w.url:
                                seen_urls.add(w.url)
                            self.render_widget(w)

                    if turn.suggestions:
                        self.render_suggestions(turn.suggestions)
                    else:
                        self.active_suggestions = []

        except (asyncio.CancelledError, KeyboardInterrupt):
            sys.stdout.write("\r\033[2K")
            print(f"\n{TEXT_MUTED}[Запрос прерван по Ctrl+C]{RESET}\n")
            return
        except Exception as e:
            print(f"\n\033[31m[Ошибка]: {e}{RESET}")

    async def run_loop(self):
        if self.is_ephemeral:
            self.dialog_id = await self.manager.create_new_dialog(
                agent_id=self.current_agent, is_ephemeral=True
            )

        self.print_banner()

        while True:
            try:
                prompt_str = f"{BOLD}{ALICE_RED}>>> {RESET}"
                user_input = await asyncio.get_event_loop().run_in_executor(None, input, prompt_str)
                text = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{TEXT_MUTED}Завершение сеанса...{RESET}")
                break

            if not text:
                continue

            clean_cmd = text.lstrip("/").lower()
            parts = clean_cmd.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("exit", "bye", "quit", "q"):
                print(f"{TEXT_MUTED}До свидания!{RESET}")
                break

            elif cmd in ("help", "?"):
                self.print_help()
                continue

            elif cmd in ("list", "agents"):
                self.print_agents_table()
                continue

            elif cmd in ("agent", "model"):
                if not arg:
                    agent = registry.get(self.current_agent)
                    print(f"{TEXT_MUTED}Текущий режим: {BOLD}{agent.name}{RESET}. Использование: agent <имя>")
                else:
                    new_agent = registry.get(arg)
                    self.current_agent = new_agent.id
                    self.active_suggestions = []
                    print(f"{ACCENT_GREEN}✓{RESET} Режим изменен на: {BOLD}{new_agent.name}{RESET}")
                continue

            elif cmd == "ephemeral":
                if arg in ("on", "1", "true"):
                    self.is_ephemeral = True
                    self.dialog_id = await self.manager.create_new_dialog(agent_id=self.current_agent, is_ephemeral=True)
                    print(f"{ACCENT_GREEN}✓{RESET} Временный режим включен. Создан диалог {self.dialog_id[:10]}...")
                elif arg in ("off", "0", "false"):
                    self.is_ephemeral = False
                    print(f"{TEXT_MUTED}Временный режим отключен. Диалоги сохраняются в истории.{RESET}")
                else:
                    print(f"{TEXT_MUTED}Текущий статус ephemeral: {self.is_ephemeral}. Использование: ephemeral on/off{RESET}")
                continue

            elif cmd == "new":
                new_id = await self.manager.create_new_dialog(agent_id=self.current_agent, is_ephemeral=self.is_ephemeral)
                self.dialog_id = new_id
                self.active_suggestions = []
                print(f"{ACCENT_GREEN}✓{RESET} Создан новый диалог: {DIM}{new_id}{RESET}")
                continue

            elif cmd == "clear":
                os.system("cls" if os.name == "nt" else "clear")
                self.print_banner()
                continue

            elif cmd == "dialogs":
                print(f"\n{BOLD}Диалоги сессии:{RESET}")
                for did in self.manager.known_dialogs:
                    active_flag = f" {ALICE_RED}● активен{RESET}" if did == self.dialog_id else ""
                    eph_flag = f" {ACCENT_YELLOW}[ВРЕМЕННЫЙ]{RESET}" if did in self.manager.ephemeral_dialogs else ""
                    print(f"  {ACCENT_CYAN}{did[:10]}...{RESET}{eph_flag}{active_flag}")
                print()
                continue

            # Выбор варианта цифрой
            is_suggest = False
            if text.isdigit() and self.active_suggestions:
                idx = int(text) - 1
                if 0 <= idx < len(self.active_suggestions):
                    selected = self.active_suggestions[idx]
                    text = selected.payload or selected.title
                    is_suggest = True
                    print(f"{DIM}Выбран вариант [{idx+1}]: {selected.title}{RESET}")

            # Запуск выполнения хода
            await self.execute_turn(text, is_suggest=is_suggest)


async def async_main():
    parser = argparse.ArgumentParser(description="Yandex Alice AI CLI")
    parser.add_argument("--cookies", default=".alice_cookies", help="Путь к файлу кук (.alice_cookies)")
    parser.add_argument("--agent", default="pro", help="Начальный агент (pro, taxi, lavka, legal, best_price...)")
    parser.add_argument("-e", "--ephemeral", action="store_true", help="Временная сессия: удалить чат после выхода")
    parser.add_argument("-v", "--verbose", action="store_true", help="Компактный лог событий WS и RPC")
    args = parser.parse_args()

    try:
        cookie_str, cookie_dict = load_cookies(args.cookies)
    except Exception as e:
        print(f"\033[31m[Ошибка]: {e}{RESET}")
        sys.exit(1)

    print(f"{TEXT_MUTED}Проверка авторизации: прочитано {len(cookie_dict)} кук...{RESET}")
    if "Session_id" in cookie_dict:
        print(f"{ACCENT_GREEN}✓{RESET} Session_id найден ({cookie_dict['Session_id'][:15]}...)")
    else:
        print(f"\033[31m✗ Session_id отсутствует в {args.cookies}!\033[0m")

    manager = AliceAgentManager(cookie_str, cookie_dict, verbose=args.verbose)
    try:
        await manager.initialize()
    except Exception as e:
        print(f"\n\033[31m[Ошибка подключения]: {e}{RESET}")
        sys.exit(1)

    session = AliceCliSession(manager=manager, default_agent=args.agent, ephemeral=args.ephemeral)
    try:
        await session.run_loop()
    finally:
        await manager.shutdown()


def main():
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()