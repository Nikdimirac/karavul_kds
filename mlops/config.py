# -*- coding: utf-8 -*-
"""
mlops/config.py
================
KARAVUL — "Sürekli Öğrenme" MLOps Boru Hattı — MERKEZİ AYAR DOSYASI.

Bu paket (`mlops/`), kullanıcı talebi ("projenin 7/24 kendi kendini
geliştirebilmesi için otomatik olarak yeni kriz senaryoları üretip modeli
eğitecek bir MLOps/otomasyon altyapısı kur") üzerine eklendi.

MİMARİ KARAR (kullanıcı onayıyla — bkz. oturum kaydı): "KAPILI (onaylı)
geçiş" seçildi. Yani bu paketteki HİÇBİR script, canlı `karavul-kurmay`
Ollama etiketine YAZMAZ. Otomatik döngü (`orchestrator.py`) en fazla
`CANDIDATE_TAG` (`karavul-kurmay-candidate`) adlı AYRI bir etikete kadar
gider ve orada durur; canlıya geçiş (promotion) SADECE insan tarafından,
`promote_candidate.py` çalıştırılarak yapılır. Gerekçe: projenin kendi
tarihinde (bkz. proje kökündeki AI_MEMORY.md §4) `rope_theta` alanının
sessizce kaybolması gibi, KISA testlerde fark edilmeyen ama gerçek
kullanımda modeli bozan bir regresyon YAŞANDI — otomatik/gözetimsiz
promosyon, aynı sınıf bir hatanın bir sunum ORTASINDA canlıya sızmasını
YAPISAL olarak mümkün kılardı.

Kullanıcı ayrıca otomatik bir Windows Görev Zamanlayıcı tetikleyicisi
İSTEMEDİ ("zamanlamayı ben tetikleyeceğim") — bu yüzden bu pakette
`schtasks` KAYDI YOKTUR; `orchestrator.py` SADECE elle (veya kullanıcının
kendi kuracağı bir zamanlayıcıyla) çalıştırılır.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# CANLI HATA DÜZELTMESİ: bu modül `URETIM_ETIKETI`yi `os.getenv(...)` ile
# okur — `.env` proje genelinde `src/core/database.py` TARAFINDAN yüklenir,
# ama Python import SIRASI garanti DEĞİLDİR (ör. `eval_harness.py`de bu
# modül `src.core.database`den ÖNCE import edilirse, `.env` HENÜZ
# yüklenmemiş olur ve `URETIM_ETIKETI` SESSİZCE yanlış/varsayılan bir
# değere düşer). Bu modül artık KENDİ `.env`ini yükler — `database.py`nin
# AYNI çağrısıyla (idempotent, `python-dotenv` zaten yüklenmiş değişkenleri
# tekrar OKUMAZ) çakışmaz, import sırasından TAMAMEN bağımsız hale gelir.
load_dotenv()

PROJE_KOKU = Path(__file__).resolve().parent.parent
MLOPS_DIZINI = PROJE_KOKU / "mlops"
RAPORLAR_DIZINI = MLOPS_DIZINI / "reports"
DURUM_DOSYASI = MLOPS_DIZINI / "state.json"
DONGU_LOG_DOSYASI = MLOPS_DIZINI / "CYCLE_LOG.md"

VERI_SETI_DOSYASI = PROJE_KOKU / "karavul_dataset.jsonl"
LORA_DIZINI = PROJE_KOKU / "karavul_kurmay_lora"
MERGED_DIZINI = PROJE_KOKU / "karavul_kurmay_merged"

# --- Servis adresleri (api.py/.env ile AYNI varsayılanlar) -----------------
API_TABAN_URL = os.getenv("KARAVUL_API_BASE_URL", "http://localhost:8000")
NEO4J_HTTP_URL = os.getenv("KARAVUL_NEO4J_HTTP_URL", "http://localhost:7474")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# --- Model etiketleri --------------------------------------------------
# ÜRETİM etiketi HER ZAMAN `.env`deki `OLLAMA_TACTICAL_MODEL`den okunur —
# bu dosyada SABİT bir isim YAZILMAZ ki `.env` değişirse (ör. kullanıcı
# başka bir prod etiketine geçerse) mlops paketi SESSİZCE eskimiş bir isme
# göre çalışmaya devam etmesin.
URETIM_ETIKETI = os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay")
CANDIDATE_TAG = os.getenv("KARAVUL_CANDIDATE_ETIKETI", "karavul-kurmay-candidate")
CANDIDATE_F16_TAG = f"{CANDIDATE_TAG}-f16"  # ara adım — bkz. orchestrator.py "GGUF İÇE AKTARMA" notu

# --- Eğitim tetikleme eşiği ----------------------------------------------
YENI_ORNEK_ESIGI = int(os.getenv("KARAVUL_YENI_ORNEK_ESIGI", "150"))
"""Son eğitimden bu yana dataset'e eklenen GEÇERLİ örnek sayısı bu eşiği
AŞMADIKÇA `orchestrator.py` yeniden eğitim TETİKLEMEZ (sadece senaryo
toplamaya devam eder) — küçük artışlar için saatler süren bir GPU-bağlayıcı
QLoRA turu israf olurdu."""

# --- Conda ortamı (fine-tuning yığını SADECE burada kurulu — bkz. proje
#     köküne AI_MEMORY.md §6 "Python ortamları — KARIŞTIRMA") -------------
CONDA_ORTAM_ADI = os.getenv("KARAVUL_CONDA_ORTAM", "karavul")


def _egitim_python_yolu_bul(ortam_adi: str) -> Path:
    """`train_model.py`/`test_merge_infer.py` (unsloth/torch>=2.4/trl)
    HANGİ python.exe İLE çalıştırılacağını, `conda` KABUĞUNA HİÇ
    başvurmadan bulur.

    CANLI HATA (2026-09-08 gece): `orchestrator.py` önceden `conda run -n
    karavul python ...` kullanıyordu — Windows'ta `conda` PATH'te
    olmadığından `[WinError 2]` ile patladı. İLK düzeltme (`sys.executable`
    kullan) YANLIŞ bir varsayıma dayanıyordu: `orchestrator.py`'nin KENDİSİ
    (`eval_harness`/`war_gaming` üzerinden `neo4j`/`python-dotenv`
    GEREKTİRİR — bkz. AI_MEMORY.md §6 "Python ortamları") sistem Python'ında
    (Python312) çalıştırılmalı, ama o ortamda torch SADECE 2.3.1 (unsloth
    >=2.4 ister) — yani `sys.executable` iki gereksinimi de KARŞILAYAMAZ.
    Bu yüzden EĞİTİM adımı, `conda` kabuğuna GİRMEDEN, doğrudan `karavul`
    ortamının python.exe'sinin STANDART Miniconda/Anaconda dizin yoluyla
    (`<taban>/envs/<ortam_adi>/python.exe`) bulunmasıyla ayrı tutulur —
    `KARAVUL_TRAIN_PYTHON_EXE` ortam değişkeniyle HER ZAMAN geçersiz
    kılınabilir (farklı bir kurulum/kullanıcı için)."""
    override = os.getenv("KARAVUL_TRAIN_PYTHON_EXE")
    if override:
        return Path(override)
    ev_dizini = Path.home()
    for taban in ("miniconda3", "anaconda3", "Miniconda3", "Anaconda3"):
        aday = ev_dizini / taban / "envs" / ortam_adi / "python.exe"
        if aday.exists():
            return aday
    # Hiçbir standart yol bulunamadıysa mevcut yorumlayıcıya düş — en azından
    # AÇIK bir "ModuleNotFoundError: unsloth" hatası verir, `conda run`ın
    # SESSİZ [WinError 2]'sinden daha teşhis edilebilirdir.
    return Path(sys.executable)


TRAIN_PYTHON_EXE = _egitim_python_yolu_bul(CONDA_ORTAM_ADI)
"""EĞİTİM alt-süreçleri (`train_model.py`, `test_merge_infer.py`) için
kullanılacak python.exe — `orchestrator.py`'yi çalıştıran yorumlayıcıdan
(`sys.executable`, sistem Python'ı olmalı) BİLİNÇLİ OLARAK FARKLI."""

# --- Değerlendirme (eval) eşikleri ----------------------------------------
GPU_BOSTA_ESIK_YUZDE = int(os.getenv("KARAVUL_GPU_BOSTA_ESIK", "90"))
"""`nvidia-smi` kullanım yüzdesi bu değerin ALTINDAYSA GPU "boşta" sayılır
— ağır (eğitim) adımlardan önce kontrol edilir (bkz. `gpu_check.py`).
Senaryo/veri toplama adımı için KONTROL EDİLMEZ (bkz. `orchestrator.py`
docstring'i — o adım zaten normal kullanımla aynı büyüklükte bir GPU
yüküdür, engelleyici DEĞİLDİR).

KULLANICI TALEBİYLE %15 → %90 YÜKSELTİLDİ (2026-09-09): eski %15 eşik,
Kurucu arka planda SIRADAN uygulamalar (tarayıcı/Wallpaper Engine/Discord
vb. — hiçbiri QLoRA ile GERÇEKTEN yarışmaz) açıkken bile eğitimi
başlatmıyordu; ayrıca [B] veri toplama adımının KENDİSİ Ollama modelini
VRAM'de bırakıp bir SONRAKİ turda YANLIŞ "meşgul" okumasına yol açıyordu
(bkz. `orchestrator.py`'deki "[B sonrası]" model-boşaltma düzeltmesi — O
düzeltme birincil çözümdür, bu eşik SADECE ikincil bir güvenlik payı).
%90 hâlâ TAM/neredeyse-tam bir GPU kapma senaryosunu (ör. ağır bir oyun)
yakalar ama günlük kullanım kalıplarını ARTIK ENGELLEMEZ. `KARAVUL_GPU_
BOSTA_ESIK` ortam değişkeniyle her zaman geçersiz kılınabilir."""

for _dizin in (MLOPS_DIZINI, RAPORLAR_DIZINI):
    _dizin.mkdir(parents=True, exist_ok=True)
