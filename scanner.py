import os
import time
import xxhash
from blake3 import blake3
from PyQt6.QtCore import QThread, pyqtSignal
from db import DatabaseManager

class ScannerThread(QThread):
    progress_signal = pyqtSignal(str)
    progress_percent = pyqtSignal(int, int)
    time_signal = pyqtSignal(float, float)
    finished_signal = pyqtSignal(dict)

    SKIP_FOLDERS = {
        '$recycle.bin', 'system volume information', 'appdata', 
        'windows', 'programdata', 'node_modules', '.git', '.vs'
    }

    def __init__(self, folder_path, db_path="file_cache.db"):
        super().__init__()
        self.folder_path = os.path.abspath(folder_path)
        self.db = DatabaseManager(db_path)
        self.is_running = True

    def calculate_eta(self, start_time, current_idx, total_count):
        if current_idx == 0:
            return 0.0
        elapsed = time.time() - start_time
        avg_time = elapsed / current_idx
        remaining = total_count - current_idx
        return avg_time * remaining

    def run(self):
        try:
            start_time = time.time()
            self.progress_signal.emit(f"⚡ เริ่มการสแกนความเร็วสูง (Incremental Scan) ใน: {self.folder_path}")

            size_map = {}
            found_paths_set = set()
            scanned_count = 0
            new_or_changed_count = 0

            # ---------------- 1. สแกนหาไฟล์ และคัดกรองไฟล์ใหม่/ไฟล์แก้ไข ----------------
            stack = [self.folder_path]

            while stack:
                if not self.is_running:
                    return

                current_dir = stack.pop()
                try:
                    with os.scandir(current_dir) as entries:
                        for entry in entries:
                            if not self.is_running:
                                return

                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() not in self.SKIP_FOLDERS and not entry.name.startswith('.'):
                                    stack.append(entry.path)
                            
                            elif entry.is_file(follow_symlinks=False):
                                scanned_count += 1
                                full_path = entry.path
                                found_paths_set.add(full_path)

                                if scanned_count % 1000 == 0:
                                    elapsed = time.time() - start_time
                                    self.progress_signal.emit(f"🔎 ตรวจสอบโครงสร้างดิสก์แล้ว {scanned_count:,} ไฟล์...")
                                    self.time_signal.emit(elapsed, 0)

                                try:
                                    stat = entry.stat(follow_symlinks=False)
                                    size = stat.st_size
                                    mtime = stat.st_mtime

                                    if size > 0:
                                        size_map.setdefault(size, []).append((full_path, mtime))
                                except Exception:
                                    continue
                except PermissionError:
                    continue
                except Exception:
                    continue

            # ---------------- 2. ล้างข้อมูลไฟล์ขยะที่หายไปออกจาก DB ----------------
            if hasattr(self.db, 'sync_and_clean_ghosts'):
                self.db.sync_and_clean_ghosts(self.folder_path, found_paths_set)

            # ---------------- 3. ดึงเฉพาะกลุ่มที่มีขนาดซ้ำกันมาพิจารณา ----------------
            candidate_files = []
            for size, items in size_map.items():
                if len(items) > 1:
                    for path, mtime in items:
                        candidate_files.append((path, size, mtime))

            total_candidates = len(candidate_files)
            
            if total_candidates == 0:
                self.finished_signal.emit({})
                return

            # ---------------- 4. Incremental Process (ประมวลผลเฉพาะไฟล์ใหม่) ----------------
            processed_count = 0
            for path, size, mtime in candidate_files:
                if not self.is_running:
                    return

                processed_count += 1
                
                # เช็กว่าไฟล์นี้มีข้อมูลอยู่แล้วและยังไม่โดนแก้ไขใช่ไหม?
                cached = self.db.get_cached_file(path, size, mtime)
                xx_val = cached[0] if cached and cached[0] else None
                b3_val = cached[1] if cached and cached[1] else None

                # ถ้ายังไม่มี Hash ใน DB -> คำนวณใหม่เฉพาะไฟล์นี้!
                if not xx_val or not b3_val:
                    new_or_changed_count += 1
                    
                    elapsed = time.time() - start_time
                    eta = self.calculate_eta(start_time, processed_count, total_candidates)
                    self.progress_signal.emit(f"⚡ คำนวณไฟล์ใหม่/แก้ไข ({new_or_changed_count}): {os.path.basename(path)}")
                    self.progress_percent.emit(processed_count, total_candidates)
                    self.time_signal.emit(elapsed, eta)

                    if not xx_val:
                        xx_val = self.compute_xxhash(path)
                    if xx_val and not b3_val:
                        b3_val = self.compute_blake3(path)

                    if xx_val and b3_val:
                        self.db.upsert_file(path, size, mtime, xx_val, b3_val)
                else:
                    # ถ้ามีใน DB แล้ว -> ข้ามไปอย่างรวดเร็ว (ไม่ต้องอ่านไฟล์จากดิสก์)
                    if processed_count % 500 == 0 or processed_count == total_candidates:
                        elapsed = time.time() - start_time
                        self.progress_signal.emit(f"⏩ ใช้ดัชนีเดิมแบบด่วน ({processed_count}/{total_candidates})...")
                        self.progress_percent.emit(processed_count, total_candidates)
                        self.time_signal.emit(elapsed, 0)

            # ---------------- 5. ดึงผลลัพธ์ไฟล์ซ้ำและตรวจสอบก่อนส่งออก ----------------
            duplicates = self.db.get_duplicates(self.folder_path)
            
            result_map = {}
            for path, size, b3 in duplicates:
                if os.path.exists(path):
                    result_map.setdefault(b3, []).append((path, size))

            final_duplicates = {k: v for k, v in result_map.items() if len(v) > 1}
            self.finished_signal.emit(final_duplicates)

        except Exception as e:
            print(f"Error in ScannerThread: {e}")
            self.finished_signal.emit({})

    def compute_xxhash(self, filepath):
        try:
            x = xxhash.xxh64()
            with open(filepath, 'rb') as f:
                while chunk := f.read(65536):
                    x.update(chunk)
            return x.hexdigest()
        except Exception:
            return None

    def compute_blake3(self, filepath):
        try:
            b = blake3()
            with open(filepath, 'rb') as f:
                while chunk := f.read(65536):
                    b.update(chunk)
            return b.hexdigest()
        except Exception:
            return None

    def stop(self):
        self.is_running = False