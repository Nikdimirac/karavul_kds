

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple, Type, Union, get_args, get_origin

from dotenv import load_dotenv
from pydantic import ValidationError

try:
    from langchain_community.chat_models import ChatOllama
except ImportError:  
    from langchain_community.llms import Ollama as ChatOllama 

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.core.models import (
    BaseNode,
    CommunicationNetwork,
    CommunicationNetworkType,
    EnergyInfrastructure,
    EnergyInfrastructureType,
    Event,
    EventSeverity,
    EventType,
    Facility,
    FacilityStatus,
    GecersizFacilityTuruAtlandi,
    GecersizInfrastructureTuruAtlandi,
    Infrastructure,
    InfrastructureType,
    OperationalStatus,
    Relationship,
    RelationshipType,
    ResourceHub,
    ResourceType,
    Unit,
    UnitType,
)

logger = logging.getLogger(__name__)

load_dotenv()


_OLLAMA_NUM_GPU = int(os.getenv("OLLAMA_NUM_GPU", "999"))
_OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", str(min(16, os.cpu_count() or 8))))

_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE = float(os.getenv("OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE", "400"))
"

ENTITY_MODEL_MAP: Dict[str, Type[BaseNode]] = {
    "facilities": Facility,
    "infrastructures": Infrastructure,
    "energy_infrastructures": EnergyInfrastructure,
    "communication_networks": CommunicationNetwork,
    "resource_hubs": ResourceHub,
    "units": Unit,
    "events": Event,
}



_TR_TIMEZONE = timezone(timedelta(hours=3))


def _current_time_iso() -> str:
    return datetime.now(_TR_TIMEZONE).isoformat(timespec="seconds")




_TURKISH_CHAR_MAP = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    }
)


def normalize_tr(value: str) -> str:
 
    return value.strip().translate(_TURKISH_CHAR_MAP).lower()


@lru_cache(maxsize=None)
def _enum_lookup(enum_cls: Type[Enum]) -> Dict[str, Enum]:
    """Bir Enum sinifi icin normalize-edilmis-deger -> uye eslemesini uretir (cache'li)."""
    return {normalize_tr(str(member.value)): member for member in enum_cls}


def normalize_enum_value(enum_cls: Type[Enum], raw_value: Any) -> Any:
   
    if raw_value is None or isinstance(raw_value, enum_cls) or not isinstance(raw_value, str):
        return raw_value
    return _enum_lookup(enum_cls).get(normalize_tr(raw_value), raw_value)


def _extract_enum_type(annotation: Any) -> Optional[Type[Enum]]:
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation
    if get_origin(annotation) is Union:
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, Enum):
                return arg
    return None


def sanitize_enum_fields(model_cls: Type[Any], item: Dict[str, Any]) -> Dict[str, Any]:
   
    sanitized = dict(item)
    for field_name, field_info in model_cls.model_fields.items():
        if field_name not in sanitized:
            continue
        enum_cls = _extract_enum_type(field_info.annotation)
        if enum_cls is not None:
            sanitized[field_name] = normalize_enum_value(enum_cls, sanitized[field_name])
    return sanitized


_REQUIRED_FIELD_FALLBACKS: Dict[Type[BaseNode], Dict[str, Any]] = {
    Facility: {"kapasite": 0.0},
    Infrastructure: {"uzunluk_km": 0.0},
    EnergyInfrastructure: {"kapasite_mw": 0.0},
    
    CommunicationNetwork: {"kapsama_yaricapi_km": 0.0, "batarya_omru_saat": 0.0},
    ResourceHub: {"stok_seviyesi_yuzde": 0.0, "tukenme_hizi_gun": 0.0},
    Unit: {"personel_sayisi": 0},
    Event: {"etki_alani_km": 1.0, "siddet": EventSeverity.ORTA.value},
}


def apply_required_field_fallbacks(model_cls: Type[BaseNode], item: Dict[str, Any]) -> Dict[str, Any]:
  
    varsayilanlar = _REQUIRED_FIELD_FALLBACKS.get(model_cls)
    if not varsayilanlar:
        return item
    doldurulmus = dict(item)
    for alan_adi, varsayilan_deger in varsayilanlar.items():
        if doldurulmus.get(alan_adi) is None:
            doldurulmus[alan_adi] = varsayilan_deger
    return doldurulmus


def _gecersiz_event_type_ise_varsayilana_cek(item: Dict[str, Any]) -> Dict[str, Any]:
   
    ham_deger = item.get("event_type")
    if ham_deger is None or isinstance(ham_deger, EventType):
        return item
    if normalize_tr(str(ham_deger)) in _enum_lookup(EventType):
        return item  
    logger.warning(
        "Gecersiz event_type '%s' tespit edildi; 'OLAY TURU SINIFLANDIRMA KURALI' geregi "
        "varsayilan '%s'e cekiliyor (kayit ARTIK REDDEDILMEYECEK).",
        ham_deger, EventType.PATLAMA.value,
    )
    duzeltilmis = dict(item)
    duzeltilmis["event_type"] = EventType.PATLAMA.value
    return duzeltilmis



_GECERSIZ_DURUM_VARSAYILANI: Dict[Type[Any], Any] = {
    OperationalStatus: OperationalStatus.HASARLI,
    FacilityStatus: FacilityStatus.HASARLI,
}


def _gecersiz_durum_ise_varsayilana_cek(model_cls: Type[BaseNode], item: Dict[str, Any]) -> Dict[str, Any]:
   
    duzeltilmis = dict(item)
    degisti = False
    for alan_adi, enum_cls in (("durum", OperationalStatus), ("mevcut_durum", FacilityStatus)):
        ham_deger = duzeltilmis.get(alan_adi)
        if ham_deger is None or isinstance(ham_deger, enum_cls):
            continue
        if normalize_tr(str(ham_deger)) in _enum_lookup(enum_cls):
            continue  
        varsayilan = _GECERSIZ_DURUM_VARSAYILANI[enum_cls]
        logger.warning(
            "Gecersiz %s '%s' tespit edildi; en yakin karsiligi olan '%s'e cekiliyor "
            "(kayit ARTIK REDDEDILMEYECEK).",
            alan_adi, ham_deger, varsayilan.value,
        )
        duzeltilmis[alan_adi] = varsayilan.value
        degisti = True
    if degisti and model_cls is Infrastructure:
        duzeltilmis["acik_mi"] = False
    return duzeltilmis



_UNIT_TYPE_ES_ANLAMLILAR: Dict[str, UnitType] = {
    normalize_tr("Tibbi"): UnitType.SAGLIK,
    normalize_tr("Medikal"): UnitType.SAGLIK,
    normalize_tr("Tibbi Mudahale"): UnitType.SAGLIK,
    normalize_tr("Ambulans"): UnitType.SAGLIK,
    normalize_tr("Saglik Ekibi"): UnitType.SAGLIK,
    normalize_tr("Hastane Ekibi"): UnitType.SAGLIK,
    normalize_tr("UMKE"): UnitType.SAGLIK,
    normalize_tr("Arama Kurtarma"): UnitType.ARAMA_KURTARMA,
    normalize_tr("Arama-Kurtarma"): UnitType.ARAMA_KURTARMA,
    normalize_tr("Kurtarma Ekibi"): UnitType.ARAMA_KURTARMA,
    normalize_tr("AKUT"): UnitType.ARAMA_KURTARMA,
    normalize_tr("Enkaz Kurtarma"): UnitType.ARAMA_KURTARMA,
    normalize_tr("Lojistik Destek"): UnitType.LOJISTIK,
    normalize_tr("Ikmal"): UnitType.LOJISTIK,
    normalize_tr("Ikmal Birimi"): UnitType.LOJISTIK,
    normalize_tr("Tedarik"): UnitType.LOJISTIK,
    normalize_tr("Nakliye"): UnitType.LOJISTIK,
    normalize_tr("Yangin"): UnitType.ITFAIYE,
    normalize_tr("Yangin Birimi"): UnitType.ITFAIYE,
    normalize_tr("Yangin Ekibi"): UnitType.ITFAIYE,
    normalize_tr("Deniz Kuvvetleri"): UnitType.SAHIL_GUVENLIK,
    normalize_tr("Sahil Koruma"): UnitType.SAHIL_GUVENLIK,
}


def _gecersiz_unit_type_ise_es_anlamlisina_cek(item: Dict[str, Any]) -> Dict[str, Any]:

    ham_deger = item.get("unit_type")
    if ham_deger is None or isinstance(ham_deger, UnitType) or not isinstance(ham_deger, str):
        return item
    anahtar = normalize_tr(ham_deger)
    if anahtar in _enum_lookup(UnitType):
        return item  
    es_anlamli = _UNIT_TYPE_ES_ANLAMLILAR.get(anahtar)
    if es_anlamli is None:
       
        adaylar: Dict[str, UnitType] = {**_enum_lookup(UnitType), **_UNIT_TYPE_ES_ANLAMLILAR}
        eslesenler = [
            (aday_anahtar, deger) for aday_anahtar, deger in adaylar.items()
            if aday_anahtar and aday_anahtar in anahtar
        ]
        if eslesenler:
            es_anlamli = max(eslesenler, key=lambda cift: len(cift[0]))[1]
    if es_anlamli is None:
        return item
    logger.warning(
        "Es-anlamli unit_type '%s' tespit edildi; dogru UnitType uyesi '%s'e esleniyor "
        "(kayit ARTIK REDDEDILMEYECEK).",
        ham_deger, es_anlamli.value,
    )
    duzeltilmis = dict(item)
    duzeltilmis["unit_type"] = es_anlamli.value
    return duzeltilmis


_COMMUNICATION_NETWORK_TYPE_ES_ANLAMLILAR: Dict[str, CommunicationNetworkType] = {
    normalize_tr("Iletisim Merkezi"): CommunicationNetworkType.BAZ_ISTASYONU,
    normalize_tr("Telsiz Istasyonu"): CommunicationNetworkType.BAZ_ISTASYONU,
    normalize_tr("Verici"): CommunicationNetworkType.BAZ_ISTASYONU,
    normalize_tr("Anten"): CommunicationNetworkType.BAZ_ISTASYONU,
    normalize_tr("Repeater"): CommunicationNetworkType.BAZ_ISTASYONU,
}


def _gecersiz_network_type_ise_es_anlamlisina_cek(item: Dict[str, Any]) -> Dict[str, Any]:
   
    ham_deger = item.get("network_type")
    if ham_deger is None or isinstance(ham_deger, CommunicationNetworkType) or not isinstance(ham_deger, str):
        return item
    anahtar = normalize_tr(ham_deger)
    if anahtar in _enum_lookup(CommunicationNetworkType):
        return item
    es_anlamli = _COMMUNICATION_NETWORK_TYPE_ES_ANLAMLILAR.get(anahtar)
    if es_anlamli is None:
        adaylar: Dict[str, CommunicationNetworkType] = {
            **_enum_lookup(CommunicationNetworkType), **_COMMUNICATION_NETWORK_TYPE_ES_ANLAMLILAR,
        }
        eslesenler = [
            (aday_anahtar, deger) for aday_anahtar, deger in adaylar.items()
            if aday_anahtar and aday_anahtar in anahtar
        ]
        if eslesenler:
            es_anlamli = max(eslesenler, key=lambda cift: len(cift[0]))[1]
    if es_anlamli is None:
        return item
    logger.warning(
        "Es-anlamli network_type '%s' tespit edildi; dogru CommunicationNetworkType uyesi '%s'e "
        "esleniyor (kayit ARTIK REDDEDILMEYECEK).",
        ham_deger, es_anlamli.value,
    )
    duzeltilmis = dict(item)
    duzeltilmis["network_type"] = es_anlamli.value
    return duzeltilmis


_RESOURCE_TYPE_ES_ANLAMLILAR: Dict[str, ResourceType] = {
    normalize_tr("Kaynak"): ResourceType.YAKIT,
    normalize_tr("Kaynak Merkezi"): ResourceType.YAKIT,
    normalize_tr("Dogalgaz"): ResourceType.YAKIT,
    normalize_tr("Akaryakit"): ResourceType.YAKIT,
    normalize_tr("Benzin"): ResourceType.YAKIT,
    normalize_tr("Mazot"): ResourceType.YAKIT,
    normalize_tr("LPG"): ResourceType.YAKIT,
}


def _gecersiz_resource_type_ise_es_anlamlisina_cek(item: Dict[str, Any]) -> Dict[str, Any]:
   
    ham_deger = item.get("resource_type")
    if ham_deger is None or isinstance(ham_deger, ResourceType) or not isinstance(ham_deger, str):
        return item
    anahtar = normalize_tr(ham_deger)
    if anahtar in _enum_lookup(ResourceType):
        return item
    es_anlamli = _RESOURCE_TYPE_ES_ANLAMLILAR.get(anahtar)
    if es_anlamli is None:
        adaylar: Dict[str, ResourceType] = {**_enum_lookup(ResourceType), **_RESOURCE_TYPE_ES_ANLAMLILAR}
        eslesenler = [
            (aday_anahtar, deger) for aday_anahtar, deger in adaylar.items()
            if aday_anahtar and aday_anahtar in anahtar
        ]
        if eslesenler:
            es_anlamli = max(eslesenler, key=lambda cift: len(cift[0]))[1]
    if es_anlamli is None:
        return item
    logger.warning(
        "Es-anlamli resource_type '%s' tespit edildi; dogru ResourceType uyesi '%s'e esleniyor "
        "(kayit ARTIK REDDEDILMEYECEK).",
        ham_deger, es_anlamli.value,
    )
    duzeltilmis = dict(item)
    duzeltilmis["resource_type"] = es_anlamli.value
    return duzeltilmis



_YAPI_TIP_KOK_KELIMELERI: Tuple[Tuple[str, str], ...] = (
    
    ("Kopru", "kopru"),
    ("Kopru", "koprusu"),
    ("Tunel", "tunel"),
    ("Tunel", "tuneli"),
    ("Viyaduk", "viyaduk"),
    ("Viyaduk", "viyaduku"),

    ("Sokak", "kavsak"),
    ("Sokak", "kavsagi"),
    ("Sokak", "cadde"),
    ("Sokak", "sokak"),
    ("Sokak", "bulvar"),
    ("Sokak", "yol"),
)

_YAPI_FUZZY_ESIK = 0.78



_KAPALI_SAYILAN_DURUMLAR = frozenset({"hasarli", "yok edildi"})


_KRITIK_TERIM_KOKLERI: Tuple[str, ...] = tuple(sorted({kok for _, kok in _YAPI_TIP_KOK_KELIMELERI}))

_METIN_ON_DUZELTME_FUZZY_ESIK = 0.78


def _metindeki_kritik_terimleri_duzelt(text: str) -> str:
  
    parcalar = re.findall(r"\w+|\W+", text, flags=re.UNICODE)
    sonuc_parcalari: List[str] = []
    for parca in parcalar:
        if not parca.isalpha():
            sonuc_parcalari.append(parca)
            continue
        katlanmis = normalize_tr(parca)
        if katlanmis in _KRITIK_TERIM_KOKLERI:
            sonuc_parcalari.append(parca)  
            continue
        en_iyi_kok: Optional[str] = None
        en_iyi_oran = 0.0
        for kok in _KRITIK_TERIM_KOKLERI:
            oran = difflib.SequenceMatcher(None, katlanmis, kok).ratio()
            if oran > en_iyi_oran:
                en_iyi_kok, en_iyi_oran = kok, oran
        if en_iyi_kok is not None and en_iyi_oran >= _METIN_ON_DUZELTME_FUZZY_ESIK:
            logger.info(
                "Girdi ON-DUZELTME: '%s' -> '%s' (benzerlik=%.2f)", parca, en_iyi_kok, en_iyi_oran
            )
            sonuc_parcalari.append(en_iyi_kok)
        else:
            sonuc_parcalari.append(parca)
    return "".join(sonuc_parcalari)


def _isim_yapi_tipini_belirle(isim: Any) -> Optional[str]:
    
    if not isinstance(isim, str) or not isim.strip():
        return None
    katlanmis_tam = normalize_tr(isim)

    for tip, kok in _YAPI_TIP_KOK_KELIMELERI:
        if kok in katlanmis_tam:
            return tip

    kelimeler = katlanmis_tam.split()
    for tip, kok in _YAPI_TIP_KOK_KELIMELERI:
        for kelime in kelimeler:
            if difflib.SequenceMatcher(None, kelime, kok).ratio() >= _YAPI_FUZZY_ESIK:
                return tip
    return None


def reroute_misplaced_street_facilities(raw_json: Dict[str, Any]) -> Dict[str, Any]:
  
    facilities = raw_json.get("facilities")
    if not facilities:
        return raw_json

    kalan_facilities: List[Any] = []
    kurtarilanlar: List[Dict[str, Any]] = []
    for item in facilities:
        isim = item.get("isim") if isinstance(item, dict) else None
        yapi_tipi = _isim_yapi_tipini_belirle(isim) if isinstance(item, dict) else None
        if not isinstance(item, dict) or yapi_tipi is None:
            kalan_facilities.append(item)
            continue

        acik_mi = item.get("acik_mi")
        if acik_mi is None:
            durum_normalize = normalize_tr(str(item.get("durum") or item.get("mevcut_durum") or ""))
            acik_mi = durum_normalize not in _KAPALI_SAYILAN_DURUMLAR

        kurtarilanlar.append(
            {
                "isim": isim,
                "aciklama": item.get("aciklama"),
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "durum": item.get("durum"),
                "infrastructure_type": yapi_tipi,
                "acik_mi": acik_mi,
                "uzunluk_km": item.get("uzunluk_km"),
                "tonaj_kapasitesi": item.get("tonaj_kapasitesi"),
                "bitis_enlem": item.get("bitis_enlem"),
                "bitis_boylam": item.get("bitis_boylam"),
            }
        )
        logger.warning(
            "Entity Routing KURTARMA: '%s' LLM tarafindan yanlislikla 'facilities' listesine "
            "konulmustu; Pydantic dogrulamasindan ONCE 'infrastructures'e (%s) tasindi.",
            isim,
            yapi_tipi,
        )

    if not kurtarilanlar:
        return raw_json

    duzeltilmis = dict(raw_json)
    duzeltilmis["facilities"] = kalan_facilities
    duzeltilmis["infrastructures"] = list(raw_json.get("infrastructures") or []) + kurtarilanlar
    return duzeltilmis


_TESIS_TIP_KOK_KELIMELERI: Tuple[Tuple[str, str], ...] = (
    
    ("Askeri Us", "hava ussu"),
    ("Askeri Us", "jet ussu"),
    ("Askeri Us", "askeri us"),
    ("Askeri Us", "kislasi"),
    ("Askeri Us", "kisla"),
    ("Askeri Us", "karakolu"),
    ("Askeri Us", "karakol"),
    ("Askeri Us", "garnizon"),
    ("Havalimani", "havalimani"),
    ("Havalimani", "havaalani"),
    ("Havalimani", "hava alani"),
    ("Hastane", "hastane"),
    ("Hastane", "saglik ocagi"),
    ("Liman", "liman"),
    ("Liman", "iskele"),
    ("Siginak", "siginak"),
    ("Siginak", "barinak"),
)


def _infrastructure_type_gecerli_mi(item: Dict[str, Any]) -> bool:
    
    ham = item.get("infrastructure_type")
    if ham is None:
        return False
    if isinstance(ham, InfrastructureType):
        return True
    return normalize_tr(str(ham)) in _enum_lookup(InfrastructureType)


def _isim_veya_tip_tesis_tipini_belirle(item: Dict[str, Any]) -> Optional[str]:
   
    parcalar = [p for p in (item.get("isim"), item.get("infrastructure_type")) if isinstance(p, str)]
    if not parcalar:
        return None
    birlesik = normalize_tr(" ".join(parcalar))
    for tip, kok in _TESIS_TIP_KOK_KELIMELERI:
        if kok in birlesik:
            return tip
    return None


def reroute_misplaced_infrastructure_facilities(raw_json: Dict[str, Any]) -> Dict[str, Any]:
   
    infrastructures = raw_json.get("infrastructures")
    if not infrastructures:
        return raw_json

    kalan_infrastructures: List[Any] = []
    kurtarilanlar: List[Dict[str, Any]] = []
    for item in infrastructures:
        if not isinstance(item, dict) or _infrastructure_type_gecerli_mi(item):
            kalan_infrastructures.append(item)
            continue
        if _isim_yapi_tipini_belirle(item.get("isim")) is not None:
            kalan_infrastructures.append(item) 
            continue
        tesis_tipi = _isim_veya_tip_tesis_tipini_belirle(item)
        if tesis_tipi is None:
            kalan_infrastructures.append(item) 
            continue

        isim = item.get("isim")
        kurtarilanlar.append(
            {
                "isim": isim,
                "aciklama": item.get("aciklama"),
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "durum": item.get("durum"),
                "facility_type": tesis_tipi,
                "mevcut_durum": item.get("durum"),
            }
        )
        logger.warning(
            "Entity Routing KURTARMA (ters yon): '%s' LLM tarafindan yanlislikla "
            "'infrastructures' listesine (gecersiz infrastructure_type='%s') konulmustu; "
            "Pydantic dogrulamasindan ONCE 'facilities'e (%s) tasindi.",
            isim, item.get("infrastructure_type"), tesis_tipi,
        )

    if not kurtarilanlar:
        return raw_json

    duzeltilmis = dict(raw_json)
    duzeltilmis["infrastructures"] = kalan_infrastructures
    duzeltilmis["facilities"] = list(raw_json.get("facilities") or []) + kurtarilanlar
    return duzeltilmis



def _energy_type_gecerli_mi(item: Dict[str, Any]) -> bool:
   
    ham = item.get("energy_type")
    if ham is None:
        return False
    if isinstance(ham, EnergyInfrastructureType):
        return True
    return normalize_tr(str(ham)) in _enum_lookup(EnergyInfrastructureType)


def reroute_misplaced_energy_resources(raw_json: Dict[str, Any]) -> Dict[str, Any]:
   
    enerjiler = raw_json.get("energy_infrastructures")
    if not enerjiler:
        return raw_json

    kalan_enerjiler: List[Any] = []
    kurtarilanlar: List[Dict[str, Any]] = []
    for item in enerjiler:
        if not isinstance(item, dict) or _energy_type_gecerli_mi(item):
            kalan_enerjiler.append(item)
            continue
        parcalar = [p for p in (item.get("isim"), item.get("energy_type")) if isinstance(p, str)]
        birlesik = normalize_tr(" ".join(parcalar)) if parcalar else ""
        yakit_mi = any(kok in birlesik for kok in _RESOURCE_TYPE_ES_ANLAMLILAR)
        if not yakit_mi:
            kalan_enerjiler.append(item) 
            continue

        isim = item.get("isim")
        kurtarilanlar.append(
            {
                "isim": isim,
                "aciklama": item.get("aciklama"),
                "enlem": item.get("enlem"),
                "boylam": item.get("boylam"),
                "durum": item.get("durum"),
                "resource_type": ResourceType.YAKIT.value,
            }
        )
        logger.warning(
            "Entity Routing KURTARMA (enerji->kaynak): '%s' LLM tarafindan yanlislikla "
            "'energy_infrastructures' listesine (gecersiz energy_type='%s') konulmustu; "
            "Pydantic dogrulamasindan ONCE 'resource_hubs'a (Yakit) tasindi.",
            isim, item.get("energy_type"),
        )

    if not kurtarilanlar:
        return raw_json

    duzeltilmis = dict(raw_json)
    duzeltilmis["energy_infrastructures"] = kalan_enerjiler
    duzeltilmis["resource_hubs"] = list(raw_json.get("resource_hubs") or []) + kurtarilanlar
    return duzeltilmis



_KABA_OLAY_TURU_ANAHTAR_KELIMELERI: List[Tuple[str, "EventType"]] = [
  
    ("orman yangini", EventType.ORMAN_YANGINI),
    ("siber sald", EventType.SIBER_SALDIRI),
    ("bombali sald", EventType.TEROR),
    ("teror", EventType.TEROR),
    ("heyelan", EventType.HEYELAN),
    ("toprak kaymasi", EventType.HEYELAN),
    (" cig ", EventType.CIG),  
    ("tahliye", EventType.TAHLIYE),
    ("volkan", EventType.VOLKANIK_PATLAMA),
    ("baraj", EventType.BARAJ_COKMESI),
    ("tsunami", EventType.TSUNAMI),
    ("gemi kazasi", EventType.GEMI_KAZASI),
    ("gemi batti", EventType.GEMI_KAZASI),
    ("ucak kazasi", EventType.UCAK_KAZASI),
    ("ucak dustu", EventType.UCAK_KAZASI),
    ("tren kazasi", EventType.TREN_KAZASI),
    ("trafik kazasi", EventType.TRAFIK_KAZASI),
    ("zincirleme kaza", EventType.TRAFIK_KAZASI),
    ("maden kazasi", EventType.MADEN_KAZASI),
    ("maden ocaginda", EventType.MADEN_KAZASI),
    ("kimyasal sizinti", EventType.KIMYASAL_SIZINTI),
    ("kimyasal sizma", EventType.KIMYASAL_SIZINTI),
    ("radyasyon", EventType.RADYASYON_SIZINTISI),
    ("nukleer sizinti", EventType.RADYASYON_SIZINTISI),
    ("salgin", EventType.SALGIN_HASTALIK),
    ("izdiham", EventType.IZDIHAM),
    ("toplu zehirlenme", EventType.TOPLU_ZEHIRLENME),
    ("gida zehirlenmesi", EventType.TOPLU_ZEHIRLENME),
    ("bina cokmesi", EventType.BINA_COKMESI),
    ("cati cokmesi", EventType.BINA_COKMESI),
    ("hortum", EventType.FIRTINA),
    ("kasirga", EventType.FIRTINA),
    ("firtina", EventType.FIRTINA),
    ("kuraklik", EventType.KURAKLIK),
    ("deprem", EventType.DEPREM),
    ("sel ", EventType.SEL),
    ("su baskini", EventType.SEL),
    ("yangin", EventType.YANGIN),
    ("patlama", EventType.PATLAMA),
    ("infilak", EventType.PATLAMA),
    ("savas", EventType.SAVAS),
    ("saldiri", EventType.TEROR),
]
"""`normalize_tr` ile sadeleştirilmiş metin İÇİNDE aranan, kaba ama ucuz bir
anahtar-kelime -> `EventType` eşlemesi. Bu, LLM'in `EXTRACTION_PROMPT_
TEMPLATE`daki "OLAY TÜRÜ SINIFLANDIRMA KURALI"nın YERİNİ ALMAZ (o kural
HALA birincil/tercih edilen yoldur, çok daha nüanslıdır) — bu SADECE model
TAMAMEN SESSİZ kaldığında devreye giren bir SON ÇARE ağıdır."""

_OLAY_TURU_ISIM_SONEKI: Dict["EventType", str] = {
    EventType.DEPREM: "Depremi",
    EventType.SEL: "Seli",
    EventType.YANGIN: "Yangini",
    EventType.ORMAN_YANGINI: "Orman Yangini",
    EventType.CIG: "Cig Olayi",
    EventType.HEYELAN: "Heyelani",
    EventType.TEROR: "Teror Olayi",
    EventType.TAHLIYE: "Tahliyesi",
    EventType.PATLAMA: "Patlamasi",
    EventType.SAVAS: "Catismasi",
    EventType.SIBER_SALDIRI: "Siber Saldirisi",
    EventType.SALGIN_HASTALIK: "Salgin Hastaligi",
    EventType.IZDIHAM: "Izdihami",
    EventType.GEMI_KAZASI: "Gemi Kazasi",
    EventType.KIMYASAL_SIZINTI: "Kimyasal Sizintisi",
    EventType.UCAK_KAZASI: "Ucak Kazasi",
    EventType.TREN_KAZASI: "Tren Kazasi",
    EventType.TRAFIK_KAZASI: "Trafik Kazasi",
    EventType.BARAJ_COKMESI: "Baraj Cokmesi",
    EventType.MADEN_KAZASI: "Maden Kazasi",
    EventType.FIRTINA: "Firtinasi",
    EventType.TSUNAMI: "Tsunamisi",
    EventType.RADYASYON_SIZINTISI: "Radyasyon Sizintisi",
    EventType.VOLKANIK_PATLAMA: "Volkanik Patlamasi",
    EventType.BINA_COKMESI: "Bina Cokmesi",
    EventType.TOPLU_ZEHIRLENME: "Toplu Zehirlenme Olayi",
    EventType.KURAKLIK: "Kurakligi",
}
"""Yedek `Event.isim`ini ("<Yer Adı> <bu sonek>") doğal bir Türkçe ifadeye
çevirmek için — ör. "Sivrice" + "Depremi" -> "Sivrice Depremi"."""

_YER_ADI_ADAY_DURAK_KELIMELERI = frozenset(
    {
        "bir", "bu", "su", "ve", "ile", "icin", "olan", "dun", "bugun",
        "yarin", "az", "cok", "sabah", "aksam", "gece", "her", "tum",
        "sonra", "once", "yine", "ama", "fakat", "ancak", "hem",
    }
)
"""`_metinden_yer_adi_adayi_cikar`ın, gerçek bir yer adı SANMAMASI gereken
çok genel/kısa Türkçe kelimeler — bu liste `Neo4jConnection._YER_ADI_DURAK_
KELIMELERI` (database.py) ile AYNI AMACA hizmet eder ama FARKLI bir bağlamda
çalışır (o, bir VARLIK İSMİNDEN tür-sonu kelimeleri eler; bu, HAM METNİN
BAŞINDAN olası dolgu kelimeleri eler) — bilinçli olarak AYRI tutulmuştur."""


def _metinden_kaba_event_type_tahmin_et(text: str) -> Optional["EventType"]:
    """`text` içinde `_KABA_OLAY_TURU_ANAHTAR_KELIMELERI`deki herhangi bir
    ifadeyi (Türkçe karakter/büyük-küçük harf farkı gözetmeksizin) arar;
    İLK eşleşen `EventType`i döner. Hiçbiri eşleşmezse `None` döner — bu
    durumda çağıran taraf (bkz. `_yedek_kriz_olayi_uret`) HİÇBİR yedek
    `Event` ÜRETMEZ (metin gerçekten bir kriz raporu OLMAYABİLİR; "boş JSON
    = HER ZAMAN gizli bir kriz var" varsayımı YANLIŞ OLURDU)."""
    normalize_edilmis = f" {normalize_tr(text)} "
    for anahtar, tur in _KABA_OLAY_TURU_ANAHTAR_KELIMELERI:
        if anahtar in normalize_edilmis:
            return tur
    return None


def _metinden_yer_adi_adayi_cikar(text: str) -> Optional[str]:

    for aday in re.findall(r"\b[A-ZÇĞİÖŞÜ][a-zçğıöşüA-ZÇĞİÖŞÜ]*\b", text):
        if len(aday) >= 3 and normalize_tr(aday) not in _YER_ADI_ADAY_DURAK_KELIMELERI:
            return aday

    ilk_kelime_esleme = re.match(r"[A-Za-zÇĞİÖŞÜçğıöşü]+", text.strip())
    if ilk_kelime_esleme:
        aday = ilk_kelime_esleme.group(0)
        if len(aday) >= 3 and normalize_tr(aday) not in _YER_ADI_ADAY_DURAK_KELIMELERI:
            return aday[:1].upper() + aday[1:].lower()
    return None


_YEDEK_OLAY_VARSAYILAN_KOORDINAT: Tuple[float, float] = (0.0, 0.0)


def _yedek_kriz_olayi_uret(text: str) -> Optional[Event]:

    olay_turu = _metinden_kaba_event_type_tahmin_et(text)
    if olay_turu is None:
        return None

    yer_adi = _metinden_yer_adi_adayi_cikar(text) or "Bilinmeyen Konum"
    sonek = _OLAY_TURU_ISIM_SONEKI.get(olay_turu, "Olayi")
    isim = f"{yer_adi} {sonek}"

    try:
        return Event(
            isim=isim,
            enlem=_YEDEK_OLAY_VARSAYILAN_KOORDINAT[0],
            boylam=_YEDEK_OLAY_VARSAYILAN_KOORDINAT[1],
            durum=OperationalStatus.AKTIF,
            event_type=olay_turu,
            etki_alani_km=5.0,
            siddet=EventSeverity.YUKSEK,
        )
    except ValidationError as exc: 
        logger.error("Yedek kriz olayi (fallback) OLUSTURULAMADI: %s", exc)
        return None


_FEW_SHOT_ORNEK_METIN = (
    "3 Mart 2024 sabahi saat 09:00 civarinda, Elazig'da sel felaketi yasandi. "
    "Elazig Havalimani sel nedeniyle kismen kullanilabilir durumda. Elazig-"
    "Bingol karayolu ulasima kapandi. Elazig Trafo Merkezi hasar aldi. "
    "Bolgedeki baz istasyonu 4 saatlik batarya ile yayinda. Bolgesel yakit "
    "deposunun stok seviyesi yuzde 30'a dustu. AFAD'a bagli 120 personelli "
    "bir arama kurtarma birimi bolgeye sevk edildi. Ayrica sehir merkezinde, "
    "Gazi Caddesi ve Lise Sokak kesisiminde bir patlama meydana geldi; bu iki "
    "cadde/sokak da trafige kapandi."
)

_FEW_SHOT_ORNEK_CIKTI: Dict[str, Any] = {
    "facilities": [
        {
            "isim": "Elazig Havalimani",
            "aciklama": "Sel nedeniyle kismen kullanilabilir durumda",
            "enlem": 38.608,
            "boylam": 39.291,
            "durum": "Kismi Aktif",
            "facility_type": "Havalimani",
            "kapasite": 50,
            "savunma_seviyesi": 0,
            "mevcut_durum": "Hasarli",
        }
    ],
    "infrastructures": [
        {
            "isim": "Elazig-Bingol Karayolu",
            "enlem": 38.68,
            "boylam": 39.30,
            "durum": "Hasarli",
            "infrastructure_type": "Karayolu",
            "tonaj_kapasitesi": 40,
            "uzunluk_km": 95,
            "acik_mi": False,
            "bitis_enlem": 38.85,
            "bitis_boylam": 39.55,
        },
      
        {
            "isim": "Gazi Caddesi",
            "enlem": 38.677,
            "boylam": 39.220,
            "durum": "Hasarli",
            "infrastructure_type": "Sokak",
            "tonaj_kapasitesi": None,
            "uzunluk_km": None,
            "acik_mi": False,
            "bitis_enlem": None,
            "bitis_boylam": None,
        },
        {
            "isim": "Lise Sokak",
            "enlem": 38.6775,
            "boylam": 39.2205,
            "durum": "Hasarli",
            "infrastructure_type": "Sokak",
            "tonaj_kapasitesi": None,
            "uzunluk_km": None,
            "acik_mi": False,
            "bitis_enlem": None,
            "bitis_boylam": None,
        },
    ],
    "energy_infrastructures": [
        {
            "isim": "Elazig Trafo Merkezi",
            "enlem": 38.67,
            "boylam": 39.25,
            "durum": "Hasarli",
            "energy_type": "Trafo",
            "kapasite_mw": 50,
            "yedek_guc_durumu": "Kismi",
        }
    ],
    "communication_networks": [
        {
            "isim": "Bolge Baz Istasyonu",
            "enlem": 38.675,
            "boylam": 39.235,
            "durum": "Aktif",
            "network_type": "Baz Istasyonu",
            "kapsama_yaricapi_km": 5,
            "batarya_omru_saat": 4,
        }
    ],
    "resource_hubs": [
        {
            "isim": "Bolgesel Yakit Deposu",
            "enlem": 38.665,
            "boylam": 39.225,
            "durum": "Aktif",
            "resource_type": "Yakit",
            "stok_seviyesi_yuzde": 30,
            "tukenme_hizi_gun": 3,
        }
    ],
    "units": [
        {
            "isim": "AFAD Arama Kurtarma Birimi",
            "enlem": 38.678,
            "boylam": 39.232,
            "durum": "Aktif",
            "unit_type": "AFAD",
            "personel_sayisi": 120,
            "hareket_kabiliyeti": "Tam Hareketli",
        }
    ],
    "events": [
        {
            "isim": "Elazig Seli",
            "enlem": 38.676,
            "boylam": 39.230,
            "durum": "Aktif",
            "event_type": "Sel",
            "etki_alani_km": 30,
            "siddet": "Yuksek",
            "zaman_damgasi": "2024-03-03T09:00:00+03:00",
        },
        {

            "isim": "Sehir Merkezi Patlamasi",
            "enlem": 38.6772,
            "boylam": 39.2202,
            "durum": "Aktif",
            "event_type": "Patlama",
            "etki_alani_km": None,
            "siddet": "Yuksek",
            "zaman_damgasi": "2024-03-03T09:00:00+03:00",
        },
    ],
    "relationships": [
        {"kaynak_isim": "Elazig Seli", "hedef_isim": "Elazig Havalimani", "tip": "AFFECTS", "agirlik": None},
        {"kaynak_isim": "Elazig Seli", "hedef_isim": "Elazig-Bingol Karayolu", "tip": "AFFECTS", "agirlik": None},
        {"kaynak_isim": "Elazig Havalimani", "hedef_isim": "Elazig Trafo Merkezi", "tip": "DEPENDS_ON", "agirlik": None},
        {"kaynak_isim": "Bolge Baz Istasyonu", "hedef_isim": "Elazig Trafo Merkezi", "tip": "DEPENDS_ON", "agirlik": None},
        {"kaynak_isim": "AFAD Arama Kurtarma Birimi", "hedef_isim": "Elazig Havalimani", "tip": "STATIONED_AT", "agirlik": None},
        {"kaynak_isim": "Sehir Merkezi Patlamasi", "hedef_isim": "Gazi Caddesi", "tip": "AFFECTS", "agirlik": None},
        {"kaynak_isim": "Sehir Merkezi Patlamasi", "hedef_isim": "Lise Sokak", "tip": "AFFECTS", "agirlik": None},
    ],
}


def _build_few_shot_json_block(ornek_cikti: Dict[str, Any] = _FEW_SHOT_ORNEK_CIKTI) -> str:
   
    ornek_json = json.dumps(ornek_cikti, ensure_ascii=False, indent=2)
    return ornek_json.replace("{", "{{").replace("}", "}}")



_FEW_SHOT_ORNEK_METIN_2 = (
    "20 Ocak 2025 sabahi, Rize'nin Ikizdere ilcesinde saganak yagislar "
    "sonucu heyelan meydana geldi. Bolgedeki ana yollar heyelan nedeniyle "
    "ulasima kapandi."
)

_FEW_SHOT_ORNEK_CIKTI_2: Dict[str, Any] = {
    "facilities": [],
    "infrastructures": [
        
        {
            "isim": "Ikizdere Ana Yolu",
            "enlem": 40.78,
            "boylam": 40.53,
            "durum": "Hasarli",
            "infrastructure_type": "Karayolu",
            "tonaj_kapasitesi": None,
            "uzunluk_km": None,
            "acik_mi": False,
            "bitis_enlem": None,
            "bitis_boylam": None,
        }
    ],
    "energy_infrastructures": [],
    "communication_networks": [],
    "resource_hubs": [],
    "units": [],
    "events": [
     
        {
            "isim": "Ikizdere Heyelani",
            "enlem": 40.78,
            "boylam": 40.53,
            "durum": "Aktif",
            "event_type": "Heyelan",
            "etki_alani_km": None,
            "siddet": "Yuksek",
            "zaman_damgasi": "2025-01-20T08:00:00+03:00",
        }
    ],
    "relationships": [
        {"kaynak_isim": "Ikizdere Heyelani", "hedef_isim": "Ikizdere Ana Yolu", "tip": "AFFECTS", "agirlik": None},
    ],
}


_FEW_SHOT_ORNEK_METIN_3 = (
    "10 Kasim 2025 sabahi, Rize'nin Camlihemsin merkezinde asiri saganak "
    "yagisa bagli devasa bir heyelan meydana geldi. Ana yollar camur ve "
    "kaya parcalariyla kapandi."
)

_FEW_SHOT_ORNEK_CIKTI_3: Dict[str, Any] = {
    "facilities": [],
    "infrastructures": [
        {
            "isim": "Camlihemsin Ana Yolu",
            "enlem": 41.05,
            "boylam": 41.03,
            "durum": "Hasarli",
            "infrastructure_type": "Karayolu",
            "tonaj_kapasitesi": None,
            "uzunluk_km": None,
            "acik_mi": False,
            "bitis_enlem": None,
            "bitis_boylam": None,
        }
    ],
    "energy_infrastructures": [],
    "communication_networks": [],
    "resource_hubs": [],
    "units": [],
    "events": [
        {
            "isim": "Camlihemsin Heyelani",
            "enlem": 41.05,
            "boylam": 41.03,
            "durum": "Aktif",
            "event_type": "Heyelan",
            "etki_alani_km": None,
            "siddet": "Yuksek",
            "zaman_damgasi": "2025-11-10T07:30:00+03:00",
        }
    ],
    "relationships": [
        {"kaynak_isim": "Camlihemsin Heyelani", "hedef_isim": "Camlihemsin Ana Yolu", "tip": "AFFECTS", "agirlik": None},
    ],
}



EXTRACTION_PROMPT_TEMPLATE = """Sen, Turkiye'nin TAMAMINDAKI (81 il) stratejik noktalara, karayolu/sokak
agina ve altyapisina hakim, kusursuz calisan bir C4ISR Karar Destek
Sistemi'nin kurmay zekasisin. Gorevin, sahadan gelen HAM ve bazen KUSURLU
(yazim hatali, telsiz/sosyal medya usulu kisaltilmis) kriz istihbaratini
analiz ederek icindeki varliklari (node) ve bu varliklar arasindaki
iliskileri (relationship/edge) tespit edip, SADECE asagida tanimlanan JSON
semasina uygun, gecerli bir JSON nesnesi olarak dondurmektir.

# YAZIM HATASI TOLERANSI KURALI (KESIN — sahada telsiz/klavye hatasi NORMALDIR)
Sahadan gelen istihbarat NADIREN kusursuz yazilmis olur (ör. "kcprüsü"
aslinda "Köprüsü"dür, "hastanesi" yerine "hastahanesi" yazilabilir, harfler
eksik/fazla/yer degistirmis olabilir). SEN bu tur yazim hatalarini BAGLAMDAN
(anlamdan) otomatik olarak DUZELT ve varligin GERCEK/DOGRU ismini `isim`
alanina TEMIZ sekilde yaz (ör. "Kömürhan Kcprüsü" -> `isim`: "Kömürhan
Köprüsü"); yazim hatasi ASLA bir varligi reddetme veya atlama GEREKCESI
DEGILDIR — aksine SEN tam olarak bunun icin, kusurlu sahadan-veriyi
kusursuz Bilgi Grafi kaydina cevirmen icin varsin.

# CIKTI SAFLIGI KURALI (KESIN — ASLA IHLAL ETME)
Cevabin SADECE ve SADECE saf, gecerli bir JSON nesnesi OLACAK. ASLA markdown
kod blogu (```json veya ```), ASLA "Iste sonuc:", "Tabii, iste istenen JSON:"
gibi bir on-aciklama/son-aciklama metni, ASLA JSON disinda tek bir karakter
bile EKLEME. Cevabinin ILK karakteri `{{`, SON karakteri `}}` olmalidir.

# VARLIK TIPLERI (node tipleri ve alanlari)
- facilities (Facility): isim, aciklama, enlem, boylam, durum, facility_type
  (Havalimani/Hastane/Askeri Us/Liman/Siginak), kapasite, savunma_seviyesi,
  mevcut_durum (Aktif/Hasarli/Yok Edildi). UYARI: `facility_type` SADECE bu
  5 degerden biri OLABILIR — bir cadde/sokak/bulvar/yol ismini BURAYA ASLA
  KOYMA, asagidaki "SEHIR ICI (URBAN) CADDE/SOKAK KURALI"na bak. AYNI SEKILDE,
  adinda "Kopru", "Viyaduk", "Tunel" veya "Kavsak" GECEN HICBIR YAPIYI (ör.
  "Komurhan Koprusu") BURAYA KOYMA — bunlar KESINLIKLE `infrastructures`
  listesine gider, asagidaki "YAPI (KOPRU/TUNEL/VIYADUK/KAVSAK) KURALI"na bak.
- infrastructures (Infrastructure): isim, enlem, boylam, infrastructure_type
  (Karayolu/Demiryolu/Kopru/Tunel/Viyaduk/Sokak), tonaj_kapasitesi, uzunluk_km,
  acik_mi, bitis_enlem, bitis_boylam (BIR GUZERGAHIN IKI UCU: enlem/boylam
  BASLANGIC noktasi, bitis_enlem/bitis_boylam BITIS noktasidir; harita bu
  varligi tek nokta degil bir CIZGI olarak cizecegi icin bu iki nokta MUTLAKA
  BIRBIRINDEN FARKLI olmalidir, ayni koordinati iki kez yazma). "Sokak" tipi
  ve SEHIR ICI cadde/sokak/bulvar kurali icin asagidaki "SEHIR ICI (URBAN)
  CADDE/SOKAK KURALI" bolumune KESINLIKLE bak.
- energy_infrastructures (EnergyInfrastructure): isim, enlem, boylam,
  energy_type (Baraj/Trafo/Santral), kapasite_mw, yedek_guc_durumu (Yok/Kismi/Tam)
- communication_networks (CommunicationNetwork): isim, enlem, boylam,
  network_type (Baz Istasyonu/Fiber/Uydu Terminali), kapsama_yaricapi_km,
  batarya_omru_saat
- resource_hubs (ResourceHub): isim, enlem, boylam, resource_type
  (Yakit/Gida/Su/Tibbi Malzeme/Muhimmat), stok_seviyesi_yuzde, tukenme_hizi_gun
- units (Unit): isim, enlem, boylam, unit_type
  (Askeri Birlik/AFAD/Saglik/Itfaiye/Polis/Agir Muhendislik/Arama Kurtarma/
  Lojistik/Sahil Guvenlik), personel_sayisi, hareket_kabiliyeti
  (Statik/Sinirli Hareketli/Tam Hareketli)
- events (Event): isim, enlem, boylam, event_type
  (Savas/Deprem/Sel/Yangin/Siber Saldiri/Patlama/Orman Yangini/Cig/Heyelan/
  Teror/Tahliye/Salgin Hastalik/Izdiham/Gemi Kazasi/Kimyasal Sizinti/
  Ucak Kazasi/Tren Kazasi/Trafik Kazasi/Baraj Cokmesi/Maden Kazasi/Firtina/
  Tsunami/Radyasyon Sizintisi/Volkanik Patlama/Bina Cokmesi/Toplu
  Zehirlenme/Kuraklik), etki_alani_km, siddet (Dusuk/Orta/Yuksek/Kritik/
  Katastrofik), zaman_damgasi. `event_type` secimi icin asagidaki "OLAY
  TURU (EVENT_TYPE) SINIFLANDIRMA KURALI" bolumune KESINLIKLE bak. ONEMLI:
  bir olayin KONUMU bir kopru/yol/cadde OLMAK ZORUNDA DEGILDIR — bir ilce
  adi, bir dag/yayla, bir mahalle veya genel bir idari bolge de gecerli bir
  konumdur; konumu metinde gecen EN GENIS ve DOGRU idari/cografi isimle
  (ör. "Sivrice", "Kizilay Meydani", "Nemrut Dagi") `isim` alanina yaz —
  bu isim GERCEK bir yol/koprüyle eslesmek ZORUNDA DEGILDIR, sistem bunu
  KENDI coğrafi eslestirme katmaninda (yerel veri + gerektiginde harici
  geocoding) cozer; SEN sadece dogru/net bir yer adi URETMEKLE
  YUKUMLUSUN, o adin ONCEDEN veritabaninda olup olmadigini DUSUNME.

# ILISKI TIPLERI (relationships alani icin gecerli degerler)
CONNECTED_TO, DEPENDS_ON, AFFECTS, STATIONED_AT, SUPPLIES, ROUTES_THROUGH,
CONTROLS, PROTECTS, THREATENS, COMMUNICATES_WITH, LOCATED_NEAR

# ILISKI ZORUNLULUGU KURALI (KESIN — asla atlama)
Metinde bir OLAY (events) ile birlikte en az bir baska varlik (tesis, sokak/
cadde, birim, altyapi vb.) geciyorsa, `relationships` listesi ASLA BOS
BIRAKILMAZ: o olay ile etkiledigi/ilgili oldugu HER varlik arasinda KESINLIKLE
bir iliski kaydi olustur:
- Olay bir varligi FIILEN etkiliyorsa (hasar verdi, kapattı, vurdu, vb.)
  -> `AFFECTS` kullan (kaynak=olayin ismi, hedef=etkilenen varligin ismi).
- Olay sadece bir varligin/konumun icinde/yakininda GERCEKLESIYORSA (baska
  bir dogrudan etki belirtilmemisse) -> `LOCATED_IN` kullan.
- Metinde BIRDEN FAZLA etkilenen varlik varsa (ör. hem bir cadde hem bir
  sokak, ör. "X Caddesi ve Y Sokak"), OLAYDAN HER BIRINE AYRI AYRI birer
  iliski kaydi olustur (TEK bir iliskiyle yetinme).
- Bu kural, yukaridaki "SEHIR ICI (URBAN) CADDE/SOKAK KURALI" ile birlikte
  calisir: sokak/cadde kayitlarini olusturduktan SONRA, MUTLAKA olay ->
  sokak/cadde iliskilerini de ekle; sadece varlik listelerini doldurup
  iliskiyi ATLAMAK bu sistemde bir HATA sayilir (varlik ile olay arasindaki
  baglanti, Bilgi Grafinda o baglanti KURULMADAN gorunmez).

# OLAY TURU (EVENT_TYPE) SINIFLANDIRMA KURALI (KESIN — asla karistirma)
# "ULUSAL OLCEKLI ESNEKLIK" (bkz. `models.EventType`): dar bir altyapi
# sablonuna "overfit" olmaktan kacinmak icin, Turkiye capinda GERCEKTEN
# sik gorulen kriz turlerinin TAMAMI (Orman Yangini, Cig, Heyelan, Teror,
# Tahliye ve digerleri dahil) taksonomiye dahil edilmistir; bilinmeyen HER
# krize "Patlama" demek ZORUNDA kalinmaz (bkz. `models.EventType`deki AYNI
# basliklı not).
`event_type` secerken asagidaki AYRIMI KESINLIKLE gozet; kasit/dusman unsuru
OLMAYAN bir olayi "Savas"/"Teror" ile ETIKETLEME (en sik yapilan hata budur):
- "Savas": SADECE ULUSLARARASI/askeri nitelikli bilincli bir silahli
  catisma/saldiri soz konusuysa (ör. "askeri catisma", "sinir otesi
  saldiri", "hava saldirisi" bir DEVLET/askeri baglaminda).
- "Teror": Bilincli, dusmanca bir eylem ama "Savas"tan FARKLI olarak IC
  GUVENLIK/kolluk odakli bir tehditse (ör. "teror saldirisi", "bombali
  saldiri" -bir teror orgutu/dusman unsuru baglaminda-, "vuruldu"). "Savas"
  ile "Teror" arasinda EMIN olamiyorsan (ör. saldirganin kimligi/motivasyonu
  acik degilse) "Teror" SEC — bu, ic guvenlik makamlarinin (Polis/Jandarma)
  ilk mudahaleci oldugu DAHA YAYGIN/genel senaryodur.
- "Patlama": Dogal gaz patlamasi, endustriyel/kaza kaynakli patlama, bomba
  imha/kaza sonucu patlama gibi KASIT/DUSMAN unsuru ACIKCA belirtilmeyen
  HER TURLU patlama/infilak icin KULLAN. "Dogal gaz patlamasi oldu" GIBI bir
  ifade NEREDEYSE HER ZAMAN "Patlama"dir, "Savas"/"Teror" DEGILDIR.
- "Yangin": Patlama sonucu degil, dogrudan bir sehir ici/bina yangini/alev
  soz konusuysa.
- "Orman Yangini": Acikca bir orman/arazi/kirsal alan yangini soz
  konusuysa (genel "Yangin"dan BILEREK AYRI — farkli bir mudahale profili
  gerektirir, bkz. modul-ustu not).
- "Deprem"/"Sel": Dogal afet acikca belirtiliyorsa.
- "Cig": Acikca bir cig (kar/buz kutlesi kaymasi) soz konusuysa.
- "Heyelan": Acikca bir heyelan/toprak kaymasi soz konusuysa (Deprem/Sel
  SONUCU olusan bir heyelan bile olsa, asil olay olarak heyelan
  belirtiliyorsa "Heyelan" SEC).
- "Siber Saldiri": Acikca bir siber/dijital saldiri soz konusuysa.
- "Tahliye": Metin, bir TEHLIKEDEN degil bizzat bir TAHLIYE/ONLEYICI
  bosaltma OPERASYONUNDAN bahsediyorsa (ör. "onleyici tahliye emri
  verildi", "bolge bosaltiliyor") — tetikleyen tehlike ayrica acikca
  belirtilmemis veya ayri bir olay olarak raporlanmissa bile, bu KENDI
  BASINA gecerli bir olay turudur.
- "Salgin Hastalik": Bulasici/toplu bir hastalik salgini (kolera, zehirlenme
  DISINDA bir epidemi/pandemi) soz konusuysa.
- "Izdiham": Kalabalik bir etkinlik/toplanmada sikisma/cignenme kaynakli
  toplu yaralanma soz konusuysa (ör. konser, mac, dini toren, sinir
  kapisi yiginlagi).
- "Gemi Kazasi": Bir deniz/liman/bogaz tasimaciligi kazasi (batma,
  carpisma, karaya oturma) soz konusuysa.
- "Kimyasal Sizinti": Endustriyel tesis/tanker/boru hatti kaynakli, ANLIK
  bir patlama DEGIL SUREGELEN bir zehirli/tehlikeli madde sizintisi/
  kontaminasyonu soz konusuysa (bir patlama SONUCU sizinti da olsa, asil
  vurgu sizinti/kontaminasyondaysa bu turu SEC).
- "Ucak Kazasi"/"Tren Kazasi": Sirasiyla bir hava araci veya rayli sistem
  kazasi ACIKCA belirtiliyorsa.
- "Trafik Kazasi": Bir karayolu uzerinde (tipik olarak cok aracli/
  zincirleme) buyuk olcekli bir trafik kazasi soz konusuysa — yolun
  KENDISININ fiziksel butunlugu (kapanmasi/yikilmasi) DEGIL, USTUNDEKI
  arac/insanlar etkileniyorsa bu turu SEC ("Kopru/Yol Yikimi" gibi bir
  altyapi hasari ayri, ilgili Infrastructure kaydinin `acik_mi=false`
  yapilmasiyla ele alinir).
- "Baraj Cokmesi": Bir barajin/enerji altyapisinin yapisal olarak
  cokmesi/gocmesi ACIKCA belirtiliyorsa (yagis kaynakli sıradan bir "Sel"
  ile KARISTIRMA — burada tetikleyici YAPISAL bir arizadir).
- "Maden Kazasi": Bir maden ocaginda gocuk/gaz patlamasi/su baskini
  kaynakli hapsolma/kaza soz konusuysa.
- "Firtina": Siddetli ruzgar/hortum/lodos kaynakli hasar (catı ucmasi,
  agac/direk devrilmesi) soz konusuysa — su/toprak kutlesi DEGIL RUZGAR
  kaynakliysa bu turu SEC ("Sel"/"Heyelan" ile KARISTIRMA).
- "Tsunami": Deniz tabanindaki bir depremin/heyelanin tetikledigi dev
  dalga ACIKCA belirtiliyorsa (SADECE kiyi bolgelerinde gecerli bir
  senaryodur).
- "Radyasyon Sizintisi": Nukleer santral/radyoaktif madde kaynakli bir
  kontaminasyon tehdidi soz konusuysa.
- "Volkanik Patlama": Bir yanardagin patlamasi/kul puskurmesi ACIKCA
  belirtiliyorsa.
- "Bina Cokmesi": Deprem/Patlama gibi HARICI bir tetikleyici
  BELIRTILMEDEN, bir yapinin (imar/zemin kusuru vb. nedenle)
  KENDILIGINDEN coktugu belirtiliyorsa.
- "Toplu Zehirlenme": Gida/su/gaz kaynakli, TEK bir olayda coklu kisiyi
  etkileyen (ama BULASICI OLMAYAN — "Salgin Hastalik"tan farkli) bir
  zehirlenme vakasi soz konusuysa.
- "Kuraklik": Uzun sureli yagis yetersizligi kaynakli bir su/tarim krizi
  ACIKCA belirtiliyorsa.
- EMIN OLAMADIGIN durumlarda (ör. "patlama" gecen ama saldiri/dusman
  ifadesi OLMAYAN bir metin): "Savas"/"Teror" DEGIL, "Patlama" SEC —
  bunlar sadece ACIKCA askeri/teror baglami varsa kullanilan, varsayilan
  OLMAYAN, DAR kategorilerdir.
- `event_type` icin GECERLI TEK 27 deger vardir: "Savas", "Deprem", "Sel",
  "Yangin", "Siber Saldiri", "Patlama", "Orman Yangini", "Cig", "Heyelan",
  "Teror", "Tahliye", "Salgin Hastalik", "Izdiham", "Gemi Kazasi",
  "Kimyasal Sizinti", "Ucak Kazasi", "Tren Kazasi", "Trafik Kazasi",
  "Baraj Cokmesi", "Maden Kazasi", "Firtina", "Tsunami", "Radyasyon
  Sizintisi", "Volkanik Patlama", "Bina Cokmesi", "Toplu Zehirlenme",
  "Kuraklik". BUNLARIN DISINDA KENDI KATEGORINI ASLA UYDURMA (ör. "Yikim",
  "Cokme", "Kaza", "Vurulma" GIBI degerler GECERSIZDIR ve kaydin TAMAMEN
  REDDEDILMESINE/KAYBOLMASINA NEDEN OLUR). Bir yapinin/koprunun HARICI bir
  tetikleyici OLMADAN cokmesi icin "Bina Cokmesi" SEC (ARTIK "Patlama"ya
  SIKISTIRILMAZ); yukaridaki 27 kategorinin HICBIRINE acikca girmeyen ama
  kasit/dusman unsuru da BELIRTILMEYEN, gercekten siniflandirilamayan bir
  olay icin SON CARE olarak "Patlama" SEC (yukaridaki "EMIN OLAMADIGIN
  durumlarda... Patlama SEC" kuralinin DOGRUDAN bir uzantisidir).

# STATU KALIBRASYONU (facilities -> mevcut_durum icin KESIN kurallar)
Facility varliklarindaki `mevcut_durum` alanini asagidaki KESIN esleme
tablosuna gore belirle; yorum katma, sadece metindeki ifadeye bak:
- Metinde "kullanilamiyor", "agir hasarli", "hasar gordu/aldi",
  "hizmet veremiyor", "kismen kullanilabilir" gibi ifadeler geciyorsa
  -> "Hasarli" SEC.
- SADECE "tamamen yikildi", "enkaz haline geldi", "yok oldu" gibi KESIN ve
  nihai yikim ifadeleri geciyorsa -> "Yok Edildi" SEC. "Hasar gordu" veya
  "kullanilamiyor" ifadeleri TEK BASINA "Yok Edildi" icin YETERLI DEGILDIR.
- Metinde herhangi bir hasar/olumsuzluk belirtisi yoksa -> "Aktif" SEC.

# KOORDINAT KALIBRASYONU (enlem/boylam icin KESIN kural)
Koordinatlari (enlem/boylam) KESINLIKLE Dogu Anadolu (ozellikle Elazig ve
cevresi, enlem 38-39, boylam 39-40 arasi) gerceğine uygun uret:
- Metinde acikca farkli bir il/bolge belirtilmedigi surece, uretilecek tum
  enlem/boylam degerleri bu araligin (enlem 38-39, boylam 39-40) makul bir
  yakininda olmalidir.
- Metinde gecen KOMSU sehir/ilce isimlerine (ör. Malatya, Bingol, Tunceli,
  Diyarbakir, Bitlis vb. sadece BAGLAM/mesafe belirtmek icin anilmis olabilir)
  ALDANIP olayin/varligin GERCEKTE bahsedildigi/gectigi yer yerine o komsu
  sehrin koordinatlarina PIN ATMA. Sadece metinde konum olarak acikca
  ISLENEN yer icin koordinat uret; sadece referans/mesafe amacli anilan bir
  yer icin ayri bir enlem/boylam UYDURMA.
- Metinde konum hic belirtilmemisse bile, rastgele veya alakasiz uzak bir
  sehrin koordinatini UYDURMA; bunun yerine Elazig ve cevresindeki (enlem
  38-39, boylam 39-40) en makul tahmini kullan.

# SEHIR ICI (URBAN) CADDE/SOKAK KURALI (KESIN — sehir ici krizler icin KRITIK)
Metinde "Cadde", "Cad.", "Sokak", "Sok.", "Bulvar", "Blv.", "Yol", "Yolu"
gibi ifadeler geciyorsa (ör. "Vali Fahri Bey Caddesi", "Akin Sokak",
"valifahribey caddesi"), bu varlik KESINLIKLE `infrastructures` LISTESINE
eklenir ve `infrastructure_type` alani KESINLIKLE "Sokak" OLMALIDIR.
EN ONEMLI/EN SIK YAPILAN HATA: Boyle bir cadde/sokak/bulvar/yol ismini ASLA
VE ASLA `facilities` (Tesis) listesine EKLEME — `FacilityType` enum'i
(Havalimani/Hastane/Askeri Us/Liman/Siginak) icinde "Sokak"/"Cadde" DIYE BIR
SECENEK YOKTUR; bu listeye konursa Pydantic dogrulamasi KESIN olarak
BASARISIZ OLUR ve kayit TAMAMEN KAYBOLUR (sistem krizi hic GORMEZ). Bir
varlik ismi cadde/sokak/bulvar/yol kelimesi TASIYORSA, bu TEK BASINA onu
`infrastructures` listesine yazman icin YETERLI VE KESIN bir sebeptir.
- "Karayolu" ile KARISTIRMA: "Karayolu" sehirlerarasi/ana devlet-il yolu
  icindir; sehir ICI her turlu cadde/sokak/bulvar/mahalle yolu icin HER ZAMAN
  "Sokak" kullan.
- Metinde BIRDEN FAZLA cadde/sokak ismi geciyorsa (ör. bir kesisim: "X
  Caddesi ve Y Sokak kesisiminde..." veya "X Caddesi Y Sokak uzerinde..."),
  HER BIR cadde/sokak icin AYRI bir `infrastructures` kaydi olustur (TEK bir
  birlesik/kesisim ismi ASLA UYDURMA, ör. "X Caddesi - Y Sokak Kesisimi"
  YAZMA) — sistem bu isimleri, ONCEDEN yuklu GERCEK bir sokak veritabaninda
  TEK TEK, AYRI AYRI arayip esler; birlesik bir isim HICBIR GERCEK sokakla
  eslesmez ve rapor sessizce etkisiz kalir.
- `isim` alanina SADECE cadde/sokak/bulvarin kendi adini yaz (ör. "Vali Fahri
  Bey Caddesi"); mahalle/ilce, olay aciklamasi veya baska bir ek ifadeyi
  `isim`e KARISTIRMA (aksi halde eslestirme basarisiz olur).
- Boyle bir sokak kaydi icin `uzunluk_km`/`tonaj_kapasitesi`/`bitis_enlem`/
  `bitis_boylam` gibi alanlar metinde acikca gecmiyorsa `null` birakabilirsin
  (bkz. asagidaki "KISA/EKSIK METIN KURALI") — sistem bunlari otomatik
  tamamlar. ASIL KRITIK olan `isim`, `infrastructure_type="Sokak"` ve
  yol kapaliysa `acik_mi=false` ile `durum`/`mevcut_durum` alanlaridir.

# YAPI (KOPRU/TUNEL/VIYADUK/KAVSAK) KURALI (KESIN — CADDE/SOKAK KURALI ile AYNI ONEMDE)
Metinde "Kopru", "Koprusu", "Viyaduk", "Tunel", "Kavsak" gibi ifadeler
geciyorsa (ör. "Komurhan Koprusu yikildi", yazim hatali "kcprusu" dahil), bu
varlik KESINLIKLE `infrastructures` LISTESINE eklenir; `facilities`
(Tesis) DEGIL. `FacilityType` enum'i (Havalimani/Hastane/Askeri Us/Liman/
Siginak) icinde bir kopru/tunel/viyaduk secenegi YOKTUR; bu listeye
konursa Pydantic dogrulamasi KESIN olarak BASARISIZ OLUR ve kayit TAMAMEN
KAYBOLUR — TIPKI cadde/sokak hatasinda oldugu gibi.
- `infrastructure_type` alanini yapinin turune gore KESIN olarak sec:
  "Kopru" (kopru/koprusu), "Tunel" (tunel/tuneli), "Viyaduk" (viyaduk/
  viyaduku). Bir kavsak icin ayri bir tip YOKTUR; kavsaklari "Sokak" ile
  isaretle (sehir/karayolu agindaki sehir-ici bir dugum noktasidir).
- Kullanicinin YAZIM HATALARI (ör. "kcprusu", "koprüsu") bu kurali
  UYGULAMANA ENGEL DEGILDIR: metindeki ifadenin ne kastettigini BAGLAMDAN
  (ör. "... yikildi", "... cokme", "iki yaka arasi ulasim") anla ve dogru
  `infrastructure_type`i yine de sec; yazim hatasini `isim` alaninda
  DUZELTEREK (ör. "Komurhan Koprusu") yaz.
- Bu da BIR GUZERGAHTIR (yukaridaki "infrastructures" alan aciklamasina
  bak): `enlem`/`boylam` ile `bitis_enlem`/`bitis_boylam` koprunun/
  tunelin/viyadugun iki UCUNU (ör. nehrin/vadinin iki yakasi) temsil eder;
  metinde acikca gecmiyorsa "KOORDINAT KALIBRASYONU"na uygun, birbirinden
  FARKLI iki makul nokta tahmin et — tek bir nokta ASLA yeterli degildir.
- Koprunun/tunelin/viyadugun cokmesi/yikilmasi nedeniyle ulasimin/
  lojistigin kesildigi belirtiliyorsa, `acik_mi=false` ve `durum`/
  `mevcut_durum` icin "Hasarli" (agir hasarli/kullanilamiyor ifadeleri)
  veya "Yok Edildi" (KESIN "yikildi"/"cokmus" gibi nihai yikim ifadeleri
  icin) sec — bkz. yukaridaki "STATU KALIBRASYONU".

# JENERIK BINA/YAPI KURALI (KESIN: "Sivrice'de bir
# bina agir hasar gordu" gibi bir metin, modelin `facility_type: "Bina"`
# UYDURMASINA neden olabilir — `FacilityType` enum'i (Havalimani/Hastane/
# Askeri Us/Liman/Siginak) icinde "Bina"/"Ev"/"Apartman"/"Isyeri" GIBI bir
# secenek YOKTUR; boyle bir deger yazilirsa Pydantic dogrulamasi KESIN
# olarak BASARISIZ OLUR ve kayit TAMAMEN KAYBOLUR)
Metinde "bina", "ev", "apartman", "isyeri", "konut" gibi JENERIK (ozel bir
stratejik islevi belirtilmeyen) bir yapidan bahsediliyorsa, bunun icin
KESINLIKLE bir `facilities` kaydi UYDURMA — yukaridaki 5 `facility_type`
degerinden (Havalimani/Hastane/Askeri Us/Liman/Siginak) HICBIRINE
UYMUYORSA, o yapiyi `facilities` listesine HIC EKLEME. Bunun yerine, o
yapinin hasarini/durumunu ilgili `events` kaydinin `aciklama` alaninda
(serbest metin olarak, ör. "Birkac bina agir hasar gordu, biri tamamen
cokmus") veya `etki_alani_km`/`siddet` gibi alanlarda YANSIT — olay
kaydinin KENDISI bu bilgiyi tasimaya YETERLIDIR, ayrica gecersiz bir
Facility UYDURMANA GEREK YOKTUR. SADECE metinde ACIKCA yukaridaki 5
stratejik tesis turunden biri (ör. "devlet hastanesi", "havalimani",
"askeri us") geciyorsa `facilities` kaydi olustur.

# KISA/EKSIK METIN KURALI (EN ONEMLI DAVRANIS KURALLARINDAN BIRI)
Kullanici bazen SADECE birkaç kelimelik, çok kisa bir metin girer (ör.
"israil hatayi bombaladi", "hastane vuruldu"). BOYLE KISA bir metin bile
GECERLI, ANLAMLI bir kriz bildirimi sayilir; bunu ASLA reddetme, tum
listeleri bos [] birakma veya "yetersiz bilgi" gerekcesiyle atlama:
- ÖNEMLİ ("Girdi Garantisi" kuralı): metin küçük harfle
  yazılmış, imla kurallarına uymayan, kesme işaretli ("sivrice'de" gibi)
  gayri resmi bir SAHA dili olsa BİLE bu bir REDDETME gerekçesi DEĞİLDİR.
  Krizin geçtiği yer sadece bir ilçe/mahalle/şehir/dağ/genel bir bölge
  adıysa (ör. "Sivrice", "Baskil", "Kızılay Meydanı") — belirli bir
  köprü/yol/tesis OLMASA BİLE — bu yer adını `events` kaydının `isim`
  alanına (ör. "Sivrice Depremi") MUTLAKA yansıt; "hedef bulamadım" diye
  DÜŞÜNÜP tüm listeleri boş [] DÖNDÜRME. SADECE bir yer adı + bir olay
  türü (ör. "deprem") görmen, GEÇERLİ bir `events` kaydı üretmen için
  YETERLİDİR — ayrı bir tesis/altyapı kaydı GEREKMEZ.
- Metinden anlambilimsel olarak cikarilabilecek EN AZ BIR `events` (Event)
  kaydi MUTLAKA uret (metin acikca bir olaydan bahsediyorsa). Metinde bir
  tesis/varlik hedef aliniyorsa (ör. "hastane", "havalimani") ilgili
  `facilities`/`infrastructures`/vb. kaydini da uret.
- SADECE varligin/olayin TURUNU (event_type, facility_type, vb.) ve `isim`
  alanini metinden/baglamdan makul sekilde belirlemen YETERLIDIR; bunlar
  olmadan bir kayit anlamsiz olur, bu yuzden bunlari MUTLAKA doldur.
- Buna karsilik, metinde acikca GECMEYEN SAYISAL/detay alanlar (ör.
  kapasite, etki_alani_km, personel_sayisi, tonaj_kapasitesi, siddet) icin
  KESIN bir rakam UYDURMAK ZORUNDA DEGILSIN: boyle alanlar icin JSON
  degerini `null` birak. Sistem, `null` birakilan bu alanlari otomatik
  olarak makul varsayilanlarla dolduracak ve kaydi YINE DE Bilgi Grafina
  yazacaktir — SEN sadece `null` yazarak bunu ACIKCA belirt, alani hic
  atlama (anahtar JSON'da MUTLAKA bulunmali, degeri `null` olabilir).
- OZETLE: "emin degilim" -> o alani `null` yap; "hic bahsedilmiyor" -> o
  VARLIK TIPINI (facilities/units/vb.) bos [] birak; AMA metin acikca bir
  olaydan/varliktan bahsediyorsa o kaydi (turu + ismiyle) MUTLAKA URET.

# ZAMAN KURALI (events -> zaman_damgasi icin)
- ANALIZ EDILECEK METIN icinde acik bir tarih/saat ifadesi (ör. "3 Mart
  2024", "dun gece", "saat 14:00'te") geciyorsa, `zaman_damgasi` alanini o
  ifadeye gore hesapla.
- Metinde HICBIR tarih/saat ifadesi YOKSA, olayin SU AN gerceklestigini
  varsay ve `zaman_damgasi` icin asagida verilen GERCEK SISTEM ZAMANINI
  ({current_time}) birebir kullan.
- Asagidaki FEW-SHOT ORNEK'teki "2024-03-03T09:00:00+03:00" degeri SADECE o
  ornege ait, o ornegin kendi metninde acikca gecen tarihten turetilmistir.
  Bu degeri ASLA baska bir metin icin KOPYALAMA.

# CIKTI FORMATI (kesinlikle bu yapida, disina hicbir aciklama/metin ekleme)
{{
  "facilities": [],
  "infrastructures": [],
  "energy_infrastructures": [],
  "communication_networks": [],
  "resource_hubs": [],
  "units": [],
  "events": [],
  "relationships": [
    {{"kaynak_isim": "...", "hedef_isim": "...", "tip": "AFFECTS", "agirlik": null}}
  ]
}}

# KURALLAR
1. Metinde acikca gecmeyen bir varlik tipi icin listeyi bos [] birak.
2. enlem/boylam metinde verilmemisse, yukaridaki "KOORDINAT KALIBRASYONU"
   bolumune uygun en iyi tahmini kullan; alani asla bos birakma.
3. `relationships` listesinde varliklari "isim" alanlariyla birebir ayni
   sekilde `kaynak_isim` / `hedef_isim` alanlarinda esletir.
4. `tip` alani sadece yukaridaki ILISKI TIPLERI listesinden biri olabilir.
5. Sadece gecerli JSON dondur; markdown kod blogu (```), yorum veya ek
   aciklama metni EKLEME.
6. infrastructures (Karayolu/Demiryolu/Kopru/Tunel) BIR GUZERGAHTIR, tek bir
   nokta DEGIL: `enlem`/`boylam` baslangic noktasini, `bitis_enlem`/
   `bitis_boylam` ise bitis noktasini temsil eder. Bu iki nokta MUTLAKA
   doldurulmali ve birbirinden FARKLI olmalidir (harita bunu bir cizgi
   olarak cizecektir); metinde iki uc nokta acikca gecmiyorsa bile
   "KOORDINAT KALIBRASYONU" kuralina uygun, birbirinden farkli iki makul
   nokta tahmin et.
7. EN ONEMLI KURAL: JSON anahtarlari (alan adlari) KESINLIKLE yukarida
   verilen TURKCE adlarla BIREBIR AYNI olmalidir. Anahtarlari ASLA
   Ingilizce'ye veya baska bir dile CEVIRME. Ornegin:
     "name"        DEGIL, "isim"             KULLAN
     "latitude"    DEGIL, "enlem"            KULLAN
     "longitude"   DEGIL, "boylam"           KULLAN
     "capacity"    DEGIL, "kapasite"         KULLAN
     "status"      DEGIL, "durum"/"mevcut_durum" KULLAN
   Asagidaki FEW-SHOT ORNEK, bu anahtar formatini birebir gostermektedir;
   alan adlarini oradaki gibi degistirmeden kopyala.
8. `mevcut_durum` icin yukaridaki "STATU KALIBRASYONU" bolumundeki KESIN
   esleme tablosunu uygula; kendi yorumunu katma.
9. `zaman_damgasi` icin yukaridaki "ZAMAN KURALI" bolumunu uygula; metinde
   tarih yoksa {current_time} degerini kullan, few-shot ornekteki tarihi
   ASLA kopyalama.
10. EN ONEMLI KURAL (KISA METIN): Yukaridaki "KISA/EKSIK METIN KURALI"
    bolumunu KESINLIKLE uygula. Metin ne kadar kisa/az detayli olursa olsun,
    acikca bir olay/varliktan bahsediyorsa TUM listeleri BOS DONDURME;
    bilinmeyen SAYISAL alanlari `null` birak ama kaydin KENDISINI reddetme.
11. EN ONEMLI KURAL (SEHIR ICI): Metinde Cadde/Sokak/Bulvar/Yol geciyorsa
    yukaridaki "SEHIR ICI (URBAN) CADDE/SOKAK KURALI" bolumunu KESINLIKLE
    uygula: bu varlik `facilities` DEGIL `infrastructures` listesine gider,
    `infrastructure_type="Sokak"`, birden fazla isim varsa AYRI kayitlar,
    `isim` alaninda SADECE cadde/sokak adi.
12. EN ONEMLI KURAL (OLAY TURU): `event_type` secerken yukaridaki "OLAY TURU
    (EVENT_TYPE) SINIFLANDIRMA KURALI"nu KESINLIKLE uygula — kasit/dusman
    unsuru YOKSA (ör. dogal gaz patlamasi) "Savas" DEGIL "Patlama" sec.
13. EN ONEMLI KURAL (ILISKI): Yukaridaki "ILISKI ZORUNLULUGU KURALI"nu
    KESINLIKLE uygula — bir olay ile birlikte baska bir varlik geciyorsa
    `relationships` ASLA BOS BIRAKILMAZ; olaydan HER etkilenen varliga
    (AFFECTS/LOCATED_IN) ayri ayri iliski ekle.
14. EN ONEMLI KURAL (KOPRU/TUNEL/VIYADUK/KAVSAK): Metinde Kopru/Koprusu/
    Viyaduk/Tunel/Kavsak geciyorsa (yazim hatali varyasyonlar dahil)
    yukaridaki "YAPI (KOPRU/TUNEL/VIYADUK/KAVSAK) KURALI" bolumunu
    KESINLIKLE uygula: bu varlik `facilities` DEGIL `infrastructures`
    listesine gider, `infrastructure_type` yapinin turune gore "Kopru"/
    "Tunel"/"Viyaduk" (kavsak icin "Sokak") olarak secilir; ASLA
    `FacilityType`e uydurulmaya CALISILMAZ.
15. EN ONEMLI KURAL (CIKTI SAFLIGI + YAZIM HATASI): Yukaridaki "CIKTI
    SAFLIGI KURALI" ve "YAZIM HATASI TOLERANSI KURALI"nu KESINLIKLE uygula:
    cevabin ilk karakteri `{{`, son karakteri `}}` olmali, ASLA markdown
    (```) veya aciklama metni EKLEME; metindeki yazim hatalarini (ör.
    "kcprüsü") BAGLAMDAN duzeltip TEMIZ ismi `isim` alanina yaz, yazim
    hatasini bir varligi ATLAMA/REDDETME GEREKCESI OLARAK KULLANMA.
16. DIKKAT — EN ONEMLI KURAL (AFET/KRIZ TURU ASLA TESIS/ALTYAPI DEGILDIR):
    Deprem, Heyelan, Patlama, Sel, Yangin, Orman Yangini, Cig, Savas,
    Teror, Siber Saldiri, Tahliye, Salgin Hastalik, Izdiham, Gemi Kazasi,
    Kimyasal Sizinti, Ucak Kazasi, Tren Kazasi, Trafik Kazasi, Baraj
    Cokmesi, Maden Kazasi, Firtina, Tsunami, Radyasyon Sizintisi, Volkanik
    Patlama, Bina Cokmesi, Toplu Zehirlenme, Kuraklik gibi bir afet/kriz
    TURUNUN KENDISI (yukaridaki "OLAY TURU (EVENT_TYPE) SINIFLANDIRMA
    KURALI"ndaki 27 deger) KESINLIKLE `events` LISTESINE gider — bunu ASLA `facilities`
    (Tesis) veya `infrastructures` (Altyapi) listesine YERLESTIRME. Tesis
    SADECE fiziksel bir BINADIR (Hastane/Havalimani/Askeri Us/Liman/
    Siginak); bir "Heyelan"in/"Sel"in KENDISI bir bina DEGILDIR. Bu
    KARISTIRMA Pydantic dogrulamasinin KESIN olarak BASARISIZ OLMASINA ve
    kaydin TAMAMEN KAYBOLMASINA (sistem krizi hic GORMEZ) neden olur.

# FEW-SHOT ORNEK 1 (SADECE JSON ANAHTAR/ALAN ADI FORMATINI GOSTERMEK ICINDIR)

Ornek metin:
\"\"\"""" + _FEW_SHOT_ORNEK_METIN + """\"\"\"

Bu ornek metin icin uretilmesi gereken dogru JSON cikti:
""" + _build_few_shot_json_block() + """

# FEW-SHOT ORNEK 2 (Heyelan/dogal afet turu icin AYRI bir ornek — "TUM
# listeler BOS donme" hatasini onlemek icin, bkz. modul-ustu "IKINCI
# FEW-SHOT ORNEGI" notu)

Ornek metin 2:
\"\"\"""" + _FEW_SHOT_ORNEK_METIN_2 + """\"\"\"

Bu ornek metin 2 icin uretilmesi gereken dogru JSON cikti:
""" + _build_few_shot_json_block(_FEW_SHOT_ORNEK_CIKTI_2) + """

# FEW-SHOT ORNEK 3 (Heyelan turunun IKINCI bir bolge/ifade varyasyonu —
# bkz. modul-ustu "UCUNCU FEW-SHOT ORNEGI" notu)

Ornek metin 3:
\"\"\"""" + _FEW_SHOT_ORNEK_METIN_3 + """\"\"\"

Bu ornek metin 3 icin uretilmesi gereken dogru JSON cikti:
""" + _build_few_shot_json_block(_FEW_SHOT_ORNEK_CIKTI_3) + """

Simdi asil gorevine don: asagidaki YENI metni, yukaridaki ornekteki
BIREBIR AYNI Turkce JSON anahtar formatini kullanarak analiz et. Metin
hangi afet/olay turunden bahsederse bahsetsin (yukaridaki orneklerle
SINIRLI DEGILDIR), ayni format ve titizlikle TUM ilgili varlik/iliskileri
cikar — HICBIR ZAMAN listeleri bos birakma (metin GERCEKTEN bir kriz/olay
anlatiyorsa). Yukaridaki "STATU KALIBRASYONU" ve "ZAMAN KURALI"
bolumlerini KESINLIKLE uygula.

GERCEK SISTEM ZAMANI (metinde tarih yoksa `zaman_damgasi` icin bunu kullan):
{current_time}

# ANALIZ EDILECEK METIN
\"\"\"{text}\"\"\"

DIKKAT: YANITIN SADECE VE SADECE GECERLI BIR JSON FORMATINDA OLMALIDIR.
JSON DISINDA HICBIR ACIKLAMA, GIRIS VEYA NOT YAZMA. '```json' GIBI
MARKDOWN ISARETLERI KULLANMA.

JSON:"""



class OllamaParser:
   
    def __init__(
        self,
        model: str = "llama3",
        base_url: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        self._base_url: str = base_url or os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        self._model_name: str = model
        self._temperature: float = temperature

        
        self.llm = ChatOllama(
            model=self._model_name,
            base_url=self._base_url,
            temperature=self._temperature,
            format="json",
            
            num_gpu=_OLLAMA_NUM_GPU,
            num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
            num_ctx=8192,
        )

        self.prompt_template: PromptTemplate = PromptTemplate(
            input_variables=["text", "current_time"],
            template=EXTRACTION_PROMPT_TEMPLATE,
        )

       
        self._chain = self.prompt_template | self.llm | StrOutputParser()

    
    def extract_entities(self, text: str) -> Dict[str, Any]:
      
        if not text or not text.strip():
            raise ValueError("extract_entities: bos metin verilemez.")

       
        duzeltilmis_text = _metindeki_kritik_terimleri_duzelt(text)
        if duzeltilmis_text != text:
            logger.info("Girdi metni on-duzeltmeden GECTI: %r -> %r", text, duzeltilmis_text)

        
        raw_response = self._invoke_llm(duzeltilmis_text, current_time=_current_time_iso())

        logger.debug("RAW LLM OUTPUT: %s", raw_response)

        raw_json = self._parse_json(raw_response)

 
        logger.debug("PARSED JSON (sanitize + json.loads sonrasi): %s", raw_json)


        raw_json = reroute_misplaced_street_facilities(raw_json)
   
        raw_json = reroute_misplaced_infrastructure_facilities(raw_json)
 
        raw_json = reroute_misplaced_energy_resources(raw_json)

        result: Dict[str, Any] = {key: [] for key in ENTITY_MODEL_MAP}
        result["relationships"] = []
        result["raw_response"] = raw_response
        validation_hatalari: List[str] = []
        result["validation_hatalari"] = validation_hatalari


        known_isimler: set[str] = set()
        for key, model_cls in ENTITY_MODEL_MAP.items():
            for item in raw_json.get(key) or []:
                node = self._coerce_node(model_cls, item, validation_hatalari)
                if node is None:
                    continue
                result[key].append(node)
                known_isimler.add(node.isim)

 
        for rel in raw_json.get("relationships") or []:
            relationship = self._coerce_relationship(rel, known_isimler, validation_hatalari)
            if relationship is not None:
                result["relationships"].append(relationship)


        varlik_uretildi_mi = any(result[key] for key in ENTITY_MODEL_MAP)
        if not varlik_uretildi_mi:

            _ONIZLEME_UZUNLUGU = 600
            ham_onizleme = raw_response.strip()
            if len(ham_onizleme) > _ONIZLEME_UZUNLUGU:
                ham_onizleme = ham_onizleme[:_ONIZLEME_UZUNLUGU] + "… (kırpıldı)"
            yedek_olay = _yedek_kriz_olayi_uret(duzeltilmis_text)
            if yedek_olay is not None:
                logger.warning(
                    "KIRMIZI EKRAN YASAĞI devreye girdi: LLM metinden HİÇBİR varlık "
                    "çıkaramadı, ama metinde tanıdık bir kriz-türü ifadesi bulundu; "
                    "yedek/minimal bir Event üretildi: '%s' (tür=%s). Bu, LLM'in TAM "
                    "çıkarımının YERİNİ TUTMAZ — sadece 'hiçbir şey işlenmedi' hatasını "
                    "önler. Ham LLM yanıtı: %s",
                    yedek_olay.isim, yedek_olay.event_type.value, raw_response,
                )
                result["events"].append(yedek_olay)
                validation_hatalari.append(
                    f"⚠️ LLM metinden hiçbir varlık çıkaramadı; sistem SON ÇARE olarak "
                    f"kaba bir anahtar-kelime taramasıyla minimal bir olay kaydı "
                    f"('{yedek_olay.isim}') oluşturdu — bu kayıt LLM'in tam bir "
                    f"analizinin YERİNİ TUTMAZ, sadece raporu tamamen kaybolmaktan "
                    f"kurtarır. Konumu/detayları daha net bir metinle tekrar "
                    f"göndermeniz önerilir.\n\n"
                    f"**Ham LLM yanıtı (teşhis için):**\n```\n{ham_onizleme}\n```"
                )
            else:
     
                validation_hatalari.append(
                    f"⚠️ LLM metinden hiçbir varlık çıkaramadı ve anahtar-kelime "
                    f"taramasıyla bile yedek bir olay üretilemedi.\n\n"
                    f"**Ham LLM yanıtı (teşhis için):**\n```\n{ham_onizleme}\n```"
                )

        return result



    def _invoke_llm(self, text: str, current_time: Optional[str] = None) -> str:
       
        _ozet = text.strip().replace("\n", " ")[:60]
        logger.info("Neo4j/on-hazirlik bitti, LLM'e gonderiliyor: '%s...'", _ozet)
        _baslangic = time.monotonic()
        try:
            cevap = self._chain.invoke(
                {"text": text, "current_time": current_time or _current_time_iso()}
            )
        except Exception as exc:  
            logger.error(
                "LLM cagrisi BASARISIZ oldu (%.1f sn sonra) '%s...': %s",
                time.monotonic() - _baslangic, _ozet, exc,
            )
            raise RuntimeError(
                f"Ollama'ya baglanilamadi (base_url={self._base_url}, "
                f"model={self._model_name}). Ollama servisinin calistigindan ve "
                f"modelin 'ollama pull {self._model_name}' ile indirildiginden emin olun."
            ) from exc
        logger.info(
            "LLM cevabi alindi (%.1f sn) '%s...'", time.monotonic() - _baslangic, _ozet,
        )
        return cevap

    @staticmethod
    def _parse_json(raw_response: str) -> Dict[str, Any]:
        
        cleaned = raw_response.strip()

        
        cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

       
        ilk_suslu = cleaned.find("{")
        son_suslu = cleaned.rfind("}")
        if ilk_suslu != -1 and son_suslu != -1 and son_suslu > ilk_suslu:
            cleaned = cleaned[ilk_suslu : son_suslu + 1].strip()

        
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)

        if not cleaned:
            raise ValueError(f"LLM cevabinda JSON bulunamadi.\nHam cevap: {raw_response}")

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM cevabi gecerli JSON degil (agresif temizlik SONRASI bile): {exc}\n"
                f"Temizlenmis metin: {cleaned}\nHam cevap: {raw_response}"
            ) from exc

    @staticmethod
    def _coerce_node(
        model_cls: Type[BaseNode], item: Dict[str, Any], hata_listesi: Optional[List[str]] = None
    ) -> Optional[BaseNode]:
        
        doldurulmus = apply_required_field_fallbacks(model_cls, item)
        if model_cls is Event:
            doldurulmus = _gecersiz_event_type_ise_varsayilana_cek(doldurulmus)
        if model_cls is Unit:
            doldurulmus = _gecersiz_unit_type_ise_es_anlamlisina_cek(doldurulmus)
        if model_cls is CommunicationNetwork:
            doldurulmus = _gecersiz_network_type_ise_es_anlamlisina_cek(doldurulmus)
        if model_cls is ResourceHub:
            doldurulmus = _gecersiz_resource_type_ise_es_anlamlisina_cek(doldurulmus)
        doldurulmus = _gecersiz_durum_ise_varsayilana_cek(model_cls, doldurulmus)
        sanitized = sanitize_enum_fields(model_cls, doldurulmus)
        try:
            return model_cls(**sanitized)
        except ValidationError as exc:
            isim = item.get("isim", "?")
           
            if model_cls is Facility and any(
                isinstance(err.get("ctx", {}).get("error"), GecersizFacilityTuruAtlandi)
                for err in exc.errors()
            ):
                logger.info(
                    "Facility kaydi '%s' sessizce atlandi (gecersiz facility_type — muhtemelen "
                    "bir Event/afet turuyla karistirildi): %s",
                    isim, exc,
                )
                return None
           
            if model_cls is Infrastructure and any(
                isinstance(err.get("ctx", {}).get("error"), GecersizInfrastructureTuruAtlandi)
                for err in exc.errors()
            ):
                logger.info(
                    "Infrastructure kaydi '%s' sessizce atlandi (gecersiz infrastructure_type — "
                    "muhtemelen bir Event/afet turuyla veya baska bir kategoriyle karistirildi): %s",
                    isim, exc,
                )
                return None
            logger.warning(
                "'%s' turunde varlik dogrulanamadi, atlaniyor | hata=%s | veri=%s",
                model_cls.__name__,
                exc,
                item,
            )
            if hata_listesi is not None:
                hata_listesi.append(
                    f"'{isim}' ({model_cls.__name__}) doğrulanamadı ve atlandı: {exc}"
                )
            return None

    @staticmethod
    def _coerce_relationship(
        rel: Dict[str, Any], known_isimler: "set[str]", hata_listesi: Optional[List[str]] = None
    ) -> Optional[Relationship]:
        
        try:
            kaynak_isim = rel["kaynak_isim"]
            hedef_isim = rel["hedef_isim"]
            if kaynak_isim not in known_isimler or hedef_isim not in known_isimler:
                logger.warning(
                    "Iliski bilinmeyen bir varlik ismine referans veriyor "
                    "(kaynak=%r bilinen=%s, hedef=%r bilinen=%s) | veri=%s",
                    kaynak_isim, kaynak_isim in known_isimler,
                    hedef_isim, hedef_isim in known_isimler,
                    rel,
                )
            tip_degeri = normalize_enum_value(RelationshipType, rel["tip"])
            return Relationship(
                kaynak_id=kaynak_isim,
                hedef_id=hedef_isim,
                tip=RelationshipType(tip_degeri),
                agirlik=rel.get("agirlik"),
            )
        except (KeyError, ValueError, ValidationError) as exc:
            logger.warning("Iliski dogrulanamadi, atlaniyor | hata=%s | veri=%s", exc, rel)
            if hata_listesi is not None:
                hata_listesi.append(
                    f"İlişki doğrulanamadı ve atlandı ({rel.get('kaynak_isim', '?')} -> "
                    f"{rel.get('hedef_isim', '?')}): {exc}"
                )
            return None


if __name__ == "__main__":  
    logging.basicConfig(level=logging.INFO)

    ORNEK_METIN = (
        "Elazig merkezde deprem oldu, devlet hastanesi hasar gordu "
        "ve iletisim koptu."
    )

    parser = OllamaParser(model=os.getenv("OLLAMA_EXTRACTION_MODEL", "llama3"))
    cikti = parser.extract_entities(ORNEK_METIN)

    for anahtar, deger in cikti.items():
        if anahtar == "raw_response":
            continue
        print(f"{anahtar}: {deger}")
