# -*- coding: utf-8 -*-
"""
mlops/promote_candidate.py
============================
KARAVUL — SÜREKLİ ÖĞRENME DÖNGÜSÜNDEKİ TEK "CANLIYA YAZAN" ADIM.

`mlops/orchestrator.py` HİÇBİR ZAMAN üretim (`karavul-kurmay`) Ollama
etiketine dokunmaz — SADECE `karavul-kurmay-candidate` adlı AYRI bir etiket
üretir ve `mlops/reports/`e bir değerlendirme raporu yazar. Adayı canlıya
almak (promote), BİLİNÇLİ olarak SADECE bu betikle, İNSANIN kendi isteğiyle,
İNTERAKTİF bir onay adımından SONRA olur (bkz. `mlops/config.py`daki
"KAPILI (onaylı) geçiş" kararı — kullanıcının kendi seçimi).

GÜVENCE — GERİ ALMA: promosyondan ÖNCE, o anki üretim modeli
`karavul-kurmay-backup-<UTC zaman damgası>` adıyla YEDEKLENİR (bkz.
`ollama cp` — sadece bir manifest/etiket kopyalar, disk alanı İKİYE
KATLANMAZ). Bir sorun fark edilirse geri almak için:
    ollama cp karavul-kurmay-backup-<zaman damgası> karavul-kurmay

Kullanım (proje kökünden, elle):
    python -m mlops.promote_candidate
    python -m mlops.promote_candidate --onay   # interaktif "EVET" sorusunu atla (CI/otomasyon İÇİN DEĞİL — bkz. main())
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# bkz. `eval_harness.py`deki AYNI korumanın AYNI gerekçesi (Windows konsolu
# + emoji/Türkçe karakter -> UnicodeEncodeError).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlops.config import CANDIDATE_TAG, URETIM_ETIKETI  # noqa: E402
from mlops.state import durumu_yukle  # noqa: E402


def _ollama_liste() -> str:
    sonuc = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30, check=False)
    return sonuc.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description="KARAVUL — aday modeli üretime AL (insan onaylı gate)")
    ap.add_argument(
        "--onay",
        action="store_true",
        help="İnteraktif 'EVET' sorusunu atla. DİKKAT: bu bayrak SADECE siz bu komutu bilerek/isteyerek "
        "çalıştırdığınızda kullanılmalı — otomatik bir zamanlayıcıya/betiğe BAĞLAMAYIN (bu, projenin "
        "'KAPILI geçiş' tasarım kararını BYPASS eder).",
    )
    args = ap.parse_args()

    liste = _ollama_liste()
    if CANDIDATE_TAG not in liste:
        print(f"🛑 '{CANDIDATE_TAG}' Ollama'da bulunamadı — önce `python -m mlops.orchestrator` ile bir aday üretin.")
        return 1

    durum = durumu_yukle()
    print("=" * 78)
    print("KARAVUL — ADAY MODELİ ÜRETİME ALMA")
    print("=" * 78)
    print(f"Aday etiket : {CANDIDATE_TAG}")
    print(f"Üretim etiketi (değiştirilecek): {URETIM_ETIKETI}")
    if durum.son_aday_rapor_yolu:
        print(f"Son değerlendirme raporu: {durum.son_aday_rapor_yolu}")
    print("Son değerlendirme raporlarının TAMAMI için: mlops/reports/ dizinine bakın.")
    print()
    print("⚠️  Devam etmeden ÖNCE ilgili raporu (mlops/reports/) OKUDUĞUNUZDAN emin olun —")
    print("    bu betik raporu SİZİN YERİNİZE DEĞERLENDİRMEZ, sadece SİZİN kararınızı UYGULAR.")
    print()

    if not args.onay:
        yanit = input(f"'{CANDIDATE_TAG}' → '{URETIM_ETIKETI}' olarak CANLIYA alınsın mı? (devam için tam olarak EVET yazın): ")
        if yanit.strip() != "EVET":
            print("İptal edildi — hiçbir değişiklik yapılmadı.")
            return 1

    zaman_damgasi = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    yedek_etiketi = f"{URETIM_ETIKETI}-backup-{zaman_damgasi}"

    yedek_alindi = False
    if URETIM_ETIKETI in liste:
        print(f"\n[1/2] Mevcut üretim modeli yedekleniyor → '{yedek_etiketi}'")
        yedek = subprocess.run(["ollama", "cp", URETIM_ETIKETI, yedek_etiketi], capture_output=True, text=True, timeout=60)
        if yedek.returncode != 0:
            print(f"🛑 Yedekleme başarısız oldu, PROMOSYON İPTAL edildi (güvenlik için): {yedek.stderr}")
            return 1
        yedek_alindi = True
        print(f"  ✅ Yedeklendi. Geri almak için: ollama cp {yedek_etiketi} {URETIM_ETIKETI}")
    else:
        print(f"\n[1/2] '{URETIM_ETIKETI}' henüz yok (ilk promosyon) — yedekleme atlandı.")

    print(f"\n[2/2] '{CANDIDATE_TAG}' → '{URETIM_ETIKETI}' olarak üretime alınıyor")
    promosyon = subprocess.run(["ollama", "cp", CANDIDATE_TAG, URETIM_ETIKETI], capture_output=True, text=True, timeout=60)
    if promosyon.returncode != 0:
        print(f"🛑 Promosyon başarısız oldu: {promosyon.stderr}")
        return 1

    print(f"\n✅ '{URETIM_ETIKETI}' artık aday modeli kullanıyor. `.env` değişmedi, api.py'yi YENİDEN BAŞLATMANIZA GEREK YOK")
    print("   (Ollama etiketleri istek anında çözülür).")
    if yedek_alindi:
        print(f"   Bir sorun fark ederseniz: ollama cp {yedek_etiketi} {URETIM_ETIKETI}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
