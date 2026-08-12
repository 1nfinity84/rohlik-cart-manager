import httpx
import re
import urllib.parse

search_term = "parky dacello"
encoded_term = urllib.parse.quote(search_term)
url = f"https://www.rohlik.cz/hledat/{encoded_term}"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

print(f"Fetching search page: {url}...")
try:
    resp = httpx.get(url, headers=headers, follow_redirects=True)
    print(f"Status: {resp.status_code}")
    
    # Try the regex pattern
    matches = re.findall(r'https://cdn\.rohlik\.cz/images/grocery/products/\d+/[^"\'\s>]+?\.(?:jpg|jpeg|png|webp)', resp.text)
    print(f"\nFound {len(matches)} matching product image URLs:")
    for idx, match in enumerate(matches[:5]):
        print(f"{idx+1}: {match}")
        
    if not matches:
        print("\nNo direct matches found. Snippet of HTML containing cdn.rohlik.cz/images:")
        # Try to find any occurrences of images to see their pattern
        occurrences = [m.start() for m in re.finditer(r'cdn\.rohlik\.cz/images', resp.text)]
        for occ in occurrences[:5]:
            print(resp.text[occ-100:occ+200])
            print("-" * 50)
except Exception as e:
    print(f"Error: {e}")
