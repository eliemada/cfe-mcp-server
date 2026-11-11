import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def test_stdio_server_lists_tools_and_reports_missing_session(tmp_path):
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("cfe-mcp")),
        env={"CFE_DATA_DIR": str(tmp_path)},
    )
    async with Client(parameters) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "cfe_session_status",
            "cfe_list_reimbursements",
            "cfe_list_requests",
            "cfe_claim_options",
            "cfe_start_login",
            "cfe_finish_login",
            "cfe_mobile_session_status",
            "cfe_mobile_start_login",
            "cfe_mobile_finish_login",
            "cfe_prepare_claim",
            "cfe_care_schema",
            "cfe_get_claim",
            "cfe_list_claims",
            "cfe_update_claim",
            "cfe_add_attachment",
            "cfe_validate_claim",
            "cfe_upload_claim_attachments",
            "cfe_remove_attachment",
            "cfe_submit_claim",
            "cfe_bank_details",
            "cfe_prepare_bank_change",
            "cfe_get_bank_change",
            "cfe_list_bank_changes",
            "cfe_submit_bank_change",
            "cfe_prepare_review",
            "cfe_messaging_options",
            "cfe_list_messages",
            "cfe_import_claim_as_review",
            "cfe_get_review",
            "cfe_list_reviews",
            "cfe_update_review",
            "cfe_add_review_attachment",
            "cfe_remove_review_attachment",
            "cfe_validate_review",
            "cfe_export_review",
            "cfe_submit_review",
        } <= names
        result = await client.call_tool("cfe_session_status", {})
        assert result.structured_content["authenticated"] is False
        submit = next(tool for tool in tools.tools if tool.name == "cfe_submit_claim")
        assert submit.annotations.read_only_hint is False
        assert submit.annotations.idempotent_hint is True
        review_submit = next(tool for tool in tools.tools if tool.name == "cfe_submit_review")
        assert review_submit.annotations.read_only_hint is False
        assert review_submit.annotations.idempotent_hint is True
        review_get = next(tool for tool in tools.tools if tool.name == "cfe_get_review")
        assert "refresh" in review_get.input_schema["properties"]
        bank_submit = next(tool for tool in tools.tools if tool.name == "cfe_submit_bank_change")
        assert bank_submit.annotations.read_only_hint is False
        assert bank_submit.annotations.idempotent_hint is True
        bank_prepare = next(tool for tool in tools.tools if tool.name == "cfe_prepare_bank_change")
        assert "proof_path" in bank_prepare.input_schema["required"]
        result = await client.call_tool("cfe_bank_details", {})
        assert result.is_error
        result = await client.call_tool("cfe_mobile_session_status", {})
        assert result.structured_content["authenticated"] is False
