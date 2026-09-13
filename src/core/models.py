"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
Neo4j Bilgi Grafı (Knowledge Graph) için Pydantic veri modelleri.

Bu modül, "System of Systems" mimarisindeki tüm düğümleri (Nodes) ve
aralarındaki ilişkileri (Edges/Relationships) tanımlar. Her düğüm sınıfı,
Neo4j'e yazılacak etiketi (`neo4j_label`) ve Cypher parametrelerine
dönüştürme mantığını (`to_cypher_properties`) barındırır.

Mimari not: Tüm düğümler `BaseNode` sınıfından türer; bu sayede her
varlık ortak biçimde kimlik (id), konum (enlem/boylam), genel operasyonel
durum (durum) ve zaman damgalarını taşır. Her alt sınıf, kendine özgü
stratejik parametreleri ekler.

FAZ 1 GENİŞLETME NOTU (Ultra-Detaylı Varlık Ontolojisi — Enterprise KDS):
--------------------------------------------------------------------------
Bu şema, "Proof of Concept" (Elazığ pilot bölgesi) ölçeğinden, tüm Türkiye'yi
sokak/mahalle düzeyinde kapsayacak (TUCBS hedefli) bir mimariye geçişin İLK
fazıdır. Bu fazda YALNIZCA ontoloji (node/enum/ilişki tipi tanımları)
derinleştirilmiştir; bunu TÜKETEN katmanlar (`nlp_parser`, `decision_engine`,
`database`, `osm_loader`, `ui.app`) HENÜZ GÜNCELLENMEMİŞTİR — bu bilinçli bir
kapsam sınırlamasıdır (bkz. proje Faz 2: ETL). Yani örneğin yeni eklenen
`AdministrativeArea` düğümü henüz haritada çizilmiyor veya karar motoruna
girmiyor; şema seviyesinde HAZIRDIR.

İki mimari konvansiyon, mevcut kod tabanıyla TUTARLI kalmak için bilinçli
olarak korunmuştur:
  1. ASCII-Türkçe enum değerleri: Tüm enum `value`'ları (Türkçe olsa bile)
     noktalı/şapkalı harf İÇERMEZ (ör. "Viyaduk" DEĞİL "Viyadük" değil,
     "Sihhiye" DEĞİL "Sıhhiye" değil). Bu, `nlp_parser.normalize_tr` ve
     `database._fold_isim` içindeki mevcut Türkçe katlama (folding)
     mantığıyla ve dosyadaki TÜM diğer enum'larla (ör. "Askeri Us",
     "Siginak") birebir tutarlıdır.
  2. Neo4j düğüm özellikleri İÇ İÇE DİZİ (array-of-array) TAŞIYAMAZ — sadece
     tek tip primitiflerden oluşan DÜZ diziler geçerlidir. Bu yüzden
     `AdministrativeArea.sinir_noktalari` bir `List[Tuple[float,float]]`
     DEĞİL, düzleştirilmiş `[enlem1, boylam1, enlem2, boylam2, ...]`
     biçiminde bir `List[float]`'tır (bkz. ilgili alanın docstring'i).
"""

from __future__ import annotations

from abc import ABC
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# "PYDANTIC SEVİYESİNDE KATI ÖNLEMLER" (Rize senaryosu — kanıtlanmış bir
# boşluk için savunma):
# LLM (Llama3) prompt kurallarını (bkz. `nlp_parser.py` kural 16) EZEREK
# "Heyelan" gibi bir afet TÜRÜNÜ `facilities` listesine, "Ana Yollar" gibi
# bir es-anlamlıyı da `infrastructure_type` alanına HAM olarak yazmaya
# DEVAM EDEBİLİYORDU — "yumuşak" (prompt) kurallar TEK BAŞINA YETERLİ
# DEĞİLDİR (bu projenin baştan beri tekrar ettiği "modele güvenmek yetmez"
# ilkesiyle AYNI sınıf bir ders). Çözüm bu OLGUYU modelin KENDİSİNE
# (`Facility`/`Infrastructure`) taşımaktır — `nlp_parser.py`nin hangi
# koddan çağrıldığından BAĞIMSIZ olarak (ör. ileride eklenecek başka bir
# LLM/ETL kaynağı için bile) AYNI güvence GEÇERLİ kalır.
#
# `normalize_tr` (bkz. `nlp_parser.py`) BURADAN İMPORT EDİLMEZ: bu modül
# (`models.py`) proje genelinde EN ALTTA yer alan, HİÇBİR ÜST katmanı
# (`nlp_parser`/`decision_engine`/`database`/vb.) import ETMEYEN temel bir
# modüldür — bunu bozmamak için AYNI küçük Türkçe-katlama mantığı burada
# BAĞIMSIZ olarak yeniden tanımlanır (bu projede tekrar eden "iki modül
# bağımsız kalsın" deseni — bkz. `real_osm_loader.KILCAL_HIGHWAY_TIPLERI`).
_TR_KATLAMA_TABLOSU = str.maketrans(
    {"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
     "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c"}
)


def _tr_katla(deger: str) -> str:
    """Karşılaştırmaya hazır sade bir anahtar üretir: Türkçe karakterleri
    ASCII karşılıklarına çevirir, küçük harfe indirger, boşluk/tire/nokta
    gibi ayırıcıları siler. Örnek: "Ana Yol", "ana-yol", "ANA YOL." ->
    "anayol" (hepsi AYNI anahtara düşer)."""
    return (
        deger.strip().translate(_TR_KATLAMA_TABLOSU).lower()
        .replace(" ", "").replace("-", "").replace(".", "").replace("_", "")
    )


class GecersizFacilityTuruAtlandi(ValueError):
    """`Facility.facility_type` GERÇEKTEN geçerli hiçbir `FacilityType`
    değerine (eş-anlamlılar dahil) karşılık GELMEDİĞİNDE (bkz. `Facility.
    _facility_type_dogrula_veya_atla`) fırlatılır — ÇOĞUNLUKLA bunun
    nedeni LLM'in bir Event/afet türünü (ör. "Heyelan", "Yangın") YANLIŞLIKLA
    bir Tesis sanmasıdır (bkz. `nlp_parser.py`daki "AFET/KRIZ TURU ASLA
    TESIS/ALTYAPI DEGILDIR" kuralı — bu, o kuralın PYDANTIC seviyesindeki
    KESIN/katı karşılığıdır).

    SIRADAN bir `ValidationError`den (ör. eksik bir sayısal alan — bu HALA
    normal şekilde kullanıcıya bildirilir) KASITLI olarak AYRI bir
    exception SINIFIDIR: çağıran taraf (bkz. `nlp_parser.OllamaParser.
    _coerce_node`) bu türden bir hatayı YAKALADIĞINDA, kullanıcıya "veri
    doğrulanamadı" şeklinde sarı bir UI uyarısı GÖSTERMEK YERİNE sessizce/
    SADECE log-seviyesinde atlar — uydurma LLM verileri yüzünden gereksiz
    bir sarı uyarı gösterilmesi İSTENMEZ. Bu GÜVENLİDİR/veri kaybı
    SAYILMAZ: asıl afet, `events` listesindeki DOĞRU `Event` kaydıyla
    ZATEN ayrıca yakalanmış OLMASI beklenir (bkz. `nlp_parser`daki "OLAY
    TURU SINIFLANDIRMA KURALI") — burada atlanan, SADECE aynı bilginin
    YANLIŞLIKLA tekrarlanan/hatalı bir Facility kopyasıdır."""


class GecersizInfrastructureTuruAtlandi(ValueError):
    """`Infrastructure.infrastructure_type` GERÇEKTEN geçerli hiçbir
    `InfrastructureType` değerine (eş-anlamlılar dahil) karşılık
    GELMEDİĞİNDE (bkz. `Infrastructure._infrastructure_type_on_donustur`)
    fırlatılır — `GecersizFacilityTuruAtlandi` İLE AYNI SINIF bir güvence,
    Infrastructure'a GENİŞLETİLMİŞ hali.

    Kanıtlanmış bir boşluk için savunma (ör. "Marmaris Orman Yangını"
    senaryosu): bu sınıf ESKİDEN YOKTU — `Infrastructure`nin kendi
    validator'ı BİLİNÇLİ olarak "Facility'nin AKSİNE burada bir 'sessizce
    atlama' YOKTUR, çünkü bir yol/köprü/tünel GERÇEKTEN bir
    Infrastructure'dır" diye belgeliyordu (bkz. o fonksiyonun ESKİ
    docstring'i). Bu varsayım YANLIŞLANDI: LLM, bir orman
    yangını OLAYININ (Event) kendisini tanımlayan "Orman" kelimesini
    yanlışlıkla AYRI bir Infrastructure kaydı (`infrastructure_type=
    'Orman'`) gibi çıkardı — TAM DA Facility'nin "aslında BAŞKA bir varlık
    türü/Event'le karıştırıldı" senaryosunun Infrastructure karşılığı.
    Asıl Event kaydı `events` listesinde AYRICA ve DOĞRU şekilde
    oluştuğundan (bkz. `nlp_parser`daki "OLAY TURU SINIFLANDIRMA
    KURALI"), burada atlanan SADECE fazladan/hiçbir benzersiz bilgi
    taşımayan bir gölge kayıttır — veri kaybı SAYILMAZ."""


# ---------------------------------------------------------------------------
# Ortak / Genel Enum Tanımları
# ---------------------------------------------------------------------------


class NodeLabel(str, Enum):
    """Neo4j düğüm etiketleri (labels)."""

    FACILITY = "Facility"
    INFRASTRUCTURE = "Infrastructure"
    ENERGY_INFRASTRUCTURE = "EnergyInfrastructure"
    COMMUNICATION_NETWORK = "CommunicationNetwork"
    RESOURCE_HUB = "ResourceHub"
    UNIT = "Unit"
    EVENT = "Event"
    ADMINISTRATIVE_AREA = "AdministrativeArea"
    # FAZ 8 EKLENTİSİ (bkz. `Settlement` sınıfının
    # docstring'i): sivil il/ilçe merkezleri.
    SETTLEMENT = "Settlement"


class OperationalStatus(str, Enum):
    """Tüm varlıklar için ortak/genel operasyonel statü.

    Alt sınıflardaki özel statü alanları (ör. Facility.mevcut_durum) bu genel
    alanla birlikte var olabilir; bu alan tüm graf üzerinde genel filtreleme
    ve görselleştirme (ör. haritada renklendirme) için kullanılır.
    """

    AKTIF = "Aktif"
    KISMI_AKTIF = "Kısmi Aktif"
    HASARLI = "Hasarlı"
    YOK_EDILDI = "Yok Edildi"
    BILINMIYOR = "Bilinmiyor"


class FacilityType(str, Enum):
    HAVALIMANI = "Havalimani"
    HASTANE = "Hastane"
    ASKERI_US = "Askeri Us"
    LIMAN = "Liman"
    SIGINAK = "Siginak"


class FacilityStatus(str, Enum):
    """Facility.mevcut_durum için istenen özel statü kümesi."""

    AKTIF = "Aktif"
    HASARLI = "Hasarlı"
    YOK_EDILDI = "Yok Edildi"


class InfrastructureType(str, Enum):
    KARAYOLU = "Karayolu"
    DEMIRYOLU = "Demiryolu"
    KOPRU = "Kopru"
    TUNEL = "Tunel"
    # FAZ 1 EKLENTISI: sokak-seviyesi ulusal kapsama icin.
    VIYADUK = "Viyaduk"
    SOKAK = "Sokak"


# "PYDANTIC SEVİYESİNDE KATI ÖNLEMLER" arama tabloları (bkz. modül-üstü
# `_tr_katla`/`GecersizFacilityTuruAtlandi` notu) — HER İKİ tablo da
# "katlanmış (folded) anahtar -> DOĞRU/kanonik Enum değer string'i" eşler;
# `Facility`/`Infrastructure`nin `model_validator(mode='before')`ları bu
# tabloları kullanır.
_FACILITY_TYPE_TABLOSU: Dict[str, str] = {_tr_katla(uye.value): uye.value for uye in FacilityType}
"""SADECE gerçek `FacilityType` değerlerini (+ TR-karakter/büyük-küçük
harf/boşluk varyasyonlarını) içerir — tasarım gereği Facility
için herhangi bir "es-anlamlı düzeltme" YOKTUR, SADECE "geçerli mi
değil mi" (ve geçerliyse doğru case'e normalize) kontrolü yapılır."""

_INFRASTRUCTURE_TYPE_ES_ANLAMLILARI: Dict[str, str] = {
    "yol": InfrastructureType.KARAYOLU.value,
    "yolu": InfrastructureType.KARAYOLU.value,
    "anayol": InfrastructureType.KARAYOLU.value,
    "anayolu": InfrastructureType.KARAYOLU.value,
    "anayollar": InfrastructureType.KARAYOLU.value,
    "anayollari": InfrastructureType.KARAYOLU.value,
    "devletyolu": InfrastructureType.KARAYOLU.value,
    "ilyolu": InfrastructureType.KARAYOLU.value,
    "otoyol": InfrastructureType.KARAYOLU.value,
    "otoban": InfrastructureType.KARAYOLU.value,
    "baglantiyolu": InfrastructureType.KARAYOLU.value,
    "gecit": InfrastructureType.KARAYOLU.value,
}
"""`nlp_parser._INFRASTRUCTURE_TYPE_ES_ANLAMLILAR` İLE AYNI eş-anlamlı
küme — "Yumuşak (prompt) kuralları bırakıp Pydantic seviyesinde KATI
önlemlere geçiyoruz" ilkesi gereği bu, ARTIK ana/birincil savunma
hattıdır; `nlp_parser.py`deki karşılığı bu yüzden KALDIRILDI (aynı
mantığın iki farklı yerde BAKIMSIZ kalma riskini taşıyan bir kopyası
olarak tutulmadı, bkz. o dosyadaki güncel yorum)."""

_INFRASTRUCTURE_TYPE_TABLOSU: Dict[str, str] = {
    **{_tr_katla(uye.value): uye.value for uye in InfrastructureType},
    **_INFRASTRUCTURE_TYPE_ES_ANLAMLILARI,
}
"""Gerçek `InfrastructureType` değerleri + `_INFRASTRUCTURE_TYPE_ES_
ANLAMLILARI`nin BİRLEŞİMİ — sözlük birleştirme SIRASI ÖNEMLİDİR: gerçek
Enum değerleri ÖNCE yazılır ki bir es-anlamlı YANLIŞLIKLA gerçek bir Enum
anahtarının ÜZERİNE YAZAMASIN (pratikte çakışma yoktur, ama bu sıralama
niyeti/güvenliği AÇIKÇA belgeler)."""


class SurfaceType(str, Enum):
    """Infrastructure.zemin_tipi — güzergahın yüzey/kaplama türü.

    Şehir içi ana arterlerden kırsal stabilize yollara kadar ulusal ölçekte
    gerçekçi kapsama için "asfalt/toprak"ın ötesinde beton ve parke taşı
    (özellikle eski mahalle sokaklarında yaygın) da eklenmiştir.
    """

    ASFALT = "Asfalt"
    BETON = "Beton"
    TOPRAK = "Toprak"
    STABILIZE = "Stabilize"
    PARKE = "Parke"


class EnergyInfrastructureType(str, Enum):
    BARAJ = "Baraj"
    TRAFO = "Trafo"
    SANTRAL = "Santral"


class BackupPowerStatus(str, Enum):
    """Yedek güç durumu."""

    YOK = "Yok"
    KISMI = "Kismi"
    TAM = "Tam"


class CommunicationNetworkType(str, Enum):
    BAZ_ISTASYONU = "Baz Istasyonu"
    FIBER = "Fiber"
    UYDU_TERMINALI = "Uydu Terminali"


class ResourceType(str, Enum):
    YAKIT = "Yakit"
    GIDA = "Gida"
    SU = "Su"
    TIBBI_MALZEME = "Tibbi Malzeme"
    MUHIMMAT = "Muhimmat"


class UnitType(str, Enum):
    ASKERI_BIRLIK = "Askeri Birlik"
    AFAD = "AFAD"
    SAGLIK = "Saglik"
    ITFAIYE = "Itfaiye"
    POLIS = "Polis"
    # "KARAR DERINLIGI" EKLENTISI (bkz. `synthetic_unit_seeder.py`): bir
    # kopru/yol yikimi gibi altyapi felaketinde asil ihtiyac duyulan
    # birim turu cogu zaman Polis/Askeri DEGIL, agir insaat ekipmanidir
    # (Karayollari vinc/dozer, ozel sektor agir makine parki). Bu tip
    # birimi zorla "Askeri Birlik" gibi baska bir kategoriye SIKISTIRMAK
    # -bu projenin tekrar tekrar duzelttigi "yanlis siniflandirma"
    # sorunuyla AYNI sinif bir hata olurdu- yerine kendi dogru kategorisi
    # eklenmistir.
    AGIR_MUHENDISLIK = "Agir Muhendislik"
    # Kanıtlanmış bir boşluk için savunma (Pydantic ValidationError:
    # `input_value='Tıbbi'`): LLM bazen SAGLIK'in kendisini degil, onun
    # OPERASYONEL karsiligi olan bagimsiz bir "arama-kurtarma ekibi"
    # veya "lojistik/ikmal birimi" onerir — bunlar SAGLIK/AFAD'in
    # icine ZORLA SIKISTIRILAMAYACAK kadar farkli gorevlere sahiptir
    # (tipki `AGIR_MUHENDISLIK`in Askeri/Polis'ten AYRI eklenmesindeki
    # gerekce gibi): arama-kurtarma enkaz altindan canli/olu cikarma
    # yetenegidir (Saglik'in tedavi gorevinden FARKLI), lojistik ise
    # yakit/gida/su/malzeme ikmal-tedarik zinciridir (mudahale eden
    # BIRLIGIN kendisi degildir). "Tıbbi"/"Medikal"/"Ambulans" gibi
    # SAGLIK'in DUZ es-anlamlilari ise burada YENI bir uye OLARAK
    # eklenmez (bu, ayni gercek kategoriyi ikiye bolup `decision_engine`
    # birim-onceligi haritalarini parcalar) — bunlar `nlp_parser.
    # _UNIT_TYPE_ES_ANLAMLILAR` uzerinden dogrudan SAGLIK'e eslenir.
    ARAMA_KURTARMA = "Arama Kurtarma"
    LOJISTIK = "Lojistik"
    # "İL/İLÇE SİYASİ HARİTA KİLİDİ" ile BİRLİKTE EKLENEN tamamlayıcı:
    # Gemi Kazası/Tsunami
    # gibi YENİ kıyı türleri (bkz. `EventType`) eskiden mecburen Arama
    # Kurtarma/AFAD'a sıkıştırılıyordu — ama denizde/kıyıda asıl birincil
    # müdahale/arama-kurtarma yetkilisi GERÇEKTE Sahil Güvenlik'tir; bu,
    # `AGIR_MUHENDISLIK`in "asıl ihtiyaç Karayolları'dır, Askeri/Polis
    # DEĞİL" gerekçesiyle AYNI sınıf bir düzeltmedir.
    SAHIL_GUVENLIK = "Sahil Guvenlik"


class MobilityStatus(str, Enum):
    """Birimin hareket kabiliyeti (ŞU AN hareket edebilir mi — bir DURUM).

    `UnitMovementType` (hareket_tipi) ile KARIŞTIRILMAMALIDIR: bu alan
    birimin doğası gereği NASIL hareket ettiğini değil, mevcut operasyonel
    kabiliyetini (ör. yakıtsız kaldığı için "Statik" hale gelmiş bir
    motorize birlik) tanımlar.
    """

    STATIK = "Statik"
    SINIRLI_HAREKETLI = "Sinirli Hareketli"
    TAM_HAREKETLI = "Tam Hareketli"


class UnitMovementType(str, Enum):
    """Unit.hareket_tipi — birimin DOĞASI gereği hareket şekli (sabit bir
    özellik; `MobilityStatus`'un aksine operasyonel duruma göre değişmez).
    """

    MOTORIZE = "Motorize"
    HAVA_INDIRME = "Hava Indirme"
    YAYA = "Yaya"


class UnitSpecialization(str, Enum):
    """Unit.uzmanlik_alani — birimin taşıdığı özel ihtisas yeteneği.

    Opsiyoneldir (`None` = özel bir ihtisası olmayan genel birim, ör. sıradan
    bir devriye).
    """

    KBRN = "KBRN"
    ENKAZ = "Enkaz"
    SIHHIYE = "Sihhiye"


class EventType(str, Enum):
    SAVAS = "Savas"
    DEPREM = "Deprem"
    SEL = "Sel"
    YANGIN = "Yangin"
    SIBER_SALDIRI = "Siber Saldiri"
    # FAZ 1 EKLENTISI: "Savas" ile KARISTIRILMAMASI GEREKEN, kasit/duşman
    # unsuru OLMAYAN patlama/kaza turu olaylar icin (ör. dogal gaz patlamasi,
    # endustriyel kaza) — bkz. `nlp_parser`daki "OLAY TURU SINIFLANDIRMA
    # KURALI"; bu ayrim olmadan LLM her patlamayi "Savas" olarak
    # etiketleme egilimindeydi (tek "silahli catisma hissi veren" secenek
    # oydu).
    PATLAMA = "Patlama"
    # "ULUSAL OLCEKLI ESNEKLIK" (sistemi tek bir yerel senaryoya/dar
    # altyapi sablonuna "overfit" olmaktan kurtarmak icindir): Turkiye
    # capinda GERCEKTEN sik gorulen ama eskiden bu
    # semada karsiligi OLMAYAN afet/kriz turleri. Her biri decision_engine.
    # _OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI'nde AYRI bir birim-onceligine
    # sahiptir (bkz. o sozlugun guncellenmis hali) — SADECE isim olarak
    # eklenmis "kozmetik" degerler DEGILDIR, karar motoru bunlari
    # GERCEKTEN farkli sekilde yorumlar.
    ORMAN_YANGINI = "Orman Yangini"
    """Genel "Yangin"dan (sehir ici/bina yangini) BILEREK AYRI: orman
    yanginlari farkli bir mudahale profili (arazi araci, hava destegi,
    genis alan) gerektirir — bkz. `synthetic_unit_seeder.KRIZ_TIPLERI` ve
    o dosyadaki "Orman Itfaiyesi" birim sablonlari."""
    CIG = "Cig"
    """Cig (avalanche) — daglik/kirsal bolgelerde (bkz. `synthetic_unit_
    seeder.py`daki Sivrice/Baskil gibi daglik cografya ornekleri) gercek
    bir tehlike sinifidir; arama-kurtarma + saglik onceliklidir."""
    HEYELAN = "Heyelan"
    """Heyelan (landslide) — Deprem/Sel gibi buyuk olcekli DEGIL ama
    ALTYAPI (yol/koprunun molozla KAPANMASI) acisindan "Kopru/Yol Yikimi"
    ile AYNI mudahale mantigini paylasir (bkz. `_ALTYAPI_ONCELIKLI_BIRIM_
    TIPLERI` — Agir Muhendislik/AFAD onceliklidir, enkazi/molozu kaldirip
    guzergahi fiziksel olarak yeniden acabilecek TEK birim turu budur)."""
    TEROR = "Teror"
    """Teror — "Savas" gibi bilincli/dusmanca bir eylem ama ULUSLARARASI
    bir catisma DEGIL, ic guvenlik/kolluk odakli bir tehdittir; bu yuzden
    "Savas" ile AYNI (Askeri Birlik/Polis onceliki) mantik uygulanir ama
    raporlama/siniflandirma acisindan AYRI tutulur (bkz. nlp_parser'daki
    "OLAY TURU SINIFLANDIRMA KURALI" — kasit/dusman unsuru olan ama askeri
    catisma OLMAYAN olaylar icin)."""
    TAHLIYE = "Tahliye"
    """Tahliye — DIGERLERINDEN farkli olarak bir TEHLIKENIN KENDISI degil,
    bizzat bir mudahale/onlem OLAYIdIR (ör. "X bolgesinde onleyici tahliye
    emri verildi" gibi, tetikleyen tehlike metinde acikca
    belirtilmemis/ayri raporlanmis olabilir). Yine de gecerli, bagimsiz
    raporlanabilir bir olay turudur — AFAD/Saglik (lojistik) + Polis
    (guvenlik/trafik) onceliklidir."""

    # Bu 16 kriz turu ile taksonomi genisletilmistir: eskiden bu semada
    # karsiligi OLMAYAN, ama Turkiye capinda GERCEKTEN sik gorulen krizler modeli
    # "her seye Patlama de" davranisina ZORLUYORDU (bkz. `nlp_parser.py`daki
    # "OLAY TURU SINIFLANDIRMA KURALI" — o dosyada da AYNI genisletme
    # UYGULANMISTIR, aksi halde LLM bu yeni degerlerin VARLIGINDAN habersiz
    # kalirdi). Her biri `decision_engine._OLAY_TURU_ONCELIKLI_BIRIM_
    # TIPLERI`nde KENDI gercek mudahale profiline gore AYRICA eslenmistir —
    # SADECE isim olarak eklenmis kozmetik degerler DEGILDIR.
    SALGIN_HASTALIK = "Salgin Hastalik"
    """Salgın Hastalık — toplu/bulaşıcı bir sağlık krizi (ör. kolera,
    zehirlenme dışı epidemi, pandemi). Bina/altyapı hasarı DEĞİL, doğrudan
    Sağlık/AFAD kapasitesini (karantina, saha hastanesi, lojistik ikmal)
    zorlayan bir olay türüdür."""
    IZDIHAM = "Izdiham"
    """İzdiham — kalabalık bir etkinlik/toplanmada (konser, maç, dini
    tören, göç/sınır kapısı yığılması vb.) sıkışma/çiğnenme kaynaklı toplu
    yaralanma; Sağlık + Polis (kalabalık yönetimi) öncelikli, tipik olarak
    TEK bir nokta/bina değil GENİŞ bir insan kütlesi etkilenir."""
    GEMI_KAZASI = "Gemi Kazasi"
    """Gemi Kazası — bir kıyı şeridi/deniz/liman/boğaz üzerinde meydana
    gelen deniz taşımacılığı kazası. DOĞASI GEREĞİ kıyısı olan bir ilde
    gerçekleşir (bkz. `turkiye_harita.cografi_on_kontrol` — coğrafi
    ön-kontrolün kıyı-zorunluluğu uyguladığı türlerden biridir)."""
    KIMYASAL_SIZINTI = "Kimyasal Sizinti"
    """Kimyasal Sızıntı — endüstriyel tesis/tanker/boru hattı kaynaklı
    zehirli/tehlikeli madde sızıntısı. "Patlama"dan BİLEREK AYRI: patlama
    ANLIK bir enerji açığa çıkışıyken, sızıntı SÜREGELEN bir kontaminasyon/
    KBRN tehdididir (bkz. `models.UnitSpecialization.KBRN`) — İtfaiye/
    Sağlık'ın yanı sıra KBRN ihtisaslı birimler ÖZELLİKLE aranmalıdır."""
    UCAK_KAZASI = "Ucak Kazasi"
    """Uçak Kazası — sivil/askeri bir hava aracının düşmesi/zorunlu inişi.
    Havalimanı/Askeri Üs facility tipleriyle KARIŞTIRILMAMALIDIR (bkz.
    `FacilityType`) — bu, o tesislerin KENDİSİ değil, bir HAVA ARACININ
    başına gelen bir OLAYdIR."""
    TREN_KAZASI = "Tren Kazasi"
    """Tren Kazası — raylı sistem (Demiryolu, bkz. `InfrastructureType.
    DEMIRYOLU`) üzerinde meydana gelen kaza/raydan çıkma; Uçak Kazası ile
    AYNI "toplu yaralanma + enkaz kurtarma" mudahale sinifini paylasir."""
    TRAFIK_KAZASI = "Trafik Kazasi"
    """Trafik Kazası — bir karayolu üzerinde meydana gelen (tipik olarak
    çok araçlı/zincirleme) büyük ölçekli bir kaza; Sağlık + Polis (trafik
    kontrolü) öncelikli, GENELLİKLE altyapının KENDİSİNİ (yolun fiziksel
    bütünlüğünü) DEĞİL, ÜZERİNDEKİ araçları/insanları etkiler — bu yüzden
    `_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI` (Ağır Mühendislik) İLE
    KARIŞTIRILMAMALIDIR."""
    BARAJ_COKMESI = "Baraj Cokmesi"
    """Baraj Çökmesi — bir `EnergyInfrastructure` (bkz.
    `EnergyInfrastructureType.BARAJ`) yapısal bütünlüğünü kaybetmesi;
    aşağı havzada ANİ ve GENİŞ ÖLÇEKLİ bir sel/tahliye zorunluluğu
    doğurur — "Sel"den BİLEREK AYRI: tetikleyici doğal (yağış) değil
    YAPISAL bir altyapı arızasıdır, birincil ihtiyaç Ağır Mühendislik +
    kitlesel tahliyedir."""
    MADEN_KAZASI = "Maden Kazasi"
    """Maden Kazası — yer altı/yer üstü bir maden ocağında göçük/gaz
    patlaması/su baskını kaynaklı hapsolma; Arama Kurtarma (enkaz altı/yer
    altı) + Sağlık + Ağır Mühendislik (galeri açma) öncelikli, kendine özgü
    bir kurtarma profili taşır."""
    FIRTINA = "Firtina"
    """Fırtına — şiddetli rüzgar/hortum/lodos kaynaklı geniş alanlı hasar
    (çatı uçması, ağaç/direk devrilmesi, elektrik kesintisi); Sel/Heyelan'ın
    AKSİNE su/toprak kütlesi DEĞİL, RÜZGAR kaynaklı bir tehlikedir — AFAD +
    İtfaiye + Ağır Mühendislik (enkaz/döküntü temizliği) öncelikli."""
    TSUNAMI = "Tsunami"
    """Tsunami — deniz tabanındaki bir depremin/heyelanın tetiklediği dev
    dalga; SADECE kıyı şeridinde fiziksel olarak MÜMKÜNDÜR (bkz.
    `turkiye_harita.cografi_on_kontrol` — bu modülün "İç Anadolu'da
    Tsunami" gibi imkansız öncülleri reddetmek için yazılmasına doğrudan
    yol açan örnek olay türüdür)."""
    RADYASYON_SIZINTISI = "Radyasyon Sizintisi"
    """Radyasyon Sızıntısı — nükleer santral/radyoaktif madde kaynaklı
    kontaminasyon tehdidi; KBRN ihtisaslı birimler + Askeri Birlik (geniş
    çaplı bölge kontrolü) öncelikli, Kimyasal Sızıntı ile AYNI KBRN
    sınıfını paylaşır ama çok daha geniş bir tahliye yarıçapı gerektirir."""
    VOLKANIK_PATLAMA = "Volkanik Patlama"
    """Volkanik Patlama — bir yanardağın (ör. Erciyes/Ağrı Dağı/Nemrut
    bölgesi) patlaması/kül püskürtmesi; SADECE bilinen volkanik sahalara
    yakın illerde fiziksel olarak MÜMKÜNDÜR (bkz.
    `turkiye_harita.cografi_on_kontrol`)."""
    BINA_COKMESI = "Bina Cokmesi"
    """Bina Çökmesi — Deprem/Patlama gibi HARİCİ bir tetikleyici
    belirtilmeden, doğrudan bir yapının (imar/zemin kusuru, aşırı yük vb.
    nedenle) KENDİLİĞİNDEN çökmesi; Arama Kurtarma (enkaz altı) + AFAD +
    Ağır Mühendislik öncelikli — eskiden bu tür bir olay (tetikleyicisi
    belirtilmeyen bir çökme) "Patlama"ya sıkıştırılıyordu (bkz.
    `nlp_parser.py`daki eski "EMIN OLAMADIGIN durumlarda Patlama SEC"
    kuralı), artık KENDİ doğru kategorisi vardır."""
    TOPLU_ZEHIRLENME = "Toplu Zehirlenme"
    """Toplu Zehirlenme — gıda/su/gaz kaynaklı, TEK bir olayda çok sayıda
    kişiyi etkileyen zehirlenme vakası; Sağlık öncelikli, Salgın
    Hastalık'tan BİLEREK AYRI: bulaşıcı DEĞİL, ORTAK bir kaynaktan (aynı
    yemek/su/gaz) TEK SEFERLİK bir maruziyettir."""
    KURAKLIK = "Kuraklik"
    """Kuraklık — uzun süreli yağış yetersizliği kaynaklı su/tarım krizi;
    DİĞER türlerin aksine ANİ değil YAVAŞ GELİŞEN (slow-onset) bir kriz
    türüdür — AFAD + Lojistik (su/gıda ikmali) öncelikli."""


class EventSeverity(str, Enum):
    """Event.siddet için niteliksel şiddet seviyesi (1-5 sayısal siddet
    alanına ek olarak kullanılabilir)."""

    DUSUK = "Dusuk"
    ORTA = "Orta"
    YUKSEK = "Yuksek"
    KRITIK = "Kritik"
    KATASTROFIK = "Katastrofik"


class AdministrativeAreaType(str, Enum):
    """AdministrativeArea.alan_tipi — Türkiye idari hiyerarşisindeki düzey."""

    MAHALLE = "Mahalle"
    KOY = "Koy"
    ILCE = "Ilce"
    BOLGE = "Bolge"


# ---------------------------------------------------------------------------
# İlişki (Edge) Tanımları
# ---------------------------------------------------------------------------


class RelationshipType(str, Enum):
    """Düğümler arası olası ilişki tipleri (Neo4j relationship type)."""

    CONNECTED_TO = "CONNECTED_TO"       # Infrastructure <-> Infrastructure/Facility
    DEPENDS_ON = "DEPENDS_ON"           # Facility -> EnergyInfrastructure vb.
    AFFECTS = "AFFECTS"                 # Event -> herhangi bir varlık
    STATIONED_AT = "STATIONED_AT"       # Unit -> Facility
    SUPPLIES = "SUPPLIES"               # ResourceHub -> Facility/Unit
    ROUTES_THROUGH = "ROUTES_THROUGH"   # Unit/Resource akışı -> Infrastructure
    CONTROLS = "CONTROLS"               # Unit -> Facility/Infrastructure
    PROTECTS = "PROTECTS"               # Unit -> Facility
    THREATENS = "THREATENS"             # Event -> Facility/Unit
    COMMUNICATES_WITH = "COMMUNICATES_WITH"  # CommunicationNetwork <-> CommunicationNetwork
    LOCATED_NEAR = "LOCATED_NEAR"       # Genel mekansal yakınlık
    # FAZ 1 EKLENTISI: yeni AdministrativeArea dugumunu grafa baglayabilmek
    # icin gereken minimum iliski. Facility/Unit/Event/Infrastructure -> AdministrativeArea.
    LOCATED_IN = "LOCATED_IN"
    # FAZ 2 EKLENTISI: idari hiyerarsi (ör. Mahalle -> Ilce -> Bolge).
    # AdministrativeArea -> AdministrativeArea (kaynak = ALT birim, hedef = UST birim).
    PART_OF = "PART_OF"


class Relationship(BaseModel):
    """İki düğüm arasındaki ilişkiyi (edge) temsil eder.

    `kaynak_id` ve `hedef_id`, ilgili `BaseNode.id` alanlarına karşılık gelir.
    """

    model_config = ConfigDict(use_enum_values=False)

    kaynak_id: str = Field(..., description="Kaynak düğümün id'si")
    hedef_id: str = Field(..., description="Hedef düğümün id'si")
    tip: RelationshipType = Field(..., description="İlişki tipi")
    agirlik: Optional[float] = Field(
        default=None, description="İlişkinin ağırlığı (ör. mesafe_km, kapasite)"
    )
    aktif_mi: bool = Field(default=True, description="İlişki şu an geçerli/aktif mi")
    ozellikler: Dict[str, Any] = Field(
        default_factory=dict, description="Ek serbest-form ilişki özellikleri"
    )
    olusturulma_tarihi: datetime = Field(default_factory=_utcnow)

    def to_cypher_properties(self) -> Dict[str, Any]:
        """İlişkiyi Neo4j'e yazılabilir düz (flat) bir property sözlüğüne çevirir."""
        props: Dict[str, Any] = {
            "agirlik": self.agirlik,
            "aktif_mi": self.aktif_mi,
            "olusturulma_tarihi": self.olusturulma_tarihi.isoformat(),
        }
        props.update(self.ozellikler)
        return {k: v for k, v in props.items() if v is not None}


# ---------------------------------------------------------------------------
# Temel Düğüm (Base Node) Sınıfı
# ---------------------------------------------------------------------------


class BaseNode(BaseModel, ABC):
    """Tüm graf varlıkları için ortak alanları taşıyan soyut temel sınıf.

    Alt sınıflar `neo4j_label` class attribute'unu override etmelidir.
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)

    # Neo4j etiketi -- her somut alt sınıf tarafından override edilmelidir.
    neo4j_label: NodeLabel = NodeLabel.FACILITY

    # --- FAZ 2 MIMARI KARARI: Multi-labeling (bkz. `neo4j_labels`) ---
    # Alt siniflar bu iki ClassVar'i OVERRIDE ederek, kategori etiketine
    # (`neo4j_label`) EK OLARAK somut alt-tiplerini yansitan ikincil bir
    # Neo4j etiketi de kazanabilir (ör. Infrastructure + Kopru -> "Bridge").
    # `ClassVar` oldugu icin Pydantic tarafindan model ALANI SAYILMAZ (semaya/
    # serilestirmeye dahil olmaz), sadece duz bir sinif sabitidir.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = None
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {}

    id: str = Field(default_factory=lambda: str(uuid4()), description="Benzersiz düğüm kimliği")
    isim: str = Field(..., description="Varlığın adı")
    aciklama: Optional[str] = Field(default=None, description="Serbest metin açıklama")

    # FAZ 3 EKLENTISI (COK-SEHIR/ULUSAL OLCEKLENME): Bu dugumun ait oldugu
    # operasyon bolgesi/il (ör. "Elazığ", "Malatya") — `real_osm_loader.
    # RealOsmLoader` her dugumu KENDI kaynak sehriyle doldurur. GERI ALMA
    # NOTU: Bu alan SALT bir provenance/koken bilgisidir — `src.ui.app`
    # (bir Karar Destek Sistemi'nin C4ISR dogasina aykiri bulunan bolgesel
    # filtreleme KALDIRILDIGI icin) bunu ARTIK herhangi bir sorguyu
    # filtrelemek icin KULLANMAZ; harita/GraphRAG TUM bolgeleri tek bir
    # butun ag olarak gosterir/degerlendirir. Alan, ileride gerekebilecek
    # analiz/hata ayiklama (ör. "bu dugum hangi ilden geldi?") icin
    # SAKLANIR ve `decision_engine`/`database`daki opsiyonel `bolge`
    # parametreleriyle (programatik/betik kullanimi icin) hala okunabilir.
    bolge: Optional[str] = Field(
        default=None,
        description="Operasyon bölgesi/il adı (ör. 'Elazığ') — çok-şehir mimarisinde filtreleme için",
    )

    # Koordinatlar
    enlem: float = Field(..., ge=-90.0, le=90.0, description="Enlem (latitude)")
    boylam: float = Field(..., ge=-180.0, le=180.0, description="Boylam (longitude)")

    # Genel statü
    durum: OperationalStatus = Field(
        default=OperationalStatus.BILINMIYOR, description="Genel operasyonel statü"
    )

    # Zaman damgaları
    olusturulma_tarihi: datetime = Field(default_factory=_utcnow)
    guncelleme_tarihi: datetime = Field(default_factory=_utcnow)

    def to_cypher_properties(self) -> Dict[str, Any]:
        """Pydantic modelini Neo4j'e yazılabilir düz bir property sözlüğüne
        dönüştürür (enum -> value, datetime -> ISO string, `neo4j_label` hariç).
        """
        data = self.model_dump(exclude={"neo4j_label"})
        return self._flatten(data)

    @staticmethod
    def _flatten(data: Dict[str, Any]) -> Dict[str, Any]:
        flat: Dict[str, Any] = {}
        for key, value in data.items():
            flat[key] = BaseNode._coerce(value)
        return flat

    @staticmethod
    def _coerce(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime,)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        return value

    @field_validator("guncelleme_tarihi", mode="before")
    @classmethod
    def _default_guncelleme(cls, v: Any) -> Any:
        return v or _utcnow()

    def neo4j_labels(self) -> List[str]:
        """Bu düğüme uygulanacak TÜM Neo4j etiketlerini (multi-label) döner.

        FAZ 2 MİMARİ KARARI: Milyonlarca düğümlük ulusal ölçekte, TEK bir
        geniş kategori etiketi (ör. sadece `:Infrastructure`) üzerinde
        `WHERE n.infrastructure_type = 'Kopru'` gibi property-filtreli
        sorgular tam etiket taraması (full label scan + filter) gerektirir.
        Bunun yerine her düğüm, kategori etiketine (ör. "Infrastructure") EK
        OLARAK somut alt-tipini yansıtan İKİNCİ bir etiket de alır (ör.
        "Bridge"); böylece `MATCH (n:Bridge)` Neo4j'in NATİF etiket indeksini
        kullanarak, property filtrelemeden çok daha hızlı çalışır.

        İlk eleman HER ZAMAN kategori etiketidir (`neo4j_label.value`, ör.
        "Infrastructure") — bu, `database.node_to_cypher`'daki MERGE anahtarı
        (isim + kategori etiketi) DEĞİŞMEDEN kalsın diye ÖNEMLİDİR (bkz. o
        fonksiyonun docstring'i): ikincil etiket SADECE `SET` ile EKLENİR,
        MERGE'in eşleşme/kimlik mantığına DAHİL EDİLMEZ.
        """
        etiketler = [self.neo4j_label.value]
        alan_adi = self._ALT_TIP_ALANI
        if alan_adi:
            alt_tip_degeri = getattr(self, alan_adi, None)
            if alt_tip_degeri is not None:
                ham_deger = alt_tip_degeri.value if isinstance(alt_tip_degeri, Enum) else alt_tip_degeri
                ikincil_etiket = self._ALT_ETIKET_ESLEMESI.get(ham_deger)
                if ikincil_etiket:
                    etiketler.append(ikincil_etiket)
        return etiketler


# ---------------------------------------------------------------------------
# Somut Düğüm Sınıfları
# ---------------------------------------------------------------------------


class Facility(BaseNode):
    """Havalimanı, Hastane, Askeri Üs vb. sabit tesisler.

    FAZ 1 NOTU: Aşağıdaki `ameliyathane_sayisi`/`yogun_bakim_kapasitesi`
    (SADECE `facility_type=Hastane` için anlamlıdır) ve
    `muhimmat_kapasitesi`/`pist_uzunlugu_metre` (SADECE `Askeri Us`; ikincisi
    ayrıca `Havalimani` için de geçerlidir) alanları, tüm alt-tipler TEK bir
    Neo4j etiketi (`:Facility`) altında kaldığından (mevcut mimariyle
    tutarlılık için, bkz. modül docstring'i) OPSİYONEL tutulmuştur — bir
    `Liman` kaydında bu alanlar basitçe `None` kalır. Tip bazlı ZORUNLULUK
    (ör. Hastane için ameliyathane_sayisi'nin zorunlu olması), gerçek veri
    kaynağının (TUCBS) alan doluluğu netleşmeden erken bağlanmaması için
    BİLİNÇLİ olarak Faz 2/ETL'e bırakılmıştır.
    """

    neo4j_label: NodeLabel = NodeLabel.FACILITY

    # Multi-label (bkz. BaseNode.neo4j_labels): facility_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "facility_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Havalimani": "Airport",
        "Hastane": "Hospital",
        "Askeri Us": "MilitaryBase",
        "Liman": "Port",
        "Siginak": "Shelter",
    }

    facility_type: FacilityType = Field(..., description="Tesis tipi")
    kapasite: float = Field(..., ge=0, description="Tesisin kapasitesi (ör. yatak, uçak/gün)")
    savunma_seviyesi: int = Field(
        default=0, ge=0, le=10, description="Tesisin savunma/güvenlik seviyesi (0-10)"
    )
    mevcut_durum: FacilityStatus = Field(
        default=FacilityStatus.AKTIF, description="Tesisin mevcut fiziksel durumu"
    )

    # --- Hastane'ye özel stratejik kapasite alanları ---
    ameliyathane_sayisi: Optional[int] = Field(
        default=None, ge=0, description="[Sadece Hastane] Aktif ameliyathane sayısı"
    )
    yogun_bakim_kapasitesi: Optional[int] = Field(
        default=None, ge=0, description="[Sadece Hastane] Yoğun bakım (ICU) yatak kapasitesi"
    )

    # --- Askeri Us / Havalimani'na ozel stratejik kapasite alanlari ---
    muhimmat_kapasitesi: Optional[float] = Field(
        default=None, ge=0, description="[Sadece Askeri Us] Depolanabilir mühimmat kapasitesi (ton)"
    )
    pist_uzunlugu_metre: Optional[float] = Field(
        default=None,
        ge=0,
        description="[Askeri Us ve Havalimani] Pist uzunluğu (metre); hangi uçak/kargo tipinin inip kalkabileceğini belirler",
    )

    @model_validator(mode="before")
    @classmethod
    def _facility_type_dogrula_veya_atla(cls, data: Any) -> Any:
        """"PYDANTIC SEVİYESİNDE KATI ÖNLEM" (bkz. modül-üstü
        `GecersizFacilityTuruAtlandi` docstring'i — Rize
        senaryosu): `facility_type` GERÇEK bir `FacilityType` değerine
        (TR-karakter/büyük-küçük harf farkı tolere edilerek) karşılık
        GELMİYORSA (ör. "Heyelan", "Yangın", "Yol" — bunlar `FacilityType`
        Enum'unda ASLA olmayacak, çünkü ya bir Event türü ya da bir
        Infrastructure türüdür), Pydantic'in normal `ValidationError`ını
        (kullanıcıya sarı bir UI uyarısı olarak sızacak olan) ASLA
        FIRLATMAZ — bunun yerine ÖZEL/ayırt edilebilir bir istisna
        (`GecersizFacilityTuruAtlandi`) fırlatır; çağıran taraf (bkz.
        `nlp_parser.OllamaParser._coerce_node`) bunu YAKALAYIP SESSİZCE
        (sadece log seviyesinde) atlar.

        `data` bir dict DEĞİLSE (ör. zaten doğrulanmış bir `Facility`
        örneği yeniden doğrulanıyorsa) DOKUNULMADAN geçirilir — bu
        validator SADECE ham LLM/dict girdisini hedefler.
        """
        if not isinstance(data, dict):
            return data
        ham_deger = data.get("facility_type")
        if not isinstance(ham_deger, str):
            return data
        dogru_deger = _FACILITY_TYPE_TABLOSU.get(_tr_katla(ham_deger))
        if dogru_deger is None:
            raise GecersizFacilityTuruAtlandi(
                f"facility_type='{ham_deger}' gecerli bir FacilityType degeri DEGIL "
                "(muhtemelen bir Event/afet turu veya baska bir varlik kategorisiyle "
                "karistirildi); bu kayit sessizce atlanacak."
            )
        if dogru_deger != ham_deger:
            return {**data, "facility_type": dogru_deger}
        return data


class Infrastructure(BaseNode):
    """Karayolu, Demiryolu, Köprü, Tünel, Viyadük, Sokak vb. bağlantı altyapıları.

    Bir yol/demiryolu tek bir nokta değil, iki nokta arasındaki bir
    GÜZERGAHTIR. `BaseNode.enlem`/`boylam` güzergahın BAŞLANGIÇ noktasını,
    `bitis_enlem`/`bitis_boylam` ise BİTİŞ noktasını taşır; böylece harita
    katmanı (bkz. `src.ui.app`) bu varlığı tek bir pin yerine bir çizgi
    (PyDeck `LineLayer`, Faz 4 öncesi Folium `PolyLine`) olarak çizebilir.
    Bitiş koordinatları LLM çıkarımında
    bulunamazsa (eski veri/None), UI tarafında güzergahın uzunluğuna göre
    tahmini bir bitiş noktası hesaplanır (bkz. `app._tahmini_bitis_noktasi`).

    FAZ 1 NOTU: `tonaj_kapasitesi`, ulusal/sokak-seviyesi kapsamda milyonlarca
    segmentin çoğunda bilinmeyeceği için REQUIRED'dan OPTIONAL'a gevşetilmiştir
    (bu, mevcut çağıranları KIRMAZ — hepsi zaten bir değer sağlıyordu, sadece
    artık zorunlu değil). "max_tonaj" ayrı bir alan olarak EKLENMEMİŞTİR; zaten
    `tonaj_kapasitesi` ile birebir aynı anlamı taşıdığından tekilleştirilmiştir.
    """

    neo4j_label: NodeLabel = NodeLabel.INFRASTRUCTURE

    # Multi-label (bkz. BaseNode.neo4j_labels): infrastructure_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "infrastructure_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Karayolu": "Road",
        "Demiryolu": "Railway",
        "Kopru": "Bridge",
        "Tunel": "Tunnel",
        "Viyaduk": "Viaduct",
        "Sokak": "Street",
    }

    infrastructure_type: InfrastructureType = Field(..., description="Altyapı tipi")
    uzunluk_km: float = Field(..., ge=0, description="Uzunluk (km)")
    acik_mi: bool = Field(default=True, description="Güzergah şu an geçişe açık mı")
    bitis_enlem: Optional[float] = Field(
        default=None, ge=-90.0, le=90.0,
        description="Güzergahın bitiş noktası enlemi (varsa); haritada çizgi çizmek için kullanılır",
    )
    bitis_boylam: Optional[float] = Field(
        default=None, ge=-180.0, le=180.0,
        description="Güzergahın bitiş noktası boylamı (varsa)",
    )

    # Azami tonaj (ör. koprunun/yolun tasiyabildigi max_tonaj). Sokak/patika
    # gibi tiplerde cogunlukla bilinmez/anlami yoktur, bu yuzden opsiyoneldir.
    tonaj_kapasitesi: Optional[float] = Field(
        default=None, ge=0, description="Taşıyabileceği azami tonaj (ton); bilinmiyorsa None"
    )

    # --- FAZ 1 EKLENTILERI: enterprise-seviye lojistik/rota planlama alanlari ---
    max_yukseklik_metre: Optional[float] = Field(
        default=None,
        ge=0,
        description="Azami araç yüksekliği kısıtı (metre) — özellikle Tunel/Kopru/Viyaduk için kritik",
    )
    serit_sayisi: Optional[int] = Field(
        default=None, ge=0, description="Güzergahın toplam şerit sayısı"
    )
    zemin_tipi: Optional[SurfaceType] = Field(
        default=None, description="Yüzey/kaplama türü (Asfalt/Beton/Toprak/Stabilize/Parke)"
    )

    # FAZ 5 EKLENTİSİ (harita LOD/hiyerarşi filtresi): OSM'nin ham "highway"
    # etiketi (ör. "motorway", "primary", "residential", "living_street"),
    # `infrastructure_type="Sokak"` içindeki milyonlarca alt-segmenti KENDİ
    # İÇİNDE önem sırasına göre ayırt edebilmek için BİREBİR saklanır (bkz.
    # `real_osm_loader.RealOsmLoader.build_nodes` ve `src.ui.app
    # .fetch_street_points`). Optional: eski (bu alandan ÖNCE) yüklenmiş
    # düğümlerde `None`'dır; UI bu durumu "sınıfı bilinmiyor" sayıp güvenli
    # tarafta kalarak GÖSTERMEYE devam eder (bkz. `app.py` yorumu).
    highway_tipi: Optional[str] = Field(
        default=None,
        description="OSM 'highway' etiketinin ham değeri (motorway/trunk/primary/secondary/residential/...) — harita LOD filtresi için",
    )

    # "KADEMELİ DİNAMİK YÜKLEME" (Tiered Dynamic Loading)
    # (bkz. `src.data_ingestion.local_osm_reader`):
    # ülke ölçeğinde TÜM sokak-seviyesi veriyi aynı anda RAM'de tutmak
    # yerine, Neo4j'e önce SADECE kalıcı bir "omurga" (motorway/trunk/
    # primary + kritik tesisler) yüklenir; bir kriz tetiklendiğinde, kriz
    # noktasının 15 km çevresindeki "kılcal damarlar" (residential/
    # tertiary vb. sokaklar) yerel diskteki OSM dosyasından ANLIK olarak
    # eklenir. Bu iki alan, SONRADAN eklenen bu GEÇİCİ düğümleri kalıcı
    # omurgadan AYIRT ETMEK için vardır — "RAM Tahliyesi" (garbage
    # collection) fonksiyonu SADECE `gecici_mi=True` olan düğümleri
    # hedefler, omurgaya ASLA dokunmaz.
    gecici_mi: bool = Field(
        default=False,
        description=(
            "TRUE ise bu düğüm kriz-tetiklemeli bir bbox yüklemesiyle SONRADAN "
            "eklenmiş bir 'kılcal damar' (residential/tertiary sokak) düğümüdür; "
            "kalıcı 'omurga' (motorway/trunk/primary + kritik tesis) düğümleri "
            "için HER ZAMAN False'tur."
        ),
    )
    operasyon_id: Optional[str] = Field(
        default=None,
        description=(
            "`gecici_mi=True` ise, bu düğümü hangi kriz-bölgesi yüklemesinin "
            "eklediğini işaretleyen bir kimlik (UUID) — RAM tahliyesi SADECE "
            "belirli bir operasyona ait düğümleri hedefleyebilsin diye."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _infrastructure_type_on_donustur(cls, data: Any) -> Any:
        """"PYDANTIC SEVİYESİNDE KATI ÖNLEM" (bkz. modül-üstü
        `_INFRASTRUCTURE_TYPE_TABLOSU` docstring'i — Rize
        senaryosu: `input_value='Yol'`): `infrastructure_type`, GERÇEK bir
        `InfrastructureType` değerine VEYA bilinen bir es-anlamlıya
        ("Yol", "Ana Yollar", "Otoyol" vb. -> "Karayolu") karşılık
        GELİYORSA, Enum doğrulaması PATLAMADAN ÖNCE sessizce doğru
        kanonik değere çevrilir.

        Kanıtlanmış bir boşluk için savunma (ör. "Marmaris Orman Yangını"
        senaryosu, bkz. modül-üstü `GecersizInfrastructureTuruAtlandi`
        docstring'i): bu fonksiyonun ESKİ hali, "Facility'nin AKSİNE burada
        bir 'sessizce atlama' YOKTUR, çünkü bir yol/köprü/tünel GERÇEKTEN
        bir Infrastructure'dır" diye belgeliyordu — bu
        varsayım YANLIŞLANDI (LLM bazen bir Event'i/olay-bağlamı kelimesini
        [ör. "Orman"] yanlışlıkla Infrastructure sanıyor, TAM DA
        Facility'deki "aslında BAŞKA bir tür" senaryosu). Artık `Facility`
        İLE TUTARLI: değer GERÇEKTEN hiçbir karşılığa (Enum DEĞERİ+
        es-anlamlı) sahip DEĞİLSE, `ValidationError` YERİNE özel/ayırt
        edilebilir `GecersizInfrastructureTuruAtlandi` fırlatılır; çağıran
        taraf (bkz. `nlp_parser.OllamaParser._coerce_node`) bunu
        YAKALAYIP SESSİZCE (sadece log seviyesinde) atlar.
        """
        if not isinstance(data, dict):
            return data
        ham_deger = data.get("infrastructure_type")
        if not isinstance(ham_deger, str):
            return data
        dogru_deger = _INFRASTRUCTURE_TYPE_TABLOSU.get(_tr_katla(ham_deger))
        if dogru_deger == ham_deger:
            return data
        if dogru_deger is None:
            raise GecersizInfrastructureTuruAtlandi(
                f"infrastructure_type='{ham_deger}' gecerli bir InfrastructureType degeri DEGIL "
                "(muhtemelen bir Event/afet turu veya baska bir varlik kategorisiyle "
                "karistirildi); bu kayit sessizce atlanacak."
            )
        return {**data, "infrastructure_type": dogru_deger}


class EnergyInfrastructure(BaseNode):
    """Baraj, Trafo vb. enerji altyapıları."""

    neo4j_label: NodeLabel = NodeLabel.ENERGY_INFRASTRUCTURE

    # Multi-label (bkz. BaseNode.neo4j_labels): energy_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "energy_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Baraj": "Dam",
        "Trafo": "TransformerStation",
        "Santral": "PowerPlant",
    }

    energy_type: EnergyInfrastructureType = Field(..., description="Enerji altyapısı tipi")
    kapasite_mw: float = Field(..., ge=0, description="Üretim/iletim kapasitesi (MW)")
    yedek_guc_durumu: BackupPowerStatus = Field(
        default=BackupPowerStatus.YOK, description="Yedek güç kaynağı durumu"
    )


class CommunicationNetwork(BaseNode):
    """Baz istasyonu, fiber hat vb. iletişim altyapıları."""

    neo4j_label: NodeLabel = NodeLabel.COMMUNICATION_NETWORK

    # Multi-label (bkz. BaseNode.neo4j_labels): network_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "network_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Baz Istasyonu": "CellTower",
        "Fiber": "FiberLine",
        "Uydu Terminali": "SatelliteTerminal",
    }

    network_type: CommunicationNetworkType = Field(..., description="İletişim ağı tipi")
    kapsama_yaricapi_km: float = Field(
        default=0, ge=0, description="Kapsama yarıçapı (km); fiber için 0 olabilir"
    )
    batarya_omru_saat: float = Field(
        default=0, ge=0, description="Şebeke kesintisinde batarya ömrü (saat)"
    )


class ResourceHub(BaseNode):
    """Yakıt, gıda, su gibi kaynak depolama/dağıtım merkezleri."""

    neo4j_label: NodeLabel = NodeLabel.RESOURCE_HUB

    # Multi-label (bkz. BaseNode.neo4j_labels): resource_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "resource_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Yakit": "FuelDepot",
        "Gida": "FoodDepot",
        "Su": "WaterDepot",
        "Tibbi Malzeme": "MedicalSupplyDepot",
        "Muhimmat": "AmmoDepot",
    }

    resource_type: ResourceType = Field(..., description="Kaynak tipi")
    stok_seviyesi_yuzde: float = Field(
        ..., ge=0, le=100, description="Mevcut stok seviyesi (%)"
    )
    tukenme_hizi_gun: float = Field(
        ..., ge=0, description="Mevcut tüketim hızıyla kaç günde tükeneceği"
    )


class Unit(BaseNode):
    """Askeri birlik, AFAD, Sağlık ekipleri gibi hareket kabiliyetine sahip birimler."""

    neo4j_label: NodeLabel = NodeLabel.UNIT

    # Multi-label (bkz. BaseNode.neo4j_labels): unit_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "unit_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Askeri Birlik": "MilitaryUnit",
        "AFAD": "AFAD",
        "Saglik": "HealthTeam",
        "Itfaiye": "FireDepartment",
        "Polis": "Police",
        "Agir Muhendislik": "HeavyEngineering",
        # "Arama Kurtarma"/"Lojistik" icin EKSIK OLAN ikincil etiketler
        # (bu iki tur eklendiginde bu tabloya YANSITILMAMISTI — Event'teki
        # AYNI sinif kucuk tutarlilik duzeltmesi, bkz. o sinifin yorumu) +
        # YENI "Sahil Guvenlik" turu (bkz. UnitType.SAHIL_GUVENLIK).
        "Arama Kurtarma": "SearchAndRescue",
        "Lojistik": "Logistics",
        "Sahil Guvenlik": "CoastGuard",
    }

    unit_type: UnitType = Field(..., description="Birim tipi")
    personel_sayisi: int = Field(..., ge=0, description="Birimdeki personel sayısı")
    hareket_kabiliyeti: MobilityStatus = Field(
        default=MobilityStatus.SINIRLI_HAREKETLI,
        description="Birimin ŞU AN hareket edebilme DURUMU (bkz. UnitMovementType ile farkı için enum docstring'i)",
    )

    # --- FAZ 1 EKLENTILERI: lojistik planlama ve ihtisas eslestirmesi icin ---
    hareket_tipi: Optional[UnitMovementType] = Field(
        default=None, description="Birimin doğası gereği hareket şekli (Motorize/Hava Indirme/Yaya)"
    )
    gunluk_ikmal_ihtiyaci_ton: Optional[float] = Field(
        default=None, ge=0, description="Birimin günlük lojistik/ikmal ihtiyacı (ton)"
    )
    uzmanlik_alani: Optional[UnitSpecialization] = Field(
        default=None, description="Birimin özel ihtisas alanı (KBRN/Enkaz/Sihhiye); yoksa None"
    )


class Event(BaseNode):
    """Savaş, deprem, sel gibi krize yol açan olaylar."""

    neo4j_label: NodeLabel = NodeLabel.EVENT

    # Multi-label (bkz. BaseNode.neo4j_labels): event_type -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "event_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Savas": "War",
        "Deprem": "Earthquake",
        "Sel": "Flood",
        "Yangin": "Fire",
        "Siber Saldiri": "CyberAttack",
        "Patlama": "Explosion",
        # "FAZ 1 EKLENTISI" turleri icin EKSIK OLAN ikincil etiketler
        # (kucuk bir tutarlilik duzeltmesi — bu 5 tur EventType'a
        # eklendiginde bu tabloya YANSITILMAMISTI; islevsel bir hataya yol
        # ACMAZ ["Sonuc: sadece ikincil Neo4j etiketi eksik kalir], ama
        # asagidaki "AKILLANDIRMA" eklentisiyle AYNI tutarlilik ilkesi
        # geregi tamamlanmistir).
        "Orman Yangini": "ForestFire",
        "Cig": "Avalanche",
        "Heyelan": "Landslide",
        "Teror": "Terrorism",
        "Tahliye": "Evacuation",
        # bkz. EventType'daki AYNI basliklı not — 16 YENI kriz turunun her biri kendi ikincil Neo4j
        # etiketini alir (bkz. `BaseNode.neo4j_labels` — milyonlarca
        # dugumlu ulusal olcekte native etiket indeksi kullanabilmek icin).
        "Salgin Hastalik": "Epidemic",
        "Izdiham": "CrowdCrush",
        "Gemi Kazasi": "ShipAccident",
        "Kimyasal Sizinti": "ChemicalSpill",
        "Ucak Kazasi": "PlaneCrash",
        "Tren Kazasi": "TrainAccident",
        "Trafik Kazasi": "TrafficAccident",
        "Baraj Cokmesi": "DamFailure",
        "Maden Kazasi": "MiningAccident",
        "Firtina": "Storm",
        "Tsunami": "Tsunami",
        "Radyasyon Sizintisi": "RadiationLeak",
        "Volkanik Patlama": "VolcanicEruption",
        "Bina Cokmesi": "BuildingCollapse",
        "Toplu Zehirlenme": "MassPoisoning",
        "Kuraklik": "Drought",
    }

    event_type: EventType = Field(..., description="Olay tipi")
    etki_alani_km: float = Field(..., ge=0, description="Olayın etki yarıçapı (km)")
    siddet: EventSeverity = Field(..., description="Olayın niteliksel şiddet seviyesi")
    zaman_damgasi: datetime = Field(
        default_factory=_utcnow, description="Olayın gerçekleştiği/tespit edildiği an"
    )


class AdministrativeArea(BaseNode):
    """Bölge/Mahalle/Köy/İlçe gibi bir coğrafi-idari birimi ve onun
    demografik/risk profilini temsil eden düğüm (FAZ 1 EKLENTİSİ).

    `BaseNode.enlem`/`boylam`, bu birimin TEMSİLİ MERKEZ (centroid) noktasını
    taşır — bir mahalle/bölge doğası gereği bir NOKTA değil bir ALAN
    olduğundan, tam sınır geometrisi gerektiğinde `sinir_noktalari` kullanılır.

    `sinir_noktalari` KASITLI OLARAK `List[Tuple[float, float]]` (enlem/boylam
    ÇİFTLERİNİN listesi) DEĞİL, DÜZLEŞTİRİLMİŞ `[enlem1, boylam1, enlem2,
    boylam2, ...]` biçiminde bir `List[float]`'tır: Neo4j düğüm özellikleri
    iç içe dizi (array-of-array) KABUL ETMEZ, sadece tek bir primitif tipten
    oluşan DÜZ diziler geçerlidir. Bu, `Infrastructure.bitis_enlem/boylam`
    için tek nokta çiftinde yeterli olan "iki ayrı skaler alan" çözümünün,
    keyfi sayıda köşe noktası taşıyan bir POLİGON için doğal genellemesidir.
    """

    neo4j_label: NodeLabel = NodeLabel.ADMINISTRATIVE_AREA

    # Multi-label (bkz. BaseNode.neo4j_labels): alan_tipi -> ikincil etiket.
    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "alan_tipi"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Mahalle": "Neighborhood",
        "Koy": "Village",
        "Ilce": "District",
        "Bolge": "Region",
    }

    alan_tipi: AdministrativeAreaType = Field(
        default=AdministrativeAreaType.MAHALLE, description="İdari birim düzeyi (Mahalle/Koy/Ilce/Bolge)"
    )

    # Nufus/bina_sayisi bu dugumun VAR OLMA sebebidir (Facility'deki
    # ameliyathane_sayisi gibi "ek/incidental" bir alan degil); bu yuzden
    # -diger Faz 1 eklentilerinin aksine- REQUIRED tutulmustur.
    nufus: int = Field(..., ge=0, description="Bölgedeki güncel/tahmini nüfus")
    bina_sayisi: int = Field(..., ge=0, description="Bölgedeki toplam bina sayısı")

    risk_faktoru: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "0.0 (risksiz) - 1.0 (azami risk) arası normalize edilmiş kompozit "
            "afet/kriz risk skoru. Genellikle ETL/analitik katmanda SONRADAN "
            "hesaplanan TÜRETİLMİŞ bir değerdir; bu yüzden (nufus/bina_sayisi'nin "
            "aksine) varsayılanı 0.0'dır."
        ),
    )

    sinir_noktalari: Optional[List[float]] = Field(
        default=None,
        description=(
            "Bölgenin poligon sınırı; [enlem1, boylam1, enlem2, boylam2, ...] "
            "biçiminde DÜZLEŞTİRİLMİŞ liste (bkz. sınıf docstring'i). "
            "Yoksa (None) sadece merkez nokta (enlem/boylam) temsilî olarak kullanılır."
        ),
    )


class SettlementType(str, Enum):
    """Settlement.yerlesim_tipi — OSM idari sınır ilişkisinin `admin_level`
    değerinden türetilir (4 = il, 6 = ilçe; bkz. `add_settlements.py`)."""

    IL_MERKEZI = "Il Merkezi"
    ILCE_MERKEZI = "Ilce Merkezi"


class Settlement(BaseNode):
    """Sivil bir yerleşim yerini (il/ilçe merkezi) temsil eden düğüm (FAZ 8
    EKLENTİSİ): Karavul bir afet yönetim sistemi olduğu
    için veritabanında sivil Yerleşim Yerleri'nin bulunması gerekir.

    "NEDEN `AdministrativeArea` DEĞİL DE YENİ BİR DÜĞÜM TİPİ" (bilinçli
    mimari karar): `AdministrativeArea` (bkz. yukarısı) ZATEN "Ilce"yi bir
    `alan_tipi` değeri olarak İÇERİYOR — ama o sınıfın `nufus`/`bina_sayisi`
    alanları BİLİNÇLİ OLARAK REQUIRED'dır (bkz. o sınıfın docstring'i: "bu
    dugumun VAR OLMA sebebidir"), çünkü `AdministrativeArea` sentetik/TUCBS
    tabanlı bir ETL'den (bkz. `tucbs_etl_loader.py`) beslenmek üzere
    tasarlandı. GERÇEK OSM idari sınır verisinde (bkz. `add_settlements.py`)
    nüfus/bina sayısı GÜVENİLİR biçimde HER il/ilçe için MEVCUT DEĞİLDİR —
    bu alanları REQUIRED bir sınıfa zorlamak ya (a) binlerce yerleşim için
    SAHTE/uydurma bir nüfus değeri yazmayı (bu projenin HER YERDE reddettiği
    "asla sahte veri" ilkesine doğrudan aykırı) ya da (b) `AdministrativeArea`
    sınıfının VAR OLMA sebebi olan bu alanları Optional'a çevirip MEVCUT
    TUCBS ETL akışını bozmayı gerektirirdi. Bunun yerine, `nufus`u GERÇEKTEN
    Optional (OSM nadiren sağlar, yoksa dürüstçe `None` kalır) tutan, hafif
    ve BAĞIMSIZ bir `Settlement` düğüm tipi eklenmiştir — iki tip AYNI
    coğrafi kavramı (yerleşim) FARKLI güven/eksiksizlik seviyelerinde temsil
    eder, bu yüzden BİLİNÇLİ olarak ayrı tutulmuştur.

    `BaseNode.enlem`/`boylam`, OSM'in idari sınır İLİŞKİSİNİN (`relation`)
    Overpass `out center` ile hesapladığı TEMSİLİ merkez noktasıdır (tam
    sınır poligonu DEĞİL — `AdministrativeArea.sinir_noktalari`nin aksine,
    bu düğüm SADECE lojistik/mesafe hesapları için bir NOKTA olarak var
    olur, bir risk/nüfus analiz katmanı DEĞİLDİR).
    """

    neo4j_label: NodeLabel = NodeLabel.SETTLEMENT

    yerlesim_tipi: SettlementType = Field(description="İl Merkezi mi, İlçe Merkezi mi (bkz. SettlementType)")
    il: str = Field(..., description="Bağlı olduğu il adı (İl Merkezi kayıtlarında kendi ismiyle AYNIDIR)")
    nufus: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "OSM `population` etiketinden (varsa) alınır — GÜVENİLİR biçimde HER "
            "yerleşim için MEVCUT DEĞİLDİR, bu yüzden Optional'dır; yoksa UYDURULMAZ, "
            "dürüstçe None kalır (bkz. sınıf docstring'indeki 'NEDEN AdministrativeArea "
            "DEĞİL' notu)."
        ),
    )


# Tüm somut düğüm tiplerinin toplu erişim için sözlüğü.
NODE_REGISTRY: Dict[NodeLabel, type[BaseNode]] = {
    NodeLabel.FACILITY: Facility,
    NodeLabel.INFRASTRUCTURE: Infrastructure,
    NodeLabel.ENERGY_INFRASTRUCTURE: EnergyInfrastructure,
    NodeLabel.COMMUNICATION_NETWORK: CommunicationNetwork,
    NodeLabel.RESOURCE_HUB: ResourceHub,
    NodeLabel.UNIT: Unit,
    NodeLabel.EVENT: Event,
    NodeLabel.ADMINISTRATIVE_AREA: AdministrativeArea,
    NodeLabel.SETTLEMENT: Settlement,
}
