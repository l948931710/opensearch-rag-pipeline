# RRF vs Weighted Fusion Baseline Comparison Report

**Generated:** 2026-05-23 21:41:14

**Chunk Config:** Clause_1000_150 (locked)
**Query Count:** 47
**RRF Constant (k):** 60

---

## 1. Overall Comparison

| Strategy | R@1 | R@5 | Micro MRR | **Macro MRR** | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Weighted 3-Way Default (D0.5/S0.2/B0.3)** | 100.00% | 100.00% | 1.0000 | **1.0000** | [1.0000, 1.0000] | 0.0191 | 0.4447 | 44.26% | 0 |
| **Weighted 3-Way Optimal (D0.4/S0.2/B0.4)** | 100.00% | 100.00% | 1.0000 | **1.0000** | [1.0000, 1.0000] | 0.0007 | 0.4597 | 44.68% | 0 |
| **Weighted 2-Way (D0.7/B0.3)** | 100.00% | 100.00% | 1.0000 | **1.0000** | [1.0000, 1.0000] | 0.0113 | 0.4140 | 45.11% | 0 |
| **RRF 2-Way (D+B, k=60)** | 95.74% | 97.87% | 0.9716 | **0.9563** | [0.9255, 1.0000] | 0.0000 | 0.0094 | 45.96% | 0 |
| **RRF 3-Way (D+S+B, k=60)** | 97.87% | 100.00% | 0.9830 | **0.9714** | [0.9489, 1.0000] | 0.0000 | 0.0148 | 44.68% | 0 |

---

## 2. Per-Category MRR Breakdown

| Strategy | manual MRR | sop MRR | faq MRR | policy MRR |
| :--- | :---: | :---: | :---: | :---: |
| **Weighted 3-Way Default (D0.5/S0.2/B0.3)** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **Weighted 3-Way Optimal (D0.4/S0.2/B0.4)** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **Weighted 2-Way (D0.7/B0.3)** | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **RRF 2-Way (D+B, k=60)** | 1.0000 | 1.0000 | 0.8810 | 0.9444 |
| **RRF 3-Way (D+S+B, k=60)** | 1.0000 | 1.0000 | 0.8857 | 1.0000 |

---

## 3. Failed Queries Analysis (R@1 ≠ 1)

### RRF 2-Way (D+B, k=60) — 2 failures

| Query ID | Category | Query | Target Doc | Rank | Top-1 Doc |
| :---: | :---: | :--- | :--- | :---: | :--- |
| Q34 | policy | 宿舍里可以自己做饭或者接电线吗？... | eval_admin_dormitory | 2 | eval_admin_dormitory |
| Q40 | faq | 宿舍热水供应是几点到几点？... | eval_company_faq | 6 | eval_admin_dormitory |

### RRF 3-Way (D+S+B, k=60) — 1 failures

| Query ID | Category | Query | Target Doc | Rank | Top-1 Doc |
| :---: | :---: | :--- | :--- | :---: | :--- |
| Q40 | faq | 宿舍热水供应是几点到几点？... | eval_company_faq | 5 | eval_admin_dormitory |


---

## 4. Decision Summary

- **Best RRF:** RRF 3-Way (D+S+B, k=60) (Macro MRR=0.9714, R@1=97.87%)
- **Best Weighted:** Weighted 3-Way Optimal (D0.4/S0.2/B0.4) (Macro MRR=1.0000, R@1=100.00%)
- **Delta:** Macro MRR=-0.0286, R@1=-2.13%

> [!NOTE]
> Weighted fusion **outperforms** RRF on this evaluation set.

