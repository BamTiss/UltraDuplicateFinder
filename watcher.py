import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class FileChangeHandler(FileSystemEventHandler):
    """คลาสจัดการเหตุการณ์เมื่อมีการเปลี่ยนแปลงไฟล์ในระบบแบบ Real-time"""
    
    def on_created(self, event):
        if not event.is_directory:
            print(f"[Real-time Watcher] ตรวจพบไฟล์ใหม่: {event.src_path}")

    def on_modified(self, event):
        if not event.is_directory:
            print(f"[Real-time Watcher] ตรวจพบการแก้ไขไฟล์: {event.src_path}")

    def on_deleted(self, event):
        if not event.is_directory:
            print(f"[Real-time Watcher] ตรวจพบการลบไฟล์: {event.src_path}")

def start_watching(path_to_watch):
    """ฟังก์ชันสั่งเริ่มเฝ้าระวังโฟลเดอร์ที่กำหนด"""
    event_handler = FileChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, path=path_to_watch, recursive=True)
    observer.start()
    return observer