"""Yüksek hacimli log → LLM çıkarım prototipi.

Çalıştırma:  python app.py            (tarayıcı kendiliğinden açılır)
Seçenekler:  python app.py --help
"""
import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.config import load_config
from engine.core import Engine
from engine.simulator import Simulator
from engine.tail import FileTailer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 20 * 1024 * 1024


def parse_ingest(body, content_type):
    """Düz metin (satır satır), NDJSON ya da JSON dizi/nesne kabul eder."""
    text = body.decode("utf-8", errors="replace")
    if "json" in (content_type or ""):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in data]
        if isinstance(data, dict):
            return [json.dumps(data, ensure_ascii=False)]
    return [line for line in text.splitlines() if line.strip()]


def make_handler(engine, sim):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LogPrototip/1.0"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            elif path == "/api/state":
                self._send(200, engine.snapshot_json())
            elif path == "/api/window":
                query = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
                try:
                    body = engine.window_json(int(query.get("id", "")))
                except ValueError:
                    body = None
                if body is None:
                    self._send(404, {"hata": "pencere bulunamadı (son 300 pencere saklanır)"})
                else:
                    self._send(200, body)
            elif path == "/api/health":
                self._send(200, {"durum": "ok"})
            else:
                self._send(404, {"hata": "bulunamadı"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"hata": "istek çok büyük (en fazla 20 MB)"})
                return
            body = self.rfile.read(length) if length else b""
            try:
                if path == "/api/ingest":
                    lines = parse_ingest(body, self.headers.get("Content-Type"))
                    accepted = engine.ingest(lines)
                    self._send(200, {"alinan": accepted, "gelen": len(lines)})
                    return
                data = json.loads(body or b"{}")
                if path == "/api/scenario":
                    sim.start_scenario(data["anahtar"])
                elif path == "/api/sim":
                    sim.configure(rate=data.get("hiz"), running=data.get("calisiyor"))
                elif path == "/api/feedback":
                    engine.feedback(data["id"], data["karar"])
                elif path == "/api/missed":
                    engine.report_missed(data.get("aciklama") or "kullanıcı bildirdi")
                elif path == "/api/settings":
                    engine.update_settings(data)
                elif path == "/api/reset":
                    engine.reset()
                    sim.reset()
                else:
                    self._send(404, {"hata": "bulunamadı"})
                    return
                self._send(200, {"tamam": True})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"hata": str(exc)})

    return Handler


def bind(host, port):
    for p in range(port, port + 11):
        try:
            return ThreadingHTTPServer((host, p), None), p
        except OSError:
            continue
    raise SystemExit(f"{port}-{port + 10} arası portların hepsi dolu. --port ile başka bir port verin.")


def main():
    ap = argparse.ArgumentParser(description="Yüksek hacimli log → LLM çıkarım prototipi")
    ap.add_argument("--port", type=int, help="web arayüzü portu (varsayılan 8080)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="dinlenecek adres; başka makinelerden log almak için 0.0.0.0")
    ap.add_argument("--no-browser", action="store_true", help="tarayıcıyı otomatik açma")
    ap.add_argument("--no-sim", action="store_true", help="simülatörü kapalı başlat (sadece gerçek loglar)")
    ap.add_argument("--tail", metavar="DOSYA", help="bu log dosyasını tail -f gibi takip et")
    ap.add_argument("--bastan", action="store_true", help="--tail dosyasını baştan oku")
    ap.add_argument("--ayarlar", default=os.path.join(ROOT, "ayarlar.json"), help="ayar dosyası")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(args.ayarlar)
    if args.no_sim:
        cfg["simulator"]["acik"] = False
    engine = Engine(cfg)
    sim = Simulator(engine, cfg)
    engine.simulator = sim
    engine.start()
    sim.start()
    if args.tail:
        FileTailer(args.tail, engine, from_start=args.bastan).start()

    server, port = bind(args.host, args.port or cfg["port"])
    server.RequestHandlerClass = make_handler(engine, sim)
    url = f"http://localhost:{port}"
    print("=" * 60)
    print("  Log Çıkarım Prototipi çalışıyor")
    print(f"  Arayüz:      {url}")
    print(f"  Log gönder:  POST {url}/api/ingest")
    if args.tail:
        print(f"  Takip edilen dosya: {args.tail}")
    print("  Durdurmak için bu pencerede Ctrl+C")
    print("=" * 60)
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatılıyor...")


if __name__ == "__main__":
    main()
