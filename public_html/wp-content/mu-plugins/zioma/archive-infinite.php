<?php
/**
 * Load more products automatically while scrolling a product category, with
 * the theme's own system.
 *
 * Archive V4 has two load-more modes in its settings (prk_option
 * 'archive_v4_load_more_mode'): 'button' and 'scroll'. In 'scroll' mode the
 * theme watches its own load-more block with an IntersectionObserver (420 px
 * ahead) and fetches the next batch through its own Ajax, keeping the active
 * filters and sorting; 'scroll' is also the theme's default. The site was set
 * to 'button'. The client wants products to appear on scroll using the theme's
 * own button and Ajax, so this only switches that one setting to 'scroll' when
 * it is read; nothing else in the theme options changes, and no markup, script
 * or style is added.
 *
 * Obeys 'archive_infinite' (off / trial / on). To go back to the button, set
 * it to 'off' (or change the mode in the theme panel and turn this off).
 */

defined( 'ABSPATH' ) || exit;

add_filter(
	'option_prk_option',
	function ( $options ) {
		if ( is_array( $options ) && zioma_mode_active( 'archive_infinite' ) && ( ! is_admin() || wp_doing_ajax() ) ) {
			$options['archive_v4_load_more_mode'] = 'scroll';
		}
		return $options;
	},
	20
);
