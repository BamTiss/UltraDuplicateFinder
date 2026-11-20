import sqlite3
import os

class DatabaseManager:
    def __init__(self, db_filename="file_cache.db"):
        app_data_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'UltraDuplicateFinder')
        os.makedirs(app_data_dir, exist_ok=True)
        
        self.db_path = os.path.join(app_data_dir, db_filename)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        # เปิดโหมด WAL และปรับแต่งความเร็วเขียนข้อมูลระดับสูง
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        conn.execute('PRAGMA cache_size=-64000;')  # ใช้ RAM Cache 64MB
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    filepath TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime REAL,
                    xxhash TEXT,
                    blake3 TEXT
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_blake3 ON files(blake3)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_size ON files(size)')
            conn.commit()

    def is_file_unchanged(self, filepath, size, mtime):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT 1 FROM files 
                    WHERE filepath = ? AND size = ? AND mtime = ? AND blake3 IS NOT NULL
                ''', (filepath, size, mtime))
                return cursor.fetchone() is not None
        except Exception:
            return False

    def sync_and_clean_ghosts(self, target_folders, active_paths_set):
        """ล้างข้อมูลไฟล์ที่ถูกลบ/เปลี่ยนชื่อไปแล้วออกจาก DB (รองรับหลายโฟลเดอร์/ไดรฟ์)"""
        try:
            if isinstance(target_folders, str):
                target_folders = [target_folders]

            with self.get_connection() as conn:
                cursor = conn.cursor()
                ghost_paths = []

                for folder in target_folders:
                    norm_folder = os.path.abspath(folder).lower()
                    if norm_folder.endswith(":\\") or norm_folder.endswith(":\\/"):
                        search_pattern = norm_folder + "%"
                    else:
                        search_pattern = norm_folder + os.sep + "%"

                    cursor.execute('SELECT filepath FROM files WHERE LOWER(filepath) LIKE ?', (search_pattern,))
                    db_rows = cursor.fetchall()
                    ghost_paths.extend([(path,) for (path,) in db_rows if path not in active_paths_set])

                if ghost_paths:
                    cursor.executemany('DELETE FROM files WHERE filepath = ?', ghost_paths)
                    conn.commit()
                    print(f"🧹 [DB Sync] ล้างดัชนีไฟล์เก่าออกแล้ว {len(ghost_paths)} รายการ")
        except Exception as e:
            print(f"Error in sync_and_clean_ghosts: {e}")

    def get_cached_files_map(self, target_folders):
        """ดึงแคชทั้งหมดของพื้นที่เป้าหมายขึ้นมาไว้ใน RAM ครั้งเดียว (เร็วกว่า Query ทีละไฟล์ 100 เท่า)"""
        cache_map = {}
        try:
            if isinstance(target_folders, str):
                target_folders = [target_folders]

            with self.get_connection() as conn:
                cursor = conn.cursor()
                for folder in target_folders:
                    norm_folder = os.path.abspath(folder).lower()
                    if norm_folder.endswith(":\\") or norm_folder.endswith(":\\/"):
                        search_pattern = norm_folder + "%"
                    else:
                        search_pattern = norm_folder + os.sep + "%"

                    cursor.execute('''
                        SELECT filepath, size, mtime, xxhash, blake3 
                        FROM files 
                        WHERE LOWER(filepath) LIKE ?
                    ''', (search_pattern,))
                    for fp, sz, mt, xx, b3 in cursor.fetchall():
                        cache_map[fp] = (sz, mt, xx, b3)
        except Exception as e:
            print(f"Error loading cache map: {e}")
        return cache_map

    def bulk_upsert_files(self, records):
        """บันทึกข้อมูลลง SQLite แบบ Batch (ครั้งละหลายร้อย/พันรายการ) ลด Disk I/O"""
        if not records:
            return
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT INTO files (filepath, size, mtime, xxhash, blake3)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(filepath) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        xxhash = excluded.xxhash,
                        blake3 = excluded.blake3
                ''', records)
                conn.commit()
        except Exception as e:
            print(f"Error in bulk_upsert_files: {e}")

    def upsert_file(self, filepath, size, mtime, xxhash_val, blake3_val):
        self.bulk_upsert_files([(filepath, size, mtime, xxhash_val, blake3_val)])

    def remove_file(self, filepath):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
                conn.commit()
        except Exception:
            pass

    def get_duplicates(self, target_folders):
        """ดึงรายการไฟล์ซ้ำข้ามโฟลเดอร์/ไดรฟ์ทั้งหมดที่เลือก"""
        try:
            if isinstance(target_folders, str):
                target_folders = [target_folders]

            conditions = []
            params = []
            for folder in target_folders:
                norm_folder = os.path.abspath(folder).lower()
                if norm_folder.endswith(":\\") or norm_folder.endswith(":\\/"):
                    conditions.append("LOWER(filepath) LIKE ?")
                    params.append(norm_folder + "%")
                else:
                    conditions.append("LOWER(filepath) LIKE ?")
                    params.append(norm_folder + os.sep + "%")

            where_clause = " OR ".join(conditions)

            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = f'''
                    SELECT filepath, size, blake3 FROM files
                    WHERE ({where_clause})
                      AND blake3 IN (
                          SELECT blake3 FROM files 
                          WHERE ({where_clause})
                            AND blake3 IS NOT NULL 
                            AND blake3 != ''
                          GROUP BY blake3 
                          HAVING COUNT(*) > 1
                      )
                    ORDER BY blake3, size DESC
                '''
                cursor.execute(query, params + params)
                return cursor.fetchall()
        except Exception as e:
            print(f"Error fetching duplicates: {e}")
            return []