import sys
import os
import json
import shutil
import ctypes
import stat
import time
import hashlib
from datetime import datetime
from send2trash import send2trash

# รายชื่อโฟลเดอร์ระบบที่ต้องข้ามการสแกนเด็ดขาด
SKIP_DIRECTORIES = {
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\system volume information",
    "c:\\$recycle.bin",
    "c:\\appdata"
}

def is_system_path(path):
    """ฟังก์ชันเช็กว่า Path นี้อยู่ในโฟลเดอร์ระบบหรือไม่"""
    clean_p = os.path.abspath(path).lower()
    for skip_path in SKIP_DIRECTORIES:
        if clean_p == skip_path or clean_p.startswith(skip_path + os.sep):
            return True
    return False

# นำเข้าไลบรารีอ่าน PDF (ใช้ pdfplumber หรือ pypdf)
pdf_library = None
try:
    import pdfplumber
    pdf_library = "pdfplumber"
except ImportError:
    try:
        import pypdf
        pdf_library = "pypdf"
    except ImportError:
        pdf_library = None

# นำเข้าไลบรารีอ่าน Word
try:
    import docx
except ImportError:
    docx = None

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QScrollArea
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QImage

class ImageZoomDialog(QDialog):
    """หน้าต่าง Pop-up สำหรับขยายดูรูปภาพขนาดใหญ่"""
    def __init__(self, pixmap, title="ตัวอย่างรูปภาพขนาดใหญ่", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 900)

        layout = QVBoxLayout(self)
        
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        
        lbl_image = QLabel(scroll_area)
        lbl_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        scaled_pixmap = pixmap.scaled(
            760, 850, 
            Qt.AspectRatioMode.KeepAspectRatio, 
            Qt.TransformationMode.SmoothTransformation
        )
        lbl_image.setPixmap(scaled_pixmap)
        
        scroll_area.setWidget(lbl_image)
        layout.addWidget(scroll_area)


from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QTreeWidget, QTreeWidgetItem,
    QProgressBar, QMessageBox, QHeaderView, QFrame, QProgressDialog
)
from PyQt6.QtCore import Qt, QPointF, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QPainterPath, QImageReader
from scanner import ScannerThread
from db import DatabaseManager


# ==========================================
# Worker Thread สำหรับลบไฟล์เบื้องหลัง
# ==========================================
class DeleteWorkerThread(QThread):
    progress_signal = pyqtSignal(int, str, bool, str)
    finished_signal = pyqtSignal(list, list)

    def __init__(self, items_to_delete, backup_dir, db):
        super().__init__()
        self.items_to_delete = items_to_delete
        self.backup_dir = backup_dir
        self.db = db
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def run(self):
        deleted_history = []
        failed_files = []

        for idx, (item, path) in enumerate(self.items_to_delete):
            if self.is_cancelled:
                break

            self.progress_signal.emit(idx, path, True, "")

            try:
                # ป้องกันการลบไฟล์ระบบชั้นสอง
                if is_system_path(path):
                    failed_files.append((path, "ข้ามการลบเนื่องจากเป็นไฟล์ในโฟลเดอร์ระบบสำคัญ"))
                    continue

                if os.path.exists(path):
                    os.chmod(path, stat.S_IWRITE)

                    backup_file_path = os.path.join(self.backup_dir, f"backup_{idx}_{os.path.basename(path)}")
                    shutil.copy2(path, backup_file_path)

                    send2trash(path)

                    if not os.path.exists(path):
                        try:
                            self.db.remove_file(path)
                        except Exception:
                            pass

                        deleted_history.append({
                            "original_path": path,
                            "backup_path": backup_file_path,
                            "deleted_at": datetime.now().isoformat(),
                            "item_ref": item
                        })
                    else:
                        failed_files.append((path, "ย้ายลง Recycle Bin ไม่สำเร็จ"))
                else:
                    failed_files.append((path, "ไม่พบไฟล์ดังกล่าว"))

            except Exception as e:
                failed_files.append((path, str(e)))

        self.finished_signal.emit(deleted_history, failed_files)


# ==========================================
# Main UI
# ==========================================
class DuplicateCleanerUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ultra Duplicate Finder - Professional UI - Ms.Tissarin Wadphrai")
        self.resize(1200, 750)
        
        self.target_folder = ""
        self.scanner_thread = None
        self.delete_thread = None
        self.history_file = "delete_history.json"
        self.backup_dir = os.path.abspath(".trash_backup")
        self.db = DatabaseManager()
        self.is_dark_mode = True
        self.scan_start_time = None
        self.last_total_scanned = 0

        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir, exist_ok=True)

        self.checkmark_path = self.create_checkmark_icon()

        self.init_ui()
        self.apply_theme()
        
        self.btn_delete.setEnabled(False)
        self.btn_undo.setEnabled(False)

        if os.path.exists(self.history_file):
            self.btn_undo.setEnabled(True)

    def create_checkmark_icon(self):
        size = 16
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        path.moveTo(QPointF(3.0, 8.5))
        path.lineTo(QPointF(6.5, 12.0))
        path.lineTo(QPointF(13.0, 4.5))

        pen = QPen(QColor("#FFFFFF"), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()

        icon_path = os.path.abspath("temp_checkmark.png")
        pixmap.save(icon_path, "PNG")
        return icon_path.replace("\\", "/")

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(15, 15, 15, 15)

        # 1. Header Bar
        header_layout = QHBoxLayout()
        self.lbl_logo = QLabel("✨ Duplicate File Detector")
        self.lbl_logo.setStyleSheet("font-size: 20px; font-weight: bold;")
        
        self.btn_theme = QPushButton("🌙 Dark Mode")
        self.btn_theme.setObjectName("btn_theme")
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_theme.clicked.connect(self.toggle_theme)

        header_layout.addWidget(self.lbl_logo)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_theme)
        main_layout.addLayout(header_layout)

        # 2. Control Section
        self.ctrl_frame = QFrame()
        self.ctrl_frame.setObjectName("ctrl_frame")
        ctrl_layout = QVBoxLayout(self.ctrl_frame)

        self.lbl_path = QLabel("โฟลเดอร์: ยังไม่ได้เลือกโฟลเดอร์")
        self.lbl_path.setStyleSheet("font-size: 13px; font-weight: bold; margin-bottom: 5px;")

        btn_box = QHBoxLayout()
        self.btn_select = QPushButton("📁 เลือกโฟลเดอร์")
        self.btn_select.setObjectName("btn_select")
        self.btn_select.clicked.connect(self.select_folder)
        
        self.btn_scan_c = QPushButton("💻 สแกนทั้งเครื่อง")
        self.btn_scan_c.setObjectName("btn_scan_c")
        self.btn_scan_c.clicked.connect(self.select_c_drive)

        self.btn_scan = QPushButton("🚀 เริ่มสแกนทันที")
        self.btn_scan.setObjectName("btn_scan")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_scan.setEnabled(False)

        btn_box.addWidget(self.btn_select)
        btn_box.addWidget(self.btn_scan_c)
        btn_box.addStretch()
        btn_box.addWidget(self.btn_scan)

        ctrl_layout.addWidget(self.lbl_path)
        ctrl_layout.addLayout(btn_box)
        main_layout.addWidget(self.ctrl_frame)

        # 3. Info Cards
        cards_layout = QHBoxLayout()
        
        free_gb = self.get_free_space_gb()
        self.lbl_card_free = QLabel(f"💾 พื้นที่คงเหลือ: {free_gb:.2f} GB")
        self.lbl_card_time = QLabel("⏳ เหลือเวลาอีกประมาณ: -")
        self.lbl_card_total_dup = QLabel("📊 ไฟล์ซ้ำทั้งหมด: 0 / 0 ไฟล์")
        self.lbl_card_deletable = QLabel("🗑️ ไฟล์ที่สามารถลบได้: 0 ไฟล์")
        self.lbl_card_save = QLabel("♻️ พื้นที่ประหยัดได้: 0.00 B")
        self.lbl_card_status = QLabel("⌛ พร้อมทำงาน")

        self.card_labels = [
            self.lbl_card_free, self.lbl_card_time, 
            self.lbl_card_total_dup, self.lbl_card_deletable,
            self.lbl_card_save, self.lbl_card_status
        ]
        for card in self.card_labels:
            card.setStyleSheet("font-size: 13px; font-weight: 500;")
            cards_layout.addWidget(card)

        main_layout.addLayout(cards_layout)

        # Status Line & Progress Bar
        self.lbl_status_line = QLabel("พร้อมสแกนข้อมูล")
        self.lbl_status_line.setStyleSheet("font-size: 12px;")
        main_layout.addWidget(self.lbl_status_line)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        main_layout.addWidget(self.progress_bar)

        # 4. Content Area
        content_layout = QHBoxLayout()

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["กลุ่ม", "เลือก / ชื่อไฟล์ / Path", "ขนาดไฟล์", "สถานะ / Hash"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # ปรับความกว้างของคอลัมน์แรกเป็น 130 เพื่อไม่ให้คำว่า กลุ่มที่ 10, 11... ถูกตัดคำเป็น ...
        self.tree.setColumnWidth(0, 130)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 220)
        
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.itemClicked.connect(self.update_preview)

        content_layout.addWidget(self.tree, 3)

        # Preview Panel
        self.preview_panel = QFrame()
        self.preview_panel.setObjectName("preview_panel")
        self.preview_panel.setMinimumWidth(280)
        
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        
        preview_title = QLabel("👁️ ตัวอย่างไฟล์ (Preview)")
        preview_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        
        self.lbl_preview_image = QLabel("เลือกไฟล์รูปภาพหรือเอกสารเพื่อดูตัวอย่าง")
        self.lbl_preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview_image.setStyleSheet("border: 1px dashed #6c757d; border-radius: 6px; padding: 10px;")
        self.lbl_preview_image.setFixedHeight(240)
        self.lbl_preview_image.setWordWrap(True)

        self.lbl_preview_info = QLabel("ยังไม่ได้เลือกไฟล์")
        self.lbl_preview_info.setWordWrap(True)
        self.lbl_preview_info.setStyleSheet("font-size: 11px;")

        preview_layout.addWidget(preview_title)
        preview_layout.addWidget(self.lbl_preview_image)
        preview_layout.addWidget(self.lbl_preview_info)
        preview_layout.addStretch()

        content_layout.addWidget(self.preview_panel, 1)

        main_layout.addLayout(content_layout)

        # 5. Bottom Buttons
        bottom_layout = QHBoxLayout()
        
        self.btn_undo = QPushButton("↩️ กู้คืนไฟล์ล่าสุด (Undo Delete)")
        self.btn_undo.setObjectName("btn_undo")
        self.btn_undo.clicked.connect(self.restore_last_deleted)

        self.btn_delete = QPushButton("🗑 ลบไฟล์ที่เลือก (ลง Recycle Bin)")
        self.btn_delete.setObjectName("btn_delete")
        self.btn_delete.clicked.connect(self.delete_selected)

        bottom_layout.addWidget(self.btn_undo)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.btn_delete)
        main_layout.addLayout(bottom_layout)

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.apply_theme()

    def apply_theme(self):
        checkbox_style = f"""
            QTreeWidget::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
            }}
            QTreeWidget::indicator:unchecked {{
                border: 1.5px solid #6c757d;
                background-color: transparent;
            }}
            QTreeWidget::indicator:checked {{
                border: 1.5px solid #34c759;
                background-color: #34c759;
                image: url("{self.checkmark_path}");
            }}
            QTreeWidget::indicator:hover {{
                border-color: #30d158;
            }}
        """

        msgbox_style = """
            QMessageBox, QProgressDialog {
                background-color: #ffffff;
            }
            QMessageBox QLabel, QProgressDialog QLabel {
                color: #1c1c1e !important;
                font-size: 13px;
                font-weight: 500;
            }
            QMessageBox QPushButton, QProgressDialog QPushButton {
                background-color: #e5e5ea;
                color: #1c1c1e !important;
                border: 1px solid #c7c7cc;
                border-radius: 6px;
                padding: 6px 16px;
                font-weight: bold;
                min-width: 60px;
            }
            QMessageBox QPushButton:hover, QProgressDialog QPushButton:hover {
                background-color: #d1d1d6;
            }
        """

        if self.is_dark_mode:
            self.btn_theme.setText("🌙 Dark Mode")
            self.setStyleSheet(f"""
                QMainWindow {{ background-color: #12121c; }}
                QLabel {{ color: #f2f2f7; }}
                
                QFrame#ctrl_frame, QFrame#preview_panel {{ 
                    background-color: #1e1e2d; 
                    border-radius: 10px; 
                    padding: 10px;
                    border: 1px solid #2c2c3e;
                }}

                QPushButton#btn_select, QPushButton#btn_scan_c {{ 
                    padding: 8px 18px; 
                    background-color: #2b2b3d; 
                    color: #ffffff; 
                    border: 1px solid #3d3d52;
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 13px;
                }}
                QPushButton#btn_select:hover, QPushButton#btn_scan_c:hover {{ background-color: #3b3b52; }}

                QPushButton#btn_theme {{
                    background-color: #2c2c3e; 
                    color: #ffffff; 
                    border: 1px solid #3d3d52;
                    border-radius: 12px; 
                    padding: 6px 16px;
                    font-weight: bold;
                }}
                QPushButton#btn_theme:hover {{ background-color: #3b3b52; }}

                QPushButton#btn_scan {{ 
                    background-color: #34c759; 
                    color: #ffffff; 
                    padding: 10px 24px;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: bold;
                }}
                QPushButton#btn_scan:hover {{ background-color: #30d158; }}
                QPushButton#btn_scan:disabled {{ background-color: #2c2c3e; color: #545458; }}

                QPushButton#btn_delete {{ 
                    background-color: #ff453a; 
                    color: #ffffff; 
                    padding: 10px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                }}
                QPushButton#btn_delete:hover {{ background-color: #ff3b30; }}
                QPushButton#btn_delete:disabled {{ background-color: #3a1c1a; color: #632a26; }}

                QPushButton#btn_undo {{ 
                    background-color: #af52de; 
                    color: #ffffff; 
                    padding: 10px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                }}
                QPushButton#btn_undo:hover {{ background-color: #bf5af2; }}
                QPushButton#btn_undo:disabled {{ background-color: #2a1b33; color: #523166; }}

                QProgressBar {{
                    border: none;
                    border-radius: 6px;
                    text-align: center;
                    background-color: #1e1e2d;
                    color: #ffffff;
                    font-weight: bold;
                    height: 18px;
                }}
                QProgressBar::chunk {{
                    background-color: #30d158;
                    border-radius: 6px;
                }}

                QTreeWidget {{ 
                    background-color: #1a1a26; 
                    color: #f2f2f7; 
                    border: 1px solid #2c2c3e; 
                    border-radius: 8px; 
                }}
                QTreeWidget::item {{ padding: 4px 0px; }}

                {checkbox_style}
                {msgbox_style}

                QHeaderView::section {{ 
                    background-color: #007aff; 
                    color: #ffffff; 
                    padding: 6px; 
                    font-weight: bold; 
                    border: none;
                }}
            """)
        else:
            self.btn_theme.setText("☀ Light Mode")
            self.setStyleSheet(f"""
                QMainWindow {{ background-color: #f2f2f7; }}
                QLabel {{ color: #1c1c1e; }}
                
                QFrame#ctrl_frame, QFrame#preview_panel {{ 
                    background-color: #ffffff; 
                    border-radius: 10px; 
                    padding: 10px;
                    border: 1px solid #d1d1d6;
                }}

                QPushButton#btn_select, QPushButton#btn_scan_c {{ 
                    padding: 8px 18px; 
                    background-color: #e5e5ea; 
                    color: #1c1c1e; 
                    border: 1px solid #c7c7cc;
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 13px;
                }}
                QPushButton#btn_select:hover, QPushButton#btn_scan_c:hover {{ background-color: #d1d1d6; }}

                QPushButton#btn_theme {{
                    background-color: #ffffff; 
                    color: #1c1c1e; 
                    border: 1px solid #c7c7cc;
                    border-radius: 12px; 
                    padding: 6px 16px;
                    font-weight: bold;
                }}
                QPushButton#btn_theme:hover {{ background-color: #e5e5ea; }}

                QPushButton#btn_scan {{ 
                    background-color: #34c759; 
                    color: #ffffff; 
                    padding: 10px 24px;
                    border-radius: 8px;
                    font-weight: bold;
                }}
                QPushButton#btn_scan:hover {{ background-color: #28a745; }}
                QPushButton#btn_scan:disabled {{ background-color: #e5e5ea; color: #a1a1a6; }}

                QPushButton#btn_delete {{ 
                    background-color: #ff3b30; 
                    color: #ffffff; 
                    padding: 10px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                }}
                QPushButton#btn_delete:hover {{ background-color: #d70015; }}
                QPushButton#btn_delete:disabled {{ background-color: #ffccc7; color: #ffffff; }}

                QPushButton#btn_undo {{ 
                    background-color: #af52de; 
                    color: #ffffff; 
                    padding: 10px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                }}
                QPushButton#btn_undo:hover {{ background-color: #8e24aa; }}
                QPushButton#btn_undo:disabled {{ background-color: #e1bee7; color: #ffffff; }}

                QProgressBar {{
                    border: 1px solid #d1d1d6;
                    border-radius: 6px;
                    text-align: center;
                    background-color: #ffffff;
                    color: #1c1c1e;
                    font-weight: bold;
                    height: 18px;
                }}
                QProgressBar::chunk {{
                    background-color: #34c759;
                    border-radius: 6px;
                }}

                QTreeWidget {{ 
                    background-color: #ffffff; 
                    color: #1c1c1e; 
                    border: 1px solid #d1d1d6; 
                    border-radius: 8px; 
                }}
                QTreeWidget::item {{ padding: 4px 0px; }}

                {checkbox_style}
                {msgbox_style}

                QHeaderView::section {{ 
                    background-color: #007aff; 
                    color: #ffffff; 
                    padding: 6px; 
                    font-weight: bold; 
                    border: none;
                }}
            """)

    def get_free_space_gb(self):
        try:
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p('C:\\'),
                None,
                None,
                ctypes.pointer(free_bytes)
            )
            return free_bytes.value / (1024 ** 3)
        except Exception:
            return 0.0

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "เลือกโฟลเดอร์ที่ต้องการสแกน")
        if folder:
            clean_folder = os.path.abspath(folder)
            if is_system_path(clean_folder):
                QMessageBox.warning(
                    self,
                    "⚠️ แจ้งเตือนโฟลเดอร์สำคัญ",
                    f"โฟลเดอร์นี้เป็นโฟลเดอร์ระบบ/โปรแกรมสำคัญ:\n\n'{clean_folder}'\n\n"
                    "ระบบปฏิเสธการสแกนเพื่อป้องกันการลบไฟล์ระบบเสียหาย!"
                )
                self.btn_scan.setEnabled(False)
                return
            
            self.target_folder = clean_folder
            self.lbl_path.setText(f"โฟลเดอร์: {self.target_folder}")
            self.btn_scan.setEnabled(True)

    def select_c_drive(self):
        c_path = os.path.abspath("C:\\")
        if os.path.exists(c_path):
            self.target_folder = c_path
            self.lbl_path.setText(f"สแกนทั้งเครื่อง: {self.target_folder}")
            self.btn_scan.setEnabled(True)

    def start_scan(self):
        if not self.target_folder or not os.path.exists(self.target_folder):
            QMessageBox.warning(self, "แจ้งเตือน", "กรุณาเลือกโฟลเดอร์ที่ถูกต้องก่อน")
            return

        if is_system_path(self.target_folder) and self.target_folder.lower() != "c:\\":
            QMessageBox.warning(
                self,
                "⚠️ แจ้งเตือนโฟลเดอร์สำคัญ",
                f"โฟลเดอร์นี้เป็นโฟลเดอร์ระบบ/โปรแกรมสำคัญ:\n\n'{self.target_folder}'\n\n"
                "ระบบจะข้ามการสแกนเพื่อป้องกันความเสียหายต่อระบบปฏิบัติการ"
            )
            return

        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree.blockSignals(False)

        self.lbl_preview_image.clear()
        self.lbl_preview_image.setText("เลือกไฟล์รูปหรือเอกสารเพื่อดูตัวอย่าง")
        self.lbl_preview_info.setText("ยังไม่ได้เลือกไฟล์")

        self.btn_scan.setEnabled(False)
        self.btn_select.setEnabled(False)
        self.btn_scan_c.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_card_status.setText("⌛ กำลังสแกน...")
        self.lbl_card_total_dup.setText("📊 ไฟล์ซ้ำทั้งหมด: 0 / 0 ไฟล์")
        self.lbl_card_deletable.setText("🗑️ ไฟล์ที่สามารถลบได้: 0 ไฟล์")
        self.lbl_card_time.setText("⏳ เหลือเวลาอีกประมาณ: คำนวณ...")

        self.scan_start_time = time.time()
        self.last_total_scanned = 0

        self.scanner_thread = ScannerThread(self.target_folder)
        self.scanner_thread.progress_signal.connect(self.update_status_signal)
        self.scanner_thread.progress_percent.connect(self.update_progress)
        self.scanner_thread.time_signal.connect(self.update_time)
        self.scanner_thread.finished_signal.connect(self.display_results)
        self.scanner_thread.start()

    def update_status_signal(self, text):
        self.lbl_status_line.setText(text)

    def update_progress(self, current, total):
        self.last_total_scanned = total
        if total > 0:
            val = int((current / total) * 100)
            self.progress_bar.setValue(val)
            self.progress_bar.setFormat(f"{val}% ({current:,}/{total:,})")

            if self.scan_start_time and current > 5:
                elapsed = time.time() - self.scan_start_time
                speed = current / elapsed
                if speed > 0 and total > current:
                    remaining_files = total - current
                    remaining_sec = int(remaining_files / speed)
                    
                    mins, secs = divmod(remaining_sec, 60)
                    if mins > 0:
                        self.lbl_card_time.setText(f"⏳ เหลือเวลาอีกประมาณ: {mins} นาที {secs} วิ")
                    else:
                        self.lbl_card_time.setText(f"⏳ เหลือเวลาอีกประมาณ: {secs} วิ")

    def update_time(self, elapsed, eta):
        pass

    def update_preview(self, item, column):
        if item.parent() is not None:
            filepath = item.text(1)
            clean_path = filepath.replace("🔒 [ ไฟล์ระบบ Windows ] ", "").replace("🔒 [ไฟล์ระบบ - ป้องกันการลบ] ", "").strip()

            if os.path.exists(clean_path):
                ext = os.path.splitext(clean_path)[1].lower()
                size_str = item.text(2)
                
                info_text = (
                    f"<b>ชื่อไฟล์:</b> {os.path.basename(clean_path)}<br>"
                    f"<b>ขนาด:</b> {size_str}<br>"
                    f"<b>Path:</b> {clean_path}<br>"
                    f"<i>🔍 (คลิกซ้ายที่รูป preview ด้านบนเพื่อขยายใหญ่)</i>"
                )

                # ตัวแปรเก็บ QPixmap สำหรับนำไปขยาย
                current_pixmap = None

                # 1. ไฟล์รูปภาพ
                if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif']:
                    reader = QImageReader(clean_path)
                    reader.setAutoTransform(True)
                    current_pixmap = QPixmap.fromImageReader(reader)
                    
                    if not current_pixmap.isNull():
                        scaled_pixmap = current_pixmap.scaled(
                            240, 200, 
                            Qt.AspectRatioMode.KeepAspectRatio, 
                            Qt.TransformationMode.SmoothTransformation
                        )
                        self.lbl_preview_image.setPixmap(scaled_pixmap)
                        self.lbl_preview_info.setText(info_text)
                    else:
                        self.lbl_preview_image.setText("🖼️ ไม่สามารถโหลดตัวอย่างภาพได้")
                        self.lbl_preview_info.setText(info_text)

                # 2. ไฟล์ PDF (แปลงเป็นรูปภาพหน้าจริง)
                elif ext == '.pdf':
                    try:
                        import fitz  # PyMuPDF
                        doc = fitz.open(clean_path)
                        if len(doc) > 0:
                            page = doc[0]  # ดึงหน้าแรก
                            pix = page.get_pixmap(dpi=200)  # เพิ่ม dpi เพื่อความคมชัดสูงขึ้นในการขยาย
                            
                            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                            current_pixmap = QPixmap.fromImage(img)

                            scaled_pixmap = current_pixmap.scaled(
                                260, 220, 
                                Qt.AspectRatioMode.KeepAspectRatio, 
                                Qt.TransformationMode.SmoothTransformation
                            )
                            self.lbl_preview_image.setPixmap(scaled_pixmap)
                            self.lbl_preview_info.setText(f"{info_text}<br><b>จำนวนหน้า:</b> {len(doc)} หน้า")
                            doc.close()
                        else:
                            self.lbl_preview_image.setText("📕 เอกสาร PDF ไม่มีหน้าเนื้อหา")
                            self.lbl_preview_info.setText(info_text)

                    except Exception as e:
                        self.lbl_preview_image.setText(f"📕 ไม่สามารถแปลง PDF เป็นภาพได้:\n{str(e)}")
                        self.lbl_preview_info.setText(info_text)

              # 3. ไฟล์ Word (.docx) -> อ่าน Text เนื้อหาพรีวิวอย่างมีเสถียรภาพ
                elif ext == '.docx':
                    if docx:
                        try:
                            doc_obj = docx.Document(clean_path)
                            paragraphs = [p.text.strip() for p in doc_obj.paragraphs if p.text.strip()]
                            text_preview = "\n".join(paragraphs[:18]).strip()
                            if text_preview:
                                self.lbl_preview_image.setText(f"📘 ตัวอย่างเนื้อหาเอกสาร Word:\n\n{text_preview}")
                            else:
                                self.lbl_preview_image.setText("📘 เอกสาร Word ว่างเปล่า หรือไม่มีข้อความ")
                        except Exception as e:
                            self.lbl_preview_image.setText(f"📘 ไม่สามารถอ่านข้อความจากเอกสารได้:\n{str(e)}")
                    else:
                        self.lbl_preview_image.setText("📘 ไม่พบโมดูล python-docx สำหรับอ่านเอกสาร")
                    
                    self.lbl_preview_info.setText(info_text.replace("🔍 (คลิกซ้ายที่รูป preview ด้านบนเพื่อขยายใหญ่)", ""))
                # 4. ไฟล์ประเภทข้อความทั่วไป (Text/Code/CSV/Log)
                elif ext in ['.txt', '.csv', '.log', '.json', '.xml', '.py', '.md', '.html', '.ini', '.bat']:
                    try:
                        with open(clean_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content_preview = f.read(500)
                        self.lbl_preview_image.setText(f"📄 ตัวอย่างข้อความ:\n\n{content_preview}...")
                        self.lbl_preview_info.setText(info_text.replace("🔍 (คลิกซ้ายที่รูป preview ด้านบนเพื่อขยายใหญ่)", ""))
                    except Exception:
                        self.lbl_preview_image.setText("📄 ไม่สามารถอ่านเนื้อหาไฟล์นี้ได้")
                        self.lbl_preview_info.setText(info_text)

                # 5. ไฟล์ประเภทอื่น
                else:
                    self.lbl_preview_image.setText(f"📦 ไฟล์ประเภท {ext.upper()}\n(ไม่รองรับการแสดงตัวอย่างเนื้อหา)")
                    self.lbl_preview_info.setText(info_text.replace("🔍 (คลิกซ้ายที่รูป preview ด้านบนเพื่อขยายใหญ่)", ""))

                # ผูกกิจกรรมคลิกรูปเพื่อเปิด Pop-up ขยายภาพ (เฉพาะเมื่อมีรูปภาพ/PDF)
                if current_pixmap and not current_pixmap.isNull():
                    self.lbl_preview_image.setCursor(Qt.CursorShape.PointingHandCursor)
                    self.lbl_preview_image.mousePressEvent = lambda event: self.open_zoom_dialog(current_pixmap, os.path.basename(clean_path))
                else:
                    self.lbl_preview_image.setCursor(Qt.CursorShape.ArrowCursor)
                    self.lbl_preview_image.mousePressEvent = None

    def open_zoom_dialog(self, pixmap, filename):
        """ฟังก์ชันเปิดหน้าต่างขยายภาพ"""
        dialog = ImageZoomDialog(pixmap, title=f"ตัวอย่างขยายใหญ่ - {filename}", parent=self)
        dialog.exec()

    def format_size(self, size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    def display_results(self, duplicates):
        self.tree.blockSignals(True)
        self.tree.clear()
        
        self.btn_scan.setEnabled(True)
        self.btn_select.setEnabled(True)
        self.btn_scan_c.setEnabled(True)
        
        if self.scan_start_time:
            total_elapsed = int(time.time() - self.scan_start_time)
            self.lbl_card_time.setText(f"⏱️ สแกนเสร็จใน: {total_elapsed} วิ")

        total_scanned = self.last_total_scanned

        if not duplicates:
            self.lbl_card_status.setText("🎉 ไม่พบรายการซ้ำแล้ว!")
            self.lbl_status_line.setText("ไม่พบไฟล์ซ้ำในพื้นที่นี้")
            self.lbl_card_save.setText("♻️ พื้นที่ประหยัดได้: 0.00 B")
            self.lbl_card_total_dup.setText(f"📊 ไฟล์ซ้ำทั้งหมด: 0 / {total_scanned:,} ไฟล์")
            self.lbl_card_deletable.setText("🗑️ ไฟล์ที่สามารถลบได้: 0 ไฟล์")
            self.btn_delete.setEnabled(False)
            self.tree.blockSignals(False)
            QMessageBox.information(self, "ผลการสแกน", "ไม่พบไฟล์ซ้ำในพื้นที่ที่เลือก")
            return

        total_duplicate_files = sum(len(files) for files in duplicates.values())
        total_groups = len(duplicates)

        self.lbl_card_status.setText("🎉 สแกนเสร็จแล้ว!")
        self.lbl_card_total_dup.setText(f"📊 ไฟล์ซ้ำทั้งหมด: {total_duplicate_files:,} / {total_scanned:,} ไฟล์")
        self.lbl_status_line.setText(f"สแกนเสร็จสิ้น! พบไฟล์ซ้ำ {total_duplicate_files:,} ไฟล์ จากไฟล์ทั้งหมด {total_scanned:,} ไฟล์ (ใน {total_groups:,} กลุ่ม)")
        self.progress_bar.setValue(100)
        self.btn_delete.setEnabled(True)

        # ----------------------------------------------------------------------
        # ตรรกะการเรียงลำดับกลุ่ม: ให้กลุ่มที่มีไฟล์ "เลือกลบได้" ขึ้นมาก่อนกลุ่มที่ถูกล็อคทั้งหมด
        # ----------------------------------------------------------------------
        def group_has_deletable_files(item_tuple):
            hash_val, files = item_tuple
            for filepath, size in files:
                is_protected, _ = self.is_protected_file(filepath)
                if not is_protected:
                    return True  # มีไฟล์ลบได้
            return False  # ติดล็อคทั้งกลุ่ม

        # แยกเป็น 2 ลิสต์: ลิสต์ที่ลบได้ขึ้นก่อน ลิสต์ที่ลบไม่ได้ไว้ทีหลัง
        sorted_duplicates = sorted(
            duplicates.items(),
            key=lambda item: group_has_deletable_files(item),
            reverse=True
        )

        group_count = 1
        for hash_val, files in sorted_duplicates:
            group_item = QTreeWidgetItem(self.tree)
            group_item.setText(0, f"กลุ่มที่ {group_count}")
            group_item.setText(1, f"พบซ้ำ {len(files)} ไฟล์ (ขนาดไฟล์ละ {self.format_size(files[0][1])})")
            group_item.setExpanded(True)
            group_count += 1

            for idx, (filepath, size) in enumerate(files):
                child = QTreeWidgetItem(group_item)
                child.setText(0, "")
                child.setText(1, filepath)
                child.setText(2, self.format_size(size))
                child.setData(2, Qt.ItemDataRole.UserRole, size) 
                
                is_protected, reason = self.is_protected_file(filepath)
                if is_protected:
                    child.setText(3, f"🛡️ ป้องกัน [ {reason} ]")
                    child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                else:
                    child.setText(3, f"🔹 Hash: {hash_val[:12]}...")
                    child.setFlags(
                        Qt.ItemFlag.ItemIsSelectable | 
                        Qt.ItemFlag.ItemIsUserCheckable | 
                        Qt.ItemFlag.ItemIsEnabled
                    )
                    
                    if idx > 0:
                        child.setCheckState(1, Qt.CheckState.Checked)
                    else:
                        child.setCheckState(1, Qt.CheckState.Unchecked)

        self.tree.blockSignals(False)
        self.update_savable_space()

    def on_item_changed(self, item, column):
        if column == 1:
            self.update_savable_space()

    def update_savable_space(self):
        total_savable_bytes = 0
        selected_file_count = 0
        root = self.tree.invisibleRootItem()
        
        for i in range(root.childCount()):
            group = root.child(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.checkState(1) == Qt.CheckState.Checked:
                    selected_file_count += 1
                    size = child.data(2, Qt.ItemDataRole.UserRole)
                    if size:
                        total_savable_bytes += size
                        
        self.lbl_card_deletable.setText(f"🗑️ ไฟล์ที่สามารถลบได้: {selected_file_count:,} ไฟล์")
        self.lbl_card_save.setText(f"♻️ พื้นที่ประหยัดได้: {self.format_size(total_savable_bytes)}")

    def is_protected_file(self, filepath):
        if is_system_path(filepath):
            return True, "ไฟล์ระบบ Windows"

        norm = os.path.normpath(filepath).lower()
        critical_extensions = ['.dll', '.sys', '.drv', '.efi', '.exe', '.com', '.bat', '.cmd']
        if os.path.splitext(norm)[1] in critical_extensions:
            return True, "ไฟล์โปรแกรมระบบ"

        return False, ""

    def delete_selected(self):
        root = self.tree.invisibleRootItem()
        items_to_delete = []
        blocked_files = []

        for i in range(root.childCount()):
            group = root.child(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.checkState(1) == Qt.CheckState.Checked:
                    filepath = child.text(1)
                    is_protected, _ = self.is_protected_file(filepath)
                    if not is_protected:
                        items_to_delete.append((child, filepath))
                    else:
                        blocked_files.append(filepath)

        if blocked_files:
            QMessageBox.critical(
                self,
                "⛔ ปฏิเสธการลบไฟล์ระบบ",
                f"พบไฟล์ในโฟลเดอร์ระบบ/โปรแกรมจำนวน {len(blocked_files)} ไฟล์\n"
                "ระบบปฏิเสธการลบเพื่อความปลอดภัยของเครื่อง"
            )

        if not items_to_delete:
            if not blocked_files:
                QMessageBox.warning(self, "แจ้งเตือน", "กรุณาเลือกไฟล์ที่ต้องการลบอย่างน้อย 1 ไฟล์")
            return

        total_files = len(items_to_delete)

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("ยืนยันการลบลง Recycle Bin")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText(
            f"ต้องการย้ายไฟล์จำนวน {total_files:,} รายการ ไปยัง Recycle Bin หรือไม่?"
        )
        msg_box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        confirm = msg_box.exec()

        if confirm == QMessageBox.StandardButton.Yes:
            if os.path.exists(self.backup_dir):
                shutil.rmtree(self.backup_dir, ignore_errors=True)
            os.makedirs(self.backup_dir, exist_ok=True)

            self.progress_dialog = QProgressDialog("กำลังย้ายไฟล์ลง Recycle Bin...", "ยกเลิก", 0, total_files, self)
            self.progress_dialog.setWindowTitle("กำลังดำเนินการลบไฟล์")
            self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self.progress_dialog.setMinimumDuration(0)
            self.progress_dialog.setValue(0)

            self.delete_thread = DeleteWorkerThread(items_to_delete, self.backup_dir, self.db)
            self.delete_thread.progress_signal.connect(self.on_delete_progress)
            self.delete_thread.finished_signal.connect(self.on_delete_finished)
            
            self.progress_dialog.canceled.connect(self.delete_thread.cancel)
            self.delete_thread.start()

    def on_delete_progress(self, current_idx, current_path, success, err):
        if hasattr(self, 'progress_dialog') and self.progress_dialog:
            self.progress_dialog.setValue(current_idx)
            short_path = current_path if len(current_path) < 60 else "..." + current_path[-57:]
            self.progress_dialog.setLabelText(f"♻️ กำลังย้ายลง Recycle Bin ({current_idx + 1:,}):\n{short_path}")

    def on_delete_finished(self, deleted_history, failed_files):
        if hasattr(self, 'progress_dialog') and self.progress_dialog:
            self.progress_dialog.close()

        self.tree.blockSignals(True)

        for item_data in deleted_history:
            item = item_data.get("item_ref")
            if item:
                parent = item.parent()
                if parent:
                    parent.removeChild(item)

        root = self.tree.invisibleRootItem()
        i = 0
        while i < root.childCount():
            group = root.child(i)
            if group.childCount() <= 1:
                root.removeChild(group)
            else:
                i += 1

        remaining_files_count = 0
        for i in range(root.childCount()):
            remaining_files_count += root.child(i).childCount()

        self.tree.blockSignals(False)

        total_scanned = self.last_total_scanned
        self.lbl_card_total_dup.setText(f"📊 ไฟล์ซ้ำทั้งหมด: {remaining_files_count:,} / {total_scanned:,} ไฟล์")

        if root.childCount() == 0:
            self.lbl_card_status.setText("🎉 ไม่พบรายการซ้ำแล้ว!")
            self.lbl_status_line.setText("ย้ายไฟล์ซ้ำลง Recycle Bin หมดแล้ว")
            self.lbl_card_save.setText("♻️ พื้นที่ประหยัดได้: 0.00 B")
            self.lbl_card_deletable.setText("🗑️ ไฟล์ที่สามารถลบได้: 0 ไฟล์")
            self.btn_delete.setEnabled(False)
        else:
            self.update_savable_space()

        if deleted_history:
            clean_history = [
                {
                    "original_path": h["original_path"],
                    "backup_path": h["backup_path"],
                    "deleted_at": h["deleted_at"]
                }
                for h in deleted_history
            ]
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(clean_history, f, ensure_ascii=False, indent=2)
            self.btn_undo.setEnabled(True)

        if failed_files and deleted_history:
            QMessageBox.warning(
                self, "แจ้งเตือน",
                f"ย้ายลง Recycle Bin สำเร็จ {len(deleted_history):,} รายการ\nแต่ย้ายไม่สำเร็จ {len(failed_files):,} รายการ"
            )
        elif failed_files and not deleted_history:
            error_msg = "\n".join([f"- {p}: {err}" for p, err in failed_files[:3]])
            QMessageBox.critical(
                self, "ย้ายไฟล์ไม่สำเร็จ",
                f"ไม่สามารถย้ายไฟล์ลง Recycle Bin ได้\n\nข้อผิดพลาด:\n{error_msg}"
            )
        else:
            if root.childCount() == 0:
                QMessageBox.information(
                    self, "สำเร็จ", 
                    "ย้ายไฟล์ซ้ำไปยัง Recycle Bin เรียบร้อยแล้ว!"
                )
            else:
                QMessageBox.information(
                    self, "สำเร็จ", 
                    f"ย้ายไฟล์ที่เลือกจำนวน {len(deleted_history):,} รายการ ไปยัง Recycle Bin เรียบร้อยแล้ว"
                )

    def restore_last_deleted(self):
        if not os.path.exists(self.history_file):
            QMessageBox.warning(self, "แจ้งเตือน", "ไม่พบประวัติไฟล์ที่กู้คืนได้")
            return

        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                history = json.load(f)

            if not history:
                QMessageBox.warning(self, "แจ้งเตือน", "ไม่มีรายการไฟล์ที่สามารถกู้คืนได้")
                return

            restored_count = 0
            for item in history:
                orig_path = item["original_path"]
                backup_path = item.get("backup_path", "")

                if backup_path and os.path.exists(backup_path):
                    os.makedirs(os.path.dirname(orig_path), exist_ok=True)
                    shutil.copy2(backup_path, orig_path)
                    restored_count += 1

            if os.path.exists(self.history_file):
                os.remove(self.history_file)
            if os.path.exists(self.backup_dir):
                shutil.rmtree(self.backup_dir, ignore_errors=True)

            # สั่ง Windows API ให้ทำการล้างไฟล์ออกจาก Recycle Bin อัตโนมัติหลังกู้คืนเสร็จ
            try:
                # Flag 7 = SHERB_NOCONFIRMATION (1) | SHERB_NOPROGRESSUI (2) | SHERB_NOSOUND (4)
                ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
            except Exception:
                pass

            self.btn_undo.setEnabled(False)

            QMessageBox.information(
                self, 
                "สำเร็จ", 
                f"กู้คืนไฟล์เรียบร้อยแล้วจำนวน {restored_count:,} รายการ\n(และทำการล้างไฟล์ขยะใน Recycle Bin ให้เรียบร้อยแล้ว)"
            )

            if self.target_folder:
                self.start_scan()

        except Exception as e:
            QMessageBox.critical(self, "ข้อผิดพลาด", f"เกิดปัญหาในการกู้คืนไฟล์: {str(e)}")

    def closeEvent(self, event):
        if os.path.exists(self.checkmark_path):
            try:
                os.remove(self.checkmark_path)
            except Exception:
                pass
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DuplicateCleanerUI()
    window.show()
    sys.exit(app.exec())