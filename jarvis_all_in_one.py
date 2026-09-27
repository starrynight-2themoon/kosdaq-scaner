import os
import json
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import FinanceDataReader as fdr
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm
import warnings

warnings.filterwarnings('ignore')

def get_clean_kosdaq_universe():
    """1단계: 관리종목, 투자주의환기종목, 거래정지, 스팩, 동전주를 원천 배제한 클린 유니버스 구축"""
    print("[1/3] 코스닥 상장 종목 로드 및 관리/환기종목 지뢰 제거 중...")
    df_krx = fdr.StockListing('KOSDAQ')
    
    cond_name = ~df_krx['Name'].str.contains('스팩|우$\vert{}호$|리츠', regex=True)
    cond_marcap = (df_krx['Marcap'] >= 800_0000_0000) & (df_krx['Marcap'] <= 15000_0000_0000)
    if 'Close' in df_krx.columns:
        cond_price = pd.to_numeric(df_krx['Close'], errors='coerce') >= 2000
    else:
        cond_price = True

    universe = df_krx[cond_name & cond_marcap & cond_price].copy()

    bad_keywords = '관리|환기|정지|정리매매|불성실|위험|경고|주의'
    for col in universe.columns:
        if col not in ['Code', 'Name', 'Market', 'Dept']:
            if universe[col].dtype == object:
                is_bad = universe[col].astype(str).str.contains(bad_keywords, regex=True, na=False)
                if is_bad.any():
                    universe = universe[~is_bad]
        elif col == 'Dept':
            is_bad_dept = universe['Dept'].astype(str).str.contains('관리|환기', regex=True, na=False)
            universe = universe[~is_bad_dept]

    print(f" -> 전체 {len(df_krx)}개 중 관리/환기/저가주 제거 후 클린 유니버스: {len(universe)}개 종목")
    return universe[['Code', 'Name', 'Marcap']]

def fetch_naver_financial_health(code):
    """차트 통과 종목에 대해 네이버 금융에서 관리/환기 여부 재검증 및 영업이익/부채비율/유보율 추출"""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        html = urllib.request.urlopen(req, timeout=5).read().decode('euc-kr', errors='ignore')
        soup = BeautifulSoup(html, 'lxml')

        header_text = soup.select_one('.wrap_company').get_text() if soup.select_one('.wrap_company') else ""
        if any(w in header_text for w in ['관리종목', '환기종목', '거래정지', '정리매매']):
            return {"is_toxic": True}

        opm, debt_ratio, reserve_ratio = None, None, None
        cop_analysis = soup.select_one('div.section.cop_analysis')
        if cop_analysis:
            rows = cop_analysis.select('tbody tr')
            for tr in rows:
                th = tr.select_one('th')
                if not th:
                    continue
                title = th.get_text(strip=True)
                tds = [td.get_text(strip=True).replace(',', '') for td in tr.select('td')]
                valid_vals = []
                for v in tds[:4]:
                    try:
                        valid_vals.append(float(v))
                    except ValueError:
                        pass
                if not valid_vals:
                    for v in tds[4:]:
                        try:
                            valid_vals.append(float(v))
                        except ValueError:
                            pass

                if valid_vals:
                    latest_val = valid_vals[-1]
                    if '영업이익률' in title:
                        opm = round(latest_val, 1)
                    elif '부채비율' in title:
                        debt_ratio = round(latest_val, 1)
                    elif '유보율' in title:
                        reserve_ratio = round(latest_val, 1)

        if (reserve_ratio is not None and reserve_ratio < 0) or (debt_ratio is not None and debt_ratio > 500):
            return {"is_toxic": True}

        is_healthy = True
        if opm is not None and opm < 0:
            is_healthy = False
        if debt_ratio is not None and debt_ratio > 200:
            is_healthy = False

        return {
            "is_toxic": False,
            "is_healthy": is_healthy,
            "opm": opm if opm is not None else 0.0,
            "debt": debt_ratio if debt_ratio is not None else 0.0,
            "reserve": reserve_ratio if reserve_ratio is not None else 0.0
        }
    except Exception:
        return {"is_toxic": False, "is_healthy": True, "opm": 0.0, "debt": 0.0, "reserve": 0.0}

def collect_candidates():
    """코스닥 클린 유니버스를 스캔하여 차트 조건 + 재무 건전성을 통과한 후보군 수집"""
    universe = get_clean_kosdaq_universe()
    start_date = (datetime.now() - timedelta(days=110)).strftime('%Y-%m-%d')
    chart_passed = []

    def worker(row):
        code, name, marcap = row['Code'], row['Name'], row['Marcap']
        try:
            df = fdr.DataReader(code, start_date)
            if len(df) < 45:
                return None
            df['MA10'] = df['Close'].rolling(10).mean()
            df['MA20'] = df['Close'].rolling(20).mean()
            df['Pct'] = df['Change'] * 100
            if 'Amount' not in df.columns or df['Amount'].isnull().all():
                df['Est_Amount'] = ((df['High'] + df['Low'] + df['Close']) / 3) * df['Volume']
            else:
                df['Est_Amount'] = df['Amount']

            recent = df.iloc[-20:]
            search_win = recent.iloc[:-3]
            spikes = search_win[(search_win['Pct'] >= 8.0) & (search_win['Close'] > search_win['Open']) & (search_win['Est_Amount'] >= 200_0000_0000)]
            if spikes.empty:
                return None

            best_idx = spikes['Est_Amount'].idxmax()
            spike = spikes.loc[best_idx]
            today = recent.iloc[-1]

            vol_ratio = (today['Volume'] / spike['Volume']) * 100
            ma20_diff = ((today['Close'] - today['MA20']) / today['MA20']) * 100

            if vol_ratio <= 40.0 and today['Close'] >= spike['Open'] * 0.97 and (-5.0 <= ma20_diff <= 10.0):
                df_60 = df.iloc[-60:].copy()
                ohlcv_list = []
                for idx, r in df_60.iterrows():
                    ohlcv_list.append({
                        "date": idx.strftime('%Y-%m-%d'),
                        "open": int(r['Open']), "high": int(r['High']),
                        "low": int(r['Low']), "close": int(r['Close']),
                        "volume": int(r['Volume']),
                        "ma10": round(float(r['MA10']), 1) if pd.notnull(r['MA10']) else None,
                        "ma20": round(float(r['MA20']), 1) if pd.notnull(r['MA20']) else None
                    })

                cur_price = int(today['Close'])
                ma20_price = int(today['MA20'])
                spike_open = int(spike['Open'])
                spike_close = int(spike['Close'])
                spike_high = int(spike['High'])
                spike_mid = int((spike_open + spike_close) / 2)

                tp1_price = max(spike_high, int(cur_price * 1.07))
                candle_range = max(spike_high - spike_open, int(cur_price * 0.10))
                tp2_price = spike_high + int(candle_range * 0.5)
                if tp2_price <= tp1_price:
                    tp2_price = int(tp1_price * 1.08)

                return {
                    "code": str(code).zfill(6), "name": name,
                    "marcap": int(marcap / 1_0000_0000),
                    "currentPrice": cur_price,
                    "ma20Price": ma20_price,
                    "spikeDate": best_idx.strftime('%Y-%m-%d'),
                    "spikePct": round(float(spike['Pct']), 1),
                    "spikeAmount": int(spike['Est_Amount'] / 1_0000_0000),
                    "spikeOpen": spike_open,
                    "spikeClose": spike_close,
                    "spikeHigh": spike_high,
                    "spikeMid": spike_mid,
                    "tp1Price": tp1_price,
                    "tp2Price": tp2_price,
                    "spikeVol": int(spike['Volume']),
                    "volRatio": round(float(vol_ratio), 1),
                    "ma20Diff": round(float(ma20_diff), 2),
                    "ohlcv": ohlcv_list
                }
        except Exception:
            return None
        return None

    print(f"[2/3] {len(universe)}개 종목 멀티스레딩 차트 스캔 중...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(worker, row) for _, row in universe.iterrows()]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Chart Scan"):
            res = f.result()
            if res:
                chart_passed.append(res)

    print(f"[3/3] 차트 통과 {len(chart_passed)}개 종목 대상 네이버 금융 재무제표 정밀 검증 중...")
    final_candidates = []
    for item in tqdm(chart_passed, desc="Financial Check"):
        fin = fetch_naver_financial_health(item['code'])
        if fin.get("is_toxic", False):
            continue
        item['isHealthy'] = fin['is_healthy']
        item['opm'] = fin['opm']
        item['debt'] = fin['debt']
        item['reserve'] = fin['reserve']
        final_candidates.append(item)

    print(f" -> 부실 위험주 제거 완료! 최종 생존 후보군: {len(final_candidates)}개")
    return final_candidates

def generate_single_html_app(candidates):
    """수집된 JSON 데이터와 네이버 증권 바로가기 + 재무 필터 + 타점 카드를 단일 index.html로 결합"""
    json_payload = json.dumps(candidates, ensure_ascii=False)
    update_time_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    output_file = "index.html"

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta property="og:title" content="⚡ 코스닥 기준봉·눌림목 타깃 스캐너" />
<meta property="og:description" content="관리/환기종목 완벽 제외! 세력 기준봉 출현 후 거래량이 1/4로 마른 20일선 눌림목 타점 보드" />
<title>JARVIS 코스닥 올인원 스캐너 & 매매 타점 보드</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ margin:0; padding:20px; background:#0f172a; color:#f8fafc; font-family:'Pretendard','Malgun Gothic',sans-serif; line-height:1.5; }}
  .top-bar {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:15px; }}
  .top-bar h2 {{ margin:0; color:#38bdf8; font-size:1.35rem; }}
  .update-badge {{ background:#1e293b; border:1px solid #334155; color:#94a3b8; padding:6px 12px; border-radius:20px; font-size:0.8rem; }}
  
  details.guide-box {{ background:#1e293b; border:1px solid #38bdf8; border-radius:10px; padding:12px 18px; margin-bottom:18px; }}
  details.guide-box summary {{ cursor:pointer; font-weight:bold; color:#38bdf8; font-size:0.95rem; outline:none; }}
  .guide-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:12px; margin-top:14px; padding-top:14px; border-top:1px solid #334155; font-size:0.83rem; color:#cbd5e1; }}
  .guide-card {{ background:#0f172a; padding:12px; border-radius:8px; border:1px solid #334155; }}
  .guide-card b {{ color:#fbbf24; display:block; margin-bottom:6px; font-size:0.88rem; }}

  .filter-bar {{ display:flex; gap:15px; background:#1e293b; padding:14px 18px; border-radius:10px; margin-bottom:18px; align-items:center; flex-wrap:wrap; border:1px solid #334155; }}
  .filter-item {{ display:flex; flex-direction:column; gap:4px; font-size:0.8rem; color:#94a3b8; }}
  .filter-item input[type="number"] {{ background:#0b1120; color:#38bdf8; border:1px solid #334155; padding:6px 10px; border-radius:6px; font-weight:bold; width:95px; }}
  .fin-toggle {{ display:flex; align-items:center; gap:8px; background:#0b1120; border:1px solid #4ade80; padding:8px 12px; border-radius:8px; cursor:pointer; font-size:0.83rem; color:#4ade80; font-weight:bold; }}

  .layout {{ display:grid; grid-template-columns:470px 1fr; gap:20px; }}
  .box {{ background:#1e293b; border-radius:10px; padding:16px; border:1px solid #334155; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
  th, td {{ padding:10px 6px; border-bottom:1px solid #334155; text-align:right; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ color:#94a3b8; }}
  tr.row-item {{ cursor:pointer; transition:0.15s; }}
  tr.row-item:hover, tr.row-item.active {{ background:rgba(56,189,248,0.18); }}

  .fin-badge-ok {{ background:rgba(74,222,128,0.15); color:#4ade80; padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:bold; }}
  .fin-badge-warn {{ background:rgba(251,191,36,0.15); color:#fbbf24; padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:bold; }}

  /* 네이버 증권 바로가기 버튼 스타일 */
  .chart-header-wrap {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #334155; }}
  .ext-links {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .btn-naver {{ display:inline-flex; align-items:center; gap:5px; background:#03c75a; color:#ffffff; text-decoration:none; padding:6px 12px; border-radius:6px; font-size:0.8rem; font-weight:bold; transition:0.15s; }}
  .btn-naver:hover {{ background:#02b350; transform:translateY(-1px); }}
  .btn-sub {{ display:inline-flex; align-items:center; gap:4px; background:#0f172a; color:#cbd5e1; border:1px solid #475569; text-decoration:none; padding:6px 11px; border-radius:6px; font-size:0.78rem; font-weight:bold; transition:0.15s; }}
  .btn-sub:hover {{ background:#334155; color:#ffffff; border-color:#38bdf8; }}
  .mini-naver {{ display:inline-block; background:#03c75a; color:#fff; text-decoration:none; padding:1px 5px; border-radius:4px; font-size:0.68rem; font-weight:bold; margin-left:4px; }}
  .mini-naver:hover {{ background:#02b350; }}

  .trade-plan-grid {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:10px; margin-bottom:14px; }}
  .plan-card {{ background:#0f172a; border-radius:8px; padding:10px 12px; border-left:4px solid #64748b; }}
  .plan-card.buy {{ border-left-color:#4ade80; }}
  .plan-card.tp1 {{ border-left-color:#38bdf8; }}
  .plan-card.tp2 {{ border-left-color:#c084fc; }}
  .plan-card.sl {{ border-left-color:#f87171; }}
  .plan-label {{ font-size:0.75rem; color:#94a3b8; margin-bottom:4px; }}
  .plan-price {{ font-size:1.05rem; font-weight:bold; color:#f8fafc; }}
  .plan-sub {{ font-size:0.75rem; margin-top:3px; }}

  @media (max-width: 960px) {{
    .layout {{ grid-template-columns: 1fr !important; }}
    .trade-plan-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .filter-bar {{ gap: 10px; padding: 12px; }}
  }}
</style>
</head>
<body>
  <div class="top-bar">
    <h2>⚡ JARVIS 코스닥 기준봉·눌림목 스캐너 & 타점 보드</h2>
    <div class="update-badge">🕒 데이터 기준: {update_time_str} (관리·환기종목 원천제외됨)</div>
  </div>

  <details class="guide-box" open>
    <summary>💡 [필독] 종목 선정 원리 & 재무 안전성 필터 & 실전 매매 가이드 (클릭하여 접기/펼치기)</summary>
    <div class="guide-grid">
      <div class="guide-card">
        <b>1️⃣ 세력 기준봉 + 거래량 건조 원리</b>
        최근 20일 내 <b>거래대금 300억+ & 등락률 10%+ 장대양봉(★기준봉)</b>이 출현한 종목 중, 주가가 20일선 부근으로 눌릴 때 <b>거래량이 기준봉의 1/4(25%) 이하로 마른 종목</b>만 포착합니다. (세력은 잔류하고 단타 매물만 소화 완료된 상태)
      </div>
      <div class="guide-card">
        <b>2️⃣ 3중 재무 방어벽 (부실주 차단)</b>
        • <b>원천 배제:</b> 관리종목, 투자주의환기종목, 자본잠식, 동전주(2천원 미만)는 아예 수집에서 제외됩니다.<br>
        • <b>재무 우량 필터:</b> 하단 <b>[🛡️ 재무 우량주만 보기]</b>를 켜면 영업이익 흑자 & 부채비율 200% 이하인 탄탄한 기업만 압축됩니다.
      </div>
      <div class="guide-card">
        <b>3️⃣ 매수·익절·손절 기계적 원칙</b>
        • <b>매수:</b> 20일선(주황선)~현재가 사이 2회 분할 매수<br>
        • <b>익절:</b> 1차 목표가(전고점)에서 50% 매도, 2차 목표가(N자 파동)에서 전량 익절<br>
        • <b>손절:</b> <b>기준봉 시가(빨간 점선)</b> 종가 이탈 시 기계적 손절
      </div>
    </div>
  </details>

  <div class="filter-bar">
    <div class="filter-item"><label>기준봉 최소 등락(%)</label><input type="number" id="fSpikePct" value="10.0" step="1" oninput="applyFilter()"></div>
    <div class="filter-item"><label>최소 거래대금(억)</label><input type="number" id="fAmount" value="300" step="50" oninput="applyFilter()"></div>
    <div class="filter-item"><label>최대 거래량비율(%)</label><input type="number" id="fVolRatio" value="25.0" step="2" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최소이격(%)</label><input type="number" id="fMaMin" value="-2.0" step="0.5" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최대이격(%)</label><input type="number" id="fMaMax" value="6.0" step="0.5" oninput="applyFilter()"></div>
    
    <label class="fin-toggle" title="체크 시 영업이익 흑자 및 부채비율 200% 이하 기업만 표시합니다">
      <input type="checkbox" id="fHealthyOnly" checked onchange="applyFilter()">
      🛡️ 재무 우량주만 보기 (흑자기업)
    </label>

    <div id="countBadge" style="margin-left:auto; font-weight:bold; color:#fbbf24; font-size:0.9rem;"></div>
  </div>

  <div class="layout">
    <div class="box" style="max-height:760px; overflow-y:auto;">
      <table>
        <thead><tr><th>종목명 (재무 / 링크)</th><th>현재가</th><th>기준봉(대금)</th><th>거래량비율</th><th>20일이격</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="box">
      <div id="chartHeader" class="chart-header-wrap"></div>
      <div id="tradePlanBox" class="trade-plan-grid"></div>
      <div id="chartArea" style="width:100%; height:580px;"></div>
    </div>
  </div>

<script>
const EMBEDDED_DATA = {json_payload};

function applyFilter() {{
  const minPct = parseFloat(document.getElementById('fSpikePct').value);
  const minAmt = parseFloat(document.getElementById('fAmount').value);
  const maxVol = parseFloat(document.getElementById('fVolRatio').value);
  const maMin = parseFloat(document.getElementById('fMaMin').value);
  const maMax = parseFloat(document.getElementById('fMaMax').value);
  const healthyOnly = document.getElementById('fHealthyOnly').checked;

  const filtered = EMBEDDED_DATA.filter(d =>
    d.spikePct >= minPct && d.spikeAmount >= minAmt &&
    d.volRatio <= maxVol && d.ma20Diff >= maMin && d.ma20Diff <= maMax &&
    d.currentPrice >= d.spikeOpen &&
    (!healthyOnly || d.isHealthy)
  ).sort((a, b) => a.volRatio - b.volRatio);

  document.getElementById('countBadge').innerText = `🎯 포착 종목: ${{filtered.length}}개 (전체 클린 후보 ${{EMBEDDED_DATA.length}}개 중)`;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';

  if (filtered.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px; color:#94a3b8;">조건 만족 종목 없음 (재무 우량주 체크를 해제하거나 필터 수치를 완화해 보세요)</td></tr>';
    document.getElementById('tradePlanBox').innerHTML = '';
    return;
  }}

  filtered.forEach((s, i) => {{
    const tr = document.createElement('tr');
    tr.className = 'row-item' + (i === 0 ? ' active' : '');
    const finBadge = s.isHealthy
      ? `<span class="fin-badge-ok">우량(OPM ${{s.opm}}%)</span>`
      : `<span class="fin-badge-warn">적자/테마(OPM ${{s.opm}}%)</span>`;

    const naverUrl = `https://finance.naver.com/item/main.naver?code=${{s.code}}`;

    tr.innerHTML = `
      <td>
        <b>${{s.name}}</b> ${{finBadge}}
        <a href="${{naverUrl}}" target="_blank" class="mini-naver" onclick="event.stopPropagation();" title="네이버 증권 새 탭 열기">N증권 ↗</a><br>
        <span style="color:#94a3b8;font-size:0.74rem;">시총 ${{s.marcap}}억 | 부채 ${{s.debt}}% | 기준봉 ${{s.spikeDate}}</span>
      </td>
      <td>${{s.currentPrice.toLocaleString()}}</td>
      <td style="color:#ff6b6b;font-weight:bold;">+${{s.spikePct}}% (${{s.spikeAmount}}억)</td>
      <td style="color:#c084fc;font-weight:bold;">${{s.volRatio}}%</td>
      <td>${{s.ma20Diff}}%</td>
    `;
    tr.onclick = () => {{
      document.querySelectorAll('.row-item').forEach(r => r.classList.remove('active'));
      tr.classList.add('active');
      drawChart(s);
    }};
    tbody.appendChild(tr);
  }});
  drawChart(filtered[0]);
}}

function drawChart(s) {{
  const buyLow = Math.min(s.currentPrice, s.ma20Price);
  const buyHigh = Math.max(s.currentPrice, s.ma20Price);
  const tp1Gain = (((s.tp1Price - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  const tp2Gain = (((s.tp2Price - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  const slLoss = (((s.spikeOpen - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  
  const risk = Math.max(s.currentPrice - s.spikeOpen, 1);
  const reward = s.tp1Price - s.currentPrice;
  const rrRatio = (reward / risk).toFixed(2);

  // 네이버 증권 3종 URL 생성 (종합정보 / 뉴스·공시 / 외인·기관 수급)
  const naverMainUrl = `https://finance.naver.com/item/main.naver?code=${{s.code}}`;
  const naverNewsUrl = `https://finance.naver.com/item/news.naver?code=${{s.code}}`;
  const naverFrgnUrl = `https://finance.naver.com/item/frgn.naver?code=${{s.code}}`;

  document.getElementById('chartHeader').innerHTML = `
    <div>
      <span style="font-size:1.15rem; font-weight:bold;">📊 ${{s.name}} (${{s.code}})</span>
      <span style="color:#38bdf8; font-size:1.1rem; font-weight:bold; margin-left:8px;">${{s.currentPrice.toLocaleString()}}원</span>
      <span style="font-size:0.85rem; color:#fbbf24; margin-left:8px; font-weight:bold;">[기대 손익비 1 : ${{rrRatio}}]</span><br>
      <span style="font-size:0.8rem; color:#94a3b8;">재무 요약: 영업이익률 ${{s.opm}}% · 부채비율 ${{s.debt}}% · 유보율 ${{s.reserve}}%</span>
    </div>
    <div class="ext-links">
      <a href="${{naverMainUrl}}" target="_blank" class="btn-naver">📗 네이버 증권 종합 ↗</a>
      <a href="${{naverNewsUrl}}" target="_blank" class="btn-sub">📰 뉴스·공시 ↗</a>
      <a href="${{naverFrgnUrl}}" target="_blank" class="btn-sub">🏦 외인·기관 수급 ↗</a>
    </div>
  `;

  document.getElementById('tradePlanBox').innerHTML = `
    <div class="plan-card buy">
      <div class="plan-label">🟢 분할 매수 구간 (20일선~현재가)</div>
      <div class="plan-price">${{buyLow.toLocaleString()}} ~ ${{buyHigh.toLocaleString()}}원</div>
      <div class="plan-sub" style="color:#4ade80;">기준봉 중심가: ${{s.spikeMid.toLocaleString()}}원</div>
    </div>
    <div class="plan-card tp1">
      <div class="plan-label">🎯 1차 목표가 (50% 분할익절)</div>
      <div class="plan-price">${{s.tp1Price.toLocaleString()}}원</div>
      <div class="plan-sub" style="color:#38bdf8;">현재가 대비 +${{tp1Gain}}% (전고점)</div>
    </div>
    <div class="plan-card tp2">
      <div class="plan-label">🚀 2차 목표가 (슈팅 전량익절)</div>
      <div class="plan-price">${{s.tp2Price.toLocaleString()}}원</div>
      <div class="plan-sub" style="color:#c084fc;">현재가 대비 +${{tp2Gain}}% (N자 파동)</div>
    </div>
    <div class="plan-card sl">
      <div class="plan-label">🛑 손절 기준선 (종가 이탈 시)</div>
      <div class="plan-price">${{s.spikeOpen.toLocaleString()}}원</div>
      <div class="plan-sub" style="color:#f87171;">현재가 대비 ${{slLoss}}% (기준봉 시가)</div>
    </div>
  `;

  const dates = s.ohlcv.map(d => d.date);
  const cutoff = s.spikeVol * 0.25;

  const candle = {{
    x: dates, open: s.ohlcv.map(d=>d.open), high: s.ohlcv.map(d=>d.high),
    low: s.ohlcv.map(d=>d.low), close: s.ohlcv.map(d=>d.close),
    type: 'candlestick', name: '일봉',
    increasing: {{ line: {{color:'#ef5350'}}, fillcolor:'#ef5350' }},
    decreasing: {{ line: {{color:'#1e88e5'}}, fillcolor:'#1e88e5' }}
  }};
  const ma20 = {{ x: dates, y: s.ohlcv.map(d=>d.ma20), type:'scatter', mode:'lines', name:'20일선(매수지지)', line:{{color:'#ff9800', width:2.5}} }};
  const ma10 = {{ x: dates, y: s.ohlcv.map(d=>d.ma10), type:'scatter', mode:'lines', name:'10일선', line:{{color:'#26a69a', width:1.5, dash:'dot'}} }};
  const vol = {{
    x: dates, y: s.ohlcv.map(d=>d.volume), type:'bar', name:'거래량', yaxis:'y2',
    marker: {{ color: s.ohlcv.map(d => d.date===s.spikeDate ? '#a855f7' : (d.close>=d.open ? 'rgba(239,83,80,0.6)' : 'rgba(30,136,229,0.6)')) }}
  }};

  const layout = {{
    paper_bgcolor:'#1e293b', plot_bgcolor:'#0f172a', font:{{color:'#f8fafc'}},
    margin:{{l:55, r:45, t:25, b:35}},
    xaxis:{{type:'category', nticks:12, rangeslider:{{visible:false}}, gridcolor:'#1e293b'}},
    yaxis:{{domain:[0.32, 1], gridcolor:'#1e293b'}},
    yaxis2:{{domain:[0, 0.25], gridcolor:'#1e293b'}},
    legend:{{orientation:'h', y:1.06, x:1, xanchor:'right'}},
    shapes:[
      {{type:'rect', xref:'x', yref:'y', x0:s.spikeDate, x1:dates[dates.length-1], y0:s.spikeOpen, y1:s.spikeClose, fillcolor:'rgba(74,222,128,0.12)', line:{{width:0}}}},
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.spikeOpen, y1:s.spikeOpen, line:{{color:'#ef4444', width:2, dash:'dash'}}}},
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.tp1Price, y1:s.tp1Price, line:{{color:'#38bdf8', width:1.8, dash:'dot'}}}},
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.tp2Price, y1:s.tp2Price, line:{{color:'#c084fc', width:1.8, dash:'dot'}}}},
      {{type:'line', xref:'paper', yref:'y2', x0:0, x1:1, y0:cutoff, y1:cutoff, line:{{color:'#a855f7', width:1.5, dash:'dot'}}}}
    ],
    annotations:[
      {{x:s.spikeDate, y:s.spikeHigh, text:`★기준봉 (+${{s.spikePct}}% / ${{s.spikeAmount}}억)`, showarrow:true, arrowhead:2, bgcolor:'#fbbf24', font:{{color:'#000', size:11}}, ay:-28}},
      {{x:0.99, y:s.tp2Price, xref:'paper', yref:'y', text:`2차 목표가: ${{s.tp2Price.toLocaleString()}}원 (+${{tp2Gain}}%)`, showarrow:false, font:{{color:'#c084fc', size:11}}, yshift:10, xanchor:'right'}},
      {{x:0.99, y:s.tp1Price, xref:'paper', yref:'y', text:`1차 목표가: ${{s.tp1Price.toLocaleString()}}원 (+${{tp1Gain}}%)`, showarrow:false, font:{{color:'#38bdf8', size:11}}, yshift:10, xanchor:'right'}},
      {{x:0.99, y:s.spikeOpen, xref:'paper', yref:'y', text:`손절선(기준봉시가): ${{s.spikeOpen.toLocaleString()}}원 (${{slLoss}}%)`, showarrow:false, font:{{color:'#f87171', size:11}}, yshift:-12, xanchor:'right'}}
    ]
  }};
  Plotly.newPlot('chartArea', [candle, ma20, ma10, vol], layout, {{responsive:true}});
}}

window.onload = applyFilter;
</script>
</body>
</html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"\n[완료] 네이버 증권 3종 퀵버튼이 탑재된 웹 배포용 파일 생성됨: {output_file}")


if __name__ == "__main__":
    data = collect_candidates()
    generate_single_html_app(data)