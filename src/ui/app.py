"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
Stratejik Komuta Kontrol ve Karar Destek Paneli (Streamlit arayüzü).

Bu panel, arka planda çalışan Bilgi Grafı boru hattının (Ollama LLM ->
Pydantic -> Neo4j, bkz. `src.data_ingestion.nlp_parser` ve
`src.core.database`) üzerine kurulu, üç ana bölümden oluşan bir operasyon
merkezi arayüzüdür:

1. Üst Panel   : Neo4j'den anlık çekilen kriz metrikleri (metric kartları).
2. Sol Panel   : Serbest metin bir rapor/telsiz konuşması girip, tek tıkla
                 Ollama üzerinden analiz ettirip Bilgi Grafı'na işleme.
3. Ana Ekran   : Tüm varlıkların 3 BOYUTLU, GPU hızlandırmalı bir taktiksel
                 haritada (PyDeck/deck.gl) gösterildiği durum haritası ve
                 son olayları özetleyen bir rapor tablosu.

FAZ 4 GÖÇÜ NOTU (Folium -> PyDeck): Faz 2 ETL'i (bkz. `tucbs_etl_loader.py`)
onbinlerce düğüm (sokak/köprü) yazabilir hale geldiğinde, Folium/Leaflet'in
DOM tabanlı marker mimarisi bu ölçekte tıkanır (binlerce ayrı HTML elemanı).
PyDeck, WebGL/deck.gl üzerinden GPU hızlandırmalı katmanlar (Hexagon/
Scatterplot/Column/Line) kullandığından onbinlerce noktayı akıcı biçimde
render edebilir. `folium`/`streamlit-folium` bu yüzden projeden TAMAMEN
çıkarılmıştır (bkz. `requirements.txt`).

Çalıştırmak için:
    streamlit run src/ui/app.py
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pydeck as pdk
import streamlit as st
from pydeck.data_utils import compute_view

# Bu dosya `src/ui/` altında çalıştığı için, proje kökünü (repo root) import
# yoluna elle ekliyoruz; böylece `streamlit run src/ui/app.py` doğrudan
# çalıştırıldığında da `src.core...` gibi mutlak importlar çözülebiliyor.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.core.database import Neo4jConnection, Neo4jConnectionError  # noqa: E402
from src.core.decision_engine import DecisionEngine, _temiz_isim, parse_oneri_maddeleri  # noqa: E402
from src.core.models import (  # noqa: E402
    Event,
    EventSeverity,
    Facility,
    FacilityStatus,
    FacilityType,
    Infrastructure,
    InfrastructureType,
    Unit,
    UnitType,
)
from src.data_ingestion.nlp_parser import ENTITY_MODEL_MAP, OllamaParser  # noqa: E402
from src.data_ingestion.osm_loader import ELAZIG_BBOX  # noqa: E402
from src.data_ingestion.real_osm_loader import (  # noqa: E402
    ANA_DAMAR_HIGHWAY_TIPLERI,
    OSM_TEKNIK_KIMLIK_IMZASI,
)
# "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI (bkz. aşağıdaki modül-içi
# not, eski `KRIZ_BOLGESI_YARICAP_KM` sabitinin bulunduğu yer): bu dosya
# ARTIK `src.data_ingestion.local_osm_reader`ı HİÇ import ETMEZ — canlı
# kriz akışı hiçbir şekilde `.osm.pbf` dosyası okumaz/taramaz; o iş
# TAMAMEN izole, tek seferlik `seed_db.py` betiğine taşınmıştır.

logger = logging.getLogger(__name__)

# "KİLİT NOKTASI" DEBUG LOGLAMASI — terminalden sistemin Neo4j'de
# mi yoksa LLM'de mi takıldığını görebilmek içindir. Bu
# dosya (Streamlit'in FİİLEN çalıştırdığı giriş noktası) daha önce HİÇ
# `logging.basicConfig` ÇAĞIRMIYORDU — Python'un kök logger'ı yapılandırma
# YOKSA varsayılan olarak SADECE WARNING+ seviyesini (ve "handler of last
# resort" ile minimal bicimde) gösterir; yani `database.py`/`decision_
# engine.py`/`nlp_parser.py` içindeki TÜM `logger.info(...)` çağrıları
# (bu oturumda EKLENEN yeni "Neo4j sorgusu başladı/bitti", "LLM'e
# gönderiliyor/cevap alındı" checkpoint'leri DAHİL) `streamlit run` ile
# başlatılan terminalde GÖRÜNMÜYORDU. `basicConfig` idempotenttir (kök
# logger'da zaten handler varsa hiçbir şey yapmaz) — bu yüzden Streamlit'in
# HER etkileşimde tüm betiği yeniden çalıştırması (rerun) YİNEDEN
# handler EKLEMEZ/çoğaltmaz, güvenlidir.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

# ---------------------------------------------------------------------------
# Sabitler / görsel eşlemeler
# ---------------------------------------------------------------------------

# Facility.mevcut_durum -> kritik/hasarlı kabul edilen değerler.
KRITIK_TESIS_DURUMLARI = ["Hasarlı", "Yok Edildi"]

# --- PyDeck renk sabitleri (RGBA, 0-255) ---
# Enum DEĞERLERİ (Türkçe karakter içerebilir, ör. "Hasarlı") elle string
# olarak yeniden yazılmaz; transkripsiyon hatası (yanlış karakter -> sessizce
# eşleşmeyen renk) riskini önlemek için doğrudan `src.core.models` enum'ları
# üzerinden (`.value`) okunur.

# GÖRSEL ÖLÇEKLENDİRME DÜZELTMESİ (Tesis/Birlik/İdari Alan ColumnLayer
# sütunları "stratosfere uzanan gökdelenler" gibi görünme riskine karşı).
# Bu ölçek ARTIK SADECE İdari Alan katmanı için
# kullanılır (bkz. `_idari_alan_katmani_olustur`) — Tesis/Birlik katmanları
# "DİNAMİK RENK KODLAMASI" düzeltmesiyle (aşağıya bak) ColumnLayer'dan
# ScatterplotLayer'a geçtiği için artık bir `elevation_scale`e ihtiyaç
# duymazlar.
_KOLON_YUKSEKLIK_OLCEGI = 0.15

# "DİNAMİK RENK KODLAMASI" DÜZELTMESİ (TÜM tesis/birimlerin durum-bazlı
# TEK bir renk paletiyle -çoğu "Aktif" olduğundan pratikte hemen hepsi AYNI
# yeşil- boyanması, kriz anında hangi simgenin hastane/itfaiye/polis/askeri
# üs/havalimanı olduğunu GÖRSEL OLARAK AYIRT ETMEYİ İMKANSIZ kılardı).
# Eskiden `TESIS_RENGI`/
# `BIRLIK_RENGI` renk/yükseklik SADECE operasyonel DURUMA (mevcut_durum/
# personel sayısı) göre belirleniyordu; artık renk SADECE TİPE (facility_
# type/unit_type) göre belirlenir — komutanın gözü rengi görür görmez
# "bu bir hastane mi, karakol mu, üs mü" sorusuna ANINDA cevap bulur.
# Hasar/durum bilgisi ARTIK yükseklikle DEĞİL (ScatterplotLayer'da 3B
# yükseklik kavramı yoktur), noktanın ÇEVRE ÇİZGİSİ (stroke) rengiyle
# taşınır — bkz. `_durum_stroke_rengi` ve `_tesis_katmani_olustur`/
# `_birlik_katmani_olustur`: Aktif = donuk/nötr çevre, Hasarlı/Yok Edildi
# = parlak kırmızı çevre. Böylece TİP (dolgu rengi) ve DURUM (çevre
# rengi) İKİ BAĞIMSIZ görsel kanalda AYNI ANDA, birbirini MASKELEMEDEN
# okunabilir.
#
# Belirlenmiş RGB değerleri; Facility.
# facility_type VE Unit.unit_type DEĞERLERİ (Türkçe karakter içerebilir)
# elle string olarak yeniden yazılmaz, doğrudan `src.core.models` enum'ları
# üzerinden (`.value`) okunur — transkripsiyon hatası riskini önler.
TAKTIKSEL_TIP_RENGI: Dict[str, List[int]] = {
    FacilityType.HASTANE.value: [255, 255, 255, 230],       # Beyaz
    UnitType.SAGLIK.value: [255, 255, 255, 230],             # Beyaz
    UnitType.ITFAIYE.value: [255, 50, 50, 230],              # Kirmizi
    UnitType.ARAMA_KURTARMA.value: [255, 50, 50, 230],       # Kirmizi
    UnitType.POLIS.value: [50, 100, 255, 230],               # Mavi
    # "Jandarma" bu semada ayri bir UnitType olarak MODELLENMEMISTIR
    # (bkz. `models.UnitType`) — kullanicinin "Polis/Jandarma" ortak
    # etiketiyle kastettigi "guvenlik gucu" semsiyesi altina, ASKERI_
    # BIRLIK de (jandarma askeri bir guvenlik gucudur) AYNI maviyle dahil
    # edilir.
    UnitType.ASKERI_BIRLIK.value: [50, 100, 255, 230],       # Mavi
    FacilityType.ASKERI_US.value: [85, 107, 47, 235],        # Zeytin yesili
    FacilityType.HAVALIMANI.value: [0, 255, 255, 230],       # Cam gobegi
}
TAKTIKSEL_VARSAYILAN_RENK: List[int] = [128, 128, 128, 210]  # Gri (Diger/Varsayilan)

# "YARIÇAP OPTİMİZASYONU": stratejik öneme sahip
# tipler (Askeri Üs, Havalimanı) standart bir tesis/birimden 1.75 KAT
# daha büyük çizilir ki
# haritada göze ilk çarpan simgeler bunlar olsun.
_TAKTIKSEL_RADIUS_STANDART = 80
_TAKTIKSEL_RADIUS_STRATEJIK = round(_TAKTIKSEL_RADIUS_STANDART * 1.75)
_TAKTIKSEL_STRATEJIK_TIPLER = frozenset({FacilityType.ASKERI_US.value, FacilityType.HAVALIMANI.value})

# "DURUM ÇEVRE RENGİ" (bkz. yukarıdaki "DİNAMİK RENK KODLAMASI" notu):
# Aktif için donuk/nötr (dolgu rengiyle KARIŞMAYAN, göze batmayan) bir
# çevre; Hasarlı/Yok Edildi için parlak/alarm verici kırmızı-turuncu bir
# çevre — komutanın gözü, TİPTEN bağımsız olarak, "hasarlı" noktaları
# haritada ANINDA yakalayabilsin.
_DURUM_STROKE_NOTR = [255, 255, 255, 90]
_DURUM_STROKE_HASARLI = [255, 61, 0, 255]

# "TOOLTİP İKON" DÜZELTMESİ (tooltip'teki "tur" etiketi TÜM tesisler için
# tek bir sabit emoji (🏥) VE tüm birimler için tek bir sabit emoji (🪖)
# kullanırsa, ör. bir havalimanı/askeri üs "🏥 Havalimani" gibi
# YANLIŞ/anlamsız bir ikonla görünür). RGB renk
# kodlamasına (`TAKTIKSEL_TIP_RENGI`) DOKUNULMAZ — bu SADECE tooltip'teki
# metin/ikon eşleştirmesidir. `TAKTIKSEL_TIP_RENGI` ile AYNI gruplamayı
# (Jandarma -> Askeri Birlik) kullanır ki renk ve ikon birbiriyle TUTARLI
# kalsın (aynı nokta hem mavi HEM DE 🚓 gösterir, asla farklı gruplara
# düşmez).
TAKTIKSEL_TIP_IKONU: Dict[str, str] = {
    FacilityType.HASTANE.value: "🏥",
    UnitType.SAGLIK.value: "🏥",
    UnitType.ITFAIYE.value: "🚒",
    UnitType.ARAMA_KURTARMA.value: "🚒",
    UnitType.POLIS.value: "🚓",
    UnitType.ASKERI_BIRLIK.value: "🚓",  # "Jandarma" - bkz. TAKTIKSEL_TIP_RENGI'ndeki AYNI gerekce
    FacilityType.ASKERI_US.value: "⚔️",
    FacilityType.HAVALIMANI.value: "✈️",
}
TAKTIKSEL_VARSAYILAN_IKON = "📍"


def _durum_stroke_rengi(durum: Optional[str]) -> List[int]:
    """Bir Facility/Unit'in operasyonel durumuna göre ScatterplotLayer
    çevre (stroke) rengini döner — bkz. modül-üstü "DURUM ÇEVRE RENGİ"
    notu. `durum` `None`/bilinmiyor ise güvenli tarafta (nötr) kalınır."""
    if durum in (FacilityStatus.HASARLI.value, FacilityStatus.YOK_EDILDI.value):
        return _DURUM_STROKE_HASARLI
    return _DURUM_STROKE_NOTR

# Event.siddet -> etki alanı/marker rengi (koyu zeminde belirgin, "Katastrofik"
# için saf beyaz — siyah zeminde kaybolmasın diye).
OLAY_SIDDET_RENGI: Dict[str, List[int]] = {
    EventSeverity.DUSUK.value: [34, 211, 238, 255],
    EventSeverity.ORTA.value: [255, 176, 32, 255],
    EventSeverity.YUKSEK.value: [255, 59, 59, 255],
    EventSeverity.KRITIK.value: [255, 0, 200, 255],
    EventSeverity.KATASTROFIK.value: [255, 255, 255, 255],
}
_OLAY_VARSAYILAN_RENK = [255, 176, 32, 255]

# Infrastructure.acik_mi -> renk (Köprü noktaları + isimli güzergah çizgileri).
YOL_ACIK_RENGI = [0, 230, 118, 205]
YOL_KAPALI_RENGI = [255, 23, 68, 235]

# GERÇEK VERİ GÜNCELLEMESİ: Sokak (Street) noktaları artık bir HexagonLayer
# yoğunluk bloğu DEĞİL (bkz. `_sokak_nokta_katmani_olustur`) — rastgele mock
# koordinatlar bu katmanda tek bir anlamsız devasa kırmızı bloğa agregе
# oluyordu. Gerçek OSM düğümleri (bkz. `real_osm_loader.RealOsmLoader`) çok
# küçük yarıçaplı, neon mavi TEK TEK noktalar olarak çizilir; şehrin gerçek
# yol ağı böylece bir nokta dokusu ("damar") halinde görülebilir.
SOKAK_RENGI = [56, 189, 248, 200]  # neon mavi

# FAZ 5 (Dinamik Kriz Katmanı İzolasyonu): kapanan sokaklar/yıkılan köprüler,
# statik/sakin arka plan dokusundan (SOKAK_RENGI/YOL_ACIK_RENGI) TAMAMEN
# AYRI, tek başına parlak turuncu-kırmızı bir "alarm" rengiyle çizilir (bkz.
# `fetch_dynamic_crisis_points`/`_kriz_katmani_olustur`) — komutanın gözü
# haritaya baktığı anda kriz noktalarını, binlerce sakin sokak noktasının
# İÇİNDE ARAMAK ZORUNDA KALMADAN, anında yakalamalıdır.
KRIZ_KATMANI_RENGI = [255, 61, 0, 255]

# GÖRSEL C4ISR — TAKTİKSEL SEVK KATMANI (bkz. `_sevk_rotalari_df_olustur`/
# `_taktiksel_sevk_katmani_olustur`): SAKOM Karar Destek Motoru'nun (bkz.
# `decision_engine.en_yakin_ulasilan_varliklari_bul`) GERÇEK yol ağı +
# Dijkstra ile seçtiği birliklerin, olay yerine giden güzergahını "askeri
# sarı/turuncu neon" bir sevk hattı olarak çizmek için kullanılır — kriz
# (kırmızı) ve sakin (mavi) katmanlardan KASITLI olarak FARKLI, üçüncü bir
# renk ailesi: komutanın gözü "nereden nereye birlik gönderiliyor" sorusunu
# haritadaki DİĞER hiçbir renkle KARIŞTIRMADAN anında yakalamalıdır.
SEVK_HATTI_RENGI = [255, 214, 0, 235]  # parlak askeri sarı

# "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI: bağımsız ve yerel bir
# askeri komuta sistemi, kriz anında devasa Tüm Türkiye PBF dosyasını veya
# OSM verilerini canlı olarak taramamalı/indirmemelidir. Eskiden bu
# noktada "Kademeli Dinamik Yükleme" (kriz tespit edildiğinde `local_osm_
# reader.LocalOsmReader.kriz_bolgesi_yukle` ile yerel `.osm.pbf` dosyasını
# ANLIK tarayıp kılcal yol ağını Neo4j'e yazan bir tetikleyici) yaşıyordu.
# BU MİMARİ TAMAMEN SÖKÜLMÜŞTÜR: canlı kriz akışı artık HİÇBİR ŞEKİLDE dosya
# I/O'suna veya yeni harita düğümü (node) yazmaya GİRMEZ — Neo4j'de halihazırda
# ne varsa Karar Motoru SADECE onu okur ("varsa vardır, yoksa yoktur"). Tüm
# ağır PBF tarama/yazma işi artık İZOLE, tek seferlik bir kurulum betiğine
# taşınmıştır: bkz. proje kökündeki `seed_db.py` (sistem İLK KURULURKEN, BİR
# KEZ çalıştırılır) ve `src.data_ingestion.local_osm_reader.LocalOsmReader`
# (o betiğin kullandığı, ama artık bu dosyadan (`app.py`) HİÇ import
# EDİLMEYEN alt katman). Bu bölümdeki eski `KRIZ_BOLGESI_YARICAP_KM`/
# `_MAX_PARALEL_KRIZ_BOLGESI_ISCISI` sabitleri ve onlara bağlı
# `get_local_osm_reader`/`_kriz_bolgesi_yukle_tetikle`/`_kriz_bolgesi_daha_
# once_yuklendi_mi` fonksiyonları (ve "Senaryo Sıfırlama"daki RAM tahliyesi
# çağrısı) bu yüzden KALDIRILDI — `seed_db.py` ile yüklenen harita verisi
# ARTIK KALICIDIR (`gecici_mi=False`), bir kriz senaryosu sıfırlandığında
# SİLİNMEZ, bu yüzden çalışma-anı bir "RAM tahliyesi" kavramına da gerek
# KALMADI.


def _risk_rengi(risk_faktoru: Optional[float]) -> List[int]:
    """AdministrativeArea.risk_faktoru (0.0-1.0) için, düşük riskte neon
    yeşilden yüksek riskte neon kırmızıya doğrusal renk interpolasyonu."""
    r = max(0.0, min(1.0, risk_faktoru or 0.0))
    yesil, kirmizi = (0, 230, 118), (255, 23, 68)
    return [int(yesil[i] + (kirmizi[i] - yesil[i]) * r) for i in range(3)] + [200]


# ---------------------------------------------------------------------------
# Kaynak (resource) yönetimi
# ---------------------------------------------------------------------------
# Streamlit, kullanıcı her etkileşimde (buton tıklaması, vb.) TÜM script'i
# baştan çalıştırır. Bağlantı/istemci gibi durum taşıyan ve kurulumu pahalı
# nesneler (Neo4j bağlantısı + kısıt oluşturma, Ollama istemcisi) bu yüzden
# `st.cache_resource` ile bir kez oluşturulup oturum boyunca yeniden
# kullanılır; aksi halde her tıklamada Neo4j'e 14 adet CREATE CONSTRAINT
# sorgusu ve Ollama istemci kurulumu tekrar tekrar yapılırdı.


@st.cache_resource(show_spinner=False)
def get_connection() -> Neo4jConnection:
    """Neo4j bağlantısını kurar, şema kısıtlarını doğrular ve önbelleğe alır."""
    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()
    return db


@st.cache_resource(show_spinner=False)
def get_parser() -> OllamaParser:
    """Yerel Ollama modeline bağlanan ayrıştırıcıyı kurar ve önbelleğe alır."""
    # bkz. `api.py`daki `get_parser` notu — extraction
    # ve taktiksel öneri görevleri AYRI env değişkenleriyle yapılandırılır.
    return OllamaParser(model=os.getenv("OLLAMA_EXTRACTION_MODEL", "llama3"))


@st.cache_resource(show_spinner=False)
def get_decision_engine() -> DecisionEngine:
    """AI Kurmay Başkanlığı karar destek motorunu (bkz. `src.core.decision_engine`)
    kurar ve önbelleğe alır. `get_connection()` ile AYNI Neo4j singleton'ını
    kullanır; ayrı bir Ollama istemcisi (parser'dan bağımsız, daha "doğal"
    taktiksel dil için biraz daha yüksek `temperature`) açar.

    `temperature` burada AYRICA verilmez — `DecisionEngine.__init__`in
    varsayılanı (bkz. o dosyadaki "PAPAĞAN MODU" notu)
    kullanılır; eskiden burada 0.2 sabitlenmişti, bu ARTIK o varsayılanı
    SESSİZCE EZERDİ (motor tarafındaki iyileştirme UI'a hiç yansımazdı).
    """
    return DecisionEngine(
        get_connection(),
        model=os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay-qwen"),
    )


# ---------------------------------------------------------------------------
# Veri erişim fonksiyonları
# ---------------------------------------------------------------------------
# Katman bazında AYRI sorgular + AYRI TTL'ler kullanılır (eski tek büyük
# `fetch_map_nodes` yerine): Sokak/Köprü/İdari Alan gibi hacimli ama
# krizle ANLIK değişmeyen katmanlar uzun TTL (300 sn) ile önbellekte tutulur;
# Tesis/Birlik/Olay/isimli-Güzergah gibi bir rapor işlendiğinde HEMEN
# güncellenmesi gereken katmanlar kısa TTL (15 sn) kullanır ve
# `_clear_data_caches` ile elle temizlenir.
#
# Etiket sorguları artık Faz 2'nin multi-label mimarisini (bkz.
# `models.BaseNode.neo4j_labels`) DOĞRUDAN kullanır: `MATCH (n:Street)` gibi
# bir sorgu, `MATCH (n:Infrastructure) WHERE n.infrastructure_type = 'Sokak'`
# yerine native Neo4j etiket indeksini kullanarak çalışır — Faz 2'nin asıl
# performans getirisi burada devreye girer.


@st.cache_data(ttl=15, show_spinner=False)
def fetch_metrics(_db: Neo4jConnection) -> Dict[str, int]:
    """Üst paneldeki kriz metriklerini Neo4j'den, veritabanındaki TÜM
    bölgeler/şehirler birlikte (tek bir ulusal ağ olarak) hesaplar.

    GERİ ALMA NOTU (bölgesel dropdown kaldırıldı): Bir önceki sürüm bu
    sorguları "Aktif Operasyon Bölgesi" seçiciyle FİLTRELİYORDU — bu, bir
    Karar Destek Sistemi'nin (C4ISR) doğasına aykırı, şehirleri birbirinden
    yapay olarak koparan bir kısıtlama olarak değerlendirildi. Artık TÜM
    sorgular veritabanındaki HER düğümü kapsar; `bolge` alanı (bkz.
    `real_osm_loader.RealOsmLoader`) düğümde SALT bir provenance/köken
    bilgisi olarak KALIR, herhangi bir filtreleme için KULLANILMAZ.

    (`_db` parametre adı, Streamlit'in cache anahtarına dahil ETMEMESİ
    içindir — başındaki alt çizgi bu anlama gelir; aksi halde Neo4jConnection
    nesnesini hash'lemeye çalışıp hata verirdi.)
    """
    toplam_olay = _db.execute_query("MATCH (e:Event) RETURN count(e) AS adet")[0]["adet"]
    kritik_tesis = _db.execute_query(
        "MATCH (f:Facility) WHERE f.mevcut_durum IN $durumlar RETURN count(f) AS adet",
        {"durumlar": KRITIK_TESIS_DURUMLARI},
    )[0]["adet"]
    kapali_yol = _db.execute_query(
        "MATCH (i:Infrastructure) WHERE i.acik_mi = false RETURN count(i) AS adet"
    )[0]["adet"]
    gorevdeki_birlik = _db.execute_query("MATCH (u:Unit) RETURN count(u) AS adet")[0]["adet"]
    toplam_altyapi = _db.execute_query("MATCH (i:Infrastructure) RETURN count(i) AS adet")[0]["adet"]
    return {
        "toplam_olay": toplam_olay,
        "kritik_tesis": kritik_tesis,
        "kapali_yol": kapali_yol,
        "gorevdeki_birlik": gorevdeki_birlik,
        "toplam_altyapi": toplam_altyapi,
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_street_points(_db: Neo4jConnection, sadece_ana_damarlar: bool = True) -> pd.DataFrame:
    """SAKİN/statik arka plan sokak dokusunu çeker: SADECE AÇIK (`acik_mi`
    `true`/bilinmiyor) sokak noktaları — kapalı sokaklar artık burada
    DEĞİL, ayrı ve parlak bir "kriz" katmanındadır (bkz. FAZ 5 "DİNAMİK KRİZ
    KATMANI İZOLASYONU" — `fetch_dynamic_crisis_points`). Bu ayrım hem
    performans (statik katman artık ASLA kriz yüzünden geçersizleşmez, çok
    uzun TTL ile önbelleklenebilir) hem de görsel netlik (komutanın gözü
    binlerce sakin nokta arasında kırmızı bir noktayı ARAMAK zorunda
    kalmaz) sağlar.

    HİYERARŞİK YOL ÇİZİMİ (LOD FİLTRESİ): `sadece_ana_damarlar=True`
    (varsayılan) iken sadece `ANA_DAMAR_HIGHWAY_TIPLERI` (motorway/trunk/
    primary/secondary) sınıfındaki OSM `highway` etiketine sahip noktalar
    döner — bir şehrin toplam yol ağının BÜYÜK ÇOĞUNLUĞUNU oluşturan
    tertiary/unclassified/residential/living_street "ara sokak" düğümleri
    genel bakışta ÇİZİLMEZ; bu, hem Neo4j'den dönen satır sayısını hem de
    GPU'nun her karede işlemesi gereken nokta sayısını (dolayısıyla "toz
    bulutu" görünümünü) DRAMATİK şekilde azaltır. `highway_tipi IS NULL`
    olan (bu alandan ÖNCE, FAZ 5 öncesi yüklenmiş) ESKİ düğümler BİLİNÇLİ
    OLARAK dışlanmaz/gizlenmez — sınıfı bilinmediği için güvenli tarafta
    kalınıp GÖSTERİLMEYE devam edilir (aksi halde eski bir bölge yeniden
    yüklenene kadar harita boş görünürdü). Tam detay için kullanıcı arayüz
    üzerinden (bkz. `render_map_section`) bu filtreyi kapatabilir.

    ÇOK UZUN TTL (1 saat): bu artık SADECE açık/sakin sokakları içerdiği
    için bir kriz raporu tarafından ASLA geçersiz kılınmaz (`_clear_data_
    caches` bu fonksiyonu bilerek TEMİZLEMEZ) — uygulama açıldığında
    Neo4j'e SADECE BİR KEZ sorgu atılır, sonraki her rerun'da (buton
    tıklama vb.) önbellekten anında döner.
    """
    sorgu = "MATCH (n:Street) WHERE (n.acik_mi IS NULL OR n.acik_mi = true)"
    parametreler: Dict[str, Any] = {}
    if sadece_ana_damarlar:
        sorgu += " AND (n.highway_tipi IS NULL OR n.highway_tipi IN $ana_damarlar)"
        parametreler["ana_damarlar"] = list(ANA_DAMAR_HIGHWAY_TIPLERI)
    sorgu += " RETURN n.enlem AS enlem, n.boylam AS boylam"
    rows = _db.execute_query(sorgu, parametreler)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["renk"] = [SOKAK_RENGI] * len(df)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_bridge_points(_db: Neo4jConnection) -> pd.DataFrame:
    """SADECE AÇIK köprü noktalarını çeker (statik/sakin katman) — yıkılan/
    kapanan köprüler artık burada DEĞİL, `fetch_dynamic_crisis_points`
    içindedir (bkz. FAZ 5 "DİNAMİK KRİZ KATMANI İZOLASYONU")."""
    rows = _db.execute_query(
        "MATCH (n:Bridge) WHERE (n.acik_mi IS NULL OR n.acik_mi = true) "
        "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam, "
        "n.uzunluk_km AS uzunluk_km, n.tonaj_kapasitesi AS tonaj_kapasitesi"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["tur"] = "🌉 Köprü"
    df["detay"] = df.apply(
        lambda r: f"AÇIK · {r.get('uzunluk_km', '—')} km · Tonaj: {r.get('tonaj_kapasitesi', '—')} ton",
        axis=1,
    )
    df["renk"] = [YOL_ACIK_RENGI] * len(df)
    return df


@st.cache_data(ttl=15, show_spinner=False)
def fetch_dynamic_crisis_points(_db: Neo4jConnection) -> pd.DataFrame:
    """FAZ 5 "DİNAMİK KRİZ KATMANI İZOLASYONU": kapanan sokaklar VE yıkılan/
    kapanan köprüleri, `fetch_street_points`/`fetch_bridge_points`teki sakin
    statik katmandan TAMAMEN AYRI, tek bir "aktif kesinti" katmanı olarak
    çeker. Bu ikisi TEK bir dataframe'de birleştirilir çünkü ikisi de aynı
    görsel muameleyi (kalın, parlak turuncu-kırmızı, üste binen) hak eder —
    bkz. `_kriz_katmani_olustur`.

    KISA TTL (15 sn) + `_clear_data_caches`: bu katman krizin GÜNCEL/CANLI
    durumunu yansıtmak ZORUNDADIR; bir rapor işlenip bir sokak/köprü
    kapatıldığında harita ANINDA tepki vermelidir (tıpkı `fetch_event_
    points` gibi — sokak/köprü ağının geri kalanının aksine bu katman
    BİLEREK statik/uzun-TTL DEĞİLDİR).
    """
    rows = _db.execute_query(
        "MATCH (n:Street) WHERE n.acik_mi = false "
        "RETURN n.enlem AS enlem, n.boylam AS boylam, coalesce(n.aciklama, 'Sokak') AS isim, "
        "'🚧 Kapalı Sokak' AS tur, 'Trafiğe KAPALI' AS detay "
        "UNION ALL "
        "MATCH (n:Bridge) WHERE n.acik_mi = false "
        "RETURN n.enlem AS enlem, n.boylam AS boylam, coalesce(n.isim, 'Köprü') AS isim, "
        "'🌉 Yıkık/Kapalı Köprü' AS tur, 'Geçişe KAPALI' AS detay"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["renk"] = [KRIZ_KATMANI_RENGI] * len(df)
    return df


def _tahmini_bitis_noktasi(ozellik: Any) -> Tuple[float, float]:
    """Infrastructure için bitiş koordinatı (bitis_enlem/bitis_boylam) veritabanında
    yoksa (eski veri veya LLM'in üretemediği durumlar), güzergahı YİNE DE tek bir
    nokta değil bir ÇİZGİ olarak gösterebilmek için deterministik bir tahmini
    bitiş noktası üretir.

    Yön (bearing), varlığın `isim` alanının hash'inden türetilir — böylece
    aynı varlık her harita yenilemesinde (rerun) hep AYNI çizgiyi üretir,
    rastgele/titreyen bir görüntü oluşmaz. Mesafe, `uzunluk_km` alanından
    (yoksa 10 km varsayımıyla) kabaca dereceye çevrilir (1° ≈ 111 km).

    `ozellik`, bir dict VEYA bir pandas Series olabilir (her ikisi de
    `[...]`/`.get(...)` destekler).
    """
    uzunluk_km = min(ozellik.get("uzunluk_km") or 10, 200)
    isim = ozellik.get("isim", "") or ""
    bearing_derece = int(hashlib.md5(isim.encode("utf-8")).hexdigest(), 16) % 360
    mesafe_derece = uzunluk_km / 111.0
    bearing_rad = math.radians(bearing_derece)
    delta_enlem = mesafe_derece * math.cos(bearing_rad)
    delta_boylam = mesafe_derece * math.sin(bearing_rad)
    return ozellik["enlem"] + delta_enlem, ozellik["boylam"] + delta_boylam


@st.cache_data(ttl=15, show_spinner=False)
def fetch_road_lines(_db: Neo4jConnection) -> pd.DataFrame:
    """Sokak/Köprü DIŞINDAKİ Infrastructure alt-tiplerini (Karayolu, Demiryolu,
    Tünel, Viyadük — özellikle kriz raporlarıyla oluşan/kapanan isimlendirilmiş
    güzergahlar), veritabanındaki TÜM bölgeler için, tam çizgi geometrisiyle
    çeker. Kısa TTL: "hangi yol kapandı" sorusunun CANLI cevaplandığı katman
    budur (bkz. `render_ai_staff_section` ile aynı önbellek temizleme
    döngüsü, `_clear_data_caches`).
    """
    rows = _db.execute_query(
        "MATCH (n:Infrastructure) WHERE NOT n:Street AND NOT n:Bridge "
        "RETURN n.isim AS isim, n.infrastructure_type AS tip, n.enlem AS enlem, n.boylam AS boylam, "
        "n.bitis_enlem AS bitis_enlem, n.bitis_boylam AS bitis_boylam, n.acik_mi AS acik_mi, "
        "n.uzunluk_km AS uzunluk_km"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    def _bitis(row: pd.Series) -> Tuple[float, float]:
        if row.get("bitis_enlem") is not None and row.get("bitis_boylam") is not None:
            return row["bitis_enlem"], row["bitis_boylam"]
        return _tahmini_bitis_noktasi(row)

    bitisler = df.apply(_bitis, axis=1)
    df["bitis_enlem_final"] = bitisler.apply(lambda t: t[0])
    df["bitis_boylam_final"] = bitisler.apply(lambda t: t[1])
    df["renk"] = df["acik_mi"].apply(lambda x: YOL_ACIK_RENGI if x else YOL_KAPALI_RENGI)
    df["genislik"] = df["acik_mi"].apply(lambda x: 2 if x else 7)
    df["tur"] = df["tip"].apply(lambda t: f"🛣️ {t}" if t else "🛣️ Güzergah")
    df["detay"] = df.apply(
        lambda r: f"{'AÇIK' if r['acik_mi'] else '🚧 KAPALI'} · {r.get('uzunluk_km', '—')} km", axis=1
    )
    return df


@st.cache_data(ttl=15, show_spinner=False)
def fetch_facility_points(_db: Neo4jConnection) -> pd.DataFrame:
    """Facility (`:Facility` — Hastane/Havalimanı/Askeri Üs/Liman/Sığınak)
    noktalarını, veritabanındaki TÜM bölgeler için çeker.

    `COALESCE(n.aciklama, n.isim)`: GERCEK OSM hastanelerinde (bkz.
    `local_osm_reader._amenity_dugumu_uret` — "ULUSAL İSİM ÇAKIŞMASI"
    düzeltmesi) `isim` benzersizlik için bir OSM tip/id soneki taşır;
    haritadaki tooltip'te bu teknik kimlik DEĞİL, `aciklama`daki TEMİZ
    gerçek isim gösterilmelidir (bkz. `fetch_recent_report`'taki AYNI desen)."""
    rows = _db.execute_query(
        "MATCH (n:Facility) RETURN COALESCE(n.aciklama, n.isim) AS isim, "
        "n.enlem AS enlem, n.boylam AS boylam, "
        "n.facility_type AS facility_type, n.mevcut_durum AS mevcut_durum, n.kapasite AS kapasite"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # "TOOLTİP İKON" DÜZELTMESİ (bkz. modül-üstü `TAKTIKSEL_TIP_IKONU`
    # notu): ikon ARTIK sabit "🏥" DEĞİL, GERÇEK `facility_type`e göre.
    df["tur"] = df["facility_type"].apply(
        lambda t: f"{TAKTIKSEL_TIP_IKONU.get(t, TAKTIKSEL_VARSAYILAN_IKON)} {t}" if t else f"{TAKTIKSEL_VARSAYILAN_IKON} Tesis"
    )
    df["detay"] = df.apply(
        lambda r: f"Durum: {r.get('mevcut_durum', '—')} · Kapasite: {r.get('kapasite', '—')}", axis=1
    )
    # "DİNAMİK RENK KODLAMASI" (bkz. modül-üstü `TAKTIKSEL_TIP_RENGI` notu):
    # dolgu rengi ARTIK duruma değil TİPE göre (hastane=beyaz, askeri
    # üs=zeytin yeşili, havalimanı=cam göbeği, vb.); durum bilgisi ayrı
    # bir kanalda (`stroke_renk`, çevre çizgisi) taşınır.
    df["renk"] = df["facility_type"].apply(lambda t: TAKTIKSEL_TIP_RENGI.get(t, TAKTIKSEL_VARSAYILAN_RENK))
    df["stroke_renk"] = df["mevcut_durum"].apply(_durum_stroke_rengi)
    # "YARIÇAP OPTİMİZASYONU": Askeri Üs/Havalimanı standarttan 1.75 kat
    # büyük çizilir (bkz. `_TAKTIKSEL_STRATEJIK_TIPLER`).
    df["radius"] = df["facility_type"].apply(
        lambda t: _TAKTIKSEL_RADIUS_STRATEJIK if t in _TAKTIKSEL_STRATEJIK_TIPLER else _TAKTIKSEL_RADIUS_STANDART
    )
    return df


@st.cache_data(ttl=15, show_spinner=False)
def fetch_unit_points(_db: Neo4jConnection) -> pd.DataFrame:
    """Unit (`:Unit` — Askeri Birlik/AFAD/Sağlık/İtfaiye/Polis) noktalarını,
    veritabanındaki TÜM bölgeler için çeker.

    `COALESCE(n.aciklama, n.isim)`: GERCEK OSM karakol/itfaiyelerinde (bkz.
    `local_osm_reader._amenity_dugumu_uret` — "ULUSAL İSİM ÇAKIŞMASI"
    düzeltmesi) `isim` benzersizlik için bir OSM tip/id soneki taşır;
    haritadaki tooltip'te bu teknik kimlik DEĞİL, `aciklama`daki TEMİZ
    gerçek isim gösterilmelidir."""
    rows = _db.execute_query(
        "MATCH (n:Unit) RETURN COALESCE(n.aciklama, n.isim) AS isim, "
        "n.enlem AS enlem, n.boylam AS boylam, "
        "n.unit_type AS unit_type, n.personel_sayisi AS personel_sayisi, "
        "n.hareket_kabiliyeti AS hareket_kabiliyeti, n.durum AS durum"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # "TOOLTİP İKON" DÜZELTMESİ (bkz. modül-üstü `TAKTIKSEL_TIP_IKONU`
    # notu): ikon ARTIK sabit "🪖" DEĞİL, GERÇEK `unit_type`a göre.
    df["tur"] = df["unit_type"].apply(
        lambda t: f"{TAKTIKSEL_TIP_IKONU.get(t, TAKTIKSEL_VARSAYILAN_IKON)} {t}" if t else f"{TAKTIKSEL_VARSAYILAN_IKON} Birlik"
    )
    df["detay"] = df.apply(
        lambda r: f"{r.get('personel_sayisi', '—')} personel · {r.get('hareket_kabiliyeti', '—')}", axis=1
    )
    # "DİNAMİK RENK KODLAMASI" (bkz. modül-üstü `TAKTIKSEL_TIP_RENGI` notu):
    # dolgu rengi ARTIK sabit "taktiksel mavi" DEĞİL, `unit_type`e göre
    # (itfaiye/arama-kurtarma=kırmızı, polis/askeri birlik=mavi, sağlık=
    # beyaz, vb.); durum bilgisi ayrı bir kanalda (`stroke_renk`) taşınır.
    df["renk"] = df["unit_type"].apply(lambda t: TAKTIKSEL_TIP_RENGI.get(t, TAKTIKSEL_VARSAYILAN_RENK))
    df["stroke_renk"] = df["durum"].apply(_durum_stroke_rengi)
    df["radius"] = _TAKTIKSEL_RADIUS_STANDART
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_admin_area_points(_db: Neo4jConnection) -> pd.DataFrame:
    """AdministrativeArea (`:AdministrativeArea` — Mahalle/Köy/İlçe/Bölge)
    noktalarını, veritabanındaki TÜM bölgeler için çeker. Uzun TTL: idari/
    demografik veri bir kriz raporuyla ANLIK değişmez (bkz.
    `tucbs_etl_loader.py` — bu katman ayrı bir ETL sürecinden beslenir)."""
    rows = _db.execute_query(
        "MATCH (n:AdministrativeArea) RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam, "
        "n.alan_tipi AS alan_tipi, n.nufus AS nufus, n.bina_sayisi AS bina_sayisi, "
        "n.risk_faktoru AS risk_faktoru"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["tur"] = df["alan_tipi"].apply(lambda t: f"📍 {t}" if t else "📍 Bölge")
    df["detay"] = df.apply(
        lambda r: f"Nüfus: {r.get('nufus', '—')} · Risk: %{round((r.get('risk_faktoru') or 0) * 100)}", axis=1
    )
    df["renk"] = df["risk_faktoru"].apply(_risk_rengi)
    df["yukseklik"] = df["risk_faktoru"].fillna(0).apply(lambda r: 200 + float(r) * 3000)
    return df


@st.cache_data(ttl=15, show_spinner=False)
def fetch_event_points(_db: Neo4jConnection) -> pd.DataFrame:
    """Event (`:Event`) noktalarını, veritabanındaki TÜM bölgeler için,
    etki alanı çemberi için gerekli alanlarla çeker."""
    rows = _db.execute_query(
        "MATCH (n:Event) RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam, "
        "n.event_type AS event_type, n.siddet AS siddet, n.etki_alani_km AS etki_alani_km"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["tur"] = df["event_type"].apply(lambda t: f"⚠️ {t}" if t else "⚠️ Olay")
    df["detay"] = df.apply(
        lambda r: f"Şiddet: {r.get('siddet', '—')} · Etki: {r.get('etki_alani_km', '—')} km", axis=1
    )
    df["renk"] = df["siddet"].apply(lambda s: OLAY_SIDDET_RENGI.get(s, _OLAY_VARSAYILAN_RENK))
    df["yaricap_metre"] = df["etki_alani_km"].fillna(0).apply(lambda km: max(float(km), 0) * 1000)
    return df


@st.cache_data(ttl=30, show_spinner=False)
def fetch_ai_recommendations(_db: Neo4jConnection, _engine: DecisionEngine) -> Dict[str, Any]:
    """AI Kurmay Başkanlığı'nın taktiksel önerilerini, veritabanındaki TÜM
    bölgeler/şehirler birlikte değerlendirilerek üretir/önbellekten döner
    (bkz. `DecisionEngine.generate_recommendations`).

    Bu bir LLM çağrısı olduğundan (metrik/harita sorgularına göre çok daha
    pahalı), `fetch_metrics`/harita katmanlarından daha uzun bir TTL (30 sn)
    kullanılır; ayrıca her rapor işlendiğinde diğerleriyle birlikte elle
    temizlenir (bkz. `_clear_data_caches`) ki yeni veri işlendiğinde öneriler
    de anında güncellensin. `_db`/`_engine` parametre adlarındaki alt çizgi,
    Streamlit'in bu nesneleri cache anahtarına dahil ETMEMESİ (hash'lemeye
    çalışmaması) içindir.
    """
    return _engine.generate_recommendations()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_recent_report(_db: Neo4jConnection) -> pd.DataFrame:
    """Son olayları ve AFFECTS/LOCATED_IN ilişkisiyle etkiledikleri
    varlıkları tablolar.

    "ETKİLENEN VARLIKLAR BOŞ KALIYOR" DÜZELTMESİ: `etkilenen.isim`
    DOĞRUDAN gösterilmez — gerçek OSM sokak/köprü düğümlerinde `isim`
    her zaman bir OSM soneki taşır (ör. "Vali Fahri Bey Caddesi (OSM
    way/123 #7)"); bunun yerine `COALESCE(etkilenen.aciklama, etkilenen.isim)`
    kullanılır (temiz gerçek isim, bkz. `real_osm_loader.RealOsmLoader`) ve
    her varlığa türünü belirten bir ek (ör. "(Altyapı)") eklenir — tıpkı
    istenen "Vali Fahri Bey Caddesi (Altyapı)" biçiminde. Ayrıca, ilişki
    artık sadece `AFFECTS` DEĞİL `AFFECTS|LOCATED_IN` ile aranır (bkz.
    `nlp_parser`daki "İLİŞKİ ZORUNLULUĞU KURALI" — model ikisinden birini
    kullanabilir). Bu sorgunun `db.add_relationship`'in GERÇEK OSM
    düğümlerine doğru ilişki kurabilmesi için `src.ui.app._grafa_yaz`daki
    "sokak isim eşlemesi" düzeltmesine de bakınız.
    """
    rows = _db.execute_query(
        "MATCH (e:Event) "
        "OPTIONAL MATCH (e)-[:AFFECTS|LOCATED_IN]->(etkilenen) "
        "WITH e, collect(DISTINCT CASE WHEN etkilenen IS NULL THEN NULL ELSE "
        "  COALESCE(etkilenen.aciklama, etkilenen.isim) + ' (' + "
        "  CASE "
        "    WHEN etkilenen:Facility THEN 'Tesis' "
        "    WHEN etkilenen:Infrastructure THEN 'Altyapı' "
        "    WHEN etkilenen:Unit THEN 'Birim' "
        "    WHEN etkilenen:EnergyInfrastructure THEN 'Enerji Altyapısı' "
        "    WHEN etkilenen:CommunicationNetwork THEN 'İletişim Altyapısı' "
        "    WHEN etkilenen:ResourceHub THEN 'Kaynak Merkezi' "
        "    WHEN etkilenen:AdministrativeArea THEN 'İdari Alan' "
        "    ELSE 'Varlık' "
        "  END + ')' END) AS etkilenenler_ham "
        "RETURN e.isim AS olay, e.event_type AS tur, e.siddet AS siddet, "
        "e.zaman_damgasi AS zaman, etkilenenler_ham "
        "ORDER BY e.zaman_damgasi DESC"
    )
    for row in rows:
        temiz = [ad for ad in row.pop("etkilenenler_ham") if ad is not None]
        row["etkilenenler"] = ", ".join(temiz) if temiz else "—"

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.rename(
        columns={
            "olay": "Olay",
            "tur": "Tür",
            "siddet": "Şiddet",
            "zaman": "Zaman",
            "etkilenenler": "Etkilenen Varlıklar",
        }
    )


# ---------------------------------------------------------------------------
# 3B Taktiksel Harita (PyDeck / deck.gl)
# ---------------------------------------------------------------------------
# Katman stratejisi (her biri veri hacmine/doğasına göre seçilmiştir):
#   Sokak (Street)                -> ScatterplotLayer: GERÇEK OSM sokak düğüm
#                                     koordinatları (bkz. `real_osm_loader`),
#                                     çok küçük yarıçaplı (radius=15) neon
#                                     mavi TEK TEK noktalar olarak çizilir —
#                                     onbinlerce nokta bir yoğunluk bloğuna
#                                     agregе EDİLMEZ, gerçek yol ağı dokusu
#                                     ("şehir damarları") olarak görünür.
#   Köprü (Bridge)                -> ScatterplotLayer: sayıca çok daha az ve
#                                     her biri kritik bir "darboğaz" olduğu
#                                     için TEK TEK, tıklanabilir noktalar.
#   Karayolu/Demiryolu/Tünel/
#   Viyadük (isimli güzergahlar)  -> LineLayer: gerçek başlangıç-bitiş
#                                     geometrisiyle çizilen, açık/kapalı
#                                     renkli güzergah çizgileri (kriz
#                                     raporlarıyla oluşan yollar burada).
#   Facility / Unit               -> ScatterplotLayer: dolgu rengi TİPE
#                                     göre (hastane/itfaiye/polis/askeri
#                                     üs/havalimanı — bkz. `TAKTIKSEL_TIP_
#                                     RENGI`), çevre çizgisi DURUMA göre
#                                     (Hasarlı/Yok Edildi = alarm kırmızısı,
#                                     bkz. `_durum_stroke_rengi`), yarıçap
#                                     stratejik tipler için büyütülmüş.
#   AdministrativeArea             -> ColumnLayer: yükseklik + renk
#                                     `risk_faktoru`'na göre (yeşil->kırmızı).
#   Event etki alanı               -> yarı saydam, parlayan kırmızı/neon
#                                     ScatterplotLayer ("glow" + merkez noktası).
#
# ÖNEMLİ (deck.gl konvansiyonu): Tüm konum accessor'ları [BOYLAM, ENLEM]
# (longitude, latitude) sırasındadır — Folium'un [enlem, boylam] sırasının
# TERSİ. Bu dosyadaki her `get_position`/`get_source_position` çağrısı
# BİLİNÇLİ olarak bu sırayı takip eder.

_TOOLTIP_STIL = {
    "backgroundColor": "#17171a",
    "color": "#f5f5f5",
    "border": "1px solid #ff3b3b",
    "fontFamily": "Consolas, 'Courier New', monospace",
    "fontSize": "12px",
    "padding": "8px 10px",
    "borderRadius": "4px",
}

# Tek, ortak bir tooltip şablonu: her katmanın DataFrame'i `isim`/`tur`/`detay`
# kolonlarını doldurur (bkz. yukarıdaki fetch_* fonksiyonları); böylece
# hangi katman "pick" edilirse edilsin aynı şablon anlamlı bir sonuç üretir.
# (İstisna: Sokak (Street) ScatterplotLayer'ı ham `isim`/`tur`/`detay`
# taşımadığından bilinçli olarak `pickable=False`'tur, bu şablona hiç girmez.)
_TOOLTIP = {"html": "<b>{isim}</b><br/>{tur}<br/>{detay}", "style": _TOOLTIP_STIL}


def _sokak_nokta_katmani_olustur(sokak_df: pd.DataFrame) -> Optional[pdk.Layer]:
    """GERÇEK OSM sokak düğüm koordinatlarını (bkz. `real_osm_loader.
    RealOsmLoader`), devasa/anlamsız bir HexagonLayer yoğunluk bloğu YERİNE,
    ÇOK küçük ve YARI SAYDAM noktalar halinde çizer — böylece şehrin gerçek
    yol ağı ("sokak damarları") üst üste bindiğinde tek tek noktalardan
    oluşan bir "toz bulutu" DEĞİL, yolun ana hattını belli eden ince bir
    neon damar/çizgi gibi görünür (bkz. FAZ 5 "GÖRSEL TEMİZLİK" — `radius`/
    `opacity` bilerek DÜŞÜRÜLMÜŞTÜR: `SOKAK_RENGI`nin kendi alfa kanalı 200
    iken katmanın `opacity`si 0.45'e çekilerek ÇİFT katmanlı bir yumuşatma
    uygulanır). ARTIK SADECE AÇIK sokakları taşır (bkz. `fetch_street_
    points`) — kapalı sokaklar `_kriz_katmani_olustur`da ayrı ve parlak
    çizilir; bu yüzden burada rengi `acik_mi`ye göre DEĞİŞTİRME mantığı
    YOKTUR. `isim`/`tur`/`detay` taşımadığından bilinçli olarak
    `pickable=False`'tur."""
    if sokak_df.empty:
        return None
    return pdk.Layer(
        "ScatterplotLayer",
        id="sokak-noktalari",
        data=sokak_df,
        get_position=["boylam", "enlem"],
        get_fill_color="renk",
        get_radius=8,
        radius_min_pixels=1,
        radius_max_pixels=2,
        opacity=0.45,
        stroked=False,
        pickable=False,
    )


def _kriz_katmani_olustur(kriz_df: pd.DataFrame) -> Optional[pdk.Layer]:
    """FAZ 5 "DİNAMİK KRİZ KATMANI İZOLASYONU": kapanan sokaklar + yıkılan/
    kapanan köprüleri (bkz. `fetch_dynamic_crisis_points`), sakin statik
    dokudan (`_sokak_nokta_katmani_olustur`/köprü katmanı) TAMAMEN AYRI,
    KALIN (büyük yarıçap) ve TAM OPAK, parlak turuncu-kırmızı (bkz.
    `KRIZ_KATMANI_RENGI`) bir katman olarak çizer. `build_deck` içinde
    katman listesinin EN SONUNA eklenir ki diğer TÜM katmanların ÜSTÜNE
    binsin — komutanın gözü haritaya bakar bakmaz aktif kesintiyi
    yakalamalıdır. `pickable=True`: bu, operasyonel açıdan EN kritik
    katmandır, tıklanabilir/vurgulanabilir olması GEREKİR."""
    if kriz_df.empty:
        return None
    return pdk.Layer(
        "ScatterplotLayer",
        id="dinamik-kriz-noktalari",
        data=kriz_df,
        get_position=["boylam", "enlem"],
        get_fill_color="renk",
        get_radius=60,
        radius_min_pixels=4,
        radius_max_pixels=14,
        stroked=True,
        get_line_color=[255, 255, 255, 200],
        line_width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )


def _sevk_rotalari_df_olustur(ai_sonuc: Dict[str, Any]) -> pd.DataFrame:
    """GÖRSEL C4ISR — TAKTİKSEL SEVK KATMANI: `fetch_ai_recommendations`in
    döndürdüğü SAKOM sonucundaki `durum.en_yakin_ulasilan_birlikler` (bkz.
    `decision_engine.SituationalPicture` / `Neo4jConnection.
    en_yakin_ulasilan_varliklari_bul`) sözlüğünü, PyDeck `PathLayer`'ın
    doğrudan tüketebileceği düz bir DataFrame'e çevirir.

    Her satır TEK bir "olay yeri -> birlik" sevk güzergahını temsil eder;
    `path` sütunu deck.gl'in beklediği `[boylam, enlem]` (lon/lat, NOT
    enlem/boylam) sıralı nokta listesidir — projedeki DİĞER TÜM katmanlar
    `get_position=["boylam","enlem"]` KULLANIRKEN, `PathLayer`'ın `path`
    alanı ayrı bir koordinat DİZİSİ olduğundan bu dönüşüm BURADA, tek bir
    yerde yapılır (aksi halde harita ters/yanlış bir yöne çizilirdi).

    Gerçek güzergahı yeniden inşa EDEMEMİŞ (bkz. `_yolu_yeniden_insa_et`
    — boş `rota_noktalari`) satırlar SESSİZCE atlanır: yarım/hatalı bir
    sevk çizgisi ASLA çizilmez, sadece o birliğin çizgisi eksik kalır
    (mesafe bilgisi yine de metin raporunda görünür durumda kalır).
    """
    if ai_sonuc.get("durum_bos") or ai_sonuc.get("cografi_red"):
        return pd.DataFrame()

    durum = ai_sonuc.get("durum")
    birlikler_haritasi = getattr(durum, "en_yakin_ulasilan_birlikler", None) if durum else None
    if not birlikler_haritasi:
        return pd.DataFrame()

    satirlar: List[Dict[str, Any]] = []
    for olay_ismi, birlikler in birlikler_haritasi.items():
        for birlik in birlikler:
            rota = birlik.get("rota_noktalari")
            if not rota or len(rota) < 2:
                continue
            # `birlik["isim"]`, `Neo4jConnection.en_yakin_ulasilan_varliklari_
            # bul`den GELEN HAM düğüm verisidir (bkz. o fonksiyonun
            # docstring'i) — GERÇEK OSM Polis/İtfaiye birimlerinde (bkz.
            # `local_osm_reader._amenity_dugumu_uret` — "ULUSAL İSİM
            # ÇAKIŞMASI" düzeltmesi) bu bir OSM tip/id soneki TAŞIYABİLİR;
            # `decision_engine._temiz_isim` ile AYNI temizleyici burada da
            # kullanılır ki haritadaki sevk etiketi de metin raporuyla
            # TUTARLI, temiz bir isim göstersin.
            temiz = _temiz_isim(birlik)
            satirlar.append(
                {
                    "olay_ismi": olay_ismi,
                    "isim": temiz,
                    "tur": f"🚨 Sevk: {temiz} → {olay_ismi}",
                    "detay": f"{birlik.get('mesafe_km', '?')} km gerçek yol mesafesi · güzergah AÇIK",
                    "mesafe_km": birlik.get("mesafe_km"),
                    # deck.gl PathLayer: [lon, lat] sirali nokta dizisi.
                    "path": [[boylam, enlem] for enlem, boylam in rota],
                }
            )
    return pd.DataFrame(satirlar)


def _taktiksel_sevk_katmani_olustur(sevk_df: pd.DataFrame) -> List[pdk.Layer]:
    """GÖRSEL C4ISR — TAKTİKSEL SEVK KATMANI: SAKOM'un GERÇEK yol ağı +
    Dijkstra ile seçtiği birliklerin olay yerine giden güzergahını, statik
    sokak dokusunun VE dinamik kriz katmanının bile ÜSTÜNDE, parlak askeri
    sarı (bkz. `SEVK_HATTI_RENGI`) bir `PathLayer` olarak çizer — bir
    "glow" (parlama) hissi için kalın bir dış-çizgi + daha ince, tam opak
    bir iç-çizgi İKİ ayrı katman olarak üst üste bindirilir (tıpkı
    `_olay_etki_katmanlari_olustur`daki glow deseniyle AYNI teknik).
    `build_deck`da katman listesinin EN SONUNA eklenir (bkz. o fonksiyonun
    yorumu) — "Raporu Analiz Et" dendiği an LLM metniyle EŞZAMANLI olarak
    haritada beliren, en üst z-seviyeli dinamik katmandır."""
    if sevk_df.empty:
        return []

    glow_katmani = pdk.Layer(
        "PathLayer",
        id="taktiksel-sevk-glow",
        data=sevk_df,
        get_path="path",
        get_color=[SEVK_HATTI_RENGI[0], SEVK_HATTI_RENGI[1], SEVK_HATTI_RENGI[2], 70],
        get_width=9,
        width_min_pixels=6,
        width_max_pixels=16,
        pickable=False,
        cap_rounded=True,
        joint_rounded=True,
    )
    cizgi_katmani = pdk.Layer(
        "PathLayer",
        id="taktiksel-sevk-hatti",
        data=sevk_df,
        get_path="path",
        get_color=SEVK_HATTI_RENGI,
        get_width=3,
        width_min_pixels=2,
        width_max_pixels=6,
        pickable=True,
        auto_highlight=True,
        cap_rounded=True,
        joint_rounded=True,
    )
    return [glow_katmani, cizgi_katmani]  # glow ONCE (altta), tam-opak cizgi SONRA (ustte).


def _kopru_katmani_olustur(kopru_df: pd.DataFrame) -> Optional[pdk.Layer]:
    if kopru_df.empty:
        return None
    return pdk.Layer(
        "ScatterplotLayer",
        id="kopru-noktalari",
        data=kopru_df,
        get_position=["boylam", "enlem"],
        get_fill_color="renk",
        get_radius=90,
        radius_min_pixels=5,
        radius_max_pixels=20,
        stroked=True,
        get_line_color=[255, 255, 255, 180],
        line_width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )


def _yol_cizgi_katmani_olustur(yol_df: pd.DataFrame) -> Optional[pdk.Layer]:
    if yol_df.empty:
        return None
    return pdk.Layer(
        "LineLayer",
        id="isimli-guzergahlar",
        data=yol_df,
        get_source_position=["boylam", "enlem"],
        get_target_position=["bitis_boylam_final", "bitis_enlem_final"],
        get_color="renk",
        get_width="genislik",
        pickable=True,
        auto_highlight=True,
    )


def _tesis_katmani_olustur(tesis_df: pd.DataFrame) -> Optional[pdk.Layer]:
    """"DİNAMİK RENK KODLAMASI" DÜZELTMESİ (Kurucu UX tespiti — bkz. modül-
    üstü `TAKTIKSEL_TIP_RENGI` notu): eskiden `ColumnLayer` (3B sütun,
    renk/yükseklik DURUMA göre) kullanılıyordu — neredeyse tüm tesisler
    "Aktif" olduğundan harita pratikte TEK bir yeşil sütun ormanı gibi
    görünüyor, hastane/askeri üs/havalimanı GÖRSEL olarak AYIRT
    EDİLEMİYORDU. Artık düz bir `ScatterplotLayer`: dolgu rengi TİPE göre
    (bkz. `fetch_facility_points`), yarıçap stratejik tipler için (Askeri
    Üs/Havalimanı) BÜYÜTÜLMÜŞ, çevre çizgisi (stroke) İSE duruma göre
    (Hasarlı/Yok Edildi = alarm kırmızısı) — TİP ve DURUM iki BAĞIMSIZ
    görsel kanalda birbirini MASKELEMEDEN okunur."""
    if tesis_df.empty:
        return None
    return pdk.Layer(
        "ScatterplotLayer",
        id="tesisler",
        data=tesis_df,
        get_position=["boylam", "enlem"],
        get_fill_color="renk",
        get_radius="radius",
        radius_min_pixels=5,
        radius_max_pixels=22,
        stroked=True,
        get_line_color="stroke_renk",
        line_width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )


def _birlik_katmani_olustur(birlik_df: pd.DataFrame) -> Optional[pdk.Layer]:
    """"DİNAMİK RENK KODLAMASI" DÜZELTMESİ — bkz. `_tesis_katmani_olustur`
    docstring'indeki AYNI gerekçe (eskiden TÜM birimler sabit "taktiksel
    mavi" bir `ColumnLayer` sütunuydu; artık `unit_type`e göre renklenen
    bir `ScatterplotLayer`)."""
    if birlik_df.empty:
        return None
    return pdk.Layer(
        "ScatterplotLayer",
        id="birlikler",
        data=birlik_df,
        get_position=["boylam", "enlem"],
        get_fill_color="renk",
        get_radius="radius",
        radius_min_pixels=5,
        radius_max_pixels=22,
        stroked=True,
        get_line_color="stroke_renk",
        line_width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )


def _idari_alan_katmani_olustur(admin_df: pd.DataFrame) -> Optional[pdk.Layer]:
    """İdari alan (mahalle/il sınırı vb.) arka plan sütunları — ÜLKE ÇAPINDA
    (81 il) yüklemede bu katman diğerlerine göre en KALABALIK olabilen
    kategorilerden biridir ve tek tek tıklanabilir/vurgulanabilir bir "kritik
    tesis" DEĞİL, salt görsel bağlam/zemin verisidir. `_sokak_nokta_katmani_
    olustur` ile AYNI GPU-tasarrufu mantığıyla `pickable=False` (ve dolayısıyla
    `auto_highlight` YOK) — GPU'nun her karede milyonlarca sütun için picking
    buffer'ı hesaplamasını önler. Operasyonel açıdan KRİTİK katmanlar (Tesis,
    Birlik, Köprü, adlı yollar, Olay) bundan MUAF tutulup `pickable=True`
    bırakılmıştır (bkz. ilgili katman fonksiyonları)."""
    if admin_df.empty:
        return None
    return pdk.Layer(
        "ColumnLayer",
        id="idari-alanlar",
        data=admin_df,
        get_position=["boylam", "enlem"],
        get_elevation="yukseklik",
        elevation_scale=_KOLON_YUKSEKLIK_OLCEGI,
        radius=220,
        get_fill_color="renk",
        opacity=0.75,
        pickable=False,
    )


def _olay_etki_katmanlari_olustur(olay_df: pd.DataFrame) -> List[pdk.Layer]:
    """Bir olayın etki alanını yarı saydam, parlayan kırmızı/neon bir "glow"
    (ScatterplotLayer, düşük opaklık) + tam opak küçük bir merkez (epicenter)
    noktası olarak İKİ katman halinde çizer. (Önceki Folium tabanlı
    `_neon_etki_cemberi_ekle` mantığının PyDeck karşılığıdır.)"""
    if olay_df.empty:
        return []

    glow_df = olay_df.copy()
    glow_df["glow_renk"] = glow_df["renk"].apply(lambda r: [r[0], r[1], r[2], 55])

    glow_katmani = pdk.Layer(
        "ScatterplotLayer",
        id="olay-etki-alani",
        data=glow_df,
        get_position=["boylam", "enlem"],
        get_radius="yaricap_metre",
        get_fill_color="glow_renk",
        stroked=True,
        get_line_color="renk",
        line_width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    merkez_katmani = pdk.Layer(
        "ScatterplotLayer",
        id="olay-merkez-noktasi",
        data=olay_df,
        get_position=["boylam", "enlem"],
        get_radius=70,
        radius_min_pixels=5,
        radius_max_pixels=14,
        get_fill_color=[255, 255, 255, 255],
        stroked=True,
        get_line_color="renk",
        line_width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    return [glow_katmani, merkez_katmani]


def _varsayilan_gorunum() -> pdk.ViewState:
    """Veri yokken kullanılan varsayılan kamera: Elazığ bölgesi (bkz.
    `osm_loader.ELAZIG_BBOX`) — mevcut/gelecek tüm pilot veri buradadır.
    `pitch=45`/`bearing=0`, haritanın 3B derinliğinin en başından belirgin
    olması için sabitlenmiştir."""
    guney, bati, kuzey, dogu = ELAZIG_BBOX
    return pdk.ViewState(
        latitude=(guney + kuzey) / 2,
        longitude=(bati + dogu) / 2,
        zoom=11,
        pitch=45,
        bearing=0,
    )


def _dinamik_gorunum_hesapla(nokta_df: pd.DataFrame) -> pdk.ViewState:
    """Veritabanindaki TÜM varlık konumlarına göre kamerayı dinamik olarak
    kadrajlar ("kör uçuş" sorunu #3: harita gerçek veri yerine tüm Orta
    Doğu'yu gösteriyordu).

    Ham min/max sınırları YERİNE 2.-98. YÜZDELİK DİLİM (percentile) aralığı
    kullanılır: `nlp_parser`daki "KOORDINAT KALIBRASYONU" kuralı, metinde
    AÇIKÇA farklı bir ülke/bölge (ör. "İsrail") belirtildiğinde bilinçli
    olarak o bölgenin gerçekçi koordinatını üretebilir; TEK BİR böyle aykırı
    (outlier) nokta, binlerce gerçek Elazığ sokak/tesis noktasından oluşan
    asıl veri kümesinin kamerasını "kilitleyip" tüm bölgeyi (ör. Orta Doğu)
    gösterecek kadar dışarı itmemelidir — kamera, verinin ASIL yoğunlaştığı
    kümeye odaklanmalıdır (bkz. sorun tanımı: "Eğer veri sadece Elazığ'daysa
    kamera direkt oraya odaklanmalı").
    """
    if len(nokta_df) < 5:
        # Çok az nokta varken yüzdelik dilim istatistiksel olarak anlamsızdır;
        # ham sınırlarla `compute_view`a bırakılır.
        return compute_view(nokta_df[["boylam", "enlem"]].values.tolist())

    alt_sinir = nokta_df.quantile(0.02)
    ust_sinir = nokta_df.quantile(0.98)
    kirpilmis = nokta_df[
        nokta_df["boylam"].between(alt_sinir["boylam"], ust_sinir["boylam"])
        & nokta_df["enlem"].between(alt_sinir["enlem"], ust_sinir["enlem"])
    ]
    if kirpilmis.empty:
        kirpilmis = nokta_df
    return compute_view(kirpilmis[["boylam", "enlem"]].values.tolist())


def build_deck(
    sokak_df: pd.DataFrame,
    kopru_df: pd.DataFrame,
    yol_df: pd.DataFrame,
    tesis_df: pd.DataFrame,
    birlik_df: pd.DataFrame,
    admin_df: pd.DataFrame,
    olay_df: pd.DataFrame,
    kriz_df: pd.DataFrame,
    sevk_df: pd.DataFrame,
) -> pdk.Deck:
    """Tüm katmanları tek bir `pdk.Deck`'te birleştirir; kamerayı mevcut
    verinin sınırlarına göre otomatik kadrajlar (`compute_view`), ama
    `pitch`/`bearing`'i HER ZAMAN 45/0'a sabitler (3B derinlik hep belirgin).

    KATMAN SIRASI BİLİNÇLİDİR: `_taktiksel_sevk_katmani_olustur` (GÖRSEL
    C4ISR — SAKOM'un sevk güzergahları) listenin KESİN EN SONUNDADIR;
    `_kriz_katmani_olustur` (kapalı sokak/köprü) dahil diğer TÜM
    katmanların ÜSTÜNE biner — komutan "Raporu Analiz Et" dediği an bu
    sarı sevk hatları, haritadaki HER ŞEYİN üzerinde belirir.
    """
    katmanlar: List[pdk.Layer] = []
    for katman in (
        _sokak_nokta_katmani_olustur(sokak_df),
        _idari_alan_katmani_olustur(admin_df),
        _yol_cizgi_katmani_olustur(yol_df),
        _kopru_katmani_olustur(kopru_df),
        *_olay_etki_katmanlari_olustur(olay_df),
        _tesis_katmani_olustur(tesis_df),
        _birlik_katmani_olustur(birlik_df),
        _kriz_katmani_olustur(kriz_df),
        *_taktiksel_sevk_katmani_olustur(sevk_df),
    ):
        if katman is not None:
            katmanlar.append(katman)

    tum_nokta_parcalari: List[pd.DataFrame] = []
    for df in (sokak_df, kopru_df, yol_df, tesis_df, birlik_df, admin_df, olay_df, kriz_df):
        if not df.empty and {"boylam", "enlem"}.issubset(df.columns):
            tum_nokta_parcalari.append(df[["boylam", "enlem"]].dropna())

    if tum_nokta_parcalari:
        gorunum = _dinamik_gorunum_hesapla(pd.concat(tum_nokta_parcalari, ignore_index=True))
        gorunum.pitch = 45
        gorunum.bearing = 0
        # GERÇEK VERİ GÜNCELLEMESİ: gerçek OSM sokak düğümleri artık tüm şehri
        # (Elazığ merkez + çevresi) kaplıyor; `compute_view` bazen tek tek
        # noktaların sıkışıklığına göre aşırı yakınlaştırabildiğinden, üst
        # sınır 15'ten 13'e düşürüldü ki şehrin TAMAMI tek bakışta net görünsün.
        gorunum.zoom = min(max(gorunum.zoom, 9), 13)
    else:
        gorunum = _varsayilan_gorunum()

    return pdk.Deck(
        layers=katmanlar,
        initial_view_state=gorunum,
        map_provider="carto",
        map_style=pdk.map_styles.CARTO_DARK,  # Mapbox token GEREKTİRMEZ.
        tooltip=_TOOLTIP,
    )


# ---------------------------------------------------------------------------
# Arayüz bölümleri
# ---------------------------------------------------------------------------

# Event.siddet -> son durum raporu tablosundaki hücre arka plan rengi. Koyu,
# tonu şiddetle orantılı renkler; "sıradan" bir tablo yerine komuta merkezi
# raporlama panelinin ciddiyetini yansıtır.
_SIDDET_HUCRE_RENGI = {
    "Dusuk": "#0d3b45",
    "Orta": "#4a3300",
    "Yuksek": "#4a0f16",
    "Kritik": "#3a0a4a",
    "Katastrofik": "#262626",
}


def _siddet_hucre_stili(deger: Any) -> str:
    renk = _SIDDET_HUCRE_RENGI.get(deger)
    if not renk:
        return ""
    return f"background-color: {renk}; color: #f5f5f5; font-weight: 600;"


def inject_tactical_theme() -> None:
    """Panele, arka plandaki Neo4j/Ollama mimarisinin ciddiyetini yansıtan
    kırmızı/siyah, "taktiksel komuta merkezi" görünümü kazandıran CSS.

    `.streamlit/config.toml` genel koyu temayı (arka plan/metin/vurgu rengi)
    belirlerken, burası daha ince ayrıntıları (metrik kartı çerçeveleri,
    başlık parıltısı, kenarlıklar) tamamlar.
    """
    st.markdown(
        """
        <style>
        .stApp { background-color: #0b0b0d; }

        h1 {
            color: #ff3b3b !important;
            font-family: 'Consolas', 'Courier New', monospace;
            letter-spacing: .03em;
            text-shadow: 0 0 16px rgba(255, 59, 59, .35);
        }
        h2, h3 {
            font-family: 'Consolas', 'Courier New', monospace;
            letter-spacing: .02em;
            color: #f0f0f0 !important;
        }

        [data-testid="stMetric"] {
            background: linear-gradient(145deg, #17171a, #1f1f23);
            border: 1px solid #3a3a3f;
            border-left: 4px solid #ff3b3b;
            border-radius: 6px;
            padding: 14px 16px 10px 16px;
        }
        [data-testid="stMetricLabel"] {
            color: #b7b7bd !important;
            font-weight: 600;
            letter-spacing: .05em;
            text-transform: uppercase;
            font-size: .78rem !important;
        }
        [data-testid="stMetricValue"] {
            color: #f5f5f5 !important;
            font-family: 'Consolas', 'Courier New', monospace;
        }

        section[data-testid="stSidebar"] {
            background-color: #111113;
            border-right: 1px solid rgba(255, 59, 59, .2);
        }
        section[data-testid="stSidebar"] h2 {
            color: #ff3b3b !important;
        }

        div[data-testid="stDataFrame"] {
            border: 1px solid #3a3a3f;
            border-radius: 6px;
            overflow: hidden;
        }

        hr { border-color: rgba(255, 59, 59, .2) !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_top_metrics(metrikler: Dict[str, int]) -> None:
    """Üst panel: Neo4j'den anlık çekilen kriz metrik kartları."""
    kol1, kol2, kol3, kol4, kol5 = st.columns(5)
    kol1.metric("🔥 Toplam Olay Sayısı", metrikler["toplam_olay"])
    kol2.metric("🏚️ Kritik / Hasarlı Tesisler", metrikler["kritik_tesis"])
    kol3.metric("🚧 Kapalı Yollar / Güzergahlar", metrikler["kapali_yol"])
    kol4.metric("🪖 Görevdeki Birlikler", metrikler["gorevdeki_birlik"])
    kol5.metric("🛣️ Kayıtlı Altyapı Segmenti", f"{metrikler['toplam_altyapi']:,}".replace(",", "."))


# ---------------------------------------------------------------------------
# Hazır kriz senaryoları (demo kütüphanesi)
# ---------------------------------------------------------------------------
# Canlı bir sunumda "boş bir metin kutusuna rapor yazma" beklemek yerine, tek
# tıkla yüklenebilen, birbirinden farklı üç kriz tipini (doğal afet / askeri-
# sabotaj / kombine sel+enerji krizi) kapsayan hazır senaryolar sunulur. Her
# senaryo, serbest metin girişiyle TAM AYNI boru hattından (Ollama ->
# Pydantic -> Neo4j, bkz. `_rapor_metnini_isle_ve_yenile`) geçer; böylece
# demo, gerçek kullanım akışının birebir aynısını gösterir.
HAZIR_SENARYOLAR: Dict[str, str] = {
    "🌍 Doğal Afet — Elazığ Depremi": (
        "Elazığ merkezli 6.8 büyüklüğünde şiddetli bir deprem meydana geldi. "
        "Fırat Üniversitesi Eğitim ve Araştırma Hastanesi ağır hasar aldı ve "
        "hizmet veremiyor. Ankara-Elazığ karayolu heyelan nedeniyle ulaşıma "
        "tamamen kapandı. AFAD'a bağlı bir arama kurtarma ekibi bölgeye sevk "
        "edildi. Bölgedeki yakıt deposunun stok seviyesi kritik seviyeye düştü."
    ),
    "🪖 Askeri / Sabotaj — Havalimanı ve Fiber Hat Saldırısı": (
        "Diyarbakır ve Malatya havalimanlarının pistleri sabotaj sonucu "
        "kullanılamaz hale geldi. Bölgedeki ana fiber optik iletişim hattı "
        "kesildiği için çok sayıda baz istasyonu devre dışı kaldı. 2. "
        "Kolordu'ya bağlı bir zırhlı birlik bölgeye doğru hareket halinde."
    ),
    "🌊 Kombine Kriz — Sel ve Enerji Altyapısı Çöküşü": (
        "Şiddetli bir sel felaketi, Keban Barajı çevresindeki trafo merkezini "
        "sular altında bıraktı. Bölgede elektrik tamamen kesildi ve iletişim "
        "ağları çöktü. Bölgeye acil jeneratör desteği gönderilmesi gerekiyor."
    ),
}


def _clear_data_caches() -> None:
    """Harita/metrik/rapor/AI-önerisi kısa süreli önbelleklerini elle
    temizler; yeni bir rapor/senaryo Bilgi Grafı'na işlendiğinde ya da
    veritabanı sıfırlandığında bir sonraki çalıştırmada (rerun) tüm panelin
    güncel veriyi göstermesi için çağrılır.

    KASITLI OLARAK TEMİZLENMEYEN İKİ FONKSİYON (FAZ 5 "STATİK HARİTA KATMANI
    ÖNBELLEKLEMESİ"): `fetch_street_points` ve `fetch_bridge_points` artık
    SADECE açık/sakin (kriz-dışı) düğümleri döndürür ve bilerek ÇOK UZUN bir
    TTL (1 saat) taşır — bunları burada temizlemek, bu optimizasyonun TÜM
    amacını (740 bin düğümlük sorgunun sadece uygulama açılışında BİR KEZ
    çalışması) boşa çıkarırdı. Bir sokak/köprü kapandığında harita YİNE DE
    ANINDA tepki verir çünkü o artık `fetch_dynamic_crisis_points`
    (aşağıda temizlenir, kısa TTL'li) üzerinden, üste binen parlak bir
    katmanla gösterilir — bkz. `_kriz_katmani_olustur`."""
    fetch_metrics.clear()
    fetch_dynamic_crisis_points.clear()
    fetch_road_lines.clear()
    fetch_facility_points.clear()
    fetch_unit_points.clear()
    fetch_admin_area_points.clear()
    fetch_event_points.clear()
    fetch_recent_report.clear()
    fetch_ai_recommendations.clear()


# OSM'den GERCEKTEN yuklenen (bkz. `real_osm_loader.RealOsmLoader.build_nodes`)
# tek Infrastructure alt-tipleri bunlardir; "NO GHOST INFRASTRUCTURE"
# politikasi (bkz. `_grafa_yaz`) SADECE bunlar icin gecerlidir — Karayolu/
# Tunel/Viyaduk bu sistemde HENUZ OSM'den yuklenmedigi icin (bkz.
# `real_osm_loader.py` modul docstring'i), bunlar icin LLM HALA TEK
# otorite kaynagidir ve meşru sekilde yeni bir dugum acilmaya devam eder.
_OSM_KAYNAKLI_ALTYAPI_TIPLERI = frozenset({InfrastructureType.SOKAK, InfrastructureType.KOPRU})

# "İSİM ÇAKIŞMASI" DÜZELTMESİ SONRASI GENİŞLETME: omurga
# yüklemesinden (bkz. `local_osm_reader.omurga_yukle`) GERÇEKTEN gelen
# Facility/Unit alt-tipleri bunlardır — yukarıdaki `_OSM_KAYNAKLI_ALTYAPI_
# TIPLERI` ile AYNI "NO GHOST" mantığı burada da uygulanır: bir rapor
# "Devlet Hastanesi ağır hasarlı" derse, sistem sahte koordinatlı YENİ bir
# Facility YARATMAK yerine GERÇEK OSM hastanesini bulup güncellemelidir
# (bkz. `_osm_kaynakli_gercek_varlik_mi`).
# "FAZ 2: TAKTİKSEL KATMANLAR" EKLENTİSİ: Askeri Üs +
# Havalimanı artık `local_osm_reader.omurga_yukle` ile (bkz. `OMURGA_
# LANDUSE_TIPLERI`/`OMURGA_AEROWAY_TIPLERI`) GERÇEKTEN OSM'den YÜKLENDİĞİ
# için buraya da eklendi — aksi halde bir rapor "İncirlik Hava Üssü"nden
# bahsettiğinde, sistem GERÇEK OSM tesisini bulmak yerine sahte koordinatlı
# bir ghost Facility yaratırdı (Hastane'nin başına gelen AYNI hata sınıfı).
# Liman/Sığınak (Facility) VE AFAD/Sağlık/Askeri Birlik/Ağır Mühendislik/
# Arama Kurtarma/Lojistik (Unit) bu ülke ölçeğinde HÂLÂ OSM'den YÜKLENMEZ
# — bunlar için LLM hâlâ TEK otorite kaynağıdır ve mevcut `add_node`/
# `_resolve_canonical_isim` (isim-bazlı fuzzy eşleme) akışına DOKUNULMAZ;
# bu tipler hiçbir zaman bir OSM soneki TAŞIMADIĞINDAN (hiç OSM'den
# yüklenmedikleri için) bu akış onlar için zaten SORUNSUZ çalışmaya devam eder.
_OSM_KAYNAKLI_FACILITY_TIPLERI = frozenset(
    {FacilityType.HASTANE, FacilityType.ASKERI_US, FacilityType.HAVALIMANI}
)
_OSM_KAYNAKLI_UNIT_TIPLERI = frozenset({UnitType.POLIS, UnitType.ITFAIYE})


def _osm_kaynakli_gercek_varlik_mi(varlik: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """`varlik` OSM'den GERÇEKTEN yüklenen bir alt-tipteyse (Sokak/Köprü
    Infrastructure, Hastane Facility, Polis/İtfaiye Unit), "NO GHOST"
    eşleştirmesinde kullanılacak `(neo4j_etiketi, guncellemeler)` çiftini
    döner; değilse (ör. bir Karayolu/Havalimanı/AFAD birimi — OSM'den HİÇ
    yüklenmeyen bir tip) `None` döner ve çağıran taraf normal `add_node`
    akışına devam eder (bkz. `_OSM_KAYNAKLI_ALTYAPI_TIPLERI`/`_OSM_
    KAYNAKLI_FACILITY_TIPLERI`/`_OSM_KAYNAKLI_UNIT_TIPLERI` docstring'i)."""
    if isinstance(varlik, Infrastructure) and varlik.infrastructure_type in _OSM_KAYNAKLI_ALTYAPI_TIPLERI:
        return "Infrastructure", {"acik_mi": varlik.acik_mi, "durum": varlik.durum.value}
    if isinstance(varlik, Facility) and varlik.facility_type in _OSM_KAYNAKLI_FACILITY_TIPLERI:
        return "Facility", {"mevcut_durum": varlik.mevcut_durum.value, "durum": varlik.durum.value}
    if isinstance(varlik, Unit) and varlik.unit_type in _OSM_KAYNAKLI_UNIT_TIPLERI:
        return "Unit", {"durum": varlik.durum.value}
    return None


def _grafa_yaz(
    db: Neo4jConnection, sonuc: Dict[str, Any]
) -> Tuple[int, int, int, int, int, int, int, List[Tuple[float, float]]]:
    """`OllamaParser.extract_entities` çıktısındaki düğüm/ilişki adaylarını
    Neo4j'e yazar; yazılan (YENİ düğüm sayısı, ilişki sayısı, GÜNCELLENEN
    gerçek altyapı düğümü sayısı, ENGELLENEN ghost-infrastructure sayısı,
    ENGELLENEN sahte-koordinat sayısı, COĞRAFİ BAĞLAMA ile KURTARILAN
    kayıt sayısı, HARİCİ GEOKODLAMA ile KURTARILAN kayıt sayısı, YAZILAN
    Event düğümlerinin NİHAİ [(enlem, boylam), ...] listesi) sekizlisini
    döner.

    Son eleman: bir Event, yukarıdaki Coğrafi Bağlama/Harici Geokodlama
    kurtarmalarından SONRAKİ NİHAİ (gerçek) koordinatıyla derlenir.
    ("OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI, bkz. modül başındaki AYNI
    başlıklı not): bu liste eskiden çağıran tarafça (`_kriz_bolgesi_yukle_
    tetikle`) krizin çevresindeki kılcal yol ağını bir `.osm.pbf`
    dosyasından ANLIK tarayıp Neo4j'e yüklemek için kullanılıyordu — o
    çağıran taraf TAMAMEN KALDIRILDI; bu son eleman artık HİÇBİR ŞEKİLDE
    dosya I/O'sunu TETİKLEMEZ, sadece geriye dönük uyumluluk için (dönüş
    imzasını değiştirmemek adına) hesaplanmaya devam eder.

    "MUTLAK KİLİT" / SAHTE KOORDİNAT KAPANI (kanıtlanmış bir boşluk için
    savunma — DÜZELTME #2): "No Ghost Infrastructure" zırhı SADECE
    Infrastructure'ı, SADECE İSİM eşleştirme başarısızlığına karşı korur.
    GERÇEK köprü doğru eşleşse AMA aynı raporun ürettiği `Event`in KENDİ
    `enlem`/`boylam`ı, modelin
    few-shot örneğinden (Van senaryosu) KOPYALADIĞI bir değere ("38.5,
    43.4" — gerçek yüklü OSM verisinin ÇOK dışında, haritanın öbür ucu)
    sahip çıktı; isim-eşleştirme bunu YAKALAYAMAZ çünkü sorun İSİM değil
    KOORDİNATTIR. Bu yüzden, aşağıdaki döngüde `db.add_node`'a giden HER
    TÜR varlık (Infrastructure'a özel DEĞİL — Facility/Unit/Event/vb. dahil
    TÜMÜ), `db.koordinat_gercekci_mi` ile GERÇEK OSM verisinin kapladığı
    dinamik coğrafi ZARFA (bkz. `db.get_real_koordinat_zarfi`) karşı
    kontrol edilir; zarfın AÇIKÇA dışına düşen HİÇBİR düğüm/olay
    yazılmaz — SADECE ve SADECE gerçek yüklü bölgenin (+ "civarı" toleransı)
    içindeki koordinatlar kabul edilir.

    "COĞRAFİ BAĞLAMA" (DÜZELTME #3: "Sivrice depremi" gibi bir senaryoda
    bu kapan Event'i SESSİZCE reddedip ekranda "HİÇBİR geçerli
    varlık/ilişki çıkarılamadı" hatasına yol açabiliyordu):
    yukarıdaki kapan bir varlığı implausible bulduğunda, ARTIK doğrudan
    reddetmez — ÖNCE `db._yer_adindan_gercek_merkez_koordinat_bul` ile bir
    KURTARMA denemesi yapılır: varlığın isminde geçen bir yer adı (ör.
    "Sivrice Depremi" -> "Sivrice") GERÇEK yüklü Street/Bridge verisinde
    aranır; eşleşirse koordinat o GERÇEK yerin merkez noktasına güncellenir
    ve varlık YİNE DE yazılır (`yer_adiyla_baglanan` sayaca eklenir).
    Eşleşme YOKSA (gerçekten hiçbir gerçek bağlam bulunamadıysa) ESKİ
    (reddetme) davranış KORUNUR — bu, "hiçbir zaman sahte/uydurma bir
    koordinatla bir düğüm yazma" ilkesini BOZMAZ, sadece reddetmeden ÖNCE
    gerçek bir alternatif olup olmadığına BAKAR.

    "KESİN VARLIK EŞLEŞTİRME" / NO GHOST INFRASTRUCTURE: OSM'den
    GERÇEKTEN yüklenen alt-tiplerden biri olan
    (`_OSM_KAYNAKLI_ALTYAPI_TIPLERI` — Sokak, Köprü) bir `Infrastructure`
    (ör. "Kömürhan Köprüsü", "Vali Fahri Bey Caddesi") ASLA doğrudan
    `add_node` ile YENİ/sahte-koordinatlı bir kopya ("ghost node") olarak
    yazılmaz — önce `db.update_infrastructure_status_by_name` ile GERÇEK
    OSM altyapı ağı (bkz. `real_osm_loader.RealOsmLoader`, `aciklama`
    alanı) içinde bulanık isim eşleştirmesiyle aranır:
      - Eşleşme BULUNURSA: GERÇEK düğüm(ler) güncellenir (kapatılan bir
        caddenin/köprünün OSM'de onlarca noktası olabilir, HEPSİ).
      - Eşleşme BULUNAMAZSA: sistem ASLA sıfırdan koordinatsız/hayali bir
        Infrastructure YARATMAZ — bu varlık TAMAMEN ATLANIR (`add_node`
        ÇAĞRILMAZ). Bu durum SESSİZCE geçilmez; `engellenen_ghost` sayacına
        eklenir ve çağıran taraf (bkz. `_rapor_metnini_isle_ve_yenile`) bunu
        kullanıcıya açıkça bildirir. Olayın (Event) KENDİSİ yine de normal
        şekilde yazılır (aşağıdaki döngüde `Infrastructure` DIŞINDAKİ tüm
        varlık tipleri gibi işlem görür) — sadece bu köprüye/caddeye
        `AFFECTS` ilişkisi kurulamaz (uç nokta grafta yok, bkz. `db.
        relationship_to_cypher`'ın `MATCH` tabanlı, sessizce 0-satır dönen
        davranışı).
    Karayolu/Tünel/Viyadük gibi OSM'den HENÜZ yüklenmeyen tipler bu
    kısıtlamaya TABİ DEĞİLDİR (bkz. `_OSM_KAYNAKLI_ALTYAPI_TIPLERI`
    docstring'i) — LLM bunlar için tek meşru kaynak olduğundan normal
    `add_node` ile yazılmaya devam ederler.

    İLİŞKİ DÜZELTMESİ ("Etkilenen Varlıklar" sütununun boş kalması sorunu):
    Bir olay, LLM'in ürettiği TEMİZ isme (ör. "Kömürhan Köprüsü") bir
    `AFFECTS` ilişkisiyle bağlanmak istese bile, gerçek OSM düğümlerinin
    `isim`i HER ZAMAN bir OSM soneki taşıdığından (ör. "...Köprüsü (OSM
    way/123)") hiçbir düğümün `isim`i o temiz isimle TAM eşleşmez —
    `db.add_relationship` bu durumda ilişkiyi SESSİZCE kuramaz. Bu yüzden
    `update_infrastructure_status_by_name`'in döndürdüğü TEMSİLİ GERÇEK isim
    (`temsili_isim`) bir eşleme sözlüğünde (`altyapi_isim_eslemesi`) tutulur;
    `relationships` yazılmadan ÖNCE her `kaynak_id`/`hedef_id` bu sözlükte
    varsa GERÇEK isme YENİDEN eşlenir — böylece ilişki, caddenin/köprünün
    gerçekten var olan bir düğümüne kurulur ve `fetch_recent_report`
    tablosunda görünür hâle gelir.
    """
    yazilan_dugum = 0
    altyapi_guncellemeleri = 0
    engellenen_ghost = 0
    engellenen_sahte_koordinat = 0
    yer_adiyla_baglanan = 0
    harici_geokodlama_ile_baglanan = 0
    yazilan_event_koordinatlari: List[Tuple[float, float]] = []
    # SAHTE KOORDINAT KAPANI: gercek OSM zarfi, RAPOR BASINA BIR KEZ hesaplanir
    # (bkz. `Neo4jConnection.get_real_koordinat_zarfi` — 60 sn'lik onbellegi
    # de vardir); ETL bulk-insert yolunda ASLA cagrilmaz, SADECE burada.
    koordinat_zarfi = db.get_real_koordinat_zarfi()
    # LLM'in urettigi TEMIZ altyapi ismi -> GERCEK OSM dugumunun kendi `isim`i.
    altyapi_isim_eslemesi: Dict[str, str] = {}
    for anahtar in ENTITY_MODEL_MAP:
        for varlik in sonuc.get(anahtar, []):
            osm_eslesme = _osm_kaynakli_gercek_varlik_mi(varlik)
            if osm_eslesme is not None:
                etiket, guncellemeler = osm_eslesme
                guncellenen, temsili_isim = db.update_infrastructure_status_by_name(
                    varlik.isim, guncellemeler, label=etiket
                )
                if guncellenen > 0:
                    altyapi_guncellemeleri += guncellenen
                    if temsili_isim:
                        altyapi_isim_eslemesi[varlik.isim] = temsili_isim
                    continue  # gercek dugum(ler) guncellendi, GHOST NODE ACILMAZ.
                # KRITIK (No Ghost Infrastructure/Facility/Unit): eslesme YOK
                # -> bu varlik icin add_node HICBIR SEKILDE cagrilmaz; sadece sayilir.
                logger.warning(
                    "Ghost %s ENGELLENDI: '%s' icin gercek bir OSM eslesmesi "
                    "bulunamadi; sahte koordinatli dugum YARATILMADI.", etiket, varlik.isim,
                )
                engellenen_ghost += 1
                continue

            # KRITIK (Sahte Koordinat Kapani — TUM varlik tipleri icin, sadece
            # Infrastructure DEGIL): koordinat gercek OSM zarfinin ACIKCA
            # disindaysa (ör. Van'a "sicrayan" bir Event), bu varlik HICBIR
            # SEKILDE yazilmaz. Bu varlige referans veren iliskiler de
            # (asagidaki dongude) uc nokta bulamayip sessizce kurulamaz.
            #
            # "COĞRAFİ BAĞLAMA" ÖNCELİK SIRASI DÜZELTMESİ (kanıtlanmış bir
            # boşluk için savunma: "Gebze Kimyasal Cadde Deposu" krizi,
            # koordinatı DENİZİN ORTASINA yerleştirebiliyordu). KÖK SEBEP: `koordinat_gercekci_
            # mi`, SADECE ülke-çapında KABA bir zarf (bounding box) kontrolü
            # yapar — kara/deniz ayrımı YAPMAZ (bkz. o fonksiyonun docstring'i:
            # "SADECE ... zarfın AÇIKÇA dışına düşen ... koordinatları
            # REDDETMEK içindir"). Gebze kıyı şeridinde olduğundan, LLM'in
            # hatalı-ama-ülke-sınırları-içi tahmini bu zarfı GEÇERDİ ve daha
            # güvenilir isim-bazlı doğrulama (aşağısı) HİÇ ÇALIŞTIRILMAZDI —
            # ESKİ kod SADECE "implausible" (zarf dışı) koordinatlarda
            # doğrulama deniyordu, "plausible ama YANLIŞ" (denizde ama ülke
            # sınırları içinde) durumunu YAKALAYAMIYORDU.
            #
            # DÜZELTME: isim-bazlı doğrulama artık LLM koordinatının
            # plausible olup OLMAMASINDAN BAĞIMSIZ, HER ZAMAN önce denenir —
            # GERÇEK, isimlendirilmiş bir yerin (bkz. `Neo4jConnection.
            # _yer_adindan_gercek_merkez_koordinat_bul`) konumu, bir LLM'in
            # ham ondalık enlem/boylam TAHMİNİNDEN her zaman daha güvenilirdir
            # (LLM'ler hassas koordinat üretmede doğası gereği zayıftır —
            # bu, sadece "implausible" durumlarda değil, "plausible ama
            # birkaç km/onlarca km YANLIŞ" durumlarda da geçerlidir). Eşleşme
            # bulunursa LLM'in koordinatı SESSİZCE GÖRMEZDEN GELİNİR ve
            # gerçek yer koordinatı kullanılır.
            gercek_merkez = db._yer_adindan_gercek_merkez_koordinat_bul(varlik.isim)
            if gercek_merkez is not None:
                varlik.enlem, varlik.boylam = gercek_merkez
                yer_adiyla_baglanan += 1
                logger.info(
                    "COĞRAFİ BAĞLAMA ile KONUM DOĞRULANDI: '%s' için isimdeki yer adına göre "
                    "gerçek merkez koordinat (%.5f, %.5f) kullanıldı (LLM'in ham tahmini yerine — "
                    "hassas koordinat üretiminde isim-bazlı doğrulama her zaman önceliklidir).",
                    varlik.isim, gercek_merkez[0], gercek_merkez[1],
                )
            elif not db.koordinat_gercekci_mi(varlik.enlem, varlik.boylam, koordinat_zarfi):
                # Yerel isim eşleşmesi YOK ve LLM'in koordinatı ülke zarfının
                # AÇIKÇA dışında (ör. Van'a "sıçrayan" bir Event) — "ULUSAL
                # ÖLÇEKLİ ESNEKLİK" 3. (SON) KADEME olarak harici geocoding
                # (Nominatim) denenir — bkz. `harici_geokodlama_ile_koordinat_
                # bul` docstring'i. Bu kademe BAŞARILI olursa, dönen koordinat
                # GERÇEK dünya konumu olduğu için (LLM hallucinasyonu DEĞİL)
                # `koordinat_zarfı`nın dışında kalsa BİLE kabul edilir — o
                # bölge için henüz yerel yol-ağı/birlik verisi olmayabilir,
                # ama DecisionEngine bunu (bkz. `en_yakin_ulasilan_
                # varliklari_bul`) DÜRÜSTÇE "BULUNAMADI" diye yansıtacaktır,
                # sessizce yanlış bir öneri ÜRETMEYECEKTİR.
                harici_koordinat = db.harici_geokodlama_ile_koordinat_bul(varlik.isim)
                if harici_koordinat is not None:
                    varlik.enlem, varlik.boylam = harici_koordinat
                    harici_geokodlama_ile_baglanan += 1
                    logger.info(
                        "HARİCİ GEOKODLAMA ile KURTARILDI: '%s' için ne LLM koordinatı ne "
                        "yerel veri eşleşmesi vardı; Nominatim üzerinden gerçek konuma "
                        "(%.5f, %.5f) bağlandı.", varlik.isim, harici_koordinat[0], harici_koordinat[1],
                    )
                else:
                    logger.warning(
                        "Sahte Koordinat ENGELLENDI: '%s' (enlem=%s, boylam=%s) gercek yuklu "
                        "OSM bolgesinin ACIKCA disinda, isminde eslesen bir yerel yer adi "
                        "YOK VE harici geokodlama da basarisiz oldu; dugum YARATILMADI.",
                        varlik.isim, varlik.enlem, varlik.boylam,
                    )
                    engellenen_sahte_koordinat += 1
                    continue

            db.add_node(varlik)
            yazilan_dugum += 1
            if isinstance(varlik, Event):
                yazilan_event_koordinatlari.append((varlik.enlem, varlik.boylam))

    yazilan_iliski = 0
    for iliski in sonuc.get("relationships", []):
        # Iliski, LLM'in temiz altyapi ismine referans veriyorsa, bunu az
        # yukarida bulunan GERCEK dugum ismine yeniden esle (bkz. yukaridaki
        # "ILISKI DUZELTMESI" aciklamasi). Engellenen bir ghost'a referans
        # veriyorsa eslesme sozlugunde YOKTUR; `db.add_relationship` bu
        # durumda uc nokta bulamayip SESSIZCE 0 satirla doner (bkz. yukaridaki
        # "NO GHOST INFRASTRUCTURE" aciklamasi) — cokme YOKTUR.
        iliski.kaynak_id = altyapi_isim_eslemesi.get(iliski.kaynak_id, iliski.kaynak_id)
        iliski.hedef_id = altyapi_isim_eslemesi.get(iliski.hedef_id, iliski.hedef_id)
        db.add_relationship(iliski)
        yazilan_iliski += 1

    return (
        yazilan_dugum, yazilan_iliski, altyapi_guncellemeleri,
        engellenen_ghost, engellenen_sahte_koordinat, yer_adiyla_baglanan,
        harici_geokodlama_ile_baglanan, yazilan_event_koordinatlari,
    )


# NOT (bkz. modül başındaki "OFFLINE-FIRST / SADECE NEO4J MİMARİ KARARI"):
# eskiden burada `_kriz_bolgesi_daha_once_yuklendi_mi`/`_kriz_bolgesi_yukle_
# tetikle` fonksiyonları vardı — bir kriz raporu işlendiğinde `.osm.pbf`
# dosyasını ANLIK tarayıp Neo4j'e yeni kılcal yol düğümü YAZIYORLARDI. Bu
# İKİ FONKSİYON DA TAMAMEN KALDIRILDI: canlı kriz akışı artık hiçbir
# koşulda dosya I/O'suna girmez veya yeni harita düğümü eklemeye çalışmaz.


def _rapor_metnini_isle_ve_yenile(
    db: Neo4jConnection, parser: OllamaParser, metin: str, kaynak_etiketi: str
) -> None:
    """Bir metni (serbest rapor VEYA hazır senaryo) Ollama ile analiz edip
    Bilgi Grafı'na işler, önbellekleri temizler ve paneli yeniler.

    Serbest metin rapor girişi ile hazır senaryo kütüphanesi AYNI bu
    fonksiyonu kullanır; böylece iki giriş yolu arasında davranış farkı
    (hata yönetimi, önbellek temizleme, rerun) oluşmaz.

    HATA GÖRÜNÜRLÜĞÜ DÜZELTMESİ: Eskiden `OllamaParser`in Pydantic tarafından
    reddettiği kayıtlar SADECE loglanıyordu (kullanıcı hiçbir şey görmüyordu,
    "0 varlık işlendi" yeşil bir "✅ başarılı" banner'ının içinde kayboluyor,
    kullanıcıya sistem "tepkisiz kalmış" gibi görünüyordu). Artık:
      1. `sonuc["validation_hatalari"]` (bkz. `OllamaParser.extract_entities`)
         doluysa, atlanan HER kayıt `st.warning` ile AÇIKÇA listelenir.
      2. Hiçbir varlık/ilişki/sokak-güncellemesi YAZILMADIYSA (toplam sıfır),
         bu asla yeşil "✅ başarılı" olarak raporlanmaz — `st.error` ile
         AÇIKÇA başarısızlık gösterilir.
    """
    with st.sidebar:
        try:
            with st.spinner("Ollama metni analiz ediyor..."):
                sonuc = parser.extract_entities(metin)
            with st.spinner("Bilgi Grafı güncelleniyor (Neo4j)..."):
                (
                    yazilan_dugum,
                    yazilan_iliski,
                    altyapi_guncellemeleri,
                    engellenen_ghost,
                    engellenen_sahte_koordinat,
                    yer_adiyla_baglanan,
                    harici_geokodlama_ile_baglanan,
                    yazilan_event_koordinatlari,
                ) = _grafa_yaz(db, sonuc)
        except (RuntimeError, ValueError, Neo4jConnectionError) as exc:
            logger.error("CRITICAL UI ERROR (rapor işleme): %s", exc, exc_info=True)
            print(f"CRITICAL UI ERROR (rapor işleme): {exc}")
            st.error(f"🛑 İstihbarat işlenirken hata oluştu: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - KASITLI: beklenmeyen HİÇBİR hata sessizce yutulmasın.
            # "ARAYÜZ HATALARINI KUSTUR": yukarıdaki
            # üç tip DIŞINDA (ör. Ollama'nın kendi istemci kütüphanesinden gelen
            # beklenmedik bir istisna) HERHANGİ bir hata buraya düşerse, Streamlit
            # kendi kırmızı hata panelini gösterse BİLE terminalde KESİN bir iz
            # bırakılsın diye AYRICA loglanır/basılır — "sessizce donmuş" izlenimi
            # bırakan asıl neden genelde budur (kullanıcı ekranda ne olduğunu HİÇ
            # göremez, terminale BAKMAK ZORUNDA kalır).
            logger.error("CRITICAL UI ERROR (rapor işleme, BEKLENMEYEN tip): %s", exc, exc_info=True)
            print(f"CRITICAL UI ERROR (rapor işleme, BEKLENMEYEN tip): {type(exc).__name__}: {exc}")
            st.error(f"🛑 Beklenmeyen bir hata oluştu ({type(exc).__name__}): {exc}\n\nDetaylar için terminale bakın.")
            return

        dogrulama_hatalari = sonuc.get("validation_hatalari") or []
        if dogrulama_hatalari:
            st.warning(
                "⚠️ Bazı kayıtlar doğrulanamadı ve ATLANDI:\n\n"
                + "\n\n".join(f"- {hata}" for hata in dogrulama_hatalari)
            )

        # COĞRAFİ BAĞLAMA (bkz. `_grafa_yaz` docstring'i): bu da bir hata
        # DEĞİLDİR — sistem, LLM'in ürettiği implausible bir koordinatı
        # REDDETMEK yerine GERÇEK bir yer adına bağlayarak KURTARMIŞTIR;
        # kullanıcı bunun FARKINDA olsun diye açıkça bildirilir.
        if yer_adiyla_baglanan:
            st.info(
                f"🧭 {yer_adiyla_baglanan} kayıt için üretilen koordinat gerçek bölge "
                "dışındaydı; isimde geçen yer adına göre GERÇEK bir yol/köprü verisinin "
                "merkez koordinatına bağlandı (bkz. Coğrafi Bağlama kuralı)."
            )
        # HARİCİ GEOKODLAMA (bkz. `_grafa_yaz` docstring'i, "ULUSAL ÖLÇEKLİ
        # ESNEKLİK"): bu da bir hata DEĞİLDİR — sistem, YEREL veride hiç
        # karşılığı olmayan bir yer adını (ör. Elazığ/Malatya dışı) GERÇEK
        # bir harici geocoding servisiyle KURTARMIŞTIR; kullanıcı bu
        # bölgede yerel yol-ağı/birlik VERİSİ olmayabileceğinin FARKINDA
        # olsun diye açıkça bildirilir.
        if harici_geokodlama_ile_baglanan:
            st.info(
                f"🌍 {harici_geokodlama_ile_baglanan} kayıt için ne LLM koordinatı ne yerel "
                "veri eşleşmesi vardı; harici geocoding (Nominatim) ile gerçek konuma "
                "bağlandı. Bu bölge için yerel yol ağı/birlik verisi henüz YÜKLENMEMİŞ "
                "olabilir — Karar Destek Motoru bu durumda dürüstçe 'birlik bulunamadı' "
                "diyecektir (uydurma bir öneri ÜRETMEYECEKTİR)."
            )
        # NO GHOST INFRASTRUCTURE (bkz. `_grafa_yaz` docstring'i): bu ASLA bir
        # hata DEĞİLDİR — sistem BİLİNÇLİ olarak sahte koordinatlı bir düğüm
        # yaratmayı REDDETMİŞTİR; kullanıcıya SESSİZCE geçilmez, açıkça bildirilir.
        if engellenen_ghost:
            st.warning(
                f"🛡️ {engellenen_ghost} altyapı/tesis/birim kaydı için GERÇEK bir OSM "
                "eşleşmesi bulunamadı; sistem sahte koordinatlı bir 'hayali' (ghost) "
                "düğüm YARATMADI (bkz. Kesin Varlık Eşleştirme kuralı). İlgili olay(lar) "
                "yine de bağımsız olarak kaydedildi, ancak bu varlığa bir ilişki "
                "kurulamamış olabilir."
            )
        # SAHTE KOORDİNAT KAPANI (bkz. `_grafa_yaz` docstring'i, "MUTLAK
        # KİLİT"): bu da ASLA bir hata DEĞİLDİR — sistem, gerçek yüklü OSM
        # bölgesinin AÇIKÇA dışına düşen (ör. "Van'a sıçrayan") bir düğümü/
        # olayı BİLİNÇLİ olarak reddetmiştir.
        if engellenen_sahte_koordinat:
            st.warning(
                f"🛡️ {engellenen_sahte_koordinat} kayıt, gerçek yüklü OSM bölgesinin "
                "(+ civarı toleransı) AÇIKÇA DIŞINA düşen bir koordinat ürettiği için "
                "reddedildi ve YAZILMADI (bkz. Sahte Koordinat Kapanı kuralı)."
            )

        if yazilan_dugum == 0 and yazilan_iliski == 0 and altyapi_guncellemeleri == 0:
            if engellenen_ghost or engellenen_sahte_koordinat:
                st.error(
                    "🛑 Hiçbir şey Bilgi Grafına yazılamadı: bu raporda geçen kayıt(lar), "
                    "gerçek bir OSM eşleşmesi bulunamadığı veya gerçek bölgenin dışında bir "
                    "koordinat ürettiği için (yukarıya bakın) reddedildi ve raporda başka "
                    "geçerli bir varlık/ilişki yoktu."
                )
            else:
                st.error(
                    "🛑 İstihbarat işlenirken hata oluştu: modelden Bilgi Grafına yazılabilecek "
                    "HİÇBİR geçerli varlık/ilişki çıkarılamadı (yukarıdaki doğrulama hatalarına "
                    "bakın; boşsa modelin ham yanıtı hiç ayrıştırılamamış olabilir)."
                )
            return

        # NOT (bkz. modül başındaki "OFFLINE-FIRST / SADECE NEO4J MİMARİ
        # KARARI"): burada eskiden `_kriz_bolgesi_yukle_tetikle` çağrılıp
        # kriz(ler)in çevresindeki kılcal yol ağı bir `.osm.pbf` dosyasından
        # ANLIK taranıp Neo4j'e yazılıyordu. Bu çağrı KALDIRILDI — Karar
        # Motoru artık SADECE Neo4j'de halihazırda var olan yol ağını okur;
        # `yazilan_event_koordinatlari` (bkz. `_grafa_yaz`) bu yüzden burada
        # ARTIK KULLANILMIYOR (geriye dönük uyumluluk için dönüş imzası
        # DEĞİŞTİRİLMEDİ).

    ozet_parcalari = []
    if yazilan_dugum:
        ozet_parcalari.append(f"{yazilan_dugum} yeni varlık")
    if altyapi_guncellemeleri:
        ozet_parcalari.append(f"{altyapi_guncellemeleri} gerçek altyapı düğümü güncellendi (kapatıldı/durumu değişti)")
    if yazilan_iliski:
        ozet_parcalari.append(f"{yazilan_iliski} ilişki")
    st.sidebar.success(f"✅ {kaynak_etiketi}: " + ", ".join(ozet_parcalari) + " Bilgi Grafı'na işlendi.")
    _clear_data_caches()
    st.rerun()


def _render_scenario_library_section(db: Neo4jConnection, parser: OllamaParser) -> None:
    """Sidebar bölüm 1: demo/sunum için hazır kriz senaryoları kütüphanesi."""
    st.sidebar.header("📌 Hazır Kriz Senaryosu Kütüphanesi")
    st.sidebar.caption(
        "Demo veya sunum için önceden hazırlanmış bir kriz senaryosu seçip "
        "tek tıkla Bilgi Grafı'na yükleyin — aynı Ollama/Neo4j hattı üzerinden işlenir."
    )
    secilen_senaryo = st.sidebar.selectbox(
        "Hazır Kriz Senaryosu Seç",
        options=list(HAZIR_SENARYOLAR.keys()),
        label_visibility="collapsed",
        key="secilen_senaryo",
    )
    with st.sidebar.expander("📄 Senaryo metnini önizle"):
        st.write(HAZIR_SENARYOLAR[secilen_senaryo])

    yukle_tetiklendi = st.sidebar.button(
        "Seçilen Senaryoyu Grafiğe Yükle",
        type="primary",
        width="stretch",
        key="senaryo_yukle_btn",
    )
    if not yukle_tetiklendi:
        return
    _rapor_metnini_isle_ve_yenile(db, parser, HAZIR_SENARYOLAR[secilen_senaryo], kaynak_etiketi="Senaryo")


def _render_report_input_section(db: Neo4jConnection, parser: OllamaParser) -> None:
    """Sidebar bölüm 2: serbest metin canlı istihbarat/rapor girişi."""
    st.sidebar.header("📡 Canlı İstihbarat / Rapor Girişi")
    st.sidebar.caption(
        "Telsiz raporu, haber metni veya durum bildirimini aşağıya yapıştırın; "
        "yerel yapay zeka (Ollama) bunu analiz edip Bilgi Grafı'na işleyecek."
    )
    rapor_metni = st.sidebar.text_area(
        "Rapor Metni",
        height=200,
        placeholder=(
            "Örnek: Elazığ merkezde şiddetli bir deprem meydana geldi. "
            "Devlet hastanesi ağır hasarlı durumda ve kullanılamıyor..."
        ),
        label_visibility="collapsed",
        key="serbest_rapor_metni",
    )
    analiz_tetiklendi = st.sidebar.button(
        "Raporu Analiz Et ve Grafiğe İşle", type="primary", width="stretch", key="rapor_analiz_btn"
    )

    if not analiz_tetiklendi:
        return
    if not rapor_metni or not rapor_metni.strip():
        st.sidebar.warning("Lütfen önce bir rapor metni girin.")
        return
    _rapor_metnini_isle_ve_yenile(db, parser, rapor_metni, kaynak_etiketi="Rapor")


def _render_database_reset_section(db: Neo4jConnection) -> None:
    """Sidebar bölüm 3: TÜM veritabanındaki kriz senaryosunu sıfırlayan
    GÜVENLİ aksiyon.

    ESKİDEN bu bölüm `clear_database()` çalıştıran bir "nükleer" buton
    içeriyordu — Bilgi Grafındaki HER ŞEYİ (47 binin üzerindeki GERÇEK OSM
    şehir topolojisi — Street/Bridge/Facility/Unit — DAHİL) GERİ ALINAMAZ
    şekilde siliyordu. Yeni bir senaryo denemek isteyen bir kullanıcı,
    farkında olmadan saatler süren bir Overpass yüklemesini de kaybediyordu.
    Artık `db.reset_crisis_scenario()` (bkz. `src.core.database`) çalışır:
    `Event` düğümlerini siler, TÜM `Infrastructure` düğümlerini tekrar
    `acik_mi=True` yapar VE TÜM `Facility`/`Unit` düğümlerinin hasar
    durumunu (`mevcut_durum`/`durum`) tekrar `'Aktif'`e döndürür — GERÇEK
    şehir topolojisinin KENDİSİ (düğümlerin varlığı) ASLA silinmez, SADECE
    bir önceki senaryonun bıraktığı durum/hasar izleri temizlenir.

    Kanıtlanmış bir boşluk için savunma ("UI Ghosting" YANLIŞ TEŞHİSİ —
    bkz. `database.reset_crisis_scenario`daki AYNI başlıklı not):
    sıfırlama SONRASI ekranda kalan bir "Kritik/Hasarlı: 1" metriği ilk
    bakışta bir Streamlit önbellek/`session_state` sorunu gibi görünebilir
    — GERÇEKTE bu bir önbellek
    hayaleti DEĞİLDİ (bu dosyada `ai_recommendations`/`event_status` gibi
    bir `session_state` DEĞİŞKENİ HİÇ VAR OLMADI, `_clear_data_caches`/
    `st.rerun()` zaten AŞAĞIDA doğru şekilde çağrılıyordu); Facility'nin
    Neo4j'deki `mevcut_durum`u GERÇEKTEN hâlâ "Hasarlı"ydı çünkü eski
    `reset_crisis_scenario` BİLİNÇLİ olarak Facility/Unit hasar alanlarına
    dokunmuyordu. Kök neden veritabanı sıfırlama KAPSAMIYDI, arayüz
    önbelleği DEĞİL.

    "OFFLINE-FIRST / SADECE NEO4J" MİMARİ KARARI (bkz. modül başındaki AYNI
    başlıklı not) SONRASI: eskiden bu fonksiyon, `reset_crisis_scenario()`
    sonrası `LocalOsmReader.gecici_kilcal_veriyi_temizle` ile canlı akışta
    ANLIK eklenmiş "geçici kılcal yol" düğümlerini de siliyordu. Canlı akış
    artık HİÇBİR ŞEKİLDE yeni harita düğümü YAZMADIĞI için (bkz. `seed_db.py`
    — tüm harita verisi ARTIK KALICI, `gecici_mi=False`, TEK SEFERLİK kurulum
    aşamasında yüklenir) bu RAM tahliyesi adımı ANLAMSIZ hale geldi ve
    KALDIRILDI — bir senaryo sıfırlaması, `seed_db.py` ile yüklenen gerçek
    şehir topolojisine/yol ağına ASLA dokunmaz.

    Yine de kazara tıklamaya karşı ucuz bir güvenlik katmanı olarak, buton
    SADECE yanındaki onay kutusu işaretliyken aktif olur (`disabled=not onay`).
    """
    st.sidebar.header("🚨 Senaryo Sıfırlama")
    st.sidebar.caption(
        "Mevcut kriz senaryosunu (tüm olaylar + kapalı yollar/sokaklar, TÜM "
        "şehirlerde) sıfırlar; yeni bir senaryoya temiz başlamak için kullanılır. "
        "Gerçek şehir topolojisi BUNDAN ETKİLENMEZ, ASLA silinmez."
    )
    onay = st.sidebar.checkbox(
        "Kriz senaryosunu sıfırlamayı onaylıyorum (tüm olaylar silinecek, "
        "kapalı yollar yeniden açılacak)",
        key="senaryo_sifirla_onay",
    )
    sifirla_tetiklendi = st.sidebar.button(
        "🚨 Sadece Kriz Senaryosunu Sıfırla",
        type="primary",
        width="stretch",
        disabled=not onay,
        key="senaryo_sifirla_btn",
    )
    if not sifirla_tetiklendi:
        return

    try:
        with st.spinner("Kriz senaryosu sıfırlanıyor (şehir topolojisi korunuyor)..."):
            sonuc = db.reset_crisis_scenario()
    except Neo4jConnectionError as exc:
        st.sidebar.error(f"Kriz senaryosu sıfırlanamadı: {exc}")
        return

    # `clear_database()`'in aksine Neo4j baglantisi/Ollama istemcileri
    # ETKILENMEDIGINDEN `st.cache_resource` temizlemeye GEREK YOK; sadece
    # kisa-sureli VERI onbellekleri (harita/metrik/rapor) temizlenir —
    # bir rapor islendiginde kullanilan AYNI yardimci (`_clear_data_caches`).
    _clear_data_caches()
    st.sidebar.success(
        f"✅ Kriz senaryosu sıfırlandı: {sonuc['silinen_olay']} olay silindi, "
        f"{sonuc['acilan_altyapi']} altyapı düğümü tekrar açıldı, "
        f"{sonuc['iyilesen_tesis']} tesis + {sonuc['iyilesen_birim']} birim tekrar Aktif "
        "yapıldı. Şehir topolojisi korundu."
    )
    st.rerun()


# "FAZ 2: TAKTİKSEL KATMANLAR VE FİLTRELER": haritadaki
# hangi tesis/birim KATEGORİLERİNİN çizileceğini kontrol eden checkbox'ların
# `st.session_state` anahtarları — `_render_tactical_filter_section` bunları
# widget `key`si olarak kullanır, `_tesis_birlik_kriz_filtrele` (bkz. o
# fonksiyonun docstring'i) `render_map_section` içinde bunları OKUR. Tek
# bir yerde toplanır ki iki fonksiyon YANLIŞLIKLA farklı anahtar isimleri
# kullanıp birbirinden KOPMASIN.
_FILTRE_HASTANE_KEY = "filtre_hastane"
_FILTRE_ITFAIYE_KEY = "filtre_itfaiye"
_FILTRE_POLIS_KEY = "filtre_polis"
_FILTRE_ASKERI_US_KEY = "filtre_askeri_us"
_FILTRE_HAVALIMANI_KEY = "filtre_havalimani"
_FILTRE_KAPALI_YOL_KEY = "filtre_kapali_yol"


def _render_tactical_filter_section() -> None:
    """Sidebar bölüm: "Taktiksel Harita Filtreleri" — kullanıcının
    haritadaki belirli tesis/birim kategorilerinin görünürlüğünü açıp/
    kapatabildiği bir katman-anahtarları paneli. Varsayılan olarak HEPSİ
    seçilidir (`value=True`) — filtre paneli açılmadan önceki davranışla
    (her şey görünür) BİREBİR aynı başlangıç durumu.

    MİMARİ KARAR (performans): filtreler VERİ ÇEKME SEVİYESİNDE
    (`fetch_facility_points`/`fetch_unit_points`) DEĞİL, harita katmanı
    İNŞA EDİLMEDEN HEMEN ÖNCE (bkz. `_tesis_birlik_kriz_filtrele`,
    `render_map_section` içinde çağrılır) bellek-içi (pandas) olarak
    uygulanır — aksi halde her checkbox tıklaması Neo4j'e YENİDEN sorgu
    attırıp o fonksiyonların `@st.cache_data` önbelleğinin TÜM amacını
    boşa çıkarırdı; checkbox'lar SADECE zaten önbellekte duran verinin
    hangi satırlarının ÇİZİLECEĞİNİ seçer, Neo4j'e HİÇBİR YENİ sorgu
    tetiklemez.

    Widget'lar sabit bir `key` ile oluşturulur (bkz. modül-üstü `_FILTRE_
    *_KEY` sabitleri) — bu, Streamlit'in değeri otomatik olarak `st.
    session_state`e yazması demektir; `render_map_section` (bkz. `main()`
    içinde `render_sidebar`DAN SONRA çağrılır) bu değerleri doğrudan
    `st.session_state`ten güvenle okuyabilir.
    """
    with st.sidebar.expander("🎯 Taktiksel Harita Filtreleri", expanded=False):
        st.caption("Haritada hangi kategorilerin gösterileceğini seçin.")
        st.checkbox("🏥 Hastaneleri Göster", value=True, key=_FILTRE_HASTANE_KEY)
        st.checkbox("🚒 İtfaiyeleri Göster", value=True, key=_FILTRE_ITFAIYE_KEY)
        st.checkbox("👮 Polis/Jandarma Göster", value=True, key=_FILTRE_POLIS_KEY)
        st.checkbox("🪖 Askeri Üsleri Göster", value=True, key=_FILTRE_ASKERI_US_KEY)
        st.checkbox("✈️ Havalimanlarını Göster", value=True, key=_FILTRE_HAVALIMANI_KEY)
        st.checkbox("🚧 Kapalı Yolları Göster", value=True, key=_FILTRE_KAPALI_YOL_KEY)


def _tesis_birlik_kriz_filtrele(
    tesis_df: pd.DataFrame, birlik_df: pd.DataFrame, kriz_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """"Taktiksel Harita Filtreleri" (bkz. `_render_tactical_filter_
    section`) checkbox durumlarına göre Facility/Unit/kapalı-yol
    DataFrame'lerini bellek-içi (pandas) olarak daraltır — bkz. o
    fonksiyonun docstring'indeki "MİMARİ KARAR (performans)" notu (Neo4j'e
    YENİDEN sorgu ATILMAZ, sadece ZATEN çekilmiş satırlar filtrelenir).

    `st.session_state.get(key, True)` KULLANILIR (`.get` ile varsayılan
    `True`) — widget teorik olarak HENÜZ render EDİLMEMİŞ olsa bile
    (`render_sidebar`, `render_map_section`DAN ÖNCE çağrıldığı için bu
    pratikte HİÇ olmaz, bkz. `main()`) güvenli tarafta (hepsini göster)
    kalınır.

    Facility'nin Liman/Sığınak, Unit'in AFAD/Sağlık/Askeri Birlik/Ağır
    Mühendislik/Arama Kurtarma/Lojistik alt-tipleri BU PANELDE HİÇ
    checkbox'ı OLMADIĞINDAN her zaman görünür kalır — SADECE kullanıcının
    AÇIKÇA istediği 6 kategori (Hastane/İtfaiye/Polis/Askeri Üs/
    Havalimanı/Kapalı Yol) bu filtreye TABİDİR.
    """
    if not tesis_df.empty:
        goster = pd.Series(True, index=tesis_df.index)
        if not st.session_state.get(_FILTRE_HASTANE_KEY, True):
            goster &= tesis_df["facility_type"] != FacilityType.HASTANE.value
        if not st.session_state.get(_FILTRE_ASKERI_US_KEY, True):
            goster &= tesis_df["facility_type"] != FacilityType.ASKERI_US.value
        if not st.session_state.get(_FILTRE_HAVALIMANI_KEY, True):
            goster &= tesis_df["facility_type"] != FacilityType.HAVALIMANI.value
        tesis_df = tesis_df[goster]

    if not birlik_df.empty:
        goster = pd.Series(True, index=birlik_df.index)
        if not st.session_state.get(_FILTRE_ITFAIYE_KEY, True):
            goster &= birlik_df["unit_type"] != UnitType.ITFAIYE.value
        if not st.session_state.get(_FILTRE_POLIS_KEY, True):
            goster &= birlik_df["unit_type"] != UnitType.POLIS.value
        birlik_df = birlik_df[goster]

    if not st.session_state.get(_FILTRE_KAPALI_YOL_KEY, True):
        kriz_df = kriz_df.iloc[0:0]

    return tesis_df, birlik_df, kriz_df


def render_sidebar(db: Neo4jConnection, parser: OllamaParser) -> None:
    """Sol panel: hazır kriz senaryosu kütüphanesi, serbest metin rapor
    girişi, taktiksel harita filtreleri ve (en altta) kriz senaryosu
    sıfırlama aracından oluşur."""
    _render_scenario_library_section(db, parser)
    st.sidebar.markdown("---")
    _render_report_input_section(db, parser)
    st.sidebar.markdown("---")
    _render_tactical_filter_section()
    st.sidebar.markdown("---")
    _render_database_reset_section(db)


def render_map_section(db: Neo4jConnection, engine: DecisionEngine) -> None:
    """Ana ekran: 3 boyutlu, GPU hızlandırmalı taktiksel durum haritası
    (PyDeck/deck.gl). Katman stratejisi için bkz. modülün "3B Taktiksel
    Harita" bölüm başlığındaki yorum bloğu.

    Harita, veritabanındaki TÜM şehirleri/bölgeleri AYNI ANDA, tek bir bütün
    ağ olarak çizer — bölgesel bir filtre/kısıtlama YOKTUR.

    GÖRSEL C4ISR — TAKTİKSEL SEVK KATMANI: `engine` parametresi BİLEREK
    eklenmiştir (eskiden bu fonksiyon SADECE `db` alıyordu) — harita artık
    `fetch_ai_recommendations(db, engine)`i (bkz. `render_ai_staff_section`
    ile AYNI, `@st.cache_data(ttl=30)` önbellekli çağrı — burada AYRICA
    çağırmak YENİ bir Ollama isteği DEMEK DEĞİLDİR, sayfanın geri kalanıyla
    AYNI önbellek girdisini paylaşır) çağırıp SAKOM'un seçtiği birliklerin
    güzergahlarını çiziyor. BİLİNÇLİ ÖDÜNLEŞİM: bu, haritanın İLK render'ının
    artık AI Kurmay Başkanlığı'nın LLM çağrısını da BEKLEMESİ anlamına gelir
    (eskiden harita ANINDA, AI bölümü ayrıca aşağıda yükleniyordu) — ama
    kullanıcının AÇIKÇA istediği "LLM metniyle EŞZAMANLI" davranış budur.
    """
    st.markdown("### 🗺️ 3B Taktiksel Durum Haritası")

    # FAZ 5 "HİYERARŞİK YOL ÇİZİMİ (LOD FİLTRESİ)": varsayılan olarak
    # (performans/görsel netlik için) sadece ana damarlar (motorway/trunk/
    # primary/secondary) çizilir; komutan isterse tüm ara sokak dokusunu
    # (tertiary/residential/vb.) da açabilir. `key` ile sabit bir widget
    # kimliği verilir ki Streamlit rerun'larında checkbox durumu KORUNSUN.
    sadece_ana_damarlar = st.checkbox(
        "🛣️ Sadece ana yol ağını göster (performans modu)",
        value=True,
        key="sadece_ana_damarlar",
        help=(
            "Açıkken sadece motorway/trunk/primary/secondary sınıfı OSM yolları çizilir "
            "(hızlı, temiz genel bakış). Kapatırsanız TÜM ara sokak/mahalle yolu dokusu da "
            "çizilir (daha detaylı ama daha yavaş/kalabalık)."
        ),
    )

    sokak_df = fetch_street_points(db, sadece_ana_damarlar)
    kopru_df = fetch_bridge_points(db)
    yol_df = fetch_road_lines(db)
    tesis_df = fetch_facility_points(db)
    birlik_df = fetch_unit_points(db)
    admin_df = fetch_admin_area_points(db)
    olay_df = fetch_event_points(db)
    kriz_df = fetch_dynamic_crisis_points(db)

    # "FAZ 2: TAKTİKSEL KATMANLAR VE FİLTRELER" (bkz. `_render_tactical_
    # filter_section`): sidebar'daki checkbox durumlarına göre tesis/
    # birim/kapalı-yol DataFrame'lerini haritaya ÇİZİLMEDEN HEMEN ÖNCE
    # daraltır — `toplam_varlik`/`ozet` (aşağıda) da FİLTRELENMİŞ sayıları
    # yansıtsın diye bu daraltma, sayım/özet hesaplanmadan ÖNCE yapılır.
    tesis_df, birlik_df, kriz_df = _tesis_birlik_kriz_filtrele(tesis_df, birlik_df, kriz_df)

    toplam_varlik = sum(
        len(df) for df in (sokak_df, kopru_df, yol_df, tesis_df, birlik_df, admin_df, olay_df, kriz_df)
    )
    if toplam_varlik == 0:
        st.info("Haritada gösterilecek herhangi bir varlık yok. Sol panelden bir rapor işleyerek başlayın.")
        return

    # GÖRSEL C4ISR — TAKTİKSEL SEVK KATMANI: `render_ai_staff_section` ile
    # AYNI önbellekli çağrı (bkz. yukarıdaki fonksiyon docstring'i). SAKOM
    # motoru çalışamıyorsa (Ollama kapalı vb.) harita YİNE DE gösterilir —
    # sadece sevk hatları eksik kalır; bu ASLA haritanın kendisini
    # ÇÖKERTMEZ (bkz. `st.spinner` içindeki try/except).
    sevk_df = pd.DataFrame()
    try:
        with st.spinner("SAKOM Karar Destek Motoru taktiksel sevk güzergahlarını hesaplıyor..."):
            ai_sonuc = fetch_ai_recommendations(db, engine)
        sevk_df = _sevk_rotalari_df_olustur(ai_sonuc)
    except RuntimeError as exc:
        logger.error("CRITICAL UI ERROR (taktiksel sevk katmanı): %s", exc, exc_info=True)
        print(f"CRITICAL UI ERROR (taktiksel sevk katmanı): {exc}")
        st.caption(f"⚠️ Taktiksel sevk katmanı hesaplanamadı (SAKOM motoru şu an erişilemiyor): {exc}")
    except Exception as exc:  # noqa: BLE001 - KASITLI: beklenmeyen HİÇBİR hata sessizce yutulmasın/haritayı ÇÖKERTMESİN.
        logger.error("CRITICAL UI ERROR (taktiksel sevk katmanı, BEKLENMEYEN tip): %s", exc, exc_info=True)
        print(f"CRITICAL UI ERROR (taktiksel sevk katmanı, BEKLENMEYEN tip): {type(exc).__name__}: {exc}")
        st.caption(f"⚠️ Taktiksel sevk katmanı hesaplanamadı (beklenmeyen hata: {type(exc).__name__}) — harita yine de gösteriliyor.")

    ozet = (
        f"🛰️ {len(sokak_df):,} sokak · {len(kopru_df):,} köprü · {len(yol_df):,} isimli güzergah · "
        f"{len(tesis_df):,} tesis · {len(birlik_df):,} birlik · {len(admin_df):,} idari alan · "
        f"{len(olay_df):,} aktif olay · 🔴 {len(kriz_df):,} aktif kesinti (kapalı yol/köprü) · "
        f"🟡 {len(sevk_df):,} taktiksel sevk güzergahı"
    ).replace(",", ".")
    st.caption(ozet)

    deck = build_deck(sokak_df, kopru_df, yol_df, tesis_df, birlik_df, admin_df, olay_df, kriz_df, sevk_df)
    st.pydeck_chart(deck, width="stretch", height=620)


def render_report_table(db: Neo4jConnection) -> None:
    """Ana ekran: TÜM şehirlerdeki son olayları ve etkilediği varlıkları
    listeleyen rapor tablosu."""
    st.markdown("### 📋 Son Durum Raporu")
    rapor_df = fetch_recent_report(db)
    if rapor_df.empty:
        st.info("Henüz kayıtlı bir olay yok.")
        return
    # Şiddet sütununu, olayın ciddiyetiyle orantılı koyu bir arka planla
    # renklendir; sıradan düz bir tablo yerine komuta merkezi raporu hissi.
    stilli_df = rapor_df.style.map(_siddet_hucre_stili, subset=["Şiddet"])
    st.dataframe(stilli_df, width="stretch", hide_index=True)


def render_ai_staff_section(db: Neo4jConnection, engine: DecisionEngine) -> None:
    """Ana ekranın altındaki "AI Kurmay Başkanlığı" bölümü: Bilgi Grafındaki
    anlık durumu (hasarlı tesisler, kapalı güzergahlar, sahadaki birlikler)
    tarayıp Ollama üzerinden 3 maddelik taktiksel karar/yönlendirme önerisi
    üreten Karar Destek Motoru'nun (bkz. `src.core.decision_engine`) çıktısını
    gösterir. Bu bölüm her rapor işlendiğinde (bkz. `_clear_data_caches`) ve
    önbellek süresi (30 sn) dolduğunda otomatik olarak yeniden çalışır;
    sistemi salt bir harita panosundan gerçek bir taktiksel beyne dönüştüren
    bileşen budur.
    """
    st.markdown("### 🤖 AI Kurmay Başkanlığı — Taktiksel Karar ve Öneri Motoru")
    st.caption(
        "Bilgi Grafındaki anlık saha durumu (hasarlı tesisler, kapalı güzergahlar, "
        "sahadaki birlikler) GraphRAG yöntemiyle otomatik taranır; yerel yapay "
        "zeka (Ollama) bu somut veriye dayanarak kritik taktiksel öneriler üretir."
    )

    try:
        with st.spinner("AI Kurmay Başkanlığı anlık saha durumunu değerlendiriyor..."):
            sonuc = fetch_ai_recommendations(db, engine)
    except RuntimeError as exc:
        logger.error("CRITICAL UI ERROR (AI Kurmay Başkanlığı): %s", exc, exc_info=True)
        print(f"CRITICAL UI ERROR (AI Kurmay Başkanlığı): {exc}")
        st.error(f"🛑 Karar Destek Motoru şu an çalışamıyor: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - KASITLI: beklenmeyen HİÇBİR hata sessizce yutulmasın.
        logger.error("CRITICAL UI ERROR (AI Kurmay Başkanlığı, BEKLENMEYEN tip): %s", exc, exc_info=True)
        print(f"CRITICAL UI ERROR (AI Kurmay Başkanlığı, BEKLENMEYEN tip): {type(exc).__name__}: {exc}")
        st.error(f"🛑 Beklenmeyen bir hata oluştu ({type(exc).__name__}): {exc}\n\nDetaylar için terminale bakın.")
        return

    if sonuc.get("durum_bos"):
        st.info(
            "Bilgi Grafında henüz değerlendirilecek bir kriz verisi yok — "
            "sol panelden bir rapor işleyerek AI Kurmay Başkanlığı'nı devreye sokun."
        )
        return

    # "COĞRAFİ ÖN-KONTROL" (bkz. `DecisionEngine.generate_recommendations`):
    # olay TÜRÜ ile bulunduğu
    # ilin fiziksel yapısı AÇIKÇA çelişiyorsa (ör. "İç Anadolu'da
    # Tsunami"), rapor LLM'e HİÇ gitmeden reddedilmiştir — komutana bunu
    # normal bir "3 maddelik öneri" gibi DEĞİL, AÇIKÇA bir hata/uyarı
    # olarak göster.
    if sonuc.get("cografi_red"):
        st.error(f"🚫 {sonuc.get('cografi_red_nedeni') or 'Coğrafi tutarsızlık tespit edildi.'}")
        return

    # "FAZ 3: DİNAMİK SAHA VE ÇEVRESEL İSTİHBARAT":
    # `DecisionEngine.generate_recommendations`in mikro-görev promptlarına
    # ENJEKTE ETTİĞİ AYNI hava durumu istihbaratı (bkz. `src.core.
    # environmental_context`), komutanın "AI neden bu kararı verdi"
    # sorusunu tooltip'e gitmeden ANINDA cevaplayabilmesi için burada da
    # küçük bir metin/ikon olarak gösterilir. Kaynak (canlı API/çevrimdışı
    # tahmin) `caption` ile HER ZAMAN AÇIKÇA belirtilir (bkz. `Cevresel
    # Durum.kaynak` — asla sahte veri gerçekmiş gibi SUNULMAZ).
    cevresel_durum = sonuc.get("cevresel_durum")
    if cevresel_durum is not None:
        kaynak_etiketi = "canlı API" if cevresel_durum.kaynak == "canli_api" else "çevrimdışı tahmin"
        st.info(
            f"{cevresel_durum.ikon} **{cevresel_durum.yagis_aciklamasi}, {cevresel_durum.sicaklik_c}°C** "
            f"· Görüş: ~{cevresel_durum.gorus_mesafesi_km} km"
        )
        st.caption(f"Kaynak: {kaynak_etiketi} — kriz bölgesinin hava durumu, AI Kurmay Başkanlığı'nın "
                   "önerilerine dahil edildi.")

    maddeler = parse_oneri_maddeleri(sonuc["oneriler_metni"])
    if not maddeler:
        st.warning("AI Kurmay Başkanlığı bir yanıt üretti ancak ayrıştırılamadı; ham yanıt aşağıdadır.")
        st.code(sonuc["oneriler_metni"])
        return

    # İlk (en kritik) öneri st.warning (kırmızımsı, en dikkat çekici) ile,
    # diğerleri st.info ile gösterilir; böylece komutanın gözü önce en
    # öncelikli aksiyona gider.
    ikonlar = ["🚨", "🧭", "📦"]
    for i, madde in enumerate(maddeler):
        kutu = st.warning if i == 0 else st.info
        kutu(f"**{ikonlar[i % len(ikonlar)]} Taktiksel Karar {i + 1}**\n\n{madde}")

    with st.expander("📊 Değerlendirmeye Esas Anlık Saha Durumu (GraphRAG Bağlamı)"):
        st.code(sonuc["durum_ozeti"], language="markdown")


# ---------------------------------------------------------------------------
# Giriş noktası
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Kriz ve Afet Yönetimi Karar Destek Sistemi",
        page_icon="🛰️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    inject_tactical_theme()

    st.title("🛰️ Stratejik Komuta Kontrol ve Karar Destek Paneli")
    st.caption("Kriz ve Afet Yönetimi Bilgi Grafı — Canlı Operasyonel Durum (3B Taktiksel Harita)")

    try:
        db = get_connection()
    except Neo4jConnectionError as exc:
        st.error(f"Neo4j veritabanına bağlanılamadı: {exc}")
        st.caption("`.env` dosyasındaki NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD ayarlarını kontrol edin.")
        st.stop()

    parser = get_parser()
    engine = get_decision_engine()

    # GERİ ALMA NOTU: "Aktif Operasyon Bölgesi" seçicisi ve tüm bölgesel
    # filtreleme KALDIRILDI — bir Karar Destek Sistemi'nin (C4ISR) doğasına
    # aykırı, şehirleri birbirinden yapay olarak koparan bir kısıtlama olarak
    # değerlendirildi. Aşağıdaki HER bileşen artık veritabanındaki TÜM
    # şehirleri/bölgeleri TEK BİR bütün ağ olarak gösterir/değerlendirir.
    render_sidebar(db, parser)

    render_top_metrics(fetch_metrics(db))
    st.markdown("---")
    render_map_section(db, engine)
    st.markdown("---")
    render_report_table(db)
    st.markdown("---")
    render_ai_staff_section(db, engine)


if __name__ == "__main__":
    main()
