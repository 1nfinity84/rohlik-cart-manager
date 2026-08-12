import os
import asyncio
import httpx
from dotenv import load_dotenv
from contextlib import AsyncExitStack
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

# Load env variables
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

email = os.getenv("RHL_Mail").strip()
password = os.getenv("RHL_Pass").strip()

async def get_rohlik_session_cookies():
    url = "https://www.rohlik.cz/services/frontend-service/login"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.rohlik.cz",
        "Origin": "https://www.rohlik.cz"
    }
    payload = {
        "email": email,
        "password": password,
        "name": ""
    }
    print(f"Logging in to Rohlik web...")
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=10.0)
        if response.status_code == 200:
            cookies = []
            for cookie_header in response.headers.get_list("set-cookie"):
                parts = cookie_header.split(";")
                if parts:
                    cookies.append(parts[0].strip())
            return "; ".join(cookies)
    return None

async def main():
    cookie_str = await get_rohlik_session_cookies()
    if not cookie_str:
        print("Failed to get session cookies!")
        return

    print(f"Session cookies obtained successfully: {cookie_str[:60]}...")

    url = "https://mcp.rohlik.cz/mcp"
    headers = {
        "Cookie": cookie_str,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("\nConnecting to Rohlik MCP with ONLY the Cookie header (no rhl-email/pass)...")
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

                # Check if guess image URL works
                guess_url = "https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/https://storage.googleapis.com/rohlik-v2-uploads-store/images/grocery/products/1431713/1431713-1.jpg"
                print(f"\nChecking guess image URL: {guess_url}")
                async with httpx.AsyncClient() as client:
                    img_resp = await client.head(guess_url)
                    print(f"Guess URL Head Status: {img_resp.status_code}")
    except Exception as e:
        print(f"Error occurred: {e}")


if __name__ == "__main__":
    asyncio.run(main())
