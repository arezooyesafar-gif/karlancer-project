<?php
/**
 * Load chosen third-party scripts on the visitor's first interaction.
 *
 * Google Analytics (gtag.js, 157 KB) loaded during page load on every page.
 * Script tags whose src matches 'delay_scripts' in the config are turned off
 * in the HTML and started on the first scroll, touch, mouse move, key press or
 * click, or after 'delay_timeout' seconds without one. Inline gtag('config')
 * calls still queue into dataLayer at load and are sent once gtag.js arrives,
 * so page views are still recorded. Nothing visible changes.
 */

defined( 'ABSPATH' ) || exit;

/**
 * Rewrites matching <script src> tags to inert placeholders and appends the
 * loader. Returns the HTML unchanged if nothing matched.
 */
function zioma_delay_rewrite( $html, $patterns, $timeout ) {
	$count = 0;
	$html  = preg_replace_callback(
		'#<script\b([^>]*)\bsrc=(["\'])([^"\']+)\2([^>]*)>\s*</script>#i',
		function ( $tag ) use ( $patterns, &$count ) {
			foreach ( $patterns as $pattern ) {
				if ( '' !== $pattern && false !== strpos( $tag[3], $pattern ) ) {
					++$count;
					return '<script type="text/plain" data-zioma-delay-src="' . esc_attr( $tag[3] ) . '"></script>';
				}
			}
			return $tag[0];
		},
		$html
	);

	if ( ! $count ) {
		return $html;
	}

	$loader = '<script data-no-optimize="1" data-no-defer="1" data-cfasync="false">(function(){'
		. 'var done=false,events=["mousemove","mousedown","keydown","touchstart","wheel","scroll"];'
		. 'function load(){if(done){return;}done=true;'
		. 'events.forEach(function(e){removeEventListener(e,load,{passive:true});});'
		. 'document.querySelectorAll("script[data-zioma-delay-src]").forEach(function(old){'
		. 'var s=document.createElement("script");s.src=old.getAttribute("data-zioma-delay-src");s.async=true;'
		. 'old.parentNode.insertBefore(s,old.nextSibling);old.removeAttribute("data-zioma-delay-src");});}'
		. 'events.forEach(function(e){addEventListener(e,load,{passive:true});});'
		. 'setTimeout(load,' . ( (int) $timeout * 1000 ) . ');'
		. '})();</script>';

	// Before </body>, or at the end when another plugin closed this buffer
	// early (before </body> was printed) and the rest of the page follows it.
	$pos = strripos( $html, '</body>' );
	if ( false === $pos ) {
		return $html . $loader;
	}

	return substr( $html, 0, $pos ) . $loader . substr( $html, $pos );
}

add_action(
	'template_redirect',
	function () {
		$config   = zioma_perf_config();
		$patterns = isset( $config['delay_scripts'] ) ? array_filter( (array) $config['delay_scripts'] ) : array();

		if ( ! $patterns || ! zioma_perf_active() || is_feed() || is_customize_preview() || isset( $_GET['elementor-preview'] ) ) {
			return;
		}
		$timeout = isset( $config['delay_timeout'] ) ? max( 1, (int) $config['delay_timeout'] ) : 20;

		ob_start(
			function ( $html ) use ( $patterns, $timeout ) {
				if ( ! is_string( $html ) || false === stripos( $html, '<html' ) ) {
					return $html;
				}
				return zioma_delay_rewrite( $html, $patterns, $timeout );
			}
		);
	},
	PHP_INT_MAX
);
