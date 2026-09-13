"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
TUCBS (Türkiye Ulusal Coğrafi Bilgi Sistemi) — Büyük Veri (Big Data) ETL
Yükleme Modülü.

Bu modül, "Proof of Concept" (Elazığ pilot bölgesi, `osm_loader.py`) ölçeğinden,
tüm Türkiye'yi sokak/mahalle düzeyinde kapsayacak (MİLYONLARCA düğüm) bir
Enterprise Karar Destek Sistemi'ne geçişin FAZ 2 bileşenidir. `osm_loader.py`
düzinelerce/yüzlerce düğümü TEK TEK (`add_node` ile, düğüm başına bir Cypher
round-trip) yazarken, bu modül milyonlarca satırı BELLEĞİ ŞİŞİRMEDEN ve
Neo4j'e binlerce ayrı istek atmadan yazacak şekilde tasarlanmıştır:

  1. `bulk_insert_nodes` / `bulk_insert_relationships`: `UNWIND` tabanlı
     TOPLU (batch, varsayılan 10.000 satır) yazma. Girdi bir ITERABLE'dır
     (generator kabul eder) — tüm veri asla tek seferde belleğe alınmaz.
  2. `generate_mock_tucbs_data`: Henüz elimizde gerçek bir TUCBS dosyası
     olmadığından, gerçekçi (Elazığ bbox'ı içinde, idari hiyerarşili) sahte
     veri üreten bir GENERATOR — gerçek bir GeoJSON/CSV okuyucusunun
     (Faz 3) yerini geçici olarak tutar; API'si (bir `Iterator[BaseNode]`
     döndürmesi) ile gerçek okuyucuyla DEĞİŞTİRİLEBİLİR olacak şekilde
     tasarlanmıştır.

MİMARİ KARAR (asenkron/senkron): Mevcut `Neo4jConnection`, resmi SENKRON
`neo4j` sürücüsünü (GraphDatabase.driver/Session) kullanır. Buradaki gerçek
performans kazancı EŞ ZAMANLILIKTAN (concurrency) değil, N ayrı yazma
çağrısını N/batch_size'a indiren UNWIND-batch stratejisinden gelir; bu yüzden
BİLİNÇLİ olarak "optimize senkron" seçilmiş, mevcut sync mimariyle ÇATIŞAN
paralel bir async sürücü/oturum yığını EKLENMEMİŞTİR (gerçek eş zamanlı
yazma —ör. çok işçili bir yükleme kümesi— gerekirse, Faz 3'te `neo4j`'nin
`AsyncGraphDatabase` sürücüsüyle AYRI bir bağlantı katmanı olarak
değerlendirilebilir).

MİMARİ KISIT (multi-label + UNWIND): Neo4j'de düğüm etiketleri VE ilişki
tipleri Cypher'da LİTERAL olmak zorundadır (parametrize edilemez). Bu yüzden
TEK bir UNWIND sorgusuna farklı etiket/ilişki-tipi kombinasyonuna sahip
satırlar KARIŞTIRILAMAZ; her iki toplu yazma metodu da girdi akışını bu
kombinasyona göre GRUPLAYIP her grup için ayrı (ama yine batch'lenmiş) bir
UNWIND sorgusu çalıştırır (bkz. `_dugum_grubunu_yaz`/`_iliski_grubunu_yaz`).

Çalıştırmak için:
    python -m src.data_ingestion.tucbs_etl_loader
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from tqdm import tqdm

from src.core.database import Neo4jConnection, _konum_point_ifadesi
from src.core.models import (
    AdministrativeArea,
    AdministrativeAreaType,
    BaseNode,
    Facility,
    FacilityStatus,
    FacilityType,
    Infrastructure,
    InfrastructureType,
    OperationalStatus,
    Relationship,
    RelationshipType,
    SurfaceType,
)
from src.data_ingestion.osm_loader import ELAZIG_BBOX

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10_000

# Etiket-grubu anahtarı: (kategori_etiketi, (ek_etiket1, ek_etiket2, ...)).
# Tuple kullanılır çünkü dict anahtarı (gruplama) olarak hashlenebilir olmalı.
_LabelGroupKey = Tuple[str, Tuple[str, ...]]


# ---------------------------------------------------------------------------
# Toplu (batch) yazma motoru
# ---------------------------------------------------------------------------


class TucbsETLLoader:
    """TUCBS (veya benzeri) büyük hacimli coğrafi/idari açık veri
    kaynaklarını Neo4j Bilgi Grafı'na, bellek disiplinli ve batch-optimize
    bir şekilde yükleyen kurumsal ETL bileşeni.

    Kullanım:
        loader = TucbsETLLoader(Neo4jConnection())
        hiyerarsi: List[Relationship] = []
        sayaclar = loader.bulk_insert_nodes(
            generate_mock_tucbs_data(count=50_000, hiyerarsi_iliskileri_cikti=hiyerarsi)
        )
        loader.bulk_insert_relationships(hiyerarsi)
    """

    def __init__(self, db: Optional[Neo4jConnection] = None) -> None:
        self.db = db or Neo4jConnection()

    # ------------------------------------------------------------------ #
    # Toplu dugum (node) yazma
    # ------------------------------------------------------------------ #

    def bulk_insert_nodes(
        self,
        nodes: Iterable[BaseNode],
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress: bool = True,
        toplam_tahmini: Optional[int] = None,
    ) -> Dict[str, int]:
        """Büyük hacimli bir düğüm akışını (`nodes`, bir generator OLABİLİR)
        Neo4j'e `UNWIND` ile toplu (batch) olarak yazar.

        Bellek disiplini: `nodes` bir ITERABLE'dır; tüm liste bellekte
        TUTULMAZ — sadece o an biriken bir batch'lik (varsayılan 10.000)
        satır bellekte tutulur, dolar dolmaz Neo4j'e yazılıp hemen atılır.
        Bu, `generate_mock_tucbs_data` gibi bir generator'dan veya gerçek bir
        CSV/GeoJSON stream okuyucusundan gelen MİLYONLARCA satırı, kaç satır
        olursa olsun SABİT bir bellek tavanıyla işleyebilmek için ZORUNLUDUR.

        Multi-label kısıtlaması (bkz. modül docstring'i): akış, her düğümün
        `neo4j_labels()` sonucuna (kategori + ikincil etiket) göre gruplanır;
        her grup kendi batch'lerini bağımsız doldurur ve doldukça yazar.

        Returns:
            Kategori etiketi (ör. "Infrastructure") -> yazılan düğüm sayısı.
        """
        tamponlar: Dict[_LabelGroupKey, List[Dict[str, Any]]] = {}
        sayaclar: Dict[str, int] = {}

        akis: Iterable[BaseNode] = nodes
        if progress:
            akis = tqdm(nodes, total=toplam_tahmini, desc="Neo4j'e düğümler yazılıyor", unit="dugum")

        for node in akis:
            etiketler = node.neo4j_labels()
            grup_anahtari: _LabelGroupKey = (etiketler[0], tuple(etiketler[1:]))
            props = node.to_cypher_properties()
            satir = {
                "isim": node.isim,
                "props": props,
                "update_props": {k: v for k, v in props.items() if k not in ("id", "isim")},
            }

            grup = tamponlar.setdefault(grup_anahtari, [])
            grup.append(satir)

            if len(grup) >= batch_size:
                yazilan = self._dugum_grubunu_yaz(grup_anahtari, grup)
                sayaclar[grup_anahtari[0]] = sayaclar.get(grup_anahtari[0], 0) + yazilan
                tamponlar[grup_anahtari] = []

        # Kalan (batch_size'i doldurmamis) tum gruplari isin sonunda yaz.
        for grup_anahtari, satirlar in tamponlar.items():
            if satirlar:
                yazilan = self._dugum_grubunu_yaz(grup_anahtari, satirlar)
                sayaclar[grup_anahtari[0]] = sayaclar.get(grup_anahtari[0], 0) + yazilan

        return sayaclar

    def _dugum_grubunu_yaz(self, grup_anahtari: _LabelGroupKey, satirlar: List[Dict[str, Any]]) -> int:
        """Tek bir (kategori_etiketi, ek_etiketler) grubuna ait satır listesini
        TEK bir `UNWIND` sorgusuyla Neo4j'e yazar (bkz. `bulk_insert_nodes`).
        """
        kategori_etiketi, ek_etiketler = grup_anahtari
        ek_set_clause = ""
        if ek_etiketler:
            ek_etiket_str = "".join(f":{etiket}" for etiket in ek_etiketler)
            ek_set_clause = f" SET n{ek_etiket_str}"

        # `konum` (Point): bkz. `database._konum_point_ifadesi` docstring'i
        # ("MEKANSAL INDEKSLEME" notu) — tekil `add_node` yolundaki AYNI
        # mantik, UNWIND-batch yolu icin de burada tekrarlanir (Cypher
        # string uretimi iki yolda da BAGIMSIZ oldugundan, tek fonksiyona
        # cikaramiyoruz; ama AYNI `_konum_point_ifadesi` yardimcisindan
        # uretilir ki iki yol birbirinden SENKRON DISI kalmasin).
        query = (
            "UNWIND $rows AS row "
            f"MERGE (n:{kategori_etiketi} {{isim: row.isim}}) "
            "ON CREATE SET n = row.props, "
            f"n.konum = {_konum_point_ifadesi('row.props')} "
            "ON MATCH SET n += row.update_props, "
            f"n.konum = {_konum_point_ifadesi('row.update_props')}"
            f"{ek_set_clause} "
            "RETURN count(n) AS yazilan"
        )
        sonuc = self.db.execute_query(query, {"rows": satirlar}, write=True)
        return sonuc[0]["yazilan"] if sonuc else 0

    # ------------------------------------------------------------------ #
    # Toplu iliski (relationship) yazma
    # ------------------------------------------------------------------ #

    def bulk_insert_relationships(
        self,
        relationships: Iterable[Relationship],
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress: bool = True,
        toplam_tahmini: Optional[int] = None,
    ) -> int:
        """`bulk_insert_nodes` ile AYNI batch/gruplama stratejisiyle, büyük
        hacimli bir ilişki akışını Neo4j'e yazar.

        İlişki TİPİ (`tip`) da tıpkı etiketler gibi Cypher'da literal olmak
        zorunda olduğundan (parametrize edilemez, bkz.
        `database.relationship_to_cypher`'daki aynı kısıtlama), akış `tip`e
        göre gruplanır.

        ÖNEMLİ: Uç noktalar (`kaynak_id`/`hedef_id`, pratikte `isim` taşır —
        bkz. `models.Relationship`) bu toplu yol üzerinde `add_relationship`
        gibi bulanık (fuzzy) isim çözümlemesinden GEÇMEZ — performans için
        bilinçli bir ödünleşimdir; bu yüzden ilişkiler, karşılık gelen
        düğümler ZATEN yazılmış TAM isimlerle verilmelidir (ki
        `generate_mock_tucbs_data`'nın ürettiği ilişkiler zaten böyledir).
        """
        tamponlar: Dict[RelationshipType, List[Dict[str, Any]]] = {}
        yazilan_toplam = 0

        akis: Iterable[Relationship] = relationships
        if progress:
            akis = tqdm(relationships, total=toplam_tahmini, desc="Neo4j'e ilişkiler yazılıyor", unit="iliski")

        for rel in akis:
            satir = {
                "kaynak_isim": rel.kaynak_id,
                "hedef_isim": rel.hedef_id,
                "props": rel.to_cypher_properties(),
            }
            grup = tamponlar.setdefault(rel.tip, [])
            grup.append(satir)

            if len(grup) >= batch_size:
                yazilan_toplam += self._iliski_grubunu_yaz(rel.tip, grup)
                tamponlar[rel.tip] = []

        for tip, satirlar in tamponlar.items():
            if satirlar:
                yazilan_toplam += self._iliski_grubunu_yaz(tip, satirlar)

        return yazilan_toplam

    def _iliski_grubunu_yaz(self, tip: RelationshipType, satirlar: List[Dict[str, Any]]) -> int:
        query = (
            "UNWIND $rows AS row "
            "MATCH (a {isim: row.kaynak_isim}) "
            "MATCH (b {isim: row.hedef_isim}) "
            f"MERGE (a)-[r:{tip.value}]->(b) "
            "SET r += row.props "
            "RETURN count(r) AS yazilan"
        )
        sonuc = self.db.execute_query(query, {"rows": satirlar}, write=True)
        return sonuc[0]["yazilan"] if sonuc else 0


# ---------------------------------------------------------------------------
# Mock TUCBS veri ureteci
# ---------------------------------------------------------------------------

# Elazığ'ın gerçek 11 ilçesi (idari hiyerarşi katmanının SABİT/gerçekçi
# iskeleti; `count` parametresine göre ÖLÇEKLENMEZ — bir il her zaman sabit
# sayıda ilçeye sahiptir, "büyük veri" hacmi asıl sokak/köprü katmanından gelir).
_ELAZIG_ILCELERI: Tuple[str, ...] = (
    "Merkez", "Kovancilar", "Karakocan", "Palu", "Baskil",
    "Agin", "Aricak", "Alacakaya", "Sivrice", "Maden", "Keban",
)

_MAHALLE_ADI_KOKLERI: Tuple[str, ...] = (
    "Cumhuriyet", "Yenisehir", "Fatih", "Ataturk", "Baris", "Gazi",
    "Kultur", "Yildiz", "Zafer", "Camlik", "Bahcelievler", "Huzur",
)

_NEHIR_VADI_ADLARI: Tuple[str, ...] = (
    "Firat", "Karasu", "Peri Cayi", "Hazar", "Uzuncayir", "Golcuk",
)

_HASTANE_TURU_ADLARI: Tuple[str, ...] = (
    "Devlet Hastanesi", "Egitim ve Arastirma Hastanesi", "Ozel Hastanesi",
)

# count'tan BAGIMSIZ, sabit boyutlu idari referans katmani.
_MOCK_MAHALLE_SAYISI = 300


def _rastgele_bitis_noktasi(
    rastgele: random.Random, enlem: float, boylam: float, uzunluk_km: float
) -> Tuple[float, float]:
    """Bir başlangıç noktasından, rastgele bir yönde (bearing) `uzunluk_km`
    kadar uzaklıkta kaba (1° ≈ 111 km) bir bitiş noktası üretir — mock
    Infrastructure kayıtlarının `bitis_enlem`/`bitis_boylam` alanlarını
    (bkz. `models.Infrastructure`) doldurmak için kullanılır."""
    bearing_derece = rastgele.uniform(0, 360)
    mesafe_derece = uzunluk_km / 111.0
    bearing_rad = math.radians(bearing_derece)
    delta_enlem = mesafe_derece * math.cos(bearing_rad)
    delta_boylam = mesafe_derece * math.sin(bearing_rad)
    return enlem + delta_enlem, boylam + delta_boylam


def generate_mock_tucbs_data(
    count: int = 50_000,
    bbox: Tuple[float, float, float, float] = ELAZIG_BBOX,
    seed: Optional[int] = None,
    hiyerarsi_iliskileri_cikti: Optional[List[Relationship]] = None,
) -> Iterator[BaseNode]:
    """Elazığ bbox'ı içinde, gerçek TUCBS dosyası gelene kadar `bulk_insert_nodes`'u
    uçtan uca sınamak için GERÇEKÇİ (ama sahte) bir düğüm akışı ÜRETEN generator.

    `count`, ÖLÇEKLENEN katmanı (Sokak/Köprü/Hastane) belirler; idari
    hiyerarşi katmanı (11 gerçek Elazığ ilçesi + ~300 mahalle) SABİTTİR ve
    `count`'tan bağımsızdır (bir ilin ilçe sayısı, o ildeki sokak sayısıyla
    ORANTILI DEĞİLDİR — bu bilinçli bir modelleme kararıdır).

    Dağılım (illustratif; gerçek TUCBS oranlarını temsil ETMEZ, sadece
    pipeline'ı gerçekçi bir karışımla sınamak içindir):
      - Ilce   : 11  (sabit)
      - Mahalle: 300 (sabit; her biri rastgele bir ilçeye PART_OF bağlanır)
      - Hastane: count'un ~%1'i (en az 1)
      - Kopru  : count'un ~%10'u (en az 1)
      - Sokak  : kalan (~%89) — böylece Sokak+Kopru+Hastane TAM OLARAK `count`
        kadar olur; idari katman bunun ÜZERİNE eklenir.

    İSİM BENZERSİZLİĞİ: Neo4j'e MERGE-by-isim mantığıyla yazıldığından (bkz.
    `database.node_to_cypher`), her üretilen düğümün `isim`i BENZERSİZ olmak
    ZORUNDADIR — aksi halde iki farklı sahte "sokak" yanlışlıkla TEK düğüme
    birleşirdi. Bu yüzden HER isim, global olarak benzersiz artan bir sayaç
    içerir (ör. "142. Sokak" — ki bu zaten gerçekçi bir Türkiye sokak adı
    biçimidir); köprü/hastane isimlerindeki sayaç eki estetik değil, KESİN
    doğruluk garantisi içindir.

    `hiyerarsi_iliskileri_cikti` verilirse (boş bir liste), Mahalle düğümleri
    üretilirken oluşan Mahalle -> İlçe (`PART_OF`) ilişkileri bu listeye YAN
    ETKİ (side-effect) olarak eklenir — bağımsız bir ikinci rastgele üretimle
    bu eşleşmeleri yeniden türetmek, iki ayrı rastgele akışın isim
    eşleşmesini KAYBETME riski taşırdı; bu yüzden TEK geçimde toplanır. Bu
    listeyi okumadan ÖNCE, döndürülen generator'ın TAMAMEN tüketilmiş
    (`bulk_insert_nodes` ile işlenmiş) olması gerekir.
    """
    rastgele = random.Random(seed)
    guney, bati, kuzey, dogu = bbox

    hastane_sayisi = max(1, round(count * 0.01))
    kopru_sayisi = max(1, round(count * 0.10))
    sokak_sayisi = max(1, count - hastane_sayisi - kopru_sayisi)

    sayac = 0  # TUM uretilen dugumler icin GLOBAL benzersiz sayac (isim garantisi).

    # --- 1) SABIT idari hiyerarsi katmani: Ilce ---
    ilce_dugumleri: List[AdministrativeArea] = []
    for ilce_adi in _ELAZIG_ILCELERI:
        sayac += 1
        enlem, boylam = rastgele.uniform(guney, kuzey), rastgele.uniform(bati, dogu)
        ilce = AdministrativeArea(
            isim=f"{ilce_adi} Ilcesi",
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            alan_tipi=AdministrativeAreaType.ILCE,
            nufus=rastgele.randint(20_000, 300_000),
            bina_sayisi=rastgele.randint(5_000, 80_000),
            risk_faktoru=round(rastgele.uniform(0.05, 0.55), 2),
        )
        ilce_dugumleri.append(ilce)
        yield ilce

    # --- 2) SABIT idari hiyerarsi katmani: Mahalle (+ PART_OF -> Ilce) ---
    for i in range(_MOCK_MAHALLE_SAYISI):
        sayac += 1
        ust_ilce = rastgele.choice(ilce_dugumleri)
        mahalle_kok = rastgele.choice(_MAHALLE_ADI_KOKLERI)
        isim = f"{mahalle_kok} Mahallesi ({ust_ilce.isim} #{i + 1})"
        enlem, boylam = rastgele.uniform(guney, kuzey), rastgele.uniform(bati, dogu)
        mahalle = AdministrativeArea(
            isim=isim,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            alan_tipi=AdministrativeAreaType.MAHALLE,
            nufus=rastgele.randint(300, 25_000),
            bina_sayisi=rastgele.randint(80, 6_000),
            risk_faktoru=round(rastgele.uniform(0.0, 1.0), 2),
        )
        yield mahalle
        if hiyerarsi_iliskileri_cikti is not None:
            hiyerarsi_iliskileri_cikti.append(
                Relationship(kaynak_id=mahalle.isim, hedef_id=ust_ilce.isim, tip=RelationshipType.PART_OF)
            )

    # --- 3) OLCEKLENEN katman: Sokak (count'un ~%89'u) ---
    for i in range(sokak_sayisi):
        sayac += 1
        enlem, boylam = rastgele.uniform(guney, kuzey), rastgele.uniform(bati, dogu)
        uzunluk_km = round(rastgele.uniform(0.1, 2.0), 2)
        bitis_enlem, bitis_boylam = _rastgele_bitis_noktasi(rastgele, enlem, boylam, uzunluk_km)
        yield Infrastructure(
            isim=f"{rastgele.choice(_ELAZIG_ILCELERI)} {i + 1}. Sokak",
            enlem=enlem,
            boylam=boylam,
            bitis_enlem=bitis_enlem,
            bitis_boylam=bitis_boylam,
            durum=OperationalStatus.AKTIF,
            infrastructure_type=InfrastructureType.SOKAK,
            uzunluk_km=uzunluk_km,
            acik_mi=True,
            tonaj_kapasitesi=rastgele.choice([None, 3.5, 7.5]),
            serit_sayisi=rastgele.choice([1, 2]),
            zemin_tipi=rastgele.choice(list(SurfaceType)),
        )

    # --- 4) OLCEKLENEN katman: Kopru (count'un ~%10'u) ---
    for i in range(kopru_sayisi):
        sayac += 1
        enlem, boylam = rastgele.uniform(guney, kuzey), rastgele.uniform(bati, dogu)
        uzunluk_km = round(rastgele.uniform(0.05, 1.5), 3)
        bitis_enlem, bitis_boylam = _rastgele_bitis_noktasi(rastgele, enlem, boylam, uzunluk_km)
        yield Infrastructure(
            isim=f"{rastgele.choice(_NEHIR_VADI_ADLARI)} {i + 1}. Kopru",
            enlem=enlem,
            boylam=boylam,
            bitis_enlem=bitis_enlem,
            bitis_boylam=bitis_boylam,
            durum=OperationalStatus.AKTIF,
            infrastructure_type=InfrastructureType.KOPRU,
            uzunluk_km=uzunluk_km,
            acik_mi=True,
            tonaj_kapasitesi=rastgele.choice([40.0, 60.0, 80.0]),
            max_yukseklik_metre=round(rastgele.uniform(3.5, 4.5), 2),
            serit_sayisi=rastgele.choice([2, 4]),
            zemin_tipi=SurfaceType.BETON,
        )

    # --- 5) OLCEKLENEN katman: Hastane (count'un ~%1'i) ---
    for i in range(hastane_sayisi):
        sayac += 1
        enlem, boylam = rastgele.uniform(guney, kuzey), rastgele.uniform(bati, dogu)
        yield Facility(
            isim=f"{rastgele.choice(_ELAZIG_ILCELERI)} {rastgele.choice(_HASTANE_TURU_ADLARI)} {i + 1}",
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.HASTANE,
            kapasite=rastgele.randint(50, 400),
            mevcut_durum=FacilityStatus.AKTIF,
            ameliyathane_sayisi=rastgele.randint(2, 20),
            yogun_bakim_kapasitesi=rastgele.randint(5, 60),
        )

    logger.info(
        "generate_mock_tucbs_data tamamlandi: %d ilce, %d mahalle, %d sokak, %d kopru, %d hastane (toplam sayac=%d)",
        len(_ELAZIG_ILCELERI), _MOCK_MAHALLE_SAYISI, sokak_sayisi, kopru_sayisi, hastane_sayisi, sayac,
    )


def main() -> None:  # pragma: no cover - manuel/CLI calistirma amaclidir
    logging.basicConfig(level=logging.INFO)

    MOCK_KAYIT_SAYISI = 50_000

    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()

    loader = TucbsETLLoader(db)
    hiyerarsi_iliskileri: List[Relationship] = []

    print(f"Mock TUCBS verisi uretiliyor ve yazılıyor (hedef: {MOCK_KAYIT_SAYISI} olcekli kayit)...")
    dugum_sayaclari = loader.bulk_insert_nodes(
        generate_mock_tucbs_data(count=MOCK_KAYIT_SAYISI, hiyerarsi_iliskileri_cikti=hiyerarsi_iliskileri),
        toplam_tahmini=MOCK_KAYIT_SAYISI + len(_ELAZIG_ILCELERI) + _MOCK_MAHALLE_SAYISI,
    )

    print("\nDugum yukleme tamamlandi:")
    for kategori, adet in dugum_sayaclari.items():
        print(f"  - {kategori}: {adet}")

    print(f"\nIdari hiyerarsi (PART_OF) iliskileri yaziliyor ({len(hiyerarsi_iliskileri)} adet)...")
    yazilan_iliski = loader.bulk_insert_relationships(
        hiyerarsi_iliskileri, toplam_tahmini=len(hiyerarsi_iliskileri)
    )
    print(f"Yazilan iliski sayisi: {yazilan_iliski}")


if __name__ == "__main__":
    main()
