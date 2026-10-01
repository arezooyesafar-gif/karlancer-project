<?php
/**
 * Unserialize large options once per request instead of on every get_option().
 *
 * WordPress keeps autoloaded options serialized and unserializes them on each
 * get_option() call. The theme reads its settings array (prk_option) about
 * 2,800 times per category page, which the inspector measured at 13 s of a
 * 40 s uncached build. This returns the first fully filtered result for the
 * rest of the request; the value itself is unchanged.
 *
 * Only arrays and scalars without objects are kept, so no caller can change
 * what another caller receives. Any add, update or delete of the option drops
 * the stored copy. Runs on front-end pages and Ajax, not in wp-admin screens
 * or the Customizer preview.
 */

defined( 'ABSPATH' ) || exit;

/**
 * Whether a value is safe to hand out repeatedly: no objects anywhere inside.
 */
function zioma_memo_is_plain( $value ) {
	if ( is_object( $value ) ) {
		return false;
	}
	if ( is_array( $value ) ) {
		foreach ( $value as $item ) {
			if ( ! zioma_memo_is_plain( $item ) ) {
				return false;
			}
		}
	}

	return true;
}

function zioma_memo_option( $pre, $option ) {
	static $loading = array();

	if ( false !== $pre || ! empty( $loading[ $option ] ) ) {
		return $pre;
	}
	if ( array_key_exists( $option, $GLOBALS['zioma_option_memo'] ) ) {
		return $GLOBALS['zioma_option_memo'][ $option ];
	}

	// Read it once the normal way, with every option_{$option} filter applied.
	$loading[ $option ] = true;
	$value              = get_option( $option );
	unset( $loading[ $option ] );

	if ( false === $value || ! zioma_memo_is_plain( $value ) ) {
		return $pre;
	}
	$GLOBALS['zioma_option_memo'][ $option ] = $value;

	return $value;
}

$GLOBALS['zioma_option_memo'] = array();

$zioma_memo_options = isset( zioma_perf_config()['memoize_options'] ) ? (array) zioma_perf_config()['memoize_options'] : array();
$zioma_memo_enabled = $zioma_memo_options
	&& zioma_server_tweaks_active()
	&& ( ! is_admin() || wp_doing_ajax() )
	&& ! isset( $_REQUEST['customize_changeset_uuid'] )
	&& ! isset( $_REQUEST['wp_customize'] )
	&& ! isset( $_POST['customized'] );

if ( $zioma_memo_enabled ) {
	foreach ( $zioma_memo_options as $zioma_memo_option ) {
		$zioma_memo_forget = function () use ( $zioma_memo_option ) {
			unset( $GLOBALS['zioma_option_memo'][ $zioma_memo_option ] );
		};
		add_filter( "pre_option_{$zioma_memo_option}", 'zioma_memo_option', PHP_INT_MAX, 2 );
		add_action( "add_option_{$zioma_memo_option}", $zioma_memo_forget );
		add_action( "update_option_{$zioma_memo_option}", $zioma_memo_forget );
		add_action( "delete_option_{$zioma_memo_option}", $zioma_memo_forget );
	}
}
unset( $zioma_memo_options, $zioma_memo_enabled, $zioma_memo_option, $zioma_memo_forget );
