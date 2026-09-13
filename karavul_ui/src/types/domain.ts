/**
 * KARAVUL — Taktiksel Komuta Merkezi
 * ===================================
 * Bu dosyadaki tipler, `api.py`deki Pydantic yanıt modelleriyle BİREBİR
 * eşleşir (alan adları dahil — backend Türkçe alan adları kullanıyor,
 * burada da AYNI isimler korunur ki iki taraf arasında sessiz bir
 * isim-uyuşmazlığı riski olmasın).
 */

export interface TesisDurumu {
  id?: string | null
  isim: string
  tip?: string | null
  durum?: string | null
  enlem: number
  boylam: number
  kapasite?: number | null
}

export interface AltyapiDurumu {
  isim: string
  tip?: string | null
  enlem: number
  boylam: number
  uzunluk_km?: number | null
  /** Her ikisi de varsa bu kayıt bir ÇİZGİ (LineString) olarak çizilir; yoksa tek nokta. */
  bitis_enlem?: number | null
  bitis_boylam?: number | null
}

export interface BirlikDurumu {
  isim: string
  tip?: string | null
  personel?: number | null
  hareket_kabiliyeti?: string | null
  enlem: number
  boylam: number
  durum?: string | null
}

export interface OlayDurumu {
  isim: string
  tip?: string | null
  siddet?: string | null
  etki_alani_km?: number | null
  enlem: number
  boylam: number
  zaman?: string | null
}

export interface YerlesimDurumu {
  isim: string
  il?: string | null
  yerlesim_tipi?: string | null
  nufus?: number | null
  enlem: number
  boylam: number
}

export interface AltyapiDurumYaniti {
  hasarli_tesisler: TesisDurumu[]
  /** Durum FARK ETMEKSİZİN (Aktif dahil) TÜM tesisler — bkz. `api.py`daki
   * `_TUM_TESIS_LISTELEME_LIMITI` docstring'i: `hasarli_tesisler` aktif kriz
   * yokken her zaman boştur, harita katmanı bu yüzden BUNU kullanır. */
  tum_tesisler: TesisDurumu[]
  kapali_yollar: AltyapiDurumu[]
  aktif_birlikler: BirlikDurumu[]
  kritik_olaylar: OlayDurumu[]
  yerlesimler: YerlesimDurumu[]
}

export interface KrizRaporuYaniti {
  yazilan_varlik_sayisi: number
  yazilan_iliski_sayisi: number
  guncellenen_altyapi_sayisi: number
  engellenen_ghost_sayisi: number
  engellenen_sahte_koordinat_sayisi: number
  dogrulama_hatalari: string[]
  durum_ozeti: string
  taktiksel_oneriler: string[]
  uyari?: string | null
}

export interface SifirlamaYaniti {
  silinen_olay: number
  acilan_altyapi: number
  iyilesen_tesis: number
  iyilesen_birim: number
}

export interface OlayCozumYaniti {
  olay_ismi: string
  silindi: boolean
  acilan_altyapi_sayisi: number
  iyilesen_tesis_sayisi: number
}
