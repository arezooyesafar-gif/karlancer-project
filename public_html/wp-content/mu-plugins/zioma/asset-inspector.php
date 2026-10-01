<?php
/**
 * Admin-only diagnostics, used to fill in performance-config.php and to track
 * down front-end bugs. Open any front-end page with ?zioma_assets=1.
 *
 * A collapsed bar at the bottom of the page lists every style and script in
 * print order with its source, file size, inline data size, dependencies and
 * dependents, plus the contexts the page matches. It also records JavaScript
 * errors, console.error calls, files that failed to load and every Ajax/fetch
 * request with its status and LiteSpeed cache header. On the server side it
 * times the main WordPress stages, the slowest callbacks on those stages'
 * hooks, every outgoing HTTP request with the plugin or theme file that made
 * it, and every stretch of more than 0.2 s in which no hook fired. "Copy
 * report" puts all of it on the clipboard as plain text.
 */

defined( 'ABSPATH' ) || exit;

function zioma_assets_inspecting() {
	return isset( $_GET['zioma_assets'] ) && ! is_admin() && current_user_can( 'manage_options' );
}

/**
 * Seconds since PHP started handling this request.
 */
function zioma_assets_elapsed() {
	return round( microtime( true ) - $_SERVER['REQUEST_TIME_FLOAT'], 3 );
}

/**
 * CPU seconds (user + system) used since this plugin loaded. Wall time much
 * larger than CPU time means the request was waiting: for a CPU share, on the
 * network, DNS, disk or the database. PHP workers serve many requests, so the
 * process total is offset by its value at load time.
 */
function zioma_assets_cpu() {
	static $start = null;

	$usage = function_exists( 'getrusage' ) ? getrusage() : array();
	if ( ! isset( $usage['ru_utime.tv_sec'] ) ) {
		return 0.0;
	}
	$total = $usage['ru_utime.tv_sec'] + $usage['ru_utime.tv_usec'] / 1e6 + $usage['ru_stime.tv_sec'] + $usage['ru_stime.tv_usec'] / 1e6;
	if ( null === $start ) {
		$start = $total;
	}

	return $total - $start;
}

/**
 * Scheme, host and path of a URL; the query string is dropped because it can
 * carry API keys.
 */
function zioma_assets_safe_url( $url ) {
	$parts = wp_parse_url( $url );
	if ( empty( $parts['host'] ) ) {
		return '(invalid url)';
	}

	return ( isset( $parts['scheme'] ) ? $parts['scheme'] . '://' : '' ) . $parts['host']
		. ( isset( $parts['path'] ) ? $parts['path'] : '' ) . ( isset( $parts['query'] ) ? '?…' : '' );
}

/**
 * First plugin or theme file in the call stack, e.g. "plugins/foo/foo.php:42".
 */
function zioma_assets_http_caller() {
	foreach ( debug_backtrace( DEBUG_BACKTRACE_IGNORE_ARGS ) as $frame ) {
		if ( empty( $frame['file'] ) ) {
			continue;
		}
		$file = wp_normalize_path( $frame['file'] );
		if ( preg_match( '#/wp-(includes|admin)/|/mu-plugins/zioma#', $file ) ) {
			continue;
		}
		$pos = strpos( $file, '/wp-content/' );

		return ( false !== $pos ? substr( $file, $pos + 12 ) : basename( $file ) ) . ':' . $frame['line'];
	}

	return '';
}

function zioma_assets_http_start( $preempt, $args, $url ) {
	$call = array(
		'method' => isset( $args['method'] ) ? $args['method'] : 'GET',
		'url'    => zioma_assets_safe_url( $url ),
		'start'  => zioma_assets_elapsed(),
		'caller' => zioma_assets_http_caller(),
	);

	if ( false !== $preempt ) {
		$call['seconds']                   = 0;
		$call['result']                    = 'answered by a pre_http_request filter';
		$GLOBALS['zioma_server']['http'][] = $call;
	} else {
		$GLOBALS['zioma_server']['pending'][ $url ][] = $call;
	}

	return $preempt;
}

function zioma_assets_http_end( $response, $context, $class, $args, $url ) {
	if ( 'response' !== $context || empty( $GLOBALS['zioma_server']['pending'][ $url ] ) ) {
		return;
	}
	$call            = array_shift( $GLOBALS['zioma_server']['pending'][ $url ] );
	$call['seconds'] = round( zioma_assets_elapsed() - $call['start'], 3 );
	$call['result']  = is_wp_error( $response ) ? 'error: ' . $response->get_error_message() : 'HTTP ' . wp_remote_retrieve_response_code( $response );

	$GLOBALS['zioma_server']['http'][] = $call;
}

/**
 * Readable name of a hook callback, with the file it lives in when known.
 */
function zioma_assets_callback_name( $callback ) {
	if ( $callback instanceof Closure && isset( $GLOBALS['zioma_server']['wrapped'][ spl_object_id( $callback ) ] ) ) {
		$callback = $GLOBALS['zioma_server']['wrapped'][ spl_object_id( $callback ) ];
	}

	try {
		if ( is_string( $callback ) && false === strpos( $callback, '::' ) ) {
			$reflection = new ReflectionFunction( $callback );
			$name       = $callback;
		} elseif ( is_array( $callback ) || is_string( $callback ) ) {
			list( $class, $method ) = is_array( $callback ) ? $callback : explode( '::', $callback, 2 );
			$class                  = is_object( $class ) ? get_class( $class ) : $class;
			$reflection             = new ReflectionMethod( $class, $method );
			$name                   = $class . '::' . $method;
		} elseif ( $callback instanceof Closure ) {
			$reflection = new ReflectionFunction( $callback );
			$name       = 'closure';
		} else {
			return is_object( $callback ) ? get_class( $callback ) : '?';
		}
	} catch ( ReflectionException $e ) {
		return isset( $name ) ? $name : '?';
	}

	$file = wp_normalize_path( (string) $reflection->getFileName() );
	$pos  = strpos( $file, '/wp-content/' );
	$file = false !== $pos ? substr( $file, $pos + 12 ) : basename( $file );

	return $name . ' (' . $file . ':' . $reflection->getStartLine() . ')';
}

/**
 * Callbacks currently running, outermost hook first, e.g.
 * "init@10: Foo::boot (plugins/foo/foo.php:12)".
 */
function zioma_assets_running_callbacks() {
	$running = array_slice( (array) $GLOBALS['wp_current_filter'], 0, -1 );
	$out     = array();

	foreach ( $running as $hook ) {
		if ( 'all' === $hook || empty( $GLOBALS['wp_filter'][ $hook ] ) || ! $GLOBALS['wp_filter'][ $hook ] instanceof WP_Hook ) {
			continue;
		}
		$priority = $GLOBALS['wp_filter'][ $hook ]->current_priority();
		if ( false === $priority || empty( $GLOBALS['wp_filter'][ $hook ]->callbacks[ $priority ] ) ) {
			continue;
		}
		$entries = $GLOBALS['wp_filter'][ $hook ]->callbacks[ $priority ];
		$names   = array_map(
			function ( $entry ) {
				return zioma_assets_callback_name( $entry['function'] );
			},
			array_slice( $entries, 0, 12 )
		);
		if ( count( $entries ) > 12 ) {
			$names[] = '+' . ( count( $entries ) - 12 ) . ' more';
		}
		$out[] = $hook . '@' . $priority . ': ' . implode( ', ', $names );
	}

	return implode( ' > ', $out );
}

/**
 * Hooked to 'all': notes every stretch longer than 0.2 s between two hooks,
 * which is where a slow callback, query loop or blocking network call sits.
 * A slow plugin file shows up as the stretch ending at its plugin_loaded.
 */
function zioma_assets_gap_probe( $hook, $first_arg = null ) {
	static $last_time = null, $last_hook = '';

	$now   = microtime( true );
	$label = in_array( $hook, array( 'plugin_loaded', 'mu_plugin_loaded' ), true ) && is_string( $first_arg )
		? $hook . '(' . basename( dirname( $first_arg ) ) . '/' . basename( $first_arg ) . ')'
		: $hook;

	if ( null !== $last_time ) {
		$pair = $last_hook . ' → ' . $label;
		if ( ! isset( $GLOBALS['zioma_server']['pairs'][ $pair ] ) ) {
			$GLOBALS['zioma_server']['pairs'][ $pair ] = array( 0.0, 0 );
		}
		$GLOBALS['zioma_server']['pairs'][ $pair ][0] += $now - $last_time;
		++$GLOBALS['zioma_server']['pairs'][ $pair ][1];
	}

	if ( null !== $last_time && $now - $last_time > 0.2 ) {
		$GLOBALS['zioma_server']['gaps'][] = array(
			'seconds' => round( $now - $last_time, 2 ),
			'at'      => round( $now - $_SERVER['REQUEST_TIME_FLOAT'], 2 ),
			'between' => $last_hook . ' → ' . $label,
			'running' => zioma_assets_running_callbacks(),
			'fired'   => zioma_assets_http_caller(),
		);
	}

	$last_hook = $label;
	$last_time = microtime( true );
}

/**
 * Runs first on each stage hook and wraps that hook's callbacks so each one
 * is timed. Removal still works because the array keys stay the same.
 */
function zioma_assets_time_callbacks() {
	$hook = current_filter();
	if ( empty( $GLOBALS['wp_filter'][ $hook ] ) || ! $GLOBALS['wp_filter'][ $hook ] instanceof WP_Hook ) {
		return;
	}

	foreach ( $GLOBALS['wp_filter'][ $hook ]->callbacks as $priority => $entries ) {
		if ( PHP_INT_MIN === $priority ) {
			continue;
		}
		foreach ( $entries as $key => $entry ) {
			$original = $entry['function'];

			$wrapper = function ( ...$args ) use ( $original, $hook, $priority ) {
				$start   = microtime( true );
				$result  = call_user_func_array( $original, $args );
				$seconds = microtime( true ) - $start;
				if ( $seconds > 0.05 ) {
					$GLOBALS['zioma_server']['callbacks'][] = array(
						'seconds'  => round( $seconds, 2 ),
						'hook'     => $hook . '@' . $priority,
						'callback' => $original,
					);
				}

				return $result;
			};

			$GLOBALS['zioma_server']['wrapped'][ spl_object_id( $wrapper ) ] = $original;
			$GLOBALS['wp_filter'][ $hook ]->callbacks[ $priority ][ $key ]['function'] = $wrapper;
		}
	}
}

/**
 * Wall and CPU time each plugin file took to load, measured from one
 * plugin_loaded / mu_plugin_loaded to the next.
 */
function zioma_assets_plugin_loaded( $file ) {
	$now = zioma_assets_elapsed();
	$cpu = zioma_assets_cpu();
	$server = &$GLOBALS['zioma_server'];

	$server['plugins'][] = array(
		'plugin'  => basename( dirname( $file ) ) . '/' . basename( $file ),
		'seconds' => round( $now - $server['mark'][0], 2 ),
		'cpu'     => round( $cpu - $server['mark'][1], 2 ),
	);
	$server['mark'] = array( $now, $cpu );
}

// Recorded for every request with the parameter, from as early as a mu-plugin
// can hook in; zioma_assets_inspecting() decides later whether it is shown.
if ( isset( $_GET['zioma_assets'] ) ) {
	zioma_assets_cpu(); // Sets the CPU baseline.
	$GLOBALS['zioma_server'] = array(
		'loaded_at' => zioma_assets_elapsed(),
		'mark'      => array( zioma_assets_elapsed(), zioma_assets_cpu() ),
		'stages'    => array(),
		'http'      => array(),
		'pending'   => array(),
		'gaps'      => array(),
		'callbacks' => array(),
		'plugins'   => array(),
		'pairs'     => array(),
		'wrapped'   => array(),
	);
	add_action( 'all', 'zioma_assets_gap_probe', 10, 2 );
	add_action( 'mu_plugin_loaded', 'zioma_assets_plugin_loaded', PHP_INT_MIN );
	add_action( 'plugin_loaded', 'zioma_assets_plugin_loaded', PHP_INT_MIN );

	foreach ( array( 'plugins_loaded', 'after_setup_theme', 'init', 'wp_loaded', 'wp', 'template_redirect', 'wp_enqueue_scripts', 'wp_head', 'wp_footer' ) as $zioma_hook ) {
		add_action(
			$zioma_hook,
			function () use ( $zioma_hook ) {
				$GLOBALS['zioma_server']['stages'][ $zioma_hook ] = array( zioma_assets_elapsed(), zioma_assets_cpu() );
			},
			PHP_INT_MIN
		);
		add_action( $zioma_hook, 'zioma_assets_time_callbacks', PHP_INT_MIN );
	}
	unset( $zioma_hook );

	add_filter( 'pre_http_request', 'zioma_assets_http_start', PHP_INT_MAX, 3 );
	add_action( 'http_api_debug', 'zioma_assets_http_end', 10, 5 );
}

/**
 * PHP settings that decide how fast PHP itself runs.
 */
function zioma_assets_environment() {
	$parts = array( 'PHP ' . PHP_VERSION );

	$status = function_exists( 'opcache_get_status' ) ? @opcache_get_status( false ) : false; // phpcs:ignore WordPress.PHP.NoSilencedErrors
	if ( is_array( $status ) && ! empty( $status['opcache_enabled'] ) ) {
		$memory  = $status['memory_usage'];
		$parts[] = sprintf(
			'OPcache on (hit rate %.1f%%, %d scripts, %d of %d MB used%s)',
			$status['opcache_statistics']['opcache_hit_rate'],
			$status['opcache_statistics']['num_cached_scripts'],
			$memory['used_memory'] / 1048576,
			( $memory['used_memory'] + $memory['free_memory'] + $memory['wasted_memory'] ) / 1048576,
			empty( $status['cache_full'] ) ? '' : ', FULL'
		);
	} elseif ( is_array( $status ) || ! extension_loaded( 'Zend OPcache' ) ) {
		$parts[] = 'OPcache OFF';
	} else {
		$parts[] = 'OPcache loaded, status hidden (opcache.enable=' . ini_get( 'opcache.enable' ) . ')';
	}

	if ( extension_loaded( 'xdebug' ) ) {
		$parts[] = 'Xdebug LOADED';
	}
	$parts[] = 'object cache: ' . ( wp_using_ext_object_cache() ? 'persistent' : 'none' );
	$parts[] = 'peak memory ' . round( memory_get_peak_usage() / 1048576, 1 ) . ' MB';

	return implode( ' | ', $parts );
}

/**
 * Plain-text lines describing how this page was built on the server.
 */
function zioma_assets_server_lines() {
	if ( empty( $GLOBALS['zioma_server'] ) ) {
		return array();
	}
	$server = $GLOBALS['zioma_server'];
	$lines  = array(
		sprintf( 'Built in %.2f s up to the footer (CPU %.2f s after this plugin loaded), %d database queries', zioma_assets_elapsed(), zioma_assets_cpu(), get_num_queries() ),
		zioma_assets_environment(),
		sprintf(
			'Server tweaks: %s (active: %s) | Asset trims: %s (active: %s)',
			isset( zioma_perf_config()['server_tweaks'] ) ? zioma_perf_config()['server_tweaks'] : 'off',
			zioma_mode_active( 'server_tweaks' ) ? 'yes' : 'no',
			isset( zioma_perf_config()['asset_trims'] ) ? zioma_perf_config()['asset_trims'] : 'off',
			zioma_mode_active( 'asset_trims' ) ? 'yes' : 'no'
		),
		sprintf( 'This plugin loaded at %s s (before that: PHP start, wp-config, WordPress core, drop-ins, earlier mu-plugins)', $server['loaded_at'] ),
	);

	$stages = array();
	foreach ( $server['stages'] as $hook => $at ) {
		$stages[] = sprintf( '%s %.2f s (CPU %.2f)', $hook, $at[0], $at[1] );
	}
	$lines[] = 'Stage start times: ' . implode( ', ', $stages );

	// The stored list, read past any option_active_plugins filter, shows what was skipped.
	$active  = (array) get_option( 'active_plugins', array() );
	$stored  = (array) maybe_unserialize( $GLOBALS['wpdb']->get_var( "SELECT option_value FROM {$GLOBALS['wpdb']->options} WHERE option_name = 'active_plugins' LIMIT 1" ) );
	$skipped = array_diff( $stored, $active );
	$lines[] = 'Active plugins on this request: ' . implode( ', ', array_map( 'dirname', $active ) );
	if ( $skipped ) {
		$lines[] = 'Skipped on this request: ' . implode( ', ', array_map( 'dirname', $skipped ) );
	}

	$plugins = $server['plugins'];
	usort(
		$plugins,
		function ( $a, $b ) {
			return $b['seconds'] <=> $a['seconds'];
		}
	);
	foreach ( $plugins as $plugin ) {
		if ( $plugin['seconds'] >= 0.05 ) {
			$lines[] = sprintf( 'PLUGIN LOAD %s s (CPU %s s) %s', $plugin['seconds'], $plugin['cpu'], $plugin['plugin'] );
		}
	}

	foreach ( $server['pending'] as $calls ) {
		foreach ( $calls as $call ) {
			$call['seconds'] = '?';
			$call['result']  = 'no response recorded';
			$server['http'][] = $call;
		}
	}
	foreach ( $server['http'] as $call ) {
		$lines[] = sprintf( 'HTTP %s %s | at %s s | took %s s | %s | from %s', $call['method'], $call['url'], $call['start'], $call['seconds'], $call['result'], $call['caller'] );
	}
	if ( ! $server['http'] ) {
		$lines[] = 'No outgoing HTTP requests.';
	}

	$slowest_first = function ( $a, $b ) {
		return $b['seconds'] <=> $a['seconds'];
	};

	$callbacks = $server['callbacks'];
	usort( $callbacks, $slowest_first );
	foreach ( array_slice( $callbacks, 0, 15 ) as $call ) {
		$lines[] = sprintf( 'CALLBACK %s s %s %s', $call['seconds'], $call['hook'], zioma_assets_callback_name( $call['callback'] ) );
	}

	$gaps = $server['gaps'];
	usort( $gaps, $slowest_first );
	foreach ( array_slice( $gaps, 0, 15 ) as $gap ) {
		$lines[] = sprintf(
			'SLOW %s s (ended at %s s) between %s | running: %s | next hook fired from %s',
			$gap['seconds'],
			$gap['at'],
			$gap['between'],
			'' === $gap['running'] ? '-' : $gap['running'],
			'' === $gap['fired'] ? '-' : $gap['fired']
		);
	}
	if ( ! $gaps ) {
		$lines[] = 'No stretch over 0.2 s without a hook.';
	}

	// Where the time between consecutive hooks adds up, e.g. inside every product card.
	$pairs = array_filter(
		$server['pairs'],
		function ( $pair ) {
			return $pair[0] >= 0.1;
		}
	);
	uasort(
		$pairs,
		function ( $a, $b ) {
			return $b[0] <=> $a[0];
		}
	);
	foreach ( array_slice( $pairs, 0, 20, true ) as $between => $pair ) {
		$lines[] = sprintf( 'TOTAL %.2f s over %d times between %s', $pair[0], $pair[1], $between );
	}

	return $lines;
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

			function record(method, url, status, type, cache, body, started) {
				log.requests.push({
					method: String(method || 'GET').toUpperCase(),
					status: status,
					ms: Math.round(performance.now() - started),
					type: String(type || '').split(';')[0],
					litespeed: cache || '',
					kb: Math.round(body.length / 102.4) / 10,
					full_page: /<html[\s>]/i.test(body) ? 'FULL PAGE' : '',
					url: url
				});
			}

			var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
			XMLHttpRequest.prototype.open = function (method, url) {
				this.ziomaRequest = { method: method, url: String(url), started: performance.now() };
				return open.apply(this, arguments);
			};
			XMLHttpRequest.prototype.send = function () {
				var xhr = this, info = xhr.ziomaRequest;
				if (info) {
					info.started = performance.now();
					xhr.addEventListener('loadend', function () {
						var text = ('' === xhr.responseType || 'text' === xhr.responseType) ? xhr.responseText : '';
						record(info.method, info.url, xhr.status || 'failed', xhr.getResponseHeader('content-type'), xhr.getResponseHeader('x-litespeed-cache'), text || '', info.started);
					});
				}
				return send.apply(this, arguments);
			};

			if (window.fetch) {
				var fetch = window.fetch;
				window.fetch = function (input, init) {
					var url = String((input && input.url) || input);
					var method = (init && init.method) || (input && input.method) || 'GET';
					var started = performance.now();
					return fetch.apply(this, arguments).then(function (response) {
						response.clone().text().then(function (text) {
							record(method, url, response.status, response.headers.get('content-type'), response.headers.get('x-litespeed-cache'), text, started);
						}, function () {});
						return response;
					}, function (error) {
						record(method, url, 'failed', '', '', '', started);
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
		$server   = zioma_assets_server_lines();
		?>
		<div id="zioma-assets" dir="ltr" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;max-height:50vh;overflow:auto;background:#fff;color:#111;font:12px/1.4 monospace;border-top:2px solid #111;text-align:left">
			<details style="padding:6px 10px">
				<summary style="cursor:pointer;font-weight:bold">
					<?php printf( 'Zioma: built in %s s | %d files, %s KB local (uncompressed)', esc_html( zioma_assets_elapsed() ), count( $rows ), esc_html( round( $total_kb, 1 ) ) ); ?>
					| <span id="zioma-live"></span>
					<button type="button" id="zioma-copy" style="margin-left:8px;font:inherit;cursor:pointer">Copy report</button>
				</summary>
				<p>Contexts: <?php echo esc_html( implode( ', ', $contexts ) ); ?></p>
				<p><b>Server (PHP)</b></p>
				<ol>
					<?php foreach ( $server as $line ) : ?>
						<li><?php echo esc_html( $line ); ?></li>
					<?php endforeach; ?>
				</ol>
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
			var server = <?php echo wp_json_encode( $server, JSON_HEX_TAG | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ); ?>;
			var log = window.ziomaDebug || { errors: [], requests: [] };

			function request(r) {
				return [r.method, r.status, r.ms + ' ms', r.type, r.litespeed, r.kb + ' KB', r.full_page, r.url].join('\t');
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
				var text = ['URL: ' + location.href, 'Contexts: ' + contexts.join(', '), 'Browser: ' + navigator.userAgent, '', 'SERVER']
					.concat(server, ['', 'FILES', keys.join('\t')])
					.concat(assets.map(function (a) { return keys.map(function (k) { return a[k]; }).join('\t'); }))
					.concat(['', 'ERRORS'], log.errors, ['', 'REQUESTS (method, status, time, type, x-litespeed-cache, size, full page?, url)'], log.requests.map(request))
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
