# syntax=docker/dockerfile:1
# =============================================================================
# Kriz ve Afet Yönetimi Karar Destek Sistemi — Streamlit arayüz/backend imajı
# -----------------------------------------------------------------------------
# Kapalı devre (on-premise / air-gapped) TSK, AFAD, UMKE dağıtımı için:
# imaj bir kez internet erişimi olan bir ortamda build edilir, ardından
# kapalı ağdaki sunuculara `docker save` / `docker load` ile taşınabilir.
# Neo4j ve Ollama ayrı konteynerlerdedir (bkz. docker-compose.yml); bu imaj
# yalnızca Python uygulama katmanını (Streamlit UI + ETL/NLP kodu) içerir.
# =============================================================================

FROM python:3.12-slim AS base

# Python konteyner içinde .pyc yazmasın (ekstra I/O) ve stdout/stderr
# tamponlanmasın (docker logs anlık aksın). PIP_NO_CACHE_DIR imaj boyutunu
# küçültür.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Sağlık kontrolü (healthcheck) `curl` kullanır; imajda varsayılan olarak
# yoktur. Katman önbelleklemesi (layer caching) için requirements.txt'i
# kaynak kodun geri kalanından ÖNCE kopyalayıp kur — kaynak değiştiğinde
# bağımlılıklar yeniden indirilmez.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kaynak kodun tamamını kopyala (.dockerignore .env/venv/önbellek gibi
# gereksiz/güvensiz dosyaları hariç tutar).
COPY . .

# Kök haklarıyla çalışmamak için ayrı bir servis kullanıcısı — üretim/askeri
# ortam sertleştirme (hardening) standardı.
RUN useradd --create-home --shell /usr/sbin/nologin kds \
    && chown -R kds:kds /app
USER kds

EXPOSE 8501

# Streamlit'in kendi sağlık uç noktası (_stcore/health); Neo4j/Ollama henüz
# hazır değilken bile UI ayakta kalabilir (bkz. app.py'deki bağlantı hata
# yönetimi) — bu yüzden healthcheck yalnızca Streamlit SÜRECİNİ, servisler
# arası bağımlılığı DEĞİL, doğrular (o docker-compose.yml'nin işi).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# `--server.address=0.0.0.0`: konteyner dışından (host/ağ) erişilebilir olsun;
# varsayılan Streamlit yalnızca localhost'a bağlanır ve konteyner dışına
# HİÇBİR isteğe cevap vermez.
ENTRYPOINT ["streamlit", "run", "src/ui/app.py", \
            "--server.address=0.0.0.0", \
            "--server.port=8501", \
            "--server.headless=true", \
            "--browser.gatherUsageStats=false"]
