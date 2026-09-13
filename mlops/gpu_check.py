# -*- coding: utf-8 -*-
"""
mlops/gpu_check.py
===================
`nvidia-smi` ile GPU kullanım yüzdesini okuyan ince bir yardımcı.

NEDEN: RTX 4070 Ti, Kurucu'nun diğer uygulamalarıyla (CS2, Wallpaper
Engine, Play Games emülatörü — bkz. proje kökü AI_MEMORY.md §6) paylaşılan
BİR TEK GPU'dur. QLoRA fine-tuning (bkz. `train_model.py`) GPU'yu 30-90+
dakika boyunca NEREDEYSE TAMAMEN meşgul eder; bu ağır adım GPU zaten
meşgulken tetiklenirse hem eğitim inanılmaz yavaşlar/OOM verebilir HEM DE
Kurucu'nun o anki kullanımını (ör. bir oyun ortasında FPS düşüşü) bozar.
Bu yüzden `orchestrator.py`, eğitim adımından ÖNCE bu modülle GPU'nun
GERÇEKTEN boşta olduğunu doğrular; meşgulse o turun eğitim adımını atlar
(senaryo/veri toplama adımı ETKİLENMEZ — bkz. `orchestrator.py` docstring'i).
"""

from __future__ import annotations

import subprocess
from typing import Optional, Tuple

from mlops.config import GPU_BOSTA_ESIK_YUZDE


def gpu_kullanim_yuzdesi() -> Optional[int]:
    """`nvidia-smi`den ANLIK GPU kullanım yüzdesini okur. `nvidia-smi`
    bulunamazsa/başarısız olursa `None` döner (ÇÖKMEZ) — çağıran taraf bu
    durumu "bilinmiyor, temkinli davran" olarak yorumlamalı."""
    try:
        sonuc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if sonuc.returncode != 0 or not sonuc.stdout.strip():
        return None
    try:
        # Birden fazla GPU varsa İLK satır (bu projenin tek-GPU geliştirme
        # makinesi için yeterli — bkz. AI_MEMORY.md "RTX 4070 Ti 12GB" notu).
        ilk_satir = sonuc.stdout.strip().splitlines()[0]
        return int(ilk_satir.strip())
    except (ValueError, IndexError):
        return None


def gpu_bosta_mi() -> Tuple[bool, Optional[int]]:
    """`(bosta_mi, kullanim_yuzdesi)` döner. `nvidia-smi` OKUNAMIYORSA
    (ör. sürücü sorunlu/uzak makine) TEMKİNLİ davranılır: "boşta DEĞİL"
    varsayılır (`False`, `None`) — ağır bir GPU işini körlemesine
    başlatmaktansa bir turu atlamak HER ZAMAN daha güvenlidir."""
    yuzde = gpu_kullanim_yuzdesi()
    if yuzde is None:
        return False, None
    return yuzde < GPU_BOSTA_ESIK_YUZDE, yuzde
