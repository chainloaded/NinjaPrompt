"""Sidebar UI. Colors come from the Qt palette that Binary Ninja fills from the active theme, so
the plugin follows theme changes without hardcoded colors."""
import copy
import json
import re
import threading

from binaryninjaui import (
    Sidebar,
    SidebarContextSensitivity,
    SidebarWidget,
    SidebarWidgetLocation,
    SidebarWidgetType,
    ThemeColor,
    UIActionHandler,
    getMonospaceFont,
    getThemeColor,
)
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import agent, config, llm
from .mcp_client import McpClient
from .store import Chat, ChatStore

# --------------------------------------------------------------------------- theme


def _mix(a, b, t):
    return QColor(
        int(a.red() * (1 - t) + b.red() * t),
        int(a.green() * (1 - t) + b.green() * t),
        int(a.blue() * (1 - t) + b.blue() * t),
    )


def _theme_color(name, fallback):
    try:
        return getThemeColor(getattr(ThemeColor, name))
    except Exception:
        return fallback


class Theme:
    def __init__(self):
        p = QApplication.palette()
        R = QPalette.ColorRole
        bg, fg = p.color(R.Window), p.color(R.WindowText)
        self.fg = fg.name()
        self.base = p.color(R.Base).name()
        self.dim = _mix(fg, bg, 0.45).name()
        self.border = _mix(bg, fg, 0.16).name()
        self.card = _mix(bg, fg, 0.07).name()
        self.accent = p.color(R.Highlight).name()
        self.accent_text = p.color(R.HighlightedText).name()
        self.red = _theme_color("RedStandardHighlightColor", QColor(224, 108, 117)).name()
        try:
            self.mono = getMonospaceFont(QApplication.activeWindow() or QWidget()).family()
        except Exception:
            self.mono = "monospace"

    def css(self):
        t = self
        return f"""
        QScrollArea, #chatBody {{ background: transparent; border: none; }}
        QTextBrowser {{ background: transparent; border: none; }}
        #userBubble {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 8px; }}
        #toolRow {{ border: 1px solid {t.border}; border-radius: 6px; }}
        #toolRow QToolButton {{ border: none; text-align: left; padding: 4px 6px; color: {t.dim}; }}
        #toolRow QToolButton:hover {{ color: {t.fg}; }}
        #toolBody {{ color: {t.dim}; font-family: "{t.mono}"; padding: 6px 8px; border-top: 1px solid {t.border}; }}
        #emptyLabel, #statusLabel {{ color: {t.dim}; }}
        #errorLabel {{ color: {t.red}; }}
        #composer {{ background: {t.base}; border: 1px solid {t.border}; border-radius: 8px; }}
        #composer QPlainTextEdit {{ background: transparent; border: none; }}
        #composer QComboBox {{ border: none; background: transparent; color: {t.dim}; padding: 2px 4px; }}
        #composer QComboBox::drop-down {{ border: none; width: 12px; }}
        #sendBtn {{ background: {t.accent}; color: {t.accent_text}; border: none; border-radius: 12px;
                    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; }}
        #sendBtn:disabled {{ background: {t.border}; color: {t.dim}; }}
        #headerBtn {{ border: none; padding: 4px 8px; border-radius: 6px; }}
        #headerBtn:hover {{ background: {t.card}; }}
        #headerBtn::menu-indicator {{ image: none; }}
        """


# --------------------------------------------------------------------------- helpers

_ADDR = re.compile(r"(?<![\w`/])(0x[0-9a-fA-F]{4,16})(?![\w`])")


def linkify(text):
    """Turn addresses into bn:// links so a click navigates Binary Ninja there."""
    out = []
    for i, part in enumerate(re.split(r"(```.*?```|`[^`\n]+`)", text, flags=re.S)):
        if i % 2:
            inner = part.strip("`")
            if not part.startswith("```") and re.fullmatch(r"0x[0-9a-fA-F]{4,16}", inner):
                part = f"[{part}](bn://{inner})"
        else:
            part = _ADDR.sub(lambda m: f"[{m.group(1)}](bn://{m.group(1)})", part)
        out.append(part)
    return "".join(out)


def _short(v, n=40):
    s = v if isinstance(v, str) else json.dumps(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _args_summary(args_json):
    try:
        args = json.loads(args_json or "{}")
        if isinstance(args, dict):
            return ", ".join(f"{k}={_short(v)}" for k, v in args.items())
    except ValueError:
        pass
    return _short(args_json or "", 60)


_live = set()


class _Bg(QObject):
    """Run fn() in a thread and call back on the GUI thread."""

    done = Signal(object, object)

    def __init__(self, fn, callback, parent):
        super().__init__(parent)
        self.fn, self.callback = fn, callback
        self.done.connect(self._finish)
        _live.add(self)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            self.done.emit(self.fn(), None)
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, e)

    def _finish(self, res, err):
        _live.discard(self)
        self.callback(res, err)
        self.deleteLater()


# --------------------------------------------------------------------------- chat widgets


class MessageText(QTextBrowser):
    navigate = Signal(int)

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setOpenLinks(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(lambda s: self.setFixedHeight(int(s.height()) + 4))
        self.anchorClicked.connect(self._clicked)

    def set_markdown(self, text):
        self.setMarkdown(linkify(text))

    def _clicked(self, url):
        if url.scheme() == "bn":
            try:
                self.navigate.emit(int(url.toString()[len("bn://"):], 16))
            except ValueError:
                pass
        else:
            QDesktopServices.openUrl(QUrl(url))


class ToolRow(QFrame):
    """One collapsed line per tool call; click to see arguments and output."""

    def __init__(self, name, args_json):
        super().__init__()
        self.setObjectName("toolRow")
        self._label = f"{name}  {_args_summary(args_json)}"
        self._args = args_json
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = QToolButton()
        self.head.setCheckable(True)
        self.head.setArrowType(Qt.ArrowType.RightArrow)
        self.head.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.head.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.head.setText(self._label + "  …")
        self.head.toggled.connect(self._toggle)
        self.body = QLabel()
        self.body.setObjectName("toolBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.body.setText(self._args)
        self.body.hide()
        lay.addWidget(self.head)
        lay.addWidget(self.body)

    def _toggle(self, on):
        self.head.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)

    def set_result(self, text):
        shown = text if len(text) <= 4000 else text[:4000] + f"\n… ({len(text) - 4000:,} more chars)"
        self.head.setText(f"{self._label}  ·  {len(text):,} chars")
        self.body.setText(f"{self._args}\n\n{shown}")


class ChatView(QScrollArea):
    navigate = Signal(int)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("chatBody")
        self.col = QVBoxLayout(body)
        self.col.setContentsMargins(10, 10, 10, 10)
        self.col.setSpacing(10)
        self.empty = QLabel()
        self.empty.setObjectName("emptyLabel")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.col.addWidget(self.empty)
        self.col.addStretch(1)
        self.setWidget(body)
        self._items, self._tools, self._stick = [], {}, True
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(lambda v: setattr(self, "_stick", v >= sb.maximum() - 24))
        sb.rangeChanged.connect(lambda _lo, hi: self._stick and sb.setValue(hi))

    def set_empty_text(self, text):
        self.empty.setText(text)

    def clear(self):
        for w in self._items:
            w.setParent(None)
            w.deleteLater()
        self._items, self._tools, self._stick = [], {}, True
        self.empty.show()

    def _add(self, w):
        self.empty.hide()
        self._items.append(w)
        self.col.insertWidget(self.col.count() - 1, w)

    def add_user(self, text):
        f = QFrame()
        f.setObjectName("userBubble")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(10, 8, 10, 8)
        lab = QLabel(text)
        lab.setTextFormat(Qt.TextFormat.PlainText)
        lab.setWordWrap(True)
        lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(lab)
        self._add(f)

    def add_assistant(self, text):
        w = MessageText()
        w.navigate.connect(self.navigate)
        w.set_markdown(text)
        self._add(w)

    def add_error(self, text):
        lab = QLabel(text)
        lab.setObjectName("errorLabel")
        lab.setWordWrap(True)
        lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._add(lab)

    def add_tool_call(self, call_id, name, args_json):
        row = ToolRow(name, args_json)
        self._tools[call_id] = row
        self._add(row)

    def set_tool_result(self, call_id, text):
        if call_id in self._tools:
            self._tools[call_id].set_result(text)

    def render(self, msg):
        role = msg.get("role")
        if role == "user":
            self.add_user(msg.get("content", ""))
        elif role == "assistant":
            if (msg.get("content") or "").strip():
                self.add_assistant(msg["content"])
            for tc in msg.get("tool_calls") or []:
                self.add_tool_call(tc["id"], tc["function"]["name"], tc["function"].get("arguments", ""))
        elif role == "tool":
            self.set_tool_result(msg.get("tool_call_id"), msg.get("content", ""))


class Composer(QPlainTextEdit):
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setPlaceholderText("Ask about this binary…")
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit()

    def _fit(self, *_):
        lines = max(1, min(8, int(self.document().size().height())))
        h = lines * QFontMetrics(self.font()).lineSpacing() + 2 * self.document().documentMargin() + 2
        self.setFixedHeight(int(h))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


# --------------------------------------------------------------------------- agent run (GUI-thread side)


class RunController(QObject):
    """Owns one agent run. The loop runs in a worker thread; every signal is delivered on the GUI thread."""

    message = Signal(object)
    status = Signal(str)
    finished = Signal(str)

    def __init__(self, widget, chat, store, profile, cfg):
        super().__init__(widget)
        self.widget, self.chat, self.store, self.profile, self.cfg = widget, chat, store, profile, cfg
        self.cancel = threading.Event()
        self.message.connect(self._on_message)
        self.status.connect(self._on_status)
        self.finished.connect(self._on_finished)

    def start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        err = ""
        try:
            agent.run_agent(
                list(self.chat.messages), self.cfg, self.profile, lambda kind, p: getattr(self, kind).emit(p), self.cancel
            )
        except agent.Cancelled:
            pass
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
        self.finished.emit(err)

    def _on_message(self, msg):
        self.widget.run_message(self, msg)

    def _on_status(self, text):
        self.widget.run_status(self, text)

    def _on_finished(self, err):
        self.widget.run_finished(self, err)


# --------------------------------------------------------------------------- settings dialog


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NinjaPrompt settings")
        self.setMinimumWidth(560)
        self.cfg = copy.deepcopy(cfg)
        self._row = -1
        tabs = QTabWidget()
        tabs.addTab(self._models_tab(), "Models")
        tabs.addTab(self._connection_tab(), "Binary Ninja")
        tabs.addTab(self._agent_tab(), "Agent")
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(box)
        self._fill_list()

    # -- models
    def _models_tab(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setMaximumWidth(150)
        add, rem = QPushButton("Add"), QPushButton("Remove")
        add.clicked.connect(self._add_profile)
        rem.clicked.connect(self._remove_profile)
        left.addWidget(self.list)
        left.addWidget(add)
        left.addWidget(rem)
        form = QFormLayout()
        self.f_name, self.f_url, self.f_model = QLineEdit(), QLineEdit(), QLineEdit()
        self.f_key, self.f_env, self.f_temp = QLineEdit(), QLineEdit(), QLineEdit()
        self.f_url.setPlaceholderText("https://api.openai.com/v1  ·  http://localhost:11434/v1  ·  https://api.deepseek.com")
        self.f_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.f_env.setPlaceholderText("optional, e.g. OPENAI_API_KEY (wins over the key above)")
        self.f_temp.setPlaceholderText("optional")
        self.f_effort = QComboBox()
        self.f_effort.setEditable(True)
        self.f_effort.addItems(["", "low", "medium", "high"])
        self.f_effort.lineEdit().setPlaceholderText("provider default")
        self.f_echo = QCheckBox("Send reasoning_content back (some thinking models need this with tools)")
        self.test_model = QPushButton("Test model")
        self.test_model.clicked.connect(self._test_model)
        for label, fld in (
            ("Name", self.f_name),
            ("Base URL", self.f_url),
            ("Model", self.f_model),
            ("API key", self.f_key),
            ("Key from env var", self.f_env),
            ("Temperature", self.f_temp),
            ("Reasoning effort", self.f_effort),
        ):
            form.addRow(label, fld)
        form.addRow("", self.f_echo)
        form.addRow("", self.test_model)
        self.f_name.textEdited.connect(lambda t: self.list.currentItem() and self.list.currentItem().setText(t))
        lay.addLayout(left)
        lay.addLayout(form, 1)
        self.list.currentRowChanged.connect(self._select)
        return w

    def _fill_list(self):
        self.list.clear()
        for p in self.cfg["profiles"]:
            self.list.addItem(p["name"])
        if self.cfg["profiles"]:
            self.list.setCurrentRow(0)
        else:
            self._select(-1)

    def _store_row(self):
        if 0 <= self._row < len(self.cfg["profiles"]):
            self.cfg["profiles"][self._row] = self._read_form()

    def _read_form(self):
        return {
            "name": self.f_name.text().strip() or "Model",
            "base_url": self.f_url.text().strip(),
            "model": self.f_model.text().strip(),
            "api_key": self.f_key.text(),
            "api_key_env": self.f_env.text().strip(),
            "temperature": self.f_temp.text().strip(),
            "reasoning_effort": self.f_effort.currentText().strip(),
            "echo_reasoning": self.f_echo.isChecked(),
        }

    def _select(self, row):
        self._store_row()
        self._row = row
        p = self.cfg["profiles"][row] if 0 <= row < len(self.cfg["profiles"]) else None
        for f in (self.f_name, self.f_url, self.f_model, self.f_key, self.f_env, self.f_temp, self.f_effort, self.f_echo, self.test_model):
            f.setEnabled(p is not None)
        p = p or config.new_profile()
        self.f_name.setText(p["name"])
        self.f_url.setText(p["base_url"])
        self.f_model.setText(p["model"])
        self.f_key.setText(p["api_key"])
        self.f_env.setText(p["api_key_env"])
        self.f_temp.setText(str(p.get("temperature", "")))
        self.f_effort.setCurrentText(p.get("reasoning_effort", ""))
        self.f_echo.setChecked(bool(p.get("echo_reasoning")))

    def _add_profile(self):
        self._store_row()
        self.cfg["profiles"].append(config.new_profile())
        self.list.addItem(self.cfg["profiles"][-1]["name"])
        self.list.setCurrentRow(len(self.cfg["profiles"]) - 1)

    def _remove_profile(self):
        r = self.list.currentRow()
        if r < 0:
            return
        self._row = -1
        del self.cfg["profiles"][r]
        self._fill_list()

    def _report(self, title, res, err):
        if err:
            QMessageBox.warning(self, title, str(err))
        else:
            QMessageBox.information(self, title, res)

    def _test_model(self):
        prof = self._read_form()
        _Bg(
            lambda: "The model replied: " + (llm.chat_completion(prof, [{"role": "user", "content": "Reply with: ok"}])[0]["content"] or "(empty)"),
            lambda res, err: self._report("Test model", res, err),
            self,
        )

    # -- connection
    def _connection_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.f_mcp_url, self.f_mcp_token = QLineEdit(self.cfg["mcp_url"]), QLineEdit(self.cfg["mcp_token"])
        self.f_mcp_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.f_mcp_token.setPlaceholderText("only if ui.mcp.token is set")
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_mcp)
        form.addRow("MCP URL", self.f_mcp_url)
        form.addRow("Bearer token", self.f_mcp_token)
        form.addRow("", test)
        hint = QLabel("Enable ui.mcp.enabled in Binary Ninja's settings, restart, then run Plugins > MCP > Start Server.\n\nSettings file (shared by all windows and files): " + config.path())
        hint.setWordWrap(True)
        form.addRow(hint)
        return w

    def _test_mcp(self):
        url, token = self.f_mcp_url.text().strip(), self.f_mcp_token.text()

        def probe():
            c = McpClient(url, token, timeout=15)
            c.initialize()
            n = len(c.list_tools())
            c.close()
            return f"Connected. The server offers {n} tools."

        _Bg(probe, lambda res, err: self._report("Test connection", res, err), self)

    # -- agent
    def _agent_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.f_prompt = QPlainTextEdit(self.cfg["system_prompt"])
        self.f_steps = QSpinBox()
        self.f_steps.setRange(1, 500)
        self.f_steps.setValue(int(self.cfg["max_steps"]))
        self.f_cap = QSpinBox()
        self.f_cap.setRange(1000, 1_000_000)
        self.f_cap.setSingleStep(1000)
        self.f_cap.setValue(int(self.cfg["max_result_chars"]))
        self.f_ctx = QSpinBox()
        self.f_ctx.setRange(5000, 5_000_000)
        self.f_ctx.setSingleStep(10000)
        self.f_ctx.setValue(int(self.cfg["context_tool_chars"]))
        form.addRow("System prompt", self.f_prompt)
        form.addRow("Max steps per message", self.f_steps)
        form.addRow("Max chars per tool result", self.f_cap)
        form.addRow("Prune old tool output above (chars)", self.f_ctx)
        return w

    def _save(self):
        self._store_row()
        self.cfg["mcp_url"] = self.f_mcp_url.text().strip()
        self.cfg["mcp_token"] = self.f_mcp_token.text()
        self.cfg["system_prompt"] = self.f_prompt.toPlainText().strip() or config.DEFAULT_SYSTEM_PROMPT
        self.cfg["max_steps"] = self.f_steps.value()
        self.cfg["max_result_chars"] = self.f_cap.value()
        self.cfg["context_tool_chars"] = self.f_ctx.value()
        names = [p["name"] for p in self.cfg["profiles"]]
        if self.cfg["active_profile"] not in names:
            self.cfg["active_profile"] = names[0] if names else ""
        config.save(self.cfg)
        self.accept()


# --------------------------------------------------------------------------- the sidebar widget


def _same_view(a, b):
    if a is b:
        return True
    if a is None or b is None:
        return False
    try:
        return a.file.session_id == b.file.session_id and a.view_type == b.view_type
    except Exception:
        return False


class NinjaPromptWidget(SidebarWidget):
    def __init__(self, name, frame, data):
        SidebarWidget.__init__(self, name)
        self.actionHandler = UIActionHandler()
        self.actionHandler.setupActionHandler(self)
        self.cfg = config.load()
        self.bv = self.store = self.chat = self.run = None
        self._css = ""
        self._title_full = ""
        self._build()
        self.restyle()
        self.set_view(data)

    # -- layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(6, 6, 6, 2)
        self.title_btn = QToolButton()
        self.title_btn.setObjectName("headerBtn")
        self.title_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.title_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._fill_menu)
        self.title_btn.setMenu(self.menu)
        new_btn = QToolButton()
        new_btn.setObjectName("headerBtn")
        new_btn.setText("+")
        new_btn.setToolTip("New chat")
        new_btn.clicked.connect(self.new_chat)
        gear = QToolButton()
        gear.setObjectName("headerBtn")
        gear.setText("⚙")
        gear.setToolTip("Settings")
        gear.clicked.connect(self.open_settings)
        head.addWidget(self.title_btn, 1)
        head.addWidget(new_btn)
        head.addWidget(gear)
        root.addLayout(head)

        self.view = ChatView()
        self.view.navigate.connect(self.goto)
        root.addWidget(self.view, 1)

        self.status = QLabel("")
        self.status.setObjectName("statusLabel")
        self.status.setContentsMargins(12, 0, 12, 2)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        root.addWidget(self.status)

        box = QFrame()
        box.setObjectName("composer")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(8, 6, 6, 6)
        bl.setSpacing(2)
        self.edit = Composer()
        self.edit.submitted.connect(self.on_send)
        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.send = QToolButton()
        self.send.setObjectName("sendBtn")
        self.send.clicked.connect(self.on_send_or_stop)
        row.addWidget(self.model_combo)
        row.addStretch(1)
        row.addWidget(self.send)
        bl.addWidget(self.edit)
        bl.addLayout(row)
        wrap = QVBoxLayout()
        wrap.setContentsMargins(8, 4, 8, 8)
        wrap.addWidget(box)
        root.addLayout(wrap)
        self._refresh_models()

    # -- theming
    def restyle(self):
        css = Theme().css()
        if css != self._css:
            self._css = css
            self.setStyleSheet(css)

    def event(self, e):
        if e.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.restyle()
        return super().event(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh_title()

    # -- binary view / chats
    def notifyViewChanged(self, view_frame):
        bv = None
        if view_frame is not None:
            try:
                bv = view_frame.getCurrentBinaryView()
            except Exception:
                bv = None
        self.set_view(bv)

    def _reload_cfg(self):
        """Settings are global and may have been changed from another window: re-read them."""
        self.cfg = config.load()
        self._refresh_models()

    def showEvent(self, e):
        self._reload_cfg()
        super().showEvent(e)

    def set_view(self, bv):
        self._reload_cfg()
        if bv is not None and _same_view(bv, self.bv):
            return
        self.bv = bv
        self.store = ChatStore(bv) if bv is not None else None
        self.chat = None
        if self.run is not None and _same_view(self.run.store.bv, bv):
            self.chat, self.store = self.run.chat, self.run.store
        elif self.store is not None:
            idx = self.store.list()
            if idx:
                self.chat = self.store.load(idx[0]["id"])
        self._show_chat()

    def _show_chat(self):
        self.view.clear()
        if self.bv is None:
            self.view.set_empty_text("Open a binary to start a chat.")
        else:
            self.view.set_empty_text("Ask a question about the binary open in the active view. The model reads it through Binary Ninja's MCP tools.")
        for m in (self.chat.messages if self.chat else []):
            self.view.render(m)
        self._refresh_title()
        self._update_controls()

    def _refresh_title(self):
        title = self.chat.title if self.chat else "New chat"
        fm = QFontMetrics(self.title_btn.font())
        self.title_btn.setText(fm.elidedText(title + "  ▾", Qt.TextElideMode.ElideRight, max(60, self.width() - 110)))

    def _fill_menu(self):
        self.menu.clear()
        chats = self.store.list() if self.store else []
        for e in chats[:30]:
            a = self.menu.addAction(("● " if self.chat and e["id"] == self.chat.id else "   ") + e["title"])
            a.triggered.connect(lambda _=False, cid=e["id"]: self.open_chat(cid))
        if chats:
            self.menu.addSeparator()
        r = self.menu.addAction("Rename chat…")
        r.setEnabled(self.chat is not None and bool(self.chat.messages))
        r.triggered.connect(self.rename_chat)
        d = self.menu.addAction("Delete chat")
        d.setEnabled(self.chat is not None and bool(self.chat.messages))
        d.triggered.connect(self.delete_chat)

    def open_chat(self, chat_id):
        if self.run is not None and self.run.chat.id == chat_id:
            self.chat = self.run.chat
        elif self.store:
            self.chat = self.store.load(chat_id)
        self._show_chat()

    def new_chat(self):
        if self.bv is None:
            return
        self.chat = Chat()
        self._show_chat()
        self.edit.setFocus()

    def rename_chat(self):
        text, ok = QInputDialog.getText(self, "Rename chat", "Title:", text=self.chat.title)
        if ok and text.strip():
            self.chat.title = text.strip()
            self.store.save(self.chat)
            self._refresh_title()

    def delete_chat(self):
        if not self.chat or (self.run and self.run.chat is self.chat):
            return
        if QMessageBox.question(self, "Delete chat", f"Delete “{self.chat.title}”?") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete(self.chat.id)
        self.chat = None
        idx = self.store.list()
        if idx:
            self.chat = self.store.load(idx[0]["id"])
        self._show_chat()

    # -- models / settings
    def _refresh_models(self):
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        names = [p["name"] for p in self.cfg["profiles"]]
        self.model_combo.addItems(names or ["No model yet"])
        if self.cfg["active_profile"] in names:
            self.model_combo.setCurrentIndex(names.index(self.cfg["active_profile"]))
        self.model_combo.blockSignals(False)

    def _model_changed(self, i):
        if 0 <= i < len(self.cfg["profiles"]):
            self.cfg["active_profile"] = self.cfg["profiles"][i]["name"]
            config.update(active_profile=self.cfg["active_profile"])

    def current_profile(self):
        i = self.model_combo.currentIndex()
        return self.cfg["profiles"][i] if 0 <= i < len(self.cfg["profiles"]) else None

    def open_settings(self):
        self._reload_cfg()
        if SettingsDialog(self.cfg, self).exec():
            self._reload_cfg()

    # -- sending
    def _update_controls(self):
        busy = self.run is not None
        mine = busy and self.chat is not None and self.run.chat is self.chat
        self.send.setText("■" if mine else "↑")
        self.send.setToolTip("Stop" if mine else "Send")
        self.send.setEnabled(self.bv is not None and (mine or not busy))
        self.edit.setPlaceholderText("Ask about this binary…" if not busy else "The model is working…")
        if not busy:
            self.status.setText("")

    def on_send_or_stop(self):
        if self.run is not None:
            self.run.cancel.set()
            self.status.setText("Stopping after the current request…")
        else:
            self.on_send()

    def on_send(self):
        text = self.edit.toPlainText().strip()
        if not text or self.run is not None or self.bv is None:
            return
        self._reload_cfg()
        profile = self.current_profile()
        if profile is None or not profile.get("model") or not profile.get("base_url"):
            self.open_settings()
            return
        if self.chat is None:
            self.chat = Chat()
        if not self.chat.messages:
            self.chat.title = text.splitlines()[0][:48]
            self._refresh_title()
        msg = {"role": "user", "content": text}
        self.chat.messages.append(msg)
        self.chat.touch()
        self.store.save(self.chat)
        self.view.render(msg)
        self.edit.clear()
        self.run = RunController(self, self.chat, self.store, profile, copy.deepcopy(self.cfg))
        self._update_controls()
        self.run.start()

    # -- run callbacks (GUI thread)
    def run_message(self, run, msg):
        run.chat.messages.append(msg)
        run.chat.touch()
        if self.chat is run.chat:
            self.view.render(msg)
        if msg.get("role") != "tool":
            run.store.save(run.chat)

    def run_status(self, run, text):
        if self.chat is run.chat:
            self.status.setText(text)

    def run_finished(self, run, err):
        run.store.save(run.chat)
        self.run = None
        if err and self.chat is run.chat:
            self.view.add_error(err)
        self._update_controls()
        run.deleteLater()

    # -- navigation
    def goto(self, addr):
        if self.bv is None:
            return
        try:
            self.bv.file.navigate(self.bv.file.view, addr)
        except Exception:
            pass


# --------------------------------------------------------------------------- sidebar registration


def _make_icon():
    img = QImage(56, 56, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(255, 255, 255))
    pen.setWidthF(4)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(9, 11, 38, 28), 8, 8)
    tail = QPainterPath()
    tail.moveTo(20, 39)
    tail.lineTo(18, 48)
    tail.lineTo(29, 39)
    p.drawPath(bubble)
    p.drawPath(tail)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255))
    for x in (20, 28, 36):
        p.drawEllipse(QPointF(x, 25), 2.6, 2.6)
    p.end()
    return img


class NinjaPromptSidebarType(SidebarWidgetType):
    def __init__(self):
        SidebarWidgetType.__init__(self, _make_icon(), "NinjaPrompt")

    def createWidget(self, frame, data):
        return NinjaPromptWidget("NinjaPrompt", frame, data)

    def defaultLocation(self):
        return SidebarWidgetLocation.RightContent

    def contextSensitivity(self):
        return SidebarContextSensitivity.SelfManagedSidebarContext


def register():
    Sidebar.addSidebarWidgetType(NinjaPromptSidebarType())
