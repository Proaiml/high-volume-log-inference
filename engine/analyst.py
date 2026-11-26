"""LLM analisti: pencere özetinden olay çıkarır.

İki mod var:
  * Simülasyon (varsayılan, anahtar gerekmez): kural tabanlı bir analist. Sadece
    kendisine gönderilen şablonları görür, yani seçim kötüyse o da olayı kaçırır.
  * Claude: özet metni Anthropic API'ye gönderilir, yapılandırılmış JSON döner.
"""
import json
import os
import time
from collections import defaultdict

from .parser import LEVEL_WEIGHT

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}

SYSTEM_PROMPT = """Sen yüksek hacimli log akışlarını izleyen deneyimli bir SRE ve olay analistisin.

Her mesajda bir zaman penceresinin ÖZETİ gelir. Özet, penceredeki logların tamamını şablonlar üzerinden temsil eder:
- Her satır bir şablondur: [ID] DURUM SEVİYE servis: şablon metni | adet, beklenen adet, oran | geçmiş
- DURUM: YENİ (ilk kez görüldü), ARTIŞ (normalden belirgin fazla), DÜŞÜŞ, KAYIP (normalde gelir ama bu pencerede hiç gelmedi), NORMAL.
- "geçmiş" alanı bu şablonun daha önce kaç doğrulanmış olayda ve kaç yanlış alarmda kanıt olduğunu söyler. Yanlış alarm geçmişi olan şablonlara daha temkinli yaklaş.
- Bazı şablonların altında ham log örnekleri vardır. Listede olmayan şablonlar normal aralıktadır.

Görevin gerçek bir operasyonel ya da güvenlik olayı olup olmadığına karar vermek:
- Aynı kök nedene bağlı belirtileri (ör. veritabanı zaman aşımları + sipariş hataları + 500 yanıtları) TEK olayda birleştir.
- Zararsız trafik artışlarını ve tek seferlik gürültüyü olay olarak raporlama. Emin değilsen raporlama.
- Her olay için dayandığın şablon ID'lerini (ör. "T12") evidence_template_ids alanına yaz; yalnızca özette geçen ID'leri kullan.
- Olay yoksa incidents listesini boş döndür.
- Tüm metinleri Türkçe yaz."""

SCHEMA = {
    "type": "object",
    "properties": {
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["dusuk", "orta", "yuksek", "kritik"]},
                    "summary": {"type": "string"},
                    "evidence_template_ids": {"type": "array", "items": {"type": "string"}},
                    "suggested_action": {"type": "string"},
                },
                "required": ["title", "severity", "summary", "evidence_template_ids", "suggested_action"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["incidents"],
    "additionalProperties": False,
}


class AnalystError(Exception):
    pass


def estimate_tokens(text):
    return int(len(text) / 3.5) + 1


def _hhmmss(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def row_text(row):
    hist = (f"{row['dogru']} doğru olay, {row['yanlis']} yanlış alarm"
            if (row["dogru"] or row["yanlis"]) else "yok")
    return (f"[T{row['id']}] {row['durum']} {row['seviye']} {row['servis']}: {row['sablon']}"
            f" | adet={row['adet']} beklenen={row['beklenen']:.0f} oran=x{row['oran']:.1f} | geçmiş: {hist}")


def format_summary(window_id, start, end, total, n_active, n_rest, level_name, selected):
    total_txt = f"{total:,}".replace(",", ".")
    lines = [
        f"PENCERE #{window_id}  {_hhmmss(start)}–{_hhmmss(end)} ({end - start:.0f} sn) | dikkat seviyesi: {level_name.upper()}",
        f"Toplam {total_txt} log, {n_active} aktif şablon. Aşağıda {len(selected)} öncelikli şablon var; "
        f"listede olmayan {n_rest} şablon normal aralıkta.",
        "",
    ]
    for row, _kind in selected:
        lines.append(row_text(row))
        for example in row["gonderilen_ornekler"]:
            lines.append(f"    örnek: {example}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- simülasyon

class SimAnalyst:
    """Gerçek bir LLM'in yapacağına benzer, kural tabanlı ve açıklanabilir analist."""

    def analyze(self, rows):
        findings = []
        for r in rows:
            if r["yanlis"] >= 2 and r["dogru"] == 0:
                continue  # geçmişte tekrar tekrar yanlış alarm üretmiş
            sev = LEVEL_WEIGHT.get(r["seviye"], 0.1)
            if r["durum"] == "YENİ" and sev >= 0.5 and r["adet"] >= 3:
                findings.append(("yeni", r))
            elif r["durum"] == "ARTIŞ" and sev >= 0.5 and r["oran"] >= 4:
                findings.append(("artis", r))
            elif r["durum"] == "KAYIP" and r["beklenen"] >= 10:
                findings.append(("kayip", r))
        if not findings:
            return []

        incidents = []
        errors = [(k, r) for k, r in findings if k != "kayip" and LEVEL_WEIGHT.get(r["seviye"], 0) >= 0.9]
        error_services = {r["servis"] for _, r in errors}
        if len(error_services) >= 2:
            incidents.append(self._build(errors, multi=True))
            findings = [f for f in findings if f not in errors]

        by_service = defaultdict(list)
        for kind, r in findings:
            by_service[r["servis"]].append((kind, r))
        for items in by_service.values():
            incidents.append(self._build(items))
        return incidents

    def _build(self, items, multi=False):
        items = sorted(items, key=lambda kr: -LEVEL_WEIGHT.get(kr[1]["seviye"], 0))
        kind, top = items[0]
        services = sorted({r["servis"] for _, r in items})
        if multi:
            title = "Çoklu servis hatası: " + ", ".join(services)
        else:
            title = {
                "yeni": f"{top['servis']}: ilk kez görülen {top['seviye']} logu",
                "artis": f"{top['servis']}: {top['seviye']} logları {top['oran']:.0f} kat arttı",
                "kayip": f"{top['servis']}: log akışı kesildi",
            }[kind]
        sentences = []
        for k, r in items[:3]:
            if k == "yeni":
                sentences.append(f"{r['servis']} servisinde daha önce hiç görülmemiş “{r['sablon']}” logu {r['adet']} kez geldi.")
            elif k == "artis":
                sentences.append(f"{r['servis']} servisinde “{r['sablon']}” logu normalin {r['oran']:.0f} katına çıktı "
                                 f"({r['adet']} adet, beklenen ~{r['beklenen']:.0f}).")
            else:
                sentences.append(f"{r['servis']} servisinden normalde ~{r['beklenen']:.0f} adet gelen “{r['sablon']}” "
                                 f"logu bu pencerede hiç gelmedi.")
        worst = max(LEVEL_WEIGHT.get(r["seviye"], 0) for _, r in items)
        biggest = max(r["oran"] for _, r in items)
        if multi or worst >= 1.0 or (worst >= 0.9 and biggest >= 20):
            severity = "kritik" if multi else "yuksek"
        elif worst >= 0.9 or kind == "kayip":
            severity = "yuksek"
        elif biggest >= 10:
            severity = "orta"
        else:
            severity = "dusuk"
        action = {
            "yeni": "Son deploy ve konfigürasyon değişikliklerini kontrol edin; gerekirse geri alın.",
            "artis": "Etkilenen servisin bağımlılıklarını (veritabanı, ağ, kuyruk) ve kaynak kullanımını kontrol edin.",
            "kayip": "Servisin ayakta olup olmadığını ve log gönderiminin çalıştığını kontrol edin.",
        }[kind]
        if multi:
            action = "Servisler arası ortak bağımlılığı (çoğunlukla veritabanı veya ağ) önce kontrol edin."
        return {
            "baslik": title,
            "ciddiyet": severity,
            "ozet": " ".join(sentences),
            "oneri": action,
            "kanit": [r["id"] for _, r in items],
        }


# --------------------------------------------------------------------------- Claude

class ClaudeAnalyst:
    def __init__(self):
        try:
            import anthropic  # noqa: F401
            self._sdk = anthropic
        except ImportError:
            self._sdk = None
        self._key = None
        self._client = None
        self._fallbacks_ok = True

    @property
    def sdk_ready(self):
        return self._sdk is not None

    @property
    def has_credentials(self):
        return bool(self._key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def set_key(self, key):
        self._key = (key or "").strip() or None
        self._client = None

    def _client_get(self):
        if self._client is None:
            if self._key:
                self._client = self._sdk.Anthropic(api_key=self._key, timeout=120.0, max_retries=1)
            else:
                self._client = self._sdk.Anthropic(timeout=120.0, max_retries=1)
        return self._client

    def analyze(self, summary, model, effort):
        """(olaylar, kullanılan_token, cevap_veren_model, not) döndürür."""
        if self._sdk is None:
            raise AnalystError("anthropic paketi kurulu değil. baslat.bat ilk kurulumda kurar "
                               "ya da: python -m pip install anthropic")
        sdk = self._sdk
        kwargs = dict(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": summary}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        try:
            client = self._client_get()
            if self._fallbacks_ok and model in FALLBACK_MODELS:
                try:
                    # Güvenlik sınıflandırıcısı reddederse istek sunucu tarafında yedek modelle tekrar çalışır.
                    resp = client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
                except (TypeError, sdk.BadRequestError):
                    self._fallbacks_ok = False
                    resp = client.messages.create(**kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except sdk.AuthenticationError:
            raise AnalystError("API anahtarı geçersiz (401).")
        except sdk.PermissionDeniedError:
            raise AnalystError("API anahtarının bu modele erişim izni yok (403).")
        except sdk.NotFoundError:
            raise AnalystError(f"Model bulunamadı: {model}")
        except sdk.RateLimitError:
            raise AnalystError("Hız sınırına takıldı (429); bu pencere atlandı.")
        except sdk.BadRequestError as e:
            raise AnalystError(f"İstek reddedildi (400): {e.message}")
        except sdk.APIStatusError as e:
            raise AnalystError(f"API hatası ({e.status_code}); bu pencere atlandı.")
        except sdk.APIConnectionError:
            raise AnalystError("Ağ hatası: Anthropic API'ye ulaşılamadı.")
        except Exception as e:  # kimlik bilgisi bulunamadı vb.
            raise AnalystError(f"Claude çağrısı başarısız: {e}")

        tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
        if resp.stop_reason == "refusal":
            return [], tokens, resp.model, "Model bu pencereyi analiz etmeyi reddetti."
        if resp.stop_reason == "max_tokens":
            return [], tokens, resp.model, "Cevap token sınırında kesildi."
        text = next((b.text for b in resp.content if b.type == "text"), None)
        try:
            data = json.loads(text or "")
        except ValueError:
            return [], tokens, resp.model, "Cevap JSON olarak okunamadı."

        incidents = []
        for item in data.get("incidents", []):
            ids = []
            for ref in item.get("evidence_template_ids", []):
                digits = "".join(ch for ch in str(ref) if ch.isdigit())
                if digits:
                    ids.append(int(digits))
            incidents.append({
                "baslik": item.get("title", "Olay"),
                "ciddiyet": item.get("severity", "orta"),
                "ozet": item.get("summary", ""),
                "oneri": item.get("suggested_action", ""),
                "kanit": ids,
            })
        return incidents, tokens, resp.model, None
