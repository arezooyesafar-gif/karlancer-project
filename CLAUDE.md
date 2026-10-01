# zioma.ir: speed optimization and bug fixing

Mirror of the server files we change on a WordPress + WooCommerce shop (theme: Parskala, with a child theme; cache: LiteSpeed Cache). The parent theme, plugins, uploads and core are not in the repo (see `.gitignore`).

## Client constraints

- The site must look exactly the same on mobile and desktop. SEO must not change.
- Back up before any significant change. Test on the client's test subdomain first.
- Put customizations in mu-plugins, never in the parent theme, so theme updates don't wipe them. The parent theme "پارس کالا" is the active theme, not the child; do not ask to switch themes (theme mods and menus are tied to the active theme).

## Layout

- `public_html/wp-content/mu-plugins/zioma-performance.php`: loader; site code is in `mu-plugins/zioma/`.
  - `performance-config.php`: per-context lists of style/script handles to dequeue, defer or preload. Find handles on the live site with `?zioma_assets=1` (admin only). Compare a page without the changes using `?zioma_perf=off`.
  - `asset-inspector.php`: the `?zioma_assets=1` bar; also records JS errors, failed files and Ajax/fetch requests, and copies everything as a text report the user pastes back.
  - `cache-compat.php`: keeps XHR requests out of the LiteSpeed page cache.
- `public_html/.htaccess`: our rules sit in the `# BEGIN Zioma` block, outside the WordPress and LiteSpeed blocks.
- `reports/`: audit and progress reports for the client (Persian).
- The GitHub repo is public. Keep secrets in `wp-config.php` as `REMOVED`, and never commit the commercial parent theme, settings exports or HAR files (they can hold keys and session cookies).

## Checks

`php -l` every changed PHP file. There is no WordPress install or site access here: the user uploads changes to the host (see `reports/02-upload-and-test.md`) and pastes inspector reports back. Never upload `wp-config.php` from the repo.
