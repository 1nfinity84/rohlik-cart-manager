import os
import httpx
from dotenv import load_dotenv

# Load env variables
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

email = os.getenv("RHL_Mail")
password = os.getenv("RHL_Pass")
manual_cookie = os.getenv("RHL_Cookie")

if not email or not password:
    print("Error: RHL_Mail or RHL_Pass not found in .env file!")
    exit(1)

email = email.strip()
password = password.strip()

obs_email = email[0] + "***" + email[email.find("@")-1:] if "@" in email else "***"

print(f"Loaded credentials from .env:")
print(f"Email: {obs_email}")
if manual_cookie and manual_cookie.strip():
    print("Manual Cookie Override detected.")
else:
    print("No manual Cookie Override detected.")

# Helper to test orders API with cookies
def test_orders_api(cookie_str):
    orders_url = "https://www.rohlik.cz/api/v3/orders/delivered?offset=0&limit=5"
    orders_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Accept": "application/json"
    }
    print(f"\nFetching orders from {orders_url} using session cookies...")
    try:
        with httpx.Client() as client:
            orders_response = client.get(orders_url, headers=orders_headers)
            print(f"Orders Status Code: {orders_response.status_code}")
            if orders_response.status_code == 200:
                print("Successfully authenticated and fetched order history!")
                print("Orders Response (first 500 chars):")
                print(orders_response.text[:500])
                return True
            else:
                print(f"Failed to fetch orders: {orders_response.text[:300]}")
    except Exception as e:
        print(f"Error testing orders API: {e}")
    return False

# Step 1: Check manual cookie override
if manual_cookie and manual_cookie.strip():
    print("\n--- Testing Manual Cookie Override ---")
    if test_orders_api(manual_cookie.strip()):
        print("\nSUCCESS: Manual cookie override is working perfectly!")
        exit(0)
    else:
        print("\nWARNING: Manual cookie override failed. Proceeding to test standard login...")

# Step 2: Test standard login
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

print(f"\n--- Testing Standard Login (POST to {url}) ---")
try:
    with httpx.Client(headers=headers) as client:
        response = client.post(url, json=payload)
        print(f"Login Status Code: {response.status_code}")
        if response.status_code == 200:
            print("Login successful! Parsing cookies...")
            cookies = []
            for cookie_header in response.headers.get_list("set-cookie"):
                parts = cookie_header.split(";")
                if parts:
                    cookies.append(parts[0].strip())
            
            cookie_str = "; ".join(cookies)
            print(f"Extracted Cookie Header: {cookie_str}")
            
            # Test the cookies
            test_orders_api(cookie_str)
        elif response.status_code == 429:
            print("\n[!] ERROR 429: Cloudflare is rate-limiting your VPS IP address.")
            print("To resolve this, you can:")
            print("1. Wait for the rate limit to expire (usually 15-30 minutes).")
            print("2. OR manually log in to www.rohlik.cz in your browser, copy your session cookies (JSESSIONID and PHPSESSID), and paste them into the RHL_Cookie field in your .env file.")
        else:
            print(f"Login failed: {response.text}")
except Exception as e:
    print(f"Error during login attempt: {e}")



