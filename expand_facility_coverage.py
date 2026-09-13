# -*- coding: utf-8 -*-
"""
expand_facility_coverage.py
=============================
KARAVUL — "VERİTABANI GENİŞLETME" (kullanıcı talebi — "Diyarbakır Jet Üssü"
canlı hatası SONRASI: "sığ düşünme güncellemeleri yaparken veri tabanında,
sistemimiz gibi veri tabanımız da zeki olmalı ... bir karar destek
sisteminde olması gereken bütün nokta ve bölgeler eklensin").

Bu betik, `run_real_data.py`nin AKSİNE, veritabanını SİLMEZ (`clear_
database` ÇAĞRILMAZ) — SADECE `RealOsmLoader`in şimdi genişletilmiş nokta
sorgusuyla (bkz. `osm_loader.OSMBaselineLoader._build_point_query` — YENİ
eklenen havalimanı/liman/santral/trafo/baraj/baz istasyonu/yakıt
istasyonu etiketleri) TÜM 81 il için EKSİK olan tesisleri EKLER
(`Neo4jConnection.add_node`'un isim-bazlı MERGE'i sayesinde zaten var olan
kayıtlar KOPYALANMAZ, sadece EKSİK olanlar eklenir/güncellenir).

PERFORMANS DÜZELTMESİ (canlı tespit — bkz. oturum notları): bu betiğin İLK
sürümü `RealOsmLoader.build_nodes()`i OLDUĞU GİBİ kullanıyordu — bu jeneratör
HER il için nokta (tesis) sorgusunun YANI SIRA sokak/köprü ağını da (`ham
["yollar"]`) BAŞTAN yield ediyordu. Sokak ağı bu projede ZATEN eksiksiz
yüklüydü (967K+ Street düğümü, bkz. AI_MEMORY.md); ama `real_osm_loader.py`
her yol segmentinin TÜM ara koordinat noktalarını AYRI birer nokta-düğümü
olarak ürettiğinden (bkz. o modülün docstring'i — "şehir damarları" tasarım
kararı), TEK bir orta ölçekli il (Adana) bile 37.647 way segmentinden
YÜZ BİNLERCE Street noktası üretti; `add_node`'un HER kayıt için yaptığı
bulanık-isim MERGE kontrolü ile bu, 3.7 SAATTE HÂLÂ İLK İLİ bitirememe
sonucunu doğurdu (81 il için GERÇEKÇİ OLMAYAN bir süre). ÇÖZÜM (İKİ AŞAMALI):
İLK denemede sadece DB YAZMA adımı (`ham["yollar"]` işlenmedi) atlandı, ama
`RealOsmLoader._fetch_raw_elements_for` YİNE DE HER il için sokak/köprü
Overpass sorgusunu ÇEKMEYE devam etti (Adana için ~60 sn BOŞA harcanan ağ
süresi) — bu betik artık `RealOsmLoader._build_point_query` + modül-seviyesi
`_run_overpass_query`yi DOĞRUDAN çağırır, sokak/köprü sorgusu Overpass'a
HİÇ GÖNDERİLMEZ. SADECE nokta/tesis elemanları (tipik bir il için birkaç
yüz eleman, ~1 dakikadan kısa) çekilip `OSMBaselineLoader._convert_point_
elements`e verilir — sokak/köprü ağı (zaten eksiksiz yüklü) NE ÇEKİLİR NE
İŞLENİR.

İl listesi HARDCODE EDİLMEZ — zaten yüklü `Settlement` (İl Merkezi)
düğümlerinden OKUNUR (bkz. `add_settlements.py`) ki 81 il TAM OLARAK aynı
kaynaktan (OSM admin_level=4) tutarlı kalsın.

Çalıştırmak için (proje kökünden, Neo4j ZATEN çalışıyorken):
    python expand_facility_coverage.py
"""

from __future__ import annotations

import logging
import sys
import time
from typing import List, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from src.core.database import Neo4jConnection, Neo4jConnectionError  # noqa: E402
from src.data_ingestion.osm_loader import OSMBaselineLoader  # noqa: E402
from src.data_ingestion.real_osm_loader import (  # noqa: E402
    RealOsmLoader,
    _run_overpass_query,
    _SEHIRLER_ARASI_BEKLEME_SANIYE,
)


def _il_listesini_getir(db: Neo4jConnection) -> List[str]:
    """Zaten yüklü `Settlement` (İl Merkezi) düğümlerinden 81 il adını okur
    — `add_settlements.py`nin ZATEN doğruladığı, OSM admin_level=4 tabanlı
    TEK kaynak (bkz. modül docstring'i). Boş dönerse (Settlement hiç
    yüklenmemişse) `RealOsmLoader`in kendi tek-şehir varsayılanına
    (`_VARSAYILAN_SEHIRLER = ("Elazığ",)`) düşülür — bu betik ASLA
    sessizce hiçbir şey yapmadan çıkmaz."""
    sonuc = db.execute_query(
        'MATCH (s:Settlement) WHERE s.yerlesim_tipi = "Il Merkezi" RETURN DISTINCT s.isim AS isim ORDER BY isim',
        {},
    )
    iller = [r["isim"] for r in sonuc if r.get("isim")]
    if not iller:
        logger.warning("Settlement (Il Merkezi) hic yuklenmemis - RealOsmLoader varsayilanina (Elazig) dusuluyor.")
    return iller


def main() -> int:
    print("=" * 78)
    print("KARAVUL — TESİS KAPSAMI GENİŞLETME (Havalimanı/Liman/Enerji/İletişim/Yakıt)")
    print("⚠️  Veritabanı SİLİNMEZ — SADECE eksik kayıtlar EKLENİR (isim-bazlı MERGE).")
    print("(Sokak/köprü ağı BİLİNÇLİ OLARAK atlanır — zaten eksiksiz yüklü, bkz. modül docstring'i.)")
    print("=" * 78)

    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"✗ Neo4j'e bağlanılamadı: {exc}")
        return 1

    iller = _il_listesini_getir(db)
    print(f"Hedef: {len(iller)} il (kaynak: mevcut Settlement düğümleri)")

    loader = RealOsmLoader(db, sehirler=iller) if iller else RealOsmLoader(db)

    basarisiz_bolgeler: List[Tuple[str, str]] = []
    sayaclar = {
        "facility": 0, "unit": 0, "energy_infrastructure": 0,
        "communication_network": 0, "resource_hub": 0,
    }
    t0 = time.monotonic()
    yazilan = 0

    for idx, il in enumerate(loader.sehirler):
        if idx > 0:
            time.sleep(_SEHIRLER_ARASI_BEKLEME_SANIYE)
        print(f"\n=== [{idx + 1}/{len(loader.sehirler)}] {il} işleniyor ===")
        try:
            # BİLİNÇLİ OLARAK `RealOsmLoader._fetch_raw_elements_for` YERİNE
            # SADECE nokta/tesis sorgusu doğrudan çalıştırılır — o metod HER
            # ZAMAN sokak/köprü sorgusunu da (`ham["yollar"]`) Overpass'tan
            # ÇEKER (bu betiğin KULLANMADIĞI, sadece ağ/zaman maliyeti
            # getiren bir veri), bkz. modül-üstü "PERFORMANS DÜZELTMESİ" notu.
            nokta_elemanlari = _run_overpass_query(RealOsmLoader._build_point_query(il))["elements"]
        except RuntimeError as exc:
            logger.error("[%s] BOLGE ATLANDI (hata cekildi, diger iller ETKILENMEZ): %s", il, exc)
            basarisiz_bolgeler.append((il, str(exc)))
            continue

        facilities, units, energiler, iletisimler, kaynaklar = OSMBaselineLoader._convert_point_elements(
            nokta_elemanlari
        )
        for grup, anahtar in (
            (facilities, "facility"), (units, "unit"), (energiler, "energy_infrastructure"),
            (iletisimler, "communication_network"), (kaynaklar, "resource_hub"),
        ):
            for node in grup:
                node.bolge = il
                db.add_node(node)
                yazilan += 1
                sayaclar[anahtar] += 1
        print(f"  {il}: {len(facilities)} tesis, {len(units)} birim, {len(energiler)} enerji, "
              f"{len(iletisimler)} iletişim, {len(kaynaklar)} kaynak (toplam {yazilan}, {time.monotonic() - t0:.0f} sn)")

    sure = time.monotonic() - t0
    print("\n" + "=" * 78)
    print(f"✅ TAMAMLANDI ({sure / 60:.1f} dakika) — toplam {yazilan} düğüm işlendi (var olanlar güncellendi, yeniler eklendi).")
    for k, v in sayaclar.items():
        print(f"  {k}: {v}")
    if basarisiz_bolgeler:
        print(f"\n⚠️ {len(basarisiz_bolgeler)} il/bölge atlandı (Overpass hatası):")
        for il, hata in basarisiz_bolgeler:
            print(f"  - {il}: {hata}")
    print("=" * 78)

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
