import { useState } from 'react'
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Loader2,
  RotateCcw,
  Send,
  ShieldAlert,
  TriangleAlert,
} from 'lucide-react'
import { karavulApi } from '../services/api'
import type { KrizRaporuYaniti, OlayDurumu } from '../types/domain'

const SIDDET_RENGI: Record<string, string> = {
  Katastrofik: 'bg-crisis-500',
  Kritik: 'bg-crisis-500',
  Yuksek: 'bg-alert-500',
}

interface FloatingPanelProps {
  aktifOlaylar: OlayDurumu[]
  onVeriDegisti: () => void
}

export default function FloatingPanel({ aktifOlaylar, onVeriDegisti }: FloatingPanelProps) {
  const [metin, setMetin] = useState('')
  const [yukleniyor, setYukleniyor] = useState(false)
  const [hata, setHata] = useState<string | null>(null)
  const [sonuc, setSonuc] = useState<KrizRaporuYaniti | null>(null)
  const [baglamAcik, setBaglamAcik] = useState(false)
  const [sifirlamaOnay, setSifirlamaOnay] = useState(false)
  const [kapatilanOlaylar, setKapatilanOlaylar] = useState<Set<string>>(new Set())

  async function olayiKapat(olayIsmi: string) {
    if (kapatilanOlaylar.has(olayIsmi)) return
    setKapatilanOlaylar((mevcut) => new Set(mevcut).add(olayIsmi))
    setHata(null)
    try {
      await karavulApi.olayiKapat(olayIsmi)
      onVeriDegisti()
    } catch (exc) {
      setHata(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setKapatilanOlaylar((mevcut) => {
        const yeni = new Set(mevcut)
        yeni.delete(olayIsmi)
        return yeni
      })
    }
  }

  async function analizEt() {
    if (!metin.trim() || yukleniyor) return
    setYukleniyor(true)
    setHata(null)
    try {
      const yanit = await karavulApi.analizEt(metin.trim())
      setSonuc(yanit)
      onVeriDegisti()
    } catch (exc) {
      setHata(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setYukleniyor(false)
    }
  }

  async function senaryoSifirla() {
    if (!sifirlamaOnay || yukleniyor) return
    setYukleniyor(true)
    setHata(null)
    try {
      await karavulApi.senaryoSifirla()
      setSonuc(null)
      setSifirlamaOnay(false)
      onVeriDegisti()
    } catch (exc) {
      setHata(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setYukleniyor(false)
    }
  }

  return (
    <div className="pointer-events-auto absolute inset-y-24 left-4 z-20 flex w-[26rem] max-w-[90vw] flex-col overflow-hidden rounded-xl border border-cmd-border/80 bg-cmd-900/65 shadow-2xl shadow-black/50 backdrop-blur-xl">
      {/* Başlık */}
      <div className="relative flex items-center gap-2 border-b border-cmd-border/70 bg-cmd-900/40 px-4 py-3">
        <span className="absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-tactical-500/80 via-tactical-500/20 to-transparent" />
        <ShieldAlert size={16} className="text-tactical-400" />
        <span className="font-display text-sm font-bold uppercase tracking-[0.2em] text-ink-100">Kriz Raporu</span>
      </div>

      {/* Giriş */}
      <div className="flex flex-col gap-2 border-b border-cmd-border/70 px-4 py-3">
        <textarea
          value={metin}
          onChange={(e) => setMetin(e.target.value)}
          placeholder="Rapor / telsiz konuşması girin…"
          rows={4}
          disabled={yukleniyor}
          className="scroll-thin resize-none rounded-md border border-cmd-border bg-cmd-950/70 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:border-tactical-500 focus:outline-none disabled:opacity-50"
        />
        <button
          onClick={analizEt}
          disabled={!metin.trim() || yukleniyor}
          className="flex items-center justify-center gap-2 rounded-md bg-tactical-500 py-2 text-sm font-semibold text-cmd-950 shadow-md shadow-tactical-500/20 transition hover:bg-tactical-400 hover:shadow-tactical-400/30 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
        >
          {yukleniyor ? <Loader2 size={16} className="animate-spin" /> : <Send size={15} />}
          {yukleniyor ? 'İşleniyor…' : 'Analiz Et'}
        </button>
      </div>

      {/* Aktif Olaylar — bireysel olay yönetimi */}
      <div className="border-b border-cmd-border/70 px-4 py-3">
        <div className="font-display mb-2 flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-ink-500">
          <span>Aktif Olaylar</span>
          <span className="rounded-full bg-cmd-700 px-1.5 py-0.5 text-crisis-400">{aktifOlaylar.length}</span>
        </div>
        {aktifOlaylar.length === 0 ? (
          <div className="py-2 text-center text-xs text-ink-500">Aktif olay yok.</div>
        ) : (
          <div className="scroll-thin flex max-h-40 flex-col gap-1.5 overflow-y-auto pr-1">
            {aktifOlaylar.map((olay) => {
              const kapatiliyor = kapatilanOlaylar.has(olay.isim)
              return (
                <div
                  key={olay.isim}
                  className="flex items-center gap-2 rounded-md border border-cmd-border/70 bg-cmd-850 px-2.5 py-1.5"
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${SIDDET_RENGI[olay.siddet ?? ''] ?? 'bg-ink-500'}`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs text-ink-100">{olay.isim}</div>
                    <div className="truncate text-[10px] text-ink-500">{olay.tip ?? '—'}</div>
                  </div>
                  <button
                    onClick={() => olayiKapat(olay.isim)}
                    disabled={kapatiliyor}
                    className="flex shrink-0 items-center gap-1 rounded-md border border-ready-500/40 px-2 py-1 text-[10px] font-semibold text-ready-400 transition hover:bg-ready-500/10 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {kapatiliyor ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle2 size={11} />}
                    Çözüldü
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Kaydırılabilir sonuç alanı */}
      <div className="scroll-thin flex-1 overflow-y-auto px-4 py-3">
        {hata && (
          <div className="mb-3 flex items-start gap-2 rounded-md border border-crisis-500/40 bg-crisis-500/10 px-3 py-2 text-xs text-crisis-400">
            <TriangleAlert size={14} className="mt-0.5 shrink-0" />
            <span>{hata}</span>
          </div>
        )}

        {sonuc && (
          <div className="flex flex-col gap-3">
            {/* Sayaç rozetleri */}
            <div className="flex flex-wrap gap-1.5 text-[11px]">
              <Rozet etiket={`+${sonuc.yazilan_varlik_sayisi} varlık`} />
              <Rozet etiket={`+${sonuc.yazilan_iliski_sayisi} ilişki`} />
              {sonuc.guncellenen_altyapi_sayisi > 0 && (
                <Rozet etiket={`${sonuc.guncellenen_altyapi_sayisi} altyapı güncellendi`} />
              )}
            </div>

            {sonuc.uyari && (
              <div className="flex items-start gap-2 rounded-md border border-alert-500/40 bg-alert-500/10 px-3 py-2 text-xs text-alert-400">
                <TriangleAlert size={14} className="mt-0.5 shrink-0" />
                <span>{sonuc.uyari}</span>
              </div>
            )}

            {sonuc.dogrulama_hatalari.length > 0 && (
              <div className="rounded-md border border-alert-500/30 bg-alert-500/5 px-3 py-2 text-xs text-alert-400">
                {sonuc.dogrulama_hatalari.map((satir, i) => (
                  <div key={i}>· {satir}</div>
                ))}
              </div>
            )}

            {/* Taktiksel öneriler */}
            {sonuc.taktiksel_oneriler.length > 0 && (
              <div className="flex flex-col gap-2">
                <div className="font-display text-[11px] font-semibold uppercase tracking-wider text-ink-500">
                  Taktiksel Öneriler
                </div>
                {sonuc.taktiksel_oneriler.map((madde, i) => (
                  <div
                    key={i}
                    className="flex gap-2.5 rounded-md border border-cmd-border/70 bg-cmd-850 px-3 py-2 text-xs leading-relaxed text-ink-100"
                  >
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-tactical-500/20 text-[10px] font-bold text-tactical-400">
                      {i + 1}
                    </span>
                    <span>{madde}</span>
                  </div>
                ))}
              </div>
            )}

            {sonuc.durum_ozeti && (
              <div>
                <button
                  onClick={() => setBaglamAcik((v) => !v)}
                  className="flex w-full items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-ink-500 hover:text-ink-300"
                >
                  GraphRAG Bağlamı
                  {baglamAcik ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                </button>
                {baglamAcik && (
                  <pre className="scroll-thin mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-cmd-border/70 bg-cmd-950/70 px-3 py-2 font-mono text-[10px] leading-relaxed text-ink-300">
                    {sonuc.durum_ozeti}
                  </pre>
                )}
              </div>
            )}
          </div>
        )}

        {!sonuc && !hata && (
          <div className="pt-6 text-center text-xs text-ink-500">Henüz işlenmiş bir rapor yok.</div>
        )}
      </div>

      {/* Senaryo sıfırlama */}
      <div className="flex items-center gap-2 border-t border-cmd-border/70 px-4 py-2.5">
        <label className="flex flex-1 items-center gap-1.5 text-[11px] text-ink-500">
          <input
            type="checkbox"
            checked={sifirlamaOnay}
            onChange={(e) => setSifirlamaOnay(e.target.checked)}
            className="accent-crisis-500"
          />
          Onaylıyorum
        </label>
        <button
          onClick={senaryoSifirla}
          disabled={!sifirlamaOnay || yukleniyor}
          className="flex items-center gap-1.5 rounded-md border border-crisis-500/40 px-2.5 py-1.5 text-[11px] font-semibold text-crisis-400 transition hover:bg-crisis-500/10 disabled:cursor-not-allowed disabled:opacity-30"
        >
          <RotateCcw size={12} />
          Senaryoyu Sıfırla
        </button>
      </div>
    </div>
  )
}

function Rozet({ etiket }: { etiket: string }) {
  return (
    <span className="rounded-full border border-ready-500/30 bg-ready-500/10 px-2 py-0.5 text-ready-400">
      {etiket}
    </span>
  )
}
