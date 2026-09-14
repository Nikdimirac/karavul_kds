

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from langchain_community.chat_models import ChatOllama
except ImportError:  
    from langchain_community.llms import Ollama as ChatOllama  

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.core import turkiye_harita
from src.core.database import Neo4jConnection
from src.core.environmental_context import CevreselDurum, cevresel_durumu_getir
from src.data_ingestion.real_osm_loader import OSM_TEKNIK_KIMLIK_IMZASI

logger = logging.getLogger(__name__)


_OLLAMA_NUM_GPU = int(os.getenv("OLLAMA_NUM_GPU", "999"))


_OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", str(min(16, os.cpu_count() or 8))))


_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE = float(os.getenv("OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE", "400"))


_AKTIF_BIRLIK_LISTELEME_LIMITI = 500

HASARLI_TESIS_DURUMLARI = ["Hasarlı", "Yok Edildi"]


KRITIK_OLAY_SIDDETLERI = ["Yuksek", "Kritik", "Katastrofik"]

VARSAYILAN_ALTERNATIF_SAYISI = 3

_KRIZ_ANLATIM_ILGILILIK_YARICAP_KM = 150.0



_ANA_KARAYOLU_HIGHWAY_TIPLERI = frozenset({"motorway", "trunk", "primary"})


def _yol_tip_etiketi(tip: Optional[str], highway_tipi: Optional[str]) -> Optional[str]:
   
    if tip != "Sokak" or not highway_tipi:
        return tip
    if highway_tipi in _ANA_KARAYOLU_HIGHWAY_TIPLERI:
        return "Ana Karayolu"
    if highway_tipi in ("secondary", "tertiary"):
        return "Cadde"
    return tip


def _geodesic_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
 
    R_KM = 6371.0088
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R_KM * math.asin(min(1.0, math.sqrt(a)))

_ARAMA_YARICAPI_KM = turkiye_harita.yaricapi_operasyonel_sinira_kisitla(50.0)

_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI: List[str] = ["Agir Muhendislik", "AFAD", "Arama Kurtarma"]


_OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI: Dict[str, List[str]] = {
  
    "Savas": ["Askeri Birlik"],
    "Siber Saldiri": ["Askeri Birlik"],
   
    "Deprem": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma", "Agir Muhendislik", "Askeri Birlik"],
    "Sel": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma"],
    "Yangin": ["Itfaiye", "Saglik"],
    "Patlama": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma"],
   
    "Orman Yangini": ["Itfaiye", "Saglik"],
    "Cig": ["AFAD", "Arama Kurtarma", "Saglik", "Askeri Birlik"],
    "Heyelan": ["Agir Muhendislik", "AFAD", "Arama Kurtarma"],
    "Teror": ["Askeri Birlik", "Polis"],
    "Tahliye": ["AFAD", "Saglik", "Polis", "Lojistik"],
    "Salgin Hastalik": ["Saglik", "AFAD", "Lojistik"],
    "Izdiham": ["Saglik", "Polis", "AFAD"],
   
    "Gemi Kazasi": ["Sahil Guvenlik", "Arama Kurtarma", "Saglik", "AFAD"],
    "Kimyasal Sizinti": ["Itfaiye", "Saglik", "AFAD"],
    "Ucak Kazasi": ["Saglik", "AFAD", "Arama Kurtarma", "Itfaiye"],
    "Tren Kazasi": ["Saglik", "AFAD", "Arama Kurtarma", "Itfaiye"],
    "Trafik Kazasi": ["Saglik", "Polis", "Itfaiye"],
    "Baraj Cokmesi": ["AFAD", "Arama Kurtarma", "Agir Muhendislik", "Askeri Birlik"],
    "Maden Kazasi": ["Arama Kurtarma", "Saglik", "Agir Muhendislik", "AFAD"],
    "Firtina": ["AFAD", "Itfaiye", "Agir Muhendislik"],
    "Tsunami": ["Sahil Guvenlik", "AFAD", "Arama Kurtarma", "Saglik", "Askeri Birlik"],
    "Radyasyon Sizintisi": ["Askeri Birlik", "AFAD", "Saglik"],
    "Volkanik Patlama": ["AFAD", "Askeri Birlik", "Saglik", "Arama Kurtarma"],
    "Bina Cokmesi": ["Arama Kurtarma", "AFAD", "Saglik", "Agir Muhendislik"],
    "Toplu Zehirlenme": ["Saglik", "AFAD"],
    "Kuraklik": ["AFAD", "Lojistik"],
}


_VARSAYILAN_ONCELIKLI_BIRIM_TIPLERI: List[str] = ["AFAD", "Arama Kurtarma", "Saglik", "Itfaiye"]


_GUVENLIK_BIRIM_TIPLERI: List[str] = ["Polis", "Askeri Birlik"]

_YETENEK_ARAMA_YARICAPI_KM = turkiye_harita.yaricapi_operasyonel_sinira_kisitla(50.0)


_ADAY_COKLUGU_KATSAYISI = 15


def _deger(kayit: Dict[str, Any], anahtar: str, varsayilan: str = "bilinmiyor") -> Any:

    deger = kayit.get(anahtar)
    return deger if deger is not None else varsayilan


def _temiz_isim(kayit: Dict[str, Any]) -> str:
 
    isim = str(_deger(kayit, "isim", "")).strip()
    imza_konumu = isim.find(OSM_TEKNIK_KIMLIK_IMZASI)
    if imza_konumu != -1:
        isim = isim[:imza_konumu].strip()
    return isim or "İsimsiz Saha Birimi"


def _gercek_yetenek_aciklamasi(kayit: Dict[str, Any]) -> str:
   
    aciklama = _deger(kayit, "aciklama", None)
 
    if isinstance(aciklama, str) and aciklama.strip().lower().startswith("yetenek"):
        return aciklama.strip()
    return "belirtilmemiş"


_INGILIZCE_SUPHELI_KELIMELER = frozenset(
    {
        "the", "is", "are", "and", "of", "to", "for", "this", "that", "with",
        "recommendation", "recommendations", "route", "unit", "units", "given",
        "logistical", "here", "critical", "decision", "decisions", "tactical",
        "should", "would", "can", "will", "location", "crisis",
      
        "deployment",
    }
)


def _tek_parca_ingilizce_supheli_mi(parca: str, esik: float, min_kelime: int) -> bool:
  
    kelimeler = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", parca.lower())
    if len(kelimeler) < min_kelime:
        return False
    supheli = sum(1 for k in kelimeler if k in _INGILIZCE_SUPHELI_KELIMELER)
    return (supheli / len(kelimeler)) > esik


def _ingilizce_supheli_mi(metin: str) -> bool:
    
    if _tek_parca_ingilizce_supheli_mi(metin, esik=0.08, min_kelime=10):
        return True
    for satir in metin.splitlines():
        if _tek_parca_ingilizce_supheli_mi(satir, esik=0.15, min_kelime=5):
            return True
    return False



_YABANCI_SCRIPT_ARALIKLARI: Tuple[Tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   
    (0x3400, 0x4DBF), 
    (0x3040, 0x30FF),   
    (0xAC00, 0xD7AF),  
    (0x0600, 0x06FF),   
    (0x0400, 0x04FF),   
    (0x0900, 0x097F),  
    (0x0E00, 0x0E7F),   
)


def _yabanci_script_icerir_mi(metin: str) -> bool:
 
    return any(
        any(alt <= ord(karakter) <= ust for alt, ust in _YABANCI_SCRIPT_ARALIKLARI)
        for karakter in metin
    )


def _dil_kilidi_ihlali_mi(metin: str) -> bool:
 
    return _ingilizce_supheli_mi(metin) or _yabanci_script_icerir_mi(metin) or _sonda_yabanci_kelime_mi(metin)


def _format_temizle(metin: str) -> str:
   
    temizlenmis = re.sub(r"```(?:\w+)?", "", metin)
    temizlenmis = temizlenmis.replace("**", "")
    temizlenmis = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", temizlenmis)

  
    temizlenmis = re.sub(
        rf"(?im)^[ \t]*(?:{re.escape(_MUDAHALE_BIRLIK_ETIKETI)}|KRİZ NOKTASI)\s*[:：].*$\n?",
        "", temizlenmis,
    )
    temizlenmis = re.sub(r"\n{3,}", "\n\n", temizlenmis)

    
    temizlenmis = re.sub(r"([a-zçğıöşü])([A-ZÇĞİÖŞÜ]{2,})", r"\1 \2", temizlenmis)

  
    ilk_madde = re.search(r"(?m)^\s*1[\.\)][ \t]", temizlenmis)
    if ilk_madde and ilk_madde.start() > 0:
        temizlenmis = temizlenmis[ilk_madde.start():]

    
    ilk_satir_sonu = temizlenmis.find("\n")
    if ilk_satir_sonu != -1:
        ilk_satir = temizlenmis[:ilk_satir_sonu].strip()
        sonraki_ham = temizlenmis[ilk_satir_sonu:]
        kalan = sonraki_ham.lstrip("\n")
        bos_satir_sayisi = len(sonraki_ham) - len(kalan)
        if (
            ilk_satir
            and len(ilk_satir) < 40
            and not ilk_satir.endswith((".", "!", "?", ":"))
            and bos_satir_sayisi >= 2
            and kalan
        ):
            temizlenmis = kalan

    return temizlenmis.strip()


def _isim_bazinda_tekillestir(
    kayitlar: List[Dict[str, Any]], adet: Optional[int] = None
) -> List[Dict[str, Any]]:
   
    gorulmus: set = set()
    sonuc: List[Dict[str, Any]] = []
    for kayit in kayitlar:
        isim = kayit.get("isim")
        if isim in gorulmus:
            continue
        gorulmus.add(isim)
        sonuc.append(kayit)
        if adet is not None and len(sonuc) >= adet:
            break
    return sonuc





def _il_geofencing_kosulu(alias: str, konum_kaydi: Dict[str, Any]) -> Tuple[str, List[str]]:
    
    olay_ili = turkiye_harita.en_yakin_il(konum_kaydi.get("enlem"), konum_kaydi.get("boylam"))
    izinli_iller = sorted(turkiye_harita.izin_verilen_iller(olay_ili))
    if not izinli_iller:
        return "", []
    return f"({alias}.bolge IS NULL OR {alias}.bolge IN $izinli_iller)", izinli_iller




@dataclass
class SituationalPicture:

    hasarli_tesisler: List[Dict[str, Any]] = field(default_factory=list)
    kapali_yollar: List[Dict[str, Any]] = field(default_factory=list)
    aktif_birlikler: List[Dict[str, Any]] = field(default_factory=list)
    kritik_olaylar: List[Dict[str, Any]] = field(default_factory=list)
    alternatif_rotalar: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    en_yakin_saglam_tesisler: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    
    en_yakin_ulasilan_birlikler: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
   
    en_yakin_guvenlik_birlikleri: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.hasarli_tesisler or self.kapali_yollar or self.kritik_olaylar)


def _durum_gercek_isimlerini_topla(durum: "SituationalPicture") -> List[str]:
   
    isimler: List[str] = []
    for kayit in (
        durum.hasarli_tesisler + durum.kapali_yollar
        + durum.kritik_olaylar + durum.aktif_birlikler
    ):
        isim = kayit.get("isim")
        if isim:
            isimler.append(str(isim))
    for esleme in (
        durum.en_yakin_ulasilan_birlikler, durum.en_yakin_guvenlik_birlikleri,
        durum.en_yakin_saglam_tesisler, durum.alternatif_rotalar,
    ):
        for adaylar in esleme.values():
            for aday in adaylar:
                isim = aday.get("isim")
                if isim:
                    isimler.append(str(isim))
    return isimler


def _turkce_kucuk_harf(metin: str) -> str:
   
    return metin.replace("İ", "i").replace("I", "ı").lower()


def _veri_referanssiz_mi(metin: str, durum: "SituationalPicture") -> bool:
    
    isimler = _durum_gercek_isimlerini_topla(durum)
    if not isimler:
        return False
    metin_kucuk = _turkce_kucuk_harf(metin)
    for isim in isimler:
        temiz = isim.split("(")[0].strip()
        if len(temiz) >= 4 and _turkce_kucuk_harf(temiz) in metin_kucuk:
            return False
    return True


_BULUNAMADI_ISARETI = "BULUNAMADI"  
_BULUNAMADI_YANIT_KELIMESI = "bulunamadı"

_ABARTILI_YETENEK_IFADELERI = (
    "birincil",
    "en uygun",
    "tam uyumlu",
    "ideal",
    "en iyi seçim",
    "en iyi seçenek",
    "birincil kaynak",
    "uygun yeteneklere sahip",
    "uygun yeteneğe sahip",
    "arama-kurtarma yeteneğine sahip",
    "arama-kurtarma yeteneklerine sahip",
    "arama kurtarma yeteneğine sahip",
    "arama kurtarma yeteneklerine sahip",
)


_ABARTILI_KONTROLDEN_MUAF_BOLUMLER = frozenset({"Tahliye"})


_MUDAHALE_BIRLIK_ETIKETI = "MÜDAHALE EDECEK BİRLİK"


_MUDAHALE_BIRLIK_ADI_PATTERN = re.compile(
    re.escape(_MUDAHALE_BIRLIK_ETIKETI) + r"\s*[:：]\s*([^\n.]+)"
)



def _kesin_birlik_ismi_uydurmasi_bul(metin: str, durum_ozeti: str) -> Optional[str]:
   
    durum_ozeti_kucuk = _turkce_kucuk_harf(durum_ozeti)
    for eslesme in _MUDAHALE_BIRLIK_ADI_PATTERN.finditer(metin):
        iddia_edilen_isim = eslesme.group(1).strip().strip("*").strip()
        if not iddia_edilen_isim:
            continue
        if _BULUNAMADI_YANIT_KELIMESI in _turkce_kucuk_harf(iddia_edilen_isim):
            continue  
        isim_govdesi = iddia_edilen_isim.split("(")[0].strip()
        if len(isim_govdesi) >= 4 and _turkce_kucuk_harf(isim_govdesi) not in durum_ozeti_kucuk:
            return isim_govdesi
    return None


def _birlik_uydurma_suphesi_mi(metin: str, durum_ozeti: str, bolum_adi: str = "") -> bool:
   
    if _BULUNAMADI_ISARETI not in durum_ozeti:
        return False

    _uydurma_isim = _kesin_birlik_ismi_uydurmasi_bul(metin, durum_ozeti)
    if _uydurma_isim is not None:
        logger.warning(
            "'%s' mikro-gorevi ISIM UYDURMA supheli: '%s' iddia edildi ama "
            "bu isim durum_ozeti'nde (GERCEK GraphRAG baglami) HIC gecmiyor.",
            bolum_adi, _uydurma_isim,
        )
        return True

    metin_kucuk = _turkce_kucuk_harf(metin)
    if _BULUNAMADI_YANIT_KELIMESI not in metin_kucuk:
        return True
    if bolum_adi in _ABARTILI_KONTROLDEN_MUAF_BOLUMLER:
        return False
    return any(ifade in metin_kucuk for ifade in _ABARTILI_YETENEK_IFADELERI)


_SABIT_BULUNAMADI_CUMLESI = (
    "Bilgi Grafında bu krize doğru yeteneğe sahip ve açık yol ağı üzerinden "
    "ulaşabilen bir birlik BULUNAMADI — bu net bir erişim sorunudur; çevre "
    "illerden/komşu illerden destek talebi değerlendirilmelidir."
)



def _kategori_icin_gercek_birlik_var_mi(durum: "SituationalPicture", guvenlik_de_sayilir: bool) -> bool:
  
    if not (durum.kritik_olaylar or durum.kapali_yollar):
        return True
    if any(durum.en_yakin_ulasilan_birlikler.values()):
        return True
    return guvenlik_de_sayilir and any(durum.en_yakin_guvenlik_birlikleri.values())


def _yanlis_bulunamadi_iddiasi_mi(metin: str, durum_ozeti: str) -> bool:
 
    if durum_ozeti and _BULUNAMADI_ISARETI in durum_ozeti:
        return False  
    metin_kucuk = _turkce_kucuk_harf(metin)
    return _BULUNAMADI_YANIT_KELIMESI in metin_kucuk and "birlik" in metin_kucuk


de
    kaynaklar = [durum.en_yakin_ulasilan_birlikler]
    if guvenlik_de_sayilir:
        kaynaklar.append(durum.en_yakin_guvenlik_birlikleri)
    for kaynak in kaynaklar:
        for birlikler in kaynak.values():
            if birlikler:
                b = birlikler[0]
                return f"{_temiz_isim(b)} ile {b.get('mesafe_km', '?')} km mesafeden müdahale edilecektir."
    return _SABIT_BULUNAMADI_CUMLESI .


_SAYI_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def _sayisal_uydurma_suphesi_mi(metin: str, referans_metin: str) -> bool:

    referans_sayilari = set(_SAYI_PATTERN.findall(referans_metin))
    for sayi in _SAYI_PATTERN.findall(metin):
        if "." in sayi or "," in sayi or len(sayi) >= 2:
            if sayi not in referans_sayilari:
                return True
    return False


_SONDA_SUPHELI_INGILIZCE_KELIMELER = frozenset(
    {
        "light", "heavy", "fast", "safe", "ready", "standby", "backup", "support",
        "team", "unit", "alert", "priority", "critical", "active", "response",
        "urgent", "primary", "secondary", "immediate", "available",
    }
)



def _sonda_yabanci_kelime_mi(metin: str) -> bool:
   
    kelimeler = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", metin)
    if not kelimeler:
        return False
    return kelimeler[-1].lower() in _SONDA_SUPHELI_INGILIZCE_KELIMELER


_MADDE_BASLANGIC = r"(?:\d+[\.\)](?=[ \t]|$)|[-•])"

_MADDE_PATTERN = re.compile(
    rf"^[ \t]*{_MADDE_BASLANGIC}[ \t]*(.+?)(?=\n[ \t]*{_MADDE_BASLANGIC}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_oneri_maddeleri(metin: str) -> List[str]:
   
    if not metin or not metin.strip():
        return []

    temiz_metin = metin.strip()
    maddeler = [
        eslesme.group(1).strip()
        for eslesme in _MADDE_PATTERN.finditer(temiz_metin)
        if eslesme.group(1).strip()
    ]
    return maddeler if maddeler else [temiz_metin]



_LOJISTIK_MIKRO_PROMPT_TEMPLATE = """Aşağıdaki kriz/afet saha verisine göre, SADECE lojistik güzergah/malzeme-
ikmal durumunu anlatan, somut birlik ve güzergah isimleri içeren, kısa
(1-2 cümle) ve profesyonel bir Türkçe analiz yaz.

KURALLAR:
- SADECE sana verilen veriyi kullan (mesafe/km, personel sayısı, isimler);
  veride olmayan hiçbir sayı/birlik/güzergah UYDURMA, veride yoksa o
  ayrıntıdan hiç bahsetme.
- Birden fazla "MÜDAHALE EDECEK BİRLİK" varsa, GERÇEKTEN lojistik/nakliye/
  ağır-mühendislik yeteneğine sahip olanı seç (sırayla ilk olduğu için
  değil).
- "ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ (2. PLAN)" bölümündeki birimlerin
  lojistik/nakliye yeteneği YOKTUR — bunları birincil çözüm gibi SUNMA,
  sadece çevre güvenliği için anılabilirler; "birincil", "en uygun", "tam
  uyumlu", "ideal" gibi övücü sözler bu birimler için KULLANMA.
- SADECE "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" yazan bir olay için (ve o
  olay için 2. PLAN birimi de yoksa), var olmayan bir birlik icat etmek
  yerine tam olarak şunu yaz: "Uygun birlik bulunamadı, çevre illerden
  destek talep edilmelidir."
- SADECE Türkçe yaz, markdown/madde numarası/başlık kullanma — düz metin.

# SAHA VERİSİ
{durum_ozeti}

# ÇEVRESEL İSTİHBARAT
Hava durumu: "{cevresel_durum}". Yoğun yağış/sis/kar varsa lojistik hızını
ve tahliye riskini buna göre değerlendirip uygun yetenekte (paletli vb.)
birlik/rota seç.

Lojistik analiz (1-2 cümle, düz metin):"""

_TAHLIYE_MIKRO_PROMPT_TEMPLATE = """Aşağıdaki kriz/afet saha verisine göre, SADECE bölgedeki/riskteki
sivillerin hangi birlikle, nasıl tahliye edileceğini anlatan, somut birlik
isimleri içeren, kısa (1-2 cümle) ve profesyonel bir Türkçe analiz yaz.

KURALLAR:
- "MÜDAHALE EDECEK BİRLİK:" krize giden ÇÖZÜM aracıdır — bunu "KRİZ
  NOKTASI:" ile ASLA KARIŞTIRMA (birliği kurtarılacak hedef gibi gösterme).
- ÖNCELİK KURALI (SENİN İÇİN ÖZEL): "ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ"
  bölümü varsa, o birim(ler) TAHLİYE için TAM UYGUN ve birincil kaynaktır
  (bu bölümün diğer analizler için geçerli olan "birincil çözüm önerme"
  kısıtı SANA uygulanmaz). Böyle bir bölüm yoksa genel "MÜDAHALE EDECEK
  BİRLİK" listesini kullan.
- SADECE hem genel liste hem 2. PLAN bölümü "BULUNAMADI"/yoksa, var
  olmayan bir birlik icat etmek yerine tam olarak şunu yaz: "Uygun birlik
  bulunamadı, çevre illerden destek talep edilmelidir."
- SADECE sana verilen veriyi kullan (mesafe/km, personel sayısı, isimler);
  veride olmayan hiçbir sayı/birlik UYDURMA, veride yoksa o ayrıntıdan hiç
  bahsetme. SADECE Türkçe yaz, markdown/madde numarası/başlık kullanma.

# SAHA VERİSİ
{durum_ozeti}

# ÇEVRESEL İSTİHBARAT
Hava durumu: "{cevresel_durum}". Yoğun yağış/sis/kar varsa tahliye riskini
buna göre değerlendirip uygun yetenekte (paletli vb.) birlik seç.

Tahliye analizi (1-2 cümle, düz metin):"""

_SEVK_MIKRO_PROMPT_TEMPLATE = """Aşağıdaki kriz/afet saha verisine göre, SADECE hangi birliğin hangi ÖZEL
yeteneğiyle (vinç, dozer, arama-kurtarma, medikal vb.) krize sevk edilmesi
gerektiğini ve NEDEN uygun olduğunu anlatan, somut birlik isimleri içeren,
kısa (1-2 cümle) ve profesyonel bir Türkçe analiz yaz.

KURALLAR:
- SADECE sana verilen veriyi kullan; veride olmayan bir birlik/yetenek/
  sayı UYDURMA. Bir birliğin "Yetenekleri: belirtilmemiş" yazıyorsa, o
  birlik için özel bir yetenek İCAT ETME — sadece tipini ve mesafesini
  kullanarak nötr bir ifade kur.
- Birden fazla birlik/yetenek varsa, en BELİRGİN/ÖZEL yeteneğe ("Yetenekleri:"
  alanında GERÇEKTEN yazan, genel lojistikten ayrışan bir yetenek) sahip
  olanı öne çıkar.
- "ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ (2. PLAN)" bölümündeki birimlerin sevk
  için özel bir yeteneği YOKTUR — bunları birincil çözüm gibi SUNMA, sadece
  çevre güvenliği için anılabilirler; "birincil", "en uygun", "tam uyumlu",
  "ideal" gibi övücü sözler bu birimler için KULLANMA.
- SADECE "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" yazan bir olay için (ve o
  olay için 2. PLAN birimi de yoksa), var olmayan bir birlik icat etmek
  yerine tam olarak şunu yaz: "Uygun birlik bulunamadı, çevre illerden
  destek talep edilmelidir."
- SADECE Türkçe yaz, markdown/madde numarası/başlık kullanma — düz metin.

# SAHA VERİSİ
{durum_ozeti}

# ÇEVRESEL İSTİHBARAT
Hava durumu: "{cevresel_durum}". Yoğun yağış/sis/kar varsa uygun yetenekte
(paletli vb.) birlik seç.

Sevkiyat analizi (1-2 cümle, düz metin):"""


_CEVIRI_PROMPT_TEMPLATE = """Aşağıdaki metin bir kriz yönetimi taktiksel raporudur ve YANLIŞLIKLA
İngilizce yazılmıştır. Görevin, bu metni anlamını ve TÜM sayısal/isim
detaylarını (km, personel sayısı, birlik/tesis/güzergah isimleri) BİREBİR
KORUYARAK, doğal ve resmi bir Türkçeye çevirmektir.

KESİN KURALLAR:
- TAM ve EKSİKSİZ çevir — metnin TAMAMINI, HER paragrafı/numaralı maddeyi
  çevir. ASLA özetleme, KISALTMA, sadece ana fikri verme — bu bir ÖZET
  değil, BİREBİR ÇEVİRİ görevidir.
- Çevrilen metnin uzunluğu orijinal İngilizce metinle KIYASLANABİLİR
  olmalı (tek cümlelik bir özet KABUL EDİLMEZ).
- SADECE ve SADECE aşağıdaki JSON şemasında cevap ver — başka HİÇBİR
  metin, açıklama, markdown kod bloğu veya orijinal İngilizce metnin
  kendisini EKLEME/TEKRARLAMA.

JSON şeması (tam olarak bu alan adıyla, `turkce_metin` değeri TAM çeviri
metnini içermeli):
{{"turkce_metin": "..."}}

# İNGİLİZCE METİN
{ingilizce_metin}

# TÜRKÇE ÇEVİRİ (JSON)
"""


_CEVIRI_SERBEST_METIN_PROMPT_TEMPLATE = """Translate the following text to Turkish. Output ONLY the complete \
Turkish translation and nothing else — no headers, no notes, no English, no summary, no markdown. \
Translate ALL of it, do not shorten it.

{ingilizce_metin}"""


class DecisionEngine:
    

    def __init__(
        self,
        db: Neo4jConnection,
        model: str = "llama3",
        base_url: Optional[str] = None,
        temperature: float = 0.35,
        max_alternatif: int = VARSAYILAN_ALTERNATIF_SAYISI,
        max_yakin_tesis: int = VARSAYILAN_ALTERNATIF_SAYISI,
    ) -> None:
        self.db = db
        self._max_alternatif = max_alternatif
        self._max_yakin_tesis = max_yakin_tesis

        self._base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self._model_name = model

     
        self.llm = ChatOllama(
            model=self._model_name,
            base_url=self._base_url,
            temperature=temperature,
            num_predict=512,
            num_ctx=4096,
            num_gpu=_OLLAMA_NUM_GPU,
            num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
        )

      
        self._lojistik_prompt_template = PromptTemplate(
            input_variables=["durum_ozeti", "cevresel_durum"], template=_LOJISTIK_MIKRO_PROMPT_TEMPLATE,
        )
        self._lojistik_chain = self._lojistik_prompt_template | self.llm | StrOutputParser()
        self._tahliye_prompt_template = PromptTemplate(
            input_variables=["durum_ozeti", "cevresel_durum"], template=_TAHLIYE_MIKRO_PROMPT_TEMPLATE,
        )
        self._tahliye_chain = self._tahliye_prompt_template | self.llm | StrOutputParser()
        self._sevk_prompt_template = PromptTemplate(
            input_variables=["durum_ozeti", "cevresel_durum"], template=_SEVK_MIKRO_PROMPT_TEMPLATE,
        )
        self._sevk_chain = self._sevk_prompt_template | self.llm | StrOutputParser()

       
        self._ceviri_llm = ChatOllama(
            model=self._model_name,
            base_url=self._base_url,
            temperature=0.1,
            format="json",
            num_predict=2048,
            num_ctx=4096,
            num_gpu=_OLLAMA_NUM_GPU,
            num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        self._ceviri_prompt_template = PromptTemplate(
            input_variables=["ingilizce_metin"],
            template=_CEVIRI_PROMPT_TEMPLATE,
        )
        self._ceviri_chain = self._ceviri_prompt_template | self._ceviri_llm | StrOutputParser()

        self._ceviri_llm_serbest = ChatOllama(
            model=self._model_name,
            base_url=self._base_url,
            temperature=0.1,
            num_predict=2048,
            num_ctx=4096,
            num_gpu=_OLLAMA_NUM_GPU,
            num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        self._ceviri_serbest_prompt_template = PromptTemplate(
            input_variables=["ingilizce_metin"],
            template=_CEVIRI_SERBEST_METIN_PROMPT_TEMPLATE,
        )
        self._ceviri_serbest_chain = (
            self._ceviri_serbest_prompt_template | self._ceviri_llm_serbest | StrOutputParser()
        )

        self._zemin_llm = ChatOllama(
            model=self._model_name, base_url=self._base_url, temperature=0.05,
            num_predict=512, num_ctx=4096,
            num_gpu=_OLLAMA_NUM_GPU, num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        self._lojistik_zemin_chain = self._lojistik_prompt_template | self._zemin_llm | StrOutputParser()
        self._tahliye_zemin_chain = self._tahliye_prompt_template | self._zemin_llm | StrOutputParser()
        self._sevk_zemin_chain = self._sevk_prompt_template | self._zemin_llm | StrOutputParser()

    def generate_recommendations(
        self,
        bolge: Optional[str] = None,
        odak_koordinatlari: Optional[List[Tuple[float, float]]] = None,
        paralel_calistir: bool = False,
    ) -> Dict[str, Any]:
      
        durum = self.gather_situational_picture(bolge=bolge, odak_koordinatlari=odak_koordinatlari)
        if durum.is_empty():
            return {
                "durum_bos": True,
                "durum_ozeti": "",
                "oneriler_metni": "",
                "durum": durum,
                "cevresel_durum": None,
            }

        for _olay in durum.kritik_olaylar:
            _tutarsizlik_nedeni = turkiye_harita.cografi_on_kontrol(
                _olay.get("tip"), _olay.get("enlem"), _olay.get("boylam")
            )
            if _tutarsizlik_nedeni:
                logger.warning(
                    "COĞRAFİ ÖN-KONTROL REDDETTİ: '%s' (%s) — %s LLM'e GÖNDERİLMEDİ.",
                    _olay.get("isim"), _olay.get("tip"), _tutarsizlik_nedeni,
                )
                _red_metni = (
                    f"⚠️ {_tutarsizlik_nedeni} Bu senaryo, Karar Destek Motoru'nun Coğrafi "
                    "Ön-Kontrol mekanizması tarafından yapay zekaya (LLM) gönderilmeden "
                    "REDDEDİLDİ; lütfen olayın yerini/türünü doğrulayıp raporu düzeltin."
                )
                return {
                    "durum_bos": False,
                    "cografi_red": True,
                    "cografi_red_nedeni": _tutarsizlik_nedeni,
                    "durum_ozeti": self.format_durum_ozeti(durum),
                    "oneriler_metni": f"1. {_red_metni}\n2. {_red_metni}\n3. {_red_metni}",
                    "durum": durum,
                    "cevresel_durum": None,
                }

        durum_ozeti = self.format_durum_ozeti(durum)

        temsili_koordinat = self._temsili_kriz_koordinati_bul(durum)
        cevresel_durum: Optional[CevreselDurum] = (
            cevresel_durumu_getir(*temsili_koordinat) if temsili_koordinat else None
        )
        cevresel_durum_metni = cevresel_durum.aciklama_metni if cevresel_durum else "Bilinmiyor (konum belirlenemedi)"

    
        girdi = {"durum_ozeti": durum_ozeti, "cevresel_durum": cevresel_durum_metni}
        _MIKRO_GOREV_HATA_YER_TUTUCUSU = (
            "Bu bölüm üretilemedi (Ollama servisiyle bağlantı sorunu/zaman aşımı — "
            "bkz. terminal logları); diğer bölümler bundan ETKİLENMEDİ."
        )

        
        _lojistik_sevk_birlik_var = _kategori_icin_gercek_birlik_var_mi(durum, guvenlik_de_sayilir=False)
        _tahliye_birlik_var = _kategori_icin_gercek_birlik_var_mi(durum, guvenlik_de_sayilir=True)

        def _mikro_gorevi_guvenli_calistir(chain: Any, zemin_chain: Any, bolum_adi: str, birlik_var_mi: bool) -> str:
            if not birlik_var_mi:
                logger.info(
                    "'%s' icin Bilgi Grafinda GERCEKTEN kullanilabilir birlik yok; "
                    "Ollama'ya HIC gidilmeden sabit/durust yer-tutucu kullaniliyor.",
                    bolum_adi,
                )
                return _SABIT_BULUNAMADI_CUMLESI
            try:
                return self._mikro_bolum_uret(chain, zemin_chain, girdi, durum, bolum_adi)
            except Exception as exc:  
                logger.error(
                    "'%s' mikro-görevi BAŞARISIZ OLDU: %s — bu bölüm yer-tutucu metinle "
                    "devam ediyor, DİĞER bölümler ETKİLENMEDİ.",
                    bolum_adi, exc, exc_info=True,
                )
                return _MIKRO_GOREV_HATA_YER_TUTUCUSU

        _gorevler = [
            (self._lojistik_chain, self._lojistik_zemin_chain, "Lojistik Rota", _lojistik_sevk_birlik_var),
            (self._tahliye_chain, self._tahliye_zemin_chain, "Tahliye", _tahliye_birlik_var),
            (self._sevk_chain, self._sevk_zemin_chain, "Birlik/Ekip Sevkiyatı", _lojistik_sevk_birlik_var),
        ]
        if paralel_calistir:
            with ThreadPoolExecutor(max_workers=3) as havuz:
                futures = [
                    havuz.submit(_mikro_gorevi_guvenli_calistir, chain, zemin, ad, birlik_var)
                    for chain, zemin, ad, birlik_var in _gorevler
                ]
                lojistik, tahliye, sevk = (f.result() for f in futures)
        else:
            lojistik, tahliye, sevk = (
                _mikro_gorevi_guvenli_calistir(chain, zemin, ad, birlik_var)
                for chain, zemin, ad, birlik_var in _gorevler
            )
        if lojistik == tahliye == sevk:
            oneriler_metni = f"1. {lojistik}"
        else:
            oneriler_metni = f"1. {lojistik}\n2. {tahliye}\n3. {sevk}"

        return {
            "durum_bos": False,
            "durum_ozeti": durum_ozeti,
            "oneriler_metni": oneriler_metni,
            "durum": durum,
            "cevresel_durum": cevresel_durum,
        }

    @staticmethod
    def _temsili_kriz_koordinati_bul(durum: "SituationalPicture") -> Optional[Tuple[float, float]]:
      
        for liste in (durum.kritik_olaylar, durum.kapali_yollar, durum.hasarli_tesisler):
            for kayit in liste:
                enlem, boylam = kayit.get("enlem"), kayit.get("boylam")
                if enlem is not None and boylam is not None:
                    return float(enlem), float(boylam)
        return None

    def _mikro_bolum_uret(
        self, chain: Any, zemin_chain: Any, girdi: Dict[str, str], durum: "SituationalPicture", bolum_adi: str,
    ) -> str:
        
      
        logger.info("LLM'e gonderiliyor: [%s]", bolum_adi)
        _t0 = time.monotonic()
        try:
            cevap = chain.invoke(girdi)
        except Exception as exc:  
            logger.error(
                "'%s' mikro-gorevi Ollama cagrisi BASARISIZ oldu (%.1f sn sonra): %s",
                bolum_adi, time.monotonic() - _t0, exc,
            )
            raise RuntimeError(
                f"Ollama'ya baglanilamadi (base_url={self._base_url}, "
                f"model={self._model_name}). Ollama servisinin calistigindan emin olun."
            ) from exc
        logger.info("LLM cevabi alindi (%.1f sn): [%s]", time.monotonic() - _t0, bolum_adi)

        _durum_ozeti_metni = girdi.get("durum_ozeti", "")
     
        _sayisal_referans_metni = _durum_ozeti_metni + "\n" + girdi.get("cevresel_durum", "")
      
        if (
            _dil_kilidi_ihlali_mi(cevap)
            or _veri_referanssiz_mi(cevap, durum)
            or _birlik_uydurma_suphesi_mi(cevap, _durum_ozeti_metni, bolum_adi)
            or _yanlis_bulunamadi_iddiasi_mi(cevap, "")
            or _sayisal_uydurma_suphesi_mi(cevap, _sayisal_referans_metni)
        ):
            logger.warning(
                "'%s' mikro-gorevi supheli; DUSUK SICAKLIKLA (0.05) TEK SEFERLIK "
                "yeniden deneniyor.", bolum_adi,
            )
            try:
                ikinci_cevap = zemin_chain.invoke(girdi)
                _ikinci_uydurma_isim = _kesin_birlik_ismi_uydurmasi_bul(ikinci_cevap, _durum_ozeti_metni)
                _ikinci_yanlis_bulunamadi = _yanlis_bulunamadi_iddiasi_mi(ikinci_cevap, "")
                if (
                    not _dil_kilidi_ihlali_mi(ikinci_cevap)
                    and not _veri_referanssiz_mi(ikinci_cevap, durum)
                    and _ikinci_uydurma_isim is None
                    and not _ikinci_yanlis_bulunamadi
                    and not _birlik_uydurma_suphesi_mi(ikinci_cevap, _durum_ozeti_metni, bolum_adi)
                    and not _sayisal_uydurma_suphesi_mi(ikinci_cevap, _sayisal_referans_metni)
                ):
                    cevap = ikinci_cevap
                elif _ikinci_yanlis_bulunamadi:
                   
                    logger.warning(
                        "'%s' mikro-gorevi IKINCI denemede de GERCEKTEN bulunan "
                        "bir birligi YANLIS sekilde 'bulunamadi' diye inkar etti "
                        "— koddan uretilen GUVENLI cumleye dusuluyor.",
                        bolum_adi,
                    )
                    cevap = _bulunan_ilk_birlik_ile_guvenli_cumle(
                        durum, guvenlik_de_sayilir=bolum_adi in _ABARTILI_KONTROLDEN_MUAF_BOLUMLER
                    )
                elif _ikinci_uydurma_isim is not None:
                   
                    logger.warning(
                        "'%s' mikro-gorevi IKINCI denemede de ISIM UYDURMA icerdi "
                        "('%s') — GUVENLI YER TUTUCUYA dusuluyor (hicbir uydurma "
                        "birlik nihai rapora YANSITILMAZ).",
                        bolum_adi, _ikinci_uydurma_isim,
                    )
                    cevap = _SABIT_BULUNAMADI_CUMLESI
                elif _dil_kilidi_ihlali_mi(ikinci_cevap):
                    logger.warning(
                        "'%s' mikro-gorevi ikinci denemede de YABANCI DIL/ALFABE uretti; "
                        "basit cevirmen deneniyor.", bolum_adi,
                    )
                    cevrilen = self._turkceye_cevir_serbest_metin(ikinci_cevap)
                    cevap = cevrilen if cevrilen and not _dil_kilidi_ihlali_mi(cevrilen) else ikinci_cevap
                else:
                   
                    cevap = ikinci_cevap
            except Exception as exc:  
                logger.warning("'%s' mikro-gorevi yeniden deneme cagrisi basarisiz oldu: %s", bolum_adi, exc)

        return _format_temizle(cevap)

    def _turkceye_cevir(self, metin: str) -> Optional[str]:
       
        try:
            ham_yanit = self._ceviri_chain.invoke({"ingilizce_metin": metin})
        except Exception as exc:  
            logger.warning("Türkçeye çeviri son-çare denemesi başarısız oldu: %s", exc)
            return None

        temizlenmis = re.sub(r"```(?:json)?", "", ham_yanit, flags=re.IGNORECASE).strip()
        ilk_suslu, son_suslu = temizlenmis.find("{"), temizlenmis.rfind("}")
        if ilk_suslu != -1 and son_suslu != -1 and son_suslu > ilk_suslu:
            temizlenmis = temizlenmis[ilk_suslu : son_suslu + 1].strip()

        try:
            veri = json.loads(temizlenmis)
        except json.JSONDecodeError as exc:
            logger.warning("Çeviri JSON'u ayrıştırılamadı: %s | ham yanıt: %s", exc, ham_yanit[:300])
            return None

        cevrilen = veri.get("turkce_metin") if isinstance(veri, dict) else None
        if not cevrilen or not isinstance(cevrilen, str) or not cevrilen.strip():
            logger.warning("Çeviri JSON'unda geçerli 'turkce_metin' alanı yok: %s", ham_yanit[:300])
            return None
        return cevrilen.strip()

    def _turkceye_cevir_serbest_metin(self, metin: str) -> Optional[str]:
      
        try:
            ham_yanit = self._ceviri_serbest_chain.invoke({"ingilizce_metin": metin})
        except Exception as exc:  
            logger.warning("Serbest metin ceviri (3. kademe) basarisiz oldu: %s", exc)
            return None

        temizlenmis = ham_yanit.strip()
        temizlenmis = re.sub(r"```(?:\w+)?", "", temizlenmis).strip()
     
        ilk_satir_sonu = temizlenmis.find("\n")
        if ilk_satir_sonu != -1:
            ilk_satir = temizlenmis[:ilk_satir_sonu].strip()
            if ilk_satir.endswith(":") and len(ilk_satir) < 40:
                temizlenmis = temizlenmis[ilk_satir_sonu + 1 :].strip()

        if not temizlenmis:
            logger.warning("Serbest metin ceviri (3. kademe) bos sonuc uretti.")
            return None
        return temizlenmis

  

    def gather_situational_picture(
        self, bolge: Optional[str] = None, odak_koordinatlari: Optional[List[Tuple[float, float]]] = None
    ) -> SituationalPicture:
        
        logger.info("Neo4j sorgusu basladi: durum resmi toplaniyor (bolge=%s)", bolge or "TUMU")
        _resim_baslangic = time.monotonic()

        def _bolge_kosulu(alias: str, ilk_kosul: bool) -> str:
          
            if not bolge:
                return ""
            baglayici = "WHERE" if ilk_kosul else "AND"
            return f" {baglayici} {alias}.bolge = $bolge"

   
        hasarli_tesisler = self.db.execute_query(
            "MATCH (f:Facility) WHERE f.mevcut_durum IN $durumlar" + _bolge_kosulu("f", False) + " "
            "WITH f, COALESCE(f.aciklama, f.isim) AS isim "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN f.id AS id, isim, f.facility_type AS tip, f.mevcut_durum AS durum, "
            "f.enlem AS enlem, f.boylam AS boylam, f.kapasite AS kapasite "
            "ORDER BY f.guncelleme_tarihi DESC",
            {"durumlar": HASARLI_TESIS_DURUMLARI, "bolge": bolge, "osm_imza": OSM_TEKNIK_KIMLIK_IMZASI},
        )
      
        kapali_yollar_ham = self.db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.acik_mi = false" + _bolge_kosulu("i", False) + " "
            "WITH COALESCE(i.aciklama, i.isim) AS isim, i "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN isim, i.infrastructure_type AS tip, i.highway_tipi AS highway_tipi, "
            "i.enlem AS enlem, i.boylam AS boylam, "
            "i.uzunluk_km AS uzunluk_km, i.tonaj_kapasitesi AS tonaj_kapasitesi "
            "ORDER BY i.guncelleme_tarihi DESC",
            {"osm_imza": OSM_TEKNIK_KIMLIK_IMZASI, "bolge": bolge},
        )
        kapali_yollar = _isim_bazinda_tekillestir(kapali_yollar_ham)
    
        aktif_birlikler = self.db.execute_query(
            "MATCH (u:Unit)" + _bolge_kosulu("u", True) + " "
            "RETURN u.isim AS isim, u.unit_type AS tip, "
            "u.personel_sayisi AS personel, u.hareket_kabiliyeti AS hareket_kabiliyeti, "
            "u.enlem AS enlem, u.boylam AS boylam "
            "LIMIT $limit",
            {"bolge": bolge, "limit": _AKTIF_BIRLIK_LISTELEME_LIMITI},
        )
        kritik_olaylar = self.db.execute_query(
            "MATCH (e:Event) WHERE e.siddet IN $siddetler" + _bolge_kosulu("e", False) + " "
            "RETURN e.isim AS isim, e.event_type AS tip, e.siddet AS siddet, "
            "e.etki_alani_km AS etki_alani_km, e.enlem AS enlem, e.boylam AS boylam, "
            "e.zaman_damgasi AS zaman "
            "ORDER BY e.zaman_damgasi DESC",
            {"siddetler": KRITIK_OLAY_SIDDETLERI, "bolge": bolge},
        )

        if odak_koordinatlari:
            _odak_gecerli = [
                (lat, lon) for lat, lon in odak_koordinatlari if lat is not None and lon is not None
            ]
            if _odak_gecerli:
                _odaklanmis_olaylar = [
                    olay for olay in kritik_olaylar
                    if olay.get("enlem") is not None and olay.get("boylam") is not None
                    and any(
                        _geodesic_km((olay["enlem"], olay["boylam"]), odak) <= 2.0
                        for odak in _odak_gecerli
                    )
                ]
                if _odaklanmis_olaylar:
                    kritik_olaylar = _odaklanmis_olaylar

      
        en_yakin_ulasilan_birlikler: Dict[str, List[Dict[str, Any]]] = {}
        en_yakin_guvenlik_birlikleri: Dict[str, List[Dict[str, Any]]] = {}
        alternatif_rotalar: Dict[str, List[Dict[str, Any]]] = {}
        en_yakin_saglam_tesisler: Dict[str, List[Dict[str, Any]]] = {}

     
        def _analiz_guvenli_calistir(fonksiyon: Any, girdi_kaydi: Dict[str, Any], gorev_adi: str) -> Optional[Any]:
            try:
                return fonksiyon(girdi_kaydi, bolge)
            except Exception as exc: 
                logger.error(
                    "%s BAŞARISIZ OLDU (%s): %s — bu iş birimi ATLANIYOR, DİĞER iş "
                    "birimleri ETKİLENMEDİ.",
                    gorev_adi, girdi_kaydi.get("isim", "bilinmiyor"), exc, exc_info=True,
                )
                return None

        for yol in kapali_yollar:
            sonuc = _analiz_guvenli_calistir(self._kapali_yol_analiz_et, yol, "Kapalı yol analizi")
            if sonuc is None:
                continue
            isim, alternatif, birlikler, guvenlik = sonuc
            alternatif_rotalar[isim] = alternatif
            if birlikler is not None:
                en_yakin_ulasilan_birlikler[isim] = birlikler
                en_yakin_guvenlik_birlikleri[isim] = guvenlik
        for olay in kritik_olaylar:
            sonuc = _analiz_guvenli_calistir(self._olay_analiz_et, olay, "Kritik olay analizi")
            if sonuc is None:
                continue
            isim, birlikler, guvenlik = sonuc
            if birlikler is not None:
                en_yakin_ulasilan_birlikler[isim] = birlikler
                en_yakin_guvenlik_birlikleri[isim] = guvenlik
        for tesis in hasarli_tesisler:
            sonuc = _analiz_guvenli_calistir(self._tesis_analiz_et, tesis, "Hasarlı tesis analizi")
            if sonuc is None:
                continue
            isim, saglam = sonuc
            en_yakin_saglam_tesisler[isim] = saglam

        logger.info(
            "Neo4j sorgusu bitti (%.1f sn) | %d hasarli tesis, %d kapali yol, %d kritik olay, "
            "%d aktif birlik | LLM'e gonderiliyor.",
            time.monotonic() - _resim_baslangic, len(hasarli_tesisler), len(kapali_yollar),
            len(kritik_olaylar), len(aktif_birlikler),
        )
        return SituationalPicture(
            hasarli_tesisler=hasarli_tesisler,
            kapali_yollar=kapali_yollar,
            aktif_birlikler=aktif_birlikler,
            kritik_olaylar=kritik_olaylar,
            alternatif_rotalar=alternatif_rotalar,
            en_yakin_saglam_tesisler=en_yakin_saglam_tesisler,
            en_yakin_ulasilan_birlikler=en_yakin_ulasilan_birlikler,
            en_yakin_guvenlik_birlikleri=en_yakin_guvenlik_birlikleri,
        )

    def _kapali_yol_analiz_et(
        self, yol: Dict[str, Any], bolge: Optional[str] = None
    ) -> "tuple[str, List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]":
       
        isim = yol["isim"]
        logger.info("Neo4j sorgusu basladi: [%s] (kapali yol)", isim)
        _t0 = time.monotonic()
        alternatif = self._en_yakin_acik_yollari_bul(yol, bolge=bolge)
        if yol.get("enlem") is None or yol.get("boylam") is None:
            logger.info("Neo4j sorgusu bitti (%.1f sn): [%s] (koordinat yok, birlik aramasi atlandi)",
                        time.monotonic() - _t0, isim)
            return isim, alternatif, None, None
       
        birlikler = self._en_yakin_ulasilan_birlikleri_bul(
            yol, oncelik_tipleri=_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI, bolge=bolge
        )
        guvenlik = self._guvenlik_birlikleri_bul(yol, bolge=bolge)
        logger.info("Neo4j sorgusu bitti (%.1f sn): [%s]", time.monotonic() - _t0, isim)
        return isim, alternatif, birlikler, guvenlik

    def _olay_analiz_et(
        self, olay: Dict[str, Any], bolge: Optional[str] = None
    ) -> "tuple[str, Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]":
       
        isim = olay["isim"]
        logger.info("Neo4j sorgusu basladi: [%s] (kritik olay)", isim)
        _t0 = time.monotonic()
        if olay.get("enlem") is None or olay.get("boylam") is None:
            logger.info("Neo4j sorgusu bitti (%.1f sn): [%s] (koordinat yok, birlik aramasi atlandi)",
                        time.monotonic() - _t0, isim)
            return isim, None, None
        oncelik_tipleri = _OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI.get(
            olay.get("tip"), _VARSAYILAN_ONCELIKLI_BIRIM_TIPLERI
        )
        birlikler = self._en_yakin_ulasilan_birlikleri_bul(
            olay, oncelik_tipleri=oncelik_tipleri, bolge=bolge
        )
        guvenlik = self._guvenlik_birlikleri_bul(olay, bolge=bolge)
        logger.info("Neo4j sorgusu bitti (%.1f sn): [%s]", time.monotonic() - _t0, isim)
        return isim, birlikler, guvenlik

    def _tesis_analiz_et(
        self, tesis: Dict[str, Any], bolge: Optional[str] = None
    ) -> "tuple[str, List[Dict[str, Any]]]":
       
        isim = tesis["isim"]
        logger.info("Neo4j sorgusu basladi: [%s] (hasarli tesis)", isim)
        _t0 = time.monotonic()
        sonuc = self._en_yakin_aktif_tesisleri_bul(tesis, bolge=bolge)
        logger.info("Neo4j sorgusu bitti (%.1f sn): [%s]", time.monotonic() - _t0, isim)
        return isim, sonuc

    def _en_yakin_acik_yollari_bul(
        self, kapali_yol: Dict[str, Any], bolge: Optional[str] = None
    ) -> List[Dict[str, Any]]:
      
        bolge_kosulu = " AND i.bolge = $bolge" if bolge else ""
        il_kosulu_metni, izinli_iller = _il_geofencing_kosulu("i", kapali_yol)
        il_kosulu = f" AND {il_kosulu_metni}" if il_kosulu_metni else ""
        adaylar = self.db.execute_query(
            "MATCH (i:Infrastructure) "
            "WHERE i.acik_mi = true AND i.konum IS NOT NULL "
            "AND point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre" + bolge_kosulu + il_kosulu + " "
            "WITH COALESCE(i.aciklama, i.isim) AS isim, i, "
            "point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) AS mesafe_metre "
            "WHERE isim <> $isim AND NOT isim CONTAINS $osm_imza "
            "RETURN isim, i.infrastructure_type AS tip, i.highway_tipi AS highway_tipi, "
            "i.tonaj_kapasitesi AS tonaj_kapasitesi, i.uzunluk_km AS uzunluk_km, "
            "mesafe_metre / 1000.0 AS yaklasik_mesafe_km "
            "ORDER BY mesafe_metre ASC LIMIT $limit",
            {
                "isim": kapali_yol["isim"],
                "enlem": kapali_yol["enlem"],
                "boylam": kapali_yol["boylam"],
                "yaricap_metre": _ARAMA_YARICAPI_KM * 1000.0,
                "osm_imza": OSM_TEKNIK_KIMLIK_IMZASI,
                "bolge": bolge,
                "izinli_iller": izinli_iller,
                "limit": self._max_alternatif * _ADAY_COKLUGU_KATSAYISI,
            },
        )
        return _isim_bazinda_tekillestir(adaylar, adet=self._max_alternatif)

    def _en_yakin_aktif_tesisleri_bul(
        self, hasarli_tesis: Dict[str, Any], bolge: Optional[str] = None
    ) -> List[Dict[str, Any]]:
    
        bolge_kosulu = " AND f.bolge = $bolge" if bolge else ""
        il_kosulu_metni, izinli_iller = _il_geofencing_kosulu("f", hasarli_tesis)
        il_kosulu = f" AND {il_kosulu_metni}" if il_kosulu_metni else ""
        return self.db.execute_query(
            "MATCH (f:Facility) "
            "WHERE f.mevcut_durum = 'Aktif' AND f.konum IS NOT NULL "
            "AND point.distance(f.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre AND f.id <> $id" + bolge_kosulu + il_kosulu + " "
            "WITH f, COALESCE(f.aciklama, f.isim) AS isim, "
            "point.distance(f.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) AS mesafe_metre "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN isim, f.facility_type AS tip, f.kapasite AS kapasite, "
            "mesafe_metre / 1000.0 AS yaklasik_mesafe_km "
            "ORDER BY mesafe_metre ASC LIMIT $limit",
            {
                "id": hasarli_tesis["id"],
                "enlem": hasarli_tesis["enlem"],
                "boylam": hasarli_tesis["boylam"],
                "yaricap_metre": _ARAMA_YARICAPI_KM * 1000.0,
                "osm_imza": OSM_TEKNIK_KIMLIK_IMZASI,
                "bolge": bolge,
                "izinli_iller": izinli_iller,
                "limit": self._max_yakin_tesis,
            },
        )

    def _en_yakin_ulasilan_birlikleri_bul(
        self,
        olay_yeri: Dict[str, Any],
        oncelik_tipleri: Optional[List[str]] = None,
        bolge: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
     
     
        il_kosulu_metni, izinli_iller = _il_geofencing_kosulu("h", olay_yeri)
        kosul_parcalari = []
        if bolge:
            kosul_parcalari.append("h.bolge = $bolge")
        if il_kosulu_metni:
            kosul_parcalari.append(il_kosulu_metni)
        ekstra_where = ("AND " + " AND ".join(kosul_parcalari)) if kosul_parcalari else ""
        ekstra_parametreler: Optional[Dict[str, Any]] = {}
        if bolge:
            ekstra_parametreler["bolge"] = bolge
        if izinli_iller:
            ekstra_parametreler["izinli_iller"] = izinli_iller
        ekstra_parametreler = ekstra_parametreler or None

        if not oncelik_tipleri:
            return self.db.en_yakin_ulasilan_varliklari_bul(
                event_enlem=olay_yeri["enlem"],
                event_boylam=olay_yeri["boylam"],
                hedef_label="Unit",
                ilk_n=self._max_alternatif,
                ekstra_where=ekstra_where,
                ekstra_parametreler=ekstra_parametreler,
            )

        sonuc = self.db.en_yakin_ulasilan_varliklari_bul(
            event_enlem=olay_yeri["enlem"],
            event_boylam=olay_yeri["boylam"],
            hedef_label="Unit",
            ilk_n=self._max_alternatif,
            ekstra_where=ekstra_where,
            ekstra_parametreler=ekstra_parametreler,
            unit_tipleri=oncelik_tipleri,
        )
        if not sonuc:
            logger.info(
                "'%s' icin %s yeteneginde birim %.0f km icinde bulunamadi; arama %.0f "
                "km'ye GENISLETILIYOR (yavaslama beklenir, SADECE bu durumda).",
                olay_yeri.get("isim"), oncelik_tipleri,
                Neo4jConnection._YOL_AGI_ARAMA_YARICAPI_KM, _YETENEK_ARAMA_YARICAPI_KM,
            )
            sonuc = self.db.en_yakin_ulasilan_varliklari_bul(
                event_enlem=olay_yeri["enlem"],
                event_boylam=olay_yeri["boylam"],
                hedef_label="Unit",
                ilk_n=self._max_alternatif,
                yol_agi_yaricapi_km=_YETENEK_ARAMA_YARICAPI_KM,
                ekstra_where=ekstra_where,
                ekstra_parametreler=ekstra_parametreler,
                unit_tipleri=oncelik_tipleri,
            )
            if not sonuc:
                logger.warning(
                    "'%s' icin %s yeteneginde HICBIR birim %.0f km icinde bile bulunamadi.",
                    olay_yeri.get("isim"), oncelik_tipleri, _YETENEK_ARAMA_YARICAPI_KM,
                )
        return sonuc

    def _guvenlik_birlikleri_bul(
        self, olay_yeri: Dict[str, Any], bolge: Optional[str] = None
    ) -> List[Dict[str, Any]]:
       
        il_kosulu_metni, izinli_iller = _il_geofencing_kosulu("h", olay_yeri)
        kosul_parcalari = []
        if bolge:
            kosul_parcalari.append("h.bolge = $bolge")
        if il_kosulu_metni:
            kosul_parcalari.append(il_kosulu_metni)
        ekstra_where = ("AND " + " AND ".join(kosul_parcalari)) if kosul_parcalari else ""
        ekstra_parametreler: Optional[Dict[str, Any]] = {}
        if bolge:
            ekstra_parametreler["bolge"] = bolge
        if izinli_iller:
            ekstra_parametreler["izinli_iller"] = izinli_iller
        return self.db.en_yakin_ulasilan_varliklari_bul(
            event_enlem=olay_yeri["enlem"],
            event_boylam=olay_yeri["boylam"],
            hedef_label="Unit",
            ilk_n=self._max_alternatif,
            ekstra_where=ekstra_where,
            ekstra_parametreler=ekstra_parametreler or None,
            unit_tipleri=_GUVENLIK_BIRIM_TIPLERI,
        )

   

    @staticmethod
    def format_durum_ozeti(durum: SituationalPicture) -> str:
     
        parcalar: List[str] = []

        if durum.kritik_olaylar:
            parcalar.append("## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR")
            for olay in durum.kritik_olaylar:
                kriz_turu = (
                    f"OLAY — {_deger(olay, 'tip')}, şiddet: {_deger(olay, 'siddet')}, "
                    f"etki alanı ~{_deger(olay, 'etki_alani_km')} km"
                )
                parcalar.append(
                    DecisionEngine._kriz_ve_mudahale_bloku(
                        olay["isim"], kriz_turu,
                        durum.en_yakin_ulasilan_birlikler.get(olay["isim"], []),
                        durum.en_yakin_guvenlik_birlikleri.get(olay["isim"], []),
                    )
                )

        _olay_konumlari: List[Tuple[float, float]] = [
            (olay["enlem"], olay["boylam"])
            for olay in durum.kritik_olaylar
            if olay.get("enlem") is not None and olay.get("boylam") is not None
        ]

        def _ilgili_mi(kayit: Dict[str, Any]) -> bool:
            if not _olay_konumlari or kayit.get("enlem") is None or kayit.get("boylam") is None:
                return True
            nokta = (kayit["enlem"], kayit["boylam"])
            return any(
                _geodesic_km(nokta, olay_yeri) <= _KRIZ_ANLATIM_ILGILILIK_YARICAP_KM
                for olay_yeri in _olay_konumlari
            )

        hasarli_tesisler_ilgili = [t for t in durum.hasarli_tesisler if _ilgili_mi(t)]
        kapali_yollar_ilgili = [y for y in durum.kapali_yollar if _ilgili_mi(y)]
        _uzak_tesis_sayisi = len(durum.hasarli_tesisler) - len(hasarli_tesisler_ilgili)
        _uzak_yol_sayisi = len(durum.kapali_yollar) - len(kapali_yollar_ilgili)
       

        if hasarli_tesisler_ilgili:
            parcalar.append("\n## HASARLI / YOK EDİLMİŞ TESİSLER")
            for tesis in hasarli_tesisler_ilgili:
                parcalar.append(f"- {tesis['isim']} ({_deger(tesis, 'tip')}) durumu: {_deger(tesis, 'durum')}")
                yakinlar = durum.en_yakin_saglam_tesisler.get(tesis["isim"], [])
                if yakinlar:
                    yakin_str = "; ".join(
                        f"{y['isim']} ({_deger(y, 'tip')}, ~{y['yaklasik_mesafe_km']:.0f} km, "
                        f"kapasite: {_deger(y, 'kapasite')})"
                        for y in yakinlar
                    )
                    parcalar.append(f"  En yakın SAĞLAM alternatif tesisler: {yakin_str}")
                else:
                    parcalar.append("  UYARI: Yakında sağlam (Aktif) bir alternatif tesis bulunamadı.")

        if kapali_yollar_ilgili:
            parcalar.append("\n## KAPALI GÜZERGAHLAR")
            for yol in kapali_yollar_ilgili:
                kriz_turu = (
                    f"KAPALI GÜZERGAH — {_yol_tip_etiketi(yol.get('tip'), yol.get('highway_tipi')) or _deger(yol, 'tip')}, "
                    f"{_deger(yol, 'uzunluk_km')} km, tonaj kapasitesi: {_deger(yol, 'tonaj_kapasitesi')} ton"
                )
                parcalar.append(
                    DecisionEngine._kriz_ve_mudahale_bloku(
                        yol["isim"], kriz_turu,
                        durum.en_yakin_ulasilan_birlikler.get(yol["isim"], []),
                        durum.en_yakin_guvenlik_birlikleri.get(yol["isim"], []),
                    )
                )
                alternatifler = durum.alternatif_rotalar.get(yol["isim"], [])
                if alternatifler:
                    alt_str = "; ".join(
                        f"{a['isim']} ({_yol_tip_etiketi(a.get('tip'), a.get('highway_tipi')) or _deger(a, 'tip')}, "
                        f"~{a['yaklasik_mesafe_km']:.0f} km uzaklıkta, tonaj: {_deger(a, 'tonaj_kapasitesi')} ton)"
                        for a in alternatifler
                    )
                    parcalar.append(f"  Yakındaki AÇIK alternatif güzergahlar: {alt_str}")
                else:
                    parcalar.append("  UYARI: Yakında açık bir alternatif güzergah bulunamadı.")

       
        if _uzak_tesis_sayisi or _uzak_yol_sayisi:
            logger.info(
                "ANLATIM İLGİLİLİK SÜZGECİ: aktif kriz konumuna ~%.0f km'den uzak olduğu için "
                "rapor metninden çıkarılan %d hasarlı tesis, %d kapalı güzergah kaydı "
                "(bunlar Bilgi Grafında hâlâ mevcut, sadece bu rapora dahil edilmedi).",
                _KRIZ_ANLATIM_ILGILILIK_YARICAP_KM, _uzak_tesis_sayisi, _uzak_yol_sayisi,
            )

        return "\n".join(parcalar) if parcalar else "Bilgi Grafında şu an değerlendirilecek bir kriz verisi yok."

    @staticmethod
    def _kriz_ve_mudahale_bloku(
        kriz_ismi: str,
        kriz_turu: str,
        birlikler: List[Dict[str, Any]],
        guvenlik_birlikleri: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        
        parcalar = [f"KRİZ NOKTASI: {kriz_ismi} ({kriz_turu})."]
        if not birlikler:
            parcalar.append(
                "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
                "(bkz. aşağıdaki öncelik) ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok "
                "(kapalı yollar mevcut birlikleri izole ediyor olabilir). Bu net bir ERİŞİM "
                "SORUNUDUR, öneri üretirken açıkça belirt."
            )
        else:
            for b in birlikler:
                parcalar.append(
                    f"  MÜDAHALE EDECEK BİRLİK: {_temiz_isim(b)} (Tip: {_deger(b, 'unit_type', 'birim')}, "
                    f"Yetenekleri: {_gercek_yetenek_aciklamasi(b)}, "
                    f"{_deger(b, 'personel_sayisi')} personel). Bu birlik, KRİZ NOKTASINA "
                    f"{b.get('mesafe_km', '?')} km uzaklıktadır ve rotası AÇIKTIR."
                )

        if guvenlik_birlikleri:
            parcalar.append("  ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ (2. PLAN — birincil çözüm ÖNERME):")
            for g in guvenlik_birlikleri:
                parcalar.append(
                    f"    - {_temiz_isim(g)} (Tip: {_deger(g, 'unit_type', 'birim')}, "
                    f"{_deger(g, 'personel_sayisi')} personel), KRİZ NOKTASINA "
                    f"{g.get('mesafe_km', '?')} km uzaklıkta, rotası AÇIK."
                )
        return "\n".join(parcalar)
