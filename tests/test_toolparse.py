from wrench.toolparse import extract_bash_block, extract_text_tool_calls, parse_arguments

SCHEMAS = {"read_file": {"properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}}},
           "edit_file": {"properties": {"path": {"type": "string"}, "old_str": {"type": "string"},
                                        "new_str": {"type": "string"}, "replace_all": {"type": "boolean"}}}}


def test_repairs_trailing_comma_and_truncation():
    assert parse_arguments('{"path": "a.py",}') == ({"path": "a.py"}, None)
    args, err = parse_arguments('{"command": "ls -la')
    assert err is None and args == {"command": "ls -la"}


def test_unwraps_arguments_wrapper_and_fences():
    assert parse_arguments('```json\n{"name": "bash", "arguments": {"command": "ls"}}\n```')[0] == {"command": "ls"}


def test_reports_unparseable_arguments():
    args, err = parse_arguments("not json at all")
    assert args == {} and "could not parse" in err


def test_qwen_xml_keeps_code_whitespace_and_coerces_types():
    text = ("I'll fix it.\n<tool_call>\n<function=edit_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n"
            "<parameter=old_str>\n    return x\n</parameter>\n<parameter=new_str>\n    return x + 1\n</parameter>\n"
            "<parameter=replace_all>\nfalse\n</parameter>\n</function>\n</tool_call>")
    calls, cleaned = extract_text_tool_calls(text, SCHEMAS)
    assert len(calls) == 1
    name, args, err = calls[0]
    assert name == "edit_file" and err is None
    assert args["old_str"] == "    return x" and args["new_str"] == "    return x + 1"
    assert args["replace_all"] is False
    assert cleaned == "I'll fix it."


def test_deepseek_dsml_leak():
    text = ('<｜DSML｜tool_calls><｜DSML｜invoke name="read_file"><｜DSML｜parameter name="path" string="true">'
            'pkg/mod.py</｜DSML｜parameter><｜DSML｜parameter name="start_line" string="false">40'
            '</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>')
    calls, cleaned = extract_text_tool_calls(text, SCHEMAS)
    assert calls == [("read_file", {"path": "pkg/mod.py", "start_line": 40}, None)]
    assert cleaned == ""


def test_deepseek_legacy_tokens():
    text = ('<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n```json\n'
            '{"path": "x.py"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>')
    calls, _ = extract_text_tool_calls(text, SCHEMAS)
    assert calls == [("read_file", {"path": "x.py"}, None)]


def test_json_inside_tool_call_tags():
    calls, _ = extract_text_tool_calls('<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>')
    assert calls == [("read_file", {"path": "a"}, None)]


def test_ignores_tool_syntax_inside_code_fences():
    text = "Example of the format:\n```\n<tool_call>\n<function=read_file>\n</function>\n</tool_call>\n```"
    assert extract_text_tool_calls(text)[0] == []


def test_bash_block_fallback():
    assert extract_bash_block("run this:\n```bash\npytest -q\n```") == "pytest -q"
