import type { Feature, Polygon } from 'geojson'

/
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
