"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
Müşterek Kriz ve Afet Koordinasyon Merkezi (SAKOM) Başkanlığı — Taktiksel
Karar Destek Motoru (GraphRAG).

Bu modül, sistemi salt bir "harita çizen pano" olmaktan çıkarıp gerçek bir
Karar Destek Sistemi (KDS) haline getiren bileşendir. `DecisionEngine`:

  1. Neo4j Bilgi Grafını tarar (hasarlı/yok edilmiş tesisler, kapalı
     güzergahlar, sahadaki aktif birlikler, kritik/katastrofik olaylar).
  2. Kapalı bir güzergah (`Infrastructure.acik_mi = false`) tespit ettiğinde,
     coğrafi olarak en yakın AÇIK alternatif güzergahları ve her hasarlı
     tesise en yakın SAĞLAM (Aktif) tesisleri ayrıca sorgular.
  3. Bu anlık graf durumunu düz metin bir "GraphRAG bağlamı" haline getirip
     yerel Ollama modeline verir; olayın TÜRÜNE göre doğru birimi
     önceliklendirerek 3 maddelik taktiksel karar/yönlendirme önerisi
     üretir. "MİKRO-GÖREV MİMARİSİ" (bkz. modül içindeki ilgili not — bir
     "Çoklu Ajan" sıralı hiyerarşisi denenmiş, 90-206 sn/rapor
     gecikme ve tekrar eden içerik ürettiği için TERK EDİLMİŞTİR): bu 3
     madde TEK bir dev LLM çağrısıyla DA, birbirine bağımlı ardışık bir
     ajan zinciriyle DE DEĞİL, 3 BAĞIMSIZ dar-görevli çağrıyla üretilip
     Python seviyesinde birleştirilir — SADECE askeri değil, SİVİL
     (İtfaiye/Sağlık-UMKE/AFAD) ve askeri bileşenleri birlikte koordine
     eden "SİVİL-ASKERİ KOORDİNASYON" önceliklendirmesi Sevk/Tahliye
     mikro-promptlarına dağıtılmıştır.

Mimari not: Bu, `src.data_ingestion.nlp_parser.OllamaParser` ile AYNI Ollama
sunucusunu ama TAM TERSİ yönde kullanır — `OllamaParser` serbest metni graf
verisine çevirirken (Text -> Graph), `DecisionEngine` graf verisini serbest
metin taktiksel öneriye çevirir (Graph -> Text, yani "GraphRAG").
"""

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
    # Guncel langchain-community siniflandirmasi
    from langchain_community.chat_models import ChatOllama
except ImportError:  # pragma: no cover - eski paket surumleri icin fallback
    from langchain_community.llms import Ollama as ChatOllama  # type: ignore

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.core import turkiye_harita
from src.core.database import Neo4jConnection
from src.core.environmental_context import CevreselDurum, cevresel_durumu_getir
from src.data_ingestion.real_osm_loader import OSM_TEKNIK_KIMLIK_IMZASI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DONANIM (GPU/CPU) OPTIMIZASYONU:
# Ollama'nin `num_gpu` secenegi -1 iken KENDI VARSAYILANI olan "otomatik
# tespit"i kullanir (Ollama'nin Go kaynagindaki DefaultOptions.NumGPU da
# ZATEN -1'dir) — yani `num_gpu=-1` VERMEK, HICBIR SEY ZORLAMADAN sadece
# Ollama'nin KENDI otomatik-tespitine (VRAM/model boyutuna gore, bazen
# YANLIS/eksik tahmin edebilen bir sezgisel) GERI DONMEK anlamina gelir —
# GPU'nun tam yuklenmemesi riskini BU YUZDEN COZMEZ.
# Ollama toplulugunun KANITLANMIS pratigi: modelin GERCEK katman sayisindan
# BUYUK bir deger vermek (Ollama bunu otomatik olarak modelin gercek
# katman sayisina KIRPAR) — bu, "TUM katmanlari GPU'ya zorla" niyetini
# GERCEKTEN yerine getiren tek yontemdir. `OLLAMA_NUM_GPU` ortam
# degiskeniyle override edilebilir (varsayilan 999 — llama3/llama3.1 gibi
# 8B-siniflari icin fazlasiyla yeterli bir ust sinirdir).
_OLLAMA_NUM_GPU = int(os.getenv("OLLAMA_NUM_GPU", "999"))
"""TÜM katmanları GPU'ya (CUDA) zorlamak için `ChatOllama(num_gpu=...)`e
verilen değer — bkz. yukarıdaki "DONANIM OPTİMİZASYONU" notu."""

_OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", str(min(16, os.cpu_count() or 8))))
"""CPU/GPU veri yolu darboğazını azaltmak için Ollama'nın (prompt işleme +
GPU'ya offload edilmeyen artık katmanlar için) kullanacağı CPU thread
sayısı — `OLLAMA_NUM_THREAD` ortam değişkeniyle override edilebilir;
verilmezse bu makinenin GERÇEK çekirdek sayısına göre (`os.cpu_count()`,
16 ile sınırlanmış) hesaplanır — rastgele sabit bir "8" yerine SİSTEME
UYGUN bir varsayılandır."""

_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE = float(os.getenv("OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE", "400"))
"""LLM istekleri için katı zaman aşımı: `ChatOllama`/`Ollama` (bkz.
yukarıdaki import fallback) örneğine verilen bu değer, LangChain'in
`requests` ile yaptığı HTTP isteğinin `timeout=` parametresine doğrudan
aktarılır (`langchain_community.llms.ollama.Ollama._create_stream`,
`timeout=self.timeout`). Yani model çökmüş/asılı kalmış bir Ollama
sürecine karşı gerçek bir ağ-seviyesi güvenlik ağıdır; olmadığında bir
istek sonsuza dek bekleyebilir (`requests`'in varsayılanı `timeout=None`
dır).

DEĞER SEÇİMİ (400 sn — Neo4j tarafındaki aynı "belgelenmiş en kötü
durumun üzerinde pay bırak" ilkesiyle, bkz. `database._VARSAYILAN_SORGU_
ZAMAN_ASIMI_SANIYE`): gerçek, çökmemiş LLM cevaplarının GPU/donanım
paralelleştirmesi öncesi bile 5-200 sn arasında sürdüğü gözlemlenmiştir.
400 sn, bu gözlemlenen en kötü durumun (200 sn) iki katı bir güvenlik
payı bırakır — yani meşru ama yavaş bir cevabı yanlışlıkla kesmez, ama
gerçek bir donma/kilitlenmeyi yine de kesin olarak durdurur.
`OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE` ortam değişkeniyle override
edilebilir."""

_AKTIF_BIRLIK_LISTELEME_LIMITI = 500
"""Bkz. `gather_situational_picture`daki `aktif_birlikler` sorgusunun
yanındaki tam gerekçe: bu alan sadece bir anti-sahtecilik (prompt leakage)
isim kontrolü için kullanılır, LLM'e ham sunulmaz; ülke çapında binlerce
satıra ulaşabilen sınırsız bir sonucu makul bir üst sınıra çeker."""

# EŞZAMANLILIK MİMARİSİ: `gather_situational_picture` ve `generate_
# recommendations`, Streamlit'in kendi çalışma modeliyle (ScriptRunContext
# worker thread'lere taşınmadığından, kendi iç kilit/kaynak yönetimiyle)
# çakışmayı önlemek için `ThreadPoolExecutor` KULLANMAZ; her ikisi de düz,
# sıralı bir `for` döngüsüyle çalışır: bir iş birimi (kapalı yol/kritik
# olay/hasarlı tesis analizi veya Lojistik/Tahliye/Sevk mikro-görevi)
# bitmeden bir sonraki başlamaz. Bu, tek bir iş biriminin yapısal olarak
# asla diğerlerini arka planda askıda bırakamayacağı (thread yok, future
# yok, `shutdown` beklemesi yok) en basit ve en sağlam garantidir; bedeli
# süredir (paralel bir tasarıma göre daha yavaş), ama bu bedel arayüzün
# yapısal olarak asla kilitlenememesine kıyasla kabul edilebilir.

# Facility.mevcut_durum -> "hasarli/etkilenmis" kabul edilen degerler.
HASARLI_TESIS_DURUMLARI = ["Hasarlı", "Yok Edildi"]

# Event.siddet -> kurmay baskanligina gosterilecek kadar "kritik" kabul
# edilen esik (bu ikilerin ALTINDAKI olaylar oneri uretiminde goz ardi edilir).
# "Yuksek" DAHIL EDILMISTIR (SADECE Kritik/Katastrofik degil): bir koprunun
# yikilmasi gibi gercek senaryolar LLM tarafindan genelde "Yuksek" siddetle
# etiketleniyor; bu esik cok DAR tutulursa "Mekansal
# Korluk" duzeltmesi (bkz. `_en_yakin_ulasilan_birlikleri_bul`) TAM DA
# ihtiyac duyulan gercek vakalarda hic devreye girmezdi.
KRITIK_OLAY_SIDDETLERI = ["Yuksek", "Kritik", "Katastrofik"]

# Bir kapali yola/hasarli tesise "yakin" sayilacak varsayilan aday sayisi.
VARSAYILAN_ALTERNATIF_SAYISI = 3

# İLGİLİLİK SÜZGECİ: `generate_recommendations`in bilinçli tasarımı
# ("bolge=None ile TÜM ülke birlikte değerlendirilir" — bkz. o fonksiyonun
# docstring'i), `format_durum_ozeti` tarafından süzülmeden bırakılırsa,
# ülke çapındaki HER kapalı yolu/hasarlı tesisi (aktif krizin konumuyla
# hiçbir ilişkisi kurulmadan) LLM'in bağlamına döker. Bu iki kaygı AYRI
# şeylerdir: "en yakın/en yetenekli birliği bulmak için ülke çapında ara"
# (DOĞRU bir KDS ilkesi — uzak bir takviye birlik gerçekten en uygun
# seçenek olabilir) ile "komutana ANLATILAN metne, olayla coğrafi ilgisi
# OLMAYAN altyapıyı karıştırmak" (YANLIŞ) birbirine KARIŞTIRILMIŞTI.
# Çözüm: arama kapsamı ülke çapında KALIR (DEĞİŞTİRİLMEDİ), ama METNE
# GİRECEK kapalı yol/hasarlı tesis listesi, aktif kritik olay(lar)a bu
# yarıçap İÇİNDE olacak şekilde SÜZÜLÜR (bkz. `format_durum_ozeti`deki
# `_ilgili_mi` kullanımı) — süzgeç HİÇBİR ŞEY bırakmazsa (ör. hiç aktif
# olay yoksa) GÜVENLİ VARSAYILAN olarak TÜM liste korunur, rapor asla
# sessizce boşalmaz.
_KRIZ_ANLATIM_ILGILILIK_YARICAP_KM = 150.0


# GÖRÜNEN AD AKILLANDIRMASI: `motorway/trunk/primary/secondary/tertiary`
# highway tiplerinin HEPSİ, veri modelinde TEK bir
# `infrastructure_type=Sokak` altında toplanır — bu KASITLIDIR ve
# değiştirilemez: `Infrastructure._ALT_ETIKET_ESLEMESI` (bkz. models.py),
# "Sokak"ı ikincil Neo4j etiketi `:Street`e eşler; TÜM rota bulma/Dijkstra
# sorguları (`WHERE n:Street OR n:Bridge`) SADECE bu etikete bakar.
# `infrastructure_type`i "Karayolu" (`:Road` etiketi) olarak değiştirmek,
# motorway/trunk/primary segmentlerini rotalanabilir ağdan TAMAMEN
# SİLERDİ (sadece 109 `:Road` düğümü var, 967binin üzerindeki `:Street`e
# KIYASLA), yani "kozmetik" bir isim düzeltmesi UYGULAMA ÇAPINDA bir
# rotalama REGRESYONUNA yol açardı. Bu yüzden ALTTAKİ VERİ/ETİKET
# DEĞİŞTİRİLMEZ — sadece komutana/LLM'e SUNULAN METİN, zaten var olan
# `highway_tipi` ham alanına (bkz. `real_osm_loader.py` — "motorway",
# "trunk", "primary" vb. BİREBİR saklanır) bakılarak AKILLANDIRILIR.
_ANA_KARAYOLU_HIGHWAY_TIPLERI = frozenset({"motorway", "trunk", "primary"})


def _yol_tip_etiketi(tip: Optional[str], highway_tipi: Optional[str]) -> Optional[str]:
    """Bir `Infrastructure` kaydının komutana gösterilecek tip etiketini
    üretir — veri modelindeki `infrastructure_type` (Neo4j etiketi/rota
    mantığı için SABİT kalır) YERİNE, SADECE bu METİN için `highway_tipi`
    ham alanına bakarak "Sokak" yerine daha doğru bir görünen ad seçer
    (bkz. yukarıdaki "GÖRÜNEN AD AKILLANDIRMASI" notu). `tip` "Sokak" DEĞİLSE
    (ör. "Kopru", "Tunel") veya `highway_tipi` bilinmiyorsa DOKUNULMADAN
    döner."""
    if tip != "Sokak" or not highway_tipi:
        return tip
    if highway_tipi in _ANA_KARAYOLU_HIGHWAY_TIPLERI:
        return "Ana Karayolu"
    if highway_tipi in ("secondary", "tertiary"):
        return "Cadde"
    return tip


def _geodesic_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """İki (enlem, boylam) noktası arası gerçek jeodezik mesafe (haversine,
    km). `src.core.database.Neo4jConnection._geodesic_km` ile AYNI formül —
    bu modül o sınıfın "private" (alt çizgili) bir metoduna bağımlı
    OLMAMASI için burada bağımsız olarak tutulur (iki modülün birbirinden
    BAĞIMSIZ test edilebilir/anlaşılabilir kalması için bilinçli küçük
    bir kod tekrarı)."""
    R_KM = 6371.0088
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R_KM * math.asin(min(1.0, math.sqrt(a)))

# MEKANSAL INDEKS (Faz 3): "en yakin acik alternatif/saglam tesis"
# sorgularinin (bkz. `_en_yakin_acik_yollari_bul`/`_en_yakin_aktif_tesisleri_bul`)
# ilk elemeyi YAPTIGI (ve `database.ensure_spatial_indexes`in POINT INDEX'ini
# KULLANDIGI) arama yaricapi. Ulusal olcekte, olay yerinden bu yaricapin
# DISINDAKI adaylar hicbir zaman "en yakin" olamayacagindan, indeks bu
# yaricapi kullanarak TUM grafi taramadan adaylari daraltabilir.
#
# "İL/İLÇE SİYASİ HARİTA KİLİDİ": bu sabit
# `turkiye_harita.yaricapi_operasyonel_sinira_kisitla` ile GEÇİRİLİR —
# şu an (50 km) zaten `MAKSIMUM_OPERASYON_YARICAPI_KM` (150 km) altında
# olduğundan bu çağrı BİR ŞEYİ DEĞİŞTİRMEZ; asıl amacı, bu sabit ileride
# YANLIŞLIKLA (ör. bir performans denemesinde) çok büyütülürse bile "Rize
# krizine İstanbul'dan birlik" sınıfı bir hatanın YAPISAL olarak
# İMKANSIZ kalmasını garanti etmektir (bkz. o fonksiyonun docstring'i).
_ARAMA_YARICAPI_KM = turkiye_harita.yaricapi_operasyonel_sinira_kisitla(50.0)

# "YETENEK BAZLI ROTA" (Capability-Based Routing): `_en_yakin_ulasilan_
# birlikleri_bul`, bir köprü/altyapı krizinde SADECE mesafeye bakarsa, 7
# km'deki bir Emniyet birimi 10 km'deki tek Ağır Mühendislik (vinç/dozer)
# biriminin önüne geçip LLM'e sadece Emniyet'i sunar; LLM de mecburen vinç
# yerine polis önerir. Aşağıdaki eşleme, olay/kapalı-yol TÜRÜNE göre HANGİ
# birim tiplerinin ÖNCELİKLİ
# aranacağını belirler — "SİVİL-ASKERİ KOORDİNASYON KURALI" (bkz.
# DECISION_PROMPT_TEMPLATE) ile AYNI önceliklendirme mantığıdır, ama artık
# SADECE modele "öyle seç" demekle YETİNİLMEZ — veri, modele ulaşmadan ÖNCE
# doğru yeteneğe göre FİLTRELENİR.
_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI: List[str] = ["Agir Muhendislik", "AFAD", "Arama Kurtarma"]
"""Bir KAPALI GÜZERGAH (köprü/yol/tünel — bkz. `gather_situational_picture`)
HER ZAMAN bir altyapı sorunudur: enkazı/molozu fiilen kaldırabilecek TEK
birim türü Ağır Mühendislik'tir, AFAD/UMKE ise arama-kurtarma desteği
sağlar. Emniyet/Askeri bu senaryoda BİRİNCİL çözüm aracı DEĞİLDİR (bkz.
`_GUVENLIK_BIRIM_TIPLERI`) — SADECE çevre güvenliği/trafik kontrolü için
İKİNCİL bir listede ayrıca sunulur."""

_OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI: Dict[str, List[str]] = {
    # "Polis" bilinçli olarak Savaş/Siber Saldırı için birincil aramaya
    # dahil edilmez — bu, modülün kendi `_GUVENLIK_BIRIM_TIPLERI`
    # docstring'inde beyan edilen ilkeyle (Emniyet/Askeri birimler
    # birincil "MÜDAHALE EDECEK BİRLİK" aramasına karışmaz) tutarlıdır;
    # menzilde sadece polis varsa arama, mevcut "2. plan" güvenlik
    # mekanizmasına (`_guvenlik_birlikleri_bul`) düşer.
    "Savas": ["Askeri Birlik"],
    "Siber Saldiri": ["Askeri Birlik"],
    # "Askeri Birlik" Deprem'de de öncelikli adaydır: DECISION_PROMPT_
    # TEMPLATE'daki "SİVİL-ASKERİ KOORDİNASYON KURALI" büyük ölçekli doğal
    # afetlerde askeri birlikleri makul bir tamamlayıcı arama-kurtarma
    # kapasitesi sayar; bu veri katmanı eşlemesi o kuralla hizalıdır.
    "Deprem": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma", "Agir Muhendislik", "Askeri Birlik"],
    "Sel": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma"],
    "Yangin": ["Itfaiye", "Saglik"],
    "Patlama": ["Itfaiye", "Saglik", "AFAD", "Arama Kurtarma"],
    # Her tür kendi gerçek müdahale profiline göre eşlenir — ör. bir
    # Heyelan'da asıl ihtiyaç molozu kaldırabilecek Ağır Mühendislik'tir,
    # genel sivil varsayılana (Itfaiye/Saglik/AFAD) düşürülmez.
    "Orman Yangini": ["Itfaiye", "Saglik"],
    "Cig": ["AFAD", "Arama Kurtarma", "Saglik", "Askeri Birlik"],
    "Heyelan": ["Agir Muhendislik", "AFAD", "Arama Kurtarma"],
    "Teror": ["Askeri Birlik", "Polis"],
    "Tahliye": ["AFAD", "Saglik", "Polis", "Lojistik"],
    "Salgin Hastalik": ["Saglik", "AFAD", "Lojistik"],
    "Izdiham": ["Saglik", "Polis", "AFAD"],
    # Gemi Kazasi/Tsunami: `UnitType.SAHIL_GUVENLIK` (bkz. o enum'un
    # docstring'i) burada ilk sırada yer alır — denizde/kıyıda asıl
    # birincil müdahale/arama-kurtarma yetkilisi gerçekte Sahil
    # Güvenlik'tir; Arama Kurtarma/Saglik/AFAD tamamlayıcı (kıyıya çıkan
    # yaralılar/genel kurtarma) olarak kalır.
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
"""Event.event_type -> öncelikli Unit.unit_type listesi (bkz. yukarıdaki
"YETENEK BAZLI ROTA" notu ve DECISION_PROMPT_TEMPLATE'daki "SİVİL-ASKERİ
KOORDİNASYON KURALI" ile AYNI sıralama mantığı)."""

_VARSAYILAN_ONCELIKLI_BIRIM_TIPLERI: List[str] = ["AFAD", "Arama Kurtarma", "Saglik", "Itfaiye"]
"""`_OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI`de eşleşmeyen bir `event_type` için
(ör. bilinmeyen/gelecekte eklenecek bir tür) güvenli, sivil-öncelikli
varsayılan.

`models.UnitType`'a eklenen `ARAMA_KURTARMA`/`LOJISTIK` üyeleri (bkz. o
enum'un docstring'i) yukarıdaki ve `_OLAY_TURU_ONCELIKLI_BIRIM_TIPLERI`
deki ilgili listelere AFAD'ın yanına eklenmiştir — aksi halde bu tipler
Pydantic doğrulamasından geçse bile Karar Motoru'nun öncelik
sıralamasında hiçbir zaman "birincil aday" olamaz, sadece geriye düşen
mesafe-bazlı sıralamada görünürlerdi."""

_GUVENLIK_BIRIM_TIPLERI: List[str] = ["Polis", "Askeri Birlik"]
""""2. plan": Emniyet/Askeri birimler birincil "MÜDAHALE EDECEK BİRLİK"
aramasına karışmaz; sadece çevre güvenliği/tahliye amaçlı, ayrı bir
sorguyla (bkz. `_guvenlik_birlikleri_bul`) sunulur."""

_YETENEK_ARAMA_YARICAPI_KM = turkiye_harita.yaricapi_operasyonel_sinira_kisitla(50.0)
"""Kullanıcının açıkça istediği "mesafe 50 km olsa bile" — doğru yeteneğe
sahip TEK bir birim bile olsa, onu bulmak için normal `_YOL_AGI_ARAMA_
YARICAPI_KM`den (25 km) DAHA GENİŞ aranır. `turkiye_harita.yaricapi_
operasyonel_sinira_kisitla` ile geçirilir — bkz. `_ARAMA_YARICAPI_KM`
tanımındaki AYNI "İL/İLÇE SİYASİ HARİTA KİLİDİ" notu."""

# Bir gercek sokak/caddenin GERCEK OSM verisinde onlarca ayri nokta-dugumu
# olabilecegi icin (bkz. `real_osm_loader.RealOsmLoader` — Street point-cloud
# mimarisi), "en yakin N alternatif" sorgularinda ayni caddenin farkli
# noktalarinin YANLISLIKLA N farkli "alternatif" gibi gorunmesini onlemek
# icin, tekillestirme ONCESI bu kadar KAT fazla aday cekilir (bkz.
# `_isim_bazinda_tekillestir`).
_ADAY_COKLUGU_KATSAYISI = 15


def _deger(kayit: Dict[str, Any], anahtar: str, varsayilan: str = "bilinmiyor") -> Any:
    """`kayit.get(anahtar, varsayilan)` gibi görünür ama KRİTİK bir farkla:
    Neo4j RETURN'ü, düğümde var olmayan bir özellik için `None` döner (ama
    `anahtar`ın KENDİSİ sözlükte hâlâ bulunur) — sıradan `.get(anahtar,
    varsayilan)` bu durumda varsayılanı DEĞİL, doğrudan `None`'ı döner. Bu
    `None`, GraphRAG bağlamına ("tonaj kapasitesi: None" gibi) ve oradan da
    AI Kurmay Başkanlığı'nın (bkz. `DECISION_PROMPT_TEMPLATE`) çıktısına
    HARFİYEN sızardı — sorun tanımındaki "None tonaj kapasitesi" TAM OLARAK
    budur. Bu yardımcı hem eksik ANAHTARI hem de `None` DEĞERİNİ aynı
    şekilde `varsayilan`a çevirir; varsayılan olarak "bilinmiyor" kullanılır
    (LLM'in context'te literal "None"/"—" görüp bunu ham haliyle tekrar
    etmesi yerine, doğal bir Türkçe ifadeyi paraphrase etmesi çok daha
    kolaydır).
    """
    deger = kayit.get(anahtar)
    return deger if deger is not None else varsayilan


def _temiz_isim(kayit: Dict[str, Any]) -> str:
    """"MÜDAHALE EDECEK BİRLİK"/"ÇEVRE GÜVENLİĞİ" bloklarında gösterilecek
    TEMİZ varlık adını döner: `en_yakin_ulasilan_varliklari_bul`in döndürdüğü BAZI
    GERÇEK OSM-kaynaklı `Unit` kayıtlarının (ör. isimsiz bir `amenity=fire_
    station` noktası) `aciklama` alanı BOŞ (`None`) OLDUĞUNDAN `isim`
    alanı TEK kimlik kaynağı olarak KALIYOR — ve bu, `real_osm_loader`ın
    benzersizlik için eklediği HAM teknik sonek de dahil olmak üzere HİÇ
    TEMİZLENMEDEN ("Itfaiye Istasyonu (OSM way/770031891)" gibi) doğrudan
    LLM'e, oradan da NİHAİ RAPORA sızıyordu — `gather_situational_picture`
    içindeki hasarlı-tesis/kapalı-yol sorgularında ZATEN uygulanan
    `COALESCE(aciklama, isim) + OSM imzalı adayları ele` düzeltmesiyle AYNI
    SINIF bir sorun, ama BİRİM-yönlendirme (routing) yolunda UYGULANMAMIŞTI.

    Çözüm: `OSM_TEKNIK_KIMLIK_IMZASI` (`"(OSM way/"`) ve sonrası `isim`den
    KIRPILIR — bu, Infrastructure sorgularındaki gibi adayı TAMAMEN ELEMEK
    yerine (bir Unit için "en yakın birlik" adayını KAYBETMEK, yanlış
    öneriye yol açardı) sadece GÖRÜNEN ismi temizler; kırpma sonrası hiçbir
    okunabilir metin KALMAZSA (ör. isim SADECE teknik imzadan oluşuyorsa)
    jenerik ama İNSAN-OKUR bir yer tutucu ("İsimsiz Saha Birimi") kullanılır
    — bu, ham teknik kimliği rapora sızdırmaktan HER ZAMAN daha iyidir."""
    isim = str(_deger(kayit, "isim", "")).strip()
    imza_konumu = isim.find(OSM_TEKNIK_KIMLIK_IMZASI)
    if imza_konumu != -1:
        isim = isim[:imza_konumu].strip()
    return isim or "İsimsiz Saha Birimi"


def _gercek_yetenek_aciklamasi(kayit: Dict[str, Any]) -> str:
    """"SAHTE YETENEK" GÜVENCESİ: `Unit.aciklama` alanının anlamı VERİ
    KAYNAĞINA göre DEĞİŞİR — `synthetic_unit_seeder.py` kaynaklı
    birimlerde GERÇEK bir yetenek özeti taşır (ör. "Yetenek: Enkaz Arama,
    Medikal, Çadır"), AMA `real_osm_loader.py`/`osm_loader.py` kaynaklı
    (gerçek OSM) birimlerde SADECE birimin TEMİZ ADINI taşır (bkz.
    `OSMBaselineLoader._convert_point_elements` — `aciklama=gercek_ad`) —
    YETENEK BİLGİSİ DEĞİLDİR. Bu ayrım yapılmazsa `aciklama` doğrudan
    "Yetenekleri: ..." diye sunulur; bu, bir polis noktasının kendi adının
    bir "yetenek" gibi gösterilmesine ve modelin gerçek bir yetenek verisi
    olmadığından "arama-kurtarma yeteneklerine sahip" gibi uydurma bir
    gerekçe icat etmesine yol açar.

    Çözüm: `aciklama`, `synthetic_unit_seeder.py`nin KULLANDIĞI "Yetenek:"
    ÖN EKİYLE başlıyorsa GERÇEK bir yetenek açıklaması sayılır ve OLDUĞU
    GİBİ döner; aksi halde (OSM'den gelen düz isim TEKRARI, veya hiç yoksa)
    dürüstçe "belirtilmemiş" döner — modelin BOŞ veriyi UYDURMAYLA
    doldurması yerine, GERÇEKTEN bilinmeyeni bilinmeyen olarak görmesi
    sağlanır."""
    aciklama = _deger(kayit, "aciklama", None)
    # NOT: "yetenek" kelimesi Türkçe'ye özgü aksan/şapka İÇERMEZ (y-e-t-e-n-
    # e-k tamamı ASCII) — tam bir `normalize_tr` (bkz. `nlp_parser.py`)
    # gerekmez, basit `.lower()` yeterlidir; bu modül `nlp_parser`ı KASITLI
    # OLARAK import ETMEZ (bkz. proje geneli "iki modül bağımsız kalsın" deseni).
    if isinstance(aciklama, str) and aciklama.strip().lower().startswith("yetenek"):
        return aciklama.strip()
    return "belirtilmemiş"


# "DİL KİLİDİ" GÜVENCESİ (kod seviyesi): prompt'un en sonunda bulunan
# "SADECE TÜRKÇE YAZ" talimatına rağmen, model bazen (özellikle prompt
# uzunluğu arttıkça) yine de İngilizce çıktı üretebilir; bu yüzden bir
# kod-seviyesi kontrolle desteklenir. Tam bir dil sınıflandırıcı DEĞİL, sadece "DİL
# KİLİDİ" kuralının İHLAL EDİLİP EDİLMEDİĞİNİ ucuza tespit eden kaba bir
# sezgiseldir (bkz. `generate_recommendations`daki TEK SEFERLİK yeniden
# deneme mekanizması).
_INGILIZCE_SUPHELI_KELIMELER = frozenset(
    {
        "the", "is", "are", "and", "of", "to", "for", "this", "that", "with",
        "recommendation", "recommendations", "route", "unit", "units", "given",
        "logistical", "here", "critical", "decision", "decisions", "tactical",
        "should", "would", "can", "will", "location", "crisis",
        # "deployment" gibi askeri jargon kelimeler, çeviri aşamasından
        # bile sağ çıkıp Türkçe metne (ör. "birlik deployment'i")
        # karışabilir — tam cümle İngilizce olmadığı için oran-tabanlı
        # sezgisel bunu tek başına yakalayamaz, ama listede olması yine de
        # belge/satır oranını yükselterek diğer İngilizce kelimelerle
        # birlikte tespiti güçlendirir.
        "deployment",
    }
)


def _tek_parca_ingilizce_supheli_mi(parca: str, esik: float, min_kelime: int) -> bool:
    """Tek bir metin parçasının (tam metin VEYA tek bir satır) İngilizce
    şüphesini kaba kelime-sıklığıyla ölçer — `_ingilizce_supheli_mi`nin
    hem belge-geneli hem satır-bazlı kontrolünün ORTAK çekirdeğidir."""
    kelimeler = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", parca.lower())
    if len(kelimeler) < min_kelime:
        return False
    supheli = sum(1 for k in kelimeler if k in _INGILIZCE_SUPHELI_KELIMELER)
    return (supheli / len(kelimeler)) > esik


def _ingilizce_supheli_mi(metin: str) -> bool:
    """`metin`in İngilizce İÇERDİĞİ (tamamen VEYA kısmen) OLASILIĞINI kaba
    bir kelime-sıklığı sezgisiyle tahmin eder (bkz. yukarıdaki "DİL KİLİDİ
    GÜVENCESİ" notu). Kısa/anlamsız metinlerde (10'dan az kelime) YANLIŞ
    ALARM vermemek için `False` döner.

    "KARIŞIK DİL" RİSKİ: SADECE belge-geneli oran kontrolü, İngilizce bir
    GİRİŞ CÜMLESİNİN ("Here are three critical tactical decision...")
    ardından gelen UZUN, çoğunlukla Türkçe (gerçek varlık isimleri/Türkçe
    kelimelerle dolu) bir gövde tarafından SEYRELTİLİP eşiğin ALTINA
    düşebilir — tüm metin Türkçeymiş gibi GEÇER, oysa İLK CÜMLE saf
    İngilizce olabilir. Çözüm: belge-geneli orana EK OLARAK, metin
    SATIR SATIR da taranır — TEK BİR satır bile yüksek İngilizce oranı
    taşıyorsa (daha KISA parçalarda tesadüfi gürültüyü elemek için biraz
    daha yüksek bir eşikle) tüm metin şüpheli sayılır. Böylece "önce
    İngilizce başla, sonra Türkçeye geç" kaçamağı KAPANIR.
    """
    if _tek_parca_ingilizce_supheli_mi(metin, esik=0.08, min_kelime=10):
        return True
    for satir in metin.splitlines():
        if _tek_parca_ingilizce_supheli_mi(satir, esik=0.15, min_kelime=5):
            return True
    return False


# "YABANCI SCRIPT" TESPİTİ — Latin ALFABESİ DIŞI karakter sızıntısı:
# `_ingilizce_supheli_mi` SADECE İngilizce kelime kalıplarını (Latin harfli)
# yakalar; farklı bir alfabeyle (ör. Çince/Kanji, Arapça, Kiril) yazılmış
# bir sızıntıyı YAKALAYAMAZ — bu tür karakterler Türkçe metinde ASLA meşru
# şekilde bulunmaz, bu yüzden bir oran eşiğine bile gerek yoktur: aşağıdaki
# Unicode blok aralıklarına düşen HERHANGİ bir karakter KESİN bir sinyaldir.
_YABANCI_SCRIPT_ARALIKLARI: Tuple[Tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs (Çince/Kanji/Hanja)
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x3040, 0x30FF),   # Hiragana + Katakana (Japonca)
    (0xAC00, 0xD7AF),   # Hangul Syllables (Korece)
    (0x0600, 0x06FF),   # Arapça
    (0x0400, 0x04FF),   # Kiril (Rusça vb.)
    (0x0900, 0x097F),   # Devanagari (Hintçe)
    (0x0E00, 0x0E7F),   # Tayca
)


def _yabanci_script_icerir_mi(metin: str) -> bool:
    """`metin`de Türkçe/Latin alfabesi DIŞINDA bir yazı sistemine (Çince,
    Arapça, Kiril, Korece, Japonca, vb. — bkz. `_YABANCI_SCRIPT_
    ARALIKLARI`) ait HERHANGİ bir karakter olup olmadığını kontrol eder.

    Kanıtlanmış bir boşluk için savunma: çok-dilli bazı modeller (ör.
    Qwen ailesi), düşük-kaynak bir dilde (Türkçe) belirsizlik arttığında
    ARA SIRA kendi ana pretraining dillerine (Çince) "kod değiştirebilir"
    — bu, `_ingilizce_supheli_mi`nin tasarlandığı "İngilizceye kayma"
    riskinden TAMAMEN FARKLI bir hata sınıfıdır ve o kontrol tarafından
    YAKALANAMAZ (Latin harf sıklığına dayanır, farklı bir alfabeyi hiç
    göremez). Bu fonksiyon, `_ingilizce_supheli_mi`nin AKSİNE bir ORAN
    eşiği KULLANMAZ — bu alfabelerden TEK bir karakter bile Türkçe bir
    taktiksel raporda meşru şekilde bulunamayacağından, varlığının
    KENDİSİ yeterli bir sinyaldir."""
    return any(
        any(alt <= ord(karakter) <= ust for alt, ust in _YABANCI_SCRIPT_ARALIKLARI)
        for karakter in metin
    )


def _dil_kilidi_ihlali_mi(metin: str) -> bool:
    """"DİL KİLİDİ" birleşik kontrolü: `metin` ya İngilizce şüpheliyse
    (bkz. `_ingilizce_supheli_mi`) YA DA Latin-dışı bir alfabe/script
    içeriyorsa (bkz. `_yabanci_script_icerir_mi`) `True` döner. Tüm
    çağıran taraflar (bkz. `_mikro_bolum_uret`), TEK BAŞINA
    `_ingilizce_supheli_mi` YERİNE bu birleşik kontrolü kullanır — böylece
    "SADECE TÜRKÇE YAZ" kuralının HANGİ yabancı dile/alfabeye ihlal
    edildiğinden BAĞIMSIZ olarak yakalanması garanti edilir. Üçüncü bileşen
    (bkz. `_sonda_yabanci_kelime_mi`) diğer ikisinin KAÇIRDIĞI, cümle
    SONUNA iliştirilmiş TEK bir yabancı kelimeyi de yakalar."""
    return _ingilizce_supheli_mi(metin) or _yabanci_script_icerir_mi(metin) or _sonda_yabanci_kelime_mi(metin)


def _format_temizle(metin: str) -> str:
    """Kod seviyesi BİÇİM güvencesi (bkz. "DİL KİLİDİ" ile AYNI ilke —
    "modele/prompt kuralına güvenmek YETMEZ"): GOREV KURALLARI kural 4,
    çıktının SADECE düz numaralı bir liste ("1. 2. 3.") olmasını, markdown
    başlık/kalın yazı/madde işareti/kod bloğu İÇERMEMESİNİ ister.

    Modele daha fazla inisiyatif alanı tanımak (daha yüksek `temperature`),
    yan etki olarak markdown biçimlendirme sızıntısını da artırabilir —
    model "1. Madde" yerine "**MADDE 1: Başlık**" + altında "* " işaretli
    alt maddeler üretebilir. Prompt kuralı (bkz. kural 4) bunu büyük ölçüde
    önler, ama yine de kod seviyesinde bir son temizlik uygulanır: kalın
    yazı (`**`) işaretleri ve markdown başlık
    (`#`) satır-başı işaretleri KALDIRILIR (metnin ANLAMI/İÇERİĞİ ASLA
    değiştirilmez, sadece dekoratif işaretler silinir); kod bloğu (```)
    çitleri de aynı şekilde temizlenir. Bu, TAM bir yeniden-yapılandırma
    (ör. "* " alt maddelerini "1." formatına zorlama) DEĞİLDİR — o kadar
    agresif bir dönüşüm içerik kaybına yol açabilir; burada SADECE en
    göze batan dekoratif markdown kaçakları temizlenir.
    """
    temizlenmis = re.sub(r"```(?:\w+)?", "", metin)
    temizlenmis = temizlenmis.replace("**", "")
    temizlenmis = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", temizlenmis)

    # "İÇ İSKELE ETİKETİ" SIZINTISI TEMİZLİĞİ (gözlemlenen gerçek hata):
    # "KRİZ NOKTASI:"/"MÜDAHALE EDECEK BİRLİK:" (bkz. `_MUDAHALE_BIRLIK_
    # ETIKETI`), `durum_ozeti`nin (modele VERİLEN GraphRAG bağlamı) SADECE
    # OKUNMASI için var olan iç iskele etiketleridir — model bunu ARA SIRA
    # kendi cevabına, sanki bir başlıkmış gibi, OLDUĞU GİBİ kopyalıyor (ör.
    # "MÜDAHALE EDECEK BİRLİK: Ankara UMKE Sahra Ekibi\n\n<gerçek cümle>").
    # Bu etiketle BAŞLAYAN bir satır, komutana sunulan METİNDE ASLA meşru
    # OLAMAZ (bu, `_kesin_birlik_ismi_uydurmasi_bul`in aradığı ETİKET, rapor
    # NESİRİ değil) — bu yüzden güvenle SİLİNİR; isim UYDURMA denetimi
    # (bkz. `_mikro_bolum_uret`) HER ZAMAN bu temizlikten ÖNCE, HAM cevap
    # üzerinde çalışır, bu yüzden burada silinmesi o denetimi ETKİLEMEZ.
    temizlenmis = re.sub(
        rf"(?im)^[ \t]*(?:{re.escape(_MUDAHALE_BIRLIK_ETIKETI)}|KRİZ NOKTASI)\s*[:：].*$\n?",
        "", temizlenmis,
    )
    temizlenmis = re.sub(r"\n{3,}", "\n\n", temizlenmis)

    # "BİTİŞİK KELİME" (glued-word) TEMİZLİĞİ (gözlemlenen gerçek hata:
    # "...içinKRİZ NOKTASına..."): küçük harfle biten bir kelimenin HEMEN
    # ardından (boşluksuz) 2+ büyük harfli bir kelime/etiket gelmesi, meşru
    # Türkçe metinde ASLA olmaz (yeni cümle/özel isim öncesi her zaman
    # boşluk/nokta bulunur) — bu yüzden bu kalıp modelin ARADA BOŞLUK
    # ATLADIĞININ güvenilir bir sinyalidir. Regex, ANLAMI DEĞİŞTİRMEDEN
    # sadece eksik boşluğu geri ekler.
    temizlenmis = re.sub(r"([a-zçğıöşü])([A-ZÇĞİÖŞÜ]{2,})", r"\1 \2", temizlenmis)

    # "PROMPT LEAKAGE" ÖN-TANITIM TEMİZLİĞİ (bkz. `_veri_referanssiz_mi` ile
    # AYNI sınıf, ama daha HAFİF bir belirti): model FORMAT A'yı
    # (numaralı liste) doğru üretse bile, önüne "Sakom Başkanı olarak, şu
    # senaryonun taktiksel raporunu oluşturacağım..." gibi kendini-tanıtan
    # bir ÖN CÜMLE ekleyebiliyor — bu da persona talimatının bir tür
    # sızıntılı yeniden-anlatımıdır. FORMAT A kullanıldıysa (satır başında
    # "1." işareti VARSA), bu işaretten ÖNCEKİ her şey ASLA meşru bir rapor
    # içeriği OLAMAZ (GOREV KURALLARI zaten "ilk kelime doğrudan içerik
    # olsun" der) — bu yüzden güvenle SİLİNİR.
    ilk_madde = re.search(r"(?m)^\s*1[\.\)][ \t]", temizlenmis)
    if ilk_madde and ilk_madde.start() > 0:
        temizlenmis = temizlenmis[ilk_madde.start():]

    # "MİKRO-GÖREV" KISA BAŞLIK SIZINTISI (bkz. modül-üstü "MİKRO-GÖREV
    # MİMARİSİ" notu): dar bir mikro-görev
    # prompt'u ("Lojistik analiz (TAM OLARAK 2 cümle):") bile bazen modelin
    # cevaba "Lojistik Analiz" gibi KISA bir başlık satırı EKLEMESİNE yol
    # açabiliyor — bu satır bir cümle DEĞİLDİR (nokta/ünlem/soru işaretiyle
    # BİTMEZ) ve hemen ardından BOŞ bir satır gelir. Böyle bir satır meşru
    # bir analiz içeriği OLAMAZ (görev "2 cümle" ister, bir başlık DEĞİL);
    # kısa (40 karakterden az) VE tümce-sonu noktalaması OLMAYAN İLK satır,
    # hemen ardından boş satır geliyorsa güvenle SİLİNİR.
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
    """GERÇEK OSM sokak ağı, bir cadde/sokağı ONLARCA ayrı nokta-düğümü
    olarak tutar (bkz. `real_osm_loader.RealOsmLoader`); bu yüzden `isim`
    (kapalı-yol/alternatif-rota sorgularında zaten `COALESCE(aciklama,
    isim)` ile GERÇEK/temiz cadde adına indirgenmiştir) bazında AYNI caddeye
    ait BİRDEN FAZLA satır gelebilir. Bu, hem GraphRAG bağlamında AYNI
    caddenin 20+ kez yinelenmesine (bağlamı şişirip modelin kafasını
    karıştırır) hem de "en yakın N alternatif" sorgularında AYNI caddenin
    farklı noktalarının N farklı alternatifmiş gibi görünmesine yol açardı.

    `kayitlar`ın ÇAĞIRAN TARAFINDAN anlamlı bir sırada (ör. mesafeye göre
    artan, ya da en güncel önce) verilmiş olması beklenir: bu fonksiyon her
    `isim` için İLK GÖRÜLEN (yani o sıralamaya göre EN İYİ) kaydı tutar.
    `adet` verilirse tekilleştirilmiş sonuç bu sayıyla sınırlanır.
    """
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


# ---------------------------------------------------------------------------
# "İL/İLÇE SİYASİ HARİTA KİLİDİ" (bkz. `turkiye_harita` modül docstring'i)
# ---------------------------------------------------------------------------


def _il_geofencing_kosulu(alias: str, konum_kaydi: Dict[str, Any]) -> Tuple[str, List[str]]:
    """`konum_kaydi`nin (bir kapalı yol/hasarlı tesis/kritik olay — `enlem`/
    `boylam` alanları taşıyan HERHANGİ bir kayıt) coğrafi konumuna göre,
    "en yakın birlik/açık yol/sağlam tesis" Cypher sorgularına eklenecek
    İKİNCİ, BAĞIMSIZ bir coğrafi kısıtlama üretir — km-yarıçapı filtresinin
    (bkz. `_ARAMA_YARICAPI_KM`/`_YOL_AGI_ARAMA_YARICAPI_KM`) YANI SIRA,
    adayın `bolge` (il) alanı olayın ilinin VEYA o ilin kara sınırı
    komşularının DIŞINDA ise ADAY TAMAMEN ELENİR (bkz. `turkiye_harita.
    izin_verilen_iller`) — "Rize'deki bir olaya İstanbul'dan birlik
    çağrılması" sınıfı bir hatayı, ham mesafe hesaplamasının ötesinde,
    Türkiye'nin GERÇEK siyasi/idari haritasına dayanarak engeller.

    `konum_kaydi.bolge IS NULL` (ör. henüz `bolge` etiketiyle yüklenmemiş
    eski/sentetik veri) olan adaylar GÜVENLİ VARSAYILAN olarak ELENMEZ —
    bu KISITLAMA DEĞİL, sadece "bu düğüm için il bilgisi YOK, km-yarıçapına
    güven" anlamına gelir (bkz. `models.BaseNode.bolge`nin opsiyonel
    doğası); aksi halde `bolge` alanı henüz doldurulmamış TÜM pilot-bölge
    verisi (ör. `synthetic_unit_seeder.py` kaynaklı bazı kayıtlar) bu
    kilit YÜZÜNDEN yanlışlıkla "erişilemez" sayılırdı.

    Döner: `(ek_where_parcasi, izinli_iller)` — `konum_kaydi`nin koordinatı
    yoksa VEYA hiçbir ile eşlenemiyorsa (`en_yakin_il` -> `None`) BOŞ
    (`"", []`) döner; bu durumda arama SADECE mevcut km-yarıçapı filtresine
    dayanmaya DEVAM eder (geofencing UYGULANAMAZ ama SESSİZCE İZİN de
    VERİLMEZ — km-yarıçapı zaten `MAKSIMUM_OPERASYON_YARICAPI_KM` ile
    sınırlıdır)."""
    olay_ili = turkiye_harita.en_yakin_il(konum_kaydi.get("enlem"), konum_kaydi.get("boylam"))
    izinli_iller = sorted(turkiye_harita.izin_verilen_iller(olay_ili))
    if not izinli_iller:
        return "", []
    return f"({alias}.bolge IS NULL OR {alias}.bolge IN $izinli_iller)", izinli_iller


# ---------------------------------------------------------------------------
# Anlik durum resmi (situational picture)
# ---------------------------------------------------------------------------


@dataclass
class SituationalPicture:
    """Bilgi Grafından çekilen, karar üretimine esas anlık saha durumu."""

    hasarli_tesisler: List[Dict[str, Any]] = field(default_factory=list)
    kapali_yollar: List[Dict[str, Any]] = field(default_factory=list)
    aktif_birlikler: List[Dict[str, Any]] = field(default_factory=list)
    kritik_olaylar: List[Dict[str, Any]] = field(default_factory=list)
    # Anahtar: kapali yolun/hasarli tesisin/kritik olayin `isim` alani.
    alternatif_rotalar: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    en_yakin_saglam_tesisler: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # "MEKANSAL KÖRLÜK" DÜZELTMESİ (bkz. `Neo4jConnection.
    # en_yakin_ulasilan_varliklari_bul`): anahtar bir kritik olayin/kapali
    # yolun `isim`i; deger, o noktadan SADECE AÇIK yol ağı üzerinden
    # GERÇEK Dijkstra yol mesafesiyle ulaşılabilen (kapalı yollarla izole
    # olanlar SESSİZCE elenmiş) en yakın birliklerin listesidir — artık
    # `aktif_birlikler`in HAM/mesafesiz listesi DEĞİL, bu alan LLM'e
    # sunulur (bkz. `format_durum_ozeti`).
    en_yakin_ulasilan_birlikler: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # "YETENEK BAZLI ROTA" (Capability-Based Routing):
    # anahtar AYNI (bir kritik olayin/kapali yolun `isim`i); değer, "2.
    # plan" — SADECE çevre güvenliği/tahliye amaçlı Emniyet/Askeri
    # birimlerin AYRI listesidir (bkz. `_guvenlik_birlikleri_bul`).
    # `en_yakin_ulasilan_birlikler`den KASITLI OLARAK AYRIDIR — LLM'e
    # "MÜDAHALE EDECEK BİRLİK" (birincil çözüm) ile "ÇEVRE GÜVENLİĞİ"
    # (ikincil/destek) rolleri KARIŞTIRILMADAN sunulsun diye.
    en_yakin_guvenlik_birlikleri: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Grafta kurmay başkanlığının değerlendireceği hiçbir kriz verisi yoksa True."""
        return not (self.hasarli_tesisler or self.kapali_yollar or self.kritik_olaylar)


def _durum_gercek_isimlerini_topla(durum: "SituationalPicture") -> List[str]:
    """`durum` içindeki TÜM gerçek varlık isimlerini (tesis/birlik/olay/yol
    + her olay/yol için bulunan en yakın birlik/rota isimleri) TEK düz bir
    listede toplar — bkz. `_veri_referanssiz_mi` docstring'i."""
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
    """Python'un standart `.lower()`'ı TÜRKÇE İ/I ayrımını
    BİLMEZ — İngilizce kuralına göre HEM 'İ' (U+0130) HEM 'I' (U+0049)
    harfini 'i' harfine çevirir. Bu yüzden modelin doğal olarak büyük
    harfle yazdığı "BULUNAMADI" (bkz. `_BULUNAMADI_ISARETI`), düz
    `.lower()` ile "bulunamadi" (noktalı i) olur — beklenen "bulunamadı"
    (noktasız ı, `_BULUNAMADI_YANIT_KELIMESI`) ile ASLA eşleşmez. Sonuç:
    model TAM OLARAK doğru/dürüst cevabı verdiğinde bile
    `_birlik_uydurma_suphesi_mi` bunu "şüpheli" sayıp GEREKSİZ bir
    düşük-sıcaklık yeniden denemeyi tetikleyebilir (ve o yeniden deneme,
    doğru cevabı daha kötü bir cevapla DEĞİŞTİREBİLİR). Bu fonksiyon
    Türkçe kuralını UYGULAR: 'İ' -> 'i', 'I' -> 'ı', SONRA gerisi
    standart `.lower()`'a bırakılır. `_veri_referanssiz_mi` VE
    `_birlik_uydurma_suphesi_mi`nin İKİSİ de kullanır ki aynı hata iki
    yerde AYRI AYRI tekrar etmesin."""
    return metin.replace("İ", "i").replace("I", "ı").lower()


def _veri_referanssiz_mi(metin: str, durum: "SituationalPicture") -> bool:
    """"PROMPT LEAKAGE" GÜVENCESİ (kod seviyesi): model bazen gerçek bir taktiksel
    rapor YERİNE, kendisine verilen SİSTEM KURALLARINI özetleyen/anlatan bir
    metin üretti, ör. "Bu rapor, SAKOM komutanına sunulacak bir taktiksel
    raporun beklentilerini belirleyen bir dizi kural ve rehberdir..." — bu
    metinde `durum_ozeti`de geçen TEK BİR gerçek tesis/birlik/olay ismi bile
    YOKTU). `_ingilizce_supheli_mi` gibi "prompt kuralına güvenmek YETMEZ"
    ilkesiyle AYNI sınıf bir güvence: `durum`daki GERÇEK varlık isimlerinden
    (bkz. `_durum_gercek_isimlerini_topla`) EN AZ BİRİNİN `metin` içinde
    GEÇİP GEÇMEDİĞİNİ kontrol eder — meşru bir taktiksel rapor, doğası
    gereği en az bir GERÇEK isim içerir (bkz. GOREV KURALLARI kural 2);
    HİÇBİRİ geçmiyorsa metin muhtemelen veriye dayanmayan bir kural-özeti/
    meta-konuşma hallüsinasyonudur. 4 karakterden KISA isimler (yanlış-
    pozitif riski) ve isimlerin OSM parantez soneki ATLANIR/temizlenir.
    `durum`da hiç isimlendirilebilir varlık yoksa (teorik olarak `is_empty()`
    zaten elemiş olur) güvenli varsayılan olarak `False` (şüpheli DEĞİL)
    döner."""
    isimler = _durum_gercek_isimlerini_topla(durum)
    if not isimler:
        return False
    metin_kucuk = _turkce_kucuk_harf(metin)
    for isim in isimler:
        temiz = isim.split("(")[0].strip()
        if len(temiz) >= 4 and _turkce_kucuk_harf(temiz) in metin_kucuk:
            return False
    return True


_BULUNAMADI_ISARETI = "BULUNAMADI"  # bkz. `_kriz_ve_mudahale_bloku`daki "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" biçimi
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
"""KATI KURAL (bkz. üç mikro-prompttaki AYNI başlıklı
kural): "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" iken SADECE 2. PLAN güvenlik
biriminin var olduğu durumlarda, modelin O BİRİMİ övücü/abartılı bir dille
(sanki gerçekten yetenek-eşleşmiş bir birincil müdahale gücüymüş gibi)
sunduğunu gösteren ifadeler — bkz. `_birlik_uydurma_suphesi_mi`daki
kullanım. Liste KASITLI OLARAK Lojistik/Sevk promptlarındaki YASAKLI
kelimelerle BİREBİR AYNIDIR (iki katman — prompt + kod — AYNI tanımı
paylaşsın diye)."""

_ABARTILI_KONTROLDEN_MUAF_BOLUMLER = frozenset({"Tahliye"})
"""`_birlik_uydurma_suphesi_mi`daki abartılı-ifade kontrolü BU bölüm(ler)
İÇİN uygulanmaz — Tahliye mikro-görevinin KENDİ, AYRI "ÖNCELİK KURALI"
(bkz. `_TAHLIYE_MIKRO_PROMPT_TEMPLATE`) 2. PLAN güvenlik birimlerini
KENDİ görevi için MEŞRU bir birincil kaynak sayar; bu durumda "uygun
yeteneklere sahip" gibi ifadeler HATA DEĞİL, doğru/beklenen davranıştır."""

_MUDAHALE_BIRLIK_ETIKETI = "MÜDAHALE EDECEK BİRLİK"
"""`format_durum_ozeti`nin (bkz. `_kriz_ve_mudahale_bloku`) GraphRAG
bağlamında kullandığı SABİT etiket ("MÜDAHALE EDECEK BİRLİK: <isim>" veya
"MÜDAHALE EDECEK BİRLİK: BULUNAMADI") — model bu etiketi genellikle KENDİ
ürettiği metne DE AYNEN kopyalar; bu, "modelin hangi birliği önerdiğini"
metinden güvenilir biçimde AYIKLAMAK için kullanılan bir çapa noktasıdır
(bkz. `_MUDAHALE_BIRLIK_ADI_PATTERN` ve `_birlik_uydurma_suphesi_mi`
içindeki "İSİM ÇAPRAZ DOĞRULAMA KATMANI")."""

_MUDAHALE_BIRLIK_ADI_PATTERN = re.compile(
    re.escape(_MUDAHALE_BIRLIK_ETIKETI) + r"\s*[:：]\s*([^\n.]+)"
)
"""`metin` içindeki HER "MÜDAHALE EDECEK BİRLİK: <isim>" iddiasını yakalar
(nokta/satır sonuna kadar) — `_birlik_uydurma_suphesi_mi`nin isim çapraz
doğrulama katmanı tarafından kullanılır."""


def _kesin_birlik_ismi_uydurmasi_bul(metin: str, durum_ozeti: str) -> Optional[str]:
    """`metin` içindeki HER "MÜDAHALE EDECEK BİRLİK: <isim>" iddiasını
    (bkz. `_MUDAHALE_BIRLIK_ADI_PATTERN`) `durum_ozeti` (modele VERİLEN
    GERÇEK GraphRAG bağlamı) ile çapraz doğrular; iddia edilen isim
    `durum_ozeti`nin HİÇBİR YERİNDE geçmiyorsa (ve "BULUNAMADI" iddiası
    DEĞİLSE) o ismi (KIRPILMIŞ gövde haliyle) döner — YOKSA `None`.

    `_birlik_uydurma_suphesi_mi` (retry TETİKLEME sinyali) İLE `_mikro_
    bolum_uret`teki "İKİNCİ DENEME DE UYDURDUYSA GÜVENLİ YER TUTUCUYA DÜŞ"
    SERT durağı (bkz. o fonksiyondaki AYNI başlıklı not) TARAFINDAN
    PAYLAŞILAN TEK bir çekirdek — iki farklı karar noktası AYNI tespiti
    İKİ KEZ farklı şekilde YENİDEN UYGULAMASIN diye ayrı bir fonksiyona
    çıkarılmıştır."""
    durum_ozeti_kucuk = _turkce_kucuk_harf(durum_ozeti)
    for eslesme in _MUDAHALE_BIRLIK_ADI_PATTERN.finditer(metin):
        iddia_edilen_isim = eslesme.group(1).strip().strip("*").strip()
        if not iddia_edilen_isim:
            continue
        if _BULUNAMADI_YANIT_KELIMESI in _turkce_kucuk_harf(iddia_edilen_isim):
            continue  # "BULUNAMADI" iddiasi -> gecerli, uydurma DEGIL.
        # Nitelik/mesafe eki ("(Tip: AFAD, ... 34 personel)" gibi) KIRPILIR —
        # sadece CIPLAK isim govdesi karsilastirilir; 4 karakterden KISA
        # govdeler (yanlis-pozitif riski) atlanir.
        isim_govdesi = iddia_edilen_isim.split("(")[0].strip()
        if len(isim_govdesi) >= 4 and _turkce_kucuk_harf(isim_govdesi) not in durum_ozeti_kucuk:
            return isim_govdesi
    return None


def _birlik_uydurma_suphesi_mi(metin: str, durum_ozeti: str, bolum_adi: str = "") -> bool:
    """""UYGUN BİRLİK YOKKEN İSİM UYDURMA" GÜVENCESİ (kod seviyesi, bkz. üç
    mikro-prompttaki "MUTLAK KURAL — UYGUN BİRLİK YOKSA"). "Prompt kuralına
    güvenmek YETMEZ" ilkesiyle (bkz. `_ingilizce_supheli_mi`/`_veri_
    referanssiz_mi`) AYNI sınıf bir güvence: modelin bu KESİN kurala RAĞMEN
    ara sıra "Akkol Birlik Komutanlığı" gibi TAMAMEN UYDURMA bir birlik ismi
    ürettiği GÖZLENDİ — bu fonksiyon, `durum_ozeti`de o olay için birlik "BULUNAMADI"
    yazdığı HALDE `metin`in beklenen "bulunamadı" ifadesini HİÇ içermediği
    durumu (güçlü bir uydurma sinyali) yakalayıp `_mikro_bolum_uret`in AYNI
    düşük-sıcaklık yeniden deneme mekanizmasını (bkz. yukarıdaki İKİ fonksiyon
    ile PAYLAŞILAN yol) tetikler.

    KUSURSUZ DEĞİLDİR ama GÖZLEMLENEN en yaygın hata kalıbını (kelimeyi
    TAMAMEN atlayıp doğrudan bir birlik icat etme) doğrudan hedefler —
    retry ORANI düşürür, %100 GARANTİ vermez.

    İKİNCİ KATMAN: bir metin, HEM "doğru yeteneğe sahip birlik
    bulunamadığından" DİYEBİLİR HEM DE aynı metinde 2. PLAN güvenlik birimi
    için "arama-kurtarma ve medikal destek için uygun yeteneklere
    sahiptir" diye EKLEYEBİLİR — basit "bulunamadı kelimesi var mı"
    kontrolü bunu KAÇIRIR, çünkü kelime GERÇEKTEN metinde vardır, sadece
    modelin KENDİSİYLE ÇELİŞEN bir ikinci cümlesi de vardır. Bu yüzden,
    "bulunamadı" kelimesi mevcut olsa BİLE, metin `_ABARTILI_YETENEK_
    IFADELERI`nden birini içeriyorsa YİNE şüpheli sayılır — SADECE Lojistik/
    Sevk için (bkz. `_ABARTILI_KONTROLDEN_MUAF_BOLUMLER` — Tahliye'nin
    KENDİ meşru istisnası bu kontrolden HARİÇ tutulur).

    "İSİM ÇAPRAZ DOĞRULAMA" KATMANI: `durum_ozeti` bir olay
    için SADECE "BULUNAMADI" derken (yani gerçekten hiçbir aday yokken),
    model metninin bir cümlesinde dürüstçe "bulunamadı" deyip BAŞKA bir
    cümlesinde "MÜDAHALE EDECEK BİRLİK: <isim>" diye TAMAMEN UYDURMA bir
    birlik icat edebilir. Yukarıdaki İKİNCİ KATMAN (`_ABARTILI_
    YETENEK_IFADELERI`) bunu her zaman YAKALAYAMAZ, çünkü kullanılan ifade
    o listedeki "abartılı övgü" kalıplarından biri OLMAYABİLİR (düz bir
    iddia cümlesi de olabilir). Bu üçüncü katman farklı/daha DOĞRUDAN bir strateji izler:
    modelin `_MUDAHALE_BIRLIK_ETIKETI` ("MÜDAHALE EDECEK BİRLİK:") ile
    BAŞLAYAN HER iddiasını tek tek yakalar (bkz. `_MUDAHALE_BIRLIK_ADI_
    PATTERN`) ve iddia edilen isim `durum_ozeti`nin (yani modele VERİLEN
    GERÇEK GraphRAG bağlamının) HİÇBİR YERİNDE geçmiyorsa — "BULUNAMADI"
    iddiası hariç — bunu KESİN bir uydurma sinyali sayar. Bu kontrol,
    "bulunamadı" kelimesinin metnin BAŞKA bir yerinde geçip geçmediğinden
    TAMAMEN BAĞIMSIZDIR (tam olarak bu yüzden, İKİNCİ KATMANIN kaçırdığı
    "hem doğru hem yanlış aynı anda söyleme" örüntüsünü de yakalar)."""
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
"""TEK, PAYLAŞILAN sabit metin: hem `_mikro_bolum_uret`in "ikinci deneme de
uydurdu" SERT durağı HEM DE `generate_recommendations`teki "LLM'e HİÇ
gitme" kısayolu (bkz. `_kategori_icin_gercek_birlik_var_mi`) AYNI dürüst/
güvenli cümleyi kullanır — iki ayrı yerde iki farklı metin YAZILIRSA,
komutana SEBEPSİZ YERE farklı ifadeli iki "bulunamadı" mesajı sunulabilir
(tutarsızlık izlenimi verir), bu yüzden TEK modül-seviyesi sabitte
toplanmıştır."""


def _kategori_icin_gercek_birlik_var_mi(durum: "SituationalPicture", guvenlik_de_sayilir: bool) -> bool:
    """Bir mikro-görev (Lojistik/Tahliye/Sevk) Ollama'ya HİÇ gönderilmeden
    ÖNCE, o kategori için Bilgi Grafında GERÇEKTEN (kod seviyesinde, LLM'in
    kendi muhakemesine hiç bırakılmadan) bulunmuş en az bir birlik olup
    olmadığını söyler — bkz. `generate_recommendations`teki kullanım.

    `guvenlik_de_sayilir=True` SADECE Tahliye kategorisi için geçilir (bkz.
    `_TAHLIYE_MIKRO_PROMPT_TEMPLATE`daki "ÖNCELİK KURALI": 2. PLAN güvenlik
    birimleri TAHLİYE için MEŞRU bir birincil kaynaktır); Lojistik/Sevk için
    `False` geçilir çünkü bu birimlerin o iki göreve özel bir yeteneği YOKTUR
    (bkz. `_guvenlik_birlikleri_bul` docstring'i).

    NEDEN BU KISAYOL GEREKLİ: bir kategori için gerçekten hiçbir birlik
    yoksa, LLM'e "belki bulursun" diye sormanın (a) gecikmeden başka hiçbir
    faydası yoktur VE (b) mikro-prompt'un "MÜDAHALE EDECEK BİRLİK:
    BULUNAMADI" ibaresini SIKÇA tekrarlaması, gözlemlenen bir halüsinasyon
    riskini (modelin bu ibareyi, GERÇEKTEN bir birlik bulunan BAŞKA bir
    krizde bile "yankılaması") artırır — bu fonksiyon, o riski TAMAMEN
    ortadan kaldırmak için LLM çağrısının kendisini atlar.

    "SADECE HASARLI TESİS" İSTİSNASI: `durum`da hiçbir kritik olay/kapalı
    yol YOKSA (SADECE hasarlı tesis varsa — `SituationalPicture.is_empty`
    bunu boş SAYMAZ), "birlik BULUNAMADI/bulundu" sorusunun KENDİSİ bu
    rapor için ANLAMSIZDIR (`_kriz_ve_mudahale_bloku` hiç üretilmemiştir,
    "MÜDAHALE EDECEK BİRLİK" kavramı devrede DEĞİLDİR) — bu durumda kısayol
    YANLIŞLIKLA devreye girip komutana "krize erişim sorunu var" gibi
    UYDURMA/ilgisiz bir mesaj sunmasın diye `True` (yani "LLM'e normal
    şekilde git") döndürülür."""
    if not (durum.kritik_olaylar or durum.kapali_yollar):
        return True
    if any(durum.en_yakin_ulasilan_birlikler.values()):
        return True
    return guvenlik_de_sayilir and any(durum.en_yakin_guvenlik_birlikleri.values())


def _yanlis_bulunamadi_iddiasi_mi(metin: str, durum_ozeti: str) -> bool:
    """"TERS YÖNLÜ ÇELİŞKİ" GÜVENCESİ (bkz. `_birlik_uydurma_suphesi_mi`nin
    TAM TERSİ yönü): o fonksiyon "gerçekten birlik YOKKEN model bir isim
    UYDURUYOR mu" sorusunu sorar; bu fonksiyon TERSİNİ sorar — GERÇEKTEN
    kullanılabilir bir birlik VARKEN model YİNE DE "uygun birlik bulunamadı"
    diye YANLIŞ bir iddiada bulunuyor mu.

    GÖZLEMLENEN GERÇEK HATA (bkz. ekran görüntüsü raporları): model bazen
    gerçek bir birliği (ör. "Ankara UMKE Sahra Ekibi ile 6.7 km mesafeden
    müdahale edecektir") DOĞRU şekilde adlandırdıktan HEMEN SONRA, aynı
    2 cümlelik yanıta KENDİSİYLE ÇELİŞEN "Uygun birlik bulunamadı, çevre
    illerden destek talep edilmelidir." cümlesini de EKLİYOR. Bu, mevcut
    hiçbir kontrol tarafından yakalanmıyordu: `_birlik_uydurma_suphesi_mi`
    SADECE `durum_ozeti`de "BULUNAMADI" GEÇTİĞİNDE devreye girer (satırın
    ilk koşuluna bkz.) — burada durum TERSİDİR (veri bir birlik GÖSTERİYOR),
    bu yüzden o fonksiyon bu deseni yapısal olarak HİÇ göremez.

    `durum_ozeti`de "BULUNAMADI" GEÇMİYORSA (yani en azından TEK bir olay
    için bile gerçekten birlik bulunmuşsa) VE model yine de "birlik" +
    "bulunamadı" kelimelerini birlikte kullanıyorsa, bu KESİN bir çelişki
    sinyalidir. `durum_ozeti=""` geçilmesi (bkz. `_mikro_bolum_uret`teki
    kullanım) kontrolü KOŞULSUZ AKTİF eder — bu fonksiyonun TEK çağıranı
    ZATEN SADECE `_kategori_icin_gercek_birlik_var_mi` `True` döndürdüğünde
    (yani BU KATEGORİ için gerçekten bir birlik bulunduğu KOD SEVİYESİNDE
    KANITLANMIŞKEN) çalışır — bu durumda `durum_ozeti`nin (raporun İÇİNDEKİ
    BAŞKA/İLGİSİZ bir olay/kapalı-yol bloğu yüzünden) BAŞKA bir yerde
    "BULUNAMADI" içermesi ARTIK ALAKASIZDIR; koşulsuz kontrol, "bu SPESİFİK
    kategori için biliniyoruz ki birlik VAR" garantisini birebir yansıtır ve
    çok-olaylı raporlarda YANLIŞ NEGATİF (gerçek çelişkinin kaçırılması)
    riskini ORTADAN KALDIRIR."""
    if durum_ozeti and _BULUNAMADI_ISARETI in durum_ozeti:
        return False  # En az bir olay icin GERCEKTEN birlik yok - "bulunamadi" demek burada MESRU olabilir.
    metin_kucuk = _turkce_kucuk_harf(metin)
    return _BULUNAMADI_YANIT_KELIMESI in metin_kucuk and "birlik" in metin_kucuk


def _bulunan_ilk_birlik_ile_guvenli_cumle(durum: "SituationalPicture", guvenlik_de_sayilir: bool) -> str:
    """`_yanlis_bulunamadi_iddiasi_mi` düşük-sıcaklıklı yeniden denemede BİLE
    `True` çıkarsa (model ısrarla GERÇEKTEN bulunan bir birliği "bulunamadı"
    diye YANLIŞ inkâr ederse) kullanılan SON çare: LLM'in serbest metnine
    HİÇ güvenmeden, Bilgi Grafında GERÇEKTEN bulunan İLK birliğin adını/
    mesafesini doğrudan koddan (f-string ile) yazar.

    `_kesin_birlik_ismi_uydurmasi_bul`in "İKİNCİ DENEME DE UYDURDUYSA GÜVENLİ
    YER TUTUCUYA DÜŞ" sert durağıyla AYNI ilkenin (LLM'in serbest metnine
    güvenilemeyen bir durumda kod-seviyesi bir gerçeğe geri dönülmesi) TERS
    yöndeki (fabrikasyon yerine yanlış inkâr) karşılığıdır. Bu fonksiyon
    SADECE `_kategori_icin_gercek_birlik_var_mi` zaten `True` döndürdüğü için
    çağrılır — yani en az bir birlik bulunması GARANTİDİR; yine de teorik
    bir yarış durumuna karşı güvenli bir sabit döner."""
    kaynaklar = [durum.en_yakin_ulasilan_birlikler]
    if guvenlik_de_sayilir:
        kaynaklar.append(durum.en_yakin_guvenlik_birlikleri)
    for kaynak in kaynaklar:
        for birlikler in kaynak.values():
            if birlikler:
                b = birlikler[0]
                return f"{_temiz_isim(b)} ile {b.get('mesafe_km', '?')} km mesafeden müdahale edilecektir."
    return _SABIT_BULUNAMADI_CUMLESI  # teorik olarak imkansiz dal (bkz. docstring).


_SAYI_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def _sayisal_uydurma_suphesi_mi(metin: str, referans_metin: str) -> bool:
    """"KESİN SAYISAL KURAL" (her 3 mikro-prompt'ta da tekrarlanan: "veride
    GEÇMEYEN HİÇBİR sayı/miktar UYDURMA") için kod-seviyesi güvence — bkz.
    `_ingilizce_supheli_mi`/`_veri_referanssiz_mi` ile AYNI "prompt kuralına
    güvenmek YETMEZ" ilkesi. Bu kural, projenin BAŞKA HİÇBİR sayısal
    kuralı için OLMAYAN tek istisnaydı (SADECE prompt metninde vardı, kod
    seviyesinde HİÇ doğrulanmıyordu) — gözlemlenen gerçek hata (model, veride
    OLMAYAN bir "50 sivili" rakamı UYDURDU) bu boşluğu doğrudan gösterdi.

    `metin`deki HER sayı, `referans_metin`de (durum_ozeti + çevresel istihbarat
    metni — İKİSİ DE modele GERÇEKTEN VERİLEN veridir) BİREBİR (string olarak)
    geçmelidir; geçmeyen bir sayı varsa bu UYDURULMUŞ bir miktar sinyalidir.
    Küçük/genel sayılar (ör. "2" — "2. PLAN" başlığından bile kaynaklanabilir)
    yanlış-pozitif riski taşıdığından, SADECE 2+ haneli VEYA ondalıklı sayılar
    kontrol edilir (tek haneli bir sayı hem çok sık rastgele eşleşir hem de
    yanlış alarm maliyeti, kaçırılan gerçek bir uydurmadan daha yüksektir)."""
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
"""`_ingilizce_supheli_mi`nin belge-geneli/satır-bazlı ORAN eşiği, Türkçe
bir cümlenin SONUNA iliştirilmiş TEK bir yabancı kelimeyi (gözlemlenen
gerçek hata: "...destek talep edilmeli light.") YAKALAYAMAZ — kısa bir
mikro-görev cevabında tek kelime, oranı eşiğin altında bırakır. Liste
KASITLI OLARAK dar/kısa tutulur (yanlış-pozitif riskini düşük tutmak için,
bkz. `_sonda_yabanci_kelime_mi`)."""


def _sonda_yabanci_kelime_mi(metin: str) -> bool:
    """`metin`in SON kelimesini (noktalama temizlenmiş) `_SONDA_SUPHELI_
    INGILIZCE_KELIMELER`le karşılaştırır — bkz. o sabitin docstring'i."""
    kelimeler = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", metin)
    if not kelimeler:
        return False
    return kelimeler[-1].lower() in _SONDA_SUPHELI_INGILIZCE_KELIMELER


# ---------------------------------------------------------------------------
# Cikti (oneri metni) ayristirma
# ---------------------------------------------------------------------------


# Bir madde/liste satırının BAŞLANGICI: "1." / "1)" / "-" / "•". Digit+nokta
# icin, ardindan bosluk/satir sonu gelme sarti aranir (lookahead) ki "3.5 km"
# gibi bir ondalik sayi yanlislikla madde isareti sanilmasin.
_MADDE_BASLANGIC = r"(?:\d+[\.\)](?=[ \t]|$)|[-•])"

# Modelin talimata ragmen ekleyebildigi bir markdown baslik/on soz satirini
# (ör. "# Kriz Yönetim Kurmay Başkanısın Önerileri") madde saymamak icin,
# eslesme SADECE gercek bir madde isaretiyle BASLAYAN bloklari yakalar; boyle
# bir on soz varsa (ilk madde isaretinden ONCEKI kisim) tamamen ATLANIR.
_MADDE_PATTERN = re.compile(
    rf"^[ \t]*{_MADDE_BASLANGIC}[ \t]*(.+?)(?=\n[ \t]*{_MADDE_BASLANGIC}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_oneri_maddeleri(metin: str) -> List[str]:
    """LLM'in ürettiği "1. ... 2. ... 3. ..." biçimindeki numaralı düz metni,
    her biri bir taktiksel karar önerisi olan bir string listesine ayırır.

    Modelin biçimlendirmesi (madde işareti "1." / "1)" / "-" gibi) küçük
    varyasyonlar gösterebileceğinden ayrıştırma toleranslıdır. Model, açık
    talimata rağmen bazen bir markdown başlığı/ön söz satırı ekleyebilir
    (ör. "# Kriz Yönetim Kurmay Başkanısın Önerileri"); bu tür bir satır bir
    madde işaretiyle BAŞLAMADIĞI için madde olarak SAYILMAZ ve atlanır — bu
    sayede öneri kutuları arasına sahte bir "0. madde" sızmaz. Hiçbir gerçek
    madde tespit edilemezse (ör. model tamamen serbest bir paragraf
    döndürdüyse) tüm metin TEK bir madde olarak geri döner ki çağıran taraf
    hiçbir şey kaybetmesin.
    """
    if not metin or not metin.strip():
        return []

    temiz_metin = metin.strip()
    maddeler = [
        eslesme.group(1).strip()
        for eslesme in _MADDE_PATTERN.finditer(temiz_metin)
        if eslesme.group(1).strip()
    ]
    return maddeler if maddeler else [temiz_metin]


# ---------------------------------------------------------------------------
# "MİKRO-GÖREV" (Micro-Tasking) MİMARİSİ
# ---------------------------------------------------------------------------
# Taktiksel öneri, 3 BAĞIMSIZ/paralel dar prompt (Lojistik/Tahliye/Sevk)
# ile üretilir; her madde birbirinden habersiz, ayrı bir çağrıyla
# oluşturulduğundan aynı içeriğin 3 madde boyunca tekrarlanması ("papağan
# modu") yapısal olarak imkânsızdır. Alternatif olarak değerlendirilen
# sıralı/hiyerarşik bir "Çoklu Ajan" (İstihbarat -> Harekat -> Komutan)
# tasarımı, gerçek bir komuta zinciri metaforu olarak kavramsal olarak
# daha doğru olsa da iki ciddi dezavantaj taşır: (1) sıralı bağımlılık
# zinciri gecikmeyi artırır (paralelleştirme yapısal olarak imkânsız
# hale gelir); (2) üst kademenin alt raporları TEK bir çağrıda
# sentezlemesi, kısıtlı girdi malzemesi yüzünden "papağan modu"nu geri
# getirebilir. Bu yüzden bağımsız mikro-görev mimarisi tercih edilir;
# buna ek olarak iki güvence uygulanır:
#   (a) SAYISAL HALÜSİNASYON KİLİDİ — model veride olmayan rakamlar
#       ("10 ambulans, 5 araç" gibi) uydurmasın diye HER 3 mikro-görevin
#       promptuna da eklenmiştir (aşağıya bak).
#   (b) OSM İSİM TEMİZLEYİCİ (`_temiz_isim`, bkz. o fonksiyonun docstring'i)
#       `_kriz_ve_mudahale_bloku`da (durum_ozeti'nin KENDİSİNİ üreten
#       paylaşılan kod yolu) yaşar — mimari ne olursa olsun HER promptun
#       gördüğü veri ZATEN temizdir.
# `num_predict`/`num_ctx` sınırları (bkz. `__init__`) dar bir görevde bile
# "gevezelik" riskine karşı ucuz bir güvencedir.
#
# "FAZ 3: DİNAMİK SAHA VE ÇEVRESEL İSTİHBARAT" EKLENTİSİ (kullanıcı
# talebi): Dijkstra en kısa yolu bulur ama "o yol şu an YÜRÜNEBİLİR mi"
# sorusuna hiç bakmaz — HER 3 mikro-görev promptuna, `src.core.
# environmental_context.cevresel_durumu_getir`den gelen bir "# ÇEVRESEL
# İSTİHBARAT" bloğu (`{cevresel_durum}`) eklendi. BİLİNÇLİ MİMARİ SINIR
# (bkz. o modülün docstring'i): bu veri LLM'e bir "ASKERİ DİREKTİF" olarak
# SUNULUR (ör. "sağanak varsa paletli araç seç") — kod seviyesinde HİÇBİR
# sert filtre/zorlama UYGULANMAZ, karar YİNE modele aittir; bu, projenin
# `_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI` gibi KESİN kod-kurallarından BİLİNÇLİ
# olarak FARKLI bir tasarım tercihidir (hava-yetenek eşleştirmesi çok
# nüanslı olduğundan sert bir tabloya indirgenmez).
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

# "DİL KİLİDİ" GÜVENCESİ — SON ÇARE (bkz. `DecisionEngine._turkceye_cevir`):
# model hem ilk hem yeniden deneme çağrısında da İngilizce ürettiğinde,
# TAMAMEN FARKLI ve BASİT bir görevle ("bunu Türkçeye çevir") SON bir
# deneme yapılır. Bir modelin "Türkçe yaz VE onlarca kurala uy" gibi
# KARMAŞIK, çok-kurallı bir görevde başarısız olsa bile, "sadece bunu
# çevir" gibi TEK-AMAÇLI, basit bir görevde başarılı olma ihtimali çok
# daha yüksektir — bu, prompt karmaşıklığının KENDİSİNİN İngilizceye
# kaymanın bir nedeni olabileceği gözlemine dayanır.
#
# TASARIM NOTU #1 (serbest-metin çeviri şablonu yerine JSON şeması): düz
# "# ÇEVRİLECEK METİN" / "# TÜRKÇE ÇEVİRİ" başlıklı bir serbest-metin
# şablonu, llama3'ün bu başlıkları ve orijinal İngilizce girdiyi çıktısına
# aynen kopyalamasına (echo) açıktır; sonuç hâlâ İngilizce metin
# içerdiğinden "Dil Kilidi" kontrolünü geçemez — çeviri başarılı olsa bile
# gizlenir. Çözüm: `OllamaParser`ın `format="json"` ile markdown/prose
# sızıntısını önlediği AYNI teknik burada da kullanılır (bkz. `__init__`
# daki `self._ceviri_llm`) — JSON şeması, modelin serbest metin echo
# etmesini çok daha zor kılar ve çeviriyi TEK, doğrudan çıkarılabilir bir
# alana (`turkce_metin`) sıkıştırır.
#
# TASARIM NOTU #2 (JSON şablonunda köşeli-parantez yer tutucusu yerine
# açık kural): `{{"turkce_metin": "<TAM ÇEVİRİ BURAYA>"}}` şeklindeki
# köşeli-parantez yer tutucusu, çok paragraflı/numaralı-liste girdilerde
# llama3'ü yanlış yönlendirebilir: model `< >` işaretlerini söz diziminin
# bir parçası sanıp içine tam çeviri yerine kısa bir özet yazabilir (ör.
# 3 paragraflık bir rapor tek cümlelik bir özete indirgenir) — hem anlam
# kaybı hem de JSON'un erken kesilmesi (kapanış tırnak/parantezi eksik
# kalması, `json.loads` hatası) riskini doğurur. Çözüm: yer tutucu
# tamamen kaldırılır; onun yerine kısaltma/özetlemeyi
# AÇIKÇA YASAKLAYAN ve "TAM, EKSİKSİZ, PARAGRAF PARAGRAF" çeviri isteyen
# net bir kural eklendi, ayrıca `__init__`de `num_predict` UZUN metinlerin
# kesilmemesi için CÖMERTÇE artırıldı (bkz. `self._ceviri_llm`).
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

# ÜÇÜNCÜ (SON) KADEME — "UI'A ASLA İNGİLİZCE METİN DÜŞMESİN" ZORUNLULUĞU:
# JSON-şemalı çeviri (yukarıdaki `_CEVIRI_PROMPT_TEMPLATE`) de başarısız
# olursa (ör. `format="json"` yine de bozuk/yarım bir JSON üretirse),
# TAMAMEN SERBEST METİN, TEK-CÜMLELİK ve JSON İÇERMEYEN bu EN BASİT
# görevle SON bir deneme yapılır — ne kadar az kısıt/şema varsa modelin
# görevi doğru anlama ihtimali o kadar yüksektir (bkz. `_turkceye_cevir`
# docstring'indeki gözlem). Bu kademe de başarısız olursa (hâlâ İngilizce
# veya boş), `generate_recommendations` İngilizce metni GÖRÜNÜR şekilde
# bırakır ve hatayı loglar — kullanıcıya SESSİZCE bozuk/boş bir sonuç
# ASLA sunulmaz.
_CEVIRI_SERBEST_METIN_PROMPT_TEMPLATE = """Translate the following text to Turkish. Output ONLY the complete \
Turkish translation and nothing else — no headers, no notes, no English, no summary, no markdown. \
Translate ALL of it, do not shorten it.

{ingilizce_metin}"""


class DecisionEngine:
    """Bilgi Grafını tarayıp Ollama üzerinden taktiksel karar önerisi üreten
    Karar Destek Motoru.

    Kullanım:
        engine = DecisionEngine(Neo4jConnection())
        sonuc = engine.generate_recommendations()
        sonuc["oneriler_metni"]   # LLM'in ham (numaralı liste) yanıtı
        sonuc["durum_ozeti"]      # Modele verilen GraphRAG bağlamı (debug/UI icin)
        sonuc["durum"]            # SituationalPicture (yapisal veri)
    """

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

        # Not: format="json" KULLANILMAZ — bu motorun çıktısı yapılandırılmış
        # (Pydantic) bir varlık değil, komutana sunulacak serbest metin bir
        # taktiksel öneridir (bkz. modül-üstü "MİKRO-GÖREV MİMARİSİ" notu).
        # `num_predict`/`num_ctx`: `num_predict` açıkça belirtilmediğinde
        # Ollama'nın varsayılanı bazı sürümlerde SINIRSIZDIR (-1), yani
        # model doğal bir durma belirtecine ulaşamazsa çağrı bağlam
        # sınırına kadar sürünebilir. Her mikro-görev zaten dar/kısa (2-4
        # cümle) olduğundan, cömert ama sınırlı bir tavan (512 token) en
        # kötü durum gecikmesini garanti altına alır; `num_ctx` da
        # durum_ozeti'nin rahatça sığacağı ama gereksiz büyük olmayacağı
        # bir değere (4096 — Q8_0 quantization'ın 12 GB VRAM'e TAM olarak
        # GPU'da sığması için bilinçli olarak sınırlanmıştır; CPU'ya
        # taşma, özellikle 3 paralel mikro-görev eşzamanlı çalışırken
        # ciddi bir gecikme riskidir) sabitlenir.
        # `num_gpu`/`num_thread` (bkz. modül-üstü `_OLLAMA_NUM_GPU`/
        # `_OLLAMA_NUM_THREAD` docstring'i): modelin TÜM katmanlarını
        # CUDA'ya zorlar (Ollama'nın kendi -1/"otomatik" varsayılanına
        # güvenmez) ve kalan CPU-taraflı işi (prompt/tokenize) bu
        # makinenin gerçek çekirdek sayısına göre paralelleştirir.
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

        # "MİKRO-GÖREV" MİMARİSİ: TEK dev prompt/zincir YERİNE, 3 BAĞIMSIZ
        # (birbirinin çıktısını GEREKTİRMEYEN) dar-görevli prompt/zincir —
        # bkz. modül-üstü `_LOJISTIK_MIKRO_PROMPT_TEMPLATE`/`_TAHLIYE_MIKRO_
        # PROMPT_TEMPLATE`/`_SEVK_MIKRO_PROMPT_TEMPLATE` docstring'i. Üçü de
        # AYNI `self.llm` örneğini (AYNI `temperature`) paylaşır — sadece
        # PROMPT'LARI farklıdır; ayrı ayrı model örneği açmanın (bellek/
        # yükleme maliyeti) gereksiz olduğu, tek bir yerel Ollama
        # sunucusunun farklı promptlarla art arda çağrılmasının yeterli
        # olduğu bir durumdur. Bu 3 çağrı birbirinin çıktısını GİRDİ olarak
        # ALMAZ — her biri SIFIRDAN, SADECE `durum_ozeti`den kendi dar
        # görevini üretir; bu hem gecikmeyi (sıralı BAĞIMLILIK yok)
        # düşürür hem "papağan modu"nu (3 maddenin birbirini taklit
        # etmesi) YAPISAL olarak İMKANSIZ kılar.
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

        # "DİL KİLİDİ" GÜVENCESİ — SON ÇARE zinciri (bkz. `_CEVIRI_PROMPT_
        # TEMPLATE` ve `_turkceye_cevir`). `self.llm`den (serbest metin)
        # FARKLI OLARAK `format="json"` KULLANILIR — `OllamaParser`daki
        # (bkz. `nlp_parser.py`) AYNI teknik: JSON şeması, modelin
        # orijinal İngilizce girdiyi/başlıkları ÇIKTISINA KOPYALAMASINI
        # (echo) çok daha zor kılar ve çeviriyi TEK bir alana sıkıştırır
        # (bkz. modül-üstü "TASARIM NOTU" bölümü).
        # `num_predict`: Ollama'nın varsayılan token üretim sınırı, UZUN
        # (çok paragraflı/numaralı listeli) raporların JSON çevirisini
        # yarıda kesebileceğinden CÖMERTÇE artırılır — kısa bir çeviri için
        # fazladan bütçe zararsızdır, ama YETERSİZ bütçe her seferinde
        # bozuk/yarım JSON'a (ve dolayısıyla İngilizce'nin UI'a sızmasına)
        # yol açar. `num_ctx` da benzer şekilde cömert tutulur (girdi UZUN
        # olabilir — orijinal İngilizce metin + prompt + çıktı hepsi
        # AYNI bağlam penceresine sığmalı), ama Q8_0'ın 12 GB VRAM'e TAM
        # olarak GPU'da sığması için 4096 ile sınırlanır (bkz. `self.llm`
        # örneğindeki AYNI gerekçe).
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

        # ÜÇÜNCÜ (SON) KADEME (bkz. `_CEVIRI_SERBEST_METIN_PROMPT_TEMPLATE`
        # docstring'i): AYRI bir LLM örneği — `format="json"` KULLANILMAZ
        # (bu kademenin TÜM amacı JSON şemasının KENDİSİNİN başarısız
        # olduğu durumu kurtarmaktır), ama aynı cömert `num_predict`/
        # `num_ctx` bütçesi korunur.
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

        # "GÜVENCE" — SON ÇARE zinciri (bkz. `_mikro_bolum_uret`daki
        # kullanımı): her mikro-görev İngilizce/veri-referanssız çıkarsa,
        # AYNI prompt DÜŞÜK sıcaklıkla (0.05) TEK bir kez daha denenir —
        # düşük sıcaklık modelin prompt'un dar/basit talimatına daha sıkı
        # sadık kalmasını teşvik eder. Mikro-görev prompt'ları ZATEN dar/
        # kısa olduğundan (bkz. modül-üstü "MİKRO-GÖREV MİMARİSİ" notu), bu
        # son çarenin gerekmesi NADİR beklenir — ama "prompt kuralına
        # güvenmek yetmez" ilkesi gereği YİNE DE tutulur. `num_predict`/
        # `num_ctx` sınırları KORUNUR — dar bir
        # görevde bile modelin ARA SIRA doğal bir durma noktasına
        # ulaşamayıp gecikmeyi katlaması riskine karşı ucuz bir güvence. ÜÇ
        # mikro-görev AYNI düşük-sıcaklık LLM örneğini paylaşır (sadece
        # promptları farklıdır).
        self._zemin_llm = ChatOllama(
            model=self._model_name, base_url=self._base_url, temperature=0.05,
            num_predict=512, num_ctx=4096,
            num_gpu=_OLLAMA_NUM_GPU, num_thread=_OLLAMA_NUM_THREAD,
            timeout=_OLLAMA_ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        self._lojistik_zemin_chain = self._lojistik_prompt_template | self._zemin_llm | StrOutputParser()
        self._tahliye_zemin_chain = self._tahliye_prompt_template | self._zemin_llm | StrOutputParser()
        self._sevk_zemin_chain = self._sevk_prompt_template | self._zemin_llm | StrOutputParser()

    # ------------------------------------------------------------------ #
    # Genel kullanim (public API)
    # ------------------------------------------------------------------ #

    def generate_recommendations(
        self,
        bolge: Optional[str] = None,
        odak_koordinatlari: Optional[List[Tuple[float, float]]] = None,
        paralel_calistir: bool = False,
    ) -> Dict[str, Any]:
        """Anlık graf durumunu toplar ve Ollama'dan 3 maddelik taktiksel
        öneri üretir.

        `bolge` verilirse SADECE o bölgeye (bkz. `models.BaseNode.bolge`)
        ait veriler değerlendirilir; `src.ui.app` bu parametreyi kullanmaz
        (tüm UI çağrıları `bolge=None` varsayılanıyla tüm şehirleri
        birlikte değerlendirir — bir Karar Destek Sistemi'nin doğası
        gereği bölgeler arası koordinasyona ihtiyaç duyulabilir). Parametre,
        programatik/betik kullanımı (ör. tek bir şehri izole test etmek)
        için opsiyonel bir yetenek olarak sunulur.

        `odak_koordinatlari`: birden fazla aktif kriz aynı anda varken,
        kısa (2 cümlelik) mikro-görev promptlarının TÜM olayları TEK bir
        anlatıda karıştırıp yanlış birlik/mesafe eşleştirmesi üretmesini
        önler. "Ülke çapında birlik ara" (arama kapsamı) ile "ülke
        çapındaki her olayı tek raporda anlat" (anlatım kapsamı) ayrı
        kaygılardır — bu parametre sadece ikincisini daraltır.
        `/api/analyze-crisis`, o çağrıda yazılan Event(ler)in koordinatını
        buraya iletir; verilirse `gather_situational_picture`, anlatıyı
        SADECE bu olay(lar)a indirger — birlik arama kapsamı (ülke
        çapında en iyi/en yakın birliği bulma) değişmeden kalır, sadece
        hangi olay(lar)ın anlatılacağı daralır.

        `paralel_calistir`: Ollama bu makinede eşzamanlı istekleri
        destekler (3 ardışık çağrı yerine paralel çalıştırıldığında
        belirgin bir hızlanma sağlar). `/api/analyze-crisis` (FastAPI) bu
        parametreyi `True` ile çağırarak paralelliği kullanır; `src.ui.app`
        (Streamlit) ise varsayılan `False` (koşulsuz sıralı) ile çağırır —
        Streamlit'in kendi çalışma modeliyle (`ScriptRunContext`'in worker
        thread'lere taşınmaması) yaşanabilecek bir etkileşimi önlemek
        içindir. İki tüketici tamamen izoledir, biri diğerini etkilemez.

        Grafta hiçbir kriz verisi yoksa (bkz. `SituationalPicture.is_empty`)
        Ollama'ya HİÇ gidilmez — boş bir durum için "öneri" üretmek anlamsız
        ve yanıltıcı olurdu; bunun yerine `durum_bos: True` döner.

        "COĞRAFİ ÖN-KONTROL" (Sanity Check): durum boş DEĞİLSE, Ollama'ya
        gitmeden ÖNCE her kritik olay `turkiye_harita.cografi_on_kontrol`
        den geçirilir — "İç Anadolu'da Tsunami" sınıfı FİZİKSEL OLARAK
        İMKANSIZ bir öncül tespit edilirse, LLM'e HİÇ gidilmeden
        `cografi_red: True` ile ANINDA reddedilir (bkz. aşağıdaki kod ve
        o fonksiyonun docstring'i — SADECE net/tartışmasız çelişkiler
        reddedilir, sınırda durumlar LLM'in muhakemesine kalır).

        "MİKRO-GÖREV" MİMARİSİ (bkz. modül-üstü not): 3 madde TEK bir dev
        LLM çağrısıyla DA, birbirine bağımlı ARDIŞIK bir ajan zinciriyle DE
        DEĞİL — 3 BAĞIMSIZ, dar-görevli çağrıyla (`_lojistik_chain`/
        `_tahliye_chain`/`_sevk_chain`) üretilir; biçim (numaralandırma)
        MODELE BIRAKILMAZ, burada Python f-string ile KOD SEVİYESİNDE
        birleştirilir (bkz. `_mikro_bolum_uret`). Bağımsızlık iki fayda
        sağlar: (1) "Papağan Modu" (3 maddenin birbirinin kopyası/tekrarı
        olması) yapısal olarak imkânsızdır — 3 madde 3 farklı, birbirinden
        habersiz çağrıyla üretilir; (2) hiçbir çağrı bir öncekini beklemek
        zorunda olmadığından paralel çalıştırılabilir.

        Mikro-görevler koşulsuz sıralı çalışır (bkz. aşağıdaki kod):
        Streamlit'in kendi çalışma modeliyle (worker thread'lere taşınmayan
        `ScriptRunContext`) bir paralel yürütme yaklaşımının yaratabileceği
        etkileşimi önlemek için bilinçli bir tercihtir. Bedeli süredir
        (paralel bir yürütmeye göre daha yavaş); kazancı arayüzün yapısal
        olarak asla kilitlenememesidir (thread/future/`shutdown` beklemesi
        yoktur).

        Raises:
            Bu fonksiyonun KENDİSİ artık istisna FIRLATMAZ — her mikro-görev
            (bkz. aşağıdaki `_mikro_gorevi_guvenli_calistir`) KENDİ içinde
            try/except'e sarılıdır; bir Ollama bağlantı hatası SADECE o
            bölümü bir yer-tutucu metne düşürür, DİĞER bölümleri/tüm
            fonksiyonu ÇÖKERTMEZ (bkz. "yutulan hata" düzeltmesi ilkesi).
        """
        durum = self.gather_situational_picture(bolge=bolge, odak_koordinatlari=odak_koordinatlari)
        if durum.is_empty():
            return {
                "durum_bos": True,
                "durum_ozeti": "",
                "oneriler_metni": "",
                "durum": durum,
                "cevresel_durum": None,
            }

        # "COĞRAFİ ÖN-KONTROL" (Sanity Check / Safdillik Filtresi): LLM'e HİÇ gitmeden önce, her kritik olayın
        # TÜRÜ ile bulunduğu ilin GERÇEK fiziksel yapısı arasında AÇIK bir
        # çelişki olup olmadığı kontrol edilir (bkz. `turkiye_harita.
        # cografi_on_kontrol` — "İç Anadolu'da Tsunami" gibi imkansız bir
        # öncül örneği DOĞRUDAN bu fonksiyonun docstring'inde belgelenir).
        # SADECE net/tartışmasız imkansızlıklar reddedilir; kontrol bu tür
        # için tanımlı değilse veya sınırda bir durumsa, karar YİNE LLM'e
        # (ve nihayetinde komutana) bırakılır.
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

        # "DİNAMİK SAHA VE ÇEVRESEL İSTİHBARAT":
        # krizin TEMSİLİ bir koordinatı (bkz. `_temsili_kriz_koordinati_
        # bul`) için hava durumu istihbaratı çekilir ve HER 3 mikro-göreve
        # ORTAK olarak enjekte edilir (bkz. `_LOJISTIK_MIKRO_PROMPT_
        # TEMPLATE`teki "# ÇEVRESEL İSTİHBARAT" bloğu). `cevresel_durumu_
        # getir` ASLA istisna fırlatmaz/bekletmez (bkz. o fonksiyonun
        # docstring'i) — bu satır rapor üretimini ASLA çökertmez/geciktirmez.
        temsili_koordinat = self._temsili_kriz_koordinati_bul(durum)
        cevresel_durum: Optional[CevreselDurum] = (
            cevresel_durumu_getir(*temsili_koordinat) if temsili_koordinat else None
        )
        cevresel_durum_metni = cevresel_durum.aciklama_metni if cevresel_durum else "Bilinmiyor (konum belirlenemedi)"

        # KALICI SENKRON YÜRÜTME (bkz. modül başındaki
        # "EŞZAMANLILIK MİMARİSİ" notu): 3 bağımsız mikro-görev (Lojistik/
        # Tahliye/Sevk) ARTIK PARALEL DEĞİL, düz SIRALI olarak çalıştırılır —
        # biri bitmeden diğeri başlamaz. Yine de HER biri KENDİ try/except'i
        # İÇİNDE çalışır (bkz. yukarıdaki "yutulan hata" düzeltmesi ilkesi):
        # `_mikro_bolum_uret` Ollama'ya hiç bağlanamayıp `RuntimeError`
        # fırlatırsa, bu HEMEN `logger.error` ile (terminalde KESİN görünür
        # şekilde) loglanır ve SADECE o bölüm için güvenli bir yer-tutucu
        # metne düşülür — TEK bir bölümün başarısız olması DİĞER İKİSİNİ
        # ASLA etkilemez/tüm raporu çökertmez.
        girdi = {"durum_ozeti": durum_ozeti, "cevresel_durum": cevresel_durum_metni}
        _MIKRO_GOREV_HATA_YER_TUTUCUSU = (
            "Bu bölüm üretilemedi (Ollama servisiyle bağlantı sorunu/zaman aşımı — "
            "bkz. terminal logları); diğer bölümler bundan ETKİLENMEDİ."
        )

        # "LLM'E HİÇ GİTME" KISAYOLU (bkz. `_kategori_icin_gercek_birlik_var_mi`
        # docstring'i): her kategori için, o kategorinin GERÇEKTEN kullanabileceği
        # en az bir birlik var mı, Ollama'ya HİÇ sorulmadan koddan belirlenir.
        # Kategori için hiç birlik yoksa mikro-görev TAMAMEN ATLANIR — hem
        # gereksiz bir Ollama çağrısı önlenir hem de (asıl amaç) modelin,
        # SIKÇA tekrarlanan "BULUNAMADI" ibaresini GERÇEKTEN birlik bulunan
        # BAŞKA bir olayda bile yankılamasının (gözlemlenen halüsinasyon
        # deseni) önü YAPISAL olarak kesilir.
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
            except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin, HER ZAMAN loglanir.
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
            # bkz. yukarıdaki `paralel_calistir` docstring notu — SADECE
            # Streamlit ScriptRunContext'inin ASLA devrede olmadığı
            # çağıranlar (ör. FastAPI) bu dala girer. `max_workers=3`:
            # 3 görev DE aynı anda başlar, biri diğerini BEKLEMEZ.
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

        # KOD SEVİYESİNDE BİRLEŞTİRME: numaralandırma/biçim ARTIK MODELE bırakılmaz,
        # doğrudan burada üretilir — bu, `parse_oneri_maddeleri`nin (bkz.
        # `src.ui.app`) beklediği "1. ... 2. ... 3. ..." formatını HER
        # ZAMAN GARANTİ eder.
        #
        # "SAHTE ÇEŞİTLİLİK" TEMİZLİĞİ: 3 kategori de (yukarıdaki "LLM'e HİÇ
        # GİTME" kısayolu YÜZÜNDEN, YA DA üçü de bağımsız olarak GÜVENLİ
        # YER TUTUCUYA düştüğü İÇİN — bkz. `_bulunan_ilk_birlik_ile_guvenli_
        # cumle`/`_SABIT_BULUNAMADI_CUMLESI`) kelimesi kelimesine AYNI metne
        # düştüyse, bunu 3 kez tekrarlayan 3 ayrı madde olarak GÖSTERMENİN
        # komutana hiçbir faydası yoktur — GERÇEK bir KDS, gerçekte TEK olan
        # bir bulguyu 3 farklı madde gibi ŞİŞİRMEZ. Bu durumda TEK bir madde
        # döndürülür (kontrol SADECE tam eşitliğe bakar — hangi SEBEPLE aynı
        # sonuca varıldığı ÖNEMSİZDİR).
        if lojistik == tahliye == sevk:
            oneriler_metni = f"1. {lojistik}"
        else:
            oneriler_metni = f"1. {lojistik}\n2. {tahliye}\n3. {sevk}"

        return {
            "durum_bos": False,
            "durum_ozeti": durum_ozeti,
            "oneriler_metni": oneriler_metni,
            "durum": durum,
            # "FAZ 3" — bkz. yukarısı: `src.ui.app` bunu küçük bir hava
            # durumu widget'ında gösterir (bkz. `render_ai_staff_section`).
            "cevresel_durum": cevresel_durum,
        }

    @staticmethod
    def _temsili_kriz_koordinati_bul(durum: "SituationalPicture") -> Optional[Tuple[float, float]]:
        """"FAZ 3: DİNAMİK SAHA VE ÇEVRESEL İSTİHBARAT" için, hava durumu
        istihbaratının hangi NOKTA için çekileceğini belirler.

        BİLİNÇLİ BASİTLEŞTİRME: bir raporda BİRDEN FAZLA farklı konumda
        olay/kapalı yol/hasarlı tesis OLABİLİR (nadir ama mümkün), ama
        gerçek dünyada tek bir kriz RAPORU neredeyse HER ZAMAN TEK bir
        coğrafi bölgeyi anlatır (ör. "İskenderun Patlaması" tek bir
        şehirdeki tüm etkileri kapsar) — bu yüzden TEK bir "temsili"
        koordinat (ilk kritik olay, yoksa ilk kapalı yol, yoksa ilk
        hasarlı tesis) yeterli kabul edilir; TÜM mikro-görevler AYNI hava
        durumu bağlamını paylaşır. Hiçbiri koordinat taşımıyorsa (teorik
        olarak `is_empty()` zaten bunu elemiş olur) `None` döner."""
        for liste in (durum.kritik_olaylar, durum.kapali_yollar, durum.hasarli_tesisler):
            for kayit in liste:
                enlem, boylam = kayit.get("enlem"), kayit.get("boylam")
                if enlem is not None and boylam is not None:
                    return float(enlem), float(boylam)
        return None

    def _mikro_bolum_uret(
        self, chain: Any, zemin_chain: Any, girdi: Dict[str, str], durum: "SituationalPicture", bolum_adi: str,
    ) -> str:
        """Tek bir mikro-görevi (Lojistik/Tahliye/Sevk) çalıştırır ve "Dil
        Kilidi" + "Veri Referanslılığı" güvencelerini (bkz.
        `_ingilizce_supheli_mi`/`_veri_referanssiz_mi`) KISALTILMIŞ, TEK
        KADEMELİ bir tekrar deneme zinciriyle uygular. `girdi`, ilgili
        `chain`in `PromptTemplate`indeki `input_variables`e karşılık gelen
        bir sözlüktür (bu mimaride HER ZAMAN SADECE `{"durum_ozeti": ...}`
        — parametre yine de genel/sözlük-tabanlı BIRAKILDI ki gerekirse
        başka bir girdi şekli kolayca eklenebilsin).

        Eski (dev-prompt) sürümdeki AĞIR 3-kademeli kurtarma zincirinin
        (retry -> çeviri kademeleri -> düşük-sıcaklık -> yer-tutucu) AKSİNE,
        burada görev ZATEN o kadar dar/kısa (2 cümle) ki İKİ basit deneme
        (normal + düşük sıcaklık) YETERLİ olması beklenir — yine de dil
        sorunu ısrar ederse mevcut basit çevirmen (`_turkceye_cevir_
        serbest_metin`) SON çare olarak kullanılır. Bu fonksiyon HİÇBİR
        ZAMAN bir "yer-tutucu hata" metni DÖNMEZ (eski "%50 üretilemedi"
        rejimine ASLA geri DÖNÜLMEZ) — ELİNDEKİ EN İYİ sonucu (kısa metin
        olduğu için düşük risklidir) her zaman döner; kod seviyesinde
        `_format_temizle` ile son bir dekoratif temizlik uygulanır.

        Raises:
            RuntimeError: Ollama servisine erişilemezse (ilk çağrı başarısız
                olursa — tekrar denemeler bu hatayı YUTAR, ilk cevaba döner).
        """
        # "KİLİT NOKTASI" DEBUG LOGLAMASI: bu mimaride
        # LLM çağrıları "olay" başına DEĞİL, mikro-görev (Lojistik/Tahliye/
        # Sevk) başına yapılır (bkz. `generate_recommendations`) — bu
        # yüzden "[Olay Adı]" yerine `bolum_adi` (mikro-görev adı)
        # kullanılır; terminalde HANGİ mikro-görevin Ollama'da beklediğini
        # ayırt etmeye yeter.
        logger.info("LLM'e gonderiliyor: [%s]", bolum_adi)
        _t0 = time.monotonic()
        try:
            cevap = chain.invoke(girdi)
        except Exception as exc:  # noqa: BLE001 - Ollama baglanti hatalarini sarmalar
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
        # "KESİN SAYISAL KURAL" doğrulaması İÇİN referans metni SADECE
        # `durum_ozeti` DEĞİL, çevresel istihbarat metnini de (ör. "31°C")
        # KAPSAR — model bu ikinci alandan da MEŞRU sayılar kullanabilir
        # (bkz. `_sayisal_uydurma_suphesi_mi` docstring'i).
        _sayisal_referans_metni = _durum_ozeti_metni + "\n" + girdi.get("cevresel_durum", "")
        # NOT (`_yanlis_bulunamadi_iddiasi_mi`ye `durum_ozeti=""` geçilmesi):
        # bu metod (bkz. tek çağıranı `generate_recommendations`) SADECE
        # `_kategori_icin_gercek_birlik_var_mi` BU KATEGORİ için `True`
        # döndürdüğünde çalıştırılır — yani bir birlik bulunduğu ZATEN KOD
        # SEVİYESİNDE KANITLANMIŞTIR. Gerçek `_durum_ozeti_metni`yi
        # geçirmek, raporun İÇİNDEKİ BAŞKA/İLGİSİZ bir olay/kapalı-yol
        # bloğu (ör. aynı raporda ayrıca listelenen, birliği OLMAYAN bir
        # kapalı güzergah) yüzünden kontrolü YANLIŞLIKLA devre dışı
        # bırakabilir (bkz. o fonksiyonun docstring'i) — boş dize bu riski
        # ortadan kaldırıp kontrolü KOŞULSUZ aktif tutar.
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
                    # "TERS YÖNLÜ ÇELİŞKİ İKİNCİ DENEMEDE DE SÜRÜYORSA" SERT
                    # DURAK (bkz. `_yanlis_bulunamadi_iddiasi_mi` docstring'i):
                    # düşük-sıcaklıklı yeniden deneme BİLE, GERÇEKTEN bulunan
                    # bir birliği "bulunamadı" diye YANLIŞ inkâr edebilir —
                    # bu durumda LLM'in serbest metnine HİÇ güvenilmez, Bilgi
                    # Grafında GERÇEKTEN bulunan birliğin adı doğrudan koddan
                    # yazılır (bkz. `_bulunan_ilk_birlik_ile_guvenli_cumle`) —
                    # komutana "birlik yok" diye YANLIŞ bir bilgi ASLA sunulmaz.
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
                    # "İKİNCİ DENEME DE UYDURDUYSA GÜVENLİ YER TUTUCUYA DÜŞ"
                    # SERT DURAK (bkz. `_kesin_birlik_ismi_uydurmasi_bul`
                    # docstring'i): düşük-sıcaklıklı (0.05) yeniden deneme
                    # BİLE aynı (veya başka) uydurma bir birlik üretebilir —
                    # ilk denemedeki isim reddedilse bile model, durum_
                    # ozeti'nde hiç geçmeyen ama coğrafi olarak daha
                    # inandırıcı görünen ikinci bir isim icat edebilir.
                    # `_veri_referanssiz_mi`/`_dil_kilidi_ihlali_mi`nin AKSİNE
                    # (onlarda "eldeki en iyi sonuç boş yer tutucudan iyidir"
                    # ilkesi geçerlidir), KESİN ismi UYDURULMUŞ bir birliği
                    # SAHAYA sevk etmek boş bir yer tutucudan HER ZAMAN DAHA
                    # KÖTÜDÜR (komutan var olmayan bir birliği bekler) — bu
                    # yüzden ikinci deneme de KESİN uydurma içeriyorsa, ELDEKİ
                    # cevap yerine dürüst/güvenli bir yer-tutucu KULLANILIR.
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
                    # Sadece veri-referanssiz (dil sorunu YOK, KESIN isim
                    # uydurmasi da YOK) — cevirinin faydasi yok, ama kisa bir
                    # metin oldugu icin ELDEKI EN SON sonucu (ikinci_cevap)
                    # yine de kullanmak, bos bir yer-tutucudan DAHA
                    # FAYDALIDIR (bkz. docstring) — KESIN isim uydurmasi bu
                    # dalda ASLA olamaz (yukaridaki elif zaten onu yakalar).
                    cevap = ikinci_cevap
            except Exception as exc:  # noqa: BLE001 - yeniden deneme basarisiz olursa ILK cevaba sessizce don.
                logger.warning("'%s' mikro-gorevi yeniden deneme cagrisi basarisiz oldu: %s", bolum_adi, exc)

        return _format_temizle(cevap)

    def _turkceye_cevir(self, metin: str) -> Optional[str]:
        """"DİL KİLİDİ" GÜVENCESİ — SON ÇARE (bkz. `_CEVIRI_PROMPT_TEMPLATE`
        docstring'i): model iki denemede de İngilizce ürettiğinde, TAMAMEN
        AYRI, BASİT ve `format="json"` ile ŞEMA-KISITLI bir görevle ("bunu
        Türkçeye çevir") son bir deneme yapılır. Ollama çağrısı başarısız
        olursa, JSON ayrıştırılamazsa VEYA `turkce_metin` alanı eksik/boşsa
        `None` döner — çağıran taraf bu durumda orijinal (İngilizce) metni
        KORUR, HİÇBİR ZAMAN boş/None/bozuk bir sonucu kullanıcıya sunmaz.

        JSON AYRIŞTIRMA (bkz. `nlp_parser.OllamaParser._parse_json` ile AYNI
        "agresif temizlik" deseni — `format="json"` bile GARANTİ DEĞİLDİR):
        model YİNE DE markdown kod bloğu veya baştaki/sondaki açıklama
        metniyle sarabileceğinden, ham yanıt önce bu kalıplardan temizlenir.
        """
        try:
            ham_yanit = self._ceviri_chain.invoke({"ingilizce_metin": metin})
        except Exception as exc:  # noqa: BLE001 - ceviri basarisiz olursa cagiran taraf orijinali korur.
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
        """ÜÇÜNCÜ (SON) KADEME (bkz. `_CEVIRI_SERBEST_METIN_PROMPT_TEMPLATE`
        docstring'i): `_turkceye_cevir` (JSON-şemalı) başarısız olduğunda
        (uzun/çok paragraflı metinlerde JSON'un yarıda kesilebildiği veya
        modelin özetleme yapabildiği durumlar için, bkz. `_CEVIRI_PROMPT_
        TEMPLATE`deki "TASARIM NOTU #2") çağrılır. Hiçbir şema/kısıt YOKTUR — sadece "çevir ve SADECE
        çeviriyi yaz" talimatı. Yine de model markdown/tırnak/açıklama
        satırı ekleyebileceğinden hafif bir temizlik uygulanır. Boş/None
        sonuç üretilirse `None` döner; çağıran taraf orijinali korur.
        """
        try:
            ham_yanit = self._ceviri_serbest_chain.invoke({"ingilizce_metin": metin})
        except Exception as exc:  # noqa: BLE001 - basarisiz olursa cagiran taraf orijinali korur.
            logger.warning("Serbest metin ceviri (3. kademe) basarisiz oldu: %s", exc)
            return None

        temizlenmis = ham_yanit.strip()
        temizlenmis = re.sub(r"```(?:\w+)?", "", temizlenmis).strip()
        # Model bazen "Türkçe çeviri:" gibi tek satırlık bir baslik ekler —
        # sadece İLK satırda ve İKİ NOKTA ile bitiyorsa (kisa bir baslik
        # gorunumundeyse) atilir; gercek cevirinin bir parcasi olan (uzun)
        # bir ilk cumleyi YANLISLIKLA silmemek icin uzunluk siniri konur.
        ilk_satir_sonu = temizlenmis.find("\n")
        if ilk_satir_sonu != -1:
            ilk_satir = temizlenmis[:ilk_satir_sonu].strip()
            if ilk_satir.endswith(":") and len(ilk_satir) < 40:
                temizlenmis = temizlenmis[ilk_satir_sonu + 1 :].strip()

        if not temizlenmis:
            logger.warning("Serbest metin ceviri (3. kademe) bos sonuc uretti.")
            return None
        return temizlenmis

    # ------------------------------------------------------------------ #
    # Graf tarama (GraphRAG "retrieval" adimi)
    # ------------------------------------------------------------------ #

    def gather_situational_picture(
        self, bolge: Optional[str] = None, odak_koordinatlari: Optional[List[Tuple[float, float]]] = None
    ) -> SituationalPicture:
        """Neo4j'den kurmay başkanlığı değerlendirmesi için gerekli tüm
        anlık verileri çeker ve ilişkilendirir (alternatif rota/tesis eşleme
        dahil).

        `odak_koordinatlari` verilirse (bkz. `generate_recommendations`
        docstring'i), `kritik_olaylar`
        (ülke çapında ÇEKİLDİKTEN SONRA) sadece bu koordinatlardan ~2 km
        içindeki olaylara İNDİRGENİR — birden fazla eş-zamanlı aktif kriz
        varken, bu ÇAĞRIYLA İLGİSİZ diğer olayların anlatıya karışmasını
        (ve dolayısıyla `hasarli_tesisler`/`kapali_yollar`ın da — bkz.
        `format_durum_ozeti`'ndeki "ANLATIM İLGİLİLİK SÜZGECİ", ki o zaten
        `kritik_olaylar`ın konumuna göre çalışır — YANLIŞ olaya göre
        süzülmesini) önler. Süzgeç HİÇBİR olayı bulamazsa (ör. konum tam
        eşleşmediyse) GÜVENLİ VARSAYILAN olarak TÜM olaylar korunur.

        `bolge` verilirse TÜM sorgular `n.bolge = $bolge` ile filtrelenir
        (programatik/betik kullanımı için opsiyonel bir yetenek — `src.ui.app`
        bunu kullanmaz). `bolge=None` (varsayılan, UI'nin FİİLEN kullandığı
        mod) filtre uygulamaz — TÜM graf/tüm şehirler birlikte taranır.
        """
        # Bu log noktası, sistemin Neo4j sorgu aşamasında mı yoksa LLM
        # üretim aşamasında mı (bkz. `generate_recommendations`/
        # `_mikro_bolum_uret`) beklediğini terminalden ayırt etmeyi sağlar.
        logger.info("Neo4j sorgusu basladi: durum resmi toplaniyor (bolge=%s)", bolge or "TUMU")
        _resim_baslangic = time.monotonic()

        def _bolge_kosulu(alias: str, ilk_kosul: bool) -> str:
            """`bolge` verilmisse, verilen degisken takma adi (ör. "f", "i")
            icin bir `bolge = $bolge` kosulu uretir; `ilk_kosul=True` ise
            `WHERE` ile, degilse `AND` ile baglanir. `bolge=None` ise bos
            string (kosul EKLENMEZ)."""
            if not bolge:
                return ""
            baglayici = "WHERE" if ilk_kosul else "AND"
            return f" {baglayici} {alias}.bolge = $bolge"

        # `COALESCE(f.aciklama, f.isim)`: GERCEK OSM hastanelerinde (bkz.
        # `local_osm_reader._amenity_dugumu_uret` — "ULUSAL ISIM CAKISMASI"
        # DUZELTMESI) `isim` benzersizlik icin bir OSM tip/id soneki tasir;
        # komutana sunulacak metinde bu teknik kimlik DEGIL, `aciklama`daki
        # TEMIZ gercek isim gorunmelidir — `kapali_yollar` sorgusundaki AYNI
        # desen (asagida). `aciklama` da NULL ise (gercekten isimsiz bir
        # eleman), COALESCE teknik `isim`e duser; boyle adaylar TAMAMEN elenir.
        hasarli_tesisler = self.db.execute_query(
            "MATCH (f:Facility) WHERE f.mevcut_durum IN $durumlar" + _bolge_kosulu("f", False) + " "
            "WITH f, COALESCE(f.aciklama, f.isim) AS isim "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN f.id AS id, isim, f.facility_type AS tip, f.mevcut_durum AS durum, "
            "f.enlem AS enlem, f.boylam AS boylam, f.kapasite AS kapasite "
            "ORDER BY f.guncelleme_tarihi DESC",
            {"durumlar": HASARLI_TESIS_DURUMLARI, "bolge": bolge, "osm_imza": OSM_TEKNIK_KIMLIK_IMZASI},
        )
        # `COALESCE(i.aciklama, i.isim)`: GERCEK OSM sokak/koprulerinde
        # (bkz. `real_osm_loader.RealOsmLoader`) `isim` benzersizlik icin bir
        # "(OSM way/... #...)" soneki tasir; komutana sunulacak metinde bu
        # teknik/veritabani kimligi DEGIL, `aciklama`daki TEMIZ gercek isim
        # gorunmelidir (bkz. sorun tanimi: "(OSM way/1330164805 #0)" sizmasi).
        # `WHERE NOT ... CONTAINS $osm_imza`: `aciklama` da NULL ise (GERCEK
        # OSM'de isimsiz bir segment), COALESCE hala teknik `isim`e duser;
        # boyle "gosterime uygun temiz adı OLMAYAN" adaylar TAMAMEN elenir
        # (komutana "Sokak Dugumu (OSM way/...)" gibi anlamsiz bir isim
        # sunmaktansa, o adayı hic GOSTERMEMEK tercih edilir).
        # `_isim_bazinda_tekillestir`: ayni caddenin GERCEK veride onlarca
        # ayri nokta-dugumu (hepsi ayni COALESCE isme sahip) olabilecegi icin,
        # bu sorgu tekillestirme ONCESI DEDUPLIKE EDILMEMIS ham satirlari
        # dondurur; asagida en guncel EN ONCE gelecek sekilde tekillestirilir.
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
        # Bu sorgu kesin bir üst sınıra tabidir: `bolge` filtresi
        # verilmediğinde ülke çapındaki TÜM aktif birlikler dönebilir. Bu
        # liste (bkz. `SituationalPicture.aktif_birlikler` alanının
        # docstring'i) SADECE `_durum_gercek_isimlerini_topla` (PROMPT
        # LEAKAGE anti-sahtecilik kontrolü) için kullanılır ve LLM'e HAM
        # olarak SUNULMAZ (bkz. o alanın yorumu) — bu yüzden TAMAMINI
        # çekmek gereksizdir; birkaç yüz temsili isim kontrol için
        # fazlasıyla yeterlidir.
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

        # Her kapalı yol/kritik olay/hasarlı tesis analizi birbirinden
        # bağımsızdır (farklı konumlar, farklı Dijkstra graf inşaları) ve
        # bilinçli olarak SIRALI (senkron) çalıştırılır — `ThreadPoolExecutor`
        # bu fonksiyonda kullanılmaz, çünkü UI katmanının (Streamlit) kendi
        # çalışma modeliyle paralel yürütme arayüz kilitlenmesine yol
        # açabilir. Neo4j session thread-güvenliği (`execute_query` her
        # çağrıda taze bir `session()` açar) tek thread'li yürütümde
        # zaten geçerlidir; bu, `database.py`'deki genel garantinin bir
        # sonucudur.
        en_yakin_ulasilan_birlikler: Dict[str, List[Dict[str, Any]]] = {}
        en_yakin_guvenlik_birlikleri: Dict[str, List[Dict[str, Any]]] = {}
        alternatif_rotalar: Dict[str, List[Dict[str, Any]]] = {}
        en_yakin_saglam_tesisler: Dict[str, List[Dict[str, Any]]] = {}

        # Kapalı yol/kritik olay/hasarlı tesis analizleri SIRALI çalışır;
        # yine de HER analiz KENDİ try/except'i İÇİNDE yürütülür: tek bir
        # yol/olay/tesisin analizi başarısız olursa bu HEMEN `logger.error`
        # ile loglanır ve SADECE o iş birimi atlanır — DİĞERLERİ ASLA
        # etkilenmez, tüm rapor çökmez.
        def _analiz_guvenli_calistir(fonksiyon: Any, girdi_kaydi: Dict[str, Any], gorev_adi: str) -> Optional[Any]:
            try:
                return fonksiyon(girdi_kaydi, bolge)
            except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin, HER ZAMAN loglanir.
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
        """Tek bir kapalı yol için gereken TÜM Neo4j sorgularını bir arada
        toplar — `gather_situational_picture` bunu her kapalı yol için
        SIRAYLA çağırır (bkz. o fonksiyondaki "STREAMLIT UI KİLİTLENMESİ"
        notu — bu bölme bir ara paralel çalıştırma İÇİN eklenmişti, ARTIK
        SADECE kodu okunaklı/tek-birim halde tutmak için KORUNUYOR).
        Aynı olay yeri için art arda çağrılan `_en_yakin_ulasilan_
        birlikleri_bul`/`_guvenlik_birlikleri_bul`, `_yerel_yol_agi_ve_
        kaynak_getir`in KISA SÜRELİ önbelleği sayesinde yerel yol ağı
        grafını İKİ KEZ KURMAZ (bkz. o metodun docstring'i) — bu yüzden
        bu ikisi BİLEREK aynı worker/iş biriminde, sırayla tutulur.

        Dönüş: `(isim, alternatif_rotalar, ulasilan_birlikler, guvenlik_
        birlikleri)` — koordinat eksikse SON İKİSİ `None` döner (eski
        döngüdeki `continue` ile "hiç girdi eklenmeme" davranışını BİREBİR
        korumak için; `None`, boş listeden `gather_situational_picture`
        içinde AYRIKA ele alınır — bkz. oradaki yorum).
        """
        isim = yol["isim"]
        logger.info("Neo4j sorgusu basladi: [%s] (kapali yol)", isim)
        _t0 = time.monotonic()
        alternatif = self._en_yakin_acik_yollari_bul(yol, bolge=bolge)
        if yol.get("enlem") is None or yol.get("boylam") is None:
            logger.info("Neo4j sorgusu bitti (%.1f sn): [%s] (koordinat yok, birlik aramasi atlandi)",
                        time.monotonic() - _t0, isim)
            return isim, alternatif, None, None
        # Bir KAPALI GÜZERGAH HER ZAMAN bir altyapı sorunudur (bkz.
        # `_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI` docstring'i) — olay TÜRÜNE
        # bakılmaz, öncelik HER ZAMAN Ağır Mühendislik/AFAD'dır.
        birlikler = self._en_yakin_ulasilan_birlikleri_bul(
            yol, oncelik_tipleri=_ALTYAPI_ONCELIKLI_BIRIM_TIPLERI, bolge=bolge
        )
        guvenlik = self._guvenlik_birlikleri_bul(yol, bolge=bolge)
        logger.info("Neo4j sorgusu bitti (%.1f sn): [%s]", time.monotonic() - _t0, isim)
        return isim, alternatif, birlikler, guvenlik

    def _olay_analiz_et(
        self, olay: Dict[str, Any], bolge: Optional[str] = None
    ) -> "tuple[str, Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]":
        """PARALELLEŞTİRME: tek bir kritik olay için `_en_yakin_ulasilan_
        birlikleri_bul`+`_guvenlik_birlikleri_bul` çiftini bir arada
        toplar — bkz. `_kapali_yol_analiz_et` docstring'indeki AYNI
        önbellek/`None`-sentinel gerekçesi."""
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
        """PARALELLEŞTİRME: tek bir hasarlı tesis için en yakın sağlam
        tesis aramasını sarmalar (bkz. `_kapali_yol_analiz_et`
        docstring'indeki AYNI paralelleştirme gerekçesi)."""
        isim = tesis["isim"]
        logger.info("Neo4j sorgusu basladi: [%s] (hasarli tesis)", isim)
        _t0 = time.monotonic()
        sonuc = self._en_yakin_aktif_tesisleri_bul(tesis, bolge=bolge)
        logger.info("Neo4j sorgusu bitti (%.1f sn): [%s]", time.monotonic() - _t0, isim)
        return isim, sonuc

    def _en_yakin_acik_yollari_bul(
        self, kapali_yol: Dict[str, Any], bolge: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Kapalı bir güzergahın başlangıç noktasına, coğrafi olarak en
        yakın AÇIK (`acik_mi = true`) alternatif güzergahları bulur.

        MEKANSAL İNDEKS (Faz 3 — ulusal ölçek): Mesafe artık ham
        `sqrt(Δenlem²+Δboylam²)` YAKLAŞIKLIĞI DEĞİL, Neo4j'nin `konum`
        (`Point`, bkz. `database._konum_point_ifadesi`) alanı üzerindeki
        `point.distance()` (GERÇEK jeodezik, metre cinsinden) ile
        hesaplanır. `WHERE point.distance(...) <= $yaricap_metre` deseni,
        `database.ensure_spatial_indexes` ile oluşturulan POINT INDEX'i
        KULLANIR — milyonlarca düğümlü ulusal ölçekte bile TÜM
        `Infrastructure` grafını taramak yerine SADECE `_ARAMA_YARICAPI_KM`
        içindeki adaylar taranır (bkz. modül docstring'i).

        `isim` alanı `COALESCE(aciklama, isim)` ile TEMİZ gerçek ada
        indirgenir (bkz. `gather_situational_picture`'daki AYNI yorum);
        aynı gerçek caddenin GERÇEK OSM verisinde onlarca ayrı nokta-düğümü
        olabileceğinden, `_max_alternatif`'in KAT FAZLASI aday çekilip
        `_isim_bazinda_tekillestir` ile TEKİLLEŞTİRİLİR — aksi halde AYNI
        caddenin farklı noktaları N farklı "alternatif" gibi görünürdü.
        `aciklama`sı da OLMAYAN (gerçekten isimsiz) segmentler, komutana
        anlamsız bir "(OSM way/...)" kimliği sunmamak için TAMAMEN elenir
        (bkz. `gather_situational_picture`'daki AYNI filtre/yorum).
        """
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
        """Hasarlı/yok edilmiş bir tesise, coğrafi olarak en yakın SAĞLAM
        (`mevcut_durum = 'Aktif'`) tesisleri bulur — MEKANSAL İNDEKS
        kullanan `point.distance()` ile (bkz. yukarıdaki AYNI yorum).

        Kendini-hariç-tutma ARTIK `f.id` (her düğüm için benzersiz UUID)
        ÜZERİNDEN yapılır, `isim`/`aciklama` ÜZERİNDEN DEĞİL ("ULUSAL İSİM
        ÇAKIŞMASI" düzeltmesi SONRASI — bkz. `local_osm_reader._amenity_
        dugumu_uret` — artık FARKLI illerdeki İKİ GERÇEK hastane aynı
        jenerik temiz ada sahip OLABİLİR, ör. iki ayrı "Devlet Hastanesi";
        isim-bazlı bir hariç tutma bu durumda hasarlı tesisin kendisini
        DEĞİL, YAKINDAKİ FARKLI/gerçek bir alternatifi de YANLIŞLIKLA
        elerdi). Dönen adayların GÖRÜNEN `isim`i yine `COALESCE(aciklama,
        isim)` ile temiz gerçek ada indirgenir ve tamamen isimsiz/teknik
        adaylar (bkz. `hasarli_tesisler` sorgusundaki AYNI desen) elenir.
        """
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
        """"MEKANSAL KÖRLÜK" + "YETENEK BAZLI ROTA" DÜZELTMESİ: bir olay
        yerinden (kritik olay VEYA kapalı yol — ikisi de çağrılabilir),
        `Neo4jConnection.en_yakin_ulasilan_varliklari_bul` ile SADECE AÇIK
        yol ağı üzerinden GERÇEK Dijkstra yol mesafesiyle ulaşılabilen VE
        (`oncelik_tipleri` verilmişse) DOĞRU YETENEĞE sahip en yakın
        birlikleri bulur. Kapalı yollarla İZOLE olmuş birlikler bu listede
        HİÇ GÖRÜNMEZ; YANLIŞ yetenekteki birlikler de (ör. bir köprü
        krizinde Emniyet) bu listeye SESSİZCE KARIŞMAZ — bkz. `_GUVENLIK_
        BIRIM_TIPLERI`/`_guvenlik_birlikleri_bul` (AYRI, "2. plan" listesi).

        KADEMELİ (tiered) ARAMA YARIÇAPI — performans gerekçesi:
        `oncelik_tipleri` verildiğinde ÖNCE ucuz varsayılan yarıçapta
        (`_YOL_AGI_ARAMA_YARICAPI_KM`, 25 km — saniyeler mertebesinde)
        aranır; DOĞRU yetenekte HİÇBİR birim bulunamazsa (SADECE bu
        durumda), arama `_YETENEK_ARAMA_YARICAPI_KM`ye (50 km)
        GENİŞLETİLİR — bu daha pahalıdır (50 km'lik bir yerel graf inşası
        onlarca saniye sürebilir) ama SADECE doğru araç başka türlü
        bulunamadığında gerçekleşir, HER aramada DEĞİL. `oncelik_tipleri` verilmemişse
        (ör. `_guvenlik_birlikleri_bul`) davranış eskisi gibi tek-yarıçaplı
        kalır. Hiçbir yarıçapta doğru yetenekte birim BULUNAMAZSA, YANLIŞ
        bir türe (ör. Emniyet) SESSİZCE GERİ DÜŞÜLMEZ — boş liste dönülür,
        `_kriz_ve_mudahale_bloku` bunu dürüstçe "BULUNAMADI" olarak
        gösterir (bkz. ayrı `en_yakin_guvenlik_birlikleri` — Emniyet HER
        ZAMAN o listede ayrıca sunulur)."""
        # NOT: `en_yakin_ulasilan_varliklari_bul` (database.py) hedef
        # düğümü HER ZAMAN `h` takma adıyla eşler (`MATCH (h:{hedef_label})
        # ...`) — hedef_label ne olursa olsun (burada "Unit"). Takma ad
        # "u" DEĞİL "h"dir; yanlış takma ad Cypher SyntaxError'a yol açar.
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
        """"2. PLAN" (Yetenek Bazlı Rota): Emniyet/Askeri
        birimlerini (bkz. `_GUVENLIK_BIRIM_TIPLERI`), birincil "MÜDAHALE
        EDECEK BİRLİK" aramasından TAMAMEN AYRI, kendi filtreli sorgusuyla
        bulur — SADECE çevre güvenliği/trafik kontrolü/tahliye amaçlı bir
        DESTEK listesidir, ASLA birincil çözüm önerisi olarak sunulmaz
        (bkz. DECISION_PROMPT_TEMPLATE "SİVİL-ASKERİ KOORDİNASYON
        KURALI"). `_en_yakin_ulasilan_birlikleri_bul` ile AYNI olay
        yerinde çağrıldığında, `Neo4jConnection._yerel_yol_agi_ve_kaynak_
        getir`in KISA SÜRELİ önbelleği sayesinde yerel yol ağı GRAFI
        YENİDEN KURULMAZ (bkz. o metodun docstring'i) — sadece hedef
        eşleştirme adımı tekrarlanır."""
        # NOT: bkz. `_en_yakin_ulasilan_birlikleri_bul` — takma ad "h"dir.
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

    # ------------------------------------------------------------------ #
    # GraphRAG baglam formatlama
    # ------------------------------------------------------------------ #

    @staticmethod
    def format_durum_ozeti(durum: SituationalPicture) -> str:
        """`SituationalPicture`'ı, LLM'e verilecek okunabilir bir GraphRAG
        bağlam metnine (düz metin, Türkçe) çevirir.

        Tüm alan okumaları `_deger()` üzerinden yapılır (SADE `.get(key,
        varsayilan)` DEĞİL) — Neo4j'den gelen `None` değerlerin ("tonaj
        kapasitesi: None" gibi) bağlama HARFİYEN sızmasını önlemek için
        (bkz. `_deger` docstring'i / sorun tanımı).
        """
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

        # "ANLATIM İLGİLİLİK SÜZGECİ" (bkz. `_KRIZ_ANLATIM_ILGILILIK_YARICAP_
        # KM` sabiti): aktif kritik olay(lar)ın konumuna göre coğrafi olarak
        # İLGİSİZ hasarlı tesis/
        # kapalı yol kayıtları, komutana sunulan METİNDEN çıkarılır — arama/
        # birlik-eşleştirme mantığı (yukarıdaki sorgular) BUNDAN ETKİLENMEZ,
        # sadece bu fonksiyonun ÜRETTİĞİ anlatı süzülür. Hiçbir aktif olay
        # yoksa (enlem/boylam referans noktası yoksa) süzgec ATLANIR — GÜVENLİ
        # VARSAYILAN, TÜM listeyi korumaktır (rapor asla sessizce boşalmaz).
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
        # NOT: süzgeç HER ŞEYİ elese BİLE (ör. TEK bir aktif olaydan ~150 km
        # içinde hiçbir kapalı yol/hasarlı tesis yoksa) "güvenli varsayılan"
        # olarak tam listeye DÖNÜLMEZ — bu, TAM OLARAK çözülen hatayı
        # (alakasız/uzak kayıtların rapora sızması) yeniden üretirdi. Sıfır
        # ilgili kayıt = bu bölümün BOŞ olması, DOĞRU ve DÜRÜST bir sonuçtur;
        # aşağıdaki başlıklar da (`hasarli_tesisler_ilgili`/`kapali_yollar_
        # ilgili` üzerinden) bu durumda hiç YAZDIRILMAZ.

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

        # ŞEFFAFLIK NOTU (bkz. yukarıdaki "ANLATIM İLGİLİLİK SÜZGECİ"): elenen
        # kayıtlar SESSİZCE yok sayılmaz ama LLM'in bağlamına/kullanıcının
        # gördüğü GraphRAG metnine DE ENJEKTE EDİLMEZ (metne karışırsa model
        # bu meta-notu KENDİSİ bir taktiksel madde sanıp yorumlayabilir) —
        # bunun yerine sunucu logunda izlenebilir kalır (bkz. "Ghost %s
        # ENGELLENDI" ile AYNI, bu projede tekrar eden "asla sessizce yutma,
        # ama kullanıcıya sunulan METNİ de kirletme" ilkesi).
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
        """"MANTIK ÇÖKÜŞÜ" (Logic Inversion) RİSKİNE KARŞI KORUMA: krizin
        kendisiyle ona müdahale edecek birliğin aynı düz metin akışında,
        ROL BELİRTMEDEN yan yana yazılması, modelin müdahale edecek birliği
        (ör. "Kale İlçe Emniyet Müdürlüğü") KRİZİN KENDİSİ/kurtarılacak
        hedef sanıp "Emniyet'i kurtarma" gibi TERSİNE bir öneri üretmesine
        yol açabilir.

        ÇÖZÜM: HER olay/kapalı-yol için, iki rolü KESİN OLARAK ayıran, satır
        satır etiketli bir blok üretilir — "KRİZ NOKTASI:" SADECE krizin
        kendisi için, "MÜDAHALE EDECEK BİRLİK:" SADECE ona giden ÇÖZÜM
        aracı (birlik) için kullanılır; bu iki etiket ASLA aynı satırda
        veya birbirinin yerine KULLANILMAZ. `birlikler` listesindeki HER
        birim zaten `en_yakin_ulasilan_varliklari_bul` tarafından SADECE
        açık yollardan geçilerek ulaşılabilir olduğu KANITLANMIŞ
        olduğundan, "rotası AÇIKTIR" ifadesi HER ZAMAN doğrudur.

        "YETENEK BAZLI ROTA" (Capability-Based Routing):
        `guvenlik_birlikleri` verilirse (bkz. `DecisionEngine._guvenlik_
        birlikleri_bul`), AYRI bir "ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ (2.
        PLAN)" bloğu olarak eklenir — bu birimler `birlikler`den KASITLI
        OLARAK farklı bir başlık altındadır ki model bunları ASLA birincil
        çözüm önerisiyle KARIŞTIRMASIN."""
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
