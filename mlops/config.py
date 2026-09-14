

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJE_KOKU = Path(__file__).resolve().parent.parent
MLOPS_DIZINI = PROJE_KOKU / "mlops"
RAPORLAR_DIZINI = MLOPS_DIZINI / "reports"
DURUM_DOSYASI = MLOPS_DIZINI / "state.json"
DONGU_LOG_DOSYASI = MLOPS_DIZINI / "CYCLE_LOG.md"

VERI_SETI_DOSYASI = PROJE_KOKU / "karavul_dataset.jsonl"
LORA_DIZINI = PROJE_KOKU / "karavul_kurmay_lora"
MERGED_DIZINI = PROJE_KOKU / "karavul_kurmay_merged"

API_TABAN_URL = os.getenv("KARAVUL_API_BASE_URL", "http://localhost:8000")
NEO4J_HTTP_URL = os.getenv("KARAVUL_NEO4J_HTTP_URL", "http://localhost:7474")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

URETIM_ETIKETI = os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay")
CANDIDATE_TAG = os.getenv("KARAVUL_CANDIDATE_ETIKETI", "karavul-kurmay-candidate")
CANDIDATE_F16_TAG = f"{CANDIDATE_TAG}-f16" 

YENI_ORNEK_ESIGI = int(os.getenv("KARAVUL_YENI_ORNEK_ESIGI", "150"))
"""Son eğitimden bu yana dataset'e eklenen GEÇERLİ örnek sayısı bu eşiği
AŞMADIKÇA `orchestrator.py` yeniden eğitim TETİKLEMEZ (sadece senaryo
toplamaya devam eder) — küçük artışlar için saatler süren bir GPU-bağlayıcı
QLoRA turu israf olurdu."""

CONDA_ORTAM_ADI = os.getenv("KARAVUL_CONDA_ORTAM", "karavul")


def _egitim_python_yolu_bul(ortam_adi: str) -> Path:
    
    override = os.getenv("KARAVUL_TRAIN_PYTHON_EXE")
    if override:
        return Path(override)
    ev_dizini = Path.home()
    for taban in ("miniconda3", "anaconda3", "Miniconda3", "Anaconda3"):
        aday = ev_dizini / taban / "envs" / ortam_adi / "python.exe"
        if aday.exists():
            return aday
    
    return Path(sys.executable)


TRAIN_PYTHON_EXE = _egitim_python_yolu_bul(CONDA_ORTAM_ADI)

GPU_BOSTA_ESIK_YUZDE = int(os.getenv("KARAVUL_GPU_BOSTA_ESIK", "90"))

for _dizin in (MLOPS_DIZINI, RAPORLAR_DIZINI):
    _dizin.mkdir(parents=True, exist_ok=True)
