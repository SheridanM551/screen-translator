"""Control panel window + system tray icon.

The panel is excluded from screen capture (like the overlay), so the Japanese text it lists
is never OCR'd and the screen content behind it is still what gets translated.
"""
from __future__ import annotations

import ctypes
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

ENGINES = [("llm", "LLM（品質好，Sakura-GalTransl 7B）"), ("nmt", "非 LLM（較快，品質較差）")]
MODES = [("refine", "先快後準（先逐句，再整頁修正）"), ("batch", "整頁一次翻（較慢，不會換字）"),
         ("single", "逐句（帶歷史，不做整頁修正）")]


def make_icon(color: str = "#2f6fde") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    f = QFont("Microsoft JhengHei", 30)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignCenter, "譯")
    p.end()
    return QIcon(pm)


def _exclude_from_capture(widget: QWidget) -> None:
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)
    except Exception:
        pass


class ControlPanel(QWidget):
    def __init__(self, engine, act: Callable[[str], None], hotkeys: dict, save_cfg: Callable,
                 hotkey_errors: list[str]):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.engine, self.act, self.save_cfg = engine, act, save_cfg
        self.setWindowTitle("即時日翻中 — 翻譯記錄與狀態")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(480)
        self._quitting = False
        self._seen_recent = 0
        hk = {k: "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in v.split("+"))
              for k, v in hotkeys.items()}

        root = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setStyleSheet("font-size:15px; font-weight:bold;")
        root.addWidget(self.status)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#888;")
        root.addWidget(self.info)

        grid = QGridLayout()
        self.btn_toggle = QPushButton()
        buttons = [
            (self.btn_toggle, "toggle"),
            (QPushButton(f"隱藏/顯示譯文  ({hk.get('peek', '')})"), "peek"),
            (QPushButton(f"選取翻譯範圍  ({hk.get('region', '')})"), "region"),
            (QPushButton(f"改回全螢幕  ({hk.get('fullscreen', '')})"), "fullscreen"),
            (QPushButton(f"清除上下文/術語  ({hk.get('reset_context', '')})"), "reset_context"),
            (QPushButton(f"結束程式  ({hk.get('quit', '')})"), "quit"),
        ]
        self._hk_toggle = hk.get("toggle", "")
        for i, (b, name) in enumerate(buttons):
            b.clicked.connect(lambda _=False, n=name: self.act(n))
            b.setMinimumHeight(30)
            grid.addWidget(b, i // 2, i % 2)
        root.addLayout(grid)

        row = QHBoxLayout()
        row.addWidget(QLabel("翻譯引擎"))
        self.engine_box = QComboBox()
        for k, label in ENGINES:
            self.engine_box.addItem(label, k)
        self.engine_box.setCurrentIndex([k for k, _ in ENGINES].index(engine.c["engine"]))
        self.engine_box.currentIndexChanged.connect(self._engine_changed)
        row.addWidget(self.engine_box, 1)
        root.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("上下文模式"))
        self.mode_box = QComboBox()
        for k, label in MODES:
            self.mode_box.addItem(label, k)
        self.mode_box.setCurrentIndex([k for k, _ in MODES].index(engine.c["context"]["mode"]))
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode_box, 1)
        root.addLayout(row)

        root.addWidget(QLabel("最近翻譯（快 = 快速版，準 = 整頁修正後）"))
        self.recent = QListWidget()
        self.recent.setWordWrap(True)
        self.recent.setMinimumHeight(220)
        root.addWidget(self.recent, 1)

        tip = "關閉此視窗會縮到右下角系統匣（熱鍵照常可用）；徹底結束請按「結束程式」。"
        if hotkey_errors:
            tip += "\n⚠ 以下熱鍵被其他程式占用，請用按鈕或改 config.json：" + "、".join(hotkey_errors)
        foot = QLabel(tip)
        foot.setWordWrap(True)
        foot.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(foot)

        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        a = QAction("翻譯記錄與狀態", self)
        a.triggered.connect(self.show_panel)
        menu.addAction(a)
        self.tray_toggle = QAction("", self)
        self.tray_toggle.triggered.connect(lambda: self.act("toggle"))
        menu.addAction(self.tray_toggle)
        for name, label in [("peek", "隱藏/顯示譯文"), ("region", "選取翻譯範圍"),
                            ("fullscreen", "改回全螢幕"), ("reset_context", "清除上下文/術語")]:
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, n=name: self.act(n))
            menu.addAction(a)
        menu.addSeparator()
        a = QAction("結束", self)
        a.triggered.connect(lambda: self.act("quit"))
        menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_panel() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()
        self._ready_announced = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    # ---------- window behaviour ----------
    def showEvent(self, e):
        super().showEvent(e)
        _exclude_from_capture(self)

    def show_panel(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self._quitting:
            e.accept()
            return
        e.ignore()
        self.hide()  # translation keeps running; toolbar / tray reopen this window

    def quit_now(self):
        self._quitting = True
        self.tray.hide()
        self.close()

    # ---------- settings ----------
    def _engine_changed(self, i):
        kind = self.engine_box.itemData(i)
        self.save_cfg(engine=kind)
        self.engine.switch_engine(kind)

    def _mode_changed(self, i):
        mode = self.mode_box.itemData(i)
        self.engine.c["context"]["mode"] = mode  # read by every new page, so it applies at once
        self.save_cfg(context={"mode": mode})

    # ---------- status ----------
    def refresh(self):
        e = self.engine
        if e.error:
            text, color = "● 錯誤：" + e.error + "（詳見 logs\\app.log）", "#c0392b"
        elif not e.ready.is_set():
            text, color = ("● 翻譯模型載入中…（約 5–10 秒）" if e.c["engine"] == "llm"
                           else "● 翻譯模型載入中…"), "#d68910"
        elif not e.enabled:
            text, color = "● 已暫停", "#7f8c8d"
        else:
            text, color = "● 翻譯中：畫面出現日文就會自動覆蓋", "#1e8449"
        self.status.setText(text)
        self.status.setStyleSheet(f"font-size:15px; font-weight:bold; color:{color};")
        self.btn_toggle.setText(("暫停翻譯" if e.enabled else "開始翻譯") + f"  ({self._hk_toggle})")
        self.tray_toggle.setText("暫停翻譯" if e.enabled else "開始翻譯")
        region = "全螢幕" if not e.region else f"區域 {e.region[2] - e.region[0]}×{e.region[3] - e.region[1]}"
        s = e.stats
        self.info.setText(f"範圍：{region}　｜　畫面上 {len(e.blocks)} 段　｜　OCR {s['ocr_ms']:.0f} ms"
                          f"　｜　上次翻譯 {s['tr_ms'] / 1000:.2f} 秒")
        self.mode_box.setEnabled(e.c["engine"] == "llm")  # NMT has no context support
        for box, keys, cur in ((self.engine_box, [k for k, _ in ENGINES], e.c["engine"]),
                               (self.mode_box, [k for k, _ in MODES], e.c["context"]["mode"])):
            if cur in keys and box.currentIndex() != keys.index(cur):  # changed from the toolbar
                box.blockSignals(True)
                box.setCurrentIndex(keys.index(cur))
                box.blockSignals(False)
        self.tray.setToolTip("即時日翻中 — " + text.lstrip("● "))
        if e.ready.is_set() and not self._ready_announced:
            self._ready_announced = True
            self.tray.showMessage("即時日翻中", "已就緒：畫面上出現日文會自動翻譯覆蓋。\n"
                                  "工具列在螢幕最上方中央，滑鼠移到最上面就會出現。",
                                  QSystemTrayIcon.Information, 3500)
        if not e.ready.is_set():
            self._ready_announced = False
        items = list(e.recent)
        if len(items) != self._seen_recent or (items and self.recent.count() == 0):
            self._seen_recent = len(items)
            self.recent.clear()
            for src, zh, tag in reversed(items[-30:]):
                it = QListWidgetItem((f"[{tag}] " if tag else "") + f"{zh}\n    {src}")
                self.recent.addItem(it)
