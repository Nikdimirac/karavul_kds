import { Layers } from 'lucide-react'

export interface KatmanGorunurlugu {
  havalimanlari: boolean
  limanlar: boolean
  askeriUsler: boolean
  hastaneler: boolean
  
  birlikler: boolean
  yollar: boolean
  yerlesimBolgeleri: boolean
}

export const VARSAYILAN_KATMAN_GORUNURLUGU: KatmanGorunurlugu = {
  havalimanlari: true,
  limanlar: true,
  askeriUsler: true,
  hastaneler: true,
  birlikler: true,
  yollar: true,
  yerlesimBolgeleri: true,
}

const SATIRLAR: Array<{ anahtar: keyof KatmanGorunurlugu; etiket: string }> = [
  { anahtar: 'hastaneler', etiket: 'Hastaneler' },
  { anahtar: 'havalimanlari', etiket: 'Havalimanları' },
  { anahtar: 'limanlar', etiket: 'Limanlar' },
  { anahtar: 'askeriUsler', etiket: 'Askeri Üsler' },
  { anahtar: 'birlikler', etiket: 'Görevdeki Birlikler' },
  { anahtar: 'yollar', etiket: 'Kapalı Yollar' },
  { anahtar: 'yerlesimBolgeleri', etiket: 'Yerleşim Bölgeleri' },
]

interface TacticalFiltersProps {
  katmanlar: KatmanGorunurlugu
  onDegistir: (anahtar: keyof KatmanGorunurlugu) => void
}

export default function TacticalFilters({ katmanlar, onDegistir }: TacticalFiltersProps) {
  return (
    <div className="pointer-events-auto absolute bottom-4 right-4 z-20 w-48 overflow-hidden rounded-lg border border-cmd-border/80 bg-cmd-900/75 shadow-lg shadow-black/40 backdrop-blur-md">
      <div className="relative flex items-center gap-2 border-b border-cmd-border/70 bg-cmd-900/40 px-3 py-2">
        <span className="absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-tactical-500/80 via-tactical-500/20 to-transparent" />
        <Layers size={13} className="text-tactical-400" />
        <span className="font-display text-[11px] font-semibold uppercase tracking-[0.15em] text-ink-100">
          Taktiksel Filtreler
        </span>
      </div>
      <div className="flex flex-col gap-1 px-3 py-2">
        {SATIRLAR.map(({ anahtar, etiket }) => (
          <label
            key={anahtar}
            className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-ink-300 hover:text-ink-100"
          >
            <input
              type="checkbox"
              checked={katmanlar[anahtar]}
              onChange={() => onDegistir(anahtar)}
              className="accent-tactical-500"
            />
            {etiket}
          </label>
        ))}
      </div>
    </div>
  )
}
