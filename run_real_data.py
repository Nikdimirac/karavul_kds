"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
GERÇEK VERİ ÇALIŞTIRMA SCRIPTİ — Mock/sahte TUCBS verisini (bkz.
`test_etl_pipeline.py`, `tucbs_etl_loader.generate_mock_tucbs_data`) BIRAKIP,
OpenStreetMap Overpass API'sinden çekilen GERÇEK şehir topolojisini
(`real_osm_loader.RealOsmLoader`) Neo4j Bilgi Grafı'na yükler.

Akış:
  1. Neo4j'e bağlan, veritabanını TAMAMEN sıfırla (`clear_database`) — önceki
     sahte veri testinin PyDeck üzerinde anlamsız devasa bir kırmızı blok
     oluşturan kalıntılarını temizler.
  2. `RealOsmLoader.build_nodes()` ile Overpass'tan gerçek hastane/askeri
     alan, itfaiye/polis (Facility/Unit) ve gerçek sokak/köprü (Street/
     Bridge) düğümlerini üretir.
  3. `TucbsETLLoader.bulk_insert_nodes` (UNWIND-batch) ile Neo4j'e yazar.
  4. Sonuç sayaçlarını doğrudan veritabanı sorgusuyla teyit edip raporlar.

FAZ 3 (ULUSAL ÖLÇEK — çoklu şehir, İDARİ ALAN sorgusu): Komut satırına
il/şehir adları verilirse (`python run_real_data.py Elazığ Malatya
Diyarbakır`), `RealOsmLoader` HER ili KENDİ GERÇEK idari alan sınırıyla
(Overpass `area["name"=...]`, bbox YAKLAŞIKLIĞI DEĞİL) sırayla çeker ve
yükler (bkz. `real_osm_loader.RealOsmLoader.sehirler`); argüman verilmezse
varsayılan olarak SADECE Elazığ ilinin idari alanı yüklenir.

⚠️  DİKKAT: Bu script, çalıştığı Neo4j veritabanındaki TÜM veriyi
    (`clear_database`) GERİ ALINAMAZ şekilde SİLER. Overpass sorgusu, seçilen
    bbox/highway kapsamına göre (ve kaç şehir verildiğine göre) birkaç
    dakika sürebilir.

Çalıştırmak için:
    python run_real_data.py
    python run_real_data.py Elazığ Malatya Diyarbakır
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Dict, List, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.data_ingestion.real_osm_loader import RealOsmLoader
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

# Windows konsolları genellikle UTF-8 DEĞİL, yerel bir kod sayfası (ör.
# Türkçe Windows'ta cp1254) kullanır; bu betikteki emoji/ok ("→") gibi
# ASCII-dışı karakterler o kod sayfasında TEMSİL EDİLEMEZ ve `print()`
# `UnicodeEncodeError` ile ÇÖKER. `sys.stdout`/`sys.stderr`'i açıkça UTF-8'e
# yeniden yapılandırmak (Python 3.7+ `TextIOWrapper.reconfigure`) bunu önler.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Harici bagimlilik eklememek icin duz ANSI renk kodlari (bkz. test_etl_pipeline.py
# ile AYNI desen — Windows Terminal / PowerShell 7 / modern konsollarda calisir).
# ---------------------------------------------------------------------------


class Renk:
    BASLIK = "\033[95m"
    MAVI = "\033[94m"
    YESIL = "\033[92m"
    SARI = "\033[93m"
    KIRMIZI = "\033[91m"
    BOLD = "\033[1m"
    BITIS = "\033[0m"


def _baslik(metin: str) -> None:
    cizgi = "=" * 78
    print(f"\n{Renk.BOLD}{Renk.BASLIK}{cizgi}\n{metin}\n{cizgi}{Renk.BITIS}")


def _satir(etiket: str, deger: object, renk: str = Renk.YESIL) -> None:
    print(f"  {Renk.BOLD}{etiket}:{Renk.BITIS} {renk}{deger}{Renk.BITIS}")


def main() -> None:
    sehirler = sys.argv[1:] or None
    hedef_baslik = ", ".join(sehirler) if sehirler else "Elazığ, Turkey Pilot Bölge"
    _baslik(f"GERÇEK VERİ YÜKLEME — OSM Overpass API ({hedef_baslik})")

    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"{Renk.KIRMIZI}{Renk.BOLD}✗ Neo4j'e bağlanılamadı: {exc}{Renk.BITIS}")
        sys.exit(1)

    # --- ADIM 1: Veritabanini TAMAMEN sifirla ---
    print(f"\n{Renk.KIRMIZI}{Renk.BOLD}[1/3] Veritabanı TAMAMEN siliniyor (clear_database)...{Renk.BITIS}")
    db.clear_database()
    db.ensure_constraints()
    print(
        f"{Renk.YESIL}    → Veritabanı sıfırlandı (eski sahte-veri/HexagonLayer kalıntıları temizlendi).{Renk.BITIS}"
    )

    # --- ADIM 2: Overpass'tan gercek veri cek + UNWIND-batch ile yaz ---
    print(
        f"\n{Renk.MAVI}{Renk.BOLD}[2/3] Overpass API'den {hedef_baslik} GERÇEK şehir topolojisi "
        f"çekiliyor ve Neo4j'e yazılıyor...{Renk.BITIS}"
    )
    loader = RealOsmLoader(db, sehirler=sehirler)
    print(
        "    (Hastane/Askeri Alan, İtfaiye/Polis, Sokak/Köprü düğümleri — "
        "her il/şehrin GERÇEK idari alan sınırı içinden, bbox YAKLAŞIKLIĞI OLMADAN çekilir)"
    )
    print(f"    İl/Şehir(ler): {loader.sehirler}")
    sayac_detay: Dict[str, int] = {}
    # BÖLGESEL HATA İZOLASYONU (bkz. `real_osm_loader.RealOsmLoader.build_nodes`):
    # 81 il gibi UZUN bir listede tek bir ilin geçici bir Overpass hatası
    # ARTIK tüm çalıştırmayı ÇÖKERTMEZ — o il atlanır, DİĞERLERİ yazılmaya
    # devam eder; hangi illerin atlandığı burada toplanıp SONUÇ RAPORU'nda
    # açıkça listelenir.
    basarisiz_bolgeler: List[Tuple[str, str]] = []
    t0 = time.perf_counter()
    try:
        dugum_sayaclari = TucbsETLLoader(db).bulk_insert_nodes(
            loader.build_nodes(sayac_cikti=sayac_detay, basarisiz_bolgeler_cikti=basarisiz_bolgeler)
        )
    except RuntimeError as exc:
        print(f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ Overpass API'den veri çekilemedi: {exc}{Renk.BITIS}")
        sys.exit(1)
    sure = time.perf_counter() - t0

    # --- ADIM 3: Veritabanina dogrudan sorgu atarak dogrula ve raporla ---
    print(f"\n{Renk.MAVI}{Renk.BOLD}[3/3] Veritabanı sorgulanarak doğrulanıyor...{Renk.BITIS}")
    gercek_toplam_dugum = db.execute_query("MATCH (n) RETURN count(n) AS adet")[0]["adet"]
    etiket_dagilimi = db.execute_query(
        "MATCH (n) UNWIND labels(n) AS etiket RETURN etiket, count(*) AS adet ORDER BY adet DESC"
    )

    _baslik("SONUÇ RAPORU")

    print(f"\n  {Renk.BOLD}Kategori (bulk_insert_nodes özetinden):{Renk.BITIS}")
    for kategori, adet in sorted(dugum_sayaclari.items(), key=lambda kv: -kv[1]):
        print(f"    - {kategori:<22} {Renk.YESIL}{adet}{Renk.BITIS}")

    if sayac_detay:
        print(f"\n  {Renk.BOLD}Alt-tip detayı (RealOsmLoader.build_nodes sayaçlarından):{Renk.BITIS}")
        for anahtar, adet in sayac_detay.items():
            print(f"    - {anahtar:<22} {Renk.YESIL}{adet}{Renk.BITIS}")

    print(f"\n  {Renk.BOLD}Neo4j'deki gerçek etiket dağılımı (multi-label — kategori + alt-tip birlikte):{Renk.BITIS}")
    for row in etiket_dagilimi:
        print(f"    - {row['etiket']:<22} {Renk.YESIL}{row['adet']}{Renk.BITIS}")

    _satir("\n  Neo4j'deki GERÇEK toplam düğüm sayısı", gercek_toplam_dugum, Renk.SARI)
    _satir("  Toplam süre", f"{sure:.2f} sn", Renk.MAVI)

    if basarisiz_bolgeler:
        print(
            f"\n  {Renk.SARI}{Renk.BOLD}⚠️  {len(basarisiz_bolgeler)}/{len(loader.sehirler)} "
            f"bölge BAŞARISIZ oldu (diğerleri ETKİLENMEDİ, aşağıda yazıldı):{Renk.BITIS}"
        )
        for il_adi, hata in basarisiz_bolgeler:
            print(f"    - {Renk.KIRMIZI}{il_adi}{Renk.BITIS}: {hata}")
        print(
            f"  {Renk.SARI}Başarısız bölgeleri yeniden denemek için: "
            f"python run_real_data.py {' '.join(il for il, _ in basarisiz_bolgeler)}{Renk.BITIS}"
        )

    # KRITIK DOGRULAMA (sorun #1: sessizce "basarili" gorunup 0 kayit
    # yuklenmesi): `RealOsmLoader.fetch_raw_elements` zaten 0 ham eleman icin
    # RuntimeError firlatir, ama savunmaci bir SON kontrol olarak burada da
    # gercek veritabani sayimi acikca dogrulanir; script ASLA 0 dugumle
    # "basarili" (yesil, exit 0) rapor vermez.
    if gercek_toplam_dugum == 0:
        print(
            f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ BAŞARISIZ: Overpass sorgusu tamamlandı ama Neo4j'e "
            f"0 düğüm yazıldı. Yukarıdaki INFO loglarında hangi Overpass sorgusunun 0 eleman "
            f"döndürdüğünü kontrol edin (bbox/highway filtresi çok dar olabilir ya da tüm "
            f"aynalar zaman aşımına uğramış olabilir).{Renk.BITIS}\n"
        )
        sys.exit(1)

    print(
        f"\n{Renk.YESIL}{Renk.BOLD}✅ Gerçek {hedef_baslik} şehir topolojisi Bilgi Grafı'na yüklendi "
        f"({gercek_toplam_dugum} düğüm doğrulandı).{Renk.BITIS}"
    )
    print(
        f"{Renk.YESIL}   Haritayı görüntülemek için: "
        f"{Renk.BOLD}streamlit run src/ui/app.py{Renk.BITIS}\n"
    )


if __name__ == "__main__":
    main()
