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


export function yerlesimIkonuGetir(yerlesimTipi: string | null | undefined): LucideIcon {
  return yerlesimTipi === 'Il Merkezi' ? Landmark : Home
}

export const RENK = {
  kriz: '#ef4444',
  alert: '#f59e0b',
  tactical: '#38bdf8',
  ready: '#22c55e',
  ink: '#e5e7eb',
 
  civil: '#a1a1c9',
} as const

export function facilityIkonuGetir(tip: string | null | undefined): LucideIcon {
  return (tip && FACILITY_IKON[tip]) || FACILITY_VARSAYILAN_IKON
}

export function birlikIkonuGetir(tip: string | null | undefined): LucideIcon {
  return (tip && BIRLIK_IKON[tip]) || BIRLIK_VARSAYILAN_IKON
}
