import pytest

from orchestrator.tools.api_call import ApiCallInput, SPEC as API_SPEC, handle as api_handle
from orchestrator.tools.base import ToolContext, ToolExecutionError
from orchestrator.tools.code_exec import CodeExecInput, handle as code_handle
from orchestrator.tools.db_query import DbQueryInput, handle as db_handle
from orchestrator.tools.file_io import (
    FileReadInput,
    FileWriteInput,
    handle_read,
    handle_write,
)
from orchestrator.tools.web_search import WebSearchInput, handle as search_handle


@pytest.fixture()
def ctx(tmp_path):
    return ToolContext(task_id="t1", specialist="analysis", workspace=tmp_path / "ws")


def test_file_write_then_read_roundtrip(ctx):
    written = handle_write(FileWriteInput(path="notes/a.txt", content="hello"), ctx)
    assert written.bytes_written == 5
    read = handle_read(FileReadInput(path="notes/a.txt"), ctx)
    assert read.content == "hello"


def test_file_path_traversal_is_blocked(ctx):
    with pytest.raises(ToolExecutionError, match="escapes the task workspace"):
        handle_write(FileWriteInput(path="../evil.txt", content="x"), ctx)


def test_file_read_missing_file(ctx):
    with pytest.raises(ToolExecutionError, match="does not exist"):
        handle_read(FileReadInput(path="nope.txt"), ctx)


def test_code_exec_subprocess_runs_python(ctx):
    result = code_handle(CodeExecInput(code="print(6 * 7)"), ctx)
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"


def test_code_exec_times_out(ctx):
    with pytest.raises(ToolExecutionError, match="timed out"):
        code_handle(CodeExecInput(code="import time; time.sleep(5)", timeout_s=1), ctx)


def test_db_query_rejects_writes(ctx):
    with pytest.raises(ToolExecutionError, match="read-only"):
        db_handle(DbQueryInput(sql="DELETE FROM demo.vector_db_stats"), ctx)


def test_db_query_rejects_multi_statement(ctx):
    with pytest.raises(ToolExecutionError, match="single SQL statement"):
        db_handle(DbQueryInput(sql="SELECT 1; SELECT 2"), ctx)


def test_api_call_rejects_non_allowlisted_host(ctx):
    with pytest.raises(ToolExecutionError, match="not in the API allowlist"):
        api_handle(ApiCallInput(method="GET", url="https://evil.example.org/x"), ctx)


def test_api_call_mock_response_for_allowlisted_host(ctx):
    result = api_handle(ApiCallInput(method="GET", url="https://api.github.com/repos"), ctx)
    assert result.status_code == 200
    assert "[mock]" in result.body


def test_api_call_post_is_sensitive_get_is_not():
    assert API_SPEC.is_sensitive(ApiCallInput(method="POST", url="https://api.github.com/x"))
    assert not API_SPEC.is_sensitive(ApiCallInput(method="GET", url="https://api.github.com/x"))


def test_web_search_mock_returns_canned_results(ctx):
    output = search_handle(WebSearchInput(query="chroma db"), ctx)
    assert output.results[0].snippet == "[mock] result for chroma db"
