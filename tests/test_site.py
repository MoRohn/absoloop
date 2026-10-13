"""Guards for the static splash page and the web-1 deploy files."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
INDEX = (SITE / "index.html").read_text(encoding="utf-8")
CADDY = (ROOT / "deploy" / "absoloop.caddy").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-web.yml").read_text(encoding="utf-8")


class SiteTests(unittest.TestCase):
    def test_splash_is_one_static_page(self):
        self.assertIn("<title>AbsoLoop — Synergetic Loops</title>", INDEX)
        self.assertIn('href="/styles.css"', INDEX)
        self.assertIn("/assets/absoloop-os.webp", INDEX)
        self.assertIn("/assets/absoloop-os-still.webp", INDEX)
        self.assertIn("prefers-reduced-motion: reduce", INDEX)
        self.assertIn("https://github.com/MoRohn/absoloop", INDEX)
        self.assertIn("https://absoloop.ai/", INDEX)
        self.assertIn('rel="canonical"', INDEX)
        self.assertIn("og:image", INDEX)
        self.assertIn("Skip to content", INDEX)
        self.assertIn('id="board"', INDEX)
        self.assertIn("See it in action", INDEX)
        self.assertIn("/assets/kanban-board.webp", INDEX)
        self.assertIn("/assets/kanban-board-2x.webp", INDEX)
        self.assertIn('loading="lazy"', INDEX)
        self.assertIn("human approval gate", INDEX)
        self.assertIn("No AbsoLoop cloud", INDEX)
        self.assertIn("MIT", INDEX)
        self.assertNotIn("<script src", INDEX)
        self.assertNotIn("googletagmanager", INDEX.lower())
        self.assertNotIn("google-analytics", INDEX.lower())
        self.assertNotIn("cloudflareinsights", INDEX.lower())
        html_pages = sorted(p.name for p in SITE.glob("*.html"))
        self.assertEqual(html_pages, ["404.html", "index.html"])

    def test_assets_exist_and_hero_is_not_the_raw_gif(self):
        hero = SITE / "assets" / "absoloop-os.webp"
        still = SITE / "assets" / "absoloop-os-still.webp"
        self.assertTrue(hero.is_file())
        self.assertTrue(still.is_file())
        self.assertLess(hero.stat().st_size, 2_000_000)
        board = SITE / "assets" / "kanban-board.webp"
        board_2x = SITE / "assets" / "kanban-board-2x.webp"
        self.assertTrue(board.is_file())
        self.assertTrue(board_2x.is_file())
        self.assertLess(board.stat().st_size, 200_000)
        self.assertLess(board_2x.stat().st_size, 250_000)
        css = (SITE / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".showcase-frame", css)
        self.assertTrue((SITE / "assets" / "og.png").is_file())
        self.assertTrue((SITE / "favicon.svg").is_file())
        self.assertTrue((SITE / "styles.css").is_file())
        self.assertIn("prefers-reduced-motion", (SITE / "styles.css").read_text(encoding="utf-8"))
        for font in (
            "syne-latin-800-normal.woff2",
            "figtree-latin-400-normal.woff2",
            "dm-mono-latin-400-normal.woff2",
        ):
            self.assertTrue((SITE / "assets" / "fonts" / font).is_file(), font)

    def test_caddy_block_matches_the_docker_host(self):
        self.assertIn("absoloop.ai, www.absoloop.ai {", CADDY)
        self.assertIn("root * /srv/sites/absoloop.ai", CADDY)
        self.assertIn("encode zstd gzip", CADDY)
        self.assertIn("file_server", CADDY)
        self.assertNotIn("nginx", CADDY.lower())
        self.assertNotIn("/var/www", CADDY)
        self.assertFalse(list(ROOT.glob("**/*nginx*")))
        # No global options block. This snippet must not replace the shared Caddyfile.
        self.assertIsNone(re.search(r"(?m)^\s*\{", CADDY))

    def test_deploy_uses_secrets_and_does_not_hardcode_a_host(self):
        for name in ("WEB1_HOST", "WEB1_USER", "WEB1_SSH_KEY"):
            self.assertIn(name, DEPLOY)
            self.assertIn(f"secrets.{name}", WORKFLOW)
        self.assertIn("/srv/sites/absoloop.ai", DEPLOY)
        self.assertIn("docker exec caddy caddy reload --config /etc/caddy/Caddyfile", DEPLOY)
        self.assertIn("/srv/caddy/Caddyfile", WORKFLOW)
        self.assertNotIn("WEB1_CADDY_BIN", WORKFLOW)
        self.assertNotIn("/var/www/absoloop", DEPLOY)
        self.assertNotIn("2.29.59.0", DEPLOY)
        self.assertNotIn("amazonaws.com", WORKFLOW.lower())
        self.assertNotRegex(WORKFLOW, r"(?im)^\s*(run:|uses:).*(cloudfront|aws\s+s3|s3\s+sync)")
        subprocess.check_call(["bash", "-n", str(ROOT / "deploy" / "deploy.sh")])


if __name__ == "__main__":
    unittest.main()
