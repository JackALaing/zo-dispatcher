CHANNEL_RETRY_DELAYS = [5, 15, 30]

# Builtin channels delivered via MCP tool calls.
# Maps channel name -> (mcp_tool_name, payload_builder(title, content) -> dict)
BUILTIN_CHANNELS = {
    "sms": ("send_sms_to_user", lambda t, c: {"message": f"**{t}**\n\n{c}"}),
    "email": ("send_email_to_user", lambda t, c: {"subject": t, "markdown_body": c}),
    "telegram": ("send_telegram_message", lambda t, c: {"message": f"**{t}**\n\n{c}"}),
}

MCP_URL = "https://api.zo.computer/mcp"
