# -*- coding: utf-8 -*-
"""
mlops/orchestrator.py
======================
KARAVUL — "SÜREKLİ ÖĞRENME" DÖNGÜSÜNÜN TEK GİRİŞ NOKTASI.

Kullanıcı talebiyle eklendi ("otomatik olarak yeni kriz senaryoları üretip
modeli eğitecek bir MLOps/otomasyon altyapısı kur"). Kullanıcının onayladığı
İKİ mimari karar (bkz. oturum kaydı + `mlops/config.py` başlığı):

  1. "KAPILI (onaylı) geçiş" — bu betik canlı `karavul-kurmay` Ollama
     etiketine ASLA yazmaz; en fazla `karavul-kurmay-candidate` adlı AYRI
     bir etikete kadar gider. Canlıya geçiş SADECE `promote_candidate.py`
     ile, insan onayıyla yapılır.
  2. "Zamanlamayı ben tetikleyeceğim" — bu betiğe HİÇBİR Windows Görev
     Zamanlayıcı kaydı EKLENMEZ; SADECE elle (`python -m mlops.orchestrator`)
     çalıştırılır.

BİR TUR NE YAPAR (sırayla):
  A) Ön koşul kontrolü — `api.py`/Neo4j/Ollama ayakta mı? (bkz. proje
     genelindeki "asla sessizce yutma" ilkesi — biri ayakta değilse betik
     AÇIKÇA nedenini yazıp çıkar, YARIM bir döngüyü SESSİZCE atlamaz.)
  B) VERİ TOPLAMA — `war_gaming.py`yi bir ALT SÜREÇ olarak, bu turluk küçük
     bir kotayla (`--senaryo-sayisi`, varsayılan 20) çalıştırır; GPU
     kontrolü YAPILMAZ (bkz. `gpu_check.py` docstring'i — bu adımın GPU
     yükü, normal API kullanımıyla ZATEN aynı büyüklüktedir, engelleyici
     DEĞİLDİR).
  C) EŞİK KONTROLÜ — son eğitimden bu yana dataset'e eklenen GEÇERLİ örnek
     sayısı `config.YENI_ORNEK_ESIGI`yi AŞMADIYSA burada durur (sadece veri
     toplamış olur — bu NORMAL ve BEKLENEN bir çıktıdır, hata DEĞİLDİR).
  D) GPU BOŞTA MI? — eşik aşıldıysa, AĞIR eğitim adımından ÖNCE
     `gpu_check.gpu_bosta_mi()` ile doğrulanır; meşgulse (ör. Kurucu bir
     oyun oynuyor) bu TUR burada durur, BİR SONRAKİ elle tetiklemede
     yeniden denenir — veri toplama adımı (B) ZATEN tamamlanmış olduğundan
     KAYIP yoktur.
  E) EĞİTİM → MERGE → GGUF İÇE AKTARMA — sırasıyla `train_model.py`,
     `test_merge_infer.py` (rope_theta düzeltmesi OTOMATİK uygulanır, bkz.
     o betiğin docstring'i) — İKİSİ DE `mlops.config.TRAIN_PYTHON_EXE`
     (ayrı `karavul` conda ortamının python.exe'si, `conda` KABUĞUNA
     GİRMEDEN doğrudan yol olarak bulunur — bkz. o sabitin docstring'i)
     İLE çalıştırılır; bu betiğin KENDİSİNİ çalıştıran yorumlayıcıdan
     (`sys.executable`, `neo4j`/`dotenv` gerektiğinden sistem Python'ı
     OLMALI) BİLİNÇLİ OLARAK FARKLI — o ortamda unsloth/torch>=2.4 YOK.
     (ESKİDEN `conda run -n karavul ...` kullanılıyordu — Windows'ta
     `conda` PATH'te olmadığından `[WinError 2]` ile patlıyordu; sonra
     KISA süreliğine yanlışlıkla `sys.executable`a değiştirildi — bu da
     `orchestrator.py`nin kendisi `neo4j` gerektirdiğinden ÇALIŞMADI, bkz.
     `config.py::_egitim_python_yolu_bul` docstring'i.) sonra
     `ollama create` ile ÖNCE F16 ara etiket, SONRA q4_K_M candidate etiketi
     üretilir (bkz. proje kökü `Modelfile`/`Modelfile.quant`teki KANITLANMIŞ
     İKİ AŞAMALI desenin AYNISI — tek adıma "sadeleştirilmedi" ki daha önce
     doğrulanmış davranıştan SAPILMASIN).
  F) DEĞERLENDİRME — `eval_harness.py` adayı üretimle KARŞILAŞTIRIR, bir
     Markdown rapor yazar.
  G) DURUM/GÜNLÜK — `state.json` ve `CYCLE_LOG.md` güncellenir; kullanıcıya
     TEK SATIRLIK, net bir "sıradaki adım senin" özeti basılır.

Kullanım:
    python -m mlops.orchestrator                     # varsayılan tur
    python -m mlops.orchestrator --senaryo-sayisi 40  # bu turda daha çok veri topla
    python -m mlops.orchestrator --sadece-veri        # eğitim adımını hiç deneme
    python -m mlops.orchestrator --egitimi-zorla      # eşik dolmamış olsa bile eğit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import requests

# bkz. `eval_harness.py`deki AYNI korumanın AYNI gerekçesi (Windows konsolu
# + emoji/Türkçe karakter -> UnicodeEncodeError).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlops import eval_harness  # noqa: E402
from mlops.config import (  # noqa: E402
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
from mlops.gpu_check import gpu_bosta_mi  # noqa: E402
from mlops.state import durumu_kaydet, durumu_yukle, simdi_utc_iso  # noqa: E402

VARSAYILAN_SENARYO_SAYISI = 20
EGITIM_ZAMAN_ASIMI_SANIYE = 4 * 60 * 60  # 4 saat — QLoRA + merge için CÖMERT ama SINIRLI bir tavan
QUICK_TIMEOUT = 20

# 2026-09-11'de train_model.py::SYSTEM_PROMPT ile SENKRONİZE edildi (Golden
# Dataset'in "KDS aktör değil danışman" ilkesi) — bu metin Ollama Modelfile'ının
# SYSTEM alanına gömülür, yani ADAY MODELİN gerçek çalışma zamanı davranışını
# belirler. Eski ("askeri lojistik emirleri üret") haliyle bırakılsaydı, model
# eğitim verisinin (danışman üslubu) TAM TERSİ bir kimlikle çalıştırılırdı.
# NOT: bu üçü (burası, train_model.py, test_merge_infer.py) AYNI metni ELLE
# tekrarlıyor; biri değişirse ÜÇÜ de değişmeli.
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
    """`True`/`False` başarı durumunu döner; ÇÖKMEZ (asla exception fırlatmaz) —
    çağıran taraf (bkz. `calistir`) başarısızlığı AÇIKÇA loglayıp döngüyü
    orada, kontrollü biçimde durdurur."""
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
    """E adımı — dönüşte `True` ise `karavul-kurmay-candidate` Ollama'da
    KULLANIMA HAZIRDIR (ama HÂLÂ üretime bağlanmamıştır)."""
    # BİLİNÇLİ OLARAK `sys.executable` DEĞİL, `TRAIN_PYTHON_EXE` — bu betiği
    # (`orchestrator.py`) çalıştıran yorumlayıcı `neo4j`/`dotenv` gerektiren
    # sistem Python'ı OLMALI (bkz. `eval_harness`/`war_gaming` importları),
    # ama o ortamda unsloth/torch>=2.4 YOK; eğitim alt-süreçleri bu yüzden
    # HER ZAMAN ayrı `karavul` conda ortamıyla çalıştırılır (bkz.
    # `mlops/config.py::_egitim_python_yolu_bul` docstring'i).
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

    # CANLI TESPİT (bu oturumda): `war_gaming.py`, senaryo üretmek için
    # ÜRETİM taktik modelini (`URETIM_ETIKETI`) Ollama üzerinden çağırır —
    # ve bu model VARSAYILAN OLARAK `keep_alive=Forever` ile VRAM'de
    # SÜRESİZ yüklü KALIR (bkz. `decision_engine.py`/`.env`). Bu yüzden
    # [D] GPU kontrolü B'DEN HEMEN SONRA çalışırsa, GPU'yu "meşgul" olarak
    # görür — AMA bu meşguliyet Kurucu'nun başka bir kullanımından DEĞİL,
    # ORCHESTRATOR'IN KENDİ B ADIMINDAN kaynaklanır (kendi kendini bloke
    # eden bir döngü — canlı testte GÖZLENDİ). Çözüm: B bittiğinde modeli
    # AÇIKÇA boşalt, GERÇEK (dış) GPU kullanımı D'de doğru ölçülsün.
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
