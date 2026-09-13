"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
"FAZ 3: Dinamik Saha ve Çevresel İstihbarat" — Çevresel İstihbarat Modülü.

SORUN: `decision_engine.DecisionEngine`, bir olay yerine en yakın/en uygun
birliği SADECE mesafe + yetenek (Capability-Based Routing) üzerinden seçer
(bkz. `decision_engine._en_yakin_ulasilan_birlikleri_bul`) — Dijkstra en
kısa yolu bulur ama "o yol şu an YÜRÜNEBİLİR mi" sorusuna hiç bakmaz. Gerçek
bir SAKOM Komutanı, 5 km'deki bir birliği YAĞMUR/SİS/KAR nedeniyle 15
km'deki başka bir birliğe TERCİH edebilir (paletli araç, daha deneyimli
ekip vb.). Bu modül, LLM'e bu kararı VERDİRMEK için (kod seviyesinde
ZORLAMAZ — bu KASITLIDIR, bkz. modül sonu "MİMARİ SINIR" notu) anlık/tahmini
hava durumu bağlamını üretir.

"ASKERİ KURAL" (Offline-First): bu modülün TÜM kamu
arayüzü (`cevresel_durumu_getir`) HİÇBİR KOŞULDA istisna FIRLATMAZ ve HİÇBİR
KOŞULDA çağıranı BEKLETMEZ (kısa bir HTTP zaman aşımı, bkz. `_ISTEK_ZAMAN_
ASIMI_SANIYE`) — bu projenin "modele/dış servise güvenmek YETMEZ" ilkesiyle
AYNI sınıf bir güvencedir (bkz. `real_osm_loader`/`nlp_parser`daki benzer
"ASLA sessizce çökme" desenleri):
  1. ÖNCE gerçek/canlı bir hava durumu API'sinden (Open-Meteo — ücretsiz,
     API anahtarı GEREKTİRMEZ) GERÇEK veri çekilmeye çalışılır.
  2. İnternet YOKSA, API zaman aşımına UĞRARSA, HTTP hatası dönerse VEYA
     yanıt beklenmeyen bir şekilde EKSİK/BOZUKSA (`KeyError`/`ValueError`
     dahil TÜM olası hatalar `_canli_hava_durumu_getir` içinde YAKALANIR),
     sistem ÇÖKMEZ — bulunduğu AYA ve enleme (kabaca coğrafi bölgeye) göre
     bir "Çevrimdışı Tahmini Hava Durumu" (B Planı, bkz. `_cevrimdisi_
     tahmin_uret`) ÜRETİR. Dönen `CevreselDurum.kaynak` alanı HANGİ
     kaynaktan geldiğini ("canli_api" / "cevrimdisi_tahmini") HER ZAMAN
     AÇIKÇA taşır — asla "sahte veri gerçekmiş gibi" sunulmaz (bkz.
     `aciklama_metni`deki `[CANLI API]`/`[ÇEVRİMDIŞI TAHMİN]` etiketi).

"MİMARİ SINIR" (bilinçli bir tasarım kararı): bu modül LLM'in kararını KOD
SEVİYESİNDE ZORLAMAZ (ör. "yağmur varsa X birliğini SEÇ" gibi sert bir
filtre YAZMAZ) — SADECE `decision_engine`'in mikro-görev promptlarına
(bkz. `DecisionEngine.__init__`daki `_LOJISTIK_MIKRO_PROMPT_TEMPLATE`/
`_TAHLIYE_MIKRO_PROMPT_TEMPLATE`/`_SEVK_MIKRO_PROMPT_TEMPLATE`) bir
"ASKERİ DİREKTİF" olarak ENJEKTE EDİLİR; asıl karar (rota/birlik
değişikliği) YİNE LLM'e aittir. Bunun nedeni: hava durumu-birlik yeteneği
eşleştirmesi (ör. "sağanak yağış -> paletli araç gerekir") çok sayıda
nüanslı/bağlama-özel senaryo içerir ve `_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI`
gibi KESİN bir kod-seviyesi kural tablosuna indirgemek bu MVP'nin kapsamı
DIŞINDADIR; LLM'in GENEL akıl yürütmesine bırakılır — SADECE ham veriyi
(GraphRAG'in KENDİSİ gibi) modele SUNAR.

Gerekli paket: `requests` (proje genelinde zaten kullanılıyor, bkz.
`real_osm_loader.py`) — EK bir bağımlılık GEREKTİRMEZ.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

# Sistem Türkiye odaklı olduğundan (TR, DST uygulamıyor) sabit +03:00
# kullanılır — `nlp_parser._TR_TIMEZONE` ile AYNI değer, BİLİNÇLİ olarak
# BAĞIMSIZ tanımlanır (bu projede tekrar eden "iki modül bağımsız kalsın"
# deseni — ör. `real_osm_loader.KILCAL_HIGHWAY_TIPLERI`).
_TR_TIMEZONE = timezone(timedelta(hours=3))

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
"""Ücretsiz, API anahtarı GEREKTİRMEYEN hava durumu servisi (Open-Meteo).
"ASKERİ KURAL" gereği bu, TEK dış bağımlılıktır ve HER ZAMAN bir B planına
(bkz. `_cevrimdisi_tahmin_uret`) sahiptir."""

_ISTEK_ZAMAN_ASIMI_SANIYE = 4
"""Canlı API çağrısı için KISA bir zaman aşımı — bu modül `decision_engine`
mikro-görev zincirinin (kullanıcı arayüzünü BEKLETEN kritik yol) BİR
PARÇASI olarak çağrılır; internet yoksa/API yavaşsa kullanıcı UZUN süre
BEKLEMEMELİDİR — kısa bir zaman aşımı, B planına HIZLICA düşülmesini
garanti eder."""


# ---------------------------------------------------------------------------
# Sonuç veri yapısı
# ---------------------------------------------------------------------------


@dataclass
class CevreselDurum:
    """Bir kriz koordinatının anlık/tahmini çevresel (hava durumu)
    istihbaratı — `decision_engine`nin mikro-görev promptlarına enjekte
    ettiği VE `src.ui.app`nin arayüzde gösterdiği TEK, ortak veri yapısı.
    """

    sicaklik_c: float
    yagis_mm: float
    yagis_aciklamasi: str
    """İnsan-okur Türkçe kategori — ör. "Açık", "Hafif Yağmurlu", "Sağanak
    Yağışlı", "Karlı", "Sisli", "Fırtınalı (Gök Gürültülü)"."""
    gorus_mesafesi_km: float
    """Tahmini görüş mesafesi (km) — canlı API yolunda WMO hava kodundan,
    çevrimdışı yolda yağış şiddeti kategorisinden KABACA türetilir (bkz.
    `_wmo_koduna_gore_siniflandir`/`_cevrimdisi_tahmin_uret`); hassas bir
    meteorolojik ölçüm DEĞİLDİR, sadece LLM'e/komutana "iyi/orta/kötü
    görüş" fikri veren bir yaklaşıklıktır."""
    ikon: str
    """Arayüzde (bkz. `src.ui.app`) doğrudan gösterilecek TEK bir emoji."""
    kaynak: str
    """`"canli_api"` VEYA `"cevrimdisi_tahmini"` — veri provenance'ı, ASLA
    gizlenmez (bkz. modül docstring'indeki "ASKERİ KURAL")."""
    aciklama_metni: str
    """`decision_engine` mikro-görev promptlarına DOĞRUDAN enjekte edilecek,
    hazır TEK SATIRLIK Türkçe özet (bkz. `cevresel_durumu_getir`)."""


# ---------------------------------------------------------------------------
# 1) CANLI YOL — Open-Meteo (ücretsiz, anahtarsız)
# ---------------------------------------------------------------------------

# WMO hava kodu (Open-Meteo'nun `weather_code` alanı, standart WMO 4677
# tablosu) -> (yağış açıklaması, tahmini görüş mesafesi km, ikon). SADECE
# LLM'e/UI'a sunulacak KABA bir sınıflandırmadır — tam WMO tablosunun HER
# kodunu ayrıştırmaz, operasyonel açıdan ANLAMLI kümelere indirger.
_WMO_SINIFLANDIRMA: Tuple[Tuple[Tuple[int, ...], str, float, str], ...] = (
    ((45, 48), "Sisli", 0.5, "🌫️"),
    ((95, 96, 99), "Fırtınalı (Gök Gürültülü)", 2.0, "⛈️"),
    ((71, 73, 75, 77, 85, 86), "Karlı", 2.5, "🌨️"),
    ((63, 65, 66, 67, 81, 82), "Sağanak Yağışlı", 3.0, "🌧️"),
    ((51, 53, 55, 56, 57, 61, 80), "Hafif Yağmurlu", 6.0, "🌦️"),
    ((1, 2, 3), "Parçalı Bulutlu", 10.0, "⛅"),
)
_WMO_VARSAYILAN = ("Açık", 10.0, "☀️")
"""`_WMO_SINIFLANDIRMA`da eşleşmeyen kod (ör. 0 = açık gökyüzü, veya
tanımsız/beklenmeyen bir kod) için güvenli varsayılan."""


def _wmo_koduna_gore_siniflandir(kod: int) -> Tuple[str, float, str]:
    for kodlar, aciklama, gorus_km, ikon in _WMO_SINIFLANDIRMA:
        if kod in kodlar:
            return aciklama, gorus_km, ikon
    return _WMO_VARSAYILAN


def _canli_hava_durumu_getir(enlem: float, boylam: float) -> Optional[CevreselDurum]:
    """Open-Meteo'dan GERÇEK anlık hava durumunu çeker. Herhangi bir ağ/
    HTTP/JSON/alan hatasında (internet yok, zaman aşımı, API değişti vb.)
    `None` döner — ASLA istisna fırlatmaz (bkz. modül docstring'indeki
    "ASKERİ KURAL"); çağıran (`cevresel_durumu_getir`) `None` durumunda
    çevrimdışı tahmine düşer."""
    try:
        yanit = requests.get(
            _OPEN_METEO_URL,
            params={
                "latitude": enlem,
                "longitude": boylam,
                "current": "temperature_2m,precipitation,weather_code",
                "timezone": "auto",
            },
            timeout=_ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        yanit.raise_for_status()
        veri = yanit.json()
        mevcut = veri["current"]
        sicaklik = round(float(mevcut["temperature_2m"]), 1)
        yagis_mm = round(float(mevcut.get("precipitation") or 0.0), 1)
        wmo_kod = int(mevcut.get("weather_code") or 0)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Canli hava durumu API'sine ulasilamadi/gecersiz yanit (%s, %s icin): %s; "
            "cevrimdisi tahmine geciliyor.", (enlem, boylam), _OPEN_METEO_URL, exc,
        )
        return None

    yagis_aciklamasi, gorus_km, ikon = _wmo_koduna_gore_siniflandir(wmo_kod)
    aciklama_metni = (
        f"Sıcaklık: {sicaklik}°C, Yağış: {yagis_aciklamasi} ({yagis_mm} mm), "
        f"Görüş Mesafesi: ~{gorus_km} km [CANLI API]"
    )
    logger.info("Canli hava durumu alindi (%.4f, %.4f): %s", enlem, boylam, aciklama_metni)
    return CevreselDurum(
        sicaklik_c=sicaklik,
        yagis_mm=yagis_mm,
        yagis_aciklamasi=yagis_aciklamasi,
        gorus_mesafesi_km=gorus_km,
        ikon=ikon,
        kaynak="canli_api",
        aciklama_metni=aciklama_metni,
    )


# ---------------------------------------------------------------------------
# 2) ÇEVRİMDIŞI TAHMİN (B Planı) — aya + kaba coğrafi bölgeye göre
# ---------------------------------------------------------------------------

# Ay numarasi (1-12) -> Turkce ay adi. `datetime.strftime('%B')` KASITLI
# OLARAK KULLANILMAZ — bu, calisma ortaminin sistem LOCALE'ine baglidir
# (ör. bu projenin gelistirildigi Windows ortaminda test edildiginde
# Turkce DEGIL "August" gibi Ingilizce bir ad URETTI); `locale.setlocale`
# ile GLOBAL yorumlayici durumunu degistirmek yerine, sabit/bagimsiz bir
# eslesme kullanilir.
_AY_ADI_TR = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}

# Ay -> TÜRKİYE ORTALAMASI taban sıcaklık (°C) VE yağış OLASILIĞI (0-1).
# BİLİNÇLİ SINIR: bu, GERÇEK bir iklim modeli DEĞİLDİR — sadece "internet
# yokken tamamen anlamsız bir değer (ör. Ağustos'ta -5°C) ÜRETMEMEK" için
# kaba/yaklaşık bir referans tablosudur (bkz. modül docstring'indeki
# "ASKERİ KURAL" — örnek: "Ağustos ayında
# Hatay" -> sıcak/kurak bir tahmin üretmesi BEKLENİR, bkz. aşağıdaki
# `_cevrimdisi_tahmin_uret`deki enlem düzeltmesiyle BİRLİKTE).
_AY_TABAN_SICAKLIK_C = {
    1: 5.0, 2: 7.0, 3: 11.0, 4: 16.0, 5: 21.0, 6: 26.0,
    7: 29.0, 8: 29.0, 9: 24.0, 10: 18.0, 11: 12.0, 12: 7.0,
}
_AY_YAGIS_OLASILIGI = {
    1: 0.55, 2: 0.50, 3: 0.45, 4: 0.40, 5: 0.35, 6: 0.15,
    7: 0.05, 8: 0.05, 9: 0.15, 10: 0.35, 11: 0.45, 12: 0.55,
}

# KABA COĞRAFİ DÜZELTME (enlem BAZLI — boylam KASITLI OLARAK kullanılmaz,
# Türkiye'nin doğu-batı iklim farkı kuzey-güney farkından ÇOK daha azdır):
# Akdeniz/Güneydoğu (Hatay, Antalya, Mersin, Şanlıurfa gibi ~enlem<=37.5)
# HER ZAMAN ulusal ortalamadan SICAK ve KURAK; Karadeniz kıyısı/Doğu
# Anadolu yüksek kesimi (~enlem>=40.5) HER ZAMAN SOĞUK ve YAĞIŞLI; arası
# (İç Anadolu/Ege/Marmara) ulusal ortalamaya YAKIN kabul edilir.
_GUNEY_ENLEM_ESIGI = 37.5
_KUZEY_ENLEM_ESIGI = 40.5
_GUNEY_SICAKLIK_DUZELTME_C = 4.0
_KUZEY_SICAKLIK_DUZELTME_C = -4.0
_GUNEY_YAGIS_CARPANI = 0.5
_KUZEY_YAGIS_CARPANI = 1.6


def _cografi_duzeltme(enlem: float) -> Tuple[float, float]:
    """`enlem`e göre (sıcaklık_düzeltme_C, yağış_olasılığı_çarpanı) döner
    — bkz. yukarıdaki "KABA COĞRAFİ DÜZELTME" notu."""
    if enlem <= _GUNEY_ENLEM_ESIGI:
        return _GUNEY_SICAKLIK_DUZELTME_C, _GUNEY_YAGIS_CARPANI
    if enlem >= _KUZEY_ENLEM_ESIGI:
        return _KUZEY_SICAKLIK_DUZELTME_C, _KUZEY_YAGIS_CARPANI
    return 0.0, 1.0


def _cevrimdisi_tahmin_uret(enlem: float, boylam: float) -> CevreselDurum:
    """"B Planı": internet YOKSA veya canlı API başarısız olursa çağrılır
    (bkz. `cevresel_durumu_getir`). Bulunulan AYA (`_TR_TIMEZONE`, sistem
    saati) ve `enlem`e göre (bkz. `_cografi_duzeltme`) kaba/yaklaşık ama
    MEVSİMSEL/COĞRAFİ OLARAK MAKUL bir hava durumu üretir — ASLA rastgele
    (`random`) DEĞİLDİR: AYNI ay+konum için HER ZAMAN AYNI sonucu üretir
    (deterministik, `hashlib.md5` tabanlı bir "sözde-rastgele ama kararlı"
    seçim — bkz. `_kararli_varyasyon_orani`) ki bir rapor/oturum içinde
    hava durumu KENDİ İÇİNDE TUTARLI kalsın, her çağrıda farklı bir
    değere ZIPLAMASIN."""
    su_an = datetime.now(_TR_TIMEZONE)
    ay = su_an.month
    taban_sicaklik = _AY_TABAN_SICAKLIK_C[ay]
    taban_yagis_olasiligi = _AY_YAGIS_OLASILIGI[ay]

    sicaklik_duzeltme, yagis_carpani = _cografi_duzeltme(enlem)
    sicaklik = round(taban_sicaklik + sicaklik_duzeltme, 1)
    efektif_yagis_olasiligi = min(taban_yagis_olasiligi * yagis_carpani, 0.9)

    varyasyon_orani = _kararli_varyasyon_orani(ay, enlem, boylam)
    yagisli_mi = varyasyon_orani < efektif_yagis_olasiligi

    if yagisli_mi:
        if sicaklik <= 2.0:
            yagis_aciklamasi, ikon, yagis_mm, gorus_km = "Karlı", "🌨️", 4.0, 2.5
        elif efektif_yagis_olasiligi > 0.6:
            yagis_aciklamasi, ikon, yagis_mm, gorus_km = "Sağanak Yağışlı", "🌧️", 12.0, 3.0
        else:
            yagis_aciklamasi, ikon, yagis_mm, gorus_km = "Hafif Yağmurlu", "🌦️", 3.0, 6.0
    else:
        yagis_mm, gorus_km = 0.0, 10.0
        yagis_aciklamasi, ikon = ("Açık", "☀️") if sicaklik >= 15.0 else ("Parçalı Bulutlu", "⛅")

    aciklama_metni = (
        f"Sıcaklık: {sicaklik}°C, Yağış: {yagis_aciklamasi} ({yagis_mm} mm), "
        f"Görüş Mesafesi: ~{gorus_km} km [ÇEVRİMDIŞI TAHMİN — {_AY_ADI_TR[ay]} ayı "
        "ortalamasına göre]"
    )
    logger.info(
        "Cevrimdisi hava durumu tahmini uretildi (%.4f, %.4f, ay=%d): %s",
        enlem, boylam, ay, aciklama_metni,
    )
    return CevreselDurum(
        sicaklik_c=sicaklik,
        yagis_mm=yagis_mm,
        yagis_aciklamasi=yagis_aciklamasi,
        gorus_mesafesi_km=gorus_km,
        ikon=ikon,
        kaynak="cevrimdisi_tahmini",
        aciklama_metni=aciklama_metni,
    )


def _kararli_varyasyon_orani(ay: int, enlem: float, boylam: float) -> float:
    """`_cevrimdisi_tahmin_uret`in "yağışlı mı değil mi" kararını verirken
    kullandığı, 0.0-1.0 arası DETERMİNİSTİK (aynı girdi = HER ZAMAN aynı
    çıktı) bir "sözde-rastgele" oran üretir — `random` modülü KASITLI
    OLARAK kullanılmaz (bir oturum içinde art arda çağrılarda farklı
    sonuç üretip TUTARSIZ görünmesin diye)."""
    anahtar = f"{ay}-{round(enlem, 1)}-{round(boylam, 1)}"
    return (int(hashlib.md5(anahtar.encode("utf-8")).hexdigest(), 16) % 1000) / 1000.0


# ---------------------------------------------------------------------------
# Genel kullanım (public API)
# ---------------------------------------------------------------------------


def cevresel_durumu_getir(enlem: float, boylam: float) -> CevreselDurum:
    """Verilen kriz koordinatı için anlık/tahmini çevresel istihbaratı
    döner — ÖNCE canlı API (bkz. `_canli_hava_durumu_getir`), o
    başarısız olursa çevrimdışı tahmin (bkz. `_cevrimdisi_tahmin_uret`).

    "ASKERİ KURAL" (bkz. modül docstring'i): bu fonksiyon HİÇBİR KOŞULDA
    istisna fırlatmaz — `decision_engine`in mikro-görev zincirini asla
    çökertmez; en kötü durumda (geçersiz enlem/boylam gibi beklenmeyen
    bir durumda BİLE) `_cevrimdisi_tahmin_uret`in kendisi saf aritmetik
    olduğundan (ağ çağrısı YOK) pratikte HİÇ başarısız olmaz.
    """
    canli = _canli_hava_durumu_getir(enlem, boylam)
    if canli is not None:
        return canli
    return _cevrimdisi_tahmin_uret(enlem, boylam)
