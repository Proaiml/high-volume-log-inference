"""Bir log dosyasını `tail -f` gibi takip edip motora aktarır."""
import os
import threading
import time


class FileTailer:
    def __init__(self, path, engine, from_start=False):
        self.path = path
        self.engine = engine
        self.from_start = from_start
        self._thread = threading.Thread(target=self._loop, name="tail", daemon=True)

    def start(self):
        self._thread.start()

    def _loop(self):
        while True:
            try:
                f = open(self.path, "r", encoding="utf-8", errors="replace")
            except OSError:
                time.sleep(1.0)
                continue
            with f:
                if not self.from_start:
                    f.seek(0, os.SEEK_END)
                pending, buf = "", []
                while True:
                    line = f.readline()
                    if line:
                        if not line.endswith("\n"):
                            pending += line  # yazımı bitmemiş satır
                            continue
                        buf.append((pending + line).rstrip("\r\n"))
                        pending = ""
                        if len(buf) >= 2000:
                            self.engine.ingest(buf)
                            buf = []
                        continue
                    if buf:
                        self.engine.ingest(buf)
                        buf = []
                    time.sleep(0.2)
                    try:
                        if os.path.getsize(self.path) < f.tell():
                            break  # dosya döndürüldü/kısaltıldı: baştan aç
                    except OSError:
                        break
            self.from_start = True
