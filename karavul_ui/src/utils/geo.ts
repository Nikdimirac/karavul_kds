import type { Feature, Polygon } from 'geojson'

/**
 * Bir merkez nokta etrafında, GERÇEK dünya yarıçapı (km) ile yaklaşık bir
 * daire Polygon üretir — MapLibre'nin native `circle` katmanı yarıçapı
 * PİKSEL cinsinden aldığından (coğrafi ölçek DEĞİL), bir olayın gerçek
 * "etki alanı"nı (km) doğru göstermek için bunun yerine bir GeoJSON
 * Polygon (çokgen) üretilir — ek bir bağımlılık (turf.js) GEREKMEDEN, düz
 * trigonometri ile.
 */
export function daireOlustur(
  merkezBoylam: number,
  merkezEnlem: number,
  yaricapKm: number,
  noktaSayisi = 64,
): Feature<Polygon> {
  const koordinatlar: [number, number][] = []
  const enlemRad = (merkezEnlem * Math.PI) / 180
  const boylamKmBasi = 111.32 * Math.cos(enlemRad) || 1e-6

  for (let i = 0; i <= noktaSayisi; i++) {
    const aci = (i / noktaSayisi) * 2 * Math.PI
    const deltaEnlem = (yaricapKm / 111.32) * Math.sin(aci)
    const deltaBoylam = (yaricapKm / boylamKmBasi) * Math.cos(aci)
    koordinatlar.push([merkezBoylam + deltaBoylam, merkezEnlem + deltaEnlem])
  }

  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'Polygon', coordinates: [koordinatlar] },
  }
}
