#!/usr/bin/env python3
import os
import sys
import sqlite3
import json
from datetime import datetime

DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")

def format_timestamp(ts_ms):
    if not ts_ms:
        return "Unknown"
    return datetime.fromtimestamp(ts_ms / 1000.0).strftime('%Y-%m-%d %H:%M:%S')

def main():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get the latest session
    cursor.execute("""
        SELECT id, title, time_created, time_updated 
        FROM session 
        ORDER BY time_created DESC 
        LIMIT 1;
    """)
    session_row = cursor.fetchone()
    if not session_row:
        print("Error: No sessions found in database.", file=sys.stderr)
        sys.exit(1)

    session_id, title, time_created, time_updated = session_row
    print(f"Caching conversation from session: '{title}' ({session_id})")

    # Fetch all messages in the session
    cursor.execute("""
        SELECT id, data, time_created 
        FROM message 
        WHERE session_id = ? 
        ORDER BY time_created ASC;
    """, (session_id,))
    messages = cursor.fetchall()

    md_lines = []
    md_lines.append(f"# OpenCode Conversation Cache")
    md_lines.append(f"**Session Title:** {title}")
    md_lines.append(f"**Session ID:** `{session_id}`")
    md_lines.append(f"**Started At:** {format_timestamp(time_created)}")
    md_lines.append(f"**Last Updated:** {format_timestamp(time_updated)}")
    md_lines.append("\n---\n")

    for msg_id, msg_data_str, msg_time in messages:
        try:
            msg_data = json.loads(msg_data_str)
        except Exception:
            msg_data = {}

        role = msg_data.get("role", "unknown").upper()
        agent = msg_data.get("agent", "")
        model = msg_data.get("modelID", "")
        
        md_lines.append(f"## [{role}] - {format_timestamp(msg_time)}")
        if model:
            md_lines.append(f"*Model: {model}*")
        md_lines.append("")

        # Fetch parts for this message
        cursor.execute("""
            SELECT data, time_created 
            FROM part 
            WHERE message_id = ? 
            ORDER BY time_created ASC;
        """, (msg_id,))
        parts = cursor.fetchall()

        for part_row in parts:
            try:
                part_data = json.loads(part_row[0])
            except Exception:
                continue

            part_type = part_data.get("type")

            if part_type == "text":
                text = part_data.get("text", "").strip()
                if text:
                    if part_data.get("synthetic"):
                        md_lines.append(f"> **System Activity:** {text}\n")
                    else:
                        md_lines.append(f"{text}\n")

            elif part_type == "reasoning":
                reasoning = part_data.get("text", "").strip()
                if reasoning:
                    md_lines.append("<details>")
                    md_lines.append("<summary><b>Assistant Thought Process</b></summary>\n")
                    md_lines.append(reasoning)
                    md_lines.append("\n</details>\n")

            elif part_type == "tool":
                tool_name = part_data.get("tool", "unknown")
                state = part_data.get("state", {})
                status = state.get("status", "unknown")
                
                # Format tool input
                tool_input = state.get("input", "")
                if isinstance(tool_input, (dict, list)):
                    tool_input_str = json.dumps(tool_input, indent=2)
                else:
                    tool_input_str = str(tool_input)

                # Format tool output
                tool_output = state.get("output", "")
                if isinstance(tool_output, (dict, list)):
                    tool_output_str = json.dumps(tool_output, indent=2)
                else:
                    tool_output_str = str(tool_output)

                # Truncate output if extremely long to keep markdown readable
                if len(tool_output_str) > 10000:
                    tool_output_str = tool_output_str[:10000] + "\n... [Output Truncated due to size] ..."

                md_lines.append(f"### Tool Call: `{tool_name}` (Status: {status})")
                md_lines.append("**Input:**")
                md_lines.append(f"```json\n{tool_input_str}\n```")
                if tool_output_str:
                    md_lines.append("**Output:**")
                    md_lines.append(f"```text\n{tool_output_str}\n```")
                md_lines.append("")

            elif part_type == "file":
                filepath = part_data.get("filename", part_data.get("url", ""))
                md_lines.append(f"*Linked File/Resource:* `{filepath}`\n")

        md_lines.append("\n---\n")

    # Save to file
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "opencode_convo_cache.md")
    
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"Success! Cached conversation written to {out_path}")

if __name__ == "__main__":
    main()
