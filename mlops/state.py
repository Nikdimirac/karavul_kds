
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from mlops.config import DURUM_DOSYASI


@dataclass
class OrkestratorDurumu:
  
    aday_surum_sayaci: int = 0
   

    son_dongu_zamani_utc: Optional[str] = None
    son_dongu_ozeti: Optional[str] = None
    son_aday_rapor_yolu: Optional[str] = None
   


def durumu_yukle() -> OrkestratorDurumu:
    if not DURUM_DOSYASI.exists():
        return OrkestratorDurumu()
    try:
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as f:
            veri = json.load(f)
        return OrkestratorDurumu(**{k: v for k, v in veri.items() if k in OrkestratorDurumu.__dataclass_fields__})
    except (json.JSONDecodeError, OSError, TypeError):
       
        return OrkestratorDurumu()


def durumu_kaydet(durum: OrkestratorDurumu) -> None:
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(asdict(durum), f, ensure_ascii=False, indent=2)


def simdi_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
