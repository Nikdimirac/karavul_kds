"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
"Kademeli Dinamik Yükleme" (Tiered Dynamic Loading) — YEREL/ÇEVRİMDIŞI OSM
Okuyucu ("Tüm Türkiye Entegrasyonu" stratejik pivotu).

SORUN: `real_osm_loader.RealOsmLoader`, canlı Overpass API sorgularıyla
TEK bir il/şehri (idari alan) çeker — bu, Elazığ/Malatya gibi BİR-İKİ il
için mükemmel çalışır, ama 81 İLİN TAMAMININ sokak-seviyesi (residential/
tertiary dahil) verisini AYNI ANDA çekip Neo4j'de tutmak (1) Overpass'ı
saatlerce/tekrar tekrar bombalamayı gerektirir (kullanım politikasına
aykırı, kapalı-devre/offline çalışma prensibiyle ÇELİŞİR), (2) 32 GB RAM'lik
bir donanımda TÜM ülkenin kılcal sokak ağını (muhtemelen milyonlarca nokta)
aynı anda bellekte/Neo4j'de tutmak PRATİK DEĞİLDİR.

ÇÖZÜM ("Kademeli Dinamik Yükleme" — 3 bileşen):
  1. YEREL OKUYUCU (bu modül): Türkiye'nin TAMAMININ önceden indirilmiş bir
     `.osm.pbf` (veya `.osm` XML) dosyasının YEREL DİSKTE bulunduğunu
     VARSAYAR — ÇALIŞMA ANINDA HİÇBİR canlı API/internet çağrısı YAPMAZ
     (bkz. `LocalOsmReader`). Ayrıştırma için `osmium` (pyosmium'un Windows
     PyPI adı) kullanılır — C++ tabanlı, akış (streaming) usulü çalışır,
     dev bir ülke-çapında dosyayı bile SABİT bir bellek tavanıyla okuyabilir
     (TÜM dosyayı belleğe YÜKLEMEZ).
  2. MAKRO/MİKRO ÇİFT KATMAN: `omurga_yukle` (BOOT/açılış anında BİR KEZ
     çalıştırılır) SADECE ana yolları (motorway/trunk/primary) ve kritik
     tesisleri (hastane/polis/itfaiye) TÜM ülke için Neo4j'e yükler — bu,
     81 ilin TAMAMINDA "hangi büyük yol nereye gidiyor, en yakın hastane/
     karakol nerede" sorularını YANITLAYABİLECEK kadar bir "iskelet"tir,
     ama kılcal/residential sokak YOKTUR (RAM'de HAFİF kalır).
     `kriz_bolgesi_yukle` (bir kriz TESPİT EDİLDİĞİNDE çağrılır) SADECE o
     kriz noktasının `yaricap_km` (varsayılan 15 km) çevresindeki TÜM
     highway tiplerini (residential dahil — "kılcal damarlar") yerel
     dosyadan ANLIK olarak okuyup Neo4j'e MERGE eder.
  3. RAM TAHLİYESİ (`gecici_kilcal_veriyi_temizle`): `kriz_bolgesi_yukle`
     ile eklenen düğümler `Infrastructure.gecici_mi=True` + `operasyon_id`
     ile İŞARETLENİR (bkz. `models.Infrastructure`); bir operasyon/senaryo
     sıfırlandığında SADECE bu işaretli düğümler güvenle SİLİNİR — kalıcı
     omurgaya (`gecici_mi=False`) ASLA dokunulmaz.

`real_osm_loader.RealOsmLoader` (Overpass tabanlı) İLE İLİŞKİSİ: O modül
SİLİNMEDİ/DEĞİŞTİRİLMEDİ — geliştirme/test ortamında (canlı internet VARSA
ve sadece 1-2 il yeterliyse) HALA geçerli, daha basit bir alternatiftir. Bu
modül ONUN YERİNE DEĞİL, ÜLKE ÇAPINDA/OFFLINE üretim dağıtımı İÇİN EKLENMİŞ
bir KARDEŞ modüldür. İKİSİ DE AYNI `models.py`/`TucbsETLLoader` ve AYNI
`(OSM way/ID #IDX)` isimlendirme sözleşmesini (bkz. `OSM_TEKNIK_KIMLIK_
IMZASI`) kullanır — bu, `database.py`'deki Dijkstra graf kurucusunun
(`_OSM_WAY_INDEX_DESENI` regex'i) ve `decision_engine.py`'deki `_temiz_isim`
temizleyicisinin, veri HANGİ yükleyiciden geldiğine BAKMAKSIZIN aynı şekilde
çalışmasını GARANTİ eder.

DÜRÜST SINIR (bu modül BU ORTAMDA gerçek bir Türkiye
çapında `.pbf` dosyasına karşı test EDİLEMEMİŞTİR, böyle bir dosya bu
geliştirme ortamında MEVCUT DEĞİLDİR): mekanizmanın kendisi (osmium akış
ayrıştırma, bbox filtreleme, Neo4j MERGE, geçici-etiketleme, temizleme),
Overpass'tan çekilmiş KÜÇÜK ama GERÇEK/standart-yapılı bir `.osm` dosyasına
karşı UÇTAN UCA doğrulanmıştır — dosya formatı
(düğüm/yol yapısı) `.pbf` ile birebir AYNIDIR, osmium ikisini de AYNI API
ile okur, bu yüzden gerçek bir Türkiye `.pbf`'i ile davranış TUTARLI olması
beklenir; YİNE DE gerçek üretim dağıtımından ÖNCE gerçek dosyayla bir SON
doğrulama YAPILMASI ŞİDDETLE ÖNERİLİR (bkz. modül sonu "KULLANIM" notu).

Gerekli paket: `pip install osmium` (bkz. requirements.txt).

Çalıştırmak için (proje kök dizininden):
    python -m src.data_ingestion.local_osm_reader omurga /yol/turkiye.osm.pbf
    python -m src.data_ingestion.local_osm_reader kriz /yol/turkiye.osm.pbf 38.42 39.22 15

SONRADAN EKLENEN NOT — "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI
(bağımsız ve yerel bir askeri komuta sistemi kriz anında devasa Tüm
Türkiye PBF dosyasını canlı olarak taramamalı/indirmemelidir): yukarıdaki
"ÇÖZÜM" bölümünün 2. maddesindeki "`kriz_bolgesi_yukle`
bir kriz TESPİT EDİLDİĞİNDE çağrılır" cümlesi ARTIK GEÇERLİ DEĞİLDİR — bu
metot (ve bu modülün TAMAMI) canlı kriz akışından (`src.ui.app`/`src.core.
decision_engine`) TAMAMEN SÖKÜLMÜŞTÜR; ARTIK sadece proje kökündeki
`seed_db.py` tarafından, sistem İLK KURULURKEN BİR KEZ çağrılır (bkz.
`LocalOsmReader` sınıf docstring'i, `kriz_bolgesi_yukle`nin yeni `kalici`
parametresi). Karar Motoru çalışma anında bu modülü HİÇ import ETMEZ —
SADECE Neo4j'de halihazırda var olan veriyi okur ("varsa vardır, yoksa
yoktur").
"""

from __future__ import annotations

import logging
import math
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import osmium
except ImportError as _exc:  # pragma: no cover - opsiyonel bagimlilik kurulu degilse
    osmium = None  # type: ignore[assignment]
    _OSMIUM_IMPORT_HATASI = _exc
else:
    _OSMIUM_IMPORT_HATASI = None

from src.core.database import Neo4jConnection
from src.core.models import (
    BaseNode,
    Facility,
    FacilityType,
    FacilityStatus,
    Infrastructure,
    InfrastructureType,
    MobilityStatus,
    OperationalStatus,
    Unit,
    UnitType,
)
from src.data_ingestion.real_osm_loader import OSM_TEKNIK_KIMLIK_IMZASI
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PBF dosya yolu konfigürasyonu ("Motoru Ignition'a Kabloa" — kullanıcı
# talebi 3): sistem, PROJE KÖKÜNDE `data/turkey-latest.osm.pbf` adlı bir
# dosya ARAR. Ortam değişkeni `TURKEY_OSM_PBF_PATH` verilirse bu ONUN
# YERİNE kullanılır (ör. dosya başka bir diskte/yolda tutuluyorsa).
# ---------------------------------------------------------------------------

VARSAYILAN_PBF_DOSYA_ADI = "turkey-latest.osm.pbf"
PBF_YOLU_ENV_DEGISKENI = "TURKEY_OSM_PBF_PATH"


def varsayilan_pbf_yolu() -> str:
    """Yerel Türkiye `.osm.pbf` dosyası için YAPILANDIRILAN yolu döner —
    dosyanın GERÇEKTEN var olup olmadığını KONTROL ETMEZ (bkz. `pbf_dosyasi_
    hazir_mi`); sadece "nerede aranacağını" çözer.

    Öncelik: 1) `TURKEY_OSM_PBF_PATH` ortam değişkeni (verilmişse, AYNEN
    kullanılır) 2) proje kökündeki `data/turkey-latest.osm.pbf` (varsayılan)."""
    override = os.environ.get(PBF_YOLU_ENV_DEGISKENI)
    if override:
        return override
    proje_koku = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(proje_koku, "data", VARSAYILAN_PBF_DOSYA_ADI)


def pbf_dosyasi_hazir_mi(pbf_dosya_yolu: str) -> bool:
    """`pbf_dosya_yolu`nun diskte GERÇEKTEN mevcut, sıradan bir dosya olup
    olmadığını döner — çağıran taraf (ör. `src.ui.app`) bunu, dosya YOKSA
    ÇÖKMEK yerine zarif bir "lütfen indirin" uyarısı göstermek için kullanır."""
    return bool(pbf_dosya_yolu) and os.path.isfile(pbf_dosya_yolu)


def _osmium_gerekli() -> None:
    """`osmium` paketi kurulu değilse AÇIK/anlaşılır bir hata fırlatır —
    modül IMPORT edilebilir kalır (ör. sadece sabitlerine erişmek için),
    ama GERÇEK bir dosya okuma denendiğinde SESSİZCE `None` ile çökmek
    yerine kurulum talimatını içeren bir mesaj verir."""
    if osmium is None:  # pragma: no cover
        raise RuntimeError(
            "'osmium' paketi kurulu değil (Yerel/Offline OSM okuma için "
            "ZORUNLUDUR). Kurulum: pip install osmium"
        ) from _OSMIUM_IMPORT_HATASI


# ---------------------------------------------------------------------------
# "Omurga" (BOOT-zamanı, TÜM ülke) filtre sabitleri
# ---------------------------------------------------------------------------

OMURGA_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary")
"""BOOT anında (`omurga_yukle`) yüklenen "ana yol" highway tipleri:
"primary, trunk, motorway"."""

OMURGA_AMENITY_TIPLERI: Tuple[str, ...] = ("hospital", "police", "fire_station")
"""BOOT anında yüklenen kritik tesis/birim `amenity` etiketleri:
"hastane, polis, itfaiye"."""

# "FAZ 2: TAKTİKSEL KATMANLAR" EKLENTİSİ (Askeri Üs + Havalimanı):
# bu ikisi `amenity` ETİKETİ KULLANMAZ — OSM'de askeri
# alanlar `landuse=military` (bazen ek olarak/yerine `military=base`),
# havalimanları `aeroway=aerodrome` ile etiketlenir. Bu yüzden `amenity`
# kümesine EKLENEMEZLER; `_OsmStreamHandler`e AYRI, kendi etiket
# anahtarlarını kontrol eden bir mantık (bkz. `askeri_havalimani_dahil`
# parametresi) eklenmiştir.
OMURGA_LANDUSE_TIPLERI: Tuple[str, ...] = ("military",)
"""Askeri alanlar için OSM'nin KLASİK/yaygın alan-kullanımı etiketi."""
OMURGA_MILITARY_TIPLERI: Tuple[str, ...] = ("base",)
"""`military=*` etiketinin (bkz. yukarısı) omurgaya dahil edilen DEĞERİ —
SADECE "base" (üs); `airfield`/`bunker`/`danger_area`/`range`/`training_
area` gibi diğer `military=*` değerleri BİLİNÇLİ OLARAK dışarıda bırakılır
(hedef özellikle "Askeri Üs"tü; bir atış poligonu/eğitim alanı
aynı stratejik öneme sahip DEĞİLDİR ve gereksiz veri hacmi/gürültü katar)."""
OMURGA_AEROWAY_TIPLERI: Tuple[str, ...] = ("aerodrome",)
"""Havalimanları için OSM'nin standart etiketi (`aeroway=aerodrome`) —
`aeroway=helipad`/`heliport` gibi küçük/yerel pistler BİLİNÇLİ OLARAK
dışarıda bırakılır (hedef özellikle "Havalimanı"ydı)."""

# ---------------------------------------------------------------------------
# "Kılcal Damar" (kriz-tetiklemeli, bbox) filtre sabiti
# ---------------------------------------------------------------------------

KILCAL_HIGHWAY_TIPLERI: Tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)
"""Bir kriz bölgesinin "TAM RESMİ" — TÜM highway tipleri (omurga +
kılcal) — `real_osm_loader._SOKAK_HIGHWAY_TIPLERI` ile AYNI küme (o
modülün ÖZEL sabitine bağımlılık kurmamak için burada BİLİNÇLİ olarak
yeniden tanımlanmıştır); `footway`/`cycleway`/`path`/`service` YİNE
bilinçli olarak dışarıda bırakılmıştır (aynı gerekçe: veri hacmi/gürültü).
Bu sabit SADECE referans/dokümantasyon amaçlıdır — `kriz_bolgesi_yukle`
BUNU DEĞİL, aşağıdaki `KILCAL_YALNIZCA_HIGHWAY_TIPLERI`yi kullanır (bkz. o
sabitin docstring'indeki DÜZELTME notu)."""

# Kanıtlanmış bir boşluk için savunma ("Omurga Silinmesi" — RAM Tahliyesi
# testinde BULUNDU): `kriz_bolgesi_yukle` İLK sürümde `KILCAL_HIGHWAY_TIPLERI`nin
# TAMAMINI (motorway/trunk/primary DAHİL) işliyordu. Sorun: `motorway/
# trunk/primary` segmentleri `omurga_yukle` tarafından ZATEN, `gecici_
# mi=False` ile KALICI olarak yüklenmiş OLABİLİR — AYNI gerçek-dünya
# segmentinin `isim`i (way ID + nokta indeksinden TÜRETİLDİĞİ İÇİN)
# DETERMİNİSTİK ve AYNI olduğundan, `kriz_bolgesi_yukle`nin bu segmenti
# TEKRAR yazması `TucbsETLLoader.bulk_insert_nodes`in isim-bazlı MERGE'i
# üzerinden var olan düğümün `gecici_mi`sini `False`tan `True`ya
# GÜNCELLEDİ — yani KALICI omurga segmentleri YANLIŞLIKLA "geçici" hâle
# geldi ve bir SONRAKİ RAM tahliyesinde (`gecici_kilcal_veriyi_temizle`)
# omurga ile BİRLİKTE SİLİNDİ (779 kalıcı trunk
# düğümü, RAM tahliyesinden SONRA 0'a düşebiliyordu). ÇÖZÜM: `kriz_bolgesi_yukle`
# artık SADECE omurganın KAPSAMADIĞI highway tiplerini (bu sabit) işler —
# motorway/trunk/primary zaten NATİONWİDE kalıcı olarak yüklü olduğundan,
# bunları bir kriz bölgesinde TEKRAR işlemenin ZATEN hiçbir FAYDASI yoktu
# (veri already var), sadece bu RİSKİ taşıyordu.
KILCAL_YALNIZCA_HIGHWAY_TIPLERI: Tuple[str, ...] = tuple(
    tip for tip in KILCAL_HIGHWAY_TIPLERI if tip not in OMURGA_HIGHWAY_TIPLERI
)
"""`kriz_bolgesi_yukle`nin GERÇEKTEN işlediği highway kümesi —
`KILCAL_HIGHWAY_TIPLERI` EKSİ `OMURGA_HIGHWAY_TIPLERI` (yani secondary/
tertiary/unclassified/residential/living_street). Omurga tipleri (motorway/
trunk/primary) BİLEREK HARİÇ TUTULUR (bkz. yukarıdaki düzeltme notu) —
bu tipler zaten `omurga_yukle` ile NATİONWİDE ve KALICI
olarak yüklenmiş OLMALIDIR; bir kriz bölgesinde AYNI segmentleri tekrar
işlemek hem GEREKSİZDİR (veri zaten var) hem de kalıcı bir düğümün geçici
işaretlenmesi RİSKİNİ taşır."""

_VARSAYILAN_YARICAP_KM = 5.0
""""OSM YARIÇAP DİYETİ" DÜZELTMESİ (bkz. `src.ui.app.
KRIZ_BOLGESI_YARICAP_KM`daki AYNI değeri taşıyan sabitin tam gerekçesi):
önceki değer 15 km idi, "OSM'den yerel yolların çekilmesi dakikalarca
kilitlenebiliyor" riskine karşı 5 km'ye düşürüldü."""

# ---------------------------------------------------------------------------
# "VERİ KİRLİLİĞİ" DÜZELTMESİ (kanıtlanmış bir boşluk: 1900+ birim / 9700+ köprü)
# ---------------------------------------------------------------------------
# SORUN: `omurga_yukle` ilk sürümde, `OMURGA_HIGHWAY_TIPLERI`ye (motorway/
# trunk/primary) uyan HER `bridge=yes` segmentini (isimsiz/birkaç metrelik
# bir menfez/üst geçit DAHİL — OSM, ana yolları genelde her küçük köprü/
# menfezde AYRI bir way'e böler) ve HER `amenity=hospital|police|fire_
# station` düğümünü/way'ini (bir hastane KAMPÜSÜNÜN içindeki isimsiz her
# alt bina/kapı DAHİL) koşulsuz olarak omurgaya yazıyordu — bu, ulusal
# "iskelet"i binlerce anlamsız/önemsiz noktayla ("toz bulutu") kirletti.
# ÇÖZÜM: `_OsmStreamHandler`e SADECE `omurga_yukle` (BOOT, TÜM ülke, tek
# seferlik) çağrısında `siki_filtre=True` verilir — bu MOD'da (1) bir
# köprü, GERÇEK bir `name` etiketi TAŞIMIYORSA VE `_OMURGA_MIN_KOPRU_
# UZUNLUK_KM`den KISAYSA (küçük/isimsiz menfez/üst geçit) atlanır, (2) bir
# tesis/birim (hastane/polis/itfaiye) GERÇEK bir `name` etiketi
# TAŞIMIYORSA (kampüs içi isimsiz alt-yapı/kapı noktası) atlanır. `kriz_
# bolgesi_yukle` (MİKRO, 15 km'lik dar bölge — zaten hacim SINIRLI) bu
# filtreyi KULLANMAZ (`siki_filtre=False`): yerel bir krizde küçük bir
# köprü bile GERÇEK bir rota alternatifi/darboğaz olabilir, o ölçekte
# veri hacmi zaten "kirlilik" seviyesine ULAŞMAZ.
_OMURGA_MIN_KOPRU_UZUNLUK_KM = 0.05
"""`siki_filtre=True` iken (SADECE omurga): bir köprü segmenti GERÇEK bir
`name` etiketi TAŞIMIYORSA, TOPLAM uzunluğu bu eşiğin (50 metre) ALTINDA
kalan segmentler "küçük menfez/üst geçit" sayılıp OMURGAYA YAZILMAZ.
İsimli (gerçek, büyük) köprüler uzunluğa BAKILMAKSIZIN HER ZAMAN yazılır
— kısa ama tarihi/isimli bir köprü bu yüzden kaybedilmez."""

_VARSAYILAN_HASTANE_KAPASITESI = 50.0
_VARSAYILAN_ASKERI_US_KAPASITESI = 100.0
_VARSAYILAN_HAVALIMANI_KAPASITESI = 200.0
"""`osm_loader.py`de karşılığı OLMAYAN, bu modüle özgü bir varsayılan —
havalimanları (bkz. "FAZ 2: TAKTİKSEL KATMANLAR" eklentisi) OSM'de nadiren
bir kapasite/yolcu-sayısı etiketi taşır; askeri üsten (100) biraz daha
yüksek jenerik bir varsayılan (200) kullanılır — İKİSİ DE gerçek bir
ölçüm DEĞİL, sadece haritada 3B sütun yüksekliği/UI göstergesi için
kaba bir göreli büyüklük ipucudur."""
_VARSAYILAN_PERSONEL_SAYISI = 15
"""`osm_loader.OSMBaselineLoader` ile AYNI varsayılanlar (tutarlılık için)."""


def _bbox_hesapla(
    merkez_enlem: float, merkez_boylam: float, yaricap_km: float
) -> Tuple[float, float, float, float]:
    """`merkez_enlem`/`merkez_boylam` çevresinde `yaricap_km` yarıçaplı
    (yaklaşık) bir kare bounding-box döner: `(enlem_min, boylam_min,
    enlem_max, boylam_max)`.

    STANDART COĞRAFİ YAKLAŞIKLIK (bu projede `database.get_real_koordinat_
    zarfi`nin marj-derece mantığıyla AYNI ruhta — kesin bir jeodezik
    poligon DEĞİL, ucuz/hızlı bir dikdörtgen tahmindir, bir bbox FİLTRESİ
    için yeterlidir): enlem'de 1° ≈ 111.32 km SABİTTİR; boylam'da 1°'nin
    kaç km olduğu ENLEME BAĞLI OLARAK DEĞİŞİR (kutuplara yaklaştıkça
    boylam çizgileri birbirine yaklaşır) — `cos(enlem)` ile düzeltilir.
    """
    enlem_delta = yaricap_km / 111.32
    boylam_delta = yaricap_km / (111.32 * math.cos(math.radians(merkez_enlem)) or 1e-9)
    return (
        merkez_enlem - enlem_delta,
        merkez_boylam - boylam_delta,
        merkez_enlem + enlem_delta,
        merkez_boylam + boylam_delta,
    )


def _nokta_bbox_icinde_mi(enlem: float, boylam: float, bbox: Tuple[float, float, float, float]) -> bool:
    enlem_min, boylam_min, enlem_max, boylam_max = bbox
    return enlem_min <= enlem <= enlem_max and boylam_min <= boylam <= boylam_max


# ---------------------------------------------------------------------------
# osmium tabanlı akış (streaming) handler'ları
# ---------------------------------------------------------------------------
# NEDEN AYRI HANDLER SINIFLARI (omurga/kılcal) DEĞİL, TEK PARAMETRİK SINIF:
# ikisi de AYNI temel mantığı (way -> highway filtresi, node -> amenity
# filtresi, opsiyonel bbox) paylaşır; TEK bir `_OsmStreamHandler` sınıfı,
# hangi highway/amenity kümesinin ve hangi bbox'ın (varsa) kullanılacağını
# PARAMETRE olarak alır — kod tekrarını ÖNLER.


class _OsmStreamHandler:
    """`osmium.SimpleHandler`'ı SARMALAR (miras almaz — `osmium=None` iken
    bile bu modülün İTHAL edilebilir kalması için sınıf tanımı `osmium`'a
    bağımlı OLMAMALIDIR; gerçek osmium-türetilmiş sınıf `_gercek_handler_
    olustur`da, SADECE `osmium` gerçekten kurulduğunda dinamik üretilir).

    Bulunan `Infrastructure`/`Facility`/`Unit` adaylarını `self.dugumler`
    listesinde biriktirir (bkz. `_yukle_ve_yaz`daki tüketim). Ülke-çapında
    bir `omurga_yukle` çağrısı bile SADECE motorway/trunk/primary + 3
    amenity tipini TUTTUĞUNDAN, bu liste TÜM ülke için dahi RAM'de rahatça
    tutulabilecek boyuttadır (asıl büyük veri — residential/tertiary sokak
    okyanusu — zaten HİÇ İŞLENMEDEN atlanır, bu FİLTRENİN TÜM AMACIDIR).
    """

    def __init__(
        self,
        highway_tipleri: Tuple[str, ...],
        amenity_tipleri: Tuple[str, ...],
        bbox: Optional[Tuple[float, float, float, float]],
        gecici_mi: bool,
        operasyon_id: Optional[str],
        siki_filtre: bool = False,
        askeri_havalimani_dahil: bool = False,
    ) -> None:
        self.highway_tipleri = set(highway_tipleri)
        self.amenity_tipleri = set(amenity_tipleri)
        self.bbox = bbox
        self.gecici_mi = gecici_mi
        self.operasyon_id = operasyon_id
        # "VERİ KİRLİLİĞİ" DÜZELTMESİ (bkz. `_OMURGA_MIN_KOPRU_UZUNLUK_KM`
        # docstring'i): SADECE `omurga_yukle` bunu `True` verir.
        self.siki_filtre = siki_filtre
        # "FAZ 2: TAKTİKSEL KATMANLAR" (Askeri Üs + Havalimanı — bkz.
        # `OMURGA_LANDUSE_TIPLERI`/`OMURGA_MILITARY_TIPLERI`/`OMURGA_
        # AEROWAY_TIPLERI` docstring'i): SADECE `omurga_yukle` bunu `True`
        # verir — `kriz_bolgesi_yukle` zaten `amenity_tipleri=()` ile
        # HİÇBİR tesis/birim işlemediğinden (SADECE sokak/köprü), bu bayrak
        # onun için anlamsızdır ve `False` kalır.
        self.askeri_havalimani_dahil = askeri_havalimani_dahil
        self.dugumler: List[BaseNode] = []
        self.way_sayisi = 0
        self.node_sayisi = 0
        self.elenen_kucuk_kopru_sayisi = 0
        self.elenen_isimsiz_tesis_sayisi = 0

    # -- osmium callback'leri (gercek handler _gercek_handler_olustur'da bunlara delege eder) --

    def _ozel_alan_tip_anahtari(self, tags: Any) -> Optional[str]:
        """Bir düğüm/way'in `tags`'ından, bu handler'ın işlediği "özel alan"
        kategorilerinden (hastane/itfaiye/polis/askeri üs/havalimanı)
        birine karşılık gelen TEK bir normalize edilmiş anahtar döner —
        HANGİ OSM etiket ANAHTARININ (amenity/landuse/military/aeroway)
        eşleştiğinden BAĞIMSIZ olarak, `_amenity_dugumu_uret`in tek bir
        ortak switch üzerinden çalışabilmesi için (bkz. o fonksiyonun
        docstring'i). Eşleşme yoksa `None` döner."""
        amenity = tags.get("amenity")
        if amenity in self.amenity_tipleri:
            return amenity
        if not self.askeri_havalimani_dahil:
            return None
        if tags.get("landuse") in OMURGA_LANDUSE_TIPLERI or tags.get("military") in OMURGA_MILITARY_TIPLERI:
            return "military"
        if tags.get("aeroway") in OMURGA_AEROWAY_TIPLERI:
            return "aerodrome"
        return None

    def _node_isle(self, n: Any) -> None:
        self.node_sayisi += 1
        tip_anahtari = self._ozel_alan_tip_anahtari(n.tags)
        if tip_anahtari is None:
            return
        # "VERİ KİRLİLİĞİ" DÜZELTMESİ (bkz. `_OMURGA_MIN_KOPRU_UZUNLUK_KM`
        # docstring'i): SADECE omurga modunda, GERÇEK bir `name` etiketi
        # TAŞIMAYAN tesis/birim noktaları (ör. bir hastane kampüsünün
        # isimsiz alt-kapı/bina noktaları) atlanır.
        if self.siki_filtre and not str(n.tags.get("name") or "").strip():
            self.elenen_isimsiz_tesis_sayisi += 1
            return
        if not n.location.valid():
            return
        enlem, boylam = n.location.lat, n.location.lon
        if self.bbox is not None and not _nokta_bbox_icinde_mi(enlem, boylam, self.bbox):
            return
        varlik = _amenity_dugumu_uret(tip_anahtari, n.tags, n.id, "node", enlem, boylam)
        if varlik is not None:
            self.dugumler.append(varlik)

    def _way_isle(self, w: Any) -> None:
        self.way_sayisi += 1
        tags = w.tags
        highway = tags.get("highway")
        # Askeri üs/havalimanı OSM'de HEMEN HEMEN HER ZAMAN bir poligon
        # (way/relation), NADİREN tek bir düğümdür — bu yüzden bu iki tip
        # için asıl işlenen kod yolu, `_node_isle` DEĞİL, BURASIDIR.
        tip_anahtari = self._ozel_alan_tip_anahtari(tags)

        if tip_anahtari is not None and self.siki_filtre and not str(tags.get("name") or "").strip():
            self.elenen_isimsiz_tesis_sayisi += 1
        if tip_anahtari is not None and (
            not self.siki_filtre or str(tags.get("name") or "").strip()
        ):
            # Bazi hastane/karakol/itfaiye/askeri-us/havalimani alanlari
            # OSM'de bir NOKTA degil bir POLIGON (way) olarak cizilidir;
            # temsili konum icin ILK gecerli dugum kullanilir (tam merkez/
            # centroid hesaplamak bu MVP'nin kapsami disidir — "yaklasik
            # ama gercek veriden turetilmis" bir nokta, hic nokta
            # olmamasindan HER ZAMAN iyidir).
            ilk_nokta = _way_ilk_gecerli_nokta(w)
            if ilk_nokta is not None:
                enlem, boylam = ilk_nokta
                if self.bbox is None or _nokta_bbox_icinde_mi(enlem, boylam, self.bbox):
                    varlik = _amenity_dugumu_uret(tip_anahtari, tags, w.id, "way", enlem, boylam)
                    if varlik is not None:
                        self.dugumler.append(varlik)

        if highway not in self.highway_tipleri:
            return

        gercek_ad = tags.get("name")
        bridge_etiketi = str(tags.get("bridge", "") or "").strip().lower()
        geometri = _way_gecerli_koordinatlari(w)
        if len(geometri) < 2:
            return

        if self.bbox is not None and not any(_nokta_bbox_icinde_mi(e, b, self.bbox) for e, b in geometri):
            return

        if bridge_etiketi not in ("", "no", "false", "0"):
            uzunluk_km = _geometri_uzunlugu_km(geometri)
            # "VERİ KİRLİLİĞİ" DÜZELTMESİ (bkz. `_OMURGA_MIN_KOPRU_UZUNLUK_
            # KM` docstring'i): SADECE omurga modunda, isimsiz VE kısa
            # (küçük menfez/üst geçit) köprü segmentleri atlanır.
            if (
                self.siki_filtre
                and not str(gercek_ad or "").strip()
                and uzunluk_km < _OMURGA_MIN_KOPRU_UZUNLUK_KM
            ):
                self.elenen_kucuk_kopru_sayisi += 1
                return
            temel_isim = gercek_ad or "Kopru"
            self.dugumler.append(
                Infrastructure(
                    isim=f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}way/{w.id})",
                    aciklama=gercek_ad,
                    enlem=geometri[0][0],
                    boylam=geometri[0][1],
                    bitis_enlem=geometri[-1][0],
                    bitis_boylam=geometri[-1][1],
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.KOPRU,
                    uzunluk_km=uzunluk_km,
                    acik_mi=True,
                    highway_tipi=highway,
                    gecici_mi=self.gecici_mi,
                    operasyon_id=self.operasyon_id,
                )
            )
            return

        isim_on_eki = gercek_ad or "Sokak Dugumu"
        for idx, (enlem, boylam) in enumerate(geometri):
            self.dugumler.append(
                Infrastructure(
                    isim=f"{isim_on_eki} {OSM_TEKNIK_KIMLIK_IMZASI}way/{w.id} #{idx})",
                    aciklama=gercek_ad,
                    enlem=enlem,
                    boylam=boylam,
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.SOKAK,
                    uzunluk_km=0.0,
                    acik_mi=True,
                    highway_tipi=highway,
                    gecici_mi=self.gecici_mi,
                    operasyon_id=self.operasyon_id,
                )
            )


def _way_gecerli_koordinatlari(w: Any) -> List[Tuple[float, float]]:
    """Bir osmium `Way`in düğümlerinden GEÇERLİ (konumu çözülmüş)
    `(enlem, boylam)` çiftlerini sırayla döner; çözülemeyen (`locations=
    True` ile `apply_file` çağrılmadıysa VEYA dosyada eksik bir düğüme
    referans varsa) düğümler SESSİZCE atlanır — bir segmentin BİR
    düğümünün eksik olması, TÜM segmenti kaybetmek yerine SADECE o tek
    noktayı kaybetmeyi tercih eder."""
    sonuc: List[Tuple[float, float]] = []
    for wn in w.nodes:
        try:
            if wn.location.valid():
                sonuc.append((wn.location.lat, wn.location.lon))
        except Exception:  # noqa: BLE001 - cozumlenmemis konum erisimi osmium'da istisna firlatabilir.
            continue
    return sonuc


def _way_ilk_gecerli_nokta(w: Any) -> Optional[Tuple[float, float]]:
    koordinatlar = _way_gecerli_koordinatlari(w)
    return koordinatlar[0] if koordinatlar else None


def _geometri_uzunlugu_km(geometri: List[Tuple[float, float]]) -> float:
    """Ardışık `(enlem, boylam)` noktaları arasındaki haversine
    mesafelerinin TOPLAMI — `real_osm_loader`daki `_geometri_uzunlugu_km`
    ile AYNI hesap, sadece girdi şekli (Overpass dict listesi yerine
    düz tuple listesi) farklı olduğu için burada bağımsız tanımlanmıştır."""
    toplam = 0.0
    for (e1, b1), (e2, b2) in zip(geometri, geometri[1:]):
        toplam += Neo4jConnection._geodesic_km((e1, b1), (e2, b2))
    return round(toplam, 3)


_TIP_ANAHTARI_JENERIK_ISIM: Dict[str, str] = {
    "hospital": "Hastane",
    "fire_station": "Itfaiye Istasyonu",
    "police": "Polis Karakolu",
    "military": "Askeri Us",
    "aerodrome": "Havalimani",
}
"""`_amenity_dugumu_uret`in, GERÇEK bir `name` etiketi OLMAYAN elemanlar
için kullandığı jenerik/okunabilir isim — ham OSM etiket değerini
(`"hospital"`, `"aerodrome"` gibi İngilizce/teknik kelimeleri) doğrudan
`isim`e/UI'a sızdırmamak için (bkz. `tip_anahtari.title()`in eskiden
"Aerodrome" gibi İngilizce bir kelime üreteceği durum)."""


def _amenity_dugumu_uret(
    tip_anahtari: str, tags: Any, osm_id: int, osm_tip: str, enlem: float, boylam: float
) -> Optional[BaseNode]:
    """Bir `amenity=hospital|police|fire_station` VEYA (bkz. "FAZ 2:
    TAKTİKSEL KATMANLAR" eklentisi) `landuse=military`/`military=base`/
    `aeroway=aerodrome` düğümünü/yolunu `Facility`/`Unit`e çevirir —
    `osm_loader.OSMBaselineLoader._convert_point_elements` ile AYNI alan/
    varsayılan sözleşmesini kullanır (tutarlılık için), ama GİRDİ ŞEKLİ
    osmium nesneleri OLDUĞUNDAN (Overpass dict'leri DEĞİL) bağımsız
    yeniden yazılmıştır. `tip_anahtari`, HANGİ OSM etiket ANAHTARININ
    (amenity/landuse/military/aeroway) eşleştiğinden BAĞIMSIZ, normalize
    edilmiş tek bir kategori dizesidir (bkz. `_OsmStreamHandler.
    _ozel_alan_tip_anahtari`) — "hospital"/"fire_station"/"police"/
    "military"/"aerodrome" değerlerinden biridir.

    "ULUSAL İSİM ÇAKIŞMASI" DÜZELTMESİ: `isim` ARTIK HER
    ZAMAN bir OSM `node`/`way` kimlik soneki taşır (Sokak/Köprü'de zaten
    uygulanan AYNI `(OSM node/ID)`/`(OSM way/ID)` sözleşmesi, bkz.
    `OSM_TEKNIK_KIMLIK_IMZASI`) — SADECE isimsiz elemanlar için DEĞİL,
    GERÇEK bir `name` etiketi taşıyanlar için de. Sebep: Türkiye çapında
    BİRDEN FAZLA hastane/karakol/itfaiye TAMAMEN AYNI jenerik gerçek ada
    sahip olabilir (ör. "Devlet Hastanesi", "İtfaiye Merkezi") — `isim`
    alanı Neo4j'de TEK başına benzersizlik anahtarı olduğundan (bkz.
    `database.ensure_constraints`), bu durumda FARKLI şehirlerdeki GERÇEK,
    AYRI tesisler `isim` eşleşmesi yüzünden SESSİZCE TEK bir düğüme
    birleşebiliyordu (ulusal omurga yüklemesinde 100+ Facility, 250+ Unit
    bu şekilde kayboluyordu). `aciklama` alanı GERÇEK/temiz
    adı (varsa) ayrıca taşımaya devam eder — komuta arayüzü/Karar Motoru
    (`decision_engine._temiz_isim`, `database.update_infrastructure_status_
    by_name`) bu tekniği Sokak/Köprü için ZATEN kullanıyordu; burada AYNI
    mekanizma yeniden kullanılır (bkz. `src.ui.app._grafa_yaz`'daki
    `_OSM_KAYNAKLI_FACILITY_TIPLERI`/`_OSM_KAYNAKLI_UNIT_TIPLERI`)."""
    gercek_ad = tags.get("name")
    temel_isim = gercek_ad or _TIP_ANAHTARI_JENERIK_ISIM.get(tip_anahtari, tip_anahtari.title())
    isim = f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}{osm_tip}/{osm_id})"
    if tip_anahtari == "hospital":
        kapasite = _tahmini_sayisal_deger(tags, ("capacity:beds", "beds", "capacity"), _VARSAYILAN_HASTANE_KAPASITESI)
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.HASTANE,
            kapasite=kapasite,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    if tip_anahtari == "fire_station":
        personel = int(_tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI))
        return Unit(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            unit_type=UnitType.ITFAIYE,
            personel_sayisi=personel,
            hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        )
    if tip_anahtari == "police":
        personel = int(_tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI))
        return Unit(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            unit_type=UnitType.POLIS,
            personel_sayisi=personel,
            hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        )
    # "FAZ 2: TAKTİKSEL KATMANLAR" EKLENTİSİ (Askeri Üs
    # + Havalimanı): Hastane/İtfaiye/Polis'in AKSİNE bunlar bir "Unit"
    # (personel/mobil birim) DEĞİL, `osm_loader._convert_point_elements`
    # ile AYNI mimari kararla `Facility` (sabit, coğrafi bir TESİS/alan)
    # olarak modellenir — bir askeri üs veya havalimanı, KENDİSİ bir
    # "birlik" değil, üzerinde birlik/uçak barındıran bir YER'dir (bkz.
    # `models.FacilityType.ASKERI_US`/`HAVALIMANI` — bu iki değer zaten
    # önceden VARDI, sadece OSM'den GERÇEKTEN besleniyor OLMALARI yeniydi).
    if tip_anahtari == "military":
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.ASKERI_US,
            kapasite=_VARSAYILAN_ASKERI_US_KAPASITESI,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    if tip_anahtari == "aerodrome":
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.HAVALIMANI,
            kapasite=_VARSAYILAN_HAVALIMANI_KAPASITESI,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    return None


def _tahmini_sayisal_deger(tags: Any, anahtarlar: Tuple[str, ...], varsayilan: float) -> float:
    for anahtar in anahtarlar:
        deger = tags.get(anahtar)
        if deger is None:
            continue
        try:
            return float(deger)
        except (TypeError, ValueError):
            continue
    return varsayilan


def _gercek_handler_olustur(ic_handler: _OsmStreamHandler) -> Any:
    """`_OsmStreamHandler`ı GERÇEK bir `osmium.SimpleHandler` alt sınıfına
    SARAR — bu dolaylılık, `_OsmStreamHandler`ın (ve onu içeren bu MODÜLÜN)
    `osmium` KURULU OLMASA BİLE import edilebilir kalmasını sağlar (bkz.
    modül başındaki `try/except ImportError`); gerçek osmium sınıfı SADECE
    bir dosya GERÇEKTEN okunacağı an, burada, dinamik olarak tanımlanır."""
    _osmium_gerekli()

    class _Handler(osmium.SimpleHandler):  # type: ignore[misc]
        def node(self, n: Any) -> None:
            ic_handler._node_isle(n)

        def way(self, w: Any) -> None:
            ic_handler._way_isle(w)

    return _Handler()


# ---------------------------------------------------------------------------
# LocalOsmReader — genel kullanim (public API)
# ---------------------------------------------------------------------------


class LocalOsmReader:
    """Yerel diskteki bir Türkiye `.osm.pbf` (veya `.osm` XML) dosyasından
    Neo4j'e veri yükler.

    "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI (bağımsız ve yerel bir
    askeri komuta sistemi kriz anında harita inşa etmemelidir): bu sınıf
    ARTIK SADECE proje kökündeki `seed_db.py`
    tarafından, sistem İLK KURULURKEN BİR KEZ çağrılır — canlı kriz akışı
    (bkz. `src.ui.app`/`src.core.decision_engine`) bu sınıfı HİÇ import
    ETMEZ/çağırmaz; Karar Motoru çalışma anında SADECE Neo4j'i okur.

    Kullanım (SADECE `seed_db.py` içinde, TEK SEFERLİK kurulum akışı):
        reader = LocalOsmReader(r"C:\\veri\\turkiye-latest.osm.pbf", Neo4jConnection())
        reader.omurga_yukle()                                              # TÜM ülke, KALICI
        reader.kriz_bolgesi_yukle(38.42, 39.22, 15.0, kalici=True)         # bir bölge, KALICI

    (`kalici=False` — varsayılan — ve `gecici_kilcal_veriyi_temizle` hâlâ
    mevcuttur, ama SADECE CLI/manuel kullanım ve geriye dönük uyumluluk
    içindir; yeni "kalıcı harita verisi" akışı YUKARIDAKİ `kalici=True`
    çağrısıdır.)
    """

    def __init__(self, pbf_dosya_yolu: str, db: Optional[Neo4jConnection] = None) -> None:
        self.pbf_dosya_yolu = pbf_dosya_yolu
        self.db = db or Neo4jConnection()

    # ------------------------------------------------------------------ #
    # 1) MAKRO KATMAN — BOOT-zamanı, TÜM ülke, SADECE omurga
    # ------------------------------------------------------------------ #

    def omurga_yukle(self) -> Dict[str, int]:
        """Yerel dosyanın TAMAMINI TEK BİR akış (streaming) geçişinde
        okur; SADECE `OMURGA_HIGHWAY_TIPLERI` (motorway/trunk/primary),
        `OMURGA_AMENITY_TIPLERI` (hastane/polis/itfaiye) VE (bkz. "FAZ 2:
        TAKTİKSEL KATMANLAR" eklentisi) `OMURGA_LANDUSE_TIPLERI`/`OMURGA_
        MILITARY_TIPLERI` (askeri üs) + `OMURGA_AEROWAY_TIPLERI`
        (havalimanı) filtresine uyan düğümleri Neo4j'e yazar — TÜM diğer
        veri (residential/tertiary sokak "okyanusu") HİÇ İŞLENMEDEN
        atlanır, bu yüzden ülke-çapında bile RAM/Neo4j ayak izi KÜÇÜK
        kalır. `Infrastructure.gecici_mi` HER ZAMAN `False`tur (bu KALICI
        omurgadır, RAM tahliyesi bunu ASLA silmez).

        "VERİ KİRLİLİĞİ" DÜZELTMESİ (kanıtlanmış bir boşluk: 1900+ birim/9700+
        köprü): `siki_filtre=True` verilir (bkz. `_OMURGA_MIN_KOPRU_
        UZUNLUK_KM` docstring'i) — isimsiz/küçük menfez-üst geçit köprüleri
        VE isimsiz tesis/birim noktaları (ör. bir hastane kampüsünün alt-
        bina/kapı noktaları) ulusal omurgaya YAZILMAZ; SADECE gerçek/isimli
        ve büyük ölçekli köprüler + tesisler kalıcı hale gelir — bu kural
        askeri üs/havalimanı için de AYNEN geçerlidir.

        Returns:
            {"Facility": N, "Infrastructure": N, "Unit": N} yazılan düğüm
            sayıları (bkz. `TucbsETLLoader.bulk_insert_nodes`).
        """
        logger.info(
            "OMURGA yükleme başlıyor (BOOT-zamanı, TÜM ülke, SADECE %s + %s + askeri-us(%s/%s) + "
            "havalimani(%s), SIKI FİLTRE aktif): %s",
            OMURGA_HIGHWAY_TIPLERI, OMURGA_AMENITY_TIPLERI, OMURGA_LANDUSE_TIPLERI,
            OMURGA_MILITARY_TIPLERI, OMURGA_AEROWAY_TIPLERI, self.pbf_dosya_yolu,
        )
        ic_handler = _OsmStreamHandler(
            highway_tipleri=OMURGA_HIGHWAY_TIPLERI,
            amenity_tipleri=OMURGA_AMENITY_TIPLERI,
            bbox=None,
            gecici_mi=False,
            operasyon_id=None,
            siki_filtre=True,
            askeri_havalimani_dahil=True,
        )
        return self._yukle_ve_yaz(ic_handler, "omurga")

    # ------------------------------------------------------------------ #
    # 2) MİKRO KATMAN — kriz-tetiklemeli, bbox, TÜM highway tipleri
    # ------------------------------------------------------------------ #

    def kriz_bolgesi_yukle(
        self,
        merkez_enlem: float,
        merkez_boylam: float,
        yaricap_km: float = _VARSAYILAN_YARICAP_KM,
        operasyon_id: Optional[str] = None,
        kalici: bool = False,
    ) -> Dict[str, Any]:
        """Verilen merkezin `yaricap_km` (varsayılan 15 km) çevresindeki
        bounding-box İÇİNDE kalan "kılcal damar" highway tiplerini
        (residential/tertiary/secondary/unclassified/living_street, bkz.
        `KILCAL_YALNIZCA_HIGHWAY_TIPLERI`) yerel dosyadan okuyup Neo4j'e
        MERGE eder.

        BİLİNÇLİ OLARAK motorway/trunk/primary İŞLEMEZ: bu tipler zaten
        `omurga_yukle` ile NATİONWİDE ve KALICI (`gecici_mi=False`) olarak
        yüklenmiş OLMALIDIR; aynı segmentleri burada TEKRAR işlemek hem
        gereksizdir hem de MERGE-by-isim üzerinden kalıcı düğümün
        `gecici_mi`sini `True`ya çevirip bir SONRAKİ RAM tahliyesinde
        omurganın YANLIŞLIKLA silinmesine yol açabilir (bkz. `KILCAL_
        YALNIZCA_HIGHWAY_TIPLERI` docstring'indeki düzeltme notu).

        "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI SONRASI (kriz
        motorunun canlı akışta HİÇBİR ŞEKİLDE harita I/O'suna girmemesi
        gerektiği ilkesi): bu metot ARTIK `src.ui.app`daki canlı kriz akışından
        ÇAĞRILMIYOR — TEK çağıran taraf proje kökündeki `seed_db.py`dir
        (sistem İLK KURULURKEN, BİR KEZ). Bu yüzden `kalici` parametresi
        eklendi:
          - `kalici=False` (VARSAYILAN, eski davranış KORUNUR — CLI/manuel
            kullanım ve geriye dönük uyumluluk için): eklenen düğümler
            `gecici_mi=True` + `operasyon_id` ile işaretlenir (bkz.
            `gecici_kilcal_veriyi_temizle`).
          - `kalici=True` (SADECE `seed_db.py`nin KULLANDIĞI mod): eklenen
            düğümler `omurga_yukle` ile AYNI şekilde `gecici_mi=False`,
            `operasyon_id=None` ile KALICI yazılır — bir kriz senaryosu
            sıfırlandığında (`db.reset_crisis_scenario`) ASLA silinmez;
            "varsa vardır, yoksa yoktur" kuralının gerektirdiği KALICI
            harita verisi budur.

        DÜRÜST SINIR (MVP): dosyanın TAMAMI HER ÇAĞRIDA yeniden taranır
        (tek geçişli akış filtreleme — büyük bir Türkiye dosyasında bu her
        bölge için saniyeler-onlarca saniye sürebilir, dosya boyutuna
        bağlıdır). `seed_db.py`nin TEK SEFERLİK/kurulum-anı doğası
        ("canlı kriz akışında bu maliyete GİRME" ilkesi) bu maliyeti ZATEN
        kabul edilebilir kılar;
        gerçek üretimde, dosyayı ÖNCEDEN bölgesel karo (tile)'lara ayırıp
        SADECE ilgili karoyu okumak (ör. `osmium extract` ile önceden
        hazırlanmış il/ilçe parçaları) VEYA bir mekansal indeks kurmak
        DOĞAL bir sonraki optimizasyon adımıdır — bu MVP'nin kapsamı
        DIŞINDADIR, ama ileride gerekirse KOLAYCA eklenebilir (bu sınıfın
        PUBLIC arayüzü DEĞİŞMEDEN, sadece `_yukle_ve_yaz`in dosya-okuma
        stratejisi değişir).

        Returns:
            {"operasyon_id": str veya None, "Infrastructure": N, ...} —
            `kalici=False` iken `operasyon_id` çağıran tarafça
            SAKLANMALIDIR (ör. `SituationalPicture`/UI oturum durumunda),
            sonradan `gecici_kilcal_veriyi_temizle`ye geçmek için;
            `kalici=True` iken `operasyon_id` HER ZAMAN `None`dır (kalıcı
            düğümlerin bir "operasyonu" yoktur, bkz. `omurga_yukle`).
        """
        if kalici:
            gecici_mi = False
            operasyon_id = None
        else:
            gecici_mi = True
            operasyon_id = operasyon_id or str(uuid.uuid4())
        bbox = _bbox_hesapla(merkez_enlem, merkez_boylam, yaricap_km)
        logger.info(
            "BÖLGE yükleme başlıyor (kalici=%s, operasyon_id=%s, merkez=(%.5f, %.5f), yarıçap=%.1f km, "
            "bbox=%s): %s",
            kalici, operasyon_id, merkez_enlem, merkez_boylam, yaricap_km, bbox, self.pbf_dosya_yolu,
        )
        ic_handler = _OsmStreamHandler(
            highway_tipleri=KILCAL_YALNIZCA_HIGHWAY_TIPLERI,
            amenity_tipleri=(),  # kriz-bolgesi yuklemesi SADECE sokak/kopru icindir.
            bbox=bbox,
            gecici_mi=gecici_mi,
            operasyon_id=operasyon_id,
        )
        etiket = "kalici-bolge" if kalici else f"kriz-bolgesi[{operasyon_id}]"
        sonuc = self._yukle_ve_yaz(ic_handler, etiket)
        sonuc["operasyon_id"] = operasyon_id
        return sonuc

    # ------------------------------------------------------------------ #
    # 3) RAM TAHLİYESİ (garbage collection)
    # ------------------------------------------------------------------ #

    def gecici_kilcal_veriyi_temizle(self, operasyon_id: Optional[str] = None) -> int:
        """`kriz_bolgesi_yukle` ile eklenen GEÇİCİ düğümleri Neo4j'den
        siler — GÜVENLİ: SADECE `Infrastructure.gecici_mi = true` olan
        düğümleri hedefler, kalıcı omurgaya (`gecici_mi = false`) ASLA
        dokunmaz (Cypher sorgusunun `WHERE` koşulu bunu YAPISAL olarak
        garanti eder, ayrı bir "iki kez kontrol et" mantığı GEREKMEZ).

        `operasyon_id` verilirse SADECE o operasyona ait düğümler silinir
        (birden fazla eşzamanlı kriz bölgesi varsa, birini temizlemek
        diğerini ETKİLEMEZ). `operasyon_id=None` ise (varsayılan DEĞİLDİR,
        BİLİNÇLİ bir çağrı gerektirir) TÜM geçici kılcal veri (HANGİ
        operasyona ait olursa olsun) silinir — bu "hepsini temizle" modu,
        çağıran tarafın (ör. `src.ui.app`) TÜM aktif operasyonların
        `operasyon_id`lerini takip ETMEDİĞİ basit/tek-operasyon
        senaryolar için bir kolaylıktır.

        "GÜVENLİ BATCH" MİMARİSİ (RAM limitini elle artırmak KABA KUVVET
        sorununu ÇÖZMEZ, sadece ERTELER): eski sürüm TÜM eşleşen düğümleri
        TEK bir `collect`+
        `FOREACH DETACH DELETE` transaction'ında siliyordu — yoğun/geniş
        bir kriz bölgesinde (ör. büyükşehir merkezi, 15 km yarıçap içinde
        onbinlerce kılcal sokak noktası) bu, `database.Neo4jConnection.
        clear_database`deki AYNI SINIF bir `Neo.TransientError.General.
        MemoryPoolOutOfMemoryError` riski taşır (bkz. o metodun
        docstring'i). Artık `Neo4jConnection.batched_bulk_write` (bkz. o
        metodun docstring'i) KULLANILIR — silme `_GUVENLI_BATCH_BOYUTU`luk
        (50.000) KÜÇÜK, BAĞIMSIZ transaction'lara bölünür; donanım/bellek
        yapılandırması NE OLURSA OLSUN RAM Tahliyesi ASLA tek bir dev
        transaction'a bel bağlamaz.

        Returns:
            Silinen düğüm sayısı.
        """
        if operasyon_id is not None:
            match_where = "MATCH (n:Infrastructure {gecici_mi: true, operasyon_id: $operasyon_id})"
            hedef_aciklama = f"operasyon_id='{operasyon_id}'"
        else:
            match_where = "MATCH (n:Infrastructure {gecici_mi: true})"
            hedef_aciklama = "TÜM geçici kılcal veri (operasyon_id filtresi YOK)"

        silinen = self.db.batched_bulk_write(
            match_where,
            "DETACH DELETE n",
            params={"operasyon_id": operasyon_id} if operasyon_id is not None else None,
            islem_adi=f"RAM TAHLİYESİ ({hedef_aciklama})",
        )
        logger.info("RAM TAHLİYESİ tamamlandı (%s): %d düğüm silindi.", hedef_aciklama, silinen)
        return silinen

    # ------------------------------------------------------------------ #
    # Ortak dosya-okuma/yazma mantığı
    # ------------------------------------------------------------------ #

    def _yukle_ve_yaz(self, ic_handler: _OsmStreamHandler, etiket: str) -> Dict[str, int]:
        """`ic_handler`ı yerel dosyaya ATIP (akış/streaming — TEK geçiş)
        biriken adayları `TucbsETLLoader.bulk_insert_nodes` ile Neo4j'e
        yazar. `locations=True`: osmium'un way-düğüm konumlarını dahili
        bir bellek-içi indeksle ÇÖZMESİNİ sağlar (bkz. modül-üstü canlı
        API doğrulaması) — BU OLMADAN way geometrisi (sokak/köprü
        koordinatları) ÇÖZÜLEMEZ."""
        _osmium_gerekli()
        gercek_handler = _gercek_handler_olustur(ic_handler)
        try:
            gercek_handler.apply_file(self.pbf_dosya_yolu, locations=True)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Yerel OSM dosyası okunamadı ('{self.pbf_dosya_yolu}'): {exc}. "
                "Dosya yolunun doğru olduğundan ve dosyanın geçerli bir .osm/.osm.pbf "
                "olduğundan emin olun."
            ) from exc

        logger.info(
            "[%s] Yerel dosya tarandı: %d way, %d node işlendi; %d aday varlık bulundu "
            "(siki_filtre=%s ile elenen: %d küçük/isimsiz köprü, %d isimsiz tesis/birim).",
            etiket, ic_handler.way_sayisi, ic_handler.node_sayisi, len(ic_handler.dugumler),
            ic_handler.siki_filtre, ic_handler.elenen_kucuk_kopru_sayisi,
            ic_handler.elenen_isimsiz_tesis_sayisi,
        )
        if not ic_handler.dugumler:
            logger.warning("[%s] Filtreye uyan HİÇBİR varlık bulunamadı — 0 düğüm yazıldı.", etiket)
            return {}

        loader = TucbsETLLoader(self.db)
        sayaclar = loader.bulk_insert_nodes(ic_handler.dugumler, toplam_tahmini=len(ic_handler.dugumler))
        logger.info("[%s] Neo4j'e yazıldı: %s", etiket, sayaclar)
        return sayaclar


def main() -> None:  # pragma: no cover - manuel/CLI calistirma amaclidir
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 3:
        print(
            "Kullanım:\n"
            "  python -m src.data_ingestion.local_osm_reader omurga <dosya.osm.pbf>\n"
            "  python -m src.data_ingestion.local_osm_reader kriz <dosya.osm.pbf> <enlem> <boylam> [yaricap_km]\n"
            "  python -m src.data_ingestion.local_osm_reader temizle <operasyon_id|--hepsi>"
        )
        sys.exit(1)

    komut = sys.argv[1]
    db = Neo4jConnection()
    db.connect()

    if komut == "omurga":
        reader = LocalOsmReader(sys.argv[2], db)
        print(reader.omurga_yukle())
    elif komut == "kriz":
        enlem, boylam = float(sys.argv[3]), float(sys.argv[4])
        yaricap = float(sys.argv[5]) if len(sys.argv) > 5 else _VARSAYILAN_YARICAP_KM
        reader = LocalOsmReader(sys.argv[2], db)
        print(reader.kriz_bolgesi_yukle(enlem, boylam, yaricap))
    elif komut == "temizle":
        # NOT: temizleme dosya OKUMAZ (SADECE Neo4j'e Cypher DELETE atar),
        # bu yuzden `LocalOsmReader`in `pbf_dosya_yolu` alani burada
        # KULLANILMAZ; yine de sinifi tek-tip kurmak icin bos bir deger
        # verilir.
        hedef = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "--hepsi" else None
        reader = LocalOsmReader("", db)
        print(f"{reader.gecici_kilcal_veriyi_temizle(hedef)} düğüm silindi.")
    else:
        print(f"Bilinmeyen komut: {komut}")
        sys.exit(1)

    db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
