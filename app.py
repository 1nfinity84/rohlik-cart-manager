import os
import json
import re
import asyncio
import uuid
import logging
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
import httpx
from contextlib import AsyncExitStack
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

class BusinessLogicError(Exception):
    """Exception raised for business logic errors (e.g., items out of stock) that should not trigger cookie retries."""
    pass

def clean_availability_message(msg):
    if not msg:
        return "Položka není skladem nebo ji nelze přidat."
    msg_lower = msg.lower()
    if "availability issues" in msg_lower or "not added due to availability" in msg_lower:
        match = re.search(r'\[(.*?)\]', msg)
        if match:
            items = match.group(1).replace("'", "").replace('"', "")
            return f"Položka není skladem: {items}"
        return "Položka není skladem."
    elif "not found in your rohlik order history" in msg_lower:
        return "Položka nebyla nalezena ve vaší historii nákupů."
    return msg

def get_original_exception(e):
    if hasattr(e, "exceptions") and e.exceptions:
        return get_original_exception(e.exceptions[0])
    return e

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask App
app = Flask(__name__, template_folder='templates')

# Load env variables from the current directory
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

PRODUCTS_FILE = os.path.join(base_dir, 'products.json')
COOKIES_CACHE_FILE = os.path.join(base_dir, '.session_cookies')
_invalid_manual_cookie = None

def load_cached_cookies():
    if os.path.exists(COOKIES_CACHE_FILE):
        try:
            with open(COOKIES_CACHE_FILE, 'r', encoding='utf-8') as f:
                cookie_str = f.read().strip()
                if cookie_str:
                    return cookie_str
        except Exception as e:
            logger.error(f"Error loading cached cookies: {e}")
    return None

def save_cached_cookies(cookie_str):
    try:
        with open(COOKIES_CACHE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie_str)
    except Exception as e:
        logger.error(f"Error saving cached cookies: {e}")

def clear_cached_cookies(cookie_to_invalidate=None):
    global _invalid_manual_cookie
    if cookie_to_invalidate:
        manual_cookie = os.getenv("RHL_Cookie")
        if manual_cookie and manual_cookie.strip() == cookie_to_invalidate.strip():
            _invalid_manual_cookie = cookie_to_invalidate.strip()
            logger.warning("Manual RHL_Cookie from .env was marked as invalid in memory.")
    try:
        if os.path.exists(COOKIES_CACHE_FILE):
            os.remove(COOKIES_CACHE_FILE)
            logger.info("Cleared cached session cookies.")
    except Exception as e:
        logger.error(f"Error clearing cached cookies: {e}")

def load_products():
    if os.path.exists(PRODUCTS_FILE):
        try:
            with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading products.json: {e}")
            return []
    return []

def save_products(products):
    try:
        with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(products, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving products.json: {e}")

def scrape_rohlik_product_details(url_or_id):
    """
    Given a Rohlik URL or Product ID, extracts:
    - Product ID
    - Product Name
    - Image URL
    """
    product_id = None
    if not url_or_id:
        return None, None, None
        
    url_or_id = str(url_or_id).strip()
    
    # 1. Extract product ID
    if url_or_id.isdigit():
        product_id = url_or_id
    else:
        # Match something like rohlik.cz/123456 or rohlik.cz/p123456
        match = re.search(r'rohlik\.cz/(?:p)?(\d+)', url_or_id)
        if match:
            product_id = match.group(1)
            
    if not product_id:
        return None, None, None

    # Construct the canonical URL for scraping
    scrape_url = f"https://www.rohlik.cz/{product_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    name = None
    image_url = None
    
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=5.0) as client:
            resp = client.get(scrape_url)
            if resp.status_code == 200:
                # Scrape og:image
                img_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', resp.text)
                if not img_match:
                    img_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', resp.text)
                if img_match:
                    image_url = img_match.group(1)
                    
                # Scrape og:title for default product name
                title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', resp.text)
                if not title_match:
                    title_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', resp.text)
                if title_match:
                    name = title_match.group(1)
                    # Clean up Rohlik suffix from title if present (e.g. "| Rohlik.cz")
                    name = re.sub(r'\s*\|\s*Rohlik\.cz.*$', '', name, flags=re.IGNORECASE).strip()
    except Exception as e:
        logger.error(f"Error scraping Rohlik product details for ID {product_id}: {e}")
        
    return product_id, name, image_url

def scrape_rohlik_search_image(search_term):
    """
    Queries Rohlik's internal search metadata API to find the first matching product image.
    """
    if not search_term:
        return None
        
    import urllib.parse
    encoded_term = urllib.parse.quote(search_term.strip())
    url = f"https://www.rohlik.cz/services/frontend-service/search-metadata?search={encoded_term}&offset=0&limit=3&companyId=1&canCorrect=true"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=5.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("data", {}).get("productList", [])
                if products:
                    img_path = products[0].get("imgPath")
                    if img_path:
                        # Clean double slashes if present
                        if img_path.startswith("/"):
                            img_url = f"https://cdn.rohlik.cz{img_path}"
                        else:
                            img_url = f"https://cdn.rohlik.cz/{img_path}"
                        return f"https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/{img_url}"
    except Exception as e:
        logger.error(f"Error scraping search image for '{search_term}': {e}")
    return None

def get_credentials():
    email = os.getenv("RHL_Mail")
    password = os.getenv("RHL_Pass")
    if not email or not password or email == "email@gmail.com" or password == "yourpassword":
        return None, None
    email = email.strip()
    password = password.strip()

    obs_email = email[0] + "***" + email[email.find("@")-1:] if "@" in email else "***"
    logger.info(f"Credentials loaded: Email='{obs_email}'")
    return email, password

def extract_products_from_json(data):
    """
    Recursively traverses JSON data to extract list of product-like objects
    with name/title and id/productId.
    """
    if isinstance(data, list):
        for item in data:
            yield from extract_products_from_json(item)
    elif isinstance(data, dict):
        name_val = None
        id_val = None
        
        # Look for product name/title fields
        for k in ['name', 'title', 'productName', 'itemName']:
            if k in data and isinstance(data[k], str):
                name_val = data[k]
                break
                
        # Look for product ID fields
        for k in ['productId', 'product_id', 'id', 'itemId']:
            if k in data and data[k] is not None:
                id_val = str(data[k])
                break
                
        if name_val and id_val:
            yield id_val, name_val
        else:
            # Prioritize searching nested structures representing list of items
            for key in ['orders', 'items', 'products']:
                if key in data:
                    yield from extract_products_from_json(data[key])
            for key, val in data.items():
                if key not in ['orders', 'items', 'products']:
                    yield from extract_products_from_json(val)

def find_product_in_history(history_text, search_term):
    """
    Parses the response from get_order_history (JSON or Text) to find
    the product matching the search term.
    """
    search_term_lower = search_term.lower().strip()
    
    # 1. Try parsing history as JSON
    try:
        data = json.loads(history_text)
        for prod_id, prod_name in extract_products_from_json(data):
            if search_term_lower in prod_name.lower():
                logger.info(f"JSON MATCH: Found product '{prod_name}' with ID {prod_id}")
                return prod_id, prod_name
    except json.JSONDecodeError:
        logger.warning("Failed to parse history as JSON, falling back to text regex search.")
    
    # 2. Try parsing history as text (Regex fallback)
    lines = history_text.split('\n')
    for line in lines:
        if search_term_lower in line.lower():
            # Match 5-10 digit sequences as product ID
            match = re.search(r'\b\d{5,10}\b', line)
            if match:
                logger.info(f"TEXT MATCH (digits): Found match in line '{line.strip()}' -> ID: {match.group(0)}")
                return match.group(0), line.strip()
            
            # Match "id": "12345" or similar
            match = re.search(r'(?:id|productId|product_id)[:"\s]+(\d+)', line, re.IGNORECASE)
            if match:
                logger.info(f"TEXT MATCH (regex): Found match in line '{line.strip()}' -> ID: {match.group(1)}")
                return match.group(1), line.strip()
                
    return None, None

async def get_rohlik_session_cookies(email, password, force_refresh=False):
    """
    Directly logs in to the public Rohlik frontend service to obtain
    valid PHPSESSION and JSESSIONID session cookies. Uses cached cookies if available.
    """
    # 1. If not forcing a refresh, try to load cached cookies first
    if not force_refresh:
        cached_cookies = load_cached_cookies()
        if cached_cookies:
            logger.info("Using cached session cookies.")
            return cached_cookies

    # 2. Check if we have a manual cookie override in .env (only if not forcing a refresh)
    if not force_refresh:
        manual_cookie = os.getenv("RHL_Cookie")
        if manual_cookie and manual_cookie.strip():
            if manual_cookie.strip() == _invalid_manual_cookie:
                logger.warning("Manual RHL_Cookie in .env is marked as invalid in memory; ignoring override.")
            else:
                logger.info("Using manual RHL_Cookie override from .env")
                return manual_cookie.strip()

    # 3. Otherwise, fetch new ones
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
    logger.info(f"Retrieving fresh Rohlík session cookies for {email}...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if response.status_code == 200:
                cookies = []
                for cookie_header in response.headers.get_list("set-cookie"):
                    parts = cookie_header.split(";")
                    if parts:
                        cookies.append(parts[0].strip())
                if cookies:
                    cookie_str = "; ".join(cookies)
                    logger.info("Successfully fetched session cookies.")
                    save_cached_cookies(cookie_str)
                    return cookie_str
            logger.error(f"Failed to fetch session cookies: status {response.status_code}, response: {response.text[:200]}")
    except Exception as e:
        logger.error(f"Error fetching session cookies: {e}")
    return None

async def call_rohlik_mcp_to_add(email, password, product, is_retry=False):
    """
    Connects to the Rohlik.cz MCP server via Streamable HTTP transport.
    This is the same transport that Claude CLI uses (--transport http).
    Pure Python, no Node.js needed.
    """
    url = "https://mcp.rohlik.cz/mcp"
    logger.info(f"Connecting to Rohlik MCP via Streamable HTTP for user {email} (is_retry={is_retry})")

    # Fetch fresh session cookies from frontend login
    cookie_header = await get_rohlik_session_cookies(email, password, force_refresh=is_retry)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        logger.info("Attached session cookies to MCP connection headers.")
    else:
        headers["rhl-email"] = email
        headers["rhl-pass"] = password
        logger.warning("Session cookie retrieval failed; falling back to raw credential headers.")

    async def log_request(req):
        safe_hdrs = {}
        for k, v in req.headers.items():
            if k.lower() == "rhl-pass":
                safe_hdrs[k] = "".join(c if not c.isalnum() else "*" for c in v)
            elif k.lower() == "cookie":
                # Redact cookie values for security
                cookies = []
                for cookie in v.split(";"):
                    parts = cookie.split("=")
                    if len(parts) == 2:
                        cookies.append(f"{parts[0].strip()}=*********")
                    else:
                        cookies.append("*********")
                safe_hdrs[k] = "; ".join(cookies)
            else:
                safe_hdrs[k] = v
        logger.info(f"HTTP Outgoing Request: {req.method} {req.url} - Headers: {safe_hdrs}")

    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers, event_hooks={"request": [log_request]})
            )
            streams = await stack.enter_async_context(streamable_http_client(url=url, http_client=http_client))
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                logger.info("Initializing MCP Client Session via Streamable HTTP")
                await session.initialize()
                logger.info("MCP session initialized successfully")
                
                # List and log all available tools and their schemas
                try:
                    tools_response = await session.list_tools()
                    logger.info(f"AVAILABLE TOOLS on Rohlik MCP: {[t.name for t in tools_response.tools]}")
                    for t in tools_response.tools:
                        if t.name in ['fetch_orders', 'add_items_to_cart']:
                            logger.info(f"TOOL SCHEMA for {t.name}: {t.inputSchema}")
                except Exception as te:
                    logger.error(f"Failed to list tools: {te}")
                    
                # Retrieve product info
                p_type = int(product.get("type", 1))
                quantity = int(product.get("quantity", 1))
                
                if p_type == 1:
                    # Type 1: History Lookup
                    search_term = product.get("searchTerm", "")
                    logger.info(f"Action: Type 1 History Search for term: '{search_term}'")
                    
                    # Fetch order history using the correct 'fetch_orders' tool
                    history_result = await session.call_tool("fetch_orders", {
                        "limit": 15,
                        "order_type": "delivered"
                    })
                    
                    # Consolidate response text
                    history_text = ""
                    for content in history_result.content:
                        if hasattr(content, "text"):
                            history_text += content.text
                    
                    if getattr(history_result, "isError", False) or getattr(history_result, "is_error", False):
                        raise ValueError(f"Failed to fetch order history: {history_text}")
                    
                    # Search for matching product
                    logger.info(f"Retrieved history content (first 1500 chars): {history_text[:1500]}")
                    matched_id, matched_name = find_product_in_history(history_text, search_term)
                    if not matched_id:
                        raise BusinessLogicError(f"Product matching '{search_term}' was not found in your Rohlik order history.")
                    
                    logger.info(f"Adding history-matched product: '{matched_name}' (ID: {matched_id}) with quantity {quantity}")
                    
                    # Add to cart using the correct 'add_items_to_cart' tool
                    add_result = await session.call_tool("add_items_to_cart", {
                        "items": [
                            {
                                "productId": int(matched_id),
                                "quantity": int(quantity)
                            }
                        ]
                    })
                    
                    # Get add to cart message
                    logger.info(f"add_items_to_cart raw result: {add_result}")
                    add_message = ""
                    for content in add_result.content:
                        if hasattr(content, "text"):
                            add_message += content.text
                            
                    if getattr(add_result, "isError", False) or getattr(add_result, "is_error", False):
                        raise ValueError(f"Failed to add items to Rohlik cart: {add_message}")
                        
                    # Inspect response for availability/add failures
                    try:
                        res_json = json.loads(add_message)
                        if isinstance(res_json, dict):
                            failed_ids = [str(x) for x in res_json.get("items_failed_to_add", [])]
                            if str(matched_id) in failed_ids or not res_json.get("success", True):
                                failed_msg = res_json.get("items_failed_message") or res_json.get("message") or "Položka není skladem nebo ji nelze přidat."
                                raise BusinessLogicError(failed_msg)
                    except json.JSONDecodeError:
                        pass
                            
                    return {
                        "success": True,
                        "matchedId": matched_id,
                        "matchedName": matched_name,
                        "details": add_message or f"Successfully added {quantity}x '{matched_name}' to cart."
                    }
                    
                elif p_type == 2:
                    # Type 2: Fixed Product ID
                    product_id = product.get("productId", "")
                    logger.info(f"Action: Type 2 Fixed ID cart add: ID={product_id}, Qty={quantity}")
                    
                    if not product_id:
                        raise ValueError("Product ID cannot be empty for Type 2 (Fixed ID) products.")
                    
                    # Add to cart directly using the correct 'add_items_to_cart' tool
                    add_result = await session.call_tool("add_items_to_cart", {
                        "items": [
                            {
                                "productId": int(product_id),
                                "quantity": int(quantity)
                            }
                        ]
                    })
                    
                    # Get add to cart message
                    logger.info(f"add_items_to_cart raw result (Type 2): {add_result}")
                    add_message = ""
                    for content in add_result.content:
                        if hasattr(content, "text"):
                            add_message += content.text
                            
                    if getattr(add_result, "isError", False) or getattr(add_result, "is_error", False):
                        raise ValueError(f"Failed to add items to Rohlik cart: {add_message}")
                        
                    # Inspect response for availability/add failures
                    try:
                        res_json = json.loads(add_message)
                        if isinstance(res_json, dict):
                            failed_ids = [str(x) for x in res_json.get("items_failed_to_add", [])]
                            if str(product_id) in failed_ids or not res_json.get("success", True):
                                failed_msg = res_json.get("items_failed_message") or res_json.get("message") or "Položka není skladem nebo ji nelze přidat."
                                raise BusinessLogicError(failed_msg)
                    except json.JSONDecodeError:
                        pass
                            
                    return {
                        "success": True,
                        "matchedId": product_id,
                        "matchedName": product.get("name"),
                        "details": add_message or f"Successfully added {quantity}x '{product.get('name')}' to cart."
                    }
                else:
                    raise BusinessLogicError("Invalid product type. Must be 1 (History lookup) or 2 (Fixed ID).")
    except Exception as e:
        orig_e = get_original_exception(e)
        if isinstance(orig_e, BusinessLogicError):
            raise orig_e
        if not is_retry:
            logger.warning(f"MCP connection/add failed ({e}). Retrying with fresh cookies...")
            clear_cached_cookies(cookie_header)
            return await call_rohlik_mcp_to_add(email, password, product, is_retry=True)
        raise orig_e

# --- Flask Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

@app.route('/api/check-credentials', methods=['GET'])
def check_credentials():
    email, password = get_credentials()
    if not email or not password:
        return jsonify({
            "configured": False,
            "message": "Credentials are not configured or are still set to defaults. Please update RHL_Mail and RHL_Pass in the .env file."
        })
    # Obfuscate email for safety
    obs_email = email[0] + "***" + email[email.find("@")-1:] if "@" in email else "***"
    return jsonify({
        "configured": True,
        "email": obs_email,
        "message": f"Credentials configured successfully for {obs_email}"
    })

@app.route('/api/products', methods=['GET'])
def get_products():
    return jsonify(load_products())

@app.route('/api/products', methods=['POST'])
def add_product():
    data = request.json
    if not data or 'name' not in data or 'type' not in data:
        return jsonify({"error": "Missing required fields: name and type"}), 400
        
    p_type = int(data.get("type", 1))
    name = data.get("name", "").strip()
    image_url = data.get("imageUrl", "").strip()
    product_id_input = data.get("productId", "").strip()
    search_term = data.get("searchTerm", "").strip()
    
    # Scrape if Type 2 (Fixed ID)
    if p_type == 2:
        scraped_id, scraped_name, scraped_img = scrape_rohlik_product_details(product_id_input)
        if scraped_id:
            product_id_input = scraped_id
            if not name or name == "":
                name = scraped_name or name
            if not image_url:
                image_url = scraped_img or ""
        else:
            return jsonify({"error": f"Failed to extract product ID or scrape details from input: {product_id_input}"}), 400
            
    # Scrape search image if Type 1 (History Lookup)
    elif p_type == 1:
        if not image_url and search_term:
            scraped_img = scrape_rohlik_search_image(search_term)
            if scraped_img:
                image_url = scraped_img
                
    products = load_products()
    new_product = {
        "id": str(uuid.uuid4()),
        "name": name,
        "type": p_type,
        "searchTerm": search_term if p_type == 1 else "",
        "productId": product_id_input if p_type == 2 else "",
        "imageUrl": image_url,
        "quantity": int(data.get("quantity", 1))
    }
    products.append(new_product)
    save_products(products)
    return jsonify(new_product), 201

@app.route('/api/products/<product_id>', methods=['PUT'])
def update_product(product_id):
    data = request.json
    products = load_products()
    
    product_index = -1
    for i, p in enumerate(products):
        if p["id"] == product_id:
            product_index = i
            break
            
    if product_index == -1:
        return jsonify({"error": "Product not found"}), 404
        
    p_type = int(data.get("type", products[product_index]["type"]))
    name = data.get("name", products[product_index]["name"]).strip()
    image_url = data.get("imageUrl", products[product_index].get("imageUrl", "")).strip()
    product_id_input = data.get("productId", "").strip()
    search_term = data.get("searchTerm", "").strip()
    
    # Scrape if Type 2 (Fixed ID)
    if p_type == 2:
        # If the product ID or URL changed, or if image_url is empty, trigger a scrape
        if product_id_input != products[product_index].get("productId") or not image_url:
            scraped_id, scraped_name, scraped_img = scrape_rohlik_product_details(product_id_input)
            if scraped_id:
                product_id_input = scraped_id
                if not name or name == products[product_index]["name"] or name == "":
                    name = scraped_name or name
                if not image_url or image_url == products[product_index].get("imageUrl"):
                    image_url = scraped_img or image_url
            else:
                return jsonify({"error": f"Failed to extract product ID or scrape details from input: {product_id_input}"}), 400
                
    # Scrape search image if Type 1 (History Lookup)
    elif p_type == 1:
        if not image_url or search_term != products[product_index].get("searchTerm"):
            scraped_img = scrape_rohlik_search_image(search_term)
            if scraped_img:
                image_url = scraped_img
                
    products[product_index]["name"] = name
    products[product_index]["type"] = p_type
    products[product_index]["searchTerm"] = search_term if p_type == 1 else ""
    products[product_index]["productId"] = product_id_input if p_type == 2 else ""
    products[product_index]["imageUrl"] = image_url
    products[product_index]["quantity"] = int(data.get("quantity", 1))
    
    save_products(products)
    return jsonify(products[product_index])

@app.route('/api/products/<product_id>', methods=['DELETE'])
def delete_product(product_id):
    products = load_products()
    initial_length = len(products)
    products = [p for p in products if p["id"] != product_id]
    
    if len(products) == initial_length:
        return jsonify({"error": "Product not found"}), 404
        
    save_products(products)
    return jsonify({"success": True, "message": "Product deleted successfully"})

def parse_cart_data(cart_text):
    try:
        data = json.loads(cart_text)
        items_list = []
        if isinstance(data, list):
            items_list = data
        elif isinstance(data, dict):
            raw_items = None
            if "items" in data:
                raw_items = data["items"]
            elif "cartItems" in data:
                raw_items = data["cartItems"]
            elif "data" in data and isinstance(data["data"], dict):
                inner_data = data["data"]
                if "items" in inner_data:
                    raw_items = inner_data["items"]
                elif "cart" in inner_data and isinstance(inner_data["cart"], dict):
                    raw_items = inner_data["cart"].get("items")
            
            if isinstance(raw_items, list):
                items_list = raw_items
            elif isinstance(raw_items, dict):
                items_list = list(raw_items.values())
                
        parsed_items = []
        for item in items_list:
            if not isinstance(item, dict):
                continue
            
            # Prioritize product-specific ID fields over top-level cart item ID
            pid = item.get("productId")
            if not pid and "product" in item and isinstance(item["product"], dict):
                pid = item["product"].get("id") or item["product"].get("productId")
            if not pid:
                pid = item.get("id")
            
            qty = item.get("quantity") or item.get("amount") or item.get("count") or 1
            
            name = item.get("productName") or item.get("name") or ""
            if not name and "product" in item and isinstance(item["product"], dict):
                name = item["product"].get("name") or ""
                
            # Parse orderFieldId for cart item updates
            order_field_id = item.get("orderFieldId") or item.get("cart_item_id") or ""
            
            # Parse imageUrl or image or imgPath
            img_url = item.get("imageUrl") or item.get("image") or item.get("imgPath")
            if not img_url and "product" in item and isinstance(item["product"], dict):
                img_url = item["product"].get("imageUrl") or item["product"].get("imgPath") or item["product"].get("image") or ""
                
            if img_url and isinstance(img_url, str):
                if not img_url.startswith("http"):
                    if img_url.startswith("/"):
                        img_url = f"https://cdn.rohlik.cz{img_url}"
                    else:
                        img_url = f"https://cdn.rohlik.cz/{img_url}"
                if "cdn-cgi/image" not in img_url:
                    img_url = f"https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/{img_url}"
            else:
                img_url = ""
            
            if pid:
                parsed_items.append({
                    "productId": str(pid),
                    "quantity": int(qty),
                    "name": name,
                    "orderFieldId": str(order_field_id),
                    "imageUrl": img_url
                })
        if parsed_items:
            return parsed_items
    except Exception:
        pass

    parsed_items = []
    # Match any 4 to 10 digit product IDs (e.g. 8791 for garlic)
    all_digits = re.findall(r'\b\d{4,10}\b', cart_text)
    for pid in all_digits:
        pos = cart_text.find(pid)
        surrounding = cart_text[max(0, pos-50):min(len(cart_text), pos+70)]
        
        qty_match = re.search(r'(?:quantity|amount|count|ks|pcs|x)[:\s\-]*(\d+)', surrounding, re.IGNORECASE)
        qty = 1
        if qty_match:
            qty = int(qty_match.group(1))
        else:
            qty_match_before = re.search(r'(\d+)[\s]*[x|ks|pcs]*[\s]+' + pid, surrounding, re.IGNORECASE)
            if qty_match_before:
                qty = int(qty_match_before.group(1))
                
        name_match = re.search(r'([a-zA-ZáéíóúýčďěňřšťůžÁÉÍÓÚÝČĎĚŇŘŠŤŮŽ\s\-\.\,]{3,30})[\s\(\[\{]*' + pid, cart_text[max(0, pos-40):pos])
        name = ""
        if name_match:
            name = name_match.group(1).strip()
            name = re.sub(r'^[\-\*\s]+', '', name)
            
        if not any(x["productId"] == pid for x in parsed_items):
            parsed_items.append({
                "productId": pid,
                "quantity": qty,
                "name": name,
                "orderFieldId": ""
            })
            
    return parsed_items

async def call_rohlik_mcp_to_get_cart(email, password, is_retry=False):
    url = "https://mcp.rohlik.cz/mcp"
    cookie_header = await get_rohlik_session_cookies(email, password, force_refresh=is_retry)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    else:
        headers["rhl-email"] = email
        headers["rhl-pass"] = password

    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(streamable_http_client(url=url, http_client=http_client))
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                cart_result = await session.call_tool("get_cart", {})
                cart_text = ""
                for content in cart_result.content:
                    if hasattr(content, "text"):
                        cart_text += content.text
                if getattr(cart_result, "isError", False) or getattr(cart_result, "is_error", False):
                    raise ValueError(f"Failed to fetch cart: {cart_text}")
                
                # Verify application-level success to catch expired sessions
                try:
                    data = json.loads(cart_text)
                    if isinstance(data, dict) and not data.get("success", True):
                        raise ValueError(f"Application-level failure in MCP get_cart: {cart_text}")
                except json.JSONDecodeError:
                    pass
                
                return cart_text
    except Exception as e:
        if not is_retry:
            logger.warning(f"MCP connection/get_cart failed ({e}). Retrying with fresh cookies...")
            clear_cached_cookies(cookie_header)
            return await call_rohlik_mcp_to_get_cart(email, password, is_retry=True)
        raise

CONFIG_FILE = os.path.join(base_dir, 'config.json')

def load_config():
    defaults = {"active_shopping_list_id": None, "active_shopping_list_name": None}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {**defaults, **data}
        except Exception as e:
            logger.error(f"Error loading config.json: {e}")
    return defaults

def save_config(config_data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

async def fetch_rohlik_shopping_lists(email, password, is_retry=False):
    url = "https://mcp.rohlik.cz/mcp"
    cookie_header = await get_rohlik_session_cookies(email, password, force_refresh=is_retry)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    else:
        headers["rhl-email"] = email
        headers["rhl-pass"] = password

    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(streamable_http_client(url=url, http_client=http_client))
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                res = await session.call_tool("get_user_shopping_lists_preview", {})
                res_text = ""
                for content in res.content:
                    if hasattr(content, "text"):
                        res_text += content.text
                if getattr(res, "isError", False) or getattr(res, "is_error", False):
                    raise ValueError(f"Failed to fetch shopping lists preview: {res_text}")
                
                data = json.loads(res_text)
                if not data.get("success", True):
                    raise ValueError(f"Application-level failure in MCP: {res_text}")
                return data.get("data", []) or []
    except Exception as e:
        if not is_retry:
            logger.warning(f"MCP shopping lists preview failed ({e}). Retrying with fresh cookies...")
            clear_cached_cookies(cookie_header)
            return await fetch_rohlik_shopping_lists(email, password, is_retry=True)
        logger.error(f"Error fetching shopping lists preview: {e}")
        return []

async def fetch_single_product_image(product_id):
    url = f"https://www.rohlik.cz/{product_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, follow_redirects=True, timeout=5.0)
            if resp.status_code == 200:
                img_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', resp.text)
                if not img_match:
                    img_match = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', resp.text)
                if img_match:
                    img_url = img_match.group(1)
                    if "cdn-cgi/image" in img_url:
                        img_url = re.sub(r'w=\d+,h=\d+', 'w=300,h=300', img_url)
                        img_url = img_url.replace("fit=cover,", "").replace(",fit=cover", "").replace("fit=cover", "")
                    return img_url
    except Exception as e:
        logger.error(f"Error fetching product image for {product_id}: {e}")
    return None

async def fetch_product_images_async(product_ids):
    tasks = [fetch_single_product_image(pid) for pid in product_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    images_map = {}
    for pid, res in zip(product_ids, results):
        if isinstance(res, str) and res:
            images_map[str(pid)] = res
    return images_map

async def fetch_rohlik_shopping_list_detail(email, password, list_id, is_retry=False):
    url = "https://mcp.rohlik.cz/mcp"
    cookie_header = await get_rohlik_session_cookies(email, password, force_refresh=is_retry)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    else:
        headers["rhl-email"] = email
        headers["rhl-pass"] = password

    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers)
            )
            streams = await stack.enter_async_context(streamable_http_client(url=url, http_client=http_client))
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # 1. Fetch the list details
                res = await session.call_tool("get_user_shopping_list_detail", {"shopping_list_id": int(list_id)})
                res_text = ""
                for content in res.content:
                    if hasattr(content, "text"):
                        res_text += content.text
                if getattr(res, "isError", False) or getattr(res, "is_error", False):
                    raise ValueError(f"Failed to fetch shopping list detail: {res_text}")
                
                list_data = json.loads(res_text)
                if not list_data.get("success", True):
                    raise ValueError(f"Application-level failure in MCP: {res_text}")
                list_name = list_data.get("name", "Unknown List")
                raw_products = list_data.get("products", []) or []
                
                # 2. Fetch product details and exact image paths
                product_ids = [int(p["productId"]) for p in raw_products if p.get("productId")]
                details_map = {}
                images_map = {}
                if product_ids:
                    # Fetch names and details
                    try:
                        det_res = await session.call_tool("get_product_details", {"product_ids": product_ids})
                        det_text = ""
                        for content in det_res.content:
                            if hasattr(content, "text"):
                                det_text += content.text
                        if not (getattr(det_res, "isError", False) or getattr(det_res, "is_error", False)):
                            details_map = json.loads(det_text)
                    except Exception as det_err:
                        logger.error(f"Failed to fetch product details for shopping list: {det_err}")
                        
                    # Fetch exact image paths by scraping public pages in parallel
                    try:
                        images_map = await fetch_product_images_async(product_ids)
                    except Exception as img_err:
                        logger.error(f"Failed to fetch exact product images: {img_err}")
                
                # 3. Enrich products
                enriched_products = []
                for p in raw_products:
                    pid = p.get("productId")
                    if not pid:
                        continue
                    pid_str = str(pid)
                    p_details = details_map.get(pid_str) or {}
                    
                    name = p_details.get("name") or "Unknown Product"
                    qty = p.get("amount") or p.get("quantity") or 1
                    
                    # Image URL resolution
                    img_url = images_map.get(pid_str)
                    if not img_url:
                        img_url = f"https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/https://cdn.rohlik.cz/images/grocery/products/{pid_str}/{pid_str}-1.jpg"
                    
                    enriched_products.append({
                        "productId": pid_str,
                        "name": name,
                        "quantity": int(qty),
                        "imageUrl": img_url
                    })
                    
                return {
                    "name": list_name,
                    "products": enriched_products
                }
    except Exception as e:
        if not is_retry:
            logger.warning(f"MCP shopping list detail failed ({e}). Retrying with fresh cookies...")
            clear_cached_cookies(cookie_header)
            return await fetch_rohlik_shopping_list_detail(email, password, list_id, is_retry=True)
        logger.error(f"Error fetching shopping list detail: {e}")
        return {"name": "Error Loading List", "products": []}

async def update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id=None, is_retry=False):
    cookie_header = await get_rohlik_session_cookies(email, password, force_refresh=is_retry)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    # If quantity is 0 and we have order_field_id, do DELETE
    if quantity == 0 and order_field_id:
        url = f"https://www.rohlik.cz/services/frontend-service/v2/cart?orderFieldId={order_field_id}"
        logger.info(f"Removing product {product_id} using DELETE on orderFieldId {order_field_id}")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(url, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    return {"success": True, "message": "Successfully removed item from cart."}
                elif resp.status_code in [401, 403, 405] and not is_retry:
                    logger.warning("Session cookies expired during delete. Retrying...")
                    clear_cached_cookies(cookie_header)
                    return await update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id, is_retry=True)
                else:
                    logger.error(f"Failed to delete cart item: status {resp.status_code}, response: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Error deleting cart item: {e}")
            if not is_retry:
                clear_cached_cookies(cookie_header)
                return await update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id, is_retry=True)
            raise

    # Otherwise (or if DELETE fallback failed), POST the target quantity
    url = "https://www.rohlik.cz/services/frontend-service/v2/cart"
    payload = {
        "actionId": None,
        "productId": int(product_id),
        "quantity": int(quantity),
        "recipeId": None,
        "source": "true:Shopping Lists"
    }
    logger.info(f"Updating product {product_id} quantity to {quantity} via POST")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if resp.status_code in [200, 202]:
                resp_data = resp.json()
                inner_data = resp_data.get("data", {}) or resp_data
                failed_items = inner_data.get("items_failed_to_add", [])
                if int(product_id) in failed_items or str(product_id) in failed_items:
                    message = inner_data.get("items_failed_message") or inner_data.get("message") or "Položka není skladem nebo ji nelze přidat."
                    return {"success": False, "error": message}
                return {"success": True, "message": f"Successfully updated quantity to {quantity}."}
            elif resp.status_code in [401, 403, 405] and not is_retry:
                logger.warning("Session cookies expired during POST update. Retrying...")
                clear_cached_cookies(cookie_header)
                return await update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id, is_retry=True)
            else:
                logger.error(f"Failed to update cart quantity: status {resp.status_code}, response: {resp.text[:200]}")
                try:
                    err_json = resp.json()
                    msg = err_json.get("messages", [{}])[0].get("content") or err_json.get("message")
                    if msg:
                        return {"success": False, "error": msg}
                except:
                    pass
    except Exception as e:
        logger.error(f"Error updating cart quantity: {e}")
        if not is_retry:
            clear_cached_cookies(cookie_header)
            return await update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id, is_retry=True)
        raise
        
    return {"success": False, "error": "Internal request failure"}

@app.route('/api/cart', methods=['GET'])
def get_cart_route():
    email, password = get_credentials()
    if not email or not password:
        return jsonify({"error": "Credentials not configured"}), 400
        
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        cart_text = loop.run_until_complete(call_rohlik_mcp_to_get_cart(email, password))
        loop.close()
        
        logger.info(f"Raw Cart response text from MCP: {cart_text}")
        cart_items = parse_cart_data(cart_text)
        logger.info(f"Parsed Cart items list: {cart_items}")
        
        products = load_products()
        logger.info(f"Loaded {len(products)} products from products.json database for matching.")
        
        quantity_map = {}
        for p in products:
            p_id = p["id"]
            p_type = int(p.get("type", 1))
            
            qty_in_cart = 0
            if p_type == 2:
                target_pid = str(p.get("productId", "")).strip()
                for item in cart_items:
                    item_pid = str(item.get("productId", "")).strip()
                    if item_pid == target_pid:
                        qty_in_cart += item["quantity"]
                logger.info(f"Matching Fixed ID product '{p.get('name')}' (target PID: {target_pid}) -> found qty in cart: {qty_in_cart}")
            elif p_type == 1:
                search_term = p.get("searchTerm", "").lower().strip()
                if search_term:
                    for item in cart_items:
                        item_name = item.get("name", "").lower()
                        item_pid = str(item.get("productId", "")).strip()
                        if search_term in item_name or search_term == item_pid:
                            qty_in_cart += item["quantity"]
                logger.info(f"Matching History Search product '{p.get('name')}' (term: '{search_term}') -> found qty in cart: {qty_in_cart}")
                            
            quantity_map[p_id] = qty_in_cart
            
        logger.info(f"Final resolved quantity map: {quantity_map}")
        return jsonify({
            "success": True,
            "cart": quantity_map,
            "rawCart": cart_items
        })
    except Exception as e:
        logger.exception(f"Error in /api/cart: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/cart/set-quantity', methods=['POST'])
def set_cart_quantity_route():
    email, password = get_credentials()
    if not email or not password:
        return jsonify({"error": "Credentials not configured"}), 400
        
    data = request.json or {}
    product_id = data.get("productId")
    quantity = data.get("quantity")
    order_field_id = data.get("orderFieldId")
    
    if product_id is None or quantity is None:
        return jsonify({"error": "Missing productId or quantity"}), 400
        
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        res = loop.run_until_complete(update_rohlik_cart_quantity(email, password, product_id, quantity, order_field_id))
        loop.close()
        return jsonify(res)
    except Exception as e:
        logger.exception(f"Error in set_cart_quantity_route: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/shopping-lists', methods=['GET'])
def get_shopping_lists_route():
    email, password = get_credentials()
    if not email or not password:
        return jsonify({"error": "Credentials not configured"}), 400
        
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        lists = loop.run_until_complete(fetch_rohlik_shopping_lists(email, password))
        loop.close()
        
        formatted_lists = []
        for lst in lists:
            lid = lst.get("id") or lst.get("shoppingListId")
            lname = lst.get("name") or lst.get("title") or "Unnamed List"
            if lid:
                formatted_lists.append({
                    "id": str(lid),
                    "name": lname
                })
        
        config = load_config()
        active_id = config.get("active_shopping_list_id")
        active_name = config.get("active_shopping_list_name")
        
        # Auto-detect list by name or ID if not set
        if not active_id and formatted_lists:
            if active_name:
                matched = next((l for l in formatted_lists if l["name"].lower().strip() == active_name.lower().strip()), None)
                if not matched:
                    matched = next((l for l in formatted_lists if active_name.lower().strip() in l["name"].lower()), None)
                if matched:
                    active_id = matched["id"]
                    active_name = matched["name"]
            
            if not active_id:
                    active_id = formatted_lists[0]["id"]
                    active_name = formatted_lists[0]["name"]
            
            config["active_shopping_list_id"] = active_id
            config["active_shopping_list_name"] = active_name
            save_config(config)
            
        return jsonify({
            "success": True,
            "lists": formatted_lists,
            "active_shopping_list_id": active_id,
            "active_shopping_list_name": active_name or ""
        })
    except Exception as e:
        logger.exception(f"Error in /api/shopping-lists: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/shopping-lists/active', methods=['POST'])
def set_active_shopping_list_route():
    data = request.json or {}
    list_id = data.get("active_shopping_list_id")
    list_name = data.get("active_shopping_list_name")
    
    if not list_id and not list_name:
        return jsonify({"error": "Missing active_shopping_list_id or active_shopping_list_name"}), 400
        
    config = load_config()
    email, password = get_credentials()
    
    # Resolve name to ID if only name is provided
    if list_name and not list_id and email and password:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            lists = loop.run_until_complete(fetch_rohlik_shopping_lists(email, password))
            loop.close()
            
            matched = None
            for lst in lists:
                lname = lst.get("name") or lst.get("title") or ""
                if lname.lower().strip() == list_name.lower().strip():
                    matched = lst
                    break
            
            if not matched:
                for lst in lists:
                    lname = lst.get("name") or lst.get("title") or ""
                    if list_name.lower().strip() in lname.lower():
                        matched = lst
                        break
                        
            if matched:
                list_id = str(matched.get("id") or matched.get("shoppingListId"))
                list_name = matched.get("name") or matched.get("title")
            else:
                # Store name anyway as unresolved
                config["active_shopping_list_name"] = list_name
                config["active_shopping_list_id"] = None
                save_config(config)
                return jsonify({
                    "success": True,
                    "active_shopping_list_id": None,
                    "active_shopping_list_name": list_name,
                    "warning": f"Seznam s názvem '{list_name}' nebyl na Rohlíku nalezen, ale název byl uložen."
                })
        except Exception as e:
            logger.error(f"Failed to resolve shopping list name: {e}")
            
    if list_id:
        config["active_shopping_list_id"] = str(list_id)
        if list_name:
            config["active_shopping_list_name"] = str(list_name)
        else:
            # Resolve ID to name
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                lists = loop.run_until_complete(fetch_rohlik_shopping_lists(email, password))
                loop.close()
                for lst in lists:
                    lid = str(lst.get("id") or lst.get("shoppingListId"))
                    if lid == str(list_id):
                        config["active_shopping_list_name"] = lst.get("name") or lst.get("title")
                        break
            except Exception as e:
                logger.error(f"Failed to resolve ID to name: {e}")
    else:
        return jsonify({"error": "Failed to resolve shopping list"}), 400
                
    save_config(config)
    return jsonify({
        "success": True,
        "active_shopping_list_id": config.get("active_shopping_list_id"),
        "active_shopping_list_name": config.get("active_shopping_list_name")
    })

@app.route('/api/shopping-list', methods=['GET'])
def get_shopping_list_route():
    email, password = get_credentials()
    if not email or not password:
        return jsonify({"error": "Credentials not configured"}), 400
        
    config = load_config()
    active_id = config.get("active_shopping_list_id")
    active_name = config.get("active_shopping_list_name")
    
    # Try to resolve or verify using shopping list name or fallback ID
    if email and password:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            lists = loop.run_until_complete(fetch_rohlik_shopping_lists(email, password))
            loop.close()
            
            matched = None
            if active_name:
                for lst in lists:
                    lname = lst.get("name") or lst.get("title") or ""
                    if lname.lower().strip() == active_name.lower().strip():
                        matched = lst
                        break
                if not matched:
                    for lst in lists:
                        lname = lst.get("name") or lst.get("title") or ""
                        if active_name.lower().strip() in lname.lower():
                            matched = lst
                            break
                            
            if not matched and active_id:
                for lst in lists:
                    lid = str(lst.get("id") or lst.get("shoppingListId"))
                    if lid == str(active_id):
                        matched = lst
                        break
                        
            if matched:
                active_id = str(matched.get("id") or matched.get("shoppingListId"))
                active_name = matched.get("name") or matched.get("title")
                if config.get("active_shopping_list_id") != active_id or config.get("active_shopping_list_name") != active_name:
                    config["active_shopping_list_id"] = active_id
                    config["active_shopping_list_name"] = active_name
                    save_config(config)
            else:
                # Auto-discovery
                formatted_lists = []
                for lst in lists:
                    lid = lst.get("id") or lst.get("shoppingListId")
                    lname = lst.get("name") or lst.get("title") or "Unnamed List"
                    if lid:
                        formatted_lists.append({"id": str(lid), "name": lname})
                if formatted_lists:
                    matched_list = formatted_lists[0]
                    active_id = matched_list["id"]
                    active_name = matched_list["name"]
                    config["active_shopping_list_id"] = active_id
                    config["active_shopping_list_name"] = active_name
                    save_config(config)
        except Exception as e:
            logger.error(f"Resolution / auto-discovery failed: {e}")
            
    if not active_id:
        return jsonify({"error": "No shopping list found or configured"}), 404
        
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        list_detail = loop.run_until_complete(fetch_rohlik_shopping_list_detail(email, password, active_id))
        loop.close()
        
        formatted_products = []
        for p in list_detail.get("products", []):
            pid = p.get("productId") or p.get("id")
            if not pid and "product" in p and isinstance(p["product"], dict):
                pid = p["product"].get("id") or p["product"].get("productId")
                
            if not pid:
                continue
                
            pid_str = str(pid)
            name = p.get("name") or p.get("productName")
            if not name and "product" in p and isinstance(p["product"], dict):
                name = p["product"].get("name")
            if not name:
                name = "Unknown Product"
                
            qty = p.get("quantity") or p.get("amount") or p.get("count") or 1
            if "product" in p and isinstance(p["product"], dict):
                qty = p["product"].get("amount") or qty
                
            img_url = p.get("imageUrl") or p.get("image") or p.get("imgPath")
            if "product" in p and isinstance(p["product"], dict):
                img_url = p["product"].get("imageUrl") or p["product"].get("imgPath") or img_url
                
            if img_url and isinstance(img_url, str):
                if not img_url.startswith("http"):
                    if img_url.startswith("/"):
                        img_url = f"https://cdn.rohlik.cz{img_url}"
                    else:
                        img_url = f"https://cdn.rohlik.cz/{img_url}"
                if "cdn-cgi/image" not in img_url:
                    img_url = f"https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/{img_url}"
            else:
                img_url = f"https://www.rohlik.cz/cdn-cgi/image/f=auto,w=300,h=300/https://cdn.rohlik.cz/images/grocery/products/{pid_str}/{pid_str}-1.jpg"
                
            formatted_products.append({
                "productId": pid_str,
                "name": name,
                "imageUrl": img_url,
                "quantity": int(qty)
            })
            
        return jsonify({
            "success": True,
            "name": list_detail.get("name", "Unknown List"),
            "products": formatted_products
        })
    except Exception as e:
        logger.exception(f"Error in /api/shopping-list: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/add-to-cart/<product_id>', methods=['POST'])
def add_to_cart(product_id):
    email, password = get_credentials()
    if not email or not password:
        return jsonify({
            "error": "Credentials are not configured in the .env file. Please check RHL_Mail and RHL_Pass."
        }), 400
        
    products = load_products()
    product = next((p for p in products if p["id"] == product_id), None)
    
    if not product:
        if product_id.isdigit():
            qty = int(request.args.get("quantity", 1))
            product = {
                "id": product_id,
                "name": f"Položka {product_id}",
                "type": 2,
                "productId": product_id,
                "quantity": qty
            }
        else:
            return jsonify({"error": "Product not found in your configured database."}), 404
        
    try:
        # Run async function using asyncio loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(call_rohlik_mcp_to_add(email, password, product))
        loop.close()
        return jsonify(result)
    except Exception as e:
        orig_e = get_original_exception(e)
        logger.exception(f"Error adding product {product_id} to cart: {orig_e}")
        error_msg = str(orig_e)
        if isinstance(orig_e, BusinessLogicError):
            error_msg = clean_availability_message(error_msg)
        return jsonify({
            "error": error_msg
        }), 500

if __name__ == '__main__':
    # Read port from environment (can be set in .env file, e.g., PORT=5050)
    port = int(os.getenv("PORT", 5050))
    logger.info(f"Starting Flask server on http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
