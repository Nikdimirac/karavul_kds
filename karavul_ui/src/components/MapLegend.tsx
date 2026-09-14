import { RENK } from '../utils/tacticalIcons'


const MADDELER: Array<{ renk: string; etiket: string; sekil?: 'nokta' | 'cizgi' }> = [
  { renk: RENK.kriz, etiket: 'Kriz Noktası' },
  { renk: RENK.tactical, etiket: 'Görevdeki Birlik' },
  { renk: RENK.ready, etiket: 'Sağlam Tesis' },
  { renk: RENK.kriz, etiket: 'Hasarlı Tesis' },
  { renk: RENK.kriz, etiket: 'Kapalı Yol', sekil: 'cizgi' },
  { renk: RENK.civil, etiket: 'Yerleşim Merkezi' },
]

export default function MapLegend() {
  return (
    <div className="pointer-events-auto absolute bottom-4 left-4 z-20 w-[26rem] max-w-[90vw] overflow-hidden rounded-lg border border-cmd-border/80 bg-cmd-900/75 shadow-lg shadow-black/40 backdrop-blur-md">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-3.5 py-2">
        {MADDELER.map(({ renk, etiket, sekil }, i) => (
          <div key={i} className="flex items-center gap-1.5 text-[10px] text-ink-300">
            {sekil === 'cizgi' ? (
              <span className="h-[2px] w-3 shrink-0 rounded-full" style={{ backgroundColor: renk, opacity: 0.7 }} />
            ) : (
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: renk, boxShadow: `0 0 4px 0 ${renk}99` }}
              />
            )}
            {etiket}
          </div>
        ))}
      </div>
    </div>
  )
}
