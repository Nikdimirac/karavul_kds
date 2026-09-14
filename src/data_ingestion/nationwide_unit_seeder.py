

from __future__ import annotations

import logging
import random
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.core.models import MobilityStatus, OperationalStatus, Unit, UnitSpecialization, UnitType
from src.data_ingestion.synthetic_unit_seeder import (
    _ANA_YOL_HIGHWAY_TIPLERI,
    _BirimSablonu,
    eski_sentetik_birimleri_sil,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)



NUFUS_KATMANI: Dict[str, int] = {
    "İstanbul": 1,
    "Ankara": 2, "İzmir": 2, "Bursa": 2, "Antalya": 2, "Konya": 2, "Adana": 2,
    "Şanlıurfa": 2, "Gaziantep": 2, "Kocaeli": 2, "Mersin": 2, "Diyarbakır": 2, "Hatay": 2,
    "Manisa": 3, "Kayseri": 3, "Samsun": 3, "Balıkesir": 3, "Kahramanmaraş": 3, "Van": 3,
    "Aydın": 3, "Tekirdağ": 3, "Sakarya": 3, "Denizli": 3, "Mardin": 3, "Muğla": 3,
    "Trabzon": 3, "Ordu": 3, "Erzurum": 3, "Sivas": 3, "Adıyaman": 3, "Batman": 3,
    "Şırnak": 3, "Kütahya": 3, "Osmaniye": 3, "Çanakkale": 3, "Tokat": 3, "Zonguldak": 3,
    "Afyonkarahisar": 3, "Çorum": 3, "Eskişehir": 3,
    "Yozgat": 4, "Isparta": 4, "Uşak": 4, "Amasya": 4, "Karaman": 4, "Kastamonu": 4,
    "Kırıkkale": 4, "Niğde": 4, "Giresun": 4, "Bolu": 4, "Rize": 4, "Edirne": 4,
    "Kırklareli": 4, "Nevşehir": 4, "Aksaray": 4, "Siirt": 4, "Bitlis": 4, "Ağrı": 4,
    "Bingöl": 4, "Muş": 4, "Kars": 4, "Erzincan": 4, "Karabük": 4, "Bartın": 4,
    "Sinop": 4, "Kırşehir": 4, "Hakkari": 4, "Iğdır": 4, "Düzce": 4, "Yalova": 4,
    "Burdur": 4, "Bilecik": 4, "Artvin": 4, "Çankırı": 4,
    "Tunceli": 5, "Bayburt": 5, "Ardahan": 5, "Kilis": 5, "Gümüşhane": 5,
}

assert len(NUFUS_KATMANI) == 79, f"NUFUS_KATMANI 79 il icermeli (81 - Elazig/Malatya), {len(NUFUS_KATMANI)} bulundu."

_KATMAN_BIRIM_SAYISI: Dict[int, int] = {1: 12, 2: 8, 3: 6, 4: 4, 5: 3}


def _birim_turu_ozellikleri(tur: str) -> Tuple[UnitType, str, Tuple[int, int], Dict[str, object]]:
   
    if tur == "AFAD":
        return (
            UnitType.AFAD, "Yetenek: Enkaz Arama, Medikal, Çadır", (20, 60),
            {"uzmanlik_alani": UnitSpecialization.ENKAZ, "kritik_altyapi_yakininda": True,
             "gunluk_ikmal_araligi_ton": (2.0, 5.0)},
        )
    if tur == "ITFAIYE":
        return (
            UnitType.ITFAIYE, "Yetenek: Yangın Söndürme, Kurtarma, Hava Yastığı", (15, 40),
            {"gunluk_ikmal_araligi_ton": (1.0, 2.5)},
        )
    if tur == "SAGLIK":
        return (
            UnitType.SAGLIK, "Yetenek: Medikal Triaj, Sahra Ambulansı", (15, 40),
            {"uzmanlik_alani": UnitSpecialization.SIHHIYE, "gunluk_ikmal_araligi_ton": (1.0, 2.5)},
        )
    if tur == "AGIR_MUHENDISLIK":
        return (
            UnitType.AGIR_MUHENDISLIK, "Yetenek: Dozer, Vinç, Hafriyat · Kapasite: Yüksek Tonaj", (10, 30),
            {"hareket_kabiliyeti": MobilityStatus.SINIRLI_HAREKETLI, "kritik_altyapi_yakininda": True,
             "gunluk_ikmal_araligi_ton": (3.0, 6.0)},
        )
    if tur == "KIZILAY":
        return (
            UnitType.SAGLIK, "Yetenek: Kan Nakli, Gıda/Battaniye Lojistiği", (10, 30),
            {"gunluk_ikmal_araligi_ton": (1.5, 4.0)},
        )
    if tur == "JANDARMA_AK":
        return (
            UnitType.ASKERI_BIRLIK, "Yetenek: Kırsal/Dağlık Arama-Kurtarma, Enkaz", (20, 45),
            {"uzmanlik_alani": UnitSpecialization.ENKAZ, "gunluk_ikmal_araligi_ton": (1.0, 2.5)},
        )
    raise ValueError(f"Bilinmeyen birim türü: {tur}")  


_BIRIM_TURU_CEVRIMI: Tuple[str, ...] = (
    "AFAD", "ITFAIYE", "SAGLIK", "AGIR_MUHENDISLIK", "KIZILAY", "JANDARMA_AK",
)



_ILCE_ADI_OZEL: Dict[str, str] = {}


def tum_illerin_sablonlarini_olustur() -> List[_BirimSablonu]:
  
    sablonlar: List[_BirimSablonu] = []
    for il, katman in NUFUS_KATMANI.items():
        sayi = _KATMAN_BIRIM_SAYISI[katman]
        tur_sayaclari: Dict[str, int] = {}
        for i in range(sayi):
            tur = _BIRIM_TURU_CEVRIMI[i % len(_BIRIM_TURU_CEVRIMI)]
            tur_sayaclari[tur] = tur_sayaclari.get(tur, 0) + 1
            sira_eki = f" #{tur_sayaclari[tur]}" if tur_sayaclari[tur] > 1 else ""

            unit_type, aciklama, personel_araligi, ekstra = _birim_turu_ozellikleri(tur)
            isim_taban = {
                "AFAD": f"{il} AFAD Lojistik Deposu",
                "ITFAIYE": f"{il} İtfaiye Takviye İstasyonu",
                "SAGLIK": f"{il} UMKE Sahra Ekibi",
                "AGIR_MUHENDISLIK": f"{il} Karayolları Şantiyesi (Ağır Mühendislik)",
                "KIZILAY": f"{il} Kızılay Kan ve Lojistik Merkezi",
                "JANDARMA_AK": f"{il} Jandarma Arama Kurtarma Timi",
            }[tur]

            sablonlar.append(_BirimSablonu(
                isim=isim_taban + sira_eki,
                bolge=il,
                unit_type=unit_type,
                aciklama=aciklama,
                personel_araligi=personel_araligi,
                dogrudan_isimli_yol_terimi=_ILCE_ADI_OZEL.get(il),
                hedef_kriz_tipi=None,
                **ekstra,
            ))
    return sablonlar


NATIONWIDE_TARIHSEL_ISIMLER: List[str] = []


_YERLESIM_ADI_TELAFI: Dict[str, str] = {
  
    "Hakkari": "Hakkâri",
}

_YARICAP_ADIMLARI_KM: Tuple[float, ...] = (50.0, 90.0, 150.0)



def _il_merkezi_koordinati(db: Neo4jConnection, il: str) -> Optional[Tuple[float, float]]:

    adaylar = [il, _YERLESIM_ADI_TELAFI.get(il, il)]
    for aday in adaylar:
        sonuc = db.execute_query(
            "MATCH (n:Settlement) WHERE n.yerlesim_tipi = 'Il Merkezi' AND n.isim = $isim "
            "RETURN n.enlem AS enlem, n.boylam AS boylam LIMIT 1",
            {"isim": aday}, write=False,
        )
        if sonuc:
            return sonuc[0]["enlem"], sonuc[0]["boylam"]
    sonuc = db.execute_query(
        "MATCH (n:Settlement) WHERE n.yerlesim_tipi = 'Il Merkezi' AND n.isim CONTAINS $parca "
        "RETURN n.enlem AS enlem, n.boylam AS boylam LIMIT 1",
        {"parca": il[:4]}, write=False,
    )
    if sonuc:
        return sonuc[0]["enlem"], sonuc[0]["boylam"]
    return None


def _il_merkezine_yakin_ana_yol_noktasi_sec(
    db: Neo4jConnection, il: str, il_koordinati: Tuple[float, float], kullanilmis: Set[str]
) -> Optional[Dict[str, Any]]:
    
    enlem, boylam = il_koordinati
    for yaricap_km in _YARICAP_ADIMLARI_KM:
        adaylar = db.execute_query(
            "MATCH (n:Street) WHERE n.acik_mi = true AND n.highway_tipi IN $tipler "
            "AND n.konum IS NOT NULL "
            "AND point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre "
            "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam LIMIT 2000",
            {
                "tipler": list(_ANA_YOL_HIGHWAY_TIPLERI), "enlem": enlem, "boylam": boylam,
                "yaricap_metre": yaricap_km * 1000.0,
            },
            write=False,
        )
        uygun = [a for a in adaylar if a["isim"] not in kullanilmis]
        if uygun:
            secilen = random.choice(uygun)
            kullanilmis.add(secilen["isim"])
            return secilen
    return None


def seed_units_nationwide(
    db: Neo4jConnection, sablonlar: List[_BirimSablonu], seed: Optional[int] = None
) -> Tuple[List[Unit], List[str]]:
 
    if seed is not None:
        random.seed(seed)

    kullanilmis_noktalar: Set[str] = set()
    il_koordinat_onbellegi: Dict[str, Optional[Tuple[float, float]]] = {}
    olusturulan: List[Unit] = []
    basarisiz: List[str] = []

    for sablon in sablonlar:
        if sablon.bolge not in il_koordinat_onbellegi:
            koordinat = _il_merkezi_koordinati(db, sablon.bolge)
            il_koordinat_onbellegi[sablon.bolge] = koordinat
            if koordinat is None:
                logger.warning(
                    "'%s' ili icin Settlement (Il Merkezi) koordinati BULUNAMADI — "
                    "bu ile ait TUM sablonlar atlanacak.", sablon.bolge,
                )
        koordinat = il_koordinat_onbellegi[sablon.bolge]
        if koordinat is None:
            basarisiz.append(sablon.isim)
            continue

        nokta = _il_merkezine_yakin_ana_yol_noktasi_sec(db, sablon.bolge, koordinat, kullanilmis_noktalar)
        if nokta is None:
            logger.warning(
                "'%s' icin '%s' ili merkezinin %.0f km icinde uygun (acik, ana damar) "
                "bir yol noktasi bulunamadi; ATLANDI.",
                sablon.isim, sablon.bolge, _YARICAP_ADIMLARI_KM[-1],
            )
            basarisiz.append(sablon.isim)
            continue

        birim = Unit(
            isim=sablon.isim, aciklama=sablon.aciklama, bolge=sablon.bolge,
            enlem=nokta["enlem"], boylam=nokta["boylam"], durum=OperationalStatus.AKTIF,
            unit_type=sablon.unit_type, personel_sayisi=random.randint(*sablon.personel_araligi),
            hareket_kabiliyeti=sablon.hareket_kabiliyeti, hareket_tipi=sablon.hareket_tipi,
            uzmanlik_alani=sablon.uzmanlik_alani,
            gunluk_ikmal_ihtiyaci_ton=round(random.uniform(*sablon.gunluk_ikmal_araligi_ton), 1),
        )
       
        query, parametreler = Neo4jConnection.node_to_cypher(birim)
        db.execute_query(query, parametreler, write=True)
        olusturulan.append(birim)
        logger.info(
            "Eklendi: '%s' (%s, %s) -> (%.5f, %.5f) [gercek yol noktasi: '%s']",
            birim.isim, birim.unit_type.value, birim.bolge, birim.enlem, birim.boylam, nokta["isim"],
        )

    return olusturulan, basarisiz


def main() -> int:
    print("=" * 78)
    print("KARAVUL — ULUSAL KARAR DERİNLİĞİ (79 il, Elazığ/Malatya HARİÇ)")
    print("=" * 78)
    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"✗ Neo4j'e bağlanılamadı: {exc}")
        return 1

    sablonlar = tum_illerin_sablonlarini_olustur()
    print(f"Hedef: {len(NUFUS_KATMANI)} il, {len(sablonlar)} birim şablonu "
          f"(katman dağılımı: {_KATMAN_BIRIM_SAYISI}).\n")

    print("Eski (bu betiğin ÜRETTİĞİ) birimler siliniyor (tekrar çalıştırılabilirlik)...")
    silinen = eski_sentetik_birimleri_sil(db, sablonlar=sablonlar)
    if NATIONWIDE_TARIHSEL_ISIMLER:  
        db.execute_query(
            "MATCH (n:Unit) WHERE n.isim IN $isimler DETACH DELETE n",
            {"isimler": NATIONWIDE_TARIHSEL_ISIMLER}, write=True,
        )
    print(f"  → {silinen} eski düğüm silindi.\n")

    olusturulan, basarisiz = seed_units_nationwide(db, sablonlar=sablonlar)

    print(f"\n✅ {len(olusturulan)} birim Bilgi Grafı'na yazıldı.")
    for tip in sorted({b.unit_type.value for b in olusturulan}):
        adet = sum(1 for b in olusturulan if b.unit_type.value == tip)
        print(f"  - {tip:<20} {adet}")

    if basarisiz:
        print(f"\n⚠️ {len(basarisiz)} şablon atlandı (uygun OSM ana-yol noktası bulunamadı — "
              "o il için Street verisi eksik/az olabilir):")
        for isim in basarisiz:
            print(f"    - {isim}")

    toplam_unit = db.execute_query("MATCH (n:Unit) RETURN count(n) AS c")[0]["c"]
    print(f"\nNeo4j'deki GERÇEK toplam Unit sayısı (TÜM iller): {toplam_unit}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
