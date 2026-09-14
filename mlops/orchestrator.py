

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlops import eval_harness  
from mlops.config import (  
    API_TABAN_URL,
    CANDIDATE_F16_TAG,
    CANDIDATE_TAG,
    DONGU_LOG_DOSYASI,
    LORA_DIZINI,
    MERGED_DIZINI,
    MLOPS_DIZINI,
    NEO4J_HTTP_URL,
    OLLAMA_BASE_URL,
    RAPORLAR_DIZINI,
    TRAIN_PYTHON_EXE,
    URETIM_ETIKETI,
    VERI_SETI_DOSYASI,
    YENI_ORNEK_ESIGI,
)
from mlops.gpu_check import gpu_bosta_mi  
from mlops.state import durumu_kaydet, durumu_yukle, simdi_utc_iso  

VARSAYILAN_SENARYO_SAYISI = 20
EGITIM_ZAMAN_ASIMI_SANIYE = 4 * 60 * 60  
QUICK_TIMEOUT = 20


_SYSTEM_PROMPT = (
    "Sen Karavul Kriz ve Afet Yönetimi Karar Destek Sistemi'nin kurmay "
    "zekasısın. Bir EYLEM SİSTEMİ DEĞİLSİN — yetkili insan karar vericiye "
    "seçenek sunan bir DANIŞMANSIN. ASLA emir verme, ASLA bir eylemi kendin "
    "gerçekleştirdiğini iddia etme ('sevk ettim', 'söndürdüm' gibi). Uygun "
    "birlik/kaynak bulunamıyorsa bunu DÜRÜSTÇE belirt, ASLA uydurma. "
    "Yanıtını her zaman şu 4 başlık altında ver: DURUM SENTEZİ, HAREKET "
    "TARZLARI (ALPHA/BRAVO SEÇENEKLERİ), KRİTİK DARBOĞAZLAR, KARAR/ONAY "
    "NOKTASI."
)


def _adim_basligi(metin: str) -> None:
    print("\n" + "-" * 78)
    print(metin)
    print("-" * 78)


def _servisleri_kontrol_et() -> Optional[str]:
    """Ayakta olmayan İLK servisin adını döner; hepsi ayaktaysa `None`."""
    kontroller = [
        ("api.py", f"{API_TABAN_URL}/health"),
        ("Neo4j", NEO4J_HTTP_URL),
        ("Ollama", f"{OLLAMA_BASE_URL}/"),
    ]
    for ad, url in kontroller:
        try:
            requests.get(url, timeout=QUICK_TIMEOUT).raise_for_status()
        except requests.RequestException as exc:
            return f"{ad} ({url}) yanıt vermiyor: {exc}"
    return None


def _dataset_satir_sayisi() -> int:
    if not VERI_SETI_DOSYASI.exists():
        return 0
    with open(VERI_SETI_DOSYASI, "r", encoding="utf-8") as f:
        return sum(1 for satir in f if satir.strip())


def _alt_surec_calistir(komut: List[str], *, zaman_asimi: float, ortam_aciklama: str) -> bool:
 
    print(f"  $ {' '.join(komut)}  (ortam: {ortam_aciklama})")
    try:
        sonuc = subprocess.run(komut, cwd=str(MLOPS_DIZINI.parent), timeout=zaman_asimi, check=False)
    except FileNotFoundError as exc:
        print(f"  🛑 Komut bulunamadı ({exc}) — '{ortam_aciklama}' PATH'te mi kurulu, kontrol edin.")
        return False
    except subprocess.TimeoutExpired:
        print(f"  🛑 Zaman aşımı ({zaman_asimi:.0f} sn) — işlem durduruldu.")
        return False
    if sonuc.returncode != 0:
        print(f"  🛑 Çıkış kodu {sonuc.returncode} (0 beklenirdi).")
        return False
    return True


def _egitim_bloğunu_calistir() -> bool:
   
    egitim_python = str(TRAIN_PYTHON_EXE)
    _adim_basligi("[E1/4] QLoRA fine-tuning (%s) — bu adım UZUN sürebilir (dakikalar-saatler)." % egitim_python)
    if not _alt_surec_calistir(
        [egitim_python, "train_model.py"],
        zaman_asimi=EGITIM_ZAMAN_ASIMI_SANIYE,
        ortam_aciklama=f"python:{egitim_python}",
    ):
        return False
    if not LORA_DIZINI.exists():
        print(f"  🛑 Beklenen LoRA çıktı dizini yok: {LORA_DIZINI}")
        return False

    _adim_basligi("[E2/4] Merge + rope_theta GGUF-uyumluluk düzeltmesi")
    if not _alt_surec_calistir(
        [egitim_python, "test_merge_infer.py"],
        zaman_asimi=EGITIM_ZAMAN_ASIMI_SANIYE,
        ortam_aciklama=f"python:{egitim_python}",
    ):
        return False
    if not MERGED_DIZINI.exists():
        print(f"  🛑 Beklenen merge çıktı dizini yok: {MERGED_DIZINI}")
        return False

    _adim_basligi(f"[E3/4] Ollama'ya F16 içe aktarma → '{CANDIDATE_F16_TAG}'")
    f16_modelfile = MLOPS_DIZINI / "_uretilen_Modelfile_f16"
    f16_modelfile.write_text(
        f'FROM {MERGED_DIZINI}\n\n'
        f'SYSTEM """{_SYSTEM_PROMPT}"""\n\n'
        'PARAMETER temperature 0.3\n'
        'PARAMETER repeat_penalty 1.15\n'
        'PARAMETER repeat_last_n 256\n'
        'PARAMETER num_ctx 8192\n',
        encoding="utf-8",
    )
    if not _alt_surec_calistir(
        ["ollama", "create", CANDIDATE_F16_TAG, "-f", str(f16_modelfile)],
        zaman_asimi=1800,
        ortam_aciklama="ollama CLI",
    ):
        return False

    _adim_basligi(f"[E4/4] q4_K_M'e requantize → '{CANDIDATE_TAG}'")
    quant_modelfile = MLOPS_DIZINI / "_uretilen_Modelfile_quant"
    quant_modelfile.write_text(
        f"FROM {CANDIDATE_F16_TAG}\n\n"
        "PARAMETER repeat_penalty 1.15\n"
        "PARAMETER repeat_last_n 256\n",
        encoding="utf-8",
    )
    if not _alt_surec_calistir(
        ["ollama", "create", CANDIDATE_TAG, "-f", str(quant_modelfile), "-q", "q4_K_M"],
        zaman_asimi=1800,
        ortam_aciklama="ollama CLI",
    ):
        return False

    print(f"  ✅ Aday model hazır: '{CANDIDATE_TAG}' (Ollama'da, ÜRETİME henüz BAĞLANMADI).")
    return True


def _dongu_gunlugune_ekle(satir: str) -> None:
    onceki = DONGU_LOG_DOSYASI.read_text(encoding="utf-8") if DONGU_LOG_DOSYASI.exists() else (
        "# KARAVUL MLOps — Döngü Günlüğü\n\n"
        "Her `python -m mlops.orchestrator` çalıştırmasının insan-okur özeti "
        "(en yeni SATIR EN ALTTA). Ayrıntılı makine-okur durum `state.json`dedir.\n\n"
    )
    DONGU_LOG_DOSYASI.write_text(onceki + satir + "\n", encoding="utf-8")


def calistir(senaryo_sayisi: int, sadece_veri: bool, egitimi_zorla: bool) -> int:
    _adim_basligi("[A] Ön koşul kontrolü")
    eksik = _servisleri_kontrol_et()
    if eksik:
        print(f"  🛑 {eksik}")
        print("  → Bu tur İPTAL edildi (bkz. proje geneli 'asla sessizce yutma' ilkesi).")
        return 1
    print("  ✅ api.py / Neo4j / Ollama hepsi ayakta.")

    durum = durumu_yukle()

    _adim_basligi(f"[B] Veri toplama — hedef: {senaryo_sayisi} yeni geçerli senaryo")
    env = os.environ.copy()
    env["KARAVUL_MAX_BASARILI_SENARYO"] = str(senaryo_sayisi)
    env["KARAVUL_MAX_TOPLAM_DENEME"] = str(max(senaryo_sayisi * 6, 60))
    try:
        subprocess.run(
            [sys.executable, "war_gaming.py"],
            cwd=str(MLOPS_DIZINI.parent),
            env=env,
            timeout=senaryo_sayisi * 60 + 300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠️ Veri toplama zaman aşımına uğradı — biriken kadarıyla devam ediliyor.")

    
    _adim_basligi(f"[B sonrası] Ollama '{URETIM_ETIKETI}' modeli VRAM'den boşaltılıyor")
    if not _alt_surec_calistir(
        ["ollama", "stop", URETIM_ETIKETI],
        zaman_asimi=30,
        ortam_aciklama="ollama CLI",
    ):
        print("  ⚠️ Model boşaltılamadı (kritik değil — [D] GPU kontrolü yine de yapılacak, gerçek durumu yansıtır).")

    yeni_toplam = _dataset_satir_sayisi()
    yeni_ornek_sayisi = max(0, yeni_toplam - durum.son_egitim_dataset_boyutu)
    print(f"  Dataset şu an {yeni_toplam} satır (son eğitimden bu yana +{yeni_ornek_sayisi}).")

    if sadece_veri:
        _ozet = f"{simdi_utc_iso()} — SADECE VERİ turu: dataset {yeni_toplam} satıra ulaştı (+{yeni_ornek_sayisi})."
        durum.son_dongu_zamani_utc = simdi_utc_iso()
        durum.son_dongu_ozeti = _ozet
        durumu_kaydet(durum)
        _dongu_gunlugune_ekle(_ozet)
        print(f"\n✅ {_ozet}")
        return 0

    _adim_basligi("[C] Eşik kontrolü")
    if not egitimi_zorla and yeni_ornek_sayisi < YENI_ORNEK_ESIGI:
        _ozet = (
            f"{simdi_utc_iso()} — Eşik dolmadı ({yeni_ornek_sayisi}/{YENI_ORNEK_ESIGI} yeni örnek) — "
            "eğitim TETİKLENMEDİ, sadece veri toplandı."
        )
        durum.son_dongu_zamani_utc = simdi_utc_iso()
        durum.son_dongu_ozeti = _ozet
        durumu_kaydet(durum)
        _dongu_gunlugune_ekle(_ozet)
        print(f"  ℹ️ {yeni_ornek_sayisi}/{YENI_ORNEK_ESIGI} — eğitim İÇİN yeterli yeni örnek YOK, bu turda durduruluyor.")
        print(f"\n✅ {_ozet}")
        return 0
    print(f"  ✅ Eşik aşıldı ({yeni_ornek_sayisi}/{YENI_ORNEK_ESIGI}) — eğitim tetiklenecek." if not egitimi_zorla
          else "  ⚠️ --egitimi-zorla verildi — eşikten BAĞIMSIZ eğitim tetikleniyor.")

    _adim_basligi("[D] GPU boşta mı?")
    bosta, yuzde = gpu_bosta_mi()
    if not bosta:
        _ozet = (
            f"{simdi_utc_iso()} — GPU meşgul (%{yuzde if yuzde is not None else '?'}),"
            f" eğitim bu turda ATLANDI (veri toplama tamamlandı, {yeni_ornek_sayisi} yeni örnek bekliyor)."
        )
        durum.son_dongu_zamani_utc = simdi_utc_iso()
        durum.son_dongu_ozeti = _ozet
        durumu_kaydet(durum)
        _dongu_gunlugune_ekle(_ozet)
        print(f"  🛑 GPU meşgul (%{yuzde}) — eğitim bu turda ATLANDI. Bir sonraki elle tetiklemede tekrar denenecek.")
        print(f"\n✅ {_ozet}")
        return 0
    print(f"  ✅ GPU boşta (%{yuzde if yuzde is not None else 'bilinmiyor'}).")

    _adim_basligi("[E] Eğitim → Merge → GGUF içe aktarma")
    if not _egitim_bloğunu_calistir():
        _ozet = f"{simdi_utc_iso()} — Eğitim/merge/içe aktarma BAŞARISIZ oldu (ayrıntı için terminal çıktısına bakın)."
        durum.son_dongu_zamani_utc = simdi_utc_iso()
        durum.son_dongu_ozeti = _ozet
        durumu_kaydet(durum)
        _dongu_gunlugune_ekle(_ozet)
        print(f"\n🛑 {_ozet}")
        return 1

    durum.son_egitim_dataset_boyutu = yeni_toplam
    durum.aday_surum_sayaci += 1

    _adim_basligi("[F] Değerlendirme (aday vs üretim)")
    rapor_adi = f"aday_{durum.aday_surum_sayaci}_vs_uretim.md"
    onceki_argv = sys.argv
    sys.argv = ["eval_harness.py", "--etiket", CANDIDATE_TAG, "--karsilastir", URETIM_ETIKETI, "--rapor-adi", rapor_adi]
    try:
        eval_harness.main()
    finally:
        sys.argv = onceki_argv

    durum.son_aday_rapor_yolu = str(RAPORLAR_DIZINI / rapor_adi)
    _ozet = (
        f"{simdi_utc_iso()} — Aday #{durum.aday_surum_sayaci} ('{CANDIDATE_TAG}') hazır. "
        f"Rapor: {durum.son_aday_rapor_yolu}. SONRAKİ ADIM (insan): raporu incele, uygunsa "
        "`python -m mlops.promote_candidate` çalıştır."
    )
    durum.son_dongu_zamani_utc = simdi_utc_iso()
    durum.son_dongu_ozeti = _ozet
    durumu_kaydet(durum)
    _dongu_gunlugune_ekle(_ozet)
    print(f"\n✅ {_ozet}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KARAVUL — Sürekli Öğrenme Orkestratörü (elle tetiklenir)")
    ap.add_argument("--senaryo-sayisi", type=int, default=VARSAYILAN_SENARYO_SAYISI, help="Bu turda toplanacak yeni geçerli senaryo hedefi")
    ap.add_argument("--sadece-veri", action="store_true", help="SADECE veri topla, eğitim adımını hiç deneme")
    ap.add_argument("--egitimi-zorla", action="store_true", help="Eşik dolmamış olsa bile eğitimi tetikle")
    args = ap.parse_args()
    return calistir(args.senaryo_sayisi, args.sadece_veri, args.egitimi_zorla)


if __name__ == "__main__":
    sys.exit(main())
