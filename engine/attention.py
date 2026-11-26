"""Dikkat seviyesi: LLM'in ne kadar çalışacağını belirleyen tek düğme.

0..1 arası bir değer; 0.34 altı "sakin", 0.67 altı "dikkat", üstü "alarm".
Hızlı yükselir (anomali sinyali gelince hemen), yavaş iner (her pencerede biraz).
Taban: kaçırılan olayda kalıcı olarak yükselir, yanlış alarmda biraz düşer,
zamanla varsayılan değerine döner. Tavan: token bütçesinden gelir.
"""

NAMES = ("sakin", "dikkat", "alarm")


def level_name(level):
    if level >= 0.67:
        return "alarm"
    if level >= 0.34:
        return "dikkat"
    return "sakin"


class Attention:
    def __init__(self, base_floor=0.1, release=0.15):
        self.base = base_floor
        self.floor = base_floor
        self.level = base_floor
        self.release = release
        self.signal = 0.0
        self.ceiling = 1.0

    @property
    def name(self):
        return level_name(self.level)

    def update(self, signal, ceiling):
        self.signal = signal
        self.ceiling = ceiling
        target = max(self.floor, signal)
        if target > self.level:
            self.level = target
        else:
            self.level += self.release * (target - self.level)
        self.level = max(0.0, min(self.level, ceiling))
        self.floor += 0.01 * (self.base - self.floor)

    def on_missed(self):
        self.floor = min(0.6, self.floor + 0.15)

    def on_false_alarm(self):
        self.floor = max(0.05, self.floor - 0.03)
