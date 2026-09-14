

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


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.core.database import Neo4jConnection, Neo4jConnectionError  
from src.core.decision_engine import DecisionEngine, _temiz_isim, parse_oneri_maddeleri  
from src.core.models import (  
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
from src.data_ingestion.nlp_parser import ENTITY_MODEL_MAP, OllamaParser  
from src.data_ingestion.osm_loader import ELAZIG_BBOX  
from src.data_ingestion.real_osm_loader import (  
    ANA_DAMAR_HIGHWAY_TIPLERI,
    OSM_TEKNIK_KIMLIK_IMZASI,
)


logger = logging.getLogger(__name__)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


KRITIK_TESIS_DURUMLARI = ["Hasarlı", "Yok Edildi"]


_KOLON_YUKSEKLIK_OLCEGI = 0.15


TAKTIKSEL_TIP_RENGI: Dict[str, List[int]] = {
    FacilityType.HASTANE.value: [255, 255, 255, 230],       
    UnitType.SAGLIK.value: [255, 255, 255, 230],             
    UnitType.ITFAIYE.value: [255, 50, 50, 230],             
    UnitType.ARAMA_KURTARMA.value: [255, 50, 50, 230],      
    UnitType.POLIS.value: [50, 100, 255, 230],               
   
    UnitType.ASKERI_BIRLIK.value: [50, 100, 255, 230],       
    FacilityType.ASKERI_US.value: [85, 107, 47, 235],        
    FacilityType.HAVALIMANI.value: [0, 255, 255, 230],       
}
TAKTIKSEL_VARSAYILAN_RENK: List[int] = [128, 128, 128, 210]  


_TAKTIKSEL_RADIUS_STANDART = 80
_TAKTIKSEL_RADIUS_STRATEJIK = round(_TAKTIKSEL_RADIUS_STANDART * 1.75)
_TAKTIKSEL_STRATEJIK_TIPLER = frozenset({FacilityType.ASKERI_US.value, FacilityType.HAVALIMANI.value})


_DURUM_STROKE_NOTR = [255, 255, 255, 90]
_DURUM_STROKE_HASARLI = [255, 61, 0, 255]

TAKTIKSEL_TIP_IKONU: Dict[str, str] = {
    FacilityType.HASTANE.value: "🏥",
    UnitType.SAGLIK.value: "🏥",
    UnitType.ITFAIYE.value: "🚒",
    UnitType.ARAMA_KURTARMA.value: "🚒",
    UnitType.POLIS.value: "🚓",
    UnitType.ASKERI_BIRLIK.value: "🚓",  
    FacilityType.ASKERI_US.value: "⚔️",
    FacilityType.HAVALIMANI.value: "✈️",
}
TAKTIKSEL_VARSAYILAN_IKON = "📍"


def _durum_stroke_rengi(durum: Optional[str]) -> List[int]:

    if durum in (FacilityStatus.HASARLI.value, FacilityStatus.YOK_EDILDI.value):
        return _DURUM_STROKE_HASARLI
    return _DURUM_STROKE_NOTR


OLAY_SIDDET_RENGI: Dict[str, List[int]] = {
    EventSeverity.DUSUK.value: [34, 211, 238, 255],
    EventSeverity.ORTA.value: [255, 176, 32, 255],
    EventSeverity.YUKSEK.value: [255, 59, 59, 255],
    EventSeverity.KRITIK.value: [255, 0, 200, 255],
    EventSeverity.KATASTROFIK.value: [255, 255, 255, 255],
}
_OLAY_VARSAYILAN_RENK = [255, 176, 32, 255]


YOL_ACIK_RENGI = [0, 230, 118, 205]
YOL_KAPALI_RENGI = [255, 23, 68, 235]


SOKAK_RENGI = [56, 189, 248, 200]  


KRIZ_KATMANI_RENGI = [255, 61, 0, 255]


SEVK_HATTI_RENGI = [255, 214, 0, 235]  



def _risk_rengi(risk_faktoru: Optional[float]) -> List[int]:
    r = max(0.0, min(1.0, risk_faktoru or 0.0))
    yesil, kirmizi = (0, 230, 118), (255, 23, 68)
    return [int(yesil[i] + (kirmizi[i] - yesil[i]) * r) for i in range(3)] + [200]



@st.cache_resource(show_spinner=False)
def get_connection() -> Neo4jConnection:
    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()
    return db


@st.cache_resource(show_spinner=False)
def get_parser() -> OllamaParser:

    return OllamaParser(model=os.getenv("OLLAMA_EXTRACTION_MODEL", "llama3"))


@st.cache_resource(show_spinner=False)
def get_decision_engine() -> DecisionEngine:
   
    return DecisionEngine(
        get_connection(),
        model=os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay-qwen"),
    )





@st.cache_data(ttl=15, show_spinner=False)
def fetch_metrics(_db: Neo4jConnection) -> Dict[str, int]:
  
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

    rows = _db.execute_query(
        "MATCH (n:Facility) RETURN COALESCE(n.aciklama, n.isim) AS isim, "
        "n.enlem AS enlem, n.boylam AS boylam, "
        "n.facility_type AS facility_type, n.mevcut_durum AS mevcut_durum, n.kapasite AS kapasite"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
 
    df["tur"] = df["facility_type"].apply(
        lambda t: f"{TAKTIKSEL_TIP_IKONU.get(t, TAKTIKSEL_VARSAYILAN_IKON)} {t}" if t else f"{TAKTIKSEL_VARSAYILAN_IKON} Tesis"
    )
    df["detay"] = df.apply(
        lambda r: f"Durum: {r.get('mevcut_durum', '—')} · Kapasite: {r.get('kapasite', '—')}", axis=1
    )
    
    df["renk"] = df["facility_type"].apply(lambda t: TAKTIKSEL_TIP_RENGI.get(t, TAKTIKSEL_VARSAYILAN_RENK))
    df["stroke_renk"] = df["mevcut_durum"].apply(_durum_stroke_rengi)
   
    df["radius"] = df["facility_type"].apply(
        lambda t: _TAKTIKSEL_RADIUS_STRATEJIK if t in _TAKTIKSEL_STRATEJIK_TIPLER else _TAKTIKSEL_RADIUS_STANDART
    )
    return df


@st.cache_data(ttl=15, show_spinner=False)
def fetch_unit_points(_db: Neo4jConnection) -> pd.DataFrame:
   
    rows = _db.execute_query(
        "MATCH (n:Unit) RETURN COALESCE(n.aciklama, n.isim) AS isim, "
        "n.enlem AS enlem, n.boylam AS boylam, "
        "n.unit_type AS unit_type, n.personel_sayisi AS personel_sayisi, "
        "n.hareket_kabiliyeti AS hareket_kabiliyeti, n.durum AS durum"
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    
    df["tur"] = df["unit_type"].apply(
        lambda t: f"{TAKTIKSEL_TIP_IKONU.get(t, TAKTIKSEL_VARSAYILAN_IKON)} {t}" if t else f"{TAKTIKSEL_VARSAYILAN_IKON} Birlik"
    )
    df["detay"] = df.apply(
        lambda r: f"{r.get('personel_sayisi', '—')} personel · {r.get('hareket_kabiliyeti', '—')}", axis=1
    )
   
    df["renk"] = df["unit_type"].apply(lambda t: TAKTIKSEL_TIP_RENGI.get(t, TAKTIKSEL_VARSAYILAN_RENK))
    df["stroke_renk"] = df["durum"].apply(_durum_stroke_rengi)
    df["radius"] = _TAKTIKSEL_RADIUS_STANDART
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_admin_area_points(_db: Neo4jConnection) -> pd.DataFrame:
  
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
  
    return _engine.generate_recommendations()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_recent_report(_db: Neo4jConnection) -> pd.DataFrame:
   
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




_TOOLTIP_STIL = {
    "backgroundColor": "#17171a",
    "color": "#f5f5f5",
    "border": "1px solid #ff3b3b",
    "fontFamily": "Consolas, 'Courier New', monospace",
    "fontSize": "12px",
    "padding": "8px 10px",
    "borderRadius": "4px",
}


_TOOLTIP = {"html": "<b>{isim}</b><br/>{tur}<br/>{detay}", "style": _TOOLTIP_STIL}


def _sokak_nokta_katmani_olustur(sokak_df: pd.DataFrame) -> Optional[pdk.Layer]:
   
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
            
            temiz = _temiz_isim(birlik)
            satirlar.append(
                {
                    "olay_ismi": olay_ismi,
                    "isim": temiz,
                    "tur": f"🚨 Sevk: {temiz} → {olay_ismi}",
                    "detay": f"{birlik.get('mesafe_km', '?')} km gerçek yol mesafesi · güzergah AÇIK",
                    "mesafe_km": birlik.get("mesafe_km"),
                    "path": [[boylam, enlem] for enlem, boylam in rota],
                }
            )
    return pd.DataFrame(satirlar)


def _taktiksel_sevk_katmani_olustur(sevk_df: pd.DataFrame) -> List[pdk.Layer]:
    
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
    return [glow_katmani, cizgi_katmani]  


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
    
    guney, bati, kuzey, dogu = ELAZIG_BBOX
    return pdk.ViewState(
        latitude=(guney + kuzey) / 2,
        longitude=(bati + dogu) / 2,
        zoom=11,
        pitch=45,
        bearing=0,
    )


def _dinamik_gorunum_hesapla(nokta_df: pd.DataFrame) -> pdk.ViewState:
   
    if len(nokta_df) < 5:
    
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
       
        gorunum.zoom = min(max(gorunum.zoom, 9), 13)
    else:
        gorunum = _varsayilan_gorunum()

    return pdk.Deck(
        layers=katmanlar,
        initial_view_state=gorunum,
        map_provider="carto",
        map_style=pdk.map_styles.CARTO_DARK,  
        tooltip=_TOOLTIP,
    )



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
    kol1, kol2, kol3, kol4, kol5 = st.columns(5)
    kol1.metric("🔥 Toplam Olay Sayısı", metrikler["toplam_olay"])
    kol2.metric("🏚️ Kritik / Hasarlı Tesisler", metrikler["kritik_tesis"])
    kol3.metric("🚧 Kapalı Yollar / Güzergahlar", metrikler["kapali_yol"])
    kol4.metric("🪖 Görevdeki Birlikler", metrikler["gorevdeki_birlik"])
    kol5.metric("🛣️ Kayıtlı Altyapı Segmenti", f"{metrikler['toplam_altyapi']:,}".replace(",", "."))



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

    fetch_metrics.clear()
    fetch_dynamic_crisis_points.clear()
    fetch_road_lines.clear()
    fetch_facility_points.clear()
    fetch_unit_points.clear()
    fetch_admin_area_points.clear()
    fetch_event_points.clear()
    fetch_recent_report.clear()
    fetch_ai_recommendations.clear()



_OSM_KAYNAKLI_ALTYAPI_TIPLERI = frozenset({InfrastructureType.SOKAK, InfrastructureType.KOPRU})

_OSM_KAYNAKLI_FACILITY_TIPLERI = frozenset(
    {FacilityType.HASTANE, FacilityType.ASKERI_US, FacilityType.HAVALIMANI}
)
_OSM_KAYNAKLI_UNIT_TIPLERI = frozenset({UnitType.POLIS, UnitType.ITFAIYE})


def _osm_kaynakli_gercek_varlik_mi(varlik: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
   
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
   
    yazilan_dugum = 0
    altyapi_guncellemeleri = 0
    engellenen_ghost = 0
    engellenen_sahte_koordinat = 0
    yer_adiyla_baglanan = 0
    harici_geokodlama_ile_baglanan = 0
    yazilan_event_koordinatlari: List[Tuple[float, float]] = []
  
    koordinat_zarfi = db.get_real_koordinat_zarfi()
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
                    continue  
                logger.warning(
                    "Ghost %s ENGELLENDI: '%s' icin gercek bir OSM eslesmesi "
                    "bulunamadi; sahte koordinatli dugum YARATILMADI.", etiket, varlik.isim,
                )
                engellenen_ghost += 1
                continue

           
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
 
        iliski.kaynak_id = altyapi_isim_eslemesi.get(iliski.kaynak_id, iliski.kaynak_id)
        iliski.hedef_id = altyapi_isim_eslemesi.get(iliski.hedef_id, iliski.hedef_id)
        db.add_relationship(iliski)
        yazilan_iliski += 1

    return (
        yazilan_dugum, yazilan_iliski, altyapi_guncellemeleri,
        engellenen_ghost, engellenen_sahte_koordinat, yer_adiyla_baglanan,
        harici_geokodlama_ile_baglanan, yazilan_event_koordinatlari,
    )




def _rapor_metnini_isle_ve_yenile(
    db: Neo4jConnection, parser: OllamaParser, metin: str, kaynak_etiketi: str
) -> None:
   
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
        except Exception as exc:  
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

        if yer_adiyla_baglanan:
            st.info(
                f"🧭 {yer_adiyla_baglanan} kayıt için üretilen koordinat gerçek bölge "
                "dışındaydı; isimde geçen yer adına göre GERÇEK bir yol/köprü verisinin "
                "merkez koordinatına bağlandı (bkz. Coğrafi Bağlama kuralı)."
            )
        
        if harici_geokodlama_ile_baglanan:
            st.info(
                f"🌍 {harici_geokodlama_ile_baglanan} kayıt için ne LLM koordinatı ne yerel "
                "veri eşleşmesi vardı; harici geocoding (Nominatim) ile gerçek konuma "
                "bağlandı. Bu bölge için yerel yol ağı/birlik verisi henüz YÜKLENMEMİŞ "
                "olabilir — Karar Destek Motoru bu durumda dürüstçe 'birlik bulunamadı' "
                "diyecektir (uydurma bir öneri ÜRETMEYECEKTİR)."
            )
        
        if engellenen_ghost:
            st.warning(
                f"🛡️ {engellenen_ghost} altyapı/tesis/birim kaydı için GERÇEK bir OSM "
                "eşleşmesi bulunamadı; sistem sahte koordinatlı bir 'hayali' (ghost) "
                "düğüm YARATMADI (bkz. Kesin Varlık Eşleştirme kuralı). İlgili olay(lar) "
                "yine de bağımsız olarak kaydedildi, ancak bu varlığa bir ilişki "
                "kurulamamış olabilir."
            )
       
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


    _clear_data_caches()
    st.sidebar.success(
        f"✅ Kriz senaryosu sıfırlandı: {sonuc['silinen_olay']} olay silindi, "
        f"{sonuc['acilan_altyapi']} altyapı düğümü tekrar açıldı, "
        f"{sonuc['iyilesen_tesis']} tesis + {sonuc['iyilesen_birim']} birim tekrar Aktif "
        "yapıldı. Şehir topolojisi korundu."
    )
    st.rerun()



_FILTRE_HASTANE_KEY = "filtre_hastane"
_FILTRE_ITFAIYE_KEY = "filtre_itfaiye"
_FILTRE_POLIS_KEY = "filtre_polis"
_FILTRE_ASKERI_US_KEY = "filtre_askeri_us"
_FILTRE_HAVALIMANI_KEY = "filtre_havalimani"
_FILTRE_KAPALI_YOL_KEY = "filtre_kapali_yol"


def _render_tactical_filter_section() -> None:
   
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
   
    _render_scenario_library_section(db, parser)
    st.sidebar.markdown("---")
    _render_report_input_section(db, parser)
    st.sidebar.markdown("---")
    _render_tactical_filter_section()
    st.sidebar.markdown("---")
    _render_database_reset_section(db)


def render_map_section(db: Neo4jConnection, engine: DecisionEngine) -> None:
   
    st.markdown("### 🗺️ 3B Taktiksel Durum Haritası")

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

    
    tesis_df, birlik_df, kriz_df = _tesis_birlik_kriz_filtrele(tesis_df, birlik_df, kriz_df)

    toplam_varlik = sum(
        len(df) for df in (sokak_df, kopru_df, yol_df, tesis_df, birlik_df, admin_df, olay_df, kriz_df)
    )
    if toplam_varlik == 0:
        st.info("Haritada gösterilecek herhangi bir varlık yok. Sol panelden bir rapor işleyerek başlayın.")
        return

    sevk_df = pd.DataFrame()
    try:
        with st.spinner("SAKOM Karar Destek Motoru taktiksel sevk güzergahlarını hesaplıyor..."):
            ai_sonuc = fetch_ai_recommendations(db, engine)
        sevk_df = _sevk_rotalari_df_olustur(ai_sonuc)
    except RuntimeError as exc:
        logger.error("CRITICAL UI ERROR (taktiksel sevk katmanı): %s", exc, exc_info=True)
        print(f"CRITICAL UI ERROR (taktiksel sevk katmanı): {exc}")
        st.caption(f"⚠️ Taktiksel sevk katmanı hesaplanamadı (SAKOM motoru şu an erişilemiyor): {exc}")
    except Exception as exc:  
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
    
    st.markdown("### 📋 Son Durum Raporu")
    rapor_df = fetch_recent_report(db)
    if rapor_df.empty:
        st.info("Henüz kayıtlı bir olay yok.")
        return
 
    stilli_df = rapor_df.style.map(_siddet_hucre_stili, subset=["Şiddet"])
    st.dataframe(stilli_df, width="stretch", hide_index=True)


def render_ai_staff_section(db: Neo4jConnection, engine: DecisionEngine) -> None:
  
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
    except Exception as exc:  
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

   
    if sonuc.get("cografi_red"):
        st.error(f"🚫 {sonuc.get('cografi_red_nedeni') or 'Coğrafi tutarsızlık tespit edildi.'}")
        return

    
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

    ikonlar = ["🚨", "🧭", "📦"]
    for i, madde in enumerate(maddeler):
        kutu = st.warning if i == 0 else st.info
        kutu(f"**{ikonlar[i % len(ikonlar)]} Taktiksel Karar {i + 1}**\n\n{madde}")

    with st.expander("📊 Değerlendirmeye Esas Anlık Saha Durumu (GraphRAG Bağlamı)"):
        st.code(sonuc["durum_ozeti"], language="markdown")



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
