"""Öncelik puanı: her log türüne (şablona) 0-1 arası bir puan verir.

Puan, 6 ölçütün ağırlıklı toplamından hesaplanır (lojistik regresyon):

    puan = sigmoid(sabit + a1*yenilik + a2*geçmişte_işe_yaradı + a3*olaylarla_birlikte
                   + a4*nadirlik + a5*normalden_sapma + a6*hata_seviyesi)

Öğrenme: doğrulanan olayın kanıtı olan log türü "faydalı" (1), yanlış alarmın kanıtı
"faydasız" (0), LLM'e gönderilip kullanılmayan log türü düşük ağırlıkla "faydasız"
sayılır. Ağırlıklar her güncellemede başlangıç değerlerine doğru hafifçe geri çekilir,
böylece birkaç olay sistemi savuramaz.
"""
import math

# (anahtar, kısa ad, açıklama)
FEATURES = [
    ("yenilik", "Ne kadar yeni", "Bu log türü ilk kez mi görülüyor, yoksa uzun zamandır mı var?"),
    ("kanit", "Geçmişte işe yaradı mı", "Daha önce doğrulanmış bir olayın kanıtı oldu mu, yoksa yanlış alarma mı karıştı?"),
    ("birliktelik", "Olaylarla birlikte görülüyor mu", "Geçmişteki olay anlarında bu log türü de var mıydı?"),
    ("nadirlik", "Ne kadar nadir", "Tüm loglar içinde ne kadar az görülüyor? Çok sık gelen loglar az bilgi taşır."),
    ("sapma", "Normalden ne kadar farklı", "Bu pencerede normalde beklenenden ne kadar fazla ya da az geldi?"),
    ("seviye", "Hata seviyesi", "DEBUG/INFO düşük, WARN orta, ERROR/FATAL yüksek."),
]


class PriorityModel:
    def __init__(self, initial_weights, bias, learning_rate, l2):
        self.w0 = [float(initial_weights[key]) for key, _, _ in FEATURES]
        self.w = list(self.w0)
        self.b0 = float(bias)
        self.b = float(bias)
        self.lr = float(learning_rate)
        self.l2 = float(l2)
        self.updates = 0

    def score(self, x):
        z = self.b + sum(wi * xi for wi, xi in zip(self.w, x))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def learn(self, x, y, weight=1.0):
        g = (y - self.score(x)) * weight
        for i, xi in enumerate(x):
            self.w[i] += self.lr * (g * xi - self.l2 * (self.w[i] - self.w0[i]))
        self.b += self.lr * (g - self.l2 * (self.b - self.b0))
        self.updates += 1

    def snapshot(self):
        return {
            "olcutler": [
                {"anahtar": key, "ad": name, "aciklama": desc,
                 "agirlik": round(w, 3), "baslangic": round(w0, 3)}
                for (key, name, desc), w, w0 in zip(FEATURES, self.w, self.w0)
            ],
            "sabit": round(self.b, 3),
            "guncelleme": self.updates,
        }
