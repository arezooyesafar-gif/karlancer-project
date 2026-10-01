<?php
/**
 * Admin-only diagnostics, used to fill in performance-config.php and to track
 * down front-end bugs. Open any front-end page with ?zioma_assets=1.
 *
 * A collapsed bar at the bottom of the page lists every style and script in
 * print order with its source, file size, inline data size, dependencies and
 * dependents, plus the contexts the page matches. It also records JavaScript
 * errors, console.error calls, files that failed to load and every Ajax/fetch
 * request with its status and LiteSpeed cache header. "Copy report" puts all
 * of it on the clipboard as plain text.
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

add_action(
	'wp_head',
	function () {
		if ( ! zioma_assets_inspecting() ) {
			return;
		}
		// First thing in <head>, and kept out of LiteSpeed/Cloudflare script optimization.
		?>
		<script data-no-optimize="1" data-no-defer="1" data-cfasync="false">
		(function () {
			var log = window.ziomaDebug = { errors: [], requests: [] };

			window.addEventListener('error', function (e) {
				var el = e.target;
				if (el && el !== window && (el.src || el.href)) {
					log.errors.push('failed to load: ' + (el.src || el.href));
				} else {
					log.errors.push((e.message || 'error') + (e.filename ? ' @ ' + e.filename + ':' + e.lineno + ':' + e.colno : ''));
				}
			}, true);
			window.addEventListener('unhandledrejection', function (e) {
				log.errors.push('unhandled rejection: ' + ((e.reason && e.reason.message) || e.reason));
			});

			var consoleError = console.error;
			console.error = function () {
				log.errors.push('console.error: ' + Array.prototype.map.call(arguments, String).join(' '));
				return consoleError.apply(console, arguments);
			};

			function record(method, url, status, type, cache, body) {
				log.requests.push({
					method: String(method || 'GET').toUpperCase(),
					status: status,
					type: String(type || '').split(';')[0],
					litespeed: cache || '',
					kb: Math.round(body.length / 102.4) / 10,
					full_page: /<html[\s>]/i.test(body) ? 'FULL PAGE' : '',
					url: url
				});
			}

			var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
			XMLHttpRequest.prototype.open = function (method, url) {
				this.ziomaRequest = { method: method, url: String(url) };
				return open.apply(this, arguments);
			};
			XMLHttpRequest.prototype.send = function () {
				var xhr = this, info = xhr.ziomaRequest;
				if (info) {
					xhr.addEventListener('loadend', function () {
						var text = ('' === xhr.responseType || 'text' === xhr.responseType) ? xhr.responseText : '';
						record(info.method, info.url, xhr.status || 'failed', xhr.getResponseHeader('content-type'), xhr.getResponseHeader('x-litespeed-cache'), text || '');
					});
				}
				return send.apply(this, arguments);
			};

			if (window.fetch) {
				var fetch = window.fetch;
				window.fetch = function (input, init) {
					var url = String((input && input.url) || input);
					var method = (init && init.method) || (input && input.method) || 'GET';
					return fetch.apply(this, arguments).then(function (response) {
						response.clone().text().then(function (text) {
							record(method, url, response.status, response.headers.get('content-type'), response.headers.get('x-litespeed-cache'), text);
						}, function () {});
						return response;
					}, function (error) {
						record(method, url, 'failed', '', '', '');
						throw error;
					});
				};
			}
		})();
		</script>
		<?php
	},
	0
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
		$box      = 'border:1px solid #ccc;padding:2px 4px;word-break:break-all';
		?>
		<div id="zioma-assets" dir="ltr" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;max-height:50vh;overflow:auto;background:#fff;color:#111;font:12px/1.4 monospace;border-top:2px solid #111;text-align:left">
			<details style="padding:6px 10px">
				<summary style="cursor:pointer;font-weight:bold">
					<?php printf( 'Zioma: %d files, %s KB local (uncompressed)', count( $rows ), esc_html( round( $total_kb, 1 ) ) ); ?>
					| <span id="zioma-live"></span>
					<button type="button" id="zioma-copy" style="margin-left:8px;font:inherit;cursor:pointer">Copy report</button>
				</summary>
				<p>Contexts: <?php echo esc_html( implode( ', ', $contexts ) ); ?></p>
				<table style="border-collapse:collapse;width:100%">
					<tr>
						<?php foreach ( $columns as $column ) : ?>
							<th style="<?php echo esc_attr( $box ); ?>;background:#f3f3f3"><?php echo esc_html( $column ); ?></th>
						<?php endforeach; ?>
					</tr>
					<?php foreach ( $rows as $row ) : ?>
						<tr>
							<?php foreach ( $row as $value ) : ?>
								<td style="<?php echo esc_attr( $box ); ?>"><?php echo esc_html( $value ); ?></td>
							<?php endforeach; ?>
						</tr>
					<?php endforeach; ?>
				</table>
				<p><b>JavaScript errors and failed files</b></p>
				<ol id="zioma-errors"></ol>
				<p><b>Ajax / fetch requests</b></p>
				<ol id="zioma-requests"></ol>
				<textarea id="zioma-report" readonly style="display:none;width:100%;height:12em;font:inherit"></textarea>
			</details>
		</div>
		<script data-no-optimize="1" data-no-defer="1" data-cfasync="false">
		(function () {
			var assets = <?php echo wp_json_encode( $rows, JSON_HEX_TAG | JSON_UNESCAPED_SLASHES ); ?>;
			var contexts = <?php echo wp_json_encode( $contexts, JSON_HEX_TAG ); ?>;
			var log = window.ziomaDebug || { errors: [], requests: [] };

			function request(r) {
				return [r.method, r.status, r.type, r.litespeed, r.kb + ' KB', r.full_page, r.url].join('\t');
			}
			function fill(id, lines) {
				var list = document.getElementById(id);
				list.textContent = '';
				lines.forEach(function (text) {
					var item = document.createElement('li');
					item.textContent = text;
					list.appendChild(item);
				});
			}
			function refresh() {
				document.getElementById('zioma-live').textContent = log.errors.length + ' errors, ' + log.requests.length + ' requests';
				fill('zioma-errors', log.errors);
				fill('zioma-requests', log.requests.map(request));
			}
			refresh();
			setInterval(refresh, 1000);

			document.getElementById('zioma-copy').addEventListener('click', function (e) {
				e.preventDefault();
				var button = this, box = document.getElementById('zioma-report');
				var keys = Object.keys(assets[0] || {});
				var text = ['URL: ' + location.href, 'Contexts: ' + contexts.join(', '), 'Browser: ' + navigator.userAgent, '', 'FILES', keys.join('\t')]
					.concat(assets.map(function (a) { return keys.map(function (k) { return a[k]; }).join('\t'); }))
					.concat(['', 'ERRORS'], log.errors, ['', 'REQUESTS (method, status, type, x-litespeed-cache, size, full page?, url)'], log.requests.map(request))
					.join('\n');
				function showBox() {
					box.parentNode.open = true;
					box.style.display = 'block';
					box.value = text;
					box.select();
					button.textContent = 'Copy the text in the box below';
				}
				if (navigator.clipboard && window.isSecureContext) {
					navigator.clipboard.writeText(text).then(function () { button.textContent = 'Copied'; }, showBox);
				} else {
					showBox();
				}
			});
		})();
		</script>
		<?php
	},
	9999
);
