## @file test_tool.py
#  Tool 单元测试
#  职责：纯单元（临时目录 + 临时文件，不碰 git），验证
#  load_task（JSON -> Task / 覆盖 / 缺字段 / 非法 api_type / 文件缺失）、
#  load_knowledge_base（版本排序 / 赋值行与定义行解析）、_parse_record_line、
#  _extract_version。

import json

import pytest

from Tool.tool import (
    _extract_version,
    _parse_record_line,
    load_knowledge_base,
    load_task,
)
from Tool.model import Task


# ---------------------------------------------------------------------------
# load_task
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "libName": "pandas",
    "sourceVersion": "0.19.0",
    "targetVersion": "0.20.0",
    "oldApiFqn": "pandas.DataFrame.sort",
    "apiType": "method",
    "topK": 5,
    "libRepoPath": "/tmp/pandas",
}


def _write_config(tmp_path, data):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return str(path)


def test_load_task_basic(tmp_path):
    """完整配置 -> Task 字段一一对应。"""
    path = _write_config(tmp_path, _BASE_CONFIG)
    task = load_task(path)
    assert isinstance(task, Task)
    assert task.lib_name == 'pandas'
    assert task.source_version == '0.19.0'
    assert task.target_version == '0.20.0'
    assert task.old_api_fqn == 'pandas.DataFrame.sort'
    assert task.api_type == 'method'
    assert task.top_k == 5
    assert task.lib_repo_path == '/tmp/pandas'


def test_load_task_overrides(tmp_path):
    """CLI 覆盖（snake_case key）覆盖 JSON 字段。"""
    path = _write_config(tmp_path, _BASE_CONFIG)
    task = load_task(path, overrides={'source_version': '0.18.0', 'top_k': 8})
    assert task.source_version == '0.18.0'
    assert task.top_k == 8


def test_load_task_default_topk(tmp_path):
    """缺 topK 时默认 3。"""
    data = dict(_BASE_CONFIG)
    del data['topK']
    path = _write_config(tmp_path, data)
    assert load_task(path).top_k == 3


def test_load_task_missing_field(tmp_path):
    """缺必填字段 -> ValueError。"""
    data = dict(_BASE_CONFIG)
    del data['oldApiFqn']
    path = _write_config(tmp_path, data)
    with pytest.raises(ValueError):
        load_task(path)


def test_load_task_invalid_api_type(tmp_path):
    """apiType 非法 -> ValueError。"""
    data = dict(_BASE_CONFIG)
    data['apiType'] = 'field'
    path = _write_config(tmp_path, data)
    with pytest.raises(ValueError):
        load_task(path)


def test_load_task_missing_file(tmp_path):
    """配置文件不存在 -> FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_task(str(tmp_path / 'nope.json'))


# ---------------------------------------------------------------------------
# load_knowledge_base / 行解析
# ---------------------------------------------------------------------------

def test_parse_record_line_assign():
    """赋值行（A:）-> alias_of 非空。"""
    rec = _parse_record_line('A: pandas.DataFrame.sort->pandas.DataFrame.sort_values()')
    assert rec is not None
    assert rec.fqn == 'pandas.DataFrame.sort'
    assert rec.alias_of == 'pandas.DataFrame.sort_values()'
    assert rec.signature == ''


def test_parse_record_line_definition():
    """定义行 -> signature 从 '(' 起。"""
    rec = _parse_record_line('pandas.DataFrame.sort_values(by)->None')
    assert rec is not None
    assert rec.fqn == 'pandas.DataFrame.sort_values'
    assert rec.signature == '(by)->None'
    assert rec.alias_of is None


def test_parse_record_line_section_marker():
    """分节标记 / 空行 -> None。"""
    assert _parse_record_line('----------------------------------------x.py----------------------------------------') is None
    assert _parse_record_line('') is None


def test_extract_version():
    """两种文件名约定均可提取版本。"""
    assert _extract_version('pandas0.19.0', 'pandas') == '0.19.0'
    assert _extract_version('0.19.0.json', 'pandas') == '0.19.0'
    assert _extract_version('notes.txt', 'pandas') is None


def test_load_knowledge_base(tmp_path):
    """多版本文件 -> versions 升序，records 含赋值行与定义行。"""
    kb_dir = tmp_path / 'kb'
    lib_dir = kb_dir / 'pandas'
    lib_dir.mkdir(parents=True)
    (lib_dir / 'pandas0.20.0').write_text(
        'pandas.DataFrame.sort_values(by)->None\n', encoding='utf-8')
    (lib_dir / 'pandas0.19.0').write_text(
        'A: pandas.DataFrame.sort->pandas.DataFrame.sort_values()\n'
        'pandas.DataFrame.sort_values(by)->None\n', encoding='utf-8')

    kb = load_knowledge_base('pandas', str(kb_dir))
    assert kb.lib_name == 'pandas'
    assert kb.versions == ['0.19.0', '0.20.0']
    assert kb.exists('pandas.DataFrame.sort', '0.19.0')
    assert kb.get('pandas.DataFrame.sort', '0.19.0').alias_of == \
        'pandas.DataFrame.sort_values()'
    assert not kb.exists('pandas.DataFrame.sort', '0.20.0')


def test_load_knowledge_base_missing_dir(tmp_path):
    """知识库目录不存在 -> FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_knowledge_base('pandas', str(tmp_path / 'none'))
