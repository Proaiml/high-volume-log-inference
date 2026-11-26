"""Varsayılan ayarlar. ayarlar.json dosyası varsa üzerine yazılır."""
import copy
import json
import os

DEFAULTS = {
    "port": 8080,
    # Sıfırdan başlarken ilk pencerelerde LLM çağrılmaz; sistem "normali" öğrenir.
    "isinma_pencere": 3,
    "isinma_pencere_sn": 10,
    # Dikkat seviyesine göre LLM'in ne kadar çalışacağı.
    # Pencere süreleri demo için kısa tutuldu; gerçek ortamda 60 sn ve üstü önerilir.
    "seviyeler": {
        "sakin": {"pencere_sn": 20, "token_butcesi": 2500, "ornek_sayisi": 1,
                  "model": "claude-haiku-5-5", "effort": "low"},
        "dikkat": {"pencere_sn": 10, "token_butcesi": 6000, "ornek_sayisi": 2,
                   "model": "claude-haiku-5-5", "effort": "low"},
        "alarm": {"pencere_sn": 5, "token_butcesi": 12000, "ornek_sayisi": 4,
                  "model": "claude-opus-5-5", "effort": "medium"},
    },
    "kesif_orani": 0.15,            # bütçenin keşfe ayrılan payı
    "oncelik_min_skor": 0.2,        # bu skorun altındaki NORMAL şablonlar sadece keşifle gider
    "sakin_cagri_esigi": 0.7,       # sakin seviyede LLM'i çağırmak için gereken en yüksek skor
    "saglik_kontrolu_her": 10,      # sakin seviyede her N pencerede bir yine de çağır
    "kayip_min_beklenen": 3,        # "KAYIP" sayılmak için beklenen en az adet
    "gunluk_token_butcesi": 2000000,
    "beta_unutma": 0.995,           # kanıt skorlarının her pencerede eskime oranı
    "oncelik_modeli": {
        "baslangic_agirliklari": {
            "yenilik": 2.0, "kanit": 1.0, "birliktelik": 1.5,
            "nadirlik": 1.0, "sapma": 2.5, "seviye": 2.0,
        },
        "bias": -4.0,
        "ogrenme_hizi": 0.1,
        "l2": 0.01,
    },
    "dikkat_kontrol": {"taban": 0.1, "birakma": 0.15},
    "drain": {"benzerlik_esigi": 0.6, "derinlik": 3},
    "simulator": {"acik": True, "hiz": 2000},
}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    return cfg
