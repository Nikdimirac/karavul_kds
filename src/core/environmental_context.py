

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


_TR_TIMEZONE = timezone(timedelta(hours=3))

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


_ISTEK_ZAMAN_ASIMI_SANIYE = 4


@dataclass
class CevreselDurum:


    sicaklik_c: float
    yagis_mm: float
    yagis_aciklamasi: str

    gorus_mesafesi_km: float
   
    ikon: str
    
    kaynak: str
  
    aciklama_metni: str


_WMO_SINIFLANDIRMA: Tuple[Tuple[Tuple[int, ...], str, float, str], ...] = (
    ((45, 48), "Sisli", 0.5, "🌫️"),
    ((95, 96, 99), "Fırtınalı (Gök Gürültülü)", 2.0, "⛈️"),
    ((71, 73, 75, 77, 85, 86), "Karlı", 2.5, "🌨️"),
    ((63, 65, 66, 67, 81, 82), "Sağanak Yağışlı", 3.0, "🌧️"),
    ((51, 53, 55, 56, 57, 61, 80), "Hafif Yağmurlu", 6.0, "🌦️"),
    ((1, 2, 3), "Parçalı Bulutlu", 10.0, "⛅"),
)
_WMO_VARSAYILAN = ("Açık", 10.0, "☀️")


def _wmo_koduna_gore_siniflandir(kod: int) -> Tuple[str, float, str]:
    for kodlar, aciklama, gorus_km, ikon in _WMO_SINIFLANDIRMA:
        if kod in kodlar:
            return aciklama, gorus_km, ikon
    return _WMO_VARSAYILAN


def _canli_hava_durumu_getir(enlem: float, boylam: float) -> Optional[CevreselDurum]:
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


_AY_ADI_TR = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}


_AY_TABAN_SICAKLIK_C = {
    1: 5.0, 2: 7.0, 3: 11.0, 4: 16.0, 5: 21.0, 6: 26.0,
    7: 29.0, 8: 29.0, 9: 24.0, 10: 18.0, 11: 12.0, 12: 7.0,
}
_AY_YAGIS_OLASILIGI = {
    1: 0.55, 2: 0.50, 3: 0.45, 4: 0.40, 5: 0.35, 6: 0.15,
    7: 0.05, 8: 0.05, 9: 0.15, 10: 0.35, 11: 0.45, 12: 0.55,
}


_GUNEY_ENLEM_ESIGI = 37.5
_KUZEY_ENLEM_ESIGI = 40.5
_GUNEY_SICAKLIK_DUZELTME_C = 4.0
_KUZEY_SICAKLIK_DUZELTME_C = -4.0
_GUNEY_YAGIS_CARPANI = 0.5
_KUZEY_YAGIS_CARPANI = 1.6


def _cografi_duzeltme(enlem: float) -> Tuple[float, float]:
  
    if enlem <= _GUNEY_ENLEM_ESIGI:
        return _GUNEY_SICAKLIK_DUZELTME_C, _GUNEY_YAGIS_CARPANI
    if enlem >= _KUZEY_ENLEM_ESIGI:
        return _KUZEY_SICAKLIK_DUZELTME_C, _KUZEY_YAGIS_CARPANI
    return 0.0, 1.0


def _cevrimdisi_tahmin_uret(enlem: float, boylam: float) -> CevreselDurum:

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
    
    anahtar = f"{ay}-{round(enlem, 1)}-{round(boylam, 1)}"
    return (int(hashlib.md5(anahtar.encode("utf-8")).hexdigest(), 16) % 1000) / 1000.0


def cevresel_durumu_getir(enlem: float, boylam: float) -> CevreselDurum:
  
    canli = _canli_hava_durumu_getir(enlem, boylam)
    if canli is not None:
        return canli
    return _cevrimdisi_tahmin_uret(enlem, boylam)
