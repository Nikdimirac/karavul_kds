# -*- coding: utf-8 -*-
"""
mlops/state.py
===============
`mlops/state.json`i okuyan/yazan ince bir yardımcı — orkestratörün
"son eğitimden bu yana dataset kaç satır büyüdü" ve "en son hangi aday
sürümü üretildi" gibi TURLAR ARASI hafızasını tutar (proje kökündeki
AI_MEMORY.md'nin aynı "oturumlar arası süreklilik" felsefesinin, tek bir
otomatik betik için küçük ölçekli karşılığı).

Kasıtlı olarak Neo4j/veritabanı KULLANILMAZ — bu SADECE yerel bir sayaç
dosyasıdır, projenin asıl Bilgi Grafı'ndan TAMAMEN bağımsızdır.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from mlops.config import DURUM_DOSYASI


@dataclass
class OrkestratorDurumu:
    son_egitim_dataset_boyutu: int = 0
    """En son eğitim TETİKLENDİĞİ andaki `karavul_dataset.jsonl` satır
    sayısı — bir sonraki turda "yeterince yeni örnek birikti mi?" sorusu
    bu değere göre cevaplanır (bkz. `config.YENI_ORNEK_ESIGI`)."""

    aday_surum_sayaci: int = 0
    """Kaç aday model üretildiğinin sayacı — HER yeni aday
    `karavul-kurmay-candidate` ETİKETİNİN ÜZERİNE yazılır (Ollama tag'leri
    zaten tek bir isme tek bir sürüm tutar), ama bu sayaç rapor
    dosyalarının adında/İÇİNDE "kaçıncı aday" olduğunu izlemek için tutulur."""

    son_dongu_zamani_utc: Optional[str] = None
    son_dongu_ozeti: Optional[str] = None
    son_aday_rapor_yolu: Optional[str] = None
    gecmis: list = field(default_factory=list)
    """Son birkaç turun kısa özeti (en yeni SONDA) — `CYCLE_LOG.md` ile
    AYNI bilgiyi makine tarafından okunabilir biçimde tekrarlar."""


def durumu_yukle() -> OrkestratorDurumu:
    if not DURUM_DOSYASI.exists():
        return OrkestratorDurumu()
    try:
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as f:
            veri = json.load(f)
        return OrkestratorDurumu(**{k: v for k, v in veri.items() if k in OrkestratorDurumu.__dataclass_fields__})
    except (json.JSONDecodeError, OSError, TypeError):
        # BOZUK/eksik state dosyası betiği ASLA çökertmemeli — sıfırdan
        # başlamak (en fazla bir sonraki eğitim tetiklemesi bir tur geç
        # gelir) her zaman "sessizce çökme"den daha güvenlidir.
        return OrkestratorDurumu()


def durumu_kaydet(durum: OrkestratorDurumu) -> None:
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(asdict(durum), f, ensure_ascii=False, indent=2)


def simdi_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
