"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
İL/İLÇE SİYASİ HARİTA KİLİDİ + COĞRAFİ ÖN-KONTROL.

`decision_engine.py`nin "en yakın birlik" GraphRAG arama mantığı SADECE ham
km-mesafesine dayandığında, sistemin Türkiye'nin idari/siyasi coğrafyası
hakkında hiçbir kavramı olmaz: "Rize'deki bir olaya İstanbul'dan birlik
çağırmak" gibi bir öneri, mesafe hesaplaması bir şekilde bozulur/genişlerse
kod seviyesinde engellenemez — yalnızca `_ARAMA_YARICAPI_KM` gibi sayısal
sabitlerin doğru ayarlanmış olmasına güvenilmiş olur. Aynı şekilde, coğrafi
bir doğrulama katmanı olmadan "İç Anadolu'da Tsunami" gibi fiziksel olarak
imkânsız bir öncül de LLM'e sorgusuzca iletilir.

Bu modül İKİ bağımsız güvence sağlar (ikisi de bu dosyada birlikte
tutulur çünkü ikisi de AYNI temel veriye — "bir koordinat hangi ile
düşer, o ilin komşuları/fiziksel özellikleri nedir" — dayanır):

  1. `en_yakin_il` + `izin_verilen_iller`: `decision_engine.py`nin
     "en yakın birlik/tesis/açık yol" aramalarına, ham km-yarıçapının
     ÜZERİNE, Türkiye'nin GERÇEK il sınırlarına dayanan İKİNCİ, BAĞIMSIZ
     bir coğrafi kilit ekler — bir arama SADECE olayın gerçekleştiği il
     VE o ilin kara sınırı komşuları İÇİNDE kalabilir; km-yarıçapı
     ayarı ne olursa olsun (ör. ileride yanlışlıkla 500 km'ye
     büyütülse bile) bu kilit aşılamaz.
  2. `cografi_on_kontrol`: `DecisionEngine.generate_recommendations`nin
     LLM'e gitmeden ÖNCE çağırdığı bir "Safdillik/Ön-Mantık Filtresi" —
     bir olay TÜRÜNÜN, bulunduğu ilin FİZİKSEL yapısıyla (kıyısı var mı,
     dağlık mı, bilinen bir volkanik saha mı) AÇIKÇA çelişip
     çelişmediğini kontrol eder.

VERİ KALİTESİ NOTU (dürüstlük ilkesi — bu projenin "asla sahte veri"
kuralıyla AYNI sınıf bir açıklama): aşağıdaki `IL_MERKEZ_KOORDINATLARI`
(il merkezi/valilik binası yaklaşık koordinatı), `IL_KOMSULUKLARI` (kara
sınırı komşuluğu) ve `IL_COGRAFI_PROFIL` (kıyı/dağlık/volkanik sınıflaması)
tabloları, GERÇEK genel coğrafya bilgisine dayanan bir REFERANS
tablosudur — TUCBS/resmi kadastro verisi DEĞİLDİR ve santimetrik/hukuki
sınır doğruluğu İDDİA ETMEZ. Bu, bu modülün amacı (operasyonel bir
"mantıklı yarıçap" güvencesi) için YETERLİDİR: amaç bir birliğin/tesisin
santimetrik il sınırını doğrulamak değil, "Rize krizi için Rize/Trabzon/
Artvin/Erzurum dışından (ör. İstanbul'dan) birlik önerme" gibi KABA/AÇIK
coğrafi saçmalıkları engellemektir. `en_yakin_il`, bir NOKTANIN hangi il
SINIRI içinde olduğunu (poligon verisi bu projede henüz YOK — bkz.
`models.AdministrativeArea.sinir_noktalari`nin genel olarak boş kalması)
DEĞİL, hangi il MERKEZİNE en yakın olduğunu (Voronoi yaklaşıklığı)
hesaplar; bu, il sınırına çok yakın (birkaç km) noktalarda komşu ile
KARIŞABİLİR — bu yüzden arama HER ZAMAN komşu illeri de İÇİNE ALIR (bkz.
`izin_verilen_iller`), sadece TEK bir ile DEĞİL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

# ---------------------------------------------------------------------------
# 1) İl merkezi koordinatları (81 il — yaklaşık valilik/şehir merkezi)
# ---------------------------------------------------------------------------

IL_MERKEZ_KOORDINATLARI: Dict[str, Tuple[float, float]] = {
    "Adana": (37.00, 35.32), "Adıyaman": (37.76, 38.28), "Afyonkarahisar": (38.76, 30.54),
    "Ağrı": (39.72, 43.05), "Amasya": (40.65, 35.83), "Ankara": (39.93, 32.85),
    "Antalya": (36.90, 30.71), "Artvin": (41.18, 41.82), "Aydın": (37.85, 27.85),
    "Balıkesir": (39.65, 27.88), "Bilecik": (40.15, 29.98), "Bingöl": (38.88, 40.50),
    "Bitlis": (38.40, 42.11), "Bolu": (40.74, 31.61), "Burdur": (37.72, 30.29),
    "Bursa": (40.18, 29.06), "Çanakkale": (40.15, 26.41), "Çankırı": (40.60, 33.62),
    "Çorum": (40.55, 34.95), "Denizli": (37.77, 29.09), "Diyarbakır": (37.91, 40.24),
    "Edirne": (41.68, 26.56), "Elazığ": (38.68, 39.22), "Erzincan": (39.75, 39.49),
    "Erzurum": (39.90, 41.27), "Eskişehir": (39.78, 30.52), "Gaziantep": (37.07, 37.38),
    "Giresun": (40.92, 38.39), "Gümüşhane": (40.46, 39.48), "Hakkari": (37.58, 43.74),
    "Hatay": (36.20, 36.16), "Isparta": (37.77, 30.56), "Mersin": (36.80, 34.63),
    "İstanbul": (41.01, 28.98), "İzmir": (38.42, 27.14), "Kars": (40.60, 43.09),
    "Kastamonu": (41.38, 33.78), "Kayseri": (38.73, 35.49), "Kırklareli": (41.73, 27.22),
    "Kırşehir": (39.15, 34.16), "Kocaeli": (40.85, 29.88), "Konya": (37.87, 32.48),
    "Kütahya": (39.42, 29.98), "Malatya": (38.35, 38.31), "Manisa": (38.61, 27.43),
    "Kahramanmaraş": (37.57, 36.93), "Mardin": (37.31, 40.74), "Muğla": (37.22, 28.36),
    "Muş": (38.94, 41.13), "Nevşehir": (38.62, 34.72), "Niğde": (37.97, 34.68),
    "Ordu": (40.98, 37.88), "Rize": (41.02, 40.52), "Sakarya": (40.78, 30.40),
    "Samsun": (41.29, 36.33), "Siirt": (37.93, 41.94), "Sinop": (42.03, 35.15),
    "Sivas": (39.75, 37.02), "Tekirdağ": (40.98, 27.51), "Tokat": (40.31, 36.55),
    "Trabzon": (41.00, 39.72), "Tunceli": (39.11, 39.55), "Şanlıurfa": (37.16, 38.79),
    "Uşak": (38.68, 29.41), "Van": (38.49, 43.38), "Yozgat": (39.82, 34.80),
    "Zonguldak": (41.46, 31.79), "Aksaray": (38.37, 34.03), "Bayburt": (40.26, 40.22),
    "Karaman": (37.18, 33.22), "Kırıkkale": (39.85, 33.51), "Batman": (37.88, 41.13),
    "Şırnak": (37.52, 42.46), "Bartın": (41.63, 32.34), "Ardahan": (41.11, 42.70),
    "Iğdır": (39.92, 44.05), "Yalova": (40.65, 29.28), "Karabük": (41.20, 32.63),
    "Kilis": (36.72, 37.12), "Osmaniye": (37.07, 36.25), "Düzce": (40.84, 31.16),
}
"""81 ilin YAKLAŞIK merkez koordinatı — `en_yakin_il`in Voronoi-tarzı
nearest-neighbor sınıflandırması için referans noktası. Bkz. modül
docstring'indeki "VERİ KALİTESİ NOTU"."""

assert len(IL_MERKEZ_KOORDINATLARI) == 81, "Turkiye'nin 81 ili TAM olarak temsil edilmeli."


# ---------------------------------------------------------------------------
# 2) İl komşuluk haritası (kara sınırı — "SİYASİ HARİTA")
# ---------------------------------------------------------------------------
# Simetrik bir iliski oldugundan (A, B'nin komsususa B de A'nin komsusudur),
# hata riskini azaltmak icin TEK YONLU bir kenar (edge) listesi tanimlanir
# ve asagidaki `_komsuluk_haritasi_olustur` HER IKI yonu de otomatik
# ekler — 81x81'lik bir tabloyu elle IKI KEZ (ve ASIMETRIK hata riskiyle)
# yazmak yerine.

_IL_KOMSULUK_KENARLARI: Tuple[Tuple[str, str], ...] = (
    ("Adana", "Mersin"), ("Adana", "Niğde"), ("Adana", "Kayseri"), ("Adana", "Kahramanmaraş"),
    ("Adana", "Osmaniye"), ("Adana", "Hatay"),
    ("Adıyaman", "Malatya"), ("Adıyaman", "Kahramanmaraş"), ("Adıyaman", "Gaziantep"),
    ("Adıyaman", "Şanlıurfa"), ("Adıyaman", "Diyarbakır"),
    ("Afyonkarahisar", "Kütahya"), ("Afyonkarahisar", "Uşak"), ("Afyonkarahisar", "Denizli"),
    ("Afyonkarahisar", "Burdur"), ("Afyonkarahisar", "Isparta"), ("Afyonkarahisar", "Konya"),
    ("Afyonkarahisar", "Eskişehir"),
    ("Ağrı", "Kars"), ("Ağrı", "Iğdır"), ("Ağrı", "Van"), ("Ağrı", "Muş"), ("Ağrı", "Bingöl"),
    ("Ağrı", "Erzurum"),
    ("Amasya", "Tokat"), ("Amasya", "Yozgat"), ("Amasya", "Çorum"), ("Amasya", "Samsun"),
    ("Ankara", "Çankırı"), ("Ankara", "Kırıkkale"), ("Ankara", "Kırşehir"), ("Ankara", "Aksaray"),
    ("Ankara", "Konya"), ("Ankara", "Eskişehir"), ("Ankara", "Bolu"),
    ("Antalya", "Muğla"), ("Antalya", "Burdur"), ("Antalya", "Isparta"), ("Antalya", "Konya"),
    ("Antalya", "Karaman"), ("Antalya", "Mersin"),
    ("Artvin", "Rize"), ("Artvin", "Erzurum"), ("Artvin", "Ardahan"),
    ("Aydın", "Muğla"), ("Aydın", "Denizli"), ("Aydın", "İzmir"), ("Aydın", "Manisa"),
    ("Balıkesir", "Çanakkale"), ("Balıkesir", "Bursa"), ("Balıkesir", "Kütahya"),
    ("Balıkesir", "Manisa"), ("Balıkesir", "İzmir"),
    ("Bilecik", "Bursa"), ("Bilecik", "Sakarya"), ("Bilecik", "Eskişehir"), ("Bilecik", "Kütahya"),
    ("Bingöl", "Erzurum"), ("Bingöl", "Muş"), ("Bingöl", "Diyarbakır"), ("Bingöl", "Elazığ"),
    ("Bingöl", "Tunceli"),
    ("Bitlis", "Van"), ("Bitlis", "Muş"), ("Bitlis", "Siirt"), ("Bitlis", "Batman"),
    ("Bolu", "Çankırı"), ("Bolu", "Zonguldak"), ("Bolu", "Düzce"), ("Bolu", "Sakarya"),
    ("Bolu", "Eskişehir"), ("Bolu", "Karabük"),
    ("Burdur", "Isparta"), ("Burdur", "Denizli"), ("Burdur", "Muğla"),
    ("Bursa", "Yalova"), ("Bursa", "Kocaeli"), ("Bursa", "Sakarya"), ("Bursa", "Kütahya"),
    ("Çankırı", "Karabük"), ("Çankırı", "Kastamonu"), ("Çankırı", "Çorum"), ("Çankırı", "Yozgat"),
    ("Çankırı", "Kırıkkale"),
    ("Çorum", "Samsun"), ("Çorum", "Tokat"), ("Çorum", "Yozgat"), ("Çorum", "Kastamonu"),
    ("Çorum", "Sinop"),
    ("Denizli", "Muğla"), ("Denizli", "Uşak"),
    ("Diyarbakır", "Muş"), ("Diyarbakır", "Batman"), ("Diyarbakır", "Mardin"),
    ("Diyarbakır", "Şanlıurfa"), ("Diyarbakır", "Malatya"),
    ("Edirne", "Kırklareli"), ("Edirne", "Tekirdağ"),
    ("Elazığ", "Tunceli"), ("Elazığ", "Malatya"), ("Elazığ", "Diyarbakır"),
    ("Erzincan", "Erzurum"), ("Erzincan", "Bayburt"), ("Erzincan", "Gümüşhane"),
    ("Erzincan", "Sivas"), ("Erzincan", "Tunceli"),
    ("Erzurum", "Ardahan"), ("Erzurum", "Kars"), ("Erzurum", "Muş"), ("Erzurum", "Bayburt"),
    ("Erzurum", "Rize"),
    ("Eskişehir", "Kütahya"),
    ("Gaziantep", "Kilis"), ("Gaziantep", "Şanlıurfa"), ("Gaziantep", "Kahramanmaraş"),
    ("Gaziantep", "Osmaniye"),
    ("Giresun", "Ordu"), ("Giresun", "Sivas"), ("Giresun", "Erzincan"), ("Giresun", "Gümüşhane"),
    ("Giresun", "Trabzon"),
    ("Gümüşhane", "Trabzon"), ("Gümüşhane", "Bayburt"),
    ("Hakkari", "Van"), ("Hakkari", "Şırnak"),
    ("Hatay", "Osmaniye"),
    ("Isparta", "Konya"),
    ("Mersin", "Karaman"), ("Mersin", "Konya"), ("Mersin", "Niğde"),
    ("İstanbul", "Kocaeli"), ("İstanbul", "Tekirdağ"), ("İstanbul", "Kırklareli"),
    ("İzmir", "Manisa"),
    ("Kars", "Ardahan"), ("Kars", "Iğdır"),
    ("Kastamonu", "Bartın"), ("Kastamonu", "Karabük"), ("Kastamonu", "Sinop"),
    ("Kayseri", "Sivas"), ("Kayseri", "Yozgat"), ("Kayseri", "Nevşehir"), ("Kayseri", "Niğde"),
    ("Kayseri", "Kahramanmaraş"), ("Kayseri", "Malatya"),
    ("Kırklareli", "Tekirdağ"),
    ("Kırşehir", "Kırıkkale"), ("Kırşehir", "Yozgat"), ("Kırşehir", "Nevşehir"),
    ("Kırşehir", "Aksaray"),
    ("Kocaeli", "Sakarya"), ("Kocaeli", "Yalova"),
    ("Konya", "Karaman"), ("Konya", "Niğde"), ("Konya", "Aksaray"),
    ("Kütahya", "Uşak"),
    ("Malatya", "Kahramanmaraş"), ("Malatya", "Sivas"), ("Malatya", "Kayseri"),
    ("Manisa", "Uşak"), ("Manisa", "Kütahya"),
    ("Kahramanmaraş", "Sivas"), ("Kahramanmaraş", "Osmaniye"),
    ("Mardin", "Batman"), ("Mardin", "Şırnak"), ("Mardin", "Şanlıurfa"),
    ("Muş", "Van"),
    ("Nevşehir", "Niğde"), ("Nevşehir", "Aksaray"),
    ("Niğde", "Aksaray"),
    ("Ordu", "Samsun"), ("Ordu", "Tokat"), ("Ordu", "Sivas"),
    ("Rize", "Trabzon"),
    ("Sakarya", "Düzce"),
    ("Samsun", "Sinop"), ("Samsun", "Tokat"),
    ("Siirt", "Batman"), ("Siirt", "Şırnak"),
    ("Sinop", "Kastamonu"),
    ("Sivas", "Yozgat"), ("Sivas", "Tokat"),
    ("Tokat", "Yozgat"),
    ("Trabzon", "Bayburt"),
    ("Zonguldak", "Bartın"), ("Zonguldak", "Karabük"), ("Zonguldak", "Düzce"),
    ("Bartın", "Karabük"),
    ("Karabük", "Bolu"),
    ("Yalova", "Kocaeli"),
    ("Osmaniye", "Kahramanmaraş"),
)
"""Türkiye'nin 81 ilinin kara sınırı komşuluk KENARLARI — bkz. modül
docstring'indeki "VERİ KALİTESİ NOTU" (bu, resmi bir kadastro kaynağı
DEĞİL, genel coğrafya bilgisine dayanan bir REFERANS listedir; amaç
"mantıklı bir operasyon yarıçapı" için yeterli doğruluktur)."""


def _komsuluk_haritasi_olustur() -> Dict[str, FrozenSet[str]]:
    harita: Dict[str, set] = {il: set() for il in IL_MERKEZ_KOORDINATLARI}
    for a, b in _IL_KOMSULUK_KENARLARI:
        harita.setdefault(a, set()).add(b)
        harita.setdefault(b, set()).add(a)
    return {il: frozenset(komsular) for il, komsular in harita.items()}


IL_KOMSULUKLARI: Dict[str, FrozenSet[str]] = _komsuluk_haritasi_olustur()
"""İl adı -> kara sınırı komşusu olan illerin kümesi (simetrik — bkz.
`_komsuluk_haritasi_olustur`)."""


# ---------------------------------------------------------------------------
# 3) İl coğrafi profili (kıyı/dağlık/volkanik — "COĞRAFİ ÖN-KONTROL" için)
# ---------------------------------------------------------------------------

_KARADENIZ_KIYISI: FrozenSet[str] = frozenset({
    "Kırklareli", "İstanbul", "Sakarya", "Düzce", "Zonguldak", "Bartın", "Kastamonu",
    "Sinop", "Samsun", "Ordu", "Giresun", "Trabzon", "Rize", "Artvin",
})
_MARMARA_KIYISI: FrozenSet[str] = frozenset({
    "İstanbul", "Kocaeli", "Yalova", "Bursa", "Balıkesir", "Çanakkale", "Tekirdağ",
})
_EGE_KIYISI: FrozenSet[str] = frozenset({
    "Çanakkale", "Balıkesir", "İzmir", "Aydın", "Muğla",
})
_AKDENIZ_KIYISI: FrozenSet[str] = frozenset({
    "Muğla", "Antalya", "Mersin", "Adana", "Hatay",
})

KIYISI_OLAN_ILLER: FrozenSet[str] = (
    _KARADENIZ_KIYISI | _MARMARA_KIYISI | _EGE_KIYISI | _AKDENIZ_KIYISI
)
"""En az bir denize (Karadeniz/Marmara/Ege/Akdeniz) kıyısı olan iller —
Tsunami/Gemi Kazası gibi türlerin `cografi_on_kontrol`ünde kullanılır.

VERİ KAYNAĞI NOTU: projede zaten var olan
`mlops/golden_dataset/scenario_generator.RISK_MATRIX[<il>].kiyi_ili`
bilinçli olarak birebir kopyalanmadı — o alan DAHA DAR bir soruya
("bu ilde İNANDIRICI bir liman/gemi senaryosu üretilebilir mi") cevap
verir ve bu yüzden Sakarya/Kırklareli/Düzce gibi GERÇEKTEN kısa bir
Karadeniz kıyı şeridine sahip (sırasıyla Karasu, İğneada, Akçakoca
ilçeleri) ama önemli bir limanı OLMAYAN illeri BİLEREK dışarıda bırakır.
Buradaki soru İSE daha GENİŞ ve saf fiziksel: "bu ilde bir Tsunami/Gemi
Kazası fiziksel olarak MÜMKÜN MÜ" — bu sorunun cevabı için kısa bir kıyı
şeridi bile YETERLİDİR, bu yüzden bu üç il BURADA (ama RISK_MATRIX'te
DEĞİL) yer alır. İki tablo FARKLI amaçlara hizmet eder, KASITLI olarak
uyuşmazlar."""

DAGLIK_YUKSEK_BOLGE_ILLERI: FrozenSet[str] = frozenset({
    "Amasya", "Ardahan", "Artvin", "Ağrı", "Bayburt", "Bingöl", "Bitlis",
    "Bolu", "Bursa", "Düzce", "Elazığ", "Erzincan", "Erzurum", "Giresun",
    "Gümüşhane", "Hakkari", "Iğdır", "Kahramanmaraş", "Karabük", "Kars",
    "Kastamonu", "Kayseri", "Malatya", "Muş", "Niğde", "Ordu", "Rize",
    "Samsun", "Siirt", "Sinop", "Sivas", "Tokat", "Trabzon", "Tunceli",
    "Van", "Yozgat", "Çorum", "Şırnak",
})
"""Çığ (avalanche) gibi yüksek rakım/dağlık arazi gerektiren olay türleri
için "fiziksel olarak makul" kabul edilen iller.

VERİ KAYNAĞI NOTU: bu küme, projede zaten var olan, AFAD/tarihsel
referanslara dayanan daha özenli bir kaynakla
(`mlops/golden_dataset/scenario_generator.RISK_MATRIX[<il>].cig_riski`,
"YOK" dışındaki tüm iller) birebir eşleşecek şekilde senkronize
edilmiştir. `turkiye_harita.py` o modülü çalışma zamanında import etmez
(bilinçli olarak — bkz. modül docstring'i "iki modül bağımsız kalsın"
deseni: `mlops/` bir eğitim-verisi üretim betiğidir, üretim karar
motorunun buna çalışma zamanı bağımlılığı olmamalıdır), bu yüzden değer
burada sabit bir kopya olarak durur — biri güncellenirse diğerinin de
gözden geçirilmesi gerekir (bkz. o dosyadaki `RISK_MATRIX` yorumu)."""

VOLKANIK_BOLGE_ILLERI: FrozenSet[str] = frozenset({
    "Kayseri",       # Erciyes Dağı
    "Nevşehir",      # Kapadokya volkanik platosu (Erciyes/Hasandağı etkisi)
    "Niğde",         # Hasandağı
    "Aksaray",       # Hasandağı/Melendiz
    "Ağrı",          # Ağrı Dağı (Ararat)
    "Van",           # Nemrut Dağı (Van Gölü kıyısı)
    "Bitlis",        # Nemrut Dağı'nın diğer yakası + Süphan Dağı etkisi
    "Kars",          # Kısır Dağı ve çevresi
    "Ardahan",       # Kısır Dağı volkanik platosu
    "Konya",         # Karapınar volkanik sahası (Karacadağ/Hasandağı etkisi)
})
"""Türkiye'nin bilinen Kuvaterner volkanik sahalarına (Erciyes, Hasandağı,
Ağrı Dağı, Nemrut, Süphan, Kısır Dağı, Karapınar) yakın/ait iller —
Volkanik Patlama türü için `cografi_on_kontrol`ün izin verdiği KÜME."""


@dataclass(frozen=True)
class IlCografiProfil:
    """Bir ilin `cografi_on_kontrol` için gereken özet fiziksel profili."""

    il: str
    kiyisi_var_mi: bool
    dagliknyuksek_bolge_mi: bool
    volkanik_bolge_mi: bool


def il_cografi_profili(il: str) -> IlCografiProfil:
    return IlCografiProfil(
        il=il,
        kiyisi_var_mi=il in KIYISI_OLAN_ILLER,
        dagliknyuksek_bolge_mi=il in DAGLIK_YUKSEK_BOLGE_ILLERI,
        volkanik_bolge_mi=il in VOLKANIK_BOLGE_ILLERI,
    )


# ---------------------------------------------------------------------------
# 4) Yardımcı fonksiyonlar — "İL/İLÇE SİYASİ HARİTA KİLİDİ"
# ---------------------------------------------------------------------------

_R_KM = 6371.0088


def _geodesic_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Haversine jeodezik mesafe (km) — `decision_engine._geodesic_km` /
    `database.Neo4jConnection._geodesic_km` İLE AYNI formül; bu modülün
    (BaseNode/models.py gibi) proje genelinde EN ALTTA yer alan, hiçbir üst
    katmanı import ETMEYEN bağımsız bir modül olarak kalabilmesi için
    BİLİNÇLİ olarak burada da bağımsız tanımlanır (bkz. `decision_engine.
    _geodesic_km`daki AYNI gerekçe — "iki modül bağımsız kalsın" deseni)."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _R_KM * math.asin(min(1.0, math.sqrt(a)))


def en_yakin_il(enlem: Optional[float], boylam: Optional[float]) -> Optional[str]:
    """Verilen koordinata en yakın il MERKEZİNİ (Voronoi yaklaşıklığı)
    döner — bkz. modül docstring'indeki "VERİ KALİTESİ NOTU". `enlem`/
    `boylam` eksikse (koordinatsız kayıt) `None` döner; çağıran taraf bu
    durumda GÜVENLİ VARSAYILAN olarak geofencing/coğrafi kontrolü ATLAMALIDIR
    (bir koordinat yoksa "hangi il" sorusu zaten cevaplanamaz)."""
    if enlem is None or boylam is None:
        return None
    en_yakin: Optional[str] = None
    en_kucuk_mesafe = float("inf")
    for il, merkez in IL_MERKEZ_KOORDINATLARI.items():
        mesafe = _geodesic_km((enlem, boylam), merkez)
        if mesafe < en_kucuk_mesafe:
            en_kucuk_mesafe, en_yakin = mesafe, il
    return en_yakin


def izin_verilen_iller(il: Optional[str]) -> FrozenSet[str]:
    """Bir "MÜDAHALE EDECEK BİRLİK/AÇIK YOL/SAĞLAM TESİS" aramasının
    coğrafi olarak İÇİNDE KALABİLECEĞİ il kümesini döner: `il`in KENDİSİ +
    kara sınırı komşuları (bkz. `IL_KOMSULUKLARI`). `il=None` ise (koordinat
    yok/il tespit edilemedi) BOŞ küme döner — çağıran taraf bunu "geofencing
    uygulanamaz, SADECE km-yarıçapına güven" olarak yorumlamalıdır (bkz.
    `decision_engine._il_geofencing_kosulu`)."""
    if il is None:
        return frozenset()
    return frozenset({il}) | IL_KOMSULUKLARI.get(il, frozenset())


MAKSIMUM_OPERASYON_YARICAPI_KM = 150.0
"""Mantıklı bir operasyon yarıçapının kesin üst sınırı: `decision_engine.py`
deki hiçbir mesafe-tabanlı arama (kapalı yol/hasarlı tesis/birlik) bu
değeri aşamaz. Mevcut sabitler (`_ARAMA_YARICAPI_KM`, `_YETENEK_ARAMA_
YARICAPI_KM` = 50 km) zaten bunun altında olduğundan bu kilit normal
koşullarda sessiz kalır; asıl görevi, bu sabitlerden biri ileride yanlışlıkla
çok büyük bir değere çekilirse bile arama mantığının il-sınırı ölçeğinin
(pratikte komşu il merkezleri arası mesafe genelde 80-150 km) dışına
taşmamasını garanti etmektir."""


def yaricapi_operasyonel_sinira_kisitla(istenen_yaricap_km: float) -> float:
    """`istenen_yaricap_km`i `MAKSIMUM_OPERASYON_YARICAPI_KM` ile üstten
    sınırlar — bkz. o sabitin docstring'i."""
    return min(istenen_yaricap_km, MAKSIMUM_OPERASYON_YARICAPI_KM)


# ---------------------------------------------------------------------------
# 5) "COĞRAFİ ÖN-KONTROL" (Sanity Check / Ön-Mantık Filtresi)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _GecersizOncerulKurali:
    """Bir `EventType.value` için "fiziksel olarak GEÇERLİ" kabul edilen
    il kümesi + kural ihlal edildiğinde gösterilecek insan-okur gerekçe."""

    izinli_iller: FrozenSet[str]
    gereken_ozellik_aciklamasi: str


_COGRAFI_ON_KOSUL_KURALLARI: Dict[str, _GecersizOncerulKurali] = {
    "Tsunami": _GecersizOncerulKurali(
        KIYISI_OLAN_ILLER, "bir deniz kıyısı (Tsunami sadece kıyı şeridinde fiziksel olarak mümkündür)"
    ),
    "Gemi Kazasi": _GecersizOncerulKurali(
        KIYISI_OLAN_ILLER, "bir deniz kıyısı/liman (bir gemi kazası ancak kıyısı olan bir ilde gerçekleşebilir)"
    ),
    "Cig": _GecersizOncerulKurali(
        DAGLIK_YUKSEK_BOLGE_ILLERI, "dağlık/yüksek rakımlı bir arazi yapısı (çığ düz/ova bir bölgede oluşamaz)"
    ),
    "Volkanik Patlama": _GecersizOncerulKurali(
        VOLKANIK_BOLGE_ILLERI, "bilinen bir volkanik saha (bu il, aktif/dormant bir yanardağa yakın değildir)"
    ),
}
"""Sadece net/tartışmasız fiziksel imkânsızlıklar burada listelenir — ör.
"İç Anadolu'da Tsunami" ile aynı netlikte kesin çelişkiler. Sınırda/belirsiz
durumlar (ör. "Kuraklık" her ilde teorik
olarak mümkündür, "Baraj Çökmesi" için ilin GERÇEKTEN bir barajı olup
olmadığı bu modülün kapsamında DEĞİLDİR) BİLİNÇLİ olarak buraya
EKLENMEMİŞTİR — aşırı-agresif bir filtre, meşru ama sıra dışı raporları da
reddederek sistemi kullanılamaz kılar (bkz. `KIYISI_OLAN_ILLER`/`DAGLIK_
YUKSEK_BOLGE_ILLERI`nin BİLEREK GENİŞ tutulduğu not — false-positive/
yanlış-red riski, false-negative/kaçırılan-tutarsızlık riskinden HER ZAMAN
daha PAHALIDIR: meşru bir raporu reddetmek komutanın GERÇEK bir krizi
kaybetmesine yol açabilir)."""


def cografi_on_kontrol(
    event_type_degeri: Optional[str], enlem: Optional[float], boylam: Optional[float]
) -> Optional[str]:
    """"COĞRAFİ ÖN-KONTROL" (Sanity Check) — `DecisionEngine.
    generate_recommendations`nin LLM'e gitmeden ÖNCE, her kritik olay için
    çağırdığı ana giriş noktası.

    Döner: olay TÜRÜ ile bulunduğu ilin fiziksel yapısı AÇIKÇA çelişiyorsa
    (ör. kıyısı olmayan bir ilde "Tsunami") insan-okur, "Coğrafi
    tutarsızlık tespit edildi: İmkansız öncül" ile başlayan bir Türkçe
    gerekçe metni; ÇELİŞKİ YOKSA (VEYA kontrol bu tür için tanımlı
    DEĞİLSE, VEYA koordinat/il tespit edilemiyorsa — GÜVENLİ VARSAYILAN
    her zaman "geçerli SAY"dır) `None` döner.
    """
    if not event_type_degeri:
        return None
    kural = _COGRAFI_ON_KOSUL_KURALLARI.get(event_type_degeri)
    if kural is None:
        return None
    il = en_yakin_il(enlem, boylam)
    if il is None:
        return None
    if il in kural.izinli_iller:
        return None
    return (
        f"Coğrafi tutarsızlık tespit edildi: İmkansız öncül — '{event_type_degeri}' türünde "
        f"bir olay, en yakın tespit edilen il olan '{il}' için fiziksel olarak beklenmez "
        f"({il}, {kural.gereken_ozellik_aciklamasi} özelliğine sahip görünmüyor)."
    )
