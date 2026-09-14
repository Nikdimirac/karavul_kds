

from __future__ import annotations

from abc import ABC
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_TR_KATLAMA_TABLOSU = str.maketrans(
    {"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
     "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c"}
)


def _tr_katla(deger: str) -> str:
 
    return (
        deger.strip().translate(_TR_KATLAMA_TABLOSU).lower()
        .replace(" ", "").replace("-", "").replace(".", "").replace("_", "")
    )


class GecersizFacilityTuruAtlandi(ValueError):
   

class GecersizInfrastructureTuruAtlandi(ValueError):
 
class NodeLabel(str, Enum):
   
    FACILITY = "Facility"
    INFRASTRUCTURE = "Infrastructure"
    ENERGY_INFRASTRUCTURE = "EnergyInfrastructure"
    COMMUNICATION_NETWORK = "CommunicationNetwork"
    RESOURCE_HUB = "ResourceHub"
    UNIT = "Unit"
    EVENT = "Event"
    ADMINISTRATIVE_AREA = "AdministrativeArea"
    
    SETTLEMENT = "Settlement"


class OperationalStatus(str, Enum):
    

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
 
    AKTIF = "Aktif"
    HASARLI = "Hasarlı"
    YOK_EDILDI = "Yok Edildi"


class InfrastructureType(str, Enum):
    KARAYOLU = "Karayolu"
    DEMIRYOLU = "Demiryolu"
    KOPRU = "Kopru"
    TUNEL = "Tunel"
   
    VIYADUK = "Viyaduk"
    SOKAK = "Sokak"


_FACILITY_TYPE_TABLOSU: Dict[str, str] = {_tr_katla(uye.value): uye.value for uye in FacilityType}

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


_INFRASTRUCTURE_TYPE_TABLOSU: Dict[str, str] = {
    **{_tr_katla(uye.value): uye.value for uye in InfrastructureType},
    **_INFRASTRUCTURE_TYPE_ES_ANLAMLILARI,
}



class SurfaceType(str, Enum):
   


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
  
    AGIR_MUHENDISLIK = "Agir Muhendislik"
    ARAMA_KURTARMA = "Arama Kurtarma"
    LOJISTIK = "Lojistik"
  
    SAHIL_GUVENLIK = "Sahil Guvenlik"


class MobilityStatus(str, Enum):
  

    STATIK = "Statik"
    SINIRLI_HAREKETLI = "Sinirli Hareketli"
    TAM_HAREKETLI = "Tam Hareketli"


class UnitMovementType(str, Enum):
   

    MOTORIZE = "Motorize"
    HAVA_INDIRME = "Hava Indirme"
    YAYA = "Yaya"


class UnitSpecialization(str, Enum):
 

    KBRN = "KBRN"
    ENKAZ = "Enkaz"
    SIHHIYE = "Sihhiye"


class EventType(str, Enum):
    SAVAS = "Savas"
    DEPREM = "Deprem"
    SEL = "Sel"
    YANGIN = "Yangin"
    SIBER_SALDIRI = "Siber Saldiri"
   
    PATLAMA = "Patlama"
   
    ORMAN_YANGINI = "Orman Yangini"
    
    CIG = "Cig"
   
    HEYELAN = "Heyelan"
   
    TEROR = "Teror"
    
    TAHLIYE = "Tahliye"
    
    SALGIN_HASTALIK = "Salgin Hastalik"
   
    IZDIHAM = "Izdiham"
   
    GEMI_KAZASI = "Gemi Kazasi"
 
    KIMYASAL_SIZINTI = "Kimyasal Sizinti"
  
    UCAK_KAZASI = "Ucak Kazasi"

    TREN_KAZASI = "Tren Kazasi"

    TRAFIK_KAZASI = "Trafik Kazasi"
   
    BARAJ_COKMESI = "Baraj Cokmesi"
   
    MADEN_KAZASI = "Maden Kazasi"
   
    FIRTINA = "Firtina"
 
    TSUNAMI = "Tsunami"
  
    RADYASYON_SIZINTISI = "Radyasyon Sizintisi"
    
    VOLKANIK_PATLAMA = "Volkanik Patlama"
  
    BINA_COKMESI = "Bina Cokmesi"
    
    TOPLU_ZEHIRLENME = "Toplu Zehirlenme"
   
    KURAKLIK = "Kuraklik"
  


class EventSeverity(str, Enum):

    DUSUK = "Dusuk"
    ORTA = "Orta"
    YUKSEK = "Yuksek"
    KRITIK = "Kritik"
    KATASTROFIK = "Katastrofik"


class AdministrativeAreaType(str, Enum):

    MAHALLE = "Mahalle"
    KOY = "Koy"
    ILCE = "Ilce"
    BOLGE = "Bolge"



class RelationshipType(str, Enum):

    CONNECTED_TO = "CONNECTED_TO"      
    DEPENDS_ON = "DEPENDS_ON"         
    AFFECTS = "AFFECTS"               
    STATIONED_AT = "STATIONED_AT"    
    SUPPLIES = "SUPPLIES"             
    ROUTES_THROUGH = "ROUTES_THROUGH"   
    CONTROLS = "CONTROLS"               
    PROTECTS = "PROTECTS"            
    THREATENS = "THREATENS"         
    COMMUNICATES_WITH = "COMMUNICATES_WITH"  
    LOCATED_NEAR = "LOCATED_NEAR"      
  
    LOCATED_IN = "LOCATED_IN"
  
    PART_OF = "PART_OF"


class Relationship(BaseModel):


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
        props: Dict[str, Any] = {
            "agirlik": self.agirlik,
            "aktif_mi": self.aktif_mi,
            "olusturulma_tarihi": self.olusturulma_tarihi.isoformat(),
        }
        props.update(self.ozellikler)
        return {k: v for k, v in props.items() if v is not None}


class BaseNode(BaseModel, ABC):
    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)

   
    neo4j_label: NodeLabel = NodeLabel.FACILITY


    _ALT_TIP_ALANI: ClassVar[Optional[str]] = None
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {}

    id: str = Field(default_factory=lambda: str(uuid4()), description="Benzersiz düğüm kimliği")
    isim: str = Field(..., description="Varlığın adı")
    aciklama: Optional[str] = Field(default=None, description="Serbest metin açıklama")

    bolge: Optional[str] = Field(
        default=None,
        description="Operasyon bölgesi/il adı (ör. 'Elazığ') — çok-şehir mimarisinde filtreleme için",
    )

    
    enlem: float = Field(..., ge=-90.0, le=90.0, description="Enlem (latitude)")
    boylam: float = Field(..., ge=-180.0, le=180.0, description="Boylam (longitude)")

    durum: OperationalStatus = Field(
        default=OperationalStatus.BILINMIYOR, description="Genel operasyonel statü"
    )

    olusturulma_tarihi: datetime = Field(default_factory=_utcnow)
    guncelleme_tarihi: datetime = Field(default_factory=_utcnow)

    def to_cypher_properties(self) -> Dict[str, Any]:

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




class Facility(BaseNode):
  
    neo4j_label: NodeLabel = NodeLabel.FACILITY

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

    ameliyathane_sayisi: Optional[int] = Field(
        default=None, ge=0, description="[Sadece Hastane] Aktif ameliyathane sayısı"
    )
    yogun_bakim_kapasitesi: Optional[int] = Field(
        default=None, ge=0, description="[Sadece Hastane] Yoğun bakım (ICU) yatak kapasitesi"
    )

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


    neo4j_label: NodeLabel = NodeLabel.INFRASTRUCTURE

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

    tonaj_kapasitesi: Optional[float] = Field(
        default=None, ge=0, description="Taşıyabileceği azami tonaj (ton); bilinmiyorsa None"
    )

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

   
    highway_tipi: Optional[str] = Field(
        default=None,
        description="OSM 'highway' etiketinin ham değeri (motorway/trunk/primary/secondary/residential/...) — harita LOD filtresi için",
    )

    
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

    neo4j_label: NodeLabel = NodeLabel.ENERGY_INFRASTRUCTURE

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

    neo4j_label: NodeLabel = NodeLabel.COMMUNICATION_NETWORK

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

    neo4j_label: NodeLabel = NodeLabel.UNIT

    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "unit_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Askeri Birlik": "MilitaryUnit",
        "AFAD": "AFAD",
        "Saglik": "HealthTeam",
        "Itfaiye": "FireDepartment",
        "Polis": "Police",
        "Agir Muhendislik": "HeavyEngineering",
      
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

    neo4j_label: NodeLabel = NodeLabel.EVENT

    _ALT_TIP_ALANI: ClassVar[Optional[str]] = "event_type"
    _ALT_ETIKET_ESLEMESI: ClassVar[Dict[str, str]] = {
        "Savas": "War",
        "Deprem": "Earthquake",
        "Sel": "Flood",
        "Yangin": "Fire",
        "Siber Saldiri": "CyberAttack",
        "Patlama": "Explosion",
        
        "Orman Yangini": "ForestFire",
        "Cig": "Avalanche",
        "Heyelan": "Landslide",
        "Teror": "Terrorism",
        "Tahliye": "Evacuation",
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
 

    neo4j_label: NodeLabel = NodeLabel.ADMINISTRATIVE_AREA

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


    IL_MERKEZI = "Il Merkezi"
    ILCE_MERKEZI = "Ilce Merkezi"


class Settlement(BaseNode):
   

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
