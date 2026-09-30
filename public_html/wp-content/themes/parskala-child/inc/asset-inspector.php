<?php
/**
 * Admin-only list of the styles and scripts a page prints, used to fill in
 * performance-config.php. Open any front-end page with ?zioma_assets=1.
 *
 * The panel shows each handle in print order with its source, file size,
 * inline data size, dependencies and the handles that depend on it, plus the
 * contexts the page matches. The same table is logged to the browser console.
 */

defined( 'ABSPATH' ) || exit;

function zioma_assets_inspecting() {
	return isset( $_GET['zioma_assets'] ) && ! is_admin() && current_user_can( 'manage_options' );
}

add_action(
	'init',
	function () {
		if ( zioma_assets_inspecting() ) {
			do_action( 'litespeed_control_set_nocache', 'zioma: asset inspector' );
			nocache_headers();
		}
	}
);

/**
 * Where an asset URL points: array( 'external' ) for another host,
 * array( 'local', $file ) for a file under ABSPATH, array( 'missing' ) otherwise.
 */
function zioma_assets_locate( $src ) {
	$host = wp_parse_url( $src, PHP_URL_HOST );
	if ( $host && wp_parse_url( site_url(), PHP_URL_HOST ) !== $host ) {
		return array( 'external' );
	}

	$path      = (string) wp_parse_url( $src, PHP_URL_PATH );
	$site_path = (string) wp_parse_url( site_url(), PHP_URL_PATH );
	if ( '' !== $site_path && 0 === strpos( $path, $site_path ) ) {
		$path = substr( $path, strlen( $site_path ) );
	}

	$file = realpath( ABSPATH . ltrim( $path, '/' ) );
	$root = realpath( ABSPATH );

	return ( $file && $root && 0 === strpos( $file, $root ) && is_file( $file ) ) ? array( 'local', $file ) : array( 'missing' );
}

/**
 * Rows describing every handle a dependency registry printed on this page.
 */
function zioma_assets_rows( $type, WP_Dependencies $registry ) {
	$printed     = $registry->done;
	$required_by = array();

	foreach ( $printed as $handle ) {
		if ( isset( $registry->registered[ $handle ] ) ) {
			foreach ( $registry->registered[ $handle ]->deps as $dep ) {
				$required_by[ $dep ][] = $handle;
			}
		}
	}

	$rows = array();
	foreach ( $printed as $handle ) {
		if ( ! isset( $registry->registered[ $handle ] ) ) {
			continue;
		}
		$asset = $registry->registered[ $handle ];
		$src   = is_string( $asset->src ) ? $asset->src : '';
		$where = '' === $src ? array( '' ) : zioma_assets_locate( $src );

		$inline = 0;
		foreach ( array( 'before', 'after', 'data' ) as $key ) {
			if ( ! empty( $asset->extra[ $key ] ) ) {
				$inline += strlen( is_array( $asset->extra[ $key ] ) ? implode( '', array_filter( $asset->extra[ $key ], 'is_string' ) ) : (string) $asset->extra[ $key ] );
			}
		}

		$rows[] = array(
			'type'        => $type,
			'handle'      => $handle,
			'src'         => '' === $src ? '(inline only)' : $src,
			'where'       => $where[0],
			'kb'          => isset( $where[1] ) ? round( filesize( $where[1] ) / 1024, 1 ) : '',
			'inline_kb'   => $inline ? round( $inline / 1024, 2 ) : '',
			'footer'      => ! empty( $asset->extra['group'] ) ? 'yes' : '',
			'deps'        => implode( ', ', $asset->deps ),
			'required_by' => isset( $required_by[ $handle ] ) ? implode( ', ', $required_by[ $handle ] ) : '',
		);
	}

	return $rows;
}

add_action(
	'wp_footer',
	function () {
		if ( ! zioma_assets_inspecting() ) {
			return;
		}

		$rows     = array_merge( zioma_assets_rows( 'css', wp_styles() ), zioma_assets_rows( 'js', wp_scripts() ) );
		$contexts = function_exists( 'zioma_perf_contexts' ) ? zioma_perf_contexts() : array();
		$total_kb = array_sum( array_map( 'floatval', wp_list_pluck( $rows, 'kb' ) ) );
		$columns  = array_keys( reset( $rows ) ?: array( 'type' => '' ) );
		?>
		<div id="zioma-assets" dir="ltr" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;max-height:50vh;overflow:auto;background:#fff;color:#111;font:12px/1.4 monospace;border-top:2px solid #111;text-align:left">
			<details open style="padding:6px 10px">
				<summary style="cursor:pointer;font-weight:bold">
					<?php
					printf(
						'Zioma assets: %d handles, %s KB of local files (uncompressed). Contexts: %s',
						count( $rows ),
						esc_html( round( $total_kb, 1 ) ),
						esc_html( implode( ', ', $contexts ) )
					);
					?>
				</summary>
				<table style="border-collapse:collapse;width:100%;margin-top:6px">
					<tr>
						<?php foreach ( $columns as $column ) : ?>
							<th style="border:1px solid #ccc;padding:2px 4px;background:#f3f3f3"><?php echo esc_html( $column ); ?></th>
						<?php endforeach; ?>
					</tr>
					<?php foreach ( $rows as $row ) : ?>
						<tr>
							<?php foreach ( $row as $value ) : ?>
								<td style="border:1px solid #ccc;padding:2px 4px;word-break:break-all"><?php echo esc_html( $value ); ?></td>
							<?php endforeach; ?>
						</tr>
					<?php endforeach; ?>
				</table>
			</details>
		</div>
		<script>console.table(<?php echo wp_json_encode( $rows, JSON_HEX_TAG | JSON_UNESCAPED_SLASHES ); ?>);</script>
		<?php
	},
	9999
);
