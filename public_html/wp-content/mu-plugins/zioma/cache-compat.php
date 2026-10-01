<?php
/**
 * Keep XMLHttpRequest calls to front-end URLs out of the page cache.
 *
 * A theme that loads more products with jQuery requests the same URL a normal
 * visit does. If LiteSpeed caches one of those responses and serves it to the
 * other, visitors get a bare fragment (broken layout) or the load-more script
 * gets a whole page and appends it after the footer. The rule in .htaccess
 * stops LiteSpeed from serving a cached page to such requests; this stops their
 * responses from being stored in the first place.
 */

defined( 'ABSPATH' ) || exit;

add_action(
	'init',
	function () {
		if ( is_admin() || empty( $_SERVER['HTTP_X_REQUESTED_WITH'] ) ) {
			return;
		}
		if ( 'xmlhttprequest' !== strtolower( sanitize_text_field( wp_unslash( $_SERVER['HTTP_X_REQUESTED_WITH'] ) ) ) ) {
			return;
		}

		do_action( 'litespeed_control_set_nocache', 'zioma: xhr request' );
		if ( ! headers_sent() ) {
			header( 'X-LiteSpeed-Cache-Control: no-cache' );
		}
	}
);
