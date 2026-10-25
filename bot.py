"""Controlla pagine di negozi online e notifica i nuovi prodotti su Telegram."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import platform
import re
import signal
import sqlite3
import sys
import time
import atexit
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "prodotti_visti.db"
SHOPS_PATH = ROOT / "elenco_negozi.txt"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramProductWatcher/1.0)"
TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "_gl")
_browser_driver = None


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    url: str
    available: bool = True


UNAVAILABLE_WORDS = (
    "esaurito",
    "non disponibile",
    "out of stock",
    "outofstock",
    "sold out",
    "temporaneamente non disponibile",
)


def availability_from_text(value: str) -> bool:
    text = " ".join(value.lower().split())
    return not any(word in text for word in UNAVAILABLE_WORDS)


def _card_available(node) -> bool:
    own_classes = " ".join(node.get("class", [])) if hasattr(node, "get") else ""
    card = node if re.search(r"product|prodotto|s_item", own_classes, re.I) else None
    if card is None:
        card = node.find_parent(
            ["li", "article", "div"],
            class_=re.compile(r"product|prodotto|s_item", re.I),
        )
    if card is None:
        return True
    classes = " ".join(card.get("class", []))
    return availability_from_text(f"{classes} {card.get_text(' ', strip=True)}")


def clean_url(url: str, base: str = "") -> str:
    absolute = urljoin(base, url)
    parts = urlsplit(absolute)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING_PREFIXES)]
    # Alcuni server distinguono /categoria da /categoria/ (e rispondono 404).
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def product_key(url: str) -> str:
    return hashlib.sha256(clean_url(url).encode("utf-8")).hexdigest()


def _json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _json_objects(item)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from _json_objects(value["@graph"])


def extract_products(page_url: str, body: str) -> list[Product]:
    """Estrae prodotti da JSON-LD e link di catalogo, eliminando i duplicati."""
    soup = BeautifulSoup(body, "html.parser")
    found: dict[str, Product] = {}

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _json_objects(data):
            kind = obj.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" not in kinds:
                continue
            title = str(obj.get("name") or "").strip()
            url = obj.get("url")
            if not url and isinstance(obj.get("offers"), dict):
                url = obj["offers"].get("url")
            if title and url:
                normalized = clean_url(str(url), page_url)
                offers = obj.get("offers")
                offer = offers[0] if isinstance(offers, list) and offers else offers
                availability = str(offer.get("availability") or "") if isinstance(offer, dict) else ""
                available = availability_from_text(availability)
                found[product_key(normalized)] = Product(product_key(normalized), title, normalized, available)

    # Copre Shopify (/products/) e molti cataloghi WooCommerce (/prodotto/).
    selectors = (
        'a[href*="/products/"], a[href*="/product/"], '
        'a[href*="/prodotto/"], a[href*="/prodotti/"]'
    )
    for anchor in soup.select(selectors):
        title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
        if not title:
            image = anchor.find("img")
            title = ((image.get("alt") if image else "") or "").strip()
        if not title or len(title) > 300:
            continue
        normalized = clean_url(anchor.get("href", ""), page_url)
        if normalized.startswith(("http://", "https://")):
            key = product_key(normalized)
            # Preferisce il titolo più descrittivo tra link duplicati.
            if key not in found or len(title) > len(found[key].title):
                found[key] = Product(key, title, normalized, _card_available(anchor))

    # Titolo WooCommerce quando il permalink è annidato nella categoria stessa.
    for anchor in soup.select(".woocommerce-loop-product__title a[href]"):
        title = anchor.get_text(" ", strip=True)
        if title:
            normalized = clean_url(anchor.get("href", ""), page_url)
            key = product_key(normalized)
            found[key] = Product(key, title[:300], normalized, _card_available(anchor))

    # Tema proprietario di Magic Store.
    for card in soup.select(".s_item"):
        image_link = card.select_one("a.s_thumb[href]")
        if not image_link:
            continue
        href = image_link.get("href", "")
        title_link = next(
            (a for a in card.select("a[href]") if a.get("href") == href and a.get_text(" ", strip=True)),
            None,
        )
        if title_link:
            title = title_link.get_text(" ", strip=True)
            normalized = clean_url(href, page_url)
            key = product_key(normalized)
            found[key] = Product(key, title[:300], normalized, _card_available(card))

    # Temi WooCommerce/PrestaShop con URL personalizzati (es. Rumble Store).
    cards = soup.select(
        "li.product, article.product, .product-item, .product-miniature, "
        "[data-product-id], [data-product_id]"
    )
    for card in cards:
        title_node = card.select_one(
            "h2, h3, h4, .product-title, .product-name, .woocommerce-loop-product__title"
        )
        anchors = card.select("a[href]")
        anchor = next((a for a in anchors if title_node and a.find(title_node.name)), None)
        if anchor is None and title_node:
            anchor = title_node.find_parent("a", href=True)
        if anchor is None:
            anchor = next((a for a in anchors if a.get_text(" ", strip=True)), None)
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if anchor and title:
            normalized = clean_url(anchor.get("href", ""), page_url)
            if normalized.startswith(("http://", "https://")):
                key = product_key(normalized)
                found[key] = Product(key, title[:300], normalized, _card_available(card))
    return list(found.values())


def _shopify_catalog_url(shop_url: str) -> str | None:
    parts = urlsplit(shop_url)
    marker = "/collections/"
    if marker not in parts.path:
        return None
    collection = parts.path.split(marker, 1)[1].strip("/").split("/", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, f"/collections/{collection}/products.json", "limit=250", ""))


def _products_from_shopify(data: dict, base_url: str) -> list[Product]:
    found = []
    for item in data.get("products", []):
        title = str(item.get("title") or "").strip()
        handle = str(item.get("handle") or "").strip()
        if title and handle:
            url = clean_url(f"/products/{handle}", base_url)
            variants = item.get("variants") or []
            available = any(bool(variant.get("available")) for variant in variants)
            found.append(Product(product_key(url), title, url, available))
    return found


def _products_from_woocommerce(data: list, base_url: str) -> list[Product]:
    found = []
    for item in data:
        title = str(item.get("name") or "").strip()
        url = str(item.get("permalink") or "").strip()
        if title and url:
            url = clean_url(url, base_url)
            found.append(Product(product_key(url), title, url, bool(item.get("is_in_stock", True))))
    return found


def _fetch_with_browser(shop: str) -> list[Product]:
    """Fallback per cataloghi pubblici che richiedono JavaScript/browser."""
    global _browser_driver
    if os.getenv("BROWSER_FALLBACK", "true").lower() not in {"1", "true", "yes"}:
        return []
    try:
        from selenium import webdriver
        from selenium.webdriver.support.ui import WebDriverWait

        if _browser_driver is None:
            if platform.system() == "Windows":
                from selenium.webdriver.edge.options import Options

                options = Options()
                driver_factory = webdriver.Edge
            else:
                from selenium.webdriver.chrome.options import Options

                options = Options()
                driver_factory = webdriver.Chrome
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1440,1200")
            options.add_argument("--lang=it-IT")
            _browser_driver = driver_factory(options=options)
            _browser_driver.set_page_load_timeout(45)
            atexit.register(lambda: _browser_driver and _browser_driver.quit())
        _browser_driver.get(shop)
        WebDriverWait(_browser_driver, 15).until(
            lambda driver: driver.execute_script("return document.readyState") == "complete"
        )
        return extract_products(_browser_driver.current_url, _browser_driver.page_source)
    except Exception as exc:
        logging.warning("Fallback browser non riuscito per %s: %s", shop, exc)
        return []


def fetch_products(session: requests.Session, shop: str) -> list[Product]:
    """Prova cataloghi pubblici prima della pagina HTML."""
    shopify_url = _shopify_catalog_url(shop)
    if shopify_url:
        try:
            response = session.get(shopify_url, timeout=30)
            response.raise_for_status()
            products = _products_from_shopify(response.json(), shop)
            if products:
                return products
        except (requests.RequestException, ValueError):
            pass

    try:
        response = session.get(shop, timeout=30)
        response.raise_for_status()
        return extract_products(response.url, response.text)
    except requests.HTTPError as page_error:
        if page_error.response is None or page_error.response.status_code not in {403, 429}:
            raise
        # Store API di WooCommerce: endpoint pubblico, senza credenziali.
        parts = urlsplit(shop)
        api_url = urlunsplit(
            (parts.scheme, parts.netloc, "/wp-json/wc/store/v1/products", "search=one%20piece&per_page=100", "")
        )
        try:
            response = session.get(api_url, timeout=30)
            response.raise_for_status()
            products = _products_from_woocommerce(response.json(), shop)
            if products:
                return products
        except (requests.RequestException, ValueError):
            pass
        products = _fetch_with_browser(shop)
        if products:
            return products
        raise page_error


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS products ("
            " shop_url TEXT NOT NULL, product_key TEXT NOT NULL, title TEXT NOT NULL, "
            " product_url TEXT NOT NULL, first_seen INTEGER NOT NULL, "
            " PRIMARY KEY (shop_url, product_key))"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(products)")}
        if "active" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "available" not in columns:
            self.db.execute("ALTER TABLE products ADD COLUMN available INTEGER NOT NULL DEFAULT 1")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS shops (shop_url TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS subscribers ("
            "chat_id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1, "
            "pending_catalog INTEGER NOT NULL DEFAULT 1, subscribed_at INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()

    def record(self, shop_url: str, products: Iterable[Product]) -> tuple[bool, list[Product]]:
        """Salva la baseline; dopo, restituisce i prodotti non ancora confermati."""
        products = list(products)
        initialized = self.db.execute(
            "SELECT initialized FROM shops WHERE shop_url = ?", (shop_url,)
        ).fetchone()
        was_initialized = bool(initialized and initialized[0])
        if was_initialized:
            known = {
                row[0]
                for row in self.db.execute(
                    "SELECT product_key FROM products WHERE shop_url = ?", (shop_url,)
                )
            }
            with self.db:
                self.db.execute("UPDATE products SET active=0 WHERE shop_url=?", (shop_url,))
                self.db.executemany(
                    "UPDATE products SET active=1, available=?, title=?, product_url=? "
                    "WHERE shop_url=? AND product_key=?",
                    [(int(p.available), p.title, p.url, shop_url, p.key) for p in products],
                )
            return True, [product for product in products if product.key not in known]

        now = int(time.time())
        with self.db:
            for product in products:
                self.db.execute(
                    "INSERT OR IGNORE INTO products"
                    "(shop_url,product_key,title,product_url,first_seen,active,available) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (shop_url, product.key, product.title, product.url, now, int(product.available)),
                )
            self.db.execute(
                "INSERT INTO shops(shop_url, initialized) VALUES(?, 1) "
                "ON CONFLICT(shop_url) DO UPDATE SET initialized=1",
                (shop_url,),
            )
        return False, []

    def confirm(self, shop_url: str, product: Product) -> None:
        """Segna visto un prodotto soltanto dopo una notifica riuscita."""
        with self.db:
            self.db.execute(
                "INSERT INTO products(shop_url,product_key,title,product_url,first_seen,active,available) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(shop_url,product_key) DO UPDATE SET "
                "title=excluded.title, product_url=excluded.product_url, active=1, available=excluded.available",
                (shop_url, product.key, product.title, product.url, int(time.time()), int(product.available)),
            )

    def subscribe(self, chat_id: str) -> bool:
        existing = self.db.execute(
            "SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        with self.db:
            self.db.execute(
                "INSERT INTO subscribers(chat_id, active, pending_catalog, subscribed_at) "
                "VALUES(?, 1, 1, ?) ON CONFLICT(chat_id) DO UPDATE SET active=1, pending_catalog=1",
                (chat_id, int(time.time())),
            )
        return not bool(existing and existing[0])

    def unsubscribe(self, chat_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE subscribers SET active=0, pending_catalog=0 WHERE chat_id=?", (chat_id,)
            )

    def active_chats(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT chat_id FROM subscribers WHERE active=1")]

    def pending_catalogs(self) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT chat_id FROM subscribers WHERE active=1 AND pending_catalog=1"
            )
        ]

    def available_catalogs(self) -> list[tuple[str, list[tuple[str, str]]]]:
        catalogs = []
        shops = self.db.execute(
            "SELECT shop_url FROM shops WHERE initialized=1 ORDER BY shop_url"
        ).fetchall()
        for (shop_url,) in shops:
            products = self.db.execute(
                "SELECT title, product_url FROM products WHERE shop_url=? AND active=1 "
                "AND available=1 "
                "ORDER BY title COLLATE NOCASE",
                (shop_url,),
            ).fetchall()
            catalogs.append((shop_url, products))
        return catalogs

    def catalog_sent(self, chat_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE subscribers SET pending_catalog=0 WHERE chat_id=?", (chat_id,)
            )

    def get_update_offset(self) -> int:
        row = self.db.execute("SELECT value FROM metadata WHERE key='telegram_update_offset'").fetchone()
        return int(row[0]) if row else 0

    def set_update_offset(self, offset: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('telegram_update_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(offset),),
            )


def load_shops(path: Path) -> list[str]:
    shops = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            shops.append(clean_url(line))
    return list(dict.fromkeys(shops))


def send_telegram(session: requests.Session, token: str, chat_id: str, shop: str, product: Product) -> None:
    host = urlsplit(shop).netloc
    message = (
        "🆕 <b>Nuovo prodotto</b>\n"
        f"<b>{html.escape(product.title)}</b>\n"
        f"Negozio: {html.escape(host)}\n"
        f'<a href="{html.escape(product.url, quote=True)}">Apri il prodotto</a>'
    )
    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    response.raise_for_status()


def send_text(session: requests.Session, token: str, chat_id: str, message: str) -> None:
    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def process_subscriptions(session: requests.Session, store: Store, token: str) -> None:
    response = session.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": store.get_update_offset(), "timeout": 0, "allowed_updates": '["message"]'},
        timeout=20,
    )
    response.raise_for_status()
    for update in response.json().get("result", []):
        store.set_update_offset(int(update["update_id"]) + 1)
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = str(message.get("text") or "").strip().split("@", 1)[0].lower()
        if not chat_id:
            continue
        if text == "/start":
            store.subscribe(chat_id)
            send_text(
                session,
                token,
                chat_id,
                "✅ Iscrizione attivata! Riceverai a breve il catalogo dei prodotti "
                "rilevati e, in seguito, una notifica per ogni nuovo prodotto.\n\n"
                "Usa /stop per annullare l'iscrizione.",
            )
        elif text == "/stop":
            store.unsubscribe(chat_id)
            send_text(session, token, chat_id, "Iscrizione annullata. Non riceverai altre notifiche.")
        elif text in {"/help", "/aiuto"}:
            send_text(session, token, chat_id, "/start – Iscriviti alle notifiche\n/stop – Annulla l'iscrizione")


def send_pending_catalogs(session: requests.Session, store: Store, token: str) -> None:
    for chat_id in store.pending_catalogs():
        for shop_url, products in store.available_catalogs():
            host = urlsplit(shop_url).netloc.removeprefix("www.")
            header = (
                f"🏪 <b>{html.escape(host)}</b>\n"
                f"Disponibili o prenotabili: <b>{len(products)}</b>\n\n"
            )
            if not products:
                body = "Nessun prodotto attualmente disponibile."
            else:
                entries = [
                    f'• <a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'
                    for title, url in products
                ]
                selected: list[str] = []
                for entry in entries:
                    omitted = len(entries) - len(selected) - 1
                    suffix = f"\n\n…e altri {omitted} prodotti." if omitted else ""
                    if len(header) + len("\n".join(selected + [entry])) + len(suffix) > 3900:
                        break
                    selected.append(entry)
                omitted = len(entries) - len(selected)
                body = "\n".join(selected)
                if omitted:
                    body += f"\n\n…e altri {omitted} prodotti disponibili."
            response = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": header + body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            response.raise_for_status()
        store.catalog_sent(chat_id)


def check_once(session: requests.Session, store: Store, shops: list[str], token: str, chat_ids: list[str], dry_run: bool) -> None:
    for shop in shops:
        try:
            products = fetch_products(session, shop)
            if not products:
                logging.warning("Nessun prodotto trovato: %s", shop)
                continue  # Non inizializzare: evitiamo falsi positivi al prossimo tentativo.
            initialized, new = store.record(shop, products)
            if not initialized:
                logging.info("Baseline creata per %s: %d prodotti", shop, len(products))
            elif new:
                logging.info("Trovati %d nuovi prodotti su %s", len(new), shop)
                for product in new:
                    if not product.available:
                        logging.info("Nuovo prodotto non disponibile, memorizzato senza notifica: %s", product.title)
                        store.confirm(shop, product)
                        continue
                    if dry_run:
                        logging.info("DRY RUN: %s - %s", product.title, product.url)
                    else:
                        for recipient in chat_ids:
                            try:
                                send_telegram(session, token, recipient, shop, product)
                            except requests.RequestException as exc:
                                logging.error("Notifica fallita per chat %s: %s", recipient, exc)
                    store.confirm(shop, product)
            else:
                logging.info("Nessuna novità su %s (%d prodotti)", shop, len(products))
        except requests.RequestException as exc:
            logging.error("Errore controllando %s: %s", shop, exc)
        except Exception:
            logging.exception("Errore inatteso controllando %s", shop)


def print_report(session: requests.Session, shops: list[str]) -> None:
    """Stampa un riepilogo leggibile senza modificare la baseline."""
    rows: list[tuple[str, str]] = []
    for shop in shops:
        name = urlsplit(shop).netloc.removeprefix("www.")
        try:
            count = len(fetch_products(session, shop))
            result = str(count) if count else "0 (nessun prodotto rilevato)"
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            result = f"ERRORE HTTP {status}" if status else "ERRORE DI CONNESSIONE"
        rows.append((name, result))

    width = max(len("NEGOZIO"), *(len(name) for name, _ in rows))
    print(f"{'NEGOZIO':<{width}}  PRODOTTI RILEVATI")
    print(f"{'-' * width}  -----------------")
    for name, result in rows:
        print(f"{name:<{width}}  {result}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    dry_run = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"}
    once = "--once" in sys.argv
    report = "--report" in sys.argv
    if not report and not dry_run and not token:
        print("Configura TELEGRAM_BOT_TOKEN nel file .env", file=sys.stderr)
        return 2
    try:
        interval = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "300")))
        shops = load_shops(SHOPS_PATH)
    except (OSError, ValueError) as exc:
        print(f"Configurazione non valida: {exc}", file=sys.stderr)
        return 2
    if not shops:
        print("elenco_negozi.txt non contiene URL", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9,en;q=0.7"})
    if report:
        print_report(session, shops)
        return 0
    store = Store(DB_PATH)
    if owner_chat_id and owner_chat_id not in store.active_chats():
        # Mantiene compatibilità con la configurazione originale del proprietario.
        store.subscribe(owner_chat_id)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        if not dry_run:
            try:
                process_subscriptions(session, store, token)
            except requests.RequestException as exc:
                logging.error("Impossibile leggere i comandi Telegram: %s", exc)
        check_once(session, store, shops, token, store.active_chats(), dry_run)
        if not dry_run:
            try:
                send_pending_catalogs(session, store, token)
            except requests.RequestException as exc:
                logging.error("Invio catalogo non riuscito: %s", exc)
        if once:
            break
        logging.info("Prossimo controllo tra %d secondi", interval)
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
