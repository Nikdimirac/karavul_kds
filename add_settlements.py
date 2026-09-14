

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.core.models import Settlement, SettlementType
from src.data_ingestion.osm_loader import OVERPASS_ENDPOINTS, OVERPASS_REQUEST_HEADERS
from src.data_ingestion.real_osm_loader import _run_overpass_query  
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)



_OVERPASS_ZAMAN_ASIMI_SANIYE = 180


_MIN_BEKLENEN_SONUC: Dict[str, int] = {"4": 70, "6": 800}


_BAGLANTI_YARICAPI_KM_ILK = 5.0
_BAGLANTI_YARICAPI_KM_GENIS = 10.0


def _idari_sinir_merkezlerini_getir(admin_level: str) -> List[Dict[str, Any]]:
   
.strip()
    veri = _run_overpass_query(
        sorgu,
        endpoints=OVERPASS_ENDPOINTS,
        request_timeout=_OVERPASS_ZAMAN_ASIMI_SANIYE + 60,
    )
    sonuclar: List[Dict[str, Any]] = []
    for eleman in veri.get("elements", []):
        etiketler = eleman.get("tags", {})
        merkez = eleman.get("center")
        isim = etiketler.get("name")
        if not isim or not merkez:
            continue 
        sonuclar.append(
            {
                "isim": isim,
                "enlem": merkez["lat"],
                "boylam": merkez["lon"],
                "nufus": _guvenli_int(etiketler.get("population")),
            }
        )
    return sonuclar


def _idari_sinir_merkezlerini_getir_guvenli(admin_level: str) -> List[Dict[str, Any]]:

    for deneme in (1, 2):
        sonuc = _idari_sinir_merkezlerini_getir(admin_level)
        if len(sonuc) >= _MIN_BEKLENEN_SONUC[admin_level]:
            return sonuc
        logger.warning(
            "admin_level=%s sorgusu şüpheli derecede az sonuç döndürdü (%d, beklenen >= %d) "
            "— deneme %d/2.", admin_level, len(sonuc), _MIN_BEKLENEN_SONUC[admin_level], deneme,
        )
        time.sleep(10)
    raise RuntimeError(
        f"admin_level={admin_level} sorgusu 2 denemede de yeterli sonuç döndürmedi "
        f"(beklenen >= {_MIN_BEKLENEN_SONUC[admin_level]}) — Overpass aynaları geçici olarak "
        "bozuk veri dönüyor olabilir, lütfen birkaç dakika sonra tekrar deneyin."
    )


def _guvenli_int(deger: Any) -> Optional[int]:
  
    if deger is None:
        return None
    try:
        return int(str(deger).replace(".", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _en_yakin_il(
    enlem: float, boylam: float, iller: List[Dict[str, Any]]
) -> str:
    
    en_yakin_isim = iller[0]["isim"]
    en_yakin_mesafe = float("inf")
    for il in iller:
        mesafe = Neo4jConnection._geodesic_km((enlem, boylam), (il["enlem"], il["boylam"]))
        if mesafe < en_yakin_mesafe:
            en_yakin_mesafe = mesafe
            en_yakin_isim = il["isim"]
    return en_yakin_isim


def yerlesimleri_olustur() -> List[Settlement]:
   
    print("[1/3] İl merkezleri (admin_level=4) Overpass'tan çekiliyor...")
    iller_ham = _idari_sinir_merkezlerini_getir_guvenli("4")
    print(f"      {len(iller_ham)} il merkezi bulundu.")

    print("[2/3] İlçe merkezleri (admin_level=6) Overpass'tan çekiliyor (biraz sürebilir)...")
    ilceler_ham = _idari_sinir_merkezlerini_getir_guvenli("6")
    print(f"      {len(ilceler_ham)} ilçe merkezi bulundu.")

    yerlesimler: List[Settlement] = []
    for il in iller_ham:
        yerlesimler.append(
            Settlement(
                isim=il["isim"],
                yerlesim_tipi=SettlementType.IL_MERKEZI,
                il=il["isim"],
                enlem=il["enlem"],
                boylam=il["boylam"],
                nufus=il["nufus"],
            )
        )

    for ilce in ilceler_ham:
        en_yakin_il = _en_yakin_il(ilce["enlem"], ilce["boylam"], iller_ham)
        yerlesimler.append(
            Settlement(
                isim=f"{ilce['isim']} ({en_yakin_il})",
                yerlesim_tipi=SettlementType.ILCE_MERKEZI,
                il=en_yakin_il,
                enlem=ilce["enlem"],
                boylam=ilce["boylam"],
                nufus=ilce["nufus"],
            )
        )

    return yerlesimler




def en_yakin_altyapiya_bagla(db: Neo4jConnection, yerlesim_id: str, enlem: float, boylam: float) -> Optional[float]:
  
    for yaricap_km in (_BAGLANTI_YARICAPI_KM_ILK, _BAGLANTI_YARICAPI_KM_GENIS):
        sonuc = db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.konum IS NOT NULL "
            "AND point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) <= $yaricap_metre "
            "WITH i, point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) AS mesafe_metre "
            "ORDER BY mesafe_metre ASC LIMIT 1 "
            "MATCH (s:Settlement {id: $yerlesim_id}) "
            "MERGE (s)-[r:CONNECTED_TO]->(i) "
            "SET r.mesafe_km = mesafe_metre / 1000.0 "
            "RETURN mesafe_metre / 1000.0 AS mesafe_km",
            {
                "enlem": enlem, "boylam": boylam,
                "yaricap_metre": yaricap_km * 1000.0,
                "yerlesim_id": yerlesim_id,
            },
            write=True,
        )
        if sonuc:
            return sonuc[0]["mesafe_km"]
    return None


def main() -> None:
    t0 = time.perf_counter()
    try:
        db = Neo4jConnection()
        db.connect()
        db.ensure_constraints()  
    except Neo4jConnectionError as exc:
        print(f"✗ Neo4j'e bağlanılamadı: {exc}")
        sys.exit(1)

    try:
        yerlesimler = yerlesimleri_olustur()
    except RuntimeError as exc:
        print(f"✗ Overpass'tan veri çekilemedi: {exc}")
        sys.exit(1)

    if not yerlesimler:
        print("✗ BAŞARISIZ: Overpass sorgusu 0 yerleşim döndürdü.")
        sys.exit(1)

    print(f"[3/3] {len(yerlesimler)} Settlement düğümü Neo4j'e yazılıyor...")
    sayaclar = TucbsETLLoader(db).bulk_insert_nodes(yerlesimler, toplam_tahmini=len(yerlesimler))
    print(f"      Yazıldı: {sayaclar}")

    print(f"\n[Lojistik Bağlantı] {len(yerlesimler)} yerleşim, en yakın yol/altyapı düğümüne bağlanıyor "
          f"({_BAGLANTI_YARICAPI_KM_ILK:.0f}-{_BAGLANTI_YARICAPI_KM_GENIS:.0f} km yarıçapında)...")
    baglanan = 0
    baglanamayan: List[str] = []
    for i, yerlesim in enumerate(yerlesimler, start=1):
        mesafe = en_yakin_altyapiya_bagla(db, yerlesim.id, yerlesim.enlem, yerlesim.boylam)
        if mesafe is not None:
            baglanan += 1
        else:
            baglanamayan.append(yerlesim.isim)
        if i % 100 == 0 or i == len(yerlesimler):
            print(f"      {i}/{len(yerlesimler)} işlendi ({baglanan} bağlandı)...")

    sure = time.perf_counter() - t0

    print("\n" + "=" * 78)
    print(f"✅ TAMAMLANDI: {len(yerlesimler)} yerleşim yazıldı, {baglanan}/{len(yerlesimler)} "
          f"en yakın altyapıya [:CONNECTED_TO] ile bağlandı. Süre: {sure:.1f} sn.")
    if baglanamayan:
        print(f"⚠️  {len(baglanamayan)} yerleşim {_BAGLANTI_YARICAPI_KM_GENIS:.0f} km içinde HİÇBİR "
              "altyapı düğümü bulamadı (muhtemelen o bölgenin kılcal yol ağı henüz `seed_db.py` ile "
              "yüklenmemiş) — bunlar BAĞLANTISIZ kaldı, hayalet bir ilişki UYDURULMADI:")
        for isim in baglanamayan[:20]:
            print(f"      - {isim}")
        if len(baglanamayan) > 20:
            print(f"      ... ve {len(baglanamayan) - 20} tane daha.")
    print("=" * 78)

    db.close()


if __name__ == "__main__":
    main()
