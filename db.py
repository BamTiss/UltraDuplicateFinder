import sqlite3
import os

class DatabaseManager:
    def __init__(self, db_filename="file_cache.db"):
        # ย้ายตำแหน่งบันทึกไฟล์ DB ไปยัง %LOCALAPPDATA%\UltraDuplicateFinder
        # เพื่อหลีกเลี่ยง Permission Error เมื่อติดตั้งใน C:\Program Files
        app_data_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'UltraDuplicateFinder')
        os.makedirs(app_data_dir, exist_ok=True)
        
        self.db_path = os.path.join(app_data_dir, db_filename)
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

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
        """
        [FAST CHECK] ตรวจสอบว่าไฟล์นี้ถูกบันทึกไว้แล้ว และไม่มีการเปลี่ยนแปลงใดๆ หรือไม่
        """
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

    def sync_and_clean_ghosts(self, target_folder, active_paths_set):
        """
        [FAST CLEANUP] ล้างข้อมูลไฟล์ที่ถูกลบ/เปลี่ยนชื่อไปแล้วออกจาก DB
        """
        try:
            norm_folder = os.path.abspath(target_folder).lower()
            if norm_folder.endswith(":\\") or norm_folder.endswith(":\\/"):
                search_pattern = norm_folder + "%"
            else:
                search_pattern = norm_folder + os.sep + "%"

            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT filepath FROM files WHERE LOWER(filepath) LIKE ?', (search_pattern,))
                db_rows = cursor.fetchall()

                ghost_paths = [(path,) for (path,) in db_rows if path not in active_paths_set]

                if ghost_paths:
                    cursor.executemany('DELETE FROM files WHERE filepath = ?', ghost_paths)
                    conn.commit()
                    print(f"🧹 [DB Sync] ล้างดัชนีไฟล์เก่าออกแล้ว {len(ghost_paths)} รายการ")
        except Exception as e:
            print(f"Error in sync_and_clean_ghosts: {e}")

    def get_cached_file(self, filepath, size, mtime):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT xxhash, blake3 FROM files 
                    WHERE filepath = ? AND size = ? AND mtime = ?
                ''', (filepath, size, mtime))
                return cursor.fetchone()
        except Exception:
            return None

    def upsert_file(self, filepath, size, mtime, xxhash_val, blake3_val):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO files (filepath, size, mtime, xxhash, blake3)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(filepath) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        xxhash = excluded.xxhash,
                        blake3 = excluded.blake3
                ''', (filepath, size, mtime, xxhash_val, blake3_val))
                conn.commit()
        except Exception:
            pass

    def remove_file(self, filepath):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM files WHERE filepath = ?', (filepath,))
                conn.commit()
        except Exception:
            pass

    def get_duplicates(self, target_folder):
        try:
            norm_folder = os.path.abspath(target_folder).lower()
            
            if norm_folder.endswith(":\\") or norm_folder.endswith(":\\/"):
                search_pattern = norm_folder + "%"
            else:
                search_pattern = norm_folder + os.sep + "%"

            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT filepath, size, blake3 FROM files
                    WHERE LOWER(filepath) LIKE ? 
                      AND blake3 IN (
                          SELECT blake3 FROM files 
                          WHERE LOWER(filepath) LIKE ? 
                            AND blake3 IS NOT NULL 
                            AND blake3 != ''
                          GROUP BY blake3 
                          HAVING COUNT(*) > 1
                      )
                    ORDER BY blake3, size DESC
                ''', (search_pattern, search_pattern))
                return cursor.fetchall()
        except Exception as e:
            print(f"Error fetching duplicates: {e}")
            return []