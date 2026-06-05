#!/usr/bin/env python3
"""
Generate HTML audit report comparing Ground Truth vs Actual Pipeline Chunks.
Side-by-side visual comparison with match scores, failure annotations, and images.
"""
import os, sys, json, tempfile, shutil, html, math
from collections import Counter
from typing import List, Dict, Any

sys.path.insert(0, '/Users/laijunchen/Downloads/opensearch-rag-pipeline')
os.environ['RAG_ENV'] = 'test'
from opensearch_pipeline.config import load_config
import opensearch_pipeline.config as m; m._config = load_config()
from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.chunker import DocumentChunker
from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks

SAMPLES_DIR = '/Users/laijunchen/Downloads/opensearch-rag-pipeline/scratch/eval_samples'

TEST_CASES = [
    ('pdf_sop.pdf',     'pdf',  'FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.pdf', 'step'),
    ('docx_water.docx', 'docx', 'FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx',    'step'),
    ('docx_qc.docx',    'docx', 'FL-QC-015-016标签日期确认管理规范.docx',           'clause'),
    ('docx_sop.docx',   'docx', 'FL-ZS-WI-003印刷产品检验作业导书.docx',            'step'),
    ('xlsx_sop.xlsx',   'xlsx', 'FL-QC-005-001-3电子天平操作规程.xlsx',              'text'),
    ('pptx_training.pptx','pptx','吸塑类培训资料7.10.pptx',                         'text'),
]

PDF_SOP_GT = [
    {"label":"前言(作业前提+说明)","chunk_type":"text_chunk","keywords":["作业前提","交货单","标识卡","车间","上班","报检"],"expected_images":0},
    {"label":"步骤1.1 收取交货单","chunk_type":"step_card","keywords":["收取","交货单","标识卡","日期","机台","货号","商检号","核对"],"expected_images":1},
    {"label":"步骤1.2 核对错误处理","chunk_type":"step_card","keywords":["核对错误","群通知","班长","竖起一箱"],"expected_images":1},
    {"label":"步骤1.3 抄录订单信息","chunk_type":"step_card","keywords":["抄录","订单","信息","跟踪"],"expected_images":1},
    {"label":"步骤2 交货单分类","chunk_type":"step_card","keywords":["交货单","分类","放置","四堆"],"expected_images":1},
    {"label":"步骤3.1 U8扫码报检","chunk_type":"step_card","keywords":["U8","扫码","报检","界面"],"expected_images":1},
    {"label":"步骤3.2 扫码枪扫描","chunk_type":"step_card","keywords":["扫码枪","红光","条形码","扫描"],"expected_images":1},
    {"label":"步骤4 报检填写","chunk_type":"step_card","keywords":["报检","交货单","U8","填"],"expected_images":0},
    {"label":"步骤4.1 填写设备班次","chunk_type":"step_card","keywords":["设备","班次","数量","产量"],"expected_images":1},
    {"label":"步骤4.2 班组人员","chunk_type":"step_card","keywords":["班组人员","交货单","修改","报检"],"expected_images":1},
    {"label":"步骤5 群通知完成","chunk_type":"step_card","keywords":["群通知","统计","报检完成"],"expected_images":1},
]

def load_gt():
    gt = {'pdf_sop': PDF_SOP_GT}
    with open(f'{SAMPLES_DIR}/gt_docx_analysis.json') as f:
        docx_gt = json.load(f)
    for key in ['docx_sop', 'docx_water', 'docx_qc']:
        if key in docx_gt:
            gt[key] = docx_gt[key].get('gt_chunks', [])
    with open(f'{SAMPLES_DIR}/gt_xlsx_pptx_analysis.json') as f:
        xp_gt = json.load(f)
    for key in ['xlsx_sop', 'pptx_training']:
        if key in xp_gt:
            gt[key] = xp_gt[key].get('gt_chunks', [])
    return gt

def keyword_score(kws, text):
    if not kws: return 0.0
    tl = text.lower()
    return sum(1 for k in kws if k.lower() in tl) / len(kws)

def process_doc(local_name, ext, filename, mode):
    local = f'{SAMPLES_DIR}/{local_name}'
    doc = {'doc_id': 'eval', 'version_no': 1, 'file_ext': ext,
           'filename': filename, 'raw_key': 'test', 'title': filename}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = os.path.join(tmp, local_name)
        shutil.copy2(local, tmp_path)
        doc['local_path'] = tmp_path; doc['_tmp_dir'] = tmp
        ext_obj = UnifiedExtractor()
        r = ext_obj.extract(doc)
        blocks = _inject_image_ref_blocks(r.blocks, r.assets, doc)
        chunker = DocumentChunker(split_mode=mode)
        chunks = chunker.chunk_from_blocks(blocks=blocks, doc_id='eval', version_no=1,
                                           metadata={'title': filename, 'source': 'test'})
    return chunks

def highlight_keywords(text, keywords):
    """Highlight matched keywords in text with yellow background."""
    result = html.escape(text)
    for kw in keywords:
        kw_esc = html.escape(kw)
        # Case-insensitive replace
        import re
        pattern = re.compile(re.escape(kw_esc), re.IGNORECASE)
        result = pattern.sub(f'<mark>{kw_esc}</mark>', result)
    return result

def score_color(score):
    if score >= 0.7: return '#22c55e'  # green
    if score >= 0.4: return '#f59e0b'  # amber
    return '#ef4444'  # red

def type_badge(ct, expected_ct):
    match = ct == expected_ct
    color = '#22c55e' if match else '#ef4444'
    return f'<span style="background:{color}20;color:{color};padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600">{html.escape(ct)}</span>'

def generate_html(all_data):
    """Generate full HTML audit report."""
    
    # Aggregate stats
    total_gt = sum(d['num_gt'] for d in all_data.values())
    total_matched = sum(d['matched'] for d in all_data.values())
    agg_metrics = {}
    for doc_data in all_data.values():
        for k, v in doc_data['metrics'].items():
            agg_metrics.setdefault(k, []).append(v)
    
    parts = []
    parts.append(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAG Pipeline — Ground Truth Audit Report</title>
<style>
  :root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --text: #e2e8f0; --text-dim: #94a3b8; --accent: #38bdf8;
    --green: #22c55e; --red: #ef4444; --amber: #f59e0b; --purple: #a78bfa;
    --border: #475569;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
  
  /* Header */
  .header {{ background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1px solid var(--border); border-radius: 16px; padding: 32px; margin-bottom: 32px; }}
  .header h1 {{ font-size: 28px; font-weight: 700; background: linear-gradient(90deg, var(--accent), var(--purple)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .header .subtitle {{ color: var(--text-dim); margin-top: 8px; }}
  
  /* Metric Cards */
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 24px 0; }}
  .metric-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; text-align: center; }}
  .metric-card .value {{ font-size: 28px; font-weight: 700; }}
  .metric-card .label {{ font-size: 12px; color: var(--text-dim); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  
  /* Doc Section */
  .doc-section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; margin-bottom: 24px; overflow: hidden; }}
  .doc-header {{ padding: 20px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; cursor: pointer; }}
  .doc-header:hover {{ background: var(--surface2); }}
  .doc-header h2 {{ font-size: 18px; font-weight: 600; }}
  .doc-badges {{ display: flex; gap: 8px; }}
  .badge {{ padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
  .badge-green {{ background: #22c55e20; color: var(--green); }}
  .badge-red {{ background: #ef444420; color: var(--red); }}
  .badge-amber {{ background: #f59e0b20; color: var(--amber); }}
  .badge-blue {{ background: #38bdf820; color: var(--accent); }}
  .doc-body {{ padding: 0; }}
  
  /* Comparison Row */
  .compare-row {{ display: grid; grid-template-columns: 1fr 80px 1fr; gap: 0; border-bottom: 1px solid var(--border); }}
  .compare-row:last-child {{ border-bottom: none; }}
  .compare-row.matched {{ }}
  .compare-row.unmatched {{ background: #ef444410; }}
  
  .gt-cell, .actual-cell {{ padding: 16px 20px; }}
  .gt-cell {{ border-right: 1px solid var(--border); }}
  .actual-cell {{ border-left: 1px solid var(--border); }}
  
  .score-cell {{ display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 8px; }}
  .score-value {{ font-size: 20px; font-weight: 700; }}
  .score-label {{ font-size: 10px; color: var(--text-dim); }}
  
  .cell-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .cell-title {{ font-weight: 600; font-size: 14px; }}
  .cell-meta {{ font-size: 11px; color: var(--text-dim); }}
  .cell-text {{ font-size: 13px; color: var(--text); white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; background: var(--bg); padding: 12px; border-radius: 8px; }}
  .cell-text mark {{ background: #f59e0b40; color: var(--amber); padding: 1px 2px; border-radius: 2px; }}
  
  .img-tags {{ display: flex; gap: 4px; flex-wrap: wrap; margin-top: 8px; }}
  .img-tag {{ background: var(--purple); color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
  .img-tag.missing {{ background: var(--red); }}
  
  .failure-tag {{ display: inline-block; background: #ef444420; color: var(--red); padding: 2px 8px; border-radius: 4px; font-size: 11px; margin: 2px; }}
  
  /* Column Headers */
  .col-headers {{ display: grid; grid-template-columns: 1fr 80px 1fr; background: var(--surface2); padding: 8px 20px; font-size: 12px; font-weight: 600; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }}
  .col-headers > div {{ text-align: center; }}
  .col-headers > div:first-child {{ text-align: left; }}
  .col-headers > div:last-child {{ text-align: left; }}
  
  /* Failure Summary */
  .failure-summary {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 24px; margin-bottom: 24px; }}
  .failure-summary h2 {{ font-size: 20px; margin-bottom: 16px; }}
  .failure-bar {{ display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--border); }}
  .failure-bar:last-child {{ border-bottom: none; }}
  .failure-name {{ width: 200px; font-size: 13px; font-weight: 500; }}
  .failure-count {{ width: 40px; text-align: right; font-weight: 700; font-size: 14px; }}
  .failure-bar-fill {{ height: 8px; border-radius: 4px; }}
  
  /* Unmatched */
  .unmatched-list {{ padding: 16px 24px; }}
  .unmatched-item {{ background: var(--bg); padding: 12px; border-radius: 8px; margin-bottom: 8px; border-left: 3px solid var(--red); }}
  
  /* Toggle */
  .toggle-btn {{ background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 6px 16px; border-radius: 8px; cursor: pointer; font-size: 12px; }}
  .toggle-btn:hover {{ background: var(--accent); color: var(--bg); }}
  
  /* Responsive */
  @media (max-width: 900px) {{
    .compare-row {{ grid-template-columns: 1fr; }}
    .score-cell {{ flex-direction: row; gap: 8px; padding: 8px 20px; border-bottom: 1px solid var(--border); }}
  }}
</style>
</head>
<body>
<div class="container">
''')
    
    # Header
    match_pct = total_matched / total_gt * 100 if total_gt else 0
    match_color = 'var(--green)' if match_pct >= 90 else 'var(--amber)' if match_pct >= 70 else 'var(--red)'
    parts.append(f'''
<div class="header">
  <h1>🔍 Ground Truth Audit Report</h1>
  <div class="subtitle">RAG Pipeline Chunk Strategy Evaluation — {len(all_data)} documents, {total_gt} GT chunks</div>
  <div class="metrics-grid">
    <div class="metric-card"><div class="value" style="color:{match_color}">{match_pct:.1f}%</div><div class="label">GT Match Rate</div></div>
    <div class="metric-card"><div class="value">{total_matched}/{total_gt}</div><div class="label">Chunks Matched</div></div>
    <div class="metric-card"><div class="value" style="color:var(--accent)">{sum(agg_metrics.get("recall_1",[0]))/max(len(agg_metrics.get("recall_1",[1])),1):.3f}</div><div class="label">Recall@1</div></div>
    <div class="metric-card"><div class="value" style="color:var(--accent)">{sum(agg_metrics.get("mrr",[0]))/max(len(agg_metrics.get("mrr",[1])),1):.3f}</div><div class="label">MRR</div></div>
    <div class="metric-card"><div class="value" style="color:var(--purple)">{sum(agg_metrics.get("ndcg",[0]))/max(len(agg_metrics.get("ndcg",[1])),1):.3f}</div><div class="label">nDCG</div></div>
    <div class="metric-card"><div class="value">{sum(agg_metrics.get("evidence_hit_rate",[0]))/max(len(agg_metrics.get("evidence_hit_rate",[1])),1):.3f}</div><div class="label">Evidence Hit</div></div>
    <div class="metric-card"><div class="value">{sum(agg_metrics.get("image_table_accuracy",[0]))/max(len(agg_metrics.get("image_table_accuracy",[1])),1):.3f}</div><div class="label">Image Acc</div></div>
    <div class="metric-card"><div class="value">{sum(agg_metrics.get("type_accuracy",[0]))/max(len(agg_metrics.get("type_accuracy",[1])),1):.3f}</div><div class="label">Type Acc</div></div>
  </div>
</div>
''')

    # Failure Summary
    all_failures = []
    for d in all_data.values():
        all_failures.extend(d.get('failures', []))
    fc = Counter(f['type'] for f in all_failures)
    max_count = max(fc.values()) if fc else 1
    
    parts.append('<div class="failure-summary"><h2>⚠️ Failure Mode Summary</h2>')
    colors = {
        '流程步骤断裂': 'var(--red)', '图文错配': 'var(--red)', 'GT_NOT_MATCHED': 'var(--red)',
        'chunk_type_mismatch': 'var(--amber)', 'parent title 缺失': 'var(--amber)',
        'source location 缺失': 'var(--amber)',
        'chunk 过小': 'var(--text-dim)', 'chunk 过大': 'var(--text-dim)',
    }
    for ft, cnt in fc.most_common():
        pct = cnt / max_count * 100
        col = colors.get(ft, 'var(--accent)')
        parts.append(f'''
<div class="failure-bar">
  <div class="failure-name">{html.escape(ft)}</div>
  <div class="failure-count" style="color:{col}">{cnt}</div>
  <div style="flex:1"><div class="failure-bar-fill" style="width:{pct}%;background:{col}"></div></div>
</div>''')
    parts.append('</div>')

    # Per-document sections
    for label, data in all_data.items():
        m = data['metrics']
        matched = data['matched']
        num_gt = data['num_gt']
        pct = matched / num_gt * 100 if num_gt else 0
        badge_cls = 'badge-green' if pct >= 90 else 'badge-amber' if pct >= 70 else 'badge-red'
        
        parts.append(f'''
<div class="doc-section">
  <div class="doc-header" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? 'block' : 'none'">
    <h2>📄 {html.escape(label)} <span style="font-size:13px;color:var(--text-dim);font-weight:400">({html.escape(data["ext"])} · {html.escape(data["mode"])})</span></h2>
    <div class="doc-badges">
      <span class="badge {badge_cls}">{matched}/{num_gt} matched ({pct:.0f}%)</span>
      <span class="badge badge-blue">R@1={m["recall_1"]:.2f}</span>
      <span class="badge badge-blue">nDCG={m["ndcg"]:.2f}</span>
    </div>
  </div>
  <div class="doc-body">
    <div class="col-headers"><div>Ground Truth (Expected)</div><div>Score</div><div>Actual Chunk (Pipeline Output)</div></div>
''')
        
        for mr in data['match_results']:
            gt_label = mr['gt_label']
            gt_type = mr['gt_type']
            gt_kws = mr['gt_keywords']
            exp_imgs = mr['expected_images']
            if isinstance(exp_imgs, list): exp_imgs = len(exp_imgs)
            matched_flag = mr['matched']
            best = mr['best_chunk']
            score = mr['best_score']
            
            row_class = 'matched' if matched_flag else 'unmatched'
            sc = score_color(score)
            
            # GT cell
            kw_html = ', '.join(f'<code>{html.escape(k)}</code>' for k in gt_kws)
            gt_img_html = ''
            if exp_imgs:
                gt_img_html = f'<div class="img-tags"><span class="img-tag">📷 ×{exp_imgs} expected</span></div>'
            
            # Actual cell
            if best:
                actual_text = best.get('text', '')
                actual_type = best.get('chunk_type', '')
                actual_imgs = best.get('num_images', 0)
                actual_page = best.get('page_num')
                highlighted = highlight_keywords(actual_text, gt_kws)
                
                img_match = (actual_imgs > 0) == (exp_imgs > 0)
                type_match = actual_type == gt_type
                
                actual_img_html = ''
                if exp_imgs > 0:
                    img_cls = 'img-tag' if img_match else 'img-tag missing'
                    actual_img_html = f'<div class="img-tags"><span class="{img_cls}">📷 ×{actual_imgs} {"✓" if img_match else "✗"}</span></div>'
                
                # Failures for this match
                failures_html = ''
                if matched_flag and not type_match:
                    failures_html += f'<span class="failure-tag">type: expected {html.escape(gt_type)}</span>'
                if exp_imgs > 0 and not img_match:
                    failures_html += '<span class="failure-tag">图文错配</span>'
                
                actual_cell = f'''
  <div class="cell-header">
    <div>{type_badge(actual_type, gt_type)}</div>
    <div class="cell-meta">p{actual_page or "?"}</div>
  </div>
  <div class="cell-text">{highlighted}</div>
  {actual_img_html}
  {failures_html}'''
            else:
                actual_cell = '<div class="cell-text" style="color:var(--red)">⚠️ No matching chunk found</div>'
            
            parts.append(f'''
    <div class="compare-row {row_class}">
      <div class="gt-cell">
        <div class="cell-header">
          <div class="cell-title">{html.escape(gt_label)}</div>
          <div class="cell-meta">{html.escape(gt_type)}</div>
        </div>
        <div class="cell-meta" style="margin-bottom:6px">Keywords: {kw_html}</div>
        {gt_img_html}
      </div>
      <div class="score-cell">
        <div class="score-value" style="color:{sc}">{score:.0%}</div>
        <div class="score-label">{"✅" if matched_flag else "❌"}</div>
      </div>
      <div class="actual-cell">
        {actual_cell}
      </div>
    </div>''')
        
        # Show unmatched actual chunks (not matched by any GT)
        unmatched_actuals = data.get('unmatched_actuals', [])
        if unmatched_actuals:
            parts.append(f'''
    <div class="unmatched-list">
      <div class="cell-title" style="margin-bottom:8px;color:var(--amber)">⚠️ Extra chunks not in GT ({len(unmatched_actuals)})</div>''')
            for ua in unmatched_actuals[:10]:
                parts.append(f'''
      <div class="unmatched-item">
        <div class="cell-meta">{type_badge(ua["chunk_type"], "")} p{ua.get("page_num", "?")}</div>
        <div style="font-size:12px;margin-top:4px;color:var(--text-dim)">{html.escape(ua["text"][:150])}</div>
      </div>''')
            parts.append('</div>')
        
        parts.append('  </div>\n</div>')
    
    parts.append('</div>\n</body>\n</html>')
    return '\n'.join(parts)


def main():
    gt_all = load_gt()
    all_data = {}
    
    for local_name, ext, filename, mode in TEST_CASES:
        label = local_name.split('.')[0]
        gt_chunks = gt_all.get(label, [])
        if not gt_chunks:
            continue
        
        print(f'Processing {label}...')
        chunks = process_doc(local_name, ext, filename, mode)
        
        # Match GT → actual
        match_results = []
        matched_actual_indices = set()
        
        for gt in gt_chunks:
            kws = gt.get('keywords', [])
            gt_type = gt.get('chunk_type', '')
            exp_imgs = gt.get('expected_images', 0)
            if isinstance(exp_imgs, list): exp_imgs = len(exp_imgs)
            
            scored = []
            for i, c in enumerate(chunks):
                score = keyword_score(kws, c.chunk_text)
                scored.append({
                    'index': i, 'score': score,
                    'chunk_type': c.chunk_type,
                    'num_images': len(c.extra.get('image_refs', [])),
                    'page_num': c.page_num,
                    'text': c.chunk_text[:500],
                })
            scored.sort(key=lambda x: -x['score'])
            best = scored[0] if scored else None
            matched = best and best['score'] >= 0.3
            
            if matched:
                matched_actual_indices.add(best['index'])
            
            match_results.append({
                'gt_label': gt.get('label', ''),
                'gt_type': gt_type,
                'gt_keywords': kws,
                'expected_images': exp_imgs,
                'matched': matched,
                'best_score': best['score'] if best else 0,
                'best_rank': 0,
                'type_match': (best['chunk_type'] == gt_type) if best else False,
                'image_match': ((best['num_images'] > 0) == (exp_imgs > 0)) if best else False,
                'best_chunk': best,
            })
        
        # Find unmatched actual chunks
        unmatched_actuals = []
        for i, c in enumerate(chunks):
            if i not in matched_actual_indices:
                unmatched_actuals.append({
                    'chunk_type': c.chunk_type,
                    'page_num': c.page_num,
                    'text': c.chunk_text[:200],
                })
        
        # Compute metrics
        n = len(match_results)
        recall_1 = sum(1 for r in match_results if r['matched']) / n if n else 0
        mrr = sum(1.0 for r in match_results if r['matched']) / n if n else 0
        dcg = sum(r['best_score'] / math.log2(2) for r in match_results if r['matched'])
        ideal = n / math.log2(2) if n else 1
        ndcg = dcg / ideal if ideal else 0
        evidence = sum(1 for r in match_results if r['best_score'] >= 0.5) / n if n else 0
        img_ms = [r for r in match_results if (r['expected_images'] if isinstance(r['expected_images'], int) else len(r['expected_images'])) > 0]
        img_acc = sum(1 for r in img_ms if r['image_match']) / len(img_ms) if img_ms else 1.0
        type_acc = sum(1 for r in match_results if r['type_match']) / n if n else 0
        src_acc = sum(1 for r in match_results if r['matched'] and r['best_chunk'] and r['best_chunk']['page_num'] is not None) / n if n else 0
        
        # Detect failures
        failures = []
        for r in match_results:
            if not r['matched']:
                failures.append({'type': 'GT_NOT_MATCHED', 'detail': f"{r['gt_label']}"})
            if r['matched'] and not r['type_match']:
                ft = '流程步骤断裂' if r['gt_type'] == 'step_card' else 'chunk_type_mismatch'
                failures.append({'type': ft, 'detail': f"expected {r['gt_type']} got {r['best_chunk']['chunk_type']}"})
            ei = r['expected_images']
            if isinstance(ei, list): ei = len(ei)
            if ei > 0 and r['matched'] and not r['image_match']:
                failures.append({'type': '图文错配', 'detail': f"{r['gt_label']}"})
        for c in chunks:
            if len(c.chunk_text) < 30:
                failures.append({'type': 'chunk 过小', 'detail': c.chunk_text[:30]})
            elif len(c.chunk_text) > 900:
                failures.append({'type': 'chunk 过大', 'detail': f'len={len(c.chunk_text)}'})
        if all(c.page_num is None for c in chunks):
            failures.append({'type': 'source location 缺失', 'detail': 'all page_num=None'})
        for c in chunks:
            if c.chunk_type == 'step_card' and not c.section_title:
                failures.append({'type': 'parent title 缺失', 'detail': 'step_card missing section_title'})
                break
        
        all_data[label] = {
            'ext': ext, 'mode': mode,
            'num_gt': n, 'matched': sum(1 for r in match_results if r['matched']),
            'metrics': {
                'recall_1': recall_1, 'mrr': mrr, 'ndcg': ndcg,
                'evidence_hit_rate': evidence, 'image_table_accuracy': img_acc,
                'type_accuracy': type_acc, 'source_location_accuracy': src_acc,
            },
            'match_results': match_results,
            'failures': failures,
            'unmatched_actuals': unmatched_actuals,
        }
    
    html_content = generate_html(all_data)
    out_path = f'{SAMPLES_DIR}/gt_audit_report.html'
    with open(out_path, 'w') as f:
        f.write(html_content)
    print(f'\n✅ HTML audit report: {out_path}')
    print(f'   Total: {sum(d["matched"] for d in all_data.values())}/{sum(d["num_gt"] for d in all_data.values())} GT chunks matched')

if __name__ == '__main__':
    main()
