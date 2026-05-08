import sys
import os
import json
import gc
import datetime
import ctypes
import atexit
import random
import calendar as py_calendar

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QFileDialog, 
    QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget,
    QComboBox, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QGridLayout, QSizePolicy,
    QListWidget, QListWidgetItem, QListView, QDialog, QScrollArea, QFrame,
    QTimeEdit, QAbstractSpinBox, QSlider
)
from PyQt6.QtGui import QPixmap, QImageReader, QGuiApplication, QPixmapCache, QCursor, QMovie, QImage, QIcon, QColor, QPainter, QPen, QFont, QFontMetrics
from PyQt6.QtCore import Qt, QTimer, QEvent, QSize, QPropertyAnimation, QAbstractAnimation, QThread, pyqtSignal, QRect, QEasingCurve, QTime, QUrl

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    QT_MULTIMEDIA_AVAILABLE = True
except Exception:
    QMediaPlayer = None
    QAudioOutput = None
    QT_MULTIMEDIA_AVAILABLE = False

CONFIG_FILE = "config.json"
ALARM_TRIGGER_WINDOW_SECONDS = 300
SNOOZE_MINUTES = 5
ALARM_REPEAT_DAILY = "daily"
ALARM_REPEAT_WEEKDAYS = "weekdays"
SUPPORTED_RINGTONE_EXTS = {".wav", ".mp3"}
NORMAL_THUMBNAIL_DELAY_MS = 15
LOW_POWER_THUMBNAIL_DELAY_MS = 45
CONFIG_SAVE_DEBOUNCE_MS = 1500
LOW_POWER_CONFIG_SAVE_DEBOUNCE_MS = 8000
GC_COLLECT_EVERY_IMAGES = 10
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def current_screen_geometry(widget=None, available=False):
    screen = None
    if widget is not None:
        window = widget.window()
        handle = window.windowHandle() if window else None
        if handle:
            screen = handle.screen()
        if screen is None and window:
            screen = QGuiApplication.screenAt(window.frameGeometry().center())

    if screen is None:
        screen = QGuiApplication.primaryScreen()

    if screen is None:
        return QRect(0, 0, 1024, 768)

    return screen.availableGeometry() if available else screen.geometry()

def parse_alarm_time(time_text):
    try:
        hour_text, minute_text = str(time_text).split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return None

def normalize_alarm_time(time_text):
    parsed = parse_alarm_time(time_text)
    if not parsed:
        return None
    hour, minute = parsed
    return f"{hour:02d}:{minute:02d}"

def normalize_alarm_ringtone(path):
    if not path:
        return ""

    path = os.path.abspath(os.path.expanduser(str(path)))
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED_RINGTONE_EXTS and os.path.isfile(path):
        return path
    return ""

def alarm_ringtone_text(alarm):
    ringtone_path = alarm.get("ringtone_path", "")
    if ringtone_path:
        filename = os.path.basename(ringtone_path)
        if os.path.isfile(ringtone_path):
            return filename
        return f"{filename} 不可用"
    return "系统提示音"

def normalize_alarm_repeat(repeat):
    if repeat in ("weekday", "weekdays", "workdays"):
        return ALARM_REPEAT_WEEKDAYS
    return ALARM_REPEAT_DAILY

def alarm_repeat_text(alarm):
    repeat = normalize_alarm_repeat(alarm.get("repeat", ALARM_REPEAT_DAILY))
    return "周一至周五" if repeat == ALARM_REPEAT_WEEKDAYS else "每日"

def alarm_active_on_date(alarm, target_date):
    repeat = normalize_alarm_repeat(alarm.get("repeat", ALARM_REPEAT_DAILY))
    if repeat == ALARM_REPEAT_WEEKDAYS:
        return target_date.weekday() < 5
    return True

def make_alarm(time_text, enabled=True, alarm_id=None, last_triggered_date="", repeat=ALARM_REPEAT_DAILY, ringtone_path=""):
    clean_time = normalize_alarm_time(time_text)
    if not clean_time:
        return None

    return {
        "id": alarm_id or "alarm_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "time": clean_time,
        "enabled": bool(enabled),
        "repeat": normalize_alarm_repeat(repeat),
        "ringtone_path": normalize_alarm_ringtone(ringtone_path),
        "last_triggered_date": last_triggered_date or ""
    }

def normalize_alarm_list(raw_alarms):
    alarms = []
    used_ids = set()
    if not isinstance(raw_alarms, list):
        return alarms

    for index, raw_alarm in enumerate(raw_alarms):
        if isinstance(raw_alarm, str):
            alarm = make_alarm(raw_alarm, alarm_id=f"alarm_legacy_{index}")
        elif isinstance(raw_alarm, dict):
            alarm = make_alarm(
                raw_alarm.get("time", ""),
                enabled=raw_alarm.get("enabled", True),
                alarm_id=str(raw_alarm.get("id") or f"alarm_{index}"),
                last_triggered_date=str(raw_alarm.get("last_triggered_date", "")),
                repeat=raw_alarm.get("repeat", ALARM_REPEAT_DAILY),
                ringtone_path=raw_alarm.get("ringtone_path", "")
            )
        else:
            alarm = None

        if not alarm:
            continue

        base_id = alarm["id"]
        suffix = 1
        while alarm["id"] in used_ids:
            suffix += 1
            alarm["id"] = f"{base_id}_{suffix}"
        used_ids.add(alarm["id"])
        alarms.append(alarm)

    alarms.sort(key=lambda item: item["time"])
    return alarms

def alarm_next_datetime(alarm, now=None):
    if not alarm.get("enabled", True):
        return None

    parsed = parse_alarm_time(alarm.get("time", ""))
    if not parsed:
        return None

    now = now or datetime.datetime.now()
    hour, minute = parsed
    for day_offset in range(8):
        target_date = (now + datetime.timedelta(days=day_offset)).date()
        if not alarm_active_on_date(alarm, target_date):
            continue

        candidate = datetime.datetime.combine(target_date, datetime.time(hour, minute))
        if candidate > now:
            return candidate
    return None

def next_enabled_alarm_datetime(alarms, now=None):
    now = now or datetime.datetime.now()
    candidates = [alarm_next_datetime(alarm, now) for alarm in alarms]
    candidates = [candidate for candidate in candidates if candidate is not None]
    return min(candidates) if candidates else None

def set_sleep_prevention(enabled):
    if sys.platform != "win32":
        return False

    try:
        flags = ES_CONTINUOUS
        if enabled:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        return False

class TouchMenuDialog(QDialog):
    def __init__(self, parent, title, options, delete_callback=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.selected_data = None
        self.selected_text = None

        parent_window = parent.window() if parent else None
        target_geometry = parent_window.geometry() if parent_window else current_screen_geometry(parent)
        if not target_geometry.isValid() or target_geometry.width() <= 0 or target_geometry.height() <= 0:
            target_geometry = current_screen_geometry(parent)
        self.setGeometry(target_geometry)
        screen_size = target_geometry.size()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        backdrop = QFrame()
        backdrop.setStyleSheet("background-color: rgba(0, 0, 0, 205);")
        bg_layout = QVBoxLayout(backdrop)
        
        container = QWidget()
        container.setFixedWidth(min(screen_size.width() - 40, 600))
        container.setStyleSheet(
            "QWidget { background-color: #18201c; border-radius: 8px; }"
            "QPushButton { font-size: 22px; padding: 24px; color: #fff8ec; background-color: transparent; border: none; border-bottom: 1px solid rgba(255,255,255,35); border-radius: 0; }"
            "QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); }"
            "QLabel { font-size: 24px; color: #fff8ec; font-weight: bold; padding: 25px; border-bottom: 2px solid rgba(217,182,109,80); }"
        )
        
        v = QVBoxLayout(container)
        v.setSpacing(0)
        v.setContentsMargins(0, 0, 0, 0)
        
        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")
        
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(0)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        
        for text, data_val in options.items():
            if delete_callback:
                row_widget = QFrame()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(0)
                
                btn = QPushButton(text)
                btn.setStyleSheet("QPushButton { font-size: 22px; padding: 24px; color: #fff8ec; background-color: transparent; border: none; border-bottom: 1px solid rgba(255,255,255,35); border-radius: 0; text-align: left; } QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); }")
                btn.clicked.connect(lambda checked, t=text, d=data_val: self.select_item(t, d))
                row_layout.addWidget(btn, 1)
                
                del_btn = QPushButton("删除")
                del_btn.setStyleSheet("QPushButton { font-size: 18px; padding: 24px 28px; color: #ff8a7a; background-color: transparent; border: none; border-bottom: 1px solid rgba(255,255,255,35); border-radius: 0; } QPushButton:hover, QPushButton:pressed { background-color: rgba(123, 46, 40, 180); }")
                
                def make_handler(target_widget, val):
                    return lambda: [delete_callback(val), target_widget.deleteLater()]
                    
                del_btn.clicked.connect(make_handler(row_widget, data_val))
                row_layout.addWidget(del_btn, 0)
                
                scroll_layout.addWidget(row_widget)
            else:
                btn = QPushButton(text)
                btn.clicked.connect(lambda checked, t=text, d=data_val: self.select_item(t, d))
                scroll_layout.addWidget(btn)
            
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet("color: #ff8a7a; font-size: 20px; border-bottom: none; border-top: 2px solid rgba(217,182,109,80); padding: 20px;")
        cancel_btn.clicked.connect(self.reject)
        scroll_layout.addWidget(cancel_btn)
        
        scroll.setWidget(scroll_content)
        v.addWidget(scroll)

        bg_layout.addWidget(container, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(backdrop)
        
    def select_item(self, text, data):
        self.selected_text = text
        self.selected_data = data
        self.accept()
        
    def mousePressEvent(self, event):
        child = self.childAt(event.pos())
        if child is None or child.parentWidget() == self:
            self.reject()
        super().mousePressEvent(event)

class AlarmEditorDialog(QDialog):
    def __init__(self, parent, alarm=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.selected_time = None
        self.selected_repeat = ALARM_REPEAT_DAILY
        self.selected_ringtone_path = normalize_alarm_ringtone(alarm.get("ringtone_path", "")) if alarm else ""
        self.repeat_mode = normalize_alarm_repeat(alarm.get("repeat", ALARM_REPEAT_DAILY)) if alarm else ALARM_REPEAT_DAILY

        parent_window = parent.window() if parent else None
        target_geometry = parent_window.geometry() if parent_window else current_screen_geometry(parent)
        if not target_geometry.isValid() or target_geometry.width() <= 0 or target_geometry.height() <= 0:
            target_geometry = current_screen_geometry(parent)
        self.setGeometry(target_geometry)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        backdrop = QFrame()
        backdrop.setStyleSheet("background-color: rgba(0, 0, 0, 205);")
        bg_layout = QVBoxLayout(backdrop)

        container = QWidget()
        container.setFixedWidth(min(target_geometry.width() - 40, 520))
        container.setStyleSheet("""
            QWidget { background-color: #18201c; border-radius: 8px; }
            QLabel { color: #fff8ec; background: transparent; }
            QTimeEdit {
                font-size: 54px;
                font-weight: bold;
                color: #fff8ec;
                background-color: rgba(255, 255, 255, 12);
                border: 2px solid rgba(217, 182, 109, 95);
                border-radius: 8px;
                padding: 18px;
            }
            QPushButton {
                font-size: 22px;
                padding: 18px 24px;
                color: #fff8ec;
                background-color: rgba(255, 255, 255, 14);
                border: 1px solid rgba(255, 255, 255, 48);
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); border-color: #d9b66d; }
        """)

        v = QVBoxLayout(container)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(18)

        title = QLabel("设置闹钟")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 28px; font-weight: bold;")
        v.addWidget(title)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setWrapping(True)
        self.time_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.time_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_edit.setTime(self._initialTime(alarm))
        v.addWidget(self.time_edit)

        repeat_label = QLabel("重复规则")
        repeat_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        repeat_label.setStyleSheet("font-size: 18px; color: rgba(255, 255, 255, 190);")
        v.addWidget(repeat_label)

        repeat_layout = QHBoxLayout()
        repeat_layout.setSpacing(10)
        self.btn_repeat_daily = QPushButton("每日")
        self.btn_repeat_weekdays = QPushButton("周一至周五")
        self.btn_repeat_daily.clicked.connect(lambda: self.setRepeatMode(ALARM_REPEAT_DAILY))
        self.btn_repeat_weekdays.clicked.connect(lambda: self.setRepeatMode(ALARM_REPEAT_WEEKDAYS))
        repeat_layout.addWidget(self.btn_repeat_daily)
        repeat_layout.addWidget(self.btn_repeat_weekdays)
        v.addLayout(repeat_layout)
        self.updateRepeatButtons()

        ringtone_label = QLabel("自定义铃声")
        ringtone_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ringtone_label.setStyleSheet("font-size: 18px; color: rgba(255, 255, 255, 190);")
        v.addWidget(ringtone_label)

        ringtone_layout = QHBoxLayout()
        ringtone_layout.setSpacing(10)
        self.btn_ringtone = QPushButton(self._ringtoneButtonText())
        self.btn_ringtone.setStyleSheet("""
            QPushButton {
                font-size: 16px; padding: 14px 18px; color: #fff8ec;
                background-color: rgba(255, 255, 255, 14); border: 1px solid rgba(255, 255, 255, 48); border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); border-color: #d9b66d; }
        """)
        self.btn_ringtone.clicked.connect(self.chooseRingtone)
        ringtone_layout.addWidget(self.btn_ringtone)

        self.btn_clear_ringtone = QPushButton("清除")
        self.btn_clear_ringtone.setStyleSheet("font-size: 16px; padding: 14px 18px; color: #ff8a7a; background-color: rgba(255, 255, 255, 14); border: 1px solid rgba(255, 255, 255, 48); border-radius: 8px;")
        self.btn_clear_ringtone.clicked.connect(self.clearRingtone)
        ringtone_layout.addWidget(self.btn_clear_ringtone)
        v.addLayout(ringtone_layout)

        buttons = QHBoxLayout()
        self.btn_save = QPushButton("保存")
        self.btn_cancel = QPushButton("取消")
        self.btn_save.setStyleSheet("background-color: #d9b66d; color: #1a1308; font-weight: bold;")
        self.btn_cancel.setStyleSheet("background-color: rgba(255, 255, 255, 14);")
        self.btn_save.clicked.connect(self.acceptTime)
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        v.addLayout(buttons)

        bg_layout.addWidget(container, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(backdrop)

    def _ringtoneButtonText(self):
        if self.selected_ringtone_path and os.path.isfile(self.selected_ringtone_path):
            return f"铃声: {os.path.basename(self.selected_ringtone_path)}"
        return "选择铃声文件 (wav/mp3)"

    def chooseRingtone(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择铃声文件", "",
            "音频文件 (*.wav *.mp3);;WAV 文件 (*.wav);;MP3 文件 (*.mp3);;所有文件 (*)"
        )
        if file_path:
            normalized = normalize_alarm_ringtone(file_path)
            if normalized:
                self.selected_ringtone_path = normalized
            else:
                self.selected_ringtone_path = ""
            self.btn_ringtone.setText(self._ringtoneButtonText())

    def clearRingtone(self):
        self.selected_ringtone_path = ""
        self.btn_ringtone.setText(self._ringtoneButtonText())

    def _initialTime(self, alarm):
        if alarm:
            parsed = parse_alarm_time(alarm.get("time", ""))
            if parsed:
                return QTime(parsed[0], parsed[1])

        now = datetime.datetime.now() + datetime.timedelta(minutes=5)
        rounded_total_minutes = ((now.hour * 60 + now.minute + 4) // 5) * 5
        rounded_total_minutes %= 24 * 60
        return QTime(rounded_total_minutes // 60, rounded_total_minutes % 60)

    def acceptTime(self):
        self.selected_time = self.time_edit.time().toString("HH:mm")
        self.selected_repeat = self.repeat_mode
        self.accept()

    def setRepeatMode(self, repeat):
        self.repeat_mode = normalize_alarm_repeat(repeat)
        self.updateRepeatButtons()

    def updateRepeatButtons(self):
        base = "font-size: 22px; padding: 18px 24px; border-radius: 8px;"
        active = base + "color: #1a1308; background-color: #d9b66d; border: 2px solid #efc974; font-weight: bold;"
        inactive = base + "color: #fff8ec; background-color: rgba(255, 255, 255, 14); border: 1px solid rgba(255, 255, 255, 48);"
        self.btn_repeat_daily.setStyleSheet(active if self.repeat_mode == ALARM_REPEAT_DAILY else inactive)
        self.btn_repeat_weekdays.setStyleSheet(active if self.repeat_mode == ALARM_REPEAT_WEEKDAYS else inactive)


class AlarmSettingsDialog(QDialog):
    def __init__(self, parent, alarms):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.alarms = normalize_alarm_list([dict(alarm) if isinstance(alarm, dict) else alarm for alarm in alarms])

        parent_window = parent.window() if parent else None
        target_geometry = parent_window.geometry() if parent_window else current_screen_geometry(parent)
        if not target_geometry.isValid() or target_geometry.width() <= 0 or target_geometry.height() <= 0:
            target_geometry = current_screen_geometry(parent)
        self.setGeometry(target_geometry)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        backdrop = QFrame()
        backdrop.setStyleSheet("background-color: rgba(0, 0, 0, 205);")
        bg_layout = QVBoxLayout(backdrop)

        container = QWidget()
        container.setFixedWidth(min(target_geometry.width() - 40, 900))
        container.setMinimumHeight(min(target_geometry.height() - 80, 560))
        container.setStyleSheet("""
            QWidget { background-color: #18201c; border-radius: 8px; }
            QLabel { color: #fff8ec; background: transparent; }
            QPushButton {
                font-size: 20px;
                padding: 16px 18px;
                color: #fff8ec;
                background-color: rgba(255, 255, 255, 12);
                border: 1px solid rgba(255, 255, 255, 48);
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); border-color: #d9b66d; }
        """)

        v = QVBoxLayout(container)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(14)

        title = QLabel("闹钟设置")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 30px; font-weight: bold;")
        v.addWidget(title)

        self.btn_add = QPushButton("添加闹钟")
        self.btn_add.clicked.connect(self.addAlarm)
        self.btn_add.setMinimumHeight(58)
        v.addWidget(self.btn_add)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(min(target_geometry.height() - 310, 260))
        scroll.setStyleSheet("border: none; background: transparent;")
        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.scroll_content)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        scroll.setWidget(self.scroll_content)
        v.addWidget(scroll, 1)

        self.btn_done = QPushButton("完成")
        self.btn_done.setStyleSheet("background-color: #d9b66d; color: #1a1308; font-weight: bold;")
        self.btn_done.setMinimumHeight(58)
        self.btn_done.clicked.connect(self.accept)
        v.addWidget(self.btn_done)

        bg_layout.addWidget(container, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(backdrop)
        self.rebuildList()

    def rebuildList(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not self.alarms:
            empty = QLabel("暂无闹钟")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("font-size: 20px; color: rgba(255, 255, 255, 170); padding: 35px;")
            self.list_layout.addWidget(empty)
            return

        for alarm in self.alarms:
            row = QFrame()
            row.setStyleSheet("QFrame { background-color: rgba(255, 255, 255, 12); border-radius: 8px; }")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 8, 8, 8)
            row_layout.setSpacing(8)
            info_layout = QHBoxLayout()
            info_layout.setContentsMargins(0, 0, 0, 0)
            info_layout.setSpacing(10)

            state_text = "已开启" if alarm.get("enabled", True) else "已关闭"
            main_btn = QPushButton(f"{alarm['time']}  {alarm_repeat_text(alarm)}")
            main_btn.setStyleSheet("text-align: left; border: none; background-color: transparent; font-size: 22px; padding: 10px 12px;")
            main_btn.setEnabled(False)
            info_layout.addWidget(main_btn, 0)

            state_label = QLabel(state_text)
            state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            state_label.setStyleSheet(
                "font-size: 17px; font-weight: 700; padding: 6px 13px; border-radius: 8px; "
                + ("color: #eaffef; background-color: rgba(42, 132, 74, 190);" if alarm.get("enabled", True) else "color: rgba(255, 255, 255, 190); background-color: rgba(130, 130, 130, 120);")
            )
            info_layout.addWidget(state_label, 0)
            info_layout.addStretch()
            row_layout.addLayout(info_layout, 1)

            edit_btn = QPushButton("编辑")
            edit_btn.clicked.connect(lambda checked=False, alarm_id=alarm["id"]: self.editAlarm(alarm_id))
            row_layout.addWidget(edit_btn, 0)

            toggle_btn = QPushButton("停用" if alarm.get("enabled", True) else "启用")
            toggle_btn.clicked.connect(lambda checked=False, alarm_id=alarm["id"]: self.toggleAlarm(alarm_id))
            row_layout.addWidget(toggle_btn, 0)

            del_btn = QPushButton("删除")
            del_btn.setStyleSheet("color: #ff8a7a;")
            del_btn.clicked.connect(lambda checked=False, alarm_id=alarm["id"]: self.deleteAlarm(alarm_id))
            row_layout.addWidget(del_btn, 0)

            self.list_layout.addWidget(row)

        self.list_layout.addStretch()

    def addAlarm(self):
        dlg = AlarmEditorDialog(self)
        if dlg.exec() and dlg.selected_time:
            existing = next((alarm for alarm in self.alarms if alarm["time"] == dlg.selected_time), None)
            if existing:
                existing["enabled"] = True
                existing["repeat"] = dlg.selected_repeat
                existing["ringtone_path"] = normalize_alarm_ringtone(dlg.selected_ringtone_path)
                existing["last_triggered_date"] = ""
            else:
                alarm = make_alarm(dlg.selected_time, repeat=dlg.selected_repeat, ringtone_path=dlg.selected_ringtone_path)
                if alarm:
                    self.alarms.append(alarm)
            self.alarms = normalize_alarm_list(self.alarms)
            self.rebuildList()

    def editAlarm(self, alarm_id):
        alarm = self._findAlarm(alarm_id)
        if not alarm:
            return

        dlg = AlarmEditorDialog(self, alarm)
        if dlg.exec() and dlg.selected_time:
            alarm["time"] = dlg.selected_time
            alarm["repeat"] = dlg.selected_repeat
            alarm["ringtone_path"] = normalize_alarm_ringtone(dlg.selected_ringtone_path)
            alarm["last_triggered_date"] = ""
            self.alarms = [item for item in self.alarms if item["id"] == alarm_id or item["time"] != dlg.selected_time]
            self.alarms = normalize_alarm_list(self.alarms)
            self.rebuildList()

    def toggleAlarm(self, alarm_id):
        alarm = self._findAlarm(alarm_id)
        if alarm:
            alarm["enabled"] = not alarm.get("enabled", True)
            self.rebuildList()

    def deleteAlarm(self, alarm_id):
        self.alarms = [alarm for alarm in self.alarms if alarm["id"] != alarm_id]
        self.rebuildList()

    def alarmsResult(self):
        return normalize_alarm_list(self.alarms)

    def _findAlarm(self, alarm_id):
        return next((alarm for alarm in self.alarms if alarm["id"] == alarm_id), None)


class AlarmRingDialog(QDialog):
    def __init__(self, parent, alarm_time, ringtone_path=""):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.action = "dismiss"
        self.ringtone_path = ringtone_path
        self.media_player = None
        self.audio_output = None

        parent_window = parent.window() if parent else None
        target_geometry = parent_window.geometry() if parent_window else current_screen_geometry(parent)
        if not target_geometry.isValid() or target_geometry.width() <= 0 or target_geometry.height() <= 0:
            target_geometry = current_screen_geometry(parent)
        self.setGeometry(target_geometry)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        backdrop = QFrame()
        backdrop.setStyleSheet("background-color: rgba(0, 0, 0, 215);")
        bg_layout = QVBoxLayout(backdrop)

        container = QWidget()
        container.setFixedWidth(min(target_geometry.width() - 40, 560))
        container.setStyleSheet("""
            QWidget { background-color: #18201c; border-radius: 8px; }
            QLabel { color: #fff8ec; background: transparent; }
            QPushButton {
                font-size: 22px;
                padding: 18px 22px;
                color: #fff8ec;
                background-color: rgba(255, 255, 255, 14);
                border: 1px solid rgba(255, 255, 255, 48);
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: rgba(217, 182, 109, 42); border-color: #d9b66d; }
        """)

        v = QVBoxLayout(container)
        v.setContentsMargins(28, 28, 28, 28)
        v.setSpacing(18)

        title = QLabel("闹钟响了")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 32px; font-weight: bold;")
        v.addWidget(title)

        time_label = QLabel(alarm_time)
        time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        time_label.setStyleSheet("font-size: 68px; font-weight: bold; color: #ffffff;")
        v.addWidget(time_label)

        ringtone_name = alarm_ringtone_text({"ringtone_path": ringtone_path}) if ringtone_path else "系统提示音"
        ringtone_hint = QLabel(f"铃声: {ringtone_name}")
        ringtone_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ringtone_hint.setStyleSheet("font-size: 16px; color: rgba(255, 255, 255, 150);")
        v.addWidget(ringtone_hint)

        buttons = QHBoxLayout()
        snooze_btn = QPushButton(f"稍后 {SNOOZE_MINUTES} 分钟")
        dismiss_btn = QPushButton("关闭闹钟")
        dismiss_btn.setStyleSheet("background-color: #d9b66d; color: #1a1308; font-weight: bold;")
        snooze_btn.clicked.connect(self.snooze)
        dismiss_btn.clicked.connect(self.dismiss)
        buttons.addWidget(snooze_btn)
        buttons.addWidget(dismiss_btn)
        v.addLayout(buttons)

        bg_layout.addWidget(container, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(backdrop)

        self._startRingtone()

    def _startRingtone(self):
        if QT_MULTIMEDIA_AVAILABLE and self.ringtone_path and os.path.isfile(self.ringtone_path):
            try:
                self.audio_output = QAudioOutput()
                self.media_player = QMediaPlayer()
                self.media_player.setAudioOutput(self.audio_output)
                self.media_player.setSource(QUrl.fromLocalFile(self.ringtone_path))
                self.media_player.mediaStatusChanged.connect(self._onMediaStatusChanged)
                self.audio_output.setVolume(1.0)
                self.media_player.play()
                return
            except Exception as e:
                print("自定义铃声播放失败，回退到系统提示音:", e)

        self.beep_timer = QTimer(self)
        self.beep_timer.timeout.connect(QApplication.beep)
        self.beep_timer.start(2500)
        QTimer.singleShot(120, QApplication.beep)

    def _onMediaStatusChanged(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.media_player:
                self.media_player.setPosition(0)
                self.media_player.play()

    def snooze(self):
        self._finish("snooze")

    def dismiss(self):
        self._finish("dismiss")

    def _finish(self, action):
        self.action = action
        if self.media_player:
            self.media_player.stop()
        if hasattr(self, "beep_timer") and self.beep_timer:
            self.beep_timer.stop()
        self.accept()

    def reject(self):
        self.dismiss()

    def closeEvent(self, event):
        if self.media_player:
            self.media_player.stop()
        if hasattr(self, "beep_timer") and self.beep_timer:
            self.beep_timer.stop()
        super().closeEvent(event)


class AlarmBadge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._time_text = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def setTimeText(self, text):
        self._time_text = text or ""
        self.setVisible(bool(self._time_text))
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        if not self._time_text:
            return QSize(1, 1)

        font = QFont(self.font())
        font.setPointSize(10)
        metrics = QFontMetrics(font)
        return QSize(24 + metrics.horizontalAdvance(self._time_text) + 6, 24)

    def paintEvent(self, event):
        if not self._time_text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(QColor(255, 255, 255, 215))
        pen.setWidthF(1.8)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        icon_rect = QRect(3, 6, 15, 15)
        painter.drawEllipse(icon_rect)
        painter.drawLine(10, 8, 10, 14)
        painter.drawLine(10, 14, 14, 14)
        painter.drawLine(6, 4, 3, 7)
        painter.drawLine(15, 4, 18, 7)
        painter.drawLine(7, 21, 5, 23)
        painter.drawLine(15, 21, 17, 23)

        font = QFont(self.font())
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 225))
        metrics = QFontMetrics(font)
        text_x = 25
        text_y = (self.height() + metrics.ascent() - metrics.descent()) // 2
        painter.drawText(text_x, text_y, self._time_text)


class CalendarBadge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(238, 188)
        self.background_opacity = 62

    def sizeHint(self):
        return QSize(238, 188)

    def setBackgroundOpacity(self, opacity):
        try:
            opacity = int(opacity)
        except Exception:
            opacity = 62
        self.background_opacity = max(20, min(180, opacity))
        self.update()

    def paintEvent(self, event):
        today = datetime.date.today()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        def draw_shadowed_text(rect, flags, text, color):
            painter.setPen(QColor(0, 0, 0, 155))
            painter.drawText(rect.adjusted(1, 1, 1, 1), flags, text)
            painter.setPen(color)
            painter.drawText(rect, flags, text)

        bg_rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 18, 18, self.background_opacity))
        painter.drawRoundedRect(bg_rect, 32, 32)
        painter.setPen(QPen(QColor(255, 255, 255, 28), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(bg_rect.adjusted(1, 1, -1, -1), 31, 31)

        painter.setPen(QColor(255, 255, 255, 235))
        title_font = QFont(self.font())
        title_font.setPointSize(12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(0, 0, 0, 155))
        painter.drawText(QRect(13, 10, self.width() - 24, 24), Qt.AlignmentFlag.AlignCenter, today.strftime("%Y年%m月"))
        painter.setPen(QColor(255, 255, 255, 235))
        painter.drawText(QRect(12, 9, self.width() - 24, 24), Qt.AlignmentFlag.AlignCenter, today.strftime("%Y年%m月"))

        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        week_font = QFont(self.font())
        week_font.setPointSize(9)
        week_font.setBold(True)
        painter.setFont(week_font)
        for index, day_text in enumerate(weekdays):
            x = 15 + index * 30
            draw_shadowed_text(QRect(x, 38, 28, 18), Qt.AlignmentFlag.AlignCenter, day_text, QColor(255, 255, 255, 170))

        day_font = QFont(self.font())
        day_font.setPointSize(9)
        month_days = py_calendar.Calendar(firstweekday=0).monthdayscalendar(today.year, today.month)
        for row, week in enumerate(month_days):
            for col, day in enumerate(week):
                if day == 0:
                    continue

                rect = QRect(15 + col * 30, 61 + row * 20, 28, 18)
                if day == today.day:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(0, 120, 215, 130))
                    painter.drawRoundedRect(rect.adjusted(1, 0, -1, 0), 9, 9)
                    text_color = QColor(255, 255, 255, 245)
                    day_font.setBold(True)
                else:
                    text_color = QColor(255, 255, 255, 210)
                    day_font.setBold(False)
                painter.setFont(day_font)
                draw_shadowed_text(rect, Qt.AlignmentFlag.AlignCenter, str(day), text_color)


class ThumbnailLoader(QThread):
    thumbnail_ready = pyqtSignal(int, QImage)
    
    def __init__(self, paths, delay_ms=NORMAL_THUMBNAIL_DELAY_MS, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.running = True
        self.delay_ms = delay_ms

    def run(self):
        for i, path in enumerate(self.paths):
            if not self.running: break
            try:
                # 低功耗机器上缩略图后台解码不能抢走播放线程。
                QThread.msleep(self.delay_ms)
                
                image = QImage()
                is_gif = path.lower().endswith('.gif')
                if is_gif:
                    movie = QMovie(path)
                    movie.jumpToFrame(0)
                    image = movie.currentImage()
                    if not image.isNull():
                        image = image.scaled(180, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
                else:
                    reader = QImageReader(path)
                    reader.setAutoTransform(True)
                    orig_size = reader.size()
                    if orig_size.isValid():
                        orig_size.scale(180, 140, Qt.AspectRatioMode.KeepAspectRatio)
                        reader.setScaledSize(orig_size)
                    img = reader.read()
                    if not img.isNull():
                        image = img
                        
                if not image.isNull():
                    # 统一格式转换，提升QPixmap在主线程转换时的稳定性
                    image = image.convertToFormat(QImage.Format.Format_RGB32)
                    if self.running:
                        self.thumbnail_ready.emit(i, image)
            except Exception:
                pass


class SettingsPage(QWidget):
    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self.folder_path = ""
        self.recent_paths = []
        self.folder_progress = {}
        self.folder_fixed_states = {}
        self.current_show_clock = True
        self.current_alarms = []
        self.current_low_power_mode = False
        self.current_random_play = False
        self.current_show_calendar = False
        self.current_calendar_opacity = 62
        self._config_save_timer = QTimer(self)
        self._config_save_timer.setSingleShot(True)
        self._config_save_timer.timeout.connect(self.saveConfig)
        self.initUI()
        self.loadConfig()

    def initUI(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        self.setObjectName("SettingsPage")
        self.setStyleSheet("""
            QWidget#SettingsPage {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #101311, stop:0.58 #151816, stop:1 #1b1712);
                color: #f6f0e6;
                font-family: "Microsoft YaHei UI", "Segoe UI";
            }
            QLabel {
                color: #f6f0e6;
                background: transparent;
            }
            QLabel#Eyebrow {
                color: #d9b66d;
                font-size: 14px;
                font-weight: bold;
            }
            QLabel#HeroTitle {
                color: #fff8ec;
                font-size: 42px;
                font-weight: 800;
            }
            QLabel#HeroText {
                color: rgba(246, 240, 230, 190);
                font-size: 16px;
                line-height: 130%;
            }
            QLabel#SectionTitle {
                color: #fff8ec;
                font-size: 28px;
                font-weight: 800;
            }
            QLabel#SectionHint, QLabel#RowHint, QLabel#SummaryLine {
                color: rgba(246, 240, 230, 170);
                font-size: 14px;
            }
            QLabel#RowTitle {
                color: #fff8ec;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#ErrorLabel {
                color: #ff8a7a;
                font-size: 15px;
                font-weight: 700;
            }
            QFrame#HeroPanel {
                background-color: rgba(24, 30, 27, 235);
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 8px;
            }
            QFrame#SummaryPanel {
                background-color: rgba(255, 255, 255, 12);
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 8px;
            }
            QFrame#SettingRow {
                background-color: rgba(255, 255, 255, 15);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 8px;
            }
            QPushButton {
                font-family: "Microsoft YaHei UI", "Segoe UI";
                border-radius: 8px;
                min-height: 42px;
            }
            QPushButton#OptionButton {
                color: #fff8ec;
                background-color: rgba(30, 37, 33, 230);
                border: 1px solid rgba(217, 182, 109, 95);
                font-size: 16px;
                padding: 12px 16px;
                text-align: left;
            }
            QPushButton#OptionButton:hover, QPushButton#SmallButton:hover {
                background-color: rgba(60, 70, 60, 235);
                border-color: #d9b66d;
            }
            QPushButton#SwitchOnButton {
                color: #eaffef;
                background-color: rgba(42, 132, 74, 190);
                border: 1px solid rgba(107, 214, 137, 120);
                font-size: 16px;
                font-weight: 800;
                padding: 12px 16px;
                text-align: center;
            }
            QPushButton#SwitchOffButton {
                color: rgba(255, 255, 255, 190);
                background-color: rgba(130, 130, 130, 120);
                border: 1px solid rgba(255, 255, 255, 42);
                font-size: 16px;
                font-weight: 800;
                padding: 12px 16px;
                text-align: center;
            }
            QPushButton#SwitchOnButton:hover {
                background-color: rgba(54, 154, 88, 220);
            }
            QPushButton#SwitchOffButton:hover {
                background-color: rgba(150, 150, 150, 145);
            }
            QPushButton#SmallButton {
                color: #fff8ec;
                background-color: rgba(49, 54, 49, 230);
                border: 1px solid rgba(255, 255, 255, 48);
                font-size: 15px;
                padding: 12px 16px;
                min-width: 112px;
            }
            QPushButton#PrimaryButton {
                color: #1a1308;
                background-color: #d9b66d;
                border: none;
                font-size: 24px;
                font-weight: 800;
                padding: 16px 28px;
            }
            QPushButton#PrimaryButton:hover {
                background-color: #efc974;
            }
            QPushButton#SecondaryButton {
                color: #f6f0e6;
                background-color: rgba(255, 255, 255, 14);
                border: 1px solid rgba(255, 255, 255, 48);
                font-size: 15px;
                padding: 12px 16px;
            }
            QPushButton#SecondaryButton:hover {
                background-color: rgba(255, 255, 255, 26);
                border-color: rgba(255, 255, 255, 80);
            }
            QPushButton#DangerButton {
                color: #ffe9e4;
                background-color: rgba(123, 46, 40, 190);
                border: 1px solid rgba(255, 138, 122, 95);
                font-size: 15px;
                padding: 12px 16px;
            }
            QPushButton#DangerButton:hover {
                background-color: rgba(160, 55, 47, 230);
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: rgba(255, 255, 255, 45);
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #d9b66d;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 22px;
                height: 22px;
                margin: -8px 0;
                background: #fff8ec;
                border: 2px solid #d9b66d;
                border-radius: 11px;
            }
        """)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(22)
        layout.addLayout(shell, 1)

        hero = QFrame()
        hero.setObjectName("HeroPanel")
        hero.setMinimumWidth(330)
        hero.setMaximumWidth(430)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(28, 28, 28, 28)
        hero_layout.setSpacing(16)

        eyebrow = QLabel("DIGITAL PHOTO ALBUM")
        eyebrow.setObjectName("Eyebrow")
        self.title = QLabel("电子相框")
        self.title.setObjectName("HeroTitle")

        hero_layout.addWidget(eyebrow)
        hero_layout.addWidget(self.title)
        hero_layout.addSpacing(10)

        summary_panel = QFrame()
        summary_panel.setObjectName("SummaryPanel")
        summary_layout = QVBoxLayout(summary_panel)
        summary_layout.setContentsMargins(18, 16, 18, 16)
        summary_layout.setSpacing(10)
        self.summary_folder = QLabel("相册  未选择")
        self.summary_interval = QLabel("间隔  5 秒极速挂机")
        self.summary_mode = QLabel("视效  淡出至黑屏再淡入")
        self.summary_random = QLabel("顺序  顺序播放")
        self.summary_performance = QLabel("性能  标准")
        self.summary_clock = QLabel("时钟  开启")
        self.summary_calendar = QLabel("日历  关闭")
        self.summary_alarm = QLabel("闹钟  无闹钟")
        for label in (
            self.summary_folder, self.summary_interval, self.summary_mode,
            self.summary_random, self.summary_performance, self.summary_clock,
            self.summary_calendar, self.summary_alarm
        ):
            label.setObjectName("SummaryLine")
            label.setWordWrap(True)
            summary_layout.addWidget(label)
        hero_layout.addWidget(summary_panel)

        hero_layout.addStretch()

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setMinimumHeight(28)
        hero_layout.addWidget(self.error_label)

        self.btn_start = QPushButton("开始播放")
        self.btn_start.setObjectName("PrimaryButton")
        self.btn_start.setMinimumHeight(64)
        self.btn_start.clicked.connect(self.start)
        hero_layout.addWidget(self.btn_start)

        hero_actions = QHBoxLayout()
        hero_actions.setSpacing(10)
        self.btn_fullscreen = QPushButton("窗口 / 全屏 (F11)")
        self.btn_fullscreen.setObjectName("SecondaryButton")
        self.btn_fullscreen.clicked.connect(self.main_app.toggleFullscreen)
        self.btn_exit = QPushButton("退出")
        self.btn_exit.setObjectName("DangerButton")
        self.btn_exit.clicked.connect(sys.exit)
        hero_actions.addWidget(self.btn_fullscreen)
        hero_actions.addWidget(self.btn_exit)
        hero_layout.addLayout(hero_actions)

        shell.addWidget(hero)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 6, 0, 6)
        controls_layout.setSpacing(14)
        shell.addWidget(controls, 1)

        section_title = QLabel("播放设置")
        section_title.setObjectName("SectionTitle")
        section_hint = QLabel("选择相册、播放节奏和屏幕附加信息。")
        section_hint.setObjectName("SectionHint")
        section_hint.setWordWrap(True)
        controls_layout.addWidget(section_title)
        controls_layout.addWidget(section_hint)
        controls_layout.addSpacing(6)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
            QScrollBar::handle:vertical { background: rgba(217, 182, 109, 95); border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        settings_body = QWidget()
        settings_body.setStyleSheet("background: transparent;")
        settings_layout = QVBoxLayout(settings_body)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(10)

        self.btn_recent = QPushButton("▼ 未选择目录 (点击选择历史)")
        self.btn_recent.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_recent.clicked.connect(self.onRecentClicked)
        self.btn_select = QPushButton("浏览文件夹")
        self.btn_select.clicked.connect(self.chooseDirectory)
        settings_layout.addWidget(self._makeSettingRow(
            "相册路径",
            "从历史记录快速切换，或浏览一个新的照片文件夹。",
            self.btn_recent,
            self.btn_select
        ))

        self.btn_alarm = QPushButton("▼ 无闹钟")
        self.btn_alarm.clicked.connect(self.onAlarmClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "闹钟",
            "管理提醒时间、重复规则和自定义铃声。",
            self.btn_alarm
        ))

        self.current_mode = "fade_black"
        self.btn_mode = QPushButton("▼ 淡出至黑屏再淡入")
        self.btn_mode.clicked.connect(self.onModeClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "切换视效",
            "控制每张照片之间的过渡方式。",
            self.btn_mode
        ))

        self.current_interval = 5
        self.btn_interval = QPushButton("▼ 5 秒极速挂机")
        self.btn_interval.clicked.connect(self.onIntervalClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "播放间隔",
            "预设轮播速度，播放时也可在底部控制条调速。",
            self.btn_interval
        ))

        self.btn_random = QPushButton("▼ 顺序播放")
        self.btn_random.clicked.connect(self.onRandomClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "播放顺序",
            "顺序播放为默认；随机播放会自动避开连续重复。",
            self.btn_random
        ))

        self.btn_low_power = QPushButton("▼ 关闭")
        self.btn_low_power.clicked.connect(self.onLowPowerClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "低性能模式",
            "关闭缩略图预加载，降低动画压力，并减少进度保存频率。",
            self.btn_low_power
        ))

        self.btn_clock = QPushButton("▼ 开启")
        self.btn_clock.clicked.connect(self.onClockClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "时钟显示",
            "控制播放画面左上角日期与时间。",
            self.btn_clock
        ))

        self.btn_calendar = QPushButton("▼ 关闭")
        self.btn_calendar.clicked.connect(self.onCalendarClicked)
        settings_layout.addWidget(self._makeSettingRow(
            "日历",
            "在播放画面右下角显示当月日历。",
            self.btn_calendar
        ))

        calendar_opacity_control = QWidget()
        calendar_opacity_layout = QHBoxLayout(calendar_opacity_control)
        calendar_opacity_layout.setContentsMargins(0, 0, 0, 0)
        calendar_opacity_layout.setSpacing(12)
        self.calendar_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.calendar_opacity_slider.setRange(20, 180)
        self.calendar_opacity_slider.setSingleStep(1)
        self.calendar_opacity_slider.setPageStep(8)
        self.calendar_opacity_slider.setValue(self.current_calendar_opacity)
        self.calendar_opacity_slider.valueChanged.connect(self.onCalendarOpacityChanged)
        self.calendar_opacity_label = QLabel(str(self.current_calendar_opacity))
        self.calendar_opacity_label.setMinimumWidth(36)
        self.calendar_opacity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        calendar_opacity_layout.addWidget(self.calendar_opacity_slider, 1)
        calendar_opacity_layout.addWidget(self.calendar_opacity_label)
        settings_layout.addWidget(self._makeSettingRow(
            "日历底色",
            "调整日历黑色背景的透明度，数值越大底色越明显。",
            calendar_opacity_control
        ))

        settings_layout.addStretch()
        settings_scroll.setWidget(settings_body)
        controls_layout.addWidget(settings_scroll, 1)
        self.updateClockBtn()
        self.updateLowPowerBtn()
        self.updateCalendarBtn()
        self._refreshSummary()

    def _makeSettingRow(self, title, hint, primary_button, secondary_button=None):
        row = QFrame()
        row.setObjectName("SettingRow")
        row.setMinimumHeight(84)

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(20, 16, 20, 16)
        row_layout.setSpacing(18)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("RowTitle")
        hint_label = QLabel(hint)
        hint_label.setObjectName("RowHint")
        hint_label.setWordWrap(True)

        text_layout.addWidget(title_label)
        text_layout.addWidget(hint_label)
        row_layout.addLayout(text_layout, 1)

        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        primary_button.setObjectName("OptionButton")
        primary_button.setMinimumHeight(48)
        primary_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controls_layout.addWidget(primary_button, 1)

        if secondary_button:
            secondary_button.setObjectName("SmallButton")
            secondary_button.setMinimumHeight(48)
            controls_layout.addWidget(secondary_button)

        row_layout.addLayout(controls_layout, 1)
        return row

    def _styleSwitchButton(self, button, enabled):
        button.setObjectName("SwitchOnButton" if enabled else "SwitchOffButton")
        button.style().unpolish(button)
        button.style().polish(button)

    def _compactPath(self, path, limit=48):
        if not path:
            return "未选择"

        normalized = os.path.normpath(path)
        if len(normalized) <= limit:
            return normalized

        drive, tail_path = os.path.splitdrive(normalized)
        parts = [part for part in tail_path.split(os.sep) if part]
        tail = os.path.join(*parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else normalized[-20:])
        prefix = drive + os.sep if drive else ""
        return f"{prefix}...{os.sep}{tail}"

    def _syncFolderButton(self):
        if not hasattr(self, "btn_recent"):
            return

        if self.folder_path:
            self.btn_recent.setText("▼ " + self._compactPath(self.folder_path, 58))
            self.btn_recent.setToolTip(self.folder_path)
        else:
            self.btn_recent.setText("▼ 未选择目录 (点击选择历史)")
            self.btn_recent.setToolTip("尚未选择相册目录")
        self._refreshSummary()

    def _refreshSummary(self):
        if not hasattr(self, "summary_folder"):
            return

        mode_text = {"fade_black": "淡出至黑屏再淡入", "crossfade": "双图直接淡入淡出"}.get(self.current_mode, "淡出至黑屏再淡入")
        interval_text = {5:"5 秒极速挂机", 10:"10 秒快进浏览", 20:"20 秒普通观看", 60:"1 分钟慢慢品味", 1800:"半小时极慢沉浸"}.get(self.current_interval, f"{self.current_interval} 秒")
        enabled_count = sum(1 for alarm in self.current_alarms if alarm.get("enabled", True))
        alarm_text = "无闹钟"
        if enabled_count:
            next_alarm = next_enabled_alarm_datetime(self.current_alarms)
            alarm_text = f"{enabled_count} 个，下一次 {next_alarm.strftime('%H:%M')}" if next_alarm else f"{enabled_count} 个"

        self.summary_folder.setText("相册  " + self._compactPath(self.folder_path, 42))
        self.summary_interval.setText("间隔  " + interval_text)
        self.summary_mode.setText("视效  " + mode_text)
        self.summary_random.setText("顺序  " + ("随机播放" if self.current_random_play else "顺序播放"))
        self.summary_performance.setText("性能  " + ("低性能" if self.current_low_power_mode else "标准"))
        self.summary_clock.setText("时钟  " + ("开启" if self.current_show_clock else "关闭"))
        self.summary_calendar.setText("日历  " + ("开启" if self.current_show_calendar else "关闭"))
        self.summary_alarm.setText("闹钟  " + alarm_text)

    def updateModeBtn(self):
        modes = {"fade_black": "淡出至黑屏再淡入", "crossfade": "双图直接淡入淡出"}
        self.btn_mode.setText("▼ " + modes.get(self.current_mode, "淡出至黑屏再淡入"))
        self._refreshSummary()
        
    def updateIntervalBtn(self):
        ints = {5:"5 秒极速挂机", 10:"10 秒快进浏览", 20:"20 秒普通观看", 60:"1 分钟慢慢品味", 1800:"半小时极慢沉浸"}
        self.btn_interval.setText("▼ " + ints.get(self.current_interval, f"{self.current_interval} 秒"))
        self._refreshSummary()

    def updateClockBtn(self):
        self.btn_clock.setText("已开启" if self.current_show_clock else "已关闭")
        self._styleSwitchButton(self.btn_clock, self.current_show_clock)
        self._refreshSummary()

    def updateRandomBtn(self):
        self.btn_random.setText("▼ 随机播放" if self.current_random_play else "▼ 顺序播放")
        self._refreshSummary()

    def updateLowPowerBtn(self):
        self.btn_low_power.setText("已开启" if self.current_low_power_mode else "已关闭")
        self._styleSwitchButton(self.btn_low_power, self.current_low_power_mode)
        self._refreshSummary()

    def updateCalendarBtn(self):
        self.btn_calendar.setText("已开启" if self.current_show_calendar else "已关闭")
        self._styleSwitchButton(self.btn_calendar, self.current_show_calendar)
        self._refreshSummary()

    def _normalizeCalendarOpacity(self, value):
        try:
            value = int(value)
        except Exception:
            value = 62
        return max(20, min(180, value))

    def updateCalendarOpacityControl(self):
        self.current_calendar_opacity = self._normalizeCalendarOpacity(self.current_calendar_opacity)
        if hasattr(self, "calendar_opacity_label"):
            self.calendar_opacity_label.setText(str(self.current_calendar_opacity))
        if hasattr(self, "calendar_opacity_slider") and self.calendar_opacity_slider.value() != self.current_calendar_opacity:
            self.calendar_opacity_slider.blockSignals(True)
            self.calendar_opacity_slider.setValue(self.current_calendar_opacity)
            self.calendar_opacity_slider.blockSignals(False)

    def updateAlarmBtn(self):
        enabled_count = sum(1 for alarm in self.current_alarms if alarm.get("enabled", True))
        if enabled_count == 0:
            self.btn_alarm.setText("▼ 无闹钟")
            self._refreshSummary()
            return

        next_alarm = next_enabled_alarm_datetime(self.current_alarms)
        if next_alarm:
            self.btn_alarm.setText(f"▼ {enabled_count} 个｜下一个 {next_alarm.strftime('%H:%M')}")
        else:
            self.btn_alarm.setText(f"▼ {enabled_count} 个闹钟")
        self._refreshSummary()

    def onRecentClicked(self):
        if not self.recent_paths: return
        opts = {p: p for p in self.recent_paths}
        dlg = TouchMenuDialog(self, "选择历史相册路径", opts, delete_callback=self.removeRecentPath)
        if dlg.exec():
            self.folder_path = dlg.selected_data
            self._syncFolderButton()
            self.error_label.setText("")

    def onModeClicked(self):
        opts = {"淡出至黑屏再淡入": "fade_black", "原生双图直接淡入淡出": "crossfade"}
        dlg = TouchMenuDialog(self, "选择视效切换模式", opts)
        if dlg.exec():
            self.current_mode = dlg.selected_data
            self.updateModeBtn()

    def onIntervalClicked(self):
        opts = {"5 秒极速挂机": 5, "10 秒快进浏览": 10, "20 秒普通观看": 20, "1 分钟慢慢品味": 60, "半小时极慢沉浸": 1800}
        dlg = TouchMenuDialog(self, "选择系统换图间隔", opts)
        if dlg.exec():
            self.current_interval = dlg.selected_data
            self.updateIntervalBtn()

    def onClockClicked(self):
        opts = {"开启时钟": True, "关闭时钟": False}
        dlg = TouchMenuDialog(self, "时钟显示开关", opts)
        if dlg.exec():
            self.current_show_clock = dlg.selected_data
            self.updateClockBtn()
            self.saveConfig()
            if hasattr(self.main_app, "slideshow_page"):
                self.main_app.slideshow_page.setClockVisible(self.current_show_clock)

    def onRandomClicked(self):
        self.current_random_play = not self.current_random_play
        self.updateRandomBtn()
        self.saveConfig()
        if hasattr(self.main_app, "slideshow_page"):
            self.main_app.slideshow_page.setRandomPlay(self.current_random_play)

    def onLowPowerClicked(self):
        self.current_low_power_mode = not self.current_low_power_mode
        self.updateLowPowerBtn()
        self.saveConfig()
        if hasattr(self.main_app, "slideshow_page"):
            self.main_app.slideshow_page.setLowPowerMode(self.current_low_power_mode)

    def onCalendarClicked(self):
        self.current_show_calendar = not self.current_show_calendar
        self.updateCalendarBtn()
        self.saveConfig()
        if hasattr(self.main_app, "slideshow_page"):
            self.main_app.slideshow_page.setCalendarVisible(self.current_show_calendar)

    def onCalendarOpacityChanged(self, value):
        self.current_calendar_opacity = self._normalizeCalendarOpacity(value)
        self.updateCalendarOpacityControl()
        self.saveConfigSoon(350)
        if hasattr(self.main_app, "slideshow_page"):
            self.main_app.slideshow_page.setCalendarBackgroundOpacity(self.current_calendar_opacity)

    def onAlarmClicked(self):
        dlg = AlarmSettingsDialog(self, self.current_alarms)
        if dlg.exec():
            self.current_alarms = dlg.alarmsResult()
            self.updateAlarmBtn()
            self.saveConfig()
            if hasattr(self.main_app, "refreshAlarmStatus"):
                self.main_app.refreshAlarmStatus()

    def addRecentPath(self, path):
        if not path: return
        if path in self.recent_paths:
            self.recent_paths.remove(path)
        self.recent_paths.insert(0, path)
        self.recent_paths = self.recent_paths[:10]
        self._syncFolderButton()

    def removeRecentPath(self, path):
        if path in self.recent_paths:
            self.recent_paths.remove(path)
            self.saveConfig()
            if self.folder_path == path:
                self.folder_path = ""
                self._syncFolderButton()

    def loadConfig(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    self.recent_paths = config.get("recent_paths", [])
                    folder_path = config.get("folder_path", "")
                    if folder_path:
                        self.folder_path = folder_path
                        if folder_path not in self.recent_paths:
                            self.addRecentPath(folder_path)
                        else:
                            self._syncFolderButton()
                            
                    self.current_interval = config.get("interval", 5)
                    self.updateIntervalBtn()
                    
                    self.current_mode = config.get("transition_mode", "fade_black")
                    self.updateModeBtn()

                    self.current_show_clock = config.get("show_clock", True)
                    self.updateClockBtn()

                    self.current_random_play = config.get("random_play", False)
                    self.updateRandomBtn()

                    self.current_low_power_mode = config.get("low_power_mode", False)
                    self.updateLowPowerBtn()

                    self.current_show_calendar = config.get("show_calendar", False)
                    self.updateCalendarBtn()

                    self.current_calendar_opacity = self._normalizeCalendarOpacity(config.get("calendar_background_opacity", 62))
                    self.updateCalendarOpacityControl()

                    self.current_alarms = normalize_alarm_list(config.get("alarms", []))
                    self.updateAlarmBtn()
                        
                    self.folder_progress = config.get("folder_progress", {})
                    self.folder_fixed_states = config.get("folder_fixed_states", {})
            except Exception as e:
                print("加载配置文件失败:", e)
        self._syncFolderButton()

    def saveConfig(self):
        if hasattr(self, "_config_save_timer") and self._config_save_timer.isActive():
            self._config_save_timer.stop()

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "folder_path": self.folder_path,
                    "recent_paths": self.recent_paths,
                    "interval": self.current_interval,
                    "transition_mode": self.current_mode,
                    "show_clock": self.current_show_clock,
                    "random_play": self.current_random_play,
                    "low_power_mode": self.current_low_power_mode,
                    "show_calendar": self.current_show_calendar,
                    "calendar_background_opacity": self.current_calendar_opacity,
                    "alarms": self.current_alarms,
                    "folder_progress": self.folder_progress,
                    "folder_fixed_states": self.folder_fixed_states
                }, f)
        except Exception as e:
            print("保存配置失败:", e)

    def saveConfigSoon(self, delay_ms=None):
        if delay_ms is None:
            delay_ms = LOW_POWER_CONFIG_SAVE_DEBOUNCE_MS if self.current_low_power_mode else CONFIG_SAVE_DEBOUNCE_MS
        if hasattr(self, "_config_save_timer"):
            self._config_save_timer.start(delay_ms)
        else:
            self.saveConfig()

    def flushPendingConfigSave(self):
        if hasattr(self, "_config_save_timer") and self._config_save_timer.isActive():
            self._config_save_timer.stop()
            self.saveConfig()

    def chooseDirectory(self):
        folder_path = QFileDialog.getExistingDirectory(self, "请选择相册文件夹")
        if folder_path:
            self.folder_path = folder_path
            self.addRecentPath(folder_path)
            self.clearStatus()

    def clearStatus(self):
        self.error_label.setText("")

    def start(self):
        self.clearStatus()
        
        if not self.folder_path or not os.path.isdir(self.folder_path):
            self.error_label.setText("错误: 所选路径无效，请选择历史相册或重新浏览文件夹")
            return
            
        valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
        image_paths = []
        scanned_files = 0
        last_scan_update = 0
        self.btn_start.setEnabled(False)
        self.error_label.setText("正在扫描图片...")
        QApplication.processEvents()

        try:
            for root, dirs, files in os.walk(self.folder_path):
                for file in files:
                    scanned_files += 1
                    if os.path.splitext(file)[1].lower() in valid_exts:
                        image_paths.append(os.path.join(root, file))

                    if scanned_files - last_scan_update >= 300:
                        last_scan_update = scanned_files
                        self.error_label.setText(f"正在扫描图片... 已找到 {len(image_paths)} 张")
                        QApplication.processEvents()
        finally:
            self.btn_start.setEnabled(True)
                    
        if not image_paths:
            self.error_label.setText("错误: 文件夹里未找到任何支持的图片格式")
            return
            
        self.error_label.setText(f"扫描完成，共 {len(image_paths)} 张，正在进入播放...")
        QApplication.processEvents()
        self.saveConfig()
        self.clearStatus()
        self.main_app.start_slideshow(image_paths, self.current_interval * 1000, self.current_mode, self.folder_path)


class SlideshowPage(QWidget):
    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self.image_paths = []
        self.current_index = -1
        self.transition_mode = "fade_black"
        self.fade_state = ""
        self.pending_path = ""
        self.current_movie = None 
        self.thumb_loader = None
        self._gc_counter = 0
        self.low_power_mode = False
        self.random_play = False
        self.random_history = []
        self.is_fixed = False
        self.calendar_visible = False
        
        self.initUI()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus) 
        
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._onPlayTimerTimeout)
        
        self.mouse_idle_timer = QTimer(self)
        self.mouse_idle_timer.timeout.connect(self.hideOverlayAndCursor)
        
        self.setMouseTracking(True)
        self.grabGesture(Qt.GestureType.SwipeGesture)

    def initUI(self):
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.setRowStretch(0, 1)
        self.layout.setColumnStretch(0, 1)
        
        self.label_bottom = QLabel()
        self.label_bottom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_bottom.setMouseTracking(True)
        self.label_bottom.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label_bottom.setMinimumSize(0, 0)
        
        self.label_top = QLabel()
        self.label_top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_top.setMouseTracking(True)
        self.label_top.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label_top.setMinimumSize(0, 0)
        
        self.layout.addWidget(self.label_bottom, 0, 0)
        self.layout.addWidget(self.label_top, 0, 0)
        
        self.effect = QGraphicsOpacityEffect(self.label_top)
        self.label_top.setGraphicsEffect(self.effect)
        
        self.anim = QPropertyAnimation(self.effect, b"opacity")
        self.anim.finished.connect(self.onAnimationFinished)
        
        self.setupClock()
        self.setupAlarmBadge()
        self.setupCalendar()
        self.setupOverlay()
        self.setupSidebar()

    def setupClock(self):
        self.clock_widget = QWidget()
        self.clock_visible = True
        self.clock_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.clock_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.clock_widget.setStyleSheet("background: transparent; border: none;")
        
        clock_layout = QVBoxLayout(self.clock_widget)
        clock_layout.setContentsMargins(15, 10, 15, 10)
        clock_layout.setSpacing(2)
        
        self.time_label = QLabel()
        self.time_label.setStyleSheet("font-size: 72px; font-weight: bold; color: rgba(255, 255, 255, 245); background: transparent; letter-spacing: 2px;")
        
        self.date_label = QLabel()
        self.date_label.setStyleSheet("font-size: 22px; color: rgba(255, 255, 255, 210); font-weight: bold; background: transparent; padding-left: 5px;")
        
        self._addTextShadow(self.time_label, blur=4, offset=1)
        self._addTextShadow(self.date_label, blur=3, offset=1)
        
        clock_layout.addWidget(self.time_label)
        clock_layout.addWidget(self.date_label)
        
        v_box = QVBoxLayout()
        v_box.addWidget(self.clock_widget, 0, Qt.AlignmentFlag.AlignLeft)
        v_box.addStretch()
        v_box.setContentsMargins(25, 25, 0, 0)
        
        self.layout.addLayout(v_box, 0, 0)
        
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.updateClock)
        self.clock_timer.start(1000)
        self.updateClock()

    def _addTextShadow(self, label, blur=4, offset=1):
        shadow = QGraphicsDropShadowEffect(label)
        shadow.setBlurRadius(blur)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(offset, offset)
        label.setGraphicsEffect(shadow)

    def updateClock(self):
        now = datetime.datetime.now()
        self.time_label.setText(now.strftime("%H:%M"))
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        date_str = f"{now.year}年{now.month}月{now.day}日 {weekdays[now.weekday()]}"
        self.date_label.setText(date_str)
        if hasattr(self, "calendar_widget") and self.calendar_widget.isVisible():
            self.calendar_widget.update()

    def setupAlarmBadge(self):
        self.alarm_badge = AlarmBadge()
        self.alarm_badge.hide()

        v_box = QVBoxLayout()
        v_box.addStretch()
        h_box = QHBoxLayout()
        h_box.addStretch()
        h_box.addWidget(self.alarm_badge, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        h_box.setContentsMargins(0, 0, 18, 16)
        v_box.addLayout(h_box)
        self.layout.addLayout(v_box, 0, 0)

    def setAlarmBadgeText(self, text):
        if text:
            self.alarm_badge.setTimeText(text)
            self.alarm_badge.show()
        else:
            self.alarm_badge.setTimeText("")
            self.alarm_badge.hide()

    def setupCalendar(self):
        self.calendar_widget = CalendarBadge(self)
        if hasattr(self.main_app, "settings_page"):
            self.calendar_widget.setBackgroundOpacity(self.main_app.settings_page.current_calendar_opacity)
        self.calendar_widget.hide()
        self._positionCalendar()

    def setCalendarBackgroundOpacity(self, opacity):
        if hasattr(self, "calendar_widget"):
            self.calendar_widget.setBackgroundOpacity(opacity)

    def _positionCalendar(self):
        if not hasattr(self, "calendar_widget"):
            return

        calendar_size = self.calendar_widget.sizeHint()
        bottom_margin = 70
        if hasattr(self, "overlay_widget") and not self.overlay_widget.isHidden():
            bottom_margin = max(bottom_margin, self.overlay_widget.height() + 65)

        x = max(0, self.width() - calendar_size.width() - 18)
        y = max(0, self.height() - calendar_size.height() - bottom_margin)
        self.calendar_widget.setGeometry(x, y, calendar_size.width(), calendar_size.height())
        self.calendar_widget.raise_()

    def setCalendarVisible(self, visible, persist=False):
        self.calendar_visible = visible
        self._positionCalendar()
        self.calendar_widget.setVisible(visible)
        self.updateCalendarOverlayBtn()
        if visible:
            self.calendar_widget.update()

        if hasattr(self.main_app, "settings_page"):
            self.main_app.settings_page.current_show_calendar = visible
            self.main_app.settings_page.updateCalendarBtn()
            if persist:
                self.main_app.settings_page.saveConfig()

    def toggleCalendar(self):
        self.setCalendarVisible(not self.calendar_visible, persist=True)
        self.showOverlayAndCursor()

    def setupSidebar(self):
        # 缩略图侧边列表
        self.thumb_list = QListWidget(self)
        self.thumb_list.setFixedWidth(240)
        self.thumb_list.setIconSize(QSize(180, 140))
        self.thumb_list.setSpacing(12)
        self.thumb_list.setStyleSheet("""
            QListWidget {
                background-color: rgba(20, 20, 20, 200);
                border-left: 1px solid rgba(255, 255, 255, 30);
                outline: 0;
                padding: 10px;
            }
            QListWidget::item {
                border: 2px solid transparent;
                border-radius: 8px;
                color: white;
            }
            QListWidget::item:selected {
                border: 2px solid #0078D7;
                background-color: rgba(255, 255, 255, 50);
            }
            QListWidget::item:hover {
                background-color: rgba(255, 255, 255, 20);
            }
        """)
        self.thumb_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumb_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumb_list.setMouseTracking(True)
        
        self.sidebar_anim = QPropertyAnimation(self.thumb_list, b"geometry")
        self.sidebar_anim.setDuration(400)
        self.sidebar_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self.is_sidebar_visible = False
        
        self.thumb_list.itemClicked.connect(self.onThumbnailClicked)
        
        self.sidebar_idle_timer = QTimer(self)
        self.sidebar_idle_timer.timeout.connect(self.hideSidebar)

    def setupOverlay(self):
        self.overlay_widget = QWidget()
        self.overlay_widget.setMouseTracking(True)
        self.overlay_widget.setStyleSheet("""
            QWidget {
                background-color: rgba(30, 30, 30, 200); 
                border-radius: 12px;
            }
            QPushButton {
                font-size: 16px; 
                padding: 10px 16px; 
                color: white; 
                background-color: transparent; 
                border: 1px solid #777; 
                border-radius: 6px;
                min-width: 118px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 40);
            }
        """)
        
        self.overlay_layout = QGridLayout(self.overlay_widget)
        self.overlay_layout.setContentsMargins(16, 10, 16, 10)
        self.overlay_layout.setHorizontalSpacing(10)
        self.overlay_layout.setVerticalSpacing(8)
        
        self.btn_prev = QPushButton("⏮ 上一张")
        self.btn_pause = QPushButton("⏸ 固定画框")
        self.btn_next = QPushButton("⏭ 下一张")
        self.btn_reset = QPushButton("⏪ 从头播放")
        
        self.btn_overlay_interval = QPushButton("▼ 调速")
        self.btn_overlay_interval.clicked.connect(self.onOverlayIntervalClicked)
        self.btn_random_overlay = QPushButton("顺序播放")
        self.btn_low_power_overlay = QPushButton("低性能关")
        self.btn_clock_overlay = QPushButton("🕒 时钟")
        self.btn_calendar_overlay = QPushButton("日历关")
        self.btn_alarm_overlay = QPushButton("闹钟")
        
        self.btn_fullscreen_overlay = QPushButton("🔲 全屏切换")
        self.btn_calendar_opacity_overlay = QPushButton("日历透明度")
        self.btn_settings = QPushButton("⚙ 返回设置")
        self.btn_quit = QPushButton("❌ 退出应用")
        
        self.overlay_buttons = [
            self.btn_prev, self.btn_calendar_opacity_overlay, self.btn_next, self.btn_reset,
            self.btn_overlay_interval, self.btn_random_overlay, self.btn_low_power_overlay,
            self.btn_clock_overlay, self.btn_calendar_overlay, self.btn_alarm_overlay,
            self.btn_fullscreen_overlay, self.btn_pause, self.btn_settings, self.btn_quit
        ]
        for btn in self.overlay_buttons:
            btn.setMinimumHeight(46)
            btn.setMinimumWidth(118)
            btn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        self.btn_prev.clicked.connect(self.manualPrev)
        self.btn_next.clicked.connect(self.manualNext)
        self.btn_pause.clicked.connect(self.togglePause)
        self.btn_reset.clicked.connect(self.playFromBeginning)
        self.btn_random_overlay.clicked.connect(self.toggleRandomPlay)
        self.btn_low_power_overlay.clicked.connect(self.toggleLowPowerMode)
        self.btn_clock_overlay.clicked.connect(self.toggleClock)
        self.btn_calendar_overlay.clicked.connect(self.toggleCalendar)
        self.btn_alarm_overlay.clicked.connect(self.main_app.openAlarmSettings)
        self.btn_fullscreen_overlay.clicked.connect(self.main_app.toggleFullscreen)
        self.btn_calendar_opacity_overlay.clicked.connect(self.onOverlayCalendarOpacityClicked)
        self.btn_settings.clicked.connect(self.main_app.stop_slideshow)
        self.btn_quit.clicked.connect(sys.exit)
        self.updateRandomOverlayBtn()
        self.updateLowPowerOverlayBtn()
        self.updateClockOverlayBtn()
        self.updateCalendarOverlayBtn()
        self._arrangeOverlayButtons(force=True)
        
        v_box = QVBoxLayout()
        v_box.addStretch()
        v_box.addWidget(self.overlay_widget, 0, Qt.AlignmentFlag.AlignHCenter)
        v_box.setContentsMargins(0, 0, 0, 45)
        
        self.layout.addLayout(v_box, 0, 0)

    def onOverlayIntervalClicked(self):
        opts = {"5 秒挂机": 5, "10 秒快进": 10, "20 秒普通": 20, "1 分钟沉浸": 60, "半小时极慢": 1800}
        dlg = TouchMenuDialog(self, "用手指触控调整速度", opts)
        if dlg.exec():
            new_interval_s = dlg.selected_data
            self.btn_overlay_interval.setText(f"▼ {dlg.selected_text}")
            self.play_timer.setInterval(new_interval_s * 1000)
            
            self.main_app.settings_page.current_interval = new_interval_s
            self.main_app.settings_page.updateIntervalBtn()
            self.main_app.settings_page.saveConfig()
            
            self.mouse_idle_timer.start(3000)

    def onOverlayCalendarOpacityClicked(self):
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setStyleSheet("""
            QFrame {
                background-color: rgba(30, 30, 30, 220);
                border: 1px solid rgba(255, 255, 255, 45);
                border-radius: 10px;
            }
            QLabel {
                color: #fff8ec;
                font-size: 18px;
                font-weight: 700;
                background: transparent;
                border: none;
            }
            QSlider::groove:horizontal {
                height: 7px;
                background: rgba(255, 255, 255, 55);
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #d9b66d;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 24px;
                height: 24px;
                margin: -9px 0;
                background: #fff8ec;
                border: 2px solid #d9b66d;
                border-radius: 12px;
            }
            QPushButton {
                color: #1a1308;
                background-color: #d9b66d;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 800;
                min-height: 42px;
            }
        """)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 20, 24, 20)
        panel_layout.setSpacing(14)

        value_label = QLabel()
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(20, 180)
        slider.setValue(self.main_app.settings_page.current_calendar_opacity)
        slider.setMinimumWidth(360)

        def update_value(value):
            value = self.main_app.settings_page._normalizeCalendarOpacity(value)
            value_label.setText(f"日历透明度  {value}")
            self.main_app.settings_page.current_calendar_opacity = value
            self.main_app.settings_page.updateCalendarOpacityControl()
            self.setCalendarBackgroundOpacity(value)
            self.main_app.settings_page.saveConfigSoon(350)

        slider.valueChanged.connect(update_value)
        update_value(slider.value())
        panel_layout.addWidget(value_label)
        panel_layout.addWidget(slider)
        done_button = QPushButton("完成")
        done_button.clicked.connect(dialog.accept)
        panel_layout.addWidget(done_button)
        layout.addWidget(panel)

        dialog.adjustSize()
        parent_rect = self.window().geometry()
        x = parent_rect.x() + (parent_rect.width() - dialog.width()) // 2
        y = parent_rect.y() + parent_rect.height() - dialog.height() - 170
        dialog.move(max(parent_rect.x(), x), max(parent_rect.y(), y))
        dialog.exec()

        if not self.overlay_widget.isHidden():
            self.mouse_idle_timer.start(3000)

    def updateClockOverlayBtn(self):
        if hasattr(self, "btn_clock_overlay"):
            self.btn_clock_overlay.setText("🕒 时钟开" if self.clock_visible else "🕒 时钟关")

    def updateCalendarOverlayBtn(self):
        if hasattr(self, "btn_calendar_overlay"):
            self.btn_calendar_overlay.setText("日历开" if self.calendar_visible else "日历关")

    def updateRandomOverlayBtn(self):
        if hasattr(self, "btn_random_overlay"):
            self.btn_random_overlay.setText("随机播放" if self.random_play else "顺序播放")

    def updateLowPowerOverlayBtn(self):
        if hasattr(self, "btn_low_power_overlay"):
            self.btn_low_power_overlay.setText("低性能开" if self.low_power_mode else "低性能关")

    def setClockVisible(self, visible, persist=False):
        self.clock_visible = visible
        self.clock_widget.setVisible(visible)
        self.updateClockOverlayBtn()

        if hasattr(self.main_app, "settings_page"):
            self.main_app.settings_page.current_show_clock = visible
            self.main_app.settings_page.updateClockBtn()
            if persist:
                self.main_app.settings_page.saveConfig()

    def toggleClock(self):
        self.setClockVisible(not self.clock_visible, persist=True)
        self.showOverlayAndCursor()

    def setRandomPlay(self, enabled, persist=False):
        self.random_play = bool(enabled)
        self.random_history = []
        self.updateRandomOverlayBtn()

        if hasattr(self.main_app, "settings_page"):
            self.main_app.settings_page.current_random_play = self.random_play
            self.main_app.settings_page.updateRandomBtn()
            if persist:
                self.main_app.settings_page.saveConfig()

    def toggleRandomPlay(self):
        self.setRandomPlay(not self.random_play, persist=True)
        self.showOverlayAndCursor()

    def setLowPowerMode(self, enabled, persist=False):
        self.low_power_mode = bool(enabled)
        if hasattr(self, "sidebar_anim"):
            self.sidebar_anim.setDuration(120 if self.low_power_mode else 400)
        self.updateLowPowerOverlayBtn()

        if self.low_power_mode:
            self._stopThumbnailLoader()
            if self.image_paths:
                self.thumb_list.setUpdatesEnabled(False)
                for index, path in enumerate(self.image_paths):
                    item = self.thumb_list.item(index)
                    if item and not item.text():
                        item.setText(os.path.basename(path))
                self.thumb_list.setUpdatesEnabled(True)
        elif self.image_paths and self.thumb_loader is None:
            self._startThumbnailLoader()

        if hasattr(self.main_app, "settings_page"):
            self.main_app.settings_page.current_low_power_mode = self.low_power_mode
            self.main_app.settings_page.updateLowPowerBtn()
            if persist:
                self.main_app.settings_page.saveConfig()

    def toggleLowPowerMode(self):
        self.setLowPowerMode(not self.low_power_mode, persist=True)
        self.showOverlayAndCursor()

    def _arrangeOverlayButtons(self, force=False):
        if not hasattr(self, "overlay_buttons"):
            return

        width = self.width()
        if width <= 0:
            width = current_screen_geometry(self, available=True).width()

        if width < 760:
            columns = 2
        elif width < 1800:
            columns = 5
        else:
            columns = len(self.overlay_buttons)

        button_count = len(self.overlay_buttons)
        if not force and getattr(self, "_overlay_columns", None) == columns and getattr(self, "_overlay_button_count", None) == button_count:
            return

        while self.overlay_layout.count():
            self.overlay_layout.takeAt(0)

        self._overlay_columns = columns
        self._overlay_button_count = button_count
        last_count = button_count % columns
        fill_last_row = 1 < last_count < columns
        virtual_columns = columns * last_count if fill_last_row else columns
        for col in range(max(button_count, virtual_columns)):
            self.overlay_layout.setColumnStretch(col, 1 if col < virtual_columns else 0)
        for index, button in enumerate(self.overlay_buttons):
            row = index // columns
            col = index % columns
            if fill_last_row and row == button_count // columns:
                span = virtual_columns // last_count
                self.overlay_layout.addWidget(button, row, col * span, 1, span)
            else:
                span = virtual_columns // columns
                self.overlay_layout.addWidget(button, row, col * span, 1, span)

        self.overlay_widget.setMaximumWidth(max(680, width - 120))
        self.overlay_widget.adjustSize()

    def playFromBeginning(self):
        self.pending_next_image = False
        self.random_history = []
        self.current_index = -1
        self.nextImage()
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start()

    def _stopThumbnailLoader(self):
        if self.thumb_loader:
            self.thumb_loader.running = False
            self.thumb_loader.wait()
            self.thumb_loader = None

    def _startThumbnailLoader(self):
        if self.low_power_mode or not self.image_paths:
            return

        self._stopThumbnailLoader()
        self.thumb_loader = ThumbnailLoader(self.image_paths, NORMAL_THUMBNAIL_DELAY_MS)
        self.thumb_loader.thumbnail_ready.connect(self.onThumbnailReady)
        self.thumb_loader.start()

    def start(self, image_paths, interval_ms, mode, folder_path):
        self._stopThumbnailLoader()

        self.image_paths = image_paths
        self.transition_mode = mode
        self.folder_path = folder_path
        self.low_power_mode = self.main_app.settings_page.current_low_power_mode
        self.random_play = self.main_app.settings_page.current_random_play
        self.random_history = []
        if hasattr(self, "sidebar_anim"):
            self.sidebar_anim.setDuration(120 if self.low_power_mode else 400)
        self.updateLowPowerOverlayBtn()
        self.updateRandomOverlayBtn()
        
        # 加载历史进度记忆，如果没记忆默认从第一张也就是 0 之前 (-1) 开始
        saved_index = self.main_app.settings_page.folder_progress.get(self.folder_path, 0)
        try:
            saved_index = int(saved_index)
        except Exception:
            saved_index = 0
        saved_index = max(0, min(saved_index, len(self.image_paths) - 1))
        self.is_fixed = bool(self.main_app.settings_page.folder_fixed_states.get(self.folder_path, False))
        self.current_index = saved_index if self.is_fixed else saved_index - 1
        
        self.fade_state = ""
        self.btn_pause.setText("⏸ 固定画框")
        self._syncPauseButton()
        self.setClockVisible(self.main_app.settings_page.current_show_clock)
        self.setCalendarVisible(self.main_app.settings_page.current_show_calendar)
        
        intervals_map = {5: "5 秒挂机", 10: "10 秒快进", 20: "20 秒普通", 60: "1 分钟沉浸", 1800: "半小时极慢"}
        sec = int(interval_ms / 1000)
        self.btn_overlay_interval.setText("▼ " + intervals_map.get(sec, f"{sec} 秒"))
            
        self.play_timer.setInterval(interval_ms)
        
        # 强制复位侧边栏初始位置
        self.is_sidebar_visible = False
        self.thumb_list.setGeometry(self._sidebarRect(False))
        
        # 初始化列表占位与后台加载器
        self.thumb_list.setUpdatesEnabled(False)
        self.thumb_list.clear()
        for i in range(len(self.image_paths)):
            item = QListWidgetItem()
            item.setSizeHint(QSize(210, 160))
            if self.low_power_mode:
                item.setText(os.path.basename(self.image_paths[i]))
            self.thumb_list.addItem(item)
        self.thumb_list.setUpdatesEnabled(True)
        self._startThumbnailLoader()
        
        self.showOverlayAndCursor()
        
        QTimer.singleShot(50, self._loadInitialImage)
        if not self.is_fixed:
            self.play_timer.start()
        
        self.setFocus()

    def onThumbnailReady(self, index, image):
        if self.low_power_mode:
            return

        pixmap = QPixmap.fromImage(image)
        item = self.thumb_list.item(index)
        if item:
            item.setIcon(QIcon(pixmap))

    def onThumbnailClicked(self, item):
        index = self.thumb_list.row(item)
        if index >= 0 and index < len(self.image_paths):
            self.pending_next_image = False
            self.current_index = index
            self.loadImage(self.image_paths[self.current_index])
            self.sidebar_idle_timer.start(3000)
            self.showOverlayAndCursor()
            if self.btn_pause.text() == "⏸ 固定画框":
                self.play_timer.start()

    def showSidebar(self):
        if not self.is_sidebar_visible:
            self.is_sidebar_visible = True
            self.sidebar_anim.stop()
            self.sidebar_anim.setStartValue(self.thumb_list.geometry())
            self.sidebar_anim.setEndValue(self._sidebarRect(True))
            self.sidebar_anim.start()
        self.sidebar_idle_timer.start(3000)

    def hideSidebar(self):
        if self.is_sidebar_visible:
            self.is_sidebar_visible = False
            self.sidebar_anim.stop()
            self.sidebar_anim.setStartValue(self.thumb_list.geometry())
            self.sidebar_anim.setEndValue(self._sidebarRect(False))
            self.sidebar_anim.start()

    def _sidebarRect(self, visible):
        sidebar_w = min(240, max(0, self.width()))
        sidebar_h = max(0, self.height())
        sidebar_x = max(0, self.width() - sidebar_w) if visible else self.width()
        return QRect(sidebar_x, 0, sidebar_w, sidebar_h)

    def _syncPauseButton(self):
        self.btn_pause.setText("▶ 解除固定(播放)" if self.is_fixed else "⏸ 固定画框")

    def _saveFixedState(self):
        if not self.folder_path:
            return
        self.main_app.settings_page.folder_fixed_states[self.folder_path] = self.is_fixed
        self.main_app.settings_page.saveConfig()

    def _loadInitialImage(self):
        if not self.image_paths:
            return
        if self.is_fixed:
            self.loadImage(self.image_paths[self.current_index])
        else:
            self.nextImage()

    def togglePause(self):
        self.is_fixed = not self.is_fixed
        if self.is_fixed:
            self.play_timer.stop()
            self.pending_next_image = False
        else:
            self.play_timer.start()
        self._syncPauseButton()
        self._saveFixedState()
        return
        is_playing = self.play_timer.isActive() or getattr(self, 'pending_next_image', False)
        if is_playing:
            self.play_timer.stop()
            self.pending_next_image = False 
            self.btn_pause.setText("▶ 解除固定(播放)")
        else:
            self.play_timer.start()
            self.btn_pause.setText("⏸ 固定画框")

    def manualPrev(self):
        self.prevImage()
        if self.is_fixed:
            return
        self.play_timer.start()
        return
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start() 

    def manualNext(self):
        self.nextImage()
        if self.is_fixed:
            return
        self.play_timer.start()
        return
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start() 

    def nextImage(self):
        if not self.image_paths: return
        self.pending_next_image = False
        if self.random_play and len(self.image_paths) > 1:
            if 0 <= self.current_index < len(self.image_paths):
                self.random_history.append(self.current_index)
                self.random_history = self.random_history[-120:]
            next_index = random.randrange(len(self.image_paths))
            if next_index == self.current_index:
                next_index = (next_index + 1) % len(self.image_paths)
            self.current_index = next_index
        else:
            self.current_index = (self.current_index + 1) % len(self.image_paths)
        self.loadImage(self.image_paths[self.current_index])

    def prevImage(self):
        if not self.image_paths: return
        self.pending_next_image = False
        if self.random_play and self.random_history:
            self.current_index = self.random_history.pop()
        else:
            self.current_index = (self.current_index - 1) % len(self.image_paths)
        self.loadImage(self.image_paths[self.current_index])

    def _onPlayTimerTimeout(self):
        if self.current_movie and not getattr(self, 'movie_finished_one_loop', True):
            self.pending_next_image = True
            self.play_timer.stop()
        else:
            self.nextImage()

    def _on_frame_changed(self, frameNumber):
        if getattr(self, 'last_frame_number', -1) >= 0:
            if frameNumber < self.last_frame_number:
                self.movie_finished_one_loop = True
                if getattr(self, 'pending_next_image', False):
                    self.pending_next_image = False
                    self.nextImage()
                    if self.btn_pause.text() == "⏸ 固定画框":
                        self.play_timer.start()
                    return 
                    
        if self.current_movie:
            self.movie_last_pixmap = self.current_movie.currentPixmap()
            
        self.last_frame_number = frameNumber

    def loadImage(self, path):
        # 落盘记忆防丢失
        if hasattr(self, 'folder_path') and self.folder_path:
            if self.main_app.settings_page.folder_progress.get(self.folder_path) != self.current_index:
                self.main_app.settings_page.folder_progress[self.folder_path] = self.current_index
                self.main_app.settings_page.saveConfigSoon()
            
        # 实时同步菜单上的选项高亮
        item = self.thumb_list.item(self.current_index)
        if item:
            self.thumb_list.blockSignals(True)
            item.setSelected(True)
            self.thumb_list.scrollToItem(item, QListView.ScrollHint.PositionAtCenter)
            self.thumb_list.blockSignals(False)

        old_pixmap = None
        if self.current_movie:
            if getattr(self, 'movie_last_pixmap', None) and not self.movie_last_pixmap.isNull():
                old_pixmap = self.movie_last_pixmap.copy()
            else:
                current_frame = self.current_movie.currentPixmap()
                if not current_frame.isNull():
                    old_pixmap = current_frame.copy()
                    
            self.current_movie.stop()
            self.current_movie = None
            self.movie_last_pixmap = None
        else:
            current_frame = self.label_top.pixmap()
            if current_frame and not current_frame.isNull():
                old_pixmap = current_frame.copy()
                
        if not old_pixmap or old_pixmap.isNull():
            self.fade_state = "fading_in"
            self._load_and_fade_in(path, duration=self._animationDuration(1200))
            return

        if self.transition_mode == "fade_black":
            self.pending_path = path
            self.fade_state = "fading_out"
            
            self.label_top.clear()
            self.label_top.setPixmap(old_pixmap)
            self._maybeCollectGarbage()
            
            self.effect.setOpacity(1.0)
            self.anim.stop()
            self.anim.setStartValue(1.0)
            self.anim.setEndValue(0.0)
            self.anim.setDuration(self._animationDuration(1000))
            self.anim.start()
        else:
            self.fade_state = "crossfade_in"
            self.label_bottom.setPixmap(old_pixmap)
            self.label_top.clear()
            self._maybeCollectGarbage()
            
            self._load_and_fade_in(path, duration=self._animationDuration(1800))

    def _load_and_fade_in(self, path, duration=600):
        try:
            self.current_movie = None
            is_gif = path.lower().endswith('.gif')
            target_size = self._imageTargetSize()
            
            if is_gif:
                self.movie_finished_one_loop = False
                self.pending_next_image = False
                self.last_frame_number = -1
                
                self.current_movie = QMovie(path)
                self.current_movie.frameChanged.connect(self._on_frame_changed)
                self.current_movie.jumpToFrame(0)
                orig_size = self.current_movie.currentImage().size()
                if orig_size.isValid():
                    orig_size.scale(target_size, Qt.AspectRatioMode.KeepAspectRatio)
                    self.current_movie.setScaledSize(orig_size)
                    
                self.label_top.setMovie(self.current_movie)
                self.current_movie.start()
            else:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                orig_size = reader.size()
                
                if orig_size.isValid():
                    orig_size.scale(target_size, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(orig_size)

                image = reader.read()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image)
                    self.label_top.setPixmap(pixmap)
            
            self.effect.setOpacity(0.0)
            self.anim.stop()
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.setDuration(duration)
            self.anim.start()
        except Exception as e:
            print("图片加载异常:", e)

    def onAnimationFinished(self):
        if self.fade_state == "fading_out":
            self.label_top.clear()
            self._maybeCollectGarbage()
            if hasattr(self, 'pending_path') and self.pending_path:
                self.fade_state = "fading_in"
                self._load_and_fade_in(self.pending_path, duration=self._animationDuration(1000))
        elif self.fade_state == "crossfade_in":
            self.label_bottom.clear()
            self._maybeCollectGarbage()

    def _maybeCollectGarbage(self, force=False):
        if force:
            gc.collect()
            self._gc_counter = 0
            return

        self._gc_counter += 1
        if self._gc_counter >= GC_COLLECT_EVERY_IMAGES:
            gc.collect()
            self._gc_counter = 0

    def _animationDuration(self, normal_ms):
        if self.low_power_mode:
            return max(250, int(normal_ms * 0.45))
        return normal_ms

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrangeOverlayButtons()
        self._positionCalendar()
        
        if self.is_sidebar_visible:
            self.thumb_list.setGeometry(self._sidebarRect(True))
        else:
            self.thumb_list.setGeometry(self._sidebarRect(False))
            
        if hasattr(self, 'image_paths') and self.image_paths and self.current_index >= 0 and self.isVisible():
            if not hasattr(self, '_resize_timer'):
                self._resize_timer = QTimer(self)
                self._resize_timer.setSingleShot(True)
                self._resize_timer.timeout.connect(self.reloadCurrentStatic)
            self._resize_timer.start(800 if self.low_power_mode else 300)

    def reloadCurrentStatic(self):
        if self.anim.state() == QAbstractAnimation.State.Running:
            self.anim.stop()
            
        if hasattr(self, 'current_movie') and self.current_movie:
            self.current_movie.stop()
            self.current_movie = None
        
        self.fade_state = ""
        self.effect.setOpacity(1.0)
        self.label_bottom.clear()
        self.label_top.clear() 
        gc.collect()

        try:
            path = self.image_paths[self.current_index]
            is_gif = path.lower().endswith('.gif')
            target_size = self._imageTargetSize()
            
            if is_gif:
                self.movie_finished_one_loop = False
                self.pending_next_image = False
                self.last_frame_number = -1
                
                self.current_movie = QMovie(path)
                self.current_movie.frameChanged.connect(self._on_frame_changed)
                self.current_movie.jumpToFrame(0)
                orig_size = self.current_movie.currentImage().size()
                if orig_size.isValid():
                    orig_size.scale(target_size, Qt.AspectRatioMode.KeepAspectRatio)
                    self.current_movie.setScaledSize(orig_size)
                self.label_top.setMovie(self.current_movie)
                self.current_movie.start()
            else:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                orig_size = reader.size()
                
                if orig_size.isValid():
                    orig_size.scale(target_size, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(orig_size)

                image = reader.read()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image)
                    self.label_top.setPixmap(pixmap)
        except Exception as e:
            print("旋转自适应重绘失败:", e)

    def _imageTargetSize(self):
        size = self.label_top.contentsRect().size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            size = self.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            size = current_screen_geometry(self, available=True).size()
        return size

    def stop(self):
        if self.anim.state() == QAbstractAnimation.State.Running:
            self.anim.stop()
        self.play_timer.stop()
        self.mouse_idle_timer.stop()
        if hasattr(self.main_app, "settings_page"):
            self.main_app.settings_page.flushPendingConfigSave()
        
        self._stopThumbnailLoader()
            
        if hasattr(self, 'current_movie') and self.current_movie:
            self.current_movie.stop()
            self.current_movie = None
            
        self.label_top.clear()
        self.label_bottom.clear()
        self._maybeCollectGarbage(force=True)

    def mouseMoveEvent(self, event):
        self.showOverlayAndCursor()
        
        x = event.pos().x()
        width = self.width()
        if x > width - 120:
            self.showSidebar()
        elif self.is_sidebar_visible and x > width - 240:
            self.sidebar_idle_timer.start(3000)
            
        super().mouseMoveEvent(event)
        
    def mousePressEvent(self, event):
        self.showOverlayAndCursor()
        
        # 针对无悬停光标的 Win 平板：只要单指点击屏幕最右侧边缘，立刻呼出抽屉
        x = event.pos().x()
        width = self.width()
        if x > width - 150:
            self.showSidebar()
            
        super().mousePressEvent(event)
        
    def showOverlayAndCursor(self):
        self.window().setCursor(Qt.CursorShape.ArrowCursor)
        self.overlay_widget.show()
        self._positionCalendar()
        self.mouse_idle_timer.start(3000)
        
    def hideOverlayAndCursor(self):
        self.window().setCursor(Qt.CursorShape.BlankCursor)
        self.overlay_widget.hide()
        self._positionCalendar()

    def event(self, event):
        if event.type() == QEvent.Type.Gesture:
            return self.gestureEvent(event)
        return super().event(event)

    def gestureEvent(self, event):
        from PyQt6.QtWidgets import QSwipeGesture
        swipe = event.gesture(Qt.GestureType.SwipeGesture)
        if swipe:
            if swipe.state() == Qt.GestureState.GestureFinished:
                if swipe.horizontalDirection() == QSwipeGesture.SwipeDirection.Left:
                    self.manualNext()
                    if not self.overlay_widget.isHidden():
                        self.mouse_idle_timer.start(3000)
                elif swipe.horizontalDirection() == QSwipeGesture.SwipeDirection.Right:
                    self.manualPrev()
                    if not self.overlay_widget.isHidden():
                        self.mouse_idle_timer.start(3000)
            event.accept()
            return True
        return False
        
    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
            
        if event.key() == Qt.Key.Key_F11:
            self.main_app.toggleFullscreen()
            self.showOverlayAndCursor()
            return
            
        if self.anim.state() == QAbstractAnimation.State.Running:
            return

        if event.key() == Qt.Key.Key_Escape:
            self.showOverlayAndCursor() 
        elif event.key() == Qt.Key.Key_Left:
            self.manualPrev()
            if not self.overlay_widget.isHidden():
                self.mouse_idle_timer.start(3000)
        elif event.key() == Qt.Key.Key_Right:
            self.manualNext()
            if not self.overlay_widget.isHidden():
                self.mouse_idle_timer.start(3000)

class PhotoFrameApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("电子相框")
        self.setStyleSheet("background-color: black; color: white;")
        self.is_fullscreen = False
        self._normal_geometry = QRect()
        self._setDefaultNormalGeometry()
        
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        self.stacked = QStackedWidget()
        self.stacked.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout.addWidget(self.stacked)
        
        self.settings_page = SettingsPage(self)
        self.slideshow_page = SlideshowPage(self)
        
        self.stacked.addWidget(self.settings_page)
        self.stacked.addWidget(self.slideshow_page)
        
        self.stacked.setCurrentWidget(self.settings_page)
        self._snooze_until = None
        self._alarm_dialog_open = False
        self.alarm_timer = QTimer(self)
        self.alarm_timer.timeout.connect(self.checkAlarms)
        self.alarm_timer.start(1000)
        self.refreshAlarmStatus()

    def start_slideshow(self, image_paths, interval_ms, mode, folder_path):
        self.stacked.setCurrentWidget(self.slideshow_page)
        self.slideshow_page.start(image_paths, interval_ms, mode, folder_path)
        self.refreshAlarmStatus()

    def stop_slideshow(self):
        self.slideshow_page.stop()
        self.settings_page.clearStatus()
        self.stacked.setCurrentWidget(self.settings_page)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.refreshAlarmStatus()

    def refreshAlarmStatus(self):
        self.settings_page.current_alarms = normalize_alarm_list(self.settings_page.current_alarms)
        self.settings_page.updateAlarmBtn()
        self.slideshow_page.setAlarmBadgeText(self._alarmBadgeText())

    def openAlarmSettings(self):
        dlg = AlarmSettingsDialog(self, self.settings_page.current_alarms)
        if dlg.exec():
            self.settings_page.current_alarms = dlg.alarmsResult()
            self.settings_page.updateAlarmBtn()
            self.settings_page.saveConfig()
            self.refreshAlarmStatus()
        if self.stacked.currentWidget() == self.slideshow_page:
            self.slideshow_page.showOverlayAndCursor()

    def _alarmBadgeText(self):
        now = datetime.datetime.now()
        if self._snooze_until and self._snooze_until > now:
            return self._snooze_until.strftime("%H:%M")

        next_alarm = next_enabled_alarm_datetime(self.settings_page.current_alarms, now)
        if next_alarm:
            return next_alarm.strftime("%H:%M")
        return ""

    def checkAlarms(self):
        if self._alarm_dialog_open:
            return

        now = datetime.datetime.now()
        if self._snooze_until:
            delta = (now - self._snooze_until).total_seconds()
            if 0 <= delta <= ALARM_TRIGGER_WINDOW_SECONDS:
                alarm_time = self._snooze_until.strftime("%H:%M")
                self._snooze_until = None
                self._triggerAlarm(alarm_time, "")
                return
            if delta > ALARM_TRIGGER_WINDOW_SECONDS:
                self._snooze_until = None

        date_key = now.date().isoformat()
        for alarm in self.settings_page.current_alarms:
            if not alarm.get("enabled", True):
                continue

            parsed = parse_alarm_time(alarm.get("time", ""))
            if not parsed:
                continue
            if not alarm_active_on_date(alarm, now.date()):
                continue

            scheduled = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
            delta = (now - scheduled).total_seconds()
            if 0 <= delta <= ALARM_TRIGGER_WINDOW_SECONDS and alarm.get("last_triggered_date") != date_key:
                alarm["last_triggered_date"] = date_key
                self.settings_page.saveConfig()
                self.refreshAlarmStatus()
                ringtone = alarm.get("ringtone_path", "")
                self._triggerAlarm(alarm["time"], ringtone)
                return

        if now.second == 0:
            self.refreshAlarmStatus()

    def _triggerAlarm(self, alarm_time, ringtone_path=""):
        self._alarm_dialog_open = True
        was_playing = self.stacked.currentWidget() == self.slideshow_page and self.slideshow_page.play_timer.isActive()
        if was_playing:
            self.slideshow_page.play_timer.stop()

        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.raise_()
        self.activateWindow()

        try:
            dlg = AlarmRingDialog(self, alarm_time, ringtone_path)
            dlg.exec()
            if dlg.action == "snooze":
                self._snooze_until = datetime.datetime.now() + datetime.timedelta(minutes=SNOOZE_MINUTES)
            else:
                self._snooze_until = None
        finally:
            self._alarm_dialog_open = False
            if was_playing and self.slideshow_page.btn_pause.text() == "⏸ 固定画框":
                self.slideshow_page.play_timer.start()
            self.refreshAlarmStatus()

    def closeEvent(self, event):
        set_sleep_prevention(False)
        self.slideshow_page.stop()
        event.accept()

    def toggleFullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry.isValid():
                self.setGeometry(self._normal_geometry)
            self.is_fullscreen = False
        else:
            self._normal_geometry = QRect(self.geometry())
            self.showFullScreen()
            self.is_fullscreen = True

    def _setDefaultNormalGeometry(self):
        screen_geometry = current_screen_geometry(self, available=True)
        width = min(1280, max(1024, int(screen_geometry.width() * 0.72)))
        height = min(960, max(768, int(screen_geometry.height() * 0.72)))
        window_geometry = QRect(0, 0, width, height)
        window_geometry.moveCenter(screen_geometry.center())
        self._normal_geometry = QRect(window_geometry)
        self.setGeometry(window_geometry)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F11:
            self.toggleFullscreen()
        else:
            super().keyPressEvent(event)

if __name__ == '__main__':
    QPixmapCache.setCacheLimit(1024 * 6) 
    set_sleep_prevention(True)
    app = QApplication(sys.argv)
    frame = PhotoFrameApp()
    app.aboutToQuit.connect(frame.settings_page.flushPendingConfigSave)
    frame.show()
    result = app.exec()
    set_sleep_prevention(False)
    sys.exit(result)
