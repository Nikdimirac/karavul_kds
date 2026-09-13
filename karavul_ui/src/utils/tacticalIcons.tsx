import {
  Anchor,
  Ban,
  Building2,
  Flame,
  HardHat,
  Home,
  Landmark,
  LifeBuoy,
  MapPin,
  Plane,
  Plus,
  Shield,
  Star,
  Truck,
  type LucideIcon,
} from 'lucide-react'

/**
 * KARAVUL — "Faz 6: 3B Taktiksel Harita" taktiksel simge sözlüğü.
 * `tip` alanları backend'deki `models.FacilityType`/`UnitType` enum
 * DEĞERLERİYLE (Türkçe, boşluklu — ör. "Askeri Us") BİREBİR eşleşir.
 */

export const FACILITY_IKON: Record<string, LucideIcon> = {
  Havalimani: Plane,
  Hastane: Plus,
  'Askeri Us': Shield,
  Liman: Anchor,
  Siginak: Home,
}
export const FACILITY_VARSAYILAN_IKON: LucideIcon = Building2

export const BIRLIK_IKON: Record<string, LucideIcon> = {
  'Askeri Birlik': Shield,
  AFAD: LifeBuoy,
  Saglik: Plus,
  Itfaiye: Flame,
  Polis: Star,
  'Agir Muhendislik': HardHat,
  'Arama Kurtarma': LifeBuoy,
  Lojistik: Truck,
}
export const BIRLIK_VARSAYILAN_IKON: LucideIcon = MapPin

export const ALTYAPI_IKON: LucideIcon = Ban

// "FAZ 8: SİVİL YERLEŞİM YERLERİ": il merkezi (Landmark —
// daha "kentsel/önemli" bir simge) ile ilçe merkezi (Home — sıradan bir
// yerleşim) GÖRSEL OLARAK ayrılır; ikisi de tesis/birlik simgelerinden
// FARKLI, sivil/nötr bir renk (bkz. `RENK.settlement`) taşır.
export function yerlesimIkonuGetir(yerlesimTipi: string | null | undefined): LucideIcon {
  return yerlesimTipi === 'Il Merkezi' ? Landmark : Home
}

/** Taktiksel renk paleti (bkz. `index.css` @theme token'larıyla AYNI hex'ler). */
export const RENK = {
  kriz: '#ef4444',
  alert: '#f59e0b',
  tactical: '#38bdf8',
  ready: '#22c55e',
  ink: '#e5e7eb',
  // Sivil yerleşim (Settlement) — kriz/taktiksel renklerden BİLİNÇLİ olarak
  // FARKLI, nötr/donuk bir mor-gri: komutanın gözü bunu asla bir tehdit/
  // aktif kriz rengiyle KARIŞTIRMAMALI (bkz. `TaktikselRozet` kullanımı).
  civil: '#a1a1c9',
} as const

export function facilityIkonuGetir(tip: string | null | undefined): LucideIcon {
  return (tip && FACILITY_IKON[tip]) || FACILITY_VARSAYILAN_IKON
}

export function birlikIkonuGetir(tip: string | null | undefined): LucideIcon {
  return (tip && BIRLIK_IKON[tip]) || BIRLIK_VARSAYILAN_IKON
}
