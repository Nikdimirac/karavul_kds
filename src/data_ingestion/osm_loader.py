"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
OpenStreetMap (Overpass API) "Barış Zamanı" (Baseline) Veri Yükleyici.

Sistemin krizden ÖNCEKİ, boş bir haritayla başlaması gerçekçi değildir: gerçek
bir Karar Destek Sistemi, kriz raporları işlenmeden ÖNCE de bölgedeki gerçek
kritik altyapıyı (hastaneler, itfaiye/polis, askeri alanlar, ana karayolları)
bilmelidir. Bu script, OpenStreetMap'in Overpass API'sinden PİLOT BÖLGE olarak
Elazığ (ve yakın çevresi) için bu gerçek-dünya verisini çeker, `src.core.models`
şemasına dönüştürür ve `Neo4jConnection` üzerinden Bilgi Grafı'na "Barış
Zamanı" (baseline) taban katmanı olarak yazar:

    OSM etiketi                          -> Model                (durum)
    -----------------------------------------------------------------------
    amenity=hospital                     -> Facility (Hastane)     Aktif
    landuse=military / amenity=military  -> Facility (Askeri Us)   Aktif
    aeroway=aerodrome                    -> Facility (Havalimani)  Aktif
    landuse=port / industrial=port /
    harbour=yes                          -> Facility (Liman)       Aktif
    amenity=fire_station                 -> Unit (Itfaiye)         Aktif
    amenity=police                       -> Unit (Polis)           Aktif
    highway=primary|trunk                -> Infrastructure(Karayolu) Açık

Tüm baseline varlıkları BİLİNÇLİ olarak "Aktif"/"Açık" (acik_mi=True) yazılır;
bir kriz raporu işlendiğinde (bkz. `src.data_ingestion.nlp_parser`) bu AYNI
isimli düğümler `Neo4jConnection.add_node`'un isim-tabanlı MERGE mantığıyla
güncellenip hasar/kapanma durumuna geçebilir.

Çalıştırmak için:
    python -m src.data_ingestion.osm_loader
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from src.core.database import Neo4jConnection
from src.core.models import (
    BackupPowerStatus,
    CommunicationNetwork,
    CommunicationNetworkType,
    EnergyInfrastructure,
    EnergyInfrastructureType,
    Facility,
    FacilityStatus,
    FacilityType,
    Infrastructure,
    InfrastructureType,
    MobilityStatus,
    OperationalStatus,
    ResourceHub,
    ResourceType,
    Unit,
    UnitType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

# Birden fazla Overpass aynası (mirror) sırayla denenir; bir tanesi
# kapalıysa/rate-limit uyguluyorsa script tamamen başarısız olmaz.
OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)

# Elazığ şehir merkezini (~38.677, 39.222) yaklaşık 25-30 km yarıçapında
# çevreleyen sabit bir bounding box: (guney, bati, kuzey, dogu).
#
# İdari sınır (Overpass "area") yerine BİLİNÇLİ olarak bir BBOX tercih
# edildi: (1) Overpass aynaları arasında "area" sorgusu için gereken
# admin_level/boundary etiketlemesi tutarsız olabiliyor ve sorgu hiç sonuç
# dönmeyebiliyor; (2) sabit bir bbox, sonucu hem daha öngörülebilir hem de
# daha hızlı/hafif kılıyor. Bu değer, `src.data_ingestion.nlp_parser`
# içindeki "KOORDINAT KALIBRASYONU" bölgesiyle (enlem 38-39, boylam 39-40)
# tutarlıdır.
ELAZIG_BBOX: Tuple[float, float, float, float] = (38.45, 39.00, 38.85, 39.50)

_REQUEST_TIMEOUT_SANIYE = 90
_OVERPASS_ZAMAN_ASIMI_SANIYE = 60  # Overpass QL icindeki [timeout:N]

# Overpass aynalarinin bir kismi (ör. overpass-api.de), `requests`in
# varsayilan "python-requests/X.Y" User-Agent'ini otomatik bir bot/scraper
# olarak isaretleyip 406 Not Acceptable ile REDDEDIYOR. Gercek, tanimlanabilir
# bir istemci kimligi vermek bu blokaji asar. `real_osm_loader.py` da (zaten
# bu modulden OVERPASS_ENDPOINTS/OSMBaselineLoader import ediyor) bu SABITI
# buradan import ederek yeniden kullanir — tek kaynak, iki modul arasinda
# senkron disi kalma riski yok.
OVERPASS_REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": "KrizYonetimKDS_Elazig/1.0 (test_project)"
}

# Overpass "beds"/"capacity" gibi etiketler cok nadir bulunur; bulunamazsa
# kullanilacak varsayimlar (gercekci ama KABA tahminlerdir).
_VARSAYILAN_HASTANE_KAPASITESI = 50.0
_VARSAYILAN_ASKERI_US_KAPASITESI = 100.0
_VARSAYILAN_PERSONEL_SAYISI = 15
# "VERİTABANI GENİŞLETME" (kanıtlanmış bir boşluk: `Havalimani`/`Liman`
# FacilityType üyeleri Enum'da ZATEN vardı ama HİÇBİR yükleyici bunları
# OSM'den ÇEKMİYORDU — grafta 1036+ askeri üs kaydına karşılık 0
# havalimanı vardı). `local_osm_reader.py`nin ZATEN kullandığı `_VARSAYILAN_
# HAVALIMANI_KAPASITESI = 200.0` İLE AYNI değer, tutarlılık için tekrarlanır
# (iki modül birbirini import ETMEZ, bkz. bu dosyanın başındaki "iki modül
# bağımsız kalsın" deseni notu).
_VARSAYILAN_HAVALIMANI_KAPASITESI = 200.0
_VARSAYILAN_LIMAN_KAPASITESI = 100.0

# "VERİTABANI GENİŞLETME — İKİNCİ DALGA": `models.py` zaten
# `EnergyInfrastructure` (Baraj/Trafo/Santral), `CommunicationNetwork`
# (Baz İstasyonu) ve `ResourceHub` (Yakıt) şemalarını TANIMLIYORDU ama
# HİÇBİR yükleyici bunları gerçek OSM verisinden ÇEKMİYORDU — grafta bu
# kategoriler SADECE birkaç sentetik örnekle (`synthetic_unit_seeder.py`)
# temsil ediliyordu (81 il için 2-3 kayıt). Bu, "Diyarbakır
# Jet Üssü" hatasıyla AYNI kök sorunun (şema var ama veri YOK) daha GENİŞ
# bir örneğidir. Gıda/Su/Tıbbi Malzeme/Mühimmat depoları BİLİNÇLİ OLARAK
# EKLENMEDİ — bunlar için OSM'de güvenilir/evrensel bir etiket YOKTUR
# (bkz. `Facility.facility_type` doğrulamasındaki AYNI "veri kaynağının
# alan doluluğu netleşmeden erken bağlanmaması" ilkesi); rastgele/güvenilmez
# bir eşleme ATANAN yanlış-pozitif riski, eksik veriden DAHA KÖTÜDÜR.
_VARSAYILAN_SANTRAL_KAPASITESI_MW = 50.0
_VARSAYILAN_TRAFO_KAPASITESI_MW = 10.0
_VARSAYILAN_BARAJ_KAPASITESI_MW = 100.0
_VARSAYILAN_BAZ_ISTASYONU_KAPSAMA_KM = 5.0
_VARSAYILAN_YAKIT_STOK_YUZDE = 75.0
_VARSAYILAN_YAKIT_TUKENME_GUN = 30.0

# highway tipine gore kaba tonaj kapasitesi varsayimi (trunk=devlet yolu,
# primary=il yolu; gercek tonaj OSM'de neredeyse hic bulunmaz).
_HIGHWAY_TONAJ_VARSAYIMLARI: Dict[str, float] = {"trunk": 60.0, "primary": 40.0}

_DUNYA_YARICAPI_KM = 6371.0


# ---------------------------------------------------------------------------
# Kucuk matematik/parcalama yardimcilari
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki enlem/boylam noktası arasındaki gerçek (jeodezik) mesafeyi
    haversine formülüyle kilometre cinsinden hesaplar."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * _DUNYA_YARICAPI_KM * math.asin(math.sqrt(a))


def _geometri_uzunlugu_km(geometry: Sequence[Dict[str, float]]) -> float:
    """Bir OSM yolunun (`out geom;` ile gelen ardışık nokta listesi) toplam
    uzunluğunu, ardışık noktalar arası haversine mesafelerini toplayarak
    hesaplar."""
    toplam = 0.0
    for a, b in zip(geometry, geometry[1:]):
        toplam += _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return toplam


def _en_uzak_iki_nokta(
    noktalar: Sequence[Tuple[float, float]]
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Küçük bir nokta kümesi içinde birbirine en UZAK iki noktayı (kaba bir
    "çap"/diameter yaklaşıklığı) bulur.

    Aynı isimli bir karayolu OSM'de genellikle TEK bir yol değil, birçok ayrı
    segmente (way) bölünmüş olarak bulunur (ör. "D-300" onlarca parçadan
    oluşabilir). Bu segmentleri TEK bir güzergah çizgisine (bkz.
    `_convert_highway_elements`) indirgerken, her segmentin uç noktaları bu
    fonksiyona verilir; O(n²) karşılaştırma (segment sayısı küçük olduğundan
    pratikte önemsiz) ile güzergahın gerçek uçtan uca ANA doğrultusunu temsil
    eden iki nokta seçilir.
    """
    en_iyi_cift = (noktalar[0], noktalar[0])
    en_iyi_mesafe = -1.0
    for i in range(len(noktalar)):
        for j in range(i + 1, len(noktalar)):
            mesafe = _haversine_km(*noktalar[i], *noktalar[j])
            if mesafe > en_iyi_mesafe:
                en_iyi_mesafe = mesafe
                en_iyi_cift = (noktalar[i], noktalar[j])
    return en_iyi_cift


def _eleman_koordinati(el: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Bir Overpass elemanının koordinatını döner: `node` elemanlarında
    doğrudan lat/lon, `out center` ile çekilen `way` elemanlarında ise
    `center.lat`/`center.lon` bulunur."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    center = el.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None, None


def _varsayilan_isim(el: Dict[str, Any], tip_etiketi: str) -> str:
    """OSM elemanının `name` etiketi yoksa, OSM tipi+id'sine dayalı BENZERSİZ
    bir isim üretir.

    Bu KRİTİKTİR: `Neo4jConnection.add_node`, düğümleri `isim` alanına göre
    MERGE eder (bkz. `database.node_to_cypher`). İsimsiz iki farklı hastaneye
    aynı sabit metni ("İsimsiz Hastane" gibi) vermek, ikisinin de YANLIŞLIKLA
    TEK bir düğüme birleşmesine (ikincinin birincinin verilerini ezmesine)
    yol açardı; OSM id'si her eleman için benzersiz olduğundan bu çakışmayı
    engeller.
    """
    osm_tip = el.get("type", "node")
    osm_id = el.get("id", "?")
    return f"{tip_etiketi} (OSM {osm_tip}/{osm_id})"


def _isimli_veya_teknik_isim(el: Dict[str, Any], gercek_ad: Optional[str], tip_etiketi: str) -> str:
    """`_varsayilan_isim`in GENELLEŞTİRİLMİŞ hali: `gercek_ad` (OSM `name`
    etiketi) VARSA BİLE `isim`e yine bir OSM tip/id soneki ekler (bkz.
    `_convert_point_elements`'teki "ULUSAL İSİM ÇAKIŞMASI DÜZELTMESİ"
    notu) — `_varsayilan_isim`in TEK BAŞINA (isimsiz elemanlar için)
    ürettiği benzersizlik garantisini, GERÇEK adı olan elemanlara da
    genişletir. Biçim `OSM_TEKNIK_KIMLIK_IMZASI`nın ("(OSM ") beklediği
    ortak önekle UYUMLUDUR (bu modül döngüsel import'tan kaçınmak için o
    sabiti DOĞRUDAN İMPORT ETMEZ, bkz. `_convert_point_elements`
    docstring'i)."""
    osm_tip = el.get("type", "node")
    osm_id = el.get("id", "?")
    taban = gercek_ad or tip_etiketi
    return f"{taban} (OSM {osm_tip}/{osm_id})"


def _tahmini_sayisal_deger(tags: Dict[str, Any], anahtarlar: Sequence[str], varsayilan: float) -> float:
    """`tags` içindeki ilk sayısal alanı (varsa) döner; yoksa `varsayilan`."""
    for anahtar in anahtarlar:
        deger = tags.get(anahtar)
        if deger is None:
            continue
        try:
            return float(deger)
        except (TypeError, ValueError):
            continue
    return varsayilan


# ---------------------------------------------------------------------------
# OSM Baseline Yukleyici
# ---------------------------------------------------------------------------


class OSMBaselineLoader:
    """OpenStreetMap Overpass API'sinden pilot bölge (varsayılan: Elazığ)
    için "Barış Zamanı" (baseline) kritik altyapı verisini çekip Bilgi
    Grafı'na yazan yükleyici.

    Kullanım:
        loader = OSMBaselineLoader(Neo4jConnection())
        sayaclar = loader.load_baseline()   # {"facilities": 6, "units": 4, "infrastructures": 3}
    """

    def __init__(
        self,
        db: Optional[Neo4jConnection] = None,
        bbox: Tuple[float, float, float, float] = ELAZIG_BBOX,
        overpass_endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
    ) -> None:
        self.db = db or Neo4jConnection()
        self.bbox = bbox
        self._endpoints = list(overpass_endpoints)

    # ------------------------------------------------------------------ #
    # Genel kullanim (public API)
    # ------------------------------------------------------------------ #

    def load_baseline(self) -> Dict[str, int]:
        """Overpass'tan veri çeker, modellere dönüştürür ve Neo4j'e yazar
        (`add_node` ile upsert/MERGE — script birden çok kez çalıştırılsa
        bile kopya düğüm oluşmaz). Dönüş: {"facilities": N, "units": N,
        "infrastructures": N} şeklinde yazılan düğüm sayıları."""
        varliklar = self.build_nodes()
        sayaclar: Dict[str, int] = {}
        for anahtar, liste in varliklar.items():
            for node in liste:
                self.db.add_node(node)
            sayaclar[anahtar] = len(liste)
            logger.info("Baseline yuklendi: %s -> %d dugum", anahtar, len(liste))
        return sayaclar

    def build_nodes(self) -> Dict[str, List[Any]]:
        """Overpass'tan ham elemanları çeker ve `src.core.models` Pydantic
        modellerine (Facility/Unit/Infrastructure) dönüştürür. Neo4j'e HENÜZ
        YAZMAZ (bkz. `load_baseline`) — bu ayrım, dönüştürülen veriyi Neo4j'e
        dokunmadan denetlemek/test etmek isteyen çağıranlar için kullanışlıdır.
        """
        ham = self.fetch_raw_elements()
        facilities, units, energiler, iletisimler, kaynaklar = self._convert_point_elements(ham["noktalar"])
        infrastructures = self._convert_highway_elements(ham["yollar"])
        return {
            "facilities": facilities,
            "units": units,
            "energy_infrastructures": energiler,
            "communication_networks": iletisimler,
            "resource_hubs": kaynaklar,
            "infrastructures": infrastructures,
        }

    def fetch_raw_elements(self) -> Dict[str, List[Dict[str, Any]]]:
        """Overpass'a İKİ AYRI sorgu gönderir: nokta-tipi varlıklar
        (hastane/itfaiye/polis/askeri, `out center tags;`) ve ana karayolları
        (`out geom tags;` — tam geometri gerektiğinden ayrı sorgulanır)."""
        nokta_elemanlari = self._run_overpass_query(self._build_point_query())["elements"]
        yol_elemanlari = self._run_overpass_query(self._build_highway_query())["elements"]
        return {"noktalar": nokta_elemanlari, "yollar": yol_elemanlari}

    # ------------------------------------------------------------------ #
    # Overpass sorgu insasi
    # ------------------------------------------------------------------ #

    def _bbox_str(self) -> str:
        south, west, north, east = self.bbox
        return f"{south},{west},{north},{east}"

    def _build_point_query(self) -> str:
        bbox = self._bbox_str()
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
(
  node["amenity"="hospital"]({bbox});
  way["amenity"="hospital"]({bbox});
  node["amenity"="fire_station"]({bbox});
  way["amenity"="fire_station"]({bbox});
  node["amenity"="police"]({bbox});
  way["amenity"="police"]({bbox});
  node["landuse"="military"]({bbox});
  way["landuse"="military"]({bbox});
  node["amenity"="military"]({bbox});
  way["amenity"="military"]({bbox});
  node["aeroway"="aerodrome"]({bbox});
  way["aeroway"="aerodrome"]({bbox});
  node["landuse"="port"]({bbox});
  way["landuse"="port"]({bbox});
  node["industrial"="port"]({bbox});
  way["industrial"="port"]({bbox});
  node["harbour"="yes"]({bbox});
  way["harbour"="yes"]({bbox});
  node["power"="plant"]({bbox});
  way["power"="plant"]({bbox});
  node["power"="substation"]({bbox});
  way["power"="substation"]({bbox});
  node["waterway"="dam"]({bbox});
  way["waterway"="dam"]({bbox});
  node["man_made"="communications_tower"]({bbox});
  way["man_made"="communications_tower"]({bbox});
  node["amenity"="fuel"]({bbox});
  way["amenity"="fuel"]({bbox});
);
out center tags;
"""

    def _build_highway_query(self) -> str:
        bbox = self._bbox_str()
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
(
  way["highway"~"^(primary|trunk)$"]({bbox});
);
out geom tags;
"""

    def _run_overpass_query(self, query: str) -> Dict[str, Any]:
        """Overpass QL sorgusunu, tanımlı aynalar (mirror) üzerinde sırayla
        dener; ilk başarılı yanıtı JSON olarak döner. Hiçbiri başarılı
        olmazsa `RuntimeError` fırlatır (ağ hatası/timeout Ollama'daki
        `RuntimeError` deseniyle TUTARLIDIR, bkz. `OllamaParser._invoke_llm`).
        """
        son_hata: Optional[Exception] = None
        for endpoint in self._endpoints:
            try:
                yanit = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=_REQUEST_TIMEOUT_SANIYE,
                    headers=OVERPASS_REQUEST_HEADERS,
                )
                yanit.raise_for_status()
                return yanit.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Overpass sorgusu basarisiz oldu (%s): %s", endpoint, exc)
                son_hata = exc
                continue
        raise RuntimeError(
            f"Hicbir Overpass API ucuna ulasilamadi (denenenler: {self._endpoints}): {son_hata}"
        ) from son_hata

    # ------------------------------------------------------------------ #
    # Overpass elemanlari -> Pydantic modelleri
    # ------------------------------------------------------------------ #

    @staticmethod
    def _convert_point_elements(
        elements: List[Dict[str, Any]],
    ) -> Tuple[List[Facility], List[Unit], List[EnergyInfrastructure], List[CommunicationNetwork], List[ResourceHub]]:
        """Hastane/askeri/havalimanı/liman elemanlarını `Facility`'ye,
        itfaiye/polis elemanlarını `Unit`'e, enerji/iletişim/yakıt
        elemanlarını sırasıyla `EnergyInfrastructure`/`CommunicationNetwork`/
        `ResourceHub`'a çevirir. FacilityType enum'ında itfaiye/polis
        için karşılık gelen bir tip OLMADIĞINDAN (bkz. `src.core.models`),
        bu ikisi bilinçli olarak `Unit` (personel barındıran bir birim)
        olarak modellenir.

        "VERİTABANI GENİŞLETME — İKİNCİ DALGA" (bkz. modül-üstü
        `_VARSAYILAN_SANTRAL_KAPASITESI_MW` docstring'i): dönüş imzası BEŞ
        elemanlı bir tuple'a genişledi (eskiden SADECE `facilities, units`)
        — TÜM çağıranlar (`OSMBaselineLoader.build_nodes`, `RealOsmLoader.
        build_nodes`) buna göre GÜNCELLENDİ.

        "ULUSAL İSİM ÇAKIŞMASI" DÜZELTMESİ (bkz.
        `local_osm_reader._amenity_dugumu_uret`'teki AYNI başlıklı not):
        `isim` ARTIK HER ZAMAN bir OSM tip/id soneki taşır (SADECE isimsiz
        elemanlar için DEĞİL, GERÇEK bir `name` etiketi taşıyanlar için
        de) — birden fazla şehir/il aynı çalıştırmada yüklenirken (bkz.
        `RealOsmLoader(sehirler=[...])`) AYNI jenerik gerçek ada sahip İKİ
        FARKLI gerçek tesis (ör. iki ayrı ildeki "Devlet Hastanesi"),
        `isim`in Neo4j'deki TEK BAŞINA benzersizlik anahtarı olması
        yüzünden SESSİZCE TEK bir düğüme birleşmesin diye. `aciklama`
        alanı GERÇEK/temiz adı (varsa) ayrıca taşır — bu, `real_osm_
        loader`/`local_osm_reader`ın Sokak/Köprü için ZATEN kullandığı
        AYNI `(OSM tip/ID)` sözleşmesidir (bkz. `OSM_TEKNIK_KIMLIK_
        IMZASI` — burada DOĞRUDAN İMPORT EDİLMEZ, çünkü bu modül
        `real_osm_loader`ın KENDİSİ tarafından import edilir; döngüsel
        import'tan kaçınmak için AYNI metin biçimi bağımsız olarak
        üretilir, bkz. `_osm_kimlik_soneki`)."""
        facilities: List[Facility] = []
        units: List[Unit] = []
        energiler: List[EnergyInfrastructure] = []
        iletisimler: List[CommunicationNetwork] = []
        kaynaklar: List[ResourceHub] = []

        for el in elements:
            tags = el.get("tags", {})
            enlem, boylam = _eleman_koordinati(el)
            if enlem is None or boylam is None:
                continue

            amenity = tags.get("amenity")
            is_military = tags.get("landuse") == "military" or amenity == "military"
            is_havalimani = tags.get("aeroway") == "aerodrome"
            is_liman = (
                tags.get("landuse") == "port" or tags.get("industrial") == "port" or tags.get("harbour") == "yes"
            )
            is_santral = tags.get("power") == "plant"
            is_trafo = tags.get("power") == "substation"
            is_baraj = tags.get("waterway") == "dam"
            is_baz_istasyonu = tags.get("man_made") == "communications_tower"
            is_yakit = amenity == "fuel"
            gercek_ad = tags.get("name")

            if amenity == "hospital":
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Hastane"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.HASTANE,
                        kapasite=_tahmini_sayisal_deger(
                            tags, ("capacity:beds", "beds", "capacity"), _VARSAYILAN_HASTANE_KAPASITESI
                        ),
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_military:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Askeri Alan"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.ASKERI_US,
                        kapasite=_VARSAYILAN_ASKERI_US_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_havalimani:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Havalimani"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.HAVALIMANI,
                        kapasite=_VARSAYILAN_HAVALIMANI_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_liman:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Liman"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.LIMAN,
                        kapasite=_VARSAYILAN_LIMAN_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_santral:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Elektrik Santrali"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.SANTRAL,
                        kapasite_mw=_tahmini_sayisal_deger(
                            tags, ("plant:output:electricity",), _VARSAYILAN_SANTRAL_KAPASITESI_MW
                        ),
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_trafo:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Trafo Merkezi"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.TRAFO,
                        kapasite_mw=_VARSAYILAN_TRAFO_KAPASITESI_MW,
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_baraj:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Baraj"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.BARAJ,
                        kapasite_mw=_VARSAYILAN_BARAJ_KAPASITESI_MW,
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_baz_istasyonu:
                iletisimler.append(
                    CommunicationNetwork(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Baz Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        network_type=CommunicationNetworkType.BAZ_ISTASYONU,
                        kapsama_yaricapi_km=_VARSAYILAN_BAZ_ISTASYONU_KAPSAMA_KM,
                    )
                )
            elif is_yakit:
                kaynaklar.append(
                    ResourceHub(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Yakit Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        resource_type=ResourceType.YAKIT,
                        stok_seviyesi_yuzde=_VARSAYILAN_YAKIT_STOK_YUZDE,
                        tukenme_hizi_gun=_VARSAYILAN_YAKIT_TUKENME_GUN,
                    )
                )
            elif amenity == "fire_station":
                units.append(
                    Unit(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Itfaiye Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        unit_type=UnitType.ITFAIYE,
                        personel_sayisi=int(
                            _tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI)
                        ),
                        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
                    )
                )
            elif amenity == "police":
                units.append(
                    Unit(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Polis Karakolu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        unit_type=UnitType.POLIS,
                        personel_sayisi=int(
                            _tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI)
                        ),
                        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
                    )
                )
            # Diger amenity/landuse kombinasyonlari bu baseline yukleyicinin
            # kapsami disidir; sessizce atlanir.

        return facilities, units, energiler, iletisimler, kaynaklar

    @staticmethod
    def _convert_highway_elements(elements: List[Dict[str, Any]]) -> List[Infrastructure]:
        """Ana karayolu (`highway=primary|trunk`) elemanlarını `Infrastructure`
        güzergahlarına çevirir.

        ÖNEMLİ: OSM'de tek bir isimli karayolu (ör. "Elazığ-Malatya Yolu")
        neredeyse HER ZAMAN onlarca ayrı `way` segmentine bölünmüş haldedir.
        Bu segmentler burada `isim`e göre GRUPLANIR ve TEK bir Infrastructure
        düğümüne indirgenir (toplam uzunluk = segment uzunlukları toplamı,
        uç noktalar = tüm segment uçları arasında en uzak ikili — bkz.
        `_en_uzak_iki_nokta`); aksi halde `add_node`'un isim-bazlı MERGE'i
        (bkz. `database.node_to_cypher`) her segmenti aynı düğüme yazıp
        birbirinin üzerine yazar ve veri kaybına yol açardı.
        """
        gruplar: Dict[str, Dict[str, Any]] = {}

        for el in elements:
            geometry = el.get("geometry")
            if not geometry or len(geometry) < 2:
                continue
            tags = el.get("tags", {})
            isim = tags.get("name") or _varsayilan_isim(el, "Karayolu")
            highway_tipi = tags.get("highway", "primary")

            grup = gruplar.setdefault(
                isim, {"toplam_km": 0.0, "uc_noktalar": [], "highway_tipi": highway_tipi}
            )
            grup["toplam_km"] += _geometri_uzunlugu_km(geometry)
            grup["uc_noktalar"].append((geometry[0]["lat"], geometry[0]["lon"]))
            grup["uc_noktalar"].append((geometry[-1]["lat"], geometry[-1]["lon"]))

        infra_listesi: List[Infrastructure] = []
        for isim, grup in gruplar.items():
            uc_noktalar = grup["uc_noktalar"]
            if len(uc_noktalar) < 2:
                continue
            (enlem1, boylam1), (enlem2, boylam2) = _en_uzak_iki_nokta(uc_noktalar)
            tonaj = _HIGHWAY_TONAJ_VARSAYIMLARI.get(grup["highway_tipi"], 40.0)

            infra_listesi.append(
                Infrastructure(
                    isim=isim,
                    enlem=enlem1,
                    boylam=boylam1,
                    bitis_enlem=enlem2,
                    bitis_boylam=boylam2,
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.KARAYOLU,
                    tonaj_kapasitesi=tonaj,
                    uzunluk_km=round(grup["toplam_km"], 2),
                    acik_mi=True,
                )
            )
        return infra_listesi


def main() -> None:  # pragma: no cover - manuel/CLI calistirma amaclidir
    logging.basicConfig(level=logging.INFO)

    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()

    loader = OSMBaselineLoader(db)
    print("Overpass API'den Elazığ bölgesi 'Barış Zamanı' (baseline) verisi çekiliyor...")
    sayaclar = loader.load_baseline()

    print("\nBaseline Bilgi Grafı'na yüklendi:")
    for anahtar, adet in sayaclar.items():
        print(f"  - {anahtar}: {adet}")


if __name__ == "__main__":
    main()
