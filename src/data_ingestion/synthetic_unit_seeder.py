

from __future__ import annotations

import logging
import random
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.core.models import (
    MobilityStatus,
    OperationalStatus,
    Unit,
    UnitMovementType,
    UnitSpecialization,
    UnitType,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class Renk:
    MAVI = "\033[94m"
    YESIL = "\033[92m"
    SARI = "\033[93m"
    KIRMIZI = "\033[91m"
    BOLD = "\033[1m"
    BITIS = "\033[0m"


_ANA_YOL_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary")


def _ana_yol_noktasi_sec(
    db: Neo4jConnection, bolge: str, kullanilmis: Set[str]
) -> Optional[Dict[str, Any]]:
  
    adaylar = db.execute_query(
        "MATCH (n:Street) WHERE n.bolge = $bolge AND n.acik_mi = true "
        "AND n.highway_tipi IN $tipler "
        "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam "
        "LIMIT 5000",
        {"bolge": bolge, "tipler": list(_ANA_YOL_HIGHWAY_TIPLERI)},
        write=False,
    )
    uygun = [a for a in adaylar if a["isim"] not in kullanilmis]
    if not uygun:
        return None
    secilen = random.choice(uygun)
    kullanilmis.add(secilen["isim"])
    return secilen




_KRITIK_ALTYAPI_MAKS_MESAFE_KM = 18.0

_KRITIK_ALTYAPI_DENEME_SAYISI = 8



def _kritik_altyapi_capalarini_getir(db: Neo4jConnection, bolge: str) -> List[Dict[str, Any]]:
   
    kopruler = db.execute_query(
        "MATCH (n:Bridge) WHERE n.bolge = $bolge AND n.aciklama IS NOT NULL "
        "RETURN DISTINCT n.aciklama AS isim, n.enlem AS enlem, n.boylam AS boylam",
        {"bolge": bolge},
        write=False,
    )
    ana_yol_ornekleri = db.execute_query(
        "MATCH (n:Street) WHERE n.bolge = $bolge AND n.acik_mi = true "
        "AND n.highway_tipi IN ['motorway', 'trunk'] "
        "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam "
        "LIMIT 300",
        {"bolge": bolge},
        write=False,
    )
    return list(kopruler) + list(ana_yol_ornekleri)


def _kritik_altyapi_yakininda_ana_yol_noktasi_sec(
    db: Neo4jConnection,
    bolge: str,
    kullanilmis: Set[str],
    maks_mesafe_km: float = _KRITIK_ALTYAPI_MAKS_MESAFE_KM,
    capa_arama_terimi: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    
    capalar = _kritik_altyapi_capalarini_getir(db, bolge)
    if not capalar:
        logger.warning(
            "'%s' bölgesinde hiç kritik altyapı (köprü/ana otoyol) çapası bulunamadı; "
            "stratejik kümeleme YAPILAMIYOR.", bolge,
        )
        return None

    if capa_arama_terimi:
        eslesen = [c for c in capalar if capa_arama_terimi.lower() in (c["isim"] or "").lower()]
        if eslesen:
            capalar = eslesen
            logger.info(
                "  (deterministik hedefleme: '%s' terimiyle %d eşleşen çapa bulundu)",
                capa_arama_terimi, len(eslesen),
            )
        else:
            logger.warning(
                "'%s' terimiyle eşleşen bir kritik altyapı bulunamadı ('%s' bölgesinde); "
                "TÜM çapalara geri dönülüyor.", capa_arama_terimi, bolge,
            )

    random.shuffle(capalar)
    for capa in capalar[:_KRITIK_ALTYAPI_DENEME_SAYISI]:
        adaylar = db.execute_query(
            "MATCH (n:Street) WHERE n.bolge = $bolge AND n.acik_mi = true "
            "AND n.highway_tipi IN $tipler AND n.konum IS NOT NULL "
            "AND point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre "
            "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam LIMIT 300",
            {
                "bolge": bolge, "tipler": list(_ANA_YOL_HIGHWAY_TIPLERI),
                "enlem": capa["enlem"], "boylam": capa["boylam"],
                "yaricap_metre": maks_mesafe_km * 1000.0,
            },
            write=False,
        )
        uygun = [a for a in adaylar if a["isim"] not in kullanilmis]
        if uygun:
            secilen = random.choice(uygun)
            kullanilmis.add(secilen["isim"])
            mesafe_km = Neo4jConnection._geodesic_km(
                (capa["enlem"], capa["boylam"]), (secilen["enlem"], secilen["boylam"])
            )
            logger.info(
                "  (stratejik yerleşim: '%s' kritik altyapısına ~%.1f km mesafede)",
                capa["isim"], mesafe_km,
            )
            return secilen

    logger.warning(
        "'%s' bölgesinde denenen %d kritik altyapı çapasının HİÇBİRİNDE müsait ana yol "
        "noktası bulunamadı.", bolge, min(len(capalar), _KRITIK_ALTYAPI_DENEME_SAYISI),
    )
    return None



KRIZ_TIPLERI: List[str] = ["Deprem", "Sel", "Orman Yangini", "Kopru/Altyapi Yikimi"]
"""Bu script'in artık HANGİ afet senaryolarına HAZIRLIK yaptığının kısa bir
envanteri (bkz. `models.EventType` — "Orman Yangini" ayrı bir `EventType`
DEĞİLDİR, `EventType.YANGIN`in bir alt-türüdür; burada SADECE bu script'in
hangi YERLEŞİM/BİRİM çeşitliliğini simüle ettiğini dokümante etmek içindir).
Bu liste kod tarafından ZORUNLU bir filtre olarak KULLANILMAZ — asıl eşleme
`decision_engine._OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI`dedir; burası SADECE
aşağıdaki `BIRIM_SABLONLARI`daki her yeni şablonun HANGİ senaryo için
tasarlandığını okuyan insana açıklayan bir referanstır (bkz. her şablonun
kendi yorumu)."""

_GENIS_YOL_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary", "secondary", "tertiary")
"""`_ANA_YOL_HIGHWAY_TIPLERI`den (motorway/trunk/primary — ağır ekipmanın
GEÇEBİLECEĞİ ana damarlar) KASITLI olarak DAHA GENİŞTİR: aşağıdaki
`_isimli_yol_noktasi_sec` ile yerleştirilen birimler (ör. bir Jandarma
Arama Kurtarma timi veya köy ölçeğinde bir Orman İtfaiyesi istasyonu) GERÇEK
hayatta genelde secondary/tertiary bir yol üzerindedir — Kömürhan gibi bir
Ağır Mühendislik/AFAD deposunun aksine bu birimler için "ağır tonaj
geçebilir mi" kısıtı GEÇERLİ DEĞİLDİR. `_ANA_YOL_HIGHWAY_TIPLERI` (ve onu
kullanan `_ana_yol_noktasi_sec`/`_kritik_altyapi_yakininda_ana_yol_noktasi_
sec`) BİLEREK DEĞİŞTİRİLMEDİ — o kısıt hâlâ Ağır Mühendislik/AFAD/UMKE gibi
"gerçekten ağır ekipman geçebilmeli" birimleri için doğru bir varsayımdır."""


def _isimli_yol_noktasi_sec(
    db: Neo4jConnection, bolge: str, isim_terimi: str, kullanilmis: Set[str]
) -> Optional[Dict[str, Any]]:
    
    adaylar = db.execute_query(
        "MATCH (n:Street) WHERE n.bolge = $bolge AND n.acik_mi = true "
        "AND n.highway_tipi IN $tipler AND n.isim CONTAINS $terim "
        "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam LIMIT 500",
        {"bolge": bolge, "tipler": list(_GENIS_YOL_TIPLERI), "terim": isim_terimi},
        write=False,
    )
    uygun = [a for a in adaylar if a["isim"] not in kullanilmis]
    if not uygun:
        return None
    secilen = random.choice(uygun)
    kullanilmis.add(secilen["isim"])
    return secilen



@dataclass
class _BirimSablonu:
    isim: str
    bolge: str  
    unit_type: UnitType
    aciklama: str  
    personel_araligi: Tuple[int, int]
    hareket_tipi: UnitMovementType = UnitMovementType.MOTORIZE
    hareket_kabiliyeti: MobilityStatus = MobilityStatus.TAM_HAREKETLI
    uzmanlik_alani: Optional[UnitSpecialization] = None
    gunluk_ikmal_araligi_ton: Tuple[float, float] = (1.0, 3.0)
   
    kritik_altyapi_yakininda: bool = False
    
    sabit_capa_arama_terimi: Optional[str] = None
   
    dogrudan_isimli_yol_terimi: Optional[str] = None
   
    hedef_kriz_tipi: Optional[str] = None


TARIHSEL_ISIMLER: List[str] = [
    "AFAD Elazığ İkmal Noktası",
]


BIRIM_SABLONLARI: List[_BirimSablonu] = [
   
    _BirimSablonu(
        isim="AFAD Elazığ Lojistik Deposu", bolge="Elazığ", unit_type=UnitType.AFAD,
        aciklama="Yetenek: Enkaz Arama, Medikal, Çadır", personel_araligi=(60, 100),
        uzmanlik_alani=UnitSpecialization.ENKAZ, gunluk_ikmal_araligi_ton=(3.0, 6.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="AFAD Malatya Lojistik Deposu", bolge="Malatya", unit_type=UnitType.AFAD,
        aciklama="Yetenek: Enkaz Arama, Medikal, Çadır", personel_araligi=(60, 100),
        uzmanlik_alani=UnitSpecialization.ENKAZ, gunluk_ikmal_araligi_ton=(3.0, 6.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        
        isim="Karayolları 8. Bölge Tünel Kontrol ve Müdahale Merkezi", bolge="Malatya",
        unit_type=UnitType.AGIR_MUHENDISLIK,
        aciklama="Yetenek: Tünel Trafik Kontrolü, Kurtarma, Dozer, Vinç", personel_araligi=(10, 30),
        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI, gunluk_ikmal_araligi_ton=(2.0, 4.0),
        kritik_altyapi_yakininda=True, sabit_capa_arama_terimi="Kömürhan",
    ),
    _BirimSablonu(
        isim="AFAD Malatya İkmal Noktası", bolge="Malatya", unit_type=UnitType.AFAD,
        aciklama="Yetenek: Enkaz Arama, Çadır, Battaniye", personel_araligi=(20, 40),
        uzmanlik_alani=UnitSpecialization.ENKAZ, gunluk_ikmal_araligi_ton=(1.5, 3.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="UMKE Malatya Toplanma Merkezi", bolge="Malatya", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Enkaz Arama, Medikal, Çadır", personel_araligi=(30, 60),
        uzmanlik_alani=UnitSpecialization.SIHHIYE, gunluk_ikmal_araligi_ton=(1.0, 2.5),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="UMKE Elazığ Toplanma Merkezi", bolge="Elazığ", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Enkaz Arama, Medikal, Çadır", personel_araligi=(30, 60),
        uzmanlik_alani=UnitSpecialization.SIHHIYE, gunluk_ikmal_araligi_ton=(1.0, 2.5),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="UMKE Elazığ Sahra Ekibi", bolge="Elazığ", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Medikal Triaj, Sahra Ambulansı", personel_araligi=(15, 30),
        uzmanlik_alani=UnitSpecialization.SIHHIYE, gunluk_ikmal_araligi_ton=(1.0, 2.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="Karayolları 8. Bölge Şantiyesi", bolge="Elazığ", unit_type=UnitType.AGIR_MUHENDISLIK,
        aciklama="Yetenek: Dozer, Vinç, Hafriyat · Kapasite: Yüksek Tonaj", personel_araligi=(15, 35),
        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI, gunluk_ikmal_araligi_ton=(4.0, 8.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="Karayolları Malatya Şube Şantiyesi", bolge="Malatya", unit_type=UnitType.AGIR_MUHENDISLIK,
        aciklama="Yetenek: Dozer, Vinç, Hafriyat · Kapasite: Yüksek Tonaj", personel_araligi=(15, 35),
        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI, gunluk_ikmal_araligi_ton=(4.0, 8.0),
        kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="Özel Sektör Ağır Makine Parkı (Elazığ)", bolge="Elazığ", unit_type=UnitType.AGIR_MUHENDISLIK,
        aciklama="Yetenek: Dozer, Vinç, Kırıcı, Hafriyat · Kapasite: Yüksek Tonaj",
        personel_araligi=(10, 25), hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        gunluk_ikmal_araligi_ton=(3.0, 7.0), kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="Özel Sektör Ağır Makine Parkı (Malatya)", bolge="Malatya", unit_type=UnitType.AGIR_MUHENDISLIK,
        aciklama="Yetenek: Dozer, Vinç, Kırıcı, Hafriyat · Kapasite: Yüksek Tonaj",
        personel_araligi=(10, 25), hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        gunluk_ikmal_araligi_ton=(3.0, 7.0), kritik_altyapi_yakininda=True,
    ),
    _BirimSablonu(
        isim="Sahra Hastanesi Kurulum Noktası (Elazığ)", bolge="Elazığ", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Ameliyathane, Yoğun Bakım, Triaj", personel_araligi=(40, 70),
        uzmanlik_alani=UnitSpecialization.SIHHIYE, hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        gunluk_ikmal_araligi_ton=(2.0, 4.0),
    ),
    _BirimSablonu(
        isim="Sahra Hastanesi Kurulum Noktası (Malatya)", bolge="Malatya", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Ameliyathane, Yoğun Bakım, Triaj", personel_araligi=(40, 70),
        uzmanlik_alani=UnitSpecialization.SIHHIYE, hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        gunluk_ikmal_araligi_ton=(2.0, 4.0),
    ),
    _BirimSablonu(
        isim="Kızılay Kan ve Lojistik Merkezi (Elazığ)", bolge="Elazığ", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Kan Nakli, Gıda/Battaniye Lojistiği", personel_araligi=(15, 30),
        gunluk_ikmal_araligi_ton=(2.0, 5.0),
    ),
    _BirimSablonu(
        isim="Kızılay Kan ve Lojistik Merkezi (Malatya)", bolge="Malatya", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Kan Nakli, Gıda/Battaniye Lojistiği", personel_araligi=(15, 30),
        gunluk_ikmal_araligi_ton=(2.0, 5.0),
    ),
    
    _BirimSablonu(
        
        isim="Sivrice Jandarma Arama Kurtarma Timi", bolge="Elazığ", unit_type=UnitType.ASKERI_BIRLIK,
        aciklama="Yetenek: Kırsal/Dağlık Arama-Kurtarma, Enkaz, Hazar Gölü Kıyı Operasyonları",
        personel_araligi=(20, 45), uzmanlik_alani=UnitSpecialization.ENKAZ,
        hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI, gunluk_ikmal_araligi_ton=(1.0, 2.5),
        dogrudan_isimli_yol_terimi="Sivrice", hedef_kriz_tipi="Deprem",
    ),
    _BirimSablonu(
        isim="UMKE Sivrice Göl Kenarı Sahra Hastanesi", bolge="Elazığ", unit_type=UnitType.SAGLIK,
        aciklama="Yetenek: Medikal Triaj, Sahra Ambulansı, Hazar Gölü Kıyısı Erişimi",
        personel_araligi=(15, 30), uzmanlik_alani=UnitSpecialization.SIHHIYE,
        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI, gunluk_ikmal_araligi_ton=(1.0, 2.0),
        dogrudan_isimli_yol_terimi="Sivrice", hedef_kriz_tipi="Deprem",
    ),
    _BirimSablonu(
        
        isim="Baskil Orman İtfaiyesi İstasyonu", bolge="Elazığ", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Orman Yangını Söndürme, Arazi Aracı, Dağlık Arazi Müdahalesi",
        personel_araligi=(15, 30), hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI,
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
        dogrudan_isimli_yol_terimi="Baskil", hedef_kriz_tipi="Orman Yangini",
    ),
    _BirimSablonu(

        isim="Doğanşehir Orman İtfaiyesi İstasyonu", bolge="Malatya", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Orman Yangını Söndürme, Arazi Aracı, Dağlık Arazi Müdahalesi",
        personel_araligi=(15, 30), hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI,
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
        dogrudan_isimli_yol_terimi="Doğanşehir", hedef_kriz_tipi="Orman Yangini",
    ),
    _BirimSablonu(

        isim="Yeşilyurt Sel Müdahale ve AFAD Deposu", bolge="Malatya", unit_type=UnitType.AFAD,
        aciklama="Yetenek: Su Baskını Tahliyesi, Motopomp, Bot, Çadır/Battaniye",
        personel_araligi=(25, 50), uzmanlik_alani=UnitSpecialization.ENKAZ,
        hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI, gunluk_ikmal_araligi_ton=(2.0, 4.0),
        dogrudan_isimli_yol_terimi="Yeşilyurt", hedef_kriz_tipi="Sel",
    ),
    _BirimSablonu(

        isim="Malatya İstasyon Kavşağı Jandarma Arama Kurtarma Timi", bolge="Malatya",
        unit_type=UnitType.ASKERI_BIRLIK,
        aciklama="Yetenek: Kentsel Arama-Kurtarma, Enkaz, Şehir İçi Trafik Yönetimi",
        personel_araligi=(20, 45), uzmanlik_alani=UnitSpecialization.ENKAZ,
        hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI, gunluk_ikmal_araligi_ton=(1.0, 2.5),
        dogrudan_isimli_yol_terimi="İstasyon Caddesi", hedef_kriz_tipi="Deprem",
    ),

    _BirimSablonu(
        isim="Elazığ İtfaiye Takviye İstasyonu", bolge="Elazığ", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Yangın Söndürme, Kurtarma, Hava Yastığı", personel_araligi=(20, 40),
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
    ),
    _BirimSablonu(
        isim="Malatya İtfaiye Takviye İstasyonu", bolge="Malatya", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Yangın Söndürme, Kurtarma, Hava Yastığı", personel_araligi=(20, 40),
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
    ),
]




def seed_units(
    db: Neo4jConnection, sablonlar: Optional[List[_BirimSablonu]] = None, seed: Optional[int] = None
) -> Tuple[List[Unit], List[str]]:
   
    if seed is not None:
        random.seed(seed)
    sablonlar = sablonlar if sablonlar is not None else BIRIM_SABLONLARI

    kullanilmis_noktalar: Set[str] = set()
    olusturulan: List[Unit] = []
    basarisiz: List[str] = []

    for sablon in sablonlar:
       
        if sablon.dogrudan_isimli_yol_terimi:
            nokta = _isimli_yol_noktasi_sec(
                db, sablon.bolge, sablon.dogrudan_isimli_yol_terimi, kullanilmis_noktalar
            )
        elif sablon.kritik_altyapi_yakininda:
            nokta = _kritik_altyapi_yakininda_ana_yol_noktasi_sec(
                db, sablon.bolge, kullanilmis_noktalar, capa_arama_terimi=sablon.sabit_capa_arama_terimi
            )
        else:
            nokta = _ana_yol_noktasi_sec(db, sablon.bolge, kullanilmis_noktalar)
        if nokta is None:
            logger.warning(
                "'%s' icin '%s' bolgesinde uygun (acik, ana damar) bir OSM noktasi "
                "bulunamadi; ATLANDI (o bolge hic yuklenmemis olabilir).",
                sablon.isim, sablon.bolge,
            )
            basarisiz.append(sablon.isim)
            continue

        birim = Unit(
            isim=sablon.isim,
            aciklama=sablon.aciklama,
            bolge=sablon.bolge,
            enlem=nokta["enlem"],
            boylam=nokta["boylam"],
            durum=OperationalStatus.AKTIF,
            unit_type=sablon.unit_type,
            personel_sayisi=random.randint(*sablon.personel_araligi),
            hareket_kabiliyeti=sablon.hareket_kabiliyeti,
            hareket_tipi=sablon.hareket_tipi,
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


def eski_sentetik_birimleri_sil(
    db: Neo4jConnection, sablonlar: Optional[List[_BirimSablonu]] = None
) -> int:
    
    sablonlar = sablonlar if sablonlar is not None else BIRIM_SABLONLARI
    isimler = [s.isim for s in sablonlar] + list(TARIHSEL_ISIMLER)
    if not isimler:
        return 0
    once = db.execute_query(
        "MATCH (n:Unit) WHERE n.isim IN $isimler RETURN count(n) AS c", {"isimler": isimler}
    )[0]["c"]
    db.execute_query(
        "MATCH (n:Unit) WHERE n.isim IN $isimler DETACH DELETE n", {"isimler": isimler}, write=True
    )
    logger.info("Eski sentetik birimler silindi: %d dugum.", once)
    return once


def main() -> None:
    print(f"{Renk.BOLD}{Renk.MAVI}=== Sentetik Müdahale Birliği Üretici (Karar Derinliği) ==={Renk.BITIS}")
    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"{Renk.KIRMIZI}{Renk.BOLD}✗ Neo4j'e bağlanılamadı: {exc}{Renk.BITIS}")
        sys.exit(1)

    print(f"{Renk.SARI}Eski sentetik birimler siliniyor (Stratejik Yeniden Dağıtım)...{Renk.BITIS}")
    silinen = eski_sentetik_birimleri_sil(db)
    print(f"{Renk.SARI}  → {silinen} eski birim silindi.{Renk.BITIS}\n")

    print(
        f"{Renk.MAVI}{len(BIRIM_SABLONLARI)} birim şablonu yeniden tohumlanıyor — Ağır Mühendislik/"
        f"AFAD/UMKE birimleri GERÇEK kritik altyapılara (köprü/ana otoyol) ~{_KRITIK_ALTYAPI_MAKS_MESAFE_KM:.0f} "
        f"km içinde stratejik olarak kümelenecek, diğerleri il geneline dağılacak...{Renk.BITIS}\n"
    )
    olusturulan, basarisiz = seed_units(db)

    print(f"\n{Renk.BOLD}{Renk.YESIL}✅ {len(olusturulan)} birim Bilgi Grafı'na yazıldı:{Renk.BITIS}")
    for tip in sorted({b.unit_type.value for b in olusturulan}):
        adet = sum(1 for b in olusturulan if b.unit_type.value == tip)
        print(f"  - {tip:<20} {Renk.YESIL}{adet}{Renk.BITIS}")

    if basarisiz:
        print(f"\n{Renk.SARI}{Renk.BOLD}⚠️  {len(basarisiz)} şablon atlandı (uygun OSM noktası bulunamadı):{Renk.BITIS}")
        for isim in basarisiz:
            print(f"    - {isim}")

    toplam_unit = db.execute_query("MATCH (n:Unit) RETURN count(n) AS c")[0]["c"]
    print(f"\n{Renk.BOLD}Neo4j'deki GERÇEK toplam Unit sayısı: {Renk.YESIL}{toplam_unit}{Renk.BITIS}")
    db.close()


if __name__ == "__main__":
    main()
