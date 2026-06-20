# -*- coding: utf-8 -*-
"""P3: monolithic 'prose table' flatten — a sectioned SOP trapped in one table block
must chunk normally; real data tables + blank forms must be left untouched."""
from opensearch_pipeline.chunker import (
    DocumentChunker, _is_prose_table, _flatten_prose_table_text,
)

# a sectioned SOP laid out as ONE table block (cells joined by ' | ') — the 6-0 shape.
# Must exceed the 500-char blank-form guard to be treated as a real prose table.
PROSE = ("| 1、目的 为确保管理体系的有效运行，明确整个流程中各部门的职责，合理利用调配人力资源，"
         "以使人力资源达到配置合理且满足生产经营需要，特制定本控制程序。 "
         "2、适用范围 适用于本公司各部门的人力资源管理，包括人员编制、招聘、培训、考核与调配。 "
         "3、引用文件 FL/QEM02-01《文件控制程序》 FL/QEM02-02《记录控制程序》 FL/QEM02-08《培训控制程序》。 "
         "4、术语和定义 人力资源：指公司内具有各种不同知识、技能及能力的个人，从事各种工作活动以达到组织目标。 "
         "5、职责 5.1各部门、科、室负责人根据岗位设置情况提出人员配置及培训需求并报人力资源部。 "
         "5.2人力资源部负责编制年度人员需求计划和年度培训计划并组织实施。 "
         "5.3总经理负责批准年度员工需求计划及年度培训计划。 "
         "5.4人力资源部根据相关法律法规进行人力资源管理工作，确保合法合规。 "
         "6、控制过程 6.1人员编制 6.1.1公司每年初进行人员定编，定编按照以岗定人、确保工作量饱满的原则操作。 "
         "6.1.2各部门如需增减人员，须填写人员需求申请表并经审批。 "
         "6.2招聘 6.2.1人力资源部根据批准的需求计划组织内部竞聘或外部招聘。 "
         "6.2.2新员工入职须办理入职手续并进行三级安全培训。 "
         "6.3培训 6.3.1人力资源部按年度培训计划组织培训并保存培训记录。 "
         "7、相关记录 人员需求申请表、培训计划表、培训签到记录表、员工考核表。 |")
DATA = ("| 序号 | 物料名称 | 数量 | 单位 | "
        + " | ".join(f"{i} 钢材{i} {i*10} 吨" for i in range(1, 30)))
BLANK = "| 供方名称 | | 地址 | | 邮编 | | 联系人 | |"


def _blk(text):
    return {"block_type": "table", "text": text, "page_num": 1}


def test_is_prose_table_discriminates():
    assert _is_prose_table(PROSE) is True
    assert _is_prose_table(DATA) is False        # data grid: short cells, no clause prose
    assert _is_prose_table(BLANK) is False       # blank form: < 500 chars


def test_flatten_strips_delimiters_and_keeps_clauses():
    flat = _flatten_prose_table_text(PROSE)
    assert " | " not in flat and "|" not in flat
    assert "1、目的" in flat and "6、控制过程" in flat
    assert flat.count("\n") >= 5                  # top clauses broken to lines


def test_prose_table_yields_chunks_clause_mode():
    out = DocumentChunker(split_mode="clause").chunk_from_blocks([_blk(PROSE)], "D1", "1")
    assert len(out) > 0                           # was 0 before the fix
    assert all(" | " not in (c.chunk_text or "") for c in out)
    assert all(c.chunk_type != "table_chunk" for c in out)


def test_prose_table_yields_chunks_step_mode():
    out = DocumentChunker(split_mode="step").chunk_from_blocks([_blk(PROSE)], "D2", "1")
    assert len(out) > 0


def test_data_table_unchanged():
    out = DocumentChunker(split_mode="clause").chunk_from_blocks([_blk(DATA)], "D3", "1")
    assert any(c.chunk_type == "table_chunk" for c in out)   # NOT flattened


def test_blank_form_not_flattened_to_clauses():
    out = DocumentChunker(split_mode="clause").chunk_from_blocks([_blk(BLANK)], "D4", "1")
    # blank form must not explode into clause chunks (stays as-is / table_chunk)
    assert all(c.chunk_type != "clause_chunk" for c in out)


def test_deterministic_and_no_dupes():
    a = DocumentChunker(split_mode="clause").chunk_from_blocks([_blk(PROSE)], "D5", "1")
    b = DocumentChunker(split_mode="clause").chunk_from_blocks([_blk(PROSE)], "D5", "1")
    ta = [c.chunk_text for c in a]
    tb = [c.chunk_text for c in b]
    assert ta == tb                              # byte-deterministic
    assert len(ta) == len(set(ta))               # no duplicate chunk text


def test_non_table_blocks_passthrough():
    para = {"block_type": "paragraph", "text": "普通段落文本。", "page_num": 1}
    out = DocumentChunker(split_mode="clause")._flatten_prose_tables([para])
    assert out[0] is para                         # untouched identity
