"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
"FAZ 7: GÖLGE MODU" (War Gaming) — OTONOM VERİ TOPLAMA BETİĞİ (kullanıcı
talebi — Kurucu'nun kritik C4ISR sorusu: "Sınır nerede ve iyi/kötü kararı
nasıl ayırt edeceğiz?").

Bu betik, `api.py`yi (bkz. proje kökü — `uvicorn api:app --port 8000` ile
ÇALIŞIYOR OLMALIDIR) rastgele kriz senaryolarıyla tekrar tekrar "dövüştürüp"
(war-gaming), Karar Destek Motoru'nun ÜRETTİĞİ taktiksel önerileri bir
"Otomatik Hakem" (Validator) süzgecinden geçirir — SADECE mantıklı/tutarlı
kararlar `karavul_dataset.jsonl`e (JSONL — satır başına bir JSON kaydı)
yazılır; halüsinasyon şüpheli kararlar terminale AÇIKÇA loglanıp ÇÖPE ATILIR
(kotadan SAYILMAZ).

"v2" GÜNCELLEMESİ — `ProceduralInfrastructureCrisis` (kullanıcı talebi:
"eski sabit listeleri sil, Neo4j'deki GERÇEK Settlement/otoyol/liman/hastane
düğümlerini hedef alan, coğrafi bağlama uygun kriz senaryoları üret"): eski
`SENARYO_SABLONLARI` (10 SABİT, elle yazılmış metin) TAMAMEN KALDIRILDI.
Yerine `ProceduralInfrastructureCrisis` sınıfı geldi — bu sınıf Neo4j'e
(SADECE OKUMA amaçlı, `add_settlements.py`nin YAZMA bağlantısıyla AYNI
sınıf ama BAĞIMSIZ bir örnek) bağlanıp GERÇEK varlık havuzlarını
(`Settlement`, `Otoyol` [motorway/trunk], `Liman`, `Hastane`) çeker; HER
senaryo, bu havuzlardan rastgele seçilen GERÇEK bir varlığın ismini/ilini
f-string ile bir kriz metnine gömer — "coğrafi bağlama uygunluk" (kullanıcı
talebi: "ilçede sel, otoyolda zincirleme kaza, limanda endüstriyel yangın")
her varlık TÜRÜNE, o türe ANLAMLI gelen kriz tipleriyle eşleştirilerek
sağlanır (bkz. `_SENARYO_TANIMLARI`) — bir "Liman"a asla "zincirleme kaza"
ÖNERİLMEZ. Sayısal detaylar da (deprem büyüklüğü, araç sayısı, yağış mm'si)
HER çağrıda rastgele üretildiğinden, aynı GERÇEK varlık bile HER seferinde
"benzersiz" bir rapor üretir.

MİMARİ NOT (bkz. `add_settlements.py`deki BENZER karar): bu betik artık
SADECE bir HTTP istemcisi DEĞİLDİR — hedef havuzlarını OKUMAK için Neo4j'e
DOĞRUDAN (salt-okunur) bağlanır; ama kriz raporunu GÖNDERME/senaryo
SIFIRLAMA işlemleri HÂLÂ `api.py`ye HTTP üzerinden yapılır (`decision_
engine.py`/`database.py`/`api.py`nin TEK SATIRINA yine DOKUNULMAZ — SADECE
dışarıdan okur/çağırır).

KATI KURALLAR (kullanıcı talebi — HER BİRİ AŞAĞIDA madde madde uygulanır):
  1. SINIR (KOTA): `MAX_SUCCESSFUL_SCENARIOS = 150` — betik TAM 150 geçerli/
     kusursuz senaryo topladığında OTOMATİK durur. Sonsuz döngü YOKTUR;
     AYRICA `MAX_TOTAL_DENEME` ile toplam deneme sayısına da bir ÜST SINIR
     konur (bkz. aşağıdaki "İKİNCİ GÜVENCE" notu) — API'nin sistematik
     olarak HER seferinde geçersiz sonuç ürettiği bir arıza durumunda bile
     (%0 geçerlilik oranı) betik GERÇEKTEN sonsuza dek dönemez.
  2. PROSEDÜREL KRİZ ÜRETİCİ (`ProceduralInfrastructureCrisis`): Neo4j'deki
     GERÇEK Settlement/Otoyol/Liman/Hastane varlıklarını hedef alan,
     coğrafi bağlama uygun, rastgele sayısal detaylı f-string senaryolar
     üretir (bkz. yukarıdaki "v2 GÜNCELLEMESİ" notu).
  3. OTOMATİK HAKEM (Validator): `gecerli_mi()` — HER sonuç, diske
     yazılmadan ÖNCE bu süzgeçten geçer:
       a) `taktiksel_oneriler` GERÇEKTEN dolu bir liste mi?
       b) Önerilerin HİÇBİRİNDE "kapalı bir rota/güzergah öner" (aynı
          cümlede "kapalı" + "rota/güzergah/yol" birlikte GEÇMİYOR mu)?
       c) EN AZ bir öneride "açık" (rota/güzergah/yol AÇIK olduğuna dair)
          bir ifade GEÇİYOR mu?
     Bunlardan biri bile başarısız olursa: terminale "🛑 Geçersiz Taktik -
     Reddedildi" (+ sebep) yazılır, kayıt ATILMAZ, kotadan DÜŞÜLMEZ.
  4. TEMİZLİK VE BEKLEME: HER denemeden SONRA (geçerli/geçersiz FARK ETMEZ)
     `POST /api/reset-scenario` ile Bilgi Grafı temizlenir (aksi halde
     sonraki senaryo, ÖNCEKİ senaryonun kalıntılarıyla KARIŞIRDI). Ekran
     kartını/Ollama'yı BOĞMAMAK için her tur arasında `time.sleep(4)`
     (`DONGU_BEKLEME_SANIYE`) vardır.

Çalıştırmak için (proje kökünden, `api.py` `uvicorn api:app --port 8000`
ile ZATEN çalışıyorken, Neo4j'de `add_settlements.py` ÇALIŞTIRILMIŞ olarak):
    python war_gaming.py
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.core.database import Neo4jConnection, Neo4jConnectionError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1) SINIR (KOTA) — kullanıcı talebi: "Sonsuz döngü KULLANMA"
# ---------------------------------------------------------------------------

API_TABAN_URL = os.getenv("KARAVUL_API_BASE_URL", "http://localhost:8000")
VERI_SETI_DOSYASI = os.getenv("KARAVUL_DATASET_DOSYASI", "karavul_dataset.jsonl")

MAX_SUCCESSFUL_SCENARIOS = int(os.getenv("KARAVUL_MAX_BASARILI_SENARYO", "150"))
"""Betiğin toplayacağı GEÇERLİ (Otomatik Hakem'den geçmiş) senaryo sayısı —
bu sayıya ulaşınca betik KENDİLİĞİNDEN durur. `KARAVUL_MAX_BASARILI_SENARYO`
ortam değişkeniyle geçersiz kılınabilir (bkz. `mlops/orchestrator.py` —
otomasyonun HER turda küçük, öngörülebilir bir parti toplaması için;
değişken verilmezse varsayılan davranış [150] AYNEN korunur)."""

MAX_TOTAL_DENEME = int(os.getenv("KARAVUL_MAX_TOPLAM_DENEME", "1000"))
""""İKİNCİ GÜVENCE" (kullanıcı talebinin "sonsuz döngü YOK" ilkesinin
YAPISAL garantisi): `MAX_SUCCESSFUL_SCENARIOS` SADECE geçerli sonuçları
sayar — API sistematik olarak HER seferinde geçersiz/halüsinasyonlu bir
sonuç üretirse (%0 geçerlilik), "150 geçerliye ulaşana kadar dön" kuralı
TEK BAŞINA sonsuz bir döngüye dönüşürdü. Bu ikinci, TOPLAM deneme sayısına
(geçerli+geçersiz FARK ETMEKSİZİN) dayalı üst sınır, o senaryoda bile
betiğin KESİN olarak sonlanmasını garanti eder. `KARAVUL_MAX_TOPLAM_DENEME`
ile geçersiz kılınabilir (bkz. yukarıdaki AYNI gerekçe)."""

ISTEK_ZAMAN_ASIMI_SANIYE = float(os.getenv("KARAVUL_ISTEK_ZAMAN_ASIMI_SANIYE", "180"))
"""`POST /api/analyze-crisis` GERÇEK bir Ollama LLM zincirini (rapor
ayrıştırma + 3 taktiksel mikro-görev) tetiklediğinden, `decision_engine.py`
ile AYNI "belgelenmiş en kötü durumun üzerinde pay bırak" ilkesiyle (canlı
testlerde tek bir çağrı 20-30 sn sürdü) cömert bir varsayılan seçildi."""

DONGU_BEKLEME_SANIYE = 4.0
""""EKRAN KARTINI BOĞMAMAK" (kullanıcı talebi): her tur (geçerli/geçersiz
FARK ETMEKSİZİN) arasında beklenen süre — ardışık Ollama çağrılarının
GPU'yu/yerel modeli sürekli %100'de tutmasını önler."""


# ---------------------------------------------------------------------------
# 2) `ProceduralInfrastructureCrisis` — kullanıcı talebi: "eski sabit
#    listeleri sil, GERÇEK Neo4j varlıklarını hedef alan, coğrafi bağlama
#    uygun, benzersiz senaryolar üret"
# ---------------------------------------------------------------------------

_HEDEF_HAVUZU_LIMITI = 3000
"""HER hedef havuzu (Settlement/Otoyol/Liman/Hastane) için üst sınır —
`api.py`deki AYNI "kesin sınır" ilkesi: bu betik HİÇBİR sorguyu sınırsız
BIRAKMAZ (ulusal ölçekte binlerce hastane/otoyol segmenti olabilir)."""


def _yerlesim_temiz_ad(yerlesim: Dict[str, Any]) -> str:
    """`add_settlements.py`, ilçe isimlerini `"<İlçe> (<İl>)"` biçiminde
    BENZERSİZLEŞTİRİR (bkz. o betiğin `yerlesimleri_olustur` docstring'i) —
    bu sonek, doğal okunan bir kriz cümlesi İÇİN (parantezli teknik bir ad
    yerine) çıkarılır. `il` bilgisi zaten AYRI bir alanda (`yerlesim['il']`)
    mevcuttur, kaybolmaz."""
    isim = yerlesim["isim"]
    if yerlesim.get("yerlesim_tipi") == "Ilce Merkezi":
        parantez_konumu = isim.rfind(" (")
        if parantez_konumu != -1:
            return isim[:parantez_konumu]
    return isim


def _yerlesim_konum_ifadesi(yerlesim: Dict[str, Any]) -> str:
    """"İl merkezinde" / "İl ili İlçe ilçesinde" — doğal Türkçe konum
    ifadesi (bkz. `_yerlesim_temiz_ad`)."""
    if yerlesim.get("yerlesim_tipi") == "Il Merkezi":
        return f"{yerlesim['il']} il merkezinde"
    return f"{yerlesim['il']} ili {_yerlesim_temiz_ad(yerlesim)} ilçesinde"


# --- Kategori bazlı f-string şablon fonksiyonları -------------------------
# HER fonksiyon GERÇEK bir Neo4j varlığını (dict) alır, rastgele sayısal
# detaylarla (büyüklük/araç sayısı/yağış mm'si/hektar) BENZERSİZ bir kriz
# raporu ÜRETİR — aynı varlık bile HER çağrıda farklı bir metin verir.


def _sel_senaryosu(yerlesim: Dict[str, Any]) -> str:
    yagis_mm = random.randint(70, 220)
    saat = random.randint(4, 24)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} son {saat} saatte {yagis_mm} mm'yi bulan aşırı yağış "
        f"sonucu ani bir sel felaketi yaşandı. Bölgedeki çok sayıda ev ve iş yeri su altında kaldı, "
        f"tahliye çalışmaları sürüyor."
    )


def _deprem_senaryosu(yerlesim: Dict[str, Any]) -> str:
    buyukluk = round(random.uniform(4.5, 7.8), 1)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} {buyukluk} büyüklüğünde bir deprem meydana geldi. "
        f"Çok sayıda bina ağır hasar gördü, bölgede arama kurtarma çalışmaları başlatıldı."
    )


def _orman_yangini_senaryosu(yerlesim: Dict[str, Any]) -> str:
    hektar = random.randint(50, 900)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} kırsal kesiminde çıkan orman yangını rüzgarın etkisiyle "
        f"hızla büyüyerek yaklaşık {hektar} hektarlık alanı etkisi altına aldı, yerleşim yerlerine "
        f"yaklaşıyor."
    )


def _zincirleme_kaza_senaryosu(otoyol: Dict[str, Any]) -> str:
    arac_sayisi = random.randint(8, 45)
    sebep = random.choice(["yoğun sis", "aşırı yağış", "buzlanma", "ani sürücü hatası"])
    return (
        f"{otoyol['isim']} üzerinde {sebep} nedeniyle {arac_sayisi} aracın karıştığı zincirleme "
        f"trafik kazası meydana geldi. Otoyol çift yönlü olarak trafiğe kapatıldı, çok sayıda yaralı "
        f"bulunuyor."
    )


def _liman_yangin_senaryosu(liman: Dict[str, Any]) -> str:
    varyant = random.choice(["depolama", "konteyner", "yakıt istasyonu", "gemi bakım"])
    return (
        f"{liman['isim']}'nda {varyant} alanında başlayan endüstriyel bir yangın kısa sürede büyüdü. "
        f"Liman tesisleri ağır hasar aldı, gemi trafiği durduruldu, zehirli duman bölgeye yayılıyor."
    )


def _hastane_senaryosu(hastane: Dict[str, Any]) -> str:
    varyant = random.choice(["yangin", "deprem", "teknik_ariza"])
    if varyant == "yangin":
        return (
            f"{hastane['isim']} bünyesinde jeneratör dairesinde başlayan bir yangın nedeniyle "
            f"hastanenin bir bölümü tahliye ediliyor, hasta nakli sürüyor."
        )
    if varyant == "deprem":
        buyukluk = round(random.uniform(4.8, 7.2), 1)
        return (
            f"Bölgede meydana gelen {buyukluk} büyüklüğündeki deprem {hastane['isim']}'ne ağır hasar "
            f"verdi, hastane hizmet veremiyor durumda."
        )
    return (
        f"{hastane['isim']}'nde ana elektrik sisteminde meydana gelen teknik arıza sonucu yoğun "
        f"bakım üniteleri risk altında, acil müdahale gerekiyor."
    )


# (senaryo_turu, hedef_havuzu_adı, şablon_fonksiyonu) — "COĞRAFİ BAĞLAMA
# UYGUNLUK" (kullanıcı talebi) TAM OLARAK burada sağlanır: bir "Liman"
# hedefi ASLA "Zincirleme Kaza" şablonuna GİTMEZ, bir "Otoyol" hedefi ASLA
# "Sel" şablonuna GİTMEZ — her satır, o varlık TÜRÜNE ANLAMLI gelen kriz
# tipleriyle EL İLE eşleştirilmiştir.
_SENARYO_TANIMLARI: List[Tuple[str, str, Callable[[Dict[str, Any]], str]]] = [
    ("Sel", "Settlement", _sel_senaryosu),
    ("Deprem", "Settlement", _deprem_senaryosu),
    ("Orman Yangini", "Settlement", _orman_yangini_senaryosu),
    ("Zincirleme Kaza", "Otoyol", _zincirleme_kaza_senaryosu),
    ("Liman Yangini", "Liman", _liman_yangin_senaryosu),
    ("Hastane Krizi", "Hastane", _hastane_senaryosu),
]


class ProceduralInfrastructureCrisis:
    """Neo4j'deki GERÇEK sivil/altyapı varlıklarını (Settlement/Otoyol/
    Liman/Hastane) hedef alan, coğrafi bağlama uygun, rastgele kriz
    senaryosu üretir (bkz. modül başındaki "v2 GÜNCELLEMESİ" notu).

    Hedef havuzları `__init__`de BİR KEZ Neo4j'den okunur (betik boyunca
    sabit kalır — Settlement/Otoyol/Liman/Hastane varlıkları bir war-gaming
    turu SIRASINDA değişmez, her turda yeniden sorgulamak gereksizdir).
    """

    def __init__(self, db: Neo4jConnection) -> None:
        self._db = db
        self._havuzlar: Dict[str, List[Dict[str, Any]]] = {}
        self._havuzlari_yukle()

    def _havuzlari_yukle(self) -> None:
        print("[Hedef Havuzu] Neo4j'den GERÇEK Settlement/Otoyol/Liman/Hastane varlıkları okunuyor...")

        self._havuzlar["Settlement"] = self._db.execute_query(
            "MATCH (s:Settlement) "
            "RETURN s.isim AS isim, s.il AS il, s.yerlesim_tipi AS yerlesim_tipi, "
            "s.enlem AS enlem, s.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Otoyol"] = self._db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.highway_tipi IN ['motorway','trunk'] AND i.aciklama IS NOT NULL "
            "WITH i.aciklama AS isim, head(collect(i)) AS ornek "
            "RETURN isim, ornek.enlem AS enlem, ornek.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Liman"] = self._db.execute_query(
            "MATCH (f:Facility {facility_type: 'Liman'}) "
            "RETURN COALESCE(f.aciklama, f.isim) AS isim, f.enlem AS enlem, f.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Hastane"] = self._db.execute_query(
            "MATCH (f:Facility {facility_type: 'Hastane'}) "
            "RETURN COALESCE(f.aciklama, f.isim) AS isim, f.enlem AS enlem, f.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        for havuz_adi, kayitlar in self._havuzlar.items():
            print(f"      {havuz_adi}: {len(kayitlar)} gerçek varlık bulundu.")

        if not any(self._havuzlar.values()):
            raise RuntimeError(
                "Hiçbir hedef havuzu (Settlement/Otoyol/Liman/Hastane) dolu değil — "
                "Neo4j'de gerçek veri var mı kontrol edin (bkz. `seed_db.py`/`add_settlements.py`)."
            )

    def uret(self) -> Tuple[str, str]:
        """Rastgele bir (senaryo_turu, kriz_metni) üretir — SADECE dolu
        (en az 1 gerçek varlık içeren) havuzlardan seçim yapar; boş bir
        havuza (ör. henüz hiç Liman yüklenmemiş bir kurulumda) ASLA
        düşmez."""
        uygun_tanimlar = [
            (tur, havuz_adi, sablon_fn)
            for (tur, havuz_adi, sablon_fn) in _SENARYO_TANIMLARI
            if self._havuzlar.get(havuz_adi)
        ]
        if not uygun_tanimlar:
            raise RuntimeError("Hiçbir kriz kategorisi için uygun hedef bulunamadı.")

        senaryo_turu, havuz_adi, sablon_fn = random.choice(uygun_tanimlar)
        hedef = random.choice(self._havuzlar[havuz_adi])
        return senaryo_turu, sablon_fn(hedef)


# ---------------------------------------------------------------------------
# 3) OTOMATİK HAKEM (Validator) — kullanıcı talebi: "mantıksızlık/halüsinasyon
#    varsa çöpe at, kotadan düşme"
# ---------------------------------------------------------------------------

# "kapalı" kelimesi, bir rota/güzergah/yol kelimesiyle AYNI cümlede (basit
# bir yaklaşıklıkla: aralarında 40 karakterden az mesafede) geçiyorsa, model
# muhtemelen KAPALI bir güzergahı "önerilen" bir sevkiyat/tahliye yolu gibi
# sunmuştur — bu, `decision_engine`in AÇIK yol ağı üzerinden GERÇEK Dijkstra
# hesabı yaptığı göz önüne alınınca (bkz. `_en_yakin_ulasilan_birlikleri_
# bul`) NORMALDE olmaması gereken bir mantık hatasıdır; ŞÜPHELİ kabul edilip
# REDDEDİLİR.
_KAPALI_ROTA_DESENI = re.compile(
    r"(rota|g[uü]zerg[aâ]h|yol)\w*[^.!?]{0,40}\bkapal[ıi]\b|\bkapal[ıi]\b[^.!?]{0,40}(rota|g[uü]zerg[aâ]h|yol)",
    re.IGNORECASE,
)
# EN AZ bir öneride "açık" (rotanın/güzergahın AÇIK olduğuna dair) bir
# ifadenin geçmesi beklenir — kullanıcının "rotası AÇIKTIR ifadesi geçiyor
# mu?" talebinin DÜZ (case/ek-insensitive) karşılığı; modelin GERÇEK
# çıktısı "rotası açık", "güzergahı AÇIKTIR", "açık yol ağı üzerinden" gibi
# ufak varyasyonlar üretebildiğinden TEK bir kelimeye (`açık`) bakılır,
# TAM cümleye DEĞİL — aksi halde bu kontrol neredeyse HER geçerli cevabı da
# reddederdi (aşırı katı bir desen, kendi amacını baltalardı).
_ACIK_IFADESI_DESENI = re.compile(r"a[çc][ıi]k", re.IGNORECASE)


def gecerli_mi(sonuc: Dict[str, Any]) -> Tuple[bool, str]:
    """"Otomatik Hakem": bir `/api/analyze-crisis` yanıtının diske
    yazılmaya DEĞER (mantıklı/tutarlı) olup olmadığına karar verir.

    Returns:
        `(gecerli, sebep)` — `gecerli=False` ise `sebep` REDDİN gerekçesini
        (terminale basılacak, insan-okur) bir cümle olarak taşır.
    """
    oneriler = sonuc.get("taktiksel_oneriler")
    if not isinstance(oneriler, list) or not oneriler:
        return False, "taktiksel_oneriler boş/eksik (motor bir öneri üretemedi)"

    tum_metin = " ".join(str(madde) for madde in oneriler)

    kapali_eslesme = _KAPALI_ROTA_DESENI.search(tum_metin)
    if kapali_eslesme:
        return False, f"kapalı bir rota/güzergah önerilmiş gibi görünüyor (\"...{kapali_eslesme.group(0)}...\")"

    if not _ACIK_IFADESI_DESENI.search(tum_metin):
        return False, "hiçbir öneride rotanın/güzergahın AÇIK olduğuna dair bir ifade geçmiyor"

    return True, "geçerli"


# ---------------------------------------------------------------------------
# API çağrıları (crisis-submit/reset — `api.py`ye SADECE HTTP ile konuşur;
# bkz. modül başındaki "MİMARİ NOT")
# ---------------------------------------------------------------------------


def analiz_et(rapor_metni: str) -> Dict[str, Any]:
    """`POST /api/analyze-crisis` — başarısızsa `requests.RequestException`
    (bağlantı hatası) VEYA `requests.HTTPError` (`raise_for_status`, ör.
    422/503) fırlatır; çağıran taraf (bkz. `main`) bunu YAKALAYIP bir
    "geçersiz deneme" olarak ele alır, betiği ÇÖKERTMEZ."""
    yanit = requests.post(
        f"{API_TABAN_URL}/api/analyze-crisis",
        json={"rapor_metni": rapor_metni},
        timeout=ISTEK_ZAMAN_ASIMI_SANIYE,
    )
    yanit.raise_for_status()
    return yanit.json()


def senaryoyu_sifirla() -> None:
    """`POST /api/reset-scenario` — HER denemeden SONRA (geçerli/geçersiz
    FARK ETMEKSİZİN) çağrılır (bkz. modül başındaki "4) TEMİZLİK" notu).
    Başarısız olursa betiği DURDURMAZ (sadece uyarı loglar) — bir sonraki
    senaryonun ÖNCEKİ kalıntılarla karışma riski, betiğin TAMAMEN çökmesinden
    daha iyi bir ödündür; yine de bu risk AÇIKÇA loglanır, sessizce
    yutulmaz.
    """
    try:
        yanit = requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30)
        yanit.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Senaryo sıfırlanamadı (bir sonraki tur ÖNCEKİ kalıntılarla karışabilir): %s", exc)


def kaydet(senaryo_turu: str, rapor_metni: str, sonuc: Dict[str, Any]) -> None:
    """Geçerli (Otomatik Hakem'den geçmiş) bir kaydı `VERI_SETI_DOSYASI`ye
    (JSONL — satır başına bir JSON nesnesi) EKLER (append) — betik tekrar
    çalıştırılırsa önceki toplama oturumunun üzerine YAZILMAZ."""
    kayit = {
        "zaman": datetime.now(timezone.utc).isoformat(),
        "senaryo_turu": senaryo_turu,
        "rapor_metni": rapor_metni,
        "durum_ozeti": sonuc.get("durum_ozeti", ""),
        "taktiksel_oneriler": sonuc.get("taktiksel_oneriler", []),
    }
    with open(VERI_SETI_DOSYASI, "a", encoding="utf-8") as f:
        f.write(json.dumps(kayit, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Ana döngü
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 78)
    print(f"KARAVUL — FAZ 7: GÖLGE MODU (War Gaming v2) — hedef: {MAX_SUCCESSFUL_SCENARIOS} geçerli senaryo")
    print(f"API: {API_TABAN_URL}  ·  Çıktı: {VERI_SETI_DOSYASI}")
    print("=" * 78)

    try:
        db = Neo4jConnection()
        db.connect()
        uretici = ProceduralInfrastructureCrisis(db)
    except (Neo4jConnectionError, RuntimeError) as exc:
        print(f"✗ Hedef havuzları yüklenemedi: {exc}")
        sys.exit(1)

    basarili = 0
    deneme = 0

    # KURAL 1 (SINIR/KOTA): bu `while` KOŞULSUZ DEĞİLDİR — İKİ bağımsız üst
    # sınırdan (geçerli sayısı VEYA toplam deneme sayısı) HANGİSİ önce
    # dolarsa döngü ORADA kesin olarak sonlanır; sonsuz döngü YAPISAL
    # olarak MÜMKÜN DEĞİLDİR.
    while basarili < MAX_SUCCESSFUL_SCENARIOS and deneme < MAX_TOTAL_DENEME:
        deneme += 1
        senaryo_turu, rapor_metni = uretici.uret()
        print(f"\n[{deneme}/{MAX_TOTAL_DENEME}] ({basarili}/{MAX_SUCCESSFUL_SCENARIOS} kaydedildi) "
              f"Senaryo: {senaryo_turu}")
        print(f"  → {rapor_metni[:110]}{'...' if len(rapor_metni) > 110 else ''}")

        try:
            sonuc = analiz_et(rapor_metni)
        except requests.RequestException as exc:
            print(f"  🛑 API isteği başarısız oldu: {exc}")
            senaryoyu_sifirla()
            time.sleep(DONGU_BEKLEME_SANIYE)
            continue

        gecerli, sebep = gecerli_mi(sonuc)
        if not gecerli:
            print(f"  🛑 Geçersiz Taktik - Reddedildi ({sebep})")
        else:
            kaydet(senaryo_turu, rapor_metni, sonuc)
            basarili += 1
            print(f"  ✅ Kaydedildi ({basarili}/{MAX_SUCCESSFUL_SCENARIOS})")

        # KURAL 4 (TEMİZLİK VE BEKLEME): geçerli/geçersiz FARK ETMEKSİZİN.
        senaryoyu_sifirla()
        time.sleep(DONGU_BEKLEME_SANIYE)

    print("\n" + "=" * 78)
    if basarili >= MAX_SUCCESSFUL_SCENARIOS:
        print(f"✅ HEDEFE ULAŞILDI: {basarili} geçerli senaryo '{VERI_SETI_DOSYASI}' dosyasına kaydedildi "
              f"({deneme} toplam denemede).")
    else:
        print(f"⚠️ DENEME SINIRINA ({MAX_TOTAL_DENEME}) ULAŞILDI: sadece {basarili}/{MAX_SUCCESSFUL_SCENARIOS} "
              "geçerli senaryo toplanabildi — API/Ollama'nın sağlıklı çalıştığını kontrol edin.")
    print("=" * 78)

    db.close()


if __name__ == "__main__":
    main()
