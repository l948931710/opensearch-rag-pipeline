#!/usr/bin/env python3
"""
Comprehensive GT-based chunk evaluation.

For each document:
  1. Extract + inject images + chunk (production pipeline)
  2. Match each GT chunk against actual chunks via keyword overlap
  3. Compute: Recall@K, MRR, nDCG, evidence hit, image accuracy, source loc
  4. Log failure modes
"""
import os, sys, json, tempfile, shutil, math
from collections import defaultdict, Counter
from typing import List, Dict, Any, Optional

sys.path.insert(0, '/Users/laijunchen/Downloads/opensearch-rag-pipeline')
os.environ['RAG_ENV'] = 'test'
from opensearch_pipeline.config import load_config
import opensearch_pipeline.config as m; m._config = load_config()
from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.chunker import DocumentChunker
from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks

SAMPLES_DIR = '/Users/laijunchen/Downloads/opensearch-rag-pipeline/scratch/eval_samples'

# ═══════════════════════════════════════════════════
# Test cases: (local_file, ext, display_name, split_mode)
# ═══════════════════════════════════════════════════
TEST_CASES = [
    ('pdf_sop.pdf',     'pdf',  'FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.pdf', 'step'),
    ('docx_water.docx', 'docx', 'FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx',    'step'),
    ('docx_qc.docx',    'docx', 'FL-QC-015-016标签日期确认管理规范.docx',           'clause'),
    ('docx_sop.docx',   'docx', 'FL-ZS-WI-003印刷产品检验作业导书.docx',            'step'),
    ('xlsx_sop.xlsx',   'xlsx', 'FL-QC-005-001-3电子天平操作规程.xlsx',              'text'),
    ('pptx_training.pptx','pptx','吸塑类培训资料7.10.pptx',                         'text'),
]

# ═══════════════════════════════════════════════════
# Inline GT for pdf_sop (from previous evaluation)
# ═══════════════════════════════════════════════════
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

def load_gt() -> Dict[str, List[Dict]]:
    """Load all GT definitions."""
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

def keyword_score(gt_keywords: List[str], chunk_text: str) -> float:
    """Score how well a chunk matches GT keywords (0-1)."""
    if not gt_keywords:
        return 0.0
    text_lower = chunk_text.lower()
    hits = sum(1 for kw in gt_keywords if kw.lower() in text_lower)
    return hits / len(gt_keywords)

def match_gt_to_chunks(gt_chunks: List[Dict], actual_chunks: list) -> List[Dict]:
    """
    For each GT chunk, rank actual chunks by keyword overlap.
    Returns list of match results.
    """
    results = []
    for gt in gt_chunks:
        kws = gt.get('keywords', [])
        gt_type = gt.get('chunk_type', '')
        gt_label = gt.get('label', '')
        exp_imgs = gt.get('expected_images', 0)
        if isinstance(exp_imgs, list):
            exp_imgs = len(exp_imgs)
        
        # Score all actual chunks
        scored = []
        for i, c in enumerate(actual_chunks):
            text = c.chunk_text if hasattr(c, 'chunk_text') else c.get('chunk_text', '')
            ct = c.chunk_type if hasattr(c, 'chunk_type') else c.get('chunk_type', '')
            extra = c.extra if hasattr(c, 'extra') else c.get('extra', {})
            img_refs = extra.get('image_refs', [])
            page = c.page_num if hasattr(c, 'page_num') else c.get('page_num')
            score = keyword_score(kws, text)
            scored.append({
                'rank': i,
                'score': score,
                'chunk_type': ct,
                'num_images': len(img_refs),
                'page_num': page,
                'text_preview': text[:80],
            })
        
        scored.sort(key=lambda x: -x['score'])
        
        # Best match
        best = scored[0] if scored else None
        matched = best and best['score'] >= 0.3  # threshold
        
        type_match = best['chunk_type'] == gt_type if best else False
        img_match = (best['num_images'] > 0) == (exp_imgs > 0) if best else False
        
        results.append({
            'gt_label': gt_label,
            'gt_type': gt_type,
            'gt_keywords': kws,
            'expected_images': exp_imgs,
            'matched': matched,
            'best_score': best['score'] if best else 0,
            'best_rank': scored.index(best) if best else -1,
            'type_match': type_match,
            'image_match': img_match,
            'best_chunk': best,
            'top_3': scored[:3],
        })
    
    return results

def compute_metrics(match_results: List[Dict]) -> Dict[str, float]:
    """Compute Recall@K, MRR, nDCG from match results."""
    n = len(match_results)
    if n == 0:
        return {}
    
    recall_1 = sum(1 for r in match_results if r['matched'] and r['best_rank'] == 0) / n
    recall_3 = sum(1 for r in match_results if r['matched']) / n  # within top-3 by design
    recall_5 = recall_3  # all our matches are top-1 by keyword
    
    # MRR: 1/(rank+1) for first relevant result
    mrr_sum = 0
    for r in match_results:
        if r['matched']:
            mrr_sum += 1.0 / (r['best_rank'] + 1)
    mrr = mrr_sum / n
    
    # nDCG@5
    dcg = 0
    for r in match_results:
        if r['matched']:
            rel = r['best_score']
            rank = r['best_rank'] + 1
            dcg += rel / math.log2(rank + 1)
    
    # Ideal DCG: all matched at rank 1 with score 1.0
    ideal_dcg = sum(1.0 / math.log2(2) for _ in match_results)
    ndcg = dcg / ideal_dcg if ideal_dcg > 0 else 0
    
    # Evidence hit rate
    evidence_hit = sum(1 for r in match_results if r['best_score'] >= 0.5) / n
    
    # Image/table association accuracy
    img_matches = [r for r in match_results if r['expected_images'] > 0]
    img_accuracy = sum(1 for r in img_matches if r['image_match']) / len(img_matches) if img_matches else 1.0
    
    # Type match accuracy
    type_accuracy = sum(1 for r in match_results if r['type_match']) / n
    
    # Source location accuracy (page_num present)
    page_present = sum(1 for r in match_results if r['matched'] and r['best_chunk'] and r['best_chunk']['page_num'] is not None) / n
    
    return {
        'recall_1': recall_1,
        'recall_3': recall_3,
        'recall_5': recall_5,
        'mrr': mrr,
        'ndcg': ndcg,
        'evidence_hit_rate': evidence_hit,
        'image_table_accuracy': img_accuracy,
        'type_accuracy': type_accuracy,
        'source_location_accuracy': page_present,
    }

def detect_failures(gt_chunks: List[Dict], actual_chunks: list, match_results: List[Dict]) -> List[Dict]:
    """Detect and categorize all failure modes."""
    failures = []
    
    for r in match_results:
        if not r['matched']:
            failures.append({
                'type': 'GT_NOT_MATCHED',
                'gt_label': r['gt_label'],
                'best_score': r['best_score'],
                'detail': f"GT '{r['gt_label']}' not matched (best score={r['best_score']:.2f})"
            })
        
        if r['matched'] and not r['type_match']:
            failures.append({
                'type': '流程步骤断裂' if r['gt_type'] == 'step_card' else 'chunk_type_mismatch',
                'gt_label': r['gt_label'],
                'detail': f"Expected {r['gt_type']}, got {r['best_chunk']['chunk_type']}"
            })
        
        if r['expected_images'] > 0 and r['matched'] and not r['image_match']:
            failures.append({
                'type': '图文错配',
                'gt_label': r['gt_label'],
                'detail': f"Expected images but chunk has {r['best_chunk']['num_images']} images"
            })
    
    # Check for duplicate/noise chunks
    texts = [getattr(c, 'chunk_text', '')[:100] for c in actual_chunks]
    seen = set()
    for t in texts:
        if t in seen and len(t) > 30:
            failures.append({'type': '重复噪声 chunk', 'detail': f'Duplicate: {t[:50]}'})
        seen.add(t)
    
    # Check chunk sizes
    for c in actual_chunks:
        text = c.chunk_text if hasattr(c, 'chunk_text') else c.get('chunk_text', '')
        if len(text) < 30:
            failures.append({'type': 'chunk 过小', 'detail': f'len={len(text)}: {text[:30]}'})
        elif len(text) > 900:
            failures.append({'type': 'chunk 过大', 'detail': f'len={len(text)}: {text[:50]}'})
    
    # Check parent title missing
    for c in actual_chunks:
        st = c.section_title if hasattr(c, 'section_title') else c.get('section_title', '')
        if not st and (c.chunk_type if hasattr(c, 'chunk_type') else '') == 'step_card':
            failures.append({'type': 'parent title 缺失', 'detail': f'step_card without section_title'})
            break  # only report once
    
    # Check page_num None for all
    all_none = all(
        (c.page_num if hasattr(c, 'page_num') else c.get('page_num')) is None
        for c in actual_chunks
    )
    if all_none:
        failures.append({'type': 'source location 缺失', 'detail': 'All page_num=None'})
    
    return failures

def process_document(local_name, ext, filename, mode) -> Dict:
    """Process one document through the full pipeline and evaluate."""
    local = f'{SAMPLES_DIR}/{local_name}'
    doc = {
        'doc_id': 'eval', 'version_no': 1, 'file_ext': ext,
        'filename': filename, 'raw_key': 'test', 'title': filename
    }
    
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = os.path.join(tmp, local_name)
        shutil.copy2(local, tmp_path)
        doc['local_path'] = tmp_path
        doc['_tmp_dir'] = tmp
        
        extractor = UnifiedExtractor()
        r = extractor.extract(doc)
        blocks_with_images = _inject_image_ref_blocks(r.blocks, r.assets, doc)
        
        chunker = DocumentChunker(split_mode=mode)
        chunks = chunker.chunk_from_blocks(
            blocks=blocks_with_images,
            doc_id='eval', version_no=1,
            metadata={'title': filename, 'source': 'test'}
        )
    
    return {
        'chunks': chunks,
        'num_blocks': len(r.blocks),
        'num_assets': len(r.assets),
    }

def main():
    gt_all = load_gt()
    all_results = {}
    all_failures = []
    
    print("="*80)
    print("COMPREHENSIVE GROUND TRUTH EVALUATION")
    print("="*80)
    
    for local_name, ext, filename, mode in TEST_CASES:
        label = local_name.split('.')[0]
        gt_chunks = gt_all.get(label, [])
        
        if not gt_chunks:
            print(f'\n⏭️  {label}: No GT defined, skipping')
            continue
        
        print(f'\n{"─"*70}')
        print(f'📄 {label} ({ext}, mode={mode})')
        
        result = process_document(local_name, ext, filename, mode)
        chunks = result['chunks']
        
        # Type distribution
        types = Counter(c.chunk_type for c in chunks)
        total_imgs = sum(len(c.extra.get('image_refs', [])) for c in chunks)
        print(f'   blocks={result["num_blocks"]}, assets={result["num_assets"]}')
        print(f'   chunks={len(chunks)}, types={dict(types)}, imgs_bound={total_imgs}')
        
        # Match GT
        match_results = match_gt_to_chunks(gt_chunks, chunks)
        metrics = compute_metrics(match_results)
        failures = detect_failures(gt_chunks, chunks, match_results)
        
        print(f'\n   Metrics:')
        for k, v in metrics.items():
            print(f'      {k:30s}: {v:.3f}')
        
        matched = sum(1 for r in match_results if r['matched'])
        print(f'\n   GT Match: {matched}/{len(gt_chunks)} = {matched/len(gt_chunks)*100:.1f}%')
        
        for r in match_results:
            status = '✅' if r['matched'] else '❌'
            type_ok = '✓' if r['type_match'] else '✗'
            img_ok = '✓' if r['image_match'] else ('✗' if r['expected_images'] > 0 else '—')
            print(f'      {status} {r["gt_label"][:35]:35s} score={r["best_score"]:.2f} type={type_ok} img={img_ok}')
        
        if failures:
            print(f'\n   Failures ({len(failures)}):')
            for f in failures:
                print(f'      • [{f["type"]}] {f["detail"][:60]}')
        
        all_results[label] = {
            'metrics': metrics,
            'match_results': match_results,
            'num_chunks': len(chunks),
            'num_gt': len(gt_chunks),
            'matched': matched,
            'types': dict(types),
        }
        all_failures.extend([{**f, 'doc': label} for f in failures])
    
    # ═══════════════════════════════════════════════════
    # Aggregate metrics
    # ═══════════════════════════════════════════════════
    print(f'\n{"="*80}')
    print("AGGREGATE METRICS")
    print("="*80)
    
    agg = defaultdict(list)
    for label, data in all_results.items():
        for k, v in data['metrics'].items():
            agg[k].append(v)
    
    print(f'\n{"Metric":35s} {"Mean":>8s} {"Min":>8s} {"Max":>8s}')
    print("─"*65)
    for k, vals in agg.items():
        print(f'{k:35s} {sum(vals)/len(vals):8.3f} {min(vals):8.3f} {max(vals):8.3f}')
    
    # Total GT match
    total_matched = sum(d['matched'] for d in all_results.values())
    total_gt = sum(d['num_gt'] for d in all_results.values())
    print(f'\nTotal GT Match: {total_matched}/{total_gt} = {total_matched/total_gt*100:.1f}%')
    
    # ═══════════════════════════════════════════════════
    # Failure mode summary
    # ═══════════════════════════════════════════════════
    print(f'\n{"="*80}')
    print("FAILURE MODE SUMMARY")
    print("="*80)
    
    failure_counts = Counter(f['type'] for f in all_failures)
    for ft, cnt in failure_counts.most_common():
        examples = [f for f in all_failures if f['type'] == ft][:2]
        print(f'\n  {ft} ({cnt}):')
        for ex in examples:
            print(f'    → [{ex["doc"]}] {ex["detail"][:70]}')
    
    # Save full results
    output = {
        'per_document': {},
        'aggregate_metrics': {k: sum(v)/len(v) for k, v in agg.items()},
        'total_gt_match': f'{total_matched}/{total_gt}',
        'failure_summary': dict(failure_counts),
        'all_failures': all_failures,
    }
    for label, data in all_results.items():
        output['per_document'][label] = {
            'metrics': data['metrics'],
            'num_chunks': data['num_chunks'],
            'num_gt': data['num_gt'],
            'matched': data['matched'],
            'types': data['types'],
        }
    
    with open(f'{SAMPLES_DIR}/eval_results.json', 'w') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    
    print(f'\n✅ Results saved to {SAMPLES_DIR}/eval_results.json')

if __name__ == '__main__':
    main()
