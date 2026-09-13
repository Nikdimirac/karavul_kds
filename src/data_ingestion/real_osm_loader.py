"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
GERÇEK Şehir Topolojisi Yükleyici (OSM Overpass API -> TucbsETLLoader).

`tucbs_etl_loader.generate_mock_tucbs_data`, `TucbsETLLoader`'ın UNWIND-batch
mekanizmasını uçtan uca sınamak için SAHTE (rastgele koordinatlı) veri
üretiyordu; bu sahte veri PyDeck haritasında gerçek bir şehir dokusu değil,
anlamsız/devasa tek bir kırmızı blok olarak görünüyordu. Bu modül, o mock
veri kaynağının yerine GERÇEK bir kaynak koyar: OpenStreetMap Overpass
API'sinden bir veya birden fazla il/şehir için gerçek karayolu/sokak ağını
ve kritik tesisleri çeker.

`osm_loader.OSMBaselineLoader` ile İLİŞKİSİ: `OSMBaselineLoader` sadece ana
karayollarını (`highway=primary|trunk`, TEK bir isimli güzergaha indirgenmiş)
ve nokta-tipi tesisleri (hastane/askeri/itfaiye/polis) çeker — "Barış Zamanı"
taban katmanı için yeterlidir ama sokak-seviyesi yoğunluk/doku SAĞLAMAZ. Bu
modül onun `_convert_point_elements` (kanıtlanmış, test edilmiş Facility/Unit
dönüşüm mantığı) fonksiyonunu yeniden kullanır; buna EK OLARAK çok daha geniş
bir highway-tipi kümesi (bkz. `_SOKAK_HIGHWAY_TIPLERI`) sorgulanır ve HER
segmentin TÜM ara düğüm koordinatları (sadece uç noktaları değil) ayrı ayrı
`Street` (veya `bridge=yes` etiketliyse `Bridge`) düğümleri olarak üretilir.

Neo4j'e yazma: `TucbsETLLoader.bulk_insert_nodes` (UNWIND-batch) DOĞRUDAN
kullanılır (bkz. `RealOsmLoader.load` / `run_real_data.py`) — `build_nodes`
bir GENERATOR'dır, tüm düğümler tek seferde belleğe alınmaz.

DÜZELTME NOTU ("0 Kayıtlı Altyapı" / sessiz hata sorunu): Sorgu çalıştırma
KENDİ `_run_overpass_query`'sini kullanır: (1) HTTP-200 dönüp içinde bir
`remark` (timeout/hata) taşıyan yanıtları AÇIKÇA hata sayar, (2) TÜM aynalar
0 sonuç/hata döndürürse `RuntimeError` FIRLATIR (asla sessizce geçmez),
(3) her adımda kaç ham eleman alındığını loglar.

DÜZELTME NOTU (406 Not Acceptable / bot engeli): Bazı Overpass aynaları
(özellikle overpass-api.de), `requests`in varsayılan User-Agent'ini bot
sanıp 406 ile REDDEDER. TÜM Overpass istekleri, gerçek/tanımlanabilir bir
istemci kimliği taşıyan `osm_loader.OVERPASS_REQUEST_HEADERS` başlığıyla
gönderilir.

FAZ 3 DÜZELTME NOTU (Nominatim bbox'ı TERK EDİLDİ — İDARİ ALAN sorgusuna
geçildi): Önceki sürüm, bir şehir adını OpenStreetMap'in Nominatim geocoding
servisiyle bir DİKDÖRTGEN bounding box'a çeviriyordu. Bu YANLIŞ sonuçlar
verdi: Nominatim bazen şehrin GERÇEK merkezini KAÇIRAN, sadece kırsal
çevresini kapsayan bir kutu döndürebiliyordu (bir dikdörtgen, doğası gereği
GERÇEK, düzensiz şehir/il sınırının yaklaşık bir tahminidir — "merkezi
atlama" riski bundan kaynaklanır). Bu modül artık HİÇBİR bbox/geocoding
KULLANMAZ: Overpass QL'in KENDİ `area["name"="..."]["boundary"="administrative"]`
sorgusu kullanılır — bu, OSM'in GERÇEK, kesin idari sınır POLİGONUNU sorgular;
bir dikdörtgen YAKLAŞIKLIĞI DEĞİLDİR, bu yüzden şehrin merkezi ASLA
"kutunun dışında kalıp" atlanamaz. Bu aynı zamanda `_sehir_bbox_al`/
`_bbox_boyutunu_sinirla` gibi TÜM Nominatim-özel kodu (ayrı bir dış servise
bağımlılık, ayrı bir rate-limit, ayrı bir hata sınıfı) TAMAMEN ORTADAN
KALDIRIR — tek dış bağımlılık yine Overpass API'nin kendisidir.

FAZ 3 NOTU (ÇOKLU ŞEHİR): `RealOsmLoader(sehirler=["Elazığ", "Malatya",
"Diyarbakır"])` gibi bir İL/ŞEHİR ADI LİSTESİ kabul eder; her biri KENDİ
İDARİ ALAN sorgusuyla sırayla çekilip Neo4j'e yazılır. Çoklu-şehir modunda,
ardışık şehir sorguları arasına nezaket beklemesi (`_SEHIRLER_ARASI_BEKLEME_SANIYE`)
eklenir. `_run_overpass_query`, 429 (Too Many Requests) yanıtlarında ARTAN
bir bekleme (backoff) uygulayıp yeniden dener; TÜM aynalar bir turda
başarısız olursa, birkaç TAM ayna-turu daha (yine artan beklemeyle) dener.
Her düğüme, hangi il/şehirden geldiği (`BaseNode.bolge`) yazılır — bu SALT
bir provenance/köken bilgisidir (bkz. `src.ui.app` — arayüz artık TÜM
bölgeleri AYNI ANDA, herhangi bir filtre OLMADAN gösterir; `bolge` yalnızca
ileride gerekebilecek analiz/hata ayıklama için düğümde SAKLANIR).

DÜZELTME NOTU (kesin `["name"="Elazığ"]` eşleşmesi 0 SONUÇ döndürüyordu):
İlk `area["name"="..."]` denemesi "Elazığ" için TAMAMEN BOŞ döndü — canlı
Overpass verisinde araştırıldığında kök neden bulundu: OSM'deki Elazığ ili
sınır poligonunun ANA `name` etiketi eski-usul sirkumfleksli **"Elâzığ"**
imlasını taşıyor (`name:tr` etiketi "Elazığ" olsa da Overpass `area["name"=...]`
SADECE ana `name` etiketine bakar). Tek bir şehir için özel durum eklemek
YERİNE, `_sehir_adi_regex` TÜM şehir adları için genel bir çözüm uygular:
"a"/"i"/"u" harflerini OSM'de bazen karşılaşılan sirkumfleksli karşılıklarıyla
(â/î/û) birlikte kabul eden, büyük/küçük harf duyarsız bir Overpass regex'i
üretir (`["name"~"^El[aâ]zığ$",i]` gibi) — böylece hem "Elazığ" hem "Elâzığ"
(ve başka bir ilin benzer bir imla farkı) SORUNSUZ eşleşir.

Çalıştırmak için (Neo4j'i TAMAMEN sıfırlayıp gerçek veriyle yeniden dolduran
sarmalayıcı script için bkz. `run_real_data.py`):
    python -m src.data_ingestion.real_osm_loader
    python -m src.data_ingestion.real_osm_loader Elazığ Malatya Diyarbakır
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests

from src.core.database import Neo4jConnection
from src.core.models import (
    BaseNode,
    Infrastructure,
    InfrastructureType,
    OperationalStatus,
)
from src.data_ingestion.osm_loader import (
    OVERPASS_ENDPOINTS,
    OVERPASS_REQUEST_HEADERS,
    OSMBaselineLoader,
    _geometri_uzunlugu_km,
    _tahmini_sayisal_deger,
)
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

logger = logging.getLogger(__name__)


class OverpassQueryError(RuntimeError):
    """Overpass sunucusu HTTP-200 ile yanıt verdi AMA yanıt bir hata/timeout
    `remark`i taşıyordu (bkz. modül docstring'indeki "DÜZELTME NOTU")."""


OSM_TEKNIK_KIMLIK_IMZASI = "(OSM "
"""`build_nodes`in, GERÇEK bir OSM adı OLMAYAN (isimsiz segment) veya
benzersizlik için sentetik bir sonek TAŞIYAN `isim` alanlarına eklediği
imzanın BAŞLANGICI (ör. "Sokak Dugumu (OSM way/123 #4)", "Devlet Hastanesi
(OSM node/987654321)"). `decision_engine`, GraphRAG bağlamına/AI Kurmay
Başkanlığı önerilerine bu türden HAM teknik kimliklerin sızmasını önlemek
için, bu imzayı taşıyan adayları aday havuzundan DIŞLAR — tek bir
kaynaktan (burada) içe aktarılır.

"İSİM ÇAKIŞMASI" DÜZELTMESİ (kanıtlanmış bir boşluk için savunma:
Türkiye'deki TÜM "Devlet Hastanesi"/"İtfaiye Merkezi" düğümlerinin
`isim`-bazlı MERGE üzerinden TEK bir düğüme birleşme riski): eskiden bu
imza SADECE `"(OSM way/"` idi (way ID'si
İÇERECEK şekilde SABİT) — Sokak/Köprü HER ZAMAN bir OSM "way"den geldiği
için bu yeterliydi. Facility/Unit (hastane/karakol/itfaiye) ise OSM'de hem
"node" HEM DE "way" (poligon) olarak temsil edilebilir; imza artık HER
İKİSİNİ de (`"(OSM node/..."` VE `"(OSM way/..."`) tek bir ortak önekle
("(OSM ") yakalayacak şekilde genelleştirildi. Bu değişiklik, imzayı
İNŞA EDEN her çağrı yerinin (bkz. `real_osm_loader.build_nodes`,
`local_osm_reader._OsmStreamHandler`) artık "way/"/"node/" kelimesini
KENDİSİNİN açıkça eklemesini GEREKTİRİR — imza artık bunu KENDİLİĞİNDEN
sağlamaz."""


# ---------------------------------------------------------------------------
# Overpass zaman asimi / anti-ban (retry-backoff) sabitleri
# ---------------------------------------------------------------------------

# Overpass QL sorgu-içi zaman aşımı ([timeout:N]) — sunucu tarafı. İDARİ ALAN
# sorguları (bkz. modül docstring'i — Faz 3) bir bbox'tan ÇOK DAHA FAZLA veri
# döndürebilir (tüm il); bu yüzden eski (60s) değerden daha cömert tutulur —
# "eksiksiz veri çekimi" hız kaygısından ÖNCELİKLİDİR.
_OVERPASS_ZAMAN_ASIMI_SANIYE = 120
# HTTP istemci zaman aşımı — sunucu-tarafı zaman aşımından (yukarıdaki) HER
# ZAMAN BÜYÜK tutulur (burada +60 sn tampon): aksi halde `requests` sunucu
# kendi süresini doldurup düzgün bir `remark` yanıtı vermeden ÖNCE bağlantıyı
# keser ve gerçek nedeni (timeout) bir bağlantı hatası maskeler.
_REQUEST_TIMEOUT_SANIYE = _OVERPASS_ZAMAN_ASIMI_SANIYE + 60

# ANTI-BAN / RETRY-BACKOFF: tek bir 429 (Too Many Requests) ya da gecici bir
# sunucu dalgalanmasinda hemen pes ETMEMEK icin.
_MAX_DONGU_DENEMESI = 3
"""TÜM aynalar (mirror) bir turda başarısız olursa, kaç TAM tur daha
denenir (artan beklemeyle, bkz. `_DONGU_ARASI_BEKLEME_SANIYE`)."""
_DONGU_ARASI_BEKLEME_SANIYE = 5.0
"""Bir TAM ayna-turu (tüm mirror'lar) başarısız olduğunda, bir SONRAKİ tur
öncesi beklenecek TABAN süre; deneme sayısıyla ÇARPILARAK artar (5s, 10s, ...)."""
_RATE_LIMIT_BEKLEME_SANIYE = 8.0
"""Bir ayna 429 (Too Many Requests) döndürdüğünde, sıradaki aynaya geçmeden
ÖNCE beklenecek süre — bu sunucuyu daha da öfkelendirmemek içindir."""
_SEHIRLER_ARASI_BEKLEME_SANIYE = 3.0
"""Çoklu-şehir modunda, ardışık iki şehrin sorguları arasına eklenen nezaket
beklemesi — Overpass'ı arka arkaya ağır sorgularla BOMBALAMAMAK için."""

# Gerçek şehir "damarları" görünümü için dahil edilen highway tipleri.
# `footway`/`cycleway`/`path`/`service` BİLİNÇLİ OLARAK dışarıda bırakıldı:
# İDARİ ALAN (il) ölçeğinde bunlar veri hacmini (ve Overpass sorgu süresini)
# ÇOK artırır ama görsel olarak asıl "araç yolu ağı" dokusuna fazla katkı
# sağlamaz.
_SOKAK_HIGHWAY_TIPLERI: Tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)

# FAZ 5 (harita LOD/hiyerarşi filtresi): `_SOKAK_HIGHWAY_TIPLERI`nin ALT
# KÜMESİ — şehrin "ana damarları" sayılan, harita genel bakış/performans
# modunda tek başına çizilecek highway tipleri (bkz. `Infrastructure.
# highway_tipi` ve `src.ui.app.fetch_street_points`). `tertiary`/
# `unclassified`/`residential`/`living_street` bu kümenin DIŞINDA bırakılır
# — bir şehrin OSM yol ağının BÜYÜK ÇOĞUNLUĞU (genelde %70-80'i) bu "ara
# sokak" tiplerindendir; bunları genel bakışta gizlemek hem GPU/render
# yükünü hem de "toz bulutu" görsel karmaşasını asıl azaltan adımdır.
ANA_DAMAR_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary", "secondary")

_VARSAYILAN_SEHIRLER: Tuple[str, ...] = ("Elazığ",)

# OSM'de bir il/şehrin idari sınır poligonunun ANA `name` etiketi, bazen
# modern imla YERİNE eski-usul sirkumfleksli bir varyant taşıyabilir (ör.
# Elazığ ili OSM'de "Elâzığ" olarak etiketli — bkz. modül docstring'indeki
# "DÜZELTME NOTU"). Bu eşleme, `_sehir_adi_regex`in "a"/"i"/"u" harflerini
# sirkumfleksli karşılıklarıyla (â/î/û) BİRLİKTE kabul eden bir Overpass
# regex'i üretmesi için kullanılır.
_SIRKUMFLEKS_ESLEMESI: Dict[str, str] = {"a": "[aâ]", "i": "[iî]", "u": "[uû]"}


def _sehir_adi_regex(sehir_adi: str) -> str:
    """Bir il/şehir adını, OSM'deki olası eski-usul sirkumfleks imla
    farklarına TOLERANSLI bir Overpass regex desenine çevirir (bkz.
    `_SIRKUMFLEKS_ESLEMESI` ve modül docstring'indeki "DÜZELTME NOTU").
    Sirkumfleks içermeyen normal şehir adları (ör. "Malatya") için bu,
    sıradan (ama harf-duyarsız) bir TAM eşleşme deseninden farksızdır —
    yani bu fonksiyon SADECE sirkumfleks riski taşıyan şehirler için değil,
    TÜM şehir adları için güvenle kullanılabilir."""
    parcalar = [_SIRKUMFLEKS_ESLEMESI.get(harf.lower(), re.escape(harf)) for harf in sehir_adi]
    return "^" + "".join(parcalar) + "$"


# ---------------------------------------------------------------------------
# Overpass sorgu calistirma (remark/timeout + 429 retry-backoff kontrollu)
# ---------------------------------------------------------------------------


def _run_overpass_query(
    query: str,
    endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
    request_timeout: int = _REQUEST_TIMEOUT_SANIYE,
    max_dongu: int = _MAX_DONGU_DENEMESI,
) -> Dict[str, Any]:
    """Overpass QL sorgusunu tanımlı aynalar (mirror) üzerinde sırayla dener.

    `osm_loader.OSMBaselineLoader._run_overpass_query`'den KASITLI FARKI (bkz.
    modül docstring'indeki "DÜZELTME NOTU"): HTTP-200 dönüp içinde bir hata/
    timeout `remark`i taşıyan yanıtlar da AÇIKÇA başarısız sayılır.

    ANTI-BAN / RETRY-BACKOFF: Bir ayna 429 (Too Many Requests) döndürürse,
    sıradaki aynaya geçmeden önce `_RATE_LIMIT_BEKLEME_SANIYE` kadar
    beklenir. TÜM aynalar bir turda başarısız olursa, pes etmeden ÖNCE
    `max_dongu` kadar TAM tur daha denenir — her tur arasında ARTAN bir
    bekleme uygulanır. Sadece TÜM turlar tükendiğinde `RuntimeError`
    FIRLATILIR — asla sessizce boş bir sonuçla devam edilmez.
    """
    son_hata: Optional[Exception] = None
    for dongu in range(1, max_dongu + 1):
        for endpoint in endpoints:
            try:
                yanit = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=request_timeout,
                    headers=OVERPASS_REQUEST_HEADERS,
                )
                if yanit.status_code == 429:
                    logger.warning(
                        "Overpass 429 Too Many Requests (%s) - %.0f sn bekleyip devam edilecek...",
                        endpoint, _RATE_LIMIT_BEKLEME_SANIYE,
                    )
                    son_hata = OverpassQueryError(f"429 Too Many Requests ({endpoint})")
                    time.sleep(_RATE_LIMIT_BEKLEME_SANIYE)
                    continue
                yanit.raise_for_status()
                veri = yanit.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Overpass sorgusu basarisiz oldu (%s): %s", endpoint, exc)
                son_hata = exc
                continue

            remark = veri.get("remark")
            if remark:
                logger.warning(
                    "Overpass sunucusu bir hata/timeout remark'i dondurdu (%s): %s", endpoint, remark
                )
                son_hata = OverpassQueryError(remark)
                continue

            eleman_sayisi = len(veri.get("elements", []))
            logger.info("Overpass sorgusu basarili (%s): %d eleman alindi.", endpoint, eleman_sayisi)
            return veri

        if dongu < max_dongu:
            bekleme = _DONGU_ARASI_BEKLEME_SANIYE * dongu
            logger.warning(
                "Tum Overpass aynalari basarisiz oldu (deneme %d/%d) - %.0f sn sonra tekrar denenecek...",
                dongu, max_dongu, bekleme,
            )
            time.sleep(bekleme)

    raise RuntimeError(
        f"Hicbir Overpass API ucuna ulasilamadi/gecerli veri alinamadi "
        f"({max_dongu} tam ayna-turu denendi; aynalar: {list(endpoints)}): {son_hata}"
    ) from son_hata


class RealOsmLoader:
    """OpenStreetMap Overpass API'sinden bir veya BİRDEN FAZLA il/şehrin
    GERÇEK İDARİ ALAN sınırı (`area["name"="..."]`, bbox YAKLAŞIKLIĞI
    DEĞİL) içindeki sokak/köprü ağını ve kritik tesisleri çekip,
    `TucbsETLLoader` ile Neo4j Bilgi Grafı'na `Street`/`Bridge`/`Facility`/
    `Unit` etiketleriyle yükleyen yükleyici.

    Kullanım (varsayılan: Elazığ):
        loader = RealOsmLoader(Neo4jConnection())
        sayaclar = loader.load()   # {"Facility": 6, "Unit": 4, "Infrastructure": 41230}

    Kullanım (çoklu il/şehir):
        loader = RealOsmLoader(Neo4jConnection(), sehirler=["Elazığ", "Malatya"])
        sayaclar = loader.load()   # her il/sehir KENDI idari alan sorgusuyla sirayla yuklenir
    """

    def __init__(
        self,
        db: Optional[Neo4jConnection] = None,
        sehirler: Optional[Sequence[str]] = None,
        overpass_endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
        request_timeout: int = _REQUEST_TIMEOUT_SANIYE,
    ) -> None:
        self.db = db or Neo4jConnection()
        self.sehirler: List[str] = list(sehirler) if sehirler else list(_VARSAYILAN_SEHIRLER)
        self._endpoints = list(overpass_endpoints)
        self._request_timeout = request_timeout

    # ------------------------------------------------------------------ #
    # Overpass sorgu insasi — İDARİ ALAN (area) tabanlı, bbox YOK
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_point_query(alan_adi: str) -> str:
        """Hastane/Askeri Alan/İtfaiye/Polis (nokta-tipi) sorgusu — TAM
        OLARAK `osm_loader.OSMBaselineLoader._build_point_query` ile AYNI
        etiket kümesini sorgular (tutarlılık için), ama BBOX yerine
        `area["name"=...]` kullanır; bu yüzden metni burada AYRICA
        yazılmıştır (dönüşüm mantığı `OSMBaselineLoader._convert_point_elements`
        ile YİNE DE yeniden kullanılır — bkz. `_donustur_ve_yaz`)."""
        desen = _sehir_adi_regex(alan_adi)
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
area["name"~"{desen}",i]["boundary"="administrative"]->.searchArea;
(
  node["amenity"="hospital"](area.searchArea);
  way["amenity"="hospital"](area.searchArea);
  node["amenity"="fire_station"](area.searchArea);
  way["amenity"="fire_station"](area.searchArea);
  node["amenity"="police"](area.searchArea);
  way["amenity"="police"](area.searchArea);
  node["landuse"="military"](area.searchArea);
  way["landuse"="military"](area.searchArea);
  node["amenity"="military"](area.searchArea);
  way["amenity"="military"](area.searchArea);
  node["aeroway"="aerodrome"](area.searchArea);
  way["aeroway"="aerodrome"](area.searchArea);
  node["landuse"="port"](area.searchArea);
  way["landuse"="port"](area.searchArea);
  node["industrial"="port"](area.searchArea);
  way["industrial"="port"](area.searchArea);
  node["harbour"="yes"](area.searchArea);
  way["harbour"="yes"](area.searchArea);
  node["power"="plant"](area.searchArea);
  way["power"="plant"](area.searchArea);
  node["power"="substation"](area.searchArea);
  way["power"="substation"](area.searchArea);
  node["waterway"="dam"](area.searchArea);
  way["waterway"="dam"](area.searchArea);
  node["man_made"="communications_tower"](area.searchArea);
  way["man_made"="communications_tower"](area.searchArea);
  node["amenity"="fuel"](area.searchArea);
  way["amenity"="fuel"](area.searchArea);
);
out center tags;
"""

    @staticmethod
    def _build_street_query(alan_adi: str) -> str:
        hw_regex = "|".join(_SOKAK_HIGHWAY_TIPLERI)
        desen = _sehir_adi_regex(alan_adi)
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
area["name"~"{desen}",i]["boundary"="administrative"]->.searchArea;
(
  way["highway"~"^({hw_regex})$"](area.searchArea);
);
out geom tags;
"""

    def _fetch_raw_elements_for(self, alan_adi: str) -> Dict[str, List[Dict[str, Any]]]:
        """Tek bir il/şehir (idari alan adı) için Overpass'a İKİ sorgu
        gönderir: nokta-tipi tesisler ve gerçek sokak/köprü ağı (tam
        geometriyle, `out geom tags;`). İKİSİ DE 0 eleman döndürürse
        (sessiz "0 kayıtlı altyapı" hatasını ÖNLEMEK için) açıkça
        `RuntimeError` fırlatılır — olası neden genelde YANLIŞ YAZILMIŞ bir
        il/şehir adıdır (OSM'de `name="..."` etiketiyle BİREBİR eşleşmeli).
        """
        logger.info("[%s] Overpass: tesis/nokta sorgusu (idari alan) gonderiliyor...", alan_adi)
        nokta_elemanlari = _run_overpass_query(
            self._build_point_query(alan_adi), self._endpoints, self._request_timeout
        )["elements"]
        logger.info("[%s] Overpass: %d nokta (tesis) elemani alindi.", alan_adi, len(nokta_elemanlari))

        logger.info(
            "[%s] Overpass: sokak/kopru sorgusu (idari alan) gonderiliyor (highway tipleri=%s)...",
            alan_adi, _SOKAK_HIGHWAY_TIPLERI,
        )
        yol_elemanlari = _run_overpass_query(
            self._build_street_query(alan_adi), self._endpoints, self._request_timeout
        )["elements"]
        logger.info("[%s] Overpass: %d yol (way) elemani alindi.", alan_adi, len(yol_elemanlari))

        if not nokta_elemanlari and not yol_elemanlari:
            raise RuntimeError(
                f"[{alan_adi}] Overpass API'den HICBIR eleman donmedi (0 nokta + 0 yol). Olasi "
                "nedenler: bu isimde (harf-duyarsiz, sirkumfleks-toleransli aramaya ragmen) bir "
                "idari alan OSM'de bulunamadi -- il/ilce adini kontrol edin, tum Overpass "
                "aynalari basarisiz oldu, veya sunucu(lar) sorguyu zaman asimina ugratti. "
                "Yukaridaki loglara bakin."
            )

        return {"noktalar": nokta_elemanlari, "yollar": yol_elemanlari}

    # ------------------------------------------------------------------ #
    # Overpass elemanlari -> Pydantic dugum akisi (generator)
    # ------------------------------------------------------------------ #

    def build_nodes(
        self,
        sayac_cikti: Optional[Dict[str, int]] = None,
        basarisiz_bolgeler_cikti: Optional[List[Tuple[str, str]]] = None,
    ) -> Iterator[BaseNode]:
        """Overpass'tan çekilen ham elemanları `src.core.models` düğümlerine
        çeviren bir GENERATOR döner (bellek disiplini: `TucbsETLLoader.
        bulk_insert_nodes` bunu tüketirken tek seferde belleğe almaz).

        `self.sehirler`deki HER il/şehir sırayla, KENDİ İDARİ ALAN sorgusuyla
        işlenir; ardışık şehirler arasına `_SEHIRLER_ARASI_BEKLEME_SANIYE`
        kadar nezaket beklemesi eklenir. Her düğüme, hangi ilden/şehirden
        geldiği `bolge` alanına yazılır (bkz. `models.BaseNode.bolge`) —
        BU SALT bir provenance/köken bilgisidir; arayüz (`src.ui.app`) TÜM
        bölgeleri AYNI ANDA, filtresiz gösterir.

        BÖLGESEL HATA İZOLASYONU (ÜLKE ÇAPINDA — 81 il ölçeği): 81 ilin
        TAMAMI tek bir çalıştırmada çekilirken, TEK bir ilin Overpass
        sorgusu (geçici sunucu yükü, ağ dalgalanması vb.) başarısız
        olabilir. Eskiden bu, `_fetch_raw_elements_for`'un fırlattığı
        `RuntimeError`'ın generator'ı TAMAMEN DURDURMASINA — yani o ana
        kadar BAŞARIYLA işlenmiş TÜM diğer illerin de kaybedilmesine (script
        çöker, hiçbir şey yazılmamış gibi görünür) yol açardı. Artık HER
        ilin hatası ayrı ayrı YAKALANIR, loglanır ve (varsa)
        `basarisiz_bolgeler_cikti`ye eklenir; akış BİR SONRAKİ ile DEVAM
        EDER — 80 ilin başarılı olması, 1 ilin geçici bir hatası yüzünden
        FEDA EDİLMEZ.

        `sayac_cikti` verilirse (boş bir dict), üretim TAMAMLANDIKTAN SONRA
        TÜM illerin TOPLAM alt-tip sayaçlarıyla doldurulur:
        {"facility": N, "unit": N, "sokak": N, "kopru": N}.
        `basarisiz_bolgeler_cikti` verilirse (boş bir liste), başarısız olan
        HER il için `(il_adi, hata_mesaji)` çiftiyle doldurulur.

        Sokak düğümleri: her yol segmentinin (way) TÜM ara koordinat
        noktaları (sadece uç noktaları DEĞİL) ayrı birer nokta-`Street`
        düğümü olarak üretilir — bu, haritada gerçek yol ağının yoğun bir
        nokta dokusu ("şehir damarları") olarak görülebilmesini sağlayan asıl
        karardır (bkz. modül docstring'i).

        Köprü düğümleri: `bridge` etiketi "no"/"false"/"0"/boş DIŞINDA bir
        değer taşıyan segmentler, tek bir uçtan-uca `Bridge` güzergahı olarak
        (gerçek toplam uzunluğuyla, bkz. `_geometri_uzunlugu_km`) üretilir.
        """
        toplam_facility = toplam_unit = toplam_sokak = toplam_kopru = 0
        toplam_enerji = toplam_iletisim = toplam_kaynak = 0

        for idx, alan_adi in enumerate(self.sehirler):
            if idx > 0:
                logger.info(
                    "Sehirler arasi nezaket beklemesi (%s -> siradaki): %.0f sn...",
                    alan_adi, _SEHIRLER_ARASI_BEKLEME_SANIYE,
                )
                time.sleep(_SEHIRLER_ARASI_BEKLEME_SANIYE)

            logger.info("=== Bolge isleniyor (idari alan): %s ===", alan_adi)
            try:
                ham = self._fetch_raw_elements_for(alan_adi)
            except RuntimeError as exc:
                logger.error("[%s] BOLGE ATLANDI (hata cekildi, diger iller ETKILENMEZ): %s", alan_adi, exc)
                if basarisiz_bolgeler_cikti is not None:
                    basarisiz_bolgeler_cikti.append((alan_adi, str(exc)))
                continue
            facilities, units, energiler, iletisimler, kaynaklar = OSMBaselineLoader._convert_point_elements(
                ham["noktalar"]
            )
            for facility in facilities:
                facility.bolge = alan_adi
                yield facility
            for unit in units:
                unit.bolge = alan_adi
                yield unit
            # "VERİTABANI GENİŞLETME — İKİNCİ DALGA" (bkz. `osm_loader.
            # OSMBaselineLoader._convert_point_elements` docstring'i):
            # enerji/iletişim/kaynak kategorileri de AYNI il-bazlı `bolge`
            # etiketiyle, facility/unit ile TUTARLI şekilde yield edilir.
            for enerji in energiler:
                enerji.bolge = alan_adi
                yield enerji
            for iletisim in iletisimler:
                iletisim.bolge = alan_adi
                yield iletisim
            for kaynak in kaynaklar:
                kaynak.bolge = alan_adi
                yield kaynak
            toplam_facility += len(facilities)
            toplam_unit += len(units)
            toplam_enerji += len(energiler)
            toplam_iletisim += len(iletisimler)
            toplam_kaynak += len(kaynaklar)

            for way in ham["yollar"]:
                geometry = way.get("geometry")
                if not geometry or len(geometry) < 2:
                    continue
                tags = way.get("tags", {})
                way_id = way.get("id", "?")
                bridge_etiketi = str(tags.get("bridge", "")).strip().lower()
                gercek_ad = tags.get("name")  # OSM'deki TEMIZ/gercek cadde-sokak-kopru ismi (varsa).

                if bridge_etiketi not in ("", "no", "false", "0"):
                    toplam_kopru += 1
                    uzunluk_km = round(_geometri_uzunlugu_km(geometry), 3)
                    temel_isim = gercek_ad or "Kopru"
                    yield Infrastructure(
                        isim=f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}way/{way_id})",
                        # `aciklama`: GERCEK/temiz isim (varsa) - `isim`deki OSM
                        # sonekinden ARINDIRILMIS (bkz. `database.update_infrastructure_status_by_name`).
                        aciklama=gercek_ad,
                        bolge=alan_adi,
                        enlem=geometry[0]["lat"],
                        boylam=geometry[0]["lon"],
                        bitis_enlem=geometry[-1]["lat"],
                        bitis_boylam=geometry[-1]["lon"],
                        durum=OperationalStatus.AKTIF,
                        infrastructure_type=InfrastructureType.KOPRU,
                        uzunluk_km=uzunluk_km,
                        acik_mi=True,
                        tonaj_kapasitesi=_tahmini_sayisal_deger(tags, ("maxweight",), 40.0),
                        serit_sayisi=int(_tahmini_sayisal_deger(tags, ("lanes",), 2)),
                        highway_tipi=tags.get("highway"),
                    )
                    continue

                # KRITIK (Sehir Ici Kriz Tepkisizligi duzeltmesi): `isim` YINE
                # DE (way_id, idx) cifti icerir (benzersizlik/stabil MERGE
                # icin) AMA artik -varsa- GERCEK cadde/sokak ismini de ON EKI
                # olarak tasir; `aciklama` ise SADECE temiz ismi tasir.
                isim_on_eki = gercek_ad or "Sokak Dugumu"
                for idx2, nokta in enumerate(geometry):
                    lat, lon = nokta.get("lat"), nokta.get("lon")
                    if lat is None or lon is None:
                        continue
                    toplam_sokak += 1
                    yield Infrastructure(
                        isim=f"{isim_on_eki} {OSM_TEKNIK_KIMLIK_IMZASI}way/{way_id} #{idx2})",
                        aciklama=gercek_ad,
                        bolge=alan_adi,
                        enlem=lat,
                        boylam=lon,
                        durum=OperationalStatus.AKTIF,
                        infrastructure_type=InfrastructureType.SOKAK,
                        uzunluk_km=0.0,
                        acik_mi=True,
                        highway_tipi=tags.get("highway"),
                    )

        if sayac_cikti is not None:
            sayac_cikti["facility"] = toplam_facility
            sayac_cikti["unit"] = toplam_unit
            sayac_cikti["sokak"] = toplam_sokak
            sayac_cikti["kopru"] = toplam_kopru
            sayac_cikti["energy_infrastructure"] = toplam_enerji
            sayac_cikti["communication_network"] = toplam_iletisim
            sayac_cikti["resource_hub"] = toplam_kaynak

    # ------------------------------------------------------------------ #
    # Genel kullanim (public API)
    # ------------------------------------------------------------------ #

    def load(self, basarisiz_bolgeler_cikti: Optional[List[Tuple[str, str]]] = None) -> Dict[str, int]:
        """`build_nodes` akışını `TucbsETLLoader.bulk_insert_nodes` (UNWIND-
        batch) ile Neo4j'e yazar. Dönüş: kategori etiketi (ör.
        "Infrastructure", "Facility", "Unit") -> yazılan düğüm sayısı.

        `basarisiz_bolgeler_cikti` verilirse (bkz. `build_nodes` — BÖLGESEL
        HATA İZOLASYONU), başarısız olan illerle `(il_adi, hata_mesaji)`
        çiftleriyle doldurulur; TÜM iller başarısız olmadıkça (bkz.
        `run_real_data.py`daki nihai kontrol) bu bir İSTİSNA fırlatmaz.
        """
        loader = TucbsETLLoader(self.db)
        return loader.bulk_insert_nodes(self.build_nodes(basarisiz_bolgeler_cikti=basarisiz_bolgeler_cikti))


def main() -> None:  # pragma: no cover - manuel/CLI calistirma amaclidir
    # Windows konsollari genellikle UTF-8 DEGIL, yerel bir kod sayfasi (ör.
    # Turkce Windows'ta cp1254) kullanir; `sys.stdout`u acikca UTF-8'e
    # yeniden yapilandirmak, ASCII-disi karakterler icin olasi bir
    # `UnicodeEncodeError` cokmesini onler.
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # `python -m src.data_ingestion.real_osm_loader Elazığ Malatya` gibi
    # komut satiri argumanlari verilirse COKLU-IL modu; verilmezse varsayilan
    # (Elazığ) idari alani.
    sehirler = sys.argv[1:] or None

    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()

    loader = RealOsmLoader(db, sehirler=sehirler)
    print(f"Overpass API'den {loader.sehirler} GERÇEK idari alan(lar)ı çekiliyor (bu birkaç dakika sürebilir)...")
    basarisiz_bolgeler: List[Tuple[str, str]] = []
    sayaclar = loader.load(basarisiz_bolgeler_cikti=basarisiz_bolgeler)

    toplam = sum(sayaclar.values())
    print("\nGerçek şehir topolojisi Bilgi Grafı'na yüklendi:")
    for kategori, adet in sayaclar.items():
        print(f"  - {kategori}: {adet}")

    if basarisiz_bolgeler:
        print(
            f"\n⚠️  {len(basarisiz_bolgeler)}/{len(loader.sehirler)} bölge BAŞARISIZ oldu "
            "(diğerleri etkilenmedi):"
        )
        for il_adi, hata in basarisiz_bolgeler:
            print(f"  - {il_adi}: {hata}")

    if toplam == 0:
        # Bu noktaya ulasilmasi TEORIK olarak beklenmez (fetch_raw_elements
        # zaten 0 eleman icin RuntimeError firlatir) ama savunmaci bir son
        # kontrol olarak: sessizce "basarili" gorunup hicbir dugum yazilmamis
        # olma ihtimaline karsi acikca hata verilir.
        raise RuntimeError("Yukleme 'basarili' tamamlandi ama 0 dugum yazildi — bu beklenmeyen bir durumdur.")


if __name__ == "__main__":
    main()
