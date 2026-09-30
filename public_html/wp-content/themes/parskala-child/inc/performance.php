<?php
/**
 * Front-end asset trimming driven by inc/performance-config.php.
 *
 * Admins can view any page untouched with ?zioma_perf=off, and
 * define( 'ZIOMA_PERF_DISABLED', true ) in wp-config.php turns it all off.
 */

defined( 'ABSPATH' ) || exit;

function zioma_perf_config() {
	static $config = null;

	if ( null === $config ) {
		$config = wp_parse_args(
			require __DIR__ . '/performance-config.php',
			array(
				'enabled'         => true,
				'tweaks'          => array(),
				'dequeue_styles'  => array(),
				'dequeue_scripts' => array(),
				'defer_scripts'   => array(),
				'preload'         => array(),
			)
		);
	}

	return $config;
}

function zioma_perf_active() {
	if ( is_admin() || ( defined( 'ZIOMA_PERF_DISABLED' ) && ZIOMA_PERF_DISABLED ) ) {
		return false;
	}
	if ( empty( zioma_perf_config()['enabled'] ) ) {
		return false;
	}
	if ( isset( $_GET['zioma_perf'] ) && 'off' === $_GET['zioma_perf'] && current_user_can( 'manage_options' ) ) {
		return false;
	}

	return true;
}

/**
 * Contexts the current request belongs to; keys of the config maps.
 */
function zioma_perf_contexts() {
	$contexts = array( 'all' );
	$is_shop  = false;

	if ( is_front_page() ) {
		$contexts[] = 'front_page';
	}

	if ( function_exists( 'is_woocommerce' ) ) {
		if ( is_shop() || is_product_taxonomy() || is_post_type_archive( 'product' ) ) {
			$contexts[] = 'product_archive';
		}
		if ( is_product() ) {
			$contexts[] = 'product';
		}
		if ( is_cart() ) {
			$contexts[] = 'cart';
		}
		if ( is_checkout() ) {
			$contexts[] = 'checkout';
		}
		if ( is_account_page() ) {
			$contexts[] = 'account';
		}
		$is_shop = is_woocommerce() || is_cart() || is_checkout() || is_account_page();
	}

	if ( is_singular( 'post' ) ) {
		$contexts[] = 'post';
	}
	if ( is_home() || is_category() || is_tag() || is_author() || is_date() ) {
		$contexts[] = 'blog_archive';
	}
	if ( is_page() && ! is_front_page() && ! $is_shop ) {
		$contexts[] = 'page';
	}
	if ( is_search() ) {
		$contexts[] = 'search';
	}
	if ( is_404() ) {
		$contexts[] = '404';
	}
	if ( ! $is_shop && ! is_front_page() ) {
		$contexts[] = 'non_woocommerce';
	}

	return $contexts;
}

/**
 * Handles listed in a context => handles map for the current request.
 */
function zioma_perf_handles( $map ) {
	$handles = array();

	foreach ( zioma_perf_contexts() as $context ) {
		if ( ! empty( $map[ $context ] ) ) {
			$handles = array_merge( $handles, (array) $map[ $context ] );
		}
	}

	return array_values( array_unique( $handles ) );
}

add_action(
	'init',
	function () {
		if ( ! zioma_perf_active() ) {
			return;
		}
		$tweaks = zioma_perf_config()['tweaks'];

		if ( ! empty( $tweaks['disable_emojis'] ) ) {
			remove_action( 'wp_head', 'print_emoji_detection_script', 7 );
			remove_action( 'wp_print_styles', 'print_emoji_styles' );
			remove_action( 'wp_enqueue_scripts', 'wp_enqueue_emoji_styles' );
			add_filter( 'emoji_svg_url', '__return_false' );
		}

		if ( ! empty( $tweaks['clean_head'] ) ) {
			remove_action( 'wp_head', 'rsd_link' );
			remove_action( 'wp_head', 'wlwmanifest_link' );
			remove_action( 'wp_head', 'wp_generator' );
			remove_action( 'wp_head', 'wp_shortlink_wp_head', 10 );
		}
	}
);

function zioma_perf_dequeue() {
	// The login page and other screens that never run the main query are left alone.
	if ( ! did_action( 'wp' ) || ! zioma_perf_active() ) {
		return;
	}
	$config = zioma_perf_config();

	foreach ( zioma_perf_handles( $config['dequeue_styles'] ) as $handle ) {
		wp_dequeue_style( $handle );
	}
	foreach ( zioma_perf_handles( $config['dequeue_scripts'] ) as $handle ) {
		wp_dequeue_script( $handle );
	}
}
// Late, and again right before each print, to catch assets enqueued after wp_enqueue_scripts.
add_action( 'wp_enqueue_scripts', 'zioma_perf_dequeue', 9999 );
add_action( 'wp_print_styles', 'zioma_perf_dequeue', 1 );
add_action( 'wp_print_scripts', 'zioma_perf_dequeue', 1 );
add_action( 'wp_print_footer_scripts', 'zioma_perf_dequeue', 1 );

add_action(
	'wp_enqueue_scripts',
	function () {
		if ( ! zioma_perf_active() ) {
			return;
		}
		foreach ( (array) zioma_perf_config()['defer_scripts'] as $handle ) {
			wp_script_add_data( $handle, 'strategy', 'defer' );
		}
	},
	9999
);

add_filter(
	'wp_preload_resources',
	function ( $resources ) {
		if ( ! zioma_perf_active() ) {
			return $resources;
		}
		$contexts = zioma_perf_contexts();

		foreach ( (array) zioma_perf_config()['preload'] as $resource ) {
			$context = isset( $resource['context'] ) ? $resource['context'] : 'all';
			if ( in_array( $context, $contexts, true ) ) {
				unset( $resource['context'] );
				$resources[] = $resource;
			}
		}

		return $resources;
	}
);
