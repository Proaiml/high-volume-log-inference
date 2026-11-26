"""Log satırlarını ayrıştırma ve değişken kısımları maskeleme.

Desteklenen girişler:
  * mlog tarzı satır:  2026-11-26T12:04:01.123 ERROR [payment-svc@host-2] mesaj
  * JSON satırı:       {"ts": "...", "level": "ERROR", "service": "payment-svc", "msg": "..."}
  * düz metin:         [zaman] [SEVİYE] mesaj   (seviye yoksa anahtar kelimeden tahmin edilir)
"""
import json
import re

LEVELS = {
    "TRACE": "DEBUG", "DEBUG": "DEBUG", "DBG": "DEBUG",
    "INFO": "INFO", "NOTICE": "INFO", "INF": "INFO",
    "WARN": "WARN", "WARNING": "WARN", "WRN": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR",
    "FATAL": "FATAL", "CRITICAL": "FATAL", "CRIT": "FATAL", "PANIC": "FATAL", "EMERG": "FATAL",
}
LEVEL_WEIGHT = {"DEBUG": 0.0, "INFO": 0.1, "WARN": 0.5, "ERROR": 0.9, "FATAL": 1.0}

_MLOG = re.compile(r"^(\S+)\s+([A-Za-z]+)\s+\[([^\]@\s]+)(?:@([^\]\s]+))?\]\s?(.*)$")
_TS = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ][\d:.,]+(?:Z|[+-]\d{2}:?\d{2})?\]?\s+")

# Değişken parçalar (IP, kimlik, sayı...) <*> ile değiştirilir; böylece aynı türden
# loglar aynı şablona düşer. 3 haneli HTTP durum kodları (200, 404, 500) korunur.
_MASK = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"                                # IP[:port]
    r"|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"          # UUID
    r"|\b0x[0-9a-fA-F]+\b"                                                  # 0x...
    r"|\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{6,}\b"   # hex kimlik
    r"|\b[0-9A-F]{8,}\b|\b[0-9a-f]{8,}\b"                                   # rakamsız uzun hex
    r"|\b[A-Za-z]+[-_]?\d[\w-]*"                                            # u8823, host-3, sku-12
    r"|(?<![\w.])(?![1-5]\d\d(?![\w.%]))-?\d+(?:\.\d+)?(?:ms|s|%|[kKmMgG][bB])?(?![\w.])"  # sayılar
)


def mask(message):
    return _MASK.sub("<*>", message)


def _guess_level(text):
    low = text.lower()
    if "fatal" in low or "panic" in low:
        return "FATAL"
    if "exception" in low or "error" in low or "fail" in low:
        return "ERROR"
    if "warn" in low:
        return "WARN"
    return "INFO"


def _from_json(obj):
    msg = obj.get("msg") or obj.get("message") or obj.get("log") or ""
    if not isinstance(msg, str):
        msg = json.dumps(msg, ensure_ascii=False)
    lvl = str(obj.get("level") or obj.get("severity") or obj.get("lvl") or "").upper()
    level = LEVELS.get(lvl) or _guess_level(msg)
    service = str(obj.get("service") or obj.get("app") or obj.get("logger") or "app")
    return level, service.replace(" ", "_"), msg


def parse_line(line):
    """(seviye, servis, mesaj) döndürür; boş satırda None."""
    line = line.strip()
    if not line:
        return None
    if line[0] == "{":
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return _from_json(obj)
        except ValueError:
            pass
    m = _MLOG.match(line)
    if m:
        level = LEVELS.get(m.group(2).upper())
        if level:
            return level, m.group(3), m.group(5)
    rest = _TS.sub("", line, count=1)
    first, _, after = rest.partition(" ")
    level = LEVELS.get(first.strip("[]:").upper())
    if level:
        return level, "app", after
    return _guess_level(rest), "app", rest
