
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

from src.core.database import Neo4jConnection, Neo4jConnectionError 
from src.data_ingestion.osm_loader import OSMBaselineLoader  
from src.data_ingestion.real_osm_loader import (  
    RealOsmLoader,
    _run_overpass_query,
    _SEHIRLER_ARASI_BEKLEME_SANIYE,
)


def _il_listesini_getir(db: Neo4jConnection) -> List[str]:
    
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
            .
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
