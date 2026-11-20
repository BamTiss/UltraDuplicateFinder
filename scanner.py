import os
import time
import shutil
import xxhash
from blake3 import blake3
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtCore import QThread, pyqtSignal
from db import DatabaseManager

class ScannerThread(QThread):
    progress_signal = pyqtSignal(str)
    progress_percent = pyqtSignal(int, int)  # ส่งเป็นเปอร์เซ็นต์ (0 - 100) เสมอ
    time_signal = pyqtSignal(float, float)
    stats_signal = pyqtSignal(int, int, int)  # (ทั้งหมดรวมระบบ, สแกนจริง, ข้ามระบบ)
    finished_signal = pyqtSignal(dict)

    SKIP_FOLDERS = {
        '$recycle.bin', 'system volume information', 'appdata', 
        'windows', 'program files', 'program files (x86)', 'programdata',
        'recovery', 'perflogs', 'msocache', 'intel', 'amd', 'nvidia',
        'riot games', 'epic games', 'steamlibrary', 'steam', 'ubisoft',
        'node_modules', 'site-packages', '__pycache__', 'venv', '.venv', 'env',
        '.git', '.vs', '.vscode', '.idea', '.gradle', '.nuget', '.cargo',
        'jupyter-env', 'anaconda3', 'miniconda3'
    }

    SKIP_EXTENSIONS = {
        '.exe', '.dll', '.sys', '.drv', '.efi', '.com', '.bat', '.cmd',
        '.msi', '.msu', '.cab', '.ocx', '.cpl', '.scr', '.mui', '.cat',
        '.pak', '.vpk', '.wad', '.upk', '.uasset', '.bin', '.dat', '.tmp',
        '.lnk', '.url', '.ini', '.inf', '.reg', '.pyc', '.pyo', '.pyd'
    }

    def __init__(self, target_paths, db_path="file_cache.db"):
        super().__init__()
        if isinstance(target_paths, str):
            self.target_paths = [os.path.abspath(target_paths)]
        else:
            raw_paths = sorted(list({os.path.abspath(p) for p in target_paths if os.path.exists(p)}), key=len)
            filtered_paths = []
            for p in raw_paths:
                if not any(p.lower().startswith(parent.lower().rstrip('\\') + '\\') for parent in filtered_paths):
                    filtered_paths.append(p)
            self.target_paths = filtered_paths

        self.db = DatabaseManager(db_path)
        self.is_running = True
        self._last_ui_update = 0.0
        self._estimated_total_bytes = self._estimate_target_bytes()
        self._accumulated_bytes = 0
        self._last_emitted_pct = 0

    def _estimate_target_bytes(self):
        """ประเมินขนาดพื้นที่ใช้งานจริงของเป้าหมาย เพื่อคำนวณ % ช่วงเดินสำรวจ"""
        total_used = 0
        seen_drives = set()
        for p in self.target_paths:
            drive = os.path.splitdrive(p)[0].upper()
            if drive and drive not in seen_drives:
                seen_drives.add(drive)
                try:
                    usage = shutil.disk_usage(drive + "\\")
                    # ถ้าสแกนทั้งไดรฟ์ใช้ขนาดที่ใช้จริง ถ้าสแกนโฟลเดอร์ย่อยประเมินคร่าวๆ 25%
                    if len(p.rstrip("\\")) <= 2:
                        total_used += usage.used
                    else:
                        total_used += max(usage.used // 4, 1024 * 1024 * 1024)
                except Exception:
                    pass
        return max(total_used, 1024 * 1024 * 100)

    def _emit_pct(self, pct):
        """ส่งค่าเปอร์เซ็นต์ 0-100 แบบไม่ถอยหลัง"""
        clamped = max(self._last_emitted_pct, min(100, int(pct)))
        self._last_emitted_pct = clamped
        self.progress_percent.emit(clamped, 100)

    def _emit_live_discovery_ui(self, scanned_count, skipped_count, current_folder_name=""):
        """อัปเดตตัวเลขและเปอร์เซ็นต์ (ช่วง 1% - 35%) ทุก 0.12 วินาที"""
        now = time.time()
        if now - self._last_ui_update >= 0.12:
            self._last_ui_update = now
            total_all = scanned_count + skipped_count
            self.stats_signal.emit(total_all, scanned_count, skipped_count)

            # คำนวณ % ช่วงสำรวจ (1% ถึง 35%) จากสัดส่วนขนาดไฟล์ + จำนวนรายการที่เจอ
            byte_ratio = min(1.0, self._accumulated_bytes / self._estimated_total_bytes)
            count_ratio = min(1.0, total_all / 350000.0)
            discovery_pct = 1 + int(max(byte_ratio, count_ratio * 0.85) * 34)
            self._emit_pct(discovery_pct)

            short_name = current_folder_name if len(current_folder_name) <= 35 else "..." + current_folder_name[-32:]
            self.progress_signal.emit(
                f"🔎 กำลังสำรวจ [{short_name}] | พบทั้งหมด {total_all:,} ไฟล์ (สแกนจริง {scanned_count:,} / ข้ามระบบ {skipped_count:,})"
            )

    def count_skipped_dir_fast(self, dir_path, current_scanned, base_skipped):
        """นับจำนวนไฟล์ในโฟลเดอร์ระบบพร้อมอัปเดตเปอร์เซ็นต์ต่อเนื่อง"""
        count = 0
        stack = [dir_path]
        while stack:
            if not self.is_running:
                break
            curr = stack.pop()
            try:
                with os.scandir(curr) as entries:
                    for entry in entries:
                        if not self.is_running:
                            break
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            count += 1
                            # ประเมินขนาดเฉลี่ยไฟล์ระบบ ~150 KB ต่อไฟล์เพื่อดันเปอร์เซ็นต์
                            self._accumulated_bytes += 153600
                            if count % 400 == 0:
                                self._emit_live_discovery_ui(current_scanned, base_skipped + count, os.path.basename(curr))
            except Exception:
                continue
        return count

    def run(self):
        try:
            start_time = time.time()
            self._last_emitted_pct = 0
            self._emit_pct(1)
            self.progress_signal.emit("⚡ กำลังเริ่มต้นสำรวจโครงสร้างไฟล์ทั้งระบบ...")

            size_map = {}
            found_paths_set = set()
            scanned_count = 0
            skipped_system_count = 0

            # ---------------- 1. เดินสำรวจไฟล์ (0% -> 35%) ----------------
            stack = list(self.target_paths)

            while stack:
                if not self.is_running:
                    return

                current_dir = stack.pop()
                dir_label = os.path.basename(current_dir) or current_dir
                self._emit_live_discovery_ui(scanned_count, skipped_system_count, dir_label)

                try:
                    with os.scandir(current_dir) as entries:
                        for entry in entries:
                            if not self.is_running:
                                return

                            if entry.is_dir(follow_symlinks=False):
                                folder_lower = entry.name.lower()
                                if folder_lower not in self.SKIP_FOLDERS and not folder_lower.startswith('.'):
                                    stack.append(entry.path)
                                else:
                                    skipped_system_count += self.count_skipped_dir_fast(
                                        entry.path, scanned_count, skipped_system_count
                                    )
                                    self._emit_live_discovery_ui(scanned_count, skipped_system_count, entry.name)
                            
                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].lower()
                                if ext in self.SKIP_EXTENSIONS:
                                    skipped_system_count += 1
                                    self._accumulated_bytes += 153600
                                    self._emit_live_discovery_ui(scanned_count, skipped_system_count, dir_label)
                                    continue

                                scanned_count += 1
                                full_path = entry.path
                                found_paths_set.add(full_path)

                                try:
                                    stat = entry.stat(follow_symlinks=False)
                                    size = stat.st_size
                                    mtime = stat.st_mtime
                                    self._accumulated_bytes += size

                                    if size > 0:
                                        size_map.setdefault(size, []).append((full_path, mtime))
                                except Exception:
                                    pass

                                if scanned_count % 150 == 0:
                                    self._emit_live_discovery_ui(scanned_count, skipped_system_count, dir_label)
                except PermissionError:
                    continue
                except Exception:
                    continue

            total_all = scanned_count + skipped_system_count
            self.stats_signal.emit(total_all, scanned_count, skipped_system_count)
            self._emit_pct(35)

            # ---------------- 2. ปรับปรุงฐานข้อมูลและจับกลุ่มขนาด (35% -> 45%) ----------------
            self.progress_signal.emit(f"🧹 กำลังตรวจสอบและปรับปรุงดัชนีฐานข้อมูล ({scanned_count:,} ไฟล์)...")
            self.db.sync_and_clean_ghosts(self.target_paths, found_paths_set)
            self._emit_pct(39)

            self.progress_signal.emit("📊 กำลังคัดกรองไฟล์ที่มีขนาดตรงกัน...")
            candidate_files = []
            for size, items in size_map.items():
                if len(items) > 1:
                    for path, mtime in items:
                        candidate_files.append((path, size, mtime))

            self._emit_pct(42)

            total_candidates = len(candidate_files)
            if total_candidates == 0:
                self._emit_pct(100)
                self.finished_signal.emit({})
                return

            self.progress_signal.emit(f"🗄️ กำลังโหลดดัชนีแคชจาก SQLite เพื่อเทียบไฟล์ซ้ำ ({total_candidates:,} รายการ)...")
            cache_map = self.db.get_cached_files_map(self.target_paths)
            self._emit_pct(45)

            # ---------------- 3. PASS 1: ตรวจสอบแคช และคำนวณ xxHash64 (45% -> 75%) ----------------
            xxhash_groups = {}
            files_need_xxhash = []

            for path, size, mtime in candidate_files:
                cached = cache_map.get(path)
                if cached and cached[0] == size and cached[1] == mtime and cached[2]:
                    xxhash_groups.setdefault((size, cached[2]), []).append((path, size, mtime, cached[3]))
                else:
                    files_need_xxhash.append((path, size, mtime))

            if files_need_xxhash:
                total_p1 = len(files_need_xxhash)
                self.progress_signal.emit(f"⚡ Pass 1: กำลังคำนวณ xxHash64 ไฟล์ใหม่ 0 / {total_p1:,} ไฟล์...")
                max_workers = min(8, (os.cpu_count() or 4))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_file = {
                        executor.submit(self.compute_xxhash, path): (path, size, mtime)
                        for path, size, mtime in files_need_xxhash
                    }
                    done_p1 = 0
                    last_p1_emit = 0.0
                    for future in as_completed(future_to_file):
                        if not self.is_running:
                            executor.shutdown(wait=False, cancel_futures=True)
                            return
                        path, size, mtime = future_to_file[future]
                        done_p1 += 1
                        xx_val = future.result()
                        if xx_val:
                            xxhash_groups.setdefault((size, xx_val), []).append((path, size, mtime, None))

                        now = time.time()
                        if now - last_p1_emit >= 0.08 or done_p1 == total_p1:
                            last_p1_emit = now
                            pct_p1 = 45 + int((done_p1 / total_p1) * 30)
                            self._emit_pct(pct_p1)
                            self.progress_signal.emit(
                                f"⚡ Pass 1 (xxHash64): กำลังคำนวณ {done_p1:,} / {total_p1:,} ไฟล์ ({os.path.basename(path)})"
                            )
            else:
                self._emit_pct(75)

            # ---------------- 4. PASS 2: คำนวณ BLAKE3 (75% -> 95%) ----------------
            files_need_blake3 = []
            db_records_batch = []

            for (size, xx_val), items in xxhash_groups.items():
                if len(items) > 1:
                    for path, sz, mtime, cached_b3 in items:
                        if not cached_b3:
                            files_need_blake3.append((path, sz, mtime, xx_val))
                else:
                    path, sz, mtime, cached_b3 = items[0]
                    if cached_b3 is None:
                        db_records_batch.append((path, sz, mtime, xx_val, ""))

            if files_need_blake3:
                total_p2 = len(files_need_blake3)
                self.progress_signal.emit(f"🔐 Pass 2: ยืนยันความถูกต้องด้วย BLAKE3 จำนวน 0 / {total_p2:,} ไฟล์...")
                max_workers = min(8, (os.cpu_count() or 4))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_b3 = {
                        executor.submit(self.compute_blake3, path): (path, sz, mtime, xx_val)
                        for path, sz, mtime, xx_val in files_need_blake3
                    }
                    done_p2 = 0
                    last_p2_emit = 0.0
                    for future in as_completed(future_to_b3):
                        if not self.is_running:
                            executor.shutdown(wait=False, cancel_futures=True)
                            return
                        path, sz, mtime, xx_val = future_to_b3[future]
                        done_p2 += 1
                        b3_val = future.result()
                        if b3_val:
                            db_records_batch.append((path, sz, mtime, xx_val, b3_val))

                        now = time.time()
                        if now - last_p2_emit >= 0.08 or done_p2 == total_p2:
                            last_p2_emit = now
                            pct_p2 = 75 + int((done_p2 / total_p2) * 20)
                            self._emit_pct(pct_p2)
                            self.progress_signal.emit(
                                f"🔐 Pass 2 (BLAKE3): กำลังตรวจสอบ {done_p2:,} / {total_p2:,} ไฟล์ ({os.path.basename(path)})"
                            )
            else:
                self._emit_pct(95)

            # ---------------- 5. บันทึกลงฐานข้อมูลและสรุปผล (95% -> 100%) ----------------
            if db_records_batch:
                self.progress_signal.emit(f"💾 กำลังบันทึกดัชนีไฟล์ลงฐานข้อมูล ({len(db_records_batch):,} รายการ)...")
                self.db.bulk_upsert_files(db_records_batch)
            self._emit_pct(98)

            self.progress_signal.emit("📋 กำลังจัดกลุ่มรายการไฟล์ซ้ำเพื่อแสดงผล...")
            duplicates = self.db.get_duplicates(self.target_paths)
            
            result_map = {}
            for path, size, b3 in duplicates:
                if os.path.exists(path):
                    result_map.setdefault(b3, []).append((path, size))

            final_duplicates = {k: v for k, v in result_map.items() if len(v) > 1}
            self._emit_pct(100)
            self.finished_signal.emit(final_duplicates)

        except Exception as e:
            print(f"Error in ScannerThread: {e}")
            self.finished_signal.emit({})

    def compute_xxhash(self, filepath):
        try:
            x = xxhash.xxh64()
            with open(filepath, 'rb') as f:
                while chunk := f.read(131072):
                    x.update(chunk)
            return x.hexdigest()
        except Exception:
            return None

    def compute_blake3(self, filepath):
        try:
            b = blake3()
            with open(filepath, 'rb') as f:
                while chunk := f.read(131072):
                    b.update(chunk)
            return b.hexdigest()
        except Exception:
            return None

    def stop(self):
        self.is_running = False