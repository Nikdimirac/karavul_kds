"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
Neo4j veritabanı bağlantı katmanı.

`Neo4jConnection`, uygulama boyunca tek bir Neo4j Driver örneğinin
paylaşılmasını sağlayan bir Singleton sınıftır. Bağlantı bilgilerini
`.env` dosyasından okur, temel CRUD işlemlerini sunar ve `src.core.models`
içindeki Pydantic modellerini çalıştırılabilir Cypher sorgularına çevirir.

Gerekli .env değişkenleri:
    NEO4J_URI=bolt://localhost:7687
    NEO4J_USER=neo4j
    NEO4J_PASSWORD=changeme
    NEO4J_DATABASE=neo4j

Gerekli paketler:
    pip install neo4j pydantic python-dotenv
"""

from __future__ import annotations

import difflib
import heapq
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, Session, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from src.core.models import (
    NODE_REGISTRY,
    BaseNode,
    NodeLabel,
    Relationship,
    RelationshipType,
)

# "ULUSAL OLCEKLI ESNEKLIK" (Dinamik Geocoding
# Fallback): `geopy` OPSIYONEL bir bagimliliktir (bkz. requirements.txt'
# deki "Kapali devre (on-premise)" GERILIMI notu) — kurulu DEGILSE sistem
# ASLA cokmez, sadece `harici_geokodlama_ile_koordinat_bul` HER ZAMAN
# `None` doner (yerel-veri-tabanli davranisa sessizce geri duser).
try:
    from geopy.exc import GeocoderServiceError, GeocoderTimedOut
    from geopy.geocoders import Nominatim

    _GEOPY_MEVCUT = True
except ImportError:  # pragma: no cover - opsiyonel bagimlilik kurulu degilse
    _GEOPY_MEVCUT = False

logger = logging.getLogger(__name__)

load_dotenv()


class Neo4jConnectionError(RuntimeError):
    """Neo4j bağlantı/işlem hatalarını sarmalayan uygulama-özel hata sınıfı."""


# ---------------------------------------------------------------------------
# Bulanik (fuzzy) isim karsilastirma yardimcilari
# ---------------------------------------------------------------------------
# `isim` uzerinden MERGE (bkz. Neo4jConnection.node_to_cypher), LLM'in AYNI
# gercek-dunya varligina TAM AYNI metni uretmesine bagimlidir. Ancak LLM bazen
# ayni varlik icin hafifce farkli ifadeler uretebilir (ör. "Arama Kurtarma
# Eki" / "Arama Kurtarma Ekipleri"). Bu durumda TAM string esitligi yeterli
# olmuyor; asagidaki yardimcilar, yeni bir isim veritabanina yazilmadan once
# mevcut isimlerle "yeterince benzer" olup olmadigini kontrol edip, oyleyse
# mevcut (kanonik) ismi kullanarak ayni duguma MERGE edilmesini saglar.

_TURKISH_FOLD_MAP = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    }
)


def _fold_isim(value: str) -> str:
    """Bulanik karsilastirma icin sadelestirilmis anahtar uretir: Turkce
    karakterleri ASCII'ye cevirir, kucuk harfe indirger, fazla bosluklari
    sadelestirir. (bkz. `src.data_ingestion.nlp_parser.normalize_tr` — ayni
    amaca hizmet eder, ancak katman ayriminin bozulmamasi icin -core
    data_ingestion'a bagimli olmasin diye- burada kucuk/bagimsiz bir kopyasi
    tutulur.)
    """
    return " ".join(value.strip().translate(_TURKISH_FOLD_MAP).lower().split())


FUZZY_ISIM_MATCH_THRESHOLD = 0.85
"""Iki isim'in "ayni gercek-dunya varligi" sayilmasi icin gereken minimum
benzerlik orani (`difflib.SequenceMatcher.ratio()`, 0.0-1.0 araligi). LLM
ayni varligi farkli calistirmalarda hafifce farkli ifade edebilir; bu esik
boyle varyasyonlari ayni duguma MERGE etmek icin kullanilir. Cok dusuk
tutulursa alakasiz farkli varliklar yanlislikla birlestirilebilir (false
positive) — bu yuzden yuksek (>=0.85) tutulur."""


# ---------------------------------------------------------------------------
# Altyapi adi normalizasyonu (bkz. `update_infrastructure_status_by_name`)
# ---------------------------------------------------------------------------
# SORUN: Gercek OSM'den yuklenen (bkz. `real_osm_loader.RealOsmLoader`)
# Street/Bridge dugumleri, ayni cadde/koprunun ONLARCA/birden fazla ayri
# nokta-dugumu oldugundan, benzersizlik icin `isim` alaninda bir OSM way/
# index sonek TASIR (ör. "Vali Fahri Bey Caddesi (OSM way/123456789 #7)",
# "Kömürhan Köprüsü (OSM way/987654321)"); GERCEK/temiz isim ise `aciklama`
# alaninda ayrica tutulur. LLM'den gelen TEMIZ isim ("Kömürhan Köprüsü") ile
# bu UZUN bileşik `isim` string'i arasinda `FUZZY_ISIM_MATCH_THRESHOLD`
# (0.85) esigini GECEBILECEK bir SequenceMatcher orani elde etmek neredeyse
# imkansizdir (uzunluk farki oranı asagi ceker) — bu yuzden
# `add_node`nin genel `_resolve_canonical_isim` mekanizmasi "Kömürhan
# Köprüsü"yü GERCEK OSM köprü düğümüyle ESLESTIREMIYOR ve sahte/rastgele
# koordinatli bir "GHOST" Infrastructure kopyasi yaratiyordu. Bu yuzden
# OSM-kaynakli Infrastructure (Sokak + Kopru) eslestirmesi ayri, ADANMIS bir
# normalizasyon+eslestirme (bu fonksiyon + `update_infrastructure_status_
# by_name`) kullanir; genel `_resolve_canonical_isim` bunlar icin
# KULLANILMAZ.
#
# BİLİNÇLİ SINIR: "köprü"/"tünel"/"viyadük"/
# "karayolu" gibi YAPI TÜRÜ kelimeleri bu listeye EKLENMEZ — cadde/sokak/
# bulvarın aksine bunlar birbirinin YAKIN EŞ ANLAMLISI DEĞİLDİR, aynı
# konumdaki FARKLI (ayrı) yapıları birbirinden AYIRT EDER (ör. Fırat
# üzerinde hem "Kömürhan Köprüsü" HEM DE bitişiğindeki ayrı bir yapı olan
# "Kömürhan Tüneli" GERÇEKTEN VARDIR). Bu kelimeyi de atarsak ikisi de
# "komurhan" anahtarına düşer ve sistem YANLIŞ yapıyı (köprü yerine tüneli)
# günceller. Cadde/sokak/bulvar İSE aynı kavramın (şehir içi yol) yakın
# eşanlamlılarıdır; bunları atmak KOLLİZYON değil DOĞRU NORMALİZASYONDUR.
_ALTYAPI_TIP_KELIMELERI = frozenset(
    {"caddesi", "cadde", "cad", "sokak", "sokagi", "sok", "sk", "bulvari", "bulvar", "blv"}
)


def _altyapi_adi_anahtari(isim: str) -> str:
    """Bir altyapı (cadde/sokak/bulvar VE köprü/tünel/viyadük/karayolu gibi
    özel adlı diğer güzergahlar) ismini, Türkçe katlama + küçük harf + TÜM
    boşlukların silinmesiyle, karşılaştırmaya hazır sade bir anahtara
    indirger; SADECE cadde/sokak/bulvar EŞANLAMLI tip kelimeleri atılır
    (bkz. yukarıdaki "BİLİNÇLİ SINIR" notu — köprü/tünel/viyadük/karayolu
    KASITLI OLARAK ATILMAZ, aksi halde aynı konumdaki FARKLI yapı türleri
    birbirine karışır).

    Örnek: "Vali Fahri Bey Caddesi" VE kullanıcının yazdığı "valifahribey
    caddesi" (boşluk/büyük-küçük harf farklı) İKİSİ DE -> "valifahribey"
    anahtarına indirger (birebir eşleşir, difflib gerekmeden). "Kömürhan
    Köprüsü" -> "komurhankoprusu" (köprüsü KORUNUR, "Kömürhan Tüneli" ->
    "komurhantuneli" ile KARIŞMAZ).
    """
    katlanmis = _fold_isim(isim)
    kelimeler = [k for k in katlanmis.split() if k not in _ALTYAPI_TIP_KELIMELERI]
    return "".join(kelimeler)


FUZZY_ALTYAPI_ESIK = 0.75
"""`_altyapi_adi_anahtari` normalizasyonundan SONRA (tip kelimeleri/boşluklar
zaten atıldığı için) uygulanan, `FUZZY_ISIM_MATCH_THRESHOLD`'dan daha
TOLERANSLI bir eşik: cadde/sokak/köprü isimleri genelde kısadır, tek bir
harf/yazım farkı ham orani orantisiz düşürebilir; normalizasyon zaten en
büyük gürültü kaynağını (tip kelimesi/boşluk) elediğinden daha düşük bir
eşik güvenlidir."""


# ---------------------------------------------------------------------------
# MEKANSAL INDEKSLEME (Faz 3: ulusal ölçekli mimari)
# ---------------------------------------------------------------------------
# Milyonlarca düğümlü ulusal ölçekte, GraphRAG'in "olay yerinin X km
# yarıçapındaki tüm açık güzergahları/sağlam tesisleri bul" sorguları (bkz.
# `decision_engine.py`), TÜM Türkiye'yi tarayan bir `sqrt(Δenlem²+Δboylam²)`
# ile ASLA ölçeklenmez (tam graf taraması + sıralama). Neo4j'nin YERLİ
# `Point` (WGS-84) tipi VE üzerine kurulan bir POINT INDEX, `point.distance`
# ile YARIÇAP tabanlı bir `WHERE` filtresinin (bkz. `decision_engine`daki
# sorgular) indeks kullanarak milisaniyeler içinde çalışmasını sağlar.
#
# BİLİNÇLİ KARAR: `enlem`/`boylam` skaler alanları KALDIRILMAZ — TÜM mevcut
# sorgular (PyDeck besleme, mevcut mesafe hesapları) bunlara bağlıdır ve
# `Point` nesnesini açıp (`.latitude`/`.longitude`) her okuma noktasında
# yeniden yazmak gereksiz risk/kapsam getirirdi. Bunun yerine `konum` adında
# TÜRETİLMİŞ bir `Point` alanı EK OLARAK yazılır (bkz. `node_to_cypher` /
# `tucbs_etl_loader._dugum_grubunu_yaz`); tek doğruluk kaynağı hâlâ
# enlem/boylam'dır, `konum` bundan yazma anında hesaplanır.
def _konum_point_ifadesi(props_ifadesi: str) -> str:
    """Bir Cypher parametre-harita ifadesinden (ör. `"$props"` veya
    `"row.props"`) o haritanın `enlem`/`boylam` alanlarını okuyup WGS-84
    `Point` üreten bir Cypher alt-ifadesi döner. Tek bir yerden üretilir ki
    `node_to_cypher` (tekil yazma) ve `tucbs_etl_loader._dugum_grubunu_yaz`
    (UNWIND-batch yazma) AYNI mantığı birbirinden bağımsız kopyalamasın."""
    return f"point({{latitude: {props_ifadesi}.enlem, longitude: {props_ifadesi}.boylam, crs: 'wgs-84'}})"


class Neo4jConnection:
    """Neo4j veritabanına tekil (Singleton) bağlantıyı yöneten sınıf.

    Kullanım:
        db = Neo4jConnection()          # her yerde ayni instance dondurulur
        db.add_node(facility_instance)
        db.add_relationship(rel_instance)

        # veya context manager olarak:
        with Neo4jConnection() as db:
            db.execute_query("MATCH (n) RETURN count(n) AS adet")
    """

    _instance: Optional["Neo4jConnection"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "Neo4jConnection":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:  # double-checked locking
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        # Singleton zaten kurulduysa yeniden başlatma (parametreler yok sayılır).
        if getattr(self, "_initialized", False):
            return

        self._uri: str = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self._user: str = user or os.getenv("NEO4J_USER", "neo4j")
        self._password: str = password or os.getenv("NEO4J_PASSWORD", "")
        self._database: str = database or os.getenv("NEO4J_DATABASE", "neo4j")

        if not self._password:
            logger.warning(
                "NEO4J_PASSWORD .env dosyasinda tanimli degil; "
                "bos sifre ile baglanti denenecek."
            )

        self._driver: Optional[Driver] = None

        # YOL AGI ONBELLEGI (bkz. `_yerel_yol_agi_ve_kaynak_getir`): "Yetenek
        # Bazli Rota" (Capability-Based Routing) geregi
        # `decision_engine.py` AYNI olay yeri icin ARDI ARDINA 2 ayri arama
        # yapar (once dogru yetenekli birim, sonra AYRI bir sorguyla
        # guvenlik/Emniyet birligi) — pahali yerel graf insasini (6-8 sn)
        # HER SEFERINDE TEKRARLAMAMAK icin KISA sureli (30 sn) onbelleklenir.
        self._yol_agi_onbellek: Dict[Tuple[float, float, float], Any] = {}
        self._yol_agi_onbellek_zamani: Dict[Tuple[float, float, float], float] = {}

        # "ULUSAL OLCEKLI ESNEKLIK" — HARICI GEOKODLAMA (bkz. `harici_
        # geokodlama_ile_koordinat_bul`): geolocator TEK SEFER kurulur
        # (yeniden kullanilir); `_geokodlama_onbellek` AYNI yer adi icin
        # tekrar tekrar Nominatim'e gitmeyi ONLER (hem gereksiz agi
        # trafigini hem de Nominatim'in kullanim politikasina (saniyede
        # <=1 istek) uyma riskini azaltir); `_son_geokodlama_zamani`
        # istekler ARASI ZORUNLU minimum araligi (bkz. `_GEOKODLAMA_MIN_
        # ARALIK_SANIYE`) uygulamak icin kullanilir.
        self._geolocator: Optional[Any] = None
        self._geokodlama_onbellek: Dict[str, Optional[Tuple[float, float]]] = {}
        self._son_geokodlama_zamani: float = 0.0

        self._initialized = True

    # ------------------------------------------------------------------ #
    # Bağlantı yönetimi
    # ------------------------------------------------------------------ #

    def connect(self) -> Driver:
        """Driver henüz oluşturulmadıysa oluşturur ve bağlantıyı doğrular."""
        if self._driver is None:
            try:
                self._driver = GraphDatabase.driver(
                    self._uri,
                    auth=(self._user, self._password),
                    # "BAĞLANTI HAVUZU" AYARI: `decision_engine.py`nin mikro-görev
                    # mimarisi (bkz. `_mikro_bolum_uret`), Lojistik/Tahliye/Sevk
                    # görevlerini bir `ThreadPoolExecutor` ile AYNI ANDA çalıştırır
                    # ve her biri kendi Neo4j sorgularını gönderir — sürücünün
                    # VARSAYILAN havuzu (100) zaten bu sayıyı karşılasa da, açıkça
                    # KÜÇÜK/aşırı-çekişmeli bir havuz gerçek bir "havuz tükendi ->
                    # yeni bağlantı için SONSUZA DEK bekle" kilidine yol açabilir.
                    # Açıkça 50 ayarlanır VE bir edinim zaman aşımı eklenir — havuz
                    # GERÇEKTEN tükenirse istek SONSUZA DEK beklemek yerine 60 sn
                    # sonra AÇIKÇA `ServiceUnavailable` ile başarısız olur (sessiz
                    # bir deadlock yerine terminalde GÖRÜNÜR bir hata).
                    max_connection_pool_size=50,
                    connection_acquisition_timeout=60.0,
                )
                self._driver.verify_connectivity()
                logger.info("Neo4j baglantisi kuruldu: %s (db=%s)", self._uri, self._database)
            except ServiceUnavailable as exc:
                self._driver = None
                raise Neo4jConnectionError(
                    f"Neo4j'e baglanilamadi: {self._uri}"
                ) from exc
        return self._driver

    def close(self) -> None:
        """Driver'ı kapatır ve kaynakları serbest bırakır."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            logger.info("Neo4j baglantisi kapatildi.")

    @classmethod
    def reset_singleton(cls) -> None:
        """Testlerde yeniden başlatma için singleton'ı sıfırlar."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
            cls._instance = None

    @property
    def driver(self) -> Driver:
        return self.connect()

    def session(self, **kwargs: Any) -> Session:
        return self.driver.session(database=self._database, **kwargs)

    def __enter__(self) -> "Neo4jConnection":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Genel amaçlı Cypher çalıştırma
    # ------------------------------------------------------------------ #

    _VARSAYILAN_SORGU_ZAMAN_ASIMI_SANIYE = 60.0
    """"NEO4J ÖLÜM DÖNGÜSÜ" RİSKİNE KARŞI KORUMA (çoklu kriz senaryolarında
    sistemin dakikalarca kilitli kalması riskini önler):
    HİÇBİR Cypher sorgusu (yazma/okuma fark etmez) sunucu tarafında bu
    süreden UZUN çalışamaz — `Neo4jError` olarak KESİN bir hata alırız,
    sonsuza kadar (veya "tüm Türkiye haritasını tarayarak") askıda
    KALMAYIZ. Bu, `execute_query`nin TÜM çağıranları için GEÇERLİDİR
    (Dijkstra yerel yol ağı kurulumu DAHİL — bkz. `_yerel_yol_agi_ve_
    kaynak_getir`), tek tek her sorguya ayrı ayrı eklenmesi GEREKMEZ.

    DEĞER SEÇİMİ (30 DEĞİL 60 saniye): `en_yakin_ulasilan_varliklari_bul`
    zaten dürüstçe belgelenmiş bir "EN KÖTÜ DURUM" senaryosuna sahiptir
    (bkz. `_YETENEK_ARAMA_YARICAPI_KM` — 50 km'lik genişletilmiş bir yerel
    yol ağı inşası ~30 sn sürebilir diye NOT edilmiştir);
    timeout'u tam 30 sn'ye ayarlamak bu MEŞRU/beklenen yavaş yolu YANLIŞLA
    keserdi. 60 sn, bu bilinen en-kötü-durumun ÜZERİNDE rahat bir pay
    bırakırken, GERÇEK bir "ölüm döngüsünü" (dakikalar süren bir kilitlenme)
    yine de KESİN olarak durdurur."""

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        write: bool = False,
        timeout: Optional[float] = _VARSAYILAN_SORGU_ZAMAN_ASIMI_SANIYE,
    ) -> List[Dict[str, Any]]:
        """Verilen Cypher sorgusunu çalıştırır ve kayıtları dict listesi olarak döner.

        `timeout` (saniye, varsayılan 60) — bkz. `_VARSAYILAN_SORGU_ZAMAN_
        ASIMI_SANIYE` docstring'i. SUNUCU TARAFINDA uygulanan bir
        transaction timeout'udur (istemci tarafı bir `requests`/socket
        zaman aşımı DEĞİLDİR) — sorgu bu süreyi AŞARSA Neo4j sunucusunun
        KENDİSİ transaction'ı iptal edip bir hata döner; bu sayede "çok
        büyük/yanlışlıkla sınırsız" bir sorgu istemciyi SONSUZA KADAR
        bekletemez. `timeout=None` verilerek (BİLİNÇLİ bir çağrı gerektirir)
        eski sınırsız davranışa dönülebilir — ör. çok uzun sürmesi
        BEKLENEN, nadir bir bakım/toplu işlem için.

        UYGULAMA NOTU (`neo4j.Query(text, timeout=...)` KULLANILMAZ):
        `neo4j.Query` nesnesi SADECE `session.run()` (auto-commit) ile
        çalışır — `tx.run()` (MANAGED transaction, yani `execute_write`/
        `execute_read` — bu metodun HER ZAMAN kullandığı, otomatik
        transient-hata YENİDEN DENEME'sini sağlayan mekanizma) içinde
        `TypeError: Query object is only supported for session.run`
        fırlatır. Bunun yerine `neo4j.
        unit_of_work(timeout=...)` KULLANILIR — bu, transaction
        FONKSİYONUNUN KENDİSİNE uygulanan bir dekoratördür ve managed
        transaction'ların otomatik yeniden-deneme davranışını KORUR
        (bir `Query` nesnesiyle değiştirseydik bu değerli yeniden-deneme
        güvencesini KAYBEDERDİK).
        """
        parameters = parameters or {}

        @unit_of_work(timeout=timeout)
        def _tx_calistir(tx: Any) -> List[Any]:
            return list(tx.run(query, parameters))

        try:
            with self.session() as session:
                if write:
                    records = session.execute_write(_tx_calistir)
                else:
                    records = session.execute_read(_tx_calistir)
                return [record.data() for record in records]
        except Neo4jError as exc:
            logger.error("Cypher sorgusu basarisiz oldu | query=%s | hata=%s", query, exc)
            raise Neo4jConnectionError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Model -> Cypher dönüştürme yardımcıları
    # ------------------------------------------------------------------ #

    @staticmethod
    def node_to_cypher(node: BaseNode, merge_isim: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """Bir `BaseNode` alt sınıfı örneğini, düğümü ekleyen/güncelleyen
        (upsert) bir Cypher sorgusuna ve parametrelerine çevirir.

        Kimlik (identity) anahtarı olarak `isim` (+ etiket) kullanılır; `id`
        DEĞİL. Sebep: `id`, LLM çıkarımı her çalıştığında rastgele üretilen bir
        UUID'dir ve aynı gerçek-dünya varlığı (ör. "Devlet Hastanesi") için her
        seferinde farklı bir `id` gelir. `id` üzerinden MERGE yapılsaydı, aynı
        varlık metinde her gecişte yeni bir kopya düğüm olarak eklenirdi.
        `isim` (aynı etiket içinde) kararlı/tekrarlanabilir bir dogal anahtar
        olduğundan MERGE anahtarı olarak kullanılır:
          - Düğüm ilk kez oluşturuluyorsa (ON CREATE) tüm özellikler (id dahil)
            yazılır.
          - Düğüm zaten varsa (ON MATCH) özellikler güncellenir ama orijinal
            `id` VE `isim` KORUNUR (yeni gelen rastgele id'yle veya LLM'in bu
            calistirmada urettigi hafifce farkli isim varyasyonuyla ezilmez);
            böylece düğümün kimliği zaman içinde stabil kalır.

        `merge_isim` verilirse (bkz. `add_node` / `_resolve_canonical_isim`),
        MERGE anahtari olarak `node.isim` yerine bu deger kullanilir — bulanik
        (fuzzy) eslesme sonucu bulunan mevcut "kanonik" isme MERGE edebilmek
        icindir.

        FAZ 2 GÜNCELLEMESİ (multi-label): `node.neo4j_labels()` (bkz.
        `models.BaseNode.neo4j_labels`), kategori etiketine (ör.
        "Infrastructure") EK OLARAK somut alt-tipi yansıtan bir İKİNCİL
        etiket (ör. "Bridge") döndürebilir. MERGE'in eşleşme/kimlik mantığı
        (isim + KATEGORİ etiketi) BİLİNÇLİ olarak DEĞİŞTİRİLMEMİŞTİR —
        ikincil etiket sadece koşulsuz bir `SET n:İkincil` ile EKLENİR:
          - Aynı isimli bir düğüm, alt-tipi değişse bile (nadir bir veri
            düzeltme senaryosu) YANLIŞLIKLA KOPYA OLUŞTURMAZ (MERGE hâlâ
            sadece kategori etiketine bakar).
          - İkincil etiket eklemek İDEMPOTENTTİR (düğüm zaten o etikete
            sahipse no-op'tur).
          - BİLİNEN SINIRLAMA: alt-tip değişirse (ör. bir yol "Kopru"dan
            "Tunel"e "düzeltilirse"), ESKİ ikincil etiket (`:Bridge`)
            OTOMATİK KALDIRILMAZ (hangi etiketin "eski" olduğunu bilmek
            için önce bir okuma gerekirdi); bu, sık rastlanmayan bir
            veri-düzeltme edge-case'i olduğundan bilinçli olarak kabul
            edilmiştir.

        Etiket adları (ör. "Bridge"), LLM/kullanıcı girdisinden DEĞİL, sabit
        bir Python eşleme tablosundan (`models._ALT_ETIKET_ESLEMESI`)
        geldiğinden Cypher enjeksiyon riski TAŞIMAZ (aşağıdaki
        `relationship_to_cypher`'daki `rel_type = relationship.tip.value`
        ile AYNI güvenlik varsayımı).
        """
        etiketler = node.neo4j_labels()
        kategori_etiketi = etiketler[0]
        ek_etiketler = etiketler[1:]

        props = node.to_cypher_properties()
        merge_key = merge_isim if merge_isim is not None else node.isim
        update_props = {key: value for key, value in props.items() if key not in ("id", "isim")}

        ek_etiket_clause = ""
        if ek_etiketler:
            ek_etiket_str = "".join(f":{etiket}" for etiket in ek_etiketler)
            ek_etiket_clause = f" SET n{ek_etiket_str}"

        # MEKANSAL INDEKSLEME (bkz. modul-seviyesi "MEKANSAL INDEKSLEME"
        # notu): enlem/boylam skalerlerine EK OLARAK (onlarin YERINE degil —
        # tum mevcut sorgular/PyDeck bunlara bagli kalir), Neo4j'nin YERLI
        # `Point` tipinde bir `konum` alani da yazilir; `ensure_constraints`
        # bu alan uzerinde bir POINT INDEX olusturur.
        query = (
            f"MERGE (n:{kategori_etiketi} {{isim: $isim}}) "
            "ON CREATE SET n = $props, "
            f"n.konum = {_konum_point_ifadesi('$props')} "
            "ON MATCH SET n += $update_props, "
            f"n.konum = {_konum_point_ifadesi('$update_props')}"
            f"{ek_etiket_clause} "
            "RETURN n"
        )
        parameters = {"isim": merge_key, "props": props, "update_props": update_props}
        return query, parameters

    @staticmethod
    def relationship_to_cypher(
        relationship: Relationship,
        kaynak_isim: Optional[str] = None,
        hedef_isim: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Bir `Relationship` örneğini iki düğüm arasında ilişki kuran/güncelleyen
        Cypher sorgusuna çevirir.

        Uç noktalar artık `isim` alanına göre eşleştirilir (bkz. `node_to_cypher`
        docstring'i); `kaynak_id`/`hedef_id` alanları bu nedenle pratikte ilgili
        düğümlerin `isim` değerlerini taşır (bkz. `nlp_parser._coerce_relationship`).
        `kaynak_isim`/`hedef_isim` verilirse (bkz. `add_relationship`), eşleşme
        için `relationship.kaynak_id`/`hedef_id` yerine bu bulanık-eşleşme
        sonucu bulunan kanonik isimler kullanılır. Neo4j ilişki tipleri
        parametrize edilemediğinden, `RelationshipType` enum değeri (kapalı/
        güvenli bir küme olduğundan enjeksiyon riski taşımaz) sorguya
        doğrudan gömülür.
        """
        rel_type = relationship.tip.value
        props = relationship.to_cypher_properties()
        query = (
            "MATCH (a {isim: $kaynak_isim}) "
            "MATCH (b {isim: $hedef_isim}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += $props "
            "RETURN a, r, b"
        )
        parameters = {
            "kaynak_isim": kaynak_isim if kaynak_isim is not None else relationship.kaynak_id,
            "hedef_isim": hedef_isim if hedef_isim is not None else relationship.hedef_id,
            "props": props,
        }
        return query, parameters

    def _resolve_canonical_isim(self, isim: str, label: Optional[str] = None) -> str:
        """Verilen `isim` icin veritabaninda zaten var olan, bulanik eslesen
        bir isim varsa onu ("kanonik" isim) dondurur; yoksa `isim`i
        degistirmeden dondurur.

        Once TAM (exact) eslesme aranir (coğu durumda yeterlidir ve ekstra
        islem gerektirmez). Bulunamazsa, ayni etiketteki (`label` verilirse)
        veya tum (`label=None` ise, iliski uc noktalarini cozmek icin
        kullanilir) mevcut isimlerle `difflib.SequenceMatcher` benzerligi
        hesaplanir; en yuksek skor `FUZZY_ISIM_MATCH_THRESHOLD` esigini
        gecerse o mevcut isim "kanonik" kabul edilip dondurulur. Bu sayede
        LLM'in ayni varlik icin uretebildigi kucuk isim varyasyonlari
        ("Arama Kurtarma Eki" / "...Ekipleri") ayni duguma MERGE edilir,
        veritabaninda kopya olusmaz.

        "FULL TABLE SCAN" RİSKİNE KARŞI KORUMA (`profiler_test.py` ile
        ölçülüp doğrulandı: bu fonksiyon TEK BAŞINA bir raporun
        Neo4j'e yazma süresinin ~%95'ini yutabiliyordu, ~25 sn/çağrı): docstring
        "önce TAM eşleşme aranır, çoğu durumda yeterlidir" diyordu AMA eski
        kod bunu YALANLIYORDU — TAM eşleşmeyi kontrol etmeden ÖNCE dahi
        `label` etiketindeki (veya `label=None` iken TÜM graftaki) HER
        düğümün `isim`ini (seed_db.py sonrası `Infrastructure` için 975.000+
        düğüm) Neo4j'den Python'a ÇEKİYOR, SONRA Python'da `isim in
        existing_isimler` ile üyelik kontrolü yapıyordu — yani "ucuz TAM
        eşleşme" iddiası, HER ÇAĞRIDA (ör. `add_node`/`add_relationship`
        başına 1-4 kez) tüm etiketi/graf'ı ağdan çeken bir "full label scan"a
        dönüşüyordu. Artık TAM eşleşme, `isim` üzerindeki UNIQUE CONSTRAINT'in
        (bkz. `ensure_constraints`) desteklediği İNDEKS-DESTEKLİ bir nokta
        sorgusuyla (`label` verilmişse TEK, `label=None` iken NODE_REGISTRY'
        deki HER somut etiket için AYRI ama YİNE İNDEKS-DESTEKLİ bir sorgu)
        kontrol edilir — HİÇBİR düğüm listesi Python'a ÇEKİLMEZ. Aşağıdaki
        pahalı DISTINCT+difflib taraması ARTIK SADECE gerçekten TAM eşleşme
        YOKSA (asıl, nadir "bulanık isim varyasyonu" senaryosu) çalışır.
        """
        if label:
            tam_sonuc = self.execute_query(
                f"MATCH (n:{label} {{isim: $isim}}) RETURN n.isim AS isim LIMIT 1",
                {"isim": isim},
                write=False,
            )
        else:
            # Iliski uc noktasi cozumlemesi: hangi somut etikette oldugu
            # BILINMIYOR, ama NODE_REGISTRY'deki HER somut etiketin kendi
            # `isim` UNIQUE CONSTRAINT indeksi VAR (bkz. `ensure_constraints`)
            # — TEK bir UNION ALL sorgusuyla, HER dal kendi indeksini
            # kullanarak (TUM grafi taramadan) TAM eslesme aranir.
            union_dallari = " UNION ALL ".join(
                f"MATCH (n:{etiket.value} {{isim: $isim}}) RETURN n.isim AS isim LIMIT 1"
                for etiket in NODE_REGISTRY
            )
            tam_sonuc = self.execute_query(union_dallari, {"isim": isim}, write=False)
        if tam_sonuc:
            return tam_sonuc[0]["isim"]

        # BULANIK (FUZZY) TARAMA — SADECE tam eşleşme YOKSA çalışır (nadir:
        # ör. gerçekten farklı bir isim varyasyonu VEYA hiçbir yerde
        # karşılığı olmayan bir "hayalet" referans). "STREET/BRIDGE HARİÇ
        # TUTMA" (`profiler_test.py` ile
        # ölçüldü: bir ilişkinin GERÇEKTE HİÇBİR YERDE karşılığı olmayan bir
        # uç noktaya (ör. Pydantic doğrulamasından geçemediği için hiç
        # yazılmamış bir varlık) referans vermesi, bu taramayı YİNE 975.000+
        # düğümlük tam taramaya düşürüyordu): `Street`/`Bridge` (bkz.
        # `models.Infrastructure._ALT_ETIKET_ESLEMESI` — "Sokak"/"Köprü"
        # ikincil etiketleri, NATİF Neo4j etiket indeksiyle ÜCRETSİZ dışlanır)
        # ADAY HAVUZUNA hiç dahil edilmez: bu noktalar HER ZAMAN bir OSM
        # teknik-kimlik soneki taşır (bkz. `OSM_TEKNIK_KIMLIK_IMZASI`) ve
        # taze bir LLM-üretimi isimle (Event/Unit/Facility/Karayolu/Tünel/
        # Viyadük) fuzzy oranı pratikte HİÇBİR ZAMAN eşiği geçmez — bu yüzden
        # dahil edilmeleri SADECE maliyet katar, asla gerçek bir eşleşme
        # ÜRETMEZ. (OSM-kaynaklı Sokak/Köprü'nün KENDİSİ zaten AYRI, özel bir
        # yoldan — bkz. `update_infrastructure_status_by_name` — eşleştirilir.)
        label_clause = f":{label}" if label else ""
        existing = self.execute_query(
            f"MATCH (n{label_clause}) WHERE n.isim IS NOT NULL AND NOT n:Street AND NOT n:Bridge "
            "RETURN DISTINCT n.isim AS isim",
            write=False,
        )
        existing_isimler = [row["isim"] for row in existing]
        if not existing_isimler:
            return isim

        folded_target = _fold_isim(isim)
        best_match: Optional[str] = None
        best_ratio = 0.0
        for candidate in existing_isimler:
            ratio = difflib.SequenceMatcher(None, folded_target, _fold_isim(candidate)).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = candidate

        if best_match is not None and best_ratio >= FUZZY_ISIM_MATCH_THRESHOLD:
            logger.info(
                "Bulanik isim eslesmesi: '%s' -> '%s' (benzerlik=%.2f) ayni duguma MERGE edilecek.",
                isim, best_match, best_ratio,
            )
            return best_match
        return isim

    # ------------------------------------------------------------------ #
    # SAHTE KOORDİNAT KAPANI ("Absolute Lockdown" — Hard Stop)
    # ------------------------------------------------------------------ #
    # Kanıtlanmış bir boşluk için savunma: "No Ghost Infrastructure" zırhı
    # (bkz. `update_infrastructure_status_by_name`) SADECE Infrastructure'ı,
    # SADECE isim-eşleştirme başarısızlığına karşı korur. Ama GERÇEK köprü doğru
    # eşleşse BİLE, aynı raporun ürettiği Event'in KENDİ `enlem`/`boylam`
    # alanı, modelin `EXTRACTION_PROMPT_TEMPLATE`daki few-shot ÖRNEĞİNDEN
    # (Van senaryosu, boylam≈43.3) KOPYALADIĞI bir değere ("38.5, 43.4")
    # sahip çıktı — bu Elazığ/Malatya'nın ÇOK dışında (gerçek yüklü OSM
    # verisi boylam ~37.4-39.9 aralığındadır), haritanın bambaşka bir
    # ucuna ("Van'a") düşen HAYALİ bir nokta. İsim-eşleştirme bu tür bir
    # hatayı YAKALAYAMAZ (Event'in zaten bir "gerçek karşılığı" YOKTUR,
    # sorun İSİM değil KOORDİNATTIR) — bu yüzden AYRI, TÜM düğüm tipleri
    # için geçerli, KOORDİNAT-tabanlı bir ikinci savunma hattı gerekir.
    #
    # ÇÖZÜM: sabit/hardcoded bir "yasaklı kutu" (ör. "Van'ı engelle") YERİNE
    # — bu, yarın Van GERÇEKTEN yüklenirse veya başka bir şehir hayali
    # koordinat üretirse İŞE YARAMAZ bir bant-yaması olurdu — GERÇEK OSM
    # verisinin (Street/Bridge) o AN kapladığı coğrafi ZARF (bounding box)
    # DİNAMİK olarak hesaplanır ve bir tolerans payı ("civarı") ile
    # genişletilir. Ülke çapında (81 il) daha fazla bölge yüklendikçe bu
    # zarf OTOMATİK büyür — statik bir sınır ASLA güncellenmesi gerekmez.
    _KOORDINAT_ZARFI_CACHE_SANIYE = 60.0
    """`get_real_koordinat_zarfi` sonucu bu sure kadar bellekte tutulur —
    bir kriz raporu tipik olarak birkaç saniye icinde birden fazla varlik
    icerir; her biri icin ayri bir MIN/MAX agregat sorgusu atmak yerine
    (740 bin+ dugumluk bir tarama), TEK bir hesaplama kisa sureligine
    yeniden kullanilir. `bulk_insert_nodes` (ETL) gibi TAMAMEN GUVENILIR
    kaynaklardan gelen toplu yollarda bu kapan HIC CAGRILMAZ (asagidaki
    docstring'e bakin) — sadece LLM/kullanici girdisi icin devreye girer."""

    _koordinat_zarfi_onbellek: Optional[Tuple[float, float, float, float]] = None
    _koordinat_zarfi_onbellek_zamani: float = 0.0

    def get_real_koordinat_zarfi(
        self, marj_derece: float = 1.0, _onbellek_atla: bool = False
    ) -> Optional[Tuple[float, float, float, float]]:
        """Gerçek OSM'den yüklenmiş (`:Street` veya `:Bridge`) düğümlerin
        kapladığı enlem/boylam ZARFINI (bounding box), `marj_derece` kadar
        (varsayılan 1.0° ≈ 111 km — "civarı" toleransı) her yönde
        genişletilmiş olarak döner: `(enlem_min, enlem_max, boylam_min,
        boylam_max)`. Grafta HİÇ gerçek OSM verisi yoksa (ör. taze/boş bir
        veritabanı) `None` döner — bu durumda çağıran taraf kontrolü
        GÜVENLİ VARSAYILAN olarak ATLAR (henüz hiçbir "gerçek bölge"
        tanımlı değilken her şeyi reddetmek, sistemi kullanılamaz kılardı).

        UÇ DEĞER (OUTLIER) DAYANIKLILIĞI: Ham `min`/`max` YERİNE 1.-99.
        YÜZDELİK DİLİM (percentile) kullanılır — CANLI OLARAK KEŞFEDİLDİ:
        Overpass'ın idari-alan (area) sorgusu, "Malatya" adını taşıyan
        BAŞKA bir (muhtemelen Kıbrıs'ta, ~35.3°K 33.1°D) idari alanla da
        yanlışlıkla eşleşmiş ve ~32.000 sokak düğümü GERÇEKTEN Malatya
        DIŞINDA bir koordinatla yüklenmiş durumda; ham `min`/`max` bu tek
        veri kalitesi sorununu ZARFIN TAMAMINI (ör. batıya doğru ~7°)
        gevşetmek için kullanırdı ve kapanın etkinliğini ciddi ölçüde
        zayıflatırdı. `_dinamik_gorunum_hesapla`daki (bkz. `src.ui.app`)
        AYNI 2.-98. yüzdelik dilim mantığı burada da kullanılır: az sayıda
        aykırı nokta, "gerçek bölge" tanımını ARTIK BOZAMAZ.
        """
        simdi = time.monotonic()
        if (
            not _onbellek_atla
            and self._koordinat_zarfi_onbellek is not None
            and (simdi - self._koordinat_zarfi_onbellek_zamani) < self._KOORDINAT_ZARFI_CACHE_SANIYE
        ):
            return self._koordinat_zarfi_onbellek

        sonuc = self.execute_query(
            "MATCH (n) WHERE n:Street OR n:Bridge "
            "RETURN percentileDisc(n.enlem, 0.01) AS enlem_min, percentileDisc(n.enlem, 0.99) AS enlem_max, "
            "percentileDisc(n.boylam, 0.01) AS boylam_min, percentileDisc(n.boylam, 0.99) AS boylam_max",
            write=False,
        )
        if not sonuc or sonuc[0]["enlem_min"] is None:
            self._koordinat_zarfi_onbellek = None
            self._koordinat_zarfi_onbellek_zamani = simdi
            return None

        r = sonuc[0]
        zarf = (
            r["enlem_min"] - marj_derece,
            r["enlem_max"] + marj_derece,
            r["boylam_min"] - marj_derece,
            r["boylam_max"] + marj_derece,
        )
        self._koordinat_zarfi_onbellek = zarf
        self._koordinat_zarfi_onbellek_zamani = simdi
        return zarf

    @staticmethod
    def koordinat_gercekci_mi(
        enlem: Optional[float], boylam: Optional[float], zarf: Optional[Tuple[float, float, float, float]]
    ) -> bool:
        """Bir `(enlem, boylam)` çiftinin, `zarf` (bkz. `get_real_koordinat_
        zarfi`) içinde olup olmadığını kontrol eder. `zarf=None` (henüz
        hiçbir gerçek OSM verisi yoksa) veya `enlem`/`boylam` eksikse
        GÜVENLİ VARSAYILAN olarak `True` (kabul) döner — bu fonksiyon
        SADECE gerçek bir referans zarfı VARKEN, o zarfın AÇIKÇA dışına
        düşen (ör. Van'a "sıçrayan") koordinatları REDDETMEK içindir."""
        if zarf is None or enlem is None or boylam is None:
            return True
        enlem_min, enlem_max, boylam_min, boylam_max = zarf
        return enlem_min <= enlem <= enlem_max and boylam_min <= boylam <= boylam_max

    # "COĞRAFİ BAĞLAMA" (ör. "Sivrice" gibi geniş bir yerleşim/ilçe adı
    # LLM'in kendi ürettiği (genelde implausible — bkz. `koordinat_gercekci_
    # mi`/few-shot kopyalama hatası) bir koordinatla gelebilir ve "Sahte
    # Koordinat Kapanı" bunu SESSİZCE reddedebilir). Bu, o kapanın
    # öncesinde çalışan bir KURTARMA adımıdır:
    # `koordinat_gercekci_mi` bir varlığı implausible bulduğunda, hemen
    # reddetmek YERİNE önce varlığın İSMİNDEKİ yer adı GERÇEK yüklü Street/
    # Bridge verisinde aranır; bir GERÇEK eşleşme varsa, o ismi taşıyan TÜM
    # gerçek noktaların ORTALAMASI (centroid) "o ilçenin/bölgenin en yakın
    # mantıklı düğümü" olarak kullanılır ("ilçenin/bölgenin merkez
    # koordinatlarına ... veya en yakın mantıklı
    # düğüme" bağlama). Bu proje HENÜZ ayrı bir idari-alan (ilçe/mahalle
    # sınırı) KAYDI YÜKLEMEDİĞİNDEN (bkz. `models.AdministrativeArea` —
    # tanımlı ama `real_osm_loader.py` tarafından HİÇ doldurulmuyor), "ilçe
    # merkezi" için en dürüst/gerçekçi vekil, o ilçenin adını taşıyan
    # GERÇEK yol segmentlerinin coğrafi ortalamasıdır — UYDURULMUŞ bir
    # koordinat DEĞİL, gerçek OSM verisinden TÜRETİLMİŞ bir değerdir.
    _YER_ADI_DURAK_KELIMELERI = frozenset(
        {
            "depremi", "deprem", "seli", "sel", "yangini", "yangin", "binasi",
            "bina", "merkezi", "istasyonu", "istasyon", "deposu", "koprusu",
            "kopru", "tuneli", "tunel", "kavsagi", "kavsak", "caddesi", "cadde",
            "sokak", "sokagi", "bulvari", "bulvar", "hastanesi", "hastane",
            "havalimani", "ussu", "us", "limani", "liman", "siginagi", "siginak",
            "timi", "tim", "birligi", "birlik", "ekibi", "ekip", "noktasi",
            "nokta", "patlamasi", "patlama", "saldirisi", "saldiri", "kazasi",
            "kaza", "olayi", "olay", "ile", "veya", "yolu", "yol",
        }
    )
    """`_yer_adindan_gercek_merkez_koordinat_bul`ın, bir varlık isminden
    ("Sivrice Depremi" gibi) GERÇEK bir yer-adı ADAYINI ayıklarken atladığı,
    bu sistemin KENDİ ürettiği/kullandığı jenerik tür-sonu kelimeleri (bkz.
    `models.py`daki Enum değerleri ve bu dosyanın/`nlp_parser.py`nin
    ürettiği isim kalıpları) — `normalize_tr` (bkz. `nlp_parser.py`)
    KULLANILMAZ (bu modül LLM katmanına bağımlı DEĞİLDİR); bunun yerine
    basit `str.lower()` karşılaştırması YETERLİDİR (yanlış eşleşme riski
    düşük, bu SADECE bir "dur kelimesi" listesidir, kesin bir dilbilimsel
    normalizasyon DEĞİLDİR)."""

    def _yer_adi_adaylarini_cikar(self, isim: str) -> List[str]:
        """`isim` içinden (ör. "Sivrice Depremi" -> ["Sivrice"]) GERÇEK bir
        yer-adı olabilecek kelimeleri, en olası ADAYDAN başlayarak sıralı
        bir liste olarak çıkarır — hem `_yer_adindan_gercek_merkez_
        koordinat_bul` (YEREL veri araması) hem `harici_geokodlama_ile_
        koordinat_bul` (Nominatim araması) TARAFINDAN ORTAK kullanılır.

        Öncelik SIRASI: önce `isim`in İLK kelimesi (bu sistemin isimlendirme
        kuralı — hem LLM hem `synthetic_unit_seeder.py` — HER ZAMAN yer
        adını BAŞA koyar, ör. "Sivrice Depremi", "Kömürhan Köprüsü",
        "Doğanşehir Orman İtfaiyesi İstasyonu"), ardından geri kalan
        kelimeler (`_YER_ADI_DURAK_KELIMELERI`deki jenerik tür-sonu
        kelimeler VE 4 karakterden kısa kelimeler HARİÇ, yanlış eşleşme
        riskini azaltmak için).
        """
        kelimeler = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]+", isim or "")
        if not kelimeler:
            return []

        adaylar: List[str] = []
        ilk = kelimeler[0]
        if len(ilk) >= 3:
            adaylar.append(ilk)
        for kelime in kelimeler[1:]:
            if len(kelime) >= 4 and kelime.lower() not in self._YER_ADI_DURAK_KELIMELERI:
                adaylar.append(kelime)
        return adaylar

    _yerlesim_onbellek: Optional[List[Dict[str, Any]]] = None
    _yerlesim_onbellek_zamani: float = 0.0
    _YERLESIM_ONBELLEK_CACHE_SANIYE = 60.0
    """`_yerlesimleri_getir`in TTL'i — `_KOORDINAT_ZARFI_CACHE_SANIYE` ile
    AYNI süre/mantık (bkz. o alanın docstring'i): tek bir rapor işlenirken
    birden çok varlık (Event + Facility + Infrastructure...) için bu
    fonksiyon art arda çağrılabilir; 1057 satırlık Settlement tablosunu HER
    ÇAĞRIDA yeniden çekmek yerine kısa süreliğine önbelleklenir."""

    def _yerlesimleri_getir(self) -> List[Dict[str, Any]]:
        """TÜM `Settlement` düğümlerini (isim/il/enlem/boylam) TEK seferde
        çeker ve kısa süreliğine önbellekler — bkz. `_yer_adindan_gercek_
        merkez_koordinat_bul`daki KULLANIM NEDENİ: Türkçe-katlanmış
        karşılaştırma Neo4j'in ham `=`/`STARTS WITH`'i ile YAPILAMAZ,
        Python tarafında yapılmalıdır; bu da adayları TEK TEK sorgulamak
        yerine tüm tabloyu bir kez çekmeyi GEREKTİRİR (1057 satır, ucuz)."""
        simdi = time.monotonic()
        if (
            self._yerlesim_onbellek is not None
            and (simdi - self._yerlesim_onbellek_zamani) < self._YERLESIM_ONBELLEK_CACHE_SANIYE
        ):
            return self._yerlesim_onbellek
        sonuc = self.execute_query(
            "MATCH (s:Settlement) RETURN s.isim AS isim, s.il AS il, s.enlem AS enlem, s.boylam AS boylam",
            write=False,
        )
        self._yerlesim_onbellek = sonuc
        self._yerlesim_onbellek_zamani = simdi
        return sonuc

    def _yer_adindan_gercek_merkez_koordinat_bul(self, isim: str) -> Optional[Tuple[float, float]]:
        """`isim` içindeki (bkz. `_yer_adi_adaylarini_cikar`) yer-adı
        ADAYLARINI önce `Settlement` (il/ilçe merkezleri), sonra yüklü
        `Street`/`Bridge` verisinde arar; bir eşleşme varsa GERÇEK bir
        merkez koordinat `(enlem, boylam)` olarak döner. Eşleşme yoksa
        `None` döner — çağıran taraf (bkz. `src.ui.app._grafa_yaz`) bu
        durumda bir sonraki kademeye (bkz. `harici_geokodlama_ile_
        koordinat_bul`) veya (o da başarısız olursa) MEVCUT reddetme
        davranışına geçer; bu fonksiyon ASLA bir varlığı REDDETMEZ, sadece
        opsiyonel bir KURTARMA denemesi sunar.

        DÜZELTME #1 (kanıtlanmış bir boşluk: "Mersin
        Tarsus" krizi, KONYA yakınlarına yerleşiyordu): eskiden SADECE `Street`/
        `Bridge` aranıyordu ve eşleşen TÜM noktaların HAM ORTALAMASI
        alınıyordu — "Tarsus" araması, gerçek Tarsus şehir sokaklarının
        (2306 nokta, ~37.1°K) YANI SIRA "Ankara-Tarsus Otoyolu" adlı
        (Tarsus'a GİTMEYEN, adında sadece VARIŞ NOKTASI geçen) ~700 km'lik
        bir otoyolun 4156 nokta-düğümünü de (~38.9°K, Ankara yakınları)
        eşleştiriyordu — sayıca DAHA FAZLA olan otoyol noktaları ortalamayı
        Ankara'ya doğru çekip merkezi Konya civarına sürüklüyordu. ÇÖZÜM:
        `add_settlements.py` ile yüklenen 1057 il/ilçe merkezi (`Settlement`
        düğümleri) artık ÖNCE denenir. Settlement'ta karşılık yoksa (ör.
        "Sivrice Göl Kenarı" gibi bir yerleşim-altı/köprü/tesis adı), eskisi
        gibi Street/Bridge averaging'e düşülür — ama `motorway`/`trunk`
        (uzun mesafe otoyolları — TAM DA bu kirliliğin kaynağı) bu ikinci
        kademeden HARİÇ tutulur.

        DÜZELTME #2 (kanıtlanmış bir boşluk: "Bartın Amasra Sel
        Felaketi" ~1500 km yanlış konumda, Gürcistan sınırına yakın bir
        noktaya yerleşiyordu):
        DOĞRULANAN İKİ AYRI KÖK SEBEP, İKİSİ DE bu fonksiyonun Neo4j'e HAM
        `=`/`STARTS WITH` bıraktığı içindi:

          (a) TÜRKÇE KARAKTER UYUMSUZLUĞU: LLM'in ürettiği `isim` bazen
              Türkçe karaktersiz bir varyant taşır (ör. "Bartin" [düz i],
              gerçek Settlement kaydı ise "Bartın" [noktasız ı]) — Neo4j'in
              ham `=` karşılaştırması bunları FARKLI STRING sayıp eşleşmeyi
              SESSİZCE KAÇIRIYORDU; sonuç: bu kurtarma HİÇ TETİKLENMEDİ,
              LLM'in ham (yanlış) enlem/boylam tahmini `koordinat_gercekci_
              mi`nin KABA ülke-çapı zarfını geçtiği için DOĞRUDAN kullanıldı
              (ör. enlem doğruya yakın [41.65] ama boylam ~10°
              yanlış [42.15] çıkabiliyordu). ÇÖZÜM: artık
              Neo4j'den TÜM Settlement'lar (`_yerlesimleri_getir`) çekilip
              karşılaştırma PYTHON'da `_fold_isim` (Türkçe katlama) İLE
              yapılıyor — "Bartin" ve "Bartın" artık AYNI anahtara düşer.

          (b) İL/İLÇE ÇAKIŞMASI:
              Türkiye'de AYNI ilçe adı BİRDEN FAZLA farklı ilde GERÇEKTEN
              var olabilir — ör. "Kemalpaşa" hem İzmir'de (38.4°K) HEM
              Artvin'de (41.5°K, 41.5°D — Gürcistan sınırına YAKIN) GERÇEK
              bir ilçe merkezidir. Eski `STARTS WITH $terim` + `avg()`
              deseni, bare bir "Kemalpaşa" adayı için BU İKİSİNİ DE
              eşleştirip ORTALAMASINI alıyordu — bu, HİÇBİR gerçek yere
              karşılık GELMEYEN bir "hayalet merkez" (orta Anadolu'da bir
              yerde) üretiyordu; TAM OLARAK "Ankara-Tarsus Otoyolu"
              hatasıyla AYNI SINIF bir sorun, sadece Settlement seviyesinde
              hiç fark edilmemişti. ÇÖZÜM ("İL/İLÇE HİYERARŞİSİ ZORUNLULUĞU"):
              birden fazla GERÇEK yer aynı bare isme sahipse, ASLA
              ortalama ALINMAZ — bunun yerine `isim`deki DİĞER adaylardan
              biri (ör. aynı raporda "Bartın" da geçiyorsa) bu adaylardan
              TAM OLARAK BİRİNİN `il` alanıyla eşleşiyor mu diye bakılır;
              eşleşiyorsa O TEK doğru yer seçilir. Hiçbir il ipucu yoksa
              veya çakışma YİNE DE çözülemiyorsa, bu ADAY TAMAMEN ATLANIR
              (tahmin/ortalama ÜRETİLMEZ — "asla sahte/uydurma bir koordinat
              üretme" ilkesiyle TUTARLI) ve sıradaki adaya/kademeye geçilir.
        """
        adaylar = self._yer_adi_adaylarini_cikar(isim)
        if not adaylar:
            return None
        adaylar_katlanmis = {_fold_isim(a) for a in adaylar}

        yerlesimler = self._yerlesimleri_getir()
        for aday in adaylar:
            aday_katlanmis = _fold_isim(aday)
            # `Settlement.isim`, ilçe kayıtlarında "İlçe (İl)" bicimindedir
            # (bkz. `add_settlements.py` — il-cakismasi ONLEMEK icin eklenen
            # sonek); karsilastirma icin SADECE parantez ONCESI (bare) kisim
            # kullanilir - il kayitlarinda zaten parantez YOKTUR, etkilenmez.
            eslesenler = [
                s for s in yerlesimler
                if s.get("isim") and _fold_isim(s["isim"].split(" (")[0]) == aday_katlanmis
            ]
            if not eslesenler:
                continue
            if len(eslesenler) == 1:
                s = eslesenler[0]
                logger.info(
                    "COĞRAFİ BAĞLAMA (Settlement): '%s' adayı '%s' (%s) ile eşleşti; "
                    "merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, s["isim"], s.get("il"), s["enlem"], s["boylam"],
                )
                return s["enlem"], s["boylam"]

            # BİRDEN FAZLA gerçek yer AYNI bare isme sahip (bkz. yukarıdaki
            # "DÜZELTME #2b") — İL/İLÇE HİYERARŞİSİ zorunlu
            # kılınır: raporun/isim'in DİĞER adaylarından biri, çakışan
            # eşleşmelerden TAM OLARAK BİRİNİN `il` alanıyla eşleşiyor mu?
            il_ile_eslesen = [
                s for s in eslesenler
                if s.get("il") and _fold_isim(s["il"]) in adaylar_katlanmis
            ]
            if len(il_ile_eslesen) == 1:
                s = il_ile_eslesen[0]
                logger.info(
                    "COĞRAFİ BAĞLAMA (Settlement, İL/İLÇE HİYERARŞİSİYLE ÇAKIŞMA ÇÖZÜLDÜ): "
                    "'%s' adayı %d farklı ilde eşleşti (%s); raporda geçen il ipucu sayesinde "
                    "'%s' (%s) seçildi, merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, len(eslesenler), [e.get("il") for e in eslesenler],
                    s["isim"], s.get("il"), s["enlem"], s["boylam"],
                )
                return s["enlem"], s["boylam"]

            # Çakışma il ipucuyla da ÇÖZÜLEMEDİ — KESİNLİKLE tahmin/ortalama
            # ÜRETİLMEZ; bu aday TERK edilir, döngü sıradaki adaya geçer.
            logger.warning(
                "COĞRAFİ BAĞLAMA: '%s' adayı %d FARKLI ilde eşleşti (%s) ve raporda "
                "hiçbiriyle eşleşen bir il ipucu yok — belirsizlik nedeniyle bu aday "
                "ATLANDI (ortalama/tahmin ÜRETİLMEDİ, sıradaki adaya/kademeye geçiliyor).",
                aday, len(eslesenler), [e.get("il") for e in eslesenler],
            )

        for aday in adaylar:
            sonuc = self.execute_query(
                "MATCH (n) WHERE (n:Street OR n:Bridge) AND n.acik_mi = true "
                "AND n.isim CONTAINS $terim AND n.enlem IS NOT NULL AND n.boylam IS NOT NULL "
                # Uzun mesafe otoyolları (`motorway`/`trunk`) genelde VARIŞ
                # NOKTASININ adını taşır ama o yerin İÇİNDEN GEÇMEZ (bkz.
                # yukarıdaki "Ankara-Tarsus Otoyolu" örneği) — bu ikinci
                # kademeden BİLEREK hariç tutulur (Bridge düğümlerinde
                # `highway_tipi` yoktur, `IS NULL` ile onlar ETKİLENMEZ).
                "AND (n.highway_tipi IS NULL OR NOT n.highway_tipi IN ['motorway', 'trunk']) "
                "RETURN avg(n.enlem) AS enlem, avg(n.boylam) AS boylam, count(n) AS adet",
                {"terim": aday}, write=False,
            )
            if sonuc and sonuc[0]["adet"] and sonuc[0]["adet"] > 0:
                logger.info(
                    "COĞRAFİ BAĞLAMA (Street/Bridge): '%s' adayı GERÇEK veride %d noktayla "
                    "eşleşti; merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, sonuc[0]["adet"], sonuc[0]["enlem"], sonuc[0]["boylam"],
                )
                return sonuc[0]["enlem"], sonuc[0]["boylam"]
        return None

    _GEOKODLAMA_MIN_ARALIK_SANIYE = 1.1
    """Nominatim'in halka açık kullanım politikası saniyede EN FAZLA 1
    istek izin verir; 1.1 sn küçük bir güvenlik payı bırakır. Kendi
    Nominatim sunucunuzu (bkz. `requirements.txt`'deki "Kapalı devre"
    notu) kullanıyorsanız bu kısıt gereksiz katı olabilir — gerekirse bu
    sabiti düşürün."""

    def harici_geokodlama_ile_koordinat_bul(self, isim: str) -> Optional[Tuple[float, float]]:
        """"ULUSAL ÖLÇEKLİ ESNEKLİK" — SON (3.) KADEME:
        `_yer_adindan_gercek_merkez_koordinat_bul` (YEREL Street/Bridge
        verisi) bir eşleşme BULAMADIĞINDA çağrılır — `isim`deki yer-adı
        adaylarını (bkz. `_yer_adi_adaylarini_cikar`, EN İYİ/ilk aday
        kullanılır) Nominatim (OpenStreetMap'in geocoding servisi) ile
        gerçek dünya koordinatına çevirir. Böylece "Sivrice"/"Baskil" gibi
        SADECE yerel yol adlarında geçen değil, "İzmir Merkez", "Kızılay
        Meydanı" gibi YEREL veride HİÇ karşılığı olmayan Türkiye çapındaki
        HER yer adı da haritaya işlenebilir hale gelir.

        DÜRÜST SINIR (bkz. `requirements.txt`'deki "Kapalı devre" notu):
        bu, `_yer_adindan_gercek_merkez_koordinat_bul`in AKSİNE, ÇALIŞMA
        ANINDA gerçek bir AĞ ÇAĞRISI yapar. Başarılı olursa döndürülen
        koordinat gerçek dünya konumu olarak GÜVENİLİRDİR (bu yüzden
        `koordinat_gercekci_mi` zarfının DIŞINDA kalsa BİLE reddedilmez —
        bkz. `src.ui.app._grafa_yaz`), ANCAK bu bölge için yerel yol ağı
        (Dijkstra/rota) verisi YÜKLENMEMİŞ olabilir; bu durumda `Decision
        Engine` (bkz. `en_yakin_ulasilan_varliklari_bul`) bu olay için
        DÜRÜSTÇE "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" der — sistem
        SESSİZCE yanlış/uydurma bir birlik ÖNERMEZ, sadece o bölgede henüz
        operasyonel yol-ağı kapsamı olmadığını AÇIKÇA yansıtır.

        Aşağıdaki durumların HERHANGİ BİRİNDE (opsiyonel bağımlılık kurulu
        değil, ağ hatası, zaman aşımı, sıfır sonuç) sessizce `None` döner —
        çağıran taraf bu durumda MEVCUT (reddetme) davranışına devam eder;
        bu fonksiyon ASLA istisna FIRLATMAZ (bkz. `_grafa_yaz`'ın "hiçbir
        harici servis çökmeyi TETİKLEMEMELİ" ilkesi — dil kilidi/çeviri
        zincirlerindeki AYNI desen).
        """
        if not _GEOPY_MEVCUT:
            logger.warning(
                "Harici geokodlama İSTENDİ ama 'geopy' paketi KURULU DEĞİL "
                "(bkz. requirements.txt); bu kademe ATLANIYOR."
            )
            return None

        adaylar = self._yer_adi_adaylarini_cikar(isim)
        if not adaylar:
            return None
        sorgu = adaylar[0]

        onbellek_anahtari = sorgu.lower()
        if onbellek_anahtari in self._geokodlama_onbellek:
            return self._geokodlama_onbellek[onbellek_anahtari]

        if self._geolocator is None:
            # `user_agent`: Nominatim ToS'unun ZORUNLU kıldığı, uygulamayı
            # tanımlayan bir değer (jenerik/varsayılan bir değer ban
            # riski taşır). `domain=`/`scheme=` parametreleriyle KENDİ
            # self-host Nominatim sunucunuza da yönlendirilebilir (bkz.
            # requirements.txt'deki "Kapalı devre" notu) — burada
            # DEĞİŞTİRİLMEDEN halka açık varsayılan sunucu kullanılır.
            self._geolocator = Nominatim(
                user_agent="kriz-karar-destek-sistemi-c4isr/1.0", timeout=5
            )

        gecen_sure = time.monotonic() - self._son_geokodlama_zamani
        if gecen_sure < self._GEOKODLAMA_MIN_ARALIK_SANIYE:
            time.sleep(self._GEOKODLAMA_MIN_ARALIK_SANIYE - gecen_sure)

        sonuc: Optional[Tuple[float, float]] = None
        try:
            self._son_geokodlama_zamani = time.monotonic()
            konum = self._geolocator.geocode(
                f"{sorgu}, Türkiye", country_codes="tr", exactly_one=True
            )
            if konum is not None:
                sonuc = (konum.latitude, konum.longitude)
                logger.info(
                    "HARİCİ GEOKODLAMA (Nominatim): '%s' -> (%.5f, %.5f).",
                    sorgu, sonuc[0], sonuc[1],
                )
            else:
                logger.warning("HARİCİ GEOKODLAMA: '%s' için Nominatim'de sonuç bulunamadı.", sorgu)
        except (GeocoderTimedOut, GeocoderServiceError) as exc:
            logger.warning("HARİCİ GEOKODLAMA başarısız oldu ('%s'): %s", sorgu, exc)
        except Exception as exc:  # noqa: BLE001 - harici servis ASLA pipeline'i cokertmemeli.
            logger.warning("HARİCİ GEOKODLAMA beklenmedik hatayla başarısız oldu ('%s'): %s", sorgu, exc)

        self._geokodlama_onbellek[onbellek_anahtari] = sonuc
        return sonuc

    # ------------------------------------------------------------------ #
    # "MEKANSAL KÖRLÜK" DÜZELTMESİ — Graf-Tabanlı Yol Bulma (Pathfinding)
    # ------------------------------------------------------------------ #
    # Kanıtlanmış bir boşluk için savunma: `DecisionEngine` eskiden "sahadaki
    # aktif birlikler"i DÜZ METİN gibi (hiçbir mesafe/erişilebilirlik
    # hesabı YAPMADAN) listeliyordu; LLM bu ham listeden kafasına göre
    # birim seçtiği için olay yerine 80 km uzaktaki (ör. Palu, Kovancılar)
    # birimleri önerebiliyordu — üstelik önündeki yol KAPALI olsa bile.
    #
    # NEDEN "GERÇEK" APOC/shortestPath DEĞİL: Neo4j'nin native
    # `shortestPath()`u VE APOC'un Dijkstra prosedürü, düğümler arasında
    # ZATEN VAR OLAN İLİŞKİLERİ (kenarları) gerektirir. Bu projenin gerçek
    # OSM verisi (bkz. `real_osm_loader.RealOsmLoader`) HER sokak/köprü
    # noktasını BAĞIMSIZ bir nokta-düğüm olarak yükler — aralarında (aynı
    # caddenin ardışık noktaları veya kesişen farklı caddeler arasında)
    # HİÇBİR topoloji ilişkisi (`CONNECTED_TO` vb.) YOKTUR ve Overpass'ın
    # `out geom` çıktısı OSM düğüm ID'lerini taşımadığından (sadece ham
    # koordinat) bunu üretmek YENİDEN bir ETL/şema devrimi gerektirirdi.
    # Bu yüzden yol ağı GRAFİ, HER sorguda, olay yerinin ÇEVRESİNDEKİ
    # (yarıçap) GERÇEK ana damar noktalarından, iki KANITLANMIŞ gerçek
    # topoloji kuralıyla DİNAMİK olarak (ve SADECE bellekte, veritabanına
    # YAZILMADAN) kurulur:
    #   1) AYNI OSM way'ine ait ARDIŞIK noktalar (isimdeki "(OSM way/ID
    #      #IDX)" sonekinden ayıklanır) birbirine bağlanır — bu, o caddenin/
    #      yolun KENDİ GERÇEK güzergahıdır.
    #   2) FARKLI way'lerin BİRBİRİNE ÇOK YAKIN (<= `_KESISIM_TOLERANSI_
    #      METRE`) noktaları bir KESİŞİM (intersection) olarak bağlanır —
    #      iki caddenin GERÇEKTE kesiştiği yerin yaklaşık karşılığıdır.
    # KAPALI (`acik_mi = false`) hiçbir nokta bu ağa DAHİL EDİLMEZ — kapalı
    # bir yol, üzerinden GEÇİLEMEYEN, ağdan tamamen İZOLE bir düğüm/kenar
    # haline gelir; Dijkstra bu yüzden ASLA kapalı bir güzergahtan geçen
    # bir "en kısa yol" ÜRETEMEZ (fiziksel olarak yol grafta YOKTUR).
    #
    # KAPSAM SINIRI (dürüstçe belirtilmelidir):
    # İLK sürüm, performans için yol ağını SADECE "ana damar" (motorway/
    # trunk/primary/secondary) noktalarıyla kurmuştu — ama Kömürhan
    # Köprüsü senaryosunda bu, yerel ağı 41/36.832 düğümlük anlamsız bir
    # mikro-adaya PARÇALADI: kırsal Türkiye'de iki ana damar genellikle
    # BİRBİRİNE DOĞRUDAN DEĞİL, aralarındaki yerel/tali (residential/
    # tertiary) yollar ÜZERİNDEN bağlanır — filtre tam da bu "bağlayıcı
    # dokuyu" dışarıda bırakıyordu. Bu yüzden yol ağı artık TAM Street/
    # Bridge ağından kurulur (bkz. `_yerel_acik_yol_agini_getir`); ulusal
    # ölçekte performansı korumak için arama yarıçapı `_YOL_AGI_ARAMA_
    # YARICAPI_KM` ile 25 km gibi makul bir sınırda tutulur (25 km'de
    # ~87 bin nokta, ~6 sn — kabul edilebilir; 50 km'de ~364
    # bin nokta, ~30 sn — HAYIR).

    _YOL_AGI_ARAMA_YARICAPI_KM = 25.0
    """`en_yakin_ulasilan_varliklari_bul`ın varsayılan arama yarıçapı —
    yukarıdaki "KAPSAM SINIRI" notundaki performans/kapsam ödünleşiminin
    doğrudan sonucudur."""

    _HEDEF_ADAY_COKLUGU_KATSAYISI = 20
    """`en_yakin_ulasilan_varliklari_bul`daki hedef-aday LIMIT'i için
    çarpan — bkz. o metodun içindeki "KESİN SINIR DÜZELTMESİ" yorumu.
    `decision_engine._ADAY_COKLUGU_KATSAYISI` (15) ile AYNI desenin bu
    katmandaki BAĞIMSIZ karşılığıdır (biraz daha yüksek tutuldu, çünkü
    burada adayların bir kısmı SONRADAN yol ağına 'snap' toleransı
    (`_HEDEF_SNAP_YARICAPI_KM`) dışında kalıp elenebilir)."""

    _HEDEF_ADAY_MIN_LIMITI = 50
    """`_HEDEF_ADAY_COKLUGU_KATSAYISI` ile çarpım küçük `ilk_n` (ör. 3)
    değerlerinde bile aday havuzunun aşırı dar kalmamasını garanti eden
    taban değer."""

    _YOL_AGI_DUGUM_GUVENLIK_LIMITI = 150_000
    """"KESİN SINIR" GÜVENLİK VALFİ — bkz.
    `_yerel_acik_yol_agini_getir` docstring'indeki tam gerekçe: bu bir
    performans ayarı DEĞİL, belgelenmiş en kötü gerçek durumun (50 km'de
    ~364 bin nokta) altında ama normal işleyişte HİÇ tetiklenmeyecek bir
    üst sınırdır — sadece anormal/bozuk veri karşısında sonsuz beklemeyi
    önler."""

    _KESISIM_TOLERANSI_METRE = 100.0
    """Farklı OSM way'lerine ait iki noktanın "aynı kesişimde" sayılması
    için üst mesafe sınırı. Overpass'tan gerçek paylaşılan düğüm ID'leri
    ALINAMADIĞINDAN (bkz. yukarıdaki mimari not) bu, kesişimi YAKLAŞIK
    olarak tespit eden bir mesafe-toleranslı vekildir.

    DEĞER GERÇEK VERİYLE AMPİRİK OLARAK BELİRLENMİŞTİR (ilk varsayım 25 m
    idi — bu, Kömürhan Köprüsü kapanma senaryosunda yerel yol
    ağını 41/36.832 düğümlük bir mikro-adaya PARÇALAMIŞTI): aynı fiziksel
    kesişimi paylaşan farklı OSM way uç noktaları, beklenenin aksine
    "birkaç metre" DEĞİL, genellikle onlarca metre farklı koordinatlarda
    kayıtlı (muhtemelen way'ler arası digitize/yuvarlama farkı). 25 m'den
    75 m'ye çıkarmak ulaşılabilir düğüm sayısını 41'den
    13.569'a (36.832 üzerinden) çıkardı; 150 m ve 300 m'de neredeyse HİÇ
    ek kazanç YOKTU (13.609'da doygunluğa ulaştı) — bu, daha da
    gevşetmenin GERÇEK kesişimleri değil ALAKASIZ yakın yolları
    birleştirmeye başlayacağının kanıtıdır. 100 m, bu doygunluk
    platosunun GÜVENLİ ORTASINDADIR."""

    _HEDEF_SNAP_YARICAPI_KM = 5.0
    """Bir birlik/tesisin, ana damar ağına 'bağlanabilmesi' için o ağdaki
    EN YAKIN noktaya olması gereken azami mesafe. Bunun dışında kalan bir
    birlik/tesis, ana damar ağından bu kadar uzaksa güvenilir bir kara
    yolu mesafesi hesaplanamaz sayılır ve sonuçlardan ELENİR (uydurma bir
    mesafe vermek yerine dürüstçe 'değerlendirilemedi' sayılması tercih
    edilir)."""

    _KANCA_MAKS_MESAFE_KM = 1.0
    """"KANCA ALGORİTMASI" (Snap to Connected Component): heuristik kesişim
    tespiti (bkz. `_KESISIM_TOLERANSI_METRE`), GERÇEK OSM paylaşılan düğüm
    ID'leri OLMADIĞI için bazen AYNI fiziksel yolun (ör. "Eski Malatya-Elazığ
    Yolu") FARKLI OSM way parçalarını birbirine BAĞLAYAMAZ — ör. "AFAD
    Elazığ İkmal Noktası" olay yerinden sadece 0.5 km uzaktaydı ama kendi
    yol noktası, AYNI yolun ulaşılabilir bir parçasından SADECE 388 m
    ötede, ayrı bir bileşende kaldığı için "erişilemez" görünüyordu.

    BU DEĞER BİLİNÇLİ OLARAK KÜÇÜK TUTULUR: amaç SADECE bu tür KÜÇÜK veri-
    kalitesi boşluklarını (aynı yolun bölünmüş parçaları) köprülemektir —
    GERÇEK bir fiziksel engeli (ör. bir nehir/vadi geçişi, YALNIZCA şu an
    KAPALI olan bir köprüyle aşılabilen bir boşluk) YANLIŞLIKLA
    "köprülememesi" GEREKİR. Kapalı (`acik_mi=false`) noktalar zaten yerel
    ağa HİÇ DAHİL EDİLMEDİĞİNDEN (bkz. `_yerel_acik_yol_agini_getir`), bir
    kanca ASLA kapalı bir köprü/yol ÜZERİNDEN "geçiş" oluşturamaz — sadece
    İKİ AYRI AÇIK yol parçası arasında, KISA bir mesafede (<= 1 km) köprü
    kurar. 1 km, gözlenen 388 m boşluğa güvenli bir pay
    bırakırken, tipik bir nehir/vadi geçişinin genişliğinden ÖNEMLİ ÖLÇÜDE
    KÜÇÜKTÜR — "kapalı yol ağı izole eder" garantisini BOZMAZ."""

    _OSM_WAY_INDEX_DESENI = re.compile(r"\(OSM way/(\d+)(?:\s*#(\d+))?\)")

    @staticmethod
    def _geodesic_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
        """İki (enlem, boylam) noktası arası GERÇEK jeodezik mesafeyi
        (haversine, km) hesaplar. Yerel graf inşası SIRASINDA (Python
        belleğinde, binlerce kez) çağrılacağından, her seferinde bir Neo4j
        `point.distance()` round-trip'i YERİNE saf Python'da hesaplanır."""
        R_KM = 6371.0088
        lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
        lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * R_KM * math.asin(min(1.0, math.sqrt(a)))

    @classmethod
    def _osm_way_ve_indeks_ayikla(cls, isim: str) -> Optional[Tuple[str, Optional[int]]]:
        """`isim`deki "(OSM way/{id} #{idx})" sonekinden way kimliğini ve
        (varsa) nokta indeksini ayıklar; sonek yoksa `None` döner (ör.
        LLM/kullanıcı kaynaklı, OSM'den gelmeyen bir düğüm)."""
        eslesme = cls._OSM_WAY_INDEX_DESENI.search(isim or "")
        if not eslesme:
            return None
        idx_str = eslesme.group(2)
        return eslesme.group(1), (int(idx_str) if idx_str is not None else None)

    def _yerel_acik_yol_agini_getir(self, enlem: float, boylam: float, yaricap_km: float) -> List[Dict[str, Any]]:
        """Verilen merkezin `yaricap_km` çevresindeki, SADECE AÇIK
        (`acik_mi = true` — kapalı olanlar KESİNLİKLE hariç) TÜM Street/
        Bridge noktalarını, POINT INDEX kullanan `point.distance()` ile
        çeker (bkz. `ensure_spatial_indexes`).

        NEDEN "ANA DAMAR" İLE SINIRLANDIRILMADI:
        İlk sürüm sadece ana damar (motorway/trunk/primary/secondary)
        noktalarını kullanıyordu — bu, ULUSAL ÖLÇEKTE performans için
        cazip görünse de, gerçek Kömürhan Köprüsü kapanma senaryosunda
        yerel ağı 41/36.832 düğümlük anlamsız bir mikro-adaya
        PARÇALADI: kırsal Türkiye'de iki ana damar genellikle
        BİRBİRİNE DOĞRUDAN DEĞİL, aradaki yerel/tali (residential/
        tertiary/unclassified) yollar ÜZERİNDEN bağlanır — tam olarak bu
        "bağlayıcı doku" ana damar filtresiyle dışarıda bırakılıyordu.
        TAM ağ kullanmak (bu fonksiyon), yarıçapı `_YOL_AGI_ARAMA_
        YARICAPI_KM` ile makul bir sınırda (25 km) tutarak performansı
        dengeler (bkz. `en_yakin_ulasilan_varliklari_bul`).

        "KESİN SINIR" NOTU ("sorgular çok derine
        inip binlerce düğüm dönmesin" ilkesi geçerlidir): bu sorguya KASITLI OLARAK küçük
        bir `LIMIT 5/10` KONULMADI — yukarıdaki "ANA DAMAR" notundaki
        AYNI ders burada da geçerlidir: yol ağı grafının
        rastgele bir alt kümesini (ör. sadece en yakın 10 nokta) kesip
        atmak, `_yol_agi_kur`ın ihtiyaç duyduğu ARDIŞIK way-nokta
        zincirini/kesişim dokusunu PARÇALAR ve Dijkstra'yı yanlışlıkla
        "hiçbir birim ulaşılamıyor" sonucuna düşürebilir — TAM OLARAK
        Kömürhan Köprüsü senaryosunun tekrarı olurdu. Asıl KAPSAM
        SINIRI zaten `yaricap_km` (`_YOL_AGI_ARAMA_YARICAPI_KM`/
        `_YETENEK_ARAMA_YARICAPI_KM`) ile mevcut ve ampirik olarak
        ölçülüp seçilmiştir (bkz. yukarısı: 25 km'de ~87 bin nokta/~6 sn
        kabul edilebilir, 50 km'de ~364 bin nokta/~30 sn SADECE nadir
        "yetenek bulunamadı" geri düşüşünde göze alınır).

        Yine de GERÇEK bir "kesin sınır" — sırf düzeltici, normal
        işleyişte HİÇ devreye girmeyen bir GÜVENLİK VALFİ olarak —
        `_YOL_AGI_DUGUM_GUVENLIK_LIMITI` eklendi: en yakın (merkeze en
        yakın, `ORDER BY` ile) bu sayıdaki nokta ile sınırlanır. Değer
        (150.000), belgelenmiş en kötü GERÇEK durumun (364 bin, 50 km'de)
        ÜZERİNDE bilerek tutulmadı ama onun ~%40'ı civarında, GERÇEK
        veriyle ASLA tetiklenmeyecek ama bozuk/anormal-yoğun bir veri
        seti (ör. hatalı bir OSM içe aktarımı) durumunda sistemi
        SONSUZA DEK beklemekten koruyacak bir üst sınırdır.

        "KESİN İNDEKS" DÜZELTMESİ (`EXPLAIN`
        İLE DOĞRULANMIŞ BULGU): bu sorgu `MATCH (n) WHERE (n:Street
        OR n:Bridge) ...` şeklindeyken (önceki sürüm), Neo4j'nin
        planlayıcısı `ensure_spatial_indexes`in `Infrastructure(konum)`
        üzerinde kurduğu POINT INDEX'i KULLANMIYORDU — çünkü sorgu
        DOĞRUDAN `:Street`/`:Bridge` etiketleri üzerinden eşleştiriyordu,
        `n`nin AYRICA `:Infrastructure` taşıdığını planlayıcı BİLEMEZ
        (Neo4j'de etiketler arası bir alt-tip ilişkisi TANIMLI değildir). Sonuç:
        `UnionNodeByLabelsScan n:Street|Bridge` — yani her çağrıda TÜM
        Street+Bridge düğümleri (ulusal ölçekte yüz binlerce) TARANIYOR,
        index HİÇ İŞE YARAMIYORDU (`EXPLAIN` ile doğrulandı). `MATCH
        (n:Infrastructure) WHERE (n:Street OR n:Bridge) ...` YAZILDIĞINDA
        (her Street/Bridge düğümü `BaseNode.neo4j_labels()` gereği ZATEN
        `:Infrastructure`yi de taşıdığından SONUÇ AYNIDIR, sadece
        planlayıcıya ipucu eklenir) plan `NodeIndexSeekByRange | POINT
        INDEX n:Infrastructure(konum)`e döner — `EXPLAIN` ile
        doğrulandı. `point.distance(...) <= $yaricap_metre` KOŞULUNUN
        AYRI bir `WITH` ile hesaplanan bir değişkene DEĞİL, DOĞRUDAN
        `WHERE`de yazılması ZORUNLUDUR — index-seek optimizasyonu SADECE
        bu sözdizimsel deseni tanır (bu da `EXPLAIN` ile ayrıca
        doğrulanmış, ince ama KRİTİK bir Cypher planlayıcı davranışıdır);
        `ORDER BY` için mesafe yine de gerektiğinden, AYNI ifade bilinçli
        olarak İKİNCİ KEZ bir `WITH`te hesaplanır (ucuz bir aritmetik
        tekrarı, kaçınılmaz taramaya kıyasla önemsizdir)."""
        return self.execute_query(
            "MATCH (n:Infrastructure) WHERE (n:Street OR n:Bridge) AND n.acik_mi = true "
            "AND n.konum IS NOT NULL "
            "AND point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre "
            "WITH n, point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "AS mesafe_metre "
            "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam "
            "ORDER BY mesafe_metre ASC LIMIT $dugum_limiti",
            {
                "enlem": enlem, "boylam": boylam, "yaricap_metre": yaricap_km * 1000.0,
                "dugum_limiti": self._YOL_AGI_DUGUM_GUVENLIK_LIMITI,
            },
            write=False,
        )

    @classmethod
    def _yol_agi_kur(
        cls, noktalar: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, List[Tuple[str, float]]], Dict[str, Tuple[float, float]]]:
        """`noktalar` (bkz. `_yerel_acik_yol_agini_getir`) listesinden,
        yukarıdaki modül-üstü yorumda açıklanan İKİ kuralla (aynı way'in
        ardışık noktaları + farklı way'lerin yakın kesişimleri) bellek-içi
        bir komşuluk listesi (ağırlıklı, km cinsinden) kurar. Döner:
        `(komsuluk {isim: [(komsu_isim, agirlik_km), ...]}, konum {isim: (enlem, boylam)})`.
        """
        konum: Dict[str, Tuple[float, float]] = {n["isim"]: (n["enlem"], n["boylam"]) for n in noktalar}
        komsuluk: Dict[str, List[Tuple[str, float]]] = {isim: [] for isim in konum}

        # 1) Ayni OSM way'ine ait ARDISIK noktalari bagla (gercek güzergah).
        way_gruplari: Dict[str, List[Tuple[int, str]]] = {}
        for isim in konum:
            ayiklanan = cls._osm_way_ve_indeks_ayikla(isim)
            if ayiklanan is None or ayiklanan[1] is None:
                continue
            way_id, idx = ayiklanan
            way_gruplari.setdefault(way_id, []).append((idx, isim))

        for elemanlar in way_gruplari.values():
            elemanlar.sort(key=lambda t: t[0])
            for (idx1, isim1), (idx2, isim2) in zip(elemanlar, elemanlar[1:]):
                if idx2 - idx1 != 1:
                    continue  # aralarinda (yaricap disinda kalmis) bir nokta eksik; baglama.
                agirlik = cls._geodesic_km(konum[isim1], konum[isim2])
                komsuluk[isim1].append((isim2, agirlik))
                komsuluk[isim2].append((isim1, agirlik))

        # 2) Farkli way'lerin YAKIN noktalarini (kesisim) uzamsal-izgara
        # (spatial grid) ile bagla — O(n) civarinda, all-pairs O(n^2) DEGIL.
        HUCRE_DERECE = 0.001  # ~111 m'lik izgara hucresi (kesisim toleransindan buyuk).
        izgara: Dict[Tuple[int, int], List[str]] = {}
        for isim, (enlem, boylam) in konum.items():
            hucre = (int(enlem / HUCRE_DERECE), int(boylam / HUCRE_DERECE))
            izgara.setdefault(hucre, []).append(isim)

        kontrol_edilmis: set = set()
        for (cx, cy), bu_hucredekiler in izgara.items():
            komsu_adaylari: List[str] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    komsu_adaylari.extend(izgara.get((cx + dx, cy + dy), []))
            for isim1 in bu_hucredekiler:
                way1 = cls._osm_way_ve_indeks_ayikla(isim1)
                way1_id = way1[0] if way1 else None
                for isim2 in komsu_adaylari:
                    if isim1 >= isim2:
                        continue
                    cift = (isim1, isim2)
                    if cift in kontrol_edilmis:
                        continue
                    kontrol_edilmis.add(cift)
                    way2 = cls._osm_way_ve_indeks_ayikla(isim2)
                    way2_id = way2[0] if way2 else None
                    if way1_id is not None and way1_id == way2_id:
                        continue  # ayni way, 1. adimda zaten baglandi.
                    agirlik_km = cls._geodesic_km(konum[isim1], konum[isim2])
                    if agirlik_km * 1000.0 <= cls._KESISIM_TOLERANSI_METRE:
                        komsuluk[isim1].append((isim2, agirlik_km))
                        komsuluk[isim2].append((isim1, agirlik_km))

        return komsuluk, konum

    @staticmethod
    def _dijkstra_en_kisa_mesafeler(
        komsuluk: Dict[str, List[Tuple[str, float]]], kaynak_isim: str
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
        """Klasik tek-kaynaklı Dijkstra (heapq ile): `kaynak_isim`den TÜM
        ulaşılabilir düğümlere GERÇEK en kısa mesafeyi (km) hesaplar VE
        (Görsel C4ISR — Taktiksel Sevk Katmanı için) her düğümün en kısa
        yoldaki BİR ÖNCEKİ düğümünü kaydeder ki gerçek güzergah, harita
        katmanı için SONRADAN adım adım yeniden inşa edilebilsin (bkz.
        `_yolu_yeniden_insa_et`). Ulaşılamayan (ör. kapalı yollarla İZOLE
        olmuş) düğümler mesafe sözlüğünde HİÇ YER ALMAZ — "yol yok" bu
        şekilde temsil edilir. Döner: `(mesafeler, onceki)`."""
        mesafeler: Dict[str, float] = {kaynak_isim: 0.0}
        onceki: Dict[str, str] = {}
        pq: List[Tuple[float, str]] = [(0.0, kaynak_isim)]
        ziyaret_edildi: set = set()
        while pq:
            mesafe, dugum = heapq.heappop(pq)
            if dugum in ziyaret_edildi:
                continue
            ziyaret_edildi.add(dugum)
            for komsu, agirlik in komsuluk.get(dugum, []):
                yeni_mesafe = mesafe + agirlik
                if yeni_mesafe < mesafeler.get(komsu, float("inf")):
                    mesafeler[komsu] = yeni_mesafe
                    onceki[komsu] = dugum
                    heapq.heappush(pq, (yeni_mesafe, komsu))
        return mesafeler, onceki

    @staticmethod
    def _yolu_yeniden_insa_et(
        onceki: Dict[str, str], konum: Dict[str, Tuple[float, float]], kaynak_isim: str, hedef_isim: str
    ) -> List[Tuple[float, float]]:
        """Görsel C4ISR — Taktiksel Sevk Katmanı: `_dijkstra_en_kisa_
        mesafeler`in ürettiği `onceki` (önceki-düğüm) haritasını hedeften
        kaynağa doğru geriye sararak, gerçek güzergahı OLUŞTURAN sıralı
        `(enlem, boylam)` noktalar listesini üretir (kaynaktan hedefe
        doğru sıralı). `hedef_isim == kaynak_isim` ise tek noktalık bir
        liste döner. Bir tutarsızlık (kırık zincir) durumunda BOŞ liste
        döner — çağıran taraf bunu "güzergah çizilemedi" olarak yorumlar,
        HİÇBİR ZAMAN yarım/hatalı bir çizgi ÜRETMEZ."""
        if hedef_isim == kaynak_isim:
            return [konum[kaynak_isim]] if kaynak_isim in konum else []
        yol: List[Tuple[float, float]] = []
        dugum = hedef_isim
        ziyaret_edildi: set = set()
        while dugum != kaynak_isim:
            if dugum in ziyaret_edildi or dugum not in konum:
                return []  # dongu/tutarsizlik guvenlik agi.
            ziyaret_edildi.add(dugum)
            yol.append(konum[dugum])
            onceki_dugum = onceki.get(dugum)
            if onceki_dugum is None:
                return []
            dugum = onceki_dugum
        yol.append(konum[kaynak_isim])
        yol.reverse()
        return yol

    def _en_yakin_ulasilabilir_noktayi_bul(
        self,
        baslangic_konum: Tuple[float, float],
        mesafeler: Dict[str, float],
        konum: Dict[str, Tuple[float, float]],
        maks_mesafe_km: float = _KANCA_MAKS_MESAFE_KM,
    ) -> Optional[Tuple[str, float]]:
        """"KANCA ALGORİTMASI" (Snap to Connected Component — bkz.
        `_KANCA_MAKS_MESAFE_KM` docstring'i): `baslangic_konum`a en yakın,
        `mesafeler` içinde (yani kaynaktan Dijkstra ile ZATEN ulaşılabilir
        olduğu KANITLANMIŞ) olan noktayı arar. `maks_mesafe_km`nin dışında
        kalırsa (bu artık heuristik bir veri boşluğu değil, GERÇEK bir
        fiziksel kopukluk/kapanma OLABİLECEĞİNDEN) `None` döner — çağıran
        taraf bu durumda hedefi dürüstçe "erişilemez" saymaya DEVAM eder;
        bu fonksiyon ASLA sınırsız bir mesafede "en yakını her ne olursa
        olsun bağla" YAPMAZ."""
        en_yakin_isim: Optional[str] = None
        en_yakin_km = float("inf")
        for isim in mesafeler:
            k = konum.get(isim)
            if k is None:
                continue
            m = self._geodesic_km(baslangic_konum, k)
            if m < en_yakin_km:
                en_yakin_km, en_yakin_isim = m, isim
        if en_yakin_isim is None or en_yakin_km > maks_mesafe_km:
            return None
        return en_yakin_isim, en_yakin_km

    _YOL_AGI_ONBELLEK_SANIYE = 30.0
    """"Yetenek Bazlı Rota" (Capability-Based Routing) geregi
    `decision_engine.py` AYNI olay yeri için ARDI ARDINA
    BİRDEN FAZLA arama yapabilir (önce doğru yetenekli birim, sonra AYRI
    bir sorguyla güvenlik/Emniyet birliği — bkz. `DecisionEngine.
    _guvenlik_birlikleri_bul`). Pahalı yerel yol ağı inşasını (~6-8 sn)
    HER ARAMADA TEKRARLAMAMAK için, aynı `(enlem, boylam,
    yarıçap)` üçlüsü için kısa süreliğine önbelleklenir — bkz. `Neo4jConnection.
    get_real_koordinat_zarfi`daki AYNI önbellekleme deseni."""

    def _yerel_yol_agi_ve_kaynak_getir(
        self, event_enlem: float, event_boylam: float, yol_agi_yaricapi_km: float
    ) -> Optional[
        Tuple[
            Dict[str, List[Tuple[str, float]]],
            Dict[str, Tuple[float, float]],
            Dict[str, float],
            Dict[str, str],
            str,
        ]
    ]:
        """`_yerel_acik_yol_agini_getir` + `_yol_agi_kur` + `_dijkstra_en_
        kisa_mesafeler`i TEK bir çağrıda birleştirir VE (bkz. `_YOL_AGI_
        ONBELLEK_SANIYE`) kısa süreliğine önbellekler. Döner: `(komsuluk,
        konum, mesafeler, onceki, kaynak_isim)` — grafta hiç açık yol
        noktası yoksa `None`."""
        anahtar = (round(event_enlem, 4), round(event_boylam, 4), yol_agi_yaricapi_km)
        simdi = time.monotonic()
        onbellek_zamani = self._yol_agi_onbellek_zamani.get(anahtar)
        if onbellek_zamani is not None and (simdi - onbellek_zamani) < self._YOL_AGI_ONBELLEK_SANIYE:
            return self._yol_agi_onbellek[anahtar]

        yol_noktalari = self._yerel_acik_yol_agini_getir(event_enlem, event_boylam, yol_agi_yaricapi_km)
        if not yol_noktalari:
            sonuc = None
        else:
            komsuluk, konum = self._yol_agi_kur(yol_noktalari)
            kaynak_isim = min(
                konum, key=lambda isim: self._geodesic_km((event_enlem, event_boylam), konum[isim])
            )
            mesafeler, onceki = self._dijkstra_en_kisa_mesafeler(komsuluk, kaynak_isim)
            sonuc = (komsuluk, konum, mesafeler, onceki, kaynak_isim)

        self._yol_agi_onbellek[anahtar] = sonuc
        self._yol_agi_onbellek_zamani[anahtar] = simdi
        return sonuc

    def en_yakin_ulasilan_varliklari_bul(
        self,
        event_enlem: float,
        event_boylam: float,
        hedef_label: str,
        ilk_n: int = 3,
        yol_agi_yaricapi_km: float = _YOL_AGI_ARAMA_YARICAPI_KM,
        ekstra_where: str = "",
        ekstra_parametreler: Optional[Dict[str, Any]] = None,
        unit_tipleri: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """"MEKANSAL KÖRLÜK" DÜZELTMESİ — bir olay yerinden (`event_enlem`/
        `event_boylam`), `hedef_label` etiketli (ör. "Unit", "Facility")
        varlıklara, SADECE AÇIK yol ağı üzerinden (bkz. modülün başındaki
        mimari not) GERÇEK Dijkstra en-kısa-yol mesafesini hesaplar; kapalı
        bir yoldan geçmek ZORUNDA olan veya yol ağına hiç yeterince yakın
        olmayan varlıklar SESSİZCE ELENİR — sonuçta SADECE gerçekten
        erişilebilir olanlar, GERÇEK km mesafesine göre ARTAN sırada, ilk

        "KANCA ALGORİTMASI" (Snap to Connected Component — bkz. `_KANCA_
        MAKS_MESAFE_KM` docstring'i): bir hedef, heuristik kesişim
        tespitinin (gerçek OSM paylaşılan düğüm ID'leri OLMADIĞI için) bir
        veri boşluğu bırakması yüzünden ana bileşenden İZOLE kalmışsa (ör.
        aynı yolun bölünmüş bir parçası), KISA (<= 1 km) bir "kanca" ile
        önce ulaşılabilir bir noktaya bağlanmaya çalışılır; bu şekilde
        bağlanan sonuçlar `kanca_uygulandi: True` taşır. Bu tolerans
        BİLİNÇLİ OLARAK küçük tutulmuştur — gerçek bir fiziksel kopukluğu
        (ör. kapalı bir köprünün izole ettiği taraf) YANLIŞLIKLA
        "düzeltmez"; öyle bir durumda hedef YİNE DE dürüstçe elenir.
        `ilk_n` tanesi döner.

        Her sonuç satırı: `{isim, enlem, boylam, ..., mesafe_km,
        erisilebilir: True, rota_noktalari: [(enlem, boylam), ...]}`
        (varlığın orijinal alanları + mesafe/erişilebilirlik + GÖRSEL
        C4ISR "Taktiksel Sevk Katmanı" için gerçek güzergahı OLUŞTURAN
        sıralı nokta listesi — olay yerinden hedefe doğru; bkz.
        `_yolu_yeniden_insa_et`). Hiçbir GERÇEK erişilebilir varlık yoksa
        BOŞ liste döner (çağıran taraf bunu "hiçbir birim ulaşamıyor"
        olarak yorumlamalıdır — bkz. `decision_engine.py`).

        "YETENEK BAZLI ROTA" (Capability-Based Routing): `unit_tipleri`
        verilirse (SADECE `hedef_label="Unit"`
        için anlamlıdır; ör. `["Agir Muhendislik", "AFAD"]`), sonuçlar
        `h.unit_type IN unit_tipleri` ile SINIRLANIR. Bunsuz sistem SADECE
        mesafeye bakıyordu — bir köprü/altyapı krizinde 7 km'deki bir Polis
        birimi, 10 km'deki tek Ağır Mühendislik (vinç/dozer) biriminin
        ÖNÜNE geçip LLM'e SADECE Polis'i gösteriyordu (LLM de mecburen
        vinç yerine polis öneriyordu). `unit_tipleri` ile arama artık
        "mesafede en yakın HERHANGİ bir birim" DEĞİL, "mesafede en yakın
        DOĞRU YETENEĞE SAHİP birim" sorusuna cevap verir."""
        yol_agi = self._yerel_yol_agi_ve_kaynak_getir(event_enlem, event_boylam, yol_agi_yaricapi_km)
        if yol_agi is None:
            logger.warning(
                "en_yakin_ulasilan_varliklari_bul: (%.4f, %.4f) cevresinde %.0f km icinde "
                "ACIK ana damar noktasi bulunamadi; erisilebilirlik hesaplanamiyor.",
                event_enlem, event_boylam, yol_agi_yaricapi_km,
            )
            return []
        komsuluk, konum, mesafeler, onceki, kaynak_isim = yol_agi

        parametreler: Dict[str, Any] = dict(ekstra_parametreler or {})
        parametreler.update({
            "enlem": event_enlem, "boylam": event_boylam,
            "yaricap_metre": yol_agi_yaricapi_km * 1000.0,
        })
        tip_kosulu = ""
        if unit_tipleri:
            tip_kosulu = " AND h.unit_type IN $unit_tipleri"
            parametreler["unit_tipleri"] = list(unit_tipleri)
        # "KESİN SINIR" DÜZELTMESİ ("en yakın birim/tesis
        # bulma sorgularına LIMIT ekle" ilkesi geregi): bu sorgu ÖNCEDEN limitsizdi —
        # yoğun bir bölgede (ör. büyükşehir) `yaricap_km` içinde binlerce
        # `h` düğümü dönebilir, HER BİRİ için aşağıdaki Python döngüsü
        # `konum` (yerel yol ağındaki HER nokta) ile mesafe hesaplar — yani
        # maliyet `hedef_sayisi × yol_agi_nokta_sayisi` ile ÇARPIMSAL
        # büyür. Doğru sonucu BOZMADAN sınırlamak için: adaylar ÖNCE
        # gerçek düz-hat mesafesine göre SIRALANIR (`ORDER BY ... ASC`),
        # SONRA `ilk_n`in KAT FAZLASI (`_HEDEF_ADAY_COKLUGU_KATSAYISI`)
        # kadarı çekilir — nihai `ilk_n` sonuç zaten en yakın adaylardan
        # seçildiği için bu, `_en_yakin_acik_yollari_bul`/`_en_yakin_
        # aktif_tesisleri_bul`daki (decision_engine.py) AYNI "sırala,
        # kat-fazlası çek, sonra tekilleştir/filtrele" desenidir — pratikte
        # sonucu DEĞİŞTİRMEZ (en uzaktaki, zaten seçilmeyecek adaylar elenir),
        # sadece Python tarafındaki taramayı sınırlar.
        aday_limiti = max(ilk_n * self._HEDEF_ADAY_COKLUGU_KATSAYISI, self._HEDEF_ADAY_MIN_LIMITI)
        parametreler["aday_limiti"] = aday_limiti
        # "KESİN İNDEKS" DÜZELTMESİ (`EXPLAIN` İLE
        # DOĞRULANDI): `point.distance(...) <= $yaricap_metre` KOŞULU
        # DOĞRUDAN `WHERE`de yazılmalıdır — bir `WITH` ile hesaplanan ARA
        # değişkene taşınırsa (önceki sürümdeki hata) Neo4j'nin planlayıcısı
        # `ensure_spatial_indexes`teki POINT INDEX'i KULLANMAZ, tam bir
        # `NodeByLabelScan`a düşer (bkz. `_yerel_acik_yol_agini_getir`
        # docstring'indeki AYNI keşif). `tip_kosulu`/`ekstra_where`
        # (basit eşitlik/IN filtreleri) index-seek'i BOZMADAN AYNI WHERE'e
        # eklenebilir — index taramadan SONRA ucuz bir post-filter olarak
        # uygulanırlar. `ORDER BY` için mesafe AYRI bir `WITH`te (bilinçli
        # olarak İKİNCİ KEZ) hesaplanır.
        hedefler = self.execute_query(
            f"MATCH (h:{hedef_label}) WHERE h.konum IS NOT NULL "
            "AND point.distance(h.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre" + tip_kosulu + (f" {ekstra_where}" if ekstra_where else "") + " "
            "WITH h, point.distance(h.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "AS duz_hat_mesafe_metre "
            "RETURN h ORDER BY duz_hat_mesafe_metre ASC LIMIT $aday_limiti",
            parametreler,
            write=False,
        )

        sonuclar: List[Dict[str, Any]] = []
        for row in hedefler:
            hedef = dict(row["h"])
            h_enlem, h_boylam = hedef.get("enlem"), hedef.get("boylam")
            if h_enlem is None or h_boylam is None:
                continue
            en_yakin_yol_noktasi, en_yakin_snap_km = None, float("inf")
            for yol_isim, yol_konum in konum.items():
                m = self._geodesic_km((h_enlem, h_boylam), yol_konum)
                if m < en_yakin_snap_km:
                    en_yakin_snap_km, en_yakin_yol_noktasi = m, yol_isim
            if en_yakin_yol_noktasi is None or en_yakin_snap_km > self._HEDEF_SNAP_YARICAPI_KM:
                continue  # ana damar agina yeterince yakin degil, guvenilir mesafe YOK.

            if en_yakin_yol_noktasi not in mesafeler:
                # "KANCA ALGORİTMASI" (Snap to Connected Component — CANLI
                # HATA DÜZELTMESİ): heuristik kesisim tespiti bu noktayi
                # ana bilesenden IZOLE etmis olabilir (ör. ayni yolun
                # bolunmus bir parcasi — bkz. `_KANCA_MAKS_MESAFE_KM`
                # docstring'i). KORU/ELEME KARARINDAN ONCE, KISA (<= 1 km)
                # bir kanca ile GERCEKTEN kaynaktan ulasilabilir bir noktaya
                # baglanip baglanamayacagi denenir.
                kanca = self._en_yakin_ulasilabilir_noktayi_bul(konum[en_yakin_yol_noktasi], mesafeler, konum)
                if kanca is None:
                    # KANCA ICIN DE COK UZAK: bu artik GERCEK bir kopukluk
                    # (ör. kapali bir koprunun izole ettigi taraf) OLABILIR;
                    # dürüstçe "erişilemez" sayılmaya devam edilir.
                    logger.info(
                        "'%s' olay yerinden ACIK yol agiyla ERISILEMEZ (kapali yol(lar) izole "
                        "ediyor; %.1f km'lik kanca toleransi icinde de baglanti bulunamadi).",
                        hedef.get("isim"), self._KANCA_MAKS_MESAFE_KM,
                    )
                    continue
                kanca_isim, kanca_km = kanca
                logger.info(
                    "'%s' icin KANCA uygulandi: '%s' heuristik olarak izoleydi, '%s'e %.0f m'lik "
                    "bir baglantiyla (ayni yolun bolunmus parcasi varsayimiyla) baglandi.",
                    hedef.get("isim"), en_yakin_yol_noktasi, kanca_isim, kanca_km * 1000,
                )
                hedef["mesafe_km"] = round(mesafeler[kanca_isim] + kanca_km + en_yakin_snap_km, 1)
                hedef["erisilebilir"] = True
                hedef["kanca_uygulandi"] = True
                rota = self._yolu_yeniden_insa_et(onceki, konum, kaynak_isim, kanca_isim)
                if rota:
                    rota = rota + [konum[en_yakin_yol_noktasi], (h_enlem, h_boylam)]
                hedef["rota_noktalari"] = rota
                sonuclar.append(hedef)
                continue

            hedef["mesafe_km"] = round(mesafeler[en_yakin_yol_noktasi] + en_yakin_snap_km, 1)
            hedef["erisilebilir"] = True
            hedef["kanca_uygulandi"] = False
            # Görsel C4ISR — Taktiksel Sevk Katmanı: olay yerinden (kaynak),
            # ağdaki gerçek güzergahı izleyip hedefin snap noktasına kadar
            # giden yol, ARDINDAN o snap noktasından hedefin KENDİ gerçek
            # koordinatına (son adım — "son 1 km" bağlantısı) uzanan tam
            # nokta listesi. Yeniden inşa BAŞARISIZ olursa (tutarsızlık),
            # boş liste kalır — çağıran taraf (bkz. `app.py`) bunu
            # "çizilecek güzergah yok" olarak ele alır, HİÇBİR ZAMAN
            # hatalı/yarım bir çizgi ÜRETMEZ.
            rota = self._yolu_yeniden_insa_et(onceki, konum, kaynak_isim, en_yakin_yol_noktasi)
            if rota:
                rota = rota + [(h_enlem, h_boylam)]
            hedef["rota_noktalari"] = rota
            sonuclar.append(hedef)

        sonuclar.sort(key=lambda s: s["mesafe_km"])
        return sonuclar[:ilk_n]

    # ------------------------------------------------------------------ #
    # CRUD: Düğüm (Node)
    # ------------------------------------------------------------------ #

    def add_node(self, node: BaseNode) -> Dict[str, Any]:
        """Bir düğümü ekler veya (isim eşleşiyorsa/bulanık eşleşiyorsa) günceller.

        Yazmadan önce `node.isim`, aynı etiketteki mevcut isimlerle bulanık
        (fuzzy) olarak karşılaştırılır (bkz. `_resolve_canonical_isim`); yakın
        bir eşleşme varsa yeni düğüm açmak yerine o mevcut düğüm güncellenir.
        """
        canonical_isim = self._resolve_canonical_isim(node.isim, label=node.neo4j_label.value)
        query, parameters = self.node_to_cypher(node, merge_isim=canonical_isim)
        result = self.execute_query(query, parameters, write=True)
        return result[0] if result else {}

    def add_nodes(self, nodes: Sequence[BaseNode]) -> None:
        """Birden fazla düğümü tek tek MERGE eder."""
        for node in nodes:
            self.add_node(node)

    def update_infrastructure_status_by_name(
        self, aranan_isim: str, guncellemeler: Dict[str, Any], label: str = "Infrastructure"
    ) -> Tuple[int, Optional[str]]:
        """"KESİN VARLIK EŞLEŞTİRME" (No Ghost Infrastructure): kullanıcı/LLM
        kaynaklı bir altyapı ismini (ör. "Kömürhan Köprüsü", "valifahribey
        caddesi"), GERÇEK OSM verisinden yüklenmiş `label` (varsayılan
        "Infrastructure" — TÜM alt-tipleri, Sokak+Köprü+Karayolu+Tünel+
        Viyadük birlikte, TEK sorguda kapsar) düğümlerinin `aciklama`
        alanındaki GERÇEK isimle bulanık (fuzzy, `_altyapi_adi_anahtari`)
        eşleştirir ve eşleşen TÜM nokta-düğümleri (aynı caddenin/köprünün
        OSM'de onlarca ayrı düğümü olabilir — bkz. `real_osm_loader.
        RealOsmLoader.build_nodes`) TEK bir toplu `SET` ile `guncellemeler`
        (ör. `{"acik_mi": False, "durum": "Hasarli"}`) ile günceller.

        İSİM ÇAKIŞMASI DÜZELTMESİ SONRASI GENİŞLETME (bkz. `src.ui.app.
        _osm_kaynakli_gercek_varlik_mi`): fonksiyon ismi tarihseldir
        ("Infrastructure") ama `label` PARAMETRESİ ZATEN BAŞTAN BERİ
        GENEL-AMAÇLIYDI — `local_osm_reader._amenity_dugumu_uret`
        Facility/Unit (hastane/karakol/itfaiye) için de AYNI `(OSM tip/ID)`
        sonek + `aciklama`=temiz-ad sözleşmesini benimsedikten SONRA, bu
        fonksiyon `label="Facility"`/`label="Unit"` ile ÇAĞRILARAK AYNI
        "gerçek varlığı bul, ghost yaratma" korumasını onlar için de sağlar
        — `_altyapi_adi_anahtari`nin cadde/sokak/bulvar kelime-atma mantığı
        hastane/karakol adlarında zararsız bir no-op'tur (bu kelimeler o
        isimlerde zaten geçmez).

        Kanıtlanmış bir boşluk için savunma (Ghost Infrastructure): Bu
        fonksiyon YOKKEN, LLM'in ürettiği "Kömürhan Köprüsü" ismi genel `add_node`/`_resolve_
        canonical_isim` (0.85 eşikli, TÜM `Infrastructure` etiketine karşı)
        mekanizmasından GEÇEMİYORDU çünkü gerçek OSM köprü düğümünün `isim`i
        bir teknik sonek taşır (ör. "Kömürhan Köprüsü (OSM way/987654321)")
        ve bu uzunluk farkı fuzzy oranını eşiğin ALTINA düşürüyordu; sonuç
        olarak sistem, GERÇEK köprü zaten grafta varken, LLM'in ÜRETTİĞİ
        (haritanın rastgele bir yerine düşen) SAHTE koordinatlarla İKİNCİ,
        HAYALİ bir Infrastructure düğümü ("ghost node") yaratıyordu.

        YENİ bir düğüm OLUŞTURMAZ — amaç, LLM'in "Kömürhan Köprüsü" için
        sahte/rastgele koordinatlı bir kopya düğüm yaratması YERİNE, gerçek
        OSM altyapı ağı içindeki GERÇEK düğümü bulup güncellemesidir.
        Eşleşme bulunamazsa (0 döner), çağıran taraf (bkz. `src.ui.app.
        _grafa_yaz`) OSM-kaynaklı tipler (Sokak/Köprü) için ARTIK YENİ bir
        ghost düğüm AÇMAZ (bkz. o fonksiyonun "NO GHOST INFRASTRUCTURE"
        yorumu) — SADECE OSM'de HİÇ karşılığı olmayan, doğrudan LLM'in
        kendisinin ürettiği tipler (Karayolu/Tünel/Viyadük gibi bu sistemde
        henüz OSM'den YÜKLENMEYEN tipler) için `add_node` ile meşru şekilde
        yeni bir düğüm açılmaya devam eder.

        Returns:
            `(guncellenen_dugum_sayisi, temsili_gercek_isim)` — eşleşme
            yoksa `(0, None)`. `temsili_gercek_isim`, eşleşen GERÇEK
            düğümlerden BİRİNİN `isim` alanıdır (`aciklama` DEĞİL — ör.
            "Kömürhan Köprüsü (OSM way/987654321)"). Çağıran taraf (bkz.
            `src.ui.app._grafa_yaz`) bunu, LLM'in ürettiği TEMİZ isme
            (`kaynak_id`/`hedef_id`) referans veren `relationships`
            kayıtlarını bu GERÇEK isme YENİDEN eşlemek için kullanır: aksi
            halde `Event -[AFFECTS]-> Köprü` ilişkisi, hiçbir düğümün
            `isim`i LLM'in ürettiği temiz isimle TAM eşleşmediğinden
            (gerçek düğümler hep bir OSM soneki taşır) SESSİZCE
            kurulamazdı — bkz. sorun tanımı: "Son Durum Raporu"
            tablosundaki "Etkilenen Varlıklar" sütununun boş kalması.
        """
        hedef_anahtar = _altyapi_adi_anahtari(aranan_isim)
        if not hedef_anahtar:
            return 0, None

        # "FULL TABLE SCAN" DÜZELTMESİ (bkz. `_resolve_
        # canonical_isim`daki AYNI sınıf düzeltme/gerekçe): asıl DISTINCT+
        # Python-taraması pahalı yola girmeden ÖNCE, `aciklama` üzerindeki
        # (bkz. `ensure_property_indexes`) İNDEKS-DESTEKLİ bir LİTERAL
        # eşitlik denenir. Bu, EN SIK rastlanan durumu (LLM'in ürettiği isim
        # gerçek `aciklama` değeriyle harfiyen AYNI, ör. "Kömürhan Köprüsü")
        # 975.000+ düğümlük `Infrastructure` etiketini HİÇ TARAMADAN, tek bir
        # indeks aramasıyla çözer. Bulunamazsa (BÜYÜK/KÜÇÜK harf farkı, "cadde"
        # ekinin eksikliği gibi normalize edilmesi gereken varyasyonlar) alt
        # taraftaki eski DISTINCT+anahtar/fuzzy akışına DOKUNULMADAN düşülür.
        tam_esitlik_sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) RETURN n.aciklama AS ad LIMIT 1",
            {"ad": aranan_isim},
            write=False,
        )
        if tam_esitlik_sonuc:
            return self._infrastructure_status_guncelle_ve_temsili_isim_dondur(
                label, tam_esitlik_sonuc[0]["ad"], guncellemeler
            )

        adaylar = self.execute_query(
            f"MATCH (n:{label}) WHERE n.aciklama IS NOT NULL RETURN DISTINCT n.aciklama AS ad",
            write=False,
        )
        if not adaylar:
            return 0, None

        # 1. gecis: TAM anahtar eslesmesi (fuzzy orana bile gerek kalmadan
        # cogu durumda yeterlidir, bkz. `_altyapi_adi_anahtari` docstring'i).
        eslesen_ad: Optional[str] = None
        for row in adaylar:
            if _altyapi_adi_anahtari(row["ad"]) == hedef_anahtar:
                eslesen_ad = row["ad"]
                break

        # 2. gecis (SADECE tam eslesme bulunamazsa): en yuksek difflib
        # oranina sahip adayi bul; esigin (FUZZY_ALTYAPI_ESIK) ALTINDAYSA
        # eslesme YOK sayilir (0 donulur — cagiran taraf artik GHOST NODE
        # ACMAZ, bkz. yukaridaki docstring).
        if eslesen_ad is None:
            en_iyi_oran = 0.0
            en_iyi_aday: Optional[str] = None
            for row in adaylar:
                aday_ad = row["ad"]
                oran = difflib.SequenceMatcher(None, hedef_anahtar, _altyapi_adi_anahtari(aday_ad)).ratio()
                if oran > en_iyi_oran:
                    en_iyi_oran = oran
                    en_iyi_aday = aday_ad
            if en_iyi_oran < FUZZY_ALTYAPI_ESIK:
                logger.info(
                    "Altyapi eslesmesi bulunamadi: '%s' (en yakin aday='%s', oran=%.2f, esik=%.2f)",
                    aranan_isim, en_iyi_aday, en_iyi_oran, FUZZY_ALTYAPI_ESIK,
                )
                return 0, None
            eslesen_ad = en_iyi_aday

        logger.info("Altyapi eslesmesi bulundu: '%s' -> '%s' (GERCEK OSM adi)", aranan_isim, eslesen_ad)
        return self._infrastructure_status_guncelle_ve_temsili_isim_dondur(label, eslesen_ad, guncellemeler)

    def _infrastructure_status_guncelle_ve_temsili_isim_dondur(
        self, label: str, eslesen_ad: str, guncellemeler: Dict[str, Any]
    ) -> Tuple[int, Optional[str]]:
        """`update_infrastructure_status_by_name`nin, bir `aciklama` eşleşmesi
        (TAM İNDEKS-DESTEKLİ eşitlik VEYA anahtar/fuzzy taramasından) BULUNDUKTAN
        SONRAKİ ORTAK kuyruğu: eşleşen TÜM gerçek düğümleri toplu `SET` ile
        günceller, sonra çağıran tarafın ilişki-yeniden-eşlemesi (bkz. o
        fonksiyonun docstring'i) için temsili GERÇEK `isim`i döner. İKİ ayrı
        eşleşme yolunun (hızlı eşitlik / yavaş fuzzy) AYNI kodu ÇAĞIRMASI,
        `SET`/`RETURN` sorgularının tek bir yerde kalmasını sağlar."""
        sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) SET n += $props RETURN count(n) AS adet",
            {"ad": eslesen_ad, "props": guncellemeler},
            write=True,
        )
        guncellenen = sonuc[0]["adet"] if sonuc else 0
        if not guncellenen:
            return 0, None

        temsili_sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) RETURN n.isim AS isim LIMIT 1",
            {"ad": eslesen_ad},
            write=False,
        )
        temsili_isim = temsili_sonuc[0]["isim"] if temsili_sonuc else None
        return guncellenen, temsili_isim

    def get_node(
        self, node_id: str, label: Optional[NodeLabel] = None
    ) -> Optional[Dict[str, Any]]:
        """Id'ye göre tek bir düğüm getirir. `label` verilirse arama o etikete kısıtlanır."""
        label_clause = f":{label.value}" if label else ""
        query = f"MATCH (n{label_clause} {{id: $id}}) RETURN n"
        result = self.execute_query(query, {"id": node_id}, write=False)
        return result[0]["n"] if result else None

    def find_nodes(
        self, label: NodeLabel, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Etikete (ve opsiyonel eşitlik filtrelerine) göre düğümleri listeler."""
        filters = filters or {}
        where_clause = ""
        if filters:
            conditions = " AND ".join(f"n.{key} = ${key}" for key in filters)
            where_clause = f" WHERE {conditions}"
        query = f"MATCH (n:{label.value}){where_clause} RETURN n"
        result = self.execute_query(query, filters, write=False)
        return [record["n"] for record in result]

    def delete_node(self, node_id: str, detach: bool = True) -> None:
        """Düğümü siler. `detach=True` ise bağlı tüm ilişkileri de kaldırır (DETACH DELETE)."""
        detach_clause = "DETACH " if detach else ""
        query = f"MATCH (n {{id: $id}}) {detach_clause}DELETE n"
        self.execute_query(query, {"id": node_id}, write=True)

    # ------------------------------------------------------------------ #
    # CRUD: İlişki (Relationship / Edge)
    # ------------------------------------------------------------------ #

    def add_relationship(self, relationship: Relationship) -> Dict[str, Any]:
        """İki mevcut düğüm arasında ilişki ekler veya günceller.

        Uç noktaların isimleri (`kaynak_id`/`hedef_id`), eşleştirmeden önce
        bulanık (fuzzy) olarak kanonik isme çözümlenir (bkz.
        `_resolve_canonical_isim`, `label=None` — ilişki uç noktalarının
        etiketi bilinmediğinden tüm düğümler arasında aranır); böylece bir
        önceki adımda `add_node` tarafından farklı bir isim varyasyonuyla
        kanonik düğüme MERGE edilmiş bir varlığa doğru ilişki kurulabilir.
        """
        kaynak_isim = self._resolve_canonical_isim(relationship.kaynak_id)
        hedef_isim = self._resolve_canonical_isim(relationship.hedef_id)
        query, parameters = self.relationship_to_cypher(
            relationship, kaynak_isim=kaynak_isim, hedef_isim=hedef_isim
        )
        result = self.execute_query(query, parameters, write=True)
        return result[0] if result else {}

    def add_relationships(self, relationships: Sequence[Relationship]) -> None:
        for relationship in relationships:
            self.add_relationship(relationship)

    def delete_relationship(
        self, kaynak_id: str, hedef_id: str, tip: RelationshipType
    ) -> None:
        """Belirli tip ve uç noktalara sahip ilişkiyi siler."""
        query = (
            "MATCH (a {id: $kaynak_id})-[r:" + tip.value + "]->(b {id: $hedef_id}) "
            "DELETE r"
        )
        self.execute_query(
            query, {"kaynak_id": kaynak_id, "hedef_id": hedef_id}, write=True
        )

    # ------------------------------------------------------------------ #
    # Şema / bakım yardımcıları
    # ------------------------------------------------------------------ #

    def ensure_constraints(self) -> None:
        """Her düğüm etiketi için `id` VE `isim` alanlarına benzersizlik kısıtı
        oluşturur.

        `isim` kısıtı, `node_to_cypher`'daki MERGE-by-isim upsert mantığının
        veritabanı seviyesinde de garanti altına alınmasını sağlar (ayni
        etikette ayni isimli iki düğüm asla olusamaz). `id` kısıtı, düğüm
        stabil kimligini (bkz. `node_to_cypher`) korumak icin ayrica tutulur.

        Idempotent'tir (IF NOT EXISTS); uygulama başlangıcında bir kez
        çağrılması önerilir.
        """
        for label in NODE_REGISTRY:
            id_constraint_name = f"unique_{label.value.lower()}_id"
            isim_constraint_name = f"unique_{label.value.lower()}_isim"
            self.execute_query(
                f"CREATE CONSTRAINT {id_constraint_name} IF NOT EXISTS "
                f"FOR (n:{label.value}) REQUIRE n.id IS UNIQUE",
                write=True,
            )
            self.execute_query(
                f"CREATE CONSTRAINT {isim_constraint_name} IF NOT EXISTS "
                f"FOR (n:{label.value}) REQUIRE n.isim IS UNIQUE",
                write=True,
            )
        logger.info("Neo4j kisitlari (constraints) dogrulandi/olusturuldu.")
        self.ensure_spatial_indexes()
        self.ensure_property_indexes()

    def ensure_spatial_indexes(self) -> None:
        """`Infrastructure`, `Facility` VE `Unit` etiketleri için, `konum`
        (WGS-84 `Point`) alanı üzerinde bir POINT INDEX oluşturur (bkz.
        modül-seviyesi "MEKANSAL İNDEKSLEME" notu). Bu, GraphRAG'in (bkz.
        `decision_engine.py`) "olay yerinin X km yarıçapındaki açık
        güzergahlar/sağlam tesisler/en yakın birlikler" sorgularının,
        ulusal ölçekte (milyonlarca düğüm) TÜM grafı taramak YERİNE bu
        indeksi kullanarak milisaniyeler içinde çalışmasını sağlar.
        Idempotent'tir (IF NOT EXISTS); `ensure_constraints` tarafından
        otomatik çağrılır, ayrıca elle çağrılmasına GEREK YOKTUR.

        "KESİN İNDEKS" DÜZELTMESİ: `Unit` ÖNCEDEN bu
        listede YOKTU — yani `en_yakin_ulasilan_varliklari_bul(hedef_label=
        "Unit", ...)` (en sık çağrılan varyant; HER kritik olay/kapalı yol
        için "hangi birlik müdahale edecek" sorusunu bu yanıtlar) hiçbir
        zaman POINT INDEX kullanamamıştı. Bu ülke ölçeğinde `Unit` sayısı
        (~1750) `Street`/`Bridge`ye kıyasla küçük olduğundan
        bunsuz da KATASTROFİK değildi, ama index eklendiğinden
        emin olmak ve grafik büyüdükçe (senkron/
        gerçek birlik verisi arttıkça) sorunun büyümesini ÖNLEMEK için
        eklendi."""
        for label in ("Infrastructure", "Facility", "Unit"):
            index_adi = f"spatial_{label.lower()}_konum"
            self.execute_query(
                f"CREATE POINT INDEX {index_adi} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.konum)",
                write=True,
            )
        logger.info("Neo4j mekansal (POINT) indeksleri dogrulandi/olusturuldu.")

    # "FULL TABLE SCAN" DÜZELTMESİ (kriz anında GraphRAG
    # aramasının yüz binlerce düğümü tarayarak yavaşlama riskine karşı): bu
    # sorunun ASIL kaynağı, `id`/`isim` alanlarında bir index EKSİKLİĞİ
    # DEĞİLDİR (bkz. `ensure_constraints` — HER etiket için `id` VE `isim`
    # üzerinde UNIQUE CONSTRAINT zaten var, Neo4j bir unique constraint
    # oluşturduğunda ARKA PLANDA otomatik olarak destekleyici bir index de
    # kurar) VE `konum` (koordinat) alanında bir POINT INDEX eksikliği de
    # DEĞİLDİR (bkz. yukarıdaki `ensure_spatial_indexes` — Infrastructure/
    # Facility/Unit için zaten var). Tek bir genel `:Node` etiketi VE
    # `.id`/`.location` alan adları da bu şemada YOK (bkz. `models.
    # BaseNode.neo4j_labels` — multi-label mimarisi SADECE somut etiketler
    # kullanır; koordinat alanının GERÇEK adı `konum`dur, `location` değil).
    #
    # GERÇEK KÖK NEDEN (bu dosyadaki HAM Cypher sorguları elle incelenerek
    # doğrulandı — bkz. `decision_engine.gather_situational_picture`): kriz
    # anında HER seferinde çalışan üç sorgu, `id`/`isim`/`konum` DIŞINDA bir
    # ALAN DEĞERİNE göre filtreler ve bu alanların HİÇBİRİNDE index YOKTU:
    #   - `MATCH (i:Infrastructure) WHERE i.acik_mi = false ...`
    #   - `MATCH (f:Facility) WHERE f.mevcut_durum IN [...] ...`
    #   - `MATCH (e:Event) WHERE e.siddet IN [...] ...`
    # `seed_db.py` ile TÜM Türkiye omurgası + bölgesel kılcal yol ağı KALICI
    # olarak yüklendiğinden (bkz. `src.ui.app`daki "OFFLINE-FIRST / SADECE
    # NEO4J" mimari kararı), `Infrastructure` etiketi artık YÜZ BİNLERCE
    # Street/Bridge düğümü barındırabilir — index'siz bu filtre, Neo4j'i HER
    # ÇAĞRIDA (kriz akışının en sık çalışan sorgusu) o etiketin TAMAMINI
    # (bir "full LABEL scan" — tüm veritabanı değil, ama yine de aynı
    # performans sorununu yaratan tam-tarama) taramaya ZORLAR. Bu yüzden asıl
    # eksik olan, aşağıdaki üç alan üzerindeki standart (B-tree/RANGE) property
    # index'leridir; bunlar eklenince Neo4j'nin sorgu planlayıcısı `i.acik_mi
    # = false` gibi bir koşulu TÜM etiketi taramak yerine doğrudan index
    # üzerinden çözer.
    #
    # Idempotent'tir (IF NOT EXISTS); `ensure_constraints` tarafından
    # otomatik çağrılır, ayrıca elle çağrılmasına GEREK YOKTUR.
    def ensure_property_indexes(self) -> None:
        """Kriz anında (GraphRAG) SIK filtrelenen, ama `id`/`isim`/`konum`
        DIŞINDAKİ alanlar üzerinde standart property index'leri oluşturur —
        bkz. yukarıdaki "FULL TABLE SCAN DÜZELTMESİ" notu.

        Kapsam (HAM Cypher sorguları elle taranarak belirlendi, bkz. o not):
          - `Infrastructure.acik_mi`: "kapalı yol" sayımı/listelemesi
            (`gather_situational_picture`, `fetch_metrics`, `fetch_
            dynamic_crisis_points`, `reset_crisis_scenario`) HER ZAMAN bu
            alana göre filtreler.
          - `Facility.mevcut_durum`: "hasarlı/yok edilmiş tesis" sayımı/
            listelemesi (aynı fonksiyonlar) bu alana göre filtreler.
          - `Event.siddet`: "kritik/katastrofik olay" listelemesi bu alana
            göre filtreler.
          - `Unit.durum`: `reset_crisis_scenario`nun "aktif olmayan birlik"
            sorgusu bu alana göre filtreler (bkz. `n.durum <> 'Aktif' OR
            n.durum IS NULL`) — `Unit` sayısı diğerlerine kıyasla küçük
            olsa da (bkz. `ensure_spatial_indexes`daki AYNI gerekçe),
            tutarlılık ve gelecekteki büyüme için dahil edildi.
          - `Infrastructure.aciklama` / `Facility.aciklama` / `Unit.aciklama`:
            "PROFILER TESTİ" DÜZELTMESİ (`profiler_test.py`
            ile ölçüldü) — `update_infrastructure_status_by_name`nin
            YENİ eklenen hızlı TAM eşitlik ön-kontrolünün (bkz. o metodun
            docstring'i) gerçekten indeks-destekli olabilmesi için gerekli;
            bu olmadan `{aciklama: $ad}` eşitlik araması yine 975.000+
            düğümlük bir label-scan'e geri düşerdi.
        """
        alan_bazinda_indeksler = (
            ("Infrastructure", "acik_mi"),
            ("Facility", "mevcut_durum"),
            ("Event", "siddet"),
            ("Unit", "durum"),
            ("Infrastructure", "aciklama"),
            ("Facility", "aciklama"),
            ("Unit", "aciklama"),
        )
        for label, alan in alan_bazinda_indeksler:
            index_adi = f"prop_{label.lower()}_{alan.lower()}"
            self.execute_query(
                f"CREATE INDEX {index_adi} IF NOT EXISTS FOR (n:{label}) ON (n.{alan})",
                write=True,
            )
        logger.info(
            "Neo4j property indeksleri dogrulandi/olusturuldu (%s).",
            ", ".join(f"{label}.{alan}" for label, alan in alan_bazinda_indeksler),
        )

    # "GÜVENLİ BATCH" MİMARİSİ (RAM limitini elle artırmak bir MİMARİ
    # garanti DEĞİLDİR, sadece sorunu ERTELER): `clear_database`'de
    # bulunan/düzeltilen `Neo.TransientError.
    # General.MemoryPoolOutOfMemoryError` (bkz. o metodun docstring'i) TEK
    # bir bulk-yazma operasyonuna ÖZGÜ bir hata DEĞİLDİR — TÜM grafı
    # tarayan (silme VEYA toplu güncelleme fark etmez) HERHANGİ bir "tek
    # dev transaction" deseni, veri hacmi RAM/bellek-havuzu tavanını
    # AŞTIĞINDA AYNI şekilde çöker. Bu yüzden o düzeltme TEK bir metoda
    # (`clear_database`) gömülü bırakılmaz; genel-amaçlı, YENİDEN
    # KULLANILABİLİR bir yardımcıya (`batched_bulk_write`) çıkarılır —
    # `clear_database`, `reset_crisis_scenario` (aşağıda) VE
    # `local_osm_reader.LocalOsmReader.gecici_kilcal_veriyi_temizle`
    # (bkz. o dosyadaki kullanımı) AYNI güvenceyi PAYLAŞIR: donanım/
    # yapılandırma NE OLURSA OLSUN (700 MB'lık dar bir bellek havuzu DA,
    # 16 GB'lık cömert bir havuz DA), hiçbir tek transaction, işlenen veri
    # ne kadar büyük olursa olsun, bellek tavanını YAPISAL olarak AŞAMAZ.
    _GUVENLI_BATCH_BOYUTU = 50_000
    """`batched_bulk_write`in varsayılan parça (batch) boyutu — `clear_
    database`'in kanıtlanmış (1.7M+ düğüm, hatasız) değeriyle
    AYNI; TÜM güvenli-batch çağrıları bu ortak varsayılanı paylaşır."""

    def batched_bulk_write(
        self,
        match_where_cypher: str,
        action_cypher: str,
        params: Optional[Dict[str, Any]] = None,
        batch_size: int = _GUVENLI_BATCH_BOYUTU,
        islem_adi: str = "toplu islem",
    ) -> int:
        """GENEL-AMAÇLI "GÜVENLİ BATCH" yürütücüsü: `match_where_cypher`
        (ör. `"MATCH (n) WHERE n.gecici_mi = true"`) ile eşleşen düğümleri
        TEK BİR dev transaction'da DEĞİL, `batch_size` (varsayılan 50.000)
        düğümlük KÜÇÜK, BAĞIMSIZ transaction'lara bölerek `action_cypher`
        (ör. `"DETACH DELETE n"` VEYA `"SET n.acik_mi = true"`) ile işler;
        sıfır düğüm işlenene kadar tekrarlanır (bkz. yukarıdaki "GÜVENLİ
        BATCH MİMARİSİ" notu — `clear_database`deki DÜZELTMENİN
        genelleştirilmiş hâli).

        KRİTİK SORUMLULUK (çağıran tarafa aittir): `match_where_cypher`,
        HER TURDA SADECE "henüz işlenmemiş" düğümleri eşleştirmelidir —
        aksi halde döngü İLERLEME KAYDETMEZ (aynı `batch_size` kadar
        düğüm sonsuza dek yeniden işlenir). Silme (`DETACH DELETE`)
        işlemlerinde bu OTOMATİK sağlanır (silinen düğüm bir SONRAKİ
        `MATCH`te zaten YOKTUR); toplu GÜNCELLEME (`SET`) işlemlerinde
        ise `match_where_cypher`in WHERE koşulu "henüz güncellenmemiş"
        durumu AÇIKÇA hedeflemelidir (bkz. `reset_crisis_scenario`daki
        `WHERE i.acik_mi <> true OR i.acik_mi IS NULL` örneği).

        Sözdizimi: nihai sorgu
        `f"{match_where_cypher} WITH n LIMIT $batch_size {action_cypher} RETURN count(n) AS adet"`
        şeklinde birleştirilir — bu yüzden `match_where_cypher` hedef
        düğümü DAİMA `n` takma adıyla eşlemelidir (ör.
        `"MATCH (n:Infrastructure) WHERE ..."`) ve `action_cypher` de
        AYNI `n` takma adını kullanmalıdır.

        Returns:
            Toplam işlenen (silinen/güncellenen) düğüm sayısı.
        """
        params = dict(params or {})
        toplam = 0
        while True:
            sonuc = self.execute_query(
                f"{match_where_cypher} WITH n LIMIT $batch_size {action_cypher} RETURN count(n) AS adet",
                {**params, "batch_size": batch_size},
                write=True,
            )
            islenen = sonuc[0]["adet"] if sonuc else 0
            toplam += islenen
            if islenen == 0:
                break
            logger.info("%s: %d düğüm işlendi (toplam: %d)...", islem_adi, islenen, toplam)
        return toplam

    def clear_database(self, batch_boyutu: int = _GUVENLI_BATCH_BOYUTU) -> None:
        """DİKKAT: Veritabanındaki tüm düğüm ve ilişkileri siler. Sadece test/geliştirme içindir.

        Kanıtlanmış bir boşluk için savunma (ulusal ölçekte 1.7M+ düğümlü
        bir grafta tespit edildi — `Neo.TransientError.General.
        MemoryPoolOutOfMemoryError`): eski sürüm TEK bir `MATCH (n)
        DETACH DELETE n` sorgusu/TEK bir transaction ile TÜM grafı silmeye
        çalışıyordu — Neo4j'in `dbms.memory.transaction.total.max` bellek
        havuzu, milyonlarca düğümü/ilişkiyi TEK transactionda tutmaya
        yetmeyip sorguyu (birkaç kez ARTAN beklemeyle otomatik yeniden
        denedikten SONRA bile) başarısızlıkla sonlandırıyordu — silme
        TAMAMEN başarısız oluyordu (küçük/orta ölçekli graflarda bu sorun
        HİÇ görülmezdi). Çözüm (artık `batched_bulk_write` üzerinden genel-
        amaçlı hale getirildi — bkz. o metodun docstring'i): silme
        `batch_boyutu` (varsayılan 50.000) düğümlük KÜÇÜK, BAĞIMSIZ
        transaction'lara bölünür — her tur SADECE o kadar düğümü (+
        ilişkilerini) siler, sıfır düğüm kalana kadar tekrarlanır; hiçbir
        tek transaction, ne kadar büyük graf olursa olsun, bellek tavanını
        AŞMAZ.
        """
        toplam_silinen = self.batched_bulk_write(
            "MATCH (n)", "DETACH DELETE n", batch_size=batch_boyutu, islem_adi="clear_database",
        )
        logger.warning("Neo4j veritabanindaki tum veriler silindi (%d dugum).", toplam_silinen)

    def reset_crisis_scenario(self, bolge: Optional[str] = None) -> Dict[str, int]:
        """"Nükleer" `clear_database()` YERİNE kullanılan GÜVENLİ sıfırlama:
        SADECE mevcut kriz senaryosunu temizler, gerçek şehir topolojisine
        DOKUNMAZ.

        `clear_database()`, `real_osm_loader.RealOsmLoader` ile yüklenen
        47 binin üzerindeki GERÇEK OSM düğümünü (Street/Bridge/Facility/
        Unit) de İÇEREN TÜM Bilgi Grafını siler — yeni bir senaryo denemek
        isteyen bir kullanıcı, farkında olmadan saatler süren bir Overpass
        yüklemesini de kaybederdi. Bu metod bunun yerine:
          1. TÜM `Event` düğümlerini (ve üzerlerindeki TÜM ilişkileri —
             `DETACH DELETE`) siler.
          2. TÜM `Infrastructure` düğümlerinin `acik_mi` alanını tekrar
             `True` yapar (önceki senaryonun kapattığı yollar/sokaklar
             yeniden açılır).
          3. TÜM `Facility` düğümlerinin `mevcut_durum`+`durum` alanlarını,
             TÜM `Unit` düğümlerinin `durum` alanını tekrar `'Aktif'` yapar
             (önceki senaryonun "Hasarlı"/"Yok Edildi" işaretlediği tesis/
             birimler iyileşir — bkz. aşağıdaki not).

        `bolge` verilirse sıfırlama SADECE o bölgeye ait düğümlerle
        SINIRLANIR — bu, programatik/betik kullanımı için opsiyonel bir
        yetenektir (bkz. `src.ui.app`: sidebar'daki "Sadece Kriz Senaryosunu
        Sıfırla" butonu bölgesel bir seçici İÇERMEZ, HER ZAMAN `bolge=None`
        ile TÜM şehirlerin senaryosunu birlikte sıfırlar). `bolge=None`
        (varsayılan) TÜM bölgeleri sıfırlar.

        Kanıtlanmış bir boşluk için savunma ("UI Ghosting" YANLIŞ TEŞHİSİ:
        bir sıfırlama SONRASI ekranda "Kritik/Hasarlı: 1" metriğinin ve eski
        bir AI önerisinin ASILI KALDIĞI, ilk bakışta bir Streamlit
        önbellek/`session_state` sorunu gibi görünebilir): doğrulama
        sonucunda sorun bir ÖNBELLEK GHOSTİ DEĞİLDİ; `src.ui.app._clear_data_
        caches`/`st.rerun()` ZATEN doğru şekilde çağrılıyordu VE
        `fetch_metrics`/`fetch_ai_recommendations` HER ZAMAN Neo4j'e
        CANLI sorgu atıyordu (session_state'te önbelleklenmiş bir "ai_
        recommendations" değişkeni bu kod tabanında HİÇ VAR OLMADI —
        arandı, bulunamadı). GERÇEK KÖK NEDEN: bu metod eskiden Facility/
        Unit düğümlerinin `mevcut_durum`/`durum` (hasar) alanlarına
        BİLİNÇLİ OLARAK dokunmuyordu ("bir sonraki senaryo zaten
        güncelleyecek" varsayımıyla) — ama HENÜZ yeni bir senaryo
        işlenmediyse, ÖNCEKİ senaryodan "Hasarlı" kalan bir tesis/birim
        Neo4j'de GERÇEKTEN Hasarlı KALIYORDU; arayüz bunu DOĞRU şekilde
        gösteriyordu (bu bir "hayalet" değil, GERÇEK/güncel veriydi).
        Çözüm: bu metod ARTIK Facility.`mevcut_durum`+`durum` VE
        Unit.`durum`u da `'Aktif'`e döndürür — "sıfırlama" adı ARTIK
        gerçekten UÇTAN UCA (Event + Infrastructure + Facility + Unit)
        bir "barış zamanı" tabanına döner.

        "GÜVENLİ BATCH" MİMARİSİ (RAM limitini elle artırmak KABA KUVVET
        sorununu ÇÖZMEZ, sadece ERTELER): eski sürüm HER İKİ adımı da
        (Event silme + Infrastructure
        `acik_mi` güncelleme) TEK BİR transaction'da yapıyordu — ulusal
        ölçekte 975 binin üzerindeki Infrastructure düğümünü TEK bir `SET`
        ile güncellemek, `clear_database`deki AYNI SINIF bir `Neo.
        TransientError.General.MemoryPoolOutOfMemoryError` riski taşır
        (bkz. o metodun docstring'i). Her iki adım da artık `batched_bulk_
        write` (bkz. o metodun docstring'i) ÜZERİNDEN, `_GUVENLI_BATCH_
        BOYUTU`luk (50.000) KÜÇÜK, BAĞIMSIZ transaction'lara bölünerek
        çalışır — donanım/bellek yapılandırması NE OLURSA OLSUN bu buton
        ASLA tek bir dev transaction'a bel bağlamaz.

        Returns:
            {"silinen_olay": N, "acilan_altyapi": M, "iyilesen_tesis": K,
            "iyilesen_birim": L} — silinen `Event` düğüm sayısı,
            `acik_mi=True` olarak güncellenen `Infrastructure` düğüm
            sayısı, `mevcut_durum`/`durum`u `'Aktif'`e döndürülen
            `Facility`/`Unit` düğüm sayıları.
        """
        bolge_kosulu_e = " {bolge: $bolge}" if bolge else ""
        silinen_olay = self.batched_bulk_write(
            f"MATCH (n:Event{bolge_kosulu_e})",
            "DETACH DELETE n",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Event silme)",
        )

        # İDEMPOTENT İLERLEME KOŞULU (bkz. `batched_bulk_write`
        # docstring'indeki "KRİTİK SORUMLULUK" notu): `SET` bir SİLME
        # DEĞİLDİR, bu yüzden WHERE koşulu "henüz `acik_mi=true`
        # YAPILMAMIŞ" düğümleri AÇIKÇA hedeflemelidir — aksi halde her
        # tur AYNI (zaten açık) ilk `batch_size` düğümü bulup döngü HİÇ
        # ilerlemez/bitmez. AYNI idempotent desen (`WHERE <> 'Aktif' OR
        # IS NULL`) Facility/Unit sıfırlamasında da TEKRAR kullanılır.
        bolge_kosulu_i = " AND n.bolge = $bolge" if bolge else ""
        acilan_altyapi = self.batched_bulk_write(
            f"MATCH (n:Infrastructure) WHERE (n.acik_mi <> true OR n.acik_mi IS NULL){bolge_kosulu_i}",
            "SET n.acik_mi = true",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Infrastructure acma)",
        )

        bolge_kosulu_f = " AND n.bolge = $bolge" if bolge else ""
        iyilesen_tesis = self.batched_bulk_write(
            f"MATCH (n:Facility) WHERE (n.mevcut_durum <> 'Aktif' OR n.mevcut_durum IS NULL "
            f"OR n.durum <> 'Aktif' OR n.durum IS NULL){bolge_kosulu_f}",
            "SET n.mevcut_durum = 'Aktif', n.durum = 'Aktif'",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Facility iyilestirme)",
        )
        iyilesen_birim = self.batched_bulk_write(
            f"MATCH (n:Unit) WHERE (n.durum <> 'Aktif' OR n.durum IS NULL){bolge_kosulu_f}",
            "SET n.durum = 'Aktif'",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Unit iyilestirme)",
        )

        logger.warning(
            "Kriz senaryosu sifirlandi (bolge=%s): %d Event dugumu silindi, %d Infrastructure "
            "dugumu tekrar acik_mi=True yapildi, %d Facility + %d Unit dugumu tekrar Aktif "
            "yapildi (gercek sehir topolojisi KORUNDU).",
            bolge or "TUMU",
            silinen_olay,
            acilan_altyapi,
            iyilesen_tesis,
            iyilesen_birim,
        )
        return {
            "silinen_olay": silinen_olay,
            "acilan_altyapi": acilan_altyapi,
            "iyilesen_tesis": iyilesen_tesis,
            "iyilesen_birim": iyilesen_birim,
        }


def get_db() -> Neo4jConnection:
    """`Neo4jConnection` singleton örneğine kısayol erişim fonksiyonu."""
    return Neo4jConnection()
