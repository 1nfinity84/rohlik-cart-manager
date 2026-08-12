import httpx
import json

search_term = "parky dacello"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

url = f"https://www.rohlik.cz/services/frontend-service/search-metadata?search={search_term}&offset=0&limit=3&companyId=1&canCorrect=true"
print(f"Querying: {url}...")
try:
    resp = httpx.get(url, headers=headers)
    print(f"Status: {resp.status_code}")
    data = resp.json()
    
    products = data.get("data", {}).get("productList", [])
    print(f"Found {len(products)} products.")
    
    if products:
        # Print the first product object fields
        print("\nFirst product properties:")
        first_prod = products[0]
        for key, val in first_prod.items():
            if key in ["productId", "productName", "image", "imageUrl", "img", "imgPath", "images", "thumbnail"]:
                print(f"  {key}: {val}")
            elif isinstance(val, (str, int, float)) and len(str(val)) < 150:
                print(f"  {key}: {val}")
            else:
                print(f"  {key}: <type: {type(val)}>")
except Exception as e:
    print(f"Error: {e}")
