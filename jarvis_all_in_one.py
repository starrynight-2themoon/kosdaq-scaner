import os
import json
import webbrowser
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import FinanceDataReader as fdr
import pandas as pd
from tqdm import tqdm
import warnings

warnings.filterwarnings('ignore')

def collect_candidates():
    """코스닥 전 종목을 스캔하여 브라우저에서 재필터링 가능한 후보군 일봉 데이터를 수집"""
    print("[1/2] 코스닥 상장 종목 로드 중...")
    df_krx = fdr.StockListing('KOSDAQ')
    # 정규표현식 오타(\vert{})를 파이프 기호(|)로 수정
    cond_name = ~df_krx['Name'].str.contains('스팩|우$\vert{}호$', regex=True)
    cond_marcap = (df_krx['Marcap'] >= 800_0000_0000) & (df_krx['Marcap'] <= 15000_0000_0000)
    universe = df_krx[cond_name & cond_marcap][['Code', 'Name', 'Marcap']].copy()

    start_date = (datetime.now() - timedelta(days=110)).strftime('%Y-%m-%d')
    candidates = []

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
            # HTML 내부에서 슬라이더로 조절할 수 있도록 1차 수집은 느슨하게(+8% 이상, 200억 이상) 통과시킴
            spikes = search_win[(search_win['Pct'] >= 8.0) & (search_win['Close'] > search_win['Open']) & (search_win['Est_Amount'] >= 200_0000_0000)]
            if spikes.empty:
                return None

            best_idx = spikes['Est_Amount'].idxmax()
            spike = spikes.loc[best_idx]
            today = recent.iloc[-1]

            vol_ratio = (today['Volume'] / spike['Volume']) * 100
            ma20_diff = ((today['Close'] - today['MA20']) / today['MA20']) * 100

            # 1차 느슨한 필터 (거래량 비율 40% 이하, 이격도 -5% ~ +10%)
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
                return {
                    "code": code, "name": name,
                    "marcap": int(marcap / 1_0000_0000),
                    "currentPrice": int(today['Close']),
                    "spikeDate": best_idx.strftime('%Y-%m-%d'),
                    "spikePct": round(float(spike['Pct']), 1),
                    "spikeAmount": int(spike['Est_Amount'] / 1_0000_0000),
                    "spikeOpen": int(spike['Open']),
                    "spikeClose": int(spike['Close']),
                    "spikeHigh": int(spike['High']),
                    "spikeVol": int(spike['Volume']),
                    "volRatio": round(float(vol_ratio), 1),
                    "ma20Diff": round(float(ma20_diff), 2),
                    "ohlcv": ohlcv_list
                }
        except Exception:
            return None
        return None

    print(f"[2/2] {len(universe)}개 종목 멀티스레딩 스캔 및 차트 데이터 패키징 중...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(worker, row) for _, row in universe.iterrows()]
        for f in tqdm(as_completed(futures), total=len(futures)):
            res = f.result()
            if res:
                candidates.append(res)

    return candidates

def generate_single_html_app(candidates):
    """수집된 JSON 데이터와 인터랙티브 스캐너 UI + Plotly 차트를 단 하나의 HTML 파일로 결합"""
    json_payload = json.dumps(candidates, ensure_ascii=False)
    output_file = "index.html"

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta property="og:title" content="⚡ 코스닥 기준봉·눌림목 타깃 스캐너" />
<meta property="og:description" content="최근 20일 기준봉 출현 후 거래량이 1/4로 마른 20일선 눌림목 종목 실시간 차트 보드" />
<title>JARVIS 코스닥 올인원 스캐너 & 차트 보드</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ margin:0; padding:20px; background:#0f172a; color:#f8fafc; font-family:'Malgun Gothic',sans-serif; }}
  .filter-bar {{ display:flex; gap:20px; background:#1e293b; padding:15px 20px; border-radius:10px; margin-bottom:20px; align-items:center; flex-wrap:wrap; }}
  .filter-item {{ display:flex; flex-direction:column; gap:4px; font-size:0.85rem; }}
  .filter-item input {{ background:#0b1120; color:#38bdf8; border:1px solid #334155; padding:6px 10px; border-radius:6px; font-weight:bold; width:110px; }}
  .layout {{ display:grid; grid-template-columns:460px 1fr; gap:20px; }}
  .box {{ background:#1e293b; border-radius:10px; padding:15px; border:1px solid #334155; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.84rem; }}
  th, td {{ padding:9px 6px; border-bottom:1px solid #334155; text-align:right; }}
  th:first-child, td:first-child {{ text-align:left; }}
  tr.row-item {{ cursor:pointer; }}
  tr.row-item:hover, tr.row-item.active {{ background:rgba(56,189,248,0.18); }}
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: 1fr !important; }}
    .filter-bar {{ gap: 10px; padding: 12px; }}
    .filter-item input {{ width: 90px; }}
  }}
</style>
</head>
<body>
  <h2 style="margin-top:0; color:#38bdf8;">⚡ JARVIS 코스닥 기준봉 눌림목 올인원 스캐너</h2>
  <div class="filter-bar">
    <div class="filter-item"><label>기준봉 최소 등락률(%)</label><input type="number" id="fSpikePct" value="10.0" step="1" oninput="applyFilter()"></div>
    <div class="filter-item"><label>기준봉 최소 거래대금(억)</label><input type="number" id="fAmount" value="300" step="50" oninput="applyFilter()"></div>
    <div class="filter-item"><label>최대 거래량 비율(%)</label><input type="number" id="fVolRatio" value="25.0" step="2" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최소 이격(%)</label><input type="number" id="fMaMin" value="-2.0" step="0.5" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최대 이격(%)</label><input type="number" id="fMaMax" value="6.0" step="0.5" oninput="applyFilter()"></div>
    <div id="countBadge" style="margin-left:auto; font-weight:bold; color:#fbbf24;"></div>
  </div>
  <div class="layout">
    <div class="box" style="max-height:680px; overflow-y:auto;">
      <table>
        <thead><tr><th>종목명 (시총)</th><th>현재가</th><th>기준봉(거래대금)</th><th>거래량비율</th><th>20일이격</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="box">
      <div id="chartHeader" style="font-weight:bold; margin-bottom:10px; font-size:1.05rem;"></div>
      <div id="chartArea" style="width:100%; height:620px;"></div>
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

  const filtered = EMBEDDED_DATA.filter(d =>
    d.spikePct >= minPct && d.spikeAmount >= minAmt &&
    d.volRatio <= maxVol && d.ma20Diff >= maMin && d.ma20Diff <= maMax &&
    d.currentPrice >= d.spikeOpen
  ).sort((a, b) => a.volRatio - b.volRatio);

  document.getElementById('countBadge').innerText = `포착 종목: ${{filtered.length}}개 (전체 후보 ${{EMBEDDED_DATA.length}}개 중)`;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';

  if (filtered.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px;">조건 만족 종목 없음 (상단 필터 수치를 조정해 보세요)</td></tr>';
    return;
  }}

  filtered.forEach((s, i) => {{
    const tr = document.createElement('tr');
    tr.className = 'row-item' + (i === 0 ? ' active' : '');
    tr.innerHTML = `
      <td><b>${{s.name}}</b> <span style="color:#94a3b8;font-size:0.75rem;">(${{s.marcap}}억)</span><br><span style="color:#94a3b8;font-size:0.75rem;">기준봉: ${{s.spikeDate}}</span></td>
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
  document.getElementById('chartHeader').innerHTML =
    `📊 ${{s.name}} (${{s.code}}) | 현재가: ${{s.currentPrice.toLocaleString()}}원 | 손절기준선(기준봉시가): <span style="color:#f87171;">${{s.spikeOpen.toLocaleString()}}원</span>`;
  const dates = s.ohlcv.map(d => d.date);
  const cutoff = s.spikeVol * 0.25;

  const candle = {{
    x: dates, open: s.ohlcv.map(d=>d.open), high: s.ohlcv.map(d=>d.high),
    low: s.ohlcv.map(d=>d.low), close: s.ohlcv.map(d=>d.close),
    type: 'candlestick', name: '일봉',
    increasing: {{ line: {{color:'#ef5350'}}, fillcolor:'#ef5350' }},
    decreasing: {{ line: {{color:'#1e88e5'}}, fillcolor:'#1e88e5' }}
  }};
  const ma20 = {{ x: dates, y: s.ohlcv.map(d=>d.ma20), type:'scatter', mode:'lines', name:'20일선', line:{{color:'#ff9800', width:2.5}} }};
  const ma10 = {{ x: dates, y: s.ohlcv.map(d=>d.ma10), type:'scatter', mode:'lines', name:'10일선', line:{{color:'#26a69a', width:1.5, dash:'dot'}} }};
  const vol = {{
    x: dates, y: s.ohlcv.map(d=>d.volume), type:'bar', name:'거래량', yaxis:'y2',
    marker: {{ color: s.ohlcv.map(d => d.date===s.spikeDate ? '#a855f7' : (d.close>=d.open ? 'rgba(239,83,80,0.6)' : 'rgba(30,136,229,0.6)')) }}
  }};

  const layout = {{
    paper_bgcolor:'#1e293b', plot_bgcolor:'#0f172a', font:{{color:'#f8fafc'}},
    margin:{{l:55, r:40, t:25, b:35}},
    xaxis:{{type:'category', nticks:12, rangeslider:{{visible:false}}, gridcolor:'#1e293b'}},
    yaxis:{{domain:[0.32, 1], gridcolor:'#1e293b'}},
    yaxis2:{{domain:[0, 0.25], gridcolor:'#1e293b'}},
    shapes:[
      {{type:'rect', xref:'x', yref:'y', x0:s.spikeDate, x1:dates[dates.length-1], y0:s.spikeOpen, y1:s.spikeClose, fillcolor:'rgba(74,222,128,0.12)', line:{{width:0}}}},
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.spikeOpen, y1:s.spikeOpen, line:{{color:'#ef4444', width:2, dash:'dash'}}}},
      {{type:'line', xref:'paper', yref:'y2', x0:0, x1:1, y0:cutoff, y1:cutoff, line:{{color:'#a855f7', width:1.5, dash:'dot'}}}}
    ],
    annotations:[
      {{x:s.spikeDate, y:s.spikeHigh, text:`★기준봉 (+${{s.spikePct}}% / ${{s.spikeAmount}}억)`, showarrow:true, arrowhead:2, bgcolor:'#fbbf24', font:{{color:'#000', size:11}}, ay:-30}}
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
    print(f"\n[완료] 단일 HTML 올인원 파일 생성됨: {output_file}")
    webbrowser.open('file://' + os.path.realpath(output_file))


if __name__ == "__main__":
    data = collect_candidates()
    generate_single_html_app(data)