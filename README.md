# Rohlík.cz MCP Cart Manager & Admin Panel

Moderní webový nákupní panel a backend v Pythonu pro správu produktů a okamžité přidávání zboží do košíku na **Rohlík.cz** na jedno kliknutí. 

Aplikace využívá oficiální a přímé připojení na vzdálený **Rohlík MCP (Model Context Protocol) Server** přes HTTP Streamable SSE transport (`mcp.rohlik.cz/mcp`).

> [!NOTE]
> Díky přímému spojení v Pythonu **nemusíte** na svém serveru instalovat Node.js ani stahovat žádné npm balíčky.

---

## 🌟 Hlavní Funkce

1. **Rychlé přidávání Z historie (Typ 1)**: Skript prohledá vaše minulé doručené objednávky na Rohlíku, najde nejnovější produkt odpovídající hledanému výrazu (např. *"mouka polohrubá"* nebo *"máslo"*), zjistí jeho aktuální ID a okamžitě ho vloží do košíku.
2. **Pevný produkt (Typ 2)**: Přidá přesné zboží na základě fixního ID z Rohlík.cz nebo vložené URL adresy produktu.
3. **Synchronizace s Nákupními seznamy Rohlíku**: Načítá vaše existující nákupní seznamy vytvořené v aplikaci/webu Rohlík.cz (např. seznam *"Blbosti"*, *"Týdenní nákup"*) a umožňuje je přímo procházet a upravovat.
4. **Živé ovládání košíku**: Zobrazuje aktuální stav košíku, rozdělené množství a umožňuje navyšovat, snižovat nebo odebírat položky z košíku přímo z panelu.
5. **Moderní Kiosk UI**: Responzivní, tmavý režim (glassmorphism design), dlaždicové zobrazení obrázků produktů, animace stavu přidání a toast notifikace. Vhodné pro mobil, tablet i dotykový Smart Display / Google Nest Hub.
6. **Statistiky nákupů (`top_purchased.py`)**: Pomocný skript pro analýzu a vypsání nejčastěji kupovaných produktů z vaší historie.

---

## 📁 Adresářová Struktura

```text
rohlik-clean/
├── app.py                # Hlavní Flask server (REST API, MCP klient, web backend)
├── templates/
│   └── index.html        # Webový panel (HTML5, Vanilla CSS3, JS)
├── products.json         # Databáze lokálně uložených šablon produktů
├── config.json           # Konfigurace aktivního nákupního seznamu
├── requirements.txt      # Python závislosti (Flask, httpx, mcp, python-dotenv)
├── .env.example          # Vzorový soubor pro přihlašovací údaje
├── rohlik.service        # Systemd unit soubor pro spuštění na Linuxu
├── top_purchased.py      # Pomocný skript pro vypsání nejčastěji kupovaného zboží
├── test_login.py         # Testovací skript autentizace a ziskávání session cookies
├── test_mcp.py           # Testovací skript připojení k Rohlík MCP serveru
└── README.md             # Tato dokumentace
```

---

## 🚀 Rychlá Instalace a Lokální Spuštění

### 1. Klonování / Přesun souborů
Stáhněte nebo zkopírujte složku projektu na váš počítač či server:
```bash
git clone <repository_url> rohlik
cd rohlik
```

### 2. Vytvoření virtuálního prostředí a instalace závislostí
```bash
python3 -m venv venv
source venv/bin/activate    # Na Windows použijte: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Konfigurace přihlašovacích údajů
Vytvořte soubor `.env` ze vzoru `.env.example`:
```bash
cp .env.example .env
```
Otevřete soubor `.env` v textovém editoru a zadejte vaše e-mailové heslo k účtu na Rohlík.cz:
```env
RHL_Mail="vas-email@gmail.com"
RHL_Pass="VaseHesloNaRohlik"

# Nastavení portu (výchozí 5050)
PORT=5050
```

### 4. Spuštění aplikace
```bash
python app.py
```
Aplikace se spustí na adrese `http://localhost:5050` (případně `http://<IP_SERVERU>:5050`).

---

## 🛠️ Nasazení na Linuxový Server (Deployment Guide)

Nasazení na Linuxový server (Ubuntu, Debian, Raspberry Pi OS apod.) jako automatická systémová služba **systemd**.

### 1. Příprava adresáře na serveru
Doporučené umístění je např. `/root/rohlik` nebo `/opt/rohlik`:
```bash
sudo mkdir -p /root/rohlik
# Zkopírujte obsah složky rohlik-clean do /root/rohlik
cd /root/rohlik

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # Vyplňte vaše RHL_Mail a RHL_Pass
```

### 2. Nastavení Systemd služby (`rohlik.service`)
Vytvořte konfigurační soubor služby `/etc/systemd/system/rohlik.service`:

```bash
sudo nano /etc/systemd/system/rohlik.service
```

Vložte následující obsah (upravte cestu a uživatele podle potřeby):

```ini
[Unit]
Description=Rohlík.cz MCP Cart Manager Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/rohlik
ExecStart=/root/rohlik/venv/bin/python app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Aktivujte a spusťte službu:
```bash
sudo systemctl daemon-reload
sudo systemctl enable rohlik.service
sudo systemctl start rohlik.service
```

Kontrola stavu a logů:
```bash
sudo systemctl status rohlik.service
sudo journalctl -u rohlik.service -f
```

---

## 📺 Streamování Panelu na Google Nest Hub / Chromecast (`catt`)

Pokud chcete mít nákupní panel neustále zobrazený na chytrém displeji (např. **Google Nest Hub**, **Google TV** nebo **Chromecast** na lednici či v kuchyni), můžete využít nástroj **`catt` (Cast All The Things)**.

### 1. Instalace `catt`
Na Linuxovém serveru nainstalujte `catt`:
```bash
pip install catt
# nebo pomocí pipx:
sudo apt install pipx
pipx install catt
```

### 2. Vyhledání vašeho Chromecast / Nest Hub zařízení
Ujistěte se, že server i chytré zařízení jsou na stejné síti Wi-Fi / LAN, a spusťte:
```bash
catt scan
```
Získáte název zařízení, např. `"Kuchyne Display"` nebo `"Google Nest Hub"`.

### 3. Odeslání Rohlík Panelu na displej
Spusťte příkaz pro odeslání URL adresy vašeho panelu:
```bash
catt -d "Kuchyne Display" cast_site "http://<IP_VASEHO_SERVERU>:5050"
```
Displej načte živé webové rozhraní Rohlík Panelu, kde můžete dotykem okamžitě přidávat produkty do košíku.

### 4. Automatizace vracení streamu (Cron / Systemd)
Google Nest Hub z bezpečnostních důvodů po cca 10 minutách až několika hodinách neaktivity klientský web ukončí a vrátí se na hodiny.

Pro automatické obnovování zobrazení nákupního panelu na Nest Hubu můžete nastavit **Cron job**:

```bash
crontab -e
```

Přidejte řádek, který každé 4 hodiny (nebo každou hodinu) obnoví zobrazení:
```cron
0 */4 * * * /usr/local/bin/catt -d "Kuchyne Display" cast_site "http://192.168.1.100:5050" >/dev/null 2>&1
```
*(Nahraďte IP adresu a název zařízení vašimi reálnými hodnotami).*

---

## 🔒 Zabezpečení a Ochrana Soukromí

* **`.env` a `.session_cookies`**: Soubory obsahující přihlašovací údaje a aktivní relace jsou automaticky zahrnuty v `.gitignore` a **nesmí** být nahrány do veřejného repozitáře.
* **Soubory cookies**: Služba si ukládá šifrovanou/veřejnou session cookie do `.session_cookies`, aby nebylo nutné při každém požadavku provádět nový login na Rohlík.cz a nedocházelo k blokaci ze strany Cloudflare (Error 429).

---

## 📝 Licencování

Tento projekt je určen pro osobní a nekomerční použití. Rohlík.cz je ochrannou známkou společnosti VELKÁ PECKA s.r.o.
