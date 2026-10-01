<?php
/**
 * What to trim from the front end, and where.
 *
 * Find handles with the asset inspector: open a page while logged in as an
 * admin with ?zioma_assets=1 appended to the URL. The panel also lists which
 * contexts that page matches.
 *
 * Contexts:
 *   all              every front-end page
 *   front_page       the home page
 *   product_archive  shop page, product categories, tags and attributes
 *   product          single product
 *   cart, checkout, account
 *   post             single blog post
 *   blog_archive     blog index, post categories, tags, authors, dates
 *   page             ordinary pages (not home, not WooCommerce pages)
 *   search, 404
 *   non_woocommerce  everything except home and WooCommerce pages
 *
 * Only add a handle after checking the page on desktop and mobile with and
 * without it (compare with ?zioma_perf=off). A dequeued handle still loads
 * if another enqueued script depends on it; the inspector shows those.
 */

defined( 'ABSPATH' ) || exit;

return array(
	'enabled'               => true,

	'tweaks'                => array(
		// Modern browsers render emoji natively; the detection script only adds a request.
		'disable_emojis'      => true,
		// RSD, WLW manifest, generator and shortlink tags in <head>.
		'clean_head'          => true,
		// Stop LiteSpeed lazy-loading the main product image and the first
		// product cards on shop/category pages (see lazyload.php).
		'eager_above_fold'    => true,
		'eager_product_cards' => 4,
	),

	// Server-side tweaks that make uncached pages build faster without
	// changing their output: 'off', 'trial' (only on URLs with ?zioma_trial=1,
	// to compare in the inspector first) or 'on'.
	'server_tweaks'         => 'on',

	// Options unserialized once per request instead of on every get_option()
	// (see options-memo.php). prk_option is the theme's settings array.
	'memoize_options'       => array( 'prk_option' ),

	// Admin-only plugins not loaded on visitor page views and wc-ajax calls;
	// wp-admin, admin-ajax, cron, REST and login still load them (see
	// plugin-filter.php). Plugin folder or folder/file.php.
	'frontend_skip_plugins' => array(
		'duplicator-pro',
		'woocommerce-advanced-bulk-edit',
	),

	// Script URLs (any part of the src) that load on the visitor's first
	// interaction, or after 'delay_timeout' seconds (see delay-scripts.php).
	'delay_scripts'         => array( 'googletagmanager.com/gtag/js' ),
	'delay_timeout'         => 20,

	// Removing the files below: 'off', 'trial' (only with ?zioma_trial=1) or 'on'.
	'asset_trims'           => 'on',

	// Inline critical CSS (see critical-css.php) that pins layout-shifting
	// elements to the state the theme's own scripts settle them into, so the
	// page looks identical from the first paint with no cumulative layout
	// shift. 'off', 'trial' (only with ?zioma_trial=1) or 'on'. Now 'on': the
	// client confirmed on ?zioma_trial=1 that CLS dropped from 1.02 to 0, the
	// filter/sort/search modals still open and nothing moved.
	'critical_css'          => 'on',

	// 'context' => array( 'style-handle', ... )
	// Gutenberg block styles: product and category pages are built with the
	// theme and Elementor, and Lighthouse found 99% of this file unused there.
	'dequeue_styles'        => array(
		'product_archive' => array( 'wp-block-library' ),
		'product'         => array( 'wp-block-library' ),
	),

	// 'context' => array( 'script-handle', ... )
	// Product review and question tabs exist only on product pages, yet the
	// theme loads their scripts (~22 KB compressed) on home and category pages.
	'dequeue_scripts'       => array(
		'front_page'      => array( 'prk-reviews', 'prk-product-questions' ),
		'product_archive' => array( 'prk-reviews', 'prk-product-questions' ),
	),

	// Script handles to load with defer (WordPress 6.3+ keeps dependency order).
	'defer_scripts'         => array(),

	// 'context' => CSS string, printed inline near the top of <head> (see
	// critical-css.php). Each rule must reproduce the element's settled state.
	'critical_css_rules'    => array(
		// The mobile overlay modals (category filter, sort and search, and the
		// product-page modals) are all .prk-modal elements that prk-modal.js
		// builds and hides from the footer. Until that script runs (~24-31 s on
		// a cold mobile load) they stay in the page flow at full height and then
		// collapse to nothing, shoving the footer up — a ~1.0 CLS on category
		// pages, ~0.28 on product pages. This hides a closed modal from the first
		// paint, which is exactly where the script leaves it. prk-modal.js opens
		// a modal with a stronger selector (a state class or an inline style),
		// so this rule only ever matches a modal that is already closed, and an
		// open modal still wins the cascade.
		'all' => 'html .prk-modal{display:none}',

		// Product-page CLS (~0.27 → 0.005, confirmed on ?zioma_trial=1): the
		// reviews and questions lists are rendered in full, then
		// product-gallery.js hides every item past the 4th on DOMContentLoaded
		// (hideExtras: items beyond data-visible-count, default 4, get
		// display:none), which shrinks the tab and, through scroll anchoring,
		// shoves the whole product shell up. This pins the same collapsed state
		// from first paint. It is scoped with the exact flag the script sets when
		// "show more" is tapped (data-expanded="1"), so an expanded list is never
		// hidden and the button keeps working.
		'product' => '#comments-wrap:not([data-expanded="1"]) > .comment:nth-child(n+5){display:none}' . "\n" .
			'#questions-wrap:not([data-expanded="1"]) > .question:nth-child(n+5){display:none}',
	),

	// Rules still being verified: printed only on ?zioma_trial=1, whatever the
	// 'critical_css' switch is, so they can be checked on the live site without
	// affecting ordinary visitors. Move one into 'critical_css_rules' above once
	// it is confirmed on a ?zioma_trial=1 page.
	'critical_css_trial_rules' => array(
		// Mobile home scores 38 only because the main thread spends ~11 s in
		// Style & Layout and ~8 s in Rendering (script eval is only ~2 s): the
		// page's DOM is large and the browser lays all of it out at load, even
		// what is far below the fold. content-visibility:auto tells the browser
		// to skip layout and paint for an off-screen container until it is
		// scrolled near, which cuts that work with no change to how the page
		// looks. contain-intrinsic-size with the 'auto' keyword reserves a
		// placeholder height (and remembers the real one after first render) so
		// nothing jumps. Scoped to the theme-builder footer first — it is on
		// every page and always below the first screen — to measure the win
		// before widening to other below-the-fold sections.
		'all' => 'footer#ag-theme-builder-footer{content-visibility:auto;contain-intrinsic-size:auto 1200px}',
		// Product page: the specification / reviews / questions tab sections are
		// static (no swiper) and sit well below the fold, so skipping their
		// layout and paint until the visitor scrolls to them is safe and cuts
		// the main-thread work with no visual change. (Related products is left
		// out on purpose — it runs a swiper that must measure its real width.)
		'product' => 'section#tab_specifications,section#tab_comments,section#tab_questions{content-visibility:auto;contain-intrinsic-size:auto 900px}',
	),

	// Same keys as WordPress's wp_preload_resources filter, plus an optional
	// 'context'. The two text fonts every page uses, so they download next to
	// the CSS instead of after it (Lighthouse showed them starting ~100 ms
	// after the stylesheets). Same files, so text looks the same.
	'preload'               => array(
		array( 'href' => '/wp-content/themes/parskala/fonts/iranyekan/woff2/IRANYekanX-Regular.woff2', 'as' => 'font', 'type' => 'font/woff2', 'crossorigin' => 'anonymous' ),
		array( 'href' => '/wp-content/themes/parskala/fonts/iranyekan/woff2/IRANYekanX-DemiBold.woff2', 'as' => 'font', 'type' => 'font/woff2', 'crossorigin' => 'anonymous' ),
	),
);
