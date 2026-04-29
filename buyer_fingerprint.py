"""
Facebook Ads Campaign Name (4sub) Buyer Fingerprint Classifier
==============================================================

Определяет "почерк" медиабаера по структуре названия рекламной кампании (4sub).

Агентства (селлеры): DINH, FullMedia, Luna, AdsAgency, PalmAgency,
TrustMind, FdAgency, SHD, DAP — используются многими баерами.
Классификатор группирует по СТРУКТУРНОМУ шаблону, а не по агентству.

Использование:
    from buyer_fingerprint import classify, classify_batch, detect_agency

    result = classify("DE ADSA 17 1572850050691788 TAG CTLPLM1 PXPLM1")
    # -> {'cluster': 'A3_TAG_CTLPLM', 'group': 'delimiter', 'confidence': 'high', 'agency': 'AdsAgency'}

    results = classify_batch(["item1", "item2", ...])
    # -> [{'item': ..., 'cluster': ..., ...}, ...]
"""

import re
from typing import Optional, Dict, List

__version__ = "3.0.0"

# =============================================================================
# AGENCY DETECTION (informational layer, not classification key)
# =============================================================================

AGENCY_PATTERNS = {
    "Dinhdoh":     [r'\bDH_', r'\bDinh_', r'\bDD[-_]', r'_Dinh_', r'^DH_'],
    "FullMedia":   [r'\bFULL\d', r'\bFull_', r'\bFM_', r'\bFMN', r'\bKH_', r'^FULL_'],
    "TrustMind":   [r'\bTRM', r'\btrm', r'\bTM_', r'\bTMM', r'\bTrust', r'^tm\d'],
    "AdsAgency":   [r'AdsAgency', r'\bADS\s+\d', r'\bAA_', r'\bAAA_'],
    "PalmAgency":  [r'\bPLM\d', r'\bPALMA', r'\bPalm', r'\bPALM\b'],
    "FdAgency":    [r'\bFD_', r'\bFDA_', r'^F/', r'^F1/'],
    "SHD":         [r'\bSHD', r'/shd', r'\bSD_'],
    "DAP":         [r'\bDP_', r'\bDAP_'],
}


def detect_agency(campaign_name: str) -> Optional[str]:
    """Определяет агентство (селлер) по названию кампании."""
    for agency, patterns in AGENCY_PATTERNS.items():
        for p in patterns:
            if re.search(p, campaign_name):
                return agency
    return None


# =============================================================================
# CLASSIFICATION RULES
# =============================================================================
# Порядок правил КРИТИЧЕН: более специфичные правила идут ПЕРЕД общими.
# Каждое правило — (cluster_id, group, confidence, test_func)
#
# Groups:
#   delimiter  — определяется по стилю разделителя
#   token      — определяется по уникальному токену баера
#   geo_struct — определяется по GEO + структуре
#   agency     — определяется по агентству + структуре
#   numeric    — определяется по числовому формату
#   small      — мелкие / новые кластеры
#   meta       — FBID, шаблоны, unknown
# =============================================================================

def _build_rules():
    """Строит упорядоченный список правил классификации."""
    
    rules = []
    
    def R(cluster_id, group, confidence, test_func, desc=""):
        rules.append({
            'id': cluster_id,
            'group': group,
            'confidence': confidence,
            'test': test_func,
            'desc': desc,
        })
    
    # -----------------------------------------------------------------
    # TIER 0: LITERAL / TRIVIAL (must come first)
    # -----------------------------------------------------------------
    R('META_unknown', 'meta', 'low',
      lambda i: i.strip().lower() == 'unknown',
      'Literal "unknown"')
    
    R('META_template', 'meta', 'low',
      lambda i: '{{' in i,
      '{{campaign.name}} шаблон')
    
    R('META_default', 'meta', 'low',
      lambda i: bool(re.search(r'^(New Sales Campaign|Новая кампания|Презик|Продажи)', i)),
      'Дефолтное имя кампании')
    
    # -----------------------------------------------------------------
    # TIER 1: HIGH-SPECIFICITY TOKEN RULES
    # -----------------------------------------------------------------
    R('A3_TAG_CTLPLM', 'delimiter', 'high',
      lambda i: bool(re.search(r'TAG\s*CTLPLM', i)) or bool(re.search(r'^DE\+ADSA.*TAG', i)),
      'GEO [ADSA|HU|OAC|PALM] NNN FBID TAG CTLPLM1 PXPLM1')
    
    R('B2_QWER', 'token', 'high',
      lambda i: 'QWER' in i,
      'QWER / NN / L0L / NNN')
    
    R('B5_OpssKe', 'token', 'high',
      lambda i: 'OpssKe' in i,
      'OpssKe/ NNN / Q / N')
    
    R('B9_ICWeidel', 'token', 'high',
      lambda i: bool(re.search(r'IC(Weidel|Merz)V|SLWeidelV', i)),
      'FBID_ICWeidelV4 / SLWeidelV8')
    
    R('B14_Mond', 'token', 'high',
      lambda i: bool(re.search(r'Mond\d', i)),
      'KapiteledixMond07.04')
    
    R('B13_MG_British', 'token', 'high',
      lambda i: bool(re.search(r'BritishSteady|MG-id\d+.*Split|MG[-_]PR\w*_\d|QuantumAI_FM', i)),
      '1/1-MG-id...-BritishSteadyFlow')
    
    # -----------------------------------------------------------------
    # TIER 2: DELIMITER-BASED (structural fingerprint)
    # -----------------------------------------------------------------
    
    # A1: +-+ 35/30 Dep — САМЫЙ СЛОЖНЫЙ паттерн, много вариантов
    def _is_35_delim(i):
        # Classic: FBID +-+ 35 +-+ Placement +-+ Dep +-+ 1 5 1 +-+ 700$
        if (re.search(r'\b3[05]\b', i) and 
            re.search(r'\+-\+|\+-|[-+]+3[05]|3[05][-+]', i) and
            re.search(r'dep|placement|add.to.cart|sales|auto|lead', i, re.I)):
            return True
        # Dash variant: FBID - 35 - manual - dep - 1-5-1 300$ ABO
        if re.search(r'\b3[05]\+?\s*-\s*(manual|placement|dep)', i, re.I):
            return True
        # URL-encoded: 35%2B +-+ Place
        if re.search(r'3[05]%2B.*Plac', i, re.I):
            return True
        # ukKeir variant: FBID-+35+-+ukKeir-+fbinstmob+-+Sales
        if re.search(r'3[05]\+-\+uk|uk\w+-\+fb.*Sales?$', i, re.I):
            return True
        # Space variant: FBID +- +35+-+Placement
        if re.search(r'\+\-\s*\+\s*3[05]', i):
            return True
        # Placement only (no 35 but +-+ Placement +-+ Dep with budget)
        if re.search(r'\+-\+.*Placement.*Dep.*\d+\$', i):
            return True
        # +- +Placement +- +Dep+- (space variant without 35)
        if re.search(r'\+\-\s*\+Placement\s*\+\-\s*\+Dep', i):
            return True
        # campaign_id= variant
        if re.search(r'^campaign_id=.*Placement.*Dep', i, re.I):
            return True
        # 1226319452786815 +- 35 Placement + AW4460 - 200$
        if re.search(r'\b35\s+Placement\s*\+\s*\w+\s*-\s*\d+\$', i):
            return True
        return False
    
    R('A1_delim_35', 'delimiter', 'high', _is_35_delim,
      'FBID +-+ 35 +-+ Placement +-+ Dep +-+ budget')
    
    # A2: Pipe-delimited (IN | TYPE | ...)
    R('A2_pipe', 'delimiter', 'high',
      lambda i: bool(re.search(r'^IN\s*\|\s*(ADS|FULL|ZUMY|PALM)\s*\|', i)),
      'IN | ADS/FULL/ZUMY/PALM | IV NNN | code')
    
    R('A2b_pipe_DE', 'delimiter', 'high',
      lambda i: bool(re.search(r'DE\s*\|\s*(AdsAgency|Klatten)', i)),
      'DE | AdsAgency | 3USD | ...')
    
    R('A2c_pipe_geo', 'delimiter', 'medium',
      lambda i: bool(re.match(r'^\d+\s*\|\s*(uk|de|ca)\s*\|', i, re.I)),
      'N | uk | faNN | setN')
    
    # -----------------------------------------------------------------
    # TIER 3: TOKEN-BASED CLUSTERS (unique buyer signatures)
    # -----------------------------------------------------------------
    R('B1_ZUMY', 'token', 'high',
      lambda i: bool(re.search(r'zumy', i, re.I)) and '|' not in i,
      'ZUMY-FBID-IN.../FP, Zumy_FBID_name, UK_Zumy_N')
    
    R('B3_V_geo', 'token', 'high',
      lambda i: bool(re.search(r'^V_[A-Z]{2}(fu|pa|DN|dn)\d+', i)),
      'V_INfu759-inst/fb/123-1')
    
    R('B4_PRRC', 'token', 'high',
      lambda i: i.startswith('PRRC-'),
      'PRRC-FBID-N')
    
    R('B6_HalG', 'token', 'high',
      lambda i: i.startswith('HalG'),
      'HalG/Q/NNN/N/N')
    
    R('B7_hoko', 'token', 'high',
      lambda i: bool(re.search(r'hoko|HOKO|^FR_HOKO|^\d+_fr_v\d+_hoko', i, re.I)),
      '904_fr_v1_hoko_5022, FR_HOKO_V1')
    
    R('B8_CLSA', 'token', 'high',
      lambda i: bool(re.search(r'CLSA', i)) or bool(re.match(r'^\d+C_RO$', i)),
      '1238CLSA_RO, 7689CLSA_FR')
    
    R('B10_atitu', 'token', 'high',
      lambda i: 'atitu' in i,
      '709_FRGAL2768(atitu)_FULL+-+1')
    
    R('B11_Carney', 'token', 'high',
      lambda i: i.startswith('Carney'),
      'CarneyN_fr_FBID_CBO1_purch2')
    
    R('B12_BM_color', 'token', 'high',
      lambda i: bool(re.match(r'^(VIOLET|RED|YELLOW)-BM\d', i)),
      'RED-BM3-5685-1919-IN-MIX-INC-S3')
    
    # -----------------------------------------------------------------
    # TIER 4: GEO+STRUCTURE PATTERNS
    # -----------------------------------------------------------------
    R('C1_GEO_px', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^(IN|UK|CA|CAsp)\d{2,3}\s+\d{4,6}\s+(px|tt|zm|fa)', i)),
      'IN116 87774 pxin1 vula llf, CA73 43037 ttd1 kook dh')
    
    R('C2_CA_top', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^CA\s+\d+/\d+\s+(si|vi)', i)) or bool(re.search(r'^CA\+\d+/\d+\+vi', i)),
      'CA 3/04 si_top3 R1_260/1')
    
    R('C3_FUK_FUFR', 'geo_struct', 'high',
      lambda i: bool(re.match(r'^(FUK|FUFR|Fuk|fuk|lapuk)\s*\d+', i, re.I)),
      'FUK 797 AP/B/G - 1, FUFR 812 AP/CP/G')
    
    R('C4_WJG', 'geo_struct', 'high',
      lambda i: bool(re.match(r'^WJG\s+\d+', i)),
      'WJG 12 - 55 - fukJuk')
    
    R('C5_ZO', 'geo_struct', 'high',
      lambda i: bool(re.match(r'^ZO\s+\d+\s+\w+', i)),
      'ZO 455 FR 1 / 1-1-2')
    
    R('C6_ADS_GEO', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^ADS\s+\d+\s+(FR|RO|ES|CA|DE)\b', i)),
      'ADS 537 FR 2 / 1-1-2')
    
    R('C7_in_verif', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^in-\d+-\d+\s*verif', i)),
      'in-FBID-112 verif 4p/ads')
    
    R('C8_Gold_step', 'geo_struct', 'high',
      lambda i: bool(re.search(r'(Gold|Ar-).*step', i, re.I)),
      'DE-Gold-2026-1step-Tilo-1171-2')
    
    R('C9_BA_ES', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^BA\s+\d+\s+(cata|CBO)\s*ES', i)),
      'BA 44840 cata ES01_04_9 b4 33-65+')
    
    R('C10_ES_ACR', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^ES-\d+-ACR', i)),
      'ES-1236953524710146-ACR-TT')
    
    R('C11_ES_Lang', 'geo_struct', 'high',
      lambda i: bool(re.search(r'_ES_Lang_|Pau-Garc', i)),
      '(FBID)_ES_Lang_FBfeed+autoAUD_cost1000')
    
    R('C12_Camp_ES', 'geo_struct', 'medium',
      lambda i: bool(re.match(r'^Camp', i)),
      'CampS_0904_ES_BotinI(adv+)')
    
    R('C13_dk_dot', 'geo_struct', 'high',
      lambda i: bool(re.search(r'^dk\.(ann|yt)\d+', i)),
      'dk.ann47.kje5tel1/ab113/ac/rrff')
    
    # -----------------------------------------------------------------
    # TIER 5: AGENCY + STRUCTURE (agency as dimension, structure as key)
    # -----------------------------------------------------------------
    R('D1a_FULL_UK', 'agency', 'high',
      lambda i: bool(re.search(r'FULL\d+GB|Full_UK|FULL_UK', i, re.I)),
      'FULL311GB25 — Копия, Full_UK_276')
    
    R('D1b_FULL_DE_CH', 'agency', 'high',
      lambda i: bool(re.match(r'^FULL\d+(DE|CH)', i)),
      'FULL138DE20a, FULL153CH3aUPb')
    
    R('D1c_FL_GB_DK', 'agency', 'high',
      lambda i: bool(re.match(r'^FL\d+\w*(GB|DK)', i)),
      'FL333GB1_D, FL342DK_CL_ALL')
    
    R('D1d_FULL_adset', 'agency', 'medium',
      lambda i: bool(re.search(r'(FULL|TRUST)\s+adset', i)),
      '18 FULL adset rez GB 1:3:3')
    
    R('D2_DH_GB', 'agency', 'high',
      lambda i: bool(re.search(r'^(DH_GB|T2_GB)', i)),
      'DH_GB_107_04.04_CRP_Lew_CBO125#1')
    
    R('D3_TRM', 'agency', 'high',
      lambda i: bool(re.search(r'^(TRM|trm)', i)),
      'TRM_02-147-ddd-09/04 — Копия')
    
    R('D3b_tm_re', 'agency', 'medium',
      lambda i: bool(re.search(r'^tm\d+re\d', i)),
      'tm207re1 Re35')
    
    R('D4_TV', 'agency', 'medium',
      lambda i: bool(re.search(r'TRUST\s+TV|ADS\s+TV', i)),
      '32 TRUST TV DE – копія1')
    
    R('D4b_TRUST_AC', 'agency', 'medium',
      lambda i: bool(re.search(r'(TRUST|HIU)_AC', i)),
      'TRUST_AC|5|CAFR_Senergia_Lead')
    
    R('D5_Dinh_fire', 'agency', 'high',
      lambda i: bool(re.search(r'fire(Scarct|Sber)|^Dinh_|^\d+_Dinh_', i)),
      '1_TM_870086915825322_fireScarct_DE_sales')
    
    R('D5b_Luca_eliS', 'agency', 'high',
      lambda i: bool(re.search(r'Luca.*eliS', i)),
      '2_Luca_745110188118711_eliS_FR_sales')
    
    R('D6_PLM', 'agency', 'medium',
      lambda i: bool(re.match(r'^PLM\d+', i)),
      'PLM17-760608740457888-i1')
    
    R('D6b_PALM', 'agency', 'medium',
      lambda i: bool(re.search(r'\bPALM\b', i) and 'TAG' not in i and '|' not in i),
      'PALM standalone references')
    
    R('D7_F_slash', 'agency', 'high',
      lambda i: bool(re.match(r'^F/(FREL|DE|FROM|CATNOTA|FR|2PCA)', i)),
      'F/FREL2, F/DE12')
    
    R('D7b_F1_slash', 'agency', 'high',
      lambda i: bool(re.match(r'^F1/', i)),
      'F1/1428675718218895/az/2RA')
    
    R('D8_PM_GEO', 'agency', 'medium',
      lambda i: bool(re.search(r'^PM\d+(DE|GB)', i)),
      'PM13DE27a')
    
    R('D9_SHD', 'agency', 'high',
      lambda i: bool(re.search(r'^(ca|de|in|ro)\d?-\d+-.*/(shd|zum|tm)', i)),
      'ca-1371...-450+a3/shd')
    
    R('D10_agent_T2', 'agency', 'medium',
      lambda i: bool(re.search(r'^agent\s+T2|^T[2-5]\s+K(fak|draka)', i)),
      'agent T2 KfakAlica 1x13')
    
    R('D11a_HH_DE', 'agency', 'medium',
      lambda i: bool(re.search(r'^HH_DE', i)),
      'HH_DE_L_1991771108386540_662DE')
    
    R('D11b_HiuHiu', 'agency', 'medium',
      lambda i: bool(re.search(r'^(HiuHiu|Hiu[_-]|hiu-)', i)),
      'HiuHiu_DE_L_..., Hiu_08.04_Susanna')
    
    R('D12_SLWWD', 'agency', 'medium',
      lambda i: bool(re.search(r'^SLWWD', i)),
      'SLWWD_FBID_DE+Raio+Digital')
    
    R('D13_AD_RO', 'agency', 'medium',
      lambda i: bool(re.match(r'^AD\d+[-\s]', i)) and bool(re.search(r'RO|TRBR|\(\d{4}\)', i)),
      'AD21-RO-ro9706-A-')
    
    R('D13b_ADS_reels', 'agency', 'medium',
      lambda i: bool(re.match(r'^ADS-\d+-', i)),
      'ADS-1430365468688172-IN06/40/.../FP')
    
    R('D14_DE_ACC', 'agency', 'medium',
      lambda i: bool(re.search(r'^DE_ACC', i)),
      'DE_ACC38-1430567498222139(DE_0604_1)')
    
    R('D17_IN_GOL', 'agency', 'medium',
      lambda i: bool(re.search(r'IN_GOL|IN_GOG', i)),
      'IN_GOLDI_10.04, IN_GOGANO_30.03')
    
    R('D18_DE_prod', 'agency', 'high',
      lambda i: bool(re.search(r'^DE-\d+\(DE_\d+', i)),
      'DE-1549...(DE_2203_1)-prod-11')
    
    R('D19_CROSS', 'agency', 'medium',
      lambda i: bool(re.match(r'^CROSS', i)),
      'CROSSN=DE=L=31/03=FBID=...')
    
    # -----------------------------------------------------------------
    # TIER 6: NUMERIC / CODE PATTERNS
    # -----------------------------------------------------------------
    R('E1_69_IDs', 'numeric', 'high',
      lambda i: bool(re.match(r'^6[89]\d{11}$', i.strip())),
      '6948052000962 (13-digit starting 68/69)')
    
    R('E2_tN_dataV', 'numeric', 'medium',
      lambda i: bool(re.match(r'^[tv]\d+_\d+', i)),
      't1_022 dataVca, v4_839 dataVjim')
    
    R('E3_fl_zm', 'numeric', 'medium',
      lambda i: bool(re.match(r'^(fl\d+|zm\d+in|v10_)', i)),
      'fl375in2_cr, zm10in2_cr, v10_804')
    
    R('E4_NxN', 'numeric', 'medium',
      lambda i: bool(re.match(r'^\dx\d\s+\w+', i)),
      '2x8 invide, 3x7 мерзостб')
    
    R('E5_inf', 'numeric', 'medium',
      lambda i: bool(re.search(r'\binf\s+(kamu|flo|son)\b', i)),
      '2055 inf kamu 41')
    
    R('E6_inz', 'numeric', 'medium',
      lambda i: bool(re.search(r'\b(inz|ina)\s+(kamu|kloa)\b', i)) or
                bool(re.search(r'^\d+\s+(inz|ina)\s+(kamu|kloa)', i)),
      '1023 inz kamu 3, 1002 ina kloa 4')
    
    R('E7_NNNNS', 'numeric', 'medium',
      lambda i: bool(re.match(r'^\d{3,5}S(-CPR)?$', i)),
      '1317S, 1658S-CPR')
    
    R('E8_defx', 'numeric', 'low',
      lambda i: bool(re.match(r'^defx\d+', i)),
      'defx1005')
    
    R('E9_tel', 'numeric', 'medium',
      lambda i: bool(re.match(r'^\d+\s+tel$', i)),
      '533 tel')
    
    R('E10_act', 'numeric', 'medium',
      lambda i: bool(re.match(r'^act=', i)),
      'act=3761094044199161')
    
    R('E11_FBID_GEO', 'numeric', 'medium',
      lambda i: bool(re.match(r'^\d{10,}_\d+_(PL|CA|DE|FR)$', i)),
      '1106439150703756_2_PL')
    
    R('E12_DE_ORI', 'numeric', 'medium',
      lambda i: bool(re.search(r'ORI\s+\d+-\d+\s+WF|^DE\d*\+?\s+ORI', i)),
      'DE 37+ ORI 81-84 WF 349_MANU')
    
    # -----------------------------------------------------------------
    # TIER 7: SMALL / NICHE CLUSTERS
    # -----------------------------------------------------------------
    for cid, pat, desc in [
        ('F1_GOE',       r'^GOE\s*[/+]',                      'GOE / 3 / FO / 3'),
        ('F2_KAR',       r'^KAR\s*[/+]',                      'KAR+/+6+/+y+/+5'),
        ('F3_GeN',       r'^(GeN|DN)\d?\s+\d+\s*/',           'GeN 1220 / FO 7'),
        ('F4_RooC',      r'^(RooC|Max)\s+\d+\s*/',            'RooC 327 // LQ 23'),
        ('F5_IP',        r'^IP\s+\d+\s+(ADS|TRUST|Trust)',     'IP 51 ADS DE rez'),
        ('F6_Kol',       r'(Kol|Klavo|Herok|Gero)\s+(AD|FD)\d?\s+\d+', 'Kol AD1 60247'),
        ('F7_DM1',       r'DM1\s+Naxa|Verto\s+DM|AER\d\s+ikol', 'DM1 Naxa 72162'),
        ('F8_FA_A',      r'^FA\(A\)',                          'FA(A)892..._(Opportunities)'),
        ('F9_Jcreo',     r'[JM]creo\d|^Sev\s+\d+',           '85 ... Jcreo1 31.03 Allan'),
        ('F10_Trus',     r'^Trus\d',                           'Trus106 50-65 INN52'),
        ('F11_AU',       r'^AU[5-9]\s',                        'AU5 (1457) Go Consultancy'),
        ('F12_sibs',     r'^sibs|^pl\s*-\s*Copy',             'sibs11 nat + new pix'),
        ('F13_koc',      r'^koc\d+',                           'koc11new_28V2'),
        ('F14_SpFX',     r'^SpFX',                             'SpFX-1'),
        ('F15_SJ',       r'^SJ\d+',                            'SJ53_CA01'),
        ('F16_CAI',      r'^CAI\s',                            'CAI Kalok 05600'),
        ('F17_AlicDP',   r'^AlicDP',                           'AlicDP 1'),
        ('F18_Ro',       r'^(Ro-|RORO)',                       'Ro-QuantumAI, RORO+ROEU'),
        ('F19_RiR',      r'^(RiR|yos)/',                       'RiR/kas/365/4'),
        ('F20_D1',       r'^(D1|RL1)_\d+',                    'D1_910611521351866'),
        ('F21_R_ES',     r'^R_\d.*ES',                         'R_1.152_ES_29/03'),
        ('F22_Wefun',    r'Wefun',                             '9_31-03_Wefun_CA'),
        ('F23_npx',      r'^n(px|w_px)',                       'npx_awe_me 5'),
        ('F24_H',        r'^H\d+-\d+$',                        'H1-2, H5-1'),
        ('F25_P_Iv',     r'^P\d+/(Iv|Ir)',                     'P138/Ivse'),
        ('F26_AS1',      r'^AS1_',                              'AS1_1643455890338083'),
        ('F27_aleks',    r'^aleks\s+avut',                     'aleks avut gazeta'),
        ('F28_CH',       r'^(CH_|ABO\s+CH|winner.*CH)',        'ABO CH-de, CH_CHRISTOPH'),
        ('F29_king',     r'^(king|lmking|new_king|oldking)',   'king_sp, lmking_es'),
        ('F30_DE_jerome',r'^DE[/ ].*(JEAN|jerome|SARAH)',      'DE 45 D P-de jerome'),
        ('F31_DE_MAPLE', r'MAPLE|^DE\s+\d+\s+[ZD]\s+P',      'DE 75 Z P-de jerome - MAPLE'),
        ('F32_multi',    r'_multi\d',                           'ZZ-38_multi2_Regardez'),
        ('F33_Sets',     r'Sets Catalog',                       '4 Sets Catalog 661 FR'),
        ('F34_ig_TR',    r'^ig\d+_TR',                          'ig632_TR_TPAO'),
        ('F35_gb_slash', r'^gb/(log|goku|palm|pm)/',           'gb/goku/1397/lew1tel3/'),
        ('F36_fr_oil',   r'^fr\s+oil',                          'fr oil net - 2'),
        ('F37_FR_nouveau',r'^\(\d+\)\s+FR',                    '(5) FR nouveau Swiss'),
        ('F38_vid_purch',r'vid.*purch',                         'GB vid purch'),
        ('F40_adcaN',    r'^adcaN',                             'adcaN+-+35357...'),
        ('F41_ES_cata',  r'^Es?\s+\d*\s*(Sand|Cata|NEWS|goh|1462|sanshes)', 'Es 2327 Cata5'),
        ('F42_infr',     r'^infr\d+',                           'infr241c2'),
        ('F43_tfr',      r'^(ttt_|tfr|afr)\d',                 'tfr15g1, afr2m1'),
        ('F44_lg',       r'^lg[-f]',                             'lg-ca-0304-148'),
        ('F45_RO_date',  r'^\d{2}\.\d{2}\s+RO/',               '08.04 RO/Cardiotensive'),
        ('F46_asd',      r'_asd\s+\d+|^asd_\w+\s+\d+|^goga_asd|^fas_asd', 'fas_asd 14'),
        ('F47_IN_N',     r'^IN[123]_\d',                        'IN2_1345273147244915'),
        ('F48_PA',       r'^PA_\d+',                             'PA_1295297619108659'),
        ('F49_CA_date',  r'^CA/(0?\d\.?\d|0?\d/)',              'CA/01.4/9678/7777/ADS'),
        ('F51_T2_FBID',  r'^\d*_?T2_\d+|^T2_\d+',             '3_T2_844016908624777'),
        ('F53_N_TM',     r'^\d+_(TM|T2)_\d+',                  '1_TM_870086915825322'),
        ('F55_F_CA',     r'^F\d+_(CA|CAPRIME)\d+',             'F562_CAPRIME2197'),
        ('F56_F_FBID',   r'^F\d+_\d{5,}',                      'F8_782235810836302'),
        ('F58_FA',       r'^FA_\d+',                             'FA_229_3'),
        ('F59_W',        r'^W\s*\d+$',                           'W1, W 3'),
        ('F60_FBID_DE',  r'^\d{16}\s+DE$',                      '4300876880172029 DE'),
        ('F61_LH',       r'^LH-',                                'LH-1530716258009537'),
        ('F62_PL',       r'^PL[/7]',                             'PL7/1235760321812564'),
        ('F63_agency',   r'^agency\(',                           'agency(924016993928493'),
        ('F64_Jack',     r'^Jack_',                              'Jack_1256745016314521'),
        ('F65_J_DE',     r'^J_DE',                               'J_DE_17.03-3'),
        ('F66_DSA',      r'^(DSA|SDA)_',                        'DSA_1198981428709056_DE'),
        ('F67_Rose',     r'^Rose',                               'ROSE_CA_S_+905734912027837'),
        ('F68_FullA',    r'^FullA_',                             'FullA_1249663553254707'),
        ('F69_deklam',   r'de/(klam|flip)/',                    '1198de/klam/51'),
        ('F70_Pa_wide',  r'^Pa\d+_',                             'Pa22_all_40_wide_cat2'),
        ('F71_starm',    r'starm\d',                             '10151_starm1_30/64_cbo'),
        ('F72_FBID_cost',r'^\(\d+\)_(UK|CA|ES|DE)_(cost|cutpl|LEAD)', '(FBID)_UK_cost_187'),
        ('F73_RaD_kas',  r'RaD\d?\s+kas',                       '111 RaD4 kas 145'),
        ('F74_UK_yaz',   r'^#\d+\s+UK',                         '#1 UK 2167... yaz'),
        ('F75_GROSS',    r'^GROSS_DE',                           'GROSS_DE_L_787880...'),
        ('F76_KingViet', r'KingViet',                            'KingVietTest65_...'),
    ]:
        R(cid, 'small', 'medium',
          lambda i, p=pat: bool(re.search(p, i)),
          desc)
    
    # -----------------------------------------------------------------
    # TIER 8: META / CATCH-ALL (must be LAST)
    # -----------------------------------------------------------------
    R('META_pure_FBID', 'meta', 'low',
      lambda i: bool(re.match(r'^120\d{15,}$', i.strip())),
      'Pure FB campaign ID (120...)')
    
    R('META_FBID', 'meta', 'low',
      lambda i: bool(re.match(r'^\d{13,19}$', i.strip())),
      'Generic standalone FBID')
    
    R('META_hash', 'meta', 'low',
      lambda i: bool(re.match(r'^[a-zA-Z0-9]{10}$', i)) and not i.isdigit(),
      'Random 10-char hash (test/debug)')
    
    return rules


# =============================================================================
# MAIN CLASSIFICATION FUNCTION
# =============================================================================

_RULES = _build_rules()


def classify(campaign_name: str) -> Dict:
    """
    Классифицирует название кампании (4sub) по "почерку" баера.
    
    Args:
        campaign_name: Строка 4sub из Facebook Ads
    
    Returns:
        dict с полями:
            cluster:    ID кластера (str) или None
            group:      Тип правила (delimiter/token/geo_struct/agency/numeric/small/meta)
            confidence: high / medium / low
            agency:     Определённое агентство или None
            rule_desc:  Описание сработавшего правила
    """
    name = campaign_name.strip()
    
    if not name:
        return {
            'cluster': None,
            'group': None,
            'confidence': None,
            'agency': None,
            'rule_desc': 'Empty input',
        }
    
    # Agency detection (independent of classification)
    agency = detect_agency(name)
    
    # Run rules in priority order (first match wins)
    for rule in _RULES:
        try:
            if rule['test'](name):
                return {
                    'cluster': rule['id'],
                    'group': rule['group'],
                    'confidence': rule['confidence'],
                    'agency': agency,
                    'rule_desc': rule['desc'],
                }
        except Exception:
            continue
    
    return {
        'cluster': None,
        'group': None,
        'confidence': None,
        'agency': agency,
        'rule_desc': 'No matching rule',
    }


def classify_batch(items: List[str]) -> List[Dict]:
    """
    Классифицирует список кампаний.
    
    Returns:
        Список dict с полями: item, cluster, group, confidence, agency, rule_desc
    """
    results = []
    for item in items:
        r = classify(item)
        r['item'] = item
        results.append(r)
    return results


def get_cluster_summary(items: List[str]) -> Dict:
    """
    Возвращает сводку по кластерам для списка кампаний.
    
    Returns:
        dict: {cluster_id: {'count': N, 'confidence': str, 'group': str, 'items': [...]}}
    """
    from collections import defaultdict
    summary = defaultdict(lambda: {'count': 0, 'confidence': None, 'group': None, 'items': []})
    
    for item in items:
        r = classify(item)
        cid = r['cluster'] or '_UNCLASSIFIED'
        summary[cid]['count'] += 1
        summary[cid]['confidence'] = r['confidence']
        summary[cid]['group'] = r['group']
        summary[cid]['items'].append(item)
    
    return dict(summary)


def get_all_rules() -> List[Dict]:
    """Возвращает список всех правил с описаниями."""
    return [
        {
            'id': r['id'],
            'group': r['group'],
            'confidence': r['confidence'],
            'desc': r['desc'],
        }
        for r in _RULES
    ]


# =============================================================================
# TESTS
# =============================================================================

def run_tests():
    """Запускает встроенные тесты классификатора."""
    
    tests = [
        # (input, expected_cluster, description)
        
        # A1: 35 +-+ Dep variants
        ("1460020542207172 +-+ 35 +-+ Dep +-+ 1 10 1 +-+ 1000$ +-+ Add to cart", "A1_delim_35", "classic +-+ 35"),
        ("1393228645271775 - 35 - manual - dep - 1-6-1 300$ ABO", "A1_delim_35", "dash 35"),
        ("25260297427004151+-+35+-+Placement+-+Dep+-+1+10+1+-+300%24", "A1_delim_35", "URL-encoded"),
        ("915517327519929-+35+-+ukKeir-+fbinstmob+-+Sales", "A1_delim_35", "ukKeir variant"),
        ("1226319452786815 +- 35 Placement + AW4460 - 200$", "A1_delim_35", "space 35"),
        ("1203371774701050 +- +Placement +- +Dep+- +1+5+1+- +500$+- +ABO", "A1_delim_35", "space placement"),
        ("878264948146248 - 35+ - Dep - 1 - 10 - 1 - 250$", "A1_delim_35", "35+ dash"),
        
        # A2: Pipe
        ("IN | ADS | IV 103 | WFF | in20", "A2_pipe", "IN pipe ADS"),
        ("IN | ZUMY | SV 77 | WFF | in11", "A2_pipe", "IN pipe ZUMY (not B1!)"),
        ("IN | FULL | IV102 | WFF | REZ | in22", "A2_pipe", "IN pipe FULL"),
        
        # A3: TAG CTLPLM
        ("DE ADSA 17 1572850050691788 TAG CTLPLM1 PXPLM1", "A3_TAG_CTLPLM", "ADSA TAG"),
        ("DE PALM 13 1199907367016372 TAG CTLPLM1 PXPLM1 fpa1", "A3_TAG_CTLPLM", "PALM TAG"),
        ("DE+ADSA+148+2132505534218290+TAG+CTLPLM1+PXPLM1", "A3_TAG_CTLPLM", "URL-encoded ADSA"),
        
        # B1: ZUMY (not pipe)
        ("ZUMY-916299347435826-IN10/40/IN_SUNDAR_29/FBINSTreels/SALE/PIXELTM/FP - Copy 20", "B1_ZUMY", "ZUMY-FBID"),
        ("Zumy_962516993025638_tabinili", "B1_ZUMY", "Zumy_FBID"),
        ("UK_Zumy_3", "B1_ZUMY", "UK_Zumy"),
        
        # B3: V_geo
        ("V_INfu759-inst/fb/123-1", "B3_V_geo", "V_INfu"),
        ("V_UKfu391-all/R/C/113-17", "B3_V_geo", "V_UKfu"),
        
        # C1: GEO_px
        ("IN116 87774 pxin1 vula llf", "C1_GEO_px", "IN px"),
        ("CA73 43037 ttd1 kook dh", "C1_GEO_px", "CA ttd"),
        ("UK111 80922 pxttt515 okko ttt", "C1_GEO_px", "UK px"),
        
        # D1: FULL variants
        ("FULL311GB25 — Копия", "D1a_FULL_UK", "FULL GB"),
        ("Full_UK_276", "D1a_FULL_UK", "Full_UK"),
        ("FULL138DE20a", "D1b_FULL_DE_CH", "FULL DE"),
        ("FULL153CH3aUPb", "D1b_FULL_DE_CH", "FULL CH"),
        ("FL342DK_CL_ALL", "D1c_FL_GB_DK", "FL DK"),
        
        # E1: 69 IDs
        ("6948052000962", "E1_69_IDs", "69 ID"),
        ("6892491913862", "E1_69_IDs", "68 ID"),
        
        # E6: inz/ina
        ("1023 inz kamu 3", "E6_inz", "inz kamu"),
        ("1002 ina kloa 4", "E6_inz", "ina kloa"),
        
        # META: FBIDs (order matters!)
        ("120240305835270772", "META_pure_FBID", "120-prefix FBID"),
        ("1452257729818118", "META_FBID", "generic FBID"),
        
        # META: Others
        ("unknown", "META_unknown", "literal unknown"),
        ("{{campaign.name}", "META_template", "template"),
        
        # Small clusters
        ("HalG/Q/234/1/2", "B6_HalG", "HalG"),
        ("OpssKe/ 161 / Q / 3", "B5_OpssKe", "OpssKe"),
        ("FR_QWER_V3_5967_904", "B2_QWER", "QWER"),
    ]
    
    passed = 0
    failed = 0
    
    for input_val, expected, desc in tests:
        result = classify(input_val)
        actual = result['cluster']
        
        if actual == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {desc}")
            print(f"    Input:    {input_val[:70]}")
            print(f"    Expected: {expected}")
            print(f"    Got:      {actual}")
            print(f"    Rule:     {result['rule_desc']}")
            print()
    
    total = passed + failed
    print(f"Tests: {passed}/{total} passed" + (f", {failed} FAILED" if failed else " ✅ ALL PASSED"))
    return failed == 0


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        success = run_tests()
        sys.exit(0 if success else 1)
    
    elif len(sys.argv) > 1 and sys.argv[1] == "--rules":
        for r in get_all_rules():
            print(f"[{r['confidence']:>6s}] {r['id']:<25s} {r['desc']}")
    
    elif len(sys.argv) > 1:
        # Classify single item
        r = classify(' '.join(sys.argv[1:]))
        print(f"Cluster:    {r['cluster']}")
        print(f"Group:      {r['group']}")
        print(f"Confidence: {r['confidence']}")
        print(f"Agency:     {r['agency']}")
        print(f"Rule:       {r['rule_desc']}")
    
    else:
        # Read from stdin
        import json
        for line in sys.stdin:
            line = line.strip()
            if line:
                r = classify(line)
                r['item'] = line
                print(json.dumps(r, ensure_ascii=False))
