import sys
import os
import json
import gc

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QFileDialog, 
    QVBoxLayout, QHBoxLayout, QPushButton, QStackedWidget,
    QComboBox, QGraphicsOpacityEffect, QGridLayout, QSizePolicy,
    QListWidget, QListWidgetItem, QListView, QDialog, QScrollArea, QFrame
)
from PyQt6.QtGui import QPixmap, QImageReader, QGuiApplication, QPixmapCache, QCursor, QMovie, QImage, QIcon
from PyQt6.QtCore import Qt, QTimer, QEvent, QSize, QPropertyAnimation, QAbstractAnimation, QThread, pyqtSignal, QRect, QEasingCurve

CONFIG_FILE = "config.json"

class TouchMenuDialog(QDialog):
    def __init__(self, parent, title, options, delete_callback=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.selected_data = None
        self.selected_text = None

        screen_size = QGuiApplication.primaryScreen().size()
        self.resize(screen_size)

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
        
        self.btn_exit = QPushButton("退出软件")
        self.btn_exit.setStyleSheet("font-size: 18px; padding: 10px 30px; background-color: #ff4444; margin-top: 30px; border-radius: 8px;")
        self.btn_exit.clicked.connect(sys.exit)
        layout.addWidget(self.btn_exit, alignment=Qt.AlignmentFlag.AlignCenter)

    def updateModeBtn(self):
        modes = {"fade_black": "淡出至黑屏再淡入", "crossfade": "双图直接淡入淡出"}
        self.btn_mode.setText("▼ " + modes.get(self.current_mode, "淡出至黑屏再淡入"))
        
    def updateIntervalBtn(self):
        ints = {5:"5 秒极速挂机", 10:"10 秒快进浏览", 20:"20 秒普通观看", 60:"1 分钟慢慢品味", 1800:"半小时极慢沉浸"}
        self.btn_interval.setText("▼ " + ints.get(self.current_interval, f"{self.current_interval} 秒"))

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
        
        self.label_bottom = QLabel()
        self.label_bottom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_bottom.setMouseTracking(True)
        
        self.label_top = QLabel()
        self.label_top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_top.setMouseTracking(True)
        
        self.layout.addWidget(self.label_bottom, 0, 0)
        self.layout.addWidget(self.label_top, 0, 0)
        
        self.effect = QGraphicsOpacityEffect(self.label_top)
        self.label_top.setGraphicsEffect(self.effect)
        
        self.anim = QPropertyAnimation(self.effect, b"opacity")
        self.anim.finished.connect(self.onAnimationFinished)
        
        self.setupOverlay()
        self.setupSidebar()

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
                font-size: 18px; 
                padding: 12px 25px; 
                color: white; 
                background-color: transparent; 
                border: 1px solid #777; 
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 40);
            }
        """)
        
        h_layout = QHBoxLayout(self.overlay_widget)
        h_layout.setContentsMargins(20, 10, 20, 10)
        h_layout.setSpacing(15)
        
        self.btn_prev = QPushButton("⏮ 上一张")
        self.btn_pause = QPushButton("⏸ 固定画框")
        self.btn_next = QPushButton("⏭ 下一张")
        self.btn_reset = QPushButton("⏪ 从头播放")
        
        self.btn_overlay_interval = QPushButton("▼ 调速")
        self.btn_overlay_interval.clicked.connect(self.onOverlayIntervalClicked)
        
        self.btn_settings = QPushButton("⚙ 返回设置")
        self.btn_quit = QPushButton("❌ 退出应用")
        
        h_layout.addWidget(self.btn_prev)
        h_layout.addWidget(self.btn_pause)
        h_layout.addWidget(self.btn_next)
        h_layout.addWidget(self.btn_reset)
        h_layout.addWidget(self.btn_overlay_interval)
        h_layout.addWidget(self.btn_settings)
        h_layout.addWidget(self.btn_quit)

        self.btn_prev.clicked.connect(self.manualPrev)
        self.btn_next.clicked.connect(self.manualNext)
        self.btn_pause.clicked.connect(self.togglePause)
        self.btn_reset.clicked.connect(self.playFromBeginning)
        self.btn_settings.clicked.connect(self.main_app.stop_slideshow)
        self.btn_quit.clicked.connect(sys.exit)
        
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
        
        intervals_map = {5: "5 秒挂机", 10: "10 秒快进", 20: "20 秒普通", 60: "1 分钟沉浸", 1800: "半小时极慢"}
        sec = int(interval_ms / 1000)
        self.btn_overlay_interval.setText("▼ " + intervals_map.get(sec, f"{sec} 秒"))
            
        self.play_timer.setInterval(interval_ms)
        
        # 强制复位侧边栏初始位置
        self.is_sidebar_visible = False
        self.thumb_list.setGeometry(QGuiApplication.primaryScreen().size().width(), 0, 240, QGuiApplication.primaryScreen().size().height())
        
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
            self.sidebar_anim.setEndValue(QRect(self.width() - 240, 0, 240, self.height()))
            self.sidebar_anim.start()
        self.sidebar_idle_timer.start(3000)

    def hideSidebar(self):
        if self.is_sidebar_visible:
            self.is_sidebar_visible = False
            self.sidebar_anim.stop()
            self.sidebar_anim.setStartValue(self.thumb_list.geometry())
            self.sidebar_anim.setEndValue(QRect(self.width(), 0, 240, self.height()))
            self.sidebar_anim.start()

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
            
            if is_gif:
                self.movie_finished_one_loop = False
                self.pending_next_image = False
                self.last_frame_number = -1
                
                self.current_movie = QMovie(path)
                self.current_movie.frameChanged.connect(self._on_frame_changed)
                self.current_movie.jumpToFrame(0)
                orig_size = self.current_movie.currentImage().size()
                if orig_size.isValid():
                    orig_size.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                    self.current_movie.setScaledSize(orig_size)
                    
                self.label_top.setMovie(self.current_movie)
                self.current_movie.start()
            else:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                orig_size = reader.size()
                
                if orig_size.isValid():
                    orig_size.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
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
        
        sidebar_w = 240
        sidebar_h = self.height()
        if self.is_sidebar_visible:
            self.thumb_list.setGeometry(self.width() - sidebar_w, 0, sidebar_w, sidebar_h)
        else:
            self.thumb_list.setGeometry(self.width(), 0, sidebar_w, sidebar_h)
            
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
            
            if is_gif:
                self.movie_finished_one_loop = False
                self.pending_next_image = False
                self.last_frame_number = -1
                
                self.current_movie = QMovie(path)
                self.current_movie.frameChanged.connect(self._on_frame_changed)
                self.current_movie.jumpToFrame(0)
                orig_size = self.current_movie.currentImage().size()
                if orig_size.isValid():
                    orig_size.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                    self.current_movie.setScaledSize(orig_size)
                self.label_top.setMovie(self.current_movie)
                self.current_movie.start()
            else:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                orig_size = reader.size()
                
                if orig_size.isValid():
                    orig_size.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(orig_size)

                image = reader.read()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image)
                    self.label_top.setPixmap(pixmap)
        except Exception as e:
            print("旋转自适应重绘失败:", e)

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
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")
        
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.stacked = QStackedWidget()
        self.layout.addWidget(self.stacked)
        
        self.settings_page = SettingsPage(self)
        self.slideshow_page = SlideshowPage(self)
        
        self.stacked.addWidget(self.settings_page)
        self.stacked.addWidget(self.slideshow_page)
        
        self.stacked.setCurrentWidget(self.settings_page)

    def start_slideshow(self, image_paths, interval_ms, mode, folder_path):
        self.slideshow_page.start(image_paths, interval_ms, mode, folder_path)
        self.stacked.setCurrentWidget(self.slideshow_page)

    def stop_slideshow(self):
        self.slideshow_page.stop()
        self.stacked.setCurrentWidget(self.settings_page)
        self.setCursor(Qt.CursorShape.ArrowCursor)

if __name__ == '__main__':
    QPixmapCache.setCacheLimit(1024 * 10) 
    app = QApplication(sys.argv)
    frame = PhotoFrameApp()
    frame.show()
    sys.exit(app.exec())
