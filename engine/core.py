"""Motor: log akışı → şablon → pencere özeti → öncelik puanı ile seçim → LLM → olay → geri besleme.

İş parçacıkları:
  * işçi: kuyruktan logları alır, şablona çevirir, pencere süresi dolunca pencereyi kapatır
  * analiz: her LLM çağrısı ayrı iş parçacığında çalışır, akışı bekletmez
Tüm paylaşılan durum self.lock ile korunur.
"""
import json
import math
import queue
import random
import threading
import time
from collections import deque

from .analyst import (AnalystError, ClaudeAnalyst, SimAnalyst, estimate_tokens,
                      format_summary, row_text)
from .attention import Attention
from .miner import TemplateMiner
from .priority import PriorityModel
from .parser import LEVEL_WEIGHT, mask, parse_line

EXAMPLES_PER_TEMPLATE = 4
MAX_WINDOWS_KEPT = 300
MAX_INCIDENTS_KEPT = 200
DEDUP_SECONDS = 90  # bu süre içinde aynı şablonlara dayanan olaylar tek olayda birleşir


def _hhmmss(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


class TStat:
    """Bir şablonun geçmişi."""
    __slots__ = ("seen_windows", "total", "rate", "alpha", "beta", "dogru", "yanlis",
                 "win_with", "inc_with", "first_seen")

    def __init__(self, now):
        self.seen_windows = 0   # görüldüğü pencere sayısı
        self.total = 0          # toplam adet
        self.rate = 0.0         # saniyede beklenen adet (EWMA)
        self.alpha = 1.0        # Beta(alpha, beta): kanıt skoru
        self.beta = 1.0
        self.dogru = 0          # doğrulanmış olaylarda kanıt olma sayısı
        self.yanlis = 0         # yanlış alarmlarda kanıt olma sayısı
        self.win_with = 0       # içinde bulunduğu pencere sayısı
        self.inc_with = 0       # içinde bulunduğu olay penceresi sayısı
        self.first_seen = now


class Window:
    __slots__ = ("start", "length", "counts", "examples", "total", "errors")

    def __init__(self, start, length):
        self.start = start
        self.length = length
        self.counts = {}
        self.examples = {}
        self.total = 0
        self.errors = 0


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.q = queue.Queue(maxsize=5000)
        self.simulator = None
        self.claude = ClaudeAnalyst()
        self.sim_analyst = SimAnalyst()
        self.settings = {
            "llm_modu": "simulasyon",
            "oto_geri_bildirim": True,
            "gunluk_token_butcesi": int(cfg["gunluk_token_butcesi"]),
        }
        self.gen = 0
        self._reset_state()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="motor", daemon=True)

    # ------------------------------------------------------------------ yaşam döngüsü

    def _reset_state(self):
        cfg = self.cfg
        now = time.time()
        self.gen += 1
        self.miner = TemplateMiner(sim_threshold=cfg["drain"]["benzerlik_esigi"],
                                   depth=cfg["drain"]["derinlik"])
        self.stats = {}
        n = cfg["oncelik_modeli"]
        self.priority = PriorityModel(n["baslangic_agirliklari"], n["bias"], n["ogrenme_hizi"], n["l2"])
        a = cfg["dikkat_kontrol"]
        self.attention = Attention(a["taban"], a["birakma"])
        self.rng = random.Random()
        self.window_no = 0
        self.win = Window(now, cfg["isinma_pencere_sn"])
        self.windows = {}
        self.history = deque(maxlen=180)
        self.incidents = []
        self.inc_no = 0
        self.cum_total = 0
        self.windows_total = 0
        self.inc_windows = set()
        self.inc_windows_total = 0
        self.err_share = None
        self.busy = False
        self.llm_calls = 0
        self.tokens_used = 0
        self.tokens_today = 0
        self.tokens_day = time.strftime("%Y-%m-%d")
        self.logs_in_called = 0
        self.since_call = 0
        self.metrics = {"dogru": 0, "yanlis": 0, "kacirilan": 0}
        self.dropped = 0
        self.total_logs = 0
        self.rate_samples = deque(maxlen=12)
        self.llm_error = None
        self.llm_note = None
        self.last_window_id = None
        self.started = now

    def start(self):
        self._thread.start()

    def reset(self):
        with self.lock:
            while True:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self._reset_state()

    # ------------------------------------------------------------------ giriş

    def ingest(self, lines):
        if not lines:
            return 0
        try:
            self.q.put_nowait(lines)
        except queue.Full:
            with self.lock:
                self.dropped += len(lines)
            return 0
        return len(lines)

    def _loop(self):
        while not self._stop:
            batches = []
            try:
                batches.append(self.q.get(timeout=0.1))
                while len(batches) < 50:
                    batches.append(self.q.get_nowait())
            except queue.Empty:
                pass
            with self.lock:
                for batch in batches:
                    self._process(batch)
                now = time.time()
                self._sample_rate(now)
                if now - self.win.start >= self.win.length:
                    try:
                        self._close_window(now)
                    except Exception as exc:  # motor durmasın; hatayı arayüzde göster
                        self.llm_error = f"Pencere kapatılırken hata: {exc!r}"
                        self.win = Window(now, self.cfg["seviyeler"]["sakin"]["pencere_sn"])

    def _process(self, lines):
        w = self.win
        counts, examples = w.counts, w.examples
        add = self.miner.add
        rnd = self.rng.random
        n = 0
        for raw in lines:
            rec = parse_line(raw)
            if rec is None:
                continue
            level, service, message = rec
            tid = add(level, service, mask(message)).id
            c = counts.get(tid, 0) + 1
            counts[tid] = c
            if c <= EXAMPLES_PER_TEMPLATE:
                examples.setdefault(tid, []).append(raw[:400])
            elif rnd() * c < EXAMPLES_PER_TEMPLATE:  # rezervuar örnekleme
                examples[tid][int(rnd() * EXAMPLES_PER_TEMPLATE)] = raw[:400]
            if level == "ERROR" or level == "FATAL":
                w.errors += 1
            n += 1
        w.total += n
        self.total_logs += n

    def _sample_rate(self, now):
        if not self.rate_samples or now - self.rate_samples[-1][0] >= 0.5:
            self.rate_samples.append((now, self.total_logs))

    def current_rate(self):
        if len(self.rate_samples) < 2:
            return 0.0
        (t0, c0), (t1, c1) = self.rate_samples[0], self.rate_samples[-1]
        return (c1 - c0) / max(1e-6, t1 - t0)

    # ------------------------------------------------------------------ pencere kapanışı

    def _close_window(self, now):
        cfg = self.cfg
        w = self.win
        dur = max(0.5, now - w.start)
        self.window_no += 1
        wid = self.window_no
        warm = self.window_no <= cfg["isinma_pencere"]
        counts = w.counts
        total = w.total
        cum_total = self.cum_total + total

        # Adaylar: bu pencerede görülenler + gelmesi beklenip hiç gelmeyenler (KAYIP).
        # KAYIP sadece INFO/DEBUG için anlamlı: bir hata logunun kesilmesi iyileşmedir, olay değil.
        candidates = set(counts)
        if not warm:
            templates = self.miner.templates
            for tid, st in self.stats.items():
                if (tid not in counts and st.seen_windows >= 3
                        and templates[tid].level in ("DEBUG", "INFO")
                        and st.rate * dur >= cfg["kayip_min_beklenen"]):
                    candidates.add(tid)

        rows = []
        for tid in candidates:
            rows.append(self._features(tid, counts.get(tid, 0), dur, cum_total, warm, w))

        signal, reason_sig = (0.0, "") if warm else self._signal(rows, w)
        ceiling = self._ceiling()
        self.attention.update(signal, ceiling)
        lvl = self.attention.name
        params = cfg["seviyeler"][lvl]

        selected, used_tokens = self._select(rows, params)
        top_score = max((r["skor"] for r in rows), default=0.0)

        reason = None
        if warm:
            reason = f"Isınma ({self.window_no}/{cfg['isinma_pencere']}): sistem normali öğreniyor"
        elif self.tokens_today >= self.settings["gunluk_token_butcesi"]:
            reason = "Günlük token bütçesi doldu; yalnızca ucuz dedektörler çalışıyor"
        elif self.busy:
            reason = "Önceki LLM çağrısı sürüyor; bu pencere atlandı"
        elif not selected:
            reason = "Gönderilecek şablon yok"
        elif (lvl == "sakin" and top_score < cfg["sakin_cagri_esigi"]
              and self.since_call + 1 < cfg["saglik_kontrolu_her"]):
            reason = "Sakin: LLM'e gerek görülmedi"
        called = reason is None

        n_active = len(counts)
        shown = {r["id"] for r, _ in selected}
        n_rest = sum(1 for tid in counts if tid not in shown)
        summary = format_summary(wid, w.start, now, total, n_active, n_rest, lvl, selected)
        claude_mode = self.settings["llm_modu"] == "claude"
        if called:
            reason = (f"LLM'e gönderildi: {params['model']}" if claude_mode
                      else "LLM'e gönderildi: simülasyon analisti")

        ordered = sorted(rows, key=lambda r: -r["skor"])
        sel_kind = {r["id"]: kind for r, kind in selected}
        display = []
        for r in ordered:
            if len(display) >= 40 and r["id"] not in sel_kind:
                continue
            display.append({
                "id": r["id"], "durum": r["durum"], "seviye": r["seviye"], "servis": r["servis"],
                "sablon": r["sablon"][:180], "adet": r["adet"], "beklenen": round(r["beklenen"], 1),
                "oran": round(r["oran"], 1), "skor": round(r["skor"], 3),
                "secim": sel_kind.get(r["id"], ""), "x": [round(v, 2) for v in r["x"]],
                "dogru": r["dogru"], "yanlis": r["yanlis"], "gorulen": r["gorulen"],
                "ornek": [e[:300] for e in r["ornekler"][:2]],
            })

        record = {
            "id": wid, "bas": w.start, "bit": now, "sure": round(dur, 1), "toplam": total,
            "aktif": n_active, "seviye": lvl, "dikkat": round(self.attention.level, 3),
            "sinyal": round(signal, 3), "sinyal_nedeni": reason_sig, "cagrildi": called,
            "neden": reason, "token": used_tokens if called else 0, "satirlar": display,
            "secilen": {r["id"]: r["x"] for r, _ in selected}, "mevcut": set(counts),
            "ozet": summary if called else None, "olaylar": [],
            "agirliklar": [round(v, 3) for v in self.priority.w], "sabit": round(self.priority.b, 3),
        }
        self.windows[wid] = record
        if len(self.windows) > MAX_WINDOWS_KEPT:
            for old in sorted(self.windows)[: len(self.windows) - MAX_WINDOWS_KEPT]:
                del self.windows[old]
        self.last_window_id = wid
        self.history.append({"id": wid, "t": _hhmmss(now), "dikkat": round(self.attention.level, 3),
                             "sinyal": round(signal, 3), "hiz": round(total / dur, 1), "cagri": called})

        if called:
            self.since_call = 0
            self.logs_in_called += total
            sim_rows = [r for r, _ in selected]
            self._launch(wid, sim_rows, summary, params, claude_mode)
        else:
            self.since_call += 1

        self._update_baselines(counts, dur, warm, now)
        self.cum_total = cum_total
        self.windows_total += 1
        if total:
            share = w.errors / total
            if self.err_share is None:
                self.err_share = share
            else:
                k = 0.02 if share > 3 * max(self.err_share, 0.001) else 0.1
                self.err_share += k * (share - self.err_share)

        next_len = cfg["isinma_pencere_sn"] if self.window_no < cfg["isinma_pencere"] else params["pencere_sn"]
        self.win = Window(now, next_len)

    def _features(self, tid, count, dur, cum_total, warm, w):
        tpl = self.miner.templates[tid]
        st = self.stats.get(tid)
        is_new = st is None or st.seen_windows == 0
        expected = 0.0 if is_new else st.rate * dur
        ratio = (count + 1.0) / (expected + 1.0)
        if is_new:
            durum = "YENİ"
        elif count == 0:
            durum = "KAYIP"
        elif warm:
            durum = "NORMAL"
        elif ratio >= 3 and count >= 5:
            durum = "ARTIŞ"
        elif ratio <= 1 / 3 and expected >= 10:
            durum = "DÜŞÜŞ"
        else:
            durum = "NORMAL"

        a, b = (1.0, 1.0) if st is None else (st.alpha, st.beta)
        yenilik = 1.0 if is_new else math.exp(-st.seen_windows / 4.0)
        kanit = a / (a + b)
        if self.inc_windows_total > 0 and st is not None:
            p_inc = (st.inc_with + 0.5) / (self.inc_windows_total + 1.0)
            p_all = (st.win_with + 0.5) / (self.windows_total + 1.0)
            birliktelik = math.tanh(math.log(p_inc / p_all))
        else:
            birliktelik = 0.0
        share = ((0 if st is None else st.total) + count) / max(1, cum_total)
        nadirlik = min(1.0, -math.log2(max(share, 1e-12)) / 20.0)
        sapma = 0.0 if (warm and not is_new) else min(1.0, abs(math.log2(ratio)) / 6.0)
        seviye = LEVEL_WEIGHT.get(tpl.level, 0.1)
        x = [yenilik, kanit, birliktelik, nadirlik, sapma, seviye]

        # Thompson örneklemesi: kanıt skoru belirsiz olan şablonlara da şans tanır.
        x_sample = list(x)
        x_sample[1] = self.rng.betavariate(a, b)
        return {
            "id": tid, "durum": durum, "seviye": tpl.level, "servis": tpl.service,
            "sablon": tpl.text, "adet": count, "beklenen": expected, "oran": ratio,
            "x": x, "skor": self.priority.score(x_sample),
            "dogru": 0 if st is None else st.dogru, "yanlis": 0 if st is None else st.yanlis,
            "gorulen": 0 if st is None else st.seen_windows,
            "ornekler": list(w.examples.get(tid, [])), "gonderilen_ornekler": [],
        }

    def _signal(self, rows, w):
        """Ucuz dedektörler + öncelik puanı → anlık anomali sinyali (0..1)."""
        best, why = 0.0, ""
        for r in rows:
            sev = LEVEL_WEIGHT.get(r["seviye"], 0.1)
            gate = 1.0 / (1.0 + 0.5 * max(0, r["yanlis"] - r["dogru"]))
            s = 0.0
            if r["durum"] == "YENİ" and sev >= 0.5 and r["adet"] >= 3:
                s, label = 0.7, f"ilk kez görülen {r['seviye']} logu"
            elif r["durum"] == "ARTIŞ" and sev >= 0.5 and r["oran"] >= 5:
                s, label = (1.0 if r["oran"] >= 20 else 0.8), f"{r['seviye']} logu normalin {r['oran']:.0f} katı"
            elif r["durum"] == "KAYIP" and r["beklenen"] >= 10:
                s, label = 0.65, "normalde gelen log hiç gelmedi"
            s *= gate
            if s > best:
                best, why = s, f"T{r['id']} ({r['servis']}): {label}"
        if self.err_share is not None and w.total and w.errors >= 10:
            if w.errors / w.total > 3 * max(self.err_share, 0.001) and 0.7 > best:
                best, why = 0.7, "hata oranı normalin 3 katından fazla"
        top = max((r["skor"] for r in rows), default=0.0)
        s = max(0.0, min(1.0, (top - 0.5) / 0.45))
        if s > best:
            best, why = s, "öncelik puanı yüksek"
        return best, why

    def _ceiling(self):
        day = time.strftime("%Y-%m-%d")
        if day != self.tokens_day:
            self.tokens_day, self.tokens_today = day, 0
        budget = self.settings["gunluk_token_butcesi"]
        if self.tokens_today >= budget:
            return 0.0
        if self.tokens_today >= 0.8 * budget:
            return 0.6
        return 1.0

    def _select(self, rows, params):
        """Sabit token bütçesine en yüksek skorlu şablonları yerleştir, kalanın bir kısmı keşfe."""
        cfg = self.cfg
        budget = params["token_butcesi"]
        main_budget = budget * (1.0 - cfg["kesif_orani"])
        n_examples = params["ornek_sayisi"]
        used = 80  # başlık
        selected, rest = [], []

        def cost(r, with_examples):
            r["gonderilen_ornekler"] = r["ornekler"][:n_examples] if with_examples else []
            text = row_text(r) + "".join(r["gonderilen_ornekler"])
            return estimate_tokens(text) + 8 * len(r["gonderilen_ornekler"])

        for r in sorted(rows, key=lambda r: -r["skor"]):
            interesting = r["durum"] != "NORMAL" or r["skor"] >= cfg["oncelik_min_skor"]
            if not interesting:
                rest.append(r)
                continue
            c = cost(r, True)
            if used + c <= main_budget:
                selected.append((r, "oncelik"))
                used += c
            else:
                rest.append(r)

        # Keşif: bütçenin küçük bir payı, öncelik listesine girmeyen şablonlardan rastgele.
        self.rng.shuffle(rest)
        explore_budget = budget * cfg["kesif_orani"]
        max_explore = max(2, math.ceil(len(rest) * cfg["kesif_orani"]))
        explore_used, explored = 0, 0
        for r in rest:
            if explored >= max_explore:
                break
            c = cost(r, False)
            if used + c > budget:
                continue
            if explore_used + c <= explore_budget or explored < 2:
                selected.append((r, "kesif"))
                used += c
                explore_used += c
                explored += 1
        selected.sort(key=lambda rk: -rk[0]["skor"])
        return selected, used + 250  # + cevap için tahmini çıktı

    def _update_baselines(self, counts, dur, warm, now):
        decay = self.cfg["beta_unutma"]
        for tid in set(self.stats) | set(counts):
            c = counts.get(tid, 0)
            st = self.stats.get(tid)
            if st is None:
                st = self.stats[tid] = TStat(now)
                # Isınmada görülen her şey "normal" kabul edilir; sonradan çıkan yeni şablonun
                # normali sıfıra yakın başlar, böylece olay boyunca ARTIŞ olarak görünmeye devam eder.
                st.rate = c / dur if warm else 0.02 * c / dur
            else:
                alpha = max(0.1, 1.0 / (st.seen_windows + 1))
                expected = st.rate * dur
                if not warm and c > expected and math.log2((c + 1.0) / (expected + 1.0)) > 2:
                    alpha = 0.02  # ani artış normal çizgisini hemen yukarı çekmesin
                st.rate += alpha * (c / dur - st.rate)
                st.alpha = 1.0 + (st.alpha - 1.0) * decay
                st.beta = 1.0 + (st.beta - 1.0) * decay
            if c > 0:
                st.seen_windows += 1
                st.total += c
                st.win_with += 1

    # ------------------------------------------------------------------ LLM

    def _launch(self, wid, rows, summary, params, claude_mode):
        self.busy = True
        gen = self.gen

        def work():
            note = None
            err = None
            model = "simülasyon"
            try:
                if claude_mode:
                    incidents, tokens, model, note = self.claude.analyze(summary, params["model"], params["effort"])
                else:
                    time.sleep(0.2)
                    incidents = self.sim_analyst.analyze(rows)
                    tokens = estimate_tokens(summary) + 250
            except AnalystError as exc:
                incidents, tokens, err = [], 0, str(exc)
            except Exception as exc:
                incidents, tokens, err = [], 0, f"Beklenmeyen hata: {exc!r}"
            with self.lock:
                if gen != self.gen:
                    return
                self.busy = False
                self._on_result(wid, incidents, tokens, model, err, note)

        threading.Thread(target=work, name=f"analiz-{wid}", daemon=True).start()

    def _on_result(self, wid, incidents, tokens, model, err, note):
        self.llm_calls += 1
        self.tokens_used += tokens
        self.tokens_today += tokens
        self.llm_note = note
        win = self.windows.get(wid)
        if err:
            self.llm_error = err
            if win:
                win["neden"] += " — hata"
            return
        self.llm_error = None
        if win is None:
            return
        win["token"] = tokens
        now = time.time()
        evidence_all = set()
        new_ids = []
        recent = [o for o in self.incidents[:40] if o["tur"] == "olay" and now - o["ts"] < DEDUP_SECONDS]
        for inc in incidents:
            ids = [t for t in inc["kanit"] if t in win["secilen"]]
            if not ids:
                continue
            evidence_all.update(ids)
            texts = [f"T{t} {self.miner.templates[t].service}: {self.miner.templates[t].text}" for t in ids]
            # Aynı şablonlara dayanan yakın tarihli olay varsa yenisini açma, onu güncelle.
            same = next((o for o in recent if set(o["kanit"]) & set(ids)), None)
            if same is not None:
                same["tekrar"] += 1
                same["son"] = _hhmmss(now)
                same["ts"] = now
                for t, text in zip(ids, texts):
                    if t not in same["kanit"]:
                        same["kanit"].append(t)
                        same["kanit_metin"].append(text)
                if same["id"] not in win["olaylar"]:
                    win["olaylar"].append(same["id"])
                continue
            self.inc_no += 1
            oid = f"O{self.inc_no}"
            entry = {
                "id": oid, "tur": "olay", "zaman": _hhmmss(now), "son": _hhmmss(now), "ts": now,
                "tekrar": 1, "pencere": wid, "baslik": inc["baslik"], "ciddiyet": inc["ciddiyet"],
                "ozet": inc["ozet"], "oneri": inc["oneri"], "kanit": ids, "kanit_metin": texts,
                "model": model, "karar": None, "oto": False,
            }
            self.incidents.insert(0, entry)
            recent.append(entry)
            win["olaylar"].append(oid)
            new_ids.append(oid)
        del self.incidents[MAX_INCIDENTS_KEPT:]

        # Gönderilip kullanılmayan şablonlar: zayıf olumsuz sinyal.
        for tid, x in win["secilen"].items():
            if tid not in evidence_all:
                self.priority.learn(x, 0, 0.1)
                st = self.stats.get(tid)
                if st:
                    st.beta += 0.1

        sim = self.simulator
        if self.settings["oto_geri_bildirim"] and sim is not None and sim.running:
            for oid in new_ids:
                inc = self._incident(oid)
                verdict = sim.judge(inc["kanit_metin"], time.time())
                self.feedback(oid, verdict, auto=True)

    # ------------------------------------------------------------------ geri besleme

    def _incident(self, oid):
        return next((o for o in self.incidents if o["id"] == oid), None)

    def feedback(self, oid, verdict, auto=False):
        if verdict not in ("dogru", "yanlis"):
            raise ValueError("karar 'dogru' ya da 'yanlis' olmalı")
        with self.lock:
            inc = self._incident(oid)
            if inc is None or inc["tur"] != "olay" or inc["karar"] is not None:
                return False
            inc["karar"] = verdict
            inc["oto"] = auto
            win = self.windows.get(inc["pencere"])
            y = 1 if verdict == "dogru" else 0
            for tid in inc["kanit"]:
                x = win["secilen"].get(tid) if win else None
                if x is not None:
                    self.priority.learn(x, y, 1.0)
                st = self.stats.get(tid)
                if st:
                    if y:
                        st.alpha += 1
                        st.dogru += 1
                    else:
                        st.beta += 1
                        st.yanlis += 1
            if y:
                self.metrics["dogru"] += 1
                self._mark_incident_windows([inc["pencere"], inc["pencere"] - 1])
            else:
                self.metrics["yanlis"] += 1
                self.attention.on_false_alarm()
            return True

    def _mark_incident_windows(self, ids):
        for wid in ids:
            win = self.windows.get(wid)
            if win is None or wid in self.inc_windows:
                continue
            self.inc_windows.add(wid)
            self.inc_windows_total += 1
            for tid in win["mevcut"]:
                st = self.stats.get(tid)
                if st:
                    st.inc_with += 1

    def report_missed(self, label, auto=False):
        with self.lock:
            recent = sorted(self.windows)[-3:]
            self._mark_incident_windows(recent)
            self.attention.on_missed()
            self.metrics["kacirilan"] += 1
            self.inc_no += 1
            now = time.time()
            self.incidents.insert(0, {
                "id": f"O{self.inc_no}", "tur": "kacirildi", "zaman": _hhmmss(now), "son": _hhmmss(now),
                "ts": now, "tekrar": 1,
                "pencere": recent[-1] if recent else None, "baslik": f"Kaçırılan olay: {label}",
                "ciddiyet": "yuksek",
                "ozet": "Bu olay LLM tarafından yakalanmadı. Dikkat seviyesinin tabanı yükseltildi ve son "
                        "pencerelerdeki şablonların olay birlikteliği güncellendi.",
                "oneri": "", "kanit": [], "kanit_metin": [], "model": "", "karar": "kacirildi", "oto": auto,
            })

    # ------------------------------------------------------------------ ayarlar ve durum

    def update_settings(self, data):
        with self.lock:
            if "llm_modu" in data:
                if data["llm_modu"] not in ("simulasyon", "claude"):
                    raise ValueError("llm_modu 'simulasyon' ya da 'claude' olmalı")
                self.settings["llm_modu"] = data["llm_modu"]
                self.llm_error = None
            if "api_anahtari" in data:
                self.claude.set_key(str(data["api_anahtari"]))
                self.llm_error = None
            if "oto_geri_bildirim" in data:
                self.settings["oto_geri_bildirim"] = bool(data["oto_geri_bildirim"])
            if "gunluk_token_butcesi" in data:
                self.settings["gunluk_token_butcesi"] = max(1000, int(data["gunluk_token_butcesi"]))

    def snapshot_json(self):
        """Arayüz için durum; kilit altında serileştirilir ki eşzamanlı değişiklik olmasın."""
        with self.lock:
            return json.dumps(self._snapshot(), ensure_ascii=False)

    def window_json(self, wid):
        """Geçmişteki bir pencerede LLM'e ne gittiğini döndürür (son 300 pencere saklanır)."""
        with self.lock:
            rec = self.windows.get(wid)
            if rec is None:
                return None
            return json.dumps(self._window_view(rec), ensure_ascii=False)

    @staticmethod
    def _window_view(rec):
        view = {k: rec[k] for k in ("id", "sure", "toplam", "aktif", "seviye", "dikkat", "sinyal",
                                     "sinyal_nedeni", "cagrildi", "neden", "token", "satirlar",
                                     "ozet", "agirliklar", "sabit")}
        view["olaylar"] = list(rec["olaylar"])
        view["bas"] = _hhmmss(rec["bas"])
        view["bit"] = _hhmmss(rec["bit"])
        return view

    def _snapshot(self):
        with self.lock:
            now = time.time()
            cfg = self.cfg
            last = self.windows.get(self.last_window_id) if self.last_window_id else None
            tp, fp, fn = self.metrics["dogru"], self.metrics["yanlis"], self.metrics["kacirilan"]
            warm_left = max(0, cfg["isinma_pencere"] - self.window_no)
            window = self._window_view(last) if last else None
            windows = [{
                "id": rec["id"], "bas": _hhmmss(rec["bas"]), "sure": rec["sure"], "seviye": rec["seviye"],
                "cagrildi": rec["cagrildi"], "olaylar": list(rec["olaylar"]), "toplam": rec["toplam"],
                "token": rec["token"], "gonderilen": len(rec["secilen"]) if rec["cagrildi"] else 0,
            } for rec in (self.windows[k] for k in sorted(self.windows, reverse=True)[:120])]
            sim = self.simulator
            return {
                "zaman": _hhmmss(now),
                "istatistik": {
                    "log_hizi": round(self.current_rate(), 1),
                    "toplam_log": self.total_logs,
                    "sablon_sayisi": len(self.miner.templates),
                    "dusurulen": self.dropped,
                    "llm_cagrisi": self.llm_calls,
                    "token_toplam": self.tokens_used,
                    "token_bugun": self.tokens_today,
                    "log_per_token": round(self.total_logs / self.tokens_used, 1) if self.tokens_used else None,
                    "dogru": tp, "yanlis": fp, "kacirilan": fn,
                    "kesinlik": round(tp / (tp + fp), 3) if tp + fp else None,
                    "duyarlilik": round(tp / (tp + fn), 3) if tp + fn else None,
                },
                "isinma": {"aktif": warm_left > 0, "kalan": warm_left, "toplam": cfg["isinma_pencere"]},
                "dikkat": {
                    "seviye": round(self.attention.level, 3), "ad": self.attention.name,
                    "taban": round(self.attention.floor, 3), "tavan": round(self.attention.ceiling, 3),
                    "sinyal": round(self.attention.signal, 3),
                    "parametreler": cfg["seviyeler"],
                    "gecmis": list(self.history),
                },
                "pencere_ilerleme": {
                    "gecen": round(now - self.win.start, 1), "uzunluk": self.win.length,
                    "no": self.window_no + 1, "log": self.win.total,
                },
                "son_pencere": window,
                "pencereler": windows,
                "olaylar": self.incidents[:40],
                "oncelik_modeli": self.priority.snapshot(),
                "llm": {
                    "mod": self.settings["llm_modu"], "sdk": self.claude.sdk_ready,
                    "anahtar": self.claude.has_credentials, "hata": self.llm_error, "not": self.llm_note,
                    "mesgul": self.busy,
                },
                "ayarlar": self.settings,
                "simulator": sim.snapshot() if sim else None,
            }
