import sys
import os
import json
import gc
import datetime
import ctypes
import atexit

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QFileDialog, 
    QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget,
    QComboBox, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QGridLayout, QSizePolicy,
    QListWidget, QListWidgetItem, QListView, QDialog, QScrollArea, QFrame,
    QTimeEdit, QAbstractSpinBox
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
        backdrop.setStyleSheet("background-color: rgba(0, 0, 0, 200);")
        bg_layout = QVBoxLayout(backdrop)
        
        container = QWidget()
        container.setFixedWidth(min(screen_size.width() - 40, 600))
        container.setStyleSheet(
            "QWidget { background-color: #2b2b2b; border-radius: 12px; }"
            "QPushButton { font-size: 22px; padding: 25px; color: white; background-color: transparent; border: none; border-bottom: 1px solid #444; border-radius: 0; }"
            "QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }"
            "QLabel { font-size: 24px; color: #ccc; font-weight: bold; padding: 25px; border-bottom: 2px solid #555; }"
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
                btn.setStyleSheet("QPushButton { font-size: 22px; padding: 25px; color: white; background-color: transparent; border: none; border-bottom: 1px solid #444; border-radius: 0; text-align: left; } QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }")
                btn.clicked.connect(lambda checked, t=text, d=data_val: self.select_item(t, d))
                row_layout.addWidget(btn, 1)
                
                del_btn = QPushButton("✖")
                del_btn.setStyleSheet("QPushButton { font-size: 26px; padding: 25px 35px; color: #ff6666; background-color: transparent; border: none; border-bottom: 1px solid #444; border-radius: 0; } QPushButton:hover, QPushButton:pressed { background-color: #552222; }")
                
                def make_handler(target_widget, val):
                    return lambda: [delete_callback(val), target_widget.deleteLater()]
                    
                del_btn.clicked.connect(make_handler(row_widget, data_val))
                row_layout.addWidget(del_btn, 0)
                
                scroll_layout.addWidget(row_widget)
            else:
                btn = QPushButton(text)
                btn.clicked.connect(lambda checked, t=text, d=data_val: self.select_item(t, d))
                scroll_layout.addWidget(btn)
            
        cancel_btn = QPushButton("❌ 取消重选撤销操作")
        cancel_btn.setStyleSheet("color: #ff6666; font-size: 20px; border-bottom: none; border-top: 2px solid #555; padding: 20px;")
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
            QWidget { background-color: #252525; border-radius: 12px; }
            QLabel { color: white; background: transparent; }
            QTimeEdit {
                font-size: 54px;
                font-weight: bold;
                color: white;
                background-color: #151515;
                border: 2px solid #555;
                border-radius: 10px;
                padding: 18px;
            }
            QPushButton {
                font-size: 22px;
                padding: 18px 24px;
                color: white;
                background-color: #333;
                border: none;
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }
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
                font-size: 16px; padding: 14px 18px; color: white;
                background-color: #333; border: 1px solid #555; border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }
        """)
        self.btn_ringtone.clicked.connect(self.chooseRingtone)
        ringtone_layout.addWidget(self.btn_ringtone)

        self.btn_clear_ringtone = QPushButton("清除")
        self.btn_clear_ringtone.setStyleSheet("font-size: 16px; padding: 14px 18px; color: #ff6666; background-color: #333; border: 1px solid #555; border-radius: 8px;")
        self.btn_clear_ringtone.clicked.connect(self.clearRingtone)
        ringtone_layout.addWidget(self.btn_clear_ringtone)
        v.addLayout(ringtone_layout)

        buttons = QHBoxLayout()
        self.btn_save = QPushButton("保存")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setStyleSheet("background-color: #555;")
        self.btn_save.clicked.connect(self.acceptTime)
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        v.addLayout(buttons)

        bg_layout.addWidget(container, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(backdrop)

    def _ringtoneButtonText(self):
        if self.selected_ringtone_path and os.path.isfile(self.selected_ringtone_path):
            return f"🔔 {os.path.basename(self.selected_ringtone_path)}"
        return "📂 选择铃声文件 (wav/mp3)"

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
        base = "font-size: 22px; padding: 18px 24px; color: white; border-radius: 8px;"
        active = base + "background-color: #0078D7; border: 2px solid #46a6ff;"
        inactive = base + "background-color: #333; border: 1px solid #555;"
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
        container.setFixedWidth(min(target_geometry.width() - 40, 760))
        container.setStyleSheet("""
            QWidget { background-color: #252525; border-radius: 12px; }
            QLabel { color: white; background: transparent; }
            QPushButton {
                font-size: 20px;
                padding: 16px 18px;
                color: white;
                background-color: transparent;
                border: 1px solid #555;
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }
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
        v.addWidget(self.btn_add)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")
        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.scroll_content)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        scroll.setWidget(self.scroll_content)
        v.addWidget(scroll, 1)

        self.btn_done = QPushButton("完成")
        self.btn_done.setStyleSheet("background-color: #0078D7;")
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
            row.setStyleSheet("QFrame { background-color: #1b1b1b; border-radius: 8px; }")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 8, 8, 8)
            row_layout.setSpacing(8)

            state_text = "已开启" if alarm.get("enabled", True) else "已关闭"
            main_btn = QPushButton(f"{alarm['time']}  {alarm_repeat_text(alarm)}  {state_text}")
            main_btn.setStyleSheet("text-align: left; border: none; background-color: transparent; font-size: 24px;")
            main_btn.clicked.connect(lambda checked=False, alarm_id=alarm["id"]: self.editAlarm(alarm_id))
            row_layout.addWidget(main_btn, 1)

            toggle_btn = QPushButton("停用" if alarm.get("enabled", True) else "启用")
            toggle_btn.clicked.connect(lambda checked=False, alarm_id=alarm["id"]: self.toggleAlarm(alarm_id))
            row_layout.addWidget(toggle_btn, 0)

            del_btn = QPushButton("✖")
            del_btn.setStyleSheet("color: #ff6666;")
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
            QWidget { background-color: #202124; border-radius: 14px; }
            QLabel { color: white; background: transparent; }
            QPushButton {
                font-size: 22px;
                padding: 18px 22px;
                color: white;
                background-color: #333;
                border: none;
                border-radius: 8px;
            }
            QPushButton:hover, QPushButton:pressed { background-color: #0078D7; }
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
        ringtone_hint = QLabel(f"🔔 {ringtone_name}")
        ringtone_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ringtone_hint.setStyleSheet("font-size: 16px; color: rgba(255, 255, 255, 150);")
        v.addWidget(ringtone_hint)

        buttons = QHBoxLayout()
        snooze_btn = QPushButton(f"稍后 {SNOOZE_MINUTES} 分钟")
        dismiss_btn = QPushButton("关闭闹钟")
        dismiss_btn.setStyleSheet("background-color: #0078D7;")
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

class ThumbnailLoader(QThread):
    thumbnail_ready = pyqtSignal(int, QImage)
    
    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.running = True

    def run(self):
        for i, path in enumerate(self.paths):
            if not self.running: break
            try:
                # 保护低配置 CPU，每处理一张照片放缓，确保主线程幻灯片丝滑
                QThread.msleep(15)
                
                image = QImage()
                is_gif = path.lower().endswith('.gif')
                if is_gif:
                    movie = QMovie(path)
                    movie.jumpToFrame(0)
                    image = movie.currentImage()
                    if not image.isNull():
                        image = image.scaled(180, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
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
        self.current_show_clock = True
        self.current_alarms = []
        self.initUI()
        self.loadConfig()

    def initUI(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.title = QLabel("🖥️ 电子相框控制台")
        self.title.setStyleSheet("font-size: 38px; font-weight: bold; margin-bottom: 40px;")
        layout.addWidget(self.title, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # ================= 相册路径与历史记录区块 =================
        path_layout = QHBoxLayout()
        path_label = QLabel("相册路径:")
        path_label.setStyleSheet("font-size: 20px;")
        
        self.btn_recent = QPushButton("▼ 未选择目录 (点击选择历史)")
        self.btn_recent.setStyleSheet("""
            QPushButton { font-size: 18px; padding: 10px 15px; color: white; background-color: #2b2b2b; border: 2px solid #444; border-radius: 8px; text-align: left; }
            QPushButton:hover { border: 2px solid #0078D7; background-color: #383838; }
        """)
        self.btn_recent.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_recent.setMaximumWidth(700)
        self.btn_recent.clicked.connect(self.onRecentClicked)
        
        self.btn_select = QPushButton("📂 浏览新盘夹")
        self.btn_select.setStyleSheet("font-size: 16px; padding: 8px 15px; background-color: #444; border-radius: 5px;")
        self.btn_select.clicked.connect(self.chooseDirectory)
        
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.btn_recent)
        path_layout.addWidget(self.btn_select)
        path_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(path_layout)
        
        layout.addSpacing(30)
        
        style_btn = """
            QPushButton { font-size: 18px; padding: 10px 15px; color: white; background-color: #2b2b2b; border: 2px solid #444; border-radius: 8px; min-width: 200px; text-align: left; }
            QPushButton:hover { border: 2px solid #0078D7; background-color: #383838; }
        """

        # ================= 动画模式设置 =================
        mode_layout = QHBoxLayout()
        mode_label = QLabel("视效切换模式:")
        mode_label.setStyleSheet("font-size: 20px;")
        
        self.current_mode = "fade_black"
        self.btn_mode = QPushButton("▼ 淡出至黑屏再淡入")
        self.btn_mode.setStyleSheet(style_btn)
        self.btn_mode.clicked.connect(self.onModeClicked)
        
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.btn_mode)
        mode_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(mode_layout)
        layout.addSpacing(15)

        # ================= 现代版切换间隔设置 =================
        int_layout = QHBoxLayout()
        int_label = QLabel("幻灯片预设间隔:")
        int_label.setStyleSheet("font-size: 20px;")
        
        self.current_interval = 5
        self.btn_interval = QPushButton("▼ 5 秒极速挂机")
        self.btn_interval.setStyleSheet(style_btn)
        self.btn_interval.clicked.connect(self.onIntervalClicked)
        
        int_layout.addWidget(int_label)
        int_layout.addWidget(self.btn_interval)
        int_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(int_layout)
        layout.addSpacing(15)

        # ================= 时钟显示开关 =================
        clock_layout = QHBoxLayout()
        clock_label = QLabel("时钟显示:")
        clock_label.setStyleSheet("font-size: 20px;")

        self.btn_clock = QPushButton("▼ 开启")
        self.btn_clock.setStyleSheet(style_btn)
        self.btn_clock.clicked.connect(self.onClockClicked)

        clock_layout.addWidget(clock_label)
        clock_layout.addWidget(self.btn_clock)
        clock_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(clock_layout)
        layout.addSpacing(15)

        # ================= 闹钟设置 =================
        alarm_layout = QHBoxLayout()
        alarm_label = QLabel("闹钟:")
        alarm_label.setStyleSheet("font-size: 20px;")

        self.btn_alarm = QPushButton("▼ 无闹钟")
        self.btn_alarm.setStyleSheet(style_btn)
        self.btn_alarm.clicked.connect(self.onAlarmClicked)

        alarm_layout.addWidget(alarm_label)
        alarm_layout.addWidget(self.btn_alarm)
        alarm_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(alarm_layout)

        layout.addSpacing(30)

        # 错误提示框
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("font-size: 18px; color: #ff6666;")
        layout.addWidget(self.error_label, alignment=Qt.AlignmentFlag.AlignCenter)
        
        layout.addSpacing(10)
        
        self.btn_start = QPushButton("▶ 开始播放")
        self.btn_start.setStyleSheet("font-size: 26px; padding: 18px 50px; background-color: #0078D7; border-radius: 12px;")
        self.btn_start.clicked.connect(self.start)
        layout.addWidget(self.btn_start, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.btn_fullscreen = QPushButton("🔲 窗口/全屏切换 (F11)")
        self.btn_fullscreen.setStyleSheet("font-size: 18px; padding: 10px 30px; background-color: #555555; margin-top: 30px; border-radius: 8px;")
        self.btn_fullscreen.clicked.connect(self.main_app.toggleFullscreen)
        layout.addWidget(self.btn_fullscreen, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.btn_exit = QPushButton("退出软件")
        self.btn_exit.setStyleSheet("font-size: 18px; padding: 10px 30px; background-color: #ff4444; margin-top: 10px; border-radius: 8px;")
        self.btn_exit.clicked.connect(sys.exit)
        layout.addWidget(self.btn_exit, alignment=Qt.AlignmentFlag.AlignCenter)

    def updateModeBtn(self):
        modes = {"fade_black": "淡出至黑屏再淡入", "crossfade": "双图直接淡入淡出"}
        self.btn_mode.setText("▼ " + modes.get(self.current_mode, "淡出至黑屏再淡入"))
        
    def updateIntervalBtn(self):
        ints = {5:"5 秒极速挂机", 10:"10 秒快进浏览", 20:"20 秒普通观看", 60:"1 分钟慢慢品味", 1800:"半小时极慢沉浸"}
        self.btn_interval.setText("▼ " + ints.get(self.current_interval, f"{self.current_interval} 秒"))

    def updateClockBtn(self):
        self.btn_clock.setText("▼ 开启" if self.current_show_clock else "▼ 关闭")

    def updateAlarmBtn(self):
        enabled_count = sum(1 for alarm in self.current_alarms if alarm.get("enabled", True))
        if enabled_count == 0:
            self.btn_alarm.setText("▼ 无闹钟")
            return

        next_alarm = next_enabled_alarm_datetime(self.current_alarms)
        if next_alarm:
            self.btn_alarm.setText(f"▼ {enabled_count} 个｜下一个 {next_alarm.strftime('%H:%M')}")
        else:
            self.btn_alarm.setText(f"▼ {enabled_count} 个闹钟")

    def onRecentClicked(self):
        if not self.recent_paths: return
        opts = {p: p for p in self.recent_paths}
        dlg = TouchMenuDialog(self, "选择历史相册路径", opts, delete_callback=self.removeRecentPath)
        if dlg.exec():
            self.folder_path = dlg.selected_data
            self.btn_recent.setText("▼ " + self.folder_path)
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
        self.btn_recent.setText("▼ " + path)

    def removeRecentPath(self, path):
        if path in self.recent_paths:
            self.recent_paths.remove(path)
            self.saveConfig()
            if self.folder_path == path:
                self.folder_path = ""
                self.btn_recent.setText("▼ 未选择目录 (点击选择历史)")

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
                            self.btn_recent.setText("▼ " + folder_path)
                            
                    self.current_interval = config.get("interval", 5)
                    self.updateIntervalBtn()
                    
                    self.current_mode = config.get("transition_mode", "fade_black")
                    self.updateModeBtn()

                    self.current_show_clock = config.get("show_clock", True)
                    self.updateClockBtn()

                    self.current_alarms = normalize_alarm_list(config.get("alarms", []))
                    self.updateAlarmBtn()
                        
                    self.folder_progress = config.get("folder_progress", {})
            except Exception as e:
                print("加载配置文件失败:", e)

    def saveConfig(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "folder_path": self.folder_path,
                    "recent_paths": self.recent_paths,
                    "interval": self.current_interval,
                    "transition_mode": self.current_mode,
                    "show_clock": self.current_show_clock,
                    "alarms": self.current_alarms,
                    "folder_progress": self.folder_progress
                }, f)
        except Exception as e:
            print("保存配置失败:", e)

    def chooseDirectory(self):
        folder_path = QFileDialog.getExistingDirectory(self, "请选择相册文件夹")
        if folder_path:
            self.folder_path = folder_path
            self.addRecentPath(folder_path)
            self.error_label.setText("") 

    def start(self):
        self.error_label.setText("")
        
        if not self.folder_path or not os.path.isdir(self.folder_path):
            self.error_label.setText("错误: 所选路径无效，请拉下右侧侧选单或者重新浏览")
            return
            
        valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
        image_paths = []
        for root, dirs, files in os.walk(self.folder_path):
            for file in files:
                if os.path.splitext(file)[1].lower() in valid_exts:
                    image_paths.append(os.path.join(root, file))
                    
        if not image_paths:
            self.error_label.setText("错误: 文件夹里未找到任何支持的图片格式")
            return
            
        self.saveConfig()
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
                padding: 10px 14px; 
                color: white; 
                background-color: transparent; 
                border: 1px solid #777; 
                border-radius: 6px;
                min-width: 0px;
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
        self.btn_clock_overlay = QPushButton("🕒 时钟")
        self.btn_alarm_overlay = QPushButton("闹钟")
        
        self.btn_fullscreen_overlay = QPushButton("🔲 全屏切换")
        self.btn_settings = QPushButton("⚙ 返回设置")
        self.btn_quit = QPushButton("❌ 退出应用")
        
        self.overlay_buttons = [
            self.btn_prev, self.btn_pause, self.btn_next, self.btn_reset,
            self.btn_overlay_interval, self.btn_clock_overlay, self.btn_alarm_overlay, self.btn_fullscreen_overlay,
            self.btn_settings, self.btn_quit
        ]
        for btn in self.overlay_buttons:
            btn.setMinimumHeight(46)
            btn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        self.btn_prev.clicked.connect(self.manualPrev)
        self.btn_next.clicked.connect(self.manualNext)
        self.btn_pause.clicked.connect(self.togglePause)
        self.btn_reset.clicked.connect(self.playFromBeginning)
        self.btn_clock_overlay.clicked.connect(self.toggleClock)
        self.btn_alarm_overlay.clicked.connect(self.main_app.openAlarmSettings)
        self.btn_fullscreen_overlay.clicked.connect(self.main_app.toggleFullscreen)
        self.btn_settings.clicked.connect(self.main_app.stop_slideshow)
        self.btn_quit.clicked.connect(sys.exit)
        self.updateClockOverlayBtn()
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

    def updateClockOverlayBtn(self):
        if hasattr(self, "btn_clock_overlay"):
            self.btn_clock_overlay.setText("🕒 时钟开" if self.clock_visible else "🕒 时钟关")

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

        if not force and getattr(self, "_overlay_columns", None) == columns:
            return

        while self.overlay_layout.count():
            self.overlay_layout.takeAt(0)

        self._overlay_columns = columns
        for col in range(max(len(self.overlay_buttons), columns)):
            self.overlay_layout.setColumnStretch(col, 1 if col < columns else 0)
        for index, button in enumerate(self.overlay_buttons):
            row = index // columns
            col = index % columns
            self.overlay_layout.addWidget(button, row, col)

        self.overlay_widget.setMaximumWidth(max(320, width - 80))
        self.overlay_widget.adjustSize()

    def playFromBeginning(self):
        self.pending_next_image = False
        self.current_index = -1
        self.nextImage()
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start()

    def start(self, image_paths, interval_ms, mode, folder_path):
        self.image_paths = image_paths
        self.transition_mode = mode
        self.folder_path = folder_path
        
        # 加载历史进度记忆，如果没记忆默认从第一张也就是 0 之前 (-1) 开始
        saved_index = self.main_app.settings_page.folder_progress.get(self.folder_path, 0)
        self.current_index = saved_index - 1
        
        self.fade_state = ""
        self.btn_pause.setText("⏸ 固定画框")
        self.setClockVisible(self.main_app.settings_page.current_show_clock)
        
        intervals_map = {5: "5 秒挂机", 10: "10 秒快进", 20: "20 秒普通", 60: "1 分钟沉浸", 1800: "半小时极慢"}
        sec = int(interval_ms / 1000)
        self.btn_overlay_interval.setText("▼ " + intervals_map.get(sec, f"{sec} 秒"))
            
        self.play_timer.setInterval(interval_ms)
        
        # 强制复位侧边栏初始位置
        self.is_sidebar_visible = False
        self.thumb_list.setGeometry(self._sidebarRect(False))
        
        # 初始化列表占位与后台加载器
        self.thumb_list.clear()
        for i in range(len(self.image_paths)):
            item = QListWidgetItem()
            item.setSizeHint(QSize(210, 160))
            self.thumb_list.addItem(item)
            
        self.thumb_loader = ThumbnailLoader(self.image_paths)
        self.thumb_loader.thumbnail_ready.connect(self.onThumbnailReady)
        self.thumb_loader.start()
        
        self.showOverlayAndCursor()
        
        QTimer.singleShot(50, self.nextImage)
        self.play_timer.start()
        
        self.setFocus()

    def onThumbnailReady(self, index, image):
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

    def togglePause(self):
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
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start() 

    def manualNext(self):
        self.nextImage()
        if self.btn_pause.text() == "⏸ 固定画框":
            self.play_timer.start() 

    def nextImage(self):
        if not self.image_paths: return
        self.pending_next_image = False
        self.current_index = (self.current_index + 1) % len(self.image_paths)
        self.loadImage(self.image_paths[self.current_index])

    def prevImage(self):
        if not self.image_paths: return
        self.pending_next_image = False
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
            self.main_app.settings_page.folder_progress[self.folder_path] = self.current_index
            self.main_app.settings_page.saveConfig()
            
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
            self._load_and_fade_in(path, duration=1200)
            return

        if self.transition_mode == "fade_black":
            self.pending_path = path
            self.fade_state = "fading_out"
            
            self.label_top.clear()
            self.label_top.setPixmap(old_pixmap)
            gc.collect()
            
            self.effect.setOpacity(1.0)
            self.anim.stop()
            self.anim.setStartValue(1.0)
            self.anim.setEndValue(0.0)
            self.anim.setDuration(1000)
            self.anim.start()
        else:
            self.fade_state = "crossfade_in"
            self.label_bottom.setPixmap(old_pixmap)
            self.label_top.clear()
            gc.collect()
            
            self._load_and_fade_in(path, duration=1800)

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
            gc.collect()
            if hasattr(self, 'pending_path') and self.pending_path:
                self.fade_state = "fading_in"
                self._load_and_fade_in(self.pending_path, duration=1000)
        elif self.fade_state == "crossfade_in":
            self.label_bottom.clear()
            gc.collect()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrangeOverlayButtons()
        
        if self.is_sidebar_visible:
            self.thumb_list.setGeometry(self._sidebarRect(True))
        else:
            self.thumb_list.setGeometry(self._sidebarRect(False))
            
        if hasattr(self, 'image_paths') and self.image_paths and self.current_index >= 0 and self.isVisible():
            if not hasattr(self, '_resize_timer'):
                self._resize_timer = QTimer(self)
                self._resize_timer.setSingleShot(True)
                self._resize_timer.timeout.connect(self.reloadCurrentStatic)
            self._resize_timer.start(300)

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
        
        if hasattr(self, 'thumb_loader') and self.thumb_loader:
            self.thumb_loader.running = False
            self.thumb_loader.wait()
            self.thumb_loader = None
            
        if hasattr(self, 'current_movie') and self.current_movie:
            self.current_movie.stop()
            self.current_movie = None
            
        self.label_top.clear()
        self.label_bottom.clear()
        gc.collect()

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
        self.mouse_idle_timer.start(3000)
        
    def hideOverlayAndCursor(self):
        self.window().setCursor(Qt.CursorShape.BlankCursor)
        self.overlay_widget.hide()

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
        self.setWindowTitle("🖥️ 电子相框控制台")
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
    QPixmapCache.setCacheLimit(1024 * 10) 
    set_sleep_prevention(True)
    app = QApplication(sys.argv)
    frame = PhotoFrameApp()
    frame.show()
    result = app.exec()
    set_sleep_prevention(False)
    sys.exit(result)
