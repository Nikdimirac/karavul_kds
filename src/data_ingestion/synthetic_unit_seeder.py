"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
SENTETİK MÜDAHALE BİRLİĞİ ÜRETİCİ — "Karar Derinliği" (Decision Depth) aracı.

SORUN (canlı hata raporu): Gerçek OSM verisi (bkz. `osm_loader.
OSMBaselineLoader._build_point_query`), Elazığ/Malatya için `Unit` olarak
SADECE `amenity=police`/`amenity=fire_station` gibi noktaları içeriyor —
AFAD, UMKE, Kızılay, sahra hastanesi veya ağır mühendislik/inşaat ekibi gibi
GERÇEK bir afette hayati önem taşıyan müdahale unsurları OSM'de bu şekilde
etiketlenmediği için hiç yüklenmiyor. Sonuç: SAKOM Karar Destek Motoru, bir
köprü/yol yıkımı gibi bir ALTYAPI felaketinde bile önüne SADECE Emniyet
birimleri geldiği için hep Emniyet öneriyordu — enkazı kim kaldıracak,
yaralıyı kim tedavi edecek sorularına veride HİÇBİR cevap yoktu.

Bu script, Neo4j'e Türkiye'nin gerçek afet müdahale envanterini temsil eden
(AFAD, UMKE, Karayolları ağır makine, Kızılay, sahra hastanesi, itfaiye
takviye) ~17 stratejik SENTETİK birim ekler. "Sentetik" olmaları, sahte/
gelişigüzel bir konuma sahip oldukları anlamına GELMEZ — bkz. aşağıdaki
"ENTEGRASYON" notu.

ENTEGRASYON (Dijkstra ile KUSURSUZ ÇALIŞMA GARANTİSİ): Her birim, rastgele
bir koordinata DEĞİL, veritabanında ZATEN YÜKLÜ GERÇEK bir ana yol
(motorway/trunk/primary — bkz. `_ANA_YOL_HIGHWAY_TIPLERI`) noktasının
BİREBİR AYNI enlem/boylamına yerleştirilir (bkz. `_ana_yol_noktasi_sec`).
Böylece `Neo4jConnection.en_yakin_ulasilan_varliklari_bul`daki "hedef snap"
adımı (bkz. `_HEDEF_SNAP_YARICAPI_KM`) SIFIRA YAKIN bir mesafede eşleşir ve
gerçek yol ağı + Dijkstra tabanlı erişilebilirlik hesabı bu birimler için de
GERÇEK OSM tesisleri kadar kusursuz çalışır.

TEKRAR ÇALIŞTIRILABİLİRLİK: `Neo4jConnection.add_node` MERGE-by-isim
kullandığından (bkz. `database.py`), bu script GÜVENLE BİRDEN FAZLA KEZ
çalıştırılabilir — ikinci çalıştırma yeni kopya birimler YARATMAZ, var
olanları (ör. güncellenme_tarihi) günceller. GERÇEK şehir topolojisine
(Street/Bridge/Facility) HİÇBİR ŞEKİLDE dokunmaz, SADECE yeni Unit düğümleri
ekler.

Çalıştırmak için (proje kök dizininden):
    python -m src.data_ingestion.synthetic_unit_seeder
"""

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

# Windows konsollari genellikle UTF-8 DEGIL, yerel bir kod sayfasi kullanir;
# Turkce karakterler/emoji bu yuzden `UnicodeEncodeError` ile cokebilir (bkz.
# `run_real_data.py`daki AYNI onlem).
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


# ---------------------------------------------------------------------------
# Ana yol secimi (gercek OSM verisine oturma)
# ---------------------------------------------------------------------------

# Agir ekipman/lojistik aracin GERCEKTEN rahatca gecebilecegi, real_osm_
# loader.ANA_DAMAR_HIGHWAY_TIPLERI ile TUTARLI ana damar turleri.
_ANA_YOL_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary")


def _ana_yol_noktasi_sec(
    db: Neo4jConnection, bolge: str, kullanilmis: Set[str]
) -> Optional[Dict[str, Any]]:
    """Verilen bölgede (ör. "Elazığ"), GERÇEK yüklü OSM verisinden AÇIK
    (`acik_mi = true`) bir ana yol noktası SEÇER — bu koordinat birebir
    yeni `Unit`e atanacaktır (bkz. modül docstring'indeki "ENTEGRASYON"
    notu). `kullanilmis` kümesindeki isimler (bu çalıştırmada ÖNCEDEN
    seçilmiş noktalar) tekrar seçilmez — iki birimin AYNI koordinata
    çakışıp haritada üst üste binmesini önler."""
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


# ---------------------------------------------------------------------------
# STRATEJİK YENİDEN DAĞITIM
# ---------------------------------------------------------------------------
# SORUN: İlk sürüm, TÜM birimleri il GENELİNDE saf
# rastgele dağıtıyordu — Elazığ/Malatya onlarca km'lik bir alan olduğundan,
# tek bir kriz noktasına (ör. Kömürhan Köprüsü) en yakın birim bile 25 km
# analiz yarıçapının DIŞINDA (37.9-119 km) kalabiliyordu; AFAD/UMKE/Ağır
# Mühendislik birimleri PRATİKTE hiçbir zaman "ulaşılabilir" listesinde
# GÖRÜNMÜYORDU. Gerçek bir afet müdahale envanteri RASTGELE DAĞILMAZ — AFAD/
# UMKE depoları ve Karayolları şantiyeleri BİLEREK kritik güzergahlara/
# köprülere YAKIN konumlandırılır. Bu bölüm bunu simüle eder.

_KRITIK_ALTYAPI_MAKS_MESAFE_KM = 18.0
"""Hedeflenen "15-20 km" aralığının ortası."""

_KRITIK_ALTYAPI_DENEME_SAYISI = 8
"""Tek bir kritik-altyapı çapasının çevresinde müsait (kullanılmamış) bir
nokta kalmamışsa, PES ETMEDEN ÖNCE kaç FARKLI rastgele çapa denenir."""


def _kritik_altyapi_capalarini_getir(db: Neo4jConnection, bolge: str) -> List[Dict[str, Any]]:
    """Verilen bölgedeki "kritik altyapı" ÇAPA (anchor) noktalarını getirir:
    GERÇEK, temiz-isimli Köprü (bkz. modül docstring'i — bu proje henüz
    ayrı bir "Viyadük"/"Kavşak" varlığı YÜKLEMEDİĞİNDEN, büyük viyadükler
    genelde OSM'de `bridge=yes` ile etiketlenip zaten bu Köprü kümesine
    girer) düğümleri + ana otoyol (motorway/trunk) örneklemi (büyük
    kavşakların GERÇEKTE üzerinde/yakınında olduğu güzergah türü — kesin
    kavşak-düğümü verisi olmadığından en YAKIN gerçekçi vekil budur)."""
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
    """"STRATEJİK YENİDEN DAĞITIM": Ağır Mühendislik/AFAD/UMKE gibi hayati
    birimleri, il GENELİNDE saf rastgele DEĞİL, GERÇEK bir kritik altyapı
    (köprü/viyadük veya ana otoyol — bkz. `_kritik_altyapi_capalarini_
    getir`) noktasının `maks_mesafe_km` (varsayılan 18 km) İÇİNDEKİ bir ana
    yol noktasına yerleştirir. Tek bir çapa yeterli olmazsa (o çevrede
    müsait nokta kalmamışsa) `_KRITIK_ALTYAPI_DENEME_SAYISI` kadar FARKLI
    rastgele çapa daha denenir. Hiçbiri işe yaramazsa `None` döner —
    çağıran taraf bunu (bkz. `seed_units`) LOGLAYIP AÇIKÇA bildirir, SESSİZCE
    normal rastgele seçime GERİ DÜŞMEZ (aksi halde "stratejik" garantisi
    sessizce bozulurdu).

    `capa_arama_terimi` verilirse (ör. "Kömürhan"), çapa adayları ÖNCE bu
    terimi (Türkçe-katlamalı, büyük/küçük harf duyarsız) İÇEREN gerçek
    altyapılarla SINIRLANIR — bu, "gerçek dünya binasıyla birebir eşleşen"
    DETERMİNİSTİK bir yerleşim sağlar (rastgele bir
    köprüye DEĞİL, İSTENEN GERÇEK köprüye/tünele yakın yerleştirir). Eşleşen
    hiçbir çapa yoksa (ör. isim yazımı değişmişse), bu KISITLAMA SESSİZCE
    YOK SAYILMAZ — açıkça loglanır ve TÜM çapalara geri dönülür (yine de
    stratejik bir yerleşim garanti edilsin diye)."""
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


# ---------------------------------------------------------------------------
# TÜRKİYE ÇAPINDA SENARYO HAZIRLIĞI ("tek köprü takıntısından kurtarma"
# hedefiyle): SORUN — bu script'in İLK sürümü, TÜM stratejik
# birimleri Kömürhan Köprüsü/tüneli ETRAFINDA (bkz. `kritik_altyapi_
# yakininda`) veya il GENELİNDE saf rastgele dağıtıyordu; SADECE köprü/yol
# YIKIMI senaryosuna hazırdık. GERÇEK Türkiye'de afetler ÇEŞİTLİDİR (deprem,
# sel, orman yangını, kırsal arama-kurtarma) ve HER birinin coğrafi/idari
# gerçeği farklıdır — bir orman yangını dağlık/ormanlık bir bölgede, bir sel
# genelde bir vadi/göl havzasında, bir kırsal arama-kurtarma dağlık bir
# ilçede olur. Aşağıdaki bölüm bu ÇEŞİTLİLİĞİ, HER seferinde GERÇEK bir
# coğrafi çapaya (bkz. `_isimli_yol_noktasi_sec`) oturtarak simüle eder.

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
    """"TÜRKİYE ÇAPINDA SENARYO HAZIRLIĞI": `_ana_yol_noktasi_sec` (rastgele
    HERHANGİ bir ana yol) ve `_kritik_altyapi_yakininda_ana_yol_noktasi_sec`
    (bir altyapı çapasına YAKIN bir ana yol) ile AYNI AİLEDEN, ama üçüncü ve
    en DOĞRUDAN yerleştirme stratejisi: `isim_terimi`yi (ör. "Sivrice",
    "Baskil", "İstasyon Caddesi") İÇEREN GERÇEK bir açık yolun ÜZERİNE
    (dolaylı bir "yakınlık" araması YAPMADAN, doğrudan o yolun kendi
    noktalarından birine) oturtur — bu, "bir dağ yolu/göl kenarı/şehir içi
    kavşak" gibi coğrafi olarak SPESİFİK bir yere yerleşmesi istenen (ama
    Kömürhan'daki gibi tek bir SABİT altyapıya değil, o BÖLGEYİ temsil eden
    HERHANGİ bir noktaya) birimler içindir. `_GENIS_YOL_TIPLERI` kullanır
    (bkz. o sabitin docstring'i — ana damar kısıtı burada GEÇERLİ DEĞİLDİR).
    Eşleşen açık bir yol yoksa (ör. terim yanlış yazılmışsa) `None` döner —
    çağıran taraf (`seed_units`) bunu LOGLAYIP AÇIKÇA bildirir, SESSİZCE
    başka bir yola DÜŞMEZ (aksi halde coğrafi özgüllük garantisi bozulurdu).

    Kanıtlanmış bir boşluk için savunma (`LIMIT 5000` isim-filtresinden
    ÖNCE kesiyordu): İLK sürüm önce `LIMIT 5000` satır çekip ismi PYTHON tarafında
    filtreliyordu (bkz. `_kritik_altyapi_yakininda_ana_yol_noktasi_sec`ın
    aynı deseni — o fonksiyonda ZARARSIZ çünkü `capa` sayısı zaten KÜÇÜK).
    Ama `_GENIS_YOL_TIPLERI` (5 highway tipi, secondary/tertiary DAHİL)
    kullanan BÜYÜK bölgelerde (ör. Elazığ) eşleşen tür başına ON BİNLERCE
    nokta olabildiğinden, `LIMIT 5000` "Baskil" gibi bir terimi taşıyan
    satırlara HİÇ ULAŞAMADAN kesilebiliyordu — ör. "Elazığ Baskil
    Yolu" 1433+ nokta taşımasına RAĞMEN 0 sonuç dönebiliyordu. Çözüm: isim filtresi
    artık DOĞRUDAN Cypher'da (`CONTAINS`) uygulanır — `LIMIT` SADECE zaten
    eşleşen satırlara uygulanır, ham tabloya değil.
    """
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


# ---------------------------------------------------------------------------
# Birim sablonlari (Envanter Cesitliligi)
# ---------------------------------------------------------------------------


@dataclass
class _BirimSablonu:
    isim: str
    bolge: str  # "Elazığ" veya "Malatya" — GERCEK yuklu OSM bolge etiketiyle BIREBIR eslesmeli.
    unit_type: UnitType
    aciklama: str  # "Yetenek: ..." serbest metin (bkz. BaseNode.aciklama).
    personel_araligi: Tuple[int, int]
    hareket_tipi: UnitMovementType = UnitMovementType.MOTORIZE
    hareket_kabiliyeti: MobilityStatus = MobilityStatus.TAM_HAREKETLI
    uzmanlik_alani: Optional[UnitSpecialization] = None
    gunluk_ikmal_araligi_ton: Tuple[float, float] = (1.0, 3.0)
    # STRATEJİK YENİDEN DAĞITIM: True ise bu birim il
    # genelinde rastgele DEĞİL, gerçek bir kritik altyapıya (köprü/ana
    # otoyol) `_KRITIK_ALTYAPI_MAKS_MESAFE_KM` içinde yerleştirilir (bkz.
    # `_kritik_altyapi_yakininda_ana_yol_noktasi_sec`). SADECE
    # "Ağır Mühendislik, AFAD ve UMKE" kategorileri için
    # True'dur; diğerleri (İtfaiye, Sahra Hastanesi, Kızılay) il genelinde
    # dağılımını KORUR.
    kritik_altyapi_yakininda: bool = False
    # GERÇEK DÜNYA İSİM GÜNCELLEMESİ: verilirse (ör.
    # "Kömürhan"), stratejik yerleşim RASTGELE bir kritik altyapıya DEĞİL,
    # bu terimi taşıyan GERÇEK, BELİRLİ altyapıya deterministik olarak
    # yerleştirilir (bkz. `_kritik_altyapi_yakininda_ana_yol_noktasi_sec`
    # `capa_arama_terimi` parametresi) — bu birim adının gerçek dünyadaki
    # SPESİFİK bir binayla/tesisle birebir eşleşmesi gerektiğinde kullanılır.
    sabit_capa_arama_terimi: Optional[str] = None
    # TÜRKİYE ÇAPINDA SENARYO HAZIRLIĞI: verilirse (ör.
    # "Sivrice"), bu birim `_kritik_altyapi_yakininda_ana_yol_noktasi_sec`
    # (bir altyapı ÇAPASINA yakın ana yol) YERİNE, doğrudan
    # `_isimli_yol_noktasi_sec` ile bu terimi taşıyan GERÇEK yolun ÜZERİNE
    # yerleştirilir (bkz. o fonksiyonun docstring'i — "dağ yolu/göl kenarı/
    # şehir içi kavşak" gibi coğrafi çeşitlilik İÇİN). Bu alan DOLU ise
    # `kritik_altyapi_yakininda`/`sabit_capa_arama_terimi` GÖZ ARDI EDİLİR
    # (bkz. `seed_units`daki öncelik sırası).
    dogrudan_isimli_yol_terimi: Optional[str] = None
    # Bu şablonun HANGİ afet senaryosu İÇİN tasarlandığı (bkz. `KRIZ_
    # TIPLERI`) — SADECE dokümantasyon/okunabilirlik amaçlıdır, `seed_units`
    # TARAFINDAN kullanılmaz (asıl eşleme `decision_engine`dedir).
    hedef_kriz_tipi: Optional[str] = None


# TARİHSEL İSİMLER ("Gerçek Dünya İsim Güncellemesi" sonrası):
# aşağıdaki isimler ARTIK BIRIM_SABLONLARI'nda YOKTUR (yeniden adlandırıldı/
# birleştirildi); `eski_sentetik_birimleri_sil` bunları da temizler ki
# yeniden adlandırmadan ÖNCEKİ isimle veritabanında ÖKSÜZ (orphan) bir
# düğüm KALMASIN.
#   "AFAD Elazığ İkmal Noktası" -> "Karayolları 8. Bölge Tünel Kontrol ve
#   Müdahale Merkezi" olarak yeniden adlandırıldı (gerçek Kömürhan Tüneli
#   kontrol merkeziyle birebir eşleşmesi için; unit_type de AFAD'dan Ağır
#   Mühendislik'e değişti).
TARIHSEL_ISIMLER: List[str] = [
    "AFAD Elazığ İkmal Noktası",
]


BIRIM_SABLONLARI: List[_BirimSablonu] = [
    # --- ARAMA KURTARMA (AFAD / UMKE) ---
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
        # GERÇEK DÜNYA İSİM GÜNCELLEMESİ: Kömürhan
        # bölgesinde GERÇEKTEN var olan Karayolları tünel kontrol/müdahale
        # merkeziyle birebir eşleşsin diye — eskiden "AFAD Elazığ İkmal
        # Noktası" (bkz. TARIHSEL_ISIMLER); unit_type de AFAD'dan Ağır
        # Mühendislik'e (tünel/yol bakım-müdahale doğasına uygun) değişti.
        # `sabit_capa_arama_terimi="Kömürhan"`: bu birim ARTIK rastgele bir
        # kritik altyapıya DEĞİL, deterministik olarak GERÇEK Kömürhan
        # köprüsü/tüneline yakın yerleştirilir. `bolge="Malatya"`:
        # "Kömürhan Köprüsü" gerçek OSM verisinde
        # `bolge='Malatya'` etiketiyle yüklü — "Elazığ" ile aramak bu
        # yüzden 0 sonuç verirdi.
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
    # --- AĞIR MÜHENDİSLİK (enkaz/moloz kaldırma, güzergah açma) ---
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
    # --- MEDİKAL (sahra hastanesi, kan/lojistik) ---
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
    # --- TÜRKİYE ÇAPINDA SENARYO HAZIRLIĞI ("tek köprü takıntısından
    # kurtarma" hedefiyle): aşağıdaki 6 şablon, sistemi Kömürhan
    # Köprüsü/Altyapı senaryosunun ÖTESİNE, Deprem/Sel/Orman Yangını gibi
    # FARKLI afet türlerine ve FARKLI coğrafyalara (bir dağ yolu, bir göl
    # kenarı, bir şehir içi kavşak) hazırlar (bkz. `KRIZ_TIPLERI` ve
    # `_isimli_yol_noktasi_sec`) ---
    _BirimSablonu(
        # Sivrice (Elazığ) - Hazar Gölü kıyısındaki GERÇEK ilçe, 2020
        # Sivrice-Elazığ depreminin merkez üssü; "göl kenarı" + "Deprem"
        # senaryosunu birlikte temsil eder.
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
        # Baskil (Elazığ) - Fırat vadisine bakan dağlık/ormanlık bir ilçe;
        # "dağ yolu" + "Orman Yangını" senaryosunu temsil eder.
        isim="Baskil Orman İtfaiyesi İstasyonu", bolge="Elazığ", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Orman Yangını Söndürme, Arazi Aracı, Dağlık Arazi Müdahalesi",
        personel_araligi=(15, 30), hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI,
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
        dogrudan_isimli_yol_terimi="Baskil", hedef_kriz_tipi="Orman Yangini",
    ),
    _BirimSablonu(
        # Doğanşehir (Malatya) - dağlık/ormanlık bir ilçe; Elazığ tarafındaki
        # Baskil ile SİMETRİK olarak Malatya'nın "dağ yolu"/"Orman Yangını"
        # temsilcisi.
        isim="Doğanşehir Orman İtfaiyesi İstasyonu", bolge="Malatya", unit_type=UnitType.ITFAIYE,
        aciklama="Yetenek: Orman Yangını Söndürme, Arazi Aracı, Dağlık Arazi Müdahalesi",
        personel_araligi=(15, 30), hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI,
        gunluk_ikmal_araligi_ton=(1.0, 2.0),
        dogrudan_isimli_yol_terimi="Doğanşehir", hedef_kriz_tipi="Orman Yangini",
    ),
    _BirimSablonu(
        # Yeşilyurt (Malatya) - Malatya'nın merkez ilçelerinden, vadi tabanı/
        # akarsu güzergahına yakın; "Sel" senaryosunu temsil eder.
        isim="Yeşilyurt Sel Müdahale ve AFAD Deposu", bolge="Malatya", unit_type=UnitType.AFAD,
        aciklama="Yetenek: Su Baskını Tahliyesi, Motopomp, Bot, Çadır/Battaniye",
        personel_araligi=(25, 50), uzmanlik_alani=UnitSpecialization.ENKAZ,
        hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI, gunluk_ikmal_araligi_ton=(2.0, 4.0),
        dogrudan_isimli_yol_terimi="Yeşilyurt", hedef_kriz_tipi="Sel",
    ),
    _BirimSablonu(
        # İstasyon Caddesi (Malatya) - şehir merkezinde GERÇEK bir cadde;
        # "şehir içi kavşak" senaryosunu (ör. kentsel bina çökmesi/deprem)
        # temsil eder — Kömürhan'ın kırsal/altyapı odağından FARKLI olarak
        # KENTSEL bir arama-kurtarma erişimini simüle eder.
        isim="Malatya İstasyon Kavşağı Jandarma Arama Kurtarma Timi", bolge="Malatya",
        unit_type=UnitType.ASKERI_BIRLIK,
        aciklama="Yetenek: Kentsel Arama-Kurtarma, Enkaz, Şehir İçi Trafik Yönetimi",
        personel_araligi=(20, 45), uzmanlik_alani=UnitSpecialization.ENKAZ,
        hareket_kabiliyeti=MobilityStatus.TAM_HAREKETLI, gunluk_ikmal_araligi_ton=(1.0, 2.5),
        dogrudan_isimli_yol_terimi="İstasyon Caddesi", hedef_kriz_tipi="Deprem",
    ),
    # --- İTFAİYE TAKVİYE (köprü/yol kazalarında yangın/kurtarma riski icin) ---
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


# ---------------------------------------------------------------------------
# Yazma mantigi
# ---------------------------------------------------------------------------


def seed_units(
    db: Neo4jConnection, sablonlar: Optional[List[_BirimSablonu]] = None, seed: Optional[int] = None
) -> Tuple[List[Unit], List[str]]:
    """`sablonlar`daki (varsayılan: `BIRIM_SABLONLARI`) her şablon için
    gerçek bir ana yol noktası seçip (bkz. `_ana_yol_noktasi_sec`) bir
    `Unit` oluşturur ve `db.add_node` ile Neo4j'e MERGE eder (bkz. modül
    docstring'indeki "TEKRAR ÇALIŞTIRILABİLİRLİK" notu).

    `seed` verilirse `random`ın durumu sabitlenir (deterministik/test edilebilir
    çalıştırma için); verilmezse her çalıştırmada farklı ama YİNE DE gerçek
    yol noktaları seçilir.

    Döner: `(olusturulan_birimler, basarisiz_isimler)` — bir bölgede hiç
    uygun ana yol noktası bulunamazsa (ör. o bölge hiç yüklenmemişse) o
    şablon SESSİZCE atlanmaz, `basarisiz_isimler`e eklenip loglanır.
    """
    if seed is not None:
        random.seed(seed)
    sablonlar = sablonlar if sablonlar is not None else BIRIM_SABLONLARI

    kullanilmis_noktalar: Set[str] = set()
    olusturulan: List[Unit] = []
    basarisiz: List[str] = []

    for sablon in sablonlar:
        # YERLEŞTİRME STRATEJİSİ ÖNCELİK SIRASI (3 strateji, bkz. ilgili
        # fonksiyonların docstring'i):
        # 1) `dogrudan_isimli_yol_terimi` DOLU ise ("TÜRKİYE ÇAPINDA SENARYO
        #    HAZIRLIĞI" — ör. "Sivrice", "Baskil"): o GERÇEK yolun/bölgenin
        #    ÜZERİNE doğrudan yerleştirilir.
        # 2) Aksi halde `kritik_altyapi_yakininda` ise ("STRATEJİK YENİDEN
        #    DAĞITIM" — Ağır Mühendislik/AFAD/UMKE): gerçek bir kritik
        #    altyapıya (köprü/ana otoyol) YAKIN yerleştirilir.
        # 3) Aksi halde (İtfaiye/Sahra Hastanesi/Kızılay vb.): il genelinde
        #    rastgele bir ana yol noktasına yerleştirilir (ESKİ mantık).
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
        # KRITIK (kanıtlanmış bir boşluk için savunma): `db.add_node()`
        # KULLANILMAZ. `add_node`, `_resolve_canonical_isim` ile GENEL bir
        # bulanik (fuzzy, esik=0.85) isim eslestirmesi yapar — bu, LLM'in
        # AYNI gercek-dunya varligi icin urettigi kucuk yazim varyasyonlarini
        # birlestirmek ICINDIR. Ama BU script'in KASITLI olarak BENZER
        # isimli (ör. "...(Elazığ)" / "... (Malatya)") AYRI/FARKLI birimleri
        # VARDIR; bu yuzden "Özel Sektör Ağır Makine Parkı (Malatya)" ->
        # "...(Elazığ)" (benzerlik=0.88) gibi bir eşleşme SESSIZCE ayni
        # duguma MERGE edilip iki FARKLI bolgedeki birim TEK, YANLIS-VERILI
        # bir duguma cakisabilir (isim "Elazığ" derken koordinat/bolge
        # "Malatya" olur). Bu yuzden burada DOGRUDAN, fuzzy adimi ATLAYAN bir TAM-ISIM
        # MERGE kullanilir (`node_to_cypher`, `merge_isim=None` iken zaten
        # `node.isim`in KENDISINI kullanir) — bu script'in isimleri ZATEN
        # BILEREK/KESIN olarak benzersiz secildigi icin fuzzy guvenlik agina
        # HIC GEREK YOKTUR.
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
    """"Eski sentetik birimleri sil ve yeniden tohumla" (Stratejik Yeniden
    Dağıtım): `sablonlar`daki (varsayılan: `BIRIM_
    SABLONLARI`) BİLİNEN/SABİT isimlerle TAM eşleşen `Unit` düğümlerini
    `DETACH DELETE` eder. SADECE bu script'in KENDİ ürettiği, isimleri
    ÖNCEDEN BİLİNEN birimleri hedefler — gerçek OSM'den yüklenmiş (ör.
    "Kale İlçe Emniyet Müdürlüğü") veya kullanıcının kendi kriz raporlarıyla
    oluşturduğu HİÇBİR Unit'e DOKUNMAZ (isim listesi TAM eşleşme gerektirir,
    bulanık/kısmi eşleşme YOKTUR). `TARIHSEL_ISIMLER`i de (yeniden
    adlandırılmış eski şablon isimleri) temizler ki bir isim güncellemesi
    ARDINDAN eski isimle öksüz (orphan) bir düğüm KALMASIN."""
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

    # KRITIK DOGRULAMA: veritabaninda GERCEKTEN yazildigini dogrudan sorguyla teyit et.
    toplam_unit = db.execute_query("MATCH (n:Unit) RETURN count(n) AS c")[0]["c"]
    print(f"\n{Renk.BOLD}Neo4j'deki GERÇEK toplam Unit sayısı: {Renk.YESIL}{toplam_unit}{Renk.BITIS}")
    db.close()


if __name__ == "__main__":
    main()
