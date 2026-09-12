import asyncio
import codecs
import fcntl
import os
import pty
import re
import shlex
import struct
import termios
import uuid

import pyte
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, ListItem


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


class DescButton(Button):
    def __init__(
        self,
        label: str,
        description: str,
        path: str,
        is_script: bool,
        is_new: bool = False,
        is_installed: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(label, tooltip=description, **kwargs)
        self.description = description
        self.path = path
        self.is_script = is_script
        self.is_new = is_new
        self.script_name = label
        self.is_installed = is_installed

    def on_mount(self) -> None:
        if self.is_installed:
            self.styles.border = ("heavy", "red")
            self.label = f"{self.script_name} "
        elif self.is_new:
            self.styles.border = ("heavy", "yellow")


class HistoryListItem(ListItem):
    def __init__(self, script_name: str) -> None:
        super().__init__(Label(script_name))
        self.script_name = script_name


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
        pid, fd = pty.fork()
        if pid == 0:
            argv = ["bash", "--norc", "--noprofile"]
            lang = os.environ.get("LANG") or "C.UTF-8"
            env = dict(
                os.environ,
                TERM="xterm-256color",
                LANG=lang,
                COLUMNS=str(self.ncol),
                LINES=str(self.nrow),
                PS1="$ ",
            )
            os.execvpe(argv[0], argv, env)
        self.child_pid = pid
        return fd

    def start(self) -> None:
        asyncio.create_task(self._run())
        asyncio.create_task(self._send_data())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()

        def on_output() -> None:
            try:
                raw = self.p_out.read(65536)
                self.data_or_disconnect = self._decoder.decode(raw)
                self.event.set()
            except Exception:
                loop.remove_reader(self.p_out)
                self.data_or_disconnect = None
                self.event.set()

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        while True:
            msg = await self.recv_queue.get()
            if msg[0] == "stdin":
                self.p_out.write(msg[1].encode())
            elif msg[0] == "set_size":
                winsize = struct.pack("HH", msg[1], msg[2])
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    async def _send_data(self) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            if self.data_or_disconnect is None:
                await self.send_queue.put(["disconnect", 1])
            else:
                await self.send_queue.put(["stdout", self.data_or_disconnect])


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
    _EXIT_LINE_RE = re.compile(re.escape(EXIT_MARKER) + r"\d+\r?\n?")
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

    def on_mount(self) -> None:
        self._spawn_pty()
        self.focus()

    def _spawn_pty(self) -> None:
        """Sobe um bash novo (usado no primeiro mount E pra recuperar
        automaticamente se o shell cair no meio do uso)."""
        self.pty = TerminalPTY(self.ncol, self.nrow)
        self.pty.start()
        self._screen = pyte.HistoryScreen(
            self.ncol, self.nrow, history=10000, ratio=0.25
        )
        self.stream = pyte.Stream(self._screen)
        self._display = PyteDisplay([Text()])
        self._exit_scan_buffer = ""
        if not hasattr(self, "_recv_started"):
            self._recv_started = True
            self.run_worker(self._recv(), exclusive=True)

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
        char = self.ctrl_keys.get(event.key) or event.character
        if char:
            await self.pty.recv_queue.put(["stdin", char])

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()  # não deixa o VerticalScroll externo also rolar
        self._screen.prev_page()
        self._render_screen()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._screen.next_page()
        self._render_screen()

    def on_resize(self, event: events.Resize) -> None:
        self._resize_terminal(event.size.width, event.size.height)

    def _resize_terminal(self, ncol: int, nrow: int) -> None:
        if ncol <= 0 or nrow <= 0 or (ncol == self.ncol and nrow == self.nrow):
            return

        self.ncol = ncol
        self.nrow = nrow
        self._screen.resize(nrow, ncol)  # pyte: (linhas, colunas)
        self._render_screen()

        if self.pty is not None:
            asyncio.create_task(
                self.pty.recv_queue.put(["set_size", nrow, ncol, 0, 0])
            )

    def run_script(
        self, command: str | list, env: dict[str, str] | None = None
    ) -> None:
        """Injeta 'bash <script>' no shell persistente, com um marcador
        de saída logo depois pra detectar o fim e capturar o exit code."""
        if self.pty is None:
            return
        self._awaiting_exit_code = True
        self._exit_scan_buffer = ""

        if isinstance(command, str):
            argv = ["bash", command]
        else:
            argv = list(command)

        quoted_command = " ".join(shlex.quote(str(part)) for part in argv)

        prefix = ""
        if env:
            exports = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in env.items()
            )
            prefix = f"export {exports}; "
        self._last_run_marker = f"@@LT_START@@:{uuid.uuid4().hex[:8]}"
        line = (
            f"{prefix}echo {self._last_run_marker}; "
            f"{quoted_command}; "
            "__lt_code=$?; "
            'read -rp "Pressione ENTER para continuar..." ; '
            f"echo {self.EXIT_MARKER}$__lt_code\n"
        )
        asyncio.create_task(self.pty.recv_queue.put(["stdin", line]))

    async def _recv(self) -> None:
        while True:
            message = await self.pty.send_queue.get()
            cmd = message[0]
            if cmd == "setup":
                await self.pty.recv_queue.put(
                    ["set_size", self.nrow, self.ncol, 567, 573]
                )
            elif cmd == "stdout":
                chars = message[1]
                self._check_exit_marker(chars)
                self._check_password_prompt(chars)
                self.stream.feed(self._EXIT_LINE_RE.sub("", chars))
                self._render_screen()
            elif cmd == "disconnect":
                self._awaiting_exit_code = False
                self.post_message(ScriptFinished(None))
                self._spawn_pty()

    def _check_exit_marker(self, chars: str) -> None:
        if not self._awaiting_exit_code:
            return
        self._exit_scan_buffer += chars
        idx = self._exit_scan_buffer.rfind(self.EXIT_MARKER)
        if idx == -1:
            return
        rest = self._exit_scan_buffer[idx + len(self.EXIT_MARKER) :]
        digits = ""
        for c in rest:
            if c.isdigit():
                digits += c
            else:
                break
        if digits:
            self._awaiting_exit_code = False
            self._exit_scan_buffer = ""
            self.post_message(ScriptFinished(int(digits)))

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
        asyncio.create_task(
            self.pty.recv_queue.put(["stdin", password + "\n"])
        )

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
            await asyncio.sleep(0.1)
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
