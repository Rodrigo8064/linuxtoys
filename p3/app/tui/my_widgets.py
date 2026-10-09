import asyncio
import codecs
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
import uuid

import pyte
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, ListItem

from .helper import is_removable


class FocusableLabel(Label):
    """Um Label que aceita foco via tecla TAB e emite um evento ao pressionar Enter/Espaço."""

    class Pressed(Message):
        """Disparado quando o label é clicado ou ativado via teclado."""

        def __init__(self, label: "FocusableLabel") -> None:
            self.label = label
            super().__init__()

        @property
        def control(self) -> Label:
            return self.label

    def __init__(
        self,
        renderable="",
        *,
        expand=False,
        shrink=False,
        markup=True,
        **kwargs,
    ) -> None:
        can_focus = kwargs.pop("can_focus", True)
        super().__init__(
            renderable, expand=expand, shrink=shrink, markup=markup, **kwargs
        )
        self.can_focus = can_focus

    def _on_key(self, event: events.Key):
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Pressed(self))

    def _on_click(self, event: events.Click):
        event.stop()
        self.post_message(self.Pressed(self))


class InfoButton(Button):
    def __init__(
        self,
        label: str,
        description: str,
        path: str,
        is_script: bool,
        is_new: bool = False,
        is_installed: bool = False,
        is_repo_entry: bool = False,
        is_appstream_entry: bool = False,
        has_app_page: bool = False,
        info: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(label, tooltip=description, **kwargs)
        self.description = description
        self.path = path
        self.is_script = is_script
        self.is_new = is_new
        self.script_name = label
        self.is_installed = is_installed
        self.is_appstream_entry = is_appstream_entry
        self.info = info or {}
        self.revert = bool(is_installed) and is_removable(
            str(label),
            path,
            is_repo_entry,
            is_appstream_entry,
            is_script=is_script,
            info=self.info,
        )
        self.is_repo_entry = is_repo_entry
        self.has_app_page = has_app_page

    def on_mount(self) -> None:
        if self.revert and self.is_installed:
            self.styles.border = ("tall", "red")
            self.styles.border_title_align = "left"
            self.border_title = "🗑"
            self.label = self.script_name
        elif self.is_new:
            self.styles.border = ("tall", "yellow")


class RegistryListItem(ListItem):
    def __init__(self, script_name: str) -> None:
        super().__init__(Label(script_name))
        self.script_name = script_name


class LanguageListItem(ListItem):
    def __init__(self, code: str, display_name: str) -> None:
        super().__init__(Label(display_name))
        self.code = code
        self.display_name = display_name


class PyteDisplay:
    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, console, options):
        yield from self.lines


class TerminalPTY:
    """Dono do PTY de verdade: spawna um bash persistente e faz a ponte
    entre ele e as filas assíncronas usadas pelo widget Terminal.
    Adaptado do exemplo oficial do pyte (não herda de App)."""

    def __init__(self, ncol: int, nrow: int) -> None:
        self.ncol = ncol
        self.nrow = nrow
        self.data_or_disconnect: str | None = None
        self.fd = self._open_terminal()
        self.p_out = os.fdopen(self.fd, "w+b", 0)
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.event = asyncio.Event()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _open_terminal(self) -> int:
        argv = ["bash", "--norc", "--noprofile", "--noediting", "+H"]
        env = dict(
            os.environ,
            TERM="xterm-256color",
            LANG=os.environ.get("LANG") or "C.UTF-8",
            PS1="$ ",
            PS2="",
        )
        pid, fd = pty.fork()
        if pid == 0:
            try:
                attrs = termios.tcgetattr(0)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(0, termios.TCSANOW, attrs)
            except termios.error:
                pass
            try:
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)  # nunca volta para o código do app
        self.child_pid = pid
        return fd

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def close(self) -> None:
        """Libera task, fd e processo bash."""
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
        try:
            asyncio.get_running_loop().remove_reader(self.p_out)
        except (ValueError, OSError, RuntimeError):
            pass
        try:
            self.p_out.close()
        except OSError:
            pass
        try:
            os.kill(self.child_pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            pass

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()

        def on_output() -> None:
            try:
                raw = self.p_out.read(65536)
                if not raw:
                    loop.remove_reader(self.p_out)
                    self.send_queue.put_nowait(["disconnect", 1])
                    return
                text = self._decoder.decode(raw)
                self.send_queue.put_nowait(["stdout", text])
            except Exception:
                loop.remove_reader(self.p_out)
                self.send_queue.put_nowait(["disconnect", 1])

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        while True:
            msg = await self.recv_queue.get()
            if msg[0] == "stdin":
                self.p_out.write(msg[1].encode())
            elif msg[0] == "set_size":
                winsize = struct.pack("HH", msg[1], msg[2])
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)


class ScriptFinished(Message):
    """Postada quando um script rodado via run_script() termina.
    exit_code=None significa que o próprio bash caiu (não o script)."""

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code
        super().__init__()


class PasswordPromptDetected(Message):
    """Postada quando o terminal parece estar esperando uma senha
    (heurística: procura 'password' no output recém-chegado). Não é
    garantia 100% — é detecção conservadora, igual à do library_loader."""


class Terminal(Widget, can_focus=True):
    EXIT_MARKER = "@@LT_EXIT@@:"
    _MARKER_TAG = "@@LT_"
    _MARKER_RE = re.compile(r"@@LT_(?:START@@:[0-9a-f]+|EXIT@@:\d+)\r?\n")
    _EXIT_CODE_RE = re.compile(re.escape(EXIT_MARKER) + r"(\d+)[\r\n]")
    _PASSWORD_RE = re.compile(r"(?i)password.*:\s*$")

    def __init__(self, ncol: int = 80, nrow: int = 24, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ctrl_keys = {
            "left": "\u001b[D",
            "right": "\u001b[C",
            "up": "\u001b[A",
            "down": "\u001b[B",
            "enter": "\r",
            "backspace": "\u007f",
            "tab": "\t",
            "escape": "\x1b",
            "ctrl+c": "\x03",  # SIGINT
            "ctrl+d": "\x04",  # EOF
            "ctrl+z": "\x1a",  # SIGTSTP
            "ctrl+l": "\x0c",  # Clear
        }
        self.ncol = ncol
        self.nrow = nrow
        self._display = PyteDisplay([Text()])
        self._screen = pyte.HistoryScreen(
            ncol, nrow, history=10000, ratio=0.25
        )
        self.stream = pyte.Stream(self._screen)
        self.pty: TerminalPTY | None = None
        self._exit_scan_buffer = ""
        self._awaiting_exit_code = False
        self._password_prompt_scan_buffer = ""
        self._password_prompt_pending = False
        self._last_run_marker: str | None = None
        self._marker_carry = ""

    def on_mount(self) -> None:
        self._spawn_pty()
        self.focus()

    @property
    def is_busy(self) -> bool:
        return self._awaiting_exit_code

    def _strip_markers(self, chars: str) -> str:
        """Remove linhas de marcador do texto exibido, mesmo quando elas
        chegam divididas entre dois blocos de saída."""
        data = self._MARKER_RE.sub("", self._marker_carry + chars)
        self._marker_carry = ""
        tag = self._MARKER_TAG

        idx = data.rfind(tag)
        if idx != -1 and "\n" not in data[idx:]:
            self._marker_carry = data[idx:]
            return data[:idx]
        for size in range(len(tag) - 1, 0, -1):
            if data.endswith(tag[:size]):
                self._marker_carry = data[-size:]
                return data[:-size]
        return data

    def _spawn_pty(self) -> None:
        """Sobe um bash novo (primeiro mount e recuperação de queda)."""
        old_pty, self.pty = self.pty, TerminalPTY(self.ncol, self.nrow)
        if old_pty is not None:
            old_pty.close()
        self.pty.start()
        self._screen = pyte.HistoryScreen(
            self.ncol, self.nrow, history=10000, ratio=0.25
        )
        self.stream = pyte.Stream(self._screen)
        self._display = PyteDisplay([Text()])
        self._exit_scan_buffer = ""
        self._marker_carry = ""
        if not hasattr(self, "_recv_started"):
            self._recv_started = True
            self.run_worker(self._recv(), exclusive=True)

    def on_unmount(self) -> None:
        if self.pty is not None:
            self.pty.close()

    def get_full_text(self, only_last_run: bool = False) -> str:
        """Reconstrói o texto puro (sem ANSI/cor) de tudo que já passou
        pelo terminal nessa sessão — histórico de rolagem + tela atual.
        Pensado pra relatórios de bug, não pra exibição visual."""

        def row_to_text(row: dict) -> str:
            if not row:
                return ""
            max_col = max(row.keys())
            return "".join(
                row.get(i, pyte.screens.Char(" ")).data
                for i in range(max_col + 1)
            ).rstrip()

        lines = [row_to_text(row) for row in self._screen.history.top]
        lines.extend(
            row_to_text(self._screen.buffer[row_idx])
            for row_idx in sorted(self._screen.buffer.keys())
        )
        full_text = "\n".join(lines)

        if only_last_run and self._last_run_marker:
            idx = full_text.rfind(self._last_run_marker)
            if idx != -1:
                newline_idx = full_text.find("\n", idx)
                if newline_idx != -1:
                    return full_text[newline_idx + 1 :]

        return full_text

    def render(self):
        return self._display

    async def on_key(self, event: events.Key) -> None:
        if event.key == "pageup":
            self._screen.prev_page()
            self._render_screen()
            return
        if event.key == "pagedown":
            self._screen.next_page()
            self._render_screen()
            return
        if self.pty is None:
            return
        if event.key == "ctrl+c" and self._awaiting_exit_code:
            event.prevent_default()
            event.stop()
            self.send_interrupt()
            return

        char = self.ctrl_keys.get(event.key) or event.character
        if char:
            event.prevent_default()
            event.stop()
            await self.pty.recv_queue.put(["stdin", char])

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()  # não deixa o VerticalScroll externo also rolar
        self._screen.prev_page()
        self._render_screen()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._screen.next_page()
        self._render_screen()

    async def on_resize(self, event: events.Resize) -> None:
        await self._resize_terminal(event.size.width, event.size.height)

    async def _resize_terminal(self, ncol: int, nrow: int) -> None:
        if ncol <= 0 or nrow <= 0 or (ncol == self.ncol and nrow == self.nrow):
            return

        self.ncol = ncol
        self.nrow = nrow
        self._screen.resize(nrow, ncol)  # pyte: (linhas, colunas)
        self._render_screen()

        if self.pty is not None:
            await self.pty.recv_queue.put(["set_size", nrow, ncol, 0, 0])

    @staticmethod
    def _split_marker(marker: str) -> tuple[str, str]:
        mid = len(marker) // 2
        return marker[:mid], marker[mid:]

    def run_script(
        self,
        command: str | list,
        env: dict[str, str] | None = None,
        pause_on_exit: bool = True,
    ) -> bool:
        """Injeta 'bash <script>' no shell persistente, com um marcador
        de saída logo depois pra detectar o fim e capturar o exit code."""
        if self.pty is None or self._awaiting_exit_code:
            return

        self._awaiting_exit_code = True
        self._exit_scan_buffer = ""
        self._marker_carry = ""
        self._password_prompt_pending = False
        self._password_prompt_scan_buffer = ""
        self._screen.reset()
        self._render_screen()

        argv = ["bash", command] if isinstance(command, str) else list(command)
        quoted_command = " ".join(shlex.quote(str(part)) for part in argv)
        if env:
            assignments = " ".join(
                f"{key}={shlex.quote(str(value))}"
                for key, value in env.items()
            )
            # `env` vale só para este comando: nada vaza para o bash persistente
            quoted_command = f"env {assignments} {quoted_command}"

        self._last_run_marker = f"@@LT_START@@:{uuid.uuid4().hex[:8]}"
        start_left, start_right = self._split_marker(self._last_run_marker)
        exit_left, exit_right = self._split_marker(self.EXIT_MARKER)
        pause_clause = (
            'read -rp "Pressione ENTER para continuar..." ; '
            if pause_on_exit
            else ": ;"
        )
        line = (
            f"_lt_s={shlex.quote(start_left)}; "
            f'_lt_s="$_lt_s"{shlex.quote(start_right)}; '
            'echo "$_lt_s"; '
            f"{quoted_command}; "
            "__lt_code=$?; "
            'if [ "$__lt_code" -eq 130 ] || [ "$__lt_code" -eq 100 ]; then '
            "__lt_code=100; "
            "else "
            f"{pause_clause}"
            "fi; "
            f"_lt_e={shlex.quote(exit_left)}; "
            f'_lt_e="$_lt_e"{shlex.quote(exit_right)}; '
            'echo "$_lt_e$__lt_code"\n'
        )
        self.pty.recv_queue.put_nowait(["stdin", line])
        return True

    async def _recv(self) -> None:
        while True:
            # Pega a primeira mensagem
            message = await self.pty.send_queue.get()
            messages = [message]

            # Drena todas as mensagens já disponíveis na fila (batching)
            while not self.pty.send_queue.empty():
                try:
                    messages.append(self.pty.send_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            has_stdout = False
            for msg in messages:
                cmd = msg[0]
                if cmd == "setup":
                    await self.pty.recv_queue.put(
                        ["set_size", self.nrow, self.ncol, 567, 573]
                    )
                elif cmd == "stdout":
                    chars = msg[1]
                    self._check_exit_marker(chars)
                    self._check_password_prompt(chars)
                    self.stream.feed(self._strip_markers(chars))
                    has_stdout = True
                elif cmd == "disconnect":
                    self._awaiting_exit_code = False
                    self.post_message(ScriptFinished(None))
                    self._spawn_pty()

            # Renderiza apenas uma vez por lote acumulado
            if has_stdout:
                self._render_screen()

            # Cede o loop para processar eventos de teclado/mouse sem lag
            await asyncio.sleep(0.01)

    def _check_exit_marker(self, chars: str) -> None:
        if not self._awaiting_exit_code:
            return
        buffer = self._exit_scan_buffer + chars
        match = None
        for match in self._EXIT_CODE_RE.finditer(buffer):
            pass  # fica com a última ocorrência
        if match is not None:
            self._awaiting_exit_code = False
            self._exit_scan_buffer = ""
            self.post_message(ScriptFinished(int(match.group(1))))
            return
        idx = buffer.rfind(self.EXIT_MARKER)
        keep_from = (
            idx
            if idx != -1
            else max(0, len(buffer) - (len(self.EXIT_MARKER) - 1))
        )
        self._exit_scan_buffer = buffer[keep_from:]

    def _check_password_prompt(self, chars: str) -> None:
        if self._password_prompt_pending:
            return
        self._password_prompt_scan_buffer = (
            self._password_prompt_scan_buffer + chars
        )[-200:]
        if self._PASSWORD_RE.search(self._password_prompt_scan_buffer):
            self._password_prompt_pending = True
            self.post_message(PasswordPromptDetected())

    def send_password(self, password: str) -> None:
        """Envia a senha coletada por um diálogo, como se o usuário
        tivesse digitado ela + Enter direto no terminal."""
        if self.pty is None:
            return
        self._password_prompt_pending = False
        self._password_prompt_scan_buffer = ""
        self.pty.recv_queue.put_nowait(["stdin", password + "\n"])

    def cancel_password_prompt(self) -> None:
        """Usuário cancelou o diálogo — libera a detecção de novo (útil
        se ele preferir digitar direto no terminal em vez do diálogo)."""
        self._password_prompt_pending = False
        self._password_prompt_scan_buffer = ""

    def send_interrupt(self) -> None:
        if self.pty is None:
            return

        async def _do_interrupt() -> None:
            await self.pty.recv_queue.put(["stdin", "\x03"])
            await asyncio.sleep(0.05)
            await self.pty.recv_queue.put(
                ["stdin", f"echo {self.EXIT_MARKER}100\n"]
            )

        asyncio.create_task(_do_interrupt())

    def _render_screen(self) -> None:
        lines = []
        for i, line in enumerate(self._screen.display):
            text = Text.from_ansi(line)
            x = self._screen.cursor.x
            if i == self._screen.cursor.y and x < len(text):
                cursor = text[x]
                cursor.stylize("reverse")
                new_text = text[:x]
                new_text.append(cursor)
                new_text.append(text[x + 1 :])
                text = new_text
            lines.append(text)
        self._display = PyteDisplay(lines)
        self.refresh()
