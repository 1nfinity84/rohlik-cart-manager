import os
import json
import re
import asyncio
import httpx
from dotenv import load_dotenv
from contextlib import AsyncExitStack
from collections import defaultdict
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

# Load environment variables
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

email = os.getenv("RHL_Mail")
password = os.getenv("RHL_Pass")

if email:
    email = email.strip()
if password:
    password = password.strip()

async def get_rohlik_session_cookies():
    """
    Logs in to Rohlík to obtain session cookies.
    """
    if not email or not password or email == "email@gmail.com":
        print("ERROR: Credentials are not configured in .env file (RHL_Mail / RHL_Pass).")
        return None

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
    
    print(f"Logging in to Rohlík frontend as {email}...")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=12.0)
            if response.status_code == 200:
                cookies = []
                for cookie_header in response.headers.get_list("set-cookie"):
                    parts = cookie_header.split(";")
                    if parts:
                        cookies.append(parts[0].strip())
                if cookies:
                    return "; ".join(cookies)
            print(f"Login failed: status {response.status_code}, response: {response.text[:200]}")
        except Exception as e:
            print(f"Network error during login: {e}")
    return None

def extract_items_from_history(data):
    """
    Recursively searches JSON for product items to capture:
    - ID
    - Name
    - Quantity/Amount
    """
    if isinstance(data, list):
        for item in data:
            yield from extract_items_from_history(item)
    elif isinstance(data, dict):
        name_val = None
        id_val = None
        qty_val = 1
        
        # Match common name keys
        for k in ['name', 'productName', 'itemName', 'title']:
            if k in data and isinstance(data[k], str):
                name_val = data[k]
                break
                
        # Match common ID keys
        for k in ['productId', 'product_id', 'id', 'itemId']:
            if k in data and data[k] is not None:
                id_val = str(data[k])
                break
                
        # Match common quantity keys
        for k in ['quantity', 'amount', 'count']:
            if k in data and isinstance(data[k], (int, float)):
                qty_val = int(data[k])
                break
                
        if name_val and id_val:
            # Yield this product
            yield {"productId": id_val, "name": name_val, "quantity": qty_val}
        else:
            # Recurse deeper into lists or dictionaries
            for key in ['orders', 'items', 'products', 'orderItems']:
                if key in data:
                    yield from extract_items_from_history(data[key])
            for key, val in data.items():
                if key not in ['orders', 'items', 'products', 'orderItems']:
                    yield from extract_items_from_history(val)

async def main():
    cookie_header = await get_rohlik_session_cookies()
    if not cookie_header:
        print("Could not obtain session cookies. Exiting.")
        return

    mcp_url = "https://mcp.rohlik.cz/mcp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie_header
    }

    limit = 50
    print(f"Connecting to Rohlik MCP and retrieving last {limit} orders...")
    
    product_totals = defaultdict(int)      # Total quantity purchased
    product_orders_count = defaultdict(int) # Number of orders this product appeared in
    product_names = {}                      # Map productId -> most recent name

    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(
                streamable_http_client(url=mcp_url, http_client=http_client)
            )
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # Call fetch_orders
                history_result = await session.call_tool("fetch_orders", {
                    "limit": limit,
                    "order_type": "delivered"
                })
                
                history_text = ""
                for content in history_result.content:
                    if hasattr(content, "text"):
                        history_text += content.text
                
                if getattr(history_result, "isError", False) or getattr(history_result, "is_error", False):
                    print(f"MCP Tool Error: {history_text}")
                    return
                
                # Attempt to parse history JSON
                try:
                    data = json.loads(history_text)
                except json.JSONDecodeError:
                    print("Failed to parse history response as JSON.")
                    print("Raw output sample:")
                    print(history_text[:1000])
                    return
                
                # Check for standard error response structure
                if isinstance(data, dict) and data.get("success") is False:
                    print(f"API Error in history: {data.get('message', 'Unknown error')}")
                    return

                # Traverse and count
                all_items = list(extract_items_from_history(data))
                if not all_items:
                    print("No products found in order history. Try adjusting order count/type.")
                    return

                for item in all_items:
                    pid = item["productId"]
                    name = item["name"].strip()
                    qty = item["quantity"]
                    
                    product_totals[pid] += qty
                    product_orders_count[pid] += 1
                    product_names[pid] = name  # Kept last matched name

        # Sort products by total quantity descending
        sorted_products = sorted(product_totals.items(), key=lambda x: x[1], reverse=True)
        top_20 = sorted_products[:20]

        print("\n" + "="*80)
        print(f" TOP 20 MOST PURCHASED ITEMS FROM LAST {limit} ORDERS")
        print("="*80)
        print(f" {'#':<3} | {'Product Name':<42} | {'Product ID':<10} | {'Total Qty':<9} | {'Orders':<6}")
        print("-"*80)
        
        for idx, (pid, total_qty) in enumerate(top_20, 1):
            name = product_names[pid]
            if len(name) > 40:
                name = name[:39] + "…"
            orders = product_orders_count[pid]
            print(f" {idx:<3} | {name:<42} | {pid:<10} | {total_qty:<9} | {orders:<6}")
        print("="*80 + "\n")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
