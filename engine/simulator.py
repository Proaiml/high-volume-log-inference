"""mlog tarzı bir uygulamayı taklit eden log üreteci.

Bir e-ticaret sisteminin servisleri (api-gateway, auth, payment, order, db-proxy,
cache, scheduler...) normal trafikte log üretir. Arayüzden senaryo tetiklenince
araya olay logları eklenir ya da bazı loglar kesilir. Simülatör hangi senaryonun
gerçek olay, hangisinin zararsız olduğunu bildiği için LLM'in bulduğu olayları
otomatik doğrulayabilir ve kaçırılanları bildirebilir.
"""
import random
import threading
import time
from datetime import datetime

GRACE = 25.0  # senaryo bittikten sonra tespit için tanınan süre (sn)

PATHS = ["products", "products/search", "cart", "categories", "users/me", "recommendations"]
JOBS = ["cleanup", "report-daily", "sync-stock", "email-digest"]


def _uid(r):
    return f"u{r.randint(10000, 99999)}"


def _oid(r):
    return str(r.randint(1000000, 9999999))


def _ip(r):
    return f"{r.randint(11, 223)}.{r.randint(0, 255)}.{r.randint(0, 255)}.{r.randint(1, 254)}"


# anahtar: (ağırlık, servis, seviye, üretici)
NORMAL = {
    "get_ok": (30, "api-gateway", "INFO", lambda r: f"GET /api/v1/{r.choice(PATHS)} 200 {r.randint(5, 95)}ms user={_uid(r)}"),
    "post_order": (6, "api-gateway", "INFO", lambda r: f"POST /api/v1/orders 201 {r.randint(20, 95)}ms user={_uid(r)}"),
    "get_404": (1.2, "api-gateway", "WARN", lambda r: f"GET /api/v1/{r.choice(PATHS)} 404 {r.randint(2, 30)}ms"),
    "slow_resp": (0.25, "api-gateway", "WARN", lambda r: f"Slow response from order-svc: {r.randint(1500, 2900)}ms"),
    "login": (6, "auth-svc", "INFO", lambda r: f"User {_uid(r)} logged in from {_ip(r)}"),
    "refresh": (4, "auth-svc", "INFO", lambda r: f"Token refreshed for user {_uid(r)}"),
    "bad_pass": (0.4, "auth-svc", "WARN", lambda r: f"Invalid password attempt for user {_uid(r)} from {_ip(r)}"),
    "pay_auth": (5, "payment-svc", "INFO", lambda r: f"Payment {r.getrandbits(32):08X} authorized amount={r.randint(20, 3000)}.{r.randint(10, 99)} TRY in {r.randint(40, 95)}ms"),
    "pay_capture": (4.5, "payment-svc", "INFO", lambda r: f"Payment {r.getrandbits(32):08X} captured"),
    "order_created": (6, "order-svc", "INFO", lambda r: f"Order {_oid(r)} created with {r.randint(1, 9)} items"),
    "order_status": (3, "order-svc", "INFO", lambda r: f"Order {_oid(r)} status changed to {r.choice(['PACKED', 'SHIPPED', 'DELIVERED'])}"),
    "smtp": (0.08, "order-svc", "ERROR", lambda r: f"Failed to send notification for order {_oid(r)}: SMTP timeout"),
    "query": (10, "db-proxy", "DEBUG", lambda r: f"Query executed in {r.randint(1, 60)}ms rows={r.randint(0, 99)}"),
    "pool": (1, "db-proxy", "INFO", lambda r: f"Connection pool stats active={r.randint(5, 40)} idle={r.randint(5, 40)}"),
    "cache_ratio": (2, "cache", "INFO", lambda r: f"Cache hit ratio {r.randint(80, 97)}% keys={r.randint(10000, 90000)}"),
    "evict": (1, "cache", "DEBUG", lambda r: f"Evicted {r.randint(10, 95)} keys"),
    "stock": (0.6, "inventory-svc", "INFO", lambda r: f"Stock updated for sku-{r.randint(1000, 9999)} qty={r.randint(0, 90)}"),
}
BASE_WEIGHT = sum(v[0] for v in NORMAL.values())

SCENARIOS = {
    "veritabani": {
        "ad": "Veritabanı bağlantı sorunu", "gercek": True, "sure": 60,
        "aciklama": "db-proxy zaman aşımları, sipariş hataları ve 500 yanıtları",
        "ekle": [
            (0.05, "db-proxy", "ERROR", lambda r: f"Connection to 10.0.3.{r.randint(10, 14)}:5432 timed out after {r.randint(3000, 5000)} ms"),
            (0.02, "order-svc", "ERROR", lambda r: f"Order {_oid(r)} creation failed: database unavailable"),
            (0.02, "api-gateway", "ERROR", lambda r: f"POST /api/v1/orders 500 {r.randint(3000, 5200)}ms user={_uid(r)}"),
        ],
        "carpan": {"post_order": 0.3, "order_created": 0.3},
        "imza": ["timed out after", "database unavailable", "orders 500"],
    },
    "deploy": {
        "ad": "Deploy sonrası yeni hata", "gercek": True, "sure": 60,
        "aciklama": "payment-svc'de daha önce hiç görülmemiş bir istisna",
        "ekle": [
            (0.015, "payment-svc", "ERROR", lambda r: f"NullPointerException in PaymentMapper.toDto line {r.choice([42, 57])}"),
            (0.015, "payment-svc", "ERROR", lambda r: f"at com.shop.payment.PaymentMapper.toDto(PaymentMapper.java:{r.choice([42, 57])})"),
            (0.01, "api-gateway", "WARN", lambda r: f"POST /api/v1/payments 502 {r.randint(10, 90)}ms user={_uid(r)}"),
        ],
        "carpan": {"pay_capture": 0.5},
        "imza": ["NullPointerException", "PaymentMapper", "payments 502"],
    },
    "zamanlayici": {
        "ad": "Zamanlayıcı durdu (heartbeat kesildi)", "gercek": True, "sure": 60,
        "aciklama": "scheduler servisinin heartbeat ve iş logları tamamen kesilir",
        "ekle": [],
        "carpan": {"heartbeat": 0.0, "job": 0.0},
        "imza": ["Heartbeat", "Job "],
    },
    "kilit": {
        "ad": "Kilit çakışması (yavaş artan)", "gercek": True, "sure": 90, "rampa": True,
        "aciklama": "order-svc'de kilit hataları yavaş yavaş artar",
        "ekle": [
            (0.03, "order-svc", "WARN", lambda r: f"Failed to acquire lock on order:{_oid(r)} after {r.randint(3, 9)} retries"),
        ],
        "carpan": {"slow_resp": 8.0},
        "imza": ["acquire lock", "Slow response"],
    },
    "kabakuvvet": {
        "ad": "Kaba kuvvet giriş denemesi", "gercek": True, "sure": 60,
        "aciklama": "tek bir IP bloğundan çok sayıda hatalı şifre denemesi",
        "ekle": [
            (0.04, "auth-svc", "WARN", lambda r: f"Invalid password attempt for user {_uid(r)} from 185.220.101.{r.randint(1, 30)}"),
        ],
        "carpan": {},
        "imza": ["Invalid password"],
    },
    "kampanya": {
        "ad": "Kampanya trafiği (zararsız)", "gercek": False, "sure": 60,
        "aciklama": "trafik 3 katına çıkar ve rate-limit uyarıları gelir; gerçek olay değildir",
        "ekle": [
            (0.02, "api-gateway", "WARN", lambda r: f"Rate limit approaching for client c{r.randint(100, 999)} ({r.randint(600, 990)} req/s)"),
        ],
        "carpan": {"get_ok": 3.0, "post_order": 2.5, "login": 2.0},
        "imza": ["Rate limit approaching", "GET /api", "logged in", "orders 201"],
    },
}


class Run:
    def __init__(self, rid, key, start):
        sc = SCENARIOS[key]
        self.id = rid
        self.key = key
        self.sc = sc
        self.start = start
        self.end = start + sc["sure"]
        self.detected = None
        self.false_alarms = 0
        self.reported_missed = False

    def factor(self, now):
        if not self.sc.get("rampa"):
            return 1.0
        return max(0.05, min(1.0, (now - self.start) / self.sc["sure"] * 1.4))


class Simulator:
    def __init__(self, engine, cfg):
        self.engine = engine
        self.rate = float(cfg["simulator"]["hiz"])
        self.running = bool(cfg["simulator"]["acik"])
        self.lock = threading.Lock()
        self.r = random.Random()
        self.runs = []
        self.produced = 0
        self._run_no = 0
        self._thread = threading.Thread(target=self._loop, name="simulator", daemon=True)

    def start(self):
        self._thread.start()

    def configure(self, rate=None, running=None):
        with self.lock:
            if rate is not None:
                self.rate = float(max(50, min(50000, rate)))
            if running is not None:
                self.running = bool(running)

    def start_scenario(self, key):
        if key not in SCENARIOS:
            raise ValueError(f"bilinmeyen senaryo: {key}")
        with self.lock:
            self._run_no += 1
            self.runs.append(Run(self._run_no, key, time.time()))
            del self.runs[:-30]

    def reset(self):
        with self.lock:
            self.runs = []
            self.produced = 0

    # ------------------------------------------------------------------ doğrulama

    def judge(self, evidence_texts, now):
        """LLM'in bulduğu olayın gerçek olup olmadığını senaryolara bakarak söyler."""
        with self.lock:
            for run in reversed(self.runs):
                if now < run.start or now > run.end + GRACE:
                    continue
                if any(sig in text for sig in run.sc["imza"] for text in evidence_texts):
                    if run.sc["gercek"]:
                        if run.detected is None:
                            run.detected = now
                        return "dogru"
                    run.false_alarms += 1
                    return "yanlis"
        return "yanlis"

    def _check_missed(self, now):
        missed = []
        with self.lock:
            for run in self.runs:
                if (run.sc["gercek"] and run.detected is None and not run.reported_missed
                        and now > run.end + GRACE):
                    run.reported_missed = True
                    missed.append(run.sc["ad"])
        if self.running and self.engine.settings["oto_geri_bildirim"]:
            for name in missed:
                self.engine.report_missed(name, auto=True)

    # ------------------------------------------------------------------ üretim

    def _loop(self):
        last = time.time()
        carry = carry_hb = carry_job = 0.0
        carry_extra = {}
        hb_host = 0
        while True:
            time.sleep(0.1)
            now = time.time()
            dt = min(1.0, now - last)
            last = now
            self._check_missed(now)
            with self.lock:
                if not self.running:
                    continue
                rate = self.rate
                active = [run for run in self.runs if run.start <= now < run.end]
            r = self.r
            mult = {}
            for run in active:
                f = run.factor(now)
                for key, m in run.sc["carpan"].items():
                    mult[key] = mult.get(key, 1.0) * (1.0 + (m - 1.0) * f)

            keys = list(NORMAL)
            weights = [NORMAL[k][0] * mult.get(k, 1.0) for k in keys]
            n_float = rate * dt * (sum(weights) / BASE_WEIGHT) + carry
            n = int(n_float)
            carry = n_float - n
            ts = datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            lines = []
            if n > 0:
                for key in r.choices(keys, weights=weights, k=n):
                    _, svc, lvl, gen = NORMAL[key]
                    lines.append(f"{ts} {lvl} [{svc}@host-{r.randint(1, 6)}] {gen(r)}")

            carry_hb += 6.0 * dt * mult.get("heartbeat", 1.0)
            while carry_hb >= 1.0:
                carry_hb -= 1.0
                hb_host = hb_host % 6 + 1
                lines.append(f"{ts} INFO [scheduler@host-{hb_host}] Heartbeat ok from host-{hb_host}")
            carry_job += 0.5 * dt * mult.get("job", 1.0)
            while carry_job >= 1.0:
                carry_job -= 1.0
                lines.append(f"{ts} INFO [scheduler@host-1] Job {r.choice(JOBS)} completed in {r.randint(200, 900)}ms")

            for run in active:
                f = run.factor(now)
                for i, (frac, svc, lvl, gen) in enumerate(run.sc["ekle"]):
                    k = (run.id, i)
                    c = carry_extra.get(k, 0.0) + frac * rate * dt * f
                    m = int(c)
                    carry_extra[k] = c - m
                    for _ in range(m):
                        lines.append(f"{ts} {lvl} [{svc}@host-{r.randint(1, 6)}] {gen(r)}")

            if lines:
                self.engine.ingest(lines)
                self.produced += len(lines)

    # ------------------------------------------------------------------ durum

    def snapshot(self):
        now = time.time()
        with self.lock:
            results = []
            for run in reversed(self.runs):
                sc = run.sc
                if now < run.end:
                    status, kind = "devam ediyor", "aktif"
                    if run.detected:
                        status, kind = f"yakalandı (+{run.detected - run.start:.0f} sn), sürüyor", "iyi"
                elif sc["gercek"]:
                    if run.detected:
                        status, kind = f"yakalandı (+{run.detected - run.start:.0f} sn)", "iyi"
                    elif run.reported_missed:
                        status, kind = "kaçırıldı", "kotu"
                    else:
                        status, kind = "değerlendiriliyor", "aktif"
                else:
                    if run.false_alarms:
                        status, kind = f"{run.false_alarms} yanlış alarm üretti", "kotu"
                    elif now > run.end + GRACE:
                        status, kind = "doğru: alarm üretilmedi", "iyi"
                    else:
                        status, kind = "değerlendiriliyor", "aktif"
                results.append({
                    "ad": sc["ad"], "gercek": sc["gercek"],
                    "baslangic": time.strftime("%H:%M:%S", time.localtime(run.start)),
                    "durum": status, "tur": kind,
                    "kalan": max(0, round(run.end - now)),
                })
            return {
                "calisiyor": self.running, "hiz": self.rate, "uretilen": self.produced,
                "senaryolar": [{"anahtar": k, "ad": v["ad"], "aciklama": v["aciklama"], "gercek": v["gercek"]}
                               for k, v in SCENARIOS.items()],
                "sonuclar": results[:12],
            }
