
from __future__ import annotations

import subprocess
from typing import Optional, Tuple

from mlops.config import GPU_BOSTA_ESIK_YUZDE


def gpu_kullanim_yuzdesi() -> Optional[int]:
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
        ilk_satir = sonuc.stdout.strip().splitlines()[0]
        return int(ilk_satir.strip())
    except (ValueError, IndexError):
        return None


def gpu_bosta_mi() -> Tuple[bool, Optional[int]]:
    yuzde = gpu_kullanim_yuzdesi()
    if yuzde is None:
        return False, None
    return yuzde < GPU_BOSTA_ESIK_YUZDE, yuzde
