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
	'enabled'         => true,

	'tweaks'          => array(
		// Modern browsers render emoji natively; the detection script only adds a request.
		'disable_emojis'      => true,
		// RSD, WLW manifest, generator and shortlink tags in <head>.
		'clean_head'          => true,
		// Stop LiteSpeed lazy-loading the main product image and the first
		// product cards on shop/category pages (see lazyload.php).
		'eager_above_fold'    => true,
		'eager_product_cards' => 4,
	),

	// 'context' => array( 'style-handle', ... )
	'dequeue_styles'  => array(),

	// 'context' => array( 'script-handle', ... )
	'dequeue_scripts' => array(),

	// Script handles to load with defer (WordPress 6.3+ keeps dependency order).
	'defer_scripts'   => array(),

	// Same keys as WordPress's wp_preload_resources filter, plus an optional
	// 'context'. Example:
	// array( 'href' => '/wp-content/themes/parskala/assets/fonts/x.woff2', 'as' => 'font', 'type' => 'font/woff2', 'crossorigin' => 'anonymous' ),
	'preload'         => array(),
);
