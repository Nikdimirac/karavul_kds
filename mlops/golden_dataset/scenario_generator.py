# -*- coding: utf-8 -*-
"""
mlops/golden_dataset/scenario_generator.py
============================================
KARAVUL — "ALTIN VERİ SETİ" (Golden Dataset) — MİMARİ İSKELET (Faz 1/2).

Kullanıcı talebiyle (2026-09-09) mevcut `war_gaming.py` (LLM'in kendi
kendine ürettiği, otomatik hakemle filtrelenen senaryo akışı) TERK
EDİLMİYOR ama ONA EK OLARAK, çok daha SIKI/elle-tasarlanmış bir 1000
senaryoluk "Altın Veri Seti" oluşturuluyor. Amaç iki KESİN kural:

  1. COĞRAFİ SAÇMALIK YASAK — "Karaman'da 8.5 büyüklüğünde deprem" veya
     "Konya'da liman patlaması" gibi, Türkiye'nin gerçek coğrafyasıyla
     ÇELİŞEN senaryolar dataset'e ASLA girmemeli. Bu dosyadaki
     `RISK_MATRIX`, 81 ilin GERÇEK coğrafi/sismik/jeolojik profilini
     kodlar; bir senaryo üretilmeden ÖNCE (üretici tarafı) VE üretildikten
     SONRA (bkz. `senaryo_gecerli_mi`, doğrulayıcı tarafı — proje geneli
     "üretim kuralına güvenmek YETMEZ, kod seviyesinde de doğrula"
     felsefesiyle AYNI, bkz. `decision_engine.py`daki
     `_birlik_uydurma_suphesi_mi`/`_ingilizce_supheli_mi`) bu matrise karşı
     kontrol edilir.

  2. "KDS, AKTÖR DEĞİL" YASAK — Karavul bir KARAR DESTEK SİSTEMİdir, bir
     EYLEM SİSTEMİ değil. Model ASLA "ekipleri sevk ettim", "yangını
     söndürdüm", "tahliyeyi tamamladım" gibi TAMAMLANMIŞ, BİRİNCİ TEKİL
     ŞAHIS eylem cümleleri KURAMAZ — bunlar gerçekte OLMAMIŞ olayları
     iddia eden bir HALÜSİNASYON türüdür (üstelik gerçek bir komuta
     zincirinde YETKİ GASPI anlamına gelir). Model SADECE seçenek sunar,
     komutan/yetkili İNSAN karar verir. Bkz. `ALTIN_CIKTI_SABLONU` (4
     zorunlu başlık) ve `_yasakli_eylem_kalibi_var_mi` (kod-seviyesi
     doğrulayıcı).

BU DOSYANIN KAPSAMI (Faz 1 — kullanıcı onayı BEKLENİYOR): `RISK_MATRIX`
+ geçerlilik doğrulayıcıları + Altın Çıktı şablonu/doğrulayıcısı. 1000
senaryoyu FİİLEN üretip `war_gaming.py`nin GraphRAG/Ollama boru hattından
geçirecek üretim döngüsü (Faz 2) BİLİNÇLİ OLARAK bu dosyada YOK — kullanıcı
önce mimariyi/risk matrisini onaylamak istedi (bkz. `if __name__ ==
"__main__"` altındaki öz-test, ŞİMDİLİK tek çalıştırılabilir kısım).

Kullanım (Faz 1 doğrulama):
    python -m mlops.golden_dataset.scenario_generator
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import random
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# BÖLÜM 1 — COĞRAFİ/SİSMİK GERÇEKLİK: RISK_MATRIX
# ============================================================================


class Bolge(str, Enum):
    """Klasik 7 coğrafi bölge (istatistiki NUTS bölgeleri DEĞİL — okulda
    öğretilen, halk arasında bilinen klasik ayrım; sınır illeri [ör.
    Kahramanmaraş, Çankırı] için kesin çizgi tartışmalı olabilir, ama bu
    alan SADECE bilgilendirici amaçlı — geçerlilik kararları aşağıdaki
    SOMUT bayraklara [kiyi_ili, deprem_bolgesi, vb.] dayanır, bu enum'a
    DEĞİL)."""

    MARMARA = "Marmara"
    EGE = "Ege"
    AKDENIZ = "Akdeniz"
    IC_ANADOLU = "İç Anadolu"
    KARADENIZ = "Karadeniz"
    DOGU_ANADOLU = "Doğu Anadolu"
    GUNEYDOGU_ANADOLU = "Güneydoğu Anadolu"


class RiskSeviyesi(str, Enum):
    YOK = "yok"
    DUSUK = "dusuk"
    ORTA = "orta"
    YUKSEK = "yuksek"


@dataclass(frozen=True)
class IlRiskProfili:
    """Bir ilin, senaryo üretimini/doğrulamasını YÖNLENDİREN GERÇEK
    coğrafi profili. Her alan bir sonraki bölümdeki `gecerli_olay_turleri`/
    `senaryo_gecerli_mi` tarafından OKUNUR — süs değil, KAPI."""

    bolge: Bolge
    kiyi_ili: bool
    """Gerçek deniz kıyısı var mı (Karadeniz/Marmara/Ege/Akdeniz). YOKSA
    'liman'/'sahil'/'gemi' temalı HİÇBİR senaryo bu ile ATANAMAZ — bkz.
    kullanıcının 'Konya'da liman patlaması' örneği."""
    deprem_bolgesi_afad: int
    """AFAD Türkiye Deprem Tehlike Haritası'na yakın bir yaklaşımla 1
    (en yüksek tehlike) — 5 (en düşük tehlike) arası kategori."""
    gercekci_max_deprem_mw: float
    """Bu ilde TARİHSEL/SİSMOLOJİK olarak MAKUL kabul edilebilecek üst
    büyüklük sınırı (Mw). Üretilen HİÇBİR deprem senaryosu bunu AŞAMAZ —
    kullanıcının 'Karaman'da 8.5' örneğini YAPISAL olarak imkansız kılar
    (Karaman'ın gerçekçi tavanı ~5.5, bkz. aşağıdaki matris)."""
    orman_yangini_riski: RiskSeviyesi
    sel_riski: RiskSeviyesi
    heyelan_riski: RiskSeviyesi
    cig_riski: RiskSeviyesi
    """Sadece gerçekten yüksek rakımlı/dağlık iller için YUKSEK/ORTA olmalı
    — kıyı/ova illerinde ASLA çığ senaryosu üretilemez."""
    sanayi_yogunlugu: RiskSeviyesi
    """Kimyasal depo/fabrika patlaması gibi senaryoların İNANDIRICILIĞINI
    belirler — kırsal/tarımsal bir ilde 'dev petrokimya patlaması' düşük
    olasılıklı sayılmalı (YASAK değil ama üretim ağırlığı DÜŞÜK)."""
    sinir_komsusu: Optional[str] = None
    """Kara sınırı paylaştığı ülke (yoksa None) — sınır/güvenlik/mülteci
    temalı senaryoların geçerliliği için."""


# ----------------------------------------------------------------------
# 81 İL — plaka numarası sırasıyla (1-81), TAM liste.
#
# DÜRÜSTLÜK NOTU: Aşağıdaki değerler (özellikle `gercekci_max_deprem_mw`)
# genel sismoloji bilgisi (Kuzey Anadolu Fay Hattı, Doğu Anadolu Fay Hattı,
# Şubat 2023 Kahramanmaraş depremleri bölgesi, Van 2011, Ege graben
# sistemi vb. BİLİNEN tarihsel/jeolojik gerçekler) kullanılarak MAKUL bir
# İLK YAKLAŞIM olarak hazırlandı — AFAD'ın resmi ilçe-bazlı tehlike
# haritasının BİREBİR dijital kopyası DEĞİLDİR. Kullanıcı (proje sahibi)
# bu değerleri gözden geçirip düzeltebilir; yanlış bir değer bile mevcut
# durumdan (SINIRSIZ/rastgele büyüklük) KESİNLİKLE daha güvenlidir.
# ----------------------------------------------------------------------

RISK_MATRIX: Dict[str, IlRiskProfili] = {
    # --- MARMARA ------------------------------------------------------
    "İstanbul": IlRiskProfili(Bolge.MARMARA, True, 1, 7.5, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Kocaeli": IlRiskProfili(Bolge.MARMARA, True, 1, 7.6, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Sakarya": IlRiskProfili(Bolge.MARMARA, False, 1, 7.4, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Yalova": IlRiskProfili(Bolge.MARMARA, True, 1, 7.4, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Bursa": IlRiskProfili(Bolge.MARMARA, True, 1, 7.3, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YUKSEK),
    "Balıkesir": IlRiskProfili(Bolge.MARMARA, True, 2, 6.8, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Çanakkale": IlRiskProfili(Bolge.MARMARA, True, 1, 7.0, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Tekirdağ": IlRiskProfili(Bolge.MARMARA, True, 1, 7.4, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Edirne": IlRiskProfili(Bolge.MARMARA, False, 3, 6.0, RiskSeviyesi.DUSUK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, sinir_komsusu="Yunanistan/Bulgaristan"),
    "Kırklareli": IlRiskProfili(Bolge.MARMARA, False, 3, 6.0, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, sinir_komsusu="Bulgaristan"),
    "Bilecik": IlRiskProfili(Bolge.MARMARA, False, 2, 6.8, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    # --- EGE ------------------------------------------------------------
    "İzmir": IlRiskProfili(Bolge.EGE, True, 1, 7.0, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Manisa": IlRiskProfili(Bolge.EGE, False, 2, 6.8, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Aydın": IlRiskProfili(Bolge.EGE, True, 1, 6.9, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Muğla": IlRiskProfili(Bolge.EGE, True, 1, 6.8, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Denizli": IlRiskProfili(Bolge.EGE, False, 1, 6.8, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Uşak": IlRiskProfili(Bolge.EGE, False, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Kütahya": IlRiskProfili(Bolge.EGE, False, 2, 6.5, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Afyonkarahisar": IlRiskProfili(Bolge.EGE, False, 2, 6.5, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    # --- AKDENİZ --------------------------------------------------------
    "Antalya": IlRiskProfili(Bolge.AKDENIZ, True, 2, 6.5, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Isparta": IlRiskProfili(Bolge.AKDENIZ, False, 2, 6.6, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Burdur": IlRiskProfili(Bolge.AKDENIZ, False, 1, 6.8, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Mersin": IlRiskProfili(Bolge.AKDENIZ, True, 2, 6.7, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Adana": IlRiskProfili(Bolge.AKDENIZ, True, 1, 7.2, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Osmaniye": IlRiskProfili(Bolge.AKDENIZ, False, 1, 7.6, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Hatay": IlRiskProfili(Bolge.AKDENIZ, True, 1, 7.8, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.ORTA, sinir_komsusu="Suriye"),
    "Kahramanmaraş": IlRiskProfili(Bolge.AKDENIZ, False, 1, 7.8, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA),
    # --- İÇ ANADOLU -------------------------------------------------------
    "Ankara": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 6.0, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Konya": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 6.0, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Kayseri": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 6.2, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YUKSEK),
    "Sivas": IlRiskProfili(Bolge.IC_ANADOLU, False, 2, 6.6, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Yozgat": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 6.0, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Kırıkkale": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 5.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Kırşehir": IlRiskProfili(Bolge.IC_ANADOLU, False, 4, 5.5, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Nevşehir": IlRiskProfili(Bolge.IC_ANADOLU, False, 4, 5.5, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Niğde": IlRiskProfili(Bolge.IC_ANADOLU, False, 3, 5.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Aksaray": IlRiskProfili(Bolge.IC_ANADOLU, False, 4, 5.5, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Karaman": IlRiskProfili(Bolge.IC_ANADOLU, False, 4, 5.5, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Çankırı": IlRiskProfili(Bolge.IC_ANADOLU, False, 2, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Eskişehir": IlRiskProfili(Bolge.IC_ANADOLU, False, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Çorum": IlRiskProfili(Bolge.IC_ANADOLU, False, 2, 6.8, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Amasya": IlRiskProfili(Bolge.IC_ANADOLU, False, 1, 7.0, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Tokat": IlRiskProfili(Bolge.IC_ANADOLU, False, 1, 7.0, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    # --- KARADENİZ --------------------------------------------------------
    "Zonguldak": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK),
    "Bartın": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Karabük": IlRiskProfili(Bolge.KARADENIZ, False, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.YUKSEK),
    "Kastamonu": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Sinop": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Samsun": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.6, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA),
    "Ordu": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK),
    "Giresun": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Trabzon": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA),
    "Rize": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Artvin": IlRiskProfili(Bolge.KARADENIZ, True, 2, 6.5, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="Gürcistan"),
    "Gümüşhane": IlRiskProfili(Bolge.KARADENIZ, False, 2, 6.3, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Bayburt": IlRiskProfili(Bolge.KARADENIZ, False, 2, 6.3, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Düzce": IlRiskProfili(Bolge.KARADENIZ, False, 1, 7.4, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA),
    "Bolu": IlRiskProfili(Bolge.KARADENIZ, False, 1, 7.4, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    # --- DOĞU ANADOLU -------------------------------------------------
    "Erzurum": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Erzincan": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.8, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Kars": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 2, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="Ermenistan"),
    "Ardahan": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 2, 6.5, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="Gürcistan/Ermenistan"),
    "Iğdır": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="Ermenistan/Azerbaycan(Nahçıvan)/İran"),
    "Ağrı": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="İran"),
    "Van": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="İran"),
    "Muş": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.0, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Bitlis": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Bingöl": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.4, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK),
    "Tunceli": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.2, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Elazığ": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.4, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA),
    "Malatya": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 7.8, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA),
    "Hakkari": IlRiskProfili(Bolge.DOGU_ANADOLU, False, 1, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YUKSEK, RiskSeviyesi.YUKSEK, RiskSeviyesi.DUSUK, sinir_komsusu="Irak/İran"),
    # --- GÜNEYDOĞU ANADOLU ------------------------------------------------
    "Gaziantep": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 1, 7.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.YUKSEK, sinir_komsusu="Suriye"),
    "Adıyaman": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 1, 7.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK),
    "Şanlıurfa": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 2, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA, sinir_komsusu="Suriye"),
    "Diyarbakır": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 2, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Mardin": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 2, 6.5, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, sinir_komsusu="Suriye"),
    "Batman": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 2, 6.5, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.YOK, RiskSeviyesi.ORTA),
    "Siirt": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 1, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.ORTA, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK),
    "Şırnak": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 1, 6.8, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YUKSEK, RiskSeviyesi.ORTA, RiskSeviyesi.DUSUK, sinir_komsusu="Irak/Suriye"),
    "Kilis": IlRiskProfili(Bolge.GUNEYDOGU_ANADOLU, False, 1, 7.6, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.DUSUK, RiskSeviyesi.YOK, RiskSeviyesi.DUSUK, sinir_komsusu="Suriye"),
}

assert len(RISK_MATRIX) == 81, f"RISK_MATRIX 81 il icermeli, {len(RISK_MATRIX)} bulundu — eksik/fazla il var."


# ============================================================================
# BÖLÜM 2 — OLAY TÜRLERİ + COĞRAFİ GEÇERLİLİK DOĞRULAYICISI
# ============================================================================


class OlayTuru(str, Enum):
    DEPREM = "Deprem"
    SEL = "Sel"
    ORMAN_YANGINI = "Orman Yangini"
    HEYELAN = "Heyelan"
    CIG = "Cig"
    LIMAN_YANGINI = "Liman Yangini"
    GEMI_KAZASI = "Gemi Kazasi"
    KIMYASAL_PATLAMA = "Kimyasal Patlama"
    ENDUSTRIYEL_YANGIN = "Endustriyel Yangin"
    ZINCIRLEME_KAZA = "Zincirleme Kaza"
    HASTANE_KRIZI = "Hastane Krizi"
    SINIR_GUVENLIK_OLAYI = "Sinir Guvenlik Olayi"
    SIBER_SALDIRI = "Siber Saldiri"
    SALDIRI = "Saldiri"


# `_gerekli_bayrak` — bir olay türünün GEÇERLİ sayılması için ilin hangi
# `IlRiskProfili` alanının YOK/DUSUK olmaması (ya da özel bir alanın dolu
# olması) gerektiğini tanımlar. `None` = coğrafyadan bağımsız, HER ilde
# üretilebilir (ör. zincirleme kaza — her ilde karayolu var; siber saldırı
# — fiziksel coğrafyaya bağlı değil).
_KIYI_GEREKTIREN_OLAYLAR = {OlayTuru.LIMAN_YANGINI, OlayTuru.GEMI_KAZASI}
_SINIR_GEREKTIREN_OLAYLAR = {OlayTuru.SINIR_GUVENLIK_OLAYI}
_COGRAFYADAN_BAGIMSIZ_OLAYLAR = {
    OlayTuru.ZINCIRLEME_KAZA, OlayTuru.HASTANE_KRIZI, OlayTuru.SIBER_SALDIRI,
    OlayTuru.KIMYASAL_PATLAMA, OlayTuru.ENDUSTRIYEL_YANGIN, OlayTuru.SALDIRI,
}
_RISK_ALANI_GEREKTIREN_OLAYLAR: Dict[OlayTuru, str] = {
    OlayTuru.ORMAN_YANGINI: "orman_yangini_riski",
    OlayTuru.SEL: "sel_riski",
    OlayTuru.HEYELAN: "heyelan_riski",
    OlayTuru.CIG: "cig_riski",
}


def gecerli_olay_turleri(il: str) -> List[OlayTuru]:
    """Bir il için COĞRAFİ OLARAK MAKUL olay türlerinin listesini döner.
    `RISK_MATRIX`de olmayan bir il için `KeyError` fırlatır (sessizce
    boş liste dönüp senaryo üretiminin sessizce hiçbir şey üretmemesi
    YERİNE, üretim betiği hatayı AÇIKÇA görsün)."""
    profil = RISK_MATRIX[il]  # KeyError bilinçli — bkz. docstring.
    sonuc: List[OlayTuru] = [OlayTuru.DEPREM]  # deprem HER ilde olabilir (büyüklük tavanı ayrı kontrol edilir).
    sonuc.extend(_COGRAFYADAN_BAGIMSIZ_OLAYLAR)
    if profil.kiyi_ili:
        sonuc.extend(_KIYI_GEREKTIREN_OLAYLAR)
    if profil.sinir_komsusu:
        sonuc.extend(_SINIR_GEREKTIREN_OLAYLAR)
    for olay, alan_adi in _RISK_ALANI_GEREKTIREN_OLAYLAR.items():
        seviye = getattr(profil, alan_adi)
        if seviye != RiskSeviyesi.YOK:
            sonuc.append(olay)
    return sonuc


def senaryo_gecerli_mi(
    il: str, olay_turu: OlayTuru, deprem_buyuklugu_mw: Optional[float] = None
) -> Tuple[bool, str]:
    """`(gecerli_mi, sebep)` döner. Senaryo üretiminden SONRA bile (LLM'in
    kendi başına bir şey uydurması ihtimaline karşı — bkz. proje geneli
    "üretim kuralına güvenme, kod seviyesinde doğrula" ilkesi) HER üretilen
    örnek bu fonksiyondan geçirilmeli, geçemeyen örnek dataset'e YAZILMAZ."""
    if il not in RISK_MATRIX:
        return False, f"'{il}' RISK_MATRIX'te tanımlı değil (81 il dışında bir isim mi?)."

    profil = RISK_MATRIX[il]
    gecerli_turler = gecerli_olay_turleri(il)
    if olay_turu not in gecerli_turler:
        return False, (
            f"'{olay_turu.value}', '{il}' ilinin coğrafi profiliyle UYUŞMUYOR "
            f"(ör. kıyısı yok → liman/gemi olayı geçersiz, dağlık değil → çığ geçersiz)."
        )

    if olay_turu == OlayTuru.DEPREM and deprem_buyuklugu_mw is not None:
        if deprem_buyuklugu_mw > profil.gercekci_max_deprem_mw:
            return False, (
                f"'{il}' için Mw {deprem_buyuklugu_mw} GERÇEKÇİ DEĞİL — bu ilin "
                f"makul üst sınırı Mw {profil.gercekci_max_deprem_mw} (bkz. RISK_MATRIX). "
                "Kullanıcının 'Karaman'da 8.5' örneği TAM OLARAK bu kontrolün önlemesi "
                "gereken hata sınıfıdır."
            )
        if deprem_buyuklugu_mw < 3.0:
            return False, "Mw 3.0 altı depremler genelde hissedilmez/krize yol açmaz, senaryo olarak anlamsız."

    return True, "geçerli"


# ============================================================================
# BÖLÜM 3 — "KDS, AKTÖR DEĞİL": ALTIN ÇIKTI ŞABLONU
# ============================================================================

ALTIN_CIKTI_BASLIKLARI: Tuple[str, str, str, str] = (
    "DURUM SENTEZİ",
    "HAREKET TARZLARI (ALPHA/BRAVO SEÇENEKLERİ)",
    "KRİTİK DARBOĞAZLAR",
    "KARAR/ONAY NOKTASI",
)


# ----------------------------------------------------------------------
# EŞ ANLAMLI İFADE HAVUZU ("PAPAĞAN DÖNGÜSÜ" ÖNLEMİ)
# ----------------------------------------------------------------------
# SORUN (pilot testte YAKALANDI, bkz. oturum notları): aynı "gerçek veri
# şekli" (ör. "birlik bulunamadı") tekrar tekrar AYNI birebir cümleyle
# üretilirse, 1000'lik Altın Veri Setinin BÜYÜK bir kısmı KELİMESİ
# KELİMESİNE aynı kalıpları taşır — bu, projenin DAHA ÖNCE yaşadığı
# "papağan döngüsü" (bkz. AI_MEMORY.md madde 9 — model veri farklı
# seçenek sunsa bile hep AYNI cümleyi ezberleyip tekrarlaması) hatasıyla
# AYNI KÖKTEN bir risktir; sadece kaynağı DEĞİŞİR (LLM'in kendi ürettiği
# tekrar DEĞİL, Altın Veri Setinin KENDİSİNİN homojen olması).
#
# ÇÖZÜM: her tekrar eden "cümle kalıbı", TEK bir sabit metin DEĞİL, birden
# çok eş anlamlı VARYANT içeren bir liste olarak tanımlanır; `secim()`
# HER çağrıda `random.choice` ile bunlardan birini seçer. Aynı ANLAMI
# taşırlar (gerçek veriyi ÇARPITMAZLAR) — SADECE yüzey biçimi (cümle
# yapısı/kelime seçimi) değişir.
_IFADE_HAVUZU: Dict[str, List[str]] = {
    "alpha_bulunamadi": [
        "Bilgi Grafında bu krize doğru yeteneğe sahip ve açık yol ağı üzerinden ulaşabilen bir birlik BULUNAMADI.",
        "Mevcut Bilgi Grafı taramasında, kriz noktasına açık güzergahtan ulaşabilecek uygun nitelikte bir birlik TESPİT EDİLEMEDİ.",
        "Sorgulanan veri tabanında bu krize yönelik, doğru yetenek ve açık rota koşulunu sağlayan bir birlik BULUNMAMAKTADIR.",
        "Kriz noktasına, gerekli yeteneğe sahip ve ulaşılabilir durumda bir birlik şu an için SAPTANAMADI.",
        "Bilgi Grafı taraması, bu krize uygun ve erişilebilir bir müdahale birliği DÖNDÜRMEDİ.",
    ],
    "alpha_bulunamadi_risk": [
        "Çevre illerden destek talebi değerlendirilmeli — bu net bir erişim sorunudur.",
        "Bu durum açık bir erişim/kapasite sorunudur; dış destek seçenekleri gözden geçirilmelidir.",
        "Mevcut veri, yerel kapasitenin yetersiz kaldığını göstermektedir — harici destek gerekebilir.",
        "Yerel envanterde uygun birlik yokluğu net bir erişim açığıdır; il dışı takviye değerlendirilmelidir.",
    ],
    "alpha_bulundu": [
        "{isim} ({tip}) biriminin kriz noktasına sevki önerilir.",
        "{isim} ({tip}) biriminin kriz bölgesine yönlendirilmesi bir seçenek olarak sunulur.",
        "Kriz noktasına en yakın uygun birlik olan {isim} ({tip})'in görevlendirilmesi önerilir.",
        "{isim} ({tip}) birimi, mevcut veriye göre bu krize müdahale için en uygun adaydır.",
        "{isim} ({tip}) biriminin bölgeye intikali değerlendirilebilecek seçeneklerden biridir.",
    ],
    "alpha_bulundu_risk": [
        "Kriz noktasına ~{mesafe} km, {personel} personel — ek yük altında kapasite sınırlı olabilir.",
        "{mesafe} km mesafe ve {personel} personel ile sınırlı bir kapasiteye sahip; ilave ihtiyaç doğabilir.",
        "Yaklaşık {mesafe} km uzaklıkta, {personel} personelle görev yapabilir — kapasite aşımı riski göz önünde bulundurulmalı.",
        "{personel} personelli bu birim ~{mesafe} km mesafede; eşzamanlı ikinci bir olayda kapasitesi zorlanabilir.",
    ],
    "bravo_komsu_il": [
        "Komşu il(ler)den takviye/destek talebi seçeneği değerlendirilmelidir.",
        "Çevre illerdeki müsait birliklerden ek takviye talep edilmesi önerilir.",
        "Bölge dışından (komşu il AFAD/İtfaiye kaynaklarından) destek talebi bir seçenek olarak sunulabilir.",
        "Yakın illerin müdahale kapasitesinden yararlanmak üzere resmi destek talebinde bulunulması değerlendirilebilir.",
        "İl dışı takviye kanalları (komşu il koordinasyonu) üzerinden ek kaynak istenmesi önerilir.",
    ],
    "bravo_komsu_il_risk": [
        "Daha uzun intikal süresi beklenir.",
        "İl dışı takviyenin intikal süresi, yerel bir birime göre belirgin şekilde uzun olacaktır.",
        "Bu seçenek zaman kaybı pahasına ek kapasite sağlar — ilk müdahale penceresini aşabilir.",
    ],
    "bravo_2plan": [
        "{isim} ({tip}) birimi ÇEVRE GÜVENLİĞİ/TAHLİYE DESTEĞİ olarak değerlendirilebilir (birincil müdahale ROLÜNDE DEĞİL).",
        "{isim} ({tip}), birincil müdahale kapasitesinde OLMAMAKLA BİRLİKTE, çevre güvenliği/tahliye desteği için değerlendirilebilir.",
        "{isim} ({tip}) birimi SADECE çevre güvenliği/tahliye desteği rolünde bir seçenek olarak sunulur; birincil çözüm ÖNERİLMEZ.",
        "{isim} ({tip}), müdahale yeteneği açısından birincil seçenek OLMASA da, çevre güvenliği/tahliye desteği sağlayabilir.",
    ],
    "bravo_2plan_risk": [
        "Kriz noktasına ~{mesafe} km — bu birim müdahale YETENEĞİ için değil, çevre güvenliği/tahliye desteği için uygundur.",
        "~{mesafe} km mesafede; rolü SINIRLI (güvenlik/tahliye desteği), birincil müdahale KAPSAMI DIŞINDA tutulmalıdır.",
        "{mesafe} km uzaklıktaki bu birim, YALNIZCA destek/güvenlik işlevi için değerlendirilmelidir.",
    ],
    "darbogaz_kapali_yol": [
        "{yol} KAPALI — birincil güzergahlardan biri kullanılamıyor.",
        "{yol} trafiğe kapalı durumda; bu güzergah üzerinden erişim mümkün DEĞİL.",
        "{yol} geçişe müsait değil, alternatif bir güzergah gerekiyor.",
        "{yol} kapalı olduğundan, bu hat üzerinden intikal PLANLANAMAZ.",
    ],
    "darbogaz_alternatif": [
        "Açık alternatif güzergahlar tespit edildi: {liste}",
        "Aşağıdaki güzergahlar hâlâ açık ve kullanılabilir durumda: {liste}",
        "Alternatif olarak değerlendirilebilecek açık yollar: {liste}",
    ],
    "darbogaz_yok": [
        "Bilgi Grafında bu krize özgü, ilave bir erişim/darboğaz kaydı tespit edilmedi.",
        "Mevcut veri tabanında bu krize özel ek bir erişim sorunu KAYITLI DEĞİL.",
        "Bu kriz için ayrıca bir güzergah/erişim kısıtı TESPİT EDİLEMEDİ.",
    ],
    "karar_sorusu": [
        "Yetkili komutan/kriz masası: ALPHA ile BRAVO seçeneklerinden hangisi ONAYLANSIN, yoksa ikisi PARALEL mi yürütülsün? Onay/ret kararı ve gerekirse ek kaynak talebi bekleniyor.",
        "Karar mercii: ALPHA mı, BRAVO mu, yoksa HER İKİSİ birlikte mi uygulansın? Nihai onay ve gerekiyorsa ilave kaynak talimatı bekleniyor.",
        "Kriz masasının değerlendirmesine sunulur: ALPHA/BRAVO seçeneklerinden hangisinin ONAYLANACAĞI, ya da paralel yürütülüp yürütülmeyeceği yetkili tarafından belirlenmelidir.",
        "Onay makamı: ALPHA ile BRAVO arasında tercih ya da eş zamanlı uygulama kararı bekleniyor; ek kaynak ihtiyacı varsa şimdi belirtilmelidir.",
    ],
}


def secim(havuz_adi: str, **bicim_degiskenleri: Any) -> str:
    """`_IFADE_HAVUZU[havuz_adi]`den RASTGELE bir eş anlamlı varyant seçer
    ve `bicim_degiskenleri` ile (varsa `{isim}`/`{tip}`/`{mesafe}` gibi
    yer tutucular) doldurur. `havuz_adi` yoksa `KeyError` (sessizce boş
    döndürmez — bir yazım hatası SESSİZCE 'anlamsız boş metin' üretmesin)."""
    varyant = random.choice(_IFADE_HAVUZU[havuz_adi])
    return varyant.format(**bicim_degiskenleri) if bicim_degiskenleri else varyant


# Model/altın örnek ASLA bu kalıplardaki gibi TAMAMLANMIŞ, birinci tekil/çoğul
# şahıs EYLEM iddiasında bulunamaz — KDS sadece seçenek sunar, YETKİLİ İNSAN
# karar verip eylemi BAŞKA bir sistem/birim üzerinden GERÇEKLEŞTİRİR. Bu liste
# hem (a) altın örnekleri YAZARKEN uyulacak KURAL hem de (b) üretilmiş HER
# metnin geçip geçmediğini kontrol eden kod-seviyesi DOĞRULAYICI olarak
# kullanılır (bkz. `_yasakli_eylem_kalibi_var_mi`) — proje geneli
# "prompt kuralına güvenmek YETMEZ" felsefesiyle TUTARLI.
_YASAKLI_EYLEM_KALIPLARI: List[str] = [
    r"\b(ekib(i|ini|ler[ıi])|birli(k|ği|ğini)|birim(i|ini)?)\s+(sevk\s+ett[ıi]m|g[öo]nderd[ıi]m|y[öo]nlendird[ıi]m)\b",
    r"\byang[ıi]n[ıi]?\s+s[öo]nd[üu]rd[üu]m\b",
    r"\btahliyeyi?\s+tamamlad[ıi]m\b",
    r"\bkriz[ıi]?\s+(çöz[düdü]m|halletti?m|sonland[ıi]rd[ıi]m)\b",
    r"\bkarar\s+verdi[mk]\b",
    r"\bonaylad[ıi]m\b",
    r"\bem[iı]r\s+verdi[mk]\b",
    r"\b(müdahale|operasyon)\s+(ba[şs]latt[ıi]m|ger[çc]ekle[şs]tird[ıi]m)\b",
    r"\bulaşt[ıi]rd[ıi]m\b",
    r"\bkurtard[ıi]m\b",
]
_YASAKLI_EYLEM_REGEX = re.compile("|".join(_YASAKLI_EYLEM_KALIPLARI), re.IGNORECASE)


def _yasakli_eylem_kalibi_var_mi(metin: str) -> Optional[str]:
    """Metinde YASAKLI bir 'tamamlanmış eylem' iddiası varsa EŞLEŞEN kalıbı
    döner (yoksa `None`) — `None` DÖNMEDIKÇE bu metin Altın Veri Setine
    ASLA yazılmamalı."""
    eslesme = _YASAKLI_EYLEM_REGEX.search(metin)
    return eslesme.group(0) if eslesme else None


@dataclass
class HareketTarziSecenegi:
    """Bir Hareket Tarzı (Course of Action) — Alpha ya da Bravo. Kasıtlı
    olarak 'yapıldı' değil 'yapılabilir/önerilir' kipinde alanlar taşır."""

    kod_adi: str  # "ALPHA" | "BRAVO"
    ozet: str  # tek cümlelik seçenek özeti (ör. "En yakın İtfaiye biriminin 11km üzerinden sevki")
    kaynaklar: List[str]  # bu seçenek için önerilen birim/kaynak isimleri
    tahmini_sure_dk: Optional[int]
    riskler: List[str]  # bu seçeneğin göze aldığı riskler/varsayımlar


def altin_cikti_uret(
    durum_sentezi: str,
    alpha: HareketTarziSecenegi,
    bravo: HareketTarziSecenegi,
    darbogazlar: List[str],
    karar_sorusu: str,
) -> str:
    """4 zorunlu başlığı, KDS'nin 'aktör değil danışman' kimliğine UYGUN
    dille üretir. Çağıran taraf `_yasakli_eylem_kalibi_var_mi(sonuc) is
    None` olduğunu MUTLAKA doğrulamalı (bu fonksiyon kendi ürettiği metni
    de öz-doğrular, bkz. fonksiyon sonu — çıktı YASAKLI bir kalıp içeriyorsa
    `ValueError` fırlatır, SESSİZCE geçmez)."""

    def _secenek_blogu(secenek: HareketTarziSecenegi) -> str:
        satirlar = [
            f"- **{secenek.kod_adi} Seçeneği:** {secenek.ozet}",
            f"  - Önerilen kaynaklar: {', '.join(secenek.kaynaklar) if secenek.kaynaklar else 'belirtilmemiş'}",
        ]
        if secenek.tahmini_sure_dk is not None:
            satirlar.append(f"  - Tahmini intikal süresi: ~{secenek.tahmini_sure_dk} dakika")
        if secenek.riskler:
            satirlar.append(f"  - Göz önünde bulundurulması gereken riskler: {', '.join(secenek.riskler)}")
        return "\n".join(satirlar)

    metin = (
        f"## {ALTIN_CIKTI_BASLIKLARI[0]}\n{durum_sentezi.strip()}\n\n"
        f"## {ALTIN_CIKTI_BASLIKLARI[1]}\n{_secenek_blogu(alpha)}\n{_secenek_blogu(bravo)}\n\n"
        f"## {ALTIN_CIKTI_BASLIKLARI[2]}\n"
        + "\n".join(f"- {d}" for d in darbogazlar) + "\n\n"
        f"## {ALTIN_CIKTI_BASLIKLARI[3]}\n{karar_sorusu.strip()}\n"
    )

    ihlal = _yasakli_eylem_kalibi_var_mi(metin)
    if ihlal:
        raise ValueError(
            f"altin_cikti_uret KENDİ ürettiği metinde yasaklı eylem kalıbı buldu: '{ihlal}'. "
            "Bu, şablonun/girdi metinlerinin (durum_sentezi, ozet, darbogazlar, karar_sorusu) "
            "'yaptım/ettim' dili İÇERDİĞİ anlamına gelir — girdi metinlerini düzelt."
        )
    return metin


if __name__ == "__main__":
    # --- Faz 1 öz-test: RISK_MATRIX + doğrulayıcılar + şablon örneği. ---
    print(f"RISK_MATRIX: {len(RISK_MATRIX)} il yüklü.\n")

    # Kullanıcının verdiği İKİ SOMUT örnek KESİNLİKLE reddedilmeli:
    for il, olay, mw in [("Karaman", OlayTuru.DEPREM, 8.5), ("Konya", OlayTuru.LIMAN_YANGINI, None)]:
        gecerli, sebep = senaryo_gecerli_mi(il, olay, mw)
        durum = "✅ REDDEDİLDİ (doğru)" if not gecerli else "❌ KABUL EDİLDİ (YANLIŞ!)"
        print(f"[{durum}] {il} / {olay.value} / Mw={mw} → {sebep}")

    # Karşı-örnek: gerçekten geçerli bir senaryo kabul EDİLMELİ.
    gecerli, sebep = senaryo_gecerli_mi("Kahramanmaraş", OlayTuru.DEPREM, 7.6)
    print(f"\n[{'✅ KABUL (doğru)' if gecerli else '❌ RED (YANLIŞ!)'}] Kahramanmaraş / Deprem / Mw=7.6 → {sebep}")
    gecerli, sebep = senaryo_gecerli_mi("İzmir", OlayTuru.LIMAN_YANGINI, None)
    print(f"[{'✅ KABUL (doğru)' if gecerli else '❌ RED (YANLIŞ!)'}] İzmir / Liman Yangını → {sebep}")

    print("\nÖrnek il için geçerli olay türleri:")
    for il in ("Karaman", "Konya", "Rize", "Hatay"):
        print(f"  {il}: {[t.value for t in gecerli_olay_turleri(il)]}")

    # --- Altın Çıktı şablonu örneği ---
    print("\n" + "=" * 78)
    ornek = altin_cikti_uret(
        durum_sentezi=(
            "Hatay Samandağ'da 6.8 büyüklüğünde deprem meydana geldi. "
            "Samandağ-Antakya karayolu heyelan nedeniyle trafiğe kapalı, "
            "bölgede enkaz altında vatandaş bulunduğu bildiriliyor."
        ),
        alpha=HareketTarziSecenegi(
            kod_adi="ALPHA",
            ozet="Antakya Arama Kurtarma Timi'nin alternatif D-420 güzergahı üzerinden sevki önerilir.",
            kaynaklar=["Antakya AFAD Arama Kurtarma Timi"],
            tahmini_sure_dk=35,
            riskler=["Alternatif güzergahın tonaj kapasitesi doğrulanmamış"],
        ),
        bravo=HareketTarziSecenegi(
            kod_adi="BRAVO",
            ozet="Komşu il (Adana) üzerinden takviye ekip talebi önerilir.",
            kaynaklar=["Adana AFAD İl Müdürlüğü"],
            tahmini_sure_dk=90,
            riskler=["Daha uzun intikal süresi, ilk 24 saat kritik penceresini aşabilir"],
        ),
        darbogazlar=[
            "Samandağ-Antakya karayolu KAPALI (heyelan) — birincil güzergah kullanılamıyor.",
            "Bölgedeki tek AFAD timi başka bir noktada (Samandağ merkez) meşgul olabilir.",
        ],
        karar_sorusu=(
            "Yetkili komutan/kriz masası: ALPHA (hızlı ama doğrulanmamış güzergah) ile "
            "BRAVO (yavaş ama güvenli takviye) arasında hangisi ONAYLANSIN, yoksa HER İKİSİ "
            "PARALEL mi başlatılsın?"
        ),
    )
    print(ornek)
    ihlal = _yasakli_eylem_kalibi_var_mi(ornek)
    print(f"\nYasaklı eylem kalıbı kontrolü: {'❌ BULUNDU: ' + ihlal if ihlal else '✅ temiz'}")
