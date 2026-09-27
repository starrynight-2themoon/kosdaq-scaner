import os
import json
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

                # 매매 타점(매수구간, 1차/2차 목표가, 손절가) 자동 계산
                cur_price = int(today['Close'])
                ma20_price = int(today['MA20'])
                spike_open = int(spike['Open'])
                spike_close = int(spike['Close'])
                spike_high = int(spike['High'])
                spike_mid = int((spike_open + spike_close) / 2)

                # 1차 목표가: 기준봉 고가 (만약 현재가가 이미 기준봉 고가 근처면 최소 +7% 보정)
                tp1_price = max(spike_high, int(cur_price * 1.07))
                # 2차 목표가: 기준봉 상승 에너지(고가 - 시가)의 1.5배 확장 (N자 파동)
                candle_range = max(spike_high - spike_open, int(cur_price * 0.10))
                tp2_price = spike_high + int(candle_range * 0.5)
                if tp2_price <= tp1_price:
                    tp2_price = int(tp1_price * 1.08)

                return {
                    "code": code, "name": name,
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

    print(f"[2/2] {len(universe)}개 종목 멀티스레딩 스캔 및 타점 계산 중...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(worker, row) for _, row in universe.iterrows()]
        for f in tqdm(as_completed(futures), total=len(futures)):
            res = f.result()
            if res:
                candidates.append(res)

    return candidates

def generate_single_html_app(candidates):
    """수집된 JSON 데이터와 매매 가이드 + 타점 카드 + Plotly 차트를 단일 index.html로 결합"""
    json_payload = json.dumps(candidates, ensure_ascii=False)
    update_time_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    output_file = "index.html"

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta property="og:title" content="⚡ 코스닥 기준봉·눌림목 타깃 스캐너" />
<meta property="og:description" content="세력 기준봉 출현 후 거래량이 1/4로 마른 20일선 눌림목 종목 & 매수/익절/손절 자동 타점 보드" />
<title>JARVIS 코스닥 올인원 스캐너 & 매매 타점 보드</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ margin:0; padding:20px; background:#0f172a; color:#f8fafc; font-family:'Pretendard','Malgun Gothic',sans-serif; line-height:1.5; }}
  .top-bar {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:15px; }}
  .top-bar h2 {{ margin:0; color:#38bdf8; font-size:1.35rem; }}
  .update-badge {{ background:#1e293b; border:1px solid #334155; color:#94a3b8; padding:6px 12px; border-radius:20px; font-size:0.8rem; }}
  
  /* 알고리즘 설명 및 매매 가이드 아코디언 */
  details.guide-box {{ background:#1e293b; border:1px solid #38bdf8; border-radius:10px; padding:12px 18px; margin-bottom:18px; }}
  details.guide-box summary {{ cursor:pointer; font-weight:bold; color:#38bdf8; font-size:0.95rem; outline:none; }}
  .guide-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:14px; margin-top:14px; padding-top:14px; border-top:1px solid #334155; font-size:0.84rem; color:#cbd5e1; }}
  .guide-card {{ background:#0f172a; padding:12px; border-radius:8px; border:1px solid #334155; }}
  .guide-card b {{ color:#fbbf24; display:block; margin-bottom:6px; font-size:0.9rem; }}

  /* 필터 바 */
  .filter-bar {{ display:flex; gap:16px; background:#1e293b; padding:14px 18px; border-radius:10px; margin-bottom:18px; align-items:center; flex-wrap:wrap; border:1px solid #334155; }}
  .filter-item {{ display:flex; flex-direction:column; gap:4px; font-size:0.8rem; color:#94a3b8; }}
  .filter-item input {{ background:#0b1120; color:#38bdf8; border:1px solid #334155; padding:6px 10px; border-radius:6px; font-weight:bold; width:105px; }}

  /* 메인 레이아웃 */
  .layout {{ display:grid; grid-template-columns:440px 1fr; gap:20px; }}
  .box {{ background:#1e293b; border-radius:10px; padding:16px; border:1px solid #334155; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.83rem; }}
  th, td {{ padding:10px 6px; border-bottom:1px solid #334155; text-align:right; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ color:#94a3b8; }}
  tr.row-item {{ cursor:pointer; transition:0.15s; }}
  tr.row-item:hover, tr.row-item.active {{ background:rgba(56,189,248,0.18); }}

  /* 종목별 매매 타점 신호등 카드 */
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
    .filter-item input {{ width: 88px; }}
  }}
</style>
</head>
<body>
  <div class="top-bar">
    <h2>⚡ JARVIS 코스닥 기준봉·눌림목 스캐너 & 타점 보드</h2>
    <div class="update-badge">🕒 데이터 기준: {update_time_str}</div>
  </div>

  <!-- 1. 친구들을 위한 종목 선정 원리 및 매매 타점 설명서 -->
  <details class="guide-box" open>
    <summary>💡 [필독] 이 종목들은 어떤 원리로 뽑혔나요? & 실전 매매(진입·익절·손절) 가이드 (클릭하여 접기/펼치기)</summary>
    <div class="guide-grid">
      <div class="guide-card">
        <b>1️⃣ 왜 이 종목들인가? (선정 원리)</b>
        최근 20일 내에 <b>거래대금 300억 이상 + 등락률 10% 이상의 장대양봉(★기준봉)</b>이 터져 메이저 수급(세력)이 유입된 종목 중, 최근 주가가 조정을 받으며 <b>거래량이 기준봉의 1/4(25%) 이하로 바짝 마른 종목</b>만 추출했습니다. 즉, '세력은 안 나갔는데 개미 매물만 소화되며 에너지가 응축된 상태'입니다.
      </div>
      <div class="guide-card">
        <b>2️⃣ 언제, 어떻게 매수하나? (진입 타점)</b>
        급등하는 날 추격 매수하지 마세요. 주가가 <b>20일 이동평균선(주황색 선) 부근이나 녹색 지지박스(기준봉 몸통) 안</b>에 머물 때, 제시된 <b>[매수 타점 구간] 내에서 2번에 나누어 분할 매수</b>합니다. 오전 9시 30분 이후 거래량이 살짝 붙으며 양봉이 나올 때가 최적입니다.
      </div>
      <div class="guide-card">
        <b>3️⃣ 언제 익절하고 언제 손절하나? (매도 원칙)</b>
        • <b>1차 익절(50% 매도):</b> 전고점 매물대인 <b>기준봉 고가(1차 목표가)</b> 도달 시 절반 수익 확정<br>
        • <b>2차 익절(잔량 매도):</b> 전고점 돌파 시 N자 파동 목표치인 <b>2차 목표가</b>에서 전량 익절<br>
        • <b>기계적 손절(필수):</b> 세력 방어선인 <b>기준봉 시가(빨간 점선)</b>를 오후 3시 종가 기준으로 이탈하면 미련 없이 손절합니다.
      </div>
    </div>
  </details>

  <!-- 2. 실시간 조건 조절 필터 -->
  <div class="filter-bar">
    <div class="filter-item"><label>기준봉 최소 등락률(%)</label><input type="number" id="fSpikePct" value="10.0" step="1" oninput="applyFilter()"></div>
    <div class="filter-item"><label>기준봉 최소 거래대금(억)</label><input type="number" id="fAmount" value="300" step="50" oninput="applyFilter()"></div>
    <div class="filter-item"><label>최대 거래량 비율(%)</label><input type="number" id="fVolRatio" value="25.0" step="2" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최소 이격(%)</label><input type="number" id="fMaMin" value="-2.0" step="0.5" oninput="applyFilter()"></div>
    <div class="filter-item"><label>20일선 최대 이격(%)</label><input type="number" id="fMaMax" value="6.0" step="0.5" oninput="applyFilter()"></div>
    <div id="countBadge" style="margin-left:auto; font-weight:bold; color:#fbbf24; font-size:0.9rem;"></div>
  </div>

  <!-- 3. 좌측 종목 리스트 & 우측 매매 전략 보드 -->
  <div class="layout">
    <div class="box" style="max-height:740px; overflow-y:auto;">
      <table>
        <thead><tr><th>종목명 (시총)</th><th>현재가</th><th>기준봉(거래대금)</th><th>거래량비율</th><th>20일이격</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="box">
      <div id="chartHeader" style="font-weight:bold; margin-bottom:12px; font-size:1.1rem;"></div>
      
      <!-- 종목별 매수/목표/손절 신호등 카드 -->
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

  const filtered = EMBEDDED_DATA.filter(d =>
    d.spikePct >= minPct && d.spikeAmount >= minAmt &&
    d.volRatio <= maxVol && d.ma20Diff >= maMin && d.ma20Diff <= maMax &&
    d.currentPrice >= d.spikeOpen
  ).sort((a, b) => a.volRatio - b.volRatio);

  document.getElementById('countBadge').innerText = `🎯 포착 종목: ${{filtered.length}}개 (전체 후보 ${{EMBEDDED_DATA.length}}개 중)`;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';

  if (filtered.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:30px; color:#94a3b8;">조건 만족 종목 없음 (상단 필터 수치를 조금 완화해 보세요)</td></tr>';
    document.getElementById('tradePlanBox').innerHTML = '';
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
  // 수익률 및 손실률, 손익비 계산
  const buyLow = Math.min(s.currentPrice, s.ma20Price);
  const buyHigh = Math.max(s.currentPrice, s.ma20Price);
  const tp1Gain = (((s.tp1Price - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  const tp2Gain = (((s.tp2Price - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  const slLoss = (((s.spikeOpen - s.currentPrice) / s.currentPrice) * 100).toFixed(1);
  
  const risk = Math.max(s.currentPrice - s.spikeOpen, 1);
  const reward = s.tp1Price - s.currentPrice;
  const rrRatio = (reward / risk).toFixed(2);

  document.getElementById('chartHeader').innerHTML =
    `📊 ${{s.name}} (${{s.code}}) — 현재가: <span style="color:#38bdf8;">${{s.currentPrice.toLocaleString()}}원</span> <span style="font-size:0.85rem; color:#fbbf24; margin-left:10px;">[기대 손익비 1 : ${{rrRatio}}]</span>`;

  // 상단 4개 매매 타점 신호등 카드 업데이트
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
      // 녹색 세력 지지 박스 (기준봉 시가~종가)
      {{type:'rect', xref:'x', yref:'y', x0:s.spikeDate, x1:dates[dates.length-1], y0:s.spikeOpen, y1:s.spikeClose, fillcolor:'rgba(74,222,128,0.12)', line:{{width:0}}}},
      // 손절 기준선 (빨간 파선)
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.spikeOpen, y1:s.spikeOpen, line:{{color:'#ef4444', width:2, dash:'dash'}}}},
      // 1차 목표가선 (하늘색 점선)
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.tp1Price, y1:s.tp1Price, line:{{color:'#38bdf8', width:1.8, dash:'dot'}}}},
      // 2차 목표가선 (보라색 점선)
      {{type:'line', xref:'paper', yref:'y', x0:0, x1:1, y0:s.tp2Price, y1:s.tp2Price, line:{{color:'#c084fc', width:1.8, dash:'dot'}}}},
      // 하단 거래량 25% 커트라인
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
    print(f"\n[완료] 매매 가이드 및 타점 보드가 추가된 웹 배포용 파일 생성됨: {output_file}")


if __name__ == "__main__":
    data = collect_candidates()
    generate_single_html_app(data)