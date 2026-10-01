# zioma.ir: speed optimization and bug fixing

Mirror of the server files we change on a WordPress + WooCommerce shop (theme: Parskala, with a child theme; cache: LiteSpeed Cache). The parent theme, plugins, uploads and core are not in the repo (see `.gitignore`).

## Client constraints

- The site must look exactly the same on mobile and desktop. SEO must not change.
- Back up before any significant change. Test on the client's test subdomain first.
- Put customizations in mu-plugins, never in the parent theme, so theme updates don't wipe them. The parent theme "پارس کالا" is the active theme, not the child; do not ask to switch themes (theme mods and menus are tied to the active theme).

## Layout

- `public_html/wp-content/mu-plugins/zioma-performance.php`: loader; site code is in `mu-plugins/zioma/`.
  - `performance-config.php`: per-context lists of style/script handles to dequeue, defer or preload. Find handles on the live site with `?zioma_assets=1` (admin only). Compare a page without the changes using `?zioma_perf=off`.
  - `asset-inspector.php`: the `?zioma_assets=1` bar; also records JS errors, failed files, Ajax/fetch requests, server stage timings, slow callbacks and hook-less stretches, outgoing HTTP calls, and copies everything as a text report the user pastes back.
  - `cache-compat.php`: keeps XHR requests out of the LiteSpeed page cache.
  - `lazyload.php`: excludes the main product image and the first product cards from LiteSpeed lazy-load (via `litespeed_media_lazy_img_excludes`).
- `public_html/.htaccess`: our rules sit in the `# BEGIN Zioma` block, outside the WordPress and LiteSpeed blocks.
- `reports/`: audit and progress reports for the client (Persian).
- The GitHub repo is public. Keep secrets in `wp-config.php` as `REMOVED`, and never commit the commercial parent theme, settings exports or HAR files (they can hold keys and session cookies).

## What we know about the live site

- zioma.ir (88.135.68.10, in Iran) resets connections from outside Iran, so this environment cannot open it. Data comes from the user: Lighthouse JSON from their Chrome, inspector reports, screenshots. Analysis: `reports/03-lighthouse-analysis.md`.
- WordPress 7.1.x, WooCommerce 10.9, Elementor 4.1, PHP 8.1.34, theme files under `themes/parskala/app/...`. LiteSpeed page cache works (`x-litespeed-cache: hit` on repeat views); cache misses take 4–11 s TTFB.
- The server cannot reach WordPress.org (plugin installs fail). Query Monitor on a category page (admin, uncached): 11.5 s total, 709 queries taking only 1.0 s, no WP HTTP API calls. So ~10 s is PHP work or raw network/DNS calls outside the WP HTTP API. First inspector report (cooking-equipment, admin, uncached, 34.6 s, 785 queries): plugins_loaded started at 4.4 s, init took 2.2 s, and wp_head→wp_footer (page body render) took 25.2 s. Second report (same page, Query Monitor still on, 20.5 s): OPcache on (99% hit), no persistent object cache; plugin loading ~4.3 s of which ~3.7 s is two hook-less stretches inside RTL-CareUnit (rtl-theme license manager; Storage.php then ORM.php, around options `22f91148…` and `rtl_rsm_localProducts`); init 2.4 s with `Prk\Woocommerce\MyAccountV4\MyAccountV4::register_endpoints` at 0.8 s; body render 12 s with no single stretch over 0.2 s.
- The inspector's SERVER section now shows wall vs CPU time, per-plugin load time, slowest stage-hook callbacks, hook-less stretches over 0.2 s, totals between consecutive hooks, and outgoing HTTP requests.

## Checks

`php -l` every changed PHP file. There is no WordPress install or site access here: the user uploads changes to the host (see `reports/02-upload-and-test.md`) and pastes inspector reports back. Never upload `wp-config.php` from the repo.
