<?php
/**
 * Load more products automatically while scrolling a product category, with
 * the theme's own system.
 *
 * Archive V4 shows numbered pagination when its load-more setting (prk_option
 * 'archive_v4_load_more_enable') is off, which is how the site was set up. When
 * it is on, it renders its own load-more block in one of two modes
 * ('archive_v4_load_more_mode'): 'button', or 'scroll', where an invisible
 * 1 px trigger is watched by an IntersectionObserver (420 px ahead) and the next
 * batch arrives through the theme's own Ajax, keeping the active filters and
 * sorting. The client wants products to appear on scroll, so this reads those
 * two settings as on / 'scroll'; nothing else in the theme options changes, and
 * no markup, script or style is added. Page URLs such as /page/2/ keep working.
 *
 * Obeys 'archive_infinite' (off / trial / on). To go back to page numbers, set
 * it to 'off'.
 */

defined( 'ABSPATH' ) || exit;

add_filter(
	'option_prk_option',
	function ( $options ) {
		if ( is_array( $options ) && zioma_mode_active( 'archive_infinite' ) && ( ! is_admin() || wp_doing_ajax() ) ) {
			$options['archive_v4_load_more_enable'] = '1';
			$options['archive_v4_load_more_mode']   = 'scroll';
		}
		return $options;
	},
	20
);
