import os
import asyncio
import httpx
from dotenv import load_dotenv
from contextlib import AsyncExitStack
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession
from app import get_rohlik_oauth_token

# Load env variables
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

email = os.getenv("RHL_Mail", "").strip()
password = os.getenv("RHL_Pass", "").strip()

async def main():
    print(f"Acquiring OAuth Bearer token for {email}...")
    token = await get_rohlik_oauth_token(email, password)
    if not token:
        print("Failed to acquire OAuth token!")
        return

    print(f"OAuth Bearer token obtained: {token[:30]}...")

    url = "https://mcp.rohlik.cz/mcp"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("\nConnecting to Rohlik MCP with OAuth Bearer token...")
    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(streamable_http_client(url=url, http_client=http_client))
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                print("Initializing session...")
                await session.initialize()
                print("Session initialized successfully!")
                
                print("Listing tools...")
                tools_response = await session.list_tools()
                print(f"Tools available: {[t.name for t in tools_response.tools]}")
                
                print("\nCalling get_product_details...")
                result = await session.call_tool("get_product_details", {
                    "product_ids": [1431713]
                })
                print("Call result:")
                for content in result.content:
                    if hasattr(content, "text"):
                        print(content.text[:2000])
    except Exception as e:
        print(f"Error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
