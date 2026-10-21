import asyncio
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from fastmcp import Client

from vision_desktops.server import mcp
from vision_desktops import state

CAPTURE_DIR = Path.home() / ".vision-desktops" / "captures"


async def amain() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "list-tools"):
        async with Client(mcp) as client:
            for t in await client.list_tools():
                print(t.name)
        return 0
    tool, raw = args[0], args[1] if len(args) > 1 else "{}"
    tool_args = json.loads(Path(raw[1:]).read_text(encoding="utf-8")) if raw.startswith("@") else json.loads(raw)
    async with Client(mcp) as client:
        result = await client.call_tool(tool, tool_args)
        for i, block in enumerate(result.content):
            if getattr(block, "type", None) == "image":
                CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
                out = CAPTURE_DIR / f"harness-{datetime.now().strftime('%H%M%S-%f')[:-3]}-{i}.jpg"
                out.write_bytes(base64.b64decode(block.data))
                print(f"[image saved: {out}]")
                state.prune_captures()
            elif hasattr(block, "text"):
                print(block.text)
        return 1 if getattr(result, "is_error", False) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
