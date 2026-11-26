#!/usr/bin/env sh
# macOS / Linux için başlatıcı:  sh baslat.sh
cd "$(dirname "$0")" || exit 1
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python bulunamadı. https://www.python.org/downloads/ adresinden Python 3.10+ kurun."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "[İlk kurulum] Sanal ortam oluşturuluyor..."
  "$PY" -m venv .venv || exit 1
fi
if ! .venv/bin/python -c "import anthropic" >/dev/null 2>&1; then
  echo "[Kurulum] Claude bağlantısı için anthropic paketi kuruluyor (internet gerekir)..."
  .venv/bin/python -m pip install --disable-pip-version-check -q --retries 1 --timeout 15 -r requirements.txt \
    || echo "[Uyarı] Paket kurulamadı. Uygulama simülasyon modunda çalışır."
fi
exec .venv/bin/python app.py "$@"
