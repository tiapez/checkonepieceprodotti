import unittest

from bot import _products_from_shopify, clean_url, extract_products


class ProductExtractionTests(unittest.TestCase):
    def test_json_ld_and_links_are_deduplicated(self):
        source = '''
        <script type="application/ld+json">
        {"@type":"Product","name":"Box OP-01","url":"/products/box-op-01?utm_source=x"}
        </script>
        <a href="/products/box-op-01">Box OP-01</a>
        <a href="/products/starter-deck"><img alt="Starter Deck"></a>
        '''
        products = extract_products("https://shop.example/collections/one-piece", source)
        self.assertEqual(2, len(products))
        self.assertEqual({"Box OP-01", "Starter Deck"}, {p.title for p in products})

    def test_clean_url_removes_tracking_and_fragment(self):
        self.assertEqual(
            "https://example.com/products/a/?variant=12",
            clean_url("HTTPS://Example.com/products/a/?utm_medium=x&variant=12#details"),
        )

    def test_shopify_catalog(self):
        products = _products_from_shopify(
            {"products": [{"title": "Booster Box", "handle": "booster-box"}]},
            "https://shop.example/collections/one-piece/",
        )
        self.assertEqual("Booster Box", products[0].title)
        self.assertEqual("https://shop.example/products/booster-box", products[0].url)


if __name__ == "__main__":
    unittest.main()
