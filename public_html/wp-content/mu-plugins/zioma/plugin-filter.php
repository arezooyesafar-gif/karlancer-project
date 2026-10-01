<?php
/**
 * Skip admin-only plugins on page views by visitors.
 *
 * Duplicator Pro is a backup tool with nothing to show on the site, yet
 * loading it (with the RTL-CareUnit license checks it runs) took 8.8 s of a
 * 40 s uncached category page. Plugins listed in 'frontend_skip_plugins' are
 * left out of the active list only for front-end page views and wc-ajax calls;
 * wp-admin, admin-ajax, cron (scheduled backups), REST, XML-RPC, WP-CLI and
 * the login page still load them as before.
 *
 * If code running on such a request saves the active plugin list, the skipped
 * plugins are put back first, so they can never be deactivated by this.
 */

defined( 'ABSPATH' ) || exit;

/**
 * Whether this request is a visitor page view (or wc-ajax call) rather than
 * admin, cron, REST, XML-RPC, CLI or login.
 */
function zioma_is_frontend_view() {
	if ( is_admin() || wp_doing_ajax() || wp_doing_cron() || ( defined( 'WP_CLI' ) && WP_CLI ) || ( defined( 'XMLRPC_REQUEST' ) && XMLRPC_REQUEST ) ) {
		return false;
	}

	$uri = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '';
	if ( preg_match( '#/wp-json(/|$)|[?&]rest_route=|wp-login\.php|wp-cron\.php|xmlrpc\.php#', $uri ) ) {
		return false;
	}

	return true;
}

$zioma_skip_plugins = isset( zioma_perf_config()['frontend_skip_plugins'] ) ? (array) zioma_perf_config()['frontend_skip_plugins'] : array();

if (
	$zioma_skip_plugins
	&& zioma_server_tweaks_active()
	&& zioma_is_frontend_view()
) {
	add_filter(
		'option_active_plugins',
		function ( $plugins ) use ( $zioma_skip_plugins ) {
			return array_values( array_diff( (array) $plugins, $zioma_skip_plugins ) );
		},
		PHP_INT_MAX
	);

	add_filter(
		'pre_update_option_active_plugins',
		function ( $new_value ) use ( $zioma_skip_plugins ) {
			global $wpdb;
			// The stored list, read straight from the database to bypass the filter above.
			$stored = maybe_unserialize( $wpdb->get_var( "SELECT option_value FROM {$wpdb->options} WHERE option_name = 'active_plugins' LIMIT 1" ) );
			$keep   = array_intersect( $zioma_skip_plugins, (array) $stored );

			return array_values( array_unique( array_merge( (array) $new_value, $keep ) ) );
		},
		PHP_INT_MAX
	);
}
unset( $zioma_skip_plugins );
